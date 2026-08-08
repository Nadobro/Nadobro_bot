"""Grading metrics — quant/scoring.py.

The point of these tests is that a metric which quietly counts neutral calls,
mis-signs a short, or reports a correlation off one outlier would make the
auto-apply envelope act on a number that means nothing.
"""
import pytest

from src.nadobro.quant import scoring


def _row(bias, fwd, *, conf=None, up=None, down=None, hit=None, regime=None):
    return {
        "bias": bias,
        "fwd_return": fwd,
        "confidence": conf,
        "excursion_up": up,
        "excursion_down": down,
        "directional_hit": hit,
        "regime": regime,
    }


class TestDirectionalFiltering:
    def test_neutral_calls_excluded(self):
        rows = [_row(0.0, 0.01), _row(0.5, 0.01), _row(None, 0.01)]
        assert len(scoring.directional(rows)) == 1

    def test_neutral_call_does_not_count_as_miss(self):
        # A bot that declines to have an opinion must not be scored as wrong.
        assert scoring.hit_rate([_row(0.5, 0.01), _row(0.0, -0.05)]) == 1.0

    def test_row_without_outcome_excluded(self):
        assert scoring.directional([_row(0.5, None)]) == []


class TestHitRate:
    def test_uses_stored_hit_when_present(self):
        rows = [_row(0.5, 0.01, hit=True), _row(0.5, 0.01, hit=False)]
        assert scoring.hit_rate(rows) == 0.5

    def test_recomputes_when_stored_hit_missing(self):
        rows = [_row(0.5, 0.02), _row(0.5, -0.02), _row(-0.5, -0.02)]
        assert scoring.hit_rate(rows) == 2 / 3

    def test_empty_is_none_not_zero(self):
        # None means "unmeasured"; 0.0 would read as "always wrong".
        assert scoring.hit_rate([]) is None


class TestMfeMae:
    def test_long_call_maps_directly(self):
        mfe, mae = scoring.mfe_mae(_row(1.0, 0.01, up=0.03, down=-0.02))
        assert (mfe, mae) == (0.03, -0.02)

    def test_short_call_is_sign_flipped(self):
        # Short that ran down 2% and against by 3%: favourable is the DOWN leg.
        mfe, mae = scoring.mfe_mae(_row(-1.0, -0.02, up=0.03, down=-0.02))
        assert (mfe, mae) == (0.02, -0.03)

    def test_neutral_is_undefined(self):
        assert scoring.mfe_mae(_row(0.0, 0.01, up=0.03, down=-0.02)) == (None, None)

    def test_edge_ratio_reads_asymmetry(self):
        rows = [_row(1.0, 0.01, up=0.04, down=-0.02)] * 4
        assert scoring.edge_ratio(rows) == 2.0


class TestExpectancy:
    def test_short_call_that_won_is_positive(self):
        assert scoring.expectancy([_row(-1.0, -0.03)]) == 0.03

    def test_averages_across_directions(self):
        assert scoring.expectancy([_row(1.0, 0.02), _row(-1.0, 0.02)]) == 0.0


class TestInformationCoefficient:
    def test_perfect_rank_agreement(self):
        rows = [_row(-1.0, -0.03), _row(-0.5, -0.01), _row(0.5, 0.01), _row(1.0, 0.03)]
        assert scoring.information_coefficient(rows) == pytest.approx(1.0)

    def test_perfect_disagreement(self):
        rows = [_row(-1.0, 0.03), _row(-0.5, 0.01), _row(0.5, -0.01), _row(1.0, -0.03)]
        assert scoring.information_coefficient(rows) == pytest.approx(-1.0)

    def test_rank_based_resists_a_single_outlier(self):
        # Nine calls in rank agreement plus one enormous adverse move. Pearson
        # is dominated by the -5.0 and goes sharply negative; Spearman only
        # loses the one inverted rank and stays positive.
        rows = [_row(0.1 * i, 0.01 * i) for i in range(1, 10)] + [_row(1.0, -5.0)]
        ic = scoring.information_coefficient(rows)
        assert ic is not None and ic > 0.4

    def test_tied_predictions_do_not_break_ranking(self):
        # Ties must get averaged ranks, not arbitrary ones.
        rows = [_row(0.5, 0.01), _row(0.5, 0.02), _row(1.0, 0.03), _row(1.0, 0.04)]
        ic = scoring.information_coefficient(rows)
        assert ic is not None and ic == pytest.approx(0.894427, abs=1e-5)

    def test_no_variance_is_none(self):
        assert scoring.information_coefficient([_row(0.5, 0.01)] * 5) is None

    def test_too_few_samples_is_none(self):
        assert scoring.information_coefficient([_row(0.5, 0.01)]) is None


class TestBrierScore:
    def test_confident_and_right_scores_low(self):
        rows = [_row(1.0, 0.02, conf=1.0, hit=True)] * 3
        assert scoring.brier_score(rows) == 0.0

    def test_confident_and_wrong_scores_worst(self):
        rows = [_row(1.0, -0.02, conf=1.0, hit=False)] * 3
        assert scoring.brier_score(rows) == 1.0

    def test_hedging_scores_the_baseline(self):
        rows = [_row(1.0, 0.02, conf=0.5, hit=True), _row(1.0, -0.02, conf=0.5, hit=False)]
        assert scoring.brier_score(rows) == 0.25


class TestCalibration:
    def test_detects_overconfidence(self):
        # Claims 0.9, right half the time.
        rows = [_row(1.0, 0.02, conf=0.9, hit=True), _row(1.0, -0.02, conf=0.9, hit=False)]
        buckets = scoring.calibration(rows)
        assert len(buckets) == 1
        assert buckets[0].gap == 0.9 - 0.5

    def test_top_edge_is_inclusive(self):
        # conf=1.0 must land in the last bucket, not vanish.
        assert scoring.calibration([_row(1.0, 0.02, conf=1.0, hit=True)])

    def test_empty_buckets_dropped(self):
        assert len(scoring.calibration([_row(1.0, 0.02, conf=0.5, hit=True)])) == 1


class TestScoreCard:
    def test_trust_gate_tracks_directional_count(self):
        rows = [_row(1.0, 0.01, hit=True)] * (scoring.MIN_SAMPLES_FOR_TRUST - 1)
        assert scoring.score(rows).trustworthy is False
        assert scoring.score(rows + [_row(1.0, 0.01, hit=True)]).trustworthy is True

    def test_neutral_rows_do_not_buy_trust(self):
        # n_rows is large but no directional calls were made.
        rows = [_row(0.0, 0.01)] * (scoring.MIN_SAMPLES_FOR_TRUST * 2)
        card = scoring.score(rows)
        assert card.n == 0 and card.trustworthy is False

    def test_empty_input_is_safe(self):
        card = scoring.score([])
        assert card.n == 0 and card.hit_rate is None
        assert card.as_dict()["trustworthy"] is False

    def test_score_by_slices(self):
        rows = [
            _row(1.0, 0.02, hit=True, regime="trend"),
            _row(1.0, -0.02, hit=False, regime="chop"),
        ]
        by_regime = scoring.score_by(rows, "regime")
        assert by_regime["trend"].hit_rate == 1.0
        assert by_regime["chop"].hit_rate == 0.0
