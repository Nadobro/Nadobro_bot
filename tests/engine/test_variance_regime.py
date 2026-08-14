"""Variance-ratio regime routine — the tunable brain behind dgrid switching.

Verifies:
1. A trending market reads VR > 1 and picks the trend-follower phase
   (rgrid) in BOTH directions. Range picks the long ladder (grid).
2. A mean-reverting range reads VR < 1 and picks grid.
3. The trend_on / range_on hysteresis band holds the current phase.
4. Insufficient history holds the current phase (never flips on noise).
5. User windows feed the computation (short/long horizons).
"""
from __future__ import annotations

import asyncio
import math

from src.nadobro.engine.routines import variance_regime as vr

PAIR = "BTC-PERP"


def ranging_candles(n: int = 60, base: float = 100.0, amp: float = 0.12, period: float = 7.0) -> list[dict]:
    """Smooth oscillation — mean-reverting, grid-friendly (bounded long-horizon
    displacement -> VR < 1). Amplitude stays under the 0.30% drift trigger so a
    sine's last-window slope cannot masquerade as a trend (amp=1% used to).
    Period is coprime-ish to the 4/12 windows so short returns never collapse
    to exact zeros."""
    return [{"close": base + amp * math.sin(2 * math.pi * i / period)} for i in range(n)]


def trending_candles(n: int = 60, base: float = 100.0, step: float = 0.4) -> list[dict]:
    """Monotonic drift — trending, grid-hostile (VR > 1)."""
    out = []
    price = base
    for _ in range(n):
        price += step
        out.append({"close": price})
    return out


def test_downtrend_reads_high_vr_and_picks_rgrid():
    result = asyncio.run(vr.run(PAIR, trending_candles(step=-0.4)))
    assert result["insufficient_history"] is False
    assert float(result["variance_ratio"]) >= 1.25
    assert result["direction"] == vr.DOWN
    assert result["phase"] == vr.RGRID


def test_uptrend_reads_high_vr_and_picks_the_trend_follower():
    result = asyncio.run(vr.run(PAIR, trending_candles(step=0.4)))
    assert float(result["variance_ratio"]) >= 1.25
    assert result["direction"] == vr.UP
    # R-Grid adds WITH the move, so an uptrend is the trend-follower phase,
    # not the ranging long ladder (a short reverse-grid ladder used to bleed
    # here, which is why UP used to stay GRID).
    assert result["phase"] == vr.RGRID


def test_range_reads_low_vr_and_picks_grid():
    result = asyncio.run(vr.run(PAIR, ranging_candles(), current_phase=vr.RGRID))
    assert float(result["variance_ratio"]) < 1.15
    assert result["phase"] == vr.GRID


def test_hysteresis_band_holds_current_phase():
    # Force a ratio between range_on and trend_on by widening the band so any
    # finite VR lands inside it: the routine must hold whatever phase it had.
    # Drift filter disabled (trend_drift_pct=0) to isolate the VR-band path —
    # otherwise the monotonic decline correctly flips short on the drift signal.
    candles = trending_candles(step=-0.4)
    held_rgrid = asyncio.run(
        vr.run(PAIR, candles, trend_on=99.0, range_on=0.0,
               trend_drift_pct=0.0, current_phase=vr.RGRID)
    )
    held_grid = asyncio.run(
        vr.run(PAIR, candles, trend_on=99.0, range_on=0.0,
               trend_drift_pct=0.0, current_phase=vr.GRID)
    )
    assert held_rgrid["phase"] == vr.RGRID
    assert held_grid["phase"] == vr.GRID


def test_insufficient_history_holds_current_phase():
    result = asyncio.run(
        vr.run(PAIR, trending_candles(n=5, step=-0.4), current_phase=vr.RGRID)
    )
    assert result["insufficient_history"] is True
    assert result["phase"] == vr.RGRID
    assert float(result["variance_ratio"]) == 0.0


def test_user_windows_change_required_history():
    # Long window 30 needs > 32 candles; 20 candles is insufficient.
    short = asyncio.run(
        vr.run(PAIR, trending_candles(n=20, step=-0.4), short_window=4, long_window=30)
    )
    assert short["insufficient_history"] is True
    enough = asyncio.run(
        vr.run(PAIR, trending_candles(n=40, step=-0.4), short_window=4, long_window=30)
    )
    assert enough["insufficient_history"] is False


def _ranging_then_decline(n=200, base=63300.0):
    """Long ranging history then a recent grind-down in the last ~15 bars — the
    real-market case where VR (computed over the WHOLE window) stays < the trend
    cutoff (~0.88 in the logs) yet price is clearly trending down right now."""
    out = []
    for i in range(n):
        c = base + 40.0 * math.sin(2 * math.pi * i / 9)
        if i >= n - 15:
            c -= 30.0 * (i - (n - 15))
        out.append({"close": c})
    return out


def test_drift_filter_flips_short_on_recent_grind_even_when_vr_low():
    # The dgrid-blind bug: a long ranging history keeps VR < trend_on, so the
    # VR-only classifier called it "ranging" and the long grid kept buying into
    # the decline. The directional-drift filter must flip it to rgrid (short).
    candles = _ranging_then_decline()
    r = asyncio.run(vr.run(PAIR, candles, trend_drift_pct=0.30))
    assert float(r["variance_ratio"]) < 1.25      # VR alone says "ranging"
    assert r["direction"] == vr.DOWN
    assert r["trend_by_drift"] is True
    assert r["phase"] == vr.RGRID                 # flips short on the drift


def test_drift_filter_off_reproduces_blind_grid_bug():
    # With the filter disabled, the same recent decline is mis-classified as a
    # range and stays a long GRID — the original blind behavior.
    candles = _ranging_then_decline()
    r = asyncio.run(vr.run(PAIR, candles, trend_drift_pct=0.0))
    assert float(r["variance_ratio"]) < 1.25
    assert r["phase"] == vr.GRID


def test_flat_range_does_not_false_trigger_drift_trend():
    # A pure oscillating range (no net drift) must NOT trip the drift filter.
    candles = [{"close": 63300.0 + 60.0 * math.sin(2 * math.pi * i / 7)} for i in range(120)]
    r = asyncio.run(vr.run(PAIR, candles, trend_drift_pct=0.30))
    assert r["trend_by_drift"] is False
    assert r["phase"] == vr.GRID


# ── RGRID-FLIPFLOP ──────────────────────────────────────────────
# Reported 2026-07-28 with a Telegram screenshot of six consecutive flip
# notifications on BTC (mainnet), forty minutes apart end to end:
#
#   9:15  GRID  -> RGRID   "downtrend detected (variance ratio 0.91)"
#   9:17  RGRID -> GRID    "downtrend detected (variance ratio 0.89)"
#   9:27  GRID  -> RGRID   "downtrend detected (variance ratio 0.84)"
#   9:29  RGRID -> GRID    "downtrend detected (variance ratio 0.83)"
#   9:54  GRID  -> RGRID   "downtrend detected (variance ratio 0.91)"
#   9:55  RGRID -> GRID    "downtrend detected (variance ratio 0.85)"
#
# EVERY message reads "downtrend detected" — the direction was right every time
# — yet three of them armed the LONG grid. Every variance ratio is below 1.0,
# under rgrid's range_on of 1.05, so the only path that can return RGRID is the
# drift branch and the only path that can return GRID is `elif vr <= range_on`,
# which ignored direction entirely. So a decline that merely EASED handed the
# short ladder back to the long grid. Each flip flattens the position
# reduce-only ("Previous position closed"), so the bot paid a round trip to land
# on the wrong side of the move it had just named, over and over.

# rgrid's live defaults (engine_runtime.map_strategy_config).
_RG = dict(short_window=3, long_window=8, trend_on=1.10, range_on=1.05,
           trend_drift_pct=0.15)


def _choppy(start: float, drop_per_candle: float, amp: float = 120.0,
            n: int = 24, period: int = 6):
    """Deterministic choppy trend: a linear drift plus a sawtooth swing.

    The swing dominates the SHORT horizon and averages out over the LONG one, so
    the variance ratio sits BELOW 1 exactly as production logged (0.83-0.97)
    while price still goes decisively one way.
    """
    out = []
    for i in range(n):
        p = (i % period) / period
        out.append({"close": start - drop_per_candle * i
                    + amp * (1.0 - 2.0 * abs(2.0 * p - 1.0))})
    return out


def test_easing_decline_keeps_the_short_ladder():
    """THE BUG: -0.13%/window is still falling. It must not re-arm long."""
    r = asyncio.run(vr.run(PAIR, _choppy(63500, 14.0), current_phase=vr.RGRID, **_RG))
    assert r["direction"] == vr.DOWN
    assert float(r["variance_ratio"]) <= _RG["range_on"], "must be in the range branch"
    assert r["trend_by_drift"] is False, "drift is UNDER the trend threshold"
    assert r["holding_trend"] is True
    assert r["phase"] == vr.RGRID, (
        "a decline that merely eased handed the short ladder back to the long "
        "grid — the reported GRID<->RGRID flip-flop"
    )


def test_a_downtrend_still_arms_the_short():
    r = asyncio.run(vr.run(PAIR, _choppy(63500, 16.0), current_phase=vr.GRID, **_RG))
    assert r["direction"] == vr.DOWN and r["trend_by_drift"] is True
    assert r["phase"] == vr.RGRID


def test_the_short_is_released_once_the_decline_stalls():
    """Hysteresis must not become a one-way trap."""
    r = asyncio.run(vr.run(PAIR, _choppy(63500, 1.0), current_phase=vr.RGRID, **_RG))
    assert abs(float(r["drift_pct"])) < float(r["trend_release_pct"])
    assert r["holding_trend"] is False
    assert r["phase"] == vr.GRID


def test_a_turn_upward_keeps_the_trend_follower():
    """A V-reversal is still a trend. R-Grid owns the side flip; dropping
    back to the ranging ladder would flatten a short into a long mean-reversion
    book in the middle of the turn.
    """
    r = asyncio.run(vr.run(PAIR, _choppy(63500, -14.0), current_phase=vr.RGRID, **_RG))
    assert r["direction"] == vr.UP
    assert r["phase"] == vr.RGRID


def test_the_release_never_holds_a_phase_we_are_not_in():
    """A long GRID must never be dragged short by the release branch."""
    r = asyncio.run(vr.run(PAIR, _choppy(63500, 14.0), current_phase=vr.GRID, **_RG))
    assert r["holding_trend"] is False
    assert r["phase"] == vr.GRID


def test_no_flipflop_across_a_realistic_downtrend():
    """The reported sequence: 6 flips before, 1 after.

    Rates of decline wobbling either side of the 0.15% trend threshold while
    price falls throughout, then a genuine stall.
    """
    rates = [16.0, 14.0, 18.0, 14.0, 17.0, 13.5, 16.5, 14.0]
    phase, flips = vr.GRID, 0
    for rate in rates:
        r = asyncio.run(vr.run(PAIR, _choppy(63500, rate), current_phase=phase, **_RG))
        if r["phase"] != phase:
            flips += 1
        phase = str(r["phase"])
    assert phase == vr.RGRID, "must end the downtrend still short"
    assert flips == 1, f"expected one flip into the trend, churned {flips}"


def test_drift_release_is_disabled_when_the_drift_filter_is_off():
    """trend_drift_pct=0 opts out of the drift trend — and of its release."""
    r = asyncio.run(vr.run(PAIR, _choppy(63500, 14.0), current_phase=vr.RGRID,
                           **dict(_RG, trend_drift_pct=0.0)))
    assert float(r["trend_release_pct"]) == 0.0
    assert r["holding_trend"] is False
    assert r["phase"] == vr.GRID
