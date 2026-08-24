"""Mid Mode v3 Phase 8 — the fused atomic requote (cancel_and_place).

The base-class rule still governs: MarketMakingController is the parent of
FillAnchored and RGrid, so the fused path is OFF by default and the disabled
path must place byte-identical orders.

Beyond that, the properties that carry money:

* when enabled, a requote uses ONE atomic cancel_and_place — no separate
  stop() cancel and no separate place();
* a fused replace that FAILS falls back to the classic stop-then-spawn and
  NEVER produces two live orders (atomic failure = old untouched);
* the slot ends pointing at exactly one live executor either way.
"""
import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator

BASE = {
    "trading_pair": "P",
    "spread_bid_pct": "0.01",
    "spread_ask_pct": "0.01",
    "order_amount_quote": "100",
    # A generous tolerance would let _should_hold keep the quote; keep it tight
    # so a mid move forces a genuine requote.
    "price_distance_tolerance": "0.0001",
    "min_quote_lifetime_s": "0",
}


def _controller(adapter, configs):
    async def build():
        orch = ExecutorOrchestrator()
        c = MarketMakingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=configs, controller_id="mm-cap",
        )
        await orch.spawn_controller(c)
        return orch, c
    return asyncio.run(build())


def _run(adapter, configs, *, mids):
    """Tick once per mid in ``mids`` so the second tick requotes off a moved mid."""
    async def body():
        orch = ExecutorOrchestrator()
        c = MarketMakingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=configs, controller_id="mm-cap",
        )
        await orch.spawn_controller(c)
        for m in mids:
            adapter.set_mid(Decimal(str(m)))
            await orch.tick_controller(c.id)
        return orch, c
    return asyncio.run(body())


def _live_orders(adapter):
    from src.nadobro.engine.adapter.base import OrderState
    return [o for o in adapter._orders.values() if o.state not in (
        OrderState.CANCELLED, OrderState.REJECTED, OrderState.FILLED)]


# --- the blast radius -------------------------------------------------------

def test_off_by_default():
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    _orch, c = _controller(adapter, dict(BASE))
    assert c.cancel_and_place_enabled is False


def test_disabled_requote_uses_classic_stop_then_spawn():
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    _run(adapter, dict(BASE), mids=[100, 100.5])
    # Classic path: a real cancel happened and NO fused replace was recorded.
    assert adapter.cancelled                 # old orders cancelled the classic way
    assert adapter.replaced == []            # fused path never taken


def test_only_the_mid_mapping_emits_the_flag():
    from decimal import Decimal as D

    from src.nadobro.strategy.engine_runtime import map_strategy_config

    conf = {"notional_usd": 100.0, "spread_bp": 5.0, "levels": 1}
    mid_cfg = map_strategy_config("mid", dict(conf), D(100), product="BTC-PERP")
    assert mid_cfg["cancel_and_place_enabled"] == D(1)
    for strategy in ("grid", "rgrid", "dgrid"):
        cfg = map_strategy_config(strategy, dict(conf), D(100), product="BTC-PERP")
        assert "cancel_and_place_enabled" not in cfg, strategy


# --- the fused path ---------------------------------------------------------

def _enabled():
    return {**BASE, "cancel_and_place_enabled": "1"}


def test_a_requote_uses_one_atomic_cancel_and_place():
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    _orch, c = _run(adapter, _enabled(), mids=[100, 100.5])
    # Both sides requoted via the fused path...
    assert len(adapter.replaced) == 2
    # ...and the old digests were dropped via forget_cancelled, not re-cancelled
    # through the classic cancel_order path.
    assert adapter.cancelled == []           # no separate cancel_order calls
    assert len(adapter.forgotten) == 2
    assert c.ladder_metrics()["atomic_replaces"] == 2


def test_the_fused_requote_never_leaves_two_live_orders_per_side():
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    _run(adapter, _enabled(), mids=[100, 100.5, 101.0])
    # One live order per side at the end — the replace is 1-for-1.
    assert len(_live_orders(adapter)) == 2


def test_the_slot_points_at_the_new_executor_after_a_fused_requote():
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    orch, c = _run(adapter, _enabled(), mids=[100, 100.5])
    for (_is_bid, _lvl), slot in c._slots.items():
        if slot.ex_id is not None:
            ex = orch.get(slot.ex_id)
            assert ex is not None and not ex.is_terminated


# --- the fallback -----------------------------------------------------------

def test_a_failed_fused_replace_falls_back_to_classic_and_makes_no_double_order():
    # cancel_and_place raises atomically (old untouched); the controller must
    # fall back to stop-then-spawn and end with exactly one live order per side.
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False,
                              fail_on=["cancel_and_place"], fail_times=99)
    orch, c = _run(adapter, _enabled(), mids=[100, 100.5])
    assert adapter.replaced == []            # every fused attempt failed
    assert adapter.cancelled                 # fell back to the classic cancel
    assert len(_live_orders(adapter)) == 2   # NO double order


def test_the_fallback_still_requotes_at_the_new_price():
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False,
                              fail_on=["cancel_and_place"], fail_times=99)
    orch, c = _run(adapter, _enabled(), mids=[100, 100.5])
    # New bid target ~ 100.5 * (1 - 0.01) = 99.495; the live bid should be near
    # it, i.e. the requote actually happened via the fallback.
    live = _live_orders(adapter)
    bids = [o for o in live if o.side.name == "BUY"]
    assert bids and abs(float(bids[0].price) - 99.495) < 0.05


# --- queue hold still wins --------------------------------------------------

def test_a_held_quote_is_never_replaced():
    # _should_hold short-circuits before the fused path; a quote inside
    # tolerance must not be atomically replaced (a replace resets queue slot).
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    orch, c = _run(adapter, {**_enabled(), "price_distance_tolerance": "0.5"},
                   mids=[100, 100.001])
    assert adapter.replaced == []            # nothing requoted; the quote held
    assert adapter.cancelled == []


# --- non-atomic venue (audit finding 2) -------------------------------------

def test_a_non_atomic_venue_that_leaves_the_old_order_resting_is_cleaned_up():
    # If cancel_and_place places the NEW order but fails to cancel the OLD one,
    # the settle path must detect the still-resting old order and cancel it, so
    # the venue never ends with two live orders on a side.
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    adapter.cap_leaves_old_resting = True
    _run(adapter, _enabled(), mids=[100, 100.5])
    # The old orders were rescued by an explicit fallback cancel...
    assert adapter.cancelled          # settle issued the fallback cancel_order
    # ...leaving exactly one live order per side, not two.
    assert len(_live_orders(adapter)) == 2


# --- leverage consistency (audit finding 1) ---------------------------------

def test_the_fused_path_signs_the_configured_leverage():
    adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    _run(adapter, {**_enabled(), "leverage": "5"}, mids=[100, 100.5])
    # Every fused replace signed the configured leverage, not a bare default.
    assert adapter.cap_leverages
    assert all(lev == 5 for lev in adapter.cap_leverages)


def test_the_classic_path_signs_the_same_leverage_as_the_fused_path():
    # Audit finding 1: the two paths must agree, or the same logical quote rests
    # at a different isolated margin / liquidation distance depending on which
    # placed it.
    classic = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    _run(classic, {**BASE, "leverage": "5"}, mids=[100, 100.5])   # fused OFF
    fused = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
    _run(fused, {**_enabled(), "leverage": "5"}, mids=[100, 100.5])   # fused ON
    assert set(classic.place_leverages) == {5}
    # The fused run's initial placements also sign 5, and its replaces sign 5.
    assert set(fused.place_leverages) == {5}
    assert set(fused.cap_leverages) == {5}
