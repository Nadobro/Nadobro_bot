"""Phase 3: pre-trade card + live MM status renderer.

Both renderers share fee / margin / participation math so the pre-trade preview
and the live ``/mm_status`` dashboard stay numerically consistent. Numbers
sourced from Nado endpoints only — no CMC, no CoinGecko.
"""

from __future__ import annotations

from typing import Optional

from src.nadobro.config import (
    NADO_BUILDER_FEE_RATE_1_BPS,
    get_product_max_leverage,
)
from src.nadobro.quant import pov_engine
from src.nadobro.venue.product_catalog import (
    get_product_maker_fee_rate,
    get_product_min_quote_notional_usd,
    get_product_taker_fee_rate,
)
from src.nadobro.quant.mm_quote_math import (
    DEFAULT_MIN_ORDER_NOTIONAL_USD,
    MID_FULL_BIAS_MARGIN_UPLIFT,
    MM_COLLATERAL_SAFETY_FACTOR,
    _resolve_directional_bias_value,
    estimate_mm_quote_capacity,
)


# Documented per-quote margin multipliers per participation preset.
# These multiply the base (notional/leverage) margin requirement and combine
# with the bias uplift (1 + |bias|×0.20) to give the pre-trade card's required
# margin per quote.
PARTICIPATION_MARGIN_MULTIPLIERS = {
    "aggressive": 2.0,
    "normal": 1.0,
    "passive": 0.5,
}

# Builder fee — Nadobro is hard-coded at 1 bps. The pre-trade
# card renders the actual locked value.
BUILDER_FEE_BPS = float(NADO_BUILDER_FEE_RATE_1_BPS) / 10.0


def _bps(fraction: float) -> float:
    return float(fraction) * 10000.0


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_pretrade_margin_per_quote_usd(
    *,
    min_order_notional_usd: float,
    leverage: float,
    safety_factor: float,
    participation_preset: Optional[str],
    directional_bias,
) -> dict:
    """Perp margin formula:

        Required margin per quote = (notional / leverage)
                                  × safety_factor
                                  × participation_multiplier
                                  × (1 + |bias| × 0.20)

    Returns the breakdown so the pre-trade card can show each component.
    """
    notional = max(1.0, float(min_order_notional_usd))
    lev = max(1.0, float(leverage or 1.0))
    sf = max(1.0, float(safety_factor or 1.0))
    base_margin = (notional / lev) * sf

    preset_multiplier = (
        PARTICIPATION_MARGIN_MULTIPLIERS.get(pov_engine.normalize_preset(participation_preset), 1.0)
        if participation_preset
        else 1.0
    )
    bias_value = _resolve_directional_bias_value(directional_bias)
    bias_uplift = 1.0 + (MID_FULL_BIAS_MARGIN_UPLIFT * abs(bias_value))

    required_margin = base_margin * preset_multiplier * bias_uplift

    return {
        "min_order_notional_usd": notional,
        "leverage": lev,
        "safety_factor": sf,
        "base_margin_usd": base_margin,
        "participation_preset": (
            pov_engine.normalize_preset(participation_preset) if participation_preset else None
        ),
        "participation_multiplier": preset_multiplier,
        "bias_value": bias_value,
        "bias_uplift": bias_uplift,
        "required_margin_per_quote_usd": required_margin,
    }


def compute_max_loss_usd(
    *,
    notional_usd: float,
    leverage: float,
    sl_pct: float,
) -> float:
    """Margin-denominated SL per the Phase 0 fix.

    ``Max Loss = (SL% / 100) × (Notional / Leverage)``  (perp margin formula).
    Returns 0.0 when SL is unset.
    """
    if sl_pct <= 0:
        return 0.0
    margin = max(0.0, float(notional_usd)) / max(1.0, float(leverage or 1.0))
    return (float(sl_pct) / 100.0) * margin


def estimate_pretrade_fees_usd(
    *,
    placed_notional_usd: float,
    maker_fee_fraction: Optional[float],
) -> dict:
    """Net per-cycle fee preview using the locked Nadobro builder rate (1 bps)
    plus the pair's maker rate from the Nado ``symbols`` payload.

    Maker rate is signed: negative on most majors (the venue rebates makers).
    Net fee can therefore go negative when the rebate beats the builder fee.
    """
    placed = max(0.0, float(placed_notional_usd))
    builder_fee_fraction = BUILDER_FEE_BPS / 10000.0
    maker_fraction = float(maker_fee_fraction) if maker_fee_fraction is not None else 0.0

    builder_fee_usd = placed * builder_fee_fraction
    maker_fee_usd = placed * maker_fraction
    net_fee_usd = builder_fee_usd + maker_fee_usd
    return {
        "placed_notional_usd": placed,
        "builder_fee_bps": BUILDER_FEE_BPS,
        "builder_fee_usd": builder_fee_usd,
        "maker_fee_bps": _bps(maker_fraction),
        "maker_fee_usd": maker_fee_usd,
        "net_fee_usd": net_fee_usd,
    }


def build_pretrade_breakdown(
    *,
    strategy_id: str,
    conf: dict,
    network: str,
    product: str,
    leverage: float,
    wallet_collateral_usd: Optional[float] = None,
    pair_24h_volume_usd: Optional[float] = None,
) -> dict:
    """One-shot summary used by the pre-trade card and ``/mm_status`` header.

    Pulls in: catalog min_size + maker fee, the perp margin formula, POV pacing,
    estimated quote capacity, max loss. All numbers are returned both raw and
    pre-formatted so the renderer just splices them into the card text.
    """
    notional_usd = max(0.0, _safe_float(conf.get("notional_usd"), 100.0))
    cycle_cfg = max(0.0, _safe_float(conf.get("cycle_notional_usd"), notional_usd))
    # Honor the per-strategy SL field (rgrid/dgrid store it as rgrid_stop_loss_pct)
    # so the "max loss" preview matches the SL the engine actually enforces.
    from src.nadobro.strategy.strategy_registry import effective_sl_tp_pct
    sl_pct = max(0.0, effective_sl_tp_pct(strategy_id, conf)[0])
    # F8 (Phase 5 audit): mirror mm_bot.run_cycle's safety-factor logic exactly.
    # mm_bot only honors a state-set safety factor when it's >= 1.0; otherwise
    # it falls back to the constant default. Using ``max(1.0, …)`` here would
    # let a manually-set 0.5 silently become 1.0 in the dashboard while the
    # engine actually used 1.25 — numbers diverge.
    sf_raw = _safe_float(conf.get("mm_collateral_safety_factor"), 0.0)
    safety_factor = sf_raw if sf_raw >= 1.0 else MM_COLLATERAL_SAFETY_FACTOR
    participation_preset = conf.get("participation_preset")
    directional_bias = conf.get("directional_bias")

    # Catalog data — caller may pre-fetch network/client; we re-resolve cheaply
    # via the 60s TTL cache when omitted.
    min_order_notional = (
        get_product_min_quote_notional_usd(product, network=network)
        or DEFAULT_MIN_ORDER_NOTIONAL_USD
    )
    user_min = conf.get("min_order_notional_usd")
    if user_min:
        try:
            min_order_notional = max(1.0, float(user_min))
        except (TypeError, ValueError):
            pass
    maker_rate_fraction = get_product_maker_fee_rate(product, network=network)
    taker_rate_fraction = get_product_taker_fee_rate(product, network=network)

    margin_breakdown = compute_pretrade_margin_per_quote_usd(
        min_order_notional_usd=min_order_notional,
        leverage=leverage,
        safety_factor=safety_factor,
        participation_preset=participation_preset,
        directional_bias=directional_bias,
    )

    capacity = estimate_mm_quote_capacity(
        collateral_usd=(wallet_collateral_usd if wallet_collateral_usd is not None else notional_usd),
        min_order_notional_usd=min_order_notional,
        leverage=leverage,
        max_open_orders=int(conf.get("max_open_orders", 6) or 6),
        safety_factor=safety_factor,
    )

    pov_meta = None
    if participation_preset and pair_24h_volume_usd and pair_24h_volume_usd > 0:
        # Preview the DEPLOYED budget at the bot's REAL cadence — the card used
        # to pass raw margin and let pov_engine invent a 1200s cycle, so it
        # showed "$100 every 1200s" for a bot placing margin x leverage every
        # 60s. Same inputs as the live path now, INCLUDING the fast-cadence
        # resolution: rgrid/mid run at min(configured, 8s), so passing the raw
        # interval here would put a cadence on the card the bot never runs and
        # overstate the cycle slice by up to 7.5x.
        from src.nadobro.core.cadence import effective_interval_seconds

        pov_meta = pov_engine.compute_pov_duration(
            notional_usd=float(notional_usd) * max(1.0, float(leverage or 1.0)),
            preset=str(participation_preset),
            pair_24h_volume_usd=float(pair_24h_volume_usd),
            interval_seconds=effective_interval_seconds(
                str(strategy_id or "").lower().strip(),
                conf.get("interval_seconds") or 60,
            ),
        )

    # Per-cycle "expected placed notional" for the fee preview. When POV is on
    # we use the POV cycle slice; otherwise the user's cycle config (or notional).
    cycle_target = float(pov_meta["cycle_notional_usd"]) if pov_meta else max(cycle_cfg, notional_usd)
    fees = estimate_pretrade_fees_usd(
        placed_notional_usd=cycle_target,
        maker_fee_fraction=maker_rate_fraction,
    )

    # Max loss is SL% of the SESSION margin (the collateral the user committed),
    # matching the session-PnL rail's basis (_resolve_margin = notional). Do NOT
    # divide by leverage here — that produced a misleading per-quote figure
    # (e.g. $0.02 instead of $0.80 at 0.8% on $100).
    max_loss_usd = compute_max_loss_usd(
        notional_usd=notional_usd,
        leverage=1.0,
        sl_pct=sl_pct,
    )

    return {
        "strategy_id": strategy_id,
        "network": network,
        "product": product,
        "notional_usd": notional_usd,
        "cycle_target_notional_usd": cycle_target,
        "leverage": leverage,
        "min_order_notional_usd": min_order_notional,
        "maker_rate_bps": _bps(maker_rate_fraction) if maker_rate_fraction is not None else None,
        "taker_rate_bps": _bps(taker_rate_fraction) if taker_rate_fraction is not None else None,
        "margin": margin_breakdown,
        "capacity": capacity,
        "pov": _annotate_pov(pov_meta, notional_usd, leverage, pair_24h_volume_usd),
        "fees": fees,
        "max_loss_usd": max_loss_usd,
        "sl_pct": sl_pct,
    }


def _annotate_pov(
    pov_meta: Optional[dict],
    notional_usd: float,
    leverage: float,
    pair_24h_volume_usd: Optional[float],
) -> Optional[dict]:
    """Attach a ``pacing_note`` saying WHY the cycle is the size it is.

    AUDIT-F7: below the throughput floor the participation preset is
    inoperative — every preset yields the same cycle, the whole deployed budget
    — so rendering "POV passive" on its own told the user a rate the bot is not
    running. Grid ships $75 margin and Mid $100, i.e. essentially every retail
    session lands here.
    """
    if not pov_meta:
        return pov_meta
    deployed = max(0.0, float(notional_usd or 0.0)) * max(1.0, float(leverage or 1.0))
    cycle = float(pov_meta.get("cycle_notional_usd") or 0.0)
    raw = (
        float(pov_meta.get("multiplier") or 0.0)
        * (max(0.0, float(pair_24h_volume_usd or 0.0)) / 1440.0)
        * (float(pov_meta.get("interval_seconds") or 60) / 60.0)
    )
    note = ""
    if deployed > 0 and cycle >= deployed - 1e-9:
        note = (
            f" — preset inoperative: capped at 100% of the ${deployed:,.0f} deployment "
            f"per cycle (raw rate would be ${raw:,.2f})"
        )
    elif cycle > raw + 1e-9:
        note = f" — floored up from ${raw:,.2f} for throughput"
    return {**pov_meta, "pacing_note": note, "raw_cycle_usd": raw, "deployed_usd": deployed}


def render_pretrade_card_lines(breakdown: dict) -> list[str]:
    """Plain (non-MarkdownV2) lines suitable for tests + plain-text fallback.

    Caller wraps in escape_md / fences for the actual Telegram message.
    """
    margin = breakdown["margin"]
    capacity = breakdown["capacity"]
    pov = breakdown.get("pov")
    fees = breakdown["fees"]
    lines: list[str] = []
    lines.append(
        f"Required margin per quote: ${margin['required_margin_per_quote_usd']:,.2f}  "
        f"(base ${margin['base_margin_usd']:,.2f} × "
        f"part {margin['participation_multiplier']:.2f} × "
        f"bias {margin['bias_uplift']:.2f})"
    )
    lines.append(
        f"Max resting quotes: {int(capacity['max_resting_quotes'])} "
        f"(per-quote margin est ${capacity['margin_per_quote_est_usd']:,.2f})"
    )
    if breakdown.get("max_loss_usd", 0.0) > 0:
        lines.append(
            f"Max loss at SL {breakdown['sl_pct']:.2f}%: "
            f"${breakdown['max_loss_usd']:,.2f} (margin-based)"
        )
    builder = fees["builder_fee_bps"]
    maker_label = (
        f"{breakdown['maker_rate_bps']:+.2f} bps"
        if breakdown.get("maker_rate_bps") is not None
        else "n/a"
    )
    lines.append(
        f"Fees per cycle: builder {builder:.2f} bps + maker {maker_label} = "
        f"net ${fees['net_fee_usd']:+,.4f} (on ${fees['placed_notional_usd']:,.2f})"
    )
    if pov:
        lines.append(
            f"POV {pov['preset']}: {pov['multiplier'] * 100:.0f}%/min, "
            f"~{pov['duration_minutes']:.1f} min duration, "
            f"cycle ${pov['cycle_notional_usd']:,.2f} every {pov['interval_seconds']}s"
            f"{pov.get('pacing_note', '')}"
        )
    return lines


def build_status_snapshot(
    *,
    state: dict,
    strategy_id: str,
    network: str,
    product: str,
    open_orders_count: int,
    positions: Optional[list] = None,
    live_metrics: Optional[dict] = None,
    live_snapshot: Optional[dict] = None,
) -> dict:
    """Live snapshot for ``/mm_status``.

    Most values are derived from ``state``. ``live_snapshot`` (from
    ``live_session.get_live_session_snapshot``) is the authoritative Nado-sourced
    view — open-position uPnL, realized PnL, volume, fees, fills and open-order
    count — and overrides the state estimates so the dashboard matches Nado.
    No external calls here — the caller fetches the snapshot.
    """
    is_volume = str(strategy_id or "").lower() == "vol"
    session_done = max(0.0, _safe_float(state.get("mm_session_notional_done_usd"), 0.0))
    session_target = max(0.0, _safe_float(state.get("session_notional_cap_usd"), 0.0))
    initial_equity = max(0.0, _safe_float(state.get("mm_initial_equity"), 0.0))
    cumulative_pnl = _safe_float(state.get("mm_cumulative_pnl"), 0.0)
    last_cycle_pnl = _safe_float(state.get("grid_last_cycle_pnl_usd"), 0.0)
    if live_metrics:
        # DB-sourced session volume / realized PnL override the (engine-empty)
        # state estimates so /mm_status shows real numbers.
        session_done = max(session_done, _safe_float(live_metrics.get("volume"), 0.0))
        cumulative_pnl = _safe_float(live_metrics.get("realized_pnl"), cumulative_pnl)

    # Authoritative live Nado figures (preferred over both state and live_metrics).
    live_snapshot = live_snapshot or {}
    unrealized_pnl = _safe_float(live_snapshot.get("unrealized_pnl"), 0.0)
    session_pnl = _safe_float(live_snapshot.get("session_pnl"), 0.0)
    session_pnl_pct = _safe_float(live_snapshot.get("session_pnl_pct"), 0.0)
    session_margin = _safe_float(live_snapshot.get("margin"), 0.0)
    if live_snapshot:
        session_done = max(session_done, _safe_float(live_snapshot.get("volume"), 0.0))
        cumulative_pnl = _safe_float(live_snapshot.get("realized_pnl"), cumulative_pnl)

    if is_volume:
        state_volume = max(
            _safe_float(state.get("volume_done_usd"), 0.0),
            _safe_float(state.get("session_volume_usd"), 0.0),
        )
        session_done = max(session_done, state_volume)
        volume_target = max(0.0, _safe_float(state.get("target_volume_usd"), 0.0))
        if volume_target > 0:
            session_target = volume_target
        if not live_snapshot:
            cumulative_pnl = _safe_float(
                state.get("session_realized_pnl_usd"),
                _safe_float(state.get("mm_cumulative_pnl"), cumulative_pnl),
            )

    # Fill rate: ratio of executed quotes vs tracked quotes over the session.
    tracked = state.get("mm_tracked_quotes") or {}
    grid_buy_fills = state.get("grid_buy_fills") or []
    grid_sell_fills = state.get("grid_sell_fills") or []
    fill_count = len(grid_buy_fills) + len(grid_sell_fills)
    # Engine strategies record fills to the DB, not ``state``. When the caller
    # supplies DB-sourced live metrics, they are authoritative for the
    # fill count / session volume / cumulative PnL the dashboard shows.
    if live_metrics:
        fill_count = int(live_metrics.get("fills") or fill_count)
    if live_snapshot:
        fill_count = int(live_snapshot.get("fills") or fill_count)
        open_orders_count = int(live_snapshot.get("open_orders") or open_orders_count)
    posted_count = max(fill_count, len(tracked))
    fill_rate = (fill_count / posted_count) if posted_count > 0 else 0.0

    inv_soft_limit = _safe_float(state.get("inventory_soft_limit_usd"), 0.0)
    inv_hard_limit = max(inv_soft_limit * 1.8, inv_soft_limit + 1.0) if inv_soft_limit > 0 else 0.0
    net_units = _safe_float(state.get("grid_prev_net_units"), 0.0)
    # Inventory USD value uses the latest mid history sample if available.
    mid_history = state.get("mm_mid_history") or []
    last_mid = float(mid_history[-1]) if mid_history else _safe_float(state.get("reference_price"), 0.0)
    inv_usd = abs(net_units) * last_mid

    pov_meta = state.get("mm_pov_engine") or {}
    pov_warning = state.get("mm_pov_engine_warning")

    # Phase 4 reliability surfaces.
    skipped_levels = list(state.get("mm_skipped_levels") or [])
    resume_reconciled_at = _safe_float(state.get("mm_resume_reconciled_at"), 0.0) or None
    resume_executed_count = int(_safe_float(state.get("mm_resume_executed_count"), 0))
    resume_tracked_count = int(_safe_float(state.get("mm_resume_tracked_count"), 0))
    market_price_retries = list(state.get("mm_market_price_retries") or [])
    open_orders_retries = list(state.get("mm_open_orders_retries") or [])
    order_observability = state.get("order_observability") or {}
    volume_remaining = _safe_float(
        state.get("volume_remaining_usd"),
        max(session_target - session_done, 0.0) if session_target > 0 else 0.0,
    )
    session_fees = _safe_float(state.get("session_fees_usd"), 0.0)
    if live_snapshot:
        session_fees = _safe_float(live_snapshot.get("fees"), session_fees)

    return {
        "strategy_id": strategy_id,
        "network": network,
        "product": product,
        "running": bool(state.get("running")),
        "is_paused": bool(state.get("mm_paused")),
        "leverage": _safe_float(state.get("leverage"), 1.0),
        "leverage_mode": str(state.get("leverage_mode") or ""),
        "open_orders_count": int(open_orders_count or 0),
        "tracked_quotes_count": len(tracked),
        "fill_count": fill_count,
        "fill_rate": fill_rate,
        "session_done_usd": session_done,
        "session_target_usd": session_target,
        "session_progress_pct": (session_done / session_target * 100.0) if session_target > 0 else 0.0,
        "initial_equity_usd": initial_equity,
        "cumulative_pnl_usd": cumulative_pnl,
        "unrealized_pnl_usd": unrealized_pnl,
        "session_pnl_usd": session_pnl,
        "session_pnl_pct": session_pnl_pct,
        "session_margin_usd": session_margin,
        "session_fees_usd": session_fees,
        "has_position": bool(live_snapshot.get("has_position")),
        "position_size": _safe_float(live_snapshot.get("position_size"), 0.0),
        "position_side": str(live_snapshot.get("position_side") or ""),
        "position_value": _safe_float(live_snapshot.get("position_value"), 0.0),
        "entry_price": _safe_float(live_snapshot.get("entry_price"), 0.0),
        "liq_price": _safe_float(live_snapshot.get("liq_price"), 0.0),
        "has_live_snapshot": bool(live_snapshot),
        "last_cycle_pnl_usd": last_cycle_pnl,
        "drawdown_pct": (
            (abs(cumulative_pnl) / initial_equity * 100.0)
            if (initial_equity > 0 and cumulative_pnl < 0)
            else 0.0
        ),
        "spread_bp": _safe_float(state.get("spread_bp"), 0.0),
        "reference_price": _safe_float(state.get("reference_price"), 0.0) or last_mid,
        "dgrid_phase": state.get("dgrid_phase"),
        "dgrid_variance_ratio": _safe_float(state.get("dgrid_variance_ratio"), 0.0) or None,
        "inv_usd": inv_usd,
        "inv_soft_limit_usd": inv_soft_limit,
        "inv_hard_limit_usd": inv_hard_limit,
        "min_order_notional_usd": _safe_float(state.get("mm_min_order_notional_usd_resolved"), 0.0),
        "max_resting_quotes_cap": int(_safe_float(state.get("mm_max_resting_quotes_cap"), 0)),
        "margin_per_quote_est_usd": _safe_float(state.get("mm_margin_per_quote_est_usd"), 0.0),
        "pov_engine": pov_meta or None,
        "pov_warning": pov_warning,
        "last_error": state.get("last_error"),
        # Phase 4 reliability fields.
        "skipped_levels": skipped_levels,
        "skipped_levels_count": len(skipped_levels),
        "resume_reconciled_at": resume_reconciled_at,
        "resume_executed_count": resume_executed_count,
        "resume_tracked_count": resume_tracked_count,
        "market_price_retries": market_price_retries,
        "open_orders_retries": open_orders_retries,
        "orders_placed": int(_safe_float(order_observability.get("orders_placed"), 0)),
        "orders_filled": int(_safe_float(order_observability.get("orders_filled"), 0)),
        "orders_cancelled": int(_safe_float(order_observability.get("orders_cancelled"), 0)),
        "vol_market": str(state.get("vol_market") or "").lower(),
        "vol_phase": state.get("vol_phase"),
        "vol_last_order_kind": state.get("vol_last_order_kind"),
        "vol_last_order_digest": state.get("vol_last_order_digest"),
        "vol_cycles_completed": int(_safe_float(state.get("vol_cycles_completed"), 0)),
        "vol_closed_cycles": int(_safe_float(state.get("vol_closed_cycles"), 0)),
        "volume_remaining_usd": max(volume_remaining, 0.0),
    }


def _compact_digest(digest) -> str:
    text = str(digest or "").strip()
    if len(text) <= 14:
        return text
    return f"{text[:8]}…{text[-6:]}"


def _volume_product_label(snapshot: dict) -> str:
    product = str(snapshot.get("product") or "?").upper().replace("-PERP", "")
    market = str(snapshot.get("vol_market") or "").lower()
    if market == "spot":
        return f"{product} SPOT"
    return f"{product}-PERP" if product != "MULTI" else product


def _render_volume_status_lines(snapshot: dict) -> list[str]:
    state_label = "PAUSED" if snapshot.get("is_paused") else ("LIVE" if snapshot.get("running") else "STOPPED")
    done = float(snapshot.get("session_done_usd") or 0.0)
    target = float(snapshot.get("session_target_usd") or 0.0)
    progress = (done / target * 100.0) if target > 0 else 0.0
    remaining = float(snapshot.get("volume_remaining_usd") or 0.0)
    phase = str(snapshot.get("vol_phase") or "idle")
    cycles = int(snapshot.get("vol_cycles_completed") or snapshot.get("vol_closed_cycles") or 0)
    placed = int(snapshot.get("orders_placed") or 0)
    filled = max(int(snapshot.get("fill_count") or 0), int(snapshot.get("orders_filled") or 0))
    cancelled = int(snapshot.get("orders_cancelled") or 0)

    lines = [
        f"VOL {_volume_product_label(snapshot)} ({snapshot.get('network')}) — {state_label}",
        f"Phase: {phase} | Cycles: {cycles}",
        (
            f"Volume: ${done:,.2f}"
            + (f" / ${target:,.2f} ({progress:.1f}%)" if target > 0 else "")
        ),
    ]
    if target > 0:
        lines.append(f"Remaining: ${remaining:,.2f}")
    lines.append(
        f"PnL: realized ${snapshot['cumulative_pnl_usd']:+,.2f}"
        + (f" | fees ${snapshot['session_fees_usd']:,.2f}" if snapshot.get("session_fees_usd") else "")
    )
    lines.append(
        f"Orders: {snapshot['open_orders_count']} open / "
        f"{placed} placed / {filled} filled / {cancelled} cancelled"
    )
    last_kind = str(snapshot.get("vol_last_order_kind") or "").strip()
    last_digest = _compact_digest(snapshot.get("vol_last_order_digest"))
    if last_kind or last_digest:
        bits = [last_kind.upper()] if last_kind else []
        if last_digest:
            bits.append(last_digest)
        lines.append(f"Last order: {' '.join(bits)}")
    if snapshot.get("last_error"):
        lines.append(f"Last error: {str(snapshot['last_error'])[:160]}")
    return lines


def render_status_lines(snapshot: dict) -> list[str]:
    """Plain-text (non-MarkdownV2) lines for /mm_status output."""
    if str(snapshot.get("strategy_id") or "").lower() == "vol":
        return _render_volume_status_lines(snapshot)

    label = (snapshot.get("strategy_id") or "MM").upper()
    state_label = "PAUSED" if snapshot.get("is_paused") else ("LIVE" if snapshot.get("running") else "STOPPED")
    lines = [
        f"{label} {snapshot.get('product', '?')} ({snapshot.get('network')}) — {state_label}",
        f"Leverage: {snapshot['leverage']:.1f}x ({snapshot.get('leverage_mode') or '—'})",
        (
            f"Session volume: ${snapshot['session_done_usd']:,.2f}"
            + (
                f" / ${snapshot['session_target_usd']:,.2f} ({snapshot['session_progress_pct']:.1f}%)"
                if snapshot['session_target_usd'] > 0 else ""
            )
        ),
    ]
    # PnL is per-RUN and realized+unrealized when a live snapshot is present
    # (matches the strategy dashboards and the Nado UI). Lead with the session
    # PnL, then break it down; fall back to the realized-only cumulative line for
    # legacy/state-only callers with no snapshot.
    if snapshot.get("has_live_snapshot"):
        margin = snapshot.get("session_margin_usd") or 0.0
        margin_note = f" of ${margin:,.2f} margin" if margin > 0 else ""
        lines.append(
            f"PnL (realized+unrealized): ${snapshot['session_pnl_usd']:+,.2f} "
            f"({snapshot['session_pnl_pct']:+.2f}%{margin_note})"
        )
        lines.append(
            f"  realized ${snapshot['cumulative_pnl_usd']:+,.2f} | "
            f"unrealized ${snapshot['unrealized_pnl_usd']:+,.2f}"
            + (f" | last cycle ${snapshot['last_cycle_pnl_usd']:+,.2f}"
               if snapshot['last_cycle_pnl_usd'] else "")
        )
        if snapshot.get("has_position"):
            value = snapshot.get("position_value") or 0.0
            lines.append(
                f"Position: {snapshot['position_side'].upper()} {snapshot['position_size']:.4f} "
                f"@ ${snapshot['entry_price']:,.2f}"
                + (f" (${value:,.2f})" if value > 0 else "")
                + (f" / liq ${snapshot['liq_price']:,.2f}" if snapshot.get("liq_price") else "")
            )
    else:
        lines.append(
            f"PnL: cumulative ${snapshot['cumulative_pnl_usd']:+,.2f}"
            + (f", drawdown {snapshot['drawdown_pct']:.2f}%" if snapshot['drawdown_pct'] > 0 else "")
            + f", last cycle ${snapshot['last_cycle_pnl_usd']:+,.2f}"
        )
    lines += [
        (
            f"Quotes: {snapshot['open_orders_count']} open / "
            f"{snapshot['tracked_quotes_count']} tracked / "
            f"{snapshot['fill_count']} fills (fill rate {snapshot['fill_rate'] * 100:.1f}%)"
        ),
        f"Spread: {snapshot['spread_bp']:.1f} bp / Ref: {snapshot['reference_price']:,.4f}",
    ]
    # Dynamic Grid ONLY. R-Grid used to run the D-Grid engine, so R-Grid cards
    # showed a "DGRID phase" line for a strategy that has no phases; a stale
    # dgrid_phase can also survive in bot_state from before that split, so gate on
    # the strategy rather than on the key being present.
    if str(snapshot.get("strategy_id") or "").lower() == "dgrid" and snapshot.get("dgrid_phase"):
        var = snapshot.get("dgrid_variance_ratio")
        lines.append(
            f"DGRID phase: {snapshot['dgrid_phase']}"
            + (f" (var ratio {var:.2f})" if var else "")
        )
    if snapshot.get("inv_soft_limit_usd", 0) > 0:
        lines.append(
            f"Inventory: ${snapshot['inv_usd']:,.2f} / "
            f"soft ${snapshot['inv_soft_limit_usd']:,.2f} / "
            f"hard ${snapshot['inv_hard_limit_usd']:,.2f}"
        )
    if snapshot.get("pov_engine"):
        pov = snapshot["pov_engine"]
        lines.append(
            f"POV {pov['preset']}: cycle ${pov['cycle_notional_usd']:,.2f} every "
            f"{pov['interval_seconds']}s, ~{pov['duration_minutes']:.1f} min total"
        )
    elif snapshot.get("pov_warning"):
        lines.append(f"POV: {snapshot['pov_warning']}")
    # Phase 4: reliability diagnostics.
    if snapshot.get("skipped_levels_count"):
        skips = snapshot["skipped_levels"][:3]
        skip_summary = "; ".join(
            f"L{s.get('level')} {s.get('side')} ({s.get('reason', 'skip')})"
            for s in skips
        )
        more = "" if len(snapshot["skipped_levels"]) <= 3 else f" +{len(snapshot['skipped_levels']) - 3} more"
        lines.append(f"Skipped this cycle: {snapshot['skipped_levels_count']} → {skip_summary}{more}")
    if snapshot.get("resume_reconciled_at"):
        lines.append(
            f"Resume reconcile: tracked={snapshot['resume_tracked_count']} "
            f"→ executed={snapshot['resume_executed_count']} "
            f"@ ts={int(snapshot['resume_reconciled_at'])}"
        )
    retries = (snapshot.get("market_price_retries") or []) + (snapshot.get("open_orders_retries") or [])
    if retries:
        lines.append(f"Gateway retries last cycle: {len(retries)} (latest: {retries[-1][:80]})")
    if snapshot.get("last_error"):
        lines.append(f"Last error: {str(snapshot['last_error'])[:160]}")
    return lines


def render_fills_lines(state: dict, limit: int = 10, db_fills: Optional[list] = None) -> list[str]:
    """Render the most recent N executions across both sides.

    ``db_fills`` (rows from ``get_session_recent_fills``) take precedence over
    the in-memory ``state`` fill lists: engine strategies record fills straight
    to the DB via DbTradeRecorder and never populate ``state``."""
    combined: list[dict] = []
    if db_fills:
        for r in db_fills:
            if not isinstance(r, dict):
                continue
            side = "BUY" if str(r.get("side") or "").lower() in ("long", "buy") else "SELL"
            combined.append({
                "side": side,
                "size": _safe_float(r.get("size")),
                "price": _safe_float(r.get("price")),
                "ts": _safe_float(r.get("ts")),
            })
    else:
        buys = list(state.get("grid_buy_fills") or [])
        sells = list(state.get("grid_sell_fills") or [])
        for f in buys:
            if isinstance(f, dict):
                combined.append({**f, "side": "BUY"})
        for f in sells:
            if isinstance(f, dict):
                combined.append({**f, "side": "SELL"})
    combined.sort(key=lambda f: float(f.get("ts") or 0.0), reverse=True)
    if not combined:
        return ["No fills recorded for this session yet."]
    lines = []
    for row in combined[: max(1, limit)]:
        ts = float(row.get("ts") or 0.0)
        price = float(row.get("price") or 0.0)
        size = float(row.get("size") or 0.0)
        notional = price * size
        lines.append(
            f"{row['side']:<4} {size:.6f} @ {price:,.4f} = ${notional:,.2f}  ts={int(ts)}"
        )
    return lines


def _resolve_leverage_for_card(strategy_id: str, conf: dict, network: str, product: str) -> float:
    """Helper: same leverage source the engine uses, so cards don't drift."""
    try:
        return float(get_product_max_leverage(product, network=network))
    except Exception:
        return max(1.0, _safe_float(conf.get("leverage"), 3.0))
