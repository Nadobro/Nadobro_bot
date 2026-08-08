"""Position Executor — places an entry order and manages the resulting
position using the Triple Barrier method (take_profit, stop_loss, time_limit,
optional trailing_stop with activation_price + trailing_delta).

Internal states: OPENING -> ACTIVE_POSITION -> CLOSING -> TERMINATED. Barrier
evaluation order on each tick: time_limit, stop_loss, trailing_stop (if
armed), take_profit. The first barrier hit wins; the close order uses that
barrier's order type and ``close_type`` is set accordingly.

The closing order is placed directly through the adapter with the barrier's
order type (Phase 1 keeps PositionExecutor self-contained rather than nesting
an OrderExecutor; the order *type* per barrier is honored).

Implemented in Phase 1.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

from src.nadobro.engine.adapter.base import Fill, NadoAdapterBase, NadoOrder, OrderState
from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.executors.order_executor import OrderExecutorConfig
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import (
    CloseType,
    ExecutionStrategy,
    OrderType,
    PositionAction,
    TradeType,
    TripleBarrierConfig,
    _dec,
)


class PositionExecState(Enum):
    OPENING = "OPENING"
    ACTIVE_POSITION = "ACTIVE_POSITION"
    CLOSING = "CLOSING"
    TERMINATED = "TERMINATED"


@dataclass
class PositionExecutorConfig:
    order_config: OrderExecutorConfig
    barriers: TripleBarrierConfig


_STRATEGY_TO_ORDER_TYPE = {
    ExecutionStrategy.LIMIT: OrderType.LIMIT,
    ExecutionStrategy.LIMIT_MAKER: OrderType.LIMIT_MAKER,
    ExecutionStrategy.MARKET: OrderType.MARKET,
    ExecutionStrategy.LIMIT_CHASER: OrderType.LIMIT_MAKER,
}


class PositionExecutor(Executor):
    def __init__(
        self,
        config: PositionExecutorConfig,
        *,
        user_id: int,
        controller_id: str,
        adapter: NadoAdapterBase,
        inventory: Optional[InventoryRepository] = None,
        keep_position: bool = False,
        executor_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            user_id=user_id,
            controller_id=controller_id,
            trading_pair=config.order_config.trading_pair,
            adapter=adapter,
            inventory=inventory,
            keep_position=keep_position,
            executor_id=executor_id,
        )
        self.config = config
        self.barriers = config.barriers
        self.position_state = PositionExecState.OPENING
        self.entry_order: Optional[NadoOrder] = None
        self.close_order: Optional[NadoOrder] = None
        self.entry_price = Decimal(0)
        self.entry_base = Decimal(0)
        self.entry_quote = Decimal(0)
        self.exit_quote = Decimal(0)
        self.exit_base = Decimal(0)
        self.opened_at: Optional[float] = None
        self.max_price: Optional[Decimal] = None
        self.min_price: Optional[Decimal] = None
        self.trailing_armed = False
        self.trail_stop_level: Optional[Decimal] = None
        self._pending_close_type: Optional[CloseType] = None
        self._e_base = Decimal(0)
        self._e_quote = Decimal(0)
        self._e_fee = Decimal(0)
        self._x_base = Decimal(0)
        self._x_quote = Decimal(0)
        self._x_fee = Decimal(0)

    @property
    def is_long(self) -> bool:
        return self.config.order_config.side is TradeType.BUY

    @property
    def exit_side(self) -> TradeType:
        return self.config.order_config.side.opposite

    # -- fill ingestion ---------------------------------------------------
    def _ingest_entry(self, order: NadoOrder) -> None:
        delta_base = order.filled_base - self._e_base
        if delta_base <= 0:
            return
        delta_quote = order.filled_quote - self._e_quote
        delta_fee = order.fee_quote - self._e_fee
        price = delta_quote / delta_base
        self._record_fill(
            Fill(
                order.id,
                self.trading_pair,
                self.config.order_config.side,
                delta_base,
                price,
                delta_fee,
                time.time(),
            ),
            order,
        )
        self._e_base, self._e_quote, self._e_fee = (
            order.filled_base,
            order.filled_quote,
            order.fee_quote,
        )
        self.entry_base = order.filled_base
        self.entry_quote = order.filled_quote

    def _ingest_exit(self, order: NadoOrder) -> None:
        delta_base = order.filled_base - self._x_base
        if delta_base <= 0:
            return
        delta_quote = order.filled_quote - self._x_quote
        delta_fee = order.fee_quote - self._x_fee
        price = delta_quote / delta_base
        self._record_fill(
            Fill(
                order.id,
                self.trading_pair,
                self.exit_side,
                delta_base,
                price,
                delta_fee,
                time.time(),
            ),
            order,
        )
        self._x_base, self._x_quote, self._x_fee = (
            order.filled_base,
            order.filled_quote,
            order.fee_quote,
        )
        self.exit_base = order.filled_base
        self.exit_quote = order.filled_quote

    # -- lifecycle --------------------------------------------------------
    async def on_create(self) -> None:
        self._activate()
        oc = self.config.order_config
        order_type = (
            self.barriers.open_order_type
            if oc.execution_strategy
            in (ExecutionStrategy.LIMIT, ExecutionStrategy.LIMIT_MAKER)
            else _STRATEGY_TO_ORDER_TYPE[oc.execution_strategy]
        )
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair,
                oc.side,
                order_type,
                oc.amount_base,
                oc.price,
                oc.leverage,
                False,
            ),
            label="place_entry",
        )
        self.entry_order = order
        self._ingest_entry(order)
        if order.state is OrderState.FILLED:
            self._on_entry_filled()

    def _on_entry_filled(self) -> None:
        if self.entry_base <= 0:
            return
        self.entry_price = self.entry_quote / self.entry_base
        self.opened_at = time.time()
        self.max_price = self.entry_price
        self.min_price = self.entry_price
        self.position_state = PositionExecState.ACTIVE_POSITION

    async def on_tick(self) -> None:
        if self.is_terminated:
            return
        if self.position_state is PositionExecState.OPENING:
            if self.entry_order is None:
                return
            entry_ref = self.entry_order
            order = await self._guard(
                lambda: self.adapter.order_status(entry_ref.id),
                label="entry_status",
            )
            self.entry_order = order
            self._ingest_entry(order)
            if order.state is OrderState.FILLED:
                self._on_entry_filled()
                return
            # BUG-PE-3 fix: terminal non-FILLED states (CANCELLED, REJECTED)
            # must not leave the executor stuck in OPENING forever.
            if order.state in (OrderState.CANCELLED, OrderState.REJECTED):
                if self.entry_base > 0:
                    # Partial fill before cancel/reject -> open the position
                    # with what we got; barriers will close the rest.
                    self._on_entry_filled()
                else:
                    self._terminate(CloseType.FAILED)
            return
        if self.position_state is PositionExecState.ACTIVE_POSITION:
            price = await self._guard(
                lambda: self.adapter.mid_price(self.trading_pair), label="mid_price"
            )
            hit = self._evaluate_barriers(price)
            if hit is not None:
                close_type, order_type, trigger_price = hit
                self._pending_close_type = close_type
                await self._open_close(order_type, trigger_price)
            return
        if self.position_state is PositionExecState.CLOSING:
            await self._poll_close()

    # -- barriers ---------------------------------------------------------
    def _evaluate_barriers(
        self, price: Decimal
    ) -> Optional[Tuple[CloseType, OrderType, Decimal]]:
        """Return (close_type, order_type, trigger_price) for the first
        barrier hit, or None. The trigger_price is the actual barrier level
        (not the current mid and not entry_price) so non-MARKET closes can
        post a meaningful limit order — fixes BUG-PE-1.
        """
        b = self.barriers
        e = self.entry_price
        if e <= 0:
            return None
        long = self.is_long
        now = time.time()
        if b.time_limit is not None and self.opened_at is not None:
            if (now - self.opened_at) >= b.time_limit:
                # Time limit fires at the prevailing mid — fall back to mid
                # because there's no "target price" for a time-based exit.
                return CloseType.TIME_LIMIT, b.time_limit_order_type, price
        if b.stop_loss is not None:
            sl_long = e * (Decimal(1) - b.stop_loss)
            sl_short = e * (Decimal(1) + b.stop_loss)
            if long and price <= sl_long:
                return CloseType.STOP_LOSS, b.stop_loss_order_type, sl_long
            if not long and price >= sl_short:
                return CloseType.STOP_LOSS, b.stop_loss_order_type, sl_short
        if b.trailing_stop is not None:
            self._update_trailing(price)
            if self.trailing_armed and self.trail_stop_level is not None:
                if long and price <= self.trail_stop_level:
                    return (
                        CloseType.TRAILING_STOP,
                        b.trailing_stop_order_type,
                        self.trail_stop_level,
                    )
                if not long and price >= self.trail_stop_level:
                    return (
                        CloseType.TRAILING_STOP,
                        b.trailing_stop_order_type,
                        self.trail_stop_level,
                    )
        if b.take_profit is not None:
            tp_long = e * (Decimal(1) + b.take_profit)
            tp_short = e * (Decimal(1) - b.take_profit)
            if long and price >= tp_long:
                return CloseType.TAKE_PROFIT, b.take_profit_order_type, tp_long
            if not long and price <= tp_short:
                return CloseType.TAKE_PROFIT, b.take_profit_order_type, tp_short
        return None

    def _update_trailing(self, price: Decimal) -> None:
        ts = self.barriers.trailing_stop
        if ts is None:
            return
        e = self.entry_price
        if self.is_long:
            self.max_price = max(self.max_price or price, price)
            if not self.trailing_armed and (price - e) / e >= ts.activation_price:
                self.trailing_armed = True
            if self.trailing_armed:
                self.trail_stop_level = self.max_price * (Decimal(1) - ts.trailing_delta)
        else:
            self.min_price = min(self.min_price or price, price)
            if not self.trailing_armed and (e - price) / e >= ts.activation_price:
                self.trailing_armed = True
            if self.trailing_armed:
                self.trail_stop_level = self.min_price * (Decimal(1) + ts.trailing_delta)

    # -- closing ----------------------------------------------------------
    async def _open_close(
        self, order_type: OrderType, trigger_price: Optional[Decimal] = None
    ) -> None:
        """Place the close order. ``trigger_price`` is the barrier's target
        price (e.g. TP/SL level) and is used as the limit price for non-MARKET
        closes — BUG-PE-1 fix. Falls back to entry_price for callers that
        don't supply one (only ``on_stop`` does that, and it uses MARKET).
        """
        self.position_state = PositionExecState.CLOSING
        remaining = self.entry_base - self.exit_base
        if remaining <= 0:
            self._finalize(self._pending_close_type or CloseType.COMPLETED)
            return
        if order_type is OrderType.MARKET:
            limit_price: Optional[Decimal] = None
        else:
            limit_price = trigger_price if trigger_price is not None else self.entry_price
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair,
                self.exit_side,
                order_type,
                remaining,
                limit_price,
                self.config.order_config.leverage,
                True,
            ),
            label="place_close",
        )
        self.close_order = order
        self._ingest_exit(order)
        if order.state is OrderState.FILLED:
            self._finalize(self._pending_close_type or CloseType.COMPLETED)
            return
        # BUG-PE-4 fix: a CANCELLED/REJECTED close order must not leave the
        # executor stuck in CLOSING. Re-attempt a MARKET close to flatten the
        # residual; if that also fails, mark FAILED so the controller can act.
        if order.state in (OrderState.CANCELLED, OrderState.REJECTED):
            await self._escalate_close()

    async def reduce_position(self, base_amount: Decimal) -> Decimal:
        """Partially reduce an open position by ``base_amount`` (base units) via
        a reduce-only MARKET order, staying in ACTIVE_POSITION. Used by the
        Delta Neutral controller to trim the over-hedged leg when the other leg
        underfills, so the hedge stays balanced instead of leaning on the drift
        gate. Returns the base actually reduced (0 if not applicable)."""
        if self.position_state is not PositionExecState.ACTIVE_POSITION:
            return Decimal(0)
        remaining = self.entry_base - self.exit_base
        amount = min(_dec(base_amount), remaining)
        if amount <= 0:
            return Decimal(0)
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair,
                self.exit_side,
                OrderType.MARKET,
                amount,
                None,
                self.config.order_config.leverage,
                True,
            ),
            label="reduce_position",
        )
        before = self.exit_base
        self._ingest_exit(order)
        return self.exit_base - before

    async def _escalate_close(self) -> None:
        """Promote a stuck close to a MARKET order. Used when the venue
        rejects/cancels a limit close (BUG-PE-4)."""
        remaining = self.entry_base - self.exit_base
        if remaining <= 0:
            self._finalize(self._pending_close_type or CloseType.COMPLETED)
            return
        try:
            order = await self._guard(
                lambda: self.adapter.place_order(
                    self.trading_pair,
                    self.exit_side,
                    OrderType.MARKET,
                    remaining,
                    None,
                    self.config.order_config.leverage,
                    True,
                ),
                label="escalate_close_market",
            )
        except Exception:  # noqa: BLE001
            self._terminate(CloseType.FAILED)
            return
        self.close_order = order
        self._ingest_exit(order)
        if order.state is OrderState.FILLED:
            self._finalize(self._pending_close_type or CloseType.COMPLETED)
        else:
            # Leave in CLOSING; subsequent ticks will poll. on_stop will
            # re-confirm cancellation.
            self.position_state = PositionExecState.CLOSING

    async def _poll_close(self) -> None:
        if self.close_order is None:
            return
        close_ref = self.close_order
        order = await self._guard(
            lambda: self.adapter.order_status(close_ref.id),
            label="close_status",
        )
        self.close_order = order
        self._ingest_exit(order)
        if order.state is OrderState.FILLED:
            self._finalize(self._pending_close_type or CloseType.COMPLETED)
            return
        if order.state in (OrderState.CANCELLED, OrderState.REJECTED):
            # BUG-PE-4 fix
            await self._escalate_close()

    def _finalize(self, close_type: CloseType) -> None:
        # BUG-PE-5 fix: realized PnL must be proportional to closed base, not
        # subtract the FULL entry_quote even when exit_base < entry_base.
        # avg_entry_price = entry_quote / entry_base
        # realized_pnl_long = exit_quote - avg_entry_price * exit_base
        # (and the symmetric form for short positions)
        if self.entry_base > 0:
            avg_entry = self.entry_quote / self.entry_base
        else:
            avg_entry = Decimal(0)
        if self.is_long:
            gross = self.exit_quote - (avg_entry * self.exit_base)
        else:
            gross = (avg_entry * self.exit_base) - self.exit_quote
        self._net_pnl_quote = gross - self._fees_paid_quote
        self.position_state = PositionExecState.TERMINATED
        self._terminate(close_type)

    async def on_stop(self, close_type: CloseType = CloseType.EARLY_STOP) -> None:
        if self.is_terminated:
            return
        # BUG-PE-6 fix: preserve any pending barrier-driven close_type rather
        # than always overwriting with EARLY_STOP. If a barrier fired this tick
        # and on_stop also fired (race), the audit log should reflect the
        # barrier, not the stop.
        if self._pending_close_type is None:
            self._pending_close_type = close_type
        # AUDIT-FIX-PE-1: confirm cancel actually removed the entry order
        # from the venue before terminating. Combined with the adapter fix
        # that surfaces silent cancel failures (engine/adapter/nado.py
        # AUDIT-FIX-1), we must round-trip a status probe and only terminate
        # if the order is now CANCELLED / REJECTED / FILLED. If it is still
        # OPEN we mark the executor FAILED so the orchestrator's stop loop
        # reconciles the residual instead of leaking an orphan entry order.
        if self.position_state is PositionExecState.OPENING:
            entry = self.entry_order
            if entry is not None and entry.state is OrderState.OPEN:
                try:
                    await self._guard(
                        lambda: self.adapter.cancel_order(entry.id),
                        label="cancel_entry",
                    )
                except Exception as exc:  # noqa: BLE001 - status probe below reconciles
                    logger.warning(
                        "position %s: entry cancel failed for %s — relying on status probe: %s",
                        self.id, entry.id, exc,
                    )
                # Always re-probe so we ingest any final fills and learn the
                # real venue state.
                try:
                    refreshed = await self._guard(
                        lambda: self.adapter.order_status(entry.id),
                        label="cancel_entry_status",
                    )
                    self.entry_order = refreshed
                    self._ingest_entry(refreshed)
                except Exception:  # noqa: BLE001
                    # Can't confirm — fail FAILED instead of EARLY_STOP so
                    # the orchestrator knows venue state is unknown.
                    self._terminate(CloseType.FAILED)
                    return
                if (
                    self.entry_order is None
                    or self.entry_order.state in (
                        OrderState.FILLED,
                        OrderState.CANCELLED,
                        OrderState.REJECTED,
                    )
                ):
                    if (
                        self.entry_order is not None
                        and self.entry_order.state is OrderState.FILLED
                        and self.entry_base > 0
                    ):
                        # Partial/full fill before cancel landed — hand off
                        # into the ACTIVE_POSITION path so barriers / close
                        # logic can flatten the residual position.
                        self._on_entry_filled()
                        return
                    self._terminate(close_type)
                    return
                # Order still resting after cancel attempt — surface FAILED
                # so a supervisor / human can reconcile the orphan.
                self._terminate(CloseType.FAILED)
                return
            self._terminate(close_type)
            return
        if self.position_state is PositionExecState.ACTIVE_POSITION:
            if self.keep_position:
                self.position_state = PositionExecState.TERMINATED
                self._terminate(close_type)
                return
            await self._open_close(OrderType.MARKET)
            if not self.is_terminated:
                # Close did not fill synchronously; do not terminate now or we
                # leak the open close order on the venue (BUG-PE-2). Leave in
                # CLOSING; subsequent ticks (or the orchestrator's stop loop)
                # will poll until terminal.
                pass
            return
        if self.position_state is PositionExecState.CLOSING:
            # BUG-PE-2 fix: do NOT terminate if the close order is still open
            # — that would leak the resting order on the venue. Cancel the
            # existing close first, then escalate to MARKET. If MARKET also
            # doesn't fill synchronously, leave CLOSING for subsequent polls.
            existing = self.close_order
            if existing is not None and existing.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                try:
                    await self._guard(
                        lambda: self.adapter.cancel_order(existing.id),
                        label="cancel_close_on_stop",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "position %s: close-order cancel failed for %s — "
                        "order may still be resting before escalation: %s",
                        self.id, existing.id, exc,
                    )
                # Refresh state once after cancel
                try:
                    refreshed = await self._guard(
                        lambda: self.adapter.order_status(existing.id),
                        label="cancel_close_status",
                    )
                    self.close_order = refreshed
                    self._ingest_exit(refreshed)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "position %s: post-cancel close status failed for %s — "
                        "exit fills may be unbooked before escalation: %s",
                        self.id, existing.id, exc,
                    )
            if not self.is_terminated:
                await self._escalate_close()
            return
        self._terminate(close_type)
