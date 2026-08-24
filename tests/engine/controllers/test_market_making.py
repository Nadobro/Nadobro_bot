import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.risk import RiskEngine
from src.nadobro.engine.types import RiskLimits, RiskState, TradeType


def _mm(adapter, inv, configs, *, orch=None, controller_id=None):
    orch = orch or ExecutorOrchestrator()
    c = MarketMakingController(
        user_id=1, orchestrator=orch, adapter=adapter, inventory=inv,
        configs=configs, controller_id=controller_id,
    )
    return orch, c


BASE = {"trading_pair": "P", "spread_bid_pct": "0.01", "spread_ask_pct": "0.01",
        "order_amount_quote": "10", "price_distance_tolerance": "0.001", "max_base_quote": "1000"}


def test_manual_spread_is_floored_at_fee_clearing_minimum():
    """MM-SPREAD-FLOOR fix: a manual sub-floor spread is raised to
    spread_floor_half_pct so the book can't quote below the fee-clearing
    minimum (which would lose on every fill)."""
    cfg = dict(BASE)
    cfg.update(spread_bid_pct="0.00001", spread_ask_pct="0.00001",  # 0.1 bp, sub-floor
               spread_floor_half_pct="0.00015")                      # 1.5 bp floor
    _, c = _mm(MockNadoAdapter(mid=Decimal(100)), InventoryRepository(), cfg)
    assert c.spread_bid_pct == Decimal("0.00015")
    assert c.spread_ask_pct == Decimal("0.00015")


def test_manual_spread_above_floor_is_unchanged():
    cfg = dict(BASE)
    cfg.update(spread_bid_pct="0.01", spread_ask_pct="0.01", spread_floor_half_pct="0.00015")
    _, c = _mm(MockNadoAdapter(mid=Decimal(100)), InventoryRepository(), cfg)
    assert c.spread_bid_pct == Decimal("0.01")
    assert c.spread_ask_pct == Decimal("0.01")


def test_quotes_around_mid():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, InventoryRepository(), dict(BASE))
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert len(orch.list(c.id, active_only=True)) == 2
        prices = sorted(o.price for o in adapter.placed)
        assert prices == [Decimal(99), Decimal(101)]

    asyncio.run(body())


def test_directional_bias_long_skews_quotes_toward_buying():
    """A full long bias (+1) tightens the bid (closer to mid → front-loads buys)
    and widens the ask, leaning into longs. With spread 1% and the documented
    ±0.2 alpha-tilt: bid 99.2 (0.8× spread), ask 101.2 (1.2× spread)."""
    async def body():
        cfg = dict(BASE)
        cfg["directional_bias"] = 1.0
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, InventoryRepository(), cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        prices = sorted(o.price for o in adapter.placed)
        assert prices == [Decimal("99.2"), Decimal("101.2")]

    asyncio.run(body())


def test_directional_bias_short_skews_quotes_toward_selling():
    """A full short bias (-1) tightens the ask and widens the bid (±0.2 tilt):
    bid 98.8, ask 100.8."""
    async def body():
        cfg = dict(BASE)
        cfg["directional_bias"] = -1.0
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, InventoryRepository(), cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        prices = sorted(o.price for o in adapter.placed)
        assert prices == [Decimal("98.8"), Decimal("100.8")]

    asyncio.run(body())


def test_directional_bias_neutral_keeps_symmetric_quotes():
    async def body():
        cfg = dict(BASE)
        cfg["directional_bias"] = 0.0
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, InventoryRepository(), cfg)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        prices = sorted(o.price for o in adapter.placed)
        assert prices == [Decimal(99), Decimal(101)]

    asyncio.run(body())


def test_directional_bias_parsing_clamps_and_tolerates_text_default():
    from src.nadobro.engine.controllers.market_making import _safe_bias
    # Legacy text default and garbage → neutral (0); out-of-range → clamped.
    assert _safe_bias("neutral") == Decimal(0)
    assert _safe_bias(None) == Decimal(0)
    assert _safe_bias(2.5) == Decimal(1)
    assert _safe_bias(-9) == Decimal(-1)
    assert _safe_bias("0.4") == Decimal("0.4")
    # Constructed with the text default, the controller is symmetric (bias 0).
    cfg = dict(BASE)
    cfg["directional_bias"] = "neutral"
    _, c = _mm(MockNadoAdapter(mid=Decimal(100)), InventoryRepository(), cfg)
    assert c.directional_bias == Decimal(0)


def test_refresh_on_drift():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, InventoryRepository(), dict(BASE))
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        bid1 = c._bid_id
        cancels_before = len(adapter.cancelled)
        adapter.set_mid(Decimal(110))
        await orch.tick_controller(c.id)
        assert c._bid_id != bid1 and len(adapter.cancelled) > cancels_before

    asyncio.run(body())


def test_inventory_suspends_buys_at_max_base():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        inv = InventoryRepository()
        inv.apply_fill(1, "P", "CID", TradeType.BUY, Decimal(20), Decimal(2000))
        orch, c = _mm(adapter, inv, dict(BASE), controller_id="CID")
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._bid_id is None and c._ask_id is not None

    asyncio.run(body())


def test_profit_protection_suspends_both():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(90))
        inv = InventoryRepository()
        inv.apply_fill(1, "P", "CID", TradeType.BUY, Decimal(20), Decimal(2000))
        cfg = dict(BASE, profit_protection=True)
        orch, c = _mm(adapter, inv, cfg, controller_id="CID")
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c._bid_id is None and c._ask_id is None

    asyncio.run(body())


def test_risk_pretick_block_skips_quoting():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        risk = RiskEngine(RiskLimits(daily_pnl_floor_quote=Decimal(0)))

        def provider(_cid):
            s = RiskState()
            s.daily_pnl_quote = Decimal(-100)
            return s

        orch = ExecutorOrchestrator(risk_engine=risk, risk_state_provider=provider)
        _, c = _mm(adapter, InventoryRepository(), dict(BASE), orch=orch)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert any(ev.kind == "controller_skipped" for ev in orch.event_log)
        assert orch.list(c.id, active_only=True) == []

    asyncio.run(body())


def _mid_policy_cfg(**extra):
    cfg = dict(BASE)
    cfg.update({
        "mid_policy_enabled": 1,
        "mid_execution_mode": "normal",
        "max_quote_lifetime_s": 12,
        "inventory_soft_limit_quote": "60",
        "inventory_hard_limit_quote": "75",
        "max_base_quote": "75",
        "min_base_quote": "-75",
        "expected_budget_usd": "10",
    })
    cfg.update(extra)
    return cfg


def test_mid_soft_inventory_keeps_both_sides_and_rebalances_prices():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        inv = InventoryRepository()
        inv.apply_fill(1, "P", "CID", TradeType.BUY, Decimal("0.7"), Decimal("70"))
        orch, c = _mm(adapter, inv, _mid_policy_cfg(), controller_id="CID")
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c.inventory_state == "soft_long"
        assert c._bid_id is not None and c._ask_id is not None
        assert sorted(o.price for o in adapter.placed) == [Decimal("98.5"), Decimal("100.75")]

    asyncio.run(body())


def test_mid_hard_inventory_only_quotes_the_reducing_side():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        inv = InventoryRepository()
        inv.apply_fill(1, "P", "CID", TradeType.BUY, Decimal("0.8"), Decimal("80"))
        orch, c = _mm(adapter, inv, _mid_policy_cfg(), controller_id="CID")
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c.inventory_state == "hard_long"
        assert c._bid_id is None and c._ask_id is not None

    asyncio.run(body())


def test_mid_expected_budget_stops_new_exposure_but_keeps_exit_quote():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        inv = InventoryRepository()
        # Fees alone exhaust the $5 loss-and-cost budget while the long remains
        # open, so only its passive reducing ask may remain.
        inv.apply_fill(
            1, "P", "CID", TradeType.BUY, Decimal("0.1"), Decimal("10"), fee_quote=Decimal("6")
        )
        orch, c = _mm(
            adapter, inv, _mid_policy_cfg(expected_budget_usd="5"), controller_id="CID"
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        assert c.budget_stop_reason == "expected_budget_exhausted"
        assert c.expected_budget_used_usd == Decimal("6")
        assert c._bid_id is None and c._ask_id is not None
        assert c.ladder_metrics()["budget_stop_reason"] == "expected_budget_exhausted"

    asyncio.run(body())


def test_mid_profile_quote_ttl_forces_refresh_when_target_is_unchanged():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch, c = _mm(adapter, InventoryRepository(), _mid_policy_cfg(max_quote_lifetime_s="1"))
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        first_bid = c._bid_id
        c._slot(True, 0).placed_at = c._now() - 2
        c._slot(False, 0).placed_at = c._now() - 2
        await orch.tick_controller(c.id)
        assert c._bid_id != first_bid
        assert len(adapter.cancelled) >= 2

    asyncio.run(body())
