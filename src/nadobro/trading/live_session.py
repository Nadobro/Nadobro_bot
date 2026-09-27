"""Authoritative live snapshot of a strategy session, sourced from Nado.

This is the single place that defines "what Nado shows for this session". It is
consumed by:

* the safety rails in ``bot_runtime._run_cycle`` — which fire SL/TP off the real
  session PnL (realized + **unrealized**), as a percentage of the configured
  margin;
* the ``/mm_status`` and ``/mm_fills`` dashboards.

PnL convention
--------------
``session_pnl = realized_pnl + unrealized_pnl - funding_paid``

**Unrealized PnL is the LIVE VENUE POSITION uPnL** for the product (the exact
number Nado/Portfolio shows), **baseline-adjusted**: we subtract the uPnL of any
position that already existed when the run started, so the figure reflects only
what THIS run did. This is the fix for the SL that failed to fire — reconstructing
the position from recorded fills undercounted it badly (0.008 vs 0.08 on Nado), so
the session PnL read ~0 while the real position was past the stop. Sourcing uPnL
from the venue position makes the strategy SL agree with Portfolio.

* ``unrealized_pnl`` — ``venue_position_uPnL - baseline_uPnL`` (run-only). When no
  position pre-existed (the common case), this is exactly the venue uPnL.
* ``realized_pnl`` — replayed from this run's fills at the real match price
  (gross of fees: the venue fee is stripped back out of the fee-inclusive
  ``quote_filled``); the recorder cash-flow fallback is used ONLY when the run is
  flat (see ``get_session_live_metrics``). Fees are a standalone metric, never in
  PnL, and ``session_pnl_net`` subtracts them exactly once.
* ``volume`` — real traded turnover on the product since the run started
  (``get_session_turnover``), to match Nado.
* ``funding_paid`` — paid-positive; reduces PnL.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from src.nadobro.quant.portfolio_calculator import derive_unrealized_pnl

logger = logging.getLogger(__name__)

# A positions row older than this (vs its ``synced_at``) is treated as stale and
# the snapshot prefers a direct venue read instead. ~2 portfolio-sync ticks.
_POSITION_STALE_SECONDS = 90.0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_margin(state: Optional[dict], session: Optional[dict]) -> float:
    """Allocated margin the SL/TP percentage is measured against. The user sets
    SL/TP as a % of this (SL 1% of $100 margin => stop at -$1)."""
    state = state or {}
    for key in ("notional_usd", "cycle_notional_usd", "fixed_margin_usd", "session_margin_usd"):
        v = _f(state.get(key), 0.0)
        if v > 0:
            return v
    if session:
        for key in ("notional_usd", "margin_usd", "target_quote"):
            v = _f(session.get(key), 0.0)
            if v > 0:
                return v
    return 0.0


def _session_baseline(session: Optional[dict]) -> tuple[float, float]:
    """Signed position size + avg entry that existed when the run started, so the
    run's PnL excludes a pre-existing/manual position on the same product. Stored
    in ``config_snapshot`` at session creation. (0, 0) when the run began flat."""
    if not session:
        return 0.0, 0.0
    snap = session.get("config_snapshot")
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except Exception:  # noqa: BLE001
            snap = None
    if not isinstance(snap, dict):
        return 0.0, 0.0
    return _f(snap.get("baseline_size")), _f(snap.get("baseline_entry"))


def _aggregate_position_rows(rows: list) -> dict:
    """Net an open product's cross + isolated ``positions`` rows into a single
    venue view (signed size, blended entry from the dominant lot, summed uPnL)."""
    net_signed = 0.0
    upnl = 0.0
    margin_used = 0.0
    dominant = None
    dominant_abs = -1.0
    newest_sync = 0.0
    for r in rows:
        size = abs(_f(r.get("size")))
        side = str(r.get("side") or "").lower()
        signed = size if side == "long" else -size
        net_signed += signed
        # Isolated rows carry est_pnl NULL (the venue's IsolatedPositionMetrics
        # has no PnL field), so this used to add 0.0 and the SL/TP rail was
        # blind to open isolated losses — DN and copy trading both run isolated.
        row_upnl = derive_unrealized_pnl(r)
        upnl += float(row_upnl) if row_upnl is not None else 0.0
        margin_used += _f(r.get("margin_used"))
        newest_sync = max(newest_sync, _f(r.get("synced_ts")))
        if size > dominant_abs:
            dominant_abs = size
            dominant = r
    return {
        "size_signed": net_signed,
        "entry": _f(dominant.get("avg_entry_price")) if dominant else 0.0,
        "liq": _f(dominant.get("est_liq_price")) if dominant else 0.0,
        "leverage": _f(dominant.get("leverage")) if dominant else 0.0,
        "margin_used": margin_used,
        "upnl": upnl,
        "synced_ts": newest_sync,
    }


def _live_position_from_client(client, product_id: int) -> Optional[dict]:
    """Fresh venue position read straight from Nado for one product (best-effort)."""
    if client is None:
        return None
    try:
        positions = client.get_all_positions() or []
    except Exception:  # noqa: BLE001 - display/guard path must never raise
        logger.debug("live position read failed pid=%s", product_id, exc_info=True)
        return None
    net_signed = 0.0
    upnl = 0.0
    dominant = None
    dominant_abs = -1.0
    matched = False
    # get_all_positions() rows carry NO ``unrealized_pnl`` key (they hold
    # amount / signed_amount / entry_price / v_quote_balance), so reading it
    # yielded 0.0 for EVERY position — cross included — whenever the DB row was
    # stale and this fallback drove the SL/TP rail. Derive it from the venue
    # identity instead, pricing at the live mark.
    mark = None
    try:
        quote = client.get_market_price(int(product_id)) or {}
        mark = quote.get("mid") or quote.get("mark") or quote.get("price")
    except Exception:  # noqa: BLE001 - guard path must never raise
        logger.debug("mark price read failed pid=%s", product_id, exc_info=True)
    for p in positions:
        if int(p.get("product_id") or 0) != int(product_id):
            continue
        matched = True
        size = abs(_f(p.get("amount")))
        signed = _f(p.get("signed_amount"), size if str(p.get("side")).upper() == "LONG" else -size)
        net_signed += signed
        row_upnl = derive_unrealized_pnl(p, mark_price=mark)
        upnl += float(row_upnl) if row_upnl is not None else 0.0
        if size > dominant_abs:
            dominant_abs = size
            dominant = p
    if not matched:
        # Nado authoritatively reports no position on this product → flat.
        return {"size_signed": 0.0, "entry": 0.0, "liq": 0.0, "leverage": 0.0,
                "margin_used": 0.0, "upnl": 0.0, "synced_ts": time.time()}
    return {
        "size_signed": net_signed,
        "entry": _f(dominant.get("price")) if dominant else 0.0,
        "liq": _f(dominant.get("liquidation_price")) if dominant else 0.0,
        "leverage": 0.0,
        "margin_used": 0.0,
        "upnl": upnl,
        "synced_ts": time.time(),
    }


def _venue_position(telegram_id: int, network: str, product_id, client) -> dict:
    """The live venue position for ``product_id`` — the SAME source Portfolio
    uses (the nado_sync-maintained ``positions`` table), with a direct client
    read when the DB row is stale or missing. Best-effort; flat on failure."""
    flat = {"size_signed": 0.0, "entry": 0.0, "liq": 0.0, "leverage": 0.0,
            "margin_used": 0.0, "upnl": 0.0, "synced_ts": 0.0}
    if product_id is None:
        return flat
    from src.nadobro.models.database import get_open_position_rows_for_product

    db_view = None
    try:
        rows = get_open_position_rows_for_product(int(telegram_id), network, int(product_id))
        if rows:
            db_view = _aggregate_position_rows(rows)
    except Exception:  # noqa: BLE001
        logger.debug("db position read failed pid=%s", product_id, exc_info=True)
    stale = (db_view is None) or ((time.time() - _f(db_view.get("synced_ts"))) > _POSITION_STALE_SECONDS)
    if stale and client is not None:
        live = _live_position_from_client(client, int(product_id))
        if live is not None:
            return live
    return db_view or flat


def _session_strategy(session: dict, state: Optional[dict]) -> str:
    return str(session.get("strategy") or (state or {}).get("strategy") or "").lower()


def _session_product_ids_for_turnover(session: dict, state: Optional[dict], network: str) -> list[int]:
    """Product ids whose venue turnover belongs to this session.

    Most strategies own one product, stored on ``strategy_sessions.product_id``.
    Delta Neutral is explicitly two-product: spot long + perp short. Its normal
    session-tagged metrics count both legs, but the venue-turnover fallback must
    also scan both product ids or missed executor/back-link fills on the spot leg
    disappear from live volume.
    """
    product_ids: list[int] = []
    seen: set[int] = set()

    def _add(pid) -> None:
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return
        if pid_int in seen:
            return
        seen.add(pid_int)
        product_ids.append(pid_int)

    _add(session.get("product_id"))
    if _session_strategy(session, state) not in {"dn", "delta_neutral", "delta-neutral"}:
        return product_ids

    product = str(session.get("product_name") or (state or {}).get("product") or "").strip()
    candidates = [product]
    if "-" in product:
        candidates.append(product.split("-", 1)[0])
    try:
        from src.nadobro.venue.product_catalog import get_dn_pair

        for candidate in candidates:
            if not candidate:
                continue
            dn_pair = get_dn_pair(candidate, network=network) or {}
            for key in ("spot_product_id", "perp_product_id"):
                _add(dn_pair.get(key))
            if len(product_ids) > 1:
                break
    except Exception:  # noqa: BLE001
        logger.debug("dn turnover product-id resolution failed", exc_info=True)
    return product_ids


def get_live_session_snapshot(
    telegram_id: int,
    network: str,
    session: dict,
    *,
    state: Optional[dict] = None,
    client=None,
    mark: Optional[float] = None,
) -> dict:
    """Live figures for ``session`` (a ``strategy_sessions`` row), scoped to THIS
    run only. Read-only, best-effort, never raises. Blocking — call via
    ``run_blocking``.

    Unrealized PnL + the open position come from the live VENUE position for the
    product (baseline-adjusted to exclude any pre-existing position), so the
    strategy SL/PnL agrees with Portfolio. Realized/fees come from this run's
    own tagged fills (per user + per session); volume is the real turnover on
    the product since the run started.
    """
    from src.nadobro.models.database import (
        count_open_orders_for_product,
        get_session_live_metrics,
        get_session_turnover,
    )

    session = session or {}
    product_id = session.get("product_id")
    session_id = int(session.get("id") or 0)

    # Per-user + per-session realized/fees (never another user's/run's fills).
    metrics = (
        get_session_live_metrics(session_id, network, user_id=int(telegram_id))
        if session_id else {}
    )
    realized = _f(metrics.get("realized_pnl"))
    fees = _f(metrics.get("fees"))
    funding_paid = _f(session.get("total_funding_paid"))

    # Live venue position (authoritative uPnL) + the run's starting baseline.
    pos = _venue_position(telegram_id, network, product_id, client)
    total_size = _f(pos.get("size_signed"))
    total_upnl = _f(pos.get("upnl"))
    baseline_size, baseline_entry = _session_baseline(session)

    # Live mark for valuing the position / deriving entry.
    mark_f = _f(mark, 0.0)
    if mark_f <= 0 and client is not None and product_id is not None:
        try:
            mp = client.get_market_price(int(product_id)) or {}
            mark_f = _f(mp.get("mid"), 0.0)
        except Exception:  # noqa: BLE001 - display/guard path must never raise
            logger.debug("live mark read failed pid=%s", product_id, exc_info=True)
    if mark_f <= 0 and abs(total_size) > 1e-12 and _f(pos.get("entry")) > 0:
        # Derive mark from the venue position: uPnL = size*(mark - entry).
        mark_f = _f(pos.get("entry")) + total_upnl / total_size

    # Baseline-adjusted, run-only position + unrealized. uPnL is additive across
    # lots: run_uPnL = total_uPnL - baseline_uPnL (signed: size*(mark-entry)).
    run_size = total_size - baseline_size
    baseline_upnl = baseline_size * (mark_f - baseline_entry) if (baseline_size and mark_f > 0) else 0.0
    unrealized = total_upnl - baseline_upnl

    session_pnl = realized + unrealized - funding_paid
    margin = _resolve_margin(state, session)
    session_pnl_pct = (session_pnl / margin * 100.0) if margin > 0 else 0.0
    # SLTP-GROSS fix: ``session_pnl`` is intentionally GROSS of fees (it is the
    # number rendered on the status / share cards). The SL/TP rail, however, must
    # judge the user's stop on the *net* economics — otherwise a -1% stop only
    # trips at -1% gross, i.e. the true loss is -1% minus accumulated fees, so the
    # stop fires late by the fee drag (worst on high-turnover grid/vol/DN). Expose
    # a net basis for the rail without changing the displayed gross PnL.
    # EXACTLY ONCE (GRIDFAM-2026-09-27-RAIL-FEE-2X): ``realized`` is replayed at
    # the real match price (the venue quote_filled is net of fee, so the replay
    # strips fee_x18 back out — database._derive_session_realized_pnl) and the
    # cross uPnL is the indexer's price-only est_pnl, so ``fees`` is subtracted
    # here and nowhere else. Before the fix the replay was fee-inclusive and a
    # zero-move round trip read -2 x fees on the rail.
    session_pnl_net = session_pnl - fees
    session_pnl_pct_net = (session_pnl_net / margin * 100.0) if margin > 0 else 0.0

    # Volume = real turnover for THIS run (matches Nado), not the under-counted
    # session-tagged sum. DN owns two products (spot+perp), so aggregate both.
    turnover_volume = 0.0
    turnover_fills = 0
    turnover_product_ids = _session_product_ids_for_turnover(session, state, network)
    for turnover_pid in turnover_product_ids:
        turnover = get_session_turnover(
            int(telegram_id), network, int(turnover_pid),
            session.get("started_at"), session.get("stopped_at"),
        )
        turnover_volume += _f(turnover.get("volume"))
        turnover_fills += int(turnover.get("fills") or 0)
    volume = max(_f(metrics.get("volume")), turnover_volume)
    fills = max(int(metrics.get("fills") or 0), turnover_fills)

    # Position view = the run's own net position (baseline-excluded).
    has_position = abs(run_size) > 1e-12
    position_side = "long" if run_size > 0 else "short" if run_size < 0 else ""
    entry_price = (mark_f - unrealized / run_size) if (run_size and mark_f > 0) else _f(pos.get("entry"))
    position_value = abs(run_size) * mark_f

    # --- open orders for the product (session owns the product during a run) ---
    open_orders = 0
    for open_pid in turnover_product_ids:
        product_open_orders = count_open_orders_for_product(int(telegram_id), network, int(open_pid))
        if product_open_orders == 0 and client is not None:
            try:
                product_open_orders = len(client.get_open_orders(int(open_pid)) or [])
            except Exception:  # noqa: BLE001
                logger.debug("live open-orders read failed pid=%s", open_pid, exc_info=True)
        open_orders += int(product_open_orders or 0)

    return {
        "product_id": product_id,
        "fills": fills,
        "volume": volume,
        "fees": fees,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "funding_paid": funding_paid,
        "open_orders": open_orders,
        "session_pnl": session_pnl,
        "session_pnl_net": session_pnl_net,
        "margin": margin,
        "session_pnl_pct": session_pnl_pct,
        "session_pnl_pct_net": session_pnl_pct_net,
        "mark": mark_f,
        "has_position": has_position,
        "position_size": abs(run_size),
        "position_side": position_side,
        "position_value": position_value,
        "entry_price": entry_price,
        "liq_price": _f(pos.get("liq")),
        "leverage": _f(pos.get("leverage")) or _f((state or {}).get("leverage"), 0.0),
        "margin_used": _f(pos.get("margin_used")),
        "net_base": run_size,
    }
