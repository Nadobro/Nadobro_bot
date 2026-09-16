"""Venue TRIGGER-order cleanup for the bot's own sessions (stop / restart).

Why this exists (2026-09-16): the Reverse Grid — standalone ``rgrid`` and D-Grid's
trend phase — arms its entry rungs and its trailing stop as venue PRICE-TRIGGER
orders. Those live on Nado's trigger service, NOT in the resting order book, so
every "cancel the strategy's resting orders" path (the boot stand-down, the
cross-process engine stop, the retriable leftover sweep) left them ARMED:

* an entry rung is NOT reduce-only — it fires on a later price cross and opens a
  position with no controller and no session rail behind it (a redeploy-rule
  violation: boot must stand everything down);
* the reduce-only stop is harmless on its own but keeps counting against the
  venue's 25-pending-triggers-per-product limit for the next run.

The in-process Stop already tears its own triggers down (``on_stop`` cancels by
digest); this module covers every path where no live controller exists, and is
a belt-and-braces sweep after an in-process stop.

PRECISION: the sweep cancels ONLY triggers the bot placed — the digests that
carry an ``order_intents`` row (linked at placement time, see
``engine_persistence.DbTradeRecorder.link_placement``). A user's own manual
TP/SL triggers on the same product have no such row and are never touched.
Best-effort: an unreadable trigger service is reported, never treated as clear.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _pending_digest(row: object) -> Optional[str]:
    """Digest of a still-PENDING ``get_trigger_orders`` row (shared parser with
    the engine adapter so both read the venue's row shape the same way)."""
    from src.nadobro.engine.adapter.nado import _pending_trigger_digest

    return _pending_trigger_digest(row)


def _norm(digest: object) -> str:
    text = str(digest or "").strip().lower()
    if text and not text.startswith("0x"):
        text = "0x" + text
    return text


async def cancel_bot_trigger_orders(
    client: Any, network: str, product_id: int, *, session_id: int | None = None,
    extra_digests: Any = (), exclude_digests: Any = (),
) -> dict:
    """Cancel the bot's own PENDING trigger orders on ``product_id``.

    Lists the account's pending triggers for the product (one trigger-service
    query; a budget-denied or unreadable list is NOT clear) and cancels the ones
    the bot owns: digests the ``order_intents`` registry vouches for (optionally
    narrowed to ``session_id``) UNIONED with ``extra_digests`` — the run's own
    trigger digests persisted as telemetry (``grid_trigger_digests``), so a missed
    placement→session DB link cannot leave a rung armed. ``exclude_digests`` keeps
    the run's protective reduce-only stop when the position is being LEFT open
    (boot stand-down, a failed flatten). Returns ``{"success": bool, "cancelled":
    int, ...}``; ``success`` is False only when the venue could not be read or a
    cancel was rejected — both mean "not confirmed clear", never "clear". Never
    raises.
    """
    pid = int(product_id or 0)
    if client is None or pid <= 0:
        return {"success": True, "cancelled": 0, "skipped": "no client or product"}
    try:
        rows = await client.get_trigger_orders(product_ids=[pid], limit=200, strict=True)
    except Exception as exc:  # noqa: BLE001 - unreadable trigger service: NOT clear
        return {"success": False, "cancelled": 0, "error": f"trigger list unavailable: {exc}"}
    if rows is None:
        return {"success": False, "cancelled": 0, "error": "trigger list unavailable (denied)"}
    pending = [d for d in (_pending_digest(r) for r in (rows or [])) if d]
    if not pending:
        return {"success": True, "cancelled": 0}
    try:
        from src.nadobro.core.async_utils import run_blocking_db
        from src.nadobro.models.database import get_bot_linked_digests

        linked = await run_blocking_db(get_bot_linked_digests, network, pending, session_id=session_id)
    except Exception as exc:  # noqa: BLE001 - registry unreadable: cancel nothing, say so
        return {"success": False, "cancelled": 0, "error": f"intent registry unavailable: {exc}"}
    pending_norm = {_norm(d): d for d in pending}
    own = {_norm(d) for d in (linked or set())}
    own |= {_norm(d) for d in (extra_digests or ()) if _norm(d) in pending_norm}
    keep = {_norm(d) for d in (exclude_digests or ()) if d}
    ours = sorted(pending_norm[d] for d in own if d in pending_norm and d not in keep)
    foreign = len(pending) - len(own)
    if foreign > 0:
        logger.warning(
            "venue trigger sweep network=%s pid=%s session=%s: %s pending trigger(s) the bot "
            "cannot vouch for are left alone (manual TP/SL, or an unlinked placement)",
            network, pid, session_id, foreign,
        )
    if not ours:
        out = {"success": True, "cancelled": 0, "pending_foreign": foreign}
        if keep & set(pending_norm):
            out["kept_protective"] = sorted(keep & set(pending_norm))
        return out
    try:
        res = await client.cancel_trigger_orders(product_id=pid, digests=ours)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "cancelled": 0, "digests": ours, "error": str(exc)}
    ok = bool(isinstance(res, dict) and res.get("success"))
    out = {"success": ok, "cancelled": len(ours) if ok else 0, "digests": ours}
    if keep & set(pending_norm):
        out["kept_protective"] = sorted(keep & set(pending_norm))
    if not ok:
        out["error"] = str((res or {}).get("error") or "cancel rejected") if isinstance(res, dict) else "cancel rejected"
    logger.info(
        "venue trigger sweep network=%s pid=%s session=%s: %s bot trigger(s) pending, cancelled=%s ok=%s",
        network, pid, session_id, len(ours), out["cancelled"], ok,
    )
    return out


async def cancel_session_trigger_orders_for_state(
    client: Any, network: str, state: dict, *, keep_protective: bool = False,
) -> dict:
    """Sweep the bot's trigger orders for the strategy described by ``state``
    (product resolved from ``state['product']``; session from
    ``state['strategy_session_id']`` when present — the sweep is not narrowed to
    the session when it is unknown, so a leftover from an earlier run on the same
    product is cleared too). The run's own persisted digests
    (``grid_trigger_digests``) are unioned in; with ``keep_protective`` the run's
    reduce-only stop (``grid_stop_digest``) is left armed because the position is
    being left open. Only the single-perp strategies own triggers."""
    strategy = str(state.get("strategy") or "").lower()
    if strategy not in ("grid", "rgrid", "dgrid", "mid"):
        return {"success": True, "cancelled": 0, "skipped": "not a single-perp strategy"}
    product = str(state.get("product") or "").strip()
    if not product or product.upper() == "MULTI":
        return {"success": True, "cancelled": 0, "skipped": "no product"}
    try:
        from src.nadobro.config import get_product_id

        pid = get_product_id(product, network=network)
    except Exception:  # policy: degrade-ok(unresolved product -> nothing to scope the sweep to)
        pid = None
    if not pid:
        return {"success": True, "cancelled": 0, "skipped": "unresolved product"}
    sid = state.get("strategy_session_id")
    try:
        sid_int: int | None = int(sid) if sid else None
    except (TypeError, ValueError):
        sid_int = None
    extra = state.get("grid_trigger_digests") or []
    exclude = [state.get("grid_stop_digest")] if (keep_protective and state.get("grid_stop_digest")) else []
    return await cancel_bot_trigger_orders(
        client, network, int(pid), session_id=sid_int,
        extra_digests=[d for d in extra if d], exclude_digests=exclude,
    )
