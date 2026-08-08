"""Canonical strategy identity and defaults.

Keep aliases, display names, supported ids, and default payloads in one place so
runtime, settings, and managed-agent surfaces do not drift.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping


STRATEGY_GRID = "grid"
STRATEGY_RGRID = "rgrid"
STRATEGY_DGRID = "dgrid"
STRATEGY_MID = "mid"
STRATEGY_DN = "dn"
STRATEGY_VOL = "vol"
STRATEGY_BRO = "bro"

SUPPORTED_STRATEGIES: tuple[str, ...] = (
    STRATEGY_GRID,
    STRATEGY_RGRID,
    STRATEGY_DGRID,
    STRATEGY_MID,
    STRATEGY_DN,
    STRATEGY_VOL,
    STRATEGY_BRO,
)

MARKET_MAKING_STRATEGIES: tuple[str, ...] = (
    STRATEGY_GRID,
    STRATEGY_RGRID,
    STRATEGY_DGRID,
    STRATEGY_MID,
)

# Reverse/Dynamic Grid store their PnL stop/take (as % of margin) under the
# rgrid_* keys in the UI, but the engine (session rail + grid barrier) reads
# sl_pct/tp_pct. Resolve the strategy's own field with a fallback so a user's
# custom SL/TP is actually honored instead of the sl_pct default.
_RGRID_SLTP_STRATEGIES: tuple[str, ...] = (STRATEGY_RGRID, STRATEGY_DGRID)


# The margin the session SL/TP percentage is measured against. MUST stay in the
# same key order as ``trading/live_session._resolve_margin`` — that function is
# what actually enforces the rail, so anything sizing itself against "the stop
# budget" has to agree with it. Note this is NOT the deployment basis:
# ``map_strategy_config`` resolves ``cycle_notional_usd`` FIRST when deciding how
# much to deploy, which is the opposite order. Mixing the two let R-Grid's step
# cap size itself against a budget 2.5x larger than the rail would ever honour
# (audit 2026-08-06). ``tests/engine/test_sltp_invariants.py`` pins the agreement.
_SESSION_MARGIN_KEYS: tuple[str, ...] = (
    "notional_usd", "cycle_notional_usd", "fixed_margin_usd", "session_margin_usd",
)


def session_margin_usd(conf: Mapping) -> float:
    """Allocated margin the user's SL/TP % is measured against (0 when unset)."""
    for key in _SESSION_MARGIN_KEYS:
        try:
            value = float(conf.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def effective_sl_tp_pct(strategy: str, conf: Mapping) -> tuple[float, float]:
    """Return ``(sl_pct, tp_pct)`` honoring the per-strategy SL/TP fields.

    rgrid/dgrid: ``rgrid_stop_loss_pct`` / ``rgrid_take_profit_pct`` (fallback to
    ``sl_pct`` / ``tp_pct``). All other strategies: ``sl_pct`` / ``tp_pct``.
    """
    def _f(value: object) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    s = str(strategy or "").lower()
    if s in _RGRID_SLTP_STRATEGIES:
        sl = conf.get("rgrid_stop_loss_pct")
        sl = conf.get("sl_pct") if sl is None else sl
        tp = conf.get("rgrid_take_profit_pct")
        tp = conf.get("tp_pct") if tp is None else tp
        return _f(sl), _f(tp)
    return _f(conf.get("sl_pct")), _f(conf.get("tp_pct"))


def sltp_is_explicit(strategy: str, conf: Mapping) -> tuple[bool, bool]:
    """``(sl_set, tp_set)`` — whether the user's own key is PRESENT for this
    strategy, even when its value is 0.

    ``effective_sl_tp_pct`` already returns 0.0 for a deliberately disarmed stop,
    but a caller cannot tell that apart from "never configured" and so may
    substitute a default, re-arming what the user switched off. That is exactly
    what DGRID-TP-DISARM-PHANTOM was: an explicit ``rgrid_take_profit_pct = 0``
    became 0.6, arming a tier ladder at [0.2, 0.4, 0.6]% of margin while the rail
    stayed disarmed. Key order mirrors ``effective_sl_tp_pct`` exactly.
    """
    s = str(strategy or "").lower()
    if s in _RGRID_SLTP_STRATEGIES:
        sl_set = conf.get("rgrid_stop_loss_pct") is not None or conf.get("sl_pct") is not None
        tp_set = conf.get("rgrid_take_profit_pct") is not None or conf.get("tp_pct") is not None
        return sl_set, tp_set
    return conf.get("sl_pct") is not None, conf.get("tp_pct") is not None

STRATEGY_ALIASES: Mapping[str, str] = {
    "mm": STRATEGY_GRID,
    "market_making": STRATEGY_GRID,
    "market-making": STRATEGY_GRID,
    "reverse_grid": STRATEGY_RGRID,
    "reverse-grid": STRATEGY_RGRID,
    "r-grid": STRATEGY_RGRID,
    "dynamic_grid": STRATEGY_DGRID,
    "dynamic-grid": STRATEGY_DGRID,
    "d-grid": STRATEGY_DGRID,
    "mid_mode": STRATEGY_MID,
    "mid-mode": STRATEGY_MID,
    "midmode": STRATEGY_MID,
    "delta-neutral": STRATEGY_DN,
    "delta_neutral": STRATEGY_DN,
    "volume": STRATEGY_VOL,
    "volume_bot": STRATEGY_VOL,
    "vol-bot": STRATEGY_VOL,
    "alpha": STRATEGY_BRO,
    "alpha_agent": STRATEGY_BRO,
}

STRATEGY_DISPLAY_NAMES: Mapping[str, str] = {
    STRATEGY_GRID: "GRID",
    STRATEGY_RGRID: "REVERSE GRID",
    STRATEGY_DGRID: "DYNAMIC GRID",
    STRATEGY_MID: "MID MODE",
    STRATEGY_DN: "DELTA NEUTRAL",
    STRATEGY_VOL: "VOLUME",
    STRATEGY_BRO: "ALPHA AGENT",
}


@dataclass(frozen=True)
class StrategyPhrase:
    strategy_id: str
    phrases: tuple[str, ...]


NATURAL_LANGUAGE_STRATEGY_PHRASES: tuple[StrategyPhrase, ...] = (
    StrategyPhrase(STRATEGY_BRO, ("alpha agent", "bro strategy", "alpha mode")),
    StrategyPhrase(STRATEGY_DN, ("delta neutral", "delta-neutral", " dn ")),
    StrategyPhrase(STRATEGY_VOL, ("volume bot", "vol bot", " vol ")),
    StrategyPhrase(STRATEGY_RGRID, ("r-grid", "rgrid", "reverse grid", "reverse-grid")),
    StrategyPhrase(STRATEGY_DGRID, ("dynamic grid", "dgrid", "d-grid")),
    # Mid Mode must be matched before plain "grid" so phrases like "mid mode"
    # don't fall through to the GRID branch.
    StrategyPhrase(STRATEGY_MID, ("mid mode", "mid-mode", " mid ")),
    StrategyPhrase(STRATEGY_GRID, ("grid",)),
)


SETTINGS_STRATEGY_DEFAULTS: Mapping[str, dict] = {
    "grid": {
        "notional_usd": 75.0, "spread_bp": 4.0, "interval_seconds": 45, "tp_pct": 0.6, "sl_pct": 0.5,
        "levels": 2, "threshold_bp": 0.0, "close_offset_bp": 24.0,
        "reference_mode": "ema_fast",
        "ema_fast_alpha": 0.45,
        "ema_slow_alpha": 0.20,
        "vol_window_points": 12,
        "vol_sensitivity": 0.02,
        "min_spread_bp": 2.0,
        "max_spread_bp": 20.0,
        "quote_ttl_seconds": 90,
        "directional_bias": "neutral",
        "inventory_soft_limit_usd": 45.0,
        "cycle_notional_usd": 75.0,
        "session_notional_cap_usd": 0.0,
        "grid_reset_threshold_pct": 0.2,
        "grid_reset_timeout_seconds": 120,
    },
    "rgrid": {
        "notional_usd": 100.0, "spread_bp": 10.0, "rgrid_spread_bp": 10.0, "interval_seconds": 60,
        "tp_pct": 1.2, "sl_pct": 0.8, "rgrid_stop_loss_pct": 0.8, "rgrid_take_profit_pct": 1.2,
        "levels": 4, "rgrid_discretion": 0.06,
        "rgrid_reset_threshold_pct": 0.2, "rgrid_reset_timeout_seconds": 120,
        # Legacy keys kept for one migration cycle.
        "min_range_pct": 1.0, "max_range_pct": 1.0,
    },
    "dgrid": {
        "notional_usd": 100.0, "cycle_notional_usd": 100.0, "spread_bp": 8.0,
        "interval_seconds": 30, "tp_pct": 1.2, "sl_pct": 0.8, "levels": 4,
        "dgrid_trend_on_variance_ratio": 1.25,
        "dgrid_range_on_variance_ratio": 1.15,
        "dgrid_spread_bp": 8.0,
        "dgrid_min_spread_bp": 2.0,
        "dgrid_max_spread_bp": 50.0,
        "dgrid_short_window_points": 4,
        "dgrid_long_window_points": 12,
        "rgrid_stop_loss_pct": 0.8,
        "rgrid_take_profit_pct": 1.2,
        "rgrid_discretion": 0.06,
        "grid_reset_threshold_pct": 0.2,
        "rgrid_reset_threshold_pct": 0.2,
        "grid_reset_timeout_seconds": 120,
        "rgrid_reset_timeout_seconds": 120,
    },
    "mid": {
        # Tread Mid Mode parity: pure mid ± spread×level pricing, no anchor logic,
        # no soft-reset. Spread default 5 bps; user-tunable in [-10, +100].
        "notional_usd": 100.0,
        "cycle_notional_usd": 100.0,
        "spread_bp": 5.0,
        "interval_seconds": 60,
        "tp_pct": 0.6,
        "sl_pct": 0.5,
        "levels": 2,
        "min_spread_bp": -10.0,
        "max_spread_bp": 100.0,
        "reference_mode": "mid",
        "ema_fast_alpha": 0.45,
        "ema_slow_alpha": 0.20,
        "vol_window_points": 12,
        "vol_sensitivity": 0.0,
        "quote_ttl_seconds": 90,
        # Mid Mode accepts directional_bias as a float in [-1.0, +1.0] (Tread spec).
        # 0.0 == neutral; ±1.0 maxes the side tilt and triggers the +20% margin uplift.
        "directional_bias": 0.0,
        "inventory_soft_limit_usd": 60.0,
    },
    "dn": {
        "notional_usd": 50.0, "spread_bp": 3.0, "interval_seconds": 90, "tp_pct": 0.8, "sl_pct": 0.6,
        "auto_close_on_maintenance": 1.0,
        "funding_entry_mode": "wait",
    },
    "vol": {
        "notional_usd": 100.0,
        "fixed_margin_usd": 100.0,
        # Per-cycle size. Product band is $100-$500 (docs/volume_bot_taker_v4.md);
        # the UI enforces it and map_strategy_config re-clamps.
        "session_margin_usd": 100.0,
        "target_volume_usd": 10000.0,
        # v3 ping-pong is timer-driven (requote/cross deadlines) — 5s ticks
        # via the fast-cadence set; the old 10s doubled every reaction lag.
        "interval_seconds": 5,
        "tp_pct": 1.0,
        # Session stop: cumulative realized PnL NET OF FEES reaching -5% of
        # margin (-$5 on $100) stops the bot and flattens. Every taker round
        # trip costs spread + both legs' fees by construction, so this is the
        # binding limit on how much volume a run completes.
        "sl_pct": 5.0,
        # v3 quoting knobs (see engine_runtime.map_strategy_config vol branch
        # for semantics). The signal-filter/direction keys retired in 2026-05
        # (docs/volume_bot.md) were dropped from these defaults 2026-07-11.
        # They stay mapped because vol_taker_mode=0 restores the v3 path.
        "vol_buy_offset_bp": 0.0,
        "vol_max_cycle_loss_bp": 20.0,
        "vol_requote_seconds": 20.0,
        "vol_cross_after_seconds": 25.0,
        "vol_cross_slippage_bp": 15.0,
        # v4 taker mode (default ON): market orders both legs; the sell waits
        # for a price above the entry that also clears both legs' fees.
        # Execution algo: maker TWAP by default (rest post-only, chase, cross
        # only when materially behind schedule). "chase" | "taker" also valid.
        # vol_taker_mode is NOT defaulted here — setting it would pin every run
        # to the taker path and the default below could never apply. It remains
        # available as an explicit per-user kill-switch.
        "vol_execution_algo": "twap",
        "vol_twap_horizon_seconds": 120.0,
        "vol_chase_interval_seconds": 20.0,
        "vol_cross_tolerance_frac": 0.5,
        "vol_max_taker_frac": 0.25,
        "vol_leg_hard_deadline_mult": 3.0,
        # Price bound on the marketable-limit taker legs.
        "vol_taker_slippage_bp": 15.0,
        # A marketable limit rests if it cannot fully sweep; cancel + re-place
        # a taker leg still live after this long.
        "vol_taker_fill_timeout_seconds": 10.0,
    },
    "bro": {
        "budget_usd": 500.0,
        "risk_level": "balanced",
        "max_positions": 3,
        "cycle_seconds": 300,
        "tp_pct": 2.0,
        "sl_pct": 1.5,
        "max_loss_pct": 15.0,
        "leverage_cap": 5,
        "min_reward_risk": 1.2,
        "products": ["BTC", "ETH", "SOL"],
        "use_sentiment": True,
        "use_cmc": True,
        "min_confidence": 0.65,
        "howl_enabled": True,
        "howl_hour_utc": 2,
    },
}

RUNTIME_STRATEGY_DEFAULTS: Mapping[str, dict] = {
    "grid": {
        "notional_usd": 400.0,
        "cycle_notional_usd": 400.0,
        "spread_bp": 4.0,
        "interval_seconds": 45,
        "threshold_bp": 12.0,
        "close_offset_bp": 24.0,
    },
    "rgrid": {
        "notional_usd": 100.0,
        "spread_bp": 10.0,
        "rgrid_spread_bp": 10.0,
        "interval_seconds": 60,
        "levels": 4,
        "rgrid_stop_loss_pct": 0.8,
        "rgrid_take_profit_pct": 1.2,
        "rgrid_discretion": 0.06,
        "rgrid_reset_threshold_pct": 0.2,
        "rgrid_reset_timeout_seconds": 120,
        # Legacy fallback (read-only compatibility).
        "min_range_pct": 1.0,
        "max_range_pct": 1.0,
    },
    "dgrid": {
        "notional_usd": 100.0,
        "cycle_notional_usd": 100.0,
        "spread_bp": 8.0,
        "interval_seconds": 30,
        "levels": 4,
        "tp_pct": 1.2,
        "sl_pct": 0.8,
        "dgrid_trend_on_variance_ratio": 1.25,
        "dgrid_range_on_variance_ratio": 1.15,
        "dgrid_spread_bp": 8.0,
        "dgrid_min_spread_bp": 2.0,
        "dgrid_max_spread_bp": 50.0,
        "dgrid_short_window_points": 4,
        "dgrid_long_window_points": 12,
        "rgrid_stop_loss_pct": 0.8,
        "rgrid_take_profit_pct": 1.2,
        "rgrid_discretion": 0.06,
        "grid_reset_threshold_pct": 0.2,
        "rgrid_reset_threshold_pct": 0.2,
        "grid_reset_timeout_seconds": 120,
        "rgrid_reset_timeout_seconds": 120,
    },
    "mid": {
        "notional_usd": 100.0,
        "cycle_notional_usd": 100.0,
        "spread_bp": 5.0,
        "interval_seconds": 60,
        "levels": 2,
        "tp_pct": 0.6,
        "sl_pct": 0.5,
        "min_spread_bp": -10.0,
        "max_spread_bp": 100.0,
        "reference_mode": "mid",
        "ema_fast_alpha": 0.45,
        "ema_slow_alpha": 0.20,
        "vol_window_points": 12,
        "vol_sensitivity": 0.0,
        "quote_ttl_seconds": 90,
        "directional_bias": 0.0,
        "inventory_soft_limit_usd": 60.0,
    },
    "dn": {
        "notional_usd": 50.0,
        "spread_bp": 3.0,
        "interval_seconds": 90,
        "auto_close_on_maintenance": 1.0,
        "funding_entry_mode": "wait",
    },
    "vol": {
        "notional_usd": 100.0,
        "fixed_margin_usd": 100.0,
        "target_volume_usd": 10000.0,
        "leverage": 1.0,
        "interval_seconds": 10,
        "vol_direction": "long",
        "vol_signal_filter_enabled": True,
        "vol_trade_exits_enabled": True,
        "vol_direction_mode": "signal",
        "vol_trade_tp_pct": 0.4,
        "vol_trade_sl_pct": 0.2,
        "vol_hold_min_seconds": 60,
        "vol_hold_max_seconds": 540,
        "vol_ema_len": 50,
        "vol_rsi_len": 14,
        "vol_rsi_long_max": 50.0,
        "vol_rsi_short_min": 50.0,
        "vol_max_spread_bp": 12.0,
        "vol_min_edge_bp": 4.0,
        "tp_pct": 1.0,
        "sl_pct": 1.0,
    },
    "bro": {
        "budget_usd": 500.0, "risk_level": "balanced", "max_positions": 3,
        "interval_seconds": 300, "cycle_seconds": 300,
        "tp_pct": 2.0, "sl_pct": 1.5, "max_loss_pct": 15.0,
        "leverage_cap": 5,
        "min_reward_risk": 1.2,
        "use_sentiment": True, "use_cmc": True, "min_confidence": 0.65,
        "howl_enabled": True, "howl_hour_utc": 2,
    },
}


def normalize_strategy_id(strategy: str | None) -> str:
    sid = str(strategy or "").lower().strip()
    return STRATEGY_ALIASES.get(sid, sid)


def is_supported_strategy(strategy: str | None) -> bool:
    return normalize_strategy_id(strategy) in SUPPORTED_STRATEGIES


def strategy_display_name(strategy: str | None) -> str:
    sid = normalize_strategy_id(strategy)
    return STRATEGY_DISPLAY_NAMES.get(sid, sid.upper() if sid else "STRATEGY")


def infer_strategy_from_text(text: str | None) -> str | None:
    t = f" {(text or '').lower()} "
    for spec in NATURAL_LANGUAGE_STRATEGY_PHRASES:
        if any(phrase in t for phrase in spec.phrases):
            return spec.strategy_id
    return None


def migrate_state_strategy(state: dict) -> dict:
    if not isinstance(state, dict):
        return state
    sid = normalize_strategy_id(str(state.get("strategy") or ""))
    if sid:
        state["strategy"] = sid
    if sid in SUPPORTED_STRATEGIES:
        state["strategy_id_v2"] = int(state.get("strategy_id_v2") or 1)
    return state


def settings_strategy_defaults() -> dict:
    return deepcopy(dict(SETTINGS_STRATEGY_DEFAULTS))


def runtime_strategy_defaults(default_bro_products: list[str] | tuple[str, ...] | None = None) -> dict:
    defaults = deepcopy(dict(RUNTIME_STRATEGY_DEFAULTS))
    if default_bro_products:
        defaults.setdefault("bro", {})["products"] = list(default_bro_products)
    return defaults


def runtime_strategy_default(strategy: str, default_bro_products: list[str] | tuple[str, ...] | None = None) -> dict:
    sid = normalize_strategy_id(strategy)
    defaults = runtime_strategy_defaults(default_bro_products)
    return deepcopy(defaults.get(sid, {"notional_usd": 100.0, "spread_bp": 5.0, "interval_seconds": 60}))
