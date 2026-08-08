"""Signal grading window math — llm/signal_scorer.py.

These cover the pure functions only (no DB, no venue). The thing they are
guarding is that a grade is either correct or absent: a scorer that anchors to
the wrong bar, truncates its window, or mis-signs a short would poison the
labeled history that every later phase learns from, and it would do it silently.
"""
from src.nadobro.llm import signal_scorer as ss


def _c(t, close, high=None, low=None):
    return {
        "time": t,
        "open": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": 1.0,
    }


# 1m bars. Anchor for a signal at t=125 is the t=120 bar (close 102).
CANDLES = [
    _c(0, 100.0, 100.5, 99.5),
    _c(60, 101.0, 101.5, 100.5),
    _c(120, 102.0, 102.5, 101.5),   # anchor
    _c(180, 103.0, 106.0, 101.0),   # window: sets both excursions
    _c(240, 104.0, 104.5, 103.5),   # window: sets the final close
    _c(300, 105.0, 105.5, 104.5),   # beyond the horizon
]

SIGNAL_TS = 125.0
HORIZON_S = 120.0


class TestEpochSeconds:
    def test_seconds_pass_through(self):
        assert ss._epoch_seconds(1_700_000_000) == 1_700_000_000

    def test_milliseconds_are_converted(self):
        assert ss._epoch_seconds(1_700_000_000_000) == 1_700_000_000

    def test_junk_is_none(self):
        assert ss._epoch_seconds(None) is None
        assert ss._epoch_seconds("abc") is None
        assert ss._epoch_seconds(0) is None


class TestCandleWindow:
    def test_anchors_to_bar_at_or_before_signal(self):
        anchor, window = ss._candle_window(CANDLES, SIGNAL_TS, SIGNAL_TS + HORIZON_S)
        assert anchor == 102.0

    def test_window_excludes_anchor_and_stops_at_horizon(self):
        _, window = ss._candle_window(CANDLES, SIGNAL_TS, SIGNAL_TS + HORIZON_S)
        assert [c["time"] for c in window] == [180, 240]

    def test_signal_before_all_history_cannot_grade(self):
        anchor, window = ss._candle_window(CANDLES, -100.0, 100.0)
        assert anchor is None and window == []

    def test_untimestamped_bar_does_not_truncate_the_anchor_search(self):
        # A bad bar mid-series must be skipped, not treated as end-of-history —
        # otherwise the grade anchors to a much older price than the signal saw.
        polluted = CANDLES[:2] + [_c(None, 999.0)] + CANDLES[2:]
        anchor, _ = ss._candle_window(polluted, SIGNAL_TS, SIGNAL_TS + HORIZON_S)
        assert anchor == 102.0

    def test_horizon_with_no_completed_bars_yields_empty_window(self):
        _, window = ss._candle_window(CANDLES, SIGNAL_TS, SIGNAL_TS + 1.0)
        assert window == []


class TestCandleOrderGuardrail:
    """The indexer serves newest-first; every consumer normalizes for itself."""

    def test_reversed_input_fails_safe_rather_than_grading_wrong(self):
        anchor, window = ss._candle_window(
            list(reversed(CANDLES)), SIGNAL_TS, SIGNAL_TS + HORIZON_S
        )
        assert anchor is None and window == []

    def test_chronological_restores_the_correct_grade(self):
        from src.nadobro.engine.routines.technical_analysis import chronological

        fixed = list(chronological(list(reversed(CANDLES))))
        assert ss._candle_window(fixed, SIGNAL_TS, SIGNAL_TS + HORIZON_S) == \
            ss._candle_window(CANDLES, SIGNAL_TS, SIGNAL_TS + HORIZON_S)


class TestGrade:
    def _graded(self, bias):
        anchor, window = ss._candle_window(CANDLES, SIGNAL_TS, SIGNAL_TS + HORIZON_S)
        return ss._grade({"id": 1, "bias": bias}, "15m", anchor, window)

    def test_forward_return_uses_final_close_against_anchor(self):
        assert self._graded(0.5)["fwd_return"] == (104.0 - 102.0) / 102.0

    def test_excursions_span_the_whole_window_not_just_the_close(self):
        out = self._graded(0.5)
        # High of 106 and low of 101 both occur mid-window, on the t=180 bar.
        assert out["excursion_up"] == (106.0 - 102.0) / 102.0
        assert out["excursion_down"] == (101.0 - 102.0) / 102.0

    def test_excursions_are_direction_neutral_and_clamped(self):
        # Stored signs never depend on the call; the short below sees the same
        # numbers as the long above. Resolution happens in quant/scoring.
        assert self._graded(-0.5)["excursion_up"] == self._graded(0.5)["excursion_up"]
        assert self._graded(0.5)["excursion_up"] >= 0.0
        assert self._graded(0.5)["excursion_down"] <= 0.0

    def test_long_call_on_an_up_move_is_a_hit(self):
        assert self._graded(0.5)["directional_hit"] is True

    def test_short_call_on_an_up_move_is_a_miss(self):
        assert self._graded(-0.5)["directional_hit"] is False

    def test_neutral_call_is_ungraded_not_wrong(self):
        assert self._graded(0.0)["directional_hit"] is None

    def test_bars_used_is_recorded(self):
        assert self._graded(0.5)["bars_used"] == 2

    def test_empty_window_is_unusable(self):
        assert ss._grade({"id": 1, "bias": 0.5}, "15m", 102.0, []) is None

    def test_zero_anchor_is_unusable(self):
        # Guards against a divide-by-zero producing an infinite return.
        assert ss._grade({"id": 1, "bias": 0.5}, "15m", 0.0, CANDLES[3:5]) is None

    def test_malformed_candle_is_unusable(self):
        assert ss._grade({"id": 1, "bias": 0.5}, "15m", 102.0, [{"close": "x"}]) is None


class TestHorizonTable:
    def test_grading_timeframe_is_finer_than_the_horizon(self):
        # Measuring a 4h call's drawdown on 4h bars would report one number
        # where sixteen happened.
        seconds_per_tf = {"1m": 60, "5m": 300, "15m": 900}
        for horizon, (seconds, timeframe, _limit) in ss.HORIZONS.items():
            assert seconds_per_tf[timeframe] < seconds, horizon

    def test_lookback_never_exceeds_what_the_fetch_can_reach(self):
        # The query bound must stay inside the candle window, or every pass
        # re-selects signals it can never anchor: a permanent skip loop that
        # spends indexer budget and grades nothing.
        for horizon, (seconds, timeframe, limit) in ss.HORIZONS.items():
            reach = ss._TF_SECONDS[timeframe] * limit
            lookback = ss.horizon_lookback_seconds(horizon)
            assert lookback > 0, horizon
            assert lookback + seconds + ss._TF_SECONDS[timeframe] <= reach, horizon

    def test_every_horizon_reaches_past_a_realistic_outage(self):
        # The scorer ticks every 5 minutes; a horizon that can only look back a
        # few minutes would drop signals on any brief restart.
        for horizon in ss.HORIZONS:
            assert ss.horizon_lookback_seconds(horizon) >= 3600.0, horizon
