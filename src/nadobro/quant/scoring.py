"""Signal grading metrics — pure math, no DB, no venue, no clock.

Turns rows from ``signal_outcomes`` into the numbers that answer "is this call
worth anything": hit rate, expectancy, information coefficient, calibration.

Why this exists
===============
The overlay has been logging every decision it makes to ``overlay_signals``
since it shipped, and grading exactly none of them. It could therefore only
drift — a weight that made money and a weight that lost money looked identical
from the inside. Everything downstream of this module (feature weighting, the
auto-apply envelope, the track record shown on a recommendation card) needs one
honest scorer, so there is one, and it lives in a leaf package where the engine,
strategy and LLM layers can all reach it.

Conventions
===========
Returns are signed FRACTIONS of the anchor price, never percents. Excursions are
stored direction-neutral (``excursion_up >= 0``, ``excursion_down <= 0``) and
resolved into MFE/MAE here against the sign of ``bias``, so a short call that
went right reads as favourable rather than as a large negative.

A ``bias`` of exactly 0 is not a directional call and is excluded from hit rate,
expectancy and IC. Counting neutral calls as wrong would make "quote nothing"
look like a losing opinion; counting them as right would make it free.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

Outcome = Mapping[str, Any]

# Below this many graded calls, treat every metric as indicative only. Chosen so
# a hit rate is distinguishable from a coin flip at roughly the 10-point level:
# the standard error of a proportion at p=0.5, n=100 is 5pp, so a 60% read is
# ~2 SE from noise. Anything that moves live money must clear this AND beat
# baseline in backtest — see the auto-apply envelope.
MIN_SAMPLES_FOR_TRUST = 100

# Default confidence buckets for calibration. Edges, not centres.
DEFAULT_CALIBRATION_EDGES: Tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)


def _f(value: object) -> Optional[float]:
    """Best-effort float. ``None`` for NULLs and anything unparseable."""
    if value is None:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def directional(outcomes: Sequence[Outcome]) -> List[Outcome]:
    """Rows carrying an actual directional call (non-zero, non-null bias)."""
    out: List[Outcome] = []
    for row in outcomes:
        bias = _f(row.get("bias"))
        if bias is not None and bias != 0.0 and _f(row.get("fwd_return")) is not None:
            out.append(row)
    return out


def mfe_mae(row: Outcome) -> Tuple[Optional[float], Optional[float]]:
    """Maximum favourable / adverse excursion, resolved against the call's sign.

    Returns ``(mfe, mae)`` with ``mfe >= 0`` and ``mae <= 0`` when both legs are
    present, or ``(None, None)`` for a neutral or unparseable row.
    """
    bias = _f(row.get("bias"))
    up = _f(row.get("excursion_up"))
    down = _f(row.get("excursion_down"))
    if bias is None or bias == 0.0 or up is None or down is None:
        return None, None
    if bias > 0:
        return up, down
    # Short call: an adverse move is the market going up.
    return -down, -up


def hit_rate(outcomes: Sequence[Outcome]) -> Optional[float]:
    """Fraction of directional calls whose sign matched the realized move.

    Prefers the stored ``directional_hit`` (written at grading time) and falls
    back to recomputing from bias and forward return, so rows graded by an older
    scorer still count.
    """
    rows = directional(outcomes)
    if not rows:
        return None
    hits = 0
    for row in rows:
        stored = row.get("directional_hit")
        if isinstance(stored, bool):
            hits += 1 if stored else 0
            continue
        bias = _f(row.get("bias")) or 0.0
        ret = _f(row.get("fwd_return")) or 0.0
        hits += 1 if (bias > 0) == (ret > 0) and ret != 0.0 else 0
    return hits / len(rows)


def expectancy(outcomes: Sequence[Outcome]) -> Optional[float]:
    """Mean forward return *in the direction of the call*, as a fraction.

    The most direct "did this opinion make money" number, and the one that stays
    honest when a strategy is right often but wrong big.
    """
    rows = directional(outcomes)
    if not rows:
        return None
    total = 0.0
    for row in rows:
        bias = _f(row.get("bias")) or 0.0
        ret = _f(row.get("fwd_return")) or 0.0
        total += ret if bias > 0 else -ret
    return total / len(rows)


def edge_ratio(outcomes: Sequence[Outcome]) -> Optional[float]:
    """Mean MFE over mean |MAE| — does the call run further right than wrong?

    Above 1.0 means the signal has favourable asymmetry even before any exit
    rule; below 1.0 means the exit rule is doing all the work.
    """
    favourable: List[float] = []
    adverse: List[float] = []
    for row in outcomes:
        mfe, mae = mfe_mae(row)
        if mfe is None or mae is None:
            continue
        favourable.append(mfe)
        adverse.append(abs(mae))
    if not favourable:
        return None
    mean_adverse = sum(adverse) / len(adverse)
    if mean_adverse <= 0:
        return None
    return (sum(favourable) / len(favourable)) / mean_adverse


def _ranks(values: Sequence[float]) -> List[float]:
    """Ascending ranks with ties averaged (needed for a correct Spearman)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def information_coefficient(outcomes: Sequence[Outcome]) -> Optional[float]:
    """Spearman rank correlation between predicted bias and realized return.

    Rank-based rather than Pearson because crypto forward returns are fat-tailed
    — a single liquidation cascade would otherwise set the correlation on its
    own. ``None`` when there is no variance to correlate (e.g. every call had
    the same bias), which is a real state, not a zero.
    """
    rows = [
        r for r in outcomes
        if _f(r.get("bias")) is not None and _f(r.get("fwd_return")) is not None
    ]
    if len(rows) < 3:
        return None
    xs = [_f(r.get("bias")) or 0.0 for r in rows]
    ys = [_f(r.get("fwd_return")) or 0.0 for r in rows]
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = float(len(rows))
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den_x = math.sqrt(sum((a - mx) ** 2 for a in rx))
    den_y = math.sqrt(sum((b - my) ** 2 for b in ry))
    if den_x <= 0 or den_y <= 0:
        return None
    return num / (den_x * den_y)


def brier_score(outcomes: Sequence[Outcome]) -> Optional[float]:
    """Mean squared error between stated confidence and being right.

    Lower is better; 0.25 is what you score by saying 0.5 to everything. This is
    the metric that catches an overlay which is directionally fine but shouts
    0.9 confidence at coin flips — exactly the failure that makes a size
    multiplier dangerous.
    """
    rows = directional(outcomes)
    scored: List[Tuple[float, float]] = []
    for row in rows:
        conf = _f(row.get("confidence"))
        if conf is None:
            continue
        stored = row.get("directional_hit")
        if isinstance(stored, bool):
            actual = 1.0 if stored else 0.0
        else:
            bias = _f(row.get("bias")) or 0.0
            ret = _f(row.get("fwd_return")) or 0.0
            actual = 1.0 if ((bias > 0) == (ret > 0) and ret != 0.0) else 0.0
        scored.append((min(max(conf, 0.0), 1.0), actual))
    if not scored:
        return None
    return sum((c - a) ** 2 for c, a in scored) / len(scored)


@dataclass(frozen=True)
class CalibrationBucket:
    low: float
    high: float
    count: int
    mean_confidence: float
    hit_rate: float

    @property
    def gap(self) -> float:
        """Stated confidence minus realized hit rate. Positive = overconfident."""
        return self.mean_confidence - self.hit_rate


def calibration(
    outcomes: Sequence[Outcome],
    edges: Sequence[float] = DEFAULT_CALIBRATION_EDGES,
) -> List[CalibrationBucket]:
    """Hit rate within each confidence band. Empty bands are dropped.

    Read the ``gap``: a well-calibrated overlay claiming 0.8 should be right
    about 80% of the time. Persistent positive gaps mean the size multiplier is
    being driven by a number that has not earned its scale.
    """
    rows = directional(outcomes)
    buckets: List[CalibrationBucket] = []
    for i in range(len(edges) - 1):
        low, high = float(edges[i]), float(edges[i + 1])
        last = i == len(edges) - 2
        members = []
        for row in rows:
            conf = _f(row.get("confidence"))
            if conf is None:
                continue
            if low <= conf < high or (last and conf == high):
                members.append(row)
        if not members:
            continue
        rate = hit_rate(members)
        if rate is None:
            continue
        mean_conf = sum(_f(r.get("confidence")) or 0.0 for r in members) / len(members)
        buckets.append(
            CalibrationBucket(
                low=low, high=high, count=len(members),
                mean_confidence=mean_conf, hit_rate=rate,
            )
        )
    return buckets


@dataclass(frozen=True)
class ScoreCard:
    """One slice's grades. ``n`` is the directional-call count, not the row count."""
    n: int
    n_rows: int
    hit_rate: Optional[float] = None
    expectancy: Optional[float] = None
    edge_ratio: Optional[float] = None
    information_coefficient: Optional[float] = None
    brier_score: Optional[float] = None
    calibration: List[CalibrationBucket] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        """Enough graded calls to act on. Gate live weight changes on this."""
        return self.n >= MIN_SAMPLES_FOR_TRUST

    def as_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n,
            "n_rows": self.n_rows,
            "trustworthy": self.trustworthy,
            "hit_rate": self.hit_rate,
            "expectancy": self.expectancy,
            "edge_ratio": self.edge_ratio,
            "information_coefficient": self.information_coefficient,
            "brier_score": self.brier_score,
            "calibration": [
                {
                    "low": b.low, "high": b.high, "count": b.count,
                    "mean_confidence": b.mean_confidence,
                    "hit_rate": b.hit_rate, "gap": b.gap,
                }
                for b in self.calibration
            ],
        }


def score(outcomes: Sequence[Outcome]) -> ScoreCard:
    """Grade one slice of outcomes. Safe on an empty sequence."""
    return ScoreCard(
        n=len(directional(outcomes)),
        n_rows=len(outcomes),
        hit_rate=hit_rate(outcomes),
        expectancy=expectancy(outcomes),
        edge_ratio=edge_ratio(outcomes),
        information_coefficient=information_coefficient(outcomes),
        brier_score=brier_score(outcomes),
        calibration=calibration(outcomes),
    )


def score_by(outcomes: Sequence[Outcome], key: str) -> Dict[Any, ScoreCard]:
    """Grade sliced by a column — ``regime``, ``horizon``, ``product_name``.

    Slicing is where the useful answers live: an overlay that is flat overall is
    often strongly positive in one regime and negative in another, and that is
    a weighting problem rather than a dead signal.
    """
    groups: Dict[Any, List[Outcome]] = {}
    for row in outcomes:
        groups.setdefault(row.get(key), []).append(row)
    return {k: score(v) for k, v in groups.items()}
