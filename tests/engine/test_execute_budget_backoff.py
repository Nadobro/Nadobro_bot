"""EXECUTE-BUDGET-BACKOFF (prod 2026-09-04, MID BTC 40x).

When the per-wallet EXECUTE bucket is drained, a strategy must BACK OFF its
placements this cycle instead of spamming ~40 doomed orders. The incident: the MID
ladder fired ~40 place_order calls in ~2s, EVERY one "throttled by wallet execute
budget"; each was retried 3x and each executor went FAILED -> a respawn storm. The
sustained request rate drained the budget until READS also 429'd and the gateway
circuit opened, which then broke the user's Stop/close.

A client-side execute throttle is a transient DEFER, never an executor FAILURE:
  1. The live adapter raises AdapterThrottled (not a generic AdapterError) and notes
     it, so controllers can back off.
  2. The executor retry policy does NOT burn its 3 attempts or go FAILED on it.
  3. The orchestrator treats it as a deferral (no FAILED / no respawn churn).
  4. Controllers DEFER further OPENING placements once the budget is throttled this
     cycle (never a reducing/exit — exits must always go through).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.nadobro.engine.adapter.base import AdapterThrottled
from src.nadobro.engine.adapter.nado import NadoAdapter, ProductMeta
from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.executors.grid_executor import GridExecutor, GridExecutorConfig
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import OrderType, TradeType
from tests.engine._mock_nado import MockNadoAdapter


# --------------------------------------------------------------------------
# 1. Adapter machinery — one throttle drains the cycle; resets per cycle.
# --------------------------------------------------------------------------
def test_execute_throttle_counter_resets_each_cycle():
    a = MockNadoAdapter()
    a.begin_cycle()
    assert a.executes_throttled_this_cycle() == 0
    assert a.execute_budget_exhausted() is False

    a._note_execute_throttled()
    assert a.executes_throttled_this_cycle() == 1
    assert a.execute_budget_exhausted() is True     # one throttle => bucket drained

    a.begin_cycle()                                 # next cycle
    assert a.executes_throttled_this_cycle() == 0
    assert a.execute_budget_exhausted() is False


# --------------------------------------------------------------------------
# 2. Live adapter raises AdapterThrottled (NOT a generic AdapterError) + notes it.
# --------------------------------------------------------------------------
class _ThrottleClient:
    """place_limit_order -> place_order returns the client's rate_limited dict when the
    per-wallet execute bucket is drained (nado_client.place_order:3295)."""

    def place_limit_order(self, *a, **k):
        return {"success": False, "error": "Rate limited — please retry in a moment.", "rate_limited": True}

    def get_market_price(self, product_id):
        return {"bid": 99.0, "ask": 101.0}


_PERP = {"AAA-PERP": ProductMeta(product_id=9, tick_size=Decimal("0.01"),
                                 lot_size=Decimal("0.001"), min_notional=Decimal(1),
                                 is_perp=True)}


def test_live_adapter_raises_throttled_and_notes_it_on_rate_limited():
    async def body():
        a = NadoAdapter(_ThrottleClient(), _PERP)
        a.begin_cycle()
        with pytest.raises(AdapterThrottled):
            await a.place_order("AAA-PERP", TradeType.BUY, OrderType.LIMIT_MAKER,
                                Decimal("0.01"), price=Decimal("100"), reduce_only=False)
        assert a.execute_budget_exhausted() is True   # noted, so controllers can defer

    asyncio.run(body())


# --------------------------------------------------------------------------
# 3. Executor + orchestrator: a throttle DEFERS (not FAILED), with no 3x retry.
# --------------------------------------------------------------------------
class _ThrottlingAdapter(MockNadoAdapter):
    """Every OPENING placement is throttled by the execute budget (drained bucket):
    note + raise AdapterThrottled, exactly like the live NadoAdapter. Reduces are
    exempt (they must always reach the venue)."""

    async def place_order(self, *args, **kwargs):
        reduce_only = kwargs.get("reduce_only", args[6] if len(args) > 6 else False)
        if not reduce_only:
            self._note_execute_throttled()
            raise AdapterThrottled("place_order throttled by execute budget: Rate limited")
        return await super().place_order(*args, **kwargs)


def _grid_cfg() -> GridExecutorConfig:
    return GridExecutorConfig(
        trading_pair="BTC-PERP", side=TradeType.BUY,
        start_price=Decimal("99"), end_price=Decimal("100"), limit_price=Decimal(0),
        total_amount_quote=Decimal(80), min_spread_between_orders=Decimal("0.002"),
        max_open_orders=8,
    )


def test_spawn_throttle_defers_not_fails_and_does_not_retry():
    async def body():
        adapter = _ThrottlingAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_grid_cfg(), user_id=1, controller_id="G", adapter=adapter,
                          inventory=InventoryRepository())
        adapter.begin_cycle()
        ok = await orch.spawn(ex)

        assert ok is False                                    # spawn deferred, not spawned
        reason = orch.last_spawn_reason("G") or ""
        assert reason.startswith("execute_throttled"), reason  # NOT executor_failed
        assert ex.retries == 0                                # the 3-attempt loop was skipped
        assert adapter.execute_budget_exhausted() is True
        # No zombie leak: the un-started (order-less) executor is POPPED from the registry,
        # so it never counts toward max_open_executors and never strands the strategy
        # (strategy-auditor 2026-09-05).
        assert ex.id not in orch._executors
        assert orch.list("G", active_only=True) == []

    asyncio.run(body())


def test_repeated_throttled_spawns_do_not_leak_executors():
    """Regression for the zombie leak: a SUSTAINED throttle must not accumulate ACTIVE
    order-less executors that would saturate max_open_executors and take the strategy
    dark even after the budget refills (strategy-auditor 2026-09-05)."""
    async def body():
        adapter = _ThrottlingAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        for _ in range(6):                                    # six throttled spawn cycles
            adapter.begin_cycle()
            ex = GridExecutor(_grid_cfg(), user_id=1, controller_id="GL", adapter=adapter,
                              inventory=InventoryRepository())
            await orch.spawn(ex)
        assert orch.list("GL", active_only=True) == []        # zero zombies accumulated

    asyncio.run(body())


def test_mid_replace_quote_holds_old_order_on_throttle():
    """A throttled FUSED requote is atomic (old order untouched), so _replace_quote returns
    True (hold) — the caller must NOT cancel the resting quote and respawn, which would
    leave a gap (and, pre-fix, leak an executor). strategy-auditor 2026-09-05."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))

        async def _throttle(*a, **k):
            adapter._note_execute_throttled()
            raise AdapterThrottled("cancel_and_place throttled by execute budget")

        adapter.cancel_and_place = _throttle
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(_MM_CFG))
        held = await c._replace_quote(TradeType.BUY, Decimal("99"), Decimal("10"),
                                      "oldid", "0xolddigest", level=0)
        assert held is True                                   # held -> caller does NOT cancel+respawn

    asyncio.run(body())


def test_mid_resumes_placing_next_cycle_after_throttle_clears():
    """The gate is PER-CYCLE: begin_cycle clears it, so placements resume once the bucket
    refills — the strategy is never permanently stuck by the gate."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(_MM_CFG))
        await orch.spawn_controller(c)

        adapter.begin_cycle()
        adapter._note_execute_throttled()                     # cycle 1: budget drained
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"), is_opening=True)
        assert adapter.placed == []                           # opening deferred

        adapter.begin_cycle()                                 # cycle 2: fresh, refilled
        assert adapter.execute_budget_exhausted() is False
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"), is_opening=True)
        assert len(adapter.placed) == 1                       # opening resumes

    asyncio.run(body())


def test_grid_recenter_defers_on_execute_throttle_wiring():
    """grid_trading / dynamic_grid recenter runs BEFORE the per-executor SL/TP tick loop and
    calls place_order directly (not via orchestrator.tick), so under an execute throttle it
    must DEFER (not escape uncaught and skip the SL/TP tick). Verified by source so it runs
    in the deps-free path (adversarial audit 2026-09-05)."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    grid = (repo / "src" / "nadobro" / "engine" / "controllers" / "grid_trading.py").read_text()
    dgrid = (repo / "src" / "nadobro" / "engine" / "controllers" / "dynamic_grid.py").read_text()
    # Uses *contended* (this OR last cycle), because the recenter runs before this cycle's
    # own placements set the flag (re-audit R1).
    assert "venue_reads_contended() or self.adapter.execute_budget_contended()" in grid
    assert "not self.adapter.execute_budget_contended()" in dgrid


def test_execute_budget_contended_carries_last_cycle():
    """The recenter runs BEFORE this cycle's placements, so it must defer on LAST cycle's
    throttle (execute_budget_contended = this OR last), mirroring venue_reads_contended —
    otherwise the recenter's own first throttle escapes on_tick (re-audit R1, 2026-09-05)."""
    a = MockNadoAdapter()
    a.begin_cycle()
    a._note_execute_throttled()                      # cycle 1: a placement throttled
    assert a.execute_budget_contended() is True

    a.begin_cycle()                                  # cycle 2: fresh — this-cycle count is 0
    assert a.execute_budget_exhausted() is False     # the opening gate (this-cycle) is open...
    assert a.execute_budget_contended() is True       # ...but the recenter still defers on last cycle

    a.begin_cycle()                                  # cycle 3: nothing throttled last cycle
    assert a.execute_budget_contended() is False


def test_grid_lays_no_rungs_when_execute_budget_already_throttled():
    """The controller gate: with the execute budget already throttled this cycle, the
    grid skips EVERY rung (no placement even attempted) rather than spamming them."""
    async def body():
        adapter = _ThrottlingAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_grid_cfg(), user_id=1, controller_id="G2", adapter=adapter,
                          inventory=InventoryRepository())
        adapter.begin_cycle()
        adapter._note_execute_throttled()          # a prior placement already throttled
        await orch.spawn(ex)
        assert adapter.placed == []                # gate skipped every rung
        assert ex.is_terminated is False

    asyncio.run(body())


# --------------------------------------------------------------------------
# 4. MID — defer the OPENING side on an execute throttle, NEVER the reducing side.
# --------------------------------------------------------------------------
_MM_CFG = dict(trading_pair="BTC", spread_bp="10", order_amount_quote="10", levels="1", leverage="1")


def test_mid_defers_opening_on_execute_throttle_but_never_reducing():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(_MM_CFG))
        await orch.spawn_controller(c)

        adapter.begin_cycle()
        adapter._note_execute_throttled()          # execute budget drained this cycle
        assert adapter.execute_budget_exhausted() is True

        # OPENING side, empty slot, budget drained -> DEFERRED.
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"), is_opening=True)
        assert adapter.placed == []

        # REDUCING side, same drained budget -> PLACED. Exits must always go through.
        await c._reconcile(TradeType.SELL, Decimal("101"), True, Decimal("100"), is_opening=False)
        assert len(adapter.placed) == 1
        assert adapter.placed[0].side is TradeType.SELL

    asyncio.run(body())
