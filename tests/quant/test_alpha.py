"""Blending short-horizon signals into one bounded directional number.

The three properties that keep a maker out of trouble:

* **the allowlist** — realized vol, spread and every candle oscillator are
  DEFENSIVE. A maker is short gamma, so folding a volatility magnitude into a
  signed direction is how it leans into a cascade;
* **abstain, never impute** — a missing component is dropped and the weights
  renormalise; it is never replaced by zero, which is a real reading;
* **the clamp** — until the weights are scored, alpha cannot exceed ±0.35, so a
  wrong prior is cheap.
"""
import pytest

from src.nadobro.quant import alpha as al


def _full(value=0.5):
    return {name: value for name in al.DIRECTIONAL_SIGNALS}


# --- the allowlist ----------------------------------------------------------

def test_defensive_signals_are_refused_with_a_reason():
    out = al.blend({"obi": 0.5, "realized_vol": 0.9, "rsi": 0.8, "atr": 0.7})
    assert out["dropped"]["realized_vol"] == "not_directional"
    assert out["dropped"]["rsi"] == "not_directional"
    assert out["dropped"]["atr"] == "not_directional"
    assert out["used"] == ["obi"]


def test_every_defensive_name_is_rejected():
    # The frozen list, checked as a set rather than one example.
    out = al.blend({name: 0.9 for name in al.DEFENSIVE_SIGNALS})
    assert set(out["dropped"]) == set(al.DEFENSIVE_SIGNALS)
    assert out["alpha"] == 0.0 and out["confidence"] == 0.0


def test_the_two_families_do_not_overlap():
    assert not (al.DIRECTIONAL_SIGNALS & al.DEFENSIVE_SIGNALS)


def test_funding_is_carry_not_alpha():
    # Carrying inventory has a cost; that belongs in the spread, not the lean.
    assert "funding" not in al.DIRECTIONAL_SIGNALS
    assert "funding" not in al.COLD_START_WEIGHTS


# --- abstain, never impute --------------------------------------------------

def test_a_missing_component_is_dropped_not_zeroed():
    both = al.blend({"obi": 1.0, "trade_imbalance": 1.0, "micro_displacement": 1.0})
    one_missing = al.blend(
        {"obi": 1.0, "trade_imbalance": 1.0, "micro_displacement": None})
    assert one_missing["dropped"]["micro_displacement"] == "missing"
    # Imputing zero would have dragged the blend toward neutral; renormalising
    # keeps the surviving components' verdict intact.
    assert one_missing["alpha"] == pytest.approx(both["alpha"])


def test_too_little_coverage_means_no_view_at_all():
    # obi alone is 0.30 of the total weight — under half, so no lean.
    out = al.blend({"obi": 1.0}, min_weight_covered=0.5)
    assert out["alpha"] == 0.0 and out["confidence"] == 0.0
    assert out["covered"] == pytest.approx(0.30)


def test_enough_coverage_produces_a_view():
    out = al.blend({"obi": 1.0, "micro_displacement": 1.0}, min_weight_covered=0.5)
    assert out["alpha"] > 0 and out["confidence"] > 0


def test_an_empty_payload_is_neutral_rather_than_an_error():
    out = al.blend({})
    assert out["alpha"] == 0.0 and out["confidence"] == 0.0


# --- the clamp --------------------------------------------------------------

def test_untrusted_weights_cannot_lean_past_the_clamp():
    out = al.blend(_full(1.0))
    assert out["alpha"] == pytest.approx(al.MAX_ALPHA_UNTRUSTED)
    short = al.blend(_full(-1.0))
    assert short["alpha"] == pytest.approx(-al.MAX_ALPHA_UNTRUSTED)


def test_a_scored_blend_may_exceed_the_cold_start_clamp():
    out = al.blend(_full(1.0), trusted=True)
    assert out["alpha"] == pytest.approx(1.0)


# --- confidence -------------------------------------------------------------

def test_unanimous_components_are_more_confident_than_split_ones():
    agree = al.blend({"obi": 0.8, "micro_displacement": 0.8, "trade_imbalance": 0.8})
    split = al.blend({"obi": 0.8, "micro_displacement": -0.8, "trade_imbalance": 0.8})
    assert agree["confidence"] > split["confidence"]


def test_a_perfectly_split_book_has_no_confidence():
    out = al.blend({"obi": 0.5, "micro_displacement": -0.6})
    assert out["confidence"] == pytest.approx(0.0, abs=0.35)


# --- helpers ----------------------------------------------------------------

def test_squash_is_bounded_and_ordered_with_no_clip_cliff():
    assert al.squash(1e9) == pytest.approx(1.0)
    assert al.squash(-1e9) == pytest.approx(-1.0)
    # A hard clip would make these identical; tanh keeps them ordered.
    assert al.squash(3.0) > al.squash(2.0)
    assert al.squash(1.0, scale=0) is None
    assert al.squash("junk") is None


def test_nan_and_inf_never_reach_the_blend():
    out = al.blend({"obi": float("nan"), "micro_displacement": float("inf"),
                    "trade_imbalance": 0.5, "ofi": 0.5, "momentum": 0.5,
                    "basis": 0.5})
    assert out["dropped"]["obi"] == "missing"
    assert out["alpha"] == out["alpha"]          # not NaN


def test_robust_z_resists_a_single_outlier():
    history = [1.0, 1.1, 0.9, 1.05, 0.95, 50.0]
    assert al.robust_z(1.0, history) == pytest.approx(0.0, abs=0.5)
    assert al.robust_z(1.0, [1.0]) is None       # not enough history
    assert al.robust_z(1.0, [2.0, 2.0, 2.0]) is None   # zero MAD


def test_standardize_bounds_everything():
    out = al.standardize({"a": 5.0, "b": -5.0, "c": 0.3, "d": "junk"})
    assert -1.0 <= out["a"] <= 1.0 and -1.0 <= out["b"] <= 1.0
    assert out["c"] == pytest.approx(0.3)
    assert out["d"] is None


# --- the single write -------------------------------------------------------

def test_fast_and_slow_are_reconciled_once():
    # Two writers to one field is how dead-bands stop working, so the fast
    # alpha and the slow timeframe vote are combined here and written once.
    assert al.resolve_bias(1.0, -1.0, fast_weight=0.75) == pytest.approx(0.5)
    assert al.resolve_bias(0.4, 0.4) == pytest.approx(0.4)


def test_a_missing_side_leaves_the_other_standing_alone():
    # Averaging against a zero nobody asserted would halve a real view.
    assert al.resolve_bias(None, 0.8) == pytest.approx(0.8)
    assert al.resolve_bias(0.8, None) == pytest.approx(0.8)
    assert al.resolve_bias(None, None) == 0.0


# --- the anchor shift -------------------------------------------------------

def test_alpha_moves_the_anchor_in_its_own_direction():
    assert al.anchor_offset_bp(0.5, half_spread_bp=10.0) > 0
    assert al.anchor_offset_bp(-0.5, half_spread_bp=10.0) < 0
    assert al.anchor_offset_bp(0.0, half_spread_bp=10.0) == 0.0


def test_the_anchor_shift_cannot_walk_a_quote_across_the_book():
    for a in (-5.0, -1.0, 1.0, 5.0):
        assert abs(al.anchor_offset_bp(a, half_spread_bp=10.0, strength=99.0)) <= 5.0


def test_no_half_spread_means_no_shift():
    assert al.anchor_offset_bp(1.0, half_spread_bp=0.0) == 0.0
    assert al.anchor_offset_bp(1.0, half_spread_bp=None) == 0.0
