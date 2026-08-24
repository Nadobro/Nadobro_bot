"""Blending short-horizon signals into ONE bounded directional number.

Pure math — no I/O, no config, stdlib only.

What may and may not enter
--------------------------
Only the names in :data:`DIRECTIONAL_SIGNALS` are blendable. Realized vol,
spread width, depth quality and every candle oscillator (EMA / RSI / MACD /
Bollinger / ATR) are **defensive**, and folding a volatility magnitude into a
signed direction is precisely how a market maker leans into a cascade: a maker
is short gamma, so "the market is moving a lot" is a reason to widen or stand
down, never a reason to pick a side. Those route to ``spread_mult`` /
``entry_ok`` / ``sl_pct`` / ``tp_pct`` instead, and :func:`blend` drops them with
a reason rather than trusting the caller to have read this paragraph.

Abstain, never impute
---------------------
A missing component is DROPPED and the remaining weights renormalise — it is
never replaced by zero, because zero is a real reading ("balanced flow") and
would quietly pull the blend toward neutral in proportion to how much of the
feed was broken. Below ``min_weight_covered`` of the total weight there is not
enough evidence to lean at all, and the result is ``alpha = 0,
confidence = 0``. Same contract as ``llm/signal_engine``.

Why everything is dimensionless before it gets here
---------------------------------------------------
Each component arrives already standardised to [-1, +1] against a spread- or
sigma-relative scale (OBI is an identity; displacements divide by the
half-spread; momentum divides by sigma*sqrt(h)). That is what makes a cold
start work: no distributional history is needed, so the very first tick of a
session produces a usable number instead of waiting for a warm-up window.

The clamp
---------
Until the scorecard says the weights are trustworthy, ``alpha`` is hard-clamped
to +/-``MAX_ALPHA_UNTRUSTED``. Through the controller's bounded actuation that
is a few percent of half-spread skew — a wrong prior costs almost nothing,
while an unclamped one can quote a whole side into an informed flow.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional, Sequence

# Signed, short-horizon, may set direction.
DIRECTIONAL_SIGNALS = frozenset({
    "obi",                  # order-book imbalance, already in [-1, 1]
    "micro_displacement",   # microprice vs mid, in half-spreads
    "trade_imbalance",      # (buy - sell) / (buy + sell) on the tape
    "ofi",                  # normalised order-flow imbalance
    "momentum",             # return over the horizon, in sigma
    "basis",                # venue vs reference, in max(spread, fee)
})

# Unsigned or slow. Contextual/defensive — these must never reach `alpha`.
DEFENSIVE_SIGNALS = frozenset({
    "realized_vol", "vol_of_vol", "spread_bp", "depth", "depth_imbalance",
    "ema", "rsi", "macd", "bollinger", "atr", "funding", "volume_profile",
})

# Cold-start priors, used until per-component scoring earns better ones.
# Funding is carry, not alpha, and is deliberately absent.
COLD_START_WEIGHTS: Dict[str, float] = {
    "obi": 0.30,
    "micro_displacement": 0.25,
    "trade_imbalance": 0.25,
    "ofi": 0.10,
    "momentum": 0.05,
    "basis": 0.05,
}

MAX_ALPHA_UNTRUSTED = 0.35


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def squash(value: Any, *, scale: float = 1.0) -> Optional[float]:
    """``tanh(value / scale)`` — bounded with no clip cliff.

    A hard clip makes every extreme reading identical, which throws away the
    difference between "strong" and "absurd" exactly where it matters most;
    tanh keeps them ordered while still bounding the result.
    """
    x = _num(value)
    s = _num(scale)
    if x is None or s is None or s == 0:
        return None
    return math.tanh(x / s)


def robust_z(value: Any, history: Sequence[Any]) -> Optional[float]:
    """Median/MAD z-score. Robust because one gap in a tick series would
    dominate a mean/stdev version and mislabel a normal reading as extreme."""
    x = _num(value)
    vals = [v for v in (_num(h) for h in (history or [])) if v is not None]
    if x is None or len(vals) < 3:
        return None
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    devs = sorted(abs(v - med) for v in vals)
    mad = devs[n // 2] if n % 2 else (devs[n // 2 - 1] + devs[n // 2]) / 2.0
    if mad <= 0:
        return None
    # 1.4826 puts MAD on the same scale as a standard deviation for normal data.
    return (x - med) / (1.4826 * mad)


def standardize(components: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    """Bound every component to [-1, 1], dropping what cannot be read.

    Inputs are expected to arrive already scale-free (see the module docstring);
    this is the belt-and-braces pass that guarantees the invariant even if a
    caller hands over something wider.
    """
    out: Dict[str, Optional[float]] = {}
    for name, raw in (components or {}).items():
        value = _num(raw)
        if value is None:
            out[str(name)] = None
            continue
        out[str(name)] = max(-1.0, min(1.0, value)) if abs(value) <= 1.0 else squash(value)
    return out


def blend(
    components: Mapping[str, Any],
    *,
    weights: Optional[Mapping[str, float]] = None,
    min_weight_covered: float = 0.5,
    trusted: bool = False,
    max_alpha: float = MAX_ALPHA_UNTRUSTED,
) -> Dict[str, Any]:
    """Weighted blend of the directional components into one bounded number.

    Returns ``{alpha, confidence, used, dropped, covered}``. ``dropped`` is an
    audit trail — a signal that silently vanishes is a signal nobody can debug,
    and a defensive name appearing here is a wiring bug worth seeing.
    """
    w = dict(weights or COLD_START_WEIGHTS)
    standardized = standardize(components)
    used: Dict[str, float] = {}
    used_w: Dict[str, float] = {}
    dropped: Dict[str, str] = {}

    for name, value in standardized.items():
        if name not in DIRECTIONAL_SIGNALS:
            # Defensive/unknown: it belongs on spread_mult or entry_ok, not here.
            dropped[name] = "not_directional"
            continue
        weight = _num(w.get(name))
        if weight is None or weight <= 0:
            dropped[name] = "no_weight"
            continue
        if value is None:
            dropped[name] = "missing"
            continue
        used[name] = value
        used_w[name] = weight

    total_w = sum(_num(v) or 0.0 for k, v in w.items() if k in DIRECTIONAL_SIGNALS)
    covered_w = sum(used_w.values())
    covered = (covered_w / total_w) if total_w > 0 else 0.0
    if covered_w <= 0 or covered < max(0.0, float(min_weight_covered)):
        return {"alpha": 0.0, "confidence": 0.0, "used": sorted(used),
                "dropped": dropped, "covered": round(covered, 4)}

    raw = sum(used[k] * used_w[k] for k in used) / covered_w
    # Agreement: unanimous components earn full confidence, a coin-flip earns
    # none. Magnitude alone is not confidence — three signals of 0.9 pointing
    # different ways is a market nobody understands, not a strong view.
    if raw == 0:
        agreement = 0.0
    else:
        agree_w = sum(used_w[k] for k in used if (used[k] > 0) == (raw > 0))
        agreement = max(0.0, 2.0 * (agree_w / covered_w) - 1.0)
    confidence = max(0.0, min(1.0, covered * agreement))

    limit = abs(_num(max_alpha) or MAX_ALPHA_UNTRUSTED)
    alpha = raw if trusted else max(-limit, min(limit, raw))
    return {
        "alpha": alpha,
        "confidence": confidence,
        "used": sorted(used),
        "dropped": dropped,
        "covered": round(covered, 4),
    }


def resolve_bias(fast_alpha: Any, slow_bias: Any, *, fast_weight: float = 0.75) -> float:
    """ONE directional number from the fast alpha and the slow timeframe vote.

    Both want to steer the same knob, and two writers to one field is how
    dead-bands stop working — so the two are reconciled here, once, and the
    caller writes the result a single time. Either side missing means the other
    stands alone rather than being averaged against a zero it never asserted.
    """
    fast = _num(fast_alpha)
    slow = _num(slow_bias)
    if fast is None and slow is None:
        return 0.0
    if fast is None:
        return max(-1.0, min(1.0, slow or 0.0))
    if slow is None:
        return max(-1.0, min(1.0, fast))
    fw = max(0.0, min(1.0, _num(fast_weight) if _num(fast_weight) is not None else 0.75))
    return max(-1.0, min(1.0, fw * fast + (1.0 - fw) * slow))


def anchor_offset_bp(
    alpha: Any, *, half_spread_bp: Any, strength: float = 0.5,
    max_frac_of_half_spread: float = 0.5,
) -> float:
    """Fair-value shift implied by ``alpha``, in bp.

    Alpha moves the ANCHOR the quotes are built around, not the spread skew:
    the spread skew field belongs to the user's directional_bias, and an
    anchor shift composes cleanly with the inventory reservation offset because
    both are displacements of the same quantity.

    Bounded by a fraction of the half-spread for the same reason the inventory
    shift is: a fair-value estimate that can move a quote more than half a
    half-spread is one that can cross the book on its own.
    """
    a = _num(alpha)
    half = _num(half_spread_bp)
    if a is None or half is None or half <= 0 or a == 0:
        return 0.0
    a = max(-1.0, min(1.0, a))
    cap = half * max(0.0, _num(max_frac_of_half_spread) or 0.0)
    return max(-cap, min(cap, a * (_num(strength) or 0.0) * half))
