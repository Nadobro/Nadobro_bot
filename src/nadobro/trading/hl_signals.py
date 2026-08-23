"""Turn the Hyperliquid feed into the six directional components Mid blends.

Why this lives in ``trading/``
------------------------------
The assembly needs ``market_data`` (the HL socket state) and ``quant`` (the pure
math) at once. ``market_data -> quant`` is NOT an allowed import edge and the
edge set may only shrink (``tests/lint/test_architecture_layers``), while
``trading`` may import both. ``strategy`` then reaches it over the allowed
``strategy -> trading`` edge to build the injected provider.

THE BOUNDARY
------------
Everything here is FORECAST. Not one value may price a Nado quote: our orders
rest in Nado's book, and quoting HL's fair value there posts behind on one side
and inside on the other, handing the basis to cross-venue arbitrageurs. The
caller uses these to decide which way to lean and how wide, never to place a
level.

Three states, and the difference matters
----------------------------------------
* ``None`` — HL should have this coin but the data is stale or absent. That is
  DEGRADED: the caller widens and shortens the ladder.
* ``{"supported": False}`` — Nado lists this product and HL does not (every
  equity/RWA: QQQ, wGOOGLx...). That is normal and permanent, so it must NOT
  read as degradation, or those markets would quote wide forever for a feed
  that was never coming.
* components — business as usual.

The trade-side convention is a HYPOTHESIS
-----------------------------------------
Whether HL's ``side`` denotes the aggressor or the resting maker is unverified
against a live socket, and the entire sign of ``trade_imbalance`` rides on it.
So the module measures itself: signed volume should correlate positively with
the return over the same interval, and once there is enough history to tell,
a convention scoring below chance DISABLES the component instead of trading
backwards.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from src.nadobro.quant import alpha as _alpha
from src.nadobro.quant import fair_value as _fv
from src.nadobro.quant import flow as _flow
from src.nadobro.quant import microstructure as _ms
from src.nadobro.quant import realized_vol as _rv
from src.nadobro.utils.env import env_bool, env_float, env_int

logger = logging.getLogger(__name__)

# Momentum horizon and the volatility halflife it is scaled by. Both are short
# because the HL feed is PUSHED — they were impossible on Nado's 3-8s poll.
_MOMENTUM_HORIZON_S = env_float("NADO_HL_MOMENTUM_HORIZON_S", 30.0)
_VOL_HALFLIFE_S = env_float("NADO_HL_VOL_HALFLIFE_S", 60.0)
_TRADE_WINDOW_S = env_float("NADO_HL_TRADE_WINDOW_S", 5.0)

# Mid ring, fed by the pushed book so the momentum/vol clock is the market's,
# not the strategy cadence's.
_MID_RING = env_int("NADO_HL_MID_RING", 4000)

# Samples before the side-convention self-check is allowed to judge, and the
# agreement below which it disables the component. 0.5 is chance; 0.45 leaves
# room for noise while still catching a fully inverted mapping.
_SIDE_CHECK_MIN_SAMPLES = env_int("NADO_HL_SIDE_CHECK_SAMPLES", 20)
_SIDE_CHECK_FLOOR = env_float("NADO_HL_SIDE_CHECK_FLOOR", 0.45)


def basis_alpha_enabled() -> bool:
    """The basis component rests on HL LEADING Nado, which is a hypothesis
    until a lead-lag study says otherwise. Kill switch for that one term."""
    return env_bool("NADO_HL_BASIS_ALPHA", True)


# --- process-wide state (one HL feed, one set of rings) ---------------------
_mids: Dict[str, Deque[tuple]] = {}          # coin -> [(ts, mid), ...]
_last_book: Dict[str, dict] = {}             # coin -> previous l2 snapshot
_side_check: Dict[str, Deque[tuple]] = {}    # coin -> [(signed_vol, return), ...]
_side_disabled: set = set()
_registered = False


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def coin_for(product_name: Any) -> str:
    """Nado product -> HL coin. A wrapped equity simply will not resolve on HL,
    which is the correct outcome rather than a wrong mapping."""
    base = str(product_name or "").upper().strip()
    for suffix in ("-PERP", "-USD", "-USDT0", "-USDT"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def _on_book(coin: str) -> None:
    """Book listener: sample the mid on the market's clock, not ours."""
    from src.nadobro.market_data import hl_ws

    book = hl_ws.book(coin)
    if not book:
        return
    mid = _ms.mid(book)
    if mid is None or mid <= 0:
        return
    ring = _mids.setdefault(coin, deque(maxlen=_MID_RING))
    ring.append((time.time(), mid))


def ensure_registered() -> None:
    """Attach the mid-ring listener to the HL feed. Idempotent.

    A module-level function (not a closure) so ``register_book_listener``'s
    equality dedupe actually fires — the same trap that leaked listeners in
    ``hl_ws`` once already.
    """
    global _registered
    if _registered:
        return
    from src.nadobro.market_data import hl_ws

    hl_ws.register_book_listener(_on_book)
    _registered = True


def reset_state() -> None:
    """Tests only."""
    global _registered
    _mids.clear()
    _last_book.clear()
    _side_check.clear()
    _side_disabled.clear()
    _registered = False


# --- the components ---------------------------------------------------------

def _micro_displacement(book: dict, spread_bp: Optional[float]) -> Optional[float]:
    """Microprice vs mid, measured in HALF-SPREADS.

    Scale-free by construction, which is what lets a cold session use it on the
    first tick: a 1bp displacement means something very different on a 2bp book
    than on a 50bp one, and dividing by the half-spread says which.
    """
    micro, mid = _ms.microprice(book), _ms.mid(book)
    if micro is None or mid is None or mid <= 0 or not spread_bp or spread_bp <= 0:
        return None
    disp_bp = (micro - mid) / mid * 10_000.0
    return _alpha.squash(disp_bp, scale=max(0.5 * spread_bp, 0.1))


def _momentum(coin: str) -> Optional[float]:
    """Return over the horizon, in units of the volatility over that horizon."""
    ring = _mids.get(coin)
    if not ring or len(ring) < 3:
        return None
    now, last = ring[-1]
    cutoff = now - _MOMENTUM_HORIZON_S
    past = None
    for ts, px in ring:
        if ts >= cutoff:
            past = px
            break
    if past is None or past <= 0 or last <= 0:
        return None
    ret = math.log(last / past)
    sigma = _rv.vol_over(list(ring), halflife_s=_VOL_HALFLIFE_S,
                         horizon_s=_MOMENTUM_HORIZON_S)
    if sigma is None or sigma <= 0:
        return None
    return _alpha.squash(ret, scale=sigma)


def _ofi(coin: str, book: dict) -> Optional[float]:
    """Event-level order-flow imbalance between consecutive PUSHED books.

    Named ``ofi`` and not ``poll_ofi`` precisely because the source is pushed:
    consecutive snapshots really are consecutive book states. Computing the same
    formula off a poll would be a different, much weaker statistic.
    """
    prev = _last_book.get(coin)
    _last_book[coin] = book
    if not prev:
        return None
    delta = _flow.ofi(prev, book)
    if delta is None:
        return None
    touch = (book.get("bids") or [[0, 0]])[0]
    ref = _num(touch[1]) if len(touch) > 1 else None
    if not ref or ref <= 0:
        return None
    return _flow.ofi_normalized(delta, ref_size=ref)


def _record_side_check(coin: str, signed_vol: Optional[float]) -> None:
    """Accumulate (signed volume, subsequent return) pairs and judge the
    convention once there is enough of them."""
    ring = _mids.get(coin)
    if signed_vol is None or not ring or len(ring) < 2:
        return
    hist = _side_check.setdefault(coin, deque(maxlen=500))
    ret = None
    now, last = ring[-1]
    for ts, px in ring:
        if ts >= now - _TRADE_WINDOW_S and px > 0:
            ret = (last - px) / px
            break
    if ret is None:
        return
    hist.append((signed_vol, ret))
    if coin in _side_disabled or len(hist) < _SIDE_CHECK_MIN_SAMPLES:
        return
    score = _flow.sign_agreement([v for v, _ in hist], [r for _, r in hist])
    if score is not None and score < _SIDE_CHECK_FLOOR:
        _side_disabled.add(coin)
        logger.warning(
            "hl trade-side convention scores %.2f for %s (below %.2f) — "
            "disabling trade_imbalance rather than trading backwards",
            score, coin, _SIDE_CHECK_FLOOR,
        )


def build_components(product_name: Any, nado_mid: Any = None) -> Optional[dict]:
    """Directional components for one product, or the two absence states.

    See the module docstring: ``None`` = degraded, ``{"supported": False}`` =
    HL does not list this market at all.
    """
    from src.nadobro.market_data import hl_ws

    coin = coin_for(product_name)
    if not coin:
        return {"supported": False}
    if not hl_ws.enabled():
        return None
    book = hl_ws.book(coin)
    if not book:
        # Distinguish "HL never had it" from "HL has it and went quiet": the
        # first is permanent and must not read as degradation.
        if hl_ws.mid(coin, max_age_s=0) is None and coin not in _mids:
            return {"supported": False}
        return None

    spread_bp = _ms.spread_bp(book)
    trades = hl_ws.recent_trades(coin, window_s=_TRADE_WINDOW_S)
    signed_vol = _flow.signed_volume(trades)
    _record_side_check(coin, signed_vol)

    imbalance = None
    if coin not in _side_disabled:
        imbalance = _flow.trade_imbalance(
            trades, now=time.time(), windows=(_TRADE_WINDOW_S,)
        ).get(float(_TRADE_WINDOW_S))

    basis = None
    if basis_alpha_enabled():
        hl_mid = _ms.mid(book)
        b_bp = _fv.basis_bp(hl_mid, _num(nado_mid))
        if b_bp is not None and spread_bp:
            # SIGN: positive basis means NADO is richer than HL. On the premise
            # that the larger venue leads, Nado is then expected to fall back
            # toward HL — so a positive basis is a NEGATIVE (short) alpha.
            # That premise is a hypothesis (see the plan's lead-lag item); the
            # component carries the smallest cold-start weight for that reason,
            # and NADO_HL_BASIS_ALPHA=false removes it entirely.
            basis = -(_alpha.squash(b_bp, scale=max(spread_bp, 1.0)) or 0.0)

    return {
        "supported": True,
        "coin": coin,
        "components": {
            "obi": _ms.obi(book),
            "micro_displacement": _micro_displacement(book, spread_bp),
            "trade_imbalance": imbalance,
            "ofi": _ofi(coin, book),
            "momentum": _momentum(coin),
            "basis": basis,
        },
        # Weights stay the cold-start priors until per-component scoring earns
        # better ones; ``trusted`` stays False so the +/-0.35 clamp holds.
        "trusted": False,
        "spread_bp": spread_bp,
        "side_convention_disabled": coin in _side_disabled,
    }
