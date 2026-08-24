"""Pure-logic tests for the Mid-mode mark-out readout — no DB.

Covers the decomposition that makes the readout trustworthy: the basis-adjusted
net (``net + side*basis``) that separates real adverse selection from a
persistent Nado-vs-HL level offset, and the verdict gate that refuses to call a
strategy positive without enough evidence.
"""
from __future__ import annotations

from src.nadobro.trading import markout_report as mr


def _row(h, side, net, gross, basis):
    return {
        "horizon_nominal_s": h, "side": side,
        "net_markout_bp": net, "markout_bp": gross, "basis_bp": basis,
    }


def test_side_sign_matches_scorer_convention():
    assert mr._side_sign("buy") == 1
    assert mr._side_sign("BUY") == 1
    assert mr._side_sign("sell") == -1
    assert mr._side_sign("Short") == -1
    assert mr._side_sign(None) == 1   # default buy, same as markout_scorer


def test_median_and_mean_handle_none_and_even_counts():
    assert mr._median([]) is None
    assert mr._median([3.0, 1.0, 2.0]) == 2.0
    assert mr._median([1.0, 3.0]) == 2.0            # even -> average of middle two
    assert mr._median([1.0, None, 3.0]) == 2.0      # None dropped
    assert mr._mean([1.0, 2.0, None]) == 1.5
    assert mr._mean([None]) is None


def test_basis_adjustment_separates_venue_offset_from_toxicity():
    # Both fills net -3/-1bp raw, both sitting on a +2bp Nado-over-HL basis.
    # basis_adj = net + side*basis:  buy -3 + (+1)(2) = -1 ; sell -1 + (-1)(2) = -3.
    rows = [
        _row(60.0, "buy", -3.0, -0.5, 2.0),
        _row(60.0, "sell", -1.0, -0.5, 2.0),
    ]
    s = mr.summarize_rows(rows)[60.0]
    assert s["n"] == 2
    assert s["net_median_bp"] == -2.0
    assert s["basis_mean_bp"] == 2.0
    assert s["adverse_share"] == 1.0
    assert s["basis_adj_net_median_bp"] == -2.0        # median of (-1, -3)
    assert s["buy"]["n"] == 1 and s["buy"]["basis_adj_net_median_bp"] == -1.0
    assert s["sell"]["n"] == 1 and s["sell"]["basis_adj_net_median_bp"] == -3.0


def test_rows_missing_basis_still_count_toward_raw_but_not_basis_adj():
    rows = [_row(60.0, "buy", -2.0, -1.0, None), _row(60.0, "buy", -4.0, -1.0, 1.0)]
    s = mr.summarize_rows(rows)[60.0]
    assert s["n"] == 2
    assert s["net_median_bp"] == -3.0                  # both raw nets counted
    # only the row WITH a basis contributes to basis_adj: -4 + (+1)(1) = -3
    assert s["basis_adj_net_median_bp"] == -3.0
    assert s["basis_mean_bp"] == 1.0                    # single basis row


def test_summarize_groups_by_horizon():
    rows = [_row(60.0, "buy", 1.0, 1.0, 0.0), _row(300.0, "sell", -1.0, -1.0, 0.0)]
    s = mr.summarize_rows(rows)
    assert set(s) == {60.0, 300.0}


def test_verdict_insufficient_data_below_gate():
    rows = [_row(60.0, "buy", 5.0, 5.0, 0.0)]           # 1 sample < gate
    v = mr.verdict(mr.summarize_rows(rows), min_samples=30)
    assert "INSUFFICIENT DATA" in v


def test_verdict_bleeding_when_raw_and_adjusted_both_negative():
    rows = [_row(60.0, "buy", -3.0, -1.0, 0.0)] * 40 + [_row(300.0, "sell", -3.0, -1.0, 0.0)] * 40
    v = mr.verdict(mr.summarize_rows(rows), min_samples=30)
    assert v.startswith("BLEEDING")


def test_verdict_mixed_when_negativity_is_only_the_basis():
    # net -1bp raw, but a big favourable basis makes the basis-adjusted edge +.
    # buy: -1 + (+1)(3) = +2 ; keep it one-sided so both raw<0 and adj>0.
    rows = [_row(60.0, "buy", -1.0, -1.0, 3.0)] * 40 + [_row(300.0, "buy", -1.0, -1.0, 3.0)] * 40
    v = mr.verdict(mr.summarize_rows(rows), min_samples=30)
    assert v.startswith("MIXED") and "level offset" in v


def test_verdict_positive_only_when_both_medians_clear_zero():
    rows = [_row(60.0, "buy", 1.5, 3.0, 0.0)] * 40 + [_row(300.0, "sell", 1.5, 3.0, 0.0)] * 40
    v = mr.verdict(mr.summarize_rows(rows), min_samples=30)
    assert v.startswith("POSITIVE")


def test_format_report_renders_table_and_verdict():
    rows = [_row(60.0, "buy", -3.0, -0.5, 2.0)] * 40
    text = mr.format_report(mr.summarize_rows(rows), strategy="mid", network="mainnet", lookback_days=30)
    assert "Mid-mode mark-out readout" in text
    assert "strategy=mid" in text
    assert "VERDICT:" in text
    assert "60s" in text


def test_format_report_on_empty_summary_says_insufficient():
    text = mr.format_report({}, strategy="mid", network="mainnet", lookback_days=7)
    assert "VERDICT:" in text
    assert "INSUFFICIENT DATA" in text
    assert "graded_samples=0" in text
