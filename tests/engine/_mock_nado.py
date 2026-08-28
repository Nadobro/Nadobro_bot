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
        # engine inventory. A pair ABSENT from this dict reads as an unreadable
        # venue (held_base -> None, fail safe); map a pair to 0 to model a flat,
        # readable venue for that pair.
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
        # Fused-replace bookkeeping (Phase 8): (old_digest, new_digest) pairs
        # from cancel_and_place, and digests dropped via forget_cancelled.
        self.replaced: List[tuple] = []
        self.forgotten: List[str] = []
        # Model a NON-ATOMIC venue: cancel_and_place places the new order but
        # leaves the old one RESTING (partial success). Exercises the settle
        # fallback that must then cancel it to avoid a double order.
        self.cap_leaves_old_resting = False
        # Leverage each path signs, so a test can prove classic == fused.
        self.place_leverages: List[int] = []
        self.cap_leverages: List[int] = []
        # Funding the short leg "earns" per call to funding_since (received-
        # positive). Tests can set this to simulate accrued funding.
        self.funding_quote: Decimal = Decimal(0)
        # Current signed daily funding rate returned by funding_rate(); None =
        # "no signal" (the default — mimics a venue that isn't reporting yet).
        self.funding_rate_value: object = None
        # Venue PRICE-TRIGGER orders (Reverse Grid rungs). Kept in their OWN maps,
        # separate from _orders, so the double models the venue's separate trigger
        # service: a trigger rests until the mid crosses its level, then fires.
        #   _triggers: digest -> {order, trigger_price, side, dependency, armed}
        self._triggers: Dict[str, dict] = {}
        self.placed_triggers: List[NadoOrder] = []
        self.cancelled_triggers: List[str] = []

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
        self.place_leverages.append(int(leverage))
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

    # -- price-trigger orders (Reverse Grid rungs) ------------------------
    async def place_trigger_order(
        self,
        trading_pair: str,
        side: TradeType,
        amount_base: Decimal,
        trigger_price: Decimal,
        *,
        slippage_pct: float = 0.5,
        dependency: Optional[str] = None,
    ) -> NadoOrder:
        self._maybe_fail("place_trigger_order")
        if _dec(amount_base) <= 0:
            raise AdapterError("place_trigger_order requires a positive amount")
        if _dec(trigger_price) <= 0:
            raise AdapterError("place_trigger_order requires a positive trigger price")
        self._counter += 1
        tid = f"trg-{self._counter}"
        order = NadoOrder(
            id=tid, trading_pair=trading_pair, side=side,
            order_type=OrderType.LIMIT, amount_base=_dec(amount_base),
            price=_dec(trigger_price),
        )
        self._triggers[tid] = {
            "order": order,
            "trigger_price": _dec(trigger_price),
            "side": side,
            "dependency": dependency,
            # A dependent rung stays UNARMED until its parent fires (pyramiding);
            # an independent rung is armed the moment it is placed.
            "armed": dependency is None,
        }
        self.placed_triggers.append(order)
        return copy.copy(order)

    async def place_stop_order(
        self,
        trading_pair: str,
        close_size: Decimal,
        stop_price: Decimal,
        position_is_long: bool,
        *,
        slippage_pct: float = 0.5,
    ) -> NadoOrder:
        self._maybe_fail("place_stop_order")
        if _dec(close_size) <= 0:
            raise AdapterError("place_stop_order requires a positive close size")
        if _dec(stop_price) <= 0:
            raise AdapterError("place_stop_order requires a positive stop price")
        self._counter += 1
        sid = f"stp-{self._counter}"
        close_side = TradeType.SELL if position_is_long else TradeType.BUY
        order = NadoOrder(
            id=sid, trading_pair=trading_pair, side=close_side,
            order_type=OrderType.LIMIT, amount_base=_dec(close_size),
            price=_dec(stop_price),
        )
        self._triggers[sid] = {
            "order": order,
            "trigger_price": _dec(stop_price),
            "side": close_side,           # a long's stop is a SELL, fires on a fall
            "dependency": None,
            "armed": True,
            "kind": "reduce",             # reduce-only: flattens, never grows/flips
        }
        self.placed_triggers.append(order)
        return copy.copy(order)

    async def cancel_trigger_order(self, order_id: str) -> bool:
        self._maybe_fail("cancel_trigger_order")
        trg = self._triggers.get(order_id)
        if trg is None:
            return False
        order = trg["order"]
        if order.state.is_terminal:      # already fired / cancelled — idempotent
            self._triggers.pop(order_id, None)
            return False
        order.state = OrderState.CANCELLED
        self.cancelled_triggers.append(order_id)
        self._triggers.pop(order_id, None)
        return True

    # -- test controls: make the venue "watch the mid" --------------------
    def fire_trigger(self, order_id: str, *, price: object = None, fee: object = Decimal(0)) -> Fill:
        """Fire ONE placed trigger as if the mid crossed its level: fill it, emit
        the Fill onto the stream, and ARM any rung that depended on it (the
        pyramiding chain). Raises KeyError for an unknown/terminal trigger."""
        trg = self._triggers.get(order_id)
        if trg is None:
            raise KeyError(f"unknown trigger {order_id}")
        order = trg["order"]
        fill_px = _dec(price) if price is not None else trg["trigger_price"]
        amount = order.amount_base
        if trg.get("kind") == "reduce" and order.trading_pair in self.venue_held:
            # Reduce-only: close AT MOST the current position — never grow or flip.
            held = abs(_dec(self.venue_held.get(order.trading_pair) or 0))
            amount = min(order.amount_base, held) if held > 0 else Decimal(0)
        fill = self._apply_fill(order, amount, fill_px, _dec(fee), partial=False)
        self._arm_dependents(order_id)
        self._triggers.pop(order_id, None)
        return fill

    def cross_triggers(self, mid: object) -> List[Fill]:
        """Fire every ARMED placed trigger whose level ``mid`` has crossed — a BUY
        rung when ``mid >= trigger_price``, a SELL rung when ``mid <= trigger_price``
        — filled AT ``mid``. Returns the fills in placement order. Models the
        venue watching the mid one tick at a time; call it repeatedly along a
        tape. A rung armed by this crossing (a freshly-fired parent's dependent)
        is eligible on the NEXT call, not this one."""
        mid_d = _dec(mid)
        fills: List[Fill] = []
        # Snapshot the set ARMED at entry: a rung armed by a fire DURING this call
        # (a freshly-fired parent's dependent) becomes eligible on the NEXT call,
        # not this one — so pyramiding advances one rung per tick, deterministically.
        eligible = [tid for tid, trg in self._triggers.items() if trg["armed"]]
        for tid in eligible:
            trg = self._triggers.get(tid)
            if trg is None:
                continue
            order = trg["order"]
            if order.state.is_terminal:
                continue
            tp = trg["trigger_price"]
            crossed = (mid_d >= tp) if trg["side"] is TradeType.BUY else (mid_d <= tp)
            if crossed:
                fills.append(self.fire_trigger(tid, price=mid_d))
        return fills

    def _arm_dependents(self, parent_id: str) -> None:
        for trg in self._triggers.values():
            dep = trg["dependency"]
            dep_digest = getattr(dep, "digest", dep)   # accept a digest or a wrapper
            if dep_digest == parent_id:
                trg["armed"] = True

    async def cancel_and_place(
        self,
        cancel_order_id: str,
        trading_pair: str,
        side: TradeType,
        order_type: OrderType,
        amount_base: Decimal,
        price: Decimal,
        leverage: int = 1,
        reduce_only: bool = False,
    ) -> NadoOrder:
        # ATOMIC like the venue: _maybe_fail raises BEFORE any state change, so a
        # failure leaves the OLD order resting and nothing new placed.
        self._maybe_fail("cancel_and_place")
        self.cap_leverages.append(int(leverage))
        old = self._orders.get(cancel_order_id)
        if old is not None and not old.state.is_terminal and not self.cap_leaves_old_resting:
            # Atomic cancel — recorded in ``replaced`` below, NOT in ``cancelled``
            # (which tracks classic cancel_order calls, so tests can tell the
            # fused path from a stop-then-spawn).
            old.state = OrderState.CANCELLED
        self._counter += 1
        oid = f"ord-{self._counter}"
        order = NadoOrder(
            id=oid, trading_pair=trading_pair, side=side, order_type=order_type,
            amount_base=_dec(amount_base),
            price=_dec(price) if price is not None else None,
        )
        self._orders[oid] = order
        self.placed.append(order)
        self.replaced.append((cancel_order_id, oid))
        return copy.copy(order)

    def forget_cancelled(self, order_id: str) -> None:
        self.forgotten.append(order_id)

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
