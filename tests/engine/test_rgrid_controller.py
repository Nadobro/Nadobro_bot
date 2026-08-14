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

from src.nadobro.engine.controllers.rgrid import _STOP_STALE_TICKS, RGridController
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
    a valid maker order, and the one that buys into strength on a pullback.

    It rests NEAR the market, not at the seed. This used to assert the bid landed
    on 100.1 — anchor x (1+spread) off the frozen session-start anchor — which is
    89bp under a market that had just printed 101, i.e. an order price action had
    already left behind. The flat anchor is on a leash now, so the bid trails to
    within about one band of the touch and is something that can actually fill.
    """
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)          # anchor := 100
        adapter.set_mid(Decimal("101"))          # price rose past 100.1
        await orch.tick_controller(c.id)
        assert len(_resting(adapter, TradeType.BUY)) == 1
        assert _resting(adapter, TradeType.SELL) == []
        px = _resting(adapter, TradeType.BUY)[0].price
        assert px < Decimal("101"), "a resting bid must not cross"
        assert px > Decimal("101") * (1 - 2 * SPREAD), (
            f"bid {px} is stale — it should trail mid, not sit on the seeded anchor"
        )

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
        px = _resting(adapter, TradeType.SELL)[0].price
        assert px > Decimal("99"), "a resting ask must not cross"
        assert px < Decimal("99") * (1 + 2 * SPREAD), (
            f"ask {px} is stale — it should trail mid, not sit on the seeded anchor"
        )

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


def test_a_resting_entry_is_not_withdrawn_at_the_touch():
    """RGRID-NO-ORDERS (2026-08-09) part 2.

    Post-only binds when an order is SENT, not while it sits on the book, and a
    resting bid stops being "postable" exactly when mid reaches it — the moment it
    is about to be hit. Cancelling there withdrew the entry at the one instant it
    could fill, so R-Grid only ever got in when the venue's fill beat the next tick
    (~8-10s apart in prod). The order must survive mid arriving at its price.
    """
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        adapter.set_mid(Decimal("100.15"))       # break up: the bid rests
        await orch.tick_controller(c.id)
        resting = _resting(adapter, TradeType.BUY)
        assert len(resting) == 1
        bid = resting[0]

        adapter.set_mid(bid.price)               # the market comes to the bid
        await orch.tick_controller(c.id)
        assert bid.id not in adapter.cancelled, (
            "the entry was cancelled at the exact price it would have filled"
        )
        assert not bid.state.is_terminal

    asyncio.run(body())


def test_the_flat_anchor_is_not_frozen_at_the_session_start_mid():
    """RGRID-NO-ORDERS (2026-08-09) part 1 — the reported "no orders, just skipping".

    While flat with an empty window nothing but the seed writes ``_anchor``
    (``_reset_exposure_window`` is gated on ``_has_fills()``), so every break was
    measured against wherever price sat when the user pressed start. Once price
    left that level R-Grid rested one entry at the seeded trigger, price walked
    away, and — the anchor only moving on a FILL — there was no way back: inert for
    the rest of the session.

    Live repro before the fix: 72 ticks over 82bp of range produced ONE order,
    zero fills, the anchor still on its seed and the bid stranded 72bp under the
    market. The anchor must track mid while flat.
    """
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)                  # seed := 100
        assert c.exposure_anchor() == Decimal(100)

        px = Decimal(100)
        for _ in range(40):                               # a steady 6bp-per-tick trend
            px *= Decimal("1.0006")
            adapter.set_mid(px)
            await orch.tick_controller(c.id)

        assert c._net_base() != 0 or _resting(adapter, TradeType.BUY), (
            "R-Grid sat out an 82bp trend entirely"
        )
        anchor = c.exposure_anchor(px)
        assert anchor > Decimal(100), "the anchor never left its seed"
        # Never further from the market than the leash allows.
        assert anchor > px * (1 - 3 * SPREAD), f"anchor {anchor} went stale vs mid {px}"

    asyncio.run(body())


def test_a_break_still_needs_a_real_move_after_the_leash():
    """The leash must not dissolve the break. Re-seeding to mid every tick would be
    the opposite failure — the band could never be breached and nothing would ever
    rest — so inside the leash the anchor does not move at all."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        for px in ("100.05", "99.95", "100.02", "99.98"):   # all inside the band
            adapter.set_mid(Decimal(px))
            await orch.tick_controller(c.id)
        assert adapter.placed == [], "quoted without a break"
        assert c.exposure_anchor() == Decimal(100), "the anchor drifted inside the band"

    asyncio.run(body())


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

        # The give-back is the ARM threshold, not the entry band: arming at +1%
        # puts the stop at 108 x (1 - 0.01) = 106.92, which is breakeven-ish on a
        # position opened at 100 and ratchets up from there. It used to be one
        # entry band (107.892) -- tighter than an ordinary pullback, so the run was
        # cut almost immediately.
        assert c._trail_price(Decimal(1)) == Decimal("106.92")
        adapter.set_mid(Decimal("106.9"))        # back THROUGH the trailed level
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
        await orch.tick_controller(c.id)          # arms; trail sits at 106.92
        adapter.set_mid(Decimal("106.9"))
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


# ==========================================================================
# 9. Trend following — R-Grid's actual mandate
#
# "As the market is trending, rgrid follows the asset price and buys or sells in
#  the direction of the price. If the asset price is dumping, it shorts and adds
#  more shorts as the price dumps more. If the price switches and starts pumping,
#  rgrid switches to Long and adds more Longs as the price continues to increase."
#
# Four separate defects made that impossible. Each has a guardrail here; each was
# verified to FAIL with its own fix reverted. Measured end to end on the repo's
# cost-aware backtester through the real mapped config, the shipped geometry
# returned -85.27 across five trending regimes and the fixed one +487.19.
# ==========================================================================
def test_the_add_leg_marches_with_the_trend_instead_of_converging():
    """RGRID-STALE-ADD. The add used to be quoted off the exposure VWAP, which is
    an AVERAGE: each successive add moves it less than the one before, so the add
    level converges while price runs on. Traced on the backtester, one leg's adds
    came at 2005.20, 2007.20, 2008.21, 2008.88, 2009.38 — steps of 10.0, 5.0, 3.3
    and 2.5bp, decaying to nothing. Off the LAST FILL each add instead needs one
    fresh band of trend extension, so the spacing cannot decay.
    """
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter)
    # A pyramid whose fills are one band apart, as a trend produces.
    for px in (100, 100.1, 100.2, 100.3):
        _seed_leg(c, "buy", px)
    anchor = c.exposure_anchor(Decimal("100.3"))

    ref = c._add_reference(Decimal(1), anchor)
    assert ref == Decimal("100.3"), "the add must key off the most recent fill"
    # The averaging reference lags the newest fill, and the gap only widens.
    assert anchor < Decimal("100.3")

    # Spacing is constant, not decaying: every add needs a full band of extension.
    step_bp = (ref * (1 + SPREAD) / Decimal("100.3") - 1) * Decimal(10000)
    assert abs(step_bp - Decimal(10)) < Decimal("0.01")


def test_the_short_side_pyramids_as_price_dumps():
    """The mirror: a dumping market must keep ADDING shorts, each one band lower."""
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter)
    for px in (100, 99.9, 99.8):
        _seed_leg(c, "sell", px)
    ref = c._add_reference(Decimal(-1), c.exposure_anchor(Decimal("99.8")))
    assert ref == Decimal("99.8")
    # The next short rests one band BELOW the last one — further into the dump.
    assert ref * (1 - SPREAD) < Decimal("99.8")


def test_the_profit_taking_exit_engages_before_the_loss_only_one():
    """RGRID-EXIT-RACE, the dominant money bug.

    The exposure-band exit fires at ``avg_entry x (1 - band)`` and is therefore
    LOSS-ONLY by construction — it can never book a gain. The trailing stop is the
    only exit that can, because it ratchets with the favourable extreme. Shipped,
    the trail armed at 2x the distance the band exit fired at, so on any tape whose
    pullbacks reach one band the loss-only exit ALWAYS won the race and the trail
    was unreachable: a +3.14% uptrend booked -$12.10 realised over 29 fills.

    The invariant: the band exit must sit strictly BEYOND the arm point.
    """
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    for spread in ("0.0005", "0.001", "0.002", "0.005"):
        _, c = _controller(adapter, extra={
            "spread_bid_pct": Decimal(spread), "spread_ask_pct": Decimal(spread),
        })
        assert c._exit_band() > c._arm_pct(), (
            f"at spread {spread} the loss-only exit still outruns the trail"
        )
        # And the give-back puts the armed stop at breakeven, never below it.
        assert c._trail_giveback() == c._arm_pct()


def test_an_armed_trail_protects_breakeven_not_a_loss():
    """Give-back == arm means that at the instant the trail arms at +arm
    favourable, its stop sits at peak x (1-arm) — the entry. A recognised winner
    can never be handed back as a loss. At one entry band the stop sat well inside
    the move and cut it immediately."""
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={"reset_threshold_pct": Decimal("0.01")})
    entry = Decimal(100)
    peak = entry * (Decimal(1) + c._arm_pct())      # exactly the arm point
    c._trail_peak = peak
    c._trail_armed = True
    stop = c._trail_price(Decimal(1))
    assert stop <= entry
    assert stop > entry * Decimal("0.99"), "the give-back overshot breakeven"


def test_the_step_cap_bounds_the_move_to_rgrids_own_exit():
    """RGRID-RAIL-RACE. The step cap exists so the strategy's OWN exit can fire
    before the session rail does, and it sizes against the adverse move to that
    exit. That move is the EXIT band (arm + band), not the entry band — sizing
    against one band while the controller waits for three hands the decision back
    to the rail. Controller and sizer must read the same number."""
    from src.nadobro.quant.rgrid_sizing import exit_band_frac

    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    for spread, reset in (("0.001", "0.002"), ("0.002", "0.004"), ("0.0005", "0.002")):
        _, c = _controller(adapter, extra={
            "spread_bid_pct": Decimal(spread), "spread_ask_pct": Decimal(spread),
            "reset_threshold_pct": Decimal(reset),
        })
        assert c._exit_band() == exit_band_frac(Decimal(spread), Decimal(reset))


def test_the_configured_pyramid_fits_inside_its_own_exposure_ceiling():
    """RGRID-NO-PYRAMID. ``margin_quote`` for this family is the DEPLOYED notional
    and the step is deployed/levels, so a full pyramid is 100% of it. The shared MM
    default of 30% admitted 1.0-1.4 steps: R-Grid took its entry and then had every
    add refused by _projected_order_within_exposure. It could not pyramid at all,
    which is the entire strategy.
    """
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    for margin, lev, levels in ((100, 5, 2), (1000, 4, 4), (250, 20, 3), (500, 3, 2)):
        cfg = map_strategy_config(
            "rgrid",
            {"notional_usd": margin, "leverage": lev, "levels": levels,
             "rgrid_spread_bp": 10.0, "rgrid_stop_loss_pct": 5.0},
            Decimal(2000), product="ETH-PERP", leverage=lev,
        )
        step = Decimal(str(cfg["order_amount_quote"]))
        cap = (Decimal(str(cfg["margin_quote"]))
               * Decimal(str(cfg["max_net_exposure_pct"])) / Decimal(100))
        rungs = max(cap, step) / step
        assert rungs >= Decimal(levels) - Decimal("0.01"), (
            f"margin={margin} lev={lev} levels={levels}: only {rungs:.2f} of "
            f"{levels} planned rungs fit — the pyramid cannot be built"
        )


def test_a_resting_entry_does_not_outlive_the_position_it_belonged_to():
    """RGRID-STALE-LEG — found in the pre-push self-audit, and introduced by the
    fix immediately above it.

    Holding an unpostable resting quote is right when the market has merely come
    to it. It is WRONG once the target has moved off it. The position can be
    flattened out-of-band — the session SL/TP rail, a liquidation, a manual close —
    none of which run _fire_trail_stop and its two-leg cancel. The next tick is
    flat, re-anchors to mid, computes an unpostable target, and an unconditional
    hold would leave the dead position's add leg resting where it can RE-OPEN
    exposure the rail just closed.
    """
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100),
                                  auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(1),
                               Decimal(100), Decimal(0))
        _seed_leg(c, "buy", 100)

        adapter.set_mid(Decimal("100.3"))
        await orch.tick_controller(c.id)
        resting = _resting(adapter, TradeType.BUY)
        assert len(resting) == 1, "expected the add leg to rest"
        add_leg = resting[0]

        # The rail closes the book without going through _fire_trail_stop.
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.SELL, Decimal(1),
                               Decimal("100.3"), Decimal(0))
        assert c._net_base() == 0
        adapter.set_mid(Decimal("100.1"))
        await orch.tick_controller(c.id)

        assert add_leg.state.is_terminal, (
            "the dead position's add leg is still working — it can re-open "
            "exposure the rail just closed"
        )
        assert c._resting.get(TradeType.BUY) is None

    asyncio.run(body())


def test_a_rebuilt_controller_will_not_price_its_add_off_a_seeded_fill():
    """The add reference obeys the same live-fill evidence rule as the crossing
    exit. ``seed_fills`` restores the whole SESSION's prints, exits included, so
    the newest BUY in a seeded window need not be this position's last add —
    pricing off one below mid would rest an add far under the market and fill it
    by adding to a long into a collapse."""
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    _, c = _controller(adapter, extra={
        # newest-first, as get_session_recent_fills returns them
        "seed_fills": [{"price": "92", "size": "1", "side": "buy"},
                       {"price": "95", "size": "1", "side": "sell"}],
    })
    anchor = c.exposure_anchor(Decimal(100))
    # Seeded only: the anchor, not the seeded 92 print.
    assert c._add_reference(Decimal(1), anchor) == anchor
    assert c._add_reference(Decimal(1), anchor) != Decimal(92)

    # A live fill on this leg is the evidence that unlocks it.
    _seed_leg(c, "buy", 101)
    assert c._add_reference(Decimal(1), c.exposure_anchor(Decimal(100))) == Decimal(101)


def test_the_strategy_exit_still_fires_before_the_session_rail():
    """The safety ordering the step cap exists to guarantee, re-proved after the
    exit was widened and the exposure ceiling raised to 100% of deployed.

    R-Grid must be able to exit on its OWN terms; if a full pyramid taking the
    adverse move to its band exit already costs more than the stop budget, the
    session rail always fires first and the user "never sees a losing trade, just
    a strategy that keeps stopping". Both the raised cap and the widened exit push
    against this, so it is checked at the tight-SL / high-leverage corners where
    the stop-budget ceiling has to bind.
    """
    from src.nadobro.quant.rgrid_sizing import TAKER_ROUND_TRIP_RATE, exit_band_frac
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    for margin, lev, levels, sl in ((100, 5, 2, 2.0), (1000, 4, 4, 5.0),
                                    (250, 20, 3, 1.0), (100, 20, 4, 0.5),
                                    (500, 10, 4, 3.0)):
        cfg = map_strategy_config(
            "rgrid",
            {"notional_usd": margin, "leverage": lev, "levels": levels,
             "rgrid_spread_bp": 10.0, "rgrid_stop_loss_pct": sl},
            Decimal(2000), product="ETH-PERP", leverage=lev,
        )
        step = Decimal(str(cfg["order_amount_quote"]))
        deployed = Decimal(str(cfg["margin_quote"]))
        cap_quote = deployed * Decimal(str(cfg["max_net_exposure_pct"])) / Decimal(100)
        reachable = min(max(cap_quote, step), step * Decimal(levels))
        move = exit_band_frac(Decimal(str(cfg["spread_ask_pct"])),
                              Decimal(str(cfg["reset_threshold_pct"])))
        loss_at_own_exit = reachable * (move + TAKER_ROUND_TRIP_RATE)
        stop_budget = Decimal(str(margin)) * Decimal(str(sl)) / Decimal(100)
        assert loss_at_own_exit <= stop_budget, (
            f"margin={margin} lev={lev} levels={levels} SL={sl}%: a full pyramid "
            f"loses ${loss_at_own_exit:.2f} reaching R-Grid's own exit against a "
            f"${stop_budget:.2f} stop — the session rail fires first"
        )


def test_status_reports_the_levels_the_engine_is_actually_working():
    """Telemetry must not re-derive levels the engine no longer uses. Both moved
    in this change: the ADD trigger is off the last fill while a position is open,
    and the EXIT is a wider derived band off the anchor. grid_metrics() computed
    both as anchor x (1 -+ entry band), so the /status card would have quoted the
    user two levels nothing was trading on."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100),
                                  auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(3),
                               Decimal("100.27"), Decimal(0))
        # A real pyramid: the VWAP anchor lags the newest add, which is the whole
        # reason the two references have to be reported separately.
        for px in ("100.0", "100.3", "100.5"):
            _seed_leg(c, "buy", Decimal(px))
        adapter.set_mid(Decimal("100.8"))
        await orch.tick_controller(c.id)

        m = c.grid_metrics()
        anchor = Decimal(str(m["grid_anchor_price"]))
        assert anchor < Decimal("100.5"), "the VWAP must lag the newest add here"
        # The add trigger tracks the LAST FILL, not the anchor.
        assert m["rgrid_buy_trigger"] == pytest.approx(
            float(Decimal("100.5") * (1 + SPREAD))
        )
        assert m["rgrid_buy_trigger"] != pytest.approx(float(anchor * (1 + SPREAD)))
        # The exit is the wider derived band, off the anchor, and below a long.
        assert m["rgrid_exit_band_bp"] == pytest.approx(float(c._exit_band() * 10000))
        assert m["rgrid_exit_trigger"] == pytest.approx(
            float(anchor * (1 - c._exit_band()))
        )
        assert m["rgrid_exit_trigger"] < float(anchor)

    asyncio.run(body())


# ==========================================================================
# 10. Findings from the pre-push SL/TP trace — the exit must stay affordable
# ==========================================================================
def test_the_exit_widens_past_the_cap_so_winners_are_not_capped():
    """RGRID-EXITBAND-INVERT, reconciled per the 2026-08-13 product ruling:
    "when the strategy is in profit, the wins shouldn't be capped."

    The mapper sizes ``exit_band_cap`` against the UNSCALED exit distance, but the
    overlay scales ``spread_ask_pct`` live by up to 3x and _band() reads it, so the
    derived exit outgrows the cap. The earlier fix reconciled by SHRINKING THE ARM
    to fit the cap — which armed the trail sooner and handed back the rest of a
    trend (measured -205/-328/-233bp at overlay x1.5). The ruling reverses that: the
    ARM is never touched, the EXIT widens to arm + band, and the user's own
    %-of-margin session rail becomes the backstop when that exceeds the stop budget.

    So the property is no longer "exit <= cap". It is: the exit sits exactly one
    entry band beyond the arm (the derived geometry's own relationship), and never
    inside the entry trigger.
    """
    adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
    cap = Decimal("0.00314")

    for factor in ("1.5", "2.0", "3.0"):
        _, c = _controller(adapter, extra={
            "spread_bid_pct": SPREAD * Decimal(factor),
            "spread_ask_pct": SPREAD * Decimal(factor),
            "reset_threshold_pct": Decimal("0.002"),
            "exit_band_cap": cap,
        })
        exit_band, arm = c._exit_geometry()
        # THE INVARIANT that stops the loss-only exit winning: exit strictly outside
        # the arm. This is what -85.27 violated; it must hold in EVERY branch — the
        # whole point of the fix.
        assert exit_band > arm, f"overlay x{factor} inverted the geometry"
        assert exit_band >= c._band(), "the exit fell inside the entry trigger"
        # The arm is NEVER shrunk to fit the cap — that is the ruling. It stays at
        # the value the geometry derives from the (scaled) band.
        from src.nadobro.quant.rgrid_sizing import arm_pct
        assert arm == arm_pct(c._band(), c.reset_threshold_pct), (
            f"overlay x{factor} shrank the arm instead of widening the exit"
        )
        # When the cap truncated the exit UNDER the arm, the exit is widened back out
        # to one entry band beyond the arm (winners uncapped, rail is the backstop).
        # When the cap still leaves exit > arm, the cap is honoured as-is — no
        # widening is needed and none happens.
        if exit_band > cap:
            assert exit_band == arm + c._band(), (
                f"overlay x{factor}: inverted cap should widen to arm + band"
            )

    # With no ceiling configured the derived distance is untouched (the identity
    # branch), so the +487-measured geometry is unchanged.
    _, c = _controller(adapter, extra={"reset_threshold_pct": Decimal("0.002")})
    assert c._exit_band() == Decimal("0.003")

def test_a_disarmed_stop_does_not_unlock_the_full_pyramid():
    """SLTP-F4. The 100% exposure default is only safe because the stop-budget
    ceiling tightens it. With the rail disarmed there is no budget, nothing
    tightens, and the full pyramid would run with no stop behind it at all."""
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    armed, disarmed = (
        map_strategy_config(
            "rgrid",
            {"notional_usd": 100, "leverage": 20, "levels": 4,
             "rgrid_spread_bp": 10.0, "rgrid_stop_loss_pct": sl},
            Decimal(2000), product="ETH-PERP", leverage=20,
        )
        for sl in (2.0, 0.0)
    )
    assert float(disarmed["max_net_exposure_pct"]) <= 30.0, (
        "a disarmed stop lifted the exposure ceiling instead of holding it"
    )
    assert float(armed["max_net_exposure_pct"]) <= 100.0
    # No budget to size against means no exit ceiling either.
    assert Decimal(str(disarmed["exit_band_cap"])) == 0


# ==========================================================================
# 11. Findings from the pre-push strategy audit
# ==========================================================================
def test_a_rebuilt_controller_will_not_arm_the_trail_off_a_seeded_cost_basis():
    """AUDIT-F1 (critical). ``_position_entry_price()`` is the leg window's VWAP,
    and ``seed_fills`` restores the whole SESSION — both sides, exits included. A
    previous SHORT cycle's cover is a BUY, so it lands in the BUY deque and drags a
    later long's "entry" down.

    Reproduced before the fix: true entry 96.5, seeded basis 94.25, so tick 1 read
    a 2.4% excursion, armed the trail instantly, and a 31bp dip crossed out of a
    healthy position at a loss — on EVERY worker handoff while a position is open.
    With no live fill the excursion is measured from observed price instead, which
    is what the docstring always claimed the trail did.
    """
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal("96.5"),
                                  auto_fill_market=False)
        orch, c = _controller(adapter, extra={
            "reset_threshold_pct": Decimal("0.002"), "trail_enabled": True,
            # newest-first: long @96.5, and cycle 1's short @100 covered @92.
            "seed_fills": [{"price": "96.5", "size": "2", "side": "buy"},
                           {"price": "92", "size": "2", "side": "buy"},
                           {"price": "100", "size": "2", "side": "sell"}],
        })
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(2),
                               Decimal("96.5"), Decimal(0))
        # The polluted basis is still there — we simply must not arm off it.
        assert c._position_entry_price() < Decimal("96.5")

        adapter.set_mid(Decimal("96.5"))
        await orch.tick_controller(c.id)
        assert c._trail_armed is False, "armed off a seeded cost basis"
        assert c._trail_origin == Decimal("96.5")

        adapter.set_mid(Decimal("96.2"))          # a 31bp dip
        await orch.tick_controller(c.id)
        assert _crossings(adapter) == [], "a 31bp dip dumped a healthy position"

    asyncio.run(body())


def test_a_refused_exit_never_falls_through_into_an_add():
    """AUDIT-F3. ``_fire_trail_stop`` returns False when the risk engine or the
    kill switch declines the close (reduce-only is exempt from the SIZE caps, but
    not from max_open_executors or the kill switch). The tick used to fall through
    to the sizing branch and rest a fresh ADD on the losing side — on the very tick
    it had just failed to close and logged "the session rail is the only stop
    left". A refused close is never a licence to add."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100),
                                  auto_fill_market=False)
        orch, c = _controller(adapter, extra={
            "reset_threshold_pct": Decimal("0.002"), "trail_enabled": True,
        })
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(1),
                               Decimal(100), Decimal(0))
        _seed_leg(c, "buy", 100)

        async def _refuse(*_a, **_k):
            return False
        c.spawn_executor = _refuse                    # type: ignore[assignment]

        adapter.set_mid(Decimal("108"))               # arms the trail
        await orch.tick_controller(c.id)
        adapter.set_mid(Decimal("104"))               # breaches it; the close is refused
        await orch.tick_controller(c.id)

        assert c._exit_failures >= 1, "the refusal was not recorded"
        assert _resting(adapter, TradeType.BUY) == [], (
            "an ADD was rested on the losing side after the close was refused"
        )

    asyncio.run(body())


def test_the_stop_budget_covers_the_crossing_print_not_just_the_fees():
    """AUDIT-F4. The exit is priced THROUGH the touch so it actually fills, bounded
    at _EXIT_CROSS_BP. That bound is realised cost on the way out, so the exposure
    ceiling has to carry it; sized against fees alone the exit could print 30bp
    worse than the modelled level — up to 1.8x the stop at shipped defaults."""
    from src.nadobro.quant.rgrid_sizing import exit_cost_frac
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    for margin, lev, levels, sl in ((100, 20, 4, 0.8), (100, 5, 2, 2.0),
                                    (1000, 4, 4, 5.0), (250, 20, 3, 1.0)):
        cfg = map_strategy_config(
            "rgrid",
            {"notional_usd": margin, "leverage": lev, "levels": levels,
             "rgrid_spread_bp": 10.0, "rgrid_stop_loss_pct": sl},
            Decimal(2000), product="ETH-PERP", leverage=lev,
        )
        step = Decimal(str(cfg["order_amount_quote"]))
        cap = (Decimal(str(cfg["margin_quote"]))
               * Decimal(str(cfg["max_net_exposure_pct"])) / Decimal(100))
        reachable = min(max(cap, step), step * Decimal(levels))
        worst = reachable * exit_cost_frac(
            Decimal(str(cfg["spread_ask_pct"])),
            Decimal(str(cfg["reset_threshold_pct"])),
        )
        budget = Decimal(str(margin)) * Decimal(str(sl)) / Decimal(100)
        assert worst <= budget, (
            f"margin={margin} lev={lev} SL={sl}%: worst-case ${worst:.2f} at the "
            f"strategy's own exit exceeds the ${budget:.2f} stop"
        )


def test_an_unfilled_crossing_exit_is_repriced_not_left_stranded():
    """AUDIT-F2. The crossing exit is a bounded marketable limit, and OrderExecutor
    neither times out nor re-prices a plain LIMIT — it terminates only on FILLED /
    CANCELLED / REJECTED, and a partial reports PARTIALLY_FILLED (not terminal).
    So on a gapped or one-sided book the exit rested unfilled and on_tick returned
    at the in-flight guard EVERY tick, forever: no re-price, no second attempt, a
    position with a stranded exit and a frozen controller — in exactly the fast
    tape the exit exists for."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=False, mid=Decimal(100),
                                  auto_fill_market=False)
        orch, c = _controller(adapter, extra={
            "reset_threshold_pct": Decimal("0.002"), "trail_enabled": True,
        })
        await orch.spawn_controller(c)
        c.inventory.apply_fill(1, PAIR, c.id, TradeType.BUY, Decimal(1),
                               Decimal(100), Decimal(0))
        _seed_leg(c, "buy", 100)

        adapter.set_mid(Decimal("108"))
        await orch.tick_controller(c.id)               # arms
        adapter.set_mid(Decimal("104"))
        await orch.tick_controller(c.id)               # crosses; the book never fills it
        first = _crossings(adapter)
        assert len(first) == 1, "expected the exit to have crossed"
        assert c._stop_id is not None

        # It sits unfilled. After the stale window it must be re-priced, not held.
        for _ in range(_ST := 6):
            adapter.set_mid(Decimal("103"))
            await orch.tick_controller(c.id)
        assert len(_crossings(adapter)) > 1, (
            "the exit was never re-priced — the controller froze with a stranded order"
        )
        # And the replacement chases the market rather than repeating a dead price.
        assert _crossings(adapter)[-1].price < first[0].price

    asyncio.run(body())
