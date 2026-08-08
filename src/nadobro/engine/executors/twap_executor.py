"""TWAP Executor — slices ``total_amount_quote`` across
``total_duration / order_interval`` and works the slices over time.

- ``n_orders = total_duration // order_interval``;
  ``amount_per_order_quote = total_amount_quote / n_orders``.
- MAKER posts each slice at mid; if it is unfilled when the next slice comes
  due it is cancelled and rolled forward (the slice is *lost*, not retried).
- TAKER fires a MARKET order at each scheduled time.
- Metrics: ``average_executed_price`` (VWAP), ``filled_amount_base``,
  ``cumulative_fees``, ``trade_pnl`` (vs price at t0), ``net_pnl``.

Slice scheduling is derived from ``start_ts`` (mutable so tests can advance
the clock deterministically).

Implemented in Phase 3.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional

logger = logging.getLogger(__name__)

from src.nadobro.engine.adapter.base import Fill, NadoAdapterBase, NadoOrder, OrderState
from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import CloseType, OrderType, TradeType, _dec


@dataclass
class TWAPExecutorConfig:
    trading_pair: str
    side: TradeType
    total_amount_quote: Decimal
    total_duration: float
    order_interval: float
    mode: str = "MAKER"  # "MAKER" | "TAKER"
    activation_bounds: Optional[Decimal] = None
    leverage: int = 1
    connector_name: str = "nado"
    # MAKER slice offset from mid in bps: a BUY rests BELOW mid and a SELL
    # ABOVE it (buy low / sell high), re-anchored to the live mid every slice
    # so the quotes follow price. 0 = legacy at-mid pricing.
    maker_offset_bp: float = 0.0

    def __post_init__(self) -> None:
        self.total_amount_quote = _dec(self.total_amount_quote)
        if self.order_interval <= 0:
            raise ValueError("order_interval must be > 0")
        if self.total_duration < self.order_interval:
            raise ValueError("total_duration must be >= order_interval")

    @property
    def n_orders(self) -> int:
        return int(self.total_duration // self.order_interval)

    @property
    def amount_per_order_quote(self) -> Decimal:
        return self.total_amount_quote / Decimal(self.n_orders)


class TWAPExecutor(Executor):
    def __init__(
        self,
        config: TWAPExecutorConfig,
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
            keep_position=True,
            executor_id=executor_id,
        )
        self.config = config
        self.n_orders = config.n_orders
        self.start_ts = 0.0
        self.current_index = -1
        self.current_order: Optional[NadoOrder] = None
        self._current_recorded = Decimal(0)
        # BUG-TWAP-2 / TWAP-3 fix: track recorded quote+fee separately so
        # multi-price fills give correct marginal price + fee deltas.
        self._current_recorded_quote = Decimal(0)
        self._current_recorded_fee = Decimal(0)
        self.filled_base = Decimal(0)
        self.filled_quote = Decimal(0)
        self.lost_slices = 0
        self.p0: Optional[Decimal] = None
        self.last_mid: Optional[Decimal] = None

    @property
    def is_maker(self) -> bool:
        return self.config.mode.upper() == "MAKER"

    @property
    def average_executed_price(self) -> Optional[Decimal]:
        return (self.filled_quote / self.filled_base) if self.filled_base > 0 else None

    def _due_index(self, now: float) -> int:
        elapsed = now - self.start_ts
        return min(self.n_orders - 1, int(elapsed // self.config.order_interval))

    # -- slice execution --------------------------------------------------
    def _reset_current_counters(self) -> None:
        self._current_recorded = Decimal(0)
        self._current_recorded_quote = Decimal(0)
        self._current_recorded_fee = Decimal(0)

    async def _place_slice(self, index: int, mid: Decimal) -> None:
        if self.is_maker:
            # Rest INSIDE the book: buy below mid, sell above it (offset in
            # bps), re-anchored to the live mid each slice so the ladder
            # follows price. At-mid post-only orders crossed on tight books.
            px = mid
            offset = _dec(self.config.maker_offset_bp or 0) / Decimal(10000)
            if offset > 0:
                if self.config.side is TradeType.BUY:
                    px = mid * (Decimal(1) - offset)
                else:
                    px = mid * (Decimal(1) + offset)
            amount_base = self.config.amount_per_order_quote / px
            order = await self._guard(
                lambda: self.adapter.place_order(
                    self.trading_pair, self.config.side, OrderType.LIMIT_MAKER,
                    amount_base, px, self.config.leverage, False,
                ),
                label="twap_maker",
            )
            self.current_order = order
            self._reset_current_counters()
            self._ingest_current(order)
        else:
            amount_base = self.config.amount_per_order_quote / mid
            order = await self._guard(
                lambda: self.adapter.place_order(
                    self.trading_pair, self.config.side, OrderType.MARKET,
                    amount_base, None, self.config.leverage, False,
                ),
                label="twap_taker",
            )
            self.current_order = order
            self._reset_current_counters()
            self._ingest_current(order)
        self.current_index = index

    def _ingest_current(self, order: NadoOrder) -> None:
        # BUG-TWAP-2 / TWAP-3 fix: exact delta ingestion with real fee delta.
        delta_base = order.filled_base - self._current_recorded
        if delta_base <= 0:
            return
        delta_quote = order.filled_quote - self._current_recorded_quote
        delta_fee = order.fee_quote - self._current_recorded_fee
        price = (delta_quote / delta_base) if delta_base > 0 else Decimal(0)
        self._record_fill(
            Fill(order.id, self.trading_pair, self.config.side, delta_base, price, delta_fee, time.time()),
            order,
        )
        self._current_recorded = order.filled_base
        self._current_recorded_quote = order.filled_quote
        self._current_recorded_fee = order.fee_quote
        self.filled_base += delta_base
        self.filled_quote += delta_quote

    def _current_filled(self) -> bool:
        return self.current_order is not None and self.current_order.state is OrderState.FILLED

    # -- lifecycle --------------------------------------------------------
    async def on_create(self) -> None:
        self._activate()
        self.start_ts = time.time()
        mid = await self._guard(lambda: self.adapter.mid_price(self.trading_pair), label="twap_mid")
        self.p0 = mid
        self.last_mid = mid
        await self._place_slice(0, mid)

    async def on_tick(self) -> None:
        if self.is_terminated:
            return
        now = time.time()
        mid = await self._guard(lambda: self.adapter.mid_price(self.trading_pair), label="twap_mid")
        self.last_mid = mid
        if self.is_maker and self.current_order is not None and not self._current_filled():
            oid = self.current_order.id
            order = await self._guard(lambda: self.adapter.order_status(oid), label="twap_status")
            self.current_order = order
            self._ingest_current(order)
        due = self._due_index(now)
        while self.current_index < due and not self.is_terminated:
            await self._advance(mid)
        if self.current_index >= self.n_orders - 1 and self._last_slice_resolved(now):
            await self._finalize()

    async def _cancel_and_ingest_current(self) -> None:
        """Cancel the resting maker slice and capture any partial fills first.
        BUG-TWAP-4 fix.
        """
        if self.current_order is None:
            return
        oid = self.current_order.id
        try:
            await self._guard(lambda: self.adapter.cancel_order(oid), label="twap_cancel")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "twap %s: slice cancel failed for %s — order may still be resting: %s",
                self.id, oid, exc,
            )
        try:
            refreshed = await self._guard(
                lambda: self.adapter.order_status(oid), label="twap_post_cancel_status",
            )
            self.current_order = refreshed
            self._ingest_current(refreshed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "twap %s: post-cancel status failed for %s — last fills may be unbooked: %s",
                self.id, oid, exc,
            )

    async def _advance(self, mid: Decimal) -> None:
        # BUG-TWAP-4 fix: capture partial fills before cancelling.
        if self.is_maker and self.current_order is not None and not self._current_filled():
            await self._cancel_and_ingest_current()
            self.lost_slices += 1
        await self._place_slice(self.current_index + 1, mid)

    def _last_slice_resolved(self, now: float) -> bool:
        if not self.is_maker:
            return True
        if self._current_filled():
            return True
        return (now - self.start_ts) >= self.config.total_duration

    async def _finalize(self) -> None:
        # BUG-TWAP-1 fix: actually cancel the last resting maker slice
        # rather than just counting it as "lost" and leaving the order live
        # on the venue. Ingest any partial fill first.
        if self.current_order is not None and self.is_maker and not self._current_filled():
            await self._cancel_and_ingest_current()
            self.lost_slices += 1
        p0 = self.p0 or Decimal(0)
        mark = self.last_mid or p0
        if self.config.side is TradeType.BUY:
            trade_pnl = (mark - p0) * self.filled_base
        else:
            trade_pnl = (p0 - mark) * self.filled_base
        self._net_pnl_quote = trade_pnl - self._fees_paid_quote
        self._terminate(CloseType.COMPLETED)

    def metrics(self) -> Dict[str, object]:
        base = super().metrics()
        base.update(
            {
                "average_executed_price": self.average_executed_price,
                "filled_amount_base": self.filled_base,
                "cumulative_fees": self._fees_paid_quote,
                "lost_slices": self.lost_slices,
                "n_orders": self.n_orders,
            }
        )
        return base

    async def on_stop(self, close_type: CloseType = CloseType.EARLY_STOP) -> None:
        if self.is_terminated:
            return
        # BUG-TWAP-1 fix on stop: don't leave a resting maker slice live on
        # the venue after termination. Cancel and absorb partials first.
        if self.is_maker and self.current_order is not None and not self._current_filled():
            await self._cancel_and_ingest_current()
        self._terminate(close_type)
