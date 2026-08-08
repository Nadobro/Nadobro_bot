"""Reusable MockNadoAdapter test double for the engine.

Supports: a scripted mid-price tape, explicit (and partial) fills, fill
latency by deferring fills to a later tick, and an adversarial mode that
raises transient ``AdapterError`` on selected methods for ``fail_times`` calls
(rate-limit storms / transient errors).
"""
from __future__ import annotations

import copy
import time
from decimal import Decimal
from typing import AsyncIterator, Dict, List, Optional

from src.nadobro.engine.adapter.base import (
    AdapterError,
    Fill,
    NadoAdapterBase,
    NadoOrder,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderState,
)
from src.nadobro.engine.types import OrderType, TradeType, _dec


class MockNadoAdapter(NadoAdapterBase):
    connector_name = "nado"

    def __init__(
        self,
        *,
        mid: object = Decimal("100"),
        mids: Optional[List[object]] = None,
        tick: object = Decimal("0.01"),
        lot: object = Decimal("0.001"),
        min_notional: object = Decimal("1"),
        auto_fill_market: bool = True,
        fail_on: Optional[List[str]] = None,
        fail_times: int = 0,
        venue_held: Optional[dict] = None,
        fill_marketable_limits: bool = False,
    ) -> None:
        # SPOT-RECONCILE: pair -> base units the VENUE reports, independent of
        # engine inventory. None entries model an unreadable venue (fail safe).
        self.venue_held: dict = dict(venue_held or {})
        self._mid = _dec(mid)
        self._mids = [_dec(m) for m in mids] if mids else None
        self._mid_idx = 0
        self._tick = _dec(tick)
        self._lot = _dec(lot)
        self._min_notional = _dec(min_notional)
        self.auto_fill_market = auto_fill_market
        self.fill_marketable_limits = fill_marketable_limits
        self.fail_on = set(fail_on or [])
        self.fail_remaining = fail_times
        self._orders: Dict[str, NadoOrder] = {}
        self._counter = 0
        self._fill_events: List[Fill] = []
        self.placed: List[NadoOrder] = []
        self.cancelled: List[str] = []
        # Funding the short leg "earns" per call to funding_since (received-
        # positive). Tests can set this to simulate accrued funding.
        self.funding_quote: Decimal = Decimal(0)
        # Current signed daily funding rate returned by funding_rate(); None =
        # "no signal" (the default — mimics a venue that isn't reporting yet).
        self.funding_rate_value: object = None

    # -- test controls ----------------------------------------------------
    def set_mid(self, value: object) -> None:
        self._mid = _dec(value)

    def _current_mid(self) -> Decimal:
        if self._mids is not None:
            return self._mids[min(self._mid_idx, len(self._mids) - 1)]
        return self._mid

    def fill_order(
        self,
        order_id: str,
        amount: object = None,
        price: object = None,
        fee: object = Decimal(0),
        partial: bool = False,
    ) -> Fill:
        order = self._orders[order_id]
        remaining = order.amount_base - order.filled_base
        amt = _dec(amount) if amount is not None else remaining
        px = _dec(price) if price is not None else (order.price or self._current_mid())
        return self._apply_fill(order, amt, px, _dec(fee), partial)

    def _apply_fill(
        self, order: NadoOrder, amount: Decimal, price: Decimal, fee: Decimal, partial: bool
    ) -> Fill:
        order.filled_base += amount
        order.filled_quote += amount * price
        order.fee_quote += fee
        if not partial and order.filled_base >= order.amount_base:
            order.state = OrderState.FILLED
        else:
            order.state = OrderState.PARTIALLY_FILLED
        if order.trading_pair in self.venue_held:
            _delta = _dec(amount) if order.side is TradeType.BUY else -_dec(amount)
            self.venue_held[order.trading_pair] = _dec(
                self.venue_held.get(order.trading_pair) or 0) + _delta
        fill = Fill(order.id, order.trading_pair, order.side, amount, price, fee, time.time())
        self._fill_events.append(fill)
        return fill

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_on and self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise AdapterError(f"transient failure in {method}")

    # -- adapter surface --------------------------------------------------
    async def place_order(
        self,
        trading_pair: str,
        side: TradeType,
        order_type: OrderType,
        amount_base: Decimal,
        price: Optional[Decimal] = None,
        leverage: int = 1,
        reduce_only: bool = False,
    ) -> NadoOrder:
        self._maybe_fail("place_order")
        self._counter += 1
        oid = f"ord-{self._counter}"
        order = NadoOrder(
            id=oid,
            trading_pair=trading_pair,
            side=side,
            order_type=order_type,
            amount_base=_dec(amount_base),
            price=_dec(price) if price is not None else None,
        )
        self._orders[oid] = order
        self.placed.append(order)
        if self.auto_fill_market and self._crosses_now(order_type, side, price):
            fill_px = price if price is not None else self._current_mid()
            self._apply_fill(order, order.amount_base, _dec(fill_px), Decimal(0), partial=False)
        return copy.copy(order)

    def _crosses_now(self, order_type, side, price) -> bool:
        """Only a MARKET order fills on placement by default.

        A MARKETABLE LIMIT (the engine's risk exits, priced through the touch)
        also crosses in reality, but inferring that here from price-vs-mid also
        catches ordinary limits that tests need to REST — a DN close at its
        take-profit barrier, for instance. Set ``fill_marketable_limits=True`` to
        opt a test into the realistic behaviour instead of guessing globally.
        """
        if order_type is OrderType.MARKET:
            return True
        if not self.fill_marketable_limits:
            return False
        if order_type is not OrderType.LIMIT or price is None:
            return False        # LIMIT_MAKER never crosses; a priceless LIMIT rests
        px, mid = _dec(price), self._current_mid()
        if mid <= 0:
            return False
        return px >= mid if side is TradeType.BUY else px <= mid

    async def held_base(self, trading_pair: str):
        """AUDIT round 4: this used to return a STATIC dict entry that fills never
        mutated, so `venue` and `book` could never disagree in the direction that
        triggered the DN sweep's sell/buy oscillator — the tests were structurally
        incapable of seeing a critical defect. Now every fill moves it."""
        self._maybe_fail("held_base")
        if trading_pair not in self.venue_held:
            return None
        return _dec(self.venue_held.get(trading_pair) or 0)

    async def cancel_order(self, order_id: str) -> bool:
        self._maybe_fail("cancel_order")
        order = self._orders.get(order_id)
        if order is None or order.state.is_terminal:
            return False
        order.state = OrderState.CANCELLED
        self.cancelled.append(order_id)
        return True

    async def order_status(self, order_id: str) -> NadoOrder:
        self._maybe_fail("order_status")
        order = self._orders.get(order_id)
        if order is None:
            raise AdapterError(f"unknown order {order_id}")
        return copy.copy(order)

    async def fill_stream(self, trading_pair: str) -> AsyncIterator[Fill]:
        for fill in list(self._fill_events):
            if fill.trading_pair == trading_pair:
                yield fill

    async def order_book(self, trading_pair: str) -> OrderBookSnapshot:
        self._maybe_fail("order_book")
        mid = self._current_mid()
        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=[OrderBookLevel(mid, Decimal(1))],
            asks=[OrderBookLevel(mid, Decimal(1))],
            timestamp=time.time(),
        )

    async def mid_price(self, trading_pair: str) -> Decimal:
        self._maybe_fail("mid_price")
        if self._mids is not None:
            value = self._mids[min(self._mid_idx, len(self._mids) - 1)]
            if self._mid_idx < len(self._mids) - 1:
                self._mid_idx += 1
            return value
        return self._mid

    async def funding_since(self, trading_pair: str, since_ts: float) -> Decimal:
        self._maybe_fail("funding_since")
        return _dec(self.funding_quote)

    async def funding_rate(self, trading_pair: str):
        self._maybe_fail("funding_rate")
        v = self.funding_rate_value
        return _dec(v) if v is not None else None

    def tick_size(self, trading_pair: str) -> Decimal:
        return self._tick

    def lot_size(self, trading_pair: str) -> Decimal:
        return self._lot

    def min_notional(self, trading_pair: str) -> Decimal:
        return self._min_notional
