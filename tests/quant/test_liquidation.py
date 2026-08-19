"""Pure liquidation-math invariants for the strategy leverage guard.

No DB or network — exercises ``src.nadobro.quant.liquidation`` directly. These
pin the math the pre-start guard (``bot_runtime._run_mm_start_guard``) and the
live proximity rail (``_evaluate_session_pnl_rail`` via
``_liq_proximity_tripped``) depend on.
"""
from __future__ import annotations

import math

import pytest

from src.nadobro.quant.liquidation import (
    LiqSafety,
    consumed_runway,
    fallback_mmf,
    leverage_requires_armed_sl,
    liquidation_safety,
    max_safe_leverage,
    move_to_liquidation,
    safe_max_sl_pct,
)


# --- fallback mmf ------------------------------------------------------------

def test_fallback_mmf_is_conservative_over_the_empirical_estimate():
    # Empirical Vertex-family mmf ~ imf/2; the fallback uses 0.75·imf, which
    # OVER-estimates the maintenance requirement -> smaller buffer -> the guard
    # fires earlier (safe direction).
    for max_lev in (20, 40, 50):
        imf = 1.0 / max_lev
        mmf = fallback_mmf(imf)
        assert mmf >= imf * 0.5           # never under the empirical estimate
        assert mmf < imf                  # maintenance is always looser than initial
        assert mmf == pytest.approx(max(imf * 0.75, 0.005))


def test_fallback_mmf_floor_protects_tiny_imf():
    # Extreme leverage: imf tiny -> the absolute floor keeps mmf non-zero so the
    # buffer math never assumes "no maintenance requirement".
    assert fallback_mmf(1.0 / 500.0) == pytest.approx(0.005)
    assert fallback_mmf(0.0) == pytest.approx(0.005)


# --- core formulas -----------------------------------------------------------

def test_move_to_liquidation_and_loss():
    mmf = 0.015
    assert move_to_liquidation(50, mmf) == pytest.approx(1 / 50 - mmf)   # 0.5% move
    # In %-of-margin terms: (1 - mmf*L)*100.
    from src.nadobro.quant.liquidation import liq_loss_pct
    assert liq_loss_pct(50, mmf) == pytest.approx((1 - mmf * 50) * 100)


def test_safe_max_sl_and_its_leverage_inverse_are_consistent():
    mmf = 0.015
    k, cushion = 0.5, 1.0
    s_star = safe_max_sl_pct(50, mmf, k=k, cushion_pct=cushion)
    assert s_star == pytest.approx(100 * k * (1 - mmf * 50) - cushion)
    # L* at exactly S* should round-trip back to ~50x.
    l_star = max_safe_leverage(s_star, mmf, k=k, cushion_pct=cushion)
    assert l_star == pytest.approx(50.0, rel=1e-6)


def test_max_safe_leverage_is_infinite_without_a_maintenance_requirement():
    assert max_safe_leverage(10.0, 0.0) == math.inf


# --- the decision ------------------------------------------------------------

def test_tight_stop_at_high_leverage_is_safe():
    mmf = fallback_mmf(1 / 50)
    v = liquidation_safety(leverage=50, mmf=mmf, sl_pct=5.0, sl_armed=True)
    assert isinstance(v, LiqSafety) and v.ok and v.reason == ""


def test_loose_stop_at_high_leverage_is_blocked_with_actionable_escapes():
    mmf = fallback_mmf(1 / 50)
    v = liquidation_safety(leverage=50, mmf=mmf, sl_pct=40.0, sl_armed=True)
    assert not v.ok and v.reason == "sl_too_loose"
    assert 0 < v.safe_max_sl_pct < 40.0            # a tighter stop is offered
    assert 1.0 <= v.max_safe_leverage < 50.0       # or a lower leverage


def test_disarmed_stop_forbidden_above_l_arm_allowed_below():
    mmf = fallback_mmf(1 / 50)
    l_arm = leverage_requires_armed_sl(mmf)
    hi = liquidation_safety(leverage=math.ceil(l_arm) + 1, mmf=mmf, sl_pct=0.0, sl_armed=False)
    assert not hi.ok and hi.reason == "disarmed_sl_high_lev"
    lo = liquidation_safety(leverage=max(1.0, l_arm - 1), mmf=mmf, sl_pct=0.0, sl_armed=False)
    assert lo.ok            # below L_arm a disarmed stop relies on the live rail


def test_default_grid_config_at_pair_max_is_not_blocked():
    # A default grid ships an armed 0.5%-of-margin stop; at pair max that must
    # remain safe (guard only bites disarmed/loose stops), else every grid breaks.
    for max_lev in (20, 40, 50):
        mmf = fallback_mmf(1 / max_lev)
        v = liquidation_safety(leverage=max_lev, mmf=mmf, sl_pct=0.5, sl_armed=True)
        assert v.ok, (max_lev, v.reason)


# --- live proximity runway ---------------------------------------------------

def test_consumed_runway_long_and_short():
    # long: entry 100, liq 98 (runway 2). mark 99 -> half consumed.
    assert consumed_runway(100, 99, 98, 1.0) == pytest.approx(0.5)
    # short: entry 100, liq 102 (runway 2). mark 101.6 -> 0.8 consumed.
    assert consumed_runway(100, 101.6, 102, -1.0) == pytest.approx(0.8)


def test_consumed_runway_rejects_bad_data():
    assert consumed_runway(100, 99, 0, 1.0) is None      # no liq
    assert consumed_runway(100, 99, 101, 1.0) is None     # wrong-side liq on a long
    assert consumed_runway(100, 101, 99, -1.0) is None    # wrong-side liq on a short
    assert consumed_runway(100, 99, 100, 1.0) is None     # degenerate zero runway
    assert consumed_runway(100, 99, 98, 0.0) is None      # flat position
