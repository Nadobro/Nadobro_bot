"""Venue-side protective stop lifecycle — an exchange-enforced backstop to the
software session SL rail.

The software rail (``bot_runtime._evaluate_session_pnl_rail``) polls live PnL and
flattens when the %-of-margin SL is breached; the leverage buffer + fast poll
tighten it. A **venue-side reduce-only trigger order** adds a second line of
defence that fires even if the bot is disconnected or lagging — placed at the
mark price equivalent of the user's SL (``quant/stop_geometry``), refreshed as
the position/entry/leverage change, and cancelled when the run goes flat.

Safety by construction:
  * every order is ``reduce_only`` — the venue guarantees it can only shrink the
    position, so even a wrong price/side can never grow or flip exposure;
  * feature-gated OFF (``NADO_VENUE_STOP_ENABLED``) until validated on testnet;
  * best-effort — every call is wrapped so it can never raise into the rail, and
    the software rail remains the primary stop.

This module owns only the *decision* logic (when to place / replace / cancel);
the actual SDK placement + reduce-only encoding live in
``NadoClient.place_reduce_only_stop`` / ``cancel_trigger_orders``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

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


async def _cancel_tracked(client, tracked: dict) -> None:
    pid = int(tracked.get("product_id") or 0)
    digests = [str(d) for d in (tracked.get("digests") or []) if str(d).strip()]
    if pid <= 0 or not digests:
        return
    try:
        await client.cancel_trigger_orders(product_id=pid, digests=digests)
    except Exception:  # noqa: BLE001 - best-effort; a stale reduce-only stop is harmless (can only reduce)
        logger.debug("venue stop cancel (tracked) failed pid=%s", pid, exc_info=True)


async def cancel_session_venue_stop(client, state: dict, *, product_id: Optional[int] = None) -> bool:
    """Cancel and forget this session's tracked venue stop — call when the run is
    stopped/flattened so no reduce-only trigger lingers. Returns ``True`` if it
    changed ``state`` (caller should persist). No-op when disabled or nothing is
    tracked."""
    if not venue_stop_enabled():
        return False
    tracked = state.get(_STATE_KEY)
    if not tracked:
        return False
    if product_id and not tracked.get("product_id"):
        tracked = {**tracked, "product_id": int(product_id)}
    await _cancel_tracked(client, tracked)
    state.pop(_STATE_KEY, None)
    return True


async def sync_session_venue_stop(client, snap: dict, sl_pct: float, state: dict) -> bool:
    """Place / refresh / cancel the venue-side reduce-only stop to match the live
    position. Idempotent: does nothing when the tracked stop already sits at
    ~the same price+size+side, so a steady position adds no venue writes. Called
    from the session rail's no-stop path (per cycle + per fast-poll). Best-effort
    — never raises; the software rail is the primary stop.

    Returns ``True`` when it changed the tracked state (placed / replaced /
    cancelled), so the caller persists ``state`` — otherwise the tracker is lost
    across polls and every poll re-places (a venue-write storm).
    """
    if not venue_stop_enabled():
        return False
    try:
        pid = int(snap.get("product_id") or 0)
        has_pos = bool(snap.get("has_position")) and abs(_f(snap.get("position_size"))) > 1e-12
        tracked = state.get(_STATE_KEY) or {}

        # Flat / disarmed / no product -> cancel any tracked stop and forget it.
        if pid <= 0 or sl_pct <= 0 or not has_pos:
            if tracked:
                await _cancel_tracked(client, tracked)
                state.pop(_STATE_KEY, None)
                return True
            return False

        is_long = str(snap.get("position_side") or "") == "long"
        stop_price = stop_loss_price(
            is_long, _f(snap.get("entry_price")), _f(snap.get("leverage")), float(sl_pct)
        )
        if stop_price is None:
            return False
        size = abs(_f(snap.get("position_size")))

        # Already placed at ~this price+size+side -> nothing to do (no venue write).
        if (
            tracked
            and bool(tracked.get("is_long")) == is_long
            and _rel_close(tracked.get("stop_price"), stop_price, env_float("NADO_VENUE_STOP_PRICE_TOL", 0.001))
            and _rel_close(tracked.get("size"), size, env_float("NADO_VENUE_STOP_SIZE_TOL", 0.01))
        ):
            return False

        # Replace: cancel the prior tracked stop, then place a fresh one.
        if tracked:
            await _cancel_tracked(client, tracked)

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
