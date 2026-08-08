"""Fill-anchored quoting controller — Grid's single-pair maker mode.

Pins the behaviours that define it:
1. The reference price re-anchors to the LAST FILL.
2. The no-cross invariant: never buy above the last sell / sell below the last
   buy — every round trip must capture spread.
3. Soft reset: when mid drifts past reset_threshold_pct from the reference,
   quotes re-anchor to mid (no flatten, no restart), and only the BEHIND leg
   drops its no-cross clamp.
4. Stall escalation: a bounded reduce-only taker concession before the SL rail.

Reverse Grid lives in tests/engine/test_rgrid_controller.py — it is a different
strategy with its own controller and taker executor.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import OrderType, TradeType

PAIR = "BTC-PERP"
SPREAD = Decimal("0.001")


def _controller(adapter, mode="grid", extra=None):
    configs = {
        "trading_pair": PAIR,
        "anchor_mode": mode,
        "spread_bid_pct": SPREAD,
        "spread_ask_pct": SPREAD,
        "order_amount_quote": Decimal(10),
        "price_distance_tolerance": Decimal("0.0001"),
    }
    configs.update(extra or {})
    orch = ExecutorOrchestrator()
    c = FillAnchoredQuotingController(
        user_id=1, orchestrator=orch, adapter=adapter,
        inventory=InventoryRepository(), configs=configs, controller_id="FA",
    )
    return orch, c


def _quotes(adapter):
    """Latest live (bid, ask) prices from the adapter's order book."""
    bids = [o for o in adapter.placed if o.side is TradeType.BUY]
    asks = [o for o in adapter.placed if o.side is TradeType.SELL]
    return (bids[-1].price if bids else None, asks[-1].price if asks else None)


def test_reference_reanchors_to_last_fill():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        bid, ask = _quotes(adapter)
        assert bid == Decimal("99.9") and ask == Decimal("100.1"), "first pair quotes around mid"

        # The bid fills at 99.9 -> reference must move there.
        bid_order = [o for o in adapter.placed if o.side is TradeType.BUY][-1]
        adapter.fill_order(bid_order.id)
        await orch.tick_controller(c.id)
        state = c.anchor_state()
        assert state["reference"] == Decimal("99.9")
        assert state["last_buy_px"] == Decimal("99.9")
        _, new_ask = _quotes(adapter)
        # Ask re-anchored to the fill: 99.9 * 1.001 (also satisfies no-cross).
        assert new_ask == Decimal("99.9") * (1 + SPREAD)

    asyncio.run(body())


def test_no_cross_invariant_clamps_quotes():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter)
        await orch.spawn_controller(c)
        # Book history: we last SOLD at 100 and last BOUGHT at 100.
        c._last_sell_px = Decimal(100)
        c._last_buy_px = Decimal(100)
        # Reference drifts up (e.g. a later fill at 100.2 within threshold).
        c._reference = Decimal("100.2")
        await orch.tick_controller(c.id)
        bid, ask = _quotes(adapter)
        # Bid must NOT exceed last_sell * (1 - spread): never buy above where
        # we sold. Ref-anchored bid would be 100.2*0.999 = 100.0998 — clamped.
        assert bid == Decimal(100) * (1 - SPREAD)
        # Ask >= last_buy * (1 + spread): never sell below where we bought.
        assert ask >= Decimal(100) * (1 + SPREAD)

    asyncio.run(body())


def test_soft_reset_reanchors_to_mid_beyond_threshold():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter, extra={"reset_threshold_pct": Decimal("0.0025")})
        await orch.spawn_controller(c)
        c._reference = Decimal(100)
        # Price runs 0.5% away — beyond the 0.25% threshold.
        adapter.set_mid(Decimal("100.5"))
        await orch.tick_controller(c.id)
        bid, _ = _quotes(adapter)
        # Quotes re-anchored to MID (100.5), not the stranded reference (100).
        assert bid == Decimal("100.5") * (1 - SPREAD)
        # Within the threshold the reference stays in charge.
        adapter.set_mid(Decimal("100.1"))
        c._reference = Decimal("100.05")
        await orch.tick_controller(c.id)
        bid2, _ = _quotes(adapter)
        assert bid2 == Decimal("100.05") * (1 - SPREAD)

    asyncio.run(body())


def _takers(adapter, side=None):
    out = [o for o in adapter.placed if o.order_type is OrderType.MARKET]
    return [o for o in out if side is None or o.side is side]


def test_fill_window_is_isolated_per_controller_and_user():
    """A different user's/run's controller never bleeds into this run's fill
    window — fills are sourced only from my_executors (scoped to this
    controller_id), so two controllers in the same process stay separate."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch = ExecutorOrchestrator()
        base = {
            "trading_pair": PAIR, "anchor_mode": "grid",
            "spread_bid_pct": SPREAD, "spread_ask_pct": SPREAD,
            "order_amount_quote": Decimal(10), "price_distance_tolerance": Decimal("0.0001"),
        }
        a = FillAnchoredQuotingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=dict(base), controller_id="rgrid:1:mainnet",
        )
        b = FillAnchoredQuotingController(
            user_id=2, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=dict(base), controller_id="rgrid:2:mainnet",
        )
        await orch.spawn_controller(a)
        await orch.spawn_controller(b)
        await orch.tick_controller(a.id)   # A rests its maker quotes
        # Fill ALL of A's resting orders.
        for ex in orch.list(a.id, active_only=False):
            order = getattr(ex, "order", None)
            if order is not None:
                adapter.fill_order(order.id)
        await orch.tick_controller(a.id)   # A absorbs its own fills
        await orch.tick_controller(b.id)   # B must NOT see A's fills
        assert len(a._fills) >= 1
        assert len(b._fills) == 0
        assert b._reference is None

    asyncio.run(body())


def test_seed_from_session_history_scopes_the_reference_to_the_run():
    """The runtime seeds the fill window from THIS session's own recorded fills
    (get_session_recent_fills, scoped by strategy_session_id + user_id), so the
    reference is per-session and survives a rebuild. No seed → empty (fresh run)."""
    adapter = MockNadoAdapter(mid=Decimal(100))
    seed = [  # newest-first, exactly as get_session_recent_fills returns
        {"price": 120, "size": 1, "side": "long"},
        {"price": 110, "size": 1, "side": "short"},
        {"price": 100, "size": 1, "side": "long"},
    ]
    _, c = _controller(adapter, extra={"seed_fills": seed})
    assert len(c._fills) == 3
    assert c._reference == Decimal(120)                 # newest fill
    assert c._reference == Decimal(120)                # newest fill = anchor
    assert c._last_buy_px == Decimal(120)              # most recent long
    assert c._last_sell_px == Decimal(110)             # the short
    # A fresh session (no seed) starts blank → quotes around mid on the first tick.
    _, c2 = _controller(adapter)
    assert not c2._fills and c2._reference is None


def test_grid_soft_reset_concedes_only_the_behind_leg():
    """Net-long grid in a downside soft reset concedes the SELL leg to mid (below
    cost, no no-cross) so it can rebalance; the BUY leg stays maker. Without the
    fix the no-cross clamp would block the conceding sell (ask >= last_buy)."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter, mode="grid", extra={"reset_threshold_pct": Decimal("0.001")})
        # Net long: bought 1 @ 100 (so the SELL leg is the behind/under-filled one).
        c.inventory.apply_fill(c.user_id, PAIR, c.id, TradeType.BUY, Decimal(1), Decimal(100), Decimal(0))
        c._reference = Decimal(100)
        c._last_buy_px = Decimal(100)
        await orch.spawn_controller(c)
        adapter.set_mid(Decimal("98"))            # 2% drop ≫ 0.1% threshold → soft reset
        await orch.tick_controller(c.id)
        bid, ask = _quotes(adapter)
        # SELL leg conceded to mid (below cost 100), NOT clamped up by no-cross.
        assert ask == Decimal("98") * (Decimal(1) + SPREAD)
        assert ask < Decimal(100) * (Decimal(1) + SPREAD)   # the old clamp would force >=100.1
        assert c._soft_reset_active is True
        # BUY leg re-anchored to mid (maker, below mid).
        assert bid == Decimal("98") * (Decimal(1) - SPREAD)

    asyncio.run(body())


def test_grid_concession_escalates_after_stalled_soft_reset():
    """When the soft-reset maker leg can't rebalance (price keeps trending away),
    after N stalled ticks a BOUNDED reduce-only taker concession fires to flatten
    part of the one-sided exposure before the SL rail."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter, mode="grid", extra={
            "reset_threshold_pct": Decimal("0.001"),
            "concession_enabled": True,
            "concession_escalation_ticks": 3,
            "concession_fraction": Decimal("0.5"),
        })
        # Net long 2 @ 100; price trends down to 98 and stays (maker can't fill).
        c.inventory.apply_fill(c.user_id, PAIR, c.id, TradeType.BUY, Decimal(2), Decimal(200), Decimal(0))
        c._reference = Decimal(100)
        c._last_buy_px = Decimal(100)
        await orch.spawn_controller(c)
        adapter.set_mid(Decimal("98"))
        await orch.tick_controller(c.id)
        await orch.tick_controller(c.id)
        assert _takers(adapter, TradeType.SELL) == []   # still stalling, no taker yet
        assert c._stall_ticks == 2
        assert c._soft_reset_active is True
        await orch.tick_controller(c.id)                # escalate
        concessions = _takers(adapter, TradeType.SELL)
        assert len(concessions) == 1
        assert concessions[0].amount_base == Decimal("1.0")   # 50% of net 2
        assert c._stall_ticks == 0
        # One taker at a time: it's in flight, so no second concession next tick.
        await orch.tick_controller(c.id)
        assert len(_takers(adapter, TradeType.SELL)) == 1

    asyncio.run(body())


def test_grid_concession_is_gated_when_disabled_or_immaterial():
    async def body():
        # Disabled → never escalates regardless of stall length.
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch, c = _controller(adapter, mode="grid", extra={
            "reset_threshold_pct": Decimal("0.001"),
            "concession_enabled": False, "concession_escalation_ticks": 2,
        })
        c.inventory.apply_fill(c.user_id, PAIR, c.id, TradeType.BUY, Decimal(2), Decimal(200), Decimal(0))
        c._reference = Decimal(100)
        await orch.spawn_controller(c)
        adapter.set_mid(Decimal("98"))
        for _ in range(5):
            await orch.tick_controller(c.id)
        assert _takers(adapter) == []

        # Enabled but immaterial exposure (< one quote notional) → no concession.
        adapter2 = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        orch2, c2 = _controller(adapter2, mode="grid", extra={
            "reset_threshold_pct": Decimal("0.001"),
            "concession_enabled": True, "concession_escalation_ticks": 2,
            "order_amount_quote": Decimal(100),   # material gate = $100 notional
        })
        c2.inventory.apply_fill(c2.user_id, PAIR, c2.id, TradeType.BUY, Decimal("0.1"), Decimal(10), Decimal(0))
        c2._reference = Decimal(100)
        await orch2.spawn_controller(c2)
        adapter2.set_mid(Decimal("98"))
        for _ in range(5):
            await orch2.tick_controller(c2.id)
        assert _takers(adapter2) == []   # 0.1*98 = $9.8 < $100 gate

    asyncio.run(body())


def test_grid_metrics_reports_green_red_reset_levels():
    adapter = MockNadoAdapter(mid=Decimal(100))
    _, c = _controller(adapter, mode="grid", extra={"reset_threshold_pct": Decimal("0.01")})
    c._last_ref = Decimal(100)
    m = c.grid_metrics()
    assert m["grid_reset_up_price"] == 101.0     # green: over-short → rise to trigger
    assert m["grid_reset_down_price"] == 99.0    # red: over-long → fall to trigger
    assert m["grid_reset_threshold_bp"] == 100.0  # 0.01 × 10000
    assert m["grid_reset_active"] is True
    assert m["grid_mode"] == "grid"


def test_runtime_opt_in_maps_override_and_treadfi_defaults():
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    grid_cfg = map_strategy_config(
        "grid", {"fill_anchored": 1, "notional_usd": 100.0, "levels": 2},
        Decimal(100), product=PAIR,
    )
    assert grid_cfg["controller_override"] == "fill_anchored"
    assert grid_cfg["anchor_mode"] == "grid"
    assert grid_cfg["reset_threshold_pct"] == Decimal("0.0025")   # TreadFi 0.25%

    rgrid_cfg = map_strategy_config(
        "rgrid", {"fill_anchored": 1, "notional_usd": 100.0, "levels": 2},
        Decimal(100), product=PAIR,
    )
    assert rgrid_cfg["anchor_mode"] == "rgrid"
    # rgrid's default arms the trailing soft reset: 0.2%, floored at 2 x spread so
    # the controller's "target inside the entry band" guard can never silently
    # disable it (see test_fill_anchored_reset_threshold_reads_ui_keys).
    assert rgrid_cfg["reset_threshold_pct"] == Decimal("0.002")
    assert rgrid_cfg["reset_threshold_pct"] >= rgrid_cfg["spread_ask_pct"] * 2

    # Opting OUT (fill_anchored=0) gives the classic ladder mapping.
    ladder_cfg = map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 2, "fill_anchored": 0}, Decimal(100), product=PAIR,
    )
    assert "controller_override" not in ladder_cfg
    assert "start_price" in ladder_cfg


