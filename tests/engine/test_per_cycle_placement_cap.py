"""Guardrails for the per-cycle order-placement cap (all strategies).

The cap bounds how many EXPOSURE-GROWING / requoting venue writes a single
strategy session may issue in one engine cycle, so a deep-ladder re-quote burst
cannot flood the rate-limited venue and starve the single-GIL event loop — the
failure that hung the whole bot when a 15-level Mid book churned hundreds of
orders in one 180s cycle.

The invariants pinned here (each maps to a "make no mistake" constraint):

  1. RESET/COUNT — the counter lives on the reused adapter and zeroes per cycle.
  2. EXEMPT RULE — only openings count; a reduce/close (incl. the SPOT case where
     reduce_only is stripped to False but never_grow survives) is NEVER counted,
     so the book can always trim/exit even when the opening budget is spent.
  3. SIZE PRESERVED — a capped cycle withholds WHOLE placements; it never resizes
     the orders that do go out (the #268 depth-cap incident broke exactly this).
  4. RESUMABLE — a placement deferred by the cap is re-issued on the next cycle
     (grid rungs, revgrid ladder), never dropped and never double-placed.
  5. NEVER SKIP THE REDUCING SIDE — Mid defers only the side that grows the
     position; the inventory-trimming side is never gated.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.nadobro.engine.adapter import base as adapter_base
from src.nadobro.engine.adapter.nado import NadoAdapter, ProductMeta
from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController
from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.controllers.reverse_grid import ReverseGridController
from src.nadobro.engine.executors.grid_executor import (
    GridExecutor,
    GridExecutorConfig,
    GridLevelState,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import OrderType, TradeType
from tests.engine._mock_nado import MockNadoAdapter


# --------------------------------------------------------------------------
# A faithful counting double: mirrors the LIVE adapter's rule that only
# exposure-growing placements consume budget. Isolated to this file so the
# shared MockNadoAdapter (used by ~15 suites) is untouched.
# --------------------------------------------------------------------------
class _CountingAdapter(MockNadoAdapter):
    async def place_order(self, *args, **kwargs):
        order = await super().place_order(*args, **kwargs)
        # reduce_only is the 7th positional arg (index 6) or a kwarg; default False.
        # Callers pass it positionally (grid) or by keyword — handle both.
        reduce_only = kwargs.get("reduce_only", args[6] if len(args) > 6 else False)
        if not reduce_only:                       # openings only, like nado.py
            self._note_opening_placement()
        return order

    async def place_trigger_order(self, *args, **kwargs):
        order = await super().place_trigger_order(*args, **kwargs)
        self._note_opening_placement()            # a trigger rung always OPENS
        return order


@pytest.fixture(autouse=True)
def _restore_cap():
    """Every test sets its own cap; restore the module default afterwards."""
    original = adapter_base._OPENING_CAP_PER_CYCLE
    yield
    adapter_base._OPENING_CAP_PER_CYCLE = original


def _set_cap(n: int) -> None:
    adapter_base._OPENING_CAP_PER_CYCLE = n


# --------------------------------------------------------------------------
# 1. RESET / COUNT mechanics (the shared base methods)
# --------------------------------------------------------------------------
def test_counter_resets_each_cycle_and_respects_cap():
    a = MockNadoAdapter()
    _set_cap(3)
    a.begin_cycle()
    assert a.opening_placements_this_cycle() == 0
    assert a.opening_budget_exhausted() is False

    a._note_opening_placement()
    a._note_opening_placement()
    assert a.opening_placements_this_cycle() == 2
    assert a.opening_budget_exhausted() is False   # 2 < 3

    a._note_opening_placement()
    assert a.opening_budget_exhausted() is True     # 3 >= 3

    a.begin_cycle()                                  # next cycle
    assert a.opening_placements_this_cycle() == 0
    assert a.opening_budget_exhausted() is False


def test_cap_zero_or_negative_disables_the_bound():
    a = MockNadoAdapter()
    for _ in range(1000):
        a._note_opening_placement()
    _set_cap(0)
    assert a.opening_budget_exhausted() is False
    _set_cap(-5)
    assert a.opening_budget_exhausted() is False


# --------------------------------------------------------------------------
# 2. EXEMPT RULE — live adapter counts openings, never reduces (spot-trap incl.)
# --------------------------------------------------------------------------
class _FakeClient:
    def place_limit_order(self, product_id, size, price, is_buy=True,
                          post_only=False, reduce_only=False, **kwargs):
        return {"digest": f"d{price}-{is_buy}", "status": "open"}

    def place_market_order(self, product_id, size, is_buy=True, reduce_only=False, **kwargs):
        return {"digest": "m1", "status": "filled", "price": 100,
                "filled_base": str(size), "filled_quote": str(size * 100)}

    def get_market_price(self, product_id):
        return {"bid": 99.0, "ask": 101.0}


_PERP = {"AAA-PERP": ProductMeta(product_id=9, tick_size=Decimal("0.01"),
                                 lot_size=Decimal("0.001"), min_notional=Decimal(1),
                                 is_perp=True)}
_SPOT = {"KBTC-USDC": ProductMeta(product_id=2, tick_size=Decimal("0.01"),
                                  lot_size=Decimal("0.001"), min_notional=Decimal(1))}


def test_live_adapter_counts_openings_not_reduces():
    async def body():
        _set_cap(100)
        # PERP opening (reduce_only=False) -> counted.
        a = NadoAdapter(_FakeClient(), _PERP)
        a.begin_cycle()
        await a.place_order("AAA-PERP", TradeType.BUY, OrderType.LIMIT_MAKER,
                            Decimal("0.01"), price=Decimal("100"), reduce_only=False)
        assert a.opening_placements_this_cycle() == 1

        # PERP reduce (reduce_only=True) -> NOT counted (the exit path is exempt).
        await a.place_order("AAA-PERP", TradeType.SELL, OrderType.LIMIT_MAKER,
                            Decimal("0.01"), price=Decimal("101"), reduce_only=True)
        assert a.opening_placements_this_cycle() == 1

    asyncio.run(body())


def test_exhausted_budget_never_blocks_a_reduce_only_close():
    """The kill path (stop button / /stop_all) must ALWAYS get through. Its flatten
    (close_all_positions) uses the RAW NadoClient and bypasses this adapter entirely,
    and its order-cancels are not placements — so the cap can never see them. Even so,
    pin the adapter-level guarantee: a reduce-only close routed THROUGH place_order is
    never gated (the adapter has no gate at all — gates live only in the three opening
    loops) and never counted, even at a fully spent budget."""
    async def body():
        _set_cap(1)
        a = NadoAdapter(_FakeClient(), _PERP)
        a.begin_cycle()
        a._note_opening_placement()                 # opening budget now EXHAUSTED
        assert a.opening_budget_exhausted() is True
        # A reduce-only close still reaches the venue (returns an order) and does
        # NOT consume budget — the book can always be flattened.
        order = await a.place_order("AAA-PERP", TradeType.SELL, OrderType.MARKET,
                                    Decimal("0.01"), reduce_only=True)
        assert order is not None
        assert a.opening_placements_this_cycle() == 1   # unchanged: reduce not counted

    asyncio.run(body())


def test_live_adapter_spot_reduce_is_exempt_despite_reduce_only_strip():
    """The SPOT trap: nado.py strips reduce_only->False for spot (venue rejects
    it), but never_grow captures the reducing intent BEFORE the strip. Counting
    must key off never_grow, or a spot CLOSE would be miscounted as an opening and
    could be skipped by the cap — the one thing that must never happen."""
    async def body():
        _set_cap(100)
        a = NadoAdapter(_FakeClient(), _SPOT)
        a.begin_cycle()
        # A reduce-only MARKET sell on a SPOT product: reduce_only is stripped to
        # False mid-method, but the placement must still be treated as reducing.
        await a.place_order("KBTC-USDC", TradeType.SELL, OrderType.MARKET,
                            Decimal("0.01"), reduce_only=True)
        assert a.opening_placements_this_cycle() == 0

    asyncio.run(body())


# --------------------------------------------------------------------------
# 3 + 4. GRID — exact-cap defer, resume next cycle, size preserved
# --------------------------------------------------------------------------
def _grid_cfg() -> GridExecutorConfig:
    # 8 rungs, sized 80/8 = 10 each. Batch cap (default 10) >= levels, so only the
    # per-cycle cap can bind. No activation bounds and mid inside the band, so every
    # level is placeable.
    return GridExecutorConfig(
        trading_pair="BTC-PERP", side=TradeType.BUY,
        start_price=Decimal("99"), end_price=Decimal("100"), limit_price=Decimal(0),
        total_amount_quote=Decimal(80), min_spread_between_orders=Decimal("0.002"),
        max_open_orders=8,
    )


def _new_grid(adapter):
    orch = ExecutorOrchestrator()
    ex = GridExecutor(_grid_cfg(), user_id=1, controller_id="G", adapter=adapter,
                      inventory=InventoryRepository())
    return orch, ex


def test_grid_defers_opens_past_cap_then_resumes_same_sizes():
    async def body():
        # Uncapped reference: how many opens, and at what sizes?
        _set_cap(0)
        ref_adapter = _CountingAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        ref_orch, ref_ex = _new_grid(ref_adapter)
        ref_adapter.begin_cycle()
        await ref_orch.spawn(ref_ex)              # on_create places the opens
        ref_orders = list(ref_adapter.placed)
        ref_sizes = sorted(str(o.amount_base) for o in ref_orders)
        ref_count = len(ref_orders)
        assert ref_count > 3                      # a real multi-rung ladder > the cap

        # Capped run: 3 opens/cycle. The ladder must deploy over several cycles,
        # never place more than the cap in one cycle, and use the SAME per-order
        # sizes as the uncapped run (no re-division of notional — the #268 lesson).
        _set_cap(3)
        adapter = _CountingAdapter(mid=Decimal("99.5"), auto_fill_market=False)
        orch, ex = _new_grid(adapter)
        per_cycle = []
        seen = 0

        adapter.begin_cycle()
        await orch.spawn(ex)                       # cycle 1 (on_create)
        per_cycle.append(len(adapter.placed) - seen)
        seen = len(adapter.placed)
        for _ in range(9):                         # subsequent cycles (ticks)
            if seen >= ref_count:
                break
            adapter.begin_cycle()                  # what EngineRuntime.tick does
            await orch.tick(ex.id)
            per_cycle.append(len(adapter.placed) - seen)
            seen = len(adapter.placed)

        assert max(per_cycle) <= 3                 # never exceeded the cap in a cycle
        assert len(per_cycle) > 1                  # genuinely spread across cycles
        assert seen == ref_count                   # eventually fully deployed
        assert sorted(str(o.amount_base) for o in adapter.placed) == ref_sizes
        # And every level ends up resting (none dropped).
        assert all(lv.state is GridLevelState.OPEN_ORDER_PLACED for lv in ex.levels)

    asyncio.run(body())


# --------------------------------------------------------------------------
# 5. MID — never skip the reducing side (the safety invariant)
# --------------------------------------------------------------------------
_MM_CFG = dict(
    trading_pair="BTC", spread_bp="10", order_amount_quote="10",
    levels="1", leverage="1",
)


def _mm(adapter):
    orch = ExecutorOrchestrator()
    c = MarketMakingController(
        user_id=1, orchestrator=orch, adapter=adapter,
        inventory=InventoryRepository(), configs=dict(_MM_CFG),
    )
    return orch, c


def test_mid_defers_opening_quote_but_never_the_reducing_quote():
    async def body():
        _set_cap(1)
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter)
        await orch.spawn_controller(c)
        # Exhaust the opening budget for this cycle.
        adapter.begin_cycle()
        adapter._note_opening_placement()
        assert adapter.opening_budget_exhausted() is True

        # OPENING side, empty slot, budget spent -> the quote is DEFERRED.
        await c._reconcile(TradeType.BUY, Decimal("99"), True, Decimal("100"),
                           is_opening=True)
        assert adapter.placed == []

        # REDUCING side, same exhausted budget -> the quote is PLACED. The book
        # must always be able to trim inventory / work a position off.
        await c._reconcile(TradeType.SELL, Decimal("101"), True, Decimal("100"),
                           is_opening=False)
        assert len(adapter.placed) == 1
        assert adapter.placed[0].side is TradeType.SELL

    asyncio.run(body())


def test_fill_anchored_marks_reducing_side_so_the_cap_never_defers_it():
    """FillAnchoredQuotingController inherits Mid's _quote_side/_reconcile gate, so
    it must pass the SAME inventory-signed is_opening — otherwise its rebalancing
    (reducing) leg would take the is_opening=True default and be gate-able, breaking
    the never-skip-the-reducing-side invariant for the grid maker path."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        c = FillAnchoredQuotingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(),
            configs={
                "trading_pair": "BTC-PERP", "anchor_mode": "grid",
                "spread_bid_pct": Decimal("0.001"), "spread_ask_pct": Decimal("0.001"),
                "order_amount_quote": Decimal(10),
                "price_distance_tolerance": Decimal("0.0001"),
            },
            controller_id="FA",
        )
        await orch.spawn_controller(c)
        c._base_value = lambda _mid: Decimal(50)   # held LONG -> SELL reduces it

        captured = {}

        async def _spy(side, target, allowed, mid, *, is_opening=True):
            captured[side] = is_opening

        c._quote_side = _spy
        await c.on_tick()

        # BUY grows the long -> opening (cap may defer). SELL trims it -> reducing
        # (the cap must NEVER defer it). Same signing Mid uses.
        assert captured[TradeType.BUY] is True
        assert captured[TradeType.SELL] is False

    asyncio.run(body())


# --------------------------------------------------------------------------
# 4 (revgrid). Resumable trigger ladder — completes over cycles, no double-place
# --------------------------------------------------------------------------
_RG_PAIR = "BTC-PERP"
_RG_CFG = {
    "trading_pair": _RG_PAIR, "levels": 4, "step_pct": Decimal("0.01"),
    "order_amount_quote": Decimal("100"),
    "revgrid_chop_stand_down": False,   # no candle feed in unit tests; gate has its own
}


def test_revgrid_ladder_completes_over_cycles_when_capped():
    async def body():
        _set_cap(3)                                # 3 rungs/cycle; ladder wants 2*4=8
        adapter = _CountingAdapter(
            mid=Decimal(100), lot=Decimal("0.0001"), min_notional=Decimal("1"),
            venue_held={_RG_PAIR: Decimal(0)},     # flat net -> arm the ladder
        )
        c = ReverseGridController(
            user_id=1, orchestrator=object(), adapter=adapter,
            inventory=None, configs=dict(_RG_CFG),
        )

        placed_each = []
        for _ in range(6):
            adapter.begin_cycle()
            before = len(adapter.placed_triggers)
            await c.on_tick()
            placed_each.append(len(adapter.placed_triggers) - before)
            if not c._ladder_incomplete and c._rungs_resting():
                break

        assert max(placed_each) <= 3               # never over the cap in a cycle
        # All 8 rungs eventually laid, and NONE double-placed (unique levels/side).
        keys = {(r.side, r.level) for r in c._rungs}
        assert len(c._rungs) == len(keys)          # no duplicate rungs
        assert len(c._rungs) == 2 * 4              # full ladder present
        assert c._ladder_incomplete is False       # marked complete when done

    asyncio.run(body())
