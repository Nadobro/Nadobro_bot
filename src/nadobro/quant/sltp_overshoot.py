"""Leverage-aware SL overshoot buffer for the session PnL rail. Pure — no I/O,
stdlib only (mirrors ``quant/liquidation.py`` and ``quant/margin.py``), so the
strategy/handlers layers import it without adding a new import edge
(``tests/lint/test_architecture_layers`` only lets the edge set shrink).

Why this exists
---------------
The session SL rail (``strategy/bot_runtime._evaluate_session_pnl_rail``) is a
*polled* check that fires when live PnL, as a %-of-margin, crosses ``-sl_pct``.
Two things make the *realized* loss overshoot the user's number under leverage:

  * detection latency — the rail only samples every poll, and
  * close latency — after the breach is seen, the flatten order still takes a
    round-trip to fill.

During that window price keeps moving, and at leverage ``L`` a price move of
``move`` fraction is ``L * move`` in %-of-margin terms. So the overshoot beyond
the barrier scales with **leverage × how fast price is currently moving**. A
bare ``pct <= -sl_pct`` reserves nothing for it, so a 10%-of-$100 stop at 50x
routinely realizes well past −$10.

The fix (this module): tighten the *trigger* by a buffer that reserves room for
that overshoot, so the rail fires early enough that the realized exit lands at
or under the user's ``sl_pct``.

    reserve_frac = clamp( max(lev_term, vol_term), 0, max_fraction )
        lev_term = leverage / lev_ref                 (primary — always known)
        vol_term = L * recent_move_frac * 100 * factor / sl_pct   (optional)
    buffer            = sl_pct * reserve_frac         (never exceeds max_fraction)
    effective_trigger = sl_pct - buffer               (fire earlier, never later)

The **leverage term** is the primary, always-available driver: a higher sizing
multiplier deploys more notional per unit of margin, so the same price wobble is
a larger %-of-margin overshoot — reserve proportionally. It needs no price
history, so the rail can apply it every poll with no extra state or reads. The
optional **volatility term** widens the buffer further when a live per-poll move
estimate is supplied (``recent_move_bp``); when it is not (0), the buffer is
purely leverage-driven. The reserve is clamped to a fraction of the SL budget so
it can never invert or disarm the stop, and low leverage + no volatility yields
≈0 buffer (fires at the user's exact number). TP is never buffered (firing a
take-profit early would leave profit on the table)."""
from __future__ import annotations

import os
from typing import Optional


# --- env-resolved tuning (read like quant/liquidation.py, stdlib only) -------

def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").split("#", 1)[0].strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def overshoot_factor() -> float:
    """Multiplier on the projected one-poll adverse move, covering the extra
    close-fill latency + slippage beyond a single sampling window. Default 1.0
    (reserve ~one poll-window of adverse move). ``0`` disables the buffer math
    while leaving the rail wiring intact."""
    return max(0.0, _env_float("NADO_SLTP_OVERSHOOT_FACTOR", 1.0))


def max_buffer_fraction() -> float:
    """Hard cap on the buffer as a fraction of ``sl_pct`` — the rail never fires
    earlier than ``(1 - this) * sl_pct``. Keeps high leverage / a volatile market
    from turning a 10% stop into a hair-trigger. Default 0.5 (never tighten past
    half the budget); clamped to [0, 0.9]."""
    return min(0.9, max(0.0, _env_float("NADO_SLTP_MAX_BUFFER_FRAC", 0.5)))


def leverage_reference() -> float:
    """Leverage at which the buffer reaches its cap: the leverage-driven reserve
    is ``max_fraction * clamp(leverage/lev_ref, 0, 1)``. Default 50x (a typical
    high-leverage perp deploy), floored at 1 to avoid divide-by-zero."""
    return max(1.0, _env_float("NADO_SLTP_LEVERAGE_REF", 50.0))


# --- pure math ---------------------------------------------------------------

def move_bp(prev_mark: float, mark: float) -> float:
    """Absolute price move between two marks, in basis points. ``0`` when either
    mark is missing/non-positive (no usable volatility estimate → no buffer)."""
    try:
        p = float(prev_mark)
        m = float(mark)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0 or m <= 0:
        return 0.0
    return abs(m - p) / p * 10_000.0


def projected_overshoot_pct(
    leverage: float, recent_move_bp: float, factor: Optional[float] = None
) -> float:
    """Projected adverse PnL overshoot, in %-of-margin, if price keeps moving at
    the recently-observed rate through the flatten latency:
    ``L * (recent_move_bp/10000) * 100 * factor``. Never negative; ``0`` when
    leverage or the recent move is unknown/zero."""
    f = overshoot_factor() if factor is None else max(0.0, factor)
    L = max(0.0, float(leverage or 0.0))
    move = max(0.0, float(recent_move_bp or 0.0))
    if L <= 0.0 or move <= 0.0 or f <= 0.0:
        return 0.0
    return L * (move / 10_000.0) * 100.0 * f


def leverage_reserve_frac(leverage: float, lev_ref: Optional[float] = None) -> float:
    """Leverage-driven reserve as a fraction (0..1, pre-cap): ``L / lev_ref``
    clamped to [0, 1]. Needs no price history, so the rail applies it every poll.
    ``0`` when leverage is unknown/≤0 (conservative → no buffer)."""
    ref = leverage_reference() if lev_ref is None else max(1.0, float(lev_ref))
    L = max(0.0, float(leverage or 0.0))
    if L <= 0.0:
        return 0.0
    return min(1.0, L / ref)


def sl_buffer_pct(
    sl_pct: float,
    leverage: float,
    recent_move_bp: float = 0.0,
    *,
    factor: Optional[float] = None,
    max_fraction: Optional[float] = None,
    lev_ref: Optional[float] = None,
) -> float:
    """The overshoot buffer to subtract from ``sl_pct``, clamped to
    ``[0, sl_pct * max_fraction]``. Reserve = ``max(leverage_term, vol_term)``:
    the leverage term is always available; the volatility term contributes only
    when ``recent_move_bp > 0``. ``0`` when the stop is disarmed (``sl_pct<=0``)
    or nothing is projected."""
    if sl_pct is None or sl_pct <= 0:
        return 0.0
    cap_frac = max_buffer_fraction() if max_fraction is None else min(0.9, max(0.0, max_fraction))
    lev_frac = leverage_reserve_frac(leverage, lev_ref)
    vol_frac = projected_overshoot_pct(leverage, recent_move_bp, factor) / float(sl_pct)
    reserve = max(0.0, min(cap_frac, max(lev_frac, vol_frac)))
    return float(sl_pct) * reserve


def effective_sl_trigger(
    sl_pct: float,
    leverage: float,
    recent_move_bp: float = 0.0,
    *,
    factor: Optional[float] = None,
    max_fraction: Optional[float] = None,
    lev_ref: Optional[float] = None,
) -> float:
    """The tightened SL trigger magnitude (still a positive %-of-margin): the
    rail should fire when ``pct_net <= -effective_sl_trigger(...)``. Equal to
    ``sl_pct`` when disarmed or when no reserve is warranted, and never below
    ``(1 - max_fraction) * sl_pct`` nor above ``sl_pct`` — so it only ever
    tightens the user's stop, never loosens or inverts it."""
    if sl_pct is None or sl_pct <= 0:
        return float(sl_pct or 0.0)
    return float(sl_pct) - sl_buffer_pct(
        sl_pct, leverage, recent_move_bp,
        factor=factor, max_fraction=max_fraction, lev_ref=lev_ref,
    )
