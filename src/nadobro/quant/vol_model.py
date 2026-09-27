"""Realized-volatility model for the grid family (Grid, D-Grid, R-Grid).

Pure math — no I/O, no config, no env. Stdlib floats only (the style of
``quant/realized_vol.py``). Every behaviour that consumes this module is OPT-IN
(default OFF); see ``docs/grid_vol_model.md``.

The statistic is EXACTLY the one the grid-family evidence measured:

    rv60 = sqrt(mean(diff(log(close))**2)) * 1e4

over the last 60 CLOSED 1-minute bars, in bp per minute, not de-meaned
(harness ``dgrid_evolved.py``). It predicts the next hour's high-low range
(Spearman 0.60 on BTC) where the variance ratio does not (0.12). This module
deliberately does NOT reuse ``realized_vol.ewma_vol``: that is a time-decayed
EWMA over an irregular tick series — a different statistic.

Per-product self-calibration
----------------------------
An absolute threshold (the BTC gate of 3.05 bp/min) is market-specific: a
product twice as volatile would never read calm. Every threshold here is a
MULTIPLE of the product's own trailing 7-day median rv60 (``VolBaseline``),
which must cover at least 72h before any verdict other than WARMING is given.
Multiplying every log return by a constant scales rv60, the baseline and every
threshold together, so the verdicts are scale-invariant (pinned by a test);
only the spacing FLOORS are absolute, because fees are absolute.

Fail-safe direction
-------------------
Everything returns ``None`` / ``UNKNOWN`` / ``WARMING`` rather than a fabricated
number when the input cannot support a reading. An empty candle list is "no
information", never "zero volatility" — a consumer that stood down on UNKNOWN
must never be told CALM by a budget-denied read.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from src.nadobro.quant.mm_profile import reservation_offset_bp
from src.nadobro.quant.vol_fee_estimator import MIXED_ROUND_TRIP_RATE

# -- model constants (evidence-derived; see docs/grid_vol_model.md) ----------
RV_WINDOW = 60                      # closed 1m bars (the evidence statistic)
CALM_DWELL_MIN = 15                 # V2 gate_min_off_s = 900
MAX_DATA_AGE_S = 180                # newest CLOSED bar older than this -> UNKNOWN
MAX_GAP_RUN_MIN = 30                # >30 consecutive no-bar minutes -> closed / dead tape
MIN_BAR_COVERAGE = 0.5              # <50% of the rv window backed by real bars -> UNKNOWN
BASELINE_LOOKBACK_H = 168           # 7 days
BASELINE_MIN_COVERAGE_H = 72
DEFAULT_GATE_MULT = 0.82            # 3.05 / 3.70 (BTC TUNE p33 / p50)
DEFAULT_SPACING_K = 2.6             # 2.6 x 3.05 = 7.9 ~ the V2 lattice's 8bp
FA_SPACING_FLOOR_BP = 6.0           # 4.0 maker round trip + 2bp buffer (fill-anchored)
CLASSIC_SPACING_FLOOR_BP = float(MIXED_ROUND_TRIP_RATE) * 1e4   # 6.8
DEFAULT_COMPRESS_MULT = 0.91        # 3.358 / 3.70 (BTC TUNE quintile edge 2)
DEFAULT_EXPAND_MULT = 1.42          # 5.252 / 3.70 (quintile edge 4)
ARM_LOOKBACK_MIN = 120
ARM_HOLD_MIN = 5
ARM_RV_WINDOW = 15
SKEW_GAMMA = 0.5
SKEW_HORIZON_MIN = 15
SKEW_MAX_FRAC = 0.5

# Verdict states.
CALM = "CALM"
HOT = "HOT"
UNKNOWN = "UNKNOWN"
WARMING = "WARMING"
ARMED = "ARMED"
WAITING = "WAITING"

# Gate reasons (mirrored as literals in engine/routines/regime_gate.VOL_GATE_REASONS).
REASON_HOT = "vol_hot"
REASON_UNKNOWN = "vol_unknown"
REASON_WARMING = "vol_warming"
REASON_RGRID_WAIT = "rgrid_vol_wait"


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


# -- data containers ---------------------------------------------------------
@dataclass(frozen=True)
class MinuteSeries:
    """Closed 1-minute closes on a gap-free minute grid.

    ``start_ts`` is the OPEN time (unix seconds, minute-aligned) of the first
    bar; bar ``i`` opened at ``start_ts + 60*i`` and closed 60s later. Missing
    minutes are forward-filled (a zero return) and flagged ``real[i] = False``.
    """

    start_ts: int
    closes: Tuple[float, ...]
    real: Tuple[bool, ...]

    @property
    def newest_ts(self) -> int:
        """Open time of the newest closed bar."""
        return self.start_ts + 60 * (len(self.closes) - 1)

    @property
    def newest_close_ts(self) -> int:
        """When the newest closed bar CLOSED."""
        return self.newest_ts + 60

    def __len__(self) -> int:
        return len(self.closes)


@dataclass(frozen=True)
class VolBaseline:
    """Trailing median rv60 of one product. ``median_rv60_bp`` is None while
    the included history covers fewer than the required hours (WARMING)."""

    median_rv60_bp: Optional[float]
    coverage_h: float
    n_minutes: int
    newest_ts: int

    @property
    def ready(self) -> bool:
        return self.median_rv60_bp is not None and self.median_rv60_bp > 0


@dataclass(frozen=True)
class GateVerdict:
    state: str
    reason: str
    rv60_bp: Optional[float] = None
    gate_bp: Optional[float] = None
    base_bp: Optional[float] = None
    base_hours: float = 0.0
    calm_streak_min: int = 0
    resume_in_min: int = CALM_DWELL_MIN
    data_age_s: Optional[float] = None
    detail: str = ""

    @property
    def calm(self) -> bool:
        return self.state == CALM


@dataclass(frozen=True)
class ArmVerdict:
    state: str
    reason: str
    rv60_bp: Optional[float] = None
    rv15_bp: Optional[float] = None
    compress_bp: Optional[float] = None
    expand_bp: Optional[float] = None
    compressed_ago_min: Optional[int] = None
    data_age_s: Optional[float] = None
    base_hours: float = 0.0
    detail: str = ""

    @property
    def armed(self) -> bool:
        return self.state == ARMED


# -- series construction -----------------------------------------------------
def _bar_time_s(raw: Any) -> Optional[int]:
    t = _num(raw)
    if t is None or t <= 0:
        return None
    if t > 1e12:            # milliseconds
        t = t / 1000.0
    return int(t)


def closed_minute_series(candles: Optional[Sequence[Any]], *, now_s: float) -> Optional[MinuteSeries]:
    """Normalise raw candle rows (``{"time", "close", ...}``) into closed bars.

    * ``time`` is coerced to int seconds (a value > 1e12 is milliseconds);
    * rows are SORTED ascending — feed order is never trusted (CANDLE-ORDER);
    * the in-progress bar (``time + 60 > now_s``) is dropped — closed bars only;
    * the result is reindexed onto a gap-free minute grid, forward-filling
      missing minutes (flagged not-real);
    * duplicate minutes keep the last row seen.

    Returns ``None`` on empty / unusable input: that is "no information",
    NEVER "zero volatility".
    """
    if not candles:
        return None
    by_minute: dict[int, float] = {}
    for row in candles:
        if isinstance(row, Mapping):
            t_raw, c_raw = row.get("time"), row.get("close")
        else:
            t_raw, c_raw = getattr(row, "time", None), getattr(row, "close", None)
        t = _bar_time_s(t_raw)
        c = _num(c_raw)
        if t is None or c is None or c <= 0:
            continue
        if t + 60 > now_s:
            continue            # the in-progress bar
        by_minute[t // 60] = c
    if not by_minute:
        return None
    minutes = sorted(by_minute)
    first, last = minutes[0], minutes[-1]
    closes: List[float] = []
    real: List[bool] = []
    prev = by_minute[first]
    for m in range(first, last + 1):
        if m in by_minute:
            prev = by_minute[m]
            closes.append(prev)
            real.append(True)
        else:
            closes.append(prev)
            real.append(False)
    return MinuteSeries(start_ts=first * 60, closes=tuple(closes), real=tuple(real))


def merge_series(older: Optional[MinuteSeries], newer: Optional[MinuteSeries],
                 *, max_minutes: Optional[int] = None) -> Optional[MinuteSeries]:
    """Union of two minute series. Overlapping minutes take the NEWER series'
    real bars (a real bar always beats a forward-fill). Minutes between the two
    that neither covers are forward-filled and flagged not-real, so a long
    feature-off gap is carried honestly as a gap (the baseline excludes it).
    ``max_minutes`` keeps only the newest N minutes."""
    if older is None or not len(older):
        out = newer
    elif newer is None or not len(newer):
        out = older
    else:
        bars: dict[int, Tuple[float, bool]] = {}
        for s in (older, newer):
            base = s.start_ts // 60
            for i, (c, r) in enumerate(zip(s.closes, s.real)):
                m = base + i
                cur = bars.get(m)
                if cur is None or r or not cur[1]:
                    bars[m] = (c, r)
        ms = sorted(bars)
        closes: List[float] = []
        real: List[bool] = []
        prev = bars[ms[0]][0]
        for m in range(ms[0], ms[-1] + 1):
            if m in bars:
                prev, r = bars[m]
                closes.append(prev)
                real.append(r)
            else:
                closes.append(prev)
                real.append(False)
        out = MinuteSeries(start_ts=ms[0] * 60, closes=tuple(closes), real=tuple(real))
    if out is not None and max_minutes is not None and len(out) > max_minutes > 0:
        cut = len(out) - max_minutes
        out = MinuteSeries(start_ts=out.start_ts + 60 * cut,
                           closes=out.closes[cut:], real=out.real[cut:])
    return out


# -- the statistic -----------------------------------------------------------
def realized_vol_bp(closes: Sequence[float], window: int = RV_WINDOW) -> Optional[float]:
    """``sqrt(mean(diff(log(c))**2)) * 1e4`` over the last ``window + 1`` closes.

    Byte-for-byte the harness formula. ``None`` with fewer than ``window + 1``
    closes or any non-positive close."""
    window = int(window)
    if window < 1 or len(closes) < window + 1:
        return None
    tail = closes[-(window + 1):]
    if any((c is None) or not (c > 0) for c in tail):
        return None
    sq = [(math.log(tail[i + 1]) - math.log(tail[i])) ** 2 for i in range(window)]
    return math.sqrt(math.fsum(sq) / window) * 1e4


def rv_series_bp(closes: Sequence[float], window: int = RV_WINDOW) -> List[Optional[float]]:
    """Rolling ``realized_vol_bp`` per minute in O(n): element ``i`` is the rv
    of ``closes[i-window .. i]`` (None for ``i < window`` or a bad close)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    window = int(window)
    if window < 1 or n < window + 1:
        return out
    sq: List[Optional[float]] = [None] * n
    for i in range(1, n):
        a, b = closes[i - 1], closes[i]
        sq[i] = (math.log(b) - math.log(a)) ** 2 if (a and b and a > 0 and b > 0) else None
    run = 0.0
    bad = 0
    for i in range(1, n):
        v = sq[i]
        if v is None:
            bad += 1
        else:
            run += v
        j = i - window
        if j >= 1:
            w = sq[j]
            if w is None:
                bad -= 1
            else:
                run -= w
        if i >= window and bad == 0:
            out[i] = math.sqrt(max(run, 0.0) / window) * 1e4
    return out


def blend_rv_bp(parts: Mapping[str, Tuple[Optional[float], float]]) -> Optional[float]:
    """Variance blend ``sqrt(sum(w*rv^2) / sum(w))`` over parts with an rv and
    a positive weight. Shipped for the harness test; not wired (weights 0)."""
    num = 0.0
    den = 0.0
    for rv, w in parts.values():
        r = _num(rv)
        ww = _num(w)
        if r is None or ww is None or ww <= 0 or r < 0:
            continue
        num += ww * r * r
        den += ww
    if den <= 0:
        return None
    return math.sqrt(num / den)


# -- baseline ----------------------------------------------------------------
def _long_gap_mask(real: Sequence[bool], cap_min: int) -> List[bool]:
    """True for minutes inside a run of MORE than ``cap_min`` consecutive
    no-bar (forward-filled) minutes."""
    n = len(real)
    mask = [False] * n
    i = 0
    while i < n:
        if real[i]:
            i += 1
            continue
        j = i
        while j < n and not real[j]:
            j += 1
        if j - i > cap_min:
            for k in range(i, j):
                mask[k] = True
        i = j
    return mask


def baseline_from_series(
    series: Optional[MinuteSeries],
    *,
    lookback_h: float = BASELINE_LOOKBACK_H,
    min_coverage_h: float = BASELINE_MIN_COVERAGE_H,
    gap_run_cap_min: int = MAX_GAP_RUN_MIN,
    window: int = RV_WINDOW,
) -> Optional[VolBaseline]:
    """Median of per-minute rv60 over the trailing ``lookback_h`` hours.

    Minutes whose rv window touches a run of more than ``gap_run_cap_min``
    forward-filled minutes are EXCLUDED: that is a closed market (equity / RWA
    perps) or dead tape, and its zero returns would drag the median down and
    make the product look permanently "hot" whenever it trades. Coverage is
    counted honestly from the included minutes; below ``min_coverage_h`` the
    median is None (WARMING). None only for a missing series."""
    if series is None or not len(series):
        return None
    lookback_min = int(max(1.0, float(lookback_h)) * 60)
    closes = series.closes
    real = series.real
    # One rv window of extra history so the oldest minute in the lookback has a value.
    keep = lookback_min + window
    if len(closes) > keep:
        closes = closes[-keep:]
        real = real[-keep:]
    rvs = rv_series_bp(closes, window)
    gap = _long_gap_mask(real, int(gap_run_cap_min))
    # prefix count of gap minutes -> O(1) "does window [i-window, i] touch a gap".
    pref = [0] * (len(gap) + 1)
    for i, g in enumerate(gap):
        pref[i + 1] = pref[i] + (1 if g else 0)
    start = max(0, len(closes) - lookback_min)
    vals: List[float] = []
    for i in range(start, len(closes)):
        v = rvs[i]
        if v is None:
            continue
        lo = max(0, i - window)
        if pref[i + 1] - pref[lo] > 0:
            continue
        vals.append(v)
    coverage_h = len(vals) / 60.0
    median = statistics.median(vals) if (vals and coverage_h >= float(min_coverage_h)) else None
    if median is not None and median <= 0:
        median = None
    return VolBaseline(
        median_rv60_bp=median, coverage_h=coverage_h, n_minutes=len(vals),
        newest_ts=series.newest_ts,
    )


# -- verdicts ----------------------------------------------------------------
def _data_age_s(series: MinuteSeries, now_s: float) -> float:
    return max(0.0, float(now_s) - float(series.newest_close_ts))


def _rv_at(closes: Sequence[float], idx: int, window: int) -> Optional[float]:
    """rv of the ``window`` returns ending at bar ``idx`` (inclusive)."""
    if idx < window or idx >= len(closes):
        return None
    return realized_vol_bp(closes[idx - window: idx + 1], window)


def _precheck(series: Optional[MinuteSeries], now_s: float, max_age_s: float,
              min_bar_coverage: float, window: int) -> Tuple[Optional[str], str, Optional[float]]:
    """Shared UNKNOWN checks. Returns (state_or_None, detail, data_age)."""
    if series is None or not len(series):
        return UNKNOWN, "no candles", None
    age = _data_age_s(series, now_s)
    if age > float(max_age_s):
        return UNKNOWN, f"candles {int(age)}s old", age
    if len(series) < window + 1:
        return UNKNOWN, "not enough candle history", age
    recent = series.real[-window:]
    cov = sum(1 for r in recent if r) / float(len(recent)) if recent else 0.0
    if cov < float(min_bar_coverage):
        return UNKNOWN, "thin / closed market", age
    return None, "", age


def gate_verdict(
    series: Optional[MinuteSeries],
    baseline: Optional[VolBaseline],
    *,
    mult: float = DEFAULT_GATE_MULT,
    dwell_min: int = CALM_DWELL_MIN,
    now_s: float,
    max_age_s: float = MAX_DATA_AGE_S,
    min_bar_coverage: float = MIN_BAR_COVERAGE,
    window: int = RV_WINDOW,
) -> GateVerdict:
    """Stateless CALM / HOT / UNKNOWN / WARMING verdict.

    CALM iff rv60 at EACH of the last ``dwell_min`` closed minutes is at or
    under ``mult x baseline median``. That is exactly the V2 harness rule
    (``gated_until = t + 900`` after any hot reading, recomputed once per
    closed minute): PAUSE is immediate, resume needs 15 calm minutes. Because
    it is computed from candle history, not a tick counter, it survives
    restarts / rebuilds and cannot be advanced by fast ticks."""
    dwell = max(1, int(dwell_min))
    bad, detail, age = _precheck(series, now_s, max_age_s, min_bar_coverage, window)
    base_hours = float(baseline.coverage_h) if baseline is not None else 0.0
    if bad is not None or series is None:
        return GateVerdict(state=UNKNOWN, reason=REASON_UNKNOWN, data_age_s=age,
                           base_hours=base_hours, detail=detail, resume_in_min=dwell)
    closes = series.closes
    n = len(closes)
    rv_now = _rv_at(closes, n - 1, window)
    if baseline is None or not baseline.ready:
        return GateVerdict(
            state=WARMING, reason=REASON_WARMING, rv60_bp=rv_now, data_age_s=age,
            base_hours=base_hours, resume_in_min=dwell,
            detail=f"{int(base_hours)}h of {int(BASELINE_MIN_COVERAGE_H)}h",
        )
    base = float(baseline.median_rv60_bp or 0.0)
    gate_bp = float(mult) * base
    if n < window + dwell:
        return GateVerdict(state=UNKNOWN, reason=REASON_UNKNOWN, rv60_bp=rv_now,
                           gate_bp=gate_bp, base_bp=base, base_hours=base_hours,
                           data_age_s=age, detail="not enough candle history",
                           resume_in_min=dwell)
    streak = 0
    for j in range(dwell):
        v = _rv_at(closes, n - 1 - j, window)
        if v is None or v > gate_bp:
            break
        streak += 1
    calm = streak >= dwell
    return GateVerdict(
        state=CALM if calm else HOT,
        reason="" if calm else REASON_HOT,
        rv60_bp=rv_now, gate_bp=gate_bp, base_bp=base, base_hours=base_hours,
        calm_streak_min=streak, resume_in_min=max(0, dwell - streak),
        data_age_s=age,
    )


def arm_verdict(
    series: Optional[MinuteSeries],
    baseline: Optional[VolBaseline],
    *,
    compress_mult: float = DEFAULT_COMPRESS_MULT,
    expand_mult: float = DEFAULT_EXPAND_MULT,
    lookback_min: int = ARM_LOOKBACK_MIN,
    hold_min: int = ARM_HOLD_MIN,
    now_s: float,
    max_age_s: float = MAX_DATA_AGE_S,
    min_bar_coverage: float = MIN_BAR_COVERAGE,
    window: int = RV_WINDOW,
    burst_window: int = ARM_RV_WINDOW,
) -> ArmVerdict:
    """R-Grid arm filter: ARMED only on a volatility EXPANSION after COMPRESSION.

    * ``expanded(t)``  ⇔ rv15(t) ≥ expand_mult × base
    * ``compressed_before(t)`` ⇔ some u in [t − lookback, t) has rv60(u) ≤ compress_mult × base
    * ARMED ⇔ some t in the last ``hold_min`` closed minutes has both.

    Stateless. UNKNOWN / WARMING exactly as for the gate — the safe direction
    for a taker trend follower is "do not arm". UNVALIDATED thresholds: a stand-
    down filter only; it cannot make R-Grid profitable."""
    bad, detail, age = _precheck(series, now_s, max_age_s, min_bar_coverage, window)
    base_hours = float(baseline.coverage_h) if baseline is not None else 0.0
    if bad is not None or series is None:
        return ArmVerdict(state=UNKNOWN, reason=REASON_UNKNOWN, data_age_s=age,
                          base_hours=base_hours, detail=detail)
    closes = series.closes
    n = len(closes)
    rv60_now = _rv_at(closes, n - 1, window)
    rv15_now = _rv_at(closes, n - 1, burst_window)
    if baseline is None or not baseline.ready:
        return ArmVerdict(state=WARMING, reason=REASON_WARMING, rv60_bp=rv60_now,
                          rv15_bp=rv15_now, data_age_s=age, base_hours=base_hours,
                          detail=f"{int(base_hours)}h of {int(BASELINE_MIN_COVERAGE_H)}h")
    base = float(baseline.median_rv60_bp or 0.0)
    compress_bp = float(compress_mult) * base
    expand_bp = float(expand_mult) * base
    hold = max(1, int(hold_min))
    look = max(1, int(lookback_min))
    first = max(0, n - hold - look)
    compressed = [False] * n
    last_comp: Optional[int] = None
    for u in range(first, n):
        v = _rv_at(closes, u, window)
        if v is not None and v <= compress_bp:
            compressed[u] = True
            last_comp = u
    # prefix count over the compressed flags
    pref = [0] * (n + 1)
    for i in range(n):
        pref[i + 1] = pref[i] + (1 if compressed[i] else 0)
    armed = False
    for t in range(max(0, n - hold), n):
        r15 = _rv_at(closes, t, burst_window)
        if r15 is None or r15 < expand_bp:
            continue
        lo = max(0, t - look)
        if pref[t] - pref[lo] > 0:
            armed = True
            break
    ago = (n - 1 - last_comp) if last_comp is not None else None
    if armed:
        return ArmVerdict(state=ARMED, reason="", rv60_bp=rv60_now, rv15_bp=rv15_now,
                          compress_bp=compress_bp, expand_bp=expand_bp,
                          compressed_ago_min=ago, data_age_s=age, base_hours=base_hours)
    # Which half is missing (for the card): with a quiet spell in the lookback
    # the ladder is waiting for the burst; otherwise it needs the quiet spell.
    comp_recent = pref[n] - pref[max(0, n - look)] > 0
    detail = "needs_expansion" if comp_recent else "needs_compression"
    return ArmVerdict(state=WAITING, reason=REASON_RGRID_WAIT, rv60_bp=rv60_now,
                      rv15_bp=rv15_now, compress_bp=compress_bp, expand_bp=expand_bp,
                      compressed_ago_min=ago, data_age_s=age, base_hours=base_hours,
                      detail=detail)


# -- actuators ---------------------------------------------------------------
def spacing_bp(
    rv60: Optional[float],
    *,
    k: float = DEFAULT_SPACING_K,
    floor_bp: float = CLASSIC_SPACING_FLOOR_BP,
    min_bp: float = 0.0,
    max_bp: float = 0.0,
    tick_bp: float = 0.0,
) -> Optional[float]:
    """Level spacing ``clip(k * rv60, lo, hi)`` in bp.

    ``lo = max(floor_bp, min_bp, 2 * tick_bp)`` — never below the fee floor
    (fees are absolute, so the floor does not scale with the product);
    ``hi = max(lo, max_bp)`` (``max_bp <= 0`` means no cap). None when rv60 is
    unknown — the caller keeps its previous spacing."""
    rv = _num(rv60)
    if rv is None or rv < 0:
        return None
    lo = max(_num(floor_bp) or 0.0, _num(min_bp) or 0.0, 2.0 * (_num(tick_bp) or 0.0))
    mx = _num(max_bp) or 0.0
    hi = max(lo, mx) if mx > 0 else float("inf")
    raw = (_num(k) or 0.0) * rv
    return max(lo, min(raw, hi))


def skew_offset_bp(
    inv_ratio: Any,
    rv60: Optional[float],
    spacing: Optional[float],
    *,
    gamma: float = SKEW_GAMMA,
    horizon_min: float = SKEW_HORIZON_MIN,
    max_frac: float = SKEW_MAX_FRAC,
) -> float:
    """Avellaneda–Stoikov-style reservation offset (bp) driven by the vol estimate.

    Delegates to ``mm_profile.reservation_offset_bp`` with
    ``sigma = rv60 * sqrt(horizon)`` and ``half_spread = spacing``: long
    inventory gives a NEGATIVE offset (quotes shift down), magnitude at most
    ``max_frac x spacing`` so it can never cross sides or breach the fee floor.
    0 when rv60 is unknown (no skew while blind; the hard cap still binds)."""
    rv = _num(rv60)
    sp = _num(spacing)
    if rv is None or rv <= 0 or sp is None or sp <= 0:
        return 0.0
    sigma = rv * math.sqrt(max(0.0, float(horizon_min)))
    return float(reservation_offset_bp(
        inv_ratio, sigma_bp=sigma, half_spread_bp=sp, gamma=gamma,
        max_frac_of_half_spread=max_frac,
    ))


def spacing_change_significant(current_bp: Optional[float], new_bp: Optional[float]) -> bool:
    """Deadband for re-spacing: only a change of at least max(1bp, 15%) of the
    current spacing counts, so per-minute rv noise never churns the ladder."""
    if new_bp is None:
        return False
    if current_bp is None or current_bp <= 0:
        return True
    return abs(float(new_bp) - float(current_bp)) >= max(1.0, 0.15 * float(current_bp))
