"""Maintenance-margin liquidation math for leveraged strategy bots. Pure — no
I/O, no package imports (stdlib only), so the engine/handlers/venue layers can
all import it without a new import edge (``tests/lint/test_architecture_layers``
only lets the edge set shrink), mirroring ``quant/margin.py``.

Why this exists
---------------
Nado is account-level cross-margin, and the bot ``leverage`` is only a *sizing*
multiplier: a strategy deploys ``margin x leverage`` of position notional against
the user's collateral. Higher leverage deploys more notional against the same
collateral, so the adverse price move that liquidates the account shrinks. The
session SL/TP rail is measured as a % of MARGIN and is leverage-blind by design,
and it can be disarmed — so nothing today keeps a high-leverage strategy a safe
distance from venue liquidation. These helpers supply that missing check.

The model (worst-case, conservative)
------------------------------------
Treat the strategy's own margin ``M`` as the sole collateral backing its
fully-deployed position ``N = M * L`` (a filled grid ladder is ``M * L`` of
one-sided notional — see ``engine_runtime.map_strategy_config``). Extra account
collateral only pushes the real liquidation *further* away, so assuming none is
the safe direction.

    adverse move -> PnL = -N * move = -M * L * move
    liquidation when equity <= maintenance requirement:
        M (1 - L*move) <= mmf * N = mmf * M * L
    =>  move_to_liq = (1 - mmf*L) / L = 1/L - mmf          (price-move fraction)
    =>  loss at liq  = (1 - mmf*L) * 100                    (% of margin)

The session SL trips at price move ``move_SL = sl_pct / (100 * L)`` (because
uPnL as a %-of-margin equals ``L * price-move%``). For the SL to protect against
liquidation we require ``move_SL <= k * move_to_liq`` for a safety factor
``k`` (default 0.5), plus an additive %-of-margin cushion for fee/funding drag:

    safe_max_sl_pct = 100 * k * (1 - mmf*L) - cushion_pct

``mmf`` (maintenance-margin fraction) comes from the venue catalog; when the
venue payload lacks a maintenance weight, callers pass a *conservative* fallback
(``fallback_mmf`` below) that over-estimates mmf and therefore shrinks the
computed buffer — the guardrail then fires earlier, never later.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# --- env-resolved default tuning (read like quant/margin.py, stdlib only) ----

def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").split("#", 1)[0].strip()
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def sl_safety_factor() -> float:
    """``k`` in ``move_SL <= k * move_to_liq``. The session stop must trip with
    this fraction of the runway-to-liquidation still remaining. Default 0.5."""
    return max(0.0, _env_float("NADO_LIQ_SL_SAFETY_FACTOR", 0.5))


def buffer_cushion_pct() -> float:
    """Additive %-of-margin reserve for fee/funding drag, subtracted from the
    safe-SL ceiling (extends the ``margin.py`` 1.20 idea into %-of-margin terms
    the poll rail speaks). Default 1.0."""
    return max(0.0, _env_float("NADO_LIQ_BUFFER_CUSHION_PCT", 1.0))


def min_runway_frac() -> float:
    """Price-move runway below which a *disarmed* session SL is forbidden. At
    ``L_arm = 1 / (mmf + this)`` liquidation is this fraction of a move away.
    Default 0.05 (5% adverse move)."""
    return max(0.0, _env_float("NADO_LIQ_MIN_RUNWAY_FRAC", 0.05))


def runway_trigger() -> float:
    """Consumed-runway fraction at which the LIVE rail protectively flattens.
    0.75 = flatten with 25% of the runway-to-liquidation remaining."""
    v = _env_float("NADO_LIQ_RUNWAY_TRIGGER", 0.75)
    return min(0.999, max(0.05, v))


def fallback_mmf_ratio() -> float:
    """Fraction of the initial-margin fraction used as the fallback mmf when the
    venue payload carries no maintenance weight. Default 0.75 (over-estimates the
    typical Vertex-family ``mmf ~ imf/2`` -> conservative)."""
    return max(0.0, _env_float("NADO_LIQ_FALLBACK_MMF_RATIO", 0.75))


def mmf_floor() -> float:
    """Absolute floor on the fallback mmf, so tiny-imf (very-high-leverage)
    assets never assume a zero maintenance requirement. Default 0.005."""
    return max(0.0, _env_float("NADO_LIQ_MMF_FLOOR", 0.005))


# --- pure math ---------------------------------------------------------------

def fallback_mmf(imf: float, ratio: Optional[float] = None, floor: Optional[float] = None) -> float:
    """Conservative maintenance-margin fraction derived from the initial one when
    the venue provides no maintenance weight. ``mmf = max(imf * ratio, floor)``.

    True ``mmf`` is always ``< imf`` (maintenance weight is looser than initial),
    so a ratio in (0, 1) is a real estimate; over-shooting it only shrinks the
    computed liquidation buffer, which is safe. ``imf <= 0`` yields the floor.
    """
    r = fallback_mmf_ratio() if ratio is None else ratio
    f = mmf_floor() if floor is None else floor
    est = float(imf) * float(r) if imf and imf > 0 else 0.0
    return max(est, f)


def move_to_liquidation(leverage: float, mmf: float) -> float:
    """Adverse price-move fraction that liquidates the worst-case position:
    ``1/L - mmf``. May be <= 0 when leverage is too high for the maintenance
    margin (any move liquidates) — callers treat that as "cannot certify"."""
    L = max(1.0, float(leverage))
    return 1.0 / L - float(mmf)


def liq_loss_pct(leverage: float, mmf: float) -> float:
    """%-of-margin loss at liquidation: ``(1 - mmf*L) * 100``."""
    L = max(1.0, float(leverage))
    return (1.0 - float(mmf) * L) * 100.0


def safe_max_sl_pct(
    leverage: float, mmf: float,
    k: Optional[float] = None, cushion_pct: Optional[float] = None,
) -> float:
    """Largest session SL (% of margin) that still trips before liquidation:
    ``100*k*(1 - mmf*L) - cushion``. Can be <= 0 when the leverage/mmf leave no
    room for any stop inside the buffer."""
    L = max(1.0, float(leverage))
    kk = sl_safety_factor() if k is None else k
    cc = buffer_cushion_pct() if cushion_pct is None else cushion_pct
    return 100.0 * kk * (1.0 - float(mmf) * L) - cc


def max_safe_leverage(
    sl_pct: float, mmf: float,
    k: Optional[float] = None, cushion_pct: Optional[float] = None,
) -> float:
    """Largest leverage at which ``sl_pct`` is still liquidation-safe, inverting
    ``safe_max_sl_pct``. Returns ``inf`` when mmf<=0 (no maintenance requirement
    known). Never below 1.0."""
    kk = sl_safety_factor() if k is None else k
    cc = buffer_cushion_pct() if cushion_pct is None else cushion_pct
    m = float(mmf)
    if m <= 0 or kk <= 0:
        return float("inf")
    num = 1.0 - (float(sl_pct) + cc) / (100.0 * kk)
    if num <= 0:
        return 1.0
    return max(1.0, num / m)


def leverage_requires_armed_sl(mmf: float, frac: Optional[float] = None) -> float:
    """``L_arm``: the leverage above which a *disarmed* SL is forbidden, because
    liquidation is within ``frac`` of an adverse move. ``1 / (mmf + frac)``."""
    fr = min_runway_frac() if frac is None else frac
    denom = float(mmf) + fr
    if denom <= 0:
        return float("inf")
    return 1.0 / denom


@dataclass(frozen=True)
class LiqSafety:
    ok: bool
    reason: str                 # "" when ok; else a machine tag
    safe_max_sl_pct: float      # S*  (largest liquidation-safe SL, % of margin)
    max_safe_leverage: float    # L*  (largest safe leverage at the given SL)
    liq_move_frac: float        # Δ_liq (adverse price-move fraction to liq)
    liq_loss_pct: float         # (1 - mmf*L) * 100
    l_arm: float                # leverage above which a disarmed SL is forbidden
    require_armed_sl: bool       # whether an armed SL is required at this leverage


def liquidation_safety(
    *,
    leverage: float,
    mmf: float,
    sl_pct: float,
    sl_armed: bool,
    k: Optional[float] = None,
    cushion_pct: Optional[float] = None,
    min_runway: Optional[float] = None,
) -> LiqSafety:
    """Decide whether a (leverage, session-SL) config is a safe distance from
    venue liquidation for the worst-case fully-deployed position.

    ``sl_armed`` is whether the user has an *effective* session stop (an explicit
    or defaulted ``sl_pct > 0``). A disarmed stop is allowed below ``L_arm`` (the
    live proximity rail is the backstop there) but forbidden above it.

    Returns a :class:`LiqSafety`; ``reason`` is one of:
      ``""`` (safe), ``"maint_margin_exceeds_leverage"`` (buffer gone),
      ``"disarmed_sl_high_lev"``, ``"sl_too_loose"``.
    """
    L = max(1.0, float(leverage))
    kk = sl_safety_factor() if k is None else k
    cc = buffer_cushion_pct() if cushion_pct is None else cushion_pct
    mr = min_runway_frac() if min_runway is None else min_runway

    d_liq = move_to_liquidation(L, mmf)
    loss = liq_loss_pct(L, mmf)
    s_star = safe_max_sl_pct(L, mmf, k=kk, cushion_pct=cc)
    l_arm = leverage_requires_armed_sl(mmf, frac=mr)
    l_star = max_safe_leverage(sl_pct, mmf, k=kk, cushion_pct=cc)
    require_armed = L > l_arm

    def _mk(ok: bool, reason: str) -> LiqSafety:
        return LiqSafety(
            ok=ok, reason=reason, safe_max_sl_pct=s_star, max_safe_leverage=l_star,
            liq_move_frac=d_liq, liq_loss_pct=loss, l_arm=l_arm,
            require_armed_sl=require_armed,
        )

    # No room for any stop inside the buffer at this leverage.
    if d_liq <= 0 or s_star <= 0:
        return _mk(False, "maint_margin_exceeds_leverage")
    # High leverage with no effective stop -> only the live rail would protect it.
    if require_armed and not (sl_armed and sl_pct > 0):
        return _mk(False, "disarmed_sl_high_lev")
    # An armed stop must sit inside the safe ceiling.
    if sl_armed and sl_pct > 0 and sl_pct > s_star:
        return _mk(False, "sl_too_loose")
    return _mk(True, "")


def consumed_runway(entry: float, mark: float, liq: float, net_base: float) -> Optional[float]:
    """Fraction of the entry->liquidation price runway already consumed by the
    current mark, directional by position side. ``None`` when the inputs cannot
    yield a trustworthy value (missing/degenerate/wrong-side liq) so the caller
    skips the live proximity check rather than acting on a bad datum.

    long  (net_base>0, entry>liq): consumed = (entry-mark)/(entry-liq)
    short (net_base<0, liq>entry): consumed = (mark-entry)/(liq-entry)
    """
    try:
        e = float(entry); m = float(mark); q = float(liq); nb = float(net_base)
    except (TypeError, ValueError):
        return None
    if e <= 0 or m <= 0 or q <= 0 or nb == 0:
        return None
    if nb > 0:  # long
        runway = e - q
        if runway <= 1e-12 or q >= m:   # liq must be below entry AND below mark
            return None
        return (e - m) / runway
    # short
    runway = q - e
    if runway <= 1e-12 or q <= m:       # liq must be above entry AND above mark
        return None
    return (m - e) / runway
