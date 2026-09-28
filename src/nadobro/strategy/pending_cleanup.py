"""Pending stop cleanups — the record of stops whose venue half is unconfirmed.

Every stop clears ``running`` first (so no cycle re-places orders) and then runs
its venue half: engine stop, order cancel / flatten, venue-trigger sweep. When
that half is not CONFIRMED — a rate-limited or unreadable book (DENIED !=
EMPTY), a crash mid-stop — orders can be left resting with nothing driving them.

* ``begin`` records an entry BEFORE the venue half runs and ``end`` deletes it
  only once the venue half is confirmed, so a crash or an unconfirmed cleanup
  leaves it pending (fail-closed);
* ``record_failed`` covers a stop that learns of the failure after the fact;
* ``bot_runtime.retry_pending_cleanups`` re-runs a CANCEL-ONLY sweep scoped to
  the products recorded here and deletes each entry it confirms. It never
  flattens: a position an unconfirmed flatten may have left is only reported
  (``flatten_unconfirmed``) for the user to close.

Entries are their own ``bot_state`` rows — never part of the run's state, which
a new Start rebuilds from scratch — one per stopped run:
``strategy_cleanup:{uid}:{network}:{strategy}:{product}:{run_id}``. Stop,
/stop_all and the network switch retry them, and the switch refuses to leave a
network while one is unconfirmed (``strategy/network_switch.py``).

Import-light on purpose (DB/config imports are lazy): tests swap ``_STORE``.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

PREFIX = "strategy_cleanup:"
IN_PROGRESS = "in_progress"
FAILED = "failed"

PERP_STRATEGIES = ("grid", "rgrid", "dgrid", "mid")
# Stops whose leftovers can be scoped to known products (desk plans resolve
# theirs from the plan's market). Nothing else is recorded: an entry that can
# never be retried would only lock the network switch.
SCOPED_STRATEGIES = (*PERP_STRATEGIES, "vol", "dn", "desk")
# What the bot-trigger sweep reads (venue_triggers.cancel_session_trigger_orders_for_state).
_TRIGGER_STATE_KEYS = ("strategy_session_id", "grid_trigger_digests", "grid_stop_digest")


class _BotStateStore:
    """Entries are plain ``bot_state`` rows."""

    def put(self, key: str, value: dict) -> None:
        from src.nadobro.models.database import set_bot_state

        set_bot_state(key, value)

    def delete(self, key: str) -> None:
        from src.nadobro.db import execute

        execute("DELETE FROM bot_state WHERE key = %s", (key,))

    def scan(self, prefix: str) -> list[tuple[str, Any]]:
        from src.nadobro.db import query_all

        rows = query_all("SELECT key, value FROM bot_state WHERE key LIKE %s", (prefix + "%",))
        # LIKE treats "_" as a wildcard — keep exact-prefix keys only.
        return [
            (str(r.get("key")), r.get("value"))
            for r in rows or []
            if str(r.get("key") or "").startswith(prefix)
        ]


_STORE: Any = _BotStateStore()


@dataclass
class Pending:
    key: str
    entry: dict
    written: bool = False


def _prefix(telegram_id: int, network: str) -> str:
    return f"{PREFIX}{int(telegram_id)}:{network}:"


def product_ids_for(strategy: str, product: str, network: str, *, market: str | None = None) -> list[int] | None:
    """The venue products a stopped run can have left orders on. ``None`` when
    any is unresolvable — never widen to an unscoped cancel."""
    product = str(product or "").strip()
    if not product or product.upper() == "MULTI":
        return None
    try:
        from src.nadobro.config import (
            get_dn_pair,
            get_product_id,
            get_spot_product_id,
            normalize_volume_spot_symbol,
        )

        if strategy in PERP_STRATEGIES or (strategy == "desk" and market != "spot"):
            pid = get_product_id(product, network=network)
            return [int(pid)] if pid is not None else None
        if strategy in ("vol", "desk"):
            name = normalize_volume_spot_symbol(product) if strategy == "vol" else product
            pid = get_spot_product_id(name, network=network)
            return [int(pid)] if pid is not None else None
        if strategy == "dn":
            pair = get_dn_pair(product, network=network) or {}
            legs = [pair.get("spot_product_id"), pair.get("perp_product_id")]
            return [int(p) for p in legs] if all(p is not None for p in legs) else None
    except Exception:  # policy: degrade-ok(unresolved -> None; the retry reports it, never widens the cancel)
        logger.debug("pending cleanup: product ids unresolved strategy=%s product=%s", strategy, product, exc_info=True)
    return None


def _new(
    telegram_id: int, network: str, state: dict, reason: str, *,
    product_ids: Optional[list[int]] = None, market: str | None = None, run_id: str | None = None,
) -> Pending | None:
    from src.nadobro.strategy.strategy_registry import normalize_strategy_id

    raw = str(state.get("strategy") or "").strip().lower()
    strategy = raw if raw == "desk" else normalize_strategy_id(raw)
    if strategy not in SCOPED_STRATEGIES:
        return None
    product = str(state.get("product") or "").upper()
    rid = str(run_id or state.get("strategy_session_id") or f"t{int(time.time() * 1000)}")
    entry = {
        "strategy": strategy,
        "product": product,
        "market": market,
        # Resolved lazily by the retry (``product_ids_for``) unless the caller
        # already holds them — a stop never pays a catalog lookup for its record.
        "product_ids": list(product_ids) if product_ids else None,
        "run_id": rid,
        "reason": reason,
        "status": IN_PROGRESS,
        "at": time.time(),
        "error": None,
        "flatten_unconfirmed": False,
        "trigger_state": {k: state.get(k) for k in _TRIGGER_STATE_KEYS if state.get(k) is not None},
    }
    return Pending(f"{_prefix(telegram_id, network)}{strategy}:{product}:{rid}", entry)


def begin(telegram_id: int, network: str, state: dict, reason: str, **kw: Any) -> Pending | None:
    """Record the stop BEFORE its venue half runs. ``None`` when the strategy has
    no scoped retry. Never raises: if the write fails the stop goes on and
    ``end`` writes the entry should the venue half fail."""
    pending = _new(telegram_id, network, state, reason, **kw)
    if pending is None:
        return None
    try:
        _STORE.put(pending.key, pending.entry)
        pending.written = True
    except Exception:  # noqa: BLE001 - see docstring; end() retries the write on failure
        logger.warning("pending cleanup: could not record %s before the stop", pending.key, exc_info=True)
    return pending


def end(pending: Pending | None, *, ok: bool, error: str | None = None, flatten_unconfirmed: bool = False) -> None:
    """Confirmed -> delete the entry; otherwise keep it pending with the error.
    Never raises (the caller already reports an unconfirmed stop)."""
    if pending is None:
        return
    try:
        if ok:
            _STORE.delete(pending.key)
            return
        pending.entry = dict(
            pending.entry,
            status=FAILED,
            error=str(error or "cleanup not confirmed")[:240],
            flatten_unconfirmed=bool(flatten_unconfirmed),
            at=time.time(),
        )
        _STORE.put(pending.key, pending.entry)
        pending.written = True
    except Exception:  # noqa: BLE001 - logged loudly; the stop's own result already says "not confirmed"
        logger.warning("pending cleanup: could not update %s (ok=%s)", pending.key, ok, exc_info=True)


def record_failed(
    telegram_id: int, network: str, state: dict, reason: str, error: str, *,
    flatten_unconfirmed: bool = False, **kw: Any,
) -> None:
    """Record a stop that learned its venue half failed after the fact."""
    end(_new(telegram_id, network, state, reason, **kw), ok=False, error=error,
        flatten_unconfirmed=flatten_unconfirmed)


def list_entries(telegram_id: int, network: str) -> list[tuple[str, dict]]:
    """Every pending cleanup on ``network``. RAISES when the store cannot be read
    (an unreadable list is never "nothing pending"). An unparsable row is kept
    as an empty entry, which the retry reports as unconfirmed."""
    out: list[tuple[str, dict]] = []
    for key, raw in _STORE.scan(_prefix(telegram_id, network)):
        try:
            entry = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # policy: degrade-ok(kept as an unscopable entry -> reported unconfirmed)
            entry = None
        out.append((key, entry if isinstance(entry, dict) else {}))
    return out


def delete(key: str) -> None:
    _STORE.delete(key)
