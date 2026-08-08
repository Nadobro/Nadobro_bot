"""GridExecutor tests: level generation, activation_bounds, limit_price stop,
per-level fill -> close, keep_position, adversarial."""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.nadobro.engine.executors.grid_executor import (
    GridExecutor,
    GridExecutorConfig,
    GridLevelState,
    generate_grid_levels,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import CloseType, OrderType, TradeType
from tests.engine._mock_nado import MockNadoAdapter

PAIR = "SOL-USDC"


def _cfg(**kw):
    base = dict(
        trading_pair=PAIR, side=TradeType.BUY, start_price=Decimal(100), end_price=Decimal(110),
        limit_price=Decimal(95), total_amount_quote=Decimal(1000),
        min_spread_between_orders=Decimal("0.02"),
    )
    base.update(kw)
    return GridExecutorConfig(**base)


def _ex(cfg, adapter, inv=None):
    return GridExecutor(cfg, user_id=1, controller_id="c", adapter=adapter, inventory=inv)


def test_level_generation_count_and_prices():
    levels = generate_grid_levels(_cfg())
    assert len(levels) == 5
    assert [lv.open_price for lv in levels] == [
        Decimal(100), Decimal("102.5"), Decimal(105), Decimal("107.5"), Decimal(110)
    ]
    assert levels[0].close_price == Decimal(100) * Decimal("1.02")
    # per-level capital 1000/5 = 200
    assert levels[0].amount_base == Decimal(200) / Decimal(100)


def test_on_create_places_all_in_bounds_open_orders():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        ex = _ex(_cfg(), adapter)
        await ex.on_create()
        assert ex.is_active
        assert len(adapter.placed) == 5
        assert all(o.order_type is OrderType.LIMIT_MAKER for o in adapter.placed)

    asyncio.run(body())


def test_level_fill_places_close_then_completes():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        inv = InventoryRepository()
        ex = _ex(_cfg(), adapter, inv)
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        await ex.on_tick()
        assert lvl.state is GridLevelState.CLOSE_ORDER_PLACED
        assert lvl.close_order_id is not None
        assert inv.get(1, PAIR, "c").buy_amount_base > 0
        adapter.fill_order(lvl.close_order_id, price=lvl.close_price)
        await ex.on_tick()
        assert lvl.state is GridLevelState.COMPLETE

    asyncio.run(body())


def test_activation_bounds_skips_far_levels():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        ex = _ex(_cfg(activation_bounds=Decimal("0.03")), adapter)
        await ex.on_create()
        # within 3% of 100 -> only 100 and 102.5
        assert len(adapter.placed) == 2

    asyncio.run(body())


def test_activation_bounds_cancels_when_mid_moves_away():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        ex = _ex(_cfg(activation_bounds=Decimal("0.03")), adapter)
        await ex.on_create()
        lvl0 = ex.levels[0]
        oid = lvl0.open_order_id
        adapter.set_mid(Decimal(110))  # 100 now ~9% from mid -> out of bounds
        await ex.on_tick()
        assert lvl0.state is GridLevelState.NOT_ACTIVE
        assert oid in adapter.cancelled

    asyncio.run(body())


def test_limit_price_breach_triggers_stop_loss():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        ex = _ex(_cfg(limit_price=Decimal(98)), adapter)
        await ex.on_create()
        adapter.set_mid(Decimal(97))  # below hard stop
        await ex.on_tick()
        assert ex.is_terminated and ex.close_type is CloseType.STOP_LOSS

    asyncio.run(body())


def test_take_profit_breach_triggers_take_profit():
    """GRID-TP-DEAD fix: a favorable move of take_profit from the average entry
    flattens the position with CloseType.TAKE_PROFIT. Previously take_profit was
    passed into the barrier but never read, so this never fired."""
    from src.nadobro.engine.types import TripleBarrierConfig

    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        # BUY grid; 5% take-profit, no limit_price/stop so only TP can fire.
        cfg = _cfg(
            start_price=Decimal(100), end_price=Decimal(100), limit_price=Decimal(0),
            triple_barrier_config=TripleBarrierConfig(take_profit=Decimal("0.05"), stop_loss=None),
        )
        ex = _ex(cfg, adapter, InventoryRepository())
        await ex.on_create()
        # Fill a level so there's an average entry (~100).
        adapter.fill_order(ex.levels[0].open_order_id, price=Decimal(100))
        await ex.on_tick()
        assert not ex.is_terminated            # +0% move, TP not hit yet
        adapter.set_mid(Decimal(106))          # +6% > 5% TP from avg entry
        await ex.on_tick()
        assert ex.is_terminated and ex.close_type is CloseType.TAKE_PROFIT

    asyncio.run(body())


def test_no_take_profit_without_a_position():
    """TP is inert until there's a real average entry — no position, nothing to
    take, even if mid runs far above the band."""
    from src.nadobro.engine.types import TripleBarrierConfig

    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        cfg = _cfg(limit_price=Decimal(0),
                   triple_barrier_config=TripleBarrierConfig(take_profit=Decimal("0.01"), stop_loss=None))
        ex = _ex(cfg, adapter, InventoryRepository())
        await ex.on_create()
        adapter.set_mid(Decimal(130))  # way up, but nothing filled
        await ex.on_tick()
        assert not ex.is_terminated

    asyncio.run(body())


def test_reduce_position_books_through_executor_and_advances_accounting():
    """DGRID-BOOK-RACE fix: reduce_position works the close THROUGH this executor
    — records the fill in the shared inventory (net drops) and advances per-level
    close accounting, so fully booked levels complete and their resting close legs
    are cancelled and the executor and the venue can't drift apart.

    TAKER RISK EXITS: it crosses, so it returns what it closed synchronously."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        inv = InventoryRepository()
        ex = _ex(_cfg(), adapter, inv)
        await ex.on_create()
        held_levels = ex.levels[:2]
        for lv in held_levels:
            adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        net_before = inv.get(1, PAIR, "c").net_amount_base
        assert net_before > 0
        one_level_base = held_levels[0].filled_base
        cancelled_before = len(adapter.cancelled)

        booked = await ex.reduce_position(one_level_base)

        assert booked > 0, "a crossing exit must book in the same call"
        net_after = inv.get(1, PAIR, "c").net_amount_base
        assert abs(float(net_before - net_after - booked)) < 1e-9
        assert any(lv.state is GridLevelState.COMPLETE for lv in ex.levels)
        assert len(adapter.cancelled) > cancelled_before

    asyncio.run(body())


def test_reduce_position_caps_at_held_inventory():
    """Asking to reduce more than is held only books what's actually held."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        inv = InventoryRepository()
        ex = _ex(_cfg(), adapter, inv)
        await ex.on_create()
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        held = lv.filled_base
        booked = await ex.reduce_position(held * Decimal(10))   # ask for way more
        assert abs(float(booked - held)) < 1e-9

    asyncio.run(body())


def test_the_profit_tier_exit_crosses():
    """TAKER RISK EXITS (2026-08-08). A profit tier is a risk exit, so it crosses.
    The maker version rested post-only and chased the touch, which is what let a
    tier chase a reversing mid until it booked a LOSS, strand below one lot, and
    orphan an order nobody was left to drive. The ladder's own paired close legs
    stay post-only — those are the strategy, not a risk exit."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        ex = _ex(_cfg(), adapter, InventoryRepository())
        await ex.on_create()
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        opens = [o for o in adapter.placed if o.order_type is OrderType.LIMIT]
        assert not opens, "the ladder itself must not cross"

        await ex.reduce_position(lv.filled_base)

        crossing = [o for o in adapter.placed if o.order_type is OrderType.LIMIT]
        assert len(crossing) == 1, "the tier exit did not cross"
        assert crossing[0].side is ex.close_side
        # ...and the ladder's own legs are still makers.
        ladder = [o for o in adapter.placed if o.order_type is OrderType.LIMIT_MAKER]
        assert ladder, "the paired close legs must still rest post-only"

    asyncio.run(body())


def test_a_sub_minimum_slice_still_exits_because_it_crosses():
    """The maker version HELD a slice under the venue minimum, because a resting
    order that small can never fill. Crossing has no such floor — a MARKET order is
    not subject to the resting minimum — so the exit always goes out. This is one
    of the concrete reasons risk exits cross."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105), min_notional=Decimal(1_000_000))
        ex = _ex(_cfg(), adapter, InventoryRepository())
        await ex.on_create()
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()

        booked = await ex.reduce_position(lv.filled_base)

        assert booked > 0, "a sub-minimum exit was refused — the position strands"
        assert [o for o in adapter.placed if o.order_type is OrderType.LIMIT]

    asyncio.run(body())



def test_reduce_position_books_nothing_on_zero_fill():
    """Regression: a reduce-only MARKET that comes back UNFILLED (no liquidity /
    already flat / venue reject — adapter reconciles to filled_base=0) must book
    nothing. Booking the requested size would inject a phantom close at price 0
    into inventory and wrongly complete a level, desyncing from the venue."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105), auto_fill_market=False)
        inv = InventoryRepository()
        ex = _ex(_cfg(), adapter, inv)
        await ex.on_create()
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id, price=lv.open_price)  # this fill IS scripted
        await ex.on_tick()
        net_before = inv.get(1, PAIR, "c").net_amount_base
        assert net_before > 0
        cancelled_before = len(adapter.cancelled)

        # The reduce-only MARKET won't auto-fill -> nothing reduced.
        booked = await ex.reduce_position(lv.filled_base)

        assert booked == Decimal(0)
        assert inv.get(1, PAIR, "c").net_amount_base == net_before  # no phantom close
        assert len(adapter.cancelled) == cancelled_before           # no level advanced

    asyncio.run(body())


def test_keep_position_false_flattens_on_stop():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        inv = InventoryRepository()
        ex = _ex(_cfg(keep_position=False), adapter, inv)
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        await ex.on_tick()
        await ex.on_stop(CloseType.EARLY_STOP)
        assert ex.is_terminated
        assert inv.get(1, PAIR, "c").net_amount_base == Decimal(0)

    asyncio.run(body())


def test_keep_position_false_retries_when_stop_flatten_zero_fills():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105), auto_fill_market=False)
        inv = InventoryRepository()
        ex = _ex(_cfg(keep_position=False), adapter, inv)
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        await ex.on_tick()
        net_before = inv.get(1, PAIR, "c").net_amount_base
        assert net_before > 0

        await ex.on_stop(CloseType.EARLY_STOP)

        markets = [o for o in adapter.placed if o.order_type is OrderType.LIMIT]
        assert len(markets) == 1
        assert markets[0].filled_base == Decimal(0)
        assert not ex.is_terminated
        assert inv.get(1, PAIR, "c").net_amount_base == net_before

    asyncio.run(body())


def test_keep_position_true_retains_on_stop():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        inv = InventoryRepository()
        ex = _ex(_cfg(keep_position=True), adapter, inv)
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        await ex.on_tick()
        bought = inv.get(1, PAIR, "c").buy_amount_base
        await ex.on_stop(CloseType.EARLY_STOP)
        assert ex.is_terminated
        assert inv.get(1, PAIR, "c").net_amount_base == bought

    asyncio.run(body())


def test_adversarial_transient_errors_on_open_placement():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105), fail_on=["place_order"], fail_times=2)
        ex = _ex(_cfg(), adapter)
        await ex.on_create()
        # first level retried twice then placed; all levels eventually placed
        assert ex.retries == 2
        assert len(adapter.placed) == 5

    asyncio.run(body())


def test_a_PARTIAL_profit_tier_booking_does_not_destroy_the_levels_close_fill():
    """GRID-CLOSE-WATERMARK. ``_close_recorded`` had two incompatible meanings:
    ``_ingest`` assigns it ABSOLUTELY as the close ORDER's cumulative-fill
    watermark, while the booking path INCREMENTED it as a held-inventory counter.

    An external close (a profit tier, or the stop-out flatten) that books less
    than a level's ``filled_base`` leaves the level in CLOSE_ORDER_PLACED (the
    completion branch needs >= filled_base), so ``_process_level`` polls that
    close order again — and ``_ingest`` then
    subtracts the tier's base from the close order's real fills. The fill is
    under-counted or discarded entirely, its price is computed against an
    un-advanced quote watermark (so it reports a wildly wrong price), and the
    executor is left holding phantom inventory the venue does not have. That
    phantom net then permanently blocks D-Grid's next phase spawn
    (dynamic_grid._spawn_phase refuses on a non-flat net), i.e. D-Grid goes dark.
    """
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        inv = InventoryRepository()
        ex = _ex(_cfg(), adapter, inv)
        await ex.on_create()
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        held = lv.filled_base
        assert inv.get(1, PAIR, "c").net_amount_base == held

        # A tier books HALF the level — the normal case, since a slice is a
        # fraction (default 33%) of the whole position.
        half = held / 2
        booked = await ex.reduce_position(half)
        assert booked == half
        assert lv.state is GridLevelState.CLOSE_ORDER_PLACED, (
            "premise: a partial booking leaves the level's close leg live"
        )

        # The level's own close leg then fills the remaining half.
        adapter.fill_order(lv.close_order_id, amount=half)
        await ex.on_tick()

        assert inv.get(1, PAIR, "c").net_amount_base == Decimal(0), (
            "a real close fill was discarded — inventory holds phantom base the "
            "venue does not have, and D-Grid can never re-arm"
        )
        assert ex._net_base() == Decimal(0)
        assert lv.state is GridLevelState.COMPLETE

    asyncio.run(body())


def test_the_price_of_a_close_fill_after_a_partial_booking_is_real():
    """Same root cause, the attribution half: the booking path must not advance
    the QUOTE watermark either, or ``_ingest`` divides the close order's full
    quote by a shrunken base delta and bridges the fill at a price far from what
    the venue actually paid."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        rec = []

        class _Spy:
            def record(self, cid, pair, side, amount_base, price, fee_quote,
                       order_id=None, timestamp=None, *, realized_pnl=None,
                       is_taker=False):
                rec.append((side, Decimal(str(amount_base)), Decimal(str(price))))

            def link_placement(self, *a):
                pass

        ex = _ex(_cfg(), adapter, InventoryRepository())
        ex.trade_recorder = _Spy()
        await ex.on_create()
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        half = lv.filled_base / 2

        await ex.reduce_position(half)
        adapter.fill_order(lv.close_order_id, amount=half, price=lv.close_price)
        await ex.on_tick()

        closes = [r for r in rec if r[0] is TradeType.SELL]
        assert closes, "no close fill was recorded at all"
        last = closes[-1]
        assert abs(last[2] - lv.close_price) < Decimal("0.01"), (
            f"close fill bridged at {last[2]} but the venue paid {lv.close_price}"
        )

    asyncio.run(body())


def test_the_stop_out_flatten_attributes_across_levels_like_any_external_close():
    """The flatten closes inventory spread over SEVERAL levels, so it must use the
    same attribution + completion rule as a profit tier — it is an external close
    too. It used to inline its own copy of both, which only worked because
    cancel-all had already run: a latent dependency on call order, duplicated, on
    the path that closes a user's position."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        inv = InventoryRepository()
        ex = _ex(_cfg(keep_position=False), adapter, inv)
        await ex.on_create()
        held = ex.levels[:3]
        for lv in held:
            adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        assert inv.get(1, PAIR, "c").net_amount_base > 0

        await ex.on_stop(CloseType.EARLY_STOP)

        assert inv.get(1, PAIR, "c").net_amount_base == Decimal(0)
        assert ex._net_base() == Decimal(0)
        # Every level that held inventory is settled and carries no dangling close
        # leg — and the accounting lands in the EXTERNAL counter, never in the
        # close-order watermark that _ingest owns.
        for lv in held:
            assert lv.state is GridLevelState.COMPLETE, f"level {lv.index} unsettled"
            assert lv.close_order_id is None
            assert lv._close_recorded + lv._close_booked_base >= lv.filled_base
            assert lv._close_booked_base > 0, "flatten volume must be external"

    asyncio.run(body())


def test_completing_a_level_reaps_a_STILL_RESTING_entry_order():
    """GRID-COMPLETE-ORPHAN. A level can be completed while its OPEN order is still
    resting and only partly filled — an external close books the filled part, which
    is all the level holds. Clearing only the close leg left the entry live on the
    venue, and with recycle_levels (D-Grid's default) the recycled slot DISCARDS the
    id and places a second entry at the same price: two live orders on one rung,
    with the orphan invisible to _net_base, to _cancel_all_resting, and to the flat
    check that gates the next phase spawn."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        ex = _ex(_cfg(recycle_levels=True), adapter, InventoryRepository())
        await ex.on_create()
        lv = ex.levels[0]
        entry_id = lv.open_order_id
        adapter.fill_order(entry_id, amount=lv.amount_base / 2, price=lv.open_price)
        await ex.on_tick()
        assert lv.state is GridLevelState.OPEN_ORDER_PLACED, (
            "premise: a partial open fill leaves the entry resting"
        )

        await ex.reduce_position(lv.filled_base)      # a tier books the partial

        assert lv.state is GridLevelState.COMPLETE
        assert lv.open_order_id is None, "the completed level still tracks an entry"
        assert entry_id in adapter.cancelled, (
            "the entry order was orphaned — it is still live on the venue and the "
            "recycle will place a SECOND order on the same rung"
        )

    asyncio.run(body())


def test_a_partial_external_book_resizes_the_levels_close_leg():
    """GRID-CLOSE-OVERSIZED. reduce_only is an ACCOUNT-level flag, not per-level, so
    a close leg left sized for base its own level no longer holds fills in full and
    closes ANOTHER rung's base at this rung's price."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        ex = _ex(_cfg(), adapter, InventoryRepository())
        await ex.on_create()
        for lv in ex.levels[:2]:
            adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        target = ex.levels[0]
        assert target.state is GridLevelState.CLOSE_ORDER_PLACED
        original = next(o for o in adapter.placed if o.id == target.close_order_id)
        assert original.amount_base == target.filled_base

        # Book HALF of that level externally.
        await ex.reduce_position(target.filled_base / 2)

        assert target.state is GridLevelState.CLOSE_ORDER_PLACED
        assert target.close_order_id != original.id, "the oversized leg still rests"
        assert original.id in adapter.cancelled
        resized = next(o for o in adapter.placed if o.id == target.close_order_id)
        expected = (target.filled_base - target._close_recorded
                    - target._close_booked_base)
        assert resized.amount_base == expected, (
            f"close leg rests {resized.amount_base} against {expected} held"
        )
        assert resized.order_type is OrderType.LIMIT_MAKER, "still a maker leg"

    asyncio.run(body())


def test_a_failing_profit_tier_does_not_kill_the_ladder():
    """A profit tier is DISCRETIONARY. Routing it through _guard meant three adapter
    errors terminated the whole executor FAILED — and _terminate cancels nothing, so
    every resting order was stranded on the venue with nobody driving them, the
    position unmanaged and D-Grid dark (its flat check refuses to spawn)."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        ex = _ex(_cfg(), adapter, InventoryRepository())
        await ex.on_create()
        lv = ex.levels[0]
        adapter.fill_order(lv.open_order_id, price=lv.open_price)
        await ex.on_tick()
        resting_before = [l.close_order_id for l in ex.levels if l.close_order_id]

        adapter.fail_on = {"place_order"}
        adapter.fail_remaining = 99
        booked = await ex.reduce_position(lv.filled_base)

        assert booked == Decimal(0)
        assert not ex.is_terminated, "a failed profit tier killed the ladder"
        assert [l.close_order_id for l in ex.levels if l.close_order_id] == resting_before

    asyncio.run(body())
