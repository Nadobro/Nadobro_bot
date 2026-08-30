"""Mid level recycling ("fill the gaps") — the opt-in, default-OFF behavior that
pins the quoting anchor to a slowly-drifting reference so a filled level re-arms at
~the same price (recycling round-tripped levels to catch reversals), bounded by a
band floor below which new buys are suppressed.

Pins:
1. OFF by default → the anchor state is never created and Mid quotes around the
   live mid exactly as before (no regression for existing Mid / Grid / R-Grid).
2. drift mode → the anchor is a slow EMA of the mid (lags it), so recycling levels
   migrate slowly instead of chasing every tick.
3. static mode → the anchor is frozen at the session's first mid.
4. A filled level re-arms near the ANCHOR (recycle), not near the moved mid.
5. The band floor suppresses NEW buys deeper than anchor*(1 - floor_pct).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import TradeType

PAIR = "P"
BASE = {
    "trading_pair": PAIR,
    "spread_bid_pct": "0.01",
    "spread_ask_pct": "0.01",
    "order_amount_quote": "100",
    "price_distance_tolerance": "0.001",
    "max_base_quote": "100000",   # inventory ceiling well clear so buys stay allowed
}


def _mm(adapter, configs, *, inv=None):
    orch = ExecutorOrchestrator()
    c = MarketMakingController(
        user_id=1, orchestrator=orch, adapter=adapter,
        inventory=inv or InventoryRepository(), configs=configs, controller_id="MM",
    )
    return orch, c


def _live_bids(c):
    return sorted(
        slot.price for (is_bid, _lvl), slot in c._slots.items()
        if is_bid and slot.ex_id is not None and slot.price is not None
    )


def _live_asks(c):
    return sorted(
        slot.price for (is_bid, _lvl), slot in c._slots.items()
        if (not is_bid) and slot.ex_id is not None and slot.price is not None
    )


# ── 1. OFF by default: no anchor, quotes follow the mid ─────────────────────
def test_recycle_off_by_default_keeps_mid_following_quotes():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, dict(BASE))
        assert c.recycle_enabled is False
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._recycle_anchor is None and c._recycle_floor_price is None
        assert _live_bids(c) == [Decimal(99)] and _live_asks(c) == [Decimal(101)]
        # Mid moves up → the bid FOLLOWS it (the behavior recycling changes).
        adapter.set_mid(Decimal(110))
        await orch.tick_controller(c.id)
        assert _live_bids(c) == [Decimal("108.9")]   # 110*(1-0.01), chased the mid

    asyncio.run(body())


# ── 2. drift: the anchor is a slow EMA of the mid (lags it) ─────────────────
def test_drift_anchor_lags_the_mid():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="drift",
                   mid_recycle_drift_alpha="0.5")
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._recycle_anchor == Decimal(100)      # first tick anchors at the mid
        adapter.set_mid(Decimal(110))
        await orch.tick_controller(c.id)
        assert c._recycle_anchor == Decimal(105)      # 100 + 0.5*(110-100), lags
        await orch.tick_controller(c.id)
        assert c._recycle_anchor == Decimal("107.5")  # 105 + 0.5*(110-105), still lagging

    asyncio.run(body())


# ── 3. static: the anchor is frozen at the session's first mid ──────────────
def test_static_anchor_is_frozen():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="static")
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._recycle_anchor == Decimal(100)
        adapter.set_mid(Decimal(130))
        await orch.tick_controller(c.id)
        assert c._recycle_anchor == Decimal(100)      # frozen, ignores the moved mid

    asyncio.run(body())


# ── 4. a filled level re-arms near the ANCHOR (recycle), not the moved mid ──
def test_filled_bid_recycles_near_the_anchor_not_the_mid():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="drift",
                   mid_recycle_drift_alpha="0.02")   # slow drift
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False,
                                  venue_held={PAIR: Decimal(0)})
        orch, c = _mm(adapter, cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert _live_bids(c) == [Decimal(99)]         # anchor 100, bid at 99

        # Fill the resting bid → the controller is now long.
        bid_order = [o for o in adapter.placed if o.side is TradeType.BUY][-1]
        adapter.fill_order(bid_order.id)

        # Price rips to 105. A mid-following MM would re-quote the bid near 104;
        # recycling re-arms it near the (barely-drifted) anchor instead.
        adapter.set_mid(Decimal(105))
        await orch.tick_controller(c.id)
        anchor = c._recycle_anchor
        assert anchor == Decimal(100) + Decimal("0.02") * Decimal(5)   # 100.10
        (bid,) = _live_bids(c)
        assert bid == anchor * (Decimal(1) - Decimal("0.01"))          # ~99.099, recycled
        assert bid < Decimal(100), "recycled bid must sit near the old level, not chase the mid"

    asyncio.run(body())


# ── 5. the band floor suppresses NEW buys deeper than anchor*(1 - floor) ────
def test_floor_suppresses_buys_below_the_band():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="static",
                   mid_recycle_floor_pct="0.02",      # 2% band
                   ladder_levels=4, ladder_step_bp="100")   # 4 levels, 1% apart
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        floor = c._recycle_floor_price
        assert floor == Decimal(100) * (Decimal(1) - Decimal("0.02"))   # 98
        bids = _live_bids(c)
        # Deep buy levels below the 98 floor are suppressed; the near ones survive.
        assert bids, "at least the near levels must still quote"
        assert min(bids) >= floor, f"no resting buy may sit below the floor {floor}: {bids}"
        assert len(bids) < 4, "the deepest levels (below the floor) must be suppressed"
        # Asks are never floored — the full sell ladder stays.
        assert len(_live_asks(c)) == 4

    asyncio.run(body())


# ── 5b. alpha=0 freezes the anchor (a static anchor via the drift knob) ─────
def test_drift_alpha_zero_freezes_the_anchor():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="drift",
                   mid_recycle_drift_alpha="0")   # 0 must be HONORED, not coerced
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, cfg)
        assert c.recycle_drift_alpha == Decimal(0)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._recycle_anchor == Decimal(100)
        adapter.set_mid(Decimal(130))
        await orch.tick_controller(c.id)
        assert c._recycle_anchor == Decimal(100)   # frozen despite drift mode

    asyncio.run(body())


# ── 5c. degenerate floor_pct disables the floor (never "suppress every buy") ─
def test_floor_pct_zero_disables_the_floor_instead_of_suppressing_all_buys():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="static",
                   mid_recycle_floor_pct="0",        # degenerate: must DISABLE, not suppress-all
                   ladder_levels=4, ladder_step_bp="100")
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._recycle_floor_price is None
        assert len(_live_bids(c)) == 4, "floor off → the whole buy ladder must quote"

    asyncio.run(body())


def test_floor_pct_ge_one_disables_the_floor():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="static",
                   mid_recycle_floor_pct="1.5",      # >=100%: disabled, not a live no-op
                   ladder_levels=4, ladder_step_bp="100")
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._recycle_floor_price is None
        assert len(_live_bids(c)) == 4

    asyncio.run(body())


# ── 5d. the floor exempts a REDUCING cover-bid (never blocks an exit) ────────
def test_floor_exempts_a_reducing_cover_bid_when_short():
    async def body():
        cfg = dict(BASE)
        cfg.update(mid_recycle_enabled=1, mid_recycle_anchor_mode="static",
                   mid_recycle_floor_pct="0.02", ladder_levels=4, ladder_step_bp="100")
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, cfg)
        c._base_value = lambda _mid: Decimal(-500)   # held SHORT — buys REDUCE it
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        floor = c._recycle_floor_price
        assert floor == Decimal(98)
        bids = _live_bids(c)
        # A short's cover-bids below the 2% floor are profit-taking exits, not fresh
        # long risk — the floor must NOT suppress them (reduce-only exemption).
        assert min(bids) < floor, "a reducing cover-bid below the floor must be allowed"
        assert len(bids) == 4

    asyncio.run(body())


# ── 6. the shared base class is untouched: inheritors never build the anchor ─
def test_inherited_controllers_are_unaffected():
    # FillAnchoredQuotingController / RGridController never set the recycle keys,
    # so recycle stays OFF and the floor is inert — a direct construction check.
    adapter = MockNadoAdapter(mid=Decimal(100))
    _, c = _mm(adapter, dict(BASE))
    assert c.recycle_enabled is False
    assert c._recycle_anchor is None
    assert c._recycle_floor_price is None
    assert c.recycle_floor_pct == Decimal("0.02")   # parsed default, but inert while OFF
