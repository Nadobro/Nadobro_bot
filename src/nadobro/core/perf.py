import logging

from src.nadobro.utils.env import env_float, env_int
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_MAX_SAMPLES = 400

# Samples carry a monotonic timestamp so the window can DECAY. Without this the
# deque only has a count bound (400) — at low traffic one burst of slow taps sat
# in the window for hours and check_slo re-reported the SAME p95 every 60s
# indefinitely (2026-08-14: identical p95=38918ms/n=60 for 25+ min while the
# actual fresh taps were 1-14s). A time window makes the aggregate reflect NOW.
_METRIC_WINDOW_SECONDS = env_float("NADO_PERF_WINDOW_SECONDS", 900.0)

# --- Service-level objectives ---------------------------------------------
# A single slow call already logs via ``log_slow``; the SLO check is the
# aggregate early-warning: it fires when the *p95* over the recent window
# crosses the target, which is the signal that the gateway/event-loop is
# degrading for everyone (not just one unlucky tap). Tunable via env.
_SLO_THRESHOLDS_MS: dict[str, float] = {
    "callback.total": env_float("NADO_SLO_CALLBACK_P95_MS", 1000.0),
    "message.total": env_float("NADO_SLO_MESSAGE_P95_MS", 2500.0),
    "card.home.build": env_float("NADO_SLO_HOME_BUILD_P95_MS", 250.0),
}
_SLO_MIN_SAMPLES = env_int("NADO_SLO_MIN_SAMPLES", 20)
# Re-log a still-breaching SLO at most this often. check_slo used to WARN on
# every 60s tick with no edge detection — 60 identical lines/hour per metric,
# which (with the LOWIQ flood) drowned the fresh per-tap slow-path lines.
_SLO_RELOG_SECONDS = env_float("NADO_SLO_RELOG_SECONDS", 600.0)
# Each deque entry is ``(monotonic_ts, value_ms)``.
_metrics: dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))
_counters: dict[str, int] = defaultdict(int)
_slo_last_warned: dict[str, float] = {}
_lock = threading.Lock()


def record_metric(metric: str, value_ms: float) -> None:
    try:
        val = float(value_ms)
    except (TypeError, ValueError):
        return
    if val < 0:
        return
    now = time.monotonic()
    with _lock:
        _metrics[metric].append((now, val))


def increment_counter(counter: str, value: int = 1) -> None:
    try:
        delta = int(value)
    except (TypeError, ValueError):
        return
    if delta <= 0:
        return
    with _lock:
        _counters[counter] += delta


def counters_snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


@contextmanager
def timed_metric(metric: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record_metric(metric, elapsed_ms)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * p
    lo = int(rank)
    hi = min(lo + 1, len(values) - 1)
    frac = rank - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def _reset() -> None:
    """Clear all samples/counters/SLO state. For test isolation."""
    with _lock:
        _metrics.clear()
        _counters.clear()
        _slo_last_warned.clear()


def snapshot() -> dict[str, dict]:
    out = {}
    now = time.monotonic()
    cutoff = now - _METRIC_WINDOW_SECONDS
    with _lock:
        items = []
        for metric, buf in _metrics.items():
            # Drop samples older than the window so aggregates reflect NOW.
            while buf and buf[0][0] < cutoff:
                buf.popleft()
            items.append((metric, [v for _, v in buf]))
    for metric, samples in items:
        vals = sorted(samples)
        if not vals:
            continue
        out[metric] = {
            "count": len(vals),
            "p50_ms": round(_percentile(vals, 0.50), 2),
            "p95_ms": round(_percentile(vals, 0.95), 2),
            "max_ms": round(vals[-1], 2),
            "avg_ms": round(sum(vals) / len(vals), 2),
        }
    return out


def summary_lines(top_n: int = 8) -> list[str]:
    snap = snapshot()
    ctrs = counters_snapshot()
    counter_lines = [
        f"{name}: count={count}" for name, count in sorted(ctrs.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    ]
    if not snap:
        if counter_lines:
            return counter_lines
        return ["No performance samples yet."]
    ranked = sorted(snap.items(), key=lambda kv: kv[1]["p95_ms"], reverse=True)
    lines = []
    for metric, data in ranked[:top_n]:
        lines.append(
            f"{metric}: p50={data['p50_ms']}ms p95={data['p95_ms']}ms "
            f"avg={data['avg_ms']}ms n={data['count']}"
        )
    lines.extend(counter_lines)
    return lines


def log_slow(metric: str, threshold_ms: float, started_at: float) -> None:
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    record_metric(metric, elapsed_ms)
    if elapsed_ms >= threshold_ms:
        logger.warning("%s slow-path %.2fms", metric, elapsed_ms)


def check_slo() -> list[str]:
    """Evaluate p95 of each tracked SLO metric against its target.

    Returns a list of human-readable breach lines (also logged at WARNING) so a
    scheduler tick or /ops view can surface sustained degradation. Empty list =
    all SLOs healthy. Cheap: reads the in-memory metric window only.
    """
    snap = snapshot()
    breaches: list[str] = []
    now = time.monotonic()
    for metric, threshold_ms in _SLO_THRESHOLDS_MS.items():
        data = snap.get(metric)
        if not data or data["count"] < _SLO_MIN_SAMPLES:
            # Window aged out / not enough live samples: the breach is over.
            if _slo_last_warned.pop(metric, None) is not None:
                logger.info("SLO recovered %s: window below %d samples", metric, _SLO_MIN_SAMPLES)
            continue
        if data["p95_ms"] >= threshold_ms:
            line = (
                f"SLO breach {metric}: p95={data['p95_ms']:.0f}ms "
                f"(target {threshold_ms:.0f}ms) p50={data['p50_ms']:.0f}ms "
                f"max={data['max_ms']:.0f}ms n={data['count']}"
            )
            # Edge-triggered: WARN on the first breach and then at most once per
            # _SLO_RELOG_SECONDS, instead of every 60s tick, so the log stays
            # readable and the counter tracks episodes not ticks.
            last = _slo_last_warned.get(metric)
            if last is None or (now - last) >= _SLO_RELOG_SECONDS:
                _slo_last_warned[metric] = now
                increment_counter(f"slo.breach.{metric}")
                logger.warning(line)
            breaches.append(line)
        else:
            if _slo_last_warned.pop(metric, None) is not None:
                logger.info("SLO recovered %s: p95=%.0fms", metric, data["p95_ms"])
    return breaches
