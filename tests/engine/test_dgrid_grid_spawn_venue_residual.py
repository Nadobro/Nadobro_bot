"""D-Grid vs the venue position it finds — the SESSION BASELINE rule (2026-09-16).

DGRID-GRIDRGRID-RESIDUAL (2026-09-02) made both phase spawns close any venue
position "the inventory does not know about" before arming. That closed a
position the USER already held on the product — a manual long, a leftover a
previous stop could not clear — the moment they tapped Start, with a reduce-only
MARKET the user never asked for. The boot stand-down and the standalone Reverse
Grid deliberately leave such a position alone; D-Grid now does the same:

* the venue position on the first successful read of the run is the BASELINE;
* a residual is what is held BEYOND the baseline — only that is closed before
  a phase arms (a partial the previous phase left behind);
* no phase arms until the baseline is known (an unreadable venue defers, and
  says so on the card via the gate telemetry).
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


def _markets(adapter):
    return [o for o in adapter.placed if o.order_type is OrderType.MARKET]


def test_a_pre_existing_position_holds_visibly_and_is_never_closed():
    """The user already holds 0.01 BTC when D-Grid starts: it was not opened by
    this run, so the run HOLDS (card: "an open position on this market was not
    opened by this run"), never market-closes it, and arms with a ZERO baseline
    once it is gone."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"), venue_held={PAIR: Decimal("0.01")})
        orch, c = _ctrl(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._venue_baseline is None
        assert not _markets(adapter), "a pre-existing position must never be market-closed at spawn"
        assert adapter.venue_held[PAIR] == Decimal("0.01"), "the user's position is untouched"
        assert not orch.list(c.id, active_only=True), "and nothing arms on top of it"
        assert c.gate_verdict == "PAUSE" and c.gate_reason == "venue_foreign_position"
        adapter.venue_held[PAIR] = Decimal(0)                    # the user closed it
        await orch.tick_controller(c.id)
        assert c._venue_baseline == Decimal(0)
        assert orch.list(c.id, active_only=True), "arms once the market is clear"
        assert c.gate_verdict == "QUOTE" and c.gate_reason == ""
        assert c.dgrid_metrics()["dgrid_venue_baseline"] == 0.0
    asyncio.run(body())


def test_a_rebuild_restores_the_baseline_and_clears_its_own_residual():
    """Mid-session rebuild: the runtime passes the persisted baseline (0) back in,
    so the run's OWN leftover position is a residual (closed reduce-only before
    the next phase arms) — never a foreign position that blocks the run."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"), venue_held={PAIR: Decimal("0.01")})
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs=dict(_DG, venue_baseline=Decimal(0),
                         candle_provider=lambda p: [{"close": 63300 + (i % 2) * 20} for i in range(200)]),
        )
        assert c._venue_baseline == Decimal(0)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        first = _markets(adapter)[0]
        assert first.side is TradeType.SELL and first.amount_base == Decimal("0.01")
        assert adapter.venue_held[PAIR] == 0 and orch.list(c.id, active_only=True)
    asyncio.run(body())


def test_a_residual_beyond_the_baseline_is_closed_reduce_only_before_the_grid_spawns():
    """A partial the previous phase left behind (held beyond the baseline) is
    still closed reduce-only before the next phase arms — exactly the residual,
    not the baseline."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"), venue_held={PAIR: Decimal("0.03")})
        orch, c = _ctrl(adapter)
        c._venue_baseline = Decimal("0.02")          # the run began with 0.02 held
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        first = _markets(adapter)[0]
        assert first.side is TradeType.SELL
        assert first.amount_base == Decimal("0.01"), "exactly the residual beyond the baseline"
        assert adapter.venue_held[PAIR] == Decimal("0.02"), "the baseline position survives"
        assert orch.list(c.id, active_only=True), "then the grid spawned"
    asyncio.run(body())


def test_an_unreadable_venue_defers_the_first_spawn_and_says_why():
    """No baseline can be taken off a bad read, so nothing arms — and the hold is
    VISIBLE (gate telemetry -> 'Quoting: PAUSED (venue position read unavailable
    — holding)') instead of a silent 'LIVE, 0 orders'. It arms as soon as the
    venue is readable again."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"))         # no venue_held entry -> None
        orch, c = _ctrl(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert not orch.list(c.id, active_only=True), "must not arm blind"
        assert c._venue_baseline is None
        assert c.gate_verdict == "PAUSE" and c.gate_reason == "venue_unreadable"
        adapter.venue_held[PAIR] = Decimal(0)                      # venue readable again
        await orch.tick_controller(c.id)
        assert c._venue_baseline == Decimal(0)
        assert orch.list(c.id, active_only=True), "arms once the baseline is known"
        assert c.gate_verdict == "QUOTE" and c.gate_reason == ""
        assert not _markets(adapter)
    asyncio.run(body())


def test_a_flat_venue_spawns_without_any_close():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"), venue_held={PAIR: Decimal(0)})
        orch, c = _ctrl(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert orch.list(c.id, active_only=True)
        assert not _markets(adapter)
    asyncio.run(body())


def test_the_trend_phase_holds_on_a_pre_existing_position_too():
    """The trigger trend phase must not arm on top of a foreign position either:
    its reduce-only stop could never fill against an opposite-signed one."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"), venue_held={PAIR: Decimal("5")}, auto_fill_market=False)
        orch = ExecutorOrchestrator()
        # A confirmed downtrend selects the RGRID phase (trend delegate) at once.
        closes = [200.0 - i * 0.8 for i in range(200)]
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs={
                "trading_pair": PAIR, "total_amount_quote": Decimal(100),
                "min_spread_between_orders": Decimal("0.002"),
                "start_price": Decimal("99"), "end_price": Decimal("100"),
                "limit_price": Decimal(0), "step_pct": Decimal("0.002"),
                "levels_count": 3, "regime_gate_enabled": 0.0,
                "dgrid_trend_follow": 1, "trend_uses_trigger": True,
                "candle_provider": lambda _p: [
                    {"time": i, "open": v, "high": v + 0.1, "low": v - 0.1, "close": v}
                    for i, v in enumerate(closes)
                ],
            },
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._venue_baseline is None and c._trend is None
        assert c.gate_verdict == "PAUSE" and c.gate_reason == "venue_foreign_position"
        assert not _markets(adapter), "the pre-existing 5 base must not be closed"
        assert adapter.venue_held[PAIR] == Decimal("5")
        adapter.venue_held[PAIR] = Decimal(0)
        await orch.tick_controller(c.id)
        assert c._venue_baseline == Decimal(0)
        assert c.current_phase == "rgrid" and c._trend is not None, "the trend phase arms once clear"
        assert c._trend._baseline_net == Decimal(0)
    asyncio.run(body())


def test_a_venue_hold_never_feeds_the_regime_gates_resume_events():
    """With the regime gate armed (the overlay does this on D-Grid), a venue hold
    used to be read as a gate PAUSE: every QUOTE verdict then walked the resume
    streak and emitted a "resumed quoting" event — one notification every two
    ticks, for the whole hold. Venue holds are not gate verdicts."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"))       # unreadable venue -> hold
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs=dict(_DG, regime_gate_enabled=1.0,
                         candle_provider=lambda p: [{"time": i, "open": 63300, "high": 63320, "low": 63280,
                                                     "close": 63300 + (i % 2) * 20, "volume": 1}
                                                    for i in range(200)]),
        )
        await orch.spawn_controller(c)
        events = []
        for _ in range(6):
            await orch.tick_controller(c.id)
            ev = c.consume_gate_event()
            if ev:
                events.append(ev)
            assert c.gate_verdict == "PAUSE" and c.gate_reason == "venue_unreadable"
        assert events == [], f"a venue hold must never emit gate resume/pause events: {events}"
    asyncio.run(body())


def test_dgrid_surfaces_the_trend_ladder_telemetry_while_the_trend_phase_runs():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"), venue_held={PAIR: Decimal(0)}, auto_fill_market=False)
        orch = ExecutorOrchestrator()
        closes = [200.0 - i * 0.8 for i in range(200)]
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter, inventory=InventoryRepository(),
            configs={
                "trading_pair": PAIR, "total_amount_quote": Decimal(100),
                "min_spread_between_orders": Decimal("0.002"),
                "start_price": Decimal("99"), "end_price": Decimal("100"),
                "limit_price": Decimal(0), "step_pct": Decimal("0.002"),
                "levels_count": 3, "regime_gate_enabled": 0.0,
                "dgrid_trend_follow": 1, "trend_uses_trigger": True,
                "candle_provider": lambda _p: [
                    {"time": i, "open": v, "high": v + 0.1, "low": v - 0.1, "close": v}
                    for i, v in enumerate(closes)
                ],
            },
        )
        assert c.grid_metrics()["grid_rungs_per_side"] == 0, "no ladder before the trend phase"
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c.current_phase == "rgrid"
        m = c.grid_metrics()
        assert m["grid_rungs_per_side"] > 0 and m["grid_rungs_armed"] == 2 * m["grid_rungs_per_side"]
        assert m["grid_step_bp"] > 0
    asyncio.run(body())
