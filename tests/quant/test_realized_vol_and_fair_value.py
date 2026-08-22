"""Realized volatility on an irregular clock, and fair-value blending.

The volatility property worth guarding is that decay is TIME-based: the HL feed
pushes on events, so a burst of ticks in one second must not count for more
than the same ticks spread over a minute.

The fair-value property worth guarding is that a stale or wildly disagreeing
reference is DROPPED rather than averaged in — a blended number that quietly
includes a dead feed is worse than no number.
"""
import math

import pytest

from src.nadobro.quant import fair_value as fv
from src.nadobro.quant import realized_vol as rv


def _series(prices, step=1.0, start=0.0):
    return [(start + i * step, p) for i, p in enumerate(prices)]


# --- realized vol -----------------------------------------------------------

def test_a_flat_series_has_zero_volatility():
    assert rv.ewma_vol(_series([100.0] * 10), halflife_s=30.0) == pytest.approx(0.0)


def test_a_more_volatile_series_scores_higher():
    calm = _series([100.0, 100.1, 100.0, 100.1, 100.0])
    wild = _series([100.0, 103.0, 97.0, 104.0, 96.0])
    assert rv.ewma_vol(wild, halflife_s=30.0) > rv.ewma_vol(calm, halflife_s=30.0)


def test_insufficient_history_returns_none_not_zero():
    # Zero would read as "no risk" and quote too tight; None forces the caller
    # to widen by policy instead.
    assert rv.ewma_vol([], halflife_s=30.0) is None
    assert rv.ewma_vol(_series([100.0]), halflife_s=30.0) is None
    assert rv.ewma_vol(_series([100.0, 101.0]), halflife_s=0.0) is None


def test_decay_is_time_based_not_per_sample():
    # THE property. Identical price path, once as a 1-second burst and once
    # spread over a minute. A per-sample EWMA would score these the same; a
    # time-decayed variance RATE must score the burst far higher, because the
    # same move happened in a fraction of the time.
    path = [100.0, 100.5, 100.0, 100.5, 100.0, 100.5]
    burst = rv.ewma_vol(_series(path, step=0.2), halflife_s=300.0)
    spread = rv.ewma_vol(_series(path, step=12.0), halflife_s=300.0)
    assert burst > spread * 3


def test_vol_scales_with_the_square_root_of_the_horizon():
    series = _series([100.0, 100.2, 99.9, 100.3, 100.1])
    one = rv.vol_over(series, halflife_s=60.0, horizon_s=1.0)
    four = rv.vol_over(series, halflife_s=60.0, horizon_s=4.0)
    assert four == pytest.approx(one * 2.0, rel=1e-9)


def test_annualize_uses_seconds_per_year():
    assert rv.annualize(1.0) == pytest.approx(math.sqrt(rv.SECONDS_PER_YEAR))
    assert rv.annualize(None) is None


def test_parkinson_reads_the_bar_range():
    flat = [{"high": 100.0, "low": 100.0} for _ in range(5)]
    wide = [{"high": 102.0, "low": 98.0} for _ in range(5)]
    assert rv.parkinson(flat) == pytest.approx(0.0)
    assert rv.parkinson(wide) > 0
    assert rv.parkinson([]) is None


def test_vol_of_vol_flags_an_unstable_estimate():
    assert rv.vol_of_vol([0.1, 0.1, 0.1]) == pytest.approx(0.0)
    assert rv.vol_of_vol([0.01, 0.5, 0.02]) > 0
    assert rv.vol_of_vol([0.1]) is None


# --- fair value -------------------------------------------------------------

def test_robust_median_ignores_missing_values():
    assert fv.robust_median([100.0, None, 102.0, 101.0]) == pytest.approx(101.0)
    assert fv.robust_median([None, None]) is None


def test_median_resists_a_single_dislocated_feed():
    # The reason to consult several references at all.
    assert fv.robust_median([100.0, 100.1, 5_000.0]) == pytest.approx(100.1)


def test_basis_sign_says_which_venue_is_richer():
    assert fv.basis_bp(100.0, 100.1) == pytest.approx(10.0)     # our venue richer
    assert fv.basis_bp(100.0, 99.9) == pytest.approx(-10.0)
    assert fv.basis_bp(None, 100.0) is None
    assert fv.basis_bp(0.0, 100.0) is None


def test_a_fresh_agreeing_reference_is_blended():
    out = fv.blend(100.0, {"hl": (100.02, 1.0)})
    assert out["value"] == pytest.approx(100.01)
    assert set(out["used"]) == {"anchor", "hl"}
    assert out["dropped"] == {}


def test_a_stale_reference_is_dropped_not_blended():
    out = fv.blend(100.0, {"hl": (100.02, 999.0)}, max_age_s=30.0)
    assert out["value"] == pytest.approx(100.0)      # anchor only
    assert "hl" in out["dropped"] and "stale" in out["dropped"]["hl"]


def test_a_wildly_disagreeing_reference_is_dropped_not_averaged():
    # One of the two venues is broken; averaging them would invent a price
    # neither market is trading at.
    out = fv.blend(100.0, {"hl": (120.0, 1.0)}, max_deviation_bp=100.0)
    assert out["value"] == pytest.approx(100.0)
    assert "deviates" in out["dropped"]["hl"]


def test_weights_renormalise_over_the_survivors():
    out = fv.blend(
        100.0,
        {"hl": (101.0, 1.0), "dead": (101.0, 999.0)},
        weights={"anchor": 1.0, "hl": 3.0},
        max_age_s=30.0, max_deviation_bp=500.0,
    )
    # (100*1 + 101*3) / 4 — the dropped feed does not silently zero the blend.
    assert out["value"] == pytest.approx(100.75)
    assert "dead" in out["dropped"]


def test_a_missing_anchor_yields_no_value_at_all():
    # Without the venue's own book there is nothing to anchor to, and a
    # reference alone must never become the quote price.
    out = fv.blend(None, {"hl": (100.0, 1.0)})
    assert out["value"] is None


def test_malformed_references_are_reported_not_silently_ignored():
    out = fv.blend(100.0, {"junk": ("x", "y")})
    assert out["dropped"]["junk"] == "malformed"


# --- funding carry ----------------------------------------------------------

def test_funding_is_a_daily_rate_divided_by_86400_not_24():
    # CLAUDE.md: funding_rate_x18 is a signed DAILY rate settled hourly.
    # Holding a long for a full day at 1% costs the full 100bp.
    assert fv.funding_carry_bp(0.01, hold_seconds=86400, side=1) == pytest.approx(100.0)
    # One hour is a 24th of that.
    assert fv.funding_carry_bp(0.01, hold_seconds=3600, side=1) == pytest.approx(100.0 / 24)


def test_the_short_side_receives_what_the_long_pays():
    long_cost = fv.funding_carry_bp(0.01, hold_seconds=3600, side=1)
    short_cost = fv.funding_carry_bp(0.01, hold_seconds=3600, side=-1)
    assert short_cost == pytest.approx(-long_cost)


def test_funding_edge_cases():
    assert fv.funding_carry_bp(None, hold_seconds=3600, side=1) is None
    assert fv.funding_carry_bp(0.01, hold_seconds=0, side=1) == 0.0
