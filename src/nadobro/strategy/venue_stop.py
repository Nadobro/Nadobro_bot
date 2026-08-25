"""Venue-side protective stop lifecycle — an exchange-enforced backstop to the
software session SL rail.

The software rail (``bot_runtime._evaluate_session_pnl_rail``) polls live PnL and
flattens when the %-of-margin SL is breached; the leverage buffer + fast poll
tighten it. A **venue-side reduce-only trigger order** adds a second line of
defence that fires even if the bot is disconnected or lagging — placed at the
mark price equivalent of the user's SL (``quant/stop_geometry``), refreshed as
the position/entry/size change, and cancelled when the run goes flat OR ends.

Design decisions that make it safe + consistent:
  * **Same margin basis as the rail.** The stop price is derived from the run's
    *effective* leverage (notional / rail-margin), not the venue-reported
    position leverage, so it fires at ``sl_pct`` of the SAME margin the software
    rail measures. That means when the venue stop fires, the rail also sees a
    ``-sl_pct`` loss on its next look and stands the session down — no
    surprise re-entry (``VENUE-STOP-REENTRY``).
  * **reduce_only always** — the venue guarantees the order can only shrink the
    position, never grow or flip it, so even a wrong price/side is bounded.
  * **Churn-bounded.** A steady position writes nothing (idempotent), and small
    refreshes are deferred by a min-reprice interval — EXCEPT a side flip or a
    position *increase*, which always re-cover immediately (never under-cover a
    growing position).
  * **Orphan-swept.** Cancelled on every session-end path via the tracked digest,
    plus a reconcile-by-product sweep on start for stops a prior run left behind
    (``VENUE-STOP-ORPHAN``).
  * feature-gated OFF (``NADO_VENUE_STOP_ENABLED``) until validated on testnet;
    every call is wrapped so it can never raise into the rail.

This module owns only the *decision* logic; the SDK placement + reduce-only
encoding live in ``NadoClient.place_reduce_only_stop`` /
``get_trigger_orders`` / ``cancel_trigger_orders``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Optional

from src.nadobro.quant.stop_geometry import stop_loss_price
from src.nadobro.utils.env import env_bool, env_float

logger = logging.getLogger(__name__)

_STATE_KEY = "_venue_stop"          # per-session tracking of the placed stop


def venue_stop_enabled() -> bool:
    """Master kill-switch. Default OFF — the venue stop is only armed once the
    integration has been validated against the live trigger service on testnet."""
    return env_bool("NADO_VENUE_STOP_ENABLED", False)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rel_close(a: Any, b: float, tol: float) -> bool:
    """True when ``a`` is within a relative ``tol`` of ``b`` (both positive)."""
    fa = _f(a)
    if fa <= 0 or b <= 0:
        return False
    return abs(fa - b) / b <= tol


def _effective_leverage(snap: dict) -> float:
    """The run's effective leverage against the rail's OWN margin basis:
    ``position_value / margin`` (both from the snapshot). This is the quantity
    that drives uPnL as a %-of-margin, so pricing the stop off it makes the
    venue trigger fire at ``sl_pct`` of the same margin the software rail
    measures. Falls back to the venue-reported leverage when the notional/margin
    aren't both available."""
    mg = _f(snap.get("margin"))
    pv = _f(snap.get("position_value"))
    if mg > 0 and pv > 0:
        return pv / mg
    return _f(snap.get("leverage"))


def _extract_digest(place_result: dict) -> Optional[str]:
    """Best-effort pull of the trigger order's digest from a place response so a
    later replace/cancel can target exactly our stop. Trigger-service payload
    shapes vary; try the common locations. ``None`` when not found (the caller
    then reconciles by product, or leaves the prior stop in place)."""
    resp = place_result.get("response")
    for container in (place_result, resp if isinstance(resp, dict) else {}):
        if not isinstance(container, dict):
            continue
        for key in ("digest", "order_digest"):
            v = container.get(key)
            if v:
                return str(v)
        data = container.get("data")
        if isinstance(data, dict):
            for key in ("digest", "order_digest"):
                v = data.get(key)
                if v:
                    return str(v)
    return None


def _row_is_reduce_only_stop(row: dict) -> bool:
    """Best-effort: does a ``get_trigger_orders`` row look like one of OUR
    reduce-only stop triggers (reduce_only + a mid-price trigger)? Conservative —
    returns False when the row doesn't clearly expose it, so the reconcile sweep
    never cancels a trigger it can't identify. (Validate the row shape on
    testnet and tighten this if needed.)"""
    if not isinstance(row, dict):
        return False
    ro = row.get("reduce_only")
    if ro is None:
        ro = row.get("reduceOnly")
    if ro is not True:
        return False
    blob = str(row).lower()
    return "mid_price_below" in blob or "mid_price_above" in blob


async def _cancel_digests(client, product_id: int, digests: Iterable[str]) -> None:
    ds = [str(d) for d in (digests or []) if str(d).strip()]
    if product_id <= 0 or not ds:
        return
    try:
        await client.cancel_trigger_orders(product_id=int(product_id), digests=ds)
    except Exception:  # noqa: BLE001 - best-effort; a stale reduce-only stop can only reduce
        logger.debug("venue stop cancel failed pid=%s", product_id, exc_info=True)


async def reconcile_venue_stops(
    client, product_id: int, *, keep_digests: Iterable[str] = ()
) -> int:
    """Orphan sweep: cancel OUR resting reduce-only stop triggers for
    ``product_id`` (except ``keep_digests``). Call on session start to clear
    stops a prior run left behind (the in-``state`` tracker is wiped on start).
    Best-effort; returns the count cancelled. No-op when disabled."""
    if not venue_stop_enabled() or int(product_id or 0) <= 0:
        return 0
    try:
        rows = await client.get_trigger_orders(product_ids=[int(product_id)])
    except Exception:  # noqa: BLE001
        logger.debug("venue stop reconcile: list failed pid=%s", product_id, exc_info=True)
        return 0
    keep = {str(d) for d in (keep_digests or [])}
    orphans: list[str] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        dig = str(r.get("digest") or r.get("order_digest") or "").strip()
        if not dig or dig in keep:
            continue
        if _row_is_reduce_only_stop(r):
            orphans.append(dig)
    if orphans:
        await _cancel_digests(client, int(product_id), orphans)
    return len(orphans)


async def cancel_session_venue_stop(
    client, state: dict, *, product_id: Optional[int] = None, reconcile: bool = True
) -> bool:
    """Cancel and forget this session's tracked venue stop — call on EVERY
    session-end path (SL/TP fired, duration cap, manual stop, stale-session) so
    no reduce-only trigger lingers. Cancels the tracked digest and, when
    ``reconcile``, also sweeps any of our reduce-only stops still resting on the
    product (covers a lost/mis-extracted digest). Returns ``True`` if it changed
    ``state``. No-op when disabled or nothing to do."""
    if not venue_stop_enabled():
        return False
    tracked = state.get(_STATE_KEY) or {}
    pid = int(tracked.get("product_id") or (product_id or 0))
    tracked_digests = [str(d) for d in (tracked.get("digests") or []) if str(d).strip()]
    await _cancel_digests(client, pid, tracked_digests)
    if reconcile and pid > 0:
        # Sweep anything we couldn't target by digest (e.g. digest not extracted).
        await reconcile_venue_stops(client, pid, keep_digests=())
    changed = _STATE_KEY in state
    state.pop(_STATE_KEY, None)
    return changed


async def sync_session_venue_stop(
    client, snap: dict, sl_pct: float, state: dict, *, now: Optional[float] = None
) -> bool:
    """Place / refresh / cancel the venue-side reduce-only stop to match the live
    position. Called from the session rail's no-stop path (per cycle + fast poll).
    Best-effort — never raises; the software rail is the primary stop.

    Idempotent (no venue write) when the tracked stop already sits at ~the same
    price+size+side. A refresh is deferred by ``NADO_VENUE_STOP_MIN_REPRICE_SECONDS``
    to bound churn on high-fill sessions — EXCEPT a side flip or a position
    *increase*, which always re-cover immediately so the stop never under-covers.

    Returns ``True`` when it changed the tracked state (placed / replaced /
    cancelled), so the caller persists ``state``.
    """
    if not venue_stop_enabled():
        return False
    try:
        _now = time.time() if now is None else float(now)
        pid = int(snap.get("product_id") or 0)
        has_pos = bool(snap.get("has_position")) and abs(_f(snap.get("position_size"))) > 1e-12
        tracked = state.get(_STATE_KEY) or {}

        # Flat / disarmed / no product -> cancel any tracked stop and forget it.
        if pid <= 0 or sl_pct <= 0 or not has_pos:
            if tracked:
                await _cancel_digests(client, int(tracked.get("product_id") or pid), tracked.get("digests") or [])
                state.pop(_STATE_KEY, None)
                return True
            return False

        # One-time per-session orphan sweep: clear reduce-only stops a PRIOR run
        # left resting on this product (the in-state tracker is wiped on start, so
        # a stop left by a manual stop / duration cap / crash can't be cancelled by
        # digest later). Runs once, the first time we manage a stop this session.
        if not state.get("_venue_stop_reconciled"):
            await reconcile_venue_stops(
                client, pid, keep_digests=[str(d) for d in (tracked.get("digests") or [])]
            )
            state["_venue_stop_reconciled"] = True

        is_long = str(snap.get("position_side") or "") == "long"
        # Price the stop off the run's EFFECTIVE leverage (notional / rail-margin),
        # so it fires at sl_pct of the SAME margin the software rail measures.
        stop_price = stop_loss_price(
            is_long, _f(snap.get("entry_price")), _effective_leverage(snap), float(sl_pct)
        )
        if stop_price is None:
            return False
        size = abs(_f(snap.get("position_size")))

        price_tol = env_float("NADO_VENUE_STOP_PRICE_TOL", 0.001)
        size_tol = env_float("NADO_VENUE_STOP_SIZE_TOL", 0.01)
        same = (
            tracked
            and bool(tracked.get("is_long")) == is_long
            and _rel_close(tracked.get("stop_price"), stop_price, price_tol)
            and _rel_close(tracked.get("size"), size, size_tol)
        )
        if same:
            return False  # idempotent — nothing to do

        # A refresh is warranted. Bound churn: defer non-urgent refreshes within
        # the min-reprice interval. Urgent = side flip or a position INCREASE
        # (must re-cover the grown size); those always reprice now.
        side_flipped = bool(tracked) and bool(tracked.get("is_long")) != is_long
        size_increased = bool(tracked) and size > _f(tracked.get("size")) * (1.0 + size_tol)
        urgent = side_flipped or size_increased or not tracked
        min_reprice = env_float("NADO_VENUE_STOP_MIN_REPRICE_SECONDS", 20.0)
        if (not urgent) and (_now - _f(tracked.get("placed_ts")) < min_reprice):
            return False  # defer: over-coverage is safe (reduce_only clamps to live size)

        # Replace: cancel the prior tracked stop, then place a fresh one.
        if tracked:
            await _cancel_digests(client, int(tracked.get("product_id") or pid), tracked.get("digests") or [])

        res = await client.place_reduce_only_stop(
            product_id=pid,
            close_size=size,
            stop_price=float(stop_price),
            position_is_long=is_long,
            slippage_pct=env_float("NADO_VENUE_STOP_SLIPPAGE_PCT", 0.5),
        )
        if res.get("success"):
            digest = _extract_digest(res)
            state[_STATE_KEY] = {
                "product_id": pid,
                "is_long": is_long,
                "stop_price": float(stop_price),
                "size": size,
                "placed_ts": _now,
                "digests": [digest] if digest else [],
            }
            return True
        # Placement failed — drop the tracker so the next poll retries rather than
        # assuming a stop is resting on the venue.
        if tracked:
            state.pop(_STATE_KEY, None)
            return True
        return False
    except Exception:  # noqa: BLE001 - venue stop is a backstop; never break the rail
        logger.debug("venue stop sync skipped", exc_info=True)
        return False
