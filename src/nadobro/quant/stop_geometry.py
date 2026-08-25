"""Price geometry for a venue-side (exchange-enforced) protective stop. Pure —
no I/O, stdlib only (mirrors ``quant/liquidation.py`` / ``quant/sltp_overshoot.py``),
so the venue/strategy layers import it without a new import edge.

The software session rail measures the stop as a **% of margin** (incl. uPnL).
A venue trigger order, by contrast, needs a **mark price** at which to fire. The
two are linked by leverage: uPnL as a %-of-margin equals ``leverage x price-move%``
(a filled position of ``margin x leverage`` notional moves ``L`` × as fast as
price, in margin terms). So the adverse price move that reaches a ``sl_pct``
%-of-margin loss is ``sl_pct / (100 * L)`` — the same relation
``quant/liquidation.py`` uses (``move_SL = sl_pct / (100 * L)``).

    long  stop = entry * (1 - sl_pct/(100*L))      (fires when price falls)
    short stop = entry * (1 + sl_pct/(100*L))      (fires when price rises)

The venue stop uses the user's RAW ``sl_pct`` (not the leverage buffer the
polled rail applies): a venue trigger fires the instant price crosses it, with
no poll latency to reserve against, so it should cap at exactly the user's
number. It is a redundant backstop to the software rail, and always
``reduce_only`` — the venue guarantees it can only shrink the position.
"""
from __future__ import annotations

from typing import Optional


def stop_loss_price(
    is_long: bool, entry_price: float, leverage: float, sl_pct: float
) -> Optional[float]:
    """Mark price at which a ``sl_pct`` %-of-margin stop is hit for a position at
    ``entry_price`` running ``leverage``. ``None`` when the inputs cannot yield a
    trustworthy price (non-positive entry/leverage/sl, or a move so large the
    stop would be at/through zero) so the caller skips placing a venue stop
    rather than acting on a bad number.

    long  -> entry * (1 - sl_pct/(100*L)); short -> entry * (1 + sl_pct/(100*L))."""
    try:
        e = float(entry_price)
        L = float(leverage)
        s = float(sl_pct)
    except (TypeError, ValueError):
        return None
    if e <= 0 or L < 1.0 or s <= 0:
        return None
    move = s / (100.0 * L)              # adverse price-move fraction at the stop
    if move >= 1.0:                     # a >=100% move: degenerate, skip
        return None
    price = e * (1.0 - move) if is_long else e * (1.0 + move)
    return price if price > 0 else None


def stop_trigger_is_below(is_long: bool) -> bool:
    """Which side of the mark the stop fires on: a LONG stop-loss fires when the
    mark falls BELOW the stop (``True``); a SHORT stop-loss fires when the mark
    rises ABOVE it (``False``). Picks MidPriceBelow vs MidPriceAbove."""
    return bool(is_long)


def stop_close_is_buy(is_long: bool) -> bool:
    """Direction of the reduce-only close the stop places: closing a LONG is a
    SELL (``False``); closing a SHORT is a BUY (``True``)."""
    return not bool(is_long)
