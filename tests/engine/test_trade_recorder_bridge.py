"""Engine -> legacy reporting bridge: every executor fill must reach the
injected ``trade_recorder`` (so /status, /mm_status, /mm_fills, portfolio and
the per-session rollup light up for engine strategies), and the orchestrator
must stamp the recorder onto executors before on_create() so the entry fill is
captured too.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.nadobro.engine.executors.grid_executor import (
    GridExecutor,
    GridExecutorConfig,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.risk import ExecutorRequest
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import TradeType
from tests.engine._mock_nado import MockNadoAdapter

PAIR = "SOL-USDC"


class _RecordingRecorder:
    """Captures recorder.record(...) calls for assertions."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, controller_id, trading_pair, side, amount_base, price,
               fee_quote, order_id=None, timestamp=None, *,
               realized_pnl=None, is_taker=False, leverage=None):
        self.calls.append({
            "controller_id": controller_id,
            "trading_pair": trading_pair,
            "side": side,
            "amount_base": Decimal(str(amount_base)),
            "price": Decimal(str(price)),
            "fee_quote": Decimal(str(fee_quote)),
            "order_id": order_id,
        })


def _cfg(**kw):
    base = dict(
        trading_pair=PAIR, side=TradeType.BUY, start_price=Decimal(100),
        end_price=Decimal(110), limit_price=Decimal(95),
        total_amount_quote=Decimal(1000),
        min_spread_between_orders=Decimal("0.02"),
    )
    base.update(kw)
    return GridExecutorConfig(**base)


def test_fill_reaches_recorder_with_human_fields():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(105))
        inv = InventoryRepository()
        rec = _RecordingRecorder()
        ex = GridExecutor(_cfg(), user_id=7, controller_id="dgrid:7:mainnet",
                          adapter=adapter, inventory=inv)
        ex.trade_recorder = rec
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        await ex.on_tick()

        assert rec.calls, "fill was not forwarded to the trade recorder"
        call = rec.calls[0]
        assert call["controller_id"] == "dgrid:7:mainnet"
        assert call["trading_pair"] == PAIR
        assert call["side"] is TradeType.BUY
        assert call["amount_base"] > 0
        assert call["price"] > 0

    asyncio.run(body())


def test_orchestrator_stamps_recorder_before_on_create():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(105))
        rec = _RecordingRecorder()
        orch = ExecutorOrchestrator(trade_recorder=rec)
        ex = GridExecutor(_cfg(), user_id=7, controller_id="dgrid:7:mainnet",
                          adapter=adapter, inventory=InventoryRepository())
        # Entry orders place (and can fill) inside on_create(); the recorder
        # must already be attached by then.
        spawned = await orch.spawn(ex, ExecutorRequest(
            order_amount_quote=Decimal(1000), position_size_quote=Decimal(1000)))
        assert spawned
        assert ex.trade_recorder is rec

    asyncio.run(body())


def test_no_recorder_is_a_noop():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(105))
        ex = GridExecutor(_cfg(), user_id=7, controller_id="dgrid:7:mainnet",
                          adapter=adapter, inventory=InventoryRepository())
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        # Must not raise with no recorder wired.
        await ex.on_tick()

    asyncio.run(body())
