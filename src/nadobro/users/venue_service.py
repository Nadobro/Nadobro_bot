"""Which venue's screens a user SEES: the /venue selection (Arcus Phase 1).

Venues run IN PARALLEL. ``users.active_venue`` only picks which venue's views
and actions the Telegram UI shows. Nothing here stops, flattens, cancels,
starts or resumes anything on either venue. It also touches no Nado cache
(clients, readonly clients, portfolio snapshots): Nado automation keeps running
on its own whatever the selection.

Every function is SYNC and may hit Postgres. Call it from a coroutine through
``core.async_utils.run_blocking_db``.
"""
from __future__ import annotations

import logging
from typing import Literal

from src.nadobro.core.feature_flags import arcus_enabled_for
from src.nadobro.db import execute_returning, query_count
from src.nadobro.users.audit_log import record_audit_event
from src.nadobro.users.user_service import _get_cached_user, get_user, invalidate_user_cache
from src.nadobro.utils.venue_scope import VENUE_ARCUS, VENUE_NADO, VENUES

logger = logging.getLogger(__name__)

SwitchOutcome = Literal["switched", "unchanged", "not_allowed"]


def _require_venue(venue: object) -> str:
    """Exact match only: 'ARCUS', 'arcus_mainnet', '' and None are all refused."""
    if not isinstance(venue, str) or venue not in VENUES:
        raise ValueError(f"unknown venue: {venue!r}")
    return venue


def get_active_venue(telegram_id: int) -> str:
    """'arcus' only when the user's row says exactly 'arcus'; otherwise 'nado',
    including a user with no row yet. Served from get_user's 10 s cache, which
    the language middleware has already warmed for the current update. A DB
    error PROPAGATES: an unreadable venue is not 'nado' (DENIED != EMPTY)."""
    user = get_user(int(telegram_id))
    if user is None:
        return VENUE_NADO
    return VENUE_ARCUS if getattr(user, "active_venue", None) == VENUE_ARCUS else VENUE_NADO


def peek_active_venue(telegram_id: int) -> str | None:
    """The venue from the in-process user cache ONLY, or None on a miss.

    Never touches Postgres, so a coroutine may call it directly (the venue gate
    runs on every update, right after the language middleware warmed this
    cache for the same update). A miss means "unknown", never 'nado': the
    caller falls back to ``get_active_venue`` through ``run_blocking_db``."""
    user = _get_cached_user(int(telegram_id))
    if user is None:
        return None
    return VENUE_ARCUS if getattr(user, "active_venue", None) == VENUE_ARCUS else VENUE_NADO


def set_active_venue(telegram_id: int, venue: str) -> SwitchOutcome:
    """Compare-and-set the user's venue. It has no other side effects.

    - An unknown venue (exact match only: 'ARCUS', 'arcus_mainnet' and None are
      all refused) or a non-positive id raises ValueError before any DB access.
    - Switching TO Arcus needs ARCUS_ENABLED and the allowlist. It returns
      'not_allowed' without writing. Switching back to Nado is always allowed,
      so a user can never be stranded.
    - 'switched' means this call flipped the row. 'unchanged' means the row was
      already there, a concurrent switch won, or there is no row.
    - The user cache is invalidated ALWAYS once the UPDATE is attempted, even on
      a miss or a DB error: a CAS miss usually means our cached view was stale.
      Nado client caches are deliberately NOT touched (unlike set_network_mode).
    """
    venue = _require_venue(venue)
    uid = int(telegram_id)
    if uid <= 0:
        # A falsy id would also make invalidate_user_cache clear EVERY user.
        raise ValueError(f"invalid telegram_id: {telegram_id!r}")
    if venue == VENUE_ARCUS and not arcus_enabled_for(uid):
        return "not_allowed"
    previous = VENUE_NADO if venue == VENUE_ARCUS else VENUE_ARCUS
    try:
        row = execute_returning(
            "UPDATE users SET active_venue = %s "
            "WHERE telegram_id = %s AND active_venue = %s "
            "RETURNING active_venue",
            (venue, uid, previous),
        )
    finally:
        invalidate_user_cache(uid)
    if not row:
        return "unchanged"
    record_audit_event(uid, "venue_switched", f"{previous}->{venue}")  # never raises
    logger.info("venue_switch telegram_id=%s %s->%s", uid, previous, venue)
    return "switched"


def count_users_on_venue(venue: str) -> int:
    """Boot-time count for gate/`/venue` registration (scope C). It raises on a DB
    error, including a missing column when the Arcus DDL failed. The caller
    picks the policy."""
    venue = _require_venue(venue)
    return query_count("SELECT COUNT(*) FROM users WHERE active_venue = %s", (venue,))
