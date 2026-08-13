"""Backtester engine — drive a real strategy controller against a candle stream
through the simulated venue, then report NET-of-fees performance.

The controllers need NO changes: they run against any ``NadoAdapterBase``, so the
backtest builds the same controller the live engine builds (via
``engine_runtime.build_controller``) and ticks it once per bar.

Loop per bar (no look-ahead): set the bar → fill resting orders crossed by THIS
bar (orders placed on a prior bar) → tick the controller (it reacts, places/cancels,
polls fills) → accrue one bar of funding → snapshot equity. Orders placed during a
tick therefore cannot fill earlier than the next bar.

Usage::

    from src.nadobro.engine.backtester import run_backtest, SimCosts
    rep = run_backtest("grid", configs, candles, costs=SimCosts(taker_fee=Decimal("0.0006")))
    print(rep.summary())

Implemented in Phase 5 (backtester).
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from src.nadobro.engine.backtester.candle_ingest import Candle
from src.nadobro.engine.backtester.executor_sim import SimCosts, SimMeta, SimNadoAdapter
from src.nadobro.engine.backtester.report import BacktestReport
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.risk import RiskEngine
from src.nadobro.engine.types import RiskLimits, RiskState, _dec

logger = logging.getLogger(__name__)


def _permissive_limits() -> RiskLimits:
    """Limits generous enough that the risk engine never rejects in a backtest —
    we are measuring strategy economics, not re-testing the risk gate (which has
    its own unit tests)."""
    big = Decimal("1000000000")
    return RiskLimits(
        max_open_executors=100000,
        max_single_order_quote=big,
        max_position_size_quote=big,
    )


def _candle_to_dict(c: Candle) -> dict:
    """Shape a sim candle like the LIVE feed's, so routines behave identically.

    ``"time"`` is load-bearing and was missing. ``venue/nado_client`` keys the live
    candle dicts ``"time"``, and ``routines/technical_analysis.chronological()`` —
    the CANDLE-ORDER guard that ``variance_regime`` and ``regime_gate`` run before
    classifying a regime — sorts on ``"time"`` and returns input order untouched
    when it finds none. Emitting only ``"ts"`` therefore made that guard a silent
    no-op in every backtest: the harness could not reproduce a newest-first candle
    bug (the exact defect the 2026-08 R-Grid work fixed), and its dicts diverged
    from the contract every controller reads live. Nothing in src/ consumes ``"ts"``
    from a candle dict; it is kept only so an external caller cannot break.
    """
    return {
        "time": c.ts, "ts": c.ts,
        "open": c.open, "high": c.high, "low": c.low,
        "close": c.close, "volume": c.volume,
    }


class BacktestEngine:
    """Owns one controller + sim adapter + inventory for a single run."""

    def __init__(
        self,
        strategy: str,
        configs: Dict[str, object],
        candles: Sequence[Candle],
        *,
        costs: Optional[SimCosts] = None,
        user_id: int = 1,
        controller_id: str = "bt",
        limits: Optional[RiskLimits] = None,
        meta: Optional[Dict[str, SimMeta]] = None,
    ) -> None:
        if not candles:
            raise ValueError("backtest requires at least one candle")
        self.strategy = strategy
        # A newest-first tape used to run the whole backtest BACKWARDS with no error
        # and no warning — every drift, EMA and trend read inverted, producing a
        # confident, meaningless report. Only candles_from_ohlc sorted; a caller
        # assembling candles by hand (or slicing a live newest-first response) got
        # silent garbage. Sort here and say so loudly, rather than trusting callers.
        self.candles = list(candles)
        if any(
            self.candles[i].ts < self.candles[i - 1].ts
            for i in range(1, len(self.candles))
        ):
            logger.warning(
                "backtest candles were not chronological (%d bars) — sorting ascending "
                "by ts; a newest-first tape would otherwise run the tape backwards",
                len(self.candles),
            )
            self.candles.sort(key=lambda c: c.ts)
        self.user_id = user_id
        self.controller_id = controller_id
        self.adapter = SimNadoAdapter(costs=costs, meta=meta)
        self.inventory = InventoryRepository()
        self.orchestrator = ExecutorOrchestrator(
            risk_engine=RiskEngine(limits or _permissive_limits()),
            risk_state_provider=lambda _cid: RiskState(),
            trade_recorder=None,
        )
        # Serve candle history up to (and including) the current bar so the
        # regime gate / dgrid variance classifier have data to work with.
        self._idx = 0
        cfg = dict(configs)
        # setdefault is NOT enough: map_strategy_config ships the key PRESENT with
        # value None as a placeholder for the live injection in run_engine_cycle
        # (engine_runtime dgrid branch + the mm defaults block). So setdefault never
        # fired, dgrid's _candles() returned [], and it logged "no candles ... cannot
        # classify regime" and HELD whatever phase it started in for the whole run —
        # every dgrid backtest ever produced by this harness measured a stuck-phase
        # ladder, not the phase switcher. Same present-but-None trap as the disarmed
        # SL/TP barrier: a key that exists with a null value is still absent.
        if cfg.get("candle_provider") is None:
            cfg["candle_provider"] = self._candle_provider
        from src.nadobro.strategy.engine_runtime import build_controller

        self.controller = build_controller(
            strategy, user_id=user_id, configs=cfg, orchestrator=self.orchestrator,
            adapter=self.adapter, inventory=self.inventory,
            limits=limits or _permissive_limits(), controller_id=controller_id,
        )

    def _candle_provider(self, _pair: str) -> List[dict]:
        return [_candle_to_dict(c) for c in self.candles[: self._idx + 1]]

    def _equity(self, mark: Decimal) -> Decimal:
        holds = self.inventory.list_for_controller(self.user_id, self.controller_id)
        realized = sum((h.realized_pnl for h in holds), Decimal(0))
        fees = sum((h.cum_fees_quote for h in holds), Decimal(0))
        unreal = sum((h.unrealized_pnl(mark) for h in holds), Decimal(0))
        return realized - fees + unreal + self.adapter.total_funding_quote

    async def _run(self) -> BacktestReport:
        self.adapter.set_candle(self.candles[0])
        await self.orchestrator.spawn_controller(self.controller)
        equity: List[Decimal] = [self._equity(self.candles[0].close)]
        for i in range(1, len(self.candles)):
            self._idx = i
            candle = self.candles[i]
            self.adapter.set_candle(candle)
            self.adapter.match_resting()
            try:
                await self.orchestrator.tick_controller(self.controller.id)
            except Exception:  # noqa: BLE001 - a tick error shouldn't abort the run
                # Consequence: this bar produced no controller action, so the
                # backtest report understates strategy activity for bar ``i``.
                # Surface it (don't abort) so a recurring tick crash is visible.
                logger.warning(
                    "backtest tick_controller failed at bar=%s/%s ts=%s "
                    "(controller=%s) — consequence: no strategy action this bar; "
                    "report may understate activity; continuing run",
                    i, len(self.candles), getattr(candle, "ts", None),
                    self.controller.id, exc_info=True,
                )
            self.adapter.accrue_funding()
            equity.append(self._equity(candle.close))

        last = self.candles[-1].close
        holds = self.inventory.list_for_controller(self.user_id, self.controller_id)
        return BacktestReport(
            strategy=self.strategy,
            bars=len(self.candles),
            realized_pnl=sum((h.realized_pnl for h in holds), Decimal(0)),
            fees=sum((h.cum_fees_quote for h in holds), Decimal(0)),
            funding=self.adapter.total_funding_quote,
            final_unrealized=sum((h.unrealized_pnl(last) for h in holds), Decimal(0)),
            orders_placed=self.adapter._counter,  # noqa: SLF001
            fills=len(self.adapter._fills),        # noqa: SLF001
            equity_curve=equity,
        )

    def run(self) -> BacktestReport:
        return asyncio.run(self._run())


def run_backtest(
    strategy: str,
    configs: Dict[str, object],
    candles: Sequence[Candle],
    *,
    costs: Optional[SimCosts] = None,
    user_id: int = 1,
    controller_id: str = "bt",
    limits: Optional[RiskLimits] = None,
    meta: Optional[Dict[str, SimMeta]] = None,
) -> BacktestReport:
    """Build the strategy's real controller, run it against ``candles`` through
    the cost-aware sim, and return a :class:`BacktestReport`."""
    return BacktestEngine(
        strategy, configs, candles, costs=costs, user_id=user_id,
        controller_id=controller_id, limits=limits, meta=meta,
    ).run()
