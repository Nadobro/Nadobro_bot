import asyncio
import math
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.executors.grid_executor import GridExecutor
from src.nadobro.engine.executors.reverse_grid_executor import ReverseGridExecutor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator

CFG = {"trading_pair": "P", "start_price": "98", "end_price": "102", "limit_price": "0",
       "total_amount_quote": "100", "min_spread_between_orders": "0.002", "max_open_orders": 4,
       # step/levels make _rebuild_bounds_for_side produce side-correct bounds
       # around the live mid (as the real engine config does), so a flipped
       # SELL grid places sells ABOVE mid rather than reusing long-grid bounds.
       "step_pct": "0.002", "levels_count": 3}


def _candles(closes):
    return [{"high": c + 1, "low": c - 1, "close": c} for c in closes]


def _down(n=60):
    return _candles([float(i) for i in range(n, 1, -1)])


def _range(n=60, base=100.0, amp=1.0, period=7.0):
    return _candles([base + amp * math.sin(2 * math.pi * i / period) for i in range(n)])


def test_trending_down_selects_reverse_grid():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        cfg = dict(CFG, candle_provider=lambda p: _down())
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c.current_phase == "rgrid"
        assert c.variance_ratio >= 1.25
        active = orch.list(c.id, active_only=True)
        assert active and isinstance(active[0], ReverseGridExecutor)

    asyncio.run(body())


def test_ranging_selects_long_grid():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        cfg = dict(CFG, candle_provider=lambda p: _range())
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c.current_phase == "grid"
        active = orch.list(c.id, active_only=True)
        assert active and isinstance(active[0], GridExecutor)

    asyncio.run(body())


def test_same_regime_does_not_flip():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        cfg = dict(CFG, candle_provider=lambda p: _down())
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        first = orch.list(c.id, active_only=True)[0]
        await orch.tick_controller(c.id)  # still a downtrend -> no flip
        active = orch.list(c.id, active_only=True)
        assert len(active) == 1 and active[0] is first

    asyncio.run(body())


def test_midflight_flip_grid_to_rgrid_on_confirmed_regime_change():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        box = {"data": _range()}
        cfg = dict(CFG, candle_provider=lambda p: box["data"], dgrid_flip_confirm_ticks=2)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        active = orch.list(c.id, active_only=True)
        assert isinstance(active[0], GridExecutor) and c.current_phase == "grid"

        # Regime turns down. Flip is debounced by confirm_ticks=2.
        box["data"] = _down()
        await orch.tick_controller(c.id)  # streak 1 -> hold
        active = orch.list(c.id, active_only=True)
        assert isinstance(active[0], GridExecutor), "must not flip on a single tick"
        await orch.tick_controller(c.id)  # streak 2 -> flip
        active = orch.list(c.id, active_only=True)
        assert len(active) == 1 and isinstance(active[0], ReverseGridExecutor)
        assert c.current_phase == "rgrid"
        # Flip event surfaced exactly once.
        event = c.consume_dgrid_event()
        assert event and event["from"] == "grid" and event["to"] == "rgrid"
        assert c.consume_dgrid_event() is None

    asyncio.run(body())


def test_metrics_exposed_for_dashboard():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        # 25bp requested. The re-center only re-quotes unfilled maker opens (no
        # flatten), so we follow price closely: an explicit value is honored with
        # only a small half-band floor (step 20bp x (3-1) = 40bp band -> 20bp
        # half-band, < 25), so 25bp passes through unchanged.
        cfg = dict(CFG, candle_provider=lambda p: _down(), dgrid_reset_threshold_bp=25.0)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        m = c.dgrid_metrics()
        assert m["dgrid_phase"] == "rgrid"
        assert m["dgrid_variance_ratio"] >= 1.25
        assert m["dgrid_reset_threshold_bp"] == 25.0  # honored, not inflated to 50

    asyncio.run(body())


def test_recenter_on_by_default_reuses_executor_no_respawn():
    # A *dynamic* grid must FOLLOW price by default — the "placed a few orders
    # and stopped, never recalibrated" report. Re-center is ON by default
    # (auto = ~one band width), and crucially it re-quotes the SAME executor's
    # ladder in place (GridExecutor.recenter) — never a flatten + respawn, so a
    # moving mid does not churn the position.
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        cfg = dict(CFG, candle_provider=lambda p: _range())  # ranging -> grid, no flip
        assert "dgrid_reset_threshold_bp" not in cfg  # user did not pin a value
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        # Auto-default = one band width (step 20bp x (3-1) = 40bp), not OFF.
        assert c.reset_threshold_bp == 40.0
        await orch.tick_controller(c.id)
        first = orch.list(c.id, active_only=True)[0]
        # Walk the mid well beyond the band, several ticks.
        for px in (101, 99, 103, 97, 104):
            adapter.set_mid(Decimal(px))
            await orch.tick_controller(c.id)
        active = orch.list(c.id, active_only=True)
        assert len(active) == 1 and active[0] is first, \
            "re-center must reuse the same executor (no flatten/respawn churn)"

    asyncio.run(body())


def test_reset_recenters_in_place_on_large_move():
    # When enabled, reset re-quotes the SAME executor's ladder in place (no
    # flatten, no new executor) only after a move past the floored threshold.
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        cfg = dict(CFG, candle_provider=lambda p: _range(), dgrid_reset_threshold_bp=200.0)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        first = orch.list(c.id, active_only=True)[0]
        opens0 = [lv.open_price for lv in first.levels]
        # RGRID-STALE-LADDER: 200bp is capped to the 40bp ladder band
        # (step 20bp x 2). A threshold wider than the grid it steers can never
        # fire before price has left the band entirely.
        assert c.reset_threshold_bp == 40.0
        # Small move (20bp) — half a band, still in range: no re-center.
        adapter.set_mid(Decimal("100.2"))
        await orch.tick_controller(c.id)
        active = orch.list(c.id, active_only=True)
        assert active[0] is first and [lv.open_price for lv in first.levels] == opens0, \
            "an in-band move must not re-center"
        # Large move (3%) — past the band threshold: re-center in place.
        adapter.set_mid(Decimal("103"))
        await orch.tick_controller(c.id)
        active = orch.list(c.id, active_only=True)
        assert len(active) == 1 and active[0] is first, "re-center must reuse the same executor"
        assert max(lv.open_price for lv in first.levels) > Decimal("102"), "ladder must move up"
        assert c.current_phase == "grid"  # same regime, not a flip
        assert c.consume_dgrid_event() is None  # re-center is not a flip notification

    asyncio.run(body())


def test_flip_deferred_while_gate_paused():
    # Finding 2 regression: a confirmed flip during a breakout/expansion PAUSE
    # must close the old position but NOT arm a fresh grid into the chaos.
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        box = {"data": _range()}  # gate disabled in cfg; we drive the verdict
        cfg = dict(CFG, candle_provider=lambda p: box["data"], dgrid_flip_confirm_ticks=1)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert isinstance(orch.list(c.id, active_only=True)[0], GridExecutor)

        # Regime turns down AND the gate is paused (breakout). Gate is disabled
        # in cfg, so set the verdict directly; on_tick won't overwrite it.
        box["data"] = _down()
        c.gate_verdict, c.gate_reason = "PAUSE", "breakout"
        await orch.tick_controller(c.id)
        assert orch.list(c.id, active_only=True) == [], "must close + sit out, not arm into breakout"
        assert c.consume_dgrid_event() is None, "no flip notification when nothing armed"

        # Range/acceptance returns: now it arms the short side.
        c.gate_verdict, c.gate_reason = "QUOTE", ""
        await orch.tick_controller(c.id)
        active = orch.list(c.id, active_only=True)
        assert len(active) == 1 and isinstance(active[0], ReverseGridExecutor)

    asyncio.run(body())


def test_tick_records_diagnostics_for_services_log():
    # The per-tick diagnostics (candle count + mid) must be captured so the
    # services-stream engine_diag line can pinpoint a no-orders run.
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        cfg = dict(CFG, candle_provider=lambda p: _down())
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._last_candle_count == len(_down())
        assert c._last_mid == Decimal(100)
        assert c.current_phase in ("grid", "rgrid")

    asyncio.run(body())


def test_diag_telemetry_attrs_are_dynamic_grid_only():
    """NO_ORDERS-DIAG: engine_diag reports candle_count/mid/phase/vr ONLY when
    the controller exposes them. DynamicGrid does; MarketMaking (Mid) — and by
    inheritance FillAnchored (Momentum/taker) — do NOT. Reporting the getattr
    defaults for those falsely reads as a starved grid (the misdiagnosis that
    sent us chasing a phantom candle-feed bug). This pins the attribute
    asymmetry the controller-aware diag relies on."""
    from src.nadobro.engine.controllers.market_making import MarketMakingController

    dg = DynamicGridController(
        user_id=1, orchestrator=ExecutorOrchestrator(),
        adapter=MockNadoAdapter(mid=Decimal(100)), inventory=InventoryRepository(),
        configs=dict(CFG, candle_provider=lambda p: _range()),
    )
    for attr in ("_last_candle_count", "_last_mid", "current_phase", "variance_ratio"):
        assert hasattr(dg, attr), f"DynamicGrid must expose {attr} for the diag"

    mm = MarketMakingController(
        user_id=1, orchestrator=ExecutorOrchestrator(),
        adapter=MockNadoAdapter(mid=Decimal(100)), inventory=InventoryRepository(),
        configs=dict(CFG),
    )
    for attr in ("_last_candle_count", "_last_mid", "current_phase", "variance_ratio"):
        assert not hasattr(mm, attr), (
            f"MarketMaking unexpectedly exposes {attr} — the diag would mislabel it"
        )


def test_profit_booking_scales_out_on_tier_cross():
    """Tiered profit-booking: as the run's uPnL crosses rising tiers (% of
    margin), a fraction of the position is closed reduce-only; each tier books
    once."""
    from src.nadobro.engine.types import OrderType, TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(102))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=cfg)
        # Seed a 1.0-base long @ 100 (margin 100): uPnL = net*(mark-entry).
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        red = _attach_reducer(c, inv)

        # mid 102 -> uPnL +2 = +2% of margin -> only tier 1 (2%) books, and the
        # closed base is recorded back to inventory.
        await c._maybe_book_profit(Decimal("102"))
        assert len(red.requests) == 1
        assert abs(float(red.requests[0]) - 0.33) < 1e-9        # 33% of 1.0
        assert inv.get(1, "P", c.id).net_amount_base == Decimal("0.670")
        assert c._booked_tiers == {0}
        # Nothing crossed: a profit tier rests (the executor is what rests it —
        # see tests/engine/executors/test_grid_executor.py).
        assert not [o for o in adapter.placed if o.order_type is OrderType.MARKET]

        # Same tier again -> nothing new (booked once).
        await c._maybe_book_profit(Decimal("102"))
        assert len(red.requests) == 1

        # Jump high enough that the *remaining* position crosses tiers 4% and
        # 6%; the second close must size from 0.67 base, not stale 1.0 base.
        await c._maybe_book_profit(Decimal("109"))
        assert len(red.requests) == 2
        assert red.requests[1] == Decimal("0.442")
        assert inv.get(1, "P", c.id).net_amount_base == Decimal("0.228")
        assert c._booked_tiers == {0, 1, 2}

    asyncio.run(body())


def test_profit_booking_does_not_book_tier_on_zero_fill():
    """A tier is credited only against a real fill. Nothing closed here (there is
    no executor to rest a close, and a profit tier is never crossed for), so the
    tier must stay available for the next attempt."""
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(102), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))

        await c._maybe_book_profit(Decimal("102"))

        assert c._booked_tiers == set()
        assert inv.get(1, "P", c.id).net_amount_base == Decimal("1.0")

    asyncio.run(body())


def test_profit_booking_does_not_fallback_around_live_executor_zero_fill():
    from src.nadobro.engine.types import TradeType

    class ZeroReduceExecutor:
        def __init__(self, controller_id):
            self.id = "zero-reduce"
            self.controller_id = controller_id
            self.is_terminated = False
            self.requests = []

        async def reduce_position(self, amount):
            self.requests.append(amount)
            return Decimal(0)

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(102))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=cfg)
        fake = ZeroReduceExecutor(c.id)
        orch._executors[fake.id] = fake
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))

        await c._maybe_book_profit(Decimal("102"))

        assert fake.requests
        assert [o for o in adapter.placed if o.side == TradeType.SELL] == []
        assert c._booked_tiers == set()
        assert inv.get(1, "P", c.id).net_amount_base == Decimal("1.0")

    asyncio.run(body())


def test_spawn_deferred_when_prior_inventory_not_flat():
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _down())
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("0.5"), Decimal("50"))

        spawned = await c._spawn_phase("rgrid", Decimal("100"))

        assert spawned is False
        assert orch.list(c.id, active_only=True) == []

    asyncio.run(body())


def test_profit_booking_skips_when_below_tier():
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(101))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        # mid 101 -> uPnL +1 = +1% < first tier (2%): no booking.
        await c._maybe_book_profit(Decimal("101"))
        assert [o for o in adapter.placed if o.side == TradeType.SELL] == []
        assert c._booked_tiers == set()

    asyncio.run(body())


# --- DGRID-TP-TIER-ANCHOR: the tiered scale-out is anchored to the user's TP,
# measured against ALLOCATED MARGIN (not the deployed = margin x leverage
# basis the old code used), so the ladder completes AT the user's setting and
# never books far below it. ------------------------------------------------


class _Reducer:
    """Stand-in for the live grid executor's ``reduce_position``.

    The real one rests a post-only close and reports fills over several ticks
    (PROFIT-TIER MAKER, 2026-08-08). These tests are about the TIER LADDER
    arithmetic — which tier fires at which uPnL, and how the slice is sized — so
    this one fills immediately and records what it was asked for. The maker
    mechanics themselves are pinned in tests/engine/executors/test_grid_executor.py.
    """

    def __init__(self, inv, controller, *, fills: bool = True):
        self.inv, self.c, self.fills = inv, controller, fills
        self.requests: list = []

    async def reduce_position(self, amount, mid=None):
        amount = Decimal(str(amount))
        self.requests.append(amount)
        if not self.fills or amount <= 0:
            return Decimal(0)
        from src.nadobro.engine.types import TradeType
        net = self.inv.get(self.c.user_id, "P", self.c.id).net_amount_base
        side = TradeType.SELL if net > 0 else TradeType.BUY
        px = Decimal(str(mid or 0))
        self.inv.apply_fill(self.c.user_id, "P", self.c.id, side, amount, amount * px)
        return amount


def _attach_reducer(c, inv, **kw):
    """Give the controller something that can work a close, the way a live grid
    executor would. Without one, _maybe_book_profit deliberately does nothing —
    it will not cross for a profit tier (see
    test_a_profit_tier_will_not_cross_when_there_is_no_executor)."""
    red = _Reducer(inv, c, **kw)
    c.my_executors = lambda active_only=True: [red]   # type: ignore[assignment]
    return red


def _anchored_cfg(tp_pct, **over):
    # margin (notional) = 100, deployed = 500 (5x). tp_margin_basis is the
    # user's margin — the SAME basis the session TP rail measures against.
    return dict(
        CFG, candle_provider=lambda p: _range(),
        margin_quote="500", tp_margin_basis=Decimal("100"), leverage=5,
        tp_pct=tp_pct, dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33,
        **over,
    )


def test_tiered_tp_anchors_to_user_tp_not_deployed_basis():
    """A user TP of 50% anchors the ladder to 50% of MARGIN. The old code booked
    the first tier at 2% of the DEPLOYED $500 (= $10 uPnL, i.e. 10% of margin);
    the anchored first tier is 16.67% of the $100 margin, so at +$10 uPnL
    NOTHING books — TP no longer fires early."""
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(110))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=_anchored_cfg(50.0))
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        red = _attach_reducer(c, inv)

        # +$10 uPnL = 10% of margin: the OLD deployed-basis code booked here
        # (10/500 = 2% >= tier1); the anchored ladder does NOT (10% < 16.67%).
        await c._maybe_book_profit(Decimal("110"))
        assert red.requests == []
        assert c._booked_tiers == set()

        # +$16.8 uPnL = 16.8% of margin >= first anchored tier (16.67%).
        await c._maybe_book_profit(Decimal("116.8"))
        assert len(red.requests) == 1
        assert c._booked_tiers == {0}

    asyncio.run(body())


def test_tiered_tp_top_tier_lands_exactly_at_user_tp():
    """The final tier books at the user's TP (50% of margin), so the scale-out
    completes AT the user's setting."""
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(150))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=_anchored_cfg(50.0))
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        _attach_reducer(c, inv)
        # +$50 uPnL = 50% of margin = the user's TP -> all three anchored tiers
        # (16.67 / 33.33 / 50) cross at once.
        await c._maybe_book_profit(Decimal("150"))
        assert c._booked_tiers == {0, 1, 2}

    asyncio.run(body())


def test_tiered_tp_scales_up_with_a_higher_user_tp():
    """TP=200% pushes the whole ladder up: at +50% of margin (the OLD top tier)
    NOTHING books; the first anchored tier is now 66.67%."""
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(150))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=_anchored_cfg(200.0))
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        _attach_reducer(c, inv)
        await c._maybe_book_profit(Decimal("150"))    # +50% < 66.67% first tier
        assert c._booked_tiers == set()
        await c._maybe_book_profit(Decimal("166.8"))  # +66.8% >= first tier
        assert c._booked_tiers == {0}

    asyncio.run(body())


def test_tiered_tp_falls_back_to_legacy_when_no_tp_set():
    """With no user TP, behavior is unchanged: fixed [2,4,6] tiers on the
    deployed (margin_quote) basis."""
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(110))
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        # No tp_pct, no tp_margin_basis: margin_quote=100 is the legacy basis.
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        _attach_reducer(c, inv)
        # +$2 uPnL = 2% of the $100 legacy basis -> first fixed tier books.
        await c._maybe_book_profit(Decimal("102"))
        assert c._booked_tiers == {0}

    asyncio.run(body())


# ==========================================================================
# Flip NOTIFICATION wording (2026-08-06)
# ==========================================================================
# Reported: "🔄 Reverse GRID switched RGRID → GRID … downtrend detected
# (variance ratio 0.83) … now quoting the LONG ladder." The decision was right
# (the decline had stalled below the release threshold, so the neutral long grid
# is correct) — the message was wrong: it read sign(drift) as a trend verdict.
def test_flip_event_records_whether_a_trend_was_actually_declared():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        box = {"data": _range()}
        cfg = dict(CFG, candle_provider=lambda p: box["data"], dgrid_flip_confirm_ticks=1)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        box["data"] = _down()
        await orch.tick_controller(c.id)
        event = c.consume_dgrid_event()
        assert event and event["to"] == "rgrid"
        assert event["trending"] == "1", "a real downtrend must be flagged as a trend"

    asyncio.run(body())


def test_flip_reason_never_calls_a_stalled_drift_a_downtrend():
    from src.nadobro.strategy.bot_runtime import dgrid_flip_reason

    # The exact reported event: back to the long grid while drift is barely
    # negative and the classifier says RANGING.
    assert dgrid_flip_reason({
        "from": "rgrid", "to": "grid", "direction": "down",
        "trending": "", "variance_ratio": "0.83", "reason": "flip",
    }) == "trend stalled — back to range mode"
    # A declared downtrend still reads as one.
    assert dgrid_flip_reason({
        "from": "grid", "to": "rgrid", "direction": "down",
        "trending": "1", "variance_ratio": "1.40", "reason": "flip",
    }) == "downtrend detected"
    assert dgrid_flip_reason({
        "from": "rgrid", "to": "grid", "direction": "up",
        "trending": "1", "variance_ratio": "1.40", "reason": "flip",
    }) == "uptrend detected"
    # The trailing-reversal flip keeps its own wording.
    assert dgrid_flip_reason({
        "from": "grid", "to": "rgrid", "direction": "flat",
        "trending": "", "reason": "reversal",
    }) == "reversal — locked profit, flipping"


def test_reverse_grid_can_no_longer_emit_a_phase_flip_at_all():
    """R-Grid is not a phase switcher — it runs its own controller and its own
    taker executor, so these messages cannot reach an R-Grid user any more."""
    from src.nadobro.engine.controllers.rgrid import RGridController
    from src.nadobro.strategy.engine_runtime import CONTROLLER_REGISTRY

    assert CONTROLLER_REGISTRY["rgrid"] is RGridController
    assert CONTROLLER_REGISTRY["dgrid"] is DynamicGridController
    assert not hasattr(RGridController, "consume_dgrid_event")


# ==========================================================================
# Financial overlay ↔ phase switching (2026-08-06)
# ==========================================================================
# D-Grid IS the switcher, so it must switch well: the variance classifier owns
# the decision and the overlay is a second opinion that can only make a flip more
# conservative. A wrong flip costs a reduce-only round trip AND leaves the book on
# the wrong side of the move.
def _dg(**cfg):
    adapter = MockNadoAdapter(mid=Decimal(100))
    orch = ExecutorOrchestrator()
    base = dict(CFG, candle_provider=lambda p: _range(), dgrid_flip_confirm_ticks=2)
    base.update(cfg)
    return DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                 inventory=InventoryRepository(), configs=base)


def test_a_confirming_overlay_leaves_the_debounce_alone():
    c = _dg(signal_regime="trend_down", signal_confidence=0.9)
    assert c._required_confirm_ticks("rgrid") == 2


def test_a_contradicting_overlay_buys_one_more_confirming_tick():
    """Overlay reads an UPtrend while the classifier wants the short phase: wait
    one extra tick rather than flipping on a contested read."""
    c = _dg(signal_regime="trend_up", signal_confidence=0.9)
    assert c._required_confirm_ticks("rgrid") == 3


def test_an_unconfident_overlay_is_ignored():
    c = _dg(signal_regime="trend_up", signal_confidence=0.10)
    assert c._required_confirm_ticks("rgrid") == 2
    assert c._signal_phase() is None


def test_a_rangebound_overlay_has_no_directional_opinion():
    for regime in ("range", "chop", ""):
        c = _dg(signal_regime=regime, signal_confidence=0.9)
        assert c._signal_phase() is None
        assert c._required_confirm_ticks("rgrid") == 2


def test_the_overlay_can_never_shorten_the_debounce_or_flip_on_its_own():
    """It is advisory: at worst D-Grid waits one tick longer. It must never make
    the switcher quicker to flip, and never cause a flip by itself."""
    for regime in ("trend_up", "trend_down", "range", "chop"):
        for conf in (0.0, 0.5, 1.0):
            c = _dg(signal_regime=regime, signal_confidence=conf,
                    dgrid_flip_confirm_ticks=2)
            assert c._required_confirm_ticks("grid") >= 2
            assert c._required_confirm_ticks("rgrid") >= 2


def test_the_switcher_still_flips_on_a_confirmed_regime_change():
    """The overlay must not break the core behaviour: a real downtrend still arms
    the short ladder once the debounce is satisfied."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        box = {"data": _range()}
        cfg = dict(CFG, candle_provider=lambda p: box["data"],
                   dgrid_flip_confirm_ticks=1,
                   signal_regime="trend_down", signal_confidence=0.9)
        c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                  inventory=InventoryRepository(), configs=cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c.current_phase == "grid"
        box["data"] = _down()
        await orch.tick_controller(c.id)
        assert c.current_phase == "rgrid"
        event = c.consume_dgrid_event()
        assert event and event["from"] == "grid" and event["to"] == "rgrid"

    asyncio.run(body())


def test_the_overlay_read_reaches_the_controller_through_the_mapped_config():
    """End-to-end wiring: overlay_actuator writes the signal read into configs,
    map_strategy_config carries the confidence knob, the controller consumes both."""
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    cfg = map_strategy_config("dgrid", {"notional_usd": 100.0}, Decimal(100), product="BTC-PERP")
    assert "dgrid_signal_min_confidence" in cfg
    c = _dg(signal_regime="trend_down", signal_confidence=0.8,
            dgrid_signal_min_confidence=cfg["dgrid_signal_min_confidence"])
    assert c._signal_phase() == "rgrid"


# ==========================================================================
# Live-editable regime knobs (audit 2026-08-06)
# ==========================================================================
def test_regime_knobs_are_refreshed_on_a_live_settings_edit():
    """Every one of these is a Regime-card button. They were read once at
    __init__, so editing them mid-session changed the card and nothing else until
    the user stopped and restarted the strategy — a silent no-op."""
    from src.nadobro.strategy.engine_runtime import _apply_dgrid_controller_config

    c = _dg(dgrid_trend_on_vr=1.25, dgrid_range_on_vr=1.15,
            dgrid_trend_drift_pct=0.30, dgrid_flip_confirm_ticks=2,
            dgrid_short_window=4, dgrid_long_window=12,
            dgrid_reversal_flip_pct=0.4, dgrid_tp_fraction=0.33)
    _apply_dgrid_controller_config(c, {
        "dgrid_trend_on_vr": 1.60, "dgrid_range_on_vr": 1.05,
        "dgrid_trend_drift_pct": 0.15, "dgrid_flip_confirm_ticks": 4,
        "dgrid_short_window": 3, "dgrid_long_window": 20,
        "dgrid_reversal_flip_pct": 0.9, "dgrid_tp_fraction": 0.5,
        "signal_regime": "trend_down", "signal_confidence": 0.8,
    })
    assert c.trend_on_vr == 1.60 and c.range_on_vr == 1.05
    assert c.trend_drift_pct == 0.15 and c.flip_confirm_ticks == 4
    assert c.short_window == 3 and c.long_window == 20
    assert c.reversal_flip_pct == 0.9 and c.tp_fraction == 0.5
    assert c.signal_regime == "trend_down" and c.signal_confidence == 0.8


def test_a_live_edit_does_not_reset_the_pending_flip_streak():
    """Otherwise a user poking at buttons could defer a flip indefinitely."""
    from src.nadobro.strategy.engine_runtime import _apply_dgrid_controller_config

    c = _dg()
    c._phase_confirm_streak = 1
    c.current_phase = "rgrid"
    _apply_dgrid_controller_config(c, {"dgrid_trend_on_vr": 1.9})
    assert c._phase_confirm_streak == 1
    assert c.current_phase == "rgrid"


def test_missing_or_malformed_knobs_keep_the_current_value():
    from src.nadobro.strategy.engine_runtime import _apply_dgrid_controller_config

    c = _dg(dgrid_trend_on_vr=1.25)
    _apply_dgrid_controller_config(c, {})                        # nothing to apply
    assert c.trend_on_vr == 1.25
    _apply_dgrid_controller_config(c, {"dgrid_trend_on_vr": "not a number"})
    assert c.trend_on_vr == 1.25


# ==========================================================================
# PROFIT-TIER MAKER (2026-08-08) — booking is asynchronous now
# ==========================================================================
# The scale-out rests post-only instead of crossing on every tier, so a slice can
# fill over several ticks (or not at all). A tier must therefore be credited only
# when its slice has really closed — crediting on the first partial would consume
# the tier and leave the profit unbooked, which is worse than the taker fee we set
# out to save.
def test_a_partial_fill_does_not_credit_the_tier_until_the_slice_closes():
    from src.nadobro.engine.types import TradeType

    class _Dribble:
        """Fills a fixed small amount per call, like a resting order being picked
        off in pieces."""

        def __init__(self, inv, c, chunk):
            self.inv, self.c, self.chunk = inv, c, Decimal(str(chunk))

        async def reduce_position(self, amount, mid=None):
            amount = Decimal(str(amount))
            take = min(self.chunk, amount)
            if take <= 0:
                return Decimal(0)
            self.inv.apply_fill(1, "P", self.c.id, TradeType.SELL, take,
                                take * Decimal(str(mid or 0)))
            return take

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(102))
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                  adapter=adapter, inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        # 0.33 slice, filled 0.1 at a time -> 4 ticks to complete.
        dribble = _Dribble(inv, c, "0.1")
        c.my_executors = lambda active_only=True: [dribble]

        await c._maybe_book_profit(Decimal("102"))
        assert c._booked_tiers == set(), "credited the tier on a PARTIAL fill"
        assert c._book_slice is not None
        assert c._book_slice.done == Decimal("0.1")
        assert c._book_slice.target == Decimal("0.33")

        await c._maybe_book_profit(Decimal("102"))
        assert c._booked_tiers == set()
        assert c._book_slice.done == Decimal("0.2")

        # Completing the slice credits the tier exactly once, and the slice clears.
        for _ in range(3):
            await c._maybe_book_profit(Decimal("102"))
        assert c._booked_tiers == {0}
        assert c._book_slice is None
        # The target was never re-sized off the shrinking position.
        assert abs(float(inv.get(1, "P", c.id).net_amount_base) - 0.67) < 1e-9

    asyncio.run(body())


def test_a_profit_tier_will_not_cross_when_there_is_no_executor():
    """The post-flip orphan state: inventory held, no executor to rest a close.
    This used to fire a naked reduce-only MARKET — the one order we have decided
    never to cross for. It must place nothing and leave the tier uncredited, so
    booking resumes when an executor exists again."""
    from src.nadobro.engine.types import OrderType, TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(109))
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                  adapter=adapter, inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))

        await c._maybe_book_profit(Decimal("109"))

        assert not [o for o in adapter.placed if o.order_type is OrderType.MARKET]
        assert not [o for o in adapter.placed if o.side is TradeType.SELL]
        assert c._booked_tiers == set()
        assert inv.get(1, "P", c.id).net_amount_base == Decimal("1.0")

    asyncio.run(body())


def test_a_tier_crossing_while_a_slice_rests_extends_it_rather_than_queueing():
    """A runaway move should scale out FASTER, not wait behind the slice already
    resting — and only one close rests at a time, so the slice must grow."""
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(102))
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                  adapter=adapter, inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        red = _attach_reducer(c, inv, fills=False)      # rests, never fills

        await c._maybe_book_profit(Decimal("102"))      # tier 0
        assert c._book_slice is not None
        first_target = c._book_slice.target
        assert c._book_slice.tiers == {0}

        await c._maybe_book_profit(Decimal("109"))      # tiers 1 and 2 cross too
        assert c._book_slice.tiers == {0, 1, 2}
        assert c._book_slice.target > first_target, "the slice did not grow"
        assert c._booked_tiers == set(), "nothing filled — nothing may be credited"
        assert red.requests, "the close must keep being worked"

    asyncio.run(body())


def test_going_flat_clears_a_resting_slice_instead_of_working_a_ghost():
    """If the position is gone (a flip flattened it, or the close filled in full),
    the slice must not keep re-quoting a close for inventory that no longer
    exists."""
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(102))
        inv = InventoryRepository()
        cfg = dict(CFG, candle_provider=lambda p: _range(), margin_quote="100",
                   dgrid_tp_tiers_pct=[2.0, 4.0, 6.0], dgrid_tp_fraction=0.33)
        c = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                  adapter=adapter, inventory=inv, configs=cfg)
        inv.apply_fill(1, "P", c.id, TradeType.BUY, Decimal("1.0"), Decimal("100"))
        red = _attach_reducer(c, inv, fills=False)

        await c._maybe_book_profit(Decimal("102"))
        assert c._book_slice is not None
        asked = len(red.requests)

        # Something else flattened it (a flip's reduce-only stop-out).
        inv.apply_fill(1, "P", c.id, TradeType.SELL, Decimal("1.0"), Decimal("102"))
        await c._maybe_book_profit(Decimal("102"))

        assert c._book_slice is None
        assert c._booked_tiers == {0}, "a flattened slice's tiers are settled"
        assert len(red.requests) == asked, "kept working a close with nothing held"

    asyncio.run(body())
