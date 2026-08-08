"""R-Grid → legacy reporting bridge (trades_<network> / strategy_sessions).

CLAUDE.md: "Engine fills are bridged into the legacy trades_<network> /
strategy_sessions tables so /status, fills, and portfolio views show real numbers
— keep that bridge in sync when touching fill handling."

R-Grid moved to its OWN controller and its OWN executor class
(RGridMakerExecutor), so this pins that the bridge is class-agnostic and that
every kind of R-Grid fill — the break entry, the trailing exit and the reversal
flatten — still reaches the recorder with a digest, a side and a size.

The venue reports NO per-fill realized PnL (realized_pnl_x18 is always 0), so all
attribution comes from these rows. A fill that misses the bridge is invisible
volume and corrupt PnL.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.rgrid import RGridController
from src.nadobro.engine.executors.rgrid_maker_executor import RGridMakerExecutor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import OrderType, TradeType

PAIR = "BTC-PERP"
SPREAD = Decimal("0.001")
CID = "rgrid:4242:mainnet"


class _SpyRecorder:
    """Stands in for DbTradeRecorder. Same call shape, no DB."""

    def __init__(self):
        self.rows: list[dict] = []
        self.placements: list[tuple[str, str]] = []

    def record(self, controller_id, trading_pair, side, amount_base, price,
               fee_quote, order_id=None, timestamp=None, *,
               realized_pnl=None, is_taker=False):
        self.rows.append({
            "controller_id": controller_id, "trading_pair": trading_pair,
            "side": side, "amount_base": amount_base, "price": price,
            "fee_quote": fee_quote, "order_digest": order_id,
            "is_taker": is_taker,
        })

    def link_placement(self, controller_id, order_id):
        self.placements.append((controller_id, order_id))


async def _walk(orch, c, adapter, prices):
    """Tick through a price path, filling whatever R-Grid rests. Maker quotes do
    not auto-fill, so the fill has to be driven explicitly."""
    await orch.tick_controller(c.id)              # establish the anchor
    for px in prices:
        adapter.set_mid(Decimal(px))
        await orch.tick_controller(c.id)
        for o in list(adapter.placed):
            if o.filled_base == 0:
                adapter.fill_order(o.id)
        await orch.tick_controller(c.id)


def _controller(adapter, recorder, extra=None):
    configs = {
        "trading_pair": PAIR,
        "spread_bid_pct": SPREAD,
        "spread_ask_pct": SPREAD,
        "order_amount_quote": Decimal(10),
        "price_distance_tolerance": Decimal("0.0001"),
    }
    configs.update(extra or {})
    orch = ExecutorOrchestrator(trade_recorder=recorder)
    c = RGridController(
        user_id=4242, orchestrator=orch, adapter=adapter,
        inventory=InventoryRepository(), configs=configs, controller_id=CID,
    )
    return orch, c


def test_a_break_entry_reaches_the_reporting_bridge():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        rec = _SpyRecorder()
        orch, c = _controller(adapter, rec)
        await orch.spawn_controller(c)
        await _walk(orch, c, adapter, ["101"])    # rests a bid at 100.1, filled

        assert rec.rows, "the entry fill never reached the bridge"
        row = rec.rows[0]
        assert row["controller_id"] == CID, "session resolution keys off this"
        assert row["trading_pair"] == PAIR
        assert row["side"] is TradeType.BUY
        assert Decimal(str(row["amount_base"])) > 0
        assert row["order_digest"], "no digest — the venue match cannot reconcile"

    asyncio.run(body())


def test_the_trailing_exit_reaches_the_bridge_too():
    """An unrecorded close is the phantom-History / missing-close-volume defect:
    the open is counted and the close is not, so the session shows a position it
    no longer holds and undercounts volume."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        rec = _SpyRecorder()
        orch, c = _controller(adapter, rec, extra={
            "reset_threshold_pct": Decimal("0.01"), "trail_enabled": True,
        })
        await orch.spawn_controller(c)
        await _walk(orch, c, adapter, ["101"])    # long on the resting bid
        entries = len(rec.rows)
        assert entries >= 1

        await _walk(orch, c, adapter, ["108", "107"])   # arm, then the exit rests+fills

        assert len(rec.rows) > entries, "the reducing leg never reached the bridge"
        assert rec.rows[-1]["side"] is TradeType.SELL
        assert rec.rows[-1]["order_digest"]

    asyncio.run(body())


def test_the_reversal_flatten_reaches_the_bridge():
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        rec = _SpyRecorder()
        orch, c = _controller(adapter, rec)
        await orch.spawn_controller(c)
        await _walk(orch, c, adapter, ["104"])
        entries = len(rec.rows)

        await _walk(orch, c, adapter, ["100"])    # price back through the sell leg
        assert len(rec.rows) > entries, "the reducing leg never reached the bridge"
        assert rec.rows[-1]["side"] is TradeType.SELL

    asyncio.run(body())


def test_maker_fills_report_maker_and_the_crossing_stop_reports_taker():
    """The flag is derived from the order type, so the mixed execution model is
    reported honestly: resting quotes are maker, the trailing stop is a taker."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        rec = _SpyRecorder()
        orch, c = _controller(adapter, rec, extra={
            "reset_threshold_pct": Decimal("0.01"), "trail_enabled": True,
        })
        await orch.spawn_controller(c)
        await _walk(orch, c, adapter, ["101", "108", "107"])

        assert rec.rows, "expected fills"
        makers = [r for r in rec.rows if r["is_taker"] is False]
        takers = [r for r in rec.rows if r["is_taker"] is True]
        assert makers, "the resting quotes should report maker"
        # Exactly the crossing orders report taker, and there is at most one stop.
        crossing = [o for o in adapter.placed if o.order_type is OrderType.LIMIT]
        assert len(takers) == len(crossing)
        assert all(o.order_type in (OrderType.LIMIT_MAKER, OrderType.LIMIT)
                   for o in adapter.placed)

    asyncio.run(body())


def test_the_recorder_is_injected_into_the_new_executor_class():
    """The orchestrator injects the recorder at spawn. RGridMakerExecutor is a new
    class; if injection were class-gated, R-Grid would go dark on every surface."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        rec = _SpyRecorder()
        orch, c = _controller(adapter, rec)
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        adapter.set_mid(Decimal("101"))
        await orch.tick_controller(c.id)

        takers = [e for e in c.my_executors(active_only=False)
                  if isinstance(e, RGridMakerExecutor)]
        assert takers, "expected an RGridMakerExecutor"
        assert all(e.trade_recorder is rec for e in takers), (
            "the recorder was not injected into R-Grid's executor"
        )

    asyncio.run(body())


def test_placement_linking_is_wired_for_rgrids_controller_id():
    """The on_place hook writes the digest->session intent BEFORE any fill, so
    nado_sync attributes the venue match to the session even if the executor's own
    fill detection misses it. It must parse R-Grid's controller id."""
    from src.nadobro.trading.engine_persistence import _parse_controller_id
    from src.nadobro.strategy.engine_runtime import deterministic_controller_id

    cid = deterministic_controller_id("rgrid", 4242, "mainnet")
    assert cid == CID
    assert _parse_controller_id(cid) == ("rgrid", 4242, "mainnet")


def test_the_bridge_survives_a_recorder_that_raises():
    """A reporting failure must never lose a fill or break execution."""
    async def body():
        class _Broken(_SpyRecorder):
            def record(self, *a, **k):
                raise RuntimeError("db down")

        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100))
        orch, c = _controller(adapter, _Broken())
        await orch.spawn_controller(c)
        await _walk(orch, c, adapter, ["101"])
        # The fill still landed in inventory even though reporting blew up.
        assert c._net_base() > 0
        assert c._has_fills()

    asyncio.run(body())
