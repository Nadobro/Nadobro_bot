"""Quote-ladder planning — pure math, no venue access.

Turns "I have D dollars to deploy on this side" into a list of
``(offset_bp, size_quote)`` levels. Used by Mid (reference = book mid) and
fill-anchored Grid (reference = last fill); only the reference differs, the
laddering is identical.

Why this exists
===============
Both modes shipped ONE order per side and then waited. Mid did not even read
``levels`` — the mapping said outright that a single bid/ask carries the full
notional — so the user's setting was dead. Nothing scaled into a move or out
of one.

The level count is bounded by the venue minimum, which is the constraint that
killed the original attempt: dividing a small notional across levels silently
produced sub-minimum orders the venue rejects. Here the clamp is explicit and
the caller is told what it got.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import List, Optional, Sequence

FLAT = "flat"
LINEAR = "linear"
GEOMETRIC = "geometric"
CURVES = (FLAT, LINEAR, GEOMETRIC)

# Geometric growth factor per level. At 2.0 the weights are 1:2:4:8, giving a
# deepest/nearest size ratio of 8 against linear's 4 — a curve that is
# genuinely more back-loaded. A ratio of 1.6 was tried first and produced a
# ratio of 4.096 vs linear's 4.0, i.e. an option that did nothing.
_GEOMETRIC_RATIO = Decimal("2.0")

# "No venue minimum reported" — expressed as a large bound rather than a
# sentinel so callers keep using plain min() against their requested count.
NO_FLOOR_LEVEL_BOUND = 1000

# Hard ceiling on rungs per side, matching the UI's own ``levels`` bound. A
# config that escapes that validator (a stored value, a bad migration) must not
# be able to ask for hundreds of rungs: the geometric weights are 2**i, so the
# near rungs underflow to dust long before the count itself becomes a problem.
MAX_LADDER_LEVELS = 20

# The quantum the sizing loop rounds to. The clamp below measures against the
# SAME quantum, so "does this rung survive rounding" is answered exactly rather
# than approximately.
_SIZE_QUANTUM = Decimal("0.00000001")


def _dec(value: object, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except Exception:  # noqa: BLE001  # policy: degrade-ok(malformed input -> default)
        return Decimal(default)


@dataclass(frozen=True)
class LadderLevel:
    index: int
    offset_bp: Decimal      # distance from the reference, always >= 0
    size_quote: Decimal     # notional for this level


def max_levels(deployed_quote: object, min_notional: object) -> int:
    """How many levels this side can carry with every level above the venue floor.

    Always >= 1: a deployment smaller than the minimum still gets ONE order and
    lets the venue be the arbiter, rather than silently placing nothing.

    A floor of zero means we have no minimum to respect, so it cannot constrain
    the count — returning 1 there would collapse every ladder on any venue or
    adapter that reports no minimum, which is the opposite of the intent.
    """
    deployed = _dec(deployed_quote)
    floor = _dec(min_notional)
    if deployed <= 0:
        return 0
    if floor <= 0:
        return NO_FLOOR_LEVEL_BOUND
    return max(1, int((deployed / floor).to_integral_value(rounding=ROUND_DOWN)))


def _weights(n: int, curve: str) -> List[Decimal]:
    """Relative size per level, index 0 = nearest the reference."""
    if n <= 1:
        return [Decimal(1)]
    c = str(curve or FLAT).strip().lower()
    if c == LINEAR:
        return [Decimal(i + 1) for i in range(n)]
    if c == GEOMETRIC:
        return [_GEOMETRIC_RATIO ** i for i in range(n)]
    return [Decimal(1)] * n


def proximity_weights(
    prices: Sequence[object],
    levels: Sequence[object],
    *,
    tolerance_bp: object = 25,
    boost: object = 1.5,
) -> List[Decimal]:
    """Per-rung multipliers that favour rungs sitting on a price LEVEL.

    Support and resistance are the one slow-timeframe object with real quoting
    value, because they are PRICES rather than directions: a bid resting where
    buyers have repeatedly shown up fills more often and gets run over less.
    Everything else the candle stack produces (EMA, RSI, MACD) is a direction
    or a magnitude, and on a 3-8s tick it cannot change between two quote
    decisions anyway.

    Returns a multiplier per rung, 1.0 where no level is near. The result is a
    RESHAPING input for :func:`plan_ladder`, which divides by the weight total —
    so this redistributes a side's deployment and never adds to it.
    """
    tol = _dec(tolerance_bp)
    factor = _dec(boost)
    refs = [_dec(v) for v in (levels or [])]
    refs = [r for r in refs if r > 0]
    out: List[Decimal] = []
    for raw in prices or []:
        price = _dec(raw)
        weight = Decimal(1)
        if price > 0 and refs and tol > 0 and factor > 0:
            nearest = min(abs(price - r) / price * Decimal(10000) for r in refs)
            if nearest <= tol:
                # Linear falloff to 1.0 at the tolerance edge, so a rung does
                # not jump in size as a level drifts one bp closer.
                closeness = (tol - nearest) / tol
                weight = Decimal(1) + (factor - Decimal(1)) * closeness
        out.append(weight)
    return out


def plan_ladder(
    deployed_quote: object,
    *,
    levels: object = 1,
    step_bp: object = 0,
    first_offset_bp: object = 0,
    curve: str = FLAT,
    min_notional: object = 0,
    level_weights: Optional[Sequence[object]] = None,
) -> List[LadderLevel]:
    """Plan one side of the ladder.

    ``levels`` is a REQUEST; the returned list may be shorter because every
    level must clear ``min_notional``. Sizes always sum to exactly
    ``deployed_quote`` — the rounding remainder rides the LAST (deepest) level,
    so the near touch level is never inflated above plan.

    Deeper levels are further from the reference (``offset_bp`` increasing),
    which is what makes the ladder scale INTO an adverse move and out of a
    favourable one.

    ``level_weights`` multiplies the curve's own weights per rung (see
    :func:`proximity_weights`). It reshapes the distribution only: the per-side
    total is unchanged, and the min-notional stepdown below is computed on the
    COMBINED weights, so a reshaped ladder cannot smuggle a sub-minimum rung
    past the check the curve stepdown exists for.
    """
    deployed = _dec(deployed_quote)
    if deployed <= 0:
        return []
    want = int(_dec(levels, "1") or 1)
    n = max(1, min(want, MAX_LADDER_LEVELS, max_levels(deployed, min_notional)))

    # LADDER-CURVE-UNDERSIZE (2026-08-03): ``max_levels`` answers "how many
    # EQUAL slices clear the floor". A curve then makes the near rungs much
    # smaller than that average — geometric over 8 levels puts 0.4% of the
    # deployment on L0 — so rungs land under the venue minimum. Nothing
    # rejected them: the venue client GROWS a sub-minimum non-reducing order
    # before signing, so the side quietly deployed more than the user's budget
    # (measured +38% on geometric/8). Step the level count down until the
    # SMALLEST weighted rung clears the floor.
    #
    # The ``> 0`` half is a floor-independent backstop (self-audit 2026-08-03):
    # with no venue minimum reported — which is what ``_plan_side`` falls back
    # to when product metadata is unavailable — a steep curve quantised the
    # near rungs to ZERO, and a zero-size order is either rejected or grown by
    # the venue client into an unbudgeted one.
    def _combined(count: int) -> List[Decimal]:
        base = _weights(count, curve)
        if not level_weights:
            return base
        out: List[Decimal] = []
        for i, b in enumerate(base):
            try:
                m = _dec(level_weights[i]) if i < len(level_weights) else Decimal(1)
            except Exception:  # noqa: BLE001 - a malformed weight is just 1.0
                m = Decimal(1)
            out.append(b * (m if m > 0 else Decimal(1)))
        return out if sum(out, Decimal(0)) > 0 else base

    floor = _dec(min_notional)
    while n > 1:
        w = _combined(n)
        smallest = (deployed * min(w) / sum(w, Decimal(0))).quantize(
            _SIZE_QUANTUM, rounding=ROUND_DOWN
        )
        if smallest > 0 and smallest >= floor:
            break
        n -= 1

    w = _combined(n)
    total_w = sum(w, Decimal(0))
    first = _dec(first_offset_bp)
    step = _dec(step_bp)

    out: List[LadderLevel] = []
    assigned = Decimal(0)
    for i in range(n):
        if i < n - 1:
            size = (deployed * w[i] / total_w).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
        else:
            size = deployed - assigned      # exact: remainder on the deepest level
        assigned += size
        out.append(LadderLevel(
            index=i,
            offset_bp=first + step * Decimal(i),
            size_quote=size,
        ))
    return out


def ladder_notional(levels: Sequence[LadderLevel]) -> Decimal:
    return sum((lv.size_quote for lv in levels), Decimal(0))


def describe(levels: Sequence[LadderLevel]) -> str:
    """One-line human summary for logs and the strategy card."""
    if not levels:
        return "no levels"
    return " | ".join(
        f"L{lv.index}@{float(lv.offset_bp):.1f}bp ${float(lv.size_quote):,.0f}"
        for lv in levels
    )
