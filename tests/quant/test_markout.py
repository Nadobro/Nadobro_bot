"""Post-fill mark-out.

The sign convention is the thing to guard: positive must mean "the market moved
OUR way", uniformly for buys and sells. Get it backwards and a bleeding book
reads as a profitable one.

The second guard is truthfulness of the horizon — a sample measured late is
discarded rather than relabelled, because keeping it biases the series toward
whatever the feed was doing while it was slow.
"""
import pytest

from src.nadobro.quant import markout as mk


def _buy(price=100.0, ts=0.0):
    return mk.FillRef(fill_id="f1", ts=ts, side=mk.BUY, fill_price=price)


def _sell(price=100.0, ts=0.0):
    return mk.FillRef(fill_id="f2", ts=ts, side=mk.SELL, fill_price=price)


# --- sign convention --------------------------------------------------------

def test_a_buy_that_rallies_scores_positive():
    assert mk.markout_bp(_buy(100.0), 101.0) == pytest.approx(100.0)


def test_a_buy_that_drops_scores_negative_and_is_adverse_selection():
    assert mk.markout_bp(_buy(100.0), 99.0) == pytest.approx(-100.0)


def test_a_sell_that_drops_scores_POSITIVE():
    # Same price move as the losing buy, opposite side => opposite sign.
    assert mk.markout_bp(_sell(100.0), 99.0) == pytest.approx(100.0)


def test_a_sell_that_rallies_scores_negative():
    assert mk.markout_bp(_sell(100.0), 101.0) == pytest.approx(-100.0)


def test_the_two_sides_are_exact_mirrors():
    for ref in (95.0, 99.5, 100.0, 100.5, 107.0):
        assert mk.markout_bp(_buy(100.0), ref) == pytest.approx(-mk.markout_bp(_sell(100.0), ref))


@pytest.mark.parametrize("price,ref", [(0.0, 100.0), (100.0, 0.0), (-1.0, 100.0)])
def test_degenerate_prices_yield_none(price, ref):
    assert mk.markout_bp(mk.FillRef("f", 0.0, mk.BUY, price), ref) is None


# --- fees -------------------------------------------------------------------

def test_net_markout_subtracts_the_fee_regardless_of_its_sign():
    fill = _buy(100.0)
    assert mk.net_markout_bp(fill, 101.0, fee_bp=5.0) == pytest.approx(95.0)
    # A fee passed negative is still a cost, never a credit.
    assert mk.net_markout_bp(fill, 101.0, fee_bp=-5.0) == pytest.approx(95.0)


def test_a_gross_win_can_still_be_a_net_loss():
    # 2bp of edge against a 5bp round trip is why gross mark-out is not enough.
    assert mk.net_markout_bp(_buy(100.0), 100.02, fee_bp=5.0) < 0


# --- reference picking / horizon truthfulness -------------------------------

def test_picks_the_first_sample_at_or_after_the_target():
    series = [(0.0, 100.0), (4.0, 100.4), (5.5, 100.6), (9.0, 101.0)]
    price, drift = mk.pick_reference(series, 5.0, max_jitter_s=2.0)
    assert price == 100.6 and drift == pytest.approx(0.5)


def test_returns_none_when_the_horizon_has_not_elapsed_yet():
    assert mk.pick_reference([(0.0, 100.0), (2.0, 100.1)], 30.0, max_jitter_s=5.0) is None


def test_a_sample_that_drifts_past_the_jitter_bound_is_discarded():
    # The feed skipped from t=1 to t=20; a "5s" mark-out read at 20s is not a
    # 5s mark-out, so it must be dropped rather than silently relabelled.
    series = [(0.0, 100.0), (1.0, 100.1), (20.0, 105.0)]
    assert mk.pick_reference(series, 5.0, max_jitter_s=2.0) is None


def test_build_sample_records_both_nominal_and_actual_horizon():
    series = [(0.0, 100.0), (5.4, 101.0)]
    s = mk.build_sample(_buy(100.0), series, horizon_s=5.0, fee_bp=2.0,
                        ref_source=mk.REF_TICK)
    assert s.horizon_nominal_s == 5.0
    assert s.horizon_actual_s == pytest.approx(5.4)   # drift is auditable
    assert s.markout_bp == pytest.approx(100.0)
    assert s.net_markout_bp == pytest.approx(98.0)
    assert s.ref_source == mk.REF_TICK


def test_build_sample_defaults_jitter_to_a_quarter_of_the_horizon():
    # 5s horizon => 1.25s tolerance. 6.0s is inside, 7.0s is not.
    assert mk.build_sample(_buy(), [(6.0, 101.0)], horizon_s=5.0, fee_bp=0.0,
                           ref_source=mk.REF_TICK) is not None
    assert mk.build_sample(_buy(), [(7.0, 101.0)], horizon_s=5.0, fee_bp=0.0,
                           ref_source=mk.REF_TICK) is None


def test_basis_is_carried_so_it_cannot_be_mistaken_for_adverse_selection():
    # Mark-out is measured on the HL reference but the fill happened on Nado;
    # a persistent basis would otherwise look like toxicity.
    s = mk.build_sample(_buy(), [(5.0, 101.0)], horizon_s=5.0, fee_bp=0.0,
                        ref_source=mk.REF_TICK, basis_bp=3.5)
    assert s.basis_bp == 3.5


# --- aggregation ------------------------------------------------------------

def _sample(bp, horizon=5.0, fee=0.0):
    return mk.MarkoutSample(horizon, horizon, 100.0, bp, bp - fee, mk.REF_TICK)


def test_summarize_groups_by_horizon_and_reports_the_adverse_share():
    samples = [_sample(10.0), _sample(-30.0), _sample(-10.0), _sample(20.0),
               _sample(5.0, horizon=60.0)]
    out = mk.summarize(samples)
    assert out[5.0]["n"] == 4
    assert out[5.0]["adverse_share"] == pytest.approx(0.5)
    assert out[60.0]["n"] == 1
    assert out[5.0]["median_bp"] == pytest.approx(0.0)   # -10,10 straddle zero


def test_summarize_is_empty_for_no_samples():
    assert mk.summarize([]) == {}


# --- the feedback rule ------------------------------------------------------

def test_no_widening_without_enough_evidence():
    thin = mk.summarize([_sample(-50.0) for _ in range(5)])
    assert mk.widen_recommendation(thin, current_half_spread_bp=5.0, horizon_s=5.0) == 1.0


def test_no_widening_when_net_markout_is_healthy():
    good = mk.summarize([_sample(20.0) for _ in range(50)])
    assert mk.widen_recommendation(good, current_half_spread_bp=5.0, horizon_s=5.0) == 1.0


def test_widens_to_cover_a_measured_shortfall():
    # Median net -5bp on a 5bp half-spread => widen to ~2x, no more.
    bad = mk.summarize([_sample(-5.0) for _ in range(50)])
    factor = mk.widen_recommendation(bad, current_half_spread_bp=5.0, horizon_s=5.0)
    assert factor == pytest.approx(2.0)


def test_widening_is_capped_and_never_tightens():
    awful = mk.summarize([_sample(-500.0) for _ in range(50)])
    factor = mk.widen_recommendation(awful, current_half_spread_bp=5.0, horizon_s=5.0,
                                     max_factor=3.0)
    assert factor == 3.0
    # A missing horizon is "no evidence", not "tighten".
    assert mk.widen_recommendation({}, current_half_spread_bp=5.0, horizon_s=5.0) == 1.0
