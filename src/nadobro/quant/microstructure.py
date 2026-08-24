"""Order-book microstructure. Pure math — no I/O, no config, stdlib only.

Takes a book in the shape both venues are normalised to::

    {"bids": [[price, size], ...], "asks": [[price, size], ...]}   best-first

``nado_client.get_market_liquidity`` and ``market_data.hl_ws.book`` both emit
exactly this, so every function here runs on either. That is deliberate: the
*caller* decides which book is the quote ANCHOR (always Nado — our orders rest
there) and which is the SIGNAL (Hyperliquid). Nothing in this module knows or
cares, which is what keeps that decision in one place instead of smeared
through the math.

Why this exists at all: the engine's ``order_book()`` fabricates an L1 book
with **size = 0**, so the market maker has been structurally blind to size —
it cannot compute a microprice, an imbalance, or a slippage estimate. Every
function returns ``None``/``0.0`` on a one-sided, empty or malformed book so a
consumer can fail open rather than special-case a degraded feed.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Sequence

Book = dict
Levels = Sequence[Sequence[float]]

BUY = "buy"
SELL = "sell"


def _levels(book: Optional[Book], side: str) -> list:
    if not isinstance(book, dict):
        return []
    raw = book.get("bids" if side == BUY else "asks") or []
    out = []
    for lvl in raw:
        try:
            price, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and size > 0:
            out.append((price, size))
    return out


def l1(book: Optional[Book]) -> Optional[tuple]:
    """``(bid, bid_size, ask, ask_size)`` or ``None`` on a one-sided book."""
    bids, asks = _levels(book, BUY), _levels(book, SELL)
    if not bids or not asks:
        return None
    return bids[0][0], bids[0][1], asks[0][0], asks[0][1]


def mid(book: Optional[Book]) -> Optional[float]:
    """Arithmetic midpoint. The naive fair value — see :func:`microprice`."""
    top = l1(book)
    if top is None:
        return None
    bid, _bs, ask, _as = top
    return (bid + ask) / 2.0


def microprice(book: Optional[Book], *, levels: int = 1) -> Optional[float]:
    """Size-weighted fair value::

        microprice = (bid * ask_size + ask * bid_size) / (bid_size + ask_size)

    NOTE THE CROSS-WEIGHTING — each price is weighted by the size on the
    OPPOSITE side, and getting it backwards silently inverts every signal built
    on it. The intuition: a large resting bid means buyers are queued and the
    next trade is more likely to lift the ask, so heavy bid size pulls fair
    value UP toward the ask. Substituting bid_size=0 gives exactly the bid,
    which is the right limit — with nothing bid, the book is all offer.

    On a balanced book this equals the midpoint, so it is a strict improvement
    over ``mid`` and never worse. ``levels`` aggregates size over the top N
    levels per side, which is steadier on a book whose touch flickers.
    """
    bids, asks = _levels(book, BUY), _levels(book, SELL)
    if not bids or not asks:
        return None
    n = max(1, int(levels))
    bid_size = sum(s for _p, s in bids[:n])
    ask_size = sum(s for _p, s in asks[:n])
    total = bid_size + ask_size
    if total <= 0:
        return None
    return (bids[0][0] * ask_size + asks[0][0] * bid_size) / total


def spread_bp(book: Optional[Book]) -> Optional[float]:
    """Touch spread in basis points of the midpoint."""
    top = l1(book)
    if top is None:
        return None
    bid, _bs, ask, _as = top
    m = (bid + ask) / 2.0
    if m <= 0:
        return None
    return (ask - bid) / m * 10_000.0


def obi(book: Optional[Book], *, levels: int = 1) -> Optional[float]:
    """Order-book imbalance over the top ``levels``::

        (bid_depth - ask_depth) / (bid_depth + ask_depth)

    Already bounded to [-1, +1], so it needs no squashing downstream.
    Positive = more visible bid liquidity.
    """
    bids, asks = _levels(book, BUY), _levels(book, SELL)
    if not bids or not asks:
        return None
    n = max(1, int(levels))
    b = sum(s for _p, s in bids[:n])
    a = sum(s for _p, s in asks[:n])
    if b + a <= 0:
        return None
    return (b - a) / (b + a)


def obi_bands(book: Optional[Book], bands: Sequence[int] = (1, 3, 5, 10)) -> dict:
    """OBI at several depths. Shallow and deep imbalance disagree exactly when
    the book is being spoofed or refilled, so the spread between bands carries
    information the touch alone does not."""
    return {int(n): obi(book, levels=int(n)) for n in bands}


def depth_notional(book: Optional[Book], side: str, *, bp: float) -> float:
    """Cumulative notional resting within ``bp`` basis points **of the touch on
    that side**. The sizing input: never quote more than a small share of it.

    Measured from the touch, not the mid, on purpose. This answers "how much
    liquidity sits near where I would rest an order", and a resting order goes
    at the touch. Anchoring on the mid instead makes the whole band fall inside
    the spread on any wide market — it would have returned 0 for a book whose
    spread merely exceeds ``bp``, which is exactly when sizing matters most.
    """
    levels = _levels(book, side)
    if not levels:
        return 0.0
    touch = levels[0][0]
    limit = touch * (1.0 - bp / 10_000.0) if side == BUY else touch * (1.0 + bp / 10_000.0)
    total = 0.0
    for price, size in levels:
        if (side == BUY and price < limit) or (side == SELL and price > limit):
            break
        total += price * size
    return total


def slippage_bp(book: Optional[Book], side: str, notional: float) -> Optional[float]:
    """Walk the ladder for ``notional`` and return the average fill's distance
    from the mid, in bp. ``None`` when the book cannot absorb it — which is
    itself the answer: the order is too large for this venue right now.

    ``side`` is the side WE take: ``BUY`` walks the asks.
    """
    m = mid(book)
    if m is None or m <= 0 or notional <= 0:
        return None
    # BUY consumes the asks, SELL consumes the bids.
    vwap = _walk_vwap(_levels(book, SELL if side == BUY else BUY), float(notional))
    if vwap is None:
        return None
    return abs(vwap - m) / m * 10_000.0


def _walk_vwap(book_side: list, notional: float) -> Optional[float]:
    remaining, base, spent = float(notional), 0.0, 0.0
    for price, size in book_side:
        level_notional = price * size
        take = min(remaining, level_notional)
        base += take / price
        spent += take
        remaining -= take
        if remaining <= 1e-12:
            return spent / base if base > 0 else None
    return None


def book_hash(book: Optional[Book], *, levels: int = 5) -> str:
    """Stable digest of the top ``levels``. Two consecutive identical hashes on
    a pushed feed mean the socket is frozen, not that the market is quiet —
    the cheapest stale-feed detector available."""
    parts = []
    for side in (BUY, SELL):
        for price, size in _levels(book, side)[: max(1, int(levels))]:
            parts.append(f"{side}:{price!r}:{size!r}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
