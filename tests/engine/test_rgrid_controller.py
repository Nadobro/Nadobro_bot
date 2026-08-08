"""Reverse Grid (R-Grid) — its own controller and its own MAKER executor.

R-Grid is a market-making strategy and every MM strategy here rests post-only
limit orders (the standing maker-first rule). Its geometry is the mirror of Grid,
and that is what makes it momentum rather than mean reversion:

    anchor    = average of the buy and sell exposure prices
    buy  leg rests at anchor x (1 + spread)     ABOVE the anchor
    sell leg rests at anchor x (1 - spread)     BELOW the anchor

Per the spec: "buy orders are placed at a price equal to or above the average ...
they only fill when the market price rises above this buy limit price". A bid
parked above the anchor becomes fillable exactly once price has risen past it —
because only then is it a resting bid BELOW market that a seller can hit. So the
fill is a momentum fill and the order is a maker order; nothing ever crosses.

Consequence: the two postability conditions are mutually exclusive, so at most one
leg rests at a time, and inside the band R-Grid waits.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.rgrid import RGridController
from src.nadobro.engine.executors.order_executor import OrderExecutorConfig
from src.nadobro.engine.executors.rgrid_maker_executor import (
    LEG_ENTRY,
    LEG_EXIT,
    LEG_TRAIL_STOP,
    RGridMakerExecutor,
    build_maker_quote,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import ExecutionStrategy, OrderType, PositionAction, TradeType

PAIR = "BTC-PERP"
SPREAD = Decimal("0.001")


def _controller(adapter, extra=None):
    configs = {
        "trading_pair": PAIR,
        "spread_bid_pct": SPREAD,
        "spread_ask_pct": SPREAD,
        "order_amount_quote": Decimal(10),
        "price_distance_tolerance": Decimal("0.0001"),
    }
    configs.update(extra or {})
    orch = ExecutorOrchestrator()
    c = RGridController(
        user_id=1, orchestrator=orch, adapter=adapter,
        inventory=InventoryRepository(), configs=configs, controller_id="RG",
    )
    return orch, c


def _resting(adapter, side=None):
    """Orders that actually REST — post-only only, optionally filtered by side.

    It used to return every placed order regardless of type, so a test asserting
    "the reducing leg rests" was satisfied by the crossing MARKET exit and passed
    while asserting the opposite of shipped behaviour."""
    out = [o for o in adapter.placed
           if o.order_type is OrderType.LIMIT_MAKER
           and (side is None or o.side is side)]
    return out


def _crossings(adapter, side=None):
    """Orders that CROSS — every risk exit is one of these."""
    return [o for o in adapter.placed
            if o.order_type is OrderType.LIMIT
            and (side is None or o.side is side)]


def _seed_leg(c, leg, px, base=Decimal(1)):
    """Give a leg exposure as if it had been FILLED live in this process.

    Distinct from ``seed_fills`` (the rebuild path), which restores the whole
    SESSION's history and does NOT count as live evidence — see
    test_a_rebuilt_controller_will_not_cross_off_a_seeded_anchor.
    """
    c._leg_fills[leg].append((Decimal(str(px)), base))
    c._live_fill_legs.add(leg)


# ==========================================================================
# 1. Maker-only, structurally
# ==========================================================================
@pytest.mark.parametrize("strategy", [
    ExecutionStrategy.LIMIT, ExecutionStrategy.MARKET,
])
def test_the_executor_refuses_anything_that_is_not_post_only(strategy):
    """Every MM strategy rests post-only limit orders. Enforced in the constructor
    so an edit cannot quietly turn a maker strategy into one paying 4.3bp a side."""
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    cfg = OrderExecutorConfig(
        PAIR, TradeType.BUY, Decimal(1), strategy,
        price=(None if strategy is ExecutionStrategy.MARKET else Decimal(100)),
    )
    with pytest.raises(ValueError, match="maker-only"):
        RGridMakerExecutor(cfg, user_id=1, controller_id="RG", adapter=adapter)


def test_only_the_trailing_stop_may_cross():
    """Everything R-Grid rests is post-only. The armed trailing stop is the single
    exemption — it has to act where a post-only order cannot sit."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter, extra={
            "reset_threshold_pct": Decimal("0.01"), "trail_enabled": True,
        })
        await orch.spawn_controller(c)
        for px in ("100", "104", "108", "103", "99"):
            adapter.set_mid(Decimal(px))
            await orch.tick_controller(c.id)
            for o in list(adapter.placed):
                if o.id in adapter._orders and o.filled_base == 0:
                    adapter.fill_order(o.id)
            await orch.tick_controller(c.id)
        assert adapter.placed, "expected R-Grid to have quoted"
        crossing = [o for o in adapter.placed if o.order_type is not OrderType.LIMIT_MAKER]
        assert all(o.order_type is OrderType.LIMIT for o in crossing), (
            f"a non-post-only, non-market order was placed: {crossing}"
        )
        # Every crossing order is a reduce-only stop, never an entry.
        stops = [e for e in c.my_executors(active_only=False)
                 if getattr(e, "leg", None) == LEG_TRAIL_STOP]
        assert len(stops) == len(crossing), "a crossing order that was not the stop"
        assert all(e.config.position_action is PositionAction.CLOSE for e in stops)

    asyncio.run(body())


def test_the_executor_allows_a_crossing_limit_only_for_the_exit():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    from src.nadobro.engine.executors.rgrid_maker_executor import build_trail_stop

    # Allowed: the stop.
    ok = RGridMakerExecutor(
        build_trail_stop(PAIR, TradeType.SELL, Decimal(1), price=Decimal("99.7")),
        user_id=1, controller_id="RG", adapter=adapter, leg=LEG_TRAIL_STOP,
    )
    assert ok.config.execution_strategy is ExecutionStrategy.LIMIT and ok.is_exit
    # Refused: a crossing (non-post-only) order dressed as an entry.
    market_entry = OrderExecutorConfig(PAIR, TradeType.BUY, Decimal(1),
                                       ExecutionStrategy.LIMIT, price=Decimal(100))
    with pytest.raises(ValueError, match="maker-only"):
        RGridMakerExecutor(market_entry, user_id=1, controller_id="RG",
                           adapter=adapter, leg=LEG_ENTRY)
    # Refused: a stop that is not reduce-only.
    not_reduce_only = OrderExecutorConfig(
        PAIR, TradeType.SELL, Decimal(1), ExecutionStrategy.LIMIT,
        price=Decimal(100), position_action=PositionAction.OPEN,
    )
    with pytest.raises(ValueError, match="reduce-only"):
        RGridMakerExecutor(not_reduce_only, user_id=1, controller_id="RG",
                           adapter=adapter, leg=LEG_TRAIL_STOP)


def test_the_reducing_leg_is_reduce_only_and_the_adding_leg_is_not():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    entry = build_maker_quote(PAIR, TradeType.BUY, Decimal(1), Decimal(99))
    exit_ = build_maker_quote(PAIR, TradeType.SELL, Decimal(1), Decimal(101), reduce_only=True)
    e = RGridMakerExecutor(entry, user_id=1, controller_id="RG", adapter=adapter, leg=LEG_ENTRY)
    x = RGridMakerExecutor(exit_, user_id=1, controller_id="RG", adapter=adapter, leg=LEG_EXIT)
    assert e.config.position_action is PositionAction.OPEN and not e.is_exit
    assert x.config.position_action is PositionAction.CLOSE and x.is_exit


# ==========================================================================
# 2. The anchor
# ==========================================================================
def test_anchor_is_the_average_of_the_two_leg_exposure_prices():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter)
    _seed_leg(c, "buy", 100, Decimal(2))
    _seed_leg(c, "buy", 100, Decimal(1))
    _seed_leg(c, "sell", 110, Decimal(1))
    assert c.leg_exposure_price("buy") == Decimal(100)
    assert c.leg_exposure_price("sell") == Decimal(110)
    # Midpoint of the legs — NOT the volume-blended 102.5.
    assert c.exposure_anchor() == Decimal(105)


def test_one_sided_book_anchors_on_the_leg_that_traded():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter)
    _seed_leg(c, "buy", 100)
    _seed_leg(c, "buy", 104)
    assert c.exposure_anchor() == Decimal(102)
    assert c.leg_exposure_price("sell") is None


def test_discretion_windows_each_leg_independently():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={"vwap_volume_fraction": Decimal("0.5")})
    for px in (100, 100, 110, 120):
        _seed_leg(c, "buy", px)
    assert c.leg_exposure_price("buy") == Decimal("115")   # last 50% of volume
    c.vwap_volume_fraction = Decimal(0)
    assert c.leg_exposure_price("buy") == Decimal("107.5")


def test_both_legs_feed_the_exposure_window():
    """Essential to the definition: the anchor is the average of the buy AND sell
    exposure prices, so excluding the reducing side would leave the sell exposure
    price permanently undefined.

    ENTRIES REST, EXITS CROSS: the reducing side is a crossing trigger now rather
    than a resting quote, so the sell fill arrives from the exposure-band exit. It
    must still feed the sell window — the anchor is what it is regardless of which
    order type produced the fill."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=True)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        # A live long of 2, so a PARTIAL reducing fill leaves the book non-flat and
        # the flat re-anchor (which correctly clears the window) does not fire.
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(2), Decimal(200), Decimal(0))
        _seed_leg(c, "buy", 100, Decimal(2))     # anchor 100 -> sell trigger at 99.9
        adapter.set_mid(Decimal("99"))           # through the trigger -> exit fires
        await orch.tick_controller(c.id)
        crossing = [o for o in adapter.placed
                    if o.side is TradeType.SELL and o.order_type is OrderType.LIMIT]
        assert crossing, "the exposure-band exit should have crossed"
        # Ingest the crossing fill, then absorb directly rather than ticking the
        # controller: the same controller tick that absorbs it also sees a flat book
        # and correctly re-anchors, clearing the window, so a full tick cannot
        # observe the intermediate state.
        if c._stop_id is not None:
            await orch.tick(c._stop_id)
        c._absorb_fills()
        assert c.leg_exposure_price("sell") is not None, (
            "a reducing fill never reached the sell exposure window"
        )
        assert c.leg_exposure_price("buy") is not None
        # And with both legs present the anchor really is their average.
        buy_px, sell_px = c.leg_exposure_price("buy"), c.leg_exposure_price("sell")
        assert c.exposure_anchor() == (buy_px + sell_px) / 2

    asyncio.run(body())


def test_seed_from_session_history_scopes_the_anchor_to_the_run():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    seed = [
        {"price": 120, "size": 1, "side": "long"},
        {"price": 100, "size": 1, "side": "long"},
        {"price": 110, "size": 1, "side": "short"},
    ]
    _, c = _controller(adapter, extra={"seed_fills": seed})
    assert c.leg_exposure_price("buy") == Decimal(110)      # (100 + 120) / 2
    assert c.leg_exposure_price("sell") == Decimal(110)
    assert c.exposure_anchor() == Decimal(110)
    _, fresh = _controller(adapter)
    assert fresh.exposure_anchor() is None


# ==========================================================================
# 3. Postability — the geometry that makes it momentum
# ==========================================================================
def test_inside_the_band_neither_leg_can_rest():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)          # anchor := 100
        for px in ("100", "100.05", "99.95"):    # all inside 100 +- 0.1%
            adapter.set_mid(Decimal(px))
            await orch.tick_controller(c.id)
        assert adapter.placed == [], "crossed instead of waiting inside the band"

    asyncio.run(body())


def test_above_the_band_only_the_buy_leg_rests():
    """Price has risen past anchor x (1+spread), so that bid is now BELOW market —
    a valid maker order, and the one that buys into strength on a pullback."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)          # anchor := 100
        adapter.set_mid(Decimal("101"))          # price rose past 100.1
        await orch.tick_controller(c.id)
        assert len(_resting(adapter, TradeType.BUY)) == 1
        assert _resting(adapter, TradeType.SELL) == []
        assert _resting(adapter, TradeType.BUY)[0].price == Decimal(100) * (1 + SPREAD)

    asyncio.run(body())


def test_below_the_band_only_the_sell_leg_rests():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)          # anchor := 100
        adapter.set_mid(Decimal("99"))           # price fell past 99.9
        await orch.tick_controller(c.id)
        assert len(_resting(adapter, TradeType.SELL)) == 1
        assert _resting(adapter, TradeType.BUY) == []
        assert _resting(adapter, TradeType.SELL)[0].price == Decimal(100) * (1 - SPREAD)

    asyncio.run(body())


def test_a_post_only_price_is_never_sent_on_the_crossing_side():
    """The venue rejects a crossing post-only order (error_code 2008), and R-Grid
    must not cross to force a fill — so the leg is simply not sent."""
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter)
    mid = Decimal(100)
    assert c._is_postable(TradeType.BUY, Decimal("99.5"), mid) is True
    assert c._is_postable(TradeType.BUY, Decimal("100.5"), mid) is False
    assert c._is_postable(TradeType.SELL, Decimal("100.5"), mid) is True
    assert c._is_postable(TradeType.SELL, Decimal("99.5"), mid) is False


def test_no_fills_waits_at_the_seeded_mid():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)          # mid == anchor: inside the band
        assert adapter.placed == []
        assert c.exposure_anchor() == Decimal(100)

    asyncio.run(body())


# ==========================================================================
# 4. Sizing
# ==========================================================================
def test_the_exposure_band_exit_CROSSES_the_whole_position():
    """ENTRIES REST, EXITS CROSS. The reducing side is a TRIGGER, not a quote: when
    mid reaches anchor*(1-band) for a long, R-Grid crosses the WHOLE position in one
    reduce-only MARKET order. A turn books everything at once instead of a step per
    tick while the move runs against it.

    This test previously asserted the reducing leg RESTED, and passed only because
    the `_resting` helper did not filter by order type — the MARKET exit satisfied
    it. Both are fixed."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=True)
        orch, c = _controller(adapter, extra={"order_amount_quote": Decimal(10)})
        await orch.spawn_controller(c)
        # Net long 3 units, anchor 100 -> sell trigger at 99.9.
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(3), Decimal(300), Decimal(0))
        _seed_leg(c, "buy", 100, Decimal(3))
        adapter.set_mid(Decimal("99"))           # through the trigger
        await orch.tick_controller(c.id)

        crossed = _crossings(adapter, TradeType.SELL)
        assert crossed, "the exposure-band exit did not cross"
        assert crossed[-1].amount_base == Decimal(3), "must close the WHOLE position"
        assert not _resting(adapter, TradeType.SELL), (
            "nothing may rest on the reducing side — it is a trigger now"
        )

    asyncio.run(body())


def test_the_adding_leg_still_rests_post_only_while_a_position_is_open():
    """The other half of the ruling: entries never cross. With a long open, the BUY
    that adds to it is still a post-only maker."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=True)
        orch, c = _controller(adapter, extra={"order_amount_quote": Decimal(10)})
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(3), Decimal(300), Decimal(0))
        _seed_leg(c, "buy", 100, Decimal(3))
        adapter.set_mid(Decimal("101"))          # above the anchor: the adder quotes
        await orch.tick_controller(c.id)

        assert _resting(adapter, TradeType.BUY), "the adding leg must rest post-only"
        assert not _crossings(adapter), "an entry must never cross"

    asyncio.run(body())


def test_a_quote_is_rounded_down_to_the_lot_and_refused_below_the_minimum():
    """NadoClient GROWS a sub-minimum non-reducing order, which would rest more
    than the risk engine and the step cap were sized against."""
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), lot=Decimal("0.5"), min_notional=Decimal(1))
    _, c = _controller(adapter)
    assert c._quantize_quote(Decimal("1.2"), Decimal(100)) == Decimal("1.0")
    assert c._quantize_quote(Decimal("0.37"), Decimal(100)) is None

    tiny = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), lot=Decimal("0.001"), min_notional=Decimal(50))
    _, c2 = _controller(tiny)
    assert c2._quantize_quote(Decimal("0.1"), Decimal(100)) is None      # $10 < $50
    assert c2._quantize_quote(Decimal("1.0"), Decimal(100)) == Decimal("1.0")


# ==========================================================================
# 5. Soft reset — a re-quote, not a market order
# ==========================================================================
def test_the_armed_soft_reset_moves_the_exit_leg_up_with_the_trend():
    """"Adjusts the opposite leg to follow the trend and lock in profits" is
    literally a re-quote of that leg — no crossing involved."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter, extra={
            "reset_threshold_pct": Decimal("0.01"), "trail_enabled": True,
        })
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(1), Decimal(100), Decimal(0))
        _seed_leg(c, "buy", 100)
        # +8% and well past the arm threshold: the trail arms at the peak.
        adapter.set_mid(Decimal("108"))
        await orch.tick_controller(c.id)
        assert c._trail_armed is True
        assert c._trail_peak == Decimal("108")
        # AT the peak the trail is not yet breached, so nothing crosses.
        assert c._trail_breached(Decimal("108"), Decimal(1)) is False
        assert [o for o in adapter.placed if o.order_type is OrderType.LIMIT] == []

        # Price comes back THROUGH the trailed level (108 x 0.999 = 107.892) — the
        # one place a post-only ask cannot sit. The stop crosses.
        adapter.set_mid(Decimal("107"))
        await orch.tick_controller(c.id)
        stops = [o for o in adapter.placed if o.order_type is OrderType.LIMIT]
        assert len(stops) == 1, "the trailing stop never crossed"
        assert stops[0].side is TradeType.SELL
        assert stops[0].amount_base == Decimal(1), "the stop closes the whole position"

    asyncio.run(body())


def test_the_trail_only_crosses_once_and_cancels_the_resting_legs_first():
    """Leaving the maker exit up alongside the stop would sell the same position
    twice — the second order re-opening the other way once the first flattened us."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter, extra={
            "reset_threshold_pct": Decimal("0.01"), "trail_enabled": True,
        })
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(1), Decimal(100), Decimal(0))
        _seed_leg(c, "buy", 100)
        adapter.set_mid(Decimal("108"))
        await orch.tick_controller(c.id)          # arms
        adapter.set_mid(Decimal("107"))
        await orch.tick_controller(c.id)          # crosses
        assert c._resting == {}, "a resting leg survived alongside the stop"
        stops = [o for o in adapter.placed if o.order_type is OrderType.LIMIT]
        assert len(stops) == 1
        # Further ticks while it settles must not stack a second stop.
        for px in ("106", "105"):
            adapter.set_mid(Decimal(px))
            await orch.tick_controller(c.id)
        assert len([o for o in adapter.placed if o.order_type is OrderType.LIMIT]) == 1

    asyncio.run(body())


def test_the_trail_only_ratchets_forward():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={
        "reset_threshold_pct": Decimal("0.01"), "trail_enabled": True,
    })
    c._position_entry_price = lambda: Decimal(100)     # type: ignore[method-assign]
    c._track_trail(Decimal("108"), Decimal(1))
    assert c._trail_peak == Decimal("108")
    c._track_trail(Decimal("104"), Decimal(1))        # a pullback
    assert c._trail_peak == Decimal("108"), "the trail gave ground"


def test_the_trail_never_arms_underwater():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={
        "reset_threshold_pct": Decimal("0.01"), "trail_enabled": True,
    })
    c._position_entry_price = lambda: Decimal(100)     # type: ignore[method-assign]
    for px in ("99", "97", "95"):
        c._track_trail(Decimal(px), Decimal(1))
    assert c._trail_armed is False, "arming underwater front-runs the SL rail"


def test_an_opposing_overlay_arms_early_but_still_needs_a_profit():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={
        "reset_threshold_pct": Decimal("0.05"),       # far away: no normal arm
        "trail_enabled": True,
        "signal_regime": "trend_down", "signal_confidence": 0.9,
    })
    c._position_entry_price = lambda: Decimal(100)     # type: ignore[method-assign]
    c._track_trail(Decimal("99"), Decimal(1))         # underwater
    assert c._trail_armed is False
    c._track_trail(Decimal("100.5"), Decimal(1))      # barely in profit
    assert c._trail_armed is True


def test_a_supportive_overlay_does_not_cut_the_run_short():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={
        "reset_threshold_pct": Decimal("0.05"), "trail_enabled": True,
        "signal_regime": "trend_up", "signal_confidence": 0.9,
    })
    c._position_entry_price = lambda: Decimal(100)     # type: ignore[method-assign]
    c._track_trail(Decimal("100.5"), Decimal(1))
    assert c._trail_armed is False


def test_going_flat_clears_the_window_and_disarms():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter)
    _seed_leg(c, "buy", 100)
    c._trail_armed, c._trail_peak = True, Decimal(105)
    c._reset_exposure_window(Decimal(104))
    assert not c._has_fills()
    assert c._anchor == Decimal(104)
    assert c._trail_armed is False and c._trail_peak is None


# ==========================================================================
# 6. Queue position + wiring
# ==========================================================================
def test_a_barely_moved_target_keeps_its_queue_position():
    """Cancel/replace churn destroys queue position, which is the entire edge of a
    maker quote."""
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={"price_distance_tolerance": Decimal("0.001")})
    assert c._price_is_close(Decimal("100.00"), Decimal("100.05")) is True
    assert c._price_is_close(Decimal("100.00"), Decimal("101.00")) is False


def test_rgrid_is_always_its_own_controller_and_never_the_phase_switcher():
    from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
    from src.nadobro.strategy.engine_runtime import (
        CONTROLLER_REGISTRY, build_controller, map_strategy_config,
    )

    assert CONTROLLER_REGISTRY["rgrid"] is RGridController
    assert CONTROLLER_REGISTRY["dgrid"] is DynamicGridController
    assert not hasattr(RGridController, "consume_dgrid_event")

    for extra in ({}, {"fill_anchored": 0}, {"fill_anchored": 1}):
        cfg = map_strategy_config(
            "rgrid", {"notional_usd": 100.0, "levels": 2, **extra}, Decimal(100), product=PAIR,
        )
        assert cfg["anchor_mode"] == "rgrid"
        assert cfg["passive_only"] is True, "R-Grid must be maker-only"
        assert cfg["trail_enabled"] is True
        assert cfg["ladder_levels"] == 1
        for dead in ("start_price", "end_price", "dgrid_trend_on_vr",
                     "dgrid_flip_confirm_ticks", "triple_barrier_config"):
            assert dead not in cfg, dead
        built = build_controller(
            "rgrid", user_id=1, configs=cfg,
            orchestrator=ExecutorOrchestrator(), adapter=MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100)),
            inventory=InventoryRepository(),
        )
        assert isinstance(built, RGridController), extra


def test_discretion_maps_to_twice_the_knob_and_clamps():
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    cfg = map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 2, "rgrid_discretion": 0.06},
        Decimal(100), product=PAIR,
    )
    assert cfg["vwap_volume_fraction"] == 0.12
    wide = map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 2, "rgrid_discretion": 0.8},
        Decimal(100), product=PAIR,
    )
    assert wide["vwap_volume_fraction"] == 1.0


def test_session_rails_are_percent_of_margin_and_not_also_a_price_barrier():
    from src.nadobro.strategy.engine_runtime import map_strategy_config
    from src.nadobro.strategy.strategy_registry import effective_sl_tp_pct

    cfg = map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 4, "sl_pct": 0.8,
                  "rgrid_stop_loss_pct": 2.0, "rgrid_take_profit_pct": 5.0},
        Decimal(100), product=PAIR,
    )
    assert "triple_barrier_config" not in cfg
    assert effective_sl_tp_pct(
        "rgrid", {"rgrid_stop_loss_pct": 2.0, "rgrid_take_profit_pct": 5.0}
    ) == (2.0, 5.0)


def test_rgrid_is_never_choked_or_gated_by_the_overlay():
    """SUPPRESS-CAP-ZERO. R-Grid keeps its configured cap, is not put reduce-only,
    and is not gated — it exists to trade the regime that triggers suppression."""
    from src.nadobro.llm.signal_engine import Signal
    from src.nadobro.strategy.overlay_actuator import (
        apply_overrides_to_configs, compute_overrides,
    )

    overrides = compute_overrides("rgrid", Signal(regime="chop", entry_ok=False, confidence=0.6))
    cfg = {"max_net_exposure_pct": 30.0, "order_amount_quote": Decimal(100)}
    apply_overrides_to_configs("rgrid", cfg, overrides)
    assert float(cfg["max_net_exposure_pct"]) == 30.0
    assert "suppress_new_entries" not in cfg
    assert "regime_gate_enabled" not in cfg


def test_metrics_expose_the_anchor_and_both_leg_prices():
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={"reset_threshold_pct": Decimal("0.01")})
    _seed_leg(c, "buy", 100)
    c._last_anchor = c.exposure_anchor()
    m = c.grid_metrics()
    assert m["grid_mode"] == "rgrid"
    assert m["grid_anchor_price"] == 100.0
    assert m["rgrid_buy_trigger"] == pytest.approx(100.1)
    assert m["rgrid_sell_trigger"] == pytest.approx(99.9)
    assert m["rgrid_trail_armed"] is False


def test_a_rebuilt_controller_will_not_cross_off_a_seeded_anchor():
    """RGRID-SEED-ANCHOR. ``seed_fills`` restores the whole SESSION's fills — not
    the open position's — and the window is only ever scrubbed on a flat book. So a
    controller rebuilt while a position is OPEN inherits prior cycles' prints,
    including their EXIT prices.

    Survivable when the reducing side merely rested; not now that it CROSSES. Here
    cycle 1 shorted at 100 and covered at 95, cycle 2 is long at 96: the seeded
    anchor is 97.75, whose sell trigger sits ABOVE mid, so an ungated band exit
    would dump a healthy long at market on the very first tick. It must wait for a
    live fill on the position's own side; the session rail remains the hard stop."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(96))
        orch, c = _controller(adapter, extra={"order_amount_quote": Decimal(10)})
        # The rebuild: session history, both legs, from a CLOSED prior cycle.
        c._seed_from_history([
            {"price": 95, "size": 1, "side": "buy"},
            {"price": 100, "size": 1, "side": "sell"},
        ])
        await orch.spawn_controller(c)
        # ...and we come back holding cycle 2's long.
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(3), Decimal(288), Decimal(0))
        assert c.exposure_anchor(Decimal(96)) > Decimal(96), (
            "premise: the seeded anchor sits above mid, so the trigger is reached"
        )

        await orch.tick_controller(c.id)

        assert not _crossings(adapter), (
            "a seeded anchor flattened a healthy position at market"
        )
        assert c._net_base() == Decimal(3), "the position was closed"

        # One live fill on the position's own side, and the exit is trusted again.
        _seed_leg(c, "buy", 96, Decimal(3))
        await orch.tick_controller(c.id)
        assert _crossings(adapter), "a live-filled position must still be able to exit"

    asyncio.run(body())


def test_a_refused_exit_is_counted_and_escalated_not_retried_in_silence():
    """The constant _MAX_CONSECUTIVE_EXIT_FAILURES existed but nothing used it: a
    refused exit (kill switch, or max_open_executors — reduce-only is exempt from
    the SIZE caps but not from those) left the position open, logged nothing, and
    retried forever."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        orch, c = _controller(adapter, extra={"order_amount_quote": Decimal(10)})
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(3), Decimal(300), Decimal(0))
        _seed_leg(c, "buy", 100, Decimal(3))

        async def _refuse(*a, **k):
            return False
        c.spawn_executor = _refuse                      # type: ignore[assignment]

        for expected in (1, 2, 3):
            assert await c._fire_trail_stop(Decimal(3), Decimal(99)) is False
            assert c._exit_failures == expected, "refusals are not being counted"

        # A success clears it, so the escalation tracks CONSECUTIVE failures.
        c._exit_failures = 4
        del c.spawn_executor
        assert await c._fire_trail_stop(Decimal(3), Decimal(99)) is True
        assert c._exit_failures == 0

    asyncio.run(body())
