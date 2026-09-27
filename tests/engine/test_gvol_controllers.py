"""The OPT-IN realized-volatility model in the grid-family controllers.

1. OFF == base branch, byte for byte: with the vol model absent, and with every
   ``gvol_*`` key the mapper emits explicitly OFF, each controller's venue-call
   log over a fixed tape equals the golden log recorded on the base branch
   (tests/engine/gvol_scenarios.py).
2. ON behaviour per strategy (docs/grid_vol_model.md): stand-down withdraws
   entries and holds (never a taker, never a close-leg cancel), resume after
   15 calm minutes re-lays the ladder once, DENIED != EMPTY on candle reads,
   the hard cap binds (held + resting <= cap + one level), skew / spacing act
   on NEW opens only, D-Grid never switches to the trend phase under the Vol
   model, R-Grid arms only on expansion after compression and never gates exits.
"""
from __future__ import annotations

import asyncio
import json
import math
import pathlib
from decimal import Decimal
from typing import Callable, List, Optional

import pytest

from tests.engine import gvol_scenarios as gs
from tests.engine.gvol_scenarios import PAIR, FakeClock, RecordingAdapter

from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController
from src.nadobro.engine.controllers.grid_trading import GridController
from src.nadobro.engine.controllers.reverse_grid import ReverseGridController
from src.nadobro.engine.executors.grid_executor import GridLevelState
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.routines import variance_regime
from src.nadobro.engine.types import OrderType, TradeType
from src.nadobro.quant import vol_model as vm

GOLDEN = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "gvol_off_golden.json").read_text()
)

# What strategy/engine_runtime._gvol_config emits for a user who never touched
# the Vol settings (every feature OFF). Kept in sync by test_gvol_mapping.py.
GVOL_ALL_OFF = {
    "gvol_gate_enabled": False, "gvol_gate_mult": 0.82,
    "gvol_spacing_enabled": False, "gvol_spacing_k": 2.6,
    "gvol_spacing_floor_bp": 6.8, "gvol_spacing_min_bp": 0.0, "gvol_spacing_max_bp": 0.0,
    "gvol_skew_enabled": False, "gvol_cap_hard": False, "gvol_cap_pct": 30.0,
}
RGRID_ALL_OFF = {
    "gvol_arm_enabled": False, "gvol_arm_compress_mult": 0.91, "gvol_arm_expand_mult": 1.42,
}


# --------------------------------------------------------------------------- 1
@pytest.mark.parametrize("name", sorted(gs.SCENARIOS))
@pytest.mark.parametrize("extra", ["absent", "explicit_off"])
def test_decisions_identical_to_base_branch_with_the_vol_model_off(name, extra):
    over = None
    if extra == "explicit_off":
        over = dict(RGRID_ALL_OFF if name == "rgrid" else GVOL_ALL_OFF)
        if name == "dgrid":
            over["dgrid_regime_model"] = "vr"
    log = json.loads(json.dumps(gs.SCENARIOS[name](over)))
    assert log == GOLDEN[name]


# --------------------------------------------------------------------------- helpers
class CandleSource:
    """1m candles whose per-minute |log return| is ``amp_fn(minute_open_ts)``
    (alternating sign), consistent across calls. ``empty`` models a budget-denied
    / failed read (the provider returns [])."""

    def __init__(self, clock: FakeClock, amp_fn: Callable[[int], float], n: int = 200) -> None:
        self.clock = clock
        self.amp_fn = amp_fn
        self.n = n
        self.empty = False
        self.calls = 0

    async def __call__(self, _pair: str) -> list:
        self.calls += 1
        if self.empty:
            return []
        cur = int(self.clock.now // 60) * 60
        origin = int(gs.T0 // 60) * 60 - 60 * 400
        px = 100.0
        rows = []
        m = origin
        while m <= cur:
            a = self.amp_fn(m)
            px *= math.exp((a if (m // 60) % 2 == 0 else -a) / 1e4)
            if m >= cur - 60 * self.n:
                rows.append({"time": m, "close": px})
            m += 60
        return list(reversed(rows))


def _baseline(median: float = 4.0):
    async def provider(_pair, _candles):
        return vm.VolBaseline(median_rv60_bp=median, coverage_h=168.0, n_minutes=10080,
                              newest_ts=0)
    return provider


CALM_AMP, HOT_AMP = 2.0, 8.0      # gate = 0.82 x 4.0 = 3.28 bp/min


def _is_open_entry(entry: list) -> bool:
    return entry[0] == "place" and entry[6] is False


def _assert_no_taker_and_no_close_cancel(adapter: RecordingAdapter) -> None:
    """Guardrail: the vol model never places a MARKET / crossing non-reduce-only
    order and never cancels a reduce-only (close) order."""
    reduce_ids = {e[1] for e in adapter.log if e[0] == "place" and e[6]}
    for e in adapter.log:
        if e[0] == "place":
            assert e[3] != OrderType.MARKET.name or e[6], e
            if e[3] == OrderType.LIMIT.name:
                assert e[6], f"crossing non-reduce-only order: {e}"
        if e[0] == "cancel":
            assert e[1] not in reduce_ids, f"cancelled a close leg: {e}"


def _grid(clock, candles, adapter, **extra):
    cfg = gs.grid_cfg({**GVOL_ALL_OFF, "candle_provider": candles,
                       "gvol_baseline_provider": _baseline(), **extra})
    orch = ExecutorOrchestrator()
    c = GridController(user_id=1, orchestrator=orch, adapter=adapter,
                       inventory=InventoryRepository(), configs=cfg, controller_id="G")
    return orch, c


async def _step(adapter, clock, c, px, *, dt: float = gs.TICK_S) -> None:
    mid = Decimal(px)
    adapter.set_mid(mid)
    adapter.cross_resting(mid)
    clock.now += dt
    await c.on_tick()


# --------------------------------------------------------------------------- grid
def test_grid_start_while_hot_places_no_entries_until_calm_then_relays_once():
    async def body():
        with FakeClock() as clock:
            hot_until = int(clock.now // 60) * 60 + 60 * 3
            src = CandleSource(clock, lambda m: HOT_AMP if m < hot_until else CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter, gvol_gate_enabled=True)
            await orch.spawn_controller(c)
            assert not any(_is_open_entry(e) for e in adapter.log)
            assert c.gate_reason == "vol_hot" and c.gate_paused
            # Still hot / dwelling: nothing enters.
            for _ in range(6):
                await _step(adapter, clock, c, "100")
            assert not any(_is_open_entry(e) for e in adapter.log)
            # 60 hot minutes must roll out of the rv60 window plus 15 calm minutes.
            ticks = 0
            while not any(_is_open_entry(e) for e in adapter.log):
                await _step(adapter, clock, c, "101")
                ticks += 1
                assert ticks < 400
            assert c.gvol_gate.state == vm.CALM
            opens = [e for e in adapter.log if _is_open_entry(e)]
            # Re-laid around the CURRENT mid (101), not the stale spawn band (~99.7).
            assert min(Decimal(e[5]) for e in opens) > Decimal("100.5")
            _assert_no_taker_and_no_close_cancel(adapter)
    asyncio.run(body())


def test_grid_hot_withdraws_entries_keeps_close_legs_and_holds():
    async def body():
        with FakeClock() as clock:
            state = {"hot": False}
            src = CandleSource(clock, lambda m: HOT_AMP if state["hot"] else CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter, gvol_gate_enabled=True)
            await orch.spawn_controller(c)
            assert c.gvol_gate.state == vm.CALM
            await _step(adapter, clock, c, "99.85")      # fills the top rungs
            ex = c.my_executors()[0]
            held = [lv for lv in ex.levels if lv.state is GridLevelState.CLOSE_ORDER_PLACED]
            assert held, "a fill should have booked a close leg"
            close_ids = {lv.close_order_id for lv in held}
            state["hot"] = True
            await _step(adapter, clock, c, "99.85", dt=60)
            assert c.gate_reason == "vol_hot"
            resting_opens = [lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED]
            assert resting_opens == []
            assert c.gvol_withdrawn > 0
            still = {lv.close_order_id for lv in ex.levels
                     if lv.state is GridLevelState.CLOSE_ORDER_PLACED}
            assert close_ids <= still
            assert ex.inventory.get(1, PAIR, "G").net_amount_base > 0     # held
            # Further hot ticks place no entry.
            n_before = len([e for e in adapter.log if _is_open_entry(e)])
            for _ in range(5):
                await _step(adapter, clock, c, "99.9")
            assert len([e for e in adapter.log if _is_open_entry(e)]) == n_before
            _assert_no_taker_and_no_close_cancel(adapter)
            m = c.grid_metrics()
            assert m["gvol_state"] == "HOT" and m["gvol_withdrawn"] >= 1
    asyncio.run(body())


def test_grid_withdraw_cancel_failure_keeps_level_bound():
    async def body():
        with FakeClock() as clock:
            state = {"hot": False}
            src = CandleSource(clock, lambda m: HOT_AMP if state["hot"] else CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"), fail_on=["cancel_order"], fail_times=0)
            orch, c = _grid(clock, src, adapter, gvol_gate_enabled=True)
            await orch.spawn_controller(c)
            ex = c.my_executors()[0]
            n_resting = len([lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED])
            adapter.fail_remaining = 100          # every cancel fails
            state["hot"] = True
            await _step(adapter, clock, c, "100", dt=60)
            bound = [lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED]
            assert len(bound) == n_resting and all(lv.open_order_id for lv in bound)
    asyncio.run(body())


def test_denied_candles_hold_last_verdict_then_fail_safe_after_180s():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter, gvol_gate_enabled=True)
            await orch.spawn_controller(c)
            assert c.gvol_gate.state == vm.CALM
            src.empty = True                     # budget-denied reads from here on
            await _step(adapter, clock, c, "100", dt=60)
            assert c.gvol_gate.state == vm.CALM   # <= 180s: last good reading holds
            assert not c.gate_paused
            await _step(adapter, clock, c, "100", dt=150)
            assert c.gvol_gate.state == vm.UNKNOWN
            assert c.gate_reason == "vol_unknown" and c.gate_paused
            ex = c.my_executors()[0]
            assert not [lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED]
            _assert_no_taker_and_no_close_cancel(adapter)
    asyncio.run(body())


def test_grid_hard_cap_binds_on_a_falling_path():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter, gvol_cap_hard=True, gvol_cap_pct=30.0)
            await orch.spawn_controller(c)
            ex = c.my_executors()[0]
            cap = Decimal(300)
            level = Decimal(1000) / 5
            first = [e for e in adapter.log if _is_open_entry(e)]
            # Nearest-first: the top rung (closest to mid) is placed first.
            prices = [Decimal(e[5]) for e in first]
            assert prices == sorted(prices, reverse=True)
            px = Decimal("100")
            for _ in range(30):
                px -= Decimal("0.08")
                await _step(adapter, clock, c, str(px))
                used = ex.held_quote(px) + ex.resting_open_quote()
                assert used <= cap + level + Decimal("1e-6"), (px, used)
            assert c.grid_metrics()["gvol_cap_usd"] == pytest.approx(300.0)
    asyncio.run(body())


def test_grid_hard_cap_off_is_the_classic_ladder():
    """Without the hard cap the ladder rests every rung (the soft cap never
    withdraws) — the documented default this opt-in fixes."""
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter)
            await orch.spawn_controller(c)
            ex = c.my_executors()[0]
            assert ex.cap_quote is None
            assert len([lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED]) == 5
    asyncio.run(body())


def test_grid_skew_shifts_new_opens_deeper_with_inventory_and_keeps_close_legs():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter, gvol_skew_enabled=True)
            await orch.spawn_controller(c)
            await _step(adapter, clock, c, "99.8")          # fill some rungs
            ex = c.my_executors()[0]
            closes_before = {lv.index: lv.close_price for lv in ex.levels
                             if lv.state is GridLevelState.CLOSE_ORDER_PLACED}
            assert closes_before
            await _step(adapter, clock, c, "99.8")
            assert c._skew_bp < 0
            bounds = c._rebuild_bounds_for_side(Decimal("99.8"))
            c._skew_bp = 0.0
            neutral = c._rebuild_bounds_for_side(Decimal("99.8"))
            assert bounds and bounds["end_price"] < neutral["end_price"]
            for lv in ex.levels:
                if lv.index in closes_before and lv.state is GridLevelState.CLOSE_ORDER_PLACED:
                    assert lv.close_price == closes_before[lv.index]
    asyncio.run(body())


def test_grid_vol_spacing_applies_through_a_recenter_behind_a_deadband():
    async def body():
        with FakeClock() as clock:
            amp = {"v": 4.0}
            src = CandleSource(clock, lambda m: amp["v"])
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter, gvol_spacing_enabled=True,
                            gvol_spacing_floor_bp=6.8)
            await orch.spawn_controller(c)
            await _step(adapter, clock, c, "100")
            assert c._step_override_bp == pytest.approx(2.6 * 4.0, abs=1e-6)   # 10.4bp
            ex = c.my_executors()[0]
            assert float(ex.config.min_spread_between_orders) * 1e4 == pytest.approx(10.4, abs=1e-6)
            # A < max(1bp, 15%) change does not re-space (no churn).
            amp["v"] = 4.2
            cancels = len([e for e in adapter.log if e[0] == "cancel"])
            for _ in range(3):
                await _step(adapter, clock, c, "100", dt=60)
            assert c._step_override_bp == pytest.approx(10.4, abs=1e-6)
            # Below the fee floor -> the floor.
            amp["v"] = 1.0
            for _ in range(70):
                await _step(adapter, clock, c, "100", dt=60)
            assert c._step_override_bp == pytest.approx(6.8)
            assert len([e for e in adapter.log if e[0] == "cancel"]) > cancels
    asyncio.run(body())


def test_vol_gate_events_fire_once_per_combined_flip():
    async def body():
        with FakeClock() as clock:
            state = {"hot": False}
            src = CandleSource(clock, lambda m: HOT_AMP if state["hot"] else CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _grid(clock, src, adapter, gvol_gate_enabled=True)
            await orch.spawn_controller(c)
            c.consume_gate_event()
            state["hot"] = True
            events = []
            for _ in range(8):
                await _step(adapter, clock, c, "100", dt=60)
                ev = c.consume_gate_event()
                if ev:
                    events.append(ev)
            assert events == [{"state": "PAUSE", "reason": "vol_hot"}]
            # Toggle OFF mid-run: one resume event, then nothing.
            c.configs["gvol_gate_enabled"] = False
            c.reload_vol_cfg()
            events = []
            for _ in range(3):
                await _step(adapter, clock, c, "100", dt=60)
                ev = c.consume_gate_event()
                if ev:
                    events.append(ev)
            assert events == [{"state": "QUOTE", "reason": "", "prev_reason": "vol_hot"}]
            assert not c.gate_paused
    asyncio.run(body())


# --------------------------------------------------------------------------- fill-anchored
def _fa(clock, src, adapter, **extra):
    cfg = gs.fa_cfg({**GVOL_ALL_OFF, "gvol_spacing_floor_bp": 6.0, "candle_provider": src,
                     "gvol_baseline_provider": _baseline(), **extra})
    orch = ExecutorOrchestrator()
    c = FillAnchoredQuotingController(user_id=1, orchestrator=orch, adapter=adapter,
                                      inventory=InventoryRepository(), configs=cfg,
                                      controller_id="FA")
    return orch, c


def _live_sides(c) -> set:
    out = set()
    for (is_bid, _lvl), slot in c._slots.items():
        if slot.ex_id is None:
            continue
        ex = c.orchestrator.get(slot.ex_id)
        if ex is not None and not ex.is_terminated:
            out.add("BUY" if is_bid else "SELL")
    return out


def test_fa_flat_and_hot_quotes_nothing_long_and_hot_only_reduces():
    async def body():
        with FakeClock() as clock:
            state = {"hot": False}
            src = CandleSource(clock, lambda m: HOT_AMP if state["hot"] else CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _fa(clock, src, adapter, gvol_gate_enabled=True)
            await orch.spawn_controller(c)
            await _step(adapter, clock, c, "100")
            assert _live_sides(c) == {"BUY", "SELL"}
            state["hot"] = True
            await _step(adapter, clock, c, "100", dt=60)
            assert c.gate_reason == "vol_hot"
            assert _live_sides(c) == set()             # flat + hot: both withdrawn
            # Now hold a long, still hot: only the reducing (SELL) side quotes.
            state["hot"] = False
            for _ in range(80):
                await _step(adapter, clock, c, "100", dt=60)
                if not c.gate_paused:
                    break
            assert not c.gate_paused
            await _step(adapter, clock, c, "99.9")      # bids fill -> long
            assert c._base_value(Decimal("99.9")) > 0
            state["hot"] = True
            await _step(adapter, clock, c, "99.9", dt=60)
            await _step(adapter, clock, c, "99.9")
            assert _live_sides(c) == {"SELL"}
            _assert_no_taker_and_no_close_cancel(adapter)
    asyncio.run(body())


def test_fa_concession_never_fires_while_vol_paused():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _fa(clock, src, adapter, gvol_gate_enabled=True,
                          concession_escalation_ticks=1)
            await orch.spawn_controller(c)
            await _step(adapter, clock, c, "100")
            await _step(adapter, clock, c, "99.7")          # long
            src.amp_fn = lambda m: HOT_AMP
            for px in ("99.4", "99.0", "98.6", "98.2", "97.8", "97.4"):
                await _step(adapter, clock, c, px, dt=60)
            assert c.gate_paused
            assert not [e for e in adapter.log if e[0] == "place" and e[3] == "MARKET"]
            assert c.concession_enabled                     # user setting untouched
    asyncio.run(body())


def test_fa_hard_cap_admits_nearest_levels_first_and_floors_at_one_level():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"))
            orch, c = _fa(clock, src, adapter, gvol_cap_hard=True, gvol_cap_pct=15.0)
            await orch.spawn_controller(c)
            await _step(adapter, clock, c, "100")
            bids = [lvl for (is_bid, lvl), s in c._slots.items() if is_bid and s.ex_id]
            # cap $150, levels $100 each: held 0 -> L0 then (0+100 <= 150) L1, stop.
            assert sorted(bids) == [0, 1]
    asyncio.run(body())


def test_fa_regime_pause_keeps_presence_first():
    async def body():
        adapter = RecordingAdapter(mid=Decimal("100"))
        orch = ExecutorOrchestrator()
        c = FillAnchoredQuotingController(user_id=1, orchestrator=orch, adapter=adapter,
                                          inventory=InventoryRepository(),
                                          configs=gs.fa_cfg(GVOL_ALL_OFF), controller_id="FA")
        await orch.spawn_controller(c)
        c.gate_verdict, c.gate_reason = "PAUSE", "trending_down"
        c.configs["regime_gate_enabled"] = 0.0
        await c.on_tick()
        assert _live_sides(c) == {"BUY", "SELL"}      # flat + regime pause: still quotes
    asyncio.run(body())


# --------------------------------------------------------------------------- D-Grid
def _dgrid(clock, src, adapter, **extra):
    cfg = gs.dgrid_cfg({**GVOL_ALL_OFF, "candle_provider": src,
                        "gvol_baseline_provider": _baseline(), **extra})
    orch = ExecutorOrchestrator()
    c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                              inventory=InventoryRepository(), configs=cfg, controller_id="D")
    return orch, c


def test_dgrid_vol_model_never_flips_to_trend_and_withdraws_when_hot():
    async def body():
        with FakeClock() as clock:
            state = {"hot": False}
            src = CandleSource(clock, lambda m: HOT_AMP if state["hot"] else CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"), venue_held={PAIR: Decimal(0)})
            orch, c = _dgrid(clock, src, adapter, dgrid_regime_model="vol",
                             gvol_gate_enabled=True, dgrid_trend_follow=True)
            await orch.spawn_controller(c)
            await _step(adapter, clock, c, "100")
            assert c.trend_follow_enabled and not c.trend_follow_effective
            assert c.my_executors(), "grid phase armed while calm"
            await _step(adapter, clock, c, "99.85")
            flips = []
            orig = c._flip_to

            async def spy(*a, **k):
                flips.append(a)
                return await orig(*a, **k)
            c._flip_to = spy
            state["hot"] = True
            for px in ("99.5", "99.0", "98.5", "98.0", "97.5", "97.0"):
                await _step(adapter, clock, c, px, dt=60)
            assert flips == []
            assert c._trend is None and c.current_phase == variance_regime.GRID
            ex = c.my_executors()[0]
            assert not [lv for lv in ex.levels if lv.state is GridLevelState.OPEN_ORDER_PLACED]
            assert c.gate_reason == "vol_hot"
            assert c.dgrid_metrics()["dgrid_regime_model"] == "vol"
            _assert_no_taker_and_no_close_cancel(adapter)
    asyncio.run(body())


def test_dgrid_vol_model_ignores_a_vr_trend_and_the_reversal_flip():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"), venue_held={PAIR: Decimal(0)})
            orch, c = _dgrid(clock, src, adapter, dgrid_regime_model="vol",
                             gvol_gate_enabled=True, dgrid_trend_follow=True)
            await orch.spawn_controller(c)

            async def trend():
                return variance_regime.RGRID
            c._classify = trend
            c._run_armed = True
            c._run_extreme = Decimal("110")
            for _ in range(5):
                await _step(adapter, clock, c, "100")
            assert c._trend is None and c.current_phase == variance_regime.GRID
            assert await c._maybe_reversal_flip(Decimal("100")) is False
    asyncio.run(body())


def test_dgrid_live_switch_to_vol_keeps_managing_a_held_trend_delegate():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: CALM_AMP)
            adapter = RecordingAdapter(mid=Decimal("100"), venue_held={PAIR: Decimal(0)})
            orch, c = _dgrid(clock, src, adapter, dgrid_trend_follow=True)
            await orch.spawn_controller(c)

            class Delegate:
                _pos_base = Decimal("1")
                ticks = 0
                gate_verdict, gate_reason = "QUOTE", ""
                configs: dict = {}

                async def on_tick(self):
                    Delegate.ticks += 1

                async def flatten_now(self, *a, **k):  # pragma: no cover - must not run
                    raise AssertionError("taker flatten on a settings change")

            c._trend = Delegate()
            c.current_phase = variance_regime.RGRID
            c.regime_model = "vol"
            c._refresh_trend_config = lambda: None
            for _ in range(4):
                await c._tick_trend_phase(variance_regime.GRID, Decimal("100"))
            assert Delegate.ticks == 4 and c._trend is not None
    asyncio.run(body())


# --------------------------------------------------------------------------- R-Grid
def _rgrid(clock, src, adapter, **extra):
    cfg = gs.rgrid_cfg({**RGRID_ALL_OFF, "candle_provider": src,
                        "gvol_baseline_provider": _baseline(), **extra})
    return ReverseGridController(user_id=1, orchestrator=object(), adapter=adapter,
                                 inventory=None, configs=cfg)


def test_rgrid_vol_arm_blocks_even_the_first_arm_without_compression_then_arms_on_burst():
    async def body():
        with FakeClock() as clock:
            burst = {"on": False, "at": 0}
            # Never compressed (always 4.0 = 1.0x median, above 0.91x) -> WAITING.
            src = CandleSource(clock, lambda m: 4.0)
            adapter = RecordingAdapter(mid=Decimal("100"), lot=Decimal("0.0001"),
                                       venue_held={PAIR: Decimal(0)})
            c = _rgrid(clock, src, adapter, gvol_arm_enabled=True)
            await c.on_start()
            for _ in range(3):
                await _step(adapter, clock, c, "100")
            assert adapter.placed_triggers == []
            assert c.gate_reason == "rgrid_vol_wait" and c.gvol_arm.detail == "needs_compression"
            # Quiet spell then a burst -> ARMED -> the ladder is placed.
            now_m = int(clock.now // 60) * 60
            burst["at"] = now_m + 60 * 2
            src.amp_fn = lambda m: 2.0 if m < burst["at"] else 12.0
            for _ in range(30):
                await _step(adapter, clock, c, "100", dt=30)
                if adapter.placed_triggers:
                    break
            assert adapter.placed_triggers, "should arm on expansion after compression"
            assert c.gvol_arm.state == vm.ARMED
    asyncio.run(body())


def test_rgrid_vol_arm_never_gates_an_open_position():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: 2.0 if m < int(gs.T0) + 120 else 12.0)
            adapter = RecordingAdapter(mid=Decimal("100"), lot=Decimal("0.0001"),
                                       venue_held={PAIR: Decimal(0)})
            c = _rgrid(clock, src, adapter, gvol_arm_enabled=True)
            await c.on_start()
            for _ in range(20):
                await _step(adapter, clock, c, "100", dt=30)
                if adapter.placed_triggers:
                    break
            assert adapter.placed_triggers
            adapter.cross_triggers(Decimal("100.4"))            # a BUY rung fires
            await _step(adapter, clock, c, "100.4")
            assert c._pos_base > 0 and c._stop_digest is not None
            stop = c._stop_digest
            # The filter now says WAITING/UNKNOWN: the position and its stop remain.
            src.empty = True
            for _ in range(6):
                await _step(adapter, clock, c, "100.5", dt=60)
            assert c._pos_base > 0
            assert stop not in adapter.cancelled_triggers or c._stop_digest is not None
            assert c.gate_verdict == "QUOTE"
    asyncio.run(body())


def test_rgrid_vol_arm_unknown_does_not_arm():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: 2.0)
            src.empty = True
            adapter = RecordingAdapter(mid=Decimal("100"), venue_held={PAIR: Decimal(0)})
            c = _rgrid(clock, src, adapter, gvol_arm_enabled=True)
            await c.on_start()
            await _step(adapter, clock, c, "100")
            assert adapter.placed_triggers == []
            assert c.gate_reason == "vol_unknown"
            assert c.grid_metrics()["gvol_state"] == "UNKNOWN"
    asyncio.run(body())


def test_rgrid_vol_arm_and_chop_guard_compose_with_and():
    async def body():
        with FakeClock() as clock:
            src = CandleSource(clock, lambda m: 2.0 if m < int(gs.T0) + 120 else 12.0)
            adapter = RecordingAdapter(mid=Decimal("100"), venue_held={PAIR: Decimal(0)})
            c = _rgrid(clock, src, adapter, gvol_arm_enabled=True, revgrid_chop_stand_down=True)
            await c.on_start()
            c._has_opened = True                 # a re-arm after a close
            for _ in range(20):
                await _step(adapter, clock, c, "100", dt=30)
            # Vol says ARMED at some point, but no confirmed trend -> chop guard holds.
            assert adapter.placed_triggers == []
    asyncio.run(body())
