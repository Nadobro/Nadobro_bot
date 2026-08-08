"""``is_taker`` on recorded fills — RG-TAKERFLAG-1 (self-review 2026-08-07).

The venue reports NO per-fill maker/taker flag on the path the engine uses: the
SDK's ``IndexerMatch`` model carries only base/quote/fee (+ digest/order/
timestamp), so ``NadoClient.get_matches`` cannot answer it. (``venue/
nado_archive`` reads an ``is_taker`` key defensively off the RAW archive HTTP
shape, which the adapter does not use.) So ``Executor._fill_was_taker`` resolves
it in three tiers, and these tests pin each one.

This flag is REPORTING ONLY — the ``trades_<network>.is_taker`` column and the
History/trade_service views. Fees are the venue's own (``fill.fee_quote``, split
into builder_fee + fill_fee by engine_persistence), so a wrong flag never
mis-prices a fill. Before this it was derived from the REQUESTED
``execution_strategy is MARKET``, which recorded three genuinely-crossing cases
as maker: the volume bot's marketable limits, the adapter's dust-exit rewrite,
and grid's reduce-only MARKET flatten (whose config has no execution_strategy
at all).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.adapter.base import NadoOrder
from src.nadobro.engine.controllers.volume_bot import VolumeBotController
from src.nadobro.engine.executors.grid_executor import GridExecutor, GridExecutorConfig
from src.nadobro.engine.executors.order_executor import (
    OrderExecutor, OrderExecutorConfig,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import (
    CloseType, ExecutionStrategy, OrderType, PositionAction, TradeType,
)

PAIR = "KBTC"


class _SpyRecorder:
    """Same call shape as DbTradeRecorder, no DB."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, controller_id, trading_pair, side, amount_base, price,
               fee_quote, order_id=None, timestamp=None, *,
               realized_pnl=None, is_taker=False):
        self.rows.append({"side": side, "amount_base": amount_base,
                          "order_id": order_id, "is_taker": is_taker})

    def link_placement(self, controller_id, order_id):
        pass

    @property
    def takers(self) -> list[dict]:
        return [r for r in self.rows if r["is_taker"] is True]

    @property
    def makers(self) -> list[dict]:
        return [r for r in self.rows if r["is_taker"] is False]


# ---------------------------------------------------------------------------
# Tier 2 — a declared crossing intent on an order that stays a LIMIT.
# ---------------------------------------------------------------------------
def test_a_vol_bot_crossing_leg_records_as_taker():
    """The volume bot's crossing legs are marketable LIMITs — priced THROUGH the
    book but still limit orders, so nothing about the order type says "taker".
    Every one of its fills used to record as maker."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        rec = _SpyRecorder()
        orch = ExecutorOrchestrator(trade_recorder=rec)
        c = VolumeBotController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), controller_id="VB",
            configs={"trading_pair": PAIR, "vol_execution_algo": "taker",
                     "total_amount_quote": "100"},
        )
        await orch.spawn_controller(c)
        assert adapter.placed, "taker mode must submit a leg on start"
        crossing = adapter.placed[0]
        assert crossing.order_type is OrderType.LIMIT, (
            "the premise of this test: it crosses while REMAINING a limit order"
        )

        adapter.fill_order(crossing.id)
        for ex in c.my_executors(active_only=False):
            await orch.tick(ex.id)

        assert rec.rows, "the crossing leg never reached the recorder"
        assert not rec.makers, (
            f"a deliberately-crossing leg recorded as MAKER: {rec.rows}"
        )
        assert len(rec.takers) == len(rec.rows)

    asyncio.run(body())


def test_the_vol_bots_resting_quotes_still_record_as_maker():
    """Control. The maker path must NOT be swept into the taker bucket — the
    whole point of the v3 maker mode is that those fills pay the maker fee."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(100), auto_fill_market=False)
        rec = _SpyRecorder()
        orch = ExecutorOrchestrator(trade_recorder=rec)
        c = VolumeBotController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), controller_id="VB",
            configs={"trading_pair": PAIR, "vol_taker_mode": 0,
                     "vol_execution_algo": "twap", "total_amount_quote": "100",
                     "vol_cross_after_seconds": 0},
        )
        await orch.spawn_controller(c)
        assert adapter.placed
        resting = adapter.placed[0]
        assert resting.order_type is OrderType.LIMIT_MAKER

        adapter.fill_order(resting.id)
        for ex in c.my_executors(active_only=False):
            await orch.tick(ex.id)

        assert rec.rows, "the resting leg never reached the recorder"
        assert not rec.takers, f"a resting post-only fill recorded as TAKER: {rec.rows}"

    asyncio.run(body())


def test_every_vol_bot_crossing_site_declares_it():
    """``crosses_book`` is only correct if EVERY crossing site sets it. A new
    taker leg that forgets to would silently record as maker, which is exactly
    the bug this closes — so pin the count at the source."""
    import pathlib

    src = pathlib.Path(
        "src/nadobro/engine/controllers/volume_bot.py"
    ).read_text()
    crossing_kinds = ("buy_taker", "sell_taker", "buy_cross", "sell_cross")
    for kind in crossing_kinds:
        assert f'kind="{kind}"' in src, f"{kind} leg disappeared — retune this test"
    # Every _spawn_order that requests a plain LIMIT is a crossing leg for this
    # controller (its resting default is LIMIT_MAKER).
    assert src.count("execution=ExecutionStrategy.LIMIT,") == len(crossing_kinds)
    assert src.count("crosses_book=True") == len(crossing_kinds), (
        "a crossing leg is not declaring crosses_book — its fills will record "
        "as maker"
    )


# ---------------------------------------------------------------------------
# Tier 1 — what the venue was ACTUALLY sent.
# ---------------------------------------------------------------------------
class _CoercingAdapter(MockNadoAdapter):
    """Models the adapter's EXIT-MIN-NOTIONAL escape: a sub-minimum reduce-only
    resting order can never fill, so ``adapter/nado.py`` rewrites it to MARKET
    *after* the executor has decided. The config still says LIMIT_MAKER."""

    async def place_order(self, trading_pair, side, order_type, amount_base,
                          price=None, leverage=1, reduce_only=False) -> NadoOrder:
        if reduce_only and order_type is not OrderType.MARKET:
            order_type = OrderType.MARKET
            price = None
        return await super().place_order(
            trading_pair, side, order_type, amount_base, price, leverage,
            reduce_only,
        )


def test_an_order_the_adapter_rewrote_to_market_records_as_taker():
    """The dust-exit rewrite is invisible to the config: it asked for a post-only
    limit and the venue got a MARKET order. The wire is the truth."""
    async def body():
        adapter = _CoercingAdapter(mid=Decimal(100), auto_fill_market=True)
        rec = _SpyRecorder()
        orch = ExecutorOrchestrator(trade_recorder=rec)
        cfg = OrderExecutorConfig(
            PAIR, TradeType.SELL, Decimal("0.001"),
            ExecutionStrategy.LIMIT_MAKER, price=Decimal(100),
            position_action=PositionAction.CLOSE,
        )
        ex = OrderExecutor(cfg, user_id=1, controller_id="c", adapter=adapter,
                           inventory=InventoryRepository())
        ex.trade_recorder = rec
        await ex.on_create()

        assert cfg.execution_strategy is ExecutionStrategy.LIMIT_MAKER, (
            "the config must still claim maker — that is the trap"
        )
        assert adapter.placed[0].order_type is OrderType.MARKET, (
            "premise: the adapter crossed on our behalf"
        )
        assert rec.rows, "the coerced exit never reached the recorder"
        assert not rec.makers, (
            f"a fill the venue took as a MARKET order recorded as maker: {rec.rows}"
        )

    asyncio.run(body())


def test_a_grid_stop_out_flatten_records_as_taker():
    """GridExecutorConfig has no ``execution_strategy`` at all, so the old
    derivation returned False for a reduce-only MARKET flatten. The resting
    ladder fills around it must stay maker."""
    async def body():
        adapter = MockNadoAdapter(fill_marketable_limits=True, mid=Decimal(105))
        rec = _SpyRecorder()
        inv = InventoryRepository()
        cfg = GridExecutorConfig(
            trading_pair="SOL-USDC", side=TradeType.BUY,
            start_price=Decimal(100), end_price=Decimal(110),
            limit_price=Decimal(95), total_amount_quote=Decimal(1000),
            min_spread_between_orders=Decimal("0.02"), keep_position=False,
        )
        ex = GridExecutor(cfg, user_id=1, controller_id="c", adapter=adapter,
                          inventory=inv)
        ex.trade_recorder = rec
        await ex.on_create()
        lvl = ex.levels[0]
        adapter.fill_order(lvl.open_order_id, price=lvl.open_price)
        await ex.on_tick()

        opens = list(rec.rows)
        assert opens and not [r for r in opens if r["is_taker"]], (
            f"the resting ladder open should be maker: {opens}"
        )

        await ex.on_stop(CloseType.EARLY_STOP)
        assert inv.get(1, "SOL-USDC", "c").net_amount_base == Decimal(0)

        flatten_rows = rec.rows[len(opens):]
        assert flatten_rows, "the flatten fill never reached the recorder"
        assert all(r["is_taker"] is True for r in flatten_rows), (
            f"the reduce-only MARKET flatten recorded as maker: {flatten_rows}"
        )

    asyncio.run(body())


# ---------------------------------------------------------------------------
# Tier 3 — the requested strategy, when there is no placed order to read.
# ---------------------------------------------------------------------------
def test_with_no_placed_order_the_requested_strategy_still_decides():
    """The original behaviour, kept as the fallback: a fill reconstructed without
    an order handle falls back to the config. MARKET => taker, maker => maker."""
    class _Bare:
        config: object = None
        order: Optional[NadoOrder] = None

        def __init__(self, strategy=None, crosses=False):
            self.config = type(
                "C", (), {"execution_strategy": strategy, "crosses_book": crosses},
            )()

    was_taker = OrderExecutor._fill_was_taker
    assert was_taker(_Bare(ExecutionStrategy.MARKET)) is True
    assert was_taker(_Bare(ExecutionStrategy.LIMIT_MAKER)) is False
    assert was_taker(_Bare(ExecutionStrategy.LIMIT)) is False
    # A declared crossing intent wins even with no order to inspect.
    assert was_taker(_Bare(ExecutionStrategy.LIMIT, crosses=True)) is True


def test_a_resting_limit_order_is_not_promoted_to_taker():
    """A placed order that really did rest must stay maker even though its type
    is a plain LIMIT — tier 1 answers False rather than falling through to the
    requested-strategy tier, which would read the same LIMIT ambiguously."""
    class _Rested:
        config = type(
            "C", (), {"execution_strategy": ExecutionStrategy.LIMIT,
                      "crosses_book": False},
        )()
        order = NadoOrder(
            id="o1", trading_pair=PAIR, side=TradeType.BUY,
            order_type=OrderType.LIMIT, amount_base=Decimal(1),
            price=Decimal(100),
        )

    assert OrderExecutor._fill_was_taker(_Rested()) is False


def test_a_nested_position_executor_config_still_reports_taker():
    """Delta Neutral's legs are MARKET, but ``PositionExecutorConfig`` holds neither
    ``execution_strategy`` nor ``crosses_book`` — they live on a NESTED
    ``order_config``. Without descending into it, every DN leg of every cycle fell
    through all three tiers and recorded as MAKER."""
    class _Nested:
        order = None

        def __init__(self, strategy):
            inner = type("Inner", (), {"execution_strategy": strategy,
                                       "crosses_book": False})()
            # No execution_strategy on the OUTER config — the DN/desk shape.
            self.config = type("Outer", (), {"order_config": inner})()

    assert OrderExecutor._fill_was_taker(_Nested(ExecutionStrategy.MARKET)) is True
    assert OrderExecutor._fill_was_taker(_Nested(ExecutionStrategy.LIMIT_MAKER)) is False


def test_a_vol_close_leg_is_never_blocked_by_a_size_cap():
    """VOL-EXIT-CAP. Prod session 104 stranded spot: the venue lot-rounds a buy fill
    UP, so a $100 session held $101.01 and the sell's $100 cap rejected it 1.4s
    after the fill. That was patched by padding the cap; the reduce-only exemption
    is the actual fix, and vol's close legs now claim it."""
    from decimal import Decimal as D

    from src.nadobro.engine.risk import ExecutorRequest, RiskEngine
    from src.nadobro.engine.types import RiskLimits, RiskState

    eng = RiskEngine(RiskLimits(max_single_order_quote=D(100),
                                max_position_size_quote=D(100)))
    # The exact shape volume_bot._spawn_order now builds for a CLOSE leg.
    closing = ExecutorRequest(D("101.01"), reduce_only=True,
                              position_action=PositionAction.CLOSE)
    ok, reason = eng.pre_executor_check("vol", closing, RiskState())
    assert ok and reason is None, f"the exit was refused: {reason}"

    # An OPENING leg of the same size is still capped.
    opening = ExecutorRequest(D("101.01"), position_action=PositionAction.OPEN)
    ok, reason = eng.pre_executor_check("vol", opening, RiskState())
    assert not ok and reason == "max_single_order_quote"
