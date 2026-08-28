"""Regime gate — should this market-making/grid strategy be quoting AT ALL?

The primary defense from the nine-run TreadFi grid post-mortem (2026-06):
a grid market maker is short volatility — it wins in compressed, ranging
markets and loses in trends/expansions. Settings tune behavior WITHIN the
right regime; only the gate keeps the strategy out of the wrong one.

Verdict model
=============
``run`` combines two deterministic reads over recent candles:

1. **Trend** — the existing :mod:`volatility_regime` EMA-stack classifier
   (RANGING / TRENDING_UP / TRENDING_DOWN).
2. **Acceptance** — an approximate volume profile built from candle
   typical-price × volume buckets: Point of Control, the value area
   (smallest price band holding ``value_area_pct`` of traded volume), and
   whether the last close sits inside it. A tall/narrow profile with price
   inside the value area = compression (grid-friendly); a wide value area
   or price escaping it = expansion/breakout (step aside).

Verdicts:
- ``QUOTE``  — ranging + price accepted in a compressed value area.
- ``PAUSE``  — trending, or price broke out of acceptance. Reasons:
  ``trending_up`` / ``trending_down`` / ``breakout`` / ``expansion``.

Fail-open by design: with insufficient candle history the verdict is
``QUOTE`` with ``gate_active=False``. A missing candle feed must degrade to
today's (ungated) behavior — silently never quoting is the historical
"no orders" failure class, which is worse than quoting ungated.

Pause semantics live in the controllers, not here: pause blocks NEW opening
quotes only. Existing positions, close legs, stops, and the inventory cap
keep running. Pause is "stop digging", never "flatten".
"""
from __future__ import annotations

from typing import Dict, Mapping, Sequence

from src.nadobro.engine.routines import volatility_regime
from src.nadobro.engine.routines.technical_analysis import (
    chronological as technical_analysis_chronological,
)
from src.nadobro.engine.routines.volatility_regime import (
    RANGING,
    TRENDING_DOWN,
    TRENDING_UP,
)

Candle = Mapping[str, float]

QUOTE = "QUOTE"
PAUSE = "PAUSE"

# Human-readable pause reasons — the single source for user-facing copy
# (cycle-1 "no orders" notify, gate flip messages, /status "Quoting" line).
GATE_REASON_HUMAN: Dict[str, str] = {
    "trending_up": "market trending up",
    "trending_down": "market trending down",
    "breakout": "price broke out of its range",
    "expansion": "range is expanding",
    # Reverse Grid chop guard: the mirror image of the grid gate. A reverse grid
    # pyramids WITH a trend, so it stands down in chop/range (where it would
    # whipsaw) and arms once a trend confirms — the opposite resume condition to
    # the ranging grid, so REVGRID_GATE_REASONS below flips the card's wording.
    "revgrid_chop": "choppy — waiting for a trend",
}

# Reasons whose resume condition is "a trend forms" (reverse grid), NOT "the
# market ranges again" (grid/mid). The /status card and the pause notice read
# this to word the resume line correctly per strategy.
REVGRID_GATE_REASONS: frozenset = frozenset({"revgrid_chop"})


# CANDLE-ORDER guardrail (2026-07-31): shared with variance_regime — see
# technical_analysis.chronological for the rationale.
_chronological = technical_analysis_chronological

# Value-area width (as a fraction of the window's full price range) above
# which the profile reads as expansion rather than compression. A perfectly
# compressed market piles ~70% of volume into a narrow band; once that band
# needs > ~65% of the whole range to hold 70% of volume, volume is smeared —
# price is travelling, not accepting.
DEFAULT_MAX_VALUE_AREA_RANGE_FRAC = 0.65
# How far beyond the value-area edge the last close must sit (as a fraction
# of the value-area width) before we call it a breakout rather than noise.
DEFAULT_BREAKOUT_TOLERANCE_FRAC = 0.15


def volume_profile(
    candles: Sequence[Candle],
    *,
    bins: int = 24,
    value_area_pct: float = 0.70,
) -> Dict[str, float]:
    """Approximate volume profile from OHLCV candles.

    Buckets each candle's volume at its typical price ((H+L+C)/3). Returns
    POC, the value-area bounds (smallest contiguous-expansion band around the
    POC holding ``value_area_pct`` of volume), and the width ratios used by
    the gate. Empty/degenerate input returns zeros.
    """
    if not candles:
        return {"poc": 0.0, "va_low": 0.0, "va_high": 0.0, "range_low": 0.0,
                "range_high": 0.0, "va_range_frac": 1.0, "total_volume": 0.0}
    lows = [float(c.get("low", c.get("close", 0.0)) or 0.0) for c in candles]
    highs = [float(c.get("high", c.get("close", 0.0)) or 0.0) for c in candles]
    range_low, range_high = min(lows), max(highs)
    if range_high <= range_low or range_low <= 0:
        return {"poc": range_low, "va_low": range_low, "va_high": range_high,
                "range_low": range_low, "range_high": range_high,
                "va_range_frac": 1.0, "total_volume": 0.0}

    width = (range_high - range_low) / bins
    volumes = [0.0] * bins
    for c in candles:
        high = float(c.get("high", 0.0) or 0.0)
        low = float(c.get("low", 0.0) or 0.0)
        close = float(c.get("close", 0.0) or 0.0)
        vol = float(c.get("volume", 0.0) or 0.0)
        typical = (high + low + close) / 3 if (high and low) else close
        idx = min(bins - 1, max(0, int((typical - range_low) / width)))
        volumes[idx] += vol

    total = sum(volumes)
    poc_idx = max(range(bins), key=lambda i: volumes[i])
    # Grow the value area outward from the POC, always absorbing the larger
    # neighbour, until it holds value_area_pct of total volume.
    lo = hi = poc_idx
    covered = volumes[poc_idx]
    target = total * value_area_pct
    while covered < target and (lo > 0 or hi < bins - 1):
        below = volumes[lo - 1] if lo > 0 else -1.0
        above = volumes[hi + 1] if hi < bins - 1 else -1.0
        if above >= below:
            hi += 1
            covered += volumes[hi]
        else:
            lo -= 1
            covered += volumes[lo]

    va_low = range_low + lo * width
    va_high = range_low + (hi + 1) * width
    va_range_frac = (va_high - va_low) / (range_high - range_low)
    return {
        "poc": range_low + (poc_idx + 0.5) * width,
        "va_low": va_low,
        "va_high": va_high,
        "range_low": range_low,
        "range_high": range_high,
        "va_range_frac": va_range_frac,
        "total_volume": total,
    }


async def run(
    trading_pair: str,
    candles: Sequence[Candle],
    *,
    max_value_area_range_frac: float = DEFAULT_MAX_VALUE_AREA_RANGE_FRAC,
    breakout_tolerance_frac: float = DEFAULT_BREAKOUT_TOLERANCE_FRAC,
    profile_bins: int = 24,
) -> Dict[str, object]:
    """Combine trend + acceptance into one QUOTE / PAUSE verdict."""
    candles = _chronological(candles)
    vol = await volatility_regime.run(trading_pair, candles)
    base: Dict[str, object] = {
        "verdict": QUOTE,
        "reason": "",
        "gate_active": True,
        "regime": vol.get("regime"),
        "atr_pct": float(str(vol.get("atr_pct") or 0.0)),
    }
    if vol.get("insufficient_history"):
        base.update({"gate_active": False, "reason": "insufficient_history"})
        return base

    regime = vol.get("regime")
    if regime in (TRENDING_UP, TRENDING_DOWN):
        base.update({
            "verdict": PAUSE,
            "reason": "trending_up" if regime == TRENDING_UP else "trending_down",
        })
        return base

    profile = volume_profile(candles, bins=profile_bins)
    base["profile"] = profile
    va_low = float(profile["va_low"])
    va_high = float(profile["va_high"])
    va_width = max(va_high - va_low, 0.0)
    last_close = float(candles[-1].get("close", 0.0) or 0.0)

    if float(profile["va_range_frac"]) > max_value_area_range_frac:
        base.update({"verdict": PAUSE, "reason": "expansion"})
        return base

    if va_width > 0 and last_close > 0:
        tolerance = va_width * breakout_tolerance_frac
        if last_close > va_high + tolerance or last_close < va_low - tolerance:
            base.update({"verdict": PAUSE, "reason": "breakout"})
            return base

    assert regime == RANGING
    return base
