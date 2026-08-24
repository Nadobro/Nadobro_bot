"""Objective profile selection, the fee floor, and the reservation offset.

Three properties carry the money here:

* the SPREAD profile can never quote inside the fee — ``δ* > f`` by
  construction, not by configuration;
* ``auto`` resolves to VOLUME whenever the spread does not clear the round trip
  with room to spare, including when the spread cannot be read at all;
* the inventory shift is bounded by the half-spread, so working inventory off
  can never cross the two sides over each other.
"""
import pytest

from src.nadobro.quant import mm_profile as mp


# --- the objective ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("volume", mp.VOLUME), ("SPREAD", mp.SPREAD), ("auto", mp.AUTO),
    ("", mp.AUTO), (None, mp.AUTO), ("typo", mp.AUTO), (7, mp.AUTO),
])
def test_an_unknown_objective_falls_back_to_auto_not_a_playbook(raw, expected):
    assert mp.normalize_objective(raw) == expected


# --- the fee floor ----------------------------------------------------------

def test_the_per_leg_fee_is_half_the_round_trip():
    assert mp.per_leg_fee_bp(5.0) == pytest.approx(2.5)
    assert mp.per_leg_fee_bp(None) == pytest.approx(mp.DEFAULT_FEE_ROUND_TRIP_BP / 2)
    assert mp.per_leg_fee_bp(0) == pytest.approx(mp.DEFAULT_FEE_ROUND_TRIP_BP / 2)


def test_the_floor_always_clears_the_fee():
    # THE invariant: a half-spread at or below the per-leg fee loses money on
    # every completed round trip.
    for rt in (1.0, 5.0, 12.5, 40.0):
        for edge in (-5.0, 0.0, 0.5, 3.0):
            floor = mp.half_spread_floor_bp(fee_round_trip_bp=rt, min_edge_bp=edge)
            assert floor > mp.per_leg_fee_bp(rt)


def test_the_shipped_default_floor_was_below_the_fee_and_this_is_not():
    # spread_floor_half_pct shipped at 0.00015 == 1.5bp, under the 2.5bp
    # per-leg fee — reachable, and a guaranteed loser when reached.
    assert mp.half_spread_floor_bp(fee_round_trip_bp=5.0, min_edge_bp=1.0) > 1.5


def test_a_calibrated_arrival_term_widens_the_floor_but_never_narrows_it():
    base = mp.half_spread_floor_bp(fee_round_trip_bp=5.0, min_edge_bp=1.0)
    assert mp.half_spread_floor_bp(
        fee_round_trip_bp=5.0, min_edge_bp=1.0, inv_k_bp=4.0) > base
    # A 1/k smaller than the configured minimum edge must not undercut it.
    assert mp.half_spread_floor_bp(
        fee_round_trip_bp=5.0, min_edge_bp=1.0, inv_k_bp=0.2) == pytest.approx(base)


# --- profile selection ------------------------------------------------------

def test_a_one_tick_book_resolves_to_volume():
    # BTC's spread does not cover a round trip; there is no edge to capture.
    assert mp.resolve_profile("auto", spread_bp=1.0, fee_round_trip_bp=5.0) == mp.VOLUME


def test_a_wide_book_resolves_to_spread():
    assert mp.resolve_profile("auto", spread_bp=40.0, fee_round_trip_bp=5.0) == mp.SPREAD


def test_a_spread_barely_over_the_fee_still_resolves_to_volume():
    # The dead band: one tick of adverse selection erases an edge this thin, so
    # the selector concedes SPREAD only with real room to spare.
    assert mp.resolve_profile("auto", spread_bp=5.4, fee_round_trip_bp=5.0) == mp.VOLUME
    assert mp.resolve_profile("auto", spread_bp=6.25, fee_round_trip_bp=5.0) == mp.SPREAD


@pytest.mark.parametrize("bad", [None, 0.0, -3.0])
def test_an_unreadable_spread_resolves_to_volume(bad):
    # An unknown market is precisely the one not to run a pricing playbook on.
    assert mp.resolve_profile("auto", spread_bp=bad, fee_round_trip_bp=5.0) == mp.VOLUME


def test_an_explicit_choice_always_beats_the_measurement():
    assert mp.resolve_profile("spread", spread_bp=1.0, fee_round_trip_bp=5.0) == mp.SPREAD
    assert mp.resolve_profile("volume", spread_bp=99.0, fee_round_trip_bp=5.0) == mp.VOLUME


# --- the reservation offset -------------------------------------------------

def test_long_inventory_shifts_the_anchor_down_and_short_shifts_it_up():
    long_off = mp.reservation_offset_bp(0.8, sigma_bp=20.0, half_spread_bp=10.0)
    short_off = mp.reservation_offset_bp(-0.8, sigma_bp=20.0, half_spread_bp=10.0)
    assert long_off < 0 < short_off          # long => quote lower => sell it off
    assert long_off == pytest.approx(-short_off)


def test_a_flat_book_is_not_skewed_at_all():
    assert mp.reservation_offset_bp(0.0, sigma_bp=20.0, half_spread_bp=10.0) == 0.0


def test_the_shift_can_never_cross_the_two_sides():
    # THE safety property. Bounded by a fraction of the half-spread, so even a
    # wildly over-scaled sigma and a saturated inventory leave bid < ask.
    half = 10.0
    for ratio in (-5.0, -1.0, -0.3, 0.3, 1.0, 5.0):
        off = mp.reservation_offset_bp(
            ratio, sigma_bp=100_000.0, half_spread_bp=half, gamma=99.0)
        assert abs(off) <= half * 0.5


def test_missing_inputs_yield_no_shift_rather_than_a_guess():
    assert mp.reservation_offset_bp(None, sigma_bp=20.0, half_spread_bp=10.0) == 0.0
    assert mp.reservation_offset_bp(0.5, sigma_bp=20.0, half_spread_bp=0.0) == 0.0
    assert mp.reservation_offset_bp(0.5, sigma_bp="junk", half_spread_bp=10.0) != 0.0


def test_without_a_volatility_estimate_the_half_spread_sets_the_scale():
    # Falling back to zero would silently disable the skew exactly when the
    # regime routine has not warmed up yet.
    assert mp.reservation_offset_bp(1.0, sigma_bp=0.0, half_spread_bp=10.0) < 0
