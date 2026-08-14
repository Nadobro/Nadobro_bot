"""Regression tests for the time-windowed perf metrics + edge-triggered SLO.

Before this, the sample deque had only a count bound (400) and no time window,
so one burst of slow taps replayed the same p95 WARNING every 60s indefinitely
(2026-08-14: identical p95=38918ms/n=60 for 25+ min). These pin the fix.
"""
import logging

from _stubs import install_test_stubs

install_test_stubs()


def _reset():
    from src.nadobro.core import perf
    perf._reset()
    return perf


def test_snapshot_keeps_exact_keys():
    """Runtime consumers (main.py perf_top, nado_tooling_service perf) depend on
    this dict shape — it must not change."""
    perf = _reset()
    perf.record_metric("callback.total", 1200.0)
    data = perf.snapshot()["callback.total"]
    assert set(data.keys()) == {"count", "p50_ms", "p95_ms", "max_ms", "avg_ms"}


def test_samples_outside_window_are_excluded(monkeypatch):
    perf = _reset()
    monkeypatch.setattr(perf, "_METRIC_WINDOW_SECONDS", 100.0)

    clock = {"t": 1_000.0}
    monkeypatch.setattr(perf.time, "monotonic", lambda: clock["t"])

    for _ in range(30):
        perf.record_metric("callback.total", 40_000.0)
    assert perf.snapshot()["callback.total"]["count"] == 30

    # Advance past the window: the old burst must age out entirely.
    clock["t"] += 200.0
    assert "callback.total" not in perf.snapshot()


def test_check_slo_is_edge_triggered(monkeypatch, caplog):
    perf = _reset()
    monkeypatch.setattr(perf, "_METRIC_WINDOW_SECONDS", 10_000.0)
    monkeypatch.setattr(perf, "_SLO_RELOG_SECONDS", 10_000.0)
    monkeypatch.setattr(perf, "_SLO_MIN_SAMPLES", 5)

    for _ in range(20):
        perf.record_metric("callback.total", 40_000.0)

    with caplog.at_level(logging.WARNING, logger="src.nadobro.core.perf"):
        first = perf.check_slo()
        second = perf.check_slo()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    # Both ticks still REPORT the breach (return value reflects live state)...
    assert len(first) == 1 and len(second) == 1
    # ...but the WARNING is logged only on the breach edge, not every tick.
    assert len(warnings) == 1
    # Counter counts episodes, not ticks.
    assert perf.counters_snapshot().get("slo.breach.callback.total") == 1


def test_check_slo_recovers_when_window_ages_out(monkeypatch, caplog):
    perf = _reset()
    monkeypatch.setattr(perf, "_METRIC_WINDOW_SECONDS", 100.0)
    monkeypatch.setattr(perf, "_SLO_MIN_SAMPLES", 5)

    clock = {"t": 5_000.0}
    monkeypatch.setattr(perf.time, "monotonic", lambda: clock["t"])

    for _ in range(20):
        perf.record_metric("callback.total", 40_000.0)
    assert perf.check_slo()  # breaching now

    clock["t"] += 200.0  # window ages out
    with caplog.at_level(logging.INFO, logger="src.nadobro.core.perf"):
        assert perf.check_slo() == []
    assert any("recovered" in r.getMessage() for r in caplog.records)
