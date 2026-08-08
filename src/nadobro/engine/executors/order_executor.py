"""Patched: engine/executors/order_executor.py

Fixes applied (search for AUDIT-FIX):
  AUDIT-FIX-OE-1: LIMIT_CHASER refresh used to cancel the resting order and
                  then call self._place(...). If _place() raised (e.g. venue
                  rejected the new quote), the executor was left with
                  self.order == None and on_tick() early-returns forever — a
                  silent leak that never reaches the chaser's max_refreshes
                  cap. Now we mark the executor FAILED so the orchestrator's
                  stop loop can intervene and a human can recover the user's
                  exposure.
  AUDIT-FIX-OE-2: _ingest now logs (at debug) when the adapter sends a
                  filled_base that regresses below our accumulator. That can
                  happen with stale snapshots and is otherwise invisible,
                  which makes it impossible to diagnose missed fills.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.nadobro.engine.adapter.base import Fill, NadoAdapterBase, NadoOrder, OrderState
from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import (
    CloseType,
    ExecutionStrategy,
    LimitChaserConfig,
    OrderType,
    PositionAction,
    TradeType,
    _dec,
)

logger = logging.getLogger(__name__)


@dataclass
class OrderExecutorConfig:
    trading_pair: str
    side: TradeType
    amount_base: Decimal
    execution_strategy: ExecutionStrategy
    price: Optional[Decimal] = None
    chaser_config: Optional[LimitChaserConfig] = None
    leverage: int = 1
    position_action: PositionAction = PositionAction.OPEN
    connector_name: str = "nado"
    # Declared crossing intent, for an order priced THROUGH the book that stays a
    # limit order. ``execution_strategy`` cannot express it — a plain LIMIT rests or
    # crosses depending on its price against the book — and only the controller that
    # chose the price knows which it meant. Read by ``_fill_was_taker`` so the
    # recorded fill is not mislabelled maker (RG-TAKERFLAG-1). Reporting only: it
    # changes no order parameter and is never sent to the venue.
    crosses_book: bool = False

    def __post_init__(self) -> None:
        self.amount_base = _dec(self.amount_base)
        if self.price is not None:
            self.price = _dec(self.price)
        if self.execution_strategy in (
            ExecutionStrategy.LIMIT,
            ExecutionStrategy.LIMIT_MAKER,
        ) and self.price is None:
            raise ValueError(f"{self.execution_strategy.value} requires a price")
        if self.execution_strategy is ExecutionStrategy.LIMIT_CHASER and self.chaser_config is None:
            raise ValueError("LIMIT_CHASER requires a chaser_config")


_STRATEGY_TO_ORDER_TYPE = {
    ExecutionStrategy.LIMIT: OrderType.LIMIT,
    ExecutionStrategy.LIMIT_MAKER: OrderType.LIMIT_MAKER,
    ExecutionStrategy.MARKET: OrderType.MARKET,
    ExecutionStrategy.LIMIT_CHASER: OrderType.LIMIT_MAKER,
}


class OrderExecutor(Executor):
    def __init__(
        self,
        config: OrderExecutorConfig,
        *,
        user_id: int,
        controller_id: str,
        adapter: NadoAdapterBase,
        inventory: Optional[InventoryRepository] = None,
        keep_position: bool = True,
        executor_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            user_id=user_id,
            controller_id=controller_id,
            trading_pair=config.trading_pair,
            adapter=adapter,
            inventory=inventory,
            keep_position=keep_position,
            executor_id=executor_id,
        )
        self.config = config
        self.order: Optional[NadoOrder] = None
        self.last_fill: Optional[Fill] = None
        self.refreshes = 0
        self.orders_placed = 0
        # COUNTERS-FIX (2026-07-31): orders_filled / orders_cancelled were
        # never defined here, so Controller.order_counts() getattr-defaulted
        # them to 0 and /status reported "filled 0 | cancelled 0" forever for
        # every OrderExecutor-based strategy (mid/MM quotes) — even while real
        # cancels were happening. GridExecutor already tracks all three.
        self.orders_filled = 0
        self.orders_cancelled = 0
        # One count per venue order: _place() resets this, the first terminal
        # observation (fill/cancel/reject) sets it. The chaser cycles through
        # several orders per executor, hence per-order (not per-executor).
        self._terminal_counted = True
        self._placement_mid: Optional[Decimal] = None
        self._recorded_base = Decimal(0)
        self._recorded_quote = Decimal(0)
        self._recorded_fee = Decimal(0)

    # -- placement helpers ------------------------------------------------
    async def _chaser_price(self) -> Decimal:
        mid = await self._guard(
            lambda: self.adapter.mid_price(self.trading_pair), label="mid_price"
        )
        self._placement_mid = mid
        dist = self.config.chaser_config.distance  # type: ignore[union-attr]
        if self.config.side is TradeType.BUY:
            return mid * (Decimal(1) - dist)
        return mid * (Decimal(1) + dist)

    async def _place(self, order_type: OrderType, price: Optional[Decimal]) -> None:
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair,
                self.config.side,
                order_type,
                self.config.amount_base,
                price,
                self.config.leverage,
                self.config.position_action is PositionAction.CLOSE,
            ),
            label="place_order",
        )
        self.order = order
        self.orders_placed += 1
        self._terminal_counted = False
        self._ingest(order)

    def _count_terminal_order(self) -> None:
        """Count the current order's terminal outcome exactly once.

        FILLED → orders_filled; CANCELLED/REJECTED → orders_cancelled. Called
        from every place a terminal state is observed (_check_complete,
        on_stop, the chaser's explicit cancel) — the flag makes multiple
        observations of the same terminal order idempotent.
        """
        if self._terminal_counted or self.order is None:
            return
        if self.order.state is OrderState.FILLED:
            self.orders_filled += 1
            self._terminal_counted = True
        elif self.order.state in (OrderState.CANCELLED, OrderState.REJECTED):
            self.orders_cancelled += 1
            self._terminal_counted = True

    def _ingest(self, order: NadoOrder) -> None:
        """Record any newly-filled quantity since the last poll."""
        delta_base = order.filled_base - self._recorded_base
        if delta_base <= 0:
            # AUDIT-FIX-OE-2: a stale snapshot can have filled_base < accumulator.
            # Surface this so we can spot adapter misbehavior without raising.
            if delta_base < 0:
                logger.debug(
                    "OrderExecutor _ingest saw regressed filled_base for %s "
                    "(snapshot=%s, accumulator=%s)",
                    order.id, order.filled_base, self._recorded_base,
                )
            return
        delta_quote = order.filled_quote - self._recorded_quote
        delta_fee = order.fee_quote - self._recorded_fee
        price = (delta_quote / delta_base) if delta_base else Decimal(0)
        fill = Fill(
            order_id=order.id,
            trading_pair=self.trading_pair,
            side=self.config.side,
            amount_base=delta_base,
            price=price,
            fee_quote=delta_fee,
            timestamp=time.time(),
        )
        self._record_fill(fill, order)
        self.last_fill = fill
        self._recorded_base = order.filled_base
        self._recorded_quote = order.filled_quote
        self._recorded_fee = order.fee_quote

    # -- lifecycle --------------------------------------------------------
    async def on_create(self) -> None:
        self._activate()
        strat = self.config.execution_strategy
        if strat is ExecutionStrategy.MARKET:
            await self._place(OrderType.MARKET, None)
        elif strat is ExecutionStrategy.LIMIT_CHASER:
            price = await self._chaser_price()
            await self._place(OrderType.LIMIT_MAKER, price)
        else:
            await self._place(_STRATEGY_TO_ORDER_TYPE[strat], self.config.price)
        await self._check_complete()

    async def on_tick(self) -> None:
        if self.is_terminated or self.order is None:
            return
        order_ref = self.order
        order = await self._guard(
            lambda: self.adapter.order_status(order_ref.id), label="order_status"
        )
        self.order = order
        self._ingest(order)
        if await self._check_complete():
            return
        if self.config.execution_strategy is ExecutionStrategy.LIMIT_CHASER:
            await self._maybe_refresh()

    async def _check_complete(self) -> bool:
        if self.order is not None and self.order.state is OrderState.FILLED:
            self._count_terminal_order()
            self._terminate(CloseType.COMPLETED)
            return True
        if self.order is not None and self.order.state in (
            OrderState.CANCELLED,
            OrderState.REJECTED,
        ) and self.config.execution_strategy is not ExecutionStrategy.LIMIT_CHASER:
            self._count_terminal_order()
            self._terminate(CloseType.EARLY_STOP)
            return True
        return False

    async def _maybe_refresh(self) -> None:
        cfg = self.config.chaser_config
        assert cfg is not None
        if self.refreshes >= cfg.max_refreshes or self.order is None:
            return
        order_ref = self.order
        mid = await self._guard(
            lambda: self.adapter.mid_price(self.trading_pair), label="mid_price"
        )
        if self._placement_mid is None or self._placement_mid == 0:
            self._placement_mid = mid
            return
        move = abs(mid - self._placement_mid) / self._placement_mid
        if move > cfg.refresh_threshold:
            # AUDIT-FIX-OE-1: cancel + place must be tracked together. If
            # cancel succeeds and place fails, the executor would otherwise
            # be left holding no order and never terminate, which silently
            # parks the user's intent and abandons the chaser. We now mark
            # the executor FAILED so the orchestrator sees the leak.
            try:
                await self._guard(
                    lambda: self.adapter.cancel_order(order_ref.id), label="cancel_order"
                )
            except Exception:  # noqa: BLE001 - placement guard below handles the rest
                logger.warning(
                    "OrderExecutor chaser cancel failed for %s; aborting refresh",
                    order_ref.id, exc_info=True,
                )
                self._terminate(CloseType.FAILED)
                return
            # The refresh cancelled a live venue order; count it here — the
            # stale snapshot in self.order won't show CANCELLED before _place
            # swaps in the replacement order.
            if not self._terminal_counted:
                self.orders_cancelled += 1
                self._terminal_counted = True
            try:
                price = await self._chaser_price()
                await self._place(OrderType.LIMIT_MAKER, price)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "OrderExecutor chaser repost failed after cancel for %s; "
                    "executor is now bare — terminating FAILED so the orchestrator "
                    "can reconcile",
                    order_ref.id, exc_info=True,
                )
                self.order = None
                self._terminate(CloseType.FAILED)
                return
            self.refreshes += 1

    async def on_stop(self, close_type: CloseType = CloseType.EARLY_STOP) -> None:
        order = self.order
        if order is None or order.state in (
            OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
        ):
            self._count_terminal_order()
            self._terminate(close_type)
            return

        try:
            await self._guard(
                lambda: self.adapter.cancel_order(order.id),
                label="cancel_order_stop",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "order-executor %s: stop cancel failed for %s — order may still be resting: %s",
                self.id, order.id, exc,
            )

        try:
            refreshed = await self._guard(
                lambda: self.adapter.order_status(order.id),
                label="cancel_confirm_status",
            )
            self.order = refreshed
            self._ingest(refreshed)
        except Exception:  # noqa: BLE001
            self._terminate(CloseType.FAILED)
            return

        if self.order is None or self.order.state in (
            OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED,
        ):
            self._count_terminal_order()
            self._terminate(close_type)
            return

        self._terminate(CloseType.FAILED)
