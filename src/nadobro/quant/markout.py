"""Post-fill mark-out — did the market move against us right after we filled?

Pure math — no I/O, no config, stdlib only.

This is the single measurement a market maker cannot run without, and Mid mode
has never had it. Spread capture is the *gross* edge; adverse selection is what
you actually keep. A maker is filled precisely when someone better informed
wanted the other side, so a book that looks profitable per-quote can bleed
steadily once mark-out is netted off.

    markout_bp = side * (ref_price(t+h) - fill_price) / fill_price * 10_000

``side`` is +1 for a buy and -1 for a sell, so the sign convention is uniform:
**positive means the market moved OUR way after the fill**. A buy filled at 100
that trades at 101 an instant later scores +100bp; the same move after a sell
scores -100bp. Net of fees is what decides whether the quote was worth posting.

Honest horizons
---------------
The reference series is Hyperliquid's PUSHED mid, so 1s/5s/30s are real. They
were impossible while Nado's 3-8s poll was the only clock. 60s/300s come from
1m candle closes: an exact grid that survives restarts and can be graded
offline, which is why they are the durable primary.

Two disciplines this module enforces rather than trusts callers with:

* A sample whose ACTUAL horizon drifts past the jitter bound is **discarded,
  not relabelled** — a "5s" mark-out measured at 9s is not a 5s mark-out, and
  silently keeping it biases the whole series toward whatever the feed was
  doing when it was slow.
* Every sample records ``horizon_actual_s`` alongside ``horizon_nominal_s``,
  so the drift is auditable after the fact instead of assumed away.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

BUY = 1
SELL = -1

REF_TICK = "tick"          # HL pushed mid, in-process ring; volatile
REF_CANDLE_1M = "candle_1m"  # HL 1m closes; exact grid, durable


@dataclass(frozen=True)
class FillRef:
    """A fill we want to grade. ``side`` is +1 buy / -1 sell."""

    fill_id: str
    ts: float
    side: int
    fill_price: float
    size_base: float = 0.0
    fee_quote: float = 0.0


@dataclass(frozen=True)
class MarkoutSample:
    horizon_nominal_s: float
    horizon_actual_s: float
    ref_price: float
    markout_bp: float
    net_markout_bp: float
    ref_source: str
    basis_bp: Optional[float] = None


def markout_bp(fill: FillRef, ref_price: float) -> Optional[float]:
    """Signed mark-out in bp. Positive = the market moved our way."""
    try:
        entry = float(fill.fill_price)
        ref = float(ref_price)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or ref <= 0:
        return None
    side = 1 if int(fill.side) >= 0 else -1
    return side * (ref - entry) / entry * 10_000.0


def net_markout_bp(fill: FillRef, ref_price: float, *, fee_bp: float) -> Optional[float]:
    """Mark-out after the round-trip fee. This is the number that says whether
    the quote was worth posting — gross mark-out can be positive while the fee
    still makes the fill a loss."""
    gross = markout_bp(fill, ref_price)
    if gross is None:
        return None
    return gross - abs(float(fee_bp))


def pick_reference(
    series: Sequence[Sequence[float]],
    t_target: float,
    *,
    max_jitter_s: float,
) -> Optional[tuple]:
    """Nearest sample AT OR AFTER ``t_target`` from ``series`` of ``(ts, price)``.

    Returns ``(price, actual_elapsed_from_target)`` or ``None`` when the closest
    available sample is further than ``max_jitter_s`` past the target — the
    sample is then dropped rather than mislabelled. Assumes ``series`` is sorted
    oldest-first, which both the tick ring and candle closes are.
    """
    if not series or max_jitter_s < 0:
        return None
    for row in series:
        try:
            ts, price = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if ts < t_target:
            continue
        if (ts - t_target) > max_jitter_s:
            return None          # the series skipped past our horizon
        if price <= 0:
            return None
        return price, ts - t_target
    return None                   # horizon has not elapsed yet


def build_sample(
    fill: FillRef,
    series: Sequence[Sequence[float]],
    *,
    horizon_s: float,
    fee_bp: float,
    ref_source: str,
    max_jitter_s: Optional[float] = None,
    basis_bp: Optional[float] = None,
) -> Optional[MarkoutSample]:
    """Grade one fill at one horizon, or ``None`` if it cannot be graded
    truthfully (horizon not elapsed, or the reference drifted too far).

    ``max_jitter_s`` defaults to 25% of the horizon: tight enough that a "5s"
    number means 5s, loose enough to survive ordinary feed irregularity.
    """
    jitter = (0.25 * float(horizon_s)) if max_jitter_s is None else float(max_jitter_s)
    picked = pick_reference(series, float(fill.ts) + float(horizon_s), max_jitter_s=jitter)
    if picked is None:
        return None
    ref_price, drift = picked
    gross = markout_bp(fill, ref_price)
    if gross is None:
        return None
    return MarkoutSample(
        horizon_nominal_s=float(horizon_s),
        horizon_actual_s=float(horizon_s) + drift,
        ref_price=ref_price,
        markout_bp=gross,
        net_markout_bp=gross - abs(float(fee_bp)),
        ref_source=str(ref_source),
        basis_bp=basis_bp,
    )


def _percentile(values: list, pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = pct / 100.0 * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize(samples: Sequence[MarkoutSample]) -> dict:
    """Aggregate per nominal horizon. Median leads because mark-out is
    fat-tailed — one gap dominates a mean and would trigger spurious widening.
    """
    out: dict = {}
    by_horizon: dict = {}
    for s in samples:
        by_horizon.setdefault(s.horizon_nominal_s, []).append(s)
    for horizon, rows in sorted(by_horizon.items()):
        gross = [r.markout_bp for r in rows]
        net = [r.net_markout_bp for r in rows]
        out[horizon] = {
            "n": len(rows),
            "mean_bp": sum(gross) / len(gross),
            "median_bp": _percentile(gross, 50),
            "p25_bp": _percentile(gross, 25),
            "p75_bp": _percentile(gross, 75),
            "mean_net_bp": sum(net) / len(net),
            "median_net_bp": _percentile(net, 50),
            "adverse_share": sum(1 for g in gross if g < 0) / len(gross),
            "mean_horizon_actual_s": sum(r.horizon_actual_s for r in rows) / len(rows),
        }
    return out


def widen_recommendation(
    summary: dict,
    *,
    current_half_spread_bp: float,
    horizon_s: float,
    min_samples: int = 30,
    max_factor: float = 3.0,
) -> float:
    """Multiplier for the half-spread given measured mark-out.

    Returns 1.0 (no change) unless there is enough evidence AND the median NET
    mark-out is negative — i.e. we are being picked off faster than the spread
    pays. The widening covers the measured shortfall and no more; it is capped,
    and it never tightens, because a mark-out series is evidence of harm and
    never evidence that quoting tighter is safe.
    """
    row = summary.get(horizon_s)
    if not row or row.get("n", 0) < min_samples:
        return 1.0
    median_net = row.get("median_net_bp")
    if median_net is None or median_net >= 0:
        return 1.0
    half = float(current_half_spread_bp)
    if half <= 0:
        return 1.0
    factor = (half + abs(median_net)) / half
    return max(1.0, min(float(max_factor), factor))
