"""GridExecutor.recenter — in-place re-quote of the resting ladder.

A true grid re-center moves the unfilled open orders to a new band around the
current mid WITHOUT realizing the held position: levels holding inventory keep
their close legs at the original target, and NO market / reduce-only order is
placed. This is the non-destructive replacement for the old dgrid "flatten +
respawn" reset that bled fees.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.grid_trading import GridController
from src.nadobro.engine.controllers.short_ladder import ShortLadderController
from src.nadobro.engine.executors.grid_executor import (
    GridExecutor,
    GridExecutorConfig,
    GridLevelState,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import OrderType, TradeType


def _cfg() -> GridExecutorConfig:
    return GridExecutorConfig(
        trading_pair="BTC-PERP", side=TradeType.BUY,
        start_price=Decimal("99"), end_price=Decimal("100"), limit_price=Decimal(0),
        total_amount_quote=Decimal(100), min_spread_between_orders=Decimal("0.002"),
        max_open_orders=3,
    )


def test_recenter_requotes_free_opens_keeps_inventory_and_does_not_flatten():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_cfg(), user_id=1, controller_id="G", adapter=adapter,
                          inventory=InventoryRepository())
        await orch.spawn(ex)  # on_create places the opens

        opens = [lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED]
        assert opens, "grid should have resting opens"
        # Fill ONE level -> it books a close leg (held inventory).
        adapter.fill_order(opens[0].open_order_id)
        await orch.tick(ex.id)
        held = next(lv for lv in ex.levels if lv.state is GridLevelState.CLOSE_ORDER_PLACED)
        held_close_price = held.close_price
        net_before = ex.inventory.get(1, "BTC-PERP", "G").net_amount_base
        assert net_before > 0, "should be holding the filled inventory"

        placed_before = len(adapter.placed)

        # Re-center the ladder UP to a new band around ~110.
        await ex.recenter(Decimal("109"), Decimal("110"))

        # 1) Held inventory untouched: same close leg, same net position.
        still_held = [lv for lv in ex.levels if lv.state is GridLevelState.CLOSE_ORDER_PLACED]
        assert len(still_held) == 1 and still_held[0].close_price == held_close_price
        assert ex.inventory.get(1, "BTC-PERP", "G").net_amount_base == net_before

        # 2) Free opens re-priced into the new band (was ~99-100, now ~109-110).
        new_opens = [lv for lv in ex.levels
                     if lv.state in (GridLevelState.NOT_ACTIVE, GridLevelState.OPEN_ORDER_PLACED)]
        assert new_opens, "expected re-quoted opens"
        assert max(lv.open_price for lv in new_opens) > Decimal("108")

        # 3) NOT a flatten: no MARKET / reduce-only order was placed.
        new_orders = adapter.placed[placed_before:]
        assert all(o.order_type is not OrderType.MARKET for o in new_orders), \
            "recenter must not place a market/flatten order"
        assert not ex.is_terminated

    asyncio.run(body())


def _recycle_cfg(recycle: bool) -> GridExecutorConfig:
    return GridExecutorConfig(
        trading_pair="BTC-PERP", side=TradeType.BUY,
        start_price=Decimal("99"), end_price=Decimal("100"), limit_price=Decimal(0),
        total_amount_quote=Decimal(100), min_spread_between_orders=Decimal("0.002"),
        max_open_orders=1, recycle_levels=recycle,
    )


def test_recycle_rearms_completed_level_and_keeps_quoting():
    """recycle_levels=True (D-Grid): a fully round-tripped level re-arms and
    re-quotes a fresh open instead of completing-and-terminating, so the grid
    keeps working its band — the fix for 'placed a few orders and stopped'."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recycle_cfg(True), user_id=1, controller_id="G", adapter=adapter,
                          inventory=InventoryRepository())
        await orch.spawn(ex)  # on_create places the open
        lv = ex.levels[0]
        assert lv.state is GridLevelState.OPEN_ORDER_PLACED
        # Round-trip to COMPLETE: fill open -> close placed -> fill close.
        adapter.fill_order(lv.open_order_id)
        await orch.tick(ex.id)
        assert ex.levels[0].state is GridLevelState.CLOSE_ORDER_PLACED
        adapter.fill_order(ex.levels[0].close_order_id)
        placed_before = len(adapter.placed)
        await orch.tick(ex.id)  # close filled -> COMPLETE -> recycle -> re-quote
        assert not ex.is_terminated, "recycling grid must not terminate on complete"
        assert len(adapter.placed) > placed_before, "recycled level must re-quote a new open"
        assert ex.levels[0].state is GridLevelState.OPEN_ORDER_PLACED

    asyncio.run(body())


def test_classic_grid_terminates_on_all_complete():
    """recycle_levels=False (classic one-shot ladder): unchanged — once every
    level round-trips, the executor terminates COMPLETED."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recycle_cfg(False), user_id=1, controller_id="G", adapter=adapter,
                          inventory=InventoryRepository())
        await orch.spawn(ex)
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id)
        await orch.tick(ex.id)
        adapter.fill_order(ex.levels[0].close_order_id)
        await orch.tick(ex.id)
        assert ex.is_terminated, "classic one-shot grid must terminate when all levels complete"

    asyncio.run(body())


def _ctrl_cfg(**over):
    cfg = {
        "trading_pair": "BTC-PERP", "start_price": Decimal("99"), "end_price": Decimal("100"),
        "limit_price": Decimal(0), "total_amount_quote": Decimal(100),
        "min_spread_between_orders": Decimal("0.002"), "max_open_orders": 3,
        "step_pct": Decimal("0.002"), "levels_count": 3,
        "regime_gate_enabled": 0.0,  # off so it always arms (rgrid-style)
    }
    cfg.update(over)
    return cfg


def test_grid_controller_recenters_in_place_and_reports_telemetry():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        c = ShortLadderController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs=_ctrl_cfg(reset_threshold_bp=200.0), controller_id="RG",
        )
        await orch.spawn_controller(c)  # on_start arms (gate off)
        active = orch.list(c.id, active_only=True)
        assert len(active) == 1
        first = active[0]
        m = c.grid_metrics()
        assert m["grid_reset_side"] == "SELL" and m["grid_reset_active"] is True
        assert float(m["grid_anchor_price"]) > 0

        # RGRID-STALE-LADDER: 200bp is capped to the 40bp band (step 20bp x 2).
        assert c.reset_threshold_bp == 40.0

        # Small in-band move (20bp): no re-center.
        adapter.set_mid(Decimal("100.2"))
        await orch.tick_controller(c.id)
        assert orch.list(c.id, active_only=True)[0] is first

        # Large move past the band threshold: re-center the SAME executor.
        adapter.set_mid(Decimal("103"))
        await orch.tick_controller(c.id)
        active = orch.list(c.id, active_only=True)
        assert len(active) == 1 and active[0] is first, "re-center must reuse the executor"
        # SELL ladder re-quotes at/above the new mid.
        assert min(lv.open_price for lv in first.levels) >= Decimal("102")

    asyncio.run(body())


def test_order_counts_aggregate_real_placements():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        c = GridController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs=_ctrl_cfg(), controller_id="G",
        )
        await orch.spawn_controller(c)  # on_start places the opens
        counts = c.order_counts()
        ex = orch.list(c.id, active_only=True)[0]
        placed_levels = [lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED]
        assert counts["orders_placed"] == len(placed_levels) > 0
        assert counts["orders_placed"] == ex.orders_placed

    asyncio.run(body())
