"""RGRID-STALE-LADDER — the re-center threshold must be bounded by the ladder.

Reported 2026-07-28: "the bot just places orders and does not quote again until
the market comes and picks the order". Investigated on production (Fly, app
nadobro-bot) against RGRID session 165 (user 5776741680, BTC-PERP, mainnet,
06:28 -> 13:32 UTC, stopped by session SL):

    total_cycles            2080        <- one tick every ~12s for 7 hours
    total_orders_placed       26        <- one order every ~16 minutes
    zero_order_cycles       2063        <- 99.2% of cycles placed nothing
    levels                     3
    spread_bp               10.0        -> ladder band = 10 x (3-1) = 20bp
    dgrid_reset_threshold_bp  80.0      <- 4x the band it is supposed to track
    dgrid_realized_move_bp   17.83      <- never got close to firing

The fills prove the ladder froze: the SAME maker price
(63352.4017854148, to ten decimal places) was re-issued from 09:58 to 13:27 —
three and a half hours — with gaps of 83, 39 and 126 minutes between fills.
BTC's ENTIRE range over the session was 63189 -> 63502 = 49bp, so
``realized_move_bp >= 80`` was arithmetically unreachable: the grid could not
re-center once, by construction.

Root cause: ``rgrid_reset_threshold_pct`` is a percent of PRICE (registry
default 1.0%; UI presets 0.8% / 1.5%) mapped straight to
``dgrid_reset_threshold_bp``, and the controller then applied it as
``max(user, 12bp, band/2)`` — a one-sided FLOOR. Nothing related the threshold
to the width of the ladder it steers, and ``_recycle_completed_levels`` re-arms
a round-tripped level at its ORIGINAL price, so the re-center is the ONLY thing
that ever moves a ladder toward price.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.controller_base import (
    LADDER_RECENTER_FLOOR_BP,
    ladder_recenter_threshold_bp,
)
from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.controllers.grid_trading import GridController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator


# ── the pure geometry rule ──────────────────────────────────────

def test_production_session_165_threshold_is_capped_to_the_band():
    """80bp asked of a 20bp ladder must come back as 20bp."""
    bp, clamped = ladder_recenter_threshold_bp(step_bp=10.0, levels=3, user_bp=80.0)
    assert clamped is True
    assert bp == 20.0


@pytest.mark.parametrize("user_pct", [0.8, 1.0, 1.5])
def test_every_shipped_rgrid_reset_preset_is_capped(user_pct):
    """The registry default (1.0%) and both UI presets all exceed the band.

    This is why the bug was not one user's misconfiguration: on a 10bp x 3
    ladder every value the product offers is 4x-7.5x too wide.
    """
    bp, clamped = ladder_recenter_threshold_bp(
        step_bp=10.0, levels=3, user_bp=user_pct * 100.0)
    assert clamped is True and bp == 20.0


def test_unset_threshold_keeps_the_band_width_auto_follow():
    assert ladder_recenter_threshold_bp(step_bp=10.0, levels=3, user_bp=0.0) == (20.0, False)


def test_a_reasonable_user_value_inside_the_band_is_honored():
    """The cap must not steamroll a value that is already sane."""
    bp, clamped = ladder_recenter_threshold_bp(step_bp=20.0, levels=5, user_bp=45.0)
    assert clamped is False and bp == 45.0   # band = 80bp, so 45 stands


def test_threshold_never_churns_inside_one_level_step():
    """Below one step a re-center cancels and re-places at the same rung."""
    bp, clamped = ladder_recenter_threshold_bp(step_bp=30.0, levels=3, user_bp=1.0)
    assert clamped is False and bp == 30.0


def test_tiny_ladders_keep_the_absolute_floor():
    bp, _ = ladder_recenter_threshold_bp(step_bp=1.0, levels=2, user_bp=0.5)
    assert bp == LADDER_RECENTER_FLOOR_BP


def test_degenerate_geometry_does_not_explode():
    assert ladder_recenter_threshold_bp(0.0, 0, 0.0)[0] == LADDER_RECENTER_FLOOR_BP
    assert ladder_recenter_threshold_bp(0.0, 1, -5.0)[0] == LADDER_RECENTER_FLOOR_BP


# ── the behaviour that was broken in production ─────────────────

_PROD_CFG = {
    # Session 165's real geometry: BTC, 10bp step, 3 levels, 0.8% reset.
    "trading_pair": "BTC-PERP",
    "start_price": "63200", "end_price": "63400", "limit_price": "0",
    "total_amount_quote": "1000", "min_spread_between_orders": "0.001",
    "max_open_orders": 3, "step_pct": "0.001", "levels_count": 3,
    "dgrid_reset_threshold_bp": 80.0,
    "regime_gate_enabled": 0.0,
}


def _range_candles():
    return [{"close": 63300 + (i % 2) * 20} for i in range(200)]


def test_rgrid_ladder_follows_a_150_dollar_btc_move():
    """The reported symptom: BTC moved >$150 and the orders did not move.

    $150 on BTC at 63373 is 23.7bp — under the old 80bp threshold, over the
    20bp band. The ladder must now re-quote toward the new mid.
    """
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"))
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(),
            configs=dict(_PROD_CFG, candle_provider=lambda p: _range_candles()),
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        ex = orch.list(c.id, active_only=True)[0]
        before = [lv.open_price for lv in ex.levels]
        assert before, "the ladder must arm before we can test following"
        assert c.reset_threshold_bp == 20.0, "80bp must be capped to the 20bp band"

        # BTC +$150.
        adapter.set_mid(Decimal("63523.5"))
        await orch.tick_controller(c.id)

        after = [lv.open_price for lv in orch.list(c.id, active_only=True)[0].levels]
        assert after != before, (
            "the ladder froze: a 23.7bp move left every quote at its old price — "
            "this is the production bug (2063/2080 cycles placed nothing)"
        )
        assert max(after) > max(before), "quotes must move toward the new mid"

    asyncio.run(body())


def test_recenter_is_not_starved_by_an_unconfirmed_phase_flip():
    """Re-center used to live in the `else` of `if flip_needed:`.

    dgrid needs 2 consecutive ticks to confirm a regime change, so on every tick
    where the classifier disagreed with the live phase the ladder ALSO skipped its
    re-center — it froze during exactly the moves big enough to shift the regime
    read.

    AUDIT round 4: the original version of this test never actually produced an
    unconfirmed flip, and it wrapped its assertion in `if live[0] is ex:` so it
    could pass by asserting nothing. It now asserts the precondition explicitly
    (_phase_confirm_streak == 1, i.e. a flip IS pending) and has no escape hatch.
    """
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"))
        orch = ExecutorOrchestrator()
        # A decisive DOWNTREND makes the classifier want RGRID while the live
        # phase is GRID; with flip_confirm_ticks=2 tick 1 leaves the flip PENDING.
        downtrend = [{"close": 64000 - i * 25} for i in range(200)]
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(),
            configs=dict(_PROD_CFG, candle_provider=lambda p: _range_candles(),
                         dgrid_flip_confirm_ticks=2),
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        ex = orch.list(c.id, active_only=True)[0]
        assert c.current_phase == "grid", c.current_phase
        before = [lv.open_price for lv in ex.levels]

        # Same tick: the regime flips to RGRID (pending) AND price moves past the
        # re-center threshold.
        c.configs["candle_provider"] = lambda p: downtrend
        adapter.set_mid(Decimal("63000.0"))
        await orch.tick_controller(c.id)

        assert c._phase_confirm_streak == 1, (
            f"precondition failed: no flip is pending (streak="
            f"{c._phase_confirm_streak}, phase={c.current_phase}) — the test is "
            f"not exercising the starvation path it claims to"
        )
        live = orch.list(c.id, active_only=True)
        assert live and live[0] is ex, "the flip should still be UNCONFIRMED"
        assert [lv.open_price for lv in ex.levels] != before, (
            "an unconfirmed flip suppressed the re-center — the ladder froze on "
            "exactly the move that shifted the classifier"
        )

    asyncio.run(body())


def test_classic_grid_shares_the_same_cap():
    """GridController carried the identical one-sided floor."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"))
        orch = ExecutorOrchestrator()
        c = GridController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(),
            configs={
                "trading_pair": "BTC-PERP", "start_price": Decimal("99"),
                "end_price": Decimal("100"), "limit_price": Decimal(0),
                "total_amount_quote": Decimal(1000),
                "min_spread_between_orders": Decimal("0.001"),
                "max_open_orders": 3, "step_pct": Decimal("0.001"),
                "levels_count": 3, "regime_gate_enabled": 0.0,
                # 1.0% — the registry default, 5x this ladder's 20bp band.
                "reset_threshold_bp": 100.0,
            },
            controller_id="G",
        )
        await orch.spawn_controller(c)
        assert c.reset_threshold_bp == 20.0

    asyncio.run(body())


# ── audit follow-up: the re-center must not eat the trailing exit ──
# Found while auditing 6946ee5. Making the re-center actually fire exposed a
# latent coupling: ``_update_run_extremes`` measured the run's favorable move
# from ``_grid_anchor_mid``, and ``_recenter`` re-seeds that same field. While
# the re-center was effectively dead (the 4x-too-wide threshold) the anchor
# stayed at spawn and the trailing arm worked. Once it fired every band width,
# the anchor chased price, the favorable move read ~0, ``_run_armed`` never went
# True and ``_maybe_reversal_flip`` — the exit that closes a winning run in
# profit — was silently disabled. Two different jobs, two anchors.

_TRAIL_CFG = {
    "trading_pair": "P", "start_price": "98", "end_price": "102", "limit_price": "0",
    "total_amount_quote": "100", "min_spread_between_orders": "0.002",
    "max_open_orders": 4, "step_pct": "0.002", "levels_count": 3,
    "dgrid_trail_arm_pct": 0.5, "dgrid_reversal_flip_pct": 0.3,
    "regime_gate_enabled": 0.0,
}


def _flat_range(n=60, base=100.0, amp=0.12, period=7.0):
    import math
    return [{"high": base + amp * math.sin(2 * math.pi * i / period) + 1,
             "low": base + amp * math.sin(2 * math.pi * i / period) - 1,
             "close": base + amp * math.sin(2 * math.pi * i / period)} for i in range(n)]


def test_recenter_does_not_reset_the_trailing_arm_anchor():
    """A +0.6% run must arm a 0.5% trail even though it re-centered on the way."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"))
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(),
            configs=dict(_TRAIL_CFG, candle_provider=lambda p: _flat_range()),
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        spawn_anchor = c._grid_anchor_mid

        for px in ("100.15", "100.30", "100.45", "100.60"):
            adapter.set_mid(Decimal(px))
            await orch.tick_controller(c.id)

        assert c._grid_anchor_mid != spawn_anchor, (
            "precondition: the ladder must have re-centered during the run"
        )
        assert c._run_armed is True, (
            "the re-center moved the trail's reference with it, so a 0.6% "
            "favorable run never cleared the 0.5% arm — the trailing take-profit "
            "was silently disabled"
        )

    asyncio.run(body())


def test_trail_anchor_survives_many_recenters():
    """The geometry anchor tracks price; the run anchor stays at the entry."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("100"))
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(),
            configs=dict(_TRAIL_CFG, candle_provider=lambda p: _flat_range()),
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        run_anchor = c._run_anchor_mid
        assert run_anchor is not None

        for px in ("100.5", "101.0", "101.5", "102.0"):
            adapter.set_mid(Decimal(px))
            await orch.tick_controller(c.id)
            if c.current_phase != "grid":
                return          # a flip legitimately re-seeds the run
            assert c._run_anchor_mid == run_anchor, (
                "the run anchor must only move on spawn / flip, never on a re-center"
            )

    asyncio.run(body())


# ── audit follow-up: a failed cancel must not orphan a live order ──
# Found while auditing 6946ee5. ``recenter`` cancelled each stale maker, then
# freed the slot and re-quoted REGARDLESS of whether the cancel succeeded.
# ``NadoAdapter.cancel_order`` only returns after verifying the order is off the
# book, so a raised error means it is still LIVE — and the executor had just
# dropped its id, so nothing could ever cancel it again. The ladder then ran at
# double the intended exposure. Dormant while the re-center never fired; routine
# once the threshold was capped to the band.

def _recenter_cfg():
    from src.nadobro.engine.executors.grid_executor import GridExecutorConfig
    from src.nadobro.engine.types import TradeType
    return GridExecutorConfig(
        trading_pair="BTC-PERP", side=TradeType.BUY,
        start_price=Decimal("99"), end_price=Decimal("100"), limit_price=Decimal(0),
        total_amount_quote=Decimal(100),
        min_spread_between_orders=Decimal("0.002"), max_open_orders=3,
    )


def test_failed_recenter_cancel_never_orphans_a_live_order():
    from src.nadobro.engine.executors.grid_executor import GridExecutor, GridLevelState

    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recenter_cfg(), user_id=1, controller_id="G",
                          adapter=adapter, inventory=InventoryRepository())
        await orch.spawn(ex)
        before = {lv.open_order_id for lv in ex.levels
                  if lv.state is GridLevelState.OPEN_ORDER_PLACED}
        assert before

        # Every cancel fails — a throttled gateway or a venue reject.
        adapter.fail_on = {"cancel_order"}
        adapter.fail_remaining = 99
        await ex.recenter(Decimal("109"), Decimal("110"))

        live = {oid for oid, o in adapter._orders.items() if not o.state.is_terminal}
        tracked = {lv.open_order_id for lv in ex.levels if lv.open_order_id}
        assert not (live - tracked), (
            f"orphaned live order(s) {sorted(live - tracked)}: the level was freed "
            f"and re-quoted while the order stayed on the book — double exposure "
            f"with no id left to cancel it"
        )
        assert before <= tracked, "the un-cancelled orders must stay owned by a level"

    asyncio.run(body())


def test_a_failed_cancel_does_not_double_the_quote_count():
    from src.nadobro.engine.executors.grid_executor import GridExecutor

    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recenter_cfg(), user_id=1, controller_id="G",
                          adapter=adapter, inventory=InventoryRepository())
        await orch.spawn(ex)
        placed_before = len(adapter.placed)

        adapter.fail_on = {"cancel_order"}
        adapter.fail_remaining = 99
        await ex.recenter(Decimal("109"), Decimal("110"))

        assert len(adapter.placed) == placed_before, (
            "nothing new may be quoted while the old orders are still resting"
        )
        assert len(ex.levels) <= ex.config.max_open_orders

    asyncio.run(body())


def test_a_successful_recenter_still_requotes():
    """The fail-closed guard must not block the normal path."""
    from src.nadobro.engine.executors.grid_executor import GridExecutor

    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recenter_cfg(), user_id=1, controller_id="G",
                          adapter=adapter, inventory=InventoryRepository())
        await orch.spawn(ex)
        placed_before = len(adapter.placed)

        await ex.recenter(Decimal("109"), Decimal("110"))

        assert len(adapter.placed) > placed_before, "a clean re-center must re-quote"
        assert min(lv.open_price for lv in ex.levels) >= Decimal("109")

    asyncio.run(body())


# ── audit round 3: a re-center must never be able to KILL the ladder ──
# Probed: 4 re-centers whose cancels fail drove _guard's failure budget to
# terminate the executor FAILED. _terminate() does not cancel resting orders, and
# recenter returns early on is_terminated — so the ladder never re-quoted again
# even after the venue recovered, reproducing the exact stale-quote symptom this
# branch exists to fix. A re-center is OPPORTUNISTIC: it retries next cycle, so a
# transient cancel failure must cost nothing beyond that round.

def test_repeated_recenter_cancel_failures_do_not_kill_the_ladder():
    from src.nadobro.engine.executors.grid_executor import GridExecutor

    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recenter_cfg(), user_id=1, controller_id="G",
                          adapter=adapter, inventory=InventoryRepository())
        await orch.spawn(ex)

        adapter.fail_on = {"cancel_order"}
        adapter.fail_remaining = 999
        for i in range(4):
            await ex.recenter(Decimal(105 + i), Decimal(106 + i))

        assert not ex.is_terminated, (
            f"the ladder was terminated ({ex.close_type}) by transient re-center "
            f"cancel failures — it can never re-quote again, which IS the original "
            f"stale-quote bug"
        )

    asyncio.run(body())


def test_the_ladder_requotes_once_the_venue_recovers():
    from src.nadobro.engine.executors.grid_executor import GridExecutor

    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recenter_cfg(), user_id=1, controller_id="G",
                          adapter=adapter, inventory=InventoryRepository())
        await orch.spawn(ex)
        original = sorted(lv.open_price for lv in ex.levels)

        adapter.fail_on = {"cancel_order"}
        adapter.fail_remaining = 999
        for i in range(4):
            await ex.recenter(Decimal(105 + i), Decimal(106 + i))

        adapter.fail_on = set()
        adapter.fail_remaining = 0
        placed_before = len(adapter.placed)
        await ex.recenter(Decimal("109"), Decimal("110"))

        assert len(adapter.placed) > placed_before, (
            "the ladder did not re-quote after the venue recovered"
        )
        assert sorted(lv.open_price for lv in ex.levels) != original, (
            "quotes are still at their original prices after recovery"
        )
        assert min(lv.open_price for lv in ex.levels) >= Decimal("109")

    asyncio.run(body())


def test_a_failed_status_probe_keeps_the_level_instead_of_discarding_a_fill():
    """AUDIT round 4: the cancel path failed CLOSED but the status probe failed
    OPEN. The cancel succeeded, so the order is off the book — but an unknown
    amount may have FILLED first. Freeing the slot re-quotes it as empty and the
    held base never gets a close leg, so the real position drifts from the ladder's
    view of it."""
    from src.nadobro.engine.executors.grid_executor import GridExecutor, GridLevelState

    async def body():
        adapter = MockNadoAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        ex = GridExecutor(_recenter_cfg(), user_id=1, controller_id="G",
                          adapter=adapter, inventory=InventoryRepository())
        await orch.spawn(ex)
        before = {lv.open_order_id for lv in ex.levels
                  if lv.state is GridLevelState.OPEN_ORDER_PLACED}
        assert before

        # Cancels succeed; the STATUS probe blows up (a 429 / transient blip).
        adapter.fail_on = {"order_status"}
        adapter.fail_remaining = 99
        await ex.recenter(Decimal("109"), Decimal("110"))

        tracked = {lv.open_order_id for lv in ex.levels if lv.open_order_id}
        assert before <= tracked, (
            "a level whose fill state is UNKNOWN was freed and re-quoted — any "
            "partial fill on it is now unaccounted for"
        )

    asyncio.run(body())
