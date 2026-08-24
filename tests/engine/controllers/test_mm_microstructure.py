"""Mid Mode v3 Phase 2 — microstructure telemetry on the sized book.

Two things must hold, and the second is the one that protects production:

1. When enabled, the controller reads ``depth_book`` (the SIZED ladder — the
   engine's ``order_book`` fabricates levels with amount=0) and records
   microprice / imbalance / spread / depth.
2. It is OBSERVATION ONLY, and it is OFF by default — so
   ``FillAnchoredQuotingController`` and ``RGridController``, which both
   inherit ``MarketMakingController``, are bit-for-bit unchanged.
"""
import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.adapter.base import OrderBookLevel, OrderBookSnapshot
from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator

BASE = {
    "trading_pair": "P",
    "spread_bid_pct": "0.01",
    "spread_ask_pct": "0.01",
    "order_amount_quote": "100",
}


class _DepthAdapter(MockNadoAdapter):
    """Mock that serves a real sized ladder, which the base mock does not."""

    def __init__(self, *a, bids=((99.0, 90.0),), asks=((101.0, 10.0),), fail=False, **kw):
        super().__init__(*a, **kw)
        self._bids, self._asks, self._fail = bids, asks, fail
        self.depth_calls = 0

    async def depth_book(self, trading_pair, depth: int = 10) -> OrderBookSnapshot:
        self.depth_calls += 1
        if self._fail:
            raise RuntimeError("depth unavailable")
        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=[OrderBookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in self._bids],
            asks=[OrderBookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in self._asks],
            timestamp=0.0,
        )


def _run(adapter, configs):
    async def body():
        orch = ExecutorOrchestrator()
        c = MarketMakingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=configs, controller_id="mm-micro",
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        return c, adapter
    return asyncio.run(body())


def test_disabled_by_default_so_inheriting_controllers_are_untouched():
    # FillAnchored (Grid) and RGrid subclass this controller. If the read were
    # on by default they would each gain a per-tick query and a behaviour risk.
    adapter = _DepthAdapter(mid=Decimal(100))
    c, adapter = _run(adapter, dict(BASE))
    assert c.microstructure_log is False
    assert adapter.depth_calls == 0
    assert c.micro == {}


def test_records_the_sized_book_view_when_enabled():
    adapter = _DepthAdapter(mid=Decimal(100), bids=((99.0, 90.0),), asks=((101.0, 10.0),))
    c, adapter = _run(adapter, {**BASE, "microstructure_log": "1"})
    assert adapter.depth_calls == 1
    m = c.micro
    # Bid-heavy book => size-weighted fair value sits above the arithmetic mid.
    assert m["book_mid"] == 100.0
    assert m["microprice"] > m["book_mid"]
    assert m["micro_vs_mid_bp"] > 0
    assert m["obi"][1] > 0
    assert m["levels"] == (1, 1)


def test_telemetry_does_not_move_the_quotes():
    # The whole point of Phase 2: measure without touching behaviour. Same
    # config, one with the read on — the placed prices must be identical.
    off = _DepthAdapter(mid=Decimal(100))
    _run(off, dict(BASE))
    on = _DepthAdapter(mid=Decimal(100))
    _run(on, {**BASE, "microstructure_log": "1"})
    assert sorted(o.price for o in off.placed) == sorted(o.price for o in on.placed)
    assert len(on.placed) == 2


def test_a_failing_depth_read_never_breaks_quoting():
    adapter = _DepthAdapter(mid=Decimal(100), fail=True)
    c, adapter = _run(adapter, {**BASE, "microstructure_log": "1"})
    assert c.micro == {}                       # nothing recorded
    assert len(adapter.placed) == 2            # but the book still quoted


def test_missing_depth_support_is_tolerated():
    # The stock mock (and any adapter that has not implemented depth_book)
    # raises NotImplementedError; that must degrade, not propagate.
    adapter = MockNadoAdapter(mid=Decimal(100))
    c, adapter = _run(adapter, {**BASE, "microstructure_log": "1"})
    assert c.micro == {}
    assert len(adapter.placed) == 2


def test_only_the_mid_mapping_turns_it_on():
    """Guard the inheritance blast radius at the mapping layer too: grid,
    rgrid and dgrid must not emit the flag, or the controllers that subclass
    MarketMakingController would silently start reading depth every tick."""
    from decimal import Decimal as D

    from src.nadobro.strategy.engine_runtime import map_strategy_config

    conf = {"notional_usd": 100.0, "spread_bp": 5.0, "levels": 2}
    mid_cfg = map_strategy_config("mid", dict(conf), D(100), product="BTC-PERP")
    assert mid_cfg["microstructure_log"] == D(1)

    for strategy in ("grid", "rgrid", "dgrid"):
        cfg = map_strategy_config(strategy, dict(conf), D(100), product="BTC-PERP")
        assert "microstructure_log" not in cfg, strategy
