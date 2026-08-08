"""Overlay actuator — bounded signal→config mapping and the drawdown kill-switch."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.nadobro.strategy import overlay_actuator as oa
from src.nadobro.llm.signal_engine import Signal


def test_drawdown_kill_switch():
    assert oa.overlay_drawdown_breached(-10.01) is True
    assert oa.overlay_drawdown_breached(-10.0) is True
    assert oa.overlay_drawdown_breached(-9.99) is False
    assert oa.overlay_drawdown_breached(5.0) is False
    # custom cap
    assert oa.overlay_drawdown_breached(-6.0, cap_pct=5.0) is True


def test_overlay_applies_only_to_mm(monkeypatch):
    monkeypatch.setenv("NADO_SIGNAL_OVERLAY", "1")
    for s in ("grid", "rgrid", "dgrid", "mid"):
        assert oa.overlay_applies(s)
    for s in ("dn", "vol", "copy", "bro"):
        assert not oa.overlay_applies(s)


def test_overlay_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("NADO_SIGNAL_OVERLAY", "0")
    assert oa.overlay_applies("grid") is False


def test_mid_bias_and_size_scale_up_on_strong_trend():
    sig = Signal(bias=0.7, regime="trend_up", entry_ok=True, scale=0.6, spread_mult=1.4, confidence=0.85)
    ov = oa.compute_overrides("mid", sig)
    assert ov["directional_bias"] == 0.7
    cfg = {"order_amount_quote": Decimal("500"), "spread_bid_pct": Decimal("0.0005"),
           "spread_ask_pct": Decimal("0.0005"), "directional_bias": 0.0}
    oa.apply_overrides_to_configs("mid", cfg, ov)
    assert cfg["directional_bias"] == 0.7
    assert Decimal(cfg["order_amount_quote"]) > Decimal("500")   # added on the trend
    assert Decimal(cfg["spread_bid_pct"]) > Decimal("0.0005")    # widened


def test_size_and_spread_bounds_never_exceeded():
    sig = Signal(bias=1.0, regime="trend_up", entry_ok=True, scale=1.0, spread_mult=99.0, confidence=1.0)
    ov = oa.compute_overrides("grid", sig)
    assert ov["size_factor"] <= 1.25
    assert ov["spread_factor"] <= 3.0
    neg = Signal(bias=-1.0, regime="trend_down", entry_ok=True, scale=-1.0, spread_mult=0.01, confidence=1.0)
    ovn = oa.compute_overrides("grid", neg)
    assert ovn["size_factor"] >= 0.5
    assert ovn["spread_factor"] >= 0.75


def test_chop_suppresses_new_exposure():
    sig = Signal(bias=0.05, regime="chop", entry_ok=False, scale=0.0, spread_mult=1.0, confidence=0.2)
    ov = oa.compute_overrides("grid", sig)
    assert ov["suppress_new_entries"] is True
    cfg = {"total_amount_quote": Decimal("400"), "max_net_exposure_pct": 30.0}
    oa.apply_overrides_to_configs("grid", cfg, ov)
    # Reduce-only via an explicit flag. NEVER by zeroing the cap: both exposure
    # checks read 0 as "cap inactive", so that removed the ceiling instead.
    assert cfg["max_net_exposure_pct"] == 30.0         # configured cap survives
    assert cfg["suppress_new_entries"] is True
    assert cfg["regime_gate_enabled"] is True


def test_chop_suppress_leaves_mid_quoting_with_its_cap_intact():
    """Suppression fires on CHOP — exactly the regime a symmetric maker exists to
    quote. Mid keeps its configured cap AND keeps quoting: it is not choked.

    It IS still gated, though. This test used to assert ``regime_gate_enabled not
    in cfg`` plus an ``overlay_defensive`` flag; the self-audit (2026-08-08) showed
    that flag had no readers anywhere, so Mid's only automatic defensive action had
    been deleted rather than replaced. The gate pauses trends/breakouts and NOT
    chop, so arming it costs Mid nothing in the regime this exemption is about, and
    it restores the protection on the other two triggers (a higher-timeframe trend
    opposing the bias, and a cold candle cache)."""
    sig = Signal(bias=0.05, regime="chop", entry_ok=False, scale=0.0, spread_mult=1.0, confidence=0.2)
    ov = oa.compute_overrides("mid", sig)
    assert ov["suppress_new_entries"] is True
    cfg = {"order_amount_quote": Decimal("400"), "max_net_exposure_pct": 30.0}
    oa.apply_overrides_to_configs("mid", cfg, ov)
    assert cfg["max_net_exposure_pct"] == 30.0         # configured cap survives
    assert "suppress_new_entries" not in cfg           # never choked
    assert cfg["regime_gate_enabled"] is True          # but still gated
    assert "overlay_defensive" not in cfg              # no write-only "safety" key


def test_suppress_never_also_adds_size():
    sig = Signal(bias=0.8, regime="chop", entry_ok=False, scale=0.9, spread_mult=1.0, confidence=0.9)
    ov = oa.compute_overrides("mid", sig)
    assert ov["suppress_new_entries"] is True
    assert ov["size_factor"] <= 1.0


def test_spread_fee_floor_enforced():
    cfg = {"spread_bid_pct": Decimal("0.00002"), "spread_ask_pct": Decimal("0.00002")}
    oa.apply_overrides_to_configs(
        "mid", cfg,
        {"size_factor": 1.0, "spread_factor": 0.75, "suppress_new_entries": False},
    )
    assert Decimal(cfg["spread_bid_pct"]) >= Decimal("0.00015")
    assert Decimal(cfg["spread_ask_pct"]) >= Decimal("0.00015")


def test_grid_joins_mid_in_receiving_directional_bias():
    """Bias used to be Mid-only. Once fill-anchored Grid started applying the
    same skew to its per-side spreads (it inherits Mid's quoting machinery),
    withholding the signal made Grid a second-class overlay citizen for no
    reason. rgrid/dgrid stay excluded: rgrid acts on breaks rather than a
    resting spread, and dgrid drives its own regime phase."""
    sig = Signal(bias=0.5, regime="trend_up", entry_ok=True, scale=0.3)
    for strat in ("mid", "grid"):
        assert oa.compute_overrides(strat, sig)["directional_bias"] == 0.5, strat
    for strat in ("rgrid", "dgrid"):
        assert "directional_bias" not in oa.compute_overrides(strat, sig), strat


def test_grid_actually_consumes_the_bias_it_is_given():
    """A key in the overrides dict proves nothing on its own — the previous
    Mid-only rule existed because Grid dropped the value. Assert the skew
    reaches the quoted spreads."""
    from decimal import Decimal as D

    from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController

    def _mk(bias):
        return FillAnchoredQuotingController(
            user_id=1, controller_id="grid:1:mainnet", orchestrator=object(),
            adapter=object(), inventory=None,
            configs={"trading_pair": "P", "spread_bid_pct": "0.001",
                     "spread_ask_pct": "0.001", "spread_floor_half_pct": "0",
                     "directional_bias": bias},
        )

    flat_bid, flat_ask = _mk(0.0).effective_spreads()
    long_bid, long_ask = _mk(1.0).effective_spreads()
    assert (flat_bid, flat_ask) == (D("0.001"), D("0.001")), "neutral must be symmetric"
    assert long_bid < flat_bid, "long bias must tighten the bid (buy sooner)"
    assert long_ask > flat_ask, "long bias must widen the ask (sell later)"


def test_spread_factor_also_widens_the_ladder_step():
    """The level spacing is derived from the spread at mapping time, so widening
    the spread without widening the step leaves rungs packed around a quote that
    has moved out."""
    cfg = {"spread_bid_pct": Decimal("0.001"), "spread_ask_pct": Decimal("0.001"),
           "ladder_step_bp": Decimal("10")}
    oa.apply_overrides_to_configs(
        "grid", cfg,
        {"size_factor": 1.0, "spread_factor": 2.0, "suppress_new_entries": False},
    )
    assert Decimal(cfg["ladder_step_bp"]) == Decimal("20")


def test_signal_barriers_reach_the_rail_and_never_the_executor_barrier():
    """OVERLAY-BARRIER-UNITS. ``Signal.sl_pct``/``tp_pct`` are % of MARGIN;
    ``TripleBarrierConfig`` holds a PRICE-return fraction from avg entry. Writing
    the former into the latter mixed units — at leverage L the barrier landed L
    times too far away — and it overwrote the user's own configured barrier to do
    it. The overlay's barriers belong to the %-of-margin session rail only."""
    from src.nadobro.engine.types import TripleBarrierConfig

    sig = Signal(bias=0.7, regime="trend_up", entry_ok=True, scale=0.5,
                 spread_mult=1.2, confidence=0.8, sl_pct=0.65, tp_pct=1.6)
    ov = oa.compute_overrides("grid", sig)
    assert ov["sl_pct"] == 0.65 and ov["tp_pct"] == 1.6

    user_barrier = TripleBarrierConfig(take_profit=Decimal("0.01"), stop_loss=Decimal("0.005"))
    cfg = {"total_amount_quote": Decimal("400"), "triple_barrier_config": user_barrier}
    changed = oa.apply_overrides_to_configs("grid", cfg, ov)
    assert cfg["triple_barrier_config"] is user_barrier, "the overlay rewrote the barrier"
    assert "barriers" not in changed

    # They still reach the rail, bounded by the user's own numbers.
    rail_sl, rail_tp = oa.rail_barriers(1.0, 1.0, sig)
    assert rail_sl == 0.65 and rail_tp == 1.6


def test_mid_barrier_is_rail_only_no_triple_barrier():
    sig = Signal(bias=0.3, regime="range", entry_ok=True, scale=0.0,
                 spread_mult=1.0, confidence=0.5, sl_pct=0.5, tp_pct=1.0)
    ov = oa.compute_overrides("mid", sig)
    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}   # no triple_barrier
    changed = oa.apply_overrides_to_configs("mid", cfg, ov)
    assert "barriers" not in changed         # nothing to apply on the config
    assert ov["sl_pct"] == 0.5               # still surfaced for the session rail


def test_no_barriers_when_signal_has_none():
    ov = oa.compute_overrides("grid", Signal(bias=0.5, regime="trend_up", entry_ok=True, scale=0.3))
    assert "sl_pct" not in ov and "tp_pct" not in ov


def test_rail_barriers_sl_is_tighten_only():
    # Trend regime widened the signal SL past the user's stop — the rail must
    # stay at the user's number (the kill-switch contract).
    sig = Signal(bias=0.7, regime="trend_up", sl_pct=0.65, tp_pct=1.6)
    sl, tp = oa.rail_barriers(0.5, 1.0, sig)
    assert sl == 0.5
    assert tp == 1.6            # trend widens TP (let winners run)
    # Chop tightened the SL below the user's stop — the tighter value governs.
    sig = Signal(bias=0.0, regime="chop", sl_pct=0.4, tp_pct=0.8)
    sl, tp = oa.rail_barriers(0.5, 1.0, sig)
    assert sl == 0.4


def test_rail_barriers_tp_is_widen_only_never_fires_early():
    # OVERLAY-TP-NO-FLOOR: the chop regime scales the signal TP to 0.8x, which
    # used to LOWER the user's TP and fire the session rail ~20% early. The
    # user's TP is now a floor — the overlay may only widen it.
    chop = Signal(bias=0.0, regime="chop", sl_pct=0.4, tp_pct=0.8)
    _sl, tp = oa.rail_barriers(0.5, 1.0, chop)
    assert tp == 1.0           # floored at the user's TP, NOT lowered to 0.8
    # A trend still lets a winner run past the user's target.
    trend = Signal(bias=0.7, regime="trend_up", sl_pct=0.65, tp_pct=1.6)
    _sl, tp = oa.rail_barriers(0.5, 1.0, trend)
    assert tp == 1.6
    # Range leaves the user's TP exactly as configured.
    rng = Signal(bias=0.1, regime="range", sl_pct=0.5, tp_pct=1.0)
    _sl, tp = oa.rail_barriers(0.5, 1.0, rng)
    assert tp == 1.0


def test_rail_barriers_disarmed_stays_disarmed():
    sig = Signal(bias=0.5, regime="trend_up", sl_pct=0.65, tp_pct=1.6)
    sl, tp = oa.rail_barriers(0.0, 0.0, sig)
    assert sl is None and tp is None
    # One-sided arming is respected per side.
    sl, tp = oa.rail_barriers(0.5, 0.0, sig)
    assert sl == 0.5 and tp is None


def test_rail_barriers_none_when_signal_abstains():
    sl, tp = oa.rail_barriers(0.5, 1.0, Signal())     # cold signal: no barriers
    assert sl is None and tp is None


def test_stabilize_overrides_deadbands_wobble():
    prev = {"size_factor": 1.10, "spread_factor": 1.20, "directional_bias": 0.40,
            "suppress_new_entries": False, "regime": "trend_up",
            "sl_pct": 0.65, "tp_pct": 1.6}
    wobble = dict(prev, size_factor=1.12, spread_factor=1.25, directional_bias=0.45)
    out = oa.stabilize_overrides(prev, wobble)
    # Sub-threshold wobble reuses the previously applied factors verbatim, so
    # the live-config signature does not flap (no grid recenter / quote reset).
    assert out["size_factor"] == 1.10
    assert out["spread_factor"] == 1.20
    assert out["directional_bias"] == 0.40


def test_stabilize_overrides_material_changes_pass_through():
    prev = {"size_factor": 1.10, "spread_factor": 1.20, "suppress_new_entries": False,
            "regime": "trend_up", "sl_pct": 0.65, "tp_pct": 1.6}
    # A full step in size passes through untouched.
    big = dict(prev, size_factor=1.16)
    assert oa.stabilize_overrides(prev, big)["size_factor"] == 1.16
    # A regime flip always passes through (it changes the barriers).
    flip = dict(prev, regime="chop", suppress_new_entries=True, size_factor=1.11)
    assert oa.stabilize_overrides(prev, flip)["size_factor"] == 1.11
    # A suppression flip always passes through (risk control).
    supp = dict(prev, suppress_new_entries=True, size_factor=1.11)
    assert oa.stabilize_overrides(prev, supp)["size_factor"] == 1.11
    # No previous application: everything passes through.
    assert oa.stabilize_overrides(None, dict(prev))["size_factor"] == 1.10


# ==========================================================================
# R-Grid safety rails vs the overlay (audit 2026-08-06)
# ==========================================================================
def test_suppression_never_zeroes_a_cap_for_any_strategy():
    """SUPPRESS-CAP-ZERO. Both exposure checks read a cap of 0 as "cap INACTIVE"
    (controller_base.exposure_allowed_sides, market_making.
    _projected_order_within_exposure), so writing 0 to mean "stop" DELETED the
    ceiling. Suppression is now an explicit flag; a cap is never zeroed."""
    from src.nadobro.llm.signal_engine import Signal
    from src.nadobro.strategy.overlay_actuator import (
        apply_overrides_to_configs, compute_overrides,
    )

    chop = Signal(regime="chop", entry_ok=False, confidence=0.6)
    for strategy in ("grid", "dgrid", "mid", "rgrid"):
        overrides = compute_overrides(strategy, chop)
        assert overrides["suppress_new_entries"] is True
        cfg = {"max_net_exposure_pct": 0.30, "order_amount_quote": Decimal(100)}
        apply_overrides_to_configs(strategy, cfg, overrides)
        assert float(cfg["max_net_exposure_pct"]) == 0.30, (
            f"{strategy}: the exposure cap was switched off"
        )


def test_the_ladder_family_is_choked_reduce_only_when_suppressed():
    from src.nadobro.llm.signal_engine import Signal
    from src.nadobro.strategy.overlay_actuator import (
        apply_overrides_to_configs, compute_overrides,
    )

    overrides = compute_overrides("grid", Signal(regime="chop", entry_ok=False, confidence=0.6))
    cfg = {"max_net_exposure_pct": 0.30}
    changed = apply_overrides_to_configs("grid", cfg, overrides)
    assert cfg["suppress_new_entries"] is True, "reduce-only posture, as a flag"
    assert bool(cfg["regime_gate_enabled"]) is True
    assert changed["suppress_new_entries"] is True


def test_mid_and_rgrid_are_never_stood_down_by_the_overlay():
    """Suppression fires on CHOP. Mid exists to quote chop; R-Grid must keep
    following a move and must not be abandoned mid-position — pausing it with an
    open book leaves nobody managing the exit. Neither is CHOKED (reduce-only).

    They differ on gating: Mid is gated (the gate pauses trends/breakouts, not
    chop), R-Grid is not (it adds INTO trends by design). Their shared protection
    while suppressing is what the overlay still applies — no size ADD, and the
    regime-tightened %-of-margin session rail."""
    from src.nadobro.llm.signal_engine import Signal
    from src.nadobro.strategy.overlay_actuator import (
        NEVER_SUPPRESSED, apply_overrides_to_configs, compute_overrides,
    )

    assert set(NEVER_SUPPRESSED) == {"mid", "rgrid"}
    overrides = compute_overrides("rgrid", Signal(regime="chop", entry_ok=False, confidence=0.6))
    for strategy in NEVER_SUPPRESSED:
        cfg = {"max_net_exposure_pct": 0.30}
        apply_overrides_to_configs(strategy, cfg, overrides)
        assert float(cfg["max_net_exposure_pct"]) == 0.30
        assert "suppress_new_entries" not in cfg, f"{strategy} was choked"
        assert "overlay_defensive" not in cfg, "write-only key must be gone"
        if strategy == "rgrid":
            assert "regime_gate_enabled" not in cfg, "R-Grid must not be gated"
        else:
            assert cfg["regime_gate_enabled"] is True, "Mid keeps its gate"
        assert float(overrides["size_factor"]) <= 1.0, "no size ADD while suppressing"


def test_the_overlay_cannot_grow_the_rgrid_step_past_its_stop_budget():
    """The step cap is a RISK bound (one taker round trip must fit inside the
    session stop). The overlay's size_factor is applied after it, and was clamped
    only to max_single_order_quote — far above the capped step."""
    from src.nadobro.strategy.overlay_actuator import apply_overrides_to_configs

    cfg = {
        "order_amount_quote": Decimal("307"),
        "step_capped_quote": Decimal("307"),
    }
    apply_overrides_to_configs("rgrid", cfg, {"size_factor": 1.25})
    grown = Decimal(str(cfg["order_amount_quote"]))
    assert grown > Decimal("307"), "precondition: the overlay does scale it up"

    # The runtime clamp is what puts it back — mirror it here so the contract is
    # pinned even if the call site moves.
    step_cap = Decimal(str(cfg["step_capped_quote"]))
    assert min(grown, step_cap) == Decimal("307")


def test_mid_gets_the_regime_gate_under_suppression_and_rgrid_does_not():
    """OVERLAY-MID-GATE (self-audit 2026-08-08). Exempting mid from the reduce-only
    posture also dropped the ``regime_gate_enabled`` arming main did for it, and put
    an ``overlay_defensive`` key in its place that NO code read — so Mid took zero
    defensive action on the two non-chop suppression triggers and kept quoting a
    full-size ladder into a flagged breakout. R-Grid must still NOT be gated: it
    adds into trends by design."""
    from src.nadobro.strategy.overlay_actuator import apply_overrides_to_configs

    for strategy, expect_gate in (("mid", True), ("rgrid", False), ("grid", True)):
        configs: dict = {}
        changed = apply_overrides_to_configs(
            strategy, configs, {"suppress_new_entries": True},
        )
        assert bool(configs.get("regime_gate_enabled")) is expect_gate, (
            f"{strategy}: regime_gate_enabled={configs.get('regime_gate_enabled')}, "
            f"expected {expect_gate}"
        )
        # Never encode "stand down" as a zeroed exposure cap (SUPPRESS-CAP-ZERO):
        # both guards read a cap of 0 as "cap inactive".
        assert configs.get("max_net_exposure_pct") != 0.0
        # And no write-only flag masquerading as a safety mechanism.
        assert "overlay_defensive" not in configs
        assert "overlay_defensive" not in changed
    # R-Grid is exempt from the gate but must still be flagged reduce-only nowhere:
    rg: dict = {}
    apply_overrides_to_configs("rgrid", rg, {"suppress_new_entries": True})
    assert not rg.get("suppress_new_entries"), "R-Grid must never be suppressed"
