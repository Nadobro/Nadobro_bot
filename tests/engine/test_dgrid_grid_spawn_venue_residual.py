"""DGRID-GRIDRGRID-RESIDUAL, the GRID direction (2026-09-02).

The trend spawn already refuses to arm on a venue residual the inventory does
not know about. The GRID spawn had no such check: a partial the previous phase
left on the venue was inherited silently by the new ladder. Same rule now —
close it reduce-only first and spawn from a venue-confirmed flat — with one
difference: an UNREADABLE venue does not block the grid (it does not baseline
out a position the way the trigger delegate does).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import OrderType, TradeType

PAIR = "BTC-PERP"
_DG = {
    "trading_pair": PAIR,
    "start_price": "63200", "end_price": "63400", "limit_price": "0",
    "total_amount_quote": "1000", "min_spread_between_orders": "0.001",
    "max_open_orders": 3, "step_pct": "0.001", "levels_count": 3,
    "regime_gate_enabled": 0.0,
}


def _ctrl(adapter):
    orch = ExecutorOrchestrator()
    c = DynamicGridController(
        user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
        configs=dict(_DG, candle_provider=lambda p: [{"close": 63300 + (i % 2) * 20} for i in range(200)]),
    )
    return orch, c


def test_a_venue_residual_is_closed_reduce_only_before_the_grid_spawns():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"), venue_held={PAIR: Decimal("0.01")})
        orch, c = _ctrl(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        first = adapter.placed[0]
        assert first.order_type is OrderType.MARKET and first.side is TradeType.SELL
        assert first.amount_base == Decimal("0.01"), "exactly the residual, reduce-only"
        assert adapter.venue_held[PAIR] == 0                       # venue confirmed flat…
        assert orch.list(c.id, active_only=True), "…and only then the grid spawned"
    asyncio.run(body())


def test_an_unreadable_venue_does_not_block_the_grid():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"))         # no venue_held entry -> None
        orch, c = _ctrl(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert orch.list(c.id, active_only=True), "grid must spawn on an unreadable (not residual) venue"
        assert not [o for o in adapter.placed if o.order_type is OrderType.MARKET]
    asyncio.run(body())


def test_a_flat_venue_spawns_without_any_close():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"), venue_held={PAIR: Decimal(0)})
        orch, c = _ctrl(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert orch.list(c.id, active_only=True)
        assert not [o for o in adapter.placed if o.order_type is OrderType.MARKET]
    asyncio.run(body())
