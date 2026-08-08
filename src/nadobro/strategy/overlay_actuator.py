"""Financial-overlay actuator — map a :class:`Signal` onto the live MM
controller config, bounded so it can never exceed the user's own settings.

Runs in the background for the four MM strategies (grid / rgrid / dgrid / mid);
the normal user configures nothing. It only ever turns knobs the controllers
already consume (directional_bias, per-side spread, order size, an explicit
reduce-only flag), so no controller code changes and every adjustment is inside
the same rails the user's config already lives behind. The session SL/TP rail and
a separate overlay-drawdown kill-switch are the backstops.

Two hard rules, both learned from bugs:

* **Never encode "stop" as a zeroed limit.** ``max_net_exposure_pct = 0`` reads as
  "cap INACTIVE" in both exposure checks, so it REMOVED the ceiling it was meant
  to tighten. Suppression is its own flag.
* **Never write a %-of-margin number into a price-move field.** ``Signal.sl_pct``
  / ``tp_pct`` are % of margin; ``TripleBarrierConfig`` holds a price return from
  avg entry. They reach the %-of-margin session rail (``rail_barriers``) and
  nothing else.

Everything here is pure and deterministic; the runtime does the I/O (candle
fetch, persistence) and the flatten.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Mapping, Optional, Tuple

from src.nadobro.core.feature_flags import env_flag
from src.nadobro.llm.signal_engine import Signal

OVERLAY_STRATEGIES = ("grid", "rgrid", "dgrid", "mid")

# Strategies the overlay must NEVER stand down, because the regime that triggers
# suppression is the regime they exist to trade:
#   * mid  — suppression fires on CHOP, which is exactly what a symmetric maker
#            quotes. Standing it down leaves it dark for no benefit.
#   * rgrid — Reverse Grid's whole thesis is following a move. Pausing it in a
#            trend is backwards, and pausing it mid-position leaves the book
#            exposed with no one managing the exit. Its risk reduction is what
#            still applies while suppressing: no size ADD (size_factor is clamped
#            to <= 1.0) and the regime-tightened %-of-margin session rail. It is
#            NOT "a tightened trailing soft reset" — nothing reads such a signal;
#            saying so once made a dead config key look like a safety mechanism.
# Mid additionally arms the regime gate, which pauses trends/breakouts and not
# chop. Both keep their configured net-exposure cap and their session SL/TP rails.
NEVER_SUPPRESSED = ("mid", "rgrid")

# Overlay-specific max drawdown (% of session margin). Independent of, and
# additional to, the user's session SL — EITHER trips flatten + stand-down.
OVERLAY_DRAWDOWN_CAP_PCT = 10.0

# Bounds — the overlay can shade size and spread, never blow past the user's
# posture. Size can add up to +25% on a strong trend or cut to 50%; the venue
# leverage/exposure caps + session rails remain the hard limits downstream.
_SIZE_LO, _SIZE_HI = 0.5, 1.25
_SPREAD_LO, _SPREAD_HI = 0.75, 3.0
# Per-side spread can never quote through this fee-clearing floor.
_SPREAD_FLOOR_FRACTION = Decimal("0.00015")


def overlay_enabled() -> bool:
    """Background overlay is ON by default; operators can disable via env."""
    return env_flag("NADO_SIGNAL_OVERLAY", True)


def overlay_applies(strategy: str) -> bool:
    return overlay_enabled() and str(strategy or "").lower() in OVERLAY_STRATEGIES


def overlay_drawdown_breached(
    session_pnl_pct_net: float, cap_pct: float = OVERLAY_DRAWDOWN_CAP_PCT
) -> bool:
    """True when the session's net drawdown has breached the overlay cap."""
    try:
        return cap_pct > 0 and float(session_pnl_pct_net) <= -abs(float(cap_pct))
    except (TypeError, ValueError):
        return False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_overrides(strategy: str, signal: Signal) -> Dict[str, object]:
    """Bounded, controller-agnostic overrides derived from the signal.

    ``size_factor``  0.5..1.25 multiplier on per-order / ladder notional.
    ``spread_factor`` 0.75..3.0 multiplier on the quoted per-side spread.
    ``directional_bias``  only for Mid (it consumes a continuous bias).
    ``suppress_new_entries``  True -> choke NEW exposure (reduce-only posture).
        Mid instead arms the regime gate; R-Grid is exempt (see NEVER_SUPPRESSED).
    """
    strat = str(strategy or "").lower()
    # Size: add on the favoured side only on a confident trend; trim otherwise.
    # scale in [-1,1]; positive -> larger, negative -> smaller.
    size_factor = _clamp(1.0 + 0.25 * float(signal.scale) * float(signal.confidence),
                         _SIZE_LO, _SIZE_HI)
    spread_factor = _clamp(float(signal.spread_mult), _SPREAD_LO, _SPREAD_HI)
    suppress = (not signal.entry_ok) or signal.regime == "chop"
    # When suppressing, do not also add size.
    if suppress:
        size_factor = min(size_factor, 1.0)
    overrides: Dict[str, object] = {
        "size_factor": round(size_factor, 4),
        "spread_factor": round(spread_factor, 4),
        "suppress_new_entries": bool(suppress),
        "regime": signal.regime,
        "bias": float(signal.bias),
        "confidence": float(signal.confidence),
    }
    # Regime-adjusted barriers, derived from the user's base SL/TP (trend widens,
    # chop tightens). These are % of MARGIN and go ONLY to the %-of-margin session
    # rail via rail_barriers() — never to a price-move barrier. The 10% overlay
    # drawdown cap is the hard backstop over both.
    if signal.sl_pct is not None:
        overrides["sl_pct"] = float(signal.sl_pct)
    if signal.tp_pct is not None:
        overrides["tp_pct"] = float(signal.tp_pct)
    # Continuous directional bias. Grid joined Mid here once the ladder landed:
    # fill-anchored Grid inherits Mid's quoting machinery and now applies the
    # same bias skew to its per-side spreads, so withholding the signal made
    # Grid a second-class citizen of the overlay for no reason. rgrid/dgrid are
    # excluded on purpose — rgrid is taker-momentum (it acts on breaks, not on
    # a resting spread) and dgrid drives its own regime phase.
    if strat in ("mid", "grid"):
        overrides["directional_bias"] = _clamp(float(signal.bias), -1.0, 1.0)
    return overrides


def rail_barriers(
    base_sl_pct: float, base_tp_pct: float, signal: Signal
) -> Tuple[Optional[float], Optional[float]]:
    """Session-rail barriers derived from the signal, bounded by the user's own
    config. Both barriers respect the user's setting as the binding contract:

    * SL is TIGHTEN-ONLY (chop ×0.8): the overlay may pull the stop closer but
      never widen it past the configured stop.
    * TP is WIDEN-ONLY: the overlay may let a winner run PAST the user's target
      in a trend (×1.6), but must never take profit BEFORE it — the chop-regime
      ×0.8 tightening used to lower the user's TP silently and fire the session
      rail early (OVERLAY-TP-NO-FLOOR). The user's TP is the floor.

    A barrier the user disarmed (``<= 0``) stays disarmed — the 10% overlay
    drawdown cap is the backstop either way."""
    sl: Optional[float] = None
    tp: Optional[float] = None
    if base_sl_pct > 0 and signal.sl_pct is not None:
        sl = min(float(signal.sl_pct), float(base_sl_pct))
    if base_tp_pct > 0 and signal.tp_pct is not None:
        # Floor at the user's configured TP: the overlay only widens it.
        tp = max(float(signal.tp_pct), float(base_tp_pct))
    return sl, tp


def stabilize_overrides(
    prev: Optional[Mapping[str, object]], overrides: Dict[str, object]
) -> Dict[str, object]:
    """Dead-band the continuous override factors against the previously APPLIED
    values. Every applied change flips the live-config signature, and each flip
    recenters the grid ladder / resets Mid quotes — so a 4th-decimal wobble in
    bias/ATR must not churn live orders every candle refresh. Regime flips,
    suppression flips and barrier changes always pass through (risk controls);
    size/spread/bias move only in ≥ 0.05 / 0.10 / 0.10 steps."""
    if not isinstance(prev, Mapping):
        return overrides

    def _n(value: object, default: float = 0.0) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    has_bias_prev = "directional_bias" in prev
    has_bias_new = "directional_bias" in overrides
    material = (
        str(prev.get("regime")) != str(overrides.get("regime"))
        or bool(prev.get("suppress_new_entries")) != bool(overrides.get("suppress_new_entries"))
        or prev.get("sl_pct") != overrides.get("sl_pct")
        or prev.get("tp_pct") != overrides.get("tp_pct")
        or has_bias_prev != has_bias_new
        or abs(_n(prev.get("size_factor"), 1.0) - _n(overrides.get("size_factor"), 1.0)) >= 0.05
        or abs(_n(prev.get("spread_factor"), 1.0) - _n(overrides.get("spread_factor"), 1.0)) >= 0.10
        or (has_bias_new
            and abs(_n(prev.get("directional_bias")) - _n(overrides.get("directional_bias"))) >= 0.10)
    )
    if material:
        return overrides
    out = dict(overrides)
    for key in ("size_factor", "spread_factor", "directional_bias"):
        if key in prev and key in out:
            out[key] = prev[key]
    return out


# The subset of override keys whose APPLIED values must stay sticky across
# cycles (persisted in state as ``overlay_applied`` and compared by
# ``stabilize_overrides``).
APPLIED_OVERRIDE_KEYS = (
    "size_factor", "spread_factor", "directional_bias",
    "suppress_new_entries", "regime", "sl_pct", "tp_pct",
)


def _mul_dec(configs: Dict[str, object], key: str, factor: float) -> bool:
    val = configs.get(key)
    if val is None:
        return False
    try:
        configs[key] = Decimal(str(val)) * Decimal(str(factor))
        return True
    except Exception:  # noqa: BLE001 - leave the config untouched on a bad value
        return False


def apply_overrides_to_configs(
    strategy: str, configs: Dict[str, object], overrides: Dict[str, object]
) -> Dict[str, object]:
    """Mutate the mapped ``configs`` in place with the bounded overrides, using
    ONLY keys the controllers already consume. Returns a compact record of what
    changed (for persistence). Fee-floors the spread; chokes new exposure via
    the existing net-exposure cap when suppressing."""
    changed: Dict[str, object] = {}

    size_factor = float(overrides.get("size_factor", 1.0) or 1.0)
    if abs(size_factor - 1.0) > 1e-9:
        # Grid family sizes the ladder via total_amount_quote; Mid + fill-anchored
        # via order_amount_quote. Scale whichever the mapped config carries.
        for key in ("order_amount_quote", "total_amount_quote"):
            if _mul_dec(configs, key, size_factor):
                changed[key] = str(configs[key])

    spread_factor = float(overrides.get("spread_factor", 1.0) or 1.0)
    if abs(spread_factor - 1.0) > 1e-9:
        for key in ("spread_bid_pct", "spread_ask_pct", "min_spread_between_orders"):
            if key in configs and _mul_dec(configs, key, spread_factor):
                # Never quote through the fee floor.
                try:
                    if Decimal(str(configs[key])) < _SPREAD_FLOOR_FRACTION:
                        configs[key] = _SPREAD_FLOOR_FRACTION
                except Exception:  # noqa: BLE001
                    pass
                changed[key] = str(configs[key])
        # LADDER: the level spacing is derived from the quoted spread at mapping
        # time, so widening the spread without widening the step leaves the
        # levels packed around a quote that has moved out — an internally
        # inconsistent book. Scale it by the same factor. No fee floor here:
        # this value is in BASIS POINTS, and clamping it against the spread
        # floor (a fraction) would be a unit error, not a safety net.
        if _mul_dec(configs, "ladder_step_bp", spread_factor):
            changed["ladder_step_bp"] = str(configs["ladder_step_bp"])

    if "directional_bias" in overrides:
        configs["directional_bias"] = float(overrides["directional_bias"])
        changed["directional_bias"] = configs["directional_bias"]

    # OVERLAY-BARRIER-UNITS: the overlay does NOT touch ``triple_barrier_config``.
    #
    # ``Signal.sl_pct`` / ``tp_pct`` are "% of MARGIN" (llm/signal_engine.py) while
    # ``TripleBarrierConfig.stop_loss`` / ``take_profit`` are "return fractions
    # relative to entry" (engine/types.py) — a PRICE move, evaluated as
    # ``avg * (1 - stop_loss)``. Dividing a %-of-margin number by 100 and storing it
    # as a price fraction mixed the two: at leverage L the resulting barrier sat L
    # times too far away (0.8% of margin at 49x became a 0.8% PRICE stop = ~39% of
    # margin), and it OVERWROTE the user's own configured barrier to do it.
    #
    # The overlay's regime-adjusted SL/TP are %-of-margin by construction, so they
    # go to the %-of-margin SESSION rail and nowhere else — ``rail_barriers()`` ->
    # ``state["overlay_sl_pct"]`` / ``state["overlay_tp_pct"]``, which the runtime
    # already applies. One number, one unit, one enforcement point.

    if overrides.get("suppress_new_entries"):
        strat = str(strategy or "").lower()
        if strat == "mid":
            # Mid keeps its exposure cap and keeps QUOTING (suppression fires on
            # chop, the regime a symmetric maker exists for) — but it still gets
            # the regime gate, which pauses new opens on trends/breakouts and NOT
            # on chop. main armed this for mid; an earlier pass on this branch
            # replaced it with an ``overlay_defensive`` flag that nothing read, so
            # Mid took ZERO defensive action on the two non-chop triggers
            # (signal_engine: a higher-timeframe trend opposing the bias, and a
            # cold candle cache) and kept quoting a full-size ladder into a
            # flagged breakout.
            configs["regime_gate_enabled"] = True
            changed["regime_gate_enabled"] = True
        elif strat in NEVER_SUPPRESSED:
            # R-Grid: exempt outright. It is a momentum strategy that ADDS into
            # trends, so arming a trend gate would pause it exactly where it is
            # supposed to work (a product ruling). Its risk reduction comes from
            # the overlay's other levers, which still apply: no size ADD while
            # suppressing (size_factor is clamped to <= 1.0 above) and the
            # regime-tightened %-of-margin session rail via rail_barriers().
            pass
        else:
            # Reduce-only posture as an EXPLICIT flag honoured by
            # Controller.exposure_allowed_sides, plus the regime gate.
            #
            # SUPPRESS-CAP-ZERO: this used to write ``max_net_exposure_pct = 0``.
            # BOTH exposure checks read a cap of 0 as "cap INACTIVE"
            # (controller_base.exposure_allowed_sides,
            # market_making._projected_order_within_exposure), so "suppression"
            # DELETED the ceiling instead of tightening it. Never encode "stop"
            # as a zeroed limit again.
            configs["suppress_new_entries"] = True
            configs["regime_gate_enabled"] = True
            changed["suppress_new_entries"] = True

    return changed
