"""Leverage is recorded on engine/desk fills so the History round-trip + Type A
PnL card show the true "Nx" instead of the trades table's ``leverage DEFAULT
1.0``.

Prod bug: a 49x DESK trade rendered as "LONG 1x". The desk/engine recorder
(``engine_persistence.DbTradeRecorder``) built its insert dict WITHOUT a
``leverage`` key, so every desk/engine fill row fell to the DB default 1.0, and
``compute_round_trips`` read that 1.0 off the opening lot. These pin the fix:
leverage flows executor.config -> _record_fill -> recorder.record -> the trade
row, and venue-only fills backfill it from the positions table.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.executors.grid_executor import GridExecutor, GridExecutorConfig
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import TradeType


# ── _config_leverage resolution ─────────────────────────────────

def _lev(cfg):
    return Executor._config_leverage(SimpleNamespace(config=cfg))


def test_config_leverage_reads_flat_and_nested_configs():
    # Grid-style: leverage directly on the config.
    assert _lev(SimpleNamespace(leverage=10)) == 10.0
    # Desk/DN (PositionExecutorConfig): leverage ONLY on the nested order_config,
    # and the nested value must win over any outer default.
    assert _lev(SimpleNamespace(order_config=SimpleNamespace(leverage=49), leverage=1)) == 49.0
    # Unknown / spot / zero → None so the recorder omits it (no misleading 1x).
    assert _lev(SimpleNamespace()) is None
    assert _lev(SimpleNamespace(leverage=0)) is None
    assert _lev(SimpleNamespace(leverage=None)) is None


# ── executor → recorder threading ───────────────────────────────

class _SpyRecorder:
    def __init__(self):
        self.rows = []

    def record(self, controller_id, trading_pair, side, amount_base, price,
               fee_quote, order_id=None, timestamp=None, *,
               realized_pnl=None, is_taker=False, leverage=None):
        self.rows.append({"side": side, "leverage": leverage})

    def link_placement(self, *a):
        pass


def test_grid_executor_records_its_config_leverage():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        rec = _SpyRecorder()
        inv = InventoryRepository()
        cfg = GridExecutorConfig(
            trading_pair="SOL-USDC", side=TradeType.BUY,
            start_price=Decimal(100), end_price=Decimal(110),
            limit_price=Decimal(95), total_amount_quote=Decimal(1000),
            min_spread_between_orders=Decimal("0.02"), keep_position=False,
            leverage=49,
        )
        ex = GridExecutor(cfg, user_id=1, controller_id="c", adapter=adapter,
                          inventory=inv)
        ex.trade_recorder = rec
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        await ex.on_tick()

        assert rec.rows, "the open fill never reached the recorder"
        assert all(r["leverage"] == 49 for r in rec.rows), (
            f"config leverage 49 was not recorded: {rec.rows}"
        )

    asyncio.run(body())


# ── DbTradeRecorder writes / omits the leverage column ──────────

def _run_record(leverage):
    from src.nadobro.trading.engine_persistence import DbTradeRecorder

    captured = {}
    with patch("src.nadobro.trading.engine_persistence.resolve_running_session_id",
               return_value=None), \
         patch("src.nadobro.trading.engine_persistence._resolve_engine_fill_product",
               return_value=(5, "BTC-PERP")), \
         patch("src.nadobro.models.database.insert_trade",
               side_effect=lambda data, network="mainnet": captured.update(data) or 1):
        DbTradeRecorder().record(
            "desk:42:mainnet", "BTC-PERP", TradeType.BUY,
            Decimal("0.03"), Decimal("80000"), Decimal("1"),
            order_id=None, leverage=leverage,
        )
    return captured


def test_db_recorder_writes_leverage_onto_the_trade_row():
    data = _run_record(49.0)
    assert data.get("source") == "manual"          # desk → History round-trips
    assert data.get("leverage") == 49.0            # the fix: real leverage stored
    # Every engine/desk fill is bot-routed → counts toward "Nadobro Vol".
    assert data.get("via_nadobro") is True


def test_db_recorder_omits_leverage_when_unknown():
    data = _run_record(None)
    # No leverage key → the column is left to its default rather than a wrong
    # value we made up. (spot / unknown-config path)
    assert "leverage" not in data


# ── venue-only fill backfill from the positions table ───────────

def test_fill_leverage_from_positions_prefers_open_row():
    from src.nadobro.venue import nado_sync

    with patch.object(nado_sync, "query_one", return_value={"leverage": 49.0}):
        assert nado_sync._fill_leverage_from_positions(42, "mainnet", 5, False) == 49.0
    # No open position row (e.g. a fully-closing fill) → None, so the INSERT
    # leaves leverage NULL instead of guessing 1x.
    with patch.object(nado_sync, "query_one", return_value=None):
        assert nado_sync._fill_leverage_from_positions(42, "mainnet", 5, False) is None
    # Missing product id → None without touching the DB.
    assert nado_sync._fill_leverage_from_positions(42, "mainnet", 0, False) is None
