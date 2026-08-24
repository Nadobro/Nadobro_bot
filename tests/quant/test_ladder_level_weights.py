"""Support/resistance reshaping of the ladder.

The invariant that must survive: ``plan_ladder`` REDISTRIBUTES a side's
deployment and never adds to it. Reshaping by price level cannot be allowed to
break that, nor to smuggle a sub-minimum rung past the stepdown the curve
weights already have to clear.
"""
from decimal import Decimal

import pytest

from src.nadobro.quant.ladder import (
    ladder_notional,
    plan_ladder,
    proximity_weights,
)


def _plan(**kw):
    base = dict(deployed_quote=Decimal(1000), levels=4, step_bp=10,
                curve="flat", min_notional=0)
    base.update(kw)
    deployed = base.pop("deployed_quote")
    return plan_ladder(deployed, **base)


# --- proximity weights ------------------------------------------------------

def test_a_rung_on_a_level_is_weighted_up():
    # 98 sits ON the level; 99 is ~101bp away and 100 ~204bp, both inside the
    # 250bp tolerance, so the boost decays with distance.
    weights = proximity_weights([100.0, 99.0, 98.0], [98.0], tolerance_bp=250, boost=2.0)
    assert weights[2] > weights[1] > weights[0] > Decimal(1)
    # Outside the tolerance there is no reshaping at all.
    assert proximity_weights([100.0], [98.0], tolerance_bp=50)[0] == Decimal(1)


def test_a_rung_far_from_every_level_is_untouched():
    assert proximity_weights([100.0, 99.0], [50.0], tolerance_bp=25) == [
        Decimal(1), Decimal(1)]


def test_the_boost_falls_off_linearly_rather_than_stepping():
    # A rung must not jump in size as a level drifts one bp closer.
    on_it = proximity_weights([100.0], [100.0], tolerance_bp=100, boost=2.0)[0]
    halfway = proximity_weights([100.0], [100.5], tolerance_bp=100, boost=2.0)[0]
    edge = proximity_weights([100.0], [101.0], tolerance_bp=100, boost=2.0)[0]
    assert on_it == pytest.approx(Decimal(2))
    assert Decimal(1) < halfway < on_it
    assert edge == pytest.approx(Decimal(1), abs=Decimal("0.01"))


def test_no_levels_means_no_reshaping():
    assert proximity_weights([100.0, 99.0], []) == [Decimal(1), Decimal(1)]
    assert proximity_weights([], [100.0]) == []


def test_junk_levels_are_ignored_rather_than_reshaping_the_book():
    assert proximity_weights([100.0], [0.0, -5.0], tolerance_bp=1000) == [Decimal(1)]


# --- the ladder invariant ---------------------------------------------------

def test_reshaping_never_changes_the_side_total():
    # THE invariant. Weighting rungs must move size between them, never add.
    plain = _plan()
    shaped = _plan(level_weights=[Decimal(1), Decimal(3), Decimal(1), Decimal(2)])
    assert ladder_notional(plain) == Decimal(1000)
    assert ladder_notional(shaped) == Decimal(1000)


def test_a_weighted_rung_actually_gets_more_size():
    shaped = _plan(level_weights=[Decimal(1), Decimal(4), Decimal(1), Decimal(1)])
    sizes = [lv.size_quote for lv in shaped]
    assert sizes[1] > sizes[0] and sizes[1] > sizes[2]


def test_offsets_are_unaffected_by_reshaping():
    # Only SIZE moves; where the rungs sit is the step's business.
    plain = _plan()
    shaped = _plan(level_weights=[Decimal(1), Decimal(9), Decimal(1), Decimal(1)])
    assert [lv.offset_bp for lv in plain] == [lv.offset_bp for lv in shaped]


def test_the_min_notional_stepdown_sees_the_combined_weights():
    # A steep reshape makes the smallest rung much smaller than the curve
    # alone implies. If the stepdown ignored the reshape, that rung would land
    # under the venue minimum and be grown by the client into unbudgeted size.
    shaped = _plan(min_notional=Decimal(200),
                   level_weights=[Decimal(1), Decimal(50), Decimal(50), Decimal(50)])
    assert all(lv.size_quote >= Decimal(200) for lv in shaped)
    assert ladder_notional(shaped) == Decimal(1000)


def test_a_degenerate_weight_list_falls_back_to_the_curve():
    zeros = _plan(level_weights=[Decimal(0)] * 4)
    plain = _plan()
    assert [lv.size_quote for lv in zeros] == [lv.size_quote for lv in plain]


def test_a_short_weight_list_leaves_the_remaining_rungs_alone():
    shaped = _plan(level_weights=[Decimal(2)])
    sizes = [lv.size_quote for lv in shaped]
    assert sizes[0] > sizes[1]
    assert sizes[1] == sizes[2]
    assert ladder_notional(shaped) == Decimal(1000)


def test_malformed_weights_do_not_break_the_plan():
    shaped = _plan(level_weights=["junk", None, Decimal(2), Decimal(1)])
    assert ladder_notional(shaped) == Decimal(1000)
    assert len(shaped) == 4


def test_omitting_weights_is_byte_identical_to_before():
    assert [(lv.index, lv.offset_bp, lv.size_quote) for lv in _plan(level_weights=None)] == \
           [(lv.index, lv.offset_bp, lv.size_quote) for lv in _plan()]
