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
that overshoot, so the realized exit lands AT the user's ``sl_pct`` (SLTP-EXACT:
not before, not after).

    reserve_frac = clamp( vol_term, 0, max_fraction )
        vol_term = L * recent_move_frac * 100 * factor / sl_pct
    buffer            = sl_pct * reserve_frac         (never exceeds max_fraction)
    effective_trigger = sl_pct - buffer               (fire earlier, never later)

The buffer is PURELY the **volatility term**: it reserves only for the overshoot
that will ACTUALLY happen — leverage × the recently-observed per-poll move × a
latency factor. So a CALM market (``recent_move_bp`` ~ 0) reserves ~0 and the stop
fires at the user's EXACT number (the realized loss lands on it); only a genuinely
fast move reserves room, and only as much as the current velocity implies, up to
``max_buffer_fraction``. There is NO leverage-only floor: a flat leverage haircut
reserves budget with no live move to justify it and fires a calm high-leverage stop
early — it stopped prod #253 at half its budget. The reserve is clamped so it can
never invert or disarm the stop, and low leverage / no volatility yields ≈0 buffer.
TP is never buffered (firing a take-profit early would leave profit on the table).

``leverage_reserve_frac`` / ``leverage_only_cap`` remain for reference/tuning but no
longer feed the buffer."""
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


def leverage_only_cap() -> float:
    """Cap on the LEVERAGE-ONLY reserve — the modest always-on floor applied when
    there is no live volatility estimate (a first poll, or a momentarily still
    mark). Kept small (default 0.15) so a high-leverage but CALM session is not
    stopped at half its SL budget: the SLTP-FEE-BLEED incident (prod #253, 49x,
    a slow -3.56% over 3h) showed the old leverage-ONLY buffer reserving the full
    0.5 cap on leverage alone even though no fast move was happening. The
    VOLATILITY term (leverage x recent move x latency) is what reserves up to the
    full ``max_buffer_fraction`` when the market is genuinely moving fast — that is
    the overshoot the buffer exists for. Clamped to [0, max_buffer_fraction]."""
    return min(max_buffer_fraction(), max(0.0, _env_float("NADO_SLTP_LEVERAGE_ONLY_CAP", 0.15)))


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
    ``[0, sl_pct * max_fraction]``.

    SLTP-EXACT: the reserve is PURELY the volatility term — the overshoot projected
    to occur while the breach is detected and the flatten fills, ``leverage x recent
    per-poll move x factor``. So a CALM session (``recent_move_bp`` ~ 0) reserves ~0
    and the stop fires at the user's EXACT number; only a genuinely fast move
    reserves room, and only as much as the current velocity implies. There is no
    leverage-only floor — a flat leverage haircut fires a calm high-leverage stop
    early (it stopped prod #253 at half its budget). ``0`` when disarmed
    (``sl_pct<=0``) or when nothing is projected (no recent move / leverage)."""
    if sl_pct is None or sl_pct <= 0:
        return 0.0
    cap_frac = max_buffer_fraction() if max_fraction is None else min(0.9, max(0.0, max_fraction))
    vol_frac = projected_overshoot_pct(leverage, recent_move_bp, factor) / float(sl_pct)
    reserve = max(0.0, min(cap_frac, vol_frac))
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
