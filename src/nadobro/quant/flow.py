"""Order-flow signals: book-change imbalance and the trade tape.

Pure math — no I/O, no config, stdlib only.

Order-flow imbalance measures *changes* in the book rather than its current
state, which is what carries short-horizon information a static snapshot does
not. The canonical best-level form (Cont-Kukanov-Stoikov) is::

    e = 1{Pb >= Pb'} * Qb  -  1{Pb <= Pb'} * Qb'
      - 1{Pa <= Pa'} * Qa  +  1{Pa >= Pa'} * Qa'

Unprimed = now, primed = previous. Read it as: bid-side size added at a price
at least as good counts positive, bid size withdrawn counts negative, and the
ask side mirrors it. A pure price move therefore registers at full size, which
is the intended behaviour — the touch moving IS the flow.

A NOTE ON NAMING, which matters more here than it looks
-------------------------------------------------------
``ofi`` in this module is computed from Hyperliquid's PUSHED l2Book stream, so
consecutive snapshots really are consecutive book states and the quantity is
the literature's. If anyone ever computes it from Nado's POLLED book instead,
it must be named ``poll_ofi`` and never ``ofi``: 3-8s poll deltas collapse an
unknown number of inserts and cancels, cancel-then-replace at the same price is
invisible, and fleeting liquidity never appears at all. The two are different
statistics and conflating them would silently import a much weaker signal.

The trade tape
--------------
``side`` semantics on the HL feed (aggressor vs resting maker, and its
encoding) are UNVERIFIED against a live socket, so nothing here hardcodes a
convention. Callers pass a ``side_is_buy`` predicate, and every signed quantity
abstains (``None``) rather than guessing when the predicate cannot classify a
trade. :func:`sign_agreement` exists so the deployed system can *measure*
whether its convention is right and disable the signal instead of trading
backwards.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

# --- trade side conventions -------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """Coerce anything to float, or None. Every field here comes off a wire
    payload whose shape is not guaranteed."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_BUYISH = {"B", "BUY", "BID", "LONG", "TAKER_BUY", "1", "TRUE"}
_SELLISH = {"A", "S", "SELL", "ASK", "SHORT", "TAKER_SELL", "0", "FALSE"}


def hl_side_is_buy(trade: Any) -> Optional[bool]:
    """Best-effort HL ``side`` -> aggressor-is-buy. ``None`` when unclassifiable.

    HL's documented ``side`` is a short code ("B"/"A"). Whether it denotes the
    aggressor or the resting maker is unverified, so treat this as a HYPOTHESIS
    to be validated by :func:`sign_agreement` in production, not as truth.
    """
    raw = trade.get("side") if isinstance(trade, dict) else getattr(trade, "side", None)
    if isinstance(raw, bool):
        return raw
    token = str(raw or "").strip().upper()
    if token in _BUYISH:
        return True
    if token in _SELLISH:
        return False
    return None


def _px_sz(trade: Any) -> Optional[tuple]:
    if isinstance(trade, dict):
        px, sz = _num(trade.get("px")), _num(trade.get("sz"))
    else:
        px, sz = _num(getattr(trade, "px", None)), _num(getattr(trade, "sz", None))
    if px is None or sz is None or px <= 0 or sz <= 0:
        return None
    return px, sz


# --- book-change imbalance --------------------------------------------------

@dataclass(frozen=True)
class BookDelta:
    ofi: float
    touch_moved: int          # -1 down, 0 unchanged, +1 up (bid side)
    bid_price_moved: int
    ask_price_moved: int


def _touch(book: Any, side: str) -> Optional[tuple]:
    if not isinstance(book, dict):
        return None
    rows = book.get(side) or []
    if not rows:
        return None
    try:
        return float(rows[0][0]), float(rows[0][1])
    except (TypeError, ValueError, IndexError):
        return None


def ofi(prev_book: Any, curr_book: Any) -> Optional[BookDelta]:
    """Best-level order-flow imbalance between two consecutive book states.

    ``None`` when either side of either book is missing — a one-sided book has
    no defined touch to difference against. The touch-move indicators are
    reported SEPARATELY from the size term so a caller can tell "size arrived"
    apart from "the price moved", which are different events that the scalar
    alone conflates.
    """
    pb, qb = _touch(prev_book, "bids") or (None, None)
    pa, qa = _touch(prev_book, "asks") or (None, None)
    nb, nqb = _touch(curr_book, "bids") or (None, None)
    na, nqa = _touch(curr_book, "asks") or (None, None)
    if None in (pb, qb, pa, qa, nb, nqb, na, nqa):
        return None

    e = 0.0
    if nb >= pb:
        e += nqb
    if nb <= pb:
        e -= qb
    if na <= pa:
        e -= nqa
    if na >= pa:
        e += qa

    bid_moved = (nb > pb) - (nb < pb)
    ask_moved = (na > pa) - (na < pa)
    return BookDelta(
        ofi=e,
        touch_moved=bid_moved if bid_moved == ask_moved else 0,
        bid_price_moved=bid_moved,
        ask_price_moved=ask_moved,
    )


def ofi_normalized(delta: Optional[BookDelta], *, ref_size: float) -> Optional[float]:
    """Squash raw OFI to [-1, +1] against a reference size (e.g. the trailing
    median touch size for the product), so the scale is self-normalising per
    market instead of needing a hand-tuned constant."""
    if delta is None or ref_size <= 0:
        return None
    x = delta.ofi / ref_size
    # tanh without importing math.tanh on a possibly-huge value.
    if x > 20:
        return 1.0
    if x < -20:
        return -1.0
    import math

    return math.tanh(x)


# --- the tape ---------------------------------------------------------------

def dedupe_trades(trades: Sequence[Any], *, key: Optional[Callable[[Any], Any]] = None) -> list:
    """Drop duplicates while preserving order.

    A websocket reconnect replays recent trades. Counting them twice doubles
    every volume, imbalance and intensity figure downstream, so this is not an
    optimisation — it is a correctness requirement.
    """
    def _default_key(t: Any) -> Any:
        if isinstance(t, dict):
            return (t.get("hash"), t.get("ts") or t.get("time"), t.get("px"), t.get("sz"), t.get("side"))
        return (getattr(t, "hash", None), getattr(t, "ts", None),
                getattr(t, "px", None), getattr(t, "sz", None), getattr(t, "side", None))

    keyfn = key or _default_key
    seen: set = set()
    out: list = []
    for t in trades or []:
        k = keyfn(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _within(trades: Sequence[Any], now: float, window_s: float) -> list:
    cutoff = now - max(0.0, float(window_s))
    out = []
    for t in trades or []:
        ts = _num(t.get("ts") if isinstance(t, dict) else getattr(t, "ts", None))
        if ts is not None and ts >= cutoff:
            out.append(t)
    return out


def signed_volume(
    trades: Sequence[Any],
    *,
    side_is_buy: Callable[[Any], Optional[bool]] = hl_side_is_buy,
) -> Optional[float]:
    """Buy volume minus sell volume, in base units. ``None`` when NO trade could
    be classified — abstaining beats inventing a direction."""
    total, classified = 0.0, 0
    for t in trades or []:
        parsed = _px_sz(t)
        if parsed is None:
            continue
        _px, sz = parsed
        is_buy = side_is_buy(t)
        if is_buy is None:
            continue
        classified += 1
        total += sz if is_buy else -sz
    return total if classified else None


def trade_imbalance(
    trades: Sequence[Any],
    *,
    now: float,
    windows: Sequence[float] = (1.0, 5.0, 30.0, 60.0),
    side_is_buy: Callable[[Any], Optional[bool]] = hl_side_is_buy,
) -> dict:
    """``(buy - sell) / (buy + sell)`` per window, already bounded to [-1, +1].

    Short windows (1s, 5s) are meaningful here ONLY because the tape is pushed.
    They were impossible on Nado's 3-8s poll and must not be reintroduced for a
    polled source.
    """
    out: dict = {}
    for w in windows:
        rows = _within(trades, now, w)
        buy = sell = 0.0
        classified = 0
        for t in rows:
            parsed = _px_sz(t)
            if parsed is None:
                continue
            _px, sz = parsed
            is_buy = side_is_buy(t)
            if is_buy is None:
                continue
            classified += 1
            if is_buy:
                buy += sz
            else:
                sell += sz
        total = buy + sell
        out[float(w)] = ((buy - sell) / total) if (classified and total > 0) else None
    return out


def vwap(trades: Sequence[Any], *, now: float, window_s: float) -> Optional[float]:
    """Volume-weighted average traded price over the window."""
    num = den = 0.0
    for t in _within(trades, now, window_s):
        parsed = _px_sz(t)
        if parsed is None:
            continue
        px, sz = parsed
        num += px * sz
        den += sz
    return (num / den) if den > 0 else None


def trade_intensity(trades: Sequence[Any], *, now: float, window_s: float) -> float:
    """Trades per second — the pacing input, and a cheap liquidity read."""
    if window_s <= 0:
        return 0.0
    return len(_within(trades, now, window_s)) / float(window_s)


def sign_agreement(signed_volumes: Sequence[float], returns: Sequence[float]) -> Optional[float]:
    """Fraction of periods where signed volume and the return agree in sign.

    The production self-check for the unverified ``side`` convention: net
    buying should accompany rising prices. A value well below 0.5 means the
    convention is INVERTED, and the caller must disable the signal rather than
    trade backwards. ``None`` when there is nothing decisive to score.
    """
    pairs = [
        (v, r) for v, r in zip(signed_volumes, returns)
        if v is not None and r is not None and v != 0 and r != 0
    ]
    if not pairs:
        return None
    agree = sum(1 for v, r in pairs if (v > 0) == (r > 0))
    return agree / len(pairs)
