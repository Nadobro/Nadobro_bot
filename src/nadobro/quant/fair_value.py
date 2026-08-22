"""Fair-value blending across references, and funding carry.

Pure math — no I/O, no config, stdlib only.

THE BOUNDARY THIS MODULE MUST NOT LET ANYONE CROSS
--------------------------------------------------
The blended value here is a FORECAST anchor, not a quote price. Our orders rest
in Nado's book, so a Nado quote is priced off Nado's own touch. If a Hyperliquid
mid were allowed to set the Nado quote directly, we would post systematically
behind on one side and inside on the other, and cross-venue arbitrageurs would
harvest the basis from us — strictly worse than being blind. Callers use this to
decide *which way to lean and how wide*, never to place a level.

Two rules make the blend safe:

* A reference that is STALE or that DISAGREES with the book beyond a threshold
  is DROPPED, never blended. A stale oracle must not drag a live quote, and a
  reference that disagrees wildly is evidence something is broken on one venue
  — the correct response is to widen or stand down, not to average the two.
* Weights renormalise over whatever survives, so dropping a reference degrades
  smoothly instead of silently zeroing the blend.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence


def robust_median(values: Sequence[Optional[float]]) -> Optional[float]:
    """Median of the present, positive values. ``None`` when nothing survives.

    Median rather than mean: with three or more venues a single dislocated or
    stale feed changes a mean materially and a median barely at all, which is
    the whole reason to consult several references.
    """
    vals = sorted(float(v) for v in (values or []) if v is not None and float(v) > 0)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def basis_bp(reference: Optional[float], venue_mid: Optional[float]) -> Optional[float]:
    """``(venue - reference) / reference`` in bp. Positive = our venue is richer.

    Recorded alongside every mark-out sample so a persistent price offset
    between venues can never be mistaken for adverse selection.
    """
    try:
        ref, venue = float(reference), float(venue_mid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ref <= 0 or venue <= 0:
        return None
    return (venue - ref) / ref * 10_000.0


def blend(
    anchor: Optional[float],
    references: Mapping[str, Sequence[float]],
    *,
    weights: Optional[Mapping[str, float]] = None,
    max_deviation_bp: float = 100.0,
    max_age_s: float = 30.0,
    now: Optional[float] = None,
) -> dict:
    """Blend ``anchor`` (the venue book) with named references.

    ``references`` maps name -> ``(price, age_seconds)``. Returns a dict with
    the blended value plus the audit trail — which references were used and
    which were dropped and why — because a fair value nobody can explain is one
    nobody should trade on.
    """
    if anchor is None or float(anchor) <= 0:
        return {"value": None, "used": [], "dropped": {"anchor": "missing"}}
    anchor_f = float(anchor)
    w = dict(weights or {})
    used: dict = {"anchor": anchor_f}
    used_w: dict = {"anchor": float(w.get("anchor", 1.0))}
    dropped: dict = {}

    for name, row in (references or {}).items():
        try:
            price, age = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            dropped[name] = "malformed"
            continue
        if price <= 0:
            dropped[name] = "non_positive"
            continue
        if age > max_age_s:
            dropped[name] = f"stale({age:.1f}s)"
            continue
        dev = abs(basis_bp(price, anchor_f) or 0.0)
        if dev > max_deviation_bp:
            # Not an average-able difference — one of the two is wrong.
            dropped[name] = f"deviates({dev:.0f}bp)"
            continue
        used[name] = price
        used_w[name] = float(w.get(name, 1.0))

    total_w = sum(used_w.values())
    if total_w <= 0:
        return {"value": anchor_f, "used": ["anchor"], "dropped": dropped}
    value = sum(used[k] * used_w[k] for k in used) / total_w
    return {"value": value, "used": sorted(used), "dropped": dropped}


def funding_carry_bp(
    daily_rate: Optional[float], *, hold_seconds: float, side: int
) -> Optional[float]:
    """Cost of carrying inventory for ``hold_seconds``, in bp of notional.

    Nado's ``funding_rate_x18`` is a signed **DAILY** rate settled hourly, so
    the conversion divides by 86400 — NOT by 24. (``cum_funding_x18`` is a
    cumulative amount, not a rate; do not substitute it.)

    Sign convention: positive funding means longs pay shorts. The returned
    value is a COST for the side that pays, so a long paying positive funding
    gets a positive number that should be added to the fee when deciding how
    wide to quote.
    """
    if daily_rate is None:
        return None
    try:
        rate = float(daily_rate)
    except (TypeError, ValueError):
        return None
    if hold_seconds <= 0:
        return 0.0
    frac = rate * (float(hold_seconds) / 86400.0)
    cost = frac if int(side) >= 0 else -frac
    return cost * 10_000.0
