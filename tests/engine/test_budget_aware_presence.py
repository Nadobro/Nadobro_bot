"""AUDIT-DENY-2026-09-02-F3: budget-aware presence-first.

When the venue is throttling our READS this cycle (a status poll came back
denied — ``adapter.reads_throttled_this_cycle() > 0``), a requote or a ladder
recenter is cancel+place traffic against the same budget: it deepens the very
contention that produced the hold. Under contention the engine keeps what
rests — opening-side requotes and recenters wait for a cycle whose reads
cleared — while a fresh spawn on an EMPTY slot and reducing-side requotes still
go through. The stop path is never gated (it has its own epoch).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.controllers.grid_trading import GridController
from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import TradeType

MM_CFG = {
    "trading_pair": "BTC", "spread_bp": "10", "order_amount_quote": "10",
    "levels": "1", "leverage": "1", "price_distance_tolerance": "0.0001",
    "min_quote_lifetime_s": "0", "max_quote_lifetime_s": "0",
}


def _mm(adapter):
    orch = ExecutorOrchestrator()
    c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                               inventory=InventoryRepository(), configs=dict(MM_CFG))
    return orch, c


# --- Mid / fill-anchored requotes ----------------------------------------------

def test_an_opening_requote_is_held_while_the_venue_throttles_reads():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter)
        await orch.spawn_controller(c)
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"))
        first = c._slot(True, 0).ex_id
        assert first is not None
        adapter._note_read_throttled()                    # a status read was denied this cycle
        await c._reconcile(TradeType.BUY, Decimal("95"), True, Decimal("100"))   # far enough to requote
        assert c._slot(True, 0).ex_id == first, "requote under contention = churn against the same budget"
        adapter.begin_cycle()                             # next cycle: reads cleared
        await c._reconcile(TradeType.BUY, Decimal("95"), True, Decimal("100"))
        assert c._slot(True, 0).ex_id != first            # the requote goes through now
    asyncio.run(body())


def test_a_reducing_side_requote_still_goes_through_under_contention():
    """Exposure must always be able to come down: only OPENING requotes wait."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter)
        await orch.spawn_controller(c)
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"), is_opening=False)
        first = c._slot(True, 0).ex_id
        adapter._note_read_throttled()
        await c._reconcile(TradeType.BUY, Decimal("95"), True, Decimal("100"), is_opening=False)
        assert c._slot(True, 0).ex_id != first
    asyncio.run(body())


def test_a_fresh_spawn_on_an_empty_slot_is_not_a_requote():
    """Presence-first still holds: an empty slot gets its quote even under
    contention — the hold is about churn, not about being absent."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter)
        await orch.spawn_controller(c)
        adapter._note_read_throttled()
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"))
        assert c._slot(True, 0).ex_id is not None and len(adapter.placed) == 1
    asyncio.run(body())


# --- Grid recenter --------------------------------------------------------------

_GRID = {
    "trading_pair": "BTC-PERP", "start_price": Decimal("99"), "end_price": Decimal("100"),
    "limit_price": Decimal(0), "total_amount_quote": Decimal(100),
    "min_spread_between_orders": Decimal("0.002"), "max_open_orders": 3,
    "step_pct": Decimal("0.002"), "levels_count": 3, "reset_threshold_bp": 20.0,
    "regime_gate_enabled": 0.0,
}


def test_a_grid_recenter_is_deferred_while_the_venue_throttles_reads():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        c = GridController(user_id=1, orchestrator=orch, adapter=adapter,
                           inventory=InventoryRepository(), configs=dict(_GRID), controller_id="G")
        await orch.spawn_controller(c)
        assert orch.list(c.id, active_only=True), "ladder armed (gate off)"
        c._anchor_mid = Decimal("100")
        moved = Decimal("101")                            # 100bp >= the reset threshold
        adapter.set_mid(moved)
        adapter._note_read_throttled()
        await c._maybe_recenter(moved)
        assert c._last_recenter_ts == 0.0, "recenter under contention = cancel+replace against a denied budget"
        adapter.begin_cycle()
        await c._maybe_recenter(moved)
        assert c._last_recenter_ts > 0.0                  # recentered once the reads cleared
    asyncio.run(body())


# --- Dynamic grid recenter ------------------------------------------------------

_DG = {
    "trading_pair": "BTC-PERP",
    "start_price": "63200", "end_price": "63400", "limit_price": "0",
    "total_amount_quote": "1000", "min_spread_between_orders": "0.001",
    "max_open_orders": 3, "step_pct": "0.001", "levels_count": 3,
    "dgrid_reset_threshold_bp": 80.0,
    "regime_gate_enabled": 0.0,
}


def test_a_dgrid_recenter_is_deferred_while_the_venue_throttles_reads():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"))
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs=dict(_DG, candle_provider=lambda p: [{"close": 63300 + (i % 2) * 20} for i in range(200)]),
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        before = [lv.open_price for lv in orch.list(c.id, active_only=True)[0].levels]
        adapter.set_mid(Decimal("63373.5") * (Decimal(1) + Decimal("0.0030")))   # 30bp: recenters normally
        adapter._note_read_throttled()
        await orch.tick_controller(c.id)
        after = [lv.open_price for lv in orch.list(c.id, active_only=True)[0].levels]
        assert after == before, "dgrid recentered in a cycle whose reads the venue denied"
        adapter.begin_cycle()
        await orch.tick_controller(c.id)
        after2 = [lv.open_price for lv in orch.list(c.id, active_only=True)[0].levels]
        assert after2 != before                           # and does once the reads clear
    asyncio.run(body())
