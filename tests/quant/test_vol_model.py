"""Known-answer + property tests for quant/vol_model (the grid-family vol model).

The oracle for the statistic is the harness expression itself
(``dgrid_evolved.py``): ``sqrt(mean(diff(log(r))**2)) * 1e4``.
"""
from __future__ import annotations

import math
import random

import pytest

from src.nadobro.quant import vol_model as vm

NOW = 1_800_000_000 - (1_800_000_000 % 60)   # minute-aligned "now"


def _oracle_rv(closes, window=60):
    r = closes[-(window + 1):]
    d = [math.log(r[i + 1]) - math.log(r[i]) for i in range(len(r) - 1)]
    return math.sqrt(sum(x * x for x in d) / len(d)) * 1e4


def _series_from_returns(rets_bp, *, start_px=100_000.0, end_open_ts=None):
    """Closed-minute series whose i-th log return is rets_bp[i] bp; the newest bar
    opened one minute before NOW (so it is closed and 0-60s old)."""
    closes = [start_px]
    for r in rets_bp:
        closes.append(closes[-1] * math.exp(r / 1e4))
    end_open = (NOW - 60) if end_open_ts is None else end_open_ts
    start = end_open - 60 * (len(closes) - 1)
    return vm.MinuteSeries(start_ts=start, closes=tuple(closes), real=tuple([True] * len(closes)))


def _alt(n, amp):
    """Alternating +amp/-amp returns: rv == amp exactly."""
    return [amp if i % 2 == 0 else -amp for i in range(n)]


def _baseline(median):
    return vm.VolBaseline(median_rv60_bp=median, coverage_h=168.0, n_minutes=10080, newest_ts=NOW)


# -- the statistic -----------------------------------------------------------
def test_realized_vol_matches_the_harness_formula():
    rng = random.Random(7)
    closes = [100.0]
    for _ in range(300):
        closes.append(closes[-1] * math.exp(rng.gauss(0, 4e-4)))
    assert vm.realized_vol_bp(closes, 60) == pytest.approx(_oracle_rv(closes), rel=1e-12)
    assert vm.realized_vol_bp(closes, 15) == pytest.approx(_oracle_rv(closes, 15), rel=1e-12)


def test_realized_vol_known_answer_and_edge_cases():
    s = _series_from_returns(_alt(60, 3.0))
    assert vm.realized_vol_bp(s.closes, 60) == pytest.approx(3.0, rel=1e-9)
    assert vm.realized_vol_bp([100.0] * 60, 60) is None          # 59 returns
    assert vm.realized_vol_bp([100.0] * 60 + [0.0], 60) is None   # non-positive close


def test_rv_series_equals_direct_computation():
    rng = random.Random(3)
    closes = [50.0]
    for _ in range(500):
        closes.append(closes[-1] * math.exp(rng.gauss(0, 5e-4)))
    series = vm.rv_series_bp(closes, 60)
    for i in (59, 60, 61, 200, 500):
        direct = vm.realized_vol_bp(closes[: i + 1], 60)
        if direct is None:
            assert series[i] is None
        else:
            assert series[i] == pytest.approx(direct, rel=1e-9)


# -- series construction -----------------------------------------------------
def test_closed_minute_series_drops_in_progress_sorts_coerces_ms_and_fills_gaps():
    rows = [
        {"time": (NOW - 60) * 1000, "close": "103"},    # ms, closed (newest)
        {"time": NOW, "close": 999},                   # in-progress -> dropped
        {"time": NOW - 240, "close": 100},
        {"time": NOW - 180, "close": 101},
        # NOW-120 missing -> forward-filled
    ]
    s = vm.closed_minute_series(rows, now_s=NOW + 10)
    assert s is not None
    assert s.closes == (100.0, 101.0, 101.0, 103.0)
    assert s.real == (True, True, False, True)
    assert s.newest_ts == NOW - 60
    assert s.newest_close_ts == NOW


def test_empty_candles_are_no_information_not_zero_vol():
    assert vm.closed_minute_series([], now_s=NOW) is None
    assert vm.closed_minute_series(None, now_s=NOW) is None
    v = vm.gate_verdict(None, _baseline(3.7), now_s=NOW)
    assert v.state == vm.UNKNOWN and v.reason == "vol_unknown"
    assert v.state != vm.CALM


def test_merge_series_keeps_real_bars_and_honest_gaps():
    a = vm.MinuteSeries(start_ts=0, closes=(1.0, 2.0), real=(True, True))
    b = vm.MinuteSeries(start_ts=240, closes=(5.0, 6.0), real=(True, True))
    m = vm.merge_series(a, b)
    assert m.closes == (1.0, 2.0, 2.0, 2.0, 5.0, 6.0)
    assert m.real == (True, True, False, False, True, True)
    trimmed = vm.merge_series(a, b, max_minutes=3)
    assert trimmed.start_ts == 180 and trimmed.closes == (2.0, 5.0, 6.0)


# -- gate --------------------------------------------------------------------
def test_gate_calm_when_last_15_minutes_are_all_under_the_gate():
    s = _series_from_returns(_alt(120, 2.0))
    v = vm.gate_verdict(s, _baseline(4.0), mult=0.82, now_s=NOW + 5)
    assert v.state == vm.CALM and v.reason == ""
    assert v.gate_bp == pytest.approx(3.28)
    assert v.rv60_bp == pytest.approx(2.0, rel=1e-9)
    assert v.calm_streak_min == 15 and v.resume_in_min == 0


def test_gate_pause_is_immediate_and_resume_needs_15_calm_minutes():
    # 60 hot minutes (rv 6) then k calm ones (rv 2): the rv60 stays above a 3.28
    # gate until enough calm returns have displaced the hot ones.
    base = _baseline(4.0)
    hot = vm.gate_verdict(_series_from_returns(_alt(120, 6.0)), base, mult=0.82, now_s=NOW)
    assert hot.state == vm.HOT and hot.reason == "vol_hot" and hot.calm_streak_min == 0
    # A single hot minute at the end -> HOT immediately.
    rets = _alt(120, 1.0) + [60.0]
    one = vm.gate_verdict(_series_from_returns(rets), base, mult=0.82, now_s=NOW)
    assert one.state == vm.HOT


def test_stateless_gate_equals_the_v2_gated_until_rule():
    """Property: at every minute the stateless verdict equals a direct simulation
    of V2 (``gated_until = t + 900`` after any hot reading, per closed minute)."""
    rng = random.Random(11)
    for trial in range(20):
        rets = []
        for _ in range(400):
            vol = 1.0 if rng.random() < 0.5 else 5.0
            rets.append(rng.gauss(0, vol))
        full = _series_from_returns(rets)
        closes = full.closes
        gate = 3.0
        gated_until = -1
        for t in range(60, len(closes)):
            rv = vm.realized_vol_bp(closes[: t + 1], 60)
            if rv > gate:
                gated_until = t + 15
            v2_on = t >= gated_until
            if t < 60 + 15:
                continue
            sub = vm.MinuteSeries(start_ts=0, closes=closes[: t + 1], real=full.real[: t + 1])
            now = sub.newest_close_ts
            v = vm.gate_verdict(sub, _baseline(gate / 0.82), mult=0.82, now_s=now)
            assert (v.state == vm.CALM) == v2_on, (trial, t)


def test_gate_unknown_on_stale_thin_and_short_data():
    base = _baseline(4.0)
    s = _series_from_returns(_alt(120, 2.0))
    stale = vm.gate_verdict(s, base, now_s=NOW + 181)
    assert stale.state == vm.UNKNOWN and "old" in stale.detail
    fresh = vm.gate_verdict(s, base, now_s=NOW + 179)
    assert fresh.state == vm.CALM
    thin = vm.MinuteSeries(start_ts=s.start_ts, closes=s.closes,
                           real=tuple([True] * 90 + [False] * 31))
    assert vm.gate_verdict(thin, base, now_s=NOW).state == vm.UNKNOWN
    short = _series_from_returns(_alt(40, 2.0))
    assert vm.gate_verdict(short, base, now_s=NOW).state == vm.UNKNOWN


def test_gate_warming_until_72h_of_baseline():
    s = _series_from_returns(_alt(120, 2.0))
    warm = vm.VolBaseline(median_rv60_bp=None, coverage_h=41.5, n_minutes=2490, newest_ts=NOW)
    v = vm.gate_verdict(s, warm, now_s=NOW)
    assert v.state == vm.WARMING and v.reason == "vol_warming" and "41h of 72h" in v.detail
    assert vm.gate_verdict(s, None, now_s=NOW).state == vm.WARMING


# -- baseline ----------------------------------------------------------------
def test_baseline_median_and_coverage():
    s = _series_from_returns(_alt(80 * 60, 3.0))
    b = vm.baseline_from_series(s)
    assert b is not None and b.ready
    assert b.median_rv60_bp == pytest.approx(3.0, rel=1e-6)
    assert b.coverage_h == pytest.approx((80 * 60 - 59) / 60.0, abs=0.05)
    short = vm.baseline_from_series(_series_from_returns(_alt(10 * 60, 3.0)))
    assert short is not None and not short.ready and short.median_rv60_bp is None


def test_baseline_excludes_long_no_bar_runs():
    rets = _alt(90 * 60, 3.0)
    s = _series_from_returns(rets)
    real = list(s.real)
    # A 10h "closed market" run: forward-filled, zero returns.
    closes = list(s.closes)
    for i in range(1000, 1600):
        closes[i] = closes[999]
        real[i] = False
    gapped = vm.MinuteSeries(start_ts=s.start_ts, closes=tuple(closes), real=tuple(real))
    b = vm.baseline_from_series(gapped)
    # Without the exclusion the zero-return minutes would drag the median down.
    assert b.median_rv60_bp == pytest.approx(3.0, rel=1e-6)
    assert b.n_minutes < vm.baseline_from_series(s).n_minutes


# -- scale invariance (the per-product guarantee) ----------------------------
@pytest.mark.parametrize("c", [0.3, 3.0, 10.0])
def test_verdicts_are_scale_invariant_and_spacing_scales_until_the_floor(c):
    rng = random.Random(5)
    rets = [rng.gauss(0, 1.0 if (i // 200) % 2 else 4.0) for i in range(80 * 60)]
    s1 = _series_from_returns(rets)
    sc = _series_from_returns([r * c for r in rets])
    b1 = vm.baseline_from_series(s1)
    bc = vm.baseline_from_series(sc)
    assert bc.median_rv60_bp == pytest.approx(b1.median_rv60_bp * c, rel=1e-6)
    for cut in range(len(s1) - 400, len(s1), 7):
        a = vm.MinuteSeries(start_ts=s1.start_ts, closes=s1.closes[:cut], real=s1.real[:cut])
        b = vm.MinuteSeries(start_ts=sc.start_ts, closes=sc.closes[:cut], real=sc.real[:cut])
        now = a.newest_close_ts
        assert vm.gate_verdict(a, b1, now_s=now).state == vm.gate_verdict(b, bc, now_s=now).state
        assert vm.arm_verdict(a, b1, now_s=now).state == vm.arm_verdict(b, bc, now_s=now).state
    rv1 = vm.realized_vol_bp(s1.closes)
    rvc = vm.realized_vol_bp(sc.closes)
    sp1 = vm.spacing_bp(rv1, k=2.6, floor_bp=0.0)
    spc = vm.spacing_bp(rvc, k=2.6, floor_bp=0.0)
    assert spc == pytest.approx(sp1 * c, rel=1e-6)


# -- spacing / skew ----------------------------------------------------------
def test_spacing_respects_floor_min_max_and_tick():
    assert vm.spacing_bp(None) is None
    assert vm.spacing_bp(3.0, k=2.6, floor_bp=6.8) == pytest.approx(7.8)
    assert vm.spacing_bp(1.0, k=2.6, floor_bp=6.8) == pytest.approx(6.8)           # fee floor
    assert vm.spacing_bp(1.0, k=2.6, floor_bp=6.8, min_bp=9.0) == pytest.approx(9.0)
    assert vm.spacing_bp(10.0, k=2.6, floor_bp=6.8, max_bp=20.0) == pytest.approx(20.0)
    assert vm.spacing_bp(1.0, k=2.6, floor_bp=6.8, tick_bp=5.0) == pytest.approx(10.0)
    # max below the floor never pushes spacing under the floor
    assert vm.spacing_bp(10.0, k=2.6, floor_bp=6.8, max_bp=3.0) == pytest.approx(6.8)
    assert vm.CLASSIC_SPACING_FLOOR_BP == pytest.approx(6.8)


def test_skew_sign_and_bound():
    assert vm.skew_offset_bp(0.5, None, 8.0) == 0.0
    assert vm.skew_offset_bp(0.0, 3.0, 8.0) == 0.0
    long = vm.skew_offset_bp(1.0, 3.0, 8.0)
    short = vm.skew_offset_bp(-1.0, 3.0, 8.0)
    assert long < 0 < short
    for q in (0.1, 0.5, 1.0, 5.0):
        assert abs(vm.skew_offset_bp(q, 50.0, 8.0)) <= 0.5 * 8.0 + 1e-12


def test_spacing_deadband():
    assert not vm.spacing_change_significant(8.0, 8.9)       # < max(1, 1.2)
    assert vm.spacing_change_significant(8.0, 9.3)
    assert not vm.spacing_change_significant(8.0, None)
    assert vm.spacing_change_significant(None, 8.0)


# -- R-Grid arm --------------------------------------------------------------
def test_arm_requires_expansion_after_compression():
    base = _baseline(4.0)   # compress <= 3.64, expand >= 5.68
    quiet_then_burst = _alt(150, 2.0) + _alt(20, 12.0)
    v = vm.arm_verdict(_series_from_returns(quiet_then_burst), base, now_s=NOW)
    assert v.state == vm.ARMED and v.compressed_ago_min is not None

    quiet_only = _alt(200, 2.0)
    w = vm.arm_verdict(_series_from_returns(quiet_only), base, now_s=NOW)
    assert w.state == vm.WAITING and w.reason == "rgrid_vol_wait"
    assert w.detail == "needs_expansion"

    hot_only = _alt(200, 12.0)       # never compressed
    h = vm.arm_verdict(_series_from_returns(hot_only), base, now_s=NOW)
    assert h.state == vm.WAITING and h.detail == "needs_compression"


def test_arm_expires_after_the_hold_window():
    base = _baseline(4.0)
    # burst ended 10 minutes ago (hold is 5): rv15 is back under the expand line.
    rets = _alt(150, 2.0) + _alt(20, 12.0) + _alt(20, 1.0)
    v = vm.arm_verdict(_series_from_returns(rets), base, now_s=NOW)
    assert v.state == vm.WAITING


def test_arm_unknown_and_warming():
    s = _series_from_returns(_alt(200, 2.0))
    assert vm.arm_verdict(None, _baseline(4.0), now_s=NOW).state == vm.UNKNOWN
    assert vm.arm_verdict(s, _baseline(4.0), now_s=NOW + 500).state == vm.UNKNOWN
    assert vm.arm_verdict(s, None, now_s=NOW).state == vm.WARMING


def test_blend():
    assert vm.blend_rv_bp({"a": (3.0, 1.0), "b": (None, 1.0)}) == pytest.approx(3.0)
    assert vm.blend_rv_bp({"a": (3.0, 1.0), "b": (4.0, 1.0)}) == pytest.approx(math.sqrt(12.5))
    assert vm.blend_rv_bp({"a": (3.0, 0.0)}) is None
