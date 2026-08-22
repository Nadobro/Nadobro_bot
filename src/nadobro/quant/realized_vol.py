"""Realized volatility from an irregular price series.

Pure math — no I/O, no config, stdlib only.

Volatility sets how wide to quote, so it needs to be measured on the same
clock the quotes react to. The existing ATR(14) on 1m candles
(``engine/routines/technical_analysis``) is a ~14-MINUTE statistic: correct for
the spread floor and the regime envelope it already drives, far too slow to
size a quote that lives seconds.

Why decay is TIME-based, not per-sample
---------------------------------------
The price series here is event-driven (Hyperliquid pushes on book and trade
events), so samples arrive irregularly — bursts during activity, gaps when
quiet. A conventional per-sample EWMA weights each observation equally, which
means a burst of ten ticks in one second counts ten times as much as ten ticks
spread over a minute. That silently reweights the estimate by *tick luck*
rather than by elapsed time, and it biases variance UP exactly during the
bursts where you most want a stable number. Decaying by ``exp(-dt/tau)``
instead makes the estimate depend on the clock, not the feed's mood.

Everything returns ``None`` rather than a fabricated number when there is not
enough history — an unknown volatility must widen quotes by policy upstream,
not be silently replaced by zero.
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _returns_with_dt(series: Sequence[Sequence[float]]) -> list:
    """``[(log_return, dt_seconds), ...]`` from ``[(ts, price), ...]``."""
    out = []
    prev_ts = prev_px = None
    for row in series or []:
        try:
            ts, px = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px <= 0:
            continue
        if prev_px is not None and prev_ts is not None:
            dt = ts - prev_ts
            if dt > 0:
                out.append((math.log(px / prev_px), dt))
        prev_ts, prev_px = ts, px
    return out


def ewma_variance(
    returns_with_dt: Sequence[Sequence[float]], *, halflife_s: float
) -> Optional[float]:
    """Time-decayed variance PER SECOND of log returns.

    Normalising each squared return by its own ``dt`` puts observations taken
    over different intervals on one scale, so the result is a variance rate
    rather than a per-observation variance that depends on sampling.
    """
    if halflife_s <= 0:
        return None
    tau = halflife_s / math.log(2.0)
    num = den = 0.0
    # Walk newest-last, decaying older observations by their age.
    rows = list(returns_with_dt)
    if not rows:
        return None
    age = 0.0
    for ret, dt in reversed(rows):
        try:
            r, d = float(ret), float(dt)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        age += d
        w = math.exp(-age / tau)
        num += w * (r * r) / d      # per-second variance contribution
        den += w
    if den <= 0:
        return None
    return num / den


def ewma_vol(
    series: Sequence[Sequence[float]], *, halflife_s: float
) -> Optional[float]:
    """Volatility per sqrt(second) from a ``[(ts, price), ...]`` series."""
    var = ewma_variance(_returns_with_dt(series), halflife_s=halflife_s)
    if var is None or var < 0:
        return None
    return math.sqrt(var)


def vol_over(series: Sequence[Sequence[float]], *, halflife_s: float,
             horizon_s: float) -> Optional[float]:
    """Expected move (as a return fraction) over ``horizon_s``.

    This is the number a quote width actually wants: sigma scaled to the
    horizon the quote is exposed for.
    """
    v = ewma_vol(series, halflife_s=halflife_s)
    if v is None or horizon_s <= 0:
        return None
    return v * math.sqrt(horizon_s)


def annualize(vol_per_sqrt_s: Optional[float]) -> Optional[float]:
    if vol_per_sqrt_s is None:
        return None
    return vol_per_sqrt_s * math.sqrt(SECONDS_PER_YEAR)


def parkinson(candles: Sequence[dict], *, period: int = 14) -> Optional[float]:
    """High/low range estimator over the last ``period`` candles.

    Uses roughly 5x less data than a close-to-close estimator for the same
    precision, because the bar's range carries information its close discards —
    useful as the slow, stable cross-check on the fast tick estimate.
    Returns volatility PER BAR.
    """
    rows = [c for c in (candles or [])][-max(1, int(period)):]
    if len(rows) < 2:
        return None
    acc, n = 0.0, 0
    for c in rows:
        hi, lo = _num(c.get("high")), _num(c.get("low"))
        if hi is None or lo is None or hi <= 0 or lo <= 0 or hi < lo:
            continue
        acc += math.log(hi / lo) ** 2
        n += 1
    if n == 0:
        return None
    return math.sqrt(acc / (4.0 * math.log(2.0) * n))


def vol_of_vol(values: Sequence[float]) -> Optional[float]:
    """Dispersion of a volatility series — high values mean the vol estimate
    itself is unstable, which is a reason to widen beyond what the level alone
    suggests."""
    vals = [x for x in (_num(v) for v in (values or [])) if x is not None]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var)
