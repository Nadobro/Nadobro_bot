"""Executor base class — abstract lifecycle (CREATED -> ACTIVE -> TERMINATED),
standardized metrics, ``controller_id`` linkage, and a retry policy (up to 3
attempts with exponential backoff on :class:`AdapterError`; on exhaustion the
executor transitions to TERMINATED with ``close_type=FAILED``).

Implemented in Phase 1.
"""
from __future__ import annotations

import abc
import asyncio
import time
import uuid
from decimal import Decimal
from typing import Awaitable, Callable, Dict, Optional, Protocol, TypeVar

from src.nadobro.engine.adapter.base import (
    AdapterError, Fill, NadoAdapterBase, NadoOrder,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import CloseType, ExecutorState, OrderType, TradeType

T = TypeVar("T")


class TradeRecorder(Protocol):
    """Structural type for the optional fill -> ``trades_<network>`` bridge the
    runtime injects (services.engine_persistence.DbTradeRecorder). Kept as a
    Protocol so the DB-agnostic engine library never imports the services
    layer; any object with a compatible ``record`` satisfies it."""

    def record(
        self,
        controller_id: str,
        trading_pair: str,
        side: TradeType,
        amount_base: Decimal,
        price: Decimal,
        fee_quote: Decimal,
        order_id: Optional[str] = ...,
        timestamp: Optional[float] = ...,
        *,
        is_taker: bool = ...,
    ) -> None:
        ...


class ExecutorFailed(Exception):
    """Raised when an executor exhausts its retry budget on an adapter call."""


class Executor(abc.ABC):
    MAX_ATTEMPTS = 3
    BASE_BACKOFF = 0.01  # seconds; exponential per attempt

    # Regime gate / inventory cap: set by the owning controller to stop NEW
    # opening orders while exits, close legs, and stops keep running.
    # Executors that don't open new exposure simply ignore it.
    suppress_new_entries: bool = False

    def __init__(
        self,
        *,
        user_id: int,
        controller_id: str,
        trading_pair: str,
        adapter: NadoAdapterBase,
        inventory: Optional[InventoryRepository] = None,
        keep_position: bool = True,
        executor_id: Optional[str] = None,
    ) -> None:
        self.id = executor_id or uuid.uuid4().hex
        self.user_id = user_id
        self.controller_id = controller_id
        self.connector_name = "nado"
        self.trading_pair = trading_pair
        self.adapter = adapter
        self.inventory = inventory
        self.keep_position = keep_position
        self.state = ExecutorState.CREATED
        self.close_type: Optional[CloseType] = None
        self.created_at = time.time()
        self.terminated_at: Optional[float] = None
        self.retries = 0
        self._net_pnl_quote = Decimal(0)
        self._fees_paid_quote = Decimal(0)
        self._volume_quote = Decimal(0)
        # Optional bridge to the legacy ``trades_<network>`` reporting tables.
        # Injected by the orchestrator at spawn time (see orchestrator.spawn);
        # ``None`` in unit tests / read-only modes, where recording is a no-op.
        self.trade_recorder: Optional[TradeRecorder] = None

    # -- lifecycle --------------------------------------------------------
    @abc.abstractmethod
    async def on_create(self) -> None:
        ...

    @abc.abstractmethod
    async def on_tick(self) -> None:
        ...

    async def on_stop(self, close_type: CloseType = CloseType.EARLY_STOP) -> None:
        self._terminate(close_type)

    def _activate(self) -> None:
        if self.state is ExecutorState.CREATED:
            self.state = ExecutorState.ACTIVE

    def _terminate(self, close_type: CloseType) -> None:
        if self.state is ExecutorState.TERMINATED:
            return
        self.state = ExecutorState.TERMINATED
        self.close_type = close_type
        self.terminated_at = time.time()

    # -- status -----------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.state is ExecutorState.ACTIVE

    @property
    def is_terminated(self) -> bool:
        return self.state is ExecutorState.TERMINATED

    @property
    def duration_seconds(self) -> float:
        end = self.terminated_at if self.terminated_at is not None else time.time()
        return end - self.created_at

    def metrics(self) -> Dict[str, object]:
        return {
            "net_pnl_quote": self._net_pnl_quote,
            "fees_paid_quote": self._fees_paid_quote,
            "volume_quote": self._volume_quote,
            "duration_seconds": self.duration_seconds,
        }

    # -- helpers ----------------------------------------------------------
    async def _guard(self, op: Callable[[], Awaitable[T]], *, label: str = "") -> T:
        """Run an async adapter op with retry + exponential backoff. On
        exhaustion, terminate the executor FAILED and raise ExecutorFailed."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                return await op()
            except AdapterError as exc:
                last_exc = exc
                self.retries += 1
                if attempt < self.MAX_ATTEMPTS - 1:
                    await asyncio.sleep(self.BASE_BACKOFF * (2 ** attempt))
        self._terminate(CloseType.FAILED)
        raise ExecutorFailed(
            f"{label or 'adapter op'} failed after {self.MAX_ATTEMPTS} attempts: {last_exc}"
        )

    def _fill_was_taker(self, order: Optional[NadoOrder] = None) -> bool:
        """Whether this fill crossed the spread.

        ``DbTradeRecorder.record`` defaults ``is_taker`` to False and nothing
        downstream ever corrected it for engine-bridged rows (nado_sync enriches
        realized PnL and product identity, not this flag), so EVERY engine fill was
        recorded as a maker fill. Harmless for a maker ladder; systematically wrong
        for R-Grid and the volume bot, which cross by design — their fee analytics
        and any ``is_taker`` filter read them as maker.

        There is no venue truth to read here: the SDK's ``IndexerMatch`` model
        carries only base/quote/fee (+ digest/order/timestamp) and has NO taker
        field, so ``get_matches`` cannot answer it. (``venue/nado_archive`` reads a
        ``is_taker`` key defensively off the RAW archive HTTP shape, which the
        engine's adapter does not use.) So the flag is resolved in three tiers,
        most authoritative first — RG-TAKERFLAG-1, self-review 2026-08-07:

        1. **What the venue was ACTUALLY sent** (``order.order_type``). This is the
           only tier that sees the adapter's EXIT-MIN-NOTIONAL escape, which
           rewrites a sub-minimum reduce-only limit into a MARKET order AFTER the
           executor decided (``adapter/nado.py``) — the config still says
           LIMIT_MAKER, the wire says MARKET, and the wire is right. It also covers
           grid's reduce-only MARKET stop-out flatten and profit-tier book-reduce,
           whose ``GridExecutorConfig`` has no ``execution_strategy`` at all.
        2. **A declared crossing intent** (``config.crosses_book``). Type alone
           cannot settle this: a plain LIMIT rests or crosses depending on its price
           against the book. The volume bot prices four legs deliberately THROUGH
           the book while leaving them limit orders, so the controller that priced
           them says so.
        3. **The REQUESTED strategy**, when there is no placed order to read (a fill
           reconstructed from a poll, or an executor that keeps no order handle).
           This is the original behaviour and stays the conservative default.
        """
        cfg = getattr(self, "config", None)
        # PositionExecutorConfig (Delta Neutral, desk) holds neither field itself —
        # they live on a NESTED ``order_config``. Without this hop, DN's MARKET
        # legs fell through every tier and recorded as maker on both legs of every
        # cycle.
        if cfg is not None and not hasattr(cfg, "execution_strategy"):
            cfg = getattr(cfg, "order_config", cfg)
        try:
            from src.nadobro.engine.types import ExecutionStrategy

            placed = order if order is not None else getattr(self, "order", None)
            placed_type = getattr(placed, "order_type", None)
            if placed_type is OrderType.MARKET:
                return True
            if bool(getattr(cfg, "crosses_book", False)):
                return True
            if placed_type is not None:
                return False        # it really did rest as a limit order
            return getattr(cfg, "execution_strategy", None) is ExecutionStrategy.MARKET
        except Exception:  # noqa: BLE001  # policy: degrade-ok(unknown shape ⇒ prior behaviour)
            return False

    def _record_fill(
        self, fill: Fill, order: Optional[NadoOrder] = None, *,
        crossed: Optional[bool] = None,
    ) -> None:
        """Book a fill into inventory and bridge it to the reporting tables.

        Pass ``order`` — the NadoOrder this fill came off — whenever the caller has
        it. It is what makes ``is_taker`` reflect the order the venue actually
        received rather than the one the config asked for; see
        :meth:`_fill_was_taker`.
        """
        self._fees_paid_quote += fill.fee_quote
        self._volume_quote += fill.amount_quote
        if self.inventory is not None:
            self.inventory.apply_fill(
                self.user_id,
                self.trading_pair,
                self.controller_id,
                fill.side,
                fill.amount_base,
                fill.amount_quote,
                fill.fee_quote,
                fill.timestamp,
            )
        # Bridge the fill into the legacy reporting tables so /status,
        # /mm_status, /mm_fills, portfolio cards, and the per-session rollup
        # reflect engine strategies. Best-effort: the recorder swallows its own
        # errors, but guard here too so a missing/edge recorder can never break
        # a fill.
        recorder = self.trade_recorder
        if recorder is not None:
            try:
                recorder.record(
                    self.controller_id,
                    self.trading_pair,
                    fill.side,
                    fill.amount_base,
                    fill.price,
                    fill.fee_quote,
                    fill.order_id,
                    fill.timestamp,
                    is_taker=(self._fill_was_taker(order) if crossed is None
                              else bool(crossed)),
                )
            except Exception:  # noqa: BLE001  # policy: degrade-ok(trade-recording is best-effort; the recorder logs its own failures — a fill must never be lost to a reporting-bridge error)
                pass

    def _record_realized(self, amount_quote: Decimal) -> None:
        self._net_pnl_quote += amount_quote
