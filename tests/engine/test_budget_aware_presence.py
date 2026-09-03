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


def _mm(adapter, **over):
    orch = ExecutorOrchestrator()
    c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                               inventory=InventoryRepository(), configs=dict(MM_CFG, **over))
    return orch, c


def test_the_throttle_hold_never_outlives_the_quote_ttl():
    """MID audit of 27e0926, finding 1: the hold must not override the profile's
    max_quote_lifetime_s — the cadence's promised refresh still happens."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, max_quote_lifetime_s="60")
        clock = [1000.0]
        c._now = lambda: clock[0]
        await orch.spawn_controller(c)
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"))
        first = c._slot(True, 0).ex_id
        adapter._note_read_throttled()
        clock[0] += 30                                    # inside the TTL: held
        await c._reconcile(TradeType.BUY, Decimal("95"), True, Decimal("100"))
        assert c._slot(True, 0).ex_id == first
        clock[0] += 31                                    # TTL elapsed: refreshed even under contention
        await c._reconcile(TradeType.BUY, Decimal("95"), True, Decimal("100"))
        assert c._slot(True, 0).ex_id != first
    asyncio.run(body())


def test_a_profile_without_a_ttl_is_still_bounded(monkeypatch):
    from src.nadobro.engine.controllers import market_making as mm
    monkeypatch.setattr(mm, "_THROTTLE_HOLD_MAX_S", 100.0)

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, max_quote_lifetime_s="0")
        clock = [1000.0]
        c._now = lambda: clock[0]
        await orch.spawn_controller(c)
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"))
        first = c._slot(True, 0).ex_id
        adapter._note_read_throttled()
        clock[0] += 50
        await c._reconcile(TradeType.BUY, Decimal("95"), True, Decimal("100"))
        assert c._slot(True, 0).ex_id == first             # held, inside the bound
        clock[0] += 51
        await c._reconcile(TradeType.BUY, Decimal("95"), True, Decimal("100"))
        assert c._slot(True, 0).ex_id != first             # bound reached: refreshed
    asyncio.run(body())


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


# --- Grid / dgrid recenters -----------------------------------------------------
#
# Both recenters are decided BEFORE the executor ticks (where the status polls
# live), and EngineRuntime.tick zeroes the per-cycle counter right before the
# tick — so "this cycle" always reads 0 at decision time (grid + dgrid audits of
# 27e0926). The signal is the PREVIOUS cycle's denials (venue_reads_contended),
# and these tests produce the denial where the live adapter does: inside
# order_status, inside the tick.


class _ThrottledPollsAdapter(MockNadoAdapter):
    """Every status poll is budget-denied while ``deny`` is True. The live
    adapter HOLDS the order (#274) and counts the denial; the mock counts it in
    the same place — inside order_status — so the tick order is real."""

    deny = True

    async def order_status(self, order_id):
        if self.deny:
            self._note_read_throttled()
        return await super().order_status(order_id)


async def _cycle(adapter, orch, c):
    adapter.begin_cycle()                    # what EngineRuntime.tick does first
    await orch.tick_controller(c.id)


_GRID = {
    "trading_pair": "BTC-PERP", "start_price": Decimal("99"), "end_price": Decimal("100"),
    "limit_price": Decimal(0), "total_amount_quote": Decimal(100),
    "min_spread_between_orders": Decimal("0.002"), "max_open_orders": 3,
    "step_pct": Decimal("0.002"), "levels_count": 3, "reset_threshold_bp": 20.0,
    "regime_gate_enabled": 0.0,
}


def test_a_grid_recenter_is_deferred_while_the_venue_throttles_reads():
    async def body():
        adapter = _ThrottledPollsAdapter(mid=Decimal("100"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        c = GridController(user_id=1, orchestrator=orch, adapter=adapter,
                           inventory=InventoryRepository(), configs=dict(_GRID), controller_id="G")
        await orch.spawn_controller(c)
        assert orch.list(c.id, active_only=True), "ladder armed (gate off)"
        await _cycle(adapter, orch, c)                 # cycle 1: polls denied INSIDE the tick
        assert adapter.reads_throttled_this_cycle() > 0
        c._anchor_mid = Decimal("100")
        adapter.set_mid(Decimal("101"))                # 100bp >= the reset threshold
        await _cycle(adapter, orch, c)                 # cycle 2: decided before its polls -> last cycle's denials
        assert c._last_recenter_ts == 0.0, "recenter under contention = cancel+replace against a denied budget"
        adapter.deny = False
        await _cycle(adapter, orch, c)                 # cycle 3: previous cycle still had denials
        assert c._last_recenter_ts == 0.0
        await _cycle(adapter, orch, c)                 # cycle 4: a clean previous cycle -> recenters
        assert c._last_recenter_ts > 0.0
    asyncio.run(body())


def test_the_contention_signal_is_the_previous_cycles_denials():
    adapter = MockNadoAdapter(mid=Decimal(100))
    assert not adapter.venue_reads_contended()
    adapter._note_read_throttled()
    assert adapter.venue_reads_contended()             # this cycle
    adapter.begin_cycle()
    assert adapter.reads_throttled_this_cycle() == 0
    assert adapter.reads_throttled_last_cycle() == 1
    assert adapter.venue_reads_contended()             # carried one cycle
    adapter.begin_cycle()
    assert not adapter.venue_reads_contended()         # then cleared


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
        adapter = _ThrottledPollsAdapter(mid=Decimal("63373.5"))
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs=dict(_DG, candle_provider=lambda p: [{"close": 63300 + (i % 2) * 20} for i in range(200)]),
        )
        await orch.spawn_controller(c)
        await _cycle(adapter, orch, c)                 # cycle 0: dgrid spawns its ladder inside the tick
        await _cycle(adapter, orch, c)                 # cycle 1: the ladder's polls are denied inside the tick
        assert adapter.reads_throttled_this_cycle() > 0
        levels = lambda: [lv.open_price for lv in orch.list(c.id, active_only=True)[0].levels]  # noqa: E731
        before = levels()
        adapter.set_mid(Decimal("63373.5") * (Decimal(1) + Decimal("0.0030")))   # 30bp: recenters normally
        await _cycle(adapter, orch, c)                 # cycle 2: last cycle's denials -> deferred
        assert levels() == before, "dgrid recentered while the venue was denying status reads"
        adapter.deny = False
        await _cycle(adapter, orch, c)                 # cycle 3: previous cycle still had denials
        assert levels() == before
        await _cycle(adapter, orch, c)                 # cycle 4: clean previous cycle -> recenters
        assert levels() != before
    asyncio.run(body())
