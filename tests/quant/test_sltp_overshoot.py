"""Pure invariants for the leverage-aware SL overshoot buffer.

No DB or network — exercises ``src.nadobro.quant.sltp_overshoot`` directly. These
pin the buffer the session SL rail (``bot_runtime._evaluate_session_pnl_rail``)
uses to fire early enough that the *realized* loss lands at or under the user's
``sl_pct`` under leverage. Audit: SLTP-OVERSHOOT-BUFFER (2026-08-25 incident —
a 10%-of-$100 stop realized > −$20 at high leverage).
"""
from __future__ import annotations

import pytest

from src.nadobro.quant.sltp_overshoot import (
    effective_sl_trigger,
    leverage_reserve_frac,
    max_buffer_fraction,
    move_bp,
    projected_overshoot_pct,
    sl_buffer_pct,
)


# --- move_bp -----------------------------------------------------------------

def test_move_bp_is_absolute_and_symmetric():
    assert move_bp(100.0, 101.0) == pytest.approx(100.0)   # +1% = 100 bp
    assert move_bp(100.0, 99.0) == pytest.approx(100.0)    # −1% same magnitude


@pytest.mark.parametrize("prev, mark", [(0.0, 100.0), (100.0, 0.0), (-1.0, 100.0)])
def test_move_bp_zero_on_bad_marks(prev, mark):
    # No usable volatility estimate -> 0 -> no buffer (conservative).
    assert move_bp(prev, mark) == 0.0


# --- projected overshoot -----------------------------------------------------

def test_overshoot_scales_with_leverage_and_move():
    # 50x, 20bp move, factor 1.0 -> 50 * 0.002 * 100 = 10.0 %-of-margin.
    assert projected_overshoot_pct(50.0, 20.0, factor=1.0) == pytest.approx(10.0)
    # Double the leverage -> double the overshoot.
    assert projected_overshoot_pct(100.0, 20.0, factor=1.0) == pytest.approx(20.0)


def test_overshoot_zero_when_unknown():
    assert projected_overshoot_pct(0.0, 20.0) == 0.0     # no leverage
    assert projected_overshoot_pct(50.0, 0.0) == 0.0     # no recent move
    assert projected_overshoot_pct(50.0, 20.0, factor=0.0) == 0.0  # disabled


# --- leverage-driven reserve (primary term, no price history) ----------------

def test_leverage_reserve_is_linear_and_capped_at_the_reference():
    assert leverage_reserve_frac(25.0, lev_ref=50.0) == pytest.approx(0.5)
    assert leverage_reserve_frac(50.0, lev_ref=50.0) == pytest.approx(1.0)
    assert leverage_reserve_frac(100.0, lev_ref=50.0) == pytest.approx(1.0)  # capped
    assert leverage_reserve_frac(0.0, lev_ref=50.0) == 0.0                   # unknown


# --- buffer clamping ---------------------------------------------------------

def test_buffer_never_exceeds_the_capped_fraction_of_sl():
    # Huge projected overshoot must be capped at sl_pct * max_fraction.
    buf = sl_buffer_pct(10.0, leverage=100.0, recent_move_bp=500.0,
                        factor=1.0, max_fraction=0.5)
    assert buf == pytest.approx(5.0)          # capped at 50% of the 10% budget


def test_buffer_is_zero_when_calm_even_at_high_leverage():
    # SLTP-EXACT: with NO recent move the buffer is ZERO — there is no leverage-only
    # floor, so a calm high-leverage session keeps its FULL SL budget and the stop
    # fires at the user's exact number (the defect that stopped #253 at half its SL).
    buf = sl_buffer_pct(10.0, leverage=25.0, recent_move_bp=0.0,
                        max_fraction=0.5, lev_ref=50.0)
    assert buf == pytest.approx(0.0)


def test_buffer_zero_when_stop_disarmed():
    assert sl_buffer_pct(0.0, 100.0, 500.0) == 0.0
    assert sl_buffer_pct(-3.0, 100.0, 500.0) == 0.0


# --- effective trigger: only ever tightens, never loosens/inverts ------------

def test_effective_trigger_tightens_under_leverage_and_volatility():
    # sl 10%, 50x, 20bp move: reserve = max(lev 1.0, vol 1.0) capped .5 -> 5%.
    eff = effective_sl_trigger(10.0, leverage=50.0, recent_move_bp=20.0,
                               factor=1.0, max_fraction=0.5, lev_ref=50.0)
    assert eff == pytest.approx(5.0)
    assert 0.0 < eff < 10.0                   # fires earlier, never disarms


def test_effective_trigger_fires_at_exactly_the_users_number_when_calm():
    # SLTP-EXACT: no recent move -> ZERO buffer -> the trigger IS the user's number,
    # at ANY leverage (a calm session realizes exactly -sl_pct, not early).
    assert effective_sl_trigger(10.0, leverage=2.0, recent_move_bp=0.0) == pytest.approx(10.0)
    assert effective_sl_trigger(10.0, leverage=50.0, recent_move_bp=0.0) == pytest.approx(10.0)


def test_effective_trigger_unchanged_when_disarmed():
    assert effective_sl_trigger(0.0, 50.0, 100.0) == 0.0


@pytest.mark.parametrize("sl,lev,move", [(1.0, 20.0, 5.0), (5.0, 10.0, 50.0), (10.0, 50.0, 20.0)])
def test_effective_trigger_bounds(sl, lev, move):
    eff = effective_sl_trigger(sl, lev, move)
    floor = (1.0 - max_buffer_fraction()) * sl
    assert floor - 1e-9 <= eff <= sl + 1e-9   # within [(1-maxfrac)·sl, sl]
