"""End-to-end integration: EngineRuntime drives a real controller whose fills
persist to a REAL Postgres (engine_position_hold + engine_executors). The only
test double is the venue adapter (no testnet here); inventory + executor
persistence + the orchestrator/controller/executor stack are all real.

Auto-skips when no database is reachable.

    DATABASE_URL=postgresql://postgres@127.0.0.1:55432/nadotest \
        PYTHONPATH=tests:. .venv/bin/python -m pytest tests/engine/test_engine_runtime_db.py
"""
from __future__ import annotations

import asyncio
import os
import pathlib
from decimal import Decimal

import pytest

from tests.engine._mock_nado import MockNadoAdapter

MIGRATIONS = pathlib.Path("src/nadobro/migrations")


def _db_reachable() -> bool:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL")):
        return False
    try:
        import psycopg2

        url = os.environ.get("SUPABASE_DATABASE_URL") or os.environ["DATABASE_URL"]
        psycopg2.connect(url).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="no reachable Postgres (DATABASE_URL)")


@pytest.fixture(scope="module", autouse=True)
def _schema():
    from src.nadobro.db import execute

    for mig in ("0007_engine_v2_tables.sql", "0009_engine_kill_switch.sql"):
        execute((MIGRATIONS / mig).read_text())
    execute("TRUNCATE engine_position_hold, engine_executors, engine_kill_switch")
    yield


def test_runtime_drives_grid_and_persists_fill_and_executor():
    async def body():
        from src.nadobro.trading.engine_persistence import DbExecutorStore, DbInventoryRepository
        from src.nadobro.strategy.engine_runtime import EngineRuntime

        # LIMIT ORDERS ONLY: the stop-out flatten is a MARKETABLE limit now, not a
        # MARKET order, so the venue double has to model a crossing limit or the
        # flatten never fills, the executor stays active for retry, and runtime.stop
        # leaves the controller running.
        adapter = MockNadoAdapter(mid=Decimal(100), fill_marketable_limits=True)
        inventory = DbInventoryRepository()            # REAL Postgres
        store = DbExecutorStore()                      # REAL Postgres
        runtime = EngineRuntime(executor_store=store)

        configs = {
            "trading_pair": "SOL-USDC", "start_price": "98", "end_price": "102",
            "limit_price": "95", "total_amount_quote": "99",
            "min_spread_between_orders": "0.01", "max_open_orders": 3,
        }
        controller = await runtime.start(9100, "testnet", "grid", configs, adapter, inventory)
        assert controller.is_active

        orch = runtime._orchestrators[(9100, "testnet", "grid")]
        grid = orch.list(controller.id)[0]
        # fill the first grid level's resting buy order on the venue double
        level0 = grid.levels[0]
        adapter.fill_order(level0.open_order_id, price=level0.open_price)

        await runtime.tick(9100, "testnet", "grid")    # ingest fill -> DB; persist executor -> DB

        # holds persisted to engine_position_hold (read via a fresh repo)
        hold = DbInventoryRepository().get(9100, "SOL-USDC", controller.id)
        assert hold.buy_amount_base > 0
        assert hold.buy_amount_quote > 0

        # executor lifecycle persisted to engine_executors
        row = DbExecutorStore().get(grid.id)
        assert row is not None
        assert row["strategy_type"] == "grid"
        assert row["user_id"] == 9100 and row["controller_id"] == controller.id

        await runtime.stop(9100, "testnet", "grid")
        assert not runtime.is_running(9100, "testnet", "grid")

    asyncio.run(body())
