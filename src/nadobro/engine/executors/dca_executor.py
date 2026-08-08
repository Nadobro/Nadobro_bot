"""DCA Executor — a ladder of ``amounts_quote[]`` paired with ``prices[]``.

Take-profit / stop-loss are computed against the *running weighted-average
entry* (``wae = sum(filled_quote) / sum(filled_base)``) and recomputed every
tick. MAKER posts every rung as a LIMIT up front; TAKER fires a MARKET order
for a rung when mid reaches its price. Exits follow the Triple Barrier
convention (time_limit > stop_loss > trailing_stop > take_profit).

Implemented in Phase 3.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

from src.nadobro.engine.adapter.base import Fill, NadoAdapterBase, NadoOrder, OrderState
from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import (
    CloseType,
    OrderType,
    TradeType,
    TrailingStop,
    _dec,
)


class DCAMode(Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class DCAState(Enum):
    ACCUMULATING = "ACCUMULATING"
    CLOSING = "CLOSING"
    TERMINATED = "TERMINATED"


@dataclass
class DCARung:
    price: Decimal
    amount_quote: Decimal
    amount_base: Decimal
    order_id: Optional[str] = None
    placed: bool = False
    filled: bool = False
    # Cumulative venue-side totals already booked for this rung. BUG-DCA-1
    # fix: track quote and fee separately so multi-price fills give the
    # correct marginal price/quote/fee for each delta.
    _recorded: Decimal = Decimal(0)
    _recorded_quote: Decimal = Decimal(0)
    _recorded_fee: Decimal = Decimal(0)


@dataclass
class DCAExecutorConfig:
    trading_pair: str
    amounts_quote: List[Decimal]
    prices: List[Decimal]
    side: TradeType = TradeType.BUY
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    time_limit: Optional[int] = None
    mode: DCAMode = DCAMode.MAKER
    trailing_stop: Optional[TrailingStop] = None
    activation_bounds: Optional[Decimal] = None
    leverage: int = 1
    keep_position: bool = False
    connector_name: str = "nado"

    def __post_init__(self) -> None:
        self.amounts_quote = [_dec(a) for a in self.amounts_quote]
        self.prices = [_dec(p) for p in self.prices]
        if len(self.amounts_quote) != len(self.prices):
            raise ValueError("amounts_quote and prices must have equal length")
        if not self.prices:
            raise ValueError("DCA needs at least one rung")
        if self.take_profit is not None:
            self.take_profit = _dec(self.take_profit)
        if self.stop_loss is not None:
            self.stop_loss = _dec(self.stop_loss)


class DCAExecutor(Executor):
    def __init__(
        self,
        config: DCAExecutorConfig,
        *,
        user_id: int,
        controller_id: str,
        adapter: NadoAdapterBase,
        inventory: Optional[InventoryRepository] = None,
        executor_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            user_id=user_id,
            controller_id=controller_id,
            trading_pair=config.trading_pair,
            adapter=adapter,
            inventory=inventory,
            keep_position=config.keep_position,
            executor_id=executor_id,
        )
        self.config = config
        self.rungs: List[DCARung] = [
            DCARung(price=p, amount_quote=q, amount_base=(q / p))
            for p, q in zip(config.prices, config.amounts_quote)
        ]
        self.dca_state = DCAState.ACCUMULATING
        self.filled_base = Decimal(0)
        self.filled_quote = Decimal(0)
        self.exit_quote = Decimal(0)
        self.opened_at = time.time()
        self.max_price: Optional[Decimal] = None
        self.min_price: Optional[Decimal] = None
        self.trailing_armed = False
        self.trail_level: Optional[Decimal] = None
        self.close_order: Optional[NadoOrder] = None
        self._close_recorded = Decimal(0)
        # BUG-DCA-1 / DCA-5 fix: track recorded close quote/fee separately.
        self._close_recorded_quote = Decimal(0)
        self._close_recorded_fee = Decimal(0)
        self._pending_close: Optional[CloseType] = None

    @property
    def is_long(self) -> bool:
        return self.config.side is TradeType.BUY

    @property
    def close_side(self) -> TradeType:
        return self.config.side.opposite

    @property
    def wae(self) -> Optional[Decimal]:
        return (self.filled_quote / self.filled_base) if self.filled_base > 0 else None

    # -- placement / ingest ----------------------------------------------
    async def _place_maker(self, rung: DCARung) -> None:
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair, self.config.side, OrderType.LIMIT,
                rung.amount_base, rung.price, self.config.leverage, False,
            ),
            label="dca_maker",
        )
        rung.order_id = order.id
        rung.placed = True

    async def _take_rung(self, rung: DCARung) -> None:
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair, self.config.side, OrderType.MARKET,
                rung.amount_base, None, self.config.leverage, False,
            ),
            label="dca_taker",
        )
        rung.order_id = order.id
        rung.placed = True
        self._ingest_rung(rung, order)

    def _ingest_rung(self, rung: DCARung, order: NadoOrder) -> None:
        # BUG-DCA-1 fix: ingest exact deltas from venue cumulative totals so
        # multi-price fills don't corrupt the running WAE / fees.
        delta_base = order.filled_base - rung._recorded
        if delta_base <= 0:
            return
        delta_quote = order.filled_quote - rung._recorded_quote
        delta_fee = order.fee_quote - rung._recorded_fee
        price = (delta_quote / delta_base) if delta_base > 0 else Decimal(0)
        self._record_fill(
            Fill(order.id, self.trading_pair, self.config.side, delta_base, price, delta_fee, time.time()),
            order,
        )
        rung._recorded = order.filled_base
        rung._recorded_quote = order.filled_quote
        rung._recorded_fee = order.fee_quote
        self.filled_base += delta_base
        self.filled_quote += delta_quote
        if order.state is OrderState.FILLED:
            rung.filled = True

    # -- lifecycle --------------------------------------------------------
    async def on_create(self) -> None:
        self._activate()
        if self.config.mode is DCAMode.MAKER:
            # BUG-DCA-2 fix: if any rung placement fails part-way, cancel
            # the ones we already placed so we don't leak open orders on the
            # venue while the executor is marked FAILED.
            placed_ids: List[str] = []
            try:
                for rung in self.rungs:
                    await self._place_maker(rung)
                    if rung.order_id is not None:
                        placed_ids.append(rung.order_id)
            except Exception:
                for oid in placed_ids:
                    captured = oid
                    try:
                        await self._guard(
                            lambda: self.adapter.cancel_order(captured),
                            label="dca_rollback_cancel",
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "dca %s: rollback cancel failed for %s — order may still be resting: %s",
                            self.id, captured, exc,
                        )
                raise

    async def on_tick(self) -> None:
        if self.is_terminated:
            return
        if self.dca_state is DCAState.CLOSING:
            await self._poll_close()
            return
        mid = await self._guard(lambda: self.adapter.mid_price(self.trading_pair), label="dca_mid")
        if self.config.mode is DCAMode.MAKER:
            for rung in self.rungs:
                if rung.placed and not rung.filled and rung.order_id is not None:
                    oid = rung.order_id
                    order = await self._guard(lambda: self.adapter.order_status(oid), label="dca_status")
                    self._ingest_rung(rung, order)
        else:  # TAKER: fire a rung when mid reaches it
            for rung in self.rungs:
                if rung.placed:
                    continue
                if (self.is_long and mid <= rung.price) or (not self.is_long and mid >= rung.price):
                    await self._take_rung(rung)
        hit = self._evaluate_barriers(mid)
        if hit is not None:
            self._pending_close = hit
            await self._open_close()

    # -- barriers ---------------------------------------------------------
    def _evaluate_barriers(self, mid: Decimal) -> Optional[CloseType]:
        wae = self.wae
        if wae is None:
            return None
        c = self.config
        if c.time_limit is not None and (time.time() - self.opened_at) >= c.time_limit:
            return CloseType.TIME_LIMIT
        if c.stop_loss is not None:
            if self.is_long and mid <= wae * (Decimal(1) - c.stop_loss):
                return CloseType.STOP_LOSS
            if not self.is_long and mid >= wae * (Decimal(1) + c.stop_loss):
                return CloseType.STOP_LOSS
        if c.trailing_stop is not None:
            self._update_trailing(mid, wae, c.trailing_stop)
            if self.trailing_armed and self.trail_level is not None:
                if self.is_long and mid <= self.trail_level:
                    return CloseType.TRAILING_STOP
                if not self.is_long and mid >= self.trail_level:
                    return CloseType.TRAILING_STOP
        if c.take_profit is not None:
            if self.is_long and mid >= wae * (Decimal(1) + c.take_profit):
                return CloseType.TAKE_PROFIT
            if not self.is_long and mid <= wae * (Decimal(1) - c.take_profit):
                return CloseType.TAKE_PROFIT
        return None

    def _update_trailing(self, mid: Decimal, wae: Decimal, ts: TrailingStop) -> None:
        if self.is_long:
            self.max_price = max(self.max_price or mid, mid)
            if not self.trailing_armed and (mid - wae) / wae >= ts.activation_price:
                self.trailing_armed = True
            if self.trailing_armed:
                self.trail_level = self.max_price * (Decimal(1) - ts.trailing_delta)
        else:
            self.min_price = min(self.min_price or mid, mid)
            if not self.trailing_armed and (wae - mid) / wae >= ts.activation_price:
                self.trailing_armed = True
            if self.trailing_armed:
                self.trail_level = self.min_price * (Decimal(1) + ts.trailing_delta)

    # -- close ------------------------------------------------------------
    async def _open_close(self) -> None:
        self.dca_state = DCAState.CLOSING
        if self.filled_base <= 0:
            self._finalize(self._pending_close or CloseType.COMPLETED)
            return
        await self._cancel_open_rungs()
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair, self.close_side, OrderType.MARKET,
                self.filled_base, None, self.config.leverage, True,
            ),
            label="dca_close",
        )
        self.close_order = order
        self._ingest_close(order)
        if order.state is OrderState.FILLED:
            self._finalize(self._pending_close or CloseType.COMPLETED)

    async def _cancel_open_rungs(self) -> None:
        # BUG-DCA-4 fix: before cancelling, poll each open rung once so any
        # partial fill that arrived between the last tick and this stop call
        # is ingested into inventory before the cancel removes it from the book.
        for rung in self.rungs:
            if rung.placed and not rung.filled and rung.order_id is not None:
                oid = rung.order_id
                try:
                    pre = await self._guard(
                        lambda: self.adapter.order_status(oid), label="dca_pre_cancel_status",
                    )
                    self._ingest_rung(rung, pre)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dca %s: pre-cancel status failed for %s — last fills may be unbooked: %s",
                        self.id, oid, exc,
                    )
                try:
                    await self._guard(lambda: self.adapter.cancel_order(oid), label="dca_cancel")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dca %s: stop cancel failed for %s — order may still be resting: %s",
                        self.id, oid, exc,
                    )

    async def _poll_close(self) -> None:
        if self.close_order is None:
            return
        cid = self.close_order.id
        order = await self._guard(lambda: self.adapter.order_status(cid), label="dca_close_status")
        self.close_order = order
        self._ingest_close(order)
        if order.state is OrderState.FILLED:
            self._finalize(self._pending_close or CloseType.COMPLETED)

    def _ingest_close(self, order: NadoOrder) -> None:
        delta_base = order.filled_base - self._close_recorded
        if delta_base <= 0:
            return
        delta_quote = order.filled_quote - self._close_recorded_quote
        delta_fee = order.fee_quote - self._close_recorded_fee
        price = (delta_quote / delta_base) if delta_base > 0 else Decimal(0)
        self._record_fill(
            Fill(order.id, self.trading_pair, self.close_side, delta_base, price, delta_fee, time.time()),
            order,
        )
        self._close_recorded = order.filled_base
        self._close_recorded_quote = order.filled_quote
        self._close_recorded_fee = order.fee_quote
        self.exit_quote += delta_quote

    def _finalize(self, close_type: CloseType) -> None:
        # Proportional realized PnL — only book the closed portion.
        if self.filled_base > 0:
            avg_entry = self.filled_quote / self.filled_base
        else:
            avg_entry = Decimal(0)
        closed_base = self._close_recorded
        if self.is_long:
            gross = self.exit_quote - (avg_entry * closed_base)
        else:
            gross = (avg_entry * closed_base) - self.exit_quote
        self._net_pnl_quote = gross - self._fees_paid_quote
        self.dca_state = DCAState.TERMINATED
        self._terminate(close_type)

    async def on_stop(self, close_type: CloseType = CloseType.EARLY_STOP) -> None:
        if self.is_terminated:
            return
        if self._pending_close is None:
            self._pending_close = close_type
        if self.config.keep_position:
            await self._cancel_open_rungs()
            self.dca_state = DCAState.TERMINATED
            self._terminate(close_type)
            return
        # BUG-DCA-3 fix: if a close order is already in flight, just poll it
        # rather than spawning a SECOND market close which would double-flatten.
        if self.dca_state is DCAState.CLOSING and self.close_order is not None:
            await self._poll_close()
            return
        await self._open_close()
        if not self.is_terminated:
            # Leave in CLOSING; subsequent ticks (or controller stop loop) will
            # poll until the close order is terminal. Terminating here would
            # leak the open close order on the venue.
            pass
