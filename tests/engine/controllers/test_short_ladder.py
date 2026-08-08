import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.short_ladder import ShortLadderController
from src.nadobro.engine.executors.reverse_grid_executor import ReverseGridExecutor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator

CFG = {"trading_pair": "P", "start_price": "98", "end_price": "102", "limit_price": "110",
       "total_amount_quote": "100", "min_spread_between_orders": "0.01", "max_open_orders": 4}


def test_short_ladder_spawns_short_executor():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        c = ShortLadderController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=dict(CFG))
        await orch.spawn_controller(c)
        execs = orch.list(c.id)
        assert len(execs) == 1 and isinstance(execs[0], ReverseGridExecutor)
        assert c.name == "short_ladder"

    asyncio.run(body())
