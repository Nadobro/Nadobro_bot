"""Variance-ratio regime routine — the brain the Dynamic Grid was missing.

The engine-v2 ``DynamicGridController`` previously classified the market with a
hardcoded EMA stack, only at first spawn, and ignored every user-facing
``dgrid_*`` setting (variance ratios, window points). This routine restores a
real, tunable regime classifier built around a horizon **variance ratio**.

Variance ratio
==============
For log returns over an overlapping horizon ``k``,
``r_t^(k) = ln(p_t / p_{t-k})``, define the per-period mean-squared return::

    M(k) = mean(r_t^(k)^2) / k

and the variance ratio between the user's two horizons
(``dgrid_short_window_points`` / ``dgrid_long_window_points``)::

    VR = M(long) / M(short)

Behavior:
- **Random walk** (iid returns): variance grows linearly with the horizon, so
  ``M(long) ~= M(short)`` and ``VR ~= 1``.
- **Trend** (directional drift): the long-horizon move accumulates the drift,
  so ``M(long) >> M(short)`` and ``VR`` rises toward ``long / short`` (> 1) —
  grid-hostile.
- **Mean-reversion** (a range that keeps snapping back, grid-friendly): the
  long-horizon move stays bounded while short-horizon noise persists, so
  ``VR < 1``.

This uses the **non-central** second moment on purpose: unlike the textbook
Lo–MacKinlay statistic (which subtracts the drift), we *want* directional drift
to register as a trend, because that is exactly the regime a long grid must
step aside from or flip against.

Phase decision (with hysteresis)
================================
Two thresholds form a hysteresis band so a ratio flickering at the boundary
never churns a flip:

- ``VR >= trend_on`` (``dgrid_trend_on_variance_ratio``, default 1.25):
  trending. The trend phase is **RGRID** in BOTH directions — R-Grid adds with
  the move (a mirrored short *ladder* used to bleed in an uptrend).
- ``VR <= range_on`` (``dgrid_range_on_variance_ratio``, default 1.15):
  ranging -> long **GRID** (the classic short-vol grid).
- In between -> hold the current phase.

DIRECTIONAL RELEASE (``TREND_RELEASE_FRACTION``)
-----------------------------------------------
Leaving **RGRID** needs the move to actually STALL, not merely ease.
The trend trigger is ``|drift| >= trend_drift_pct``; the release is only half
that. Without it the range branch was direction-blind — a low variance ratio
says "no volatility burst", it does NOT say "price has stopped" — so the moment
a decline slowed a little, ``VR <= range_on`` handed the trend follower straight
back to the long grid while price was still falling. A V-reversal (sign flip
with ``|drift|`` still above the release) stays **RGRID**: R-Grid owns the side
flip.

Deterministic: no LLM, no state beyond the inputs + the caller's current phase.
"""
from __future__ import annotations

import math
from typing import Dict, List, Mapping, Optional, Sequence

from src.nadobro.engine.routines.technical_analysis import _closes, chronological

Candle = Mapping[str, float]

GRID = "grid"        # long mean-reversion ladder (range)
RGRID = "rgrid"      # R-Grid trend follower (both directions)

UP = "up"
DOWN = "down"
FLAT = "flat"

# Hysteresis for LEAVING a directional phase. Enter a trend at
# ``|drift| >= trend_drift_pct``; release it only once |drift| has decayed below
# this fraction of that threshold. Halving gives a wide enough dead band that
# ordinary variation in the rate of a decline cannot churn the side, while a
# genuine stall still returns the ranging ladder; a V-reversal stays RGRID.
TREND_RELEASE_FRACTION = 0.5


def _log_returns(closes: Sequence[float], horizon: int) -> List[float]:
    """Overlapping ``horizon``-period log returns; skips non-positive prices."""
    out: List[float] = []
    for i in range(horizon, len(closes)):
        a, b = closes[i - horizon], closes[i]
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _mean_square_per_period(values: Sequence[float], horizon: int) -> float:
    """Non-central second moment of the returns, normalized per period."""
    n = len(values)
    if n == 0 or horizon <= 0:
        return 0.0
    return (sum(v * v for v in values) / n) / horizon


def variance_ratio(closes: Sequence[float], short_window: int, long_window: int) -> Optional[float]:
    """Horizon variance ratio ``M(long) / M(short)`` (per-period mean-squared
    returns). ``None`` when there is not enough history or the short horizon is
    degenerate (flat prices)."""
    short_window = max(1, int(short_window))
    long_window = max(short_window + 1, int(long_window))
    short_returns = _log_returns(closes, short_window)
    long_returns = _log_returns(closes, long_window)
    if len(short_returns) < 2 or len(long_returns) < 2:
        return None
    m_short = _mean_square_per_period(short_returns, short_window)
    m_long = _mean_square_per_period(long_returns, long_window)
    if m_short <= 0:
        return None
    return m_long / m_short


async def run(
    trading_pair: str,
    candles: Sequence[Candle],
    *,
    short_window: int = 4,
    long_window: int = 12,
    trend_on: float = 1.25,
    range_on: float = 1.15,
    trend_drift_pct: float = 0.30,
    current_phase: str = GRID,
) -> Dict[str, object]:
    """Classify the regime and recommend a grid phase.

    Two trend signals, EITHER of which flips direction:
    * **variance ratio** ``VR >= trend_on`` — bursty / accelerating moves.
    * **directional drift** ``|drift over long window| >= trend_drift_pct`` — a
      sustained one-way grind. This is the case a long grid bleeds in but the VR
      misses: a slow steady decline keeps ``VR < 1`` (looks "mean-reverting")
      even as price trends down. Without this filter dgrid stayed long GRID and
      kept buying into the drop. ``trend_drift_pct`` is a percent (0.30 = 0.30%).

    Returns ``{phase, variance_ratio, direction, drift_pct, trend_by_drift,
    insufficient_history}``. On insufficient history the verdict holds
    ``current_phase`` so the caller never flips on noise.
    """
    short_window = max(2, int(short_window))
    long_window = max(short_window + 1, int(long_window))
    # Need at least one full long-horizon return window plus a couple of points
    # to estimate variance.
    required = long_window + 2
    # CANDLE-ORDER guardrail (2026-07-31): the drift window reads the LAST
    # long_window closes and its sign encodes direction — a newest-first feed
    # negated it on a ~3h-stale window (long grids bought persistent declines).
    # The venue client now sorts, but this routine must stay correct for ANY
    # provider. (The variance ratio itself is reversal-invariant.)
    candles = chronological(candles) if candles else candles
    closes = _closes(candles) if candles else []
    if len(closes) < required:
        return {
            "phase": current_phase,
            "variance_ratio": 0.0,
            "direction": FLAT,
            "drift_pct": 0.0,
            "trend_by_drift": False,
            "trend_release_pct": 0.0,
            "holding_trend": False,
            "insufficient_history": True,
            "required_candles": required,
        }

    vr = variance_ratio(closes, short_window, long_window)
    if vr is None:
        return {
            "phase": current_phase,
            "variance_ratio": 0.0,
            "direction": FLAT,
            "drift_pct": 0.0,
            "trend_by_drift": False,
            "trend_release_pct": 0.0,
            "holding_trend": False,
            "insufficient_history": True,
            "required_candles": required,
        }

    # Direction over the long horizon: where has price actually gone? A single
    # endpoint-to-endpoint diff samples oscillation phase noise (a choppy decline
    # can read "up" by luck), so compare the mean of the most-recent segment to
    # the mean of the oldest segment across the long window — robust to chop.
    seg = closes[-(long_window + 1):]
    k = max(2, len(seg) // 3)
    older = sum(seg[:k]) / k
    recent = sum(seg[-k:]) / k
    drift = ((recent - older) / older) if older > 0 else 0.0
    if drift > 0:
        direction = UP
    elif drift < 0:
        direction = DOWN
    else:
        direction = FLAT

    drift_threshold = max(0.0, float(trend_drift_pct)) / 100.0
    trend_by_drift = drift_threshold > 0 and abs(drift) >= drift_threshold
    trend_by_vr = vr >= trend_on

    # RGRID-FLIPFLOP: releasing a directional phase needs the move to STALL, not
    # merely ease. See TREND_RELEASE_FRACTION.
    release = drift_threshold * TREND_RELEASE_FRACTION

    current_phase = RGRID if str(current_phase) == RGRID else GRID
    phase = current_phase
    holding_trend = False
    if trend_by_vr or trend_by_drift:
        # Trending (by burst OR by sustained drift): the trend phase is the
        # pyramiding follower (R-Grid), both directions. A reverse-grid SHORT
        # ladder used to bleed in an uptrend, so UP stayed on the long mean-
        # reversion grid; R-Grid adds WITH the move, so both sides belong here.
        if direction in (DOWN, UP):
            phase = RGRID
    elif current_phase == RGRID and release > 0 and abs(drift) >= release:
        # Trend follower armed and price is STILL moving — just not fast enough
        # to re-trip the trend threshold. Hold the follower (it owns the side
        # flip). Releasing on a mere ease used to hand a short ladder back to
        # the long grid: six flips in forty minutes, a round trip each time.
        #
        # abs(drift) so an UP trend is held the same way a DOWN trend is; a
        # genuine stall (|drift| < release) still falls through to GRID.
        phase = RGRID
        holding_trend = True
    elif vr <= range_on:
        # Ranging AND no strong drift: classic long grid.
        phase = GRID
    # else: inside the hysteresis band, no drift trend -> hold current_phase.

    return {
        "phase": phase,
        "variance_ratio": float(vr),
        "direction": direction,
        "drift_pct": float(drift * 100.0),
        "trend_by_drift": bool(trend_by_drift),
        "trend_release_pct": float(release * 100.0),
        "holding_trend": bool(holding_trend),
        "insufficient_history": False,
    }
