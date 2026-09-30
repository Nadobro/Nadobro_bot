"""Which venue's screens a user SEES: the /venue selection (Arcus Phase 1).

Venues run IN PARALLEL. ``users.active_venue`` only picks which venue's views
and actions the Telegram UI shows. Nothing here stops, flattens, cancels,
starts or resumes anything on either venue. It also touches no Nado cache
(clients, readonly clients, portfolio snapshots): Nado automation keeps running
on its own whatever the selection.

Every function is SYNC and may hit Postgres. Call it from a coroutine through
``core.async_utils.run_blocking_db`` — except ``peek_active_venue`` and
``last_known_venue``, which never do IO.
"""
from __future__ import annotations

import logging
from typing import Literal

from src.nadobro.core.feature_flags import arcus_enabled_for, arcus_mainnet_enabled
from src.nadobro.db import execute_returning, query_count
from src.nadobro.users.audit_log import record_audit_event
from src.nadobro.users.user_service import _get_cached_user, get_user, invalidate_user_cache
from src.nadobro.utils.venue_scope import (
    ARCUS_MAINNET_SCOPE,
    ARCUS_NETWORK_MAINNET,
    ARCUS_NETWORK_TESTNET,
    VENUE_ARCUS,
    VENUE_NADO,
    VENUES,
    arcus_network_from_db,
    arcus_scope_for,
    parse_arcus_net,
)

logger = logging.getLogger(__name__)

SwitchOutcome = Literal["switched", "unchanged", "not_allowed"]

# The users this PROCESS last saw on the Arcus view (a successful venue read or
# a switch). Consulted ONLY when the venue cannot be read, so a DB blip fails
# closed for a user known to be on Arcus and changes nothing for anyone else.
# Bounded by the Arcus cohort; set.add / discard / ``in`` are atomic under the
# GIL, so the loop and the DB worker threads may share it without a lock.
_last_seen_arcus: set[int] = set()


def _note_venue(uid: int, venue: str) -> None:
    if venue == VENUE_ARCUS:
        _last_seen_arcus.add(uid)
    else:
        _last_seen_arcus.discard(uid)


def last_known_venue(telegram_id: int) -> str:
    """The venue this process last saw for the user: 'arcus' only when it was
    last seen on the Arcus view, else 'nado' (including a user never seen since
    boot). No IO. For the unreadable-venue fallback only — never a substitute
    for a real read."""
    return VENUE_ARCUS if int(telegram_id) in _last_seen_arcus else VENUE_NADO


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
    uid = int(telegram_id)
    user = get_user(uid)
    venue = VENUE_ARCUS if user is not None and getattr(user, "active_venue", None) == VENUE_ARCUS else VENUE_NADO
    _note_venue(uid, venue)
    return venue


def get_active_venue_fresh(telegram_id: int) -> str:
    """``get_active_venue`` from Postgres, never from the cache: the switch
    paths decide on this (a cached row may predate a switch). Raises on a DB
    error."""
    uid = int(telegram_id)
    if uid <= 0:
        # invalidate_user_cache(0) would clear EVERY user's entry.
        raise ValueError(f"invalid telegram_id: {telegram_id!r}")
    invalidate_user_cache(uid)
    return get_active_venue(uid)


def peek_active_venue(telegram_id: int) -> str | None:
    """The venue from the in-process user cache ONLY, or None on a miss.

    Never touches Postgres, so a coroutine may call it directly (the venue gate
    runs on every update, right after the language middleware warmed this
    cache for the same update). A miss means "unknown", never 'nado': the
    caller falls back to ``get_active_venue`` through ``run_blocking_db``."""
    uid = int(telegram_id)
    user = _get_cached_user(uid)
    if user is None:
        return None
    venue = VENUE_ARCUS if getattr(user, "active_venue", None) == VENUE_ARCUS else VENUE_NADO
    _note_venue(uid, venue)
    return venue


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
    _note_venue(uid, venue)
    record_audit_event(uid, "venue_switched", f"{previous}->{venue}")  # never raises
    logger.info("venue_switch telegram_id=%s %s->%s", uid, previous, venue)
    return "switched"


def count_users_on_venue(venue: str) -> int:
    """Boot-time count for gate/`/venue` registration (scope C). It raises on a DB
    error, including a missing column when the Arcus DDL failed. The caller
    picks the policy."""
    venue = _require_venue(venue)
    return query_count("SELECT COUNT(*) FROM users WHERE active_venue = %s", (venue,))


# --- Arcus network mode (``users.arcus_network_mode``; Arcus P3b) ------------------------
# A DIFFERENT domain from Nado's ``network_mode``: switching it never touches Nado
# caches, clients or ``network_mode``, and never starts, stops or resumes anything.


def get_arcus_network_mode(telegram_id: int) -> str:
    """The Arcus network the user sees: 'mainnet' only on an EXACT stored match,
    otherwise 'testnet' (including a user with no row yet). A DB error
    PROPAGATES (DENIED != EMPTY)."""
    user = get_user(int(telegram_id))
    if user is None:
        return ARCUS_NETWORK_TESTNET
    return arcus_network_from_db(getattr(user, "arcus_network_mode", None))


def set_arcus_network_mode(telegram_id: int, mode: str) -> SwitchOutcome:
    """Compare-and-set ``users.arcus_network_mode``. It has no other side effects.

    - ``mode`` must be exactly 'testnet' or 'mainnet' (``parse_arcus_net``) and
      the id positive, else ValueError before any DB access.
    - Switching TO mainnet needs the Arcus cohort AND ``ARCUS_MAINNET_ENABLED``
      (build_decisions #7), else 'not_allowed' without writing. Switching to
      testnet is never gated, so nobody is stranded on mainnet.
    - 'switched' = this call flipped the row; 'unchanged' = already there, a
      concurrent switch won, or no row. The user cache is invalidated ALWAYS
      once the UPDATE is attempted (as ``set_active_venue`` does).
    - Callers must refuse while Arcus automation runs ("Stop, then switch").
    """
    target = parse_arcus_net(mode)
    uid = int(telegram_id)
    if uid <= 0:
        # A falsy id would also make invalidate_user_cache clear EVERY user.
        raise ValueError(f"invalid telegram_id: {telegram_id!r}")
    if arcus_scope_for(target) == ARCUS_MAINNET_SCOPE and not (
        arcus_enabled_for(uid) and arcus_mainnet_enabled()
    ):
        return "not_allowed"
    previous = {ARCUS_NETWORK_TESTNET: ARCUS_NETWORK_MAINNET, ARCUS_NETWORK_MAINNET: ARCUS_NETWORK_TESTNET}[target]
    try:
        row = execute_returning(
            "UPDATE users SET arcus_network_mode = %s "
            "WHERE telegram_id = %s AND arcus_network_mode = %s "
            "RETURNING arcus_network_mode",
            (target, uid, previous),
        )
    finally:
        invalidate_user_cache(uid)
    if not row:
        return "unchanged"
    record_audit_event(uid, "arcus_mode_switched", f"{previous}->{target}")  # never raises
    logger.info("arcus_mode_switch telegram_id=%s %s->%s", uid, previous, target)
    return "switched"


def has_live_arcus_credentials() -> bool:
    """Boot check for ``main.py``: is there any ``active``/``expired`` Arcus
    credential? Raises on a DB error, including a missing table (P1's fail-soft
    DDL); the caller picks the policy. Lives here — not in
    ``users/arcus_credentials.py`` — so the boot path never imports
    ``venue.arcus``."""
    return (
        query_count(
            "SELECT COUNT(*) FROM (SELECT 1 FROM arcus_credentials "
            "WHERE status IN ('active', 'expired') LIMIT 1) t"
        )
        > 0
    )
