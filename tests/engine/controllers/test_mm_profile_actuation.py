"""Mid Mode v3 Phase 5 — the first phase that actuates.

This is the blast-radius test. ``MarketMakingController`` is the BASE CLASS of
``FillAnchoredQuotingController`` (Grid maker) and ``RGridController``, so the
first requirement is that everything here stays off unless the ``mid`` mapping
switches it on. The rest pins the two behaviours that touch money:

* the SPREAD profile refuses to quote inside the fee, whatever the user set;
* the reservation shift works inventory off without ever crossing the sides.
"""
import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.adapter.base import OrderBookLevel, OrderBookSnapshot
from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import TradeType
from src.nadobro.quant import mm_profile as mp

BASE = {
    "trading_pair": "P",
    "spread_bid_pct": "0.01",
    "spread_ask_pct": "0.01",
    "order_amount_quote": "100",
}


class _DepthAdapter(MockNadoAdapter):
    def __init__(self, *a, bids=((99.99, 50.0),), asks=((100.01, 50.0),), **kw):
        super().__init__(*a, **kw)
        self._bids, self._asks = bids, asks
        self.depth_calls = 0

    async def depth_book(self, trading_pair, depth: int = 10) -> OrderBookSnapshot:
        self.depth_calls += 1
        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=[OrderBookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in self._bids],
            asks=[OrderBookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in self._asks],
            timestamp=0.0,
        )


def _run(adapter, configs, *, ticks=1, before_tick=None):
    async def body():
        orch = ExecutorOrchestrator()
        c = MarketMakingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=configs, controller_id="mm-prof",
        )
        await orch.spawn_controller(c)
        for _ in range(ticks):
            if before_tick is not None:
                before_tick(c)
            await orch.tick_controller(c.id)
        return c, adapter
    return asyncio.run(body())


# --- the blast radius -------------------------------------------------------

def test_everything_is_off_by_default_so_grid_and_rgrid_are_untouched():
    adapter = _DepthAdapter(mid=Decimal(100))
    c, adapter = _run(adapter, dict(BASE))
    assert c.profile_enabled is False
    assert c.profile == ""
    assert c.inventory_skew_enabled is False
    assert c.reservation_offset_bp == Decimal(0)
    assert adapter.depth_calls == 0          # no extra query either


def test_the_disabled_path_places_exactly_the_prices_it_always_did():
    off = _DepthAdapter(mid=Decimal(100))
    _run(off, dict(BASE))
    # Same config, profile machinery present but not enabled.
    also_off = _DepthAdapter(mid=Decimal(100))
    _run(also_off, {**BASE, "mid_objective": "spread"})   # objective without the flag
    assert sorted(o.price for o in off.placed) == sorted(o.price for o in also_off.placed)


def test_only_the_mid_mapping_emits_the_phase_5_keys():
    from decimal import Decimal as D

    from src.nadobro.strategy.engine_runtime import map_strategy_config

    conf = {"notional_usd": 100.0, "spread_bp": 5.0, "levels": 2}
    mid_cfg = map_strategy_config("mid", dict(conf), D(100), product="BTC-PERP")
    assert mid_cfg["profile_enabled"] == D(1)
    assert mid_cfg["mid_objective"] == "auto"
    assert mid_cfg["inventory_skew_enabled"] == D(1)

    for strategy in ("grid", "rgrid", "dgrid"):
        cfg = map_strategy_config(strategy, dict(conf), D(100), product="BTC-PERP")
        for key in ("profile_enabled", "mid_objective", "inventory_skew_enabled",
                    "fee_round_trip_bp", "min_edge_bp", "inventory_skew_gamma"):
            assert key not in cfg, f"{strategy} leaked {key}"


# --- profile resolution -----------------------------------------------------

def test_a_one_tick_book_selects_volume_and_leaves_the_floor_alone():
    # ~2bp spread on a 5bp round trip: no edge to capture.
    adapter = _DepthAdapter(mid=Decimal(100), bids=((99.99, 50.0),), asks=((100.01, 50.0),))
    c, _ = _run(adapter, {**BASE, "profile_enabled": "1"})
    assert c.profile == mp.VOLUME
    assert c.spread_floor_half_pct == Decimal("0.00015")   # shipped default


def test_a_wide_book_selects_spread_and_raises_the_floor_above_the_fee():
    adapter = _DepthAdapter(mid=Decimal(100), bids=((99.75, 50.0),), asks=((100.25, 50.0),))
    c, _ = _run(adapter, {**BASE, "profile_enabled": "1"})
    assert c.profile == mp.SPREAD
    floor_bp = float(c.spread_floor_half_pct) * 10_000.0
    assert floor_bp > mp.per_leg_fee_bp(None)              # the whole point


def test_the_spread_profile_overrides_a_user_spread_inside_the_fee():
    # A 0.5bp half-spread loses money on every completed round trip. The user
    # can ask for it; the SPREAD profile refuses to place it.
    adapter = _DepthAdapter(mid=Decimal(100), bids=((99.75, 50.0),), asks=((100.25, 50.0),))
    c, adapter = _run(adapter, {
        **BASE, "profile_enabled": "1", "mid_objective": "spread",
        "spread_bid_pct": "0.00005", "spread_ask_pct": "0.00005",
    })
    floor = c.spread_floor_half_pct
    assert c.spread_bid_pct >= floor and c.spread_ask_pct >= floor
    bid = min(o.price for o in adapter.placed)
    ask = max(o.price for o in adapter.placed)
    assert (Decimal(100) - bid) / Decimal(100) >= floor
    assert (ask - Decimal(100)) / Decimal(100) >= floor


def test_the_volume_profile_may_still_quote_inside_the_fee():
    # Not an oversight — buying fill rate with per-fill edge is the profile's
    # purpose, and the session SL rail is what bounds it.
    adapter = _DepthAdapter(mid=Decimal(100))
    c, _ = _run(adapter, {
        **BASE, "profile_enabled": "1", "mid_objective": "volume",
        "spread_bid_pct": "0.00005", "spread_ask_pct": "0.00005",
    })
    assert c.profile == mp.VOLUME
    assert c.spread_floor_half_pct == Decimal("0.00015")


def test_the_profile_is_resolved_once_not_per_tick():
    # The two profiles imply different requote behaviour; flipping mid-session
    # would churn the book instead of running either playbook.
    adapter = _DepthAdapter(mid=Decimal(100))
    c, adapter = _run(adapter, {**BASE, "profile_enabled": "1"}, ticks=4)
    assert c._profile_resolved is True
    assert adapter.depth_calls == 1


def test_an_explicit_objective_resolves_even_when_the_book_is_unreadable():
    adapter = MockNadoAdapter(mid=Decimal(100))     # no depth_book support
    c, _ = _run(adapter, {**BASE, "profile_enabled": "1", "mid_objective": "spread"})
    assert c.profile == mp.SPREAD


def test_auto_waits_for_a_readable_book_rather_than_guessing():
    adapter = MockNadoAdapter(mid=Decimal(100))     # depth raises
    c, adapter = _run(adapter, {**BASE, "profile_enabled": "1"}, ticks=2)
    assert c.profile == ""                          # still undecided
    assert c._profile_resolved is False
    assert len(adapter.placed) == 2                 # and it kept quoting


# --- the reservation shift --------------------------------------------------

def _skew_cfg(**kw):
    return {**BASE, "profile_enabled": "1", "mid_objective": "volume",
            "inventory_skew_enabled": "1", "max_base_quote": "100", **kw}


def test_long_inventory_moves_both_quotes_down():
    flat = _DepthAdapter(mid=Decimal(100))
    _run(flat, _skew_cfg())
    long_book = _DepthAdapter(mid=Decimal(100))

    def _load(c):
        # $80 long against a $100 ceiling => inventory_ratio 0.8.
        c.inventory.apply_fill(c.user_id, "P", c.id,
                               TradeType.BUY, Decimal("0.8"), Decimal("80"))

    c, long_book = _run(long_book, _skew_cfg(), before_tick=_load)
    assert c.reservation_offset_bp < 0
    assert max(long_book.placed, key=lambda o: o.price).price < max(
        flat.placed, key=lambda o: o.price).price
    assert min(long_book.placed, key=lambda o: o.price).price < min(
        flat.placed, key=lambda o: o.price).price


def test_the_shift_never_crosses_the_bid_over_the_ask():
    adapter = _DepthAdapter(mid=Decimal(100))

    def _load(c):
        # Absurdly oversized inventory and risk aversion: the bound must hold.
        c.inventory.apply_fill(c.user_id, "P", c.id,
                               TradeType.BUY, Decimal("50"), Decimal("5000"))

    c, adapter = _run(adapter, _skew_cfg(inventory_skew_gamma="99"), before_tick=_load)
    bids = [o.price for o in adapter.placed if o.side.name == "BUY"]
    asks = [o.price for o in adapter.placed if o.side.name == "SELL"]
    if bids and asks:
        assert max(bids) < min(asks)


def test_a_flat_book_is_quoted_symmetrically_around_mid():
    adapter = _DepthAdapter(mid=Decimal(100))
    c, adapter = _run(adapter, _skew_cfg())
    assert c.reservation_offset_bp == Decimal(0)
    bid = min(o.price for o in adapter.placed)
    ask = max(o.price for o in adapter.placed)
    assert (Decimal(100) - bid) == (ask - Decimal(100))


def test_the_shift_is_reported_for_the_dashboard():
    adapter = _DepthAdapter(mid=Decimal(100), bids=((99.75, 50.0),), asks=((100.25, 50.0),))
    c, _ = _run(adapter, {**BASE, "profile_enabled": "1"})
    m = c.ladder_metrics()
    assert m["profile"] == mp.SPREAD
    assert m["half_spread_floor_bp"] > 0
    assert "reservation_offset_bp" in m
