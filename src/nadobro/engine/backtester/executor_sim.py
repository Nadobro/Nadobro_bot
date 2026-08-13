"""Backtester fill simulator — a candle-driven :class:`NadoAdapterBase`.

This is the cost-aware venue model the strategies trade against in a backtest. It
is the piece that makes a backtest *honest*: it charges taker/maker fees, accrues
funding over hold time, and applies slippage to market orders — so a strategy
that looks profitable on price moves but bleeds on fees/funding shows a NEGATIVE
net result here (the exact trap a fees=0 mock would hide).

Fill model (no look-ahead): resting LIMIT/LIMIT_MAKER orders fill only when a
*subsequent* candle's range crosses their price (the engine calls
:meth:`match_resting` once per bar BEFORE ticking the controller). MARKET orders
fill immediately at the current mid adjusted for slippage.

Implemented in Phase 5 (backtester).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal

from src.nadobro.quant.vol_fee_estimator import (
    DEFAULT_BUILDER_FEE_RATE,
    DEFAULT_MAKER_FEE_RATE,
    DEFAULT_SPOT_TAKER_FEE_RATE,
)
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
from src.nadobro.engine.backtester.candle_ingest import Candle
from src.nadobro.engine.types import OrderType, TradeType, _dec


@dataclass
class SimCosts:
    """Cost model. Rates are fractions of notional (e.g. 0.00045 = 4.5 bp).

    ``maker_fee`` may be negative (a rebate). ``funding_rate_per_bar`` is the
    funding fraction charged/earned each candle on the held notional (a short
    EARNS it when positive — longs pay shorts)."""

    # ALL-IN per-side rates: the venue's own fee PLUS the 1bp builder routing that
    # policy locks on to every order. The previous defaults carried the base rates
    # only, so a maker round trip modelled 3bp against a real 5bp
    # (quant.vol_fee_estimator.MAKER_ROUND_TRIP_RATE) — understating maker cost by
    # 40%, and by exactly the margin that decides whether a tight grid step is
    # profitable. Every "net of fees" figure this harness produced was optimistic.
    # Sourced from the canonical constants so the sim cannot drift from the mapper's
    # fee floor again.
    taker_fee: Decimal = DEFAULT_SPOT_TAKER_FEE_RATE + DEFAULT_BUILDER_FEE_RATE   # 4.3bp
    maker_fee: Decimal = DEFAULT_MAKER_FEE_RATE + DEFAULT_BUILDER_FEE_RATE        # 2.5bp
    slippage_pct: Decimal = Decimal("0.0005")
    funding_rate_per_bar: Decimal = Decimal("0.0")


@dataclass
class SimMeta:
    tick: Decimal = Decimal("0.01")
    lot: Decimal = Decimal("0.000001")
    min_notional: Decimal = Decimal("1")
    # Only perps accrue funding. Defaults True; a spot leg (e.g. a DN long on
    # ``BTC-USDT0``) should be marked False so it doesn't (wrongly) pay funding
    # that cancels the perp short's earnings.
    is_perp: bool = True


class SimNadoAdapter(NadoAdapterBase):
    """Candle-driven simulated venue. Drive it from :class:`BacktestEngine`:
    call :meth:`set_candle` then :meth:`match_resting` each bar, then tick the
    controller; the controller's ``place_order``/``order_status``/``cancel_order``
    calls are served from this in-memory book."""

    connector_name = "nado"

    def __init__(
        self,
        *,
        costs: Optional[SimCosts] = None,
        meta: Optional[Dict[str, SimMeta]] = None,
        default_meta: Optional[SimMeta] = None,
    ) -> None:
        self.costs = costs or SimCosts()
        self._meta = meta or {}
        self._default_meta = default_meta or SimMeta()
        self._orders: Dict[str, NadoOrder] = {}
        self._counter = 0
        self._fills: List[Fill] = []
        self._candle: Optional[Candle] = None
        # Per-pair running net base (signed) from fills — drives funding accrual.
        self._net_base: Dict[str, Decimal] = {}
        # Funding events (ts, received_quote) so funding_since can window them.
        self._funding_events: List[tuple] = []
        # Aggregate cost telemetry for the report.
        self.total_fees_quote: Decimal = Decimal(0)
        self.total_funding_quote: Decimal = Decimal(0)

    # -- engine controls --------------------------------------------------
    def set_candle(self, candle: Candle) -> None:
        self._candle = candle

    def _m(self, pair: str) -> SimMeta:
        return self._meta.get(pair, self._default_meta)

    def _is_perp(self, pair: str) -> bool:
        """Funding accrues on perps only. Explicit meta wins; otherwise a spot
        quote suffix (``-USDT0`` / ``-USDC0`` / ``-USDC`` / ``-USDT``) marks a
        spot leg, everything else is treated as a perp."""
        m = self._meta.get(pair)
        if m is not None:
            return m.is_perp
        p = pair.upper()
        if p.endswith(("-USDT0", "-USDC0", "-USDC", "-USDT")):
            return False
        return True

    def _record_fill(self, order: NadoOrder, amount: Decimal, price: Decimal, fee: Decimal) -> None:
        order.filled_base += amount
        order.filled_quote += amount * price
        order.fee_quote += fee
        order.state = (
            OrderState.FILLED if order.filled_base >= order.amount_base else OrderState.PARTIALLY_FILLED
        )
        ts = self._candle.ts if self._candle is not None else 0.0
        self._fills.append(Fill(order.id, order.trading_pair, order.side, amount, price, fee, ts))
        self.total_fees_quote += fee
        signed = amount if order.side is TradeType.BUY else -amount
        self._net_base[order.trading_pair] = self._net_base.get(order.trading_pair, Decimal(0)) + signed

    def match_resting(self) -> None:
        """Fill resting LIMIT orders crossed by the current candle's range. A BUY
        fills when the bar trades at/below its price; a SELL when at/above. Fills
        at the order's price (maker) — the resting order got its quote."""
        c = self._candle
        if c is None:
            return
        maker = self.costs.maker_fee
        for order in list(self._orders.values()):
            if order.state.is_terminal or order.order_type is OrderType.MARKET:
                continue
            if order.price is None:
                continue
            remaining = order.amount_base - order.filled_base
            if remaining <= 0:
                continue
            crossed = (
                (order.side is TradeType.BUY and c.low <= order.price)
                or (order.side is TradeType.SELL and c.high >= order.price)
            )
            if not crossed:
                continue
            fee = remaining * order.price * maker
            self._record_fill(order, remaining, order.price, fee)

    def accrue_funding(self) -> None:
        """Accrue one bar of funding on each held position. Received-positive: a
        SHORT (net < 0) earns funding when the rate is positive; a LONG pays."""
        rate = self.costs.funding_rate_per_bar
        if rate == 0 or self._candle is None:
            return
        mark = self._candle.close
        for pair, net in self._net_base.items():
            if net == 0 or not self._is_perp(pair):
                continue
            received = rate * (-net) * mark  # short (net<0) earns when rate>0
            if received != 0:
                self._funding_events.append((self._candle.ts, received))
                self.total_funding_quote += received

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
        self._counter += 1
        oid = f"sim-{self._counter}"
        order = NadoOrder(
            id=oid, trading_pair=trading_pair, side=side, order_type=order_type,
            amount_base=_dec(amount_base), price=_dec(price) if price is not None else None,
        )
        self._orders[oid] = order
        if order_type is OrderType.MARKET:
            mid = self._candle.close if self._candle is not None else (order.price or Decimal(0))
            slip = self.costs.slippage_pct
            fill_px = mid * (Decimal(1) + slip) if side is TradeType.BUY else mid * (Decimal(1) - slip)
            fee = order.amount_base * fill_px * self.costs.taker_fee
            self._record_fill(order, order.amount_base, fill_px, fee)
        elif self._candle is not None and self._is_marketable_limit(
            side, order_type, order.price
        ):
            # MARKETABLE LIMIT: a LIMIT priced THROUGH the touch crosses NOW, at
            # the touch, as a TAKER — it does not rest.
            #
            # This was missing, and it silently invalidated most of the harness.
            # The engine's risk exits are marketable LIMITs at mid +/- 30bp
            # (grid_executor._EXIT_CROSS_BP / _exit_cross_price), not MARKET
            # orders. Treating them as purely resting meant they filled a bar
            # late at MAKER fees — and on the _stop_out flatten path never got
            # booked at all, because _stop_out inspects filled_base only at
            # placement and never stores the id to poll, so it retried forever.
            # MEASURED (dgrid, strong_trend_down, 720 bars): 236 sim fills but
            # only ONE reached inventory; the adapter charged 5.294 while the
            # report showed 0.025 — a 527bp-of-margin blind spot that inverted
            # the sign of most dgrid and rgrid results.
            #
            # LIMIT_MAKER is deliberately excluded: post-only binds at SEND, so a
            # post-only order priced through the touch is rejected/repriced live,
            # never crossed. Only OrderType.LIMIT can take.
            mid = self._candle.close
            slip = self.costs.slippage_pct
            fill_px = (
                mid * (Decimal(1) + slip) if side is TradeType.BUY
                else mid * (Decimal(1) - slip)
            )
            fee = order.amount_base * fill_px * self.costs.taker_fee
            self._record_fill(order, order.amount_base, fill_px, fee)
        return copy.copy(order)

    def _is_marketable_limit(
        self, side: TradeType, order_type: OrderType, price: Optional[Decimal]
    ) -> bool:
        """True when a plain LIMIT is priced through the touch and must cross now."""
        if order_type is not OrderType.LIMIT or price is None or self._candle is None:
            return False
        mid = self._candle.close
        slip = self.costs.slippage_pct
        if side is TradeType.BUY:
            return price >= mid * (Decimal(1) + slip)
        return price <= mid * (Decimal(1) - slip)

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or order.state.is_terminal:
            return False
        order.state = OrderState.CANCELLED
        return True

    async def order_status(self, order_id: str) -> NadoOrder:
        order = self._orders.get(order_id)
        if order is None:
            raise AdapterError(f"unknown order {order_id}")
        return copy.copy(order)

    async def fill_stream(self, trading_pair: str) -> AsyncIterator[Fill]:
        for fill in list(self._fills):
            if fill.trading_pair == trading_pair:
                yield fill

    async def order_book(self, trading_pair: str) -> OrderBookSnapshot:
        mid = self._candle.close if self._candle is not None else Decimal(0)
        half = mid * self.costs.slippage_pct
        ts = self._candle.ts if self._candle is not None else 0.0
        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=[OrderBookLevel(mid - half, Decimal(1))],
            asks=[OrderBookLevel(mid + half, Decimal(1))],
            timestamp=ts,
        )

    async def mid_price(self, trading_pair: str) -> Decimal:
        return self._candle.close if self._candle is not None else Decimal(0)

    async def funding_since(self, trading_pair: str, since_ts: float) -> Decimal:
        return sum((amt for ts, amt in self._funding_events if ts >= since_ts), Decimal(0))

    async def funding_rate(self, trading_pair: str) -> Optional[Decimal]:
        return self.costs.funding_rate_per_bar

    def tick_size(self, trading_pair: str) -> Decimal:
        return self._m(trading_pair).tick

    def lot_size(self, trading_pair: str) -> Decimal:
        return self._m(trading_pair).lot

    def min_notional(self, trading_pair: str) -> Decimal:
        return self._m(trading_pair).min_notional
