"""Deterministic decision scenarios for the grid family (Grid classic, Grid
fill-anchored, D-Grid, R-Grid).

Each scenario drives one controller through a fixed mid tape on the
MockNadoAdapter — filling resting makers the tape crosses and firing venue
triggers — under a FAKE wall clock, and returns the complete, ordered log of
venue calls the controller made (place / cancel / trigger / stop). The log is
the controller's DECISIONS.

``tests/engine/fixtures/gvol_off_golden.json`` holds the logs these scenarios
produced on the base branch (``claude/grid-family-bugfixes`` @ 2cd9a62, before
the vol model existed). ``test_gvol_controllers.py`` replays them with the vol
model absent AND with every ``gvol_*`` key explicitly OFF and requires the logs
to match byte for byte — the proof that the opt-in model changes nothing while
it is off. Regenerate ONLY from a base-branch checkout:

    PYTHONPATH=<base checkout> python tests/engine/gvol_scenarios.py > golden.json
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.adapter.base import OrderState
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import OrderType, TradeType

PAIR = "BTC-PERP"
T0 = 1_790_000_000.0          # fake wall clock origin (minute-aligned)
TICK_S = 20.0

TAPE = [
    "100", "99.92", "99.8", "99.66", "99.5", "99.62", "99.9", "100.1", "100.25",
    "100.05", "99.85", "99.6", "99.4", "99.2", "99.45", "99.8", "100.2", "100.45",
    "100.3", "100.0", "99.75", "99.55", "99.7", "99.95",
]


def _q(v: Any, places: str = "1e-8") -> Optional[str]:
    if v is None:
        return None
    return format(Decimal(str(v)).quantize(Decimal(places)), "f")


class RecordingAdapter(MockNadoAdapter):
    """MockNadoAdapter that logs every venue-side decision in order."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.log: List[list] = []

    async def place_order(self, trading_pair, side, order_type, amount_base, price=None,
                          leverage=1, reduce_only=False):
        o = await super().place_order(trading_pair, side, order_type, amount_base, price,
                                      leverage, reduce_only)
        self.log.append(["place", o.id, side.name, order_type.name, _q(amount_base),
                         _q(price), bool(reduce_only)])
        return o

    async def cancel_order(self, order_id):
        r = await super().cancel_order(order_id)
        self.log.append(["cancel", order_id, bool(r)])
        return r

    async def cancel_and_place(self, cancel_order_id, trading_pair, side, order_type,
                               amount_base, price, leverage=1, reduce_only=False):
        o = await super().cancel_and_place(cancel_order_id, trading_pair, side, order_type,
                                           amount_base, price, leverage, reduce_only)
        self.log.append(["cancel_and_place", cancel_order_id, o.id, side.name,
                         order_type.name, _q(amount_base), _q(price), bool(reduce_only)])
        return o

    async def place_trigger_order(self, trading_pair, side, amount_base, trigger_price, *,
                                  slippage_pct=0.5, dependency=None, order_type="ioc"):
        o = await super().place_trigger_order(trading_pair, side, amount_base, trigger_price,
                                              slippage_pct=slippage_pct, dependency=dependency,
                                              order_type=order_type)
        self.log.append(["trigger", o.id, side.name, _q(amount_base), _q(trigger_price),
                         str(order_type)])
        return o

    async def place_stop_order(self, trading_pair, close_size, stop_price, position_is_long, *,
                               slippage_pct=0.5):
        o = await super().place_stop_order(trading_pair, close_size, stop_price,
                                           position_is_long, slippage_pct=slippage_pct)
        self.log.append(["stop", o.id, _q(close_size), _q(stop_price), bool(position_is_long)])
        return o

    async def cancel_trigger_order(self, order_id):
        r = await super().cancel_trigger_order(order_id)
        self.log.append(["cancel_trigger", order_id, bool(r)])
        return r

    # -- tape helpers -------------------------------------------------------
    def cross_resting(self, mid: Decimal) -> None:
        """Fill every resting limit the mid has crossed (the venue matching)."""
        for oid, order in list(self._orders.items()):
            if order.state.is_terminal or order.price is None:
                continue
            if order.order_type not in (OrderType.LIMIT_MAKER, OrderType.LIMIT):
                continue
            if order.side is TradeType.BUY and mid <= order.price:
                self.fill_order(oid)
                self.log.append(["fill", oid])
            elif order.side is TradeType.SELL and mid >= order.price:
                self.fill_order(oid)
                self.log.append(["fill", oid])


class FakeClock:
    def __init__(self, start: float = T0) -> None:
        self.now = start
        self._orig: Optional[Callable[[], float]] = None

    def __enter__(self) -> "FakeClock":
        self._orig = time.time
        time.time = lambda: self.now  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        if self._orig is not None:
            time.time = self._orig  # type: ignore[assignment]


def synthetic_candles(now: float, *, n: int = 200, amp_bp: float = 2.0, px: float = 100.0,
                      include_open: bool = True) -> List[dict]:
    """1m candles ending with the in-progress bar at ``now``: alternating +/-amp
    log returns (rv == amp exactly), newest FIRST like the raw indexer."""
    cur_open = int(now // 60) * 60
    closes = [px]
    for i in range(n):
        closes.append(closes[-1] * math.exp((amp_bp if i % 2 == 0 else -amp_bp) / 1e4))
    rows = []
    for k in range(n):
        t = cur_open - 60 * (n - k) + (60 if include_open else 0)
        rows.append({"time": t, "close": closes[k + 1]})
    return list(reversed(rows))


# ---------------------------------------------------------------- configs
def grid_cfg(extra: Optional[dict] = None) -> dict:
    step = Decimal("0.0008")
    levels = 5
    mid = Decimal("100")
    span = step * (levels - 1)
    off = max(step / 2, Decimal("0.00015"))
    cfg: Dict[str, Any] = {
        "trading_pair": PAIR,
        "start_price": mid * (1 - off - span), "end_price": mid * (1 - off),
        "limit_price": Decimal(0), "total_amount_quote": Decimal(1000),
        "min_spread_between_orders": step, "max_open_orders": levels,
        "step_pct": step, "levels_count": levels, "recycle_levels": True,
        "leverage": 1, "margin_quote": Decimal(1000), "max_net_exposure_pct": 30.0,
        "regime_gate_enabled": 0.0, "reset_threshold_bp": 0.0,
    }
    cfg.update(extra or {})
    return cfg


def fa_cfg(extra: Optional[dict] = None) -> dict:
    cfg: Dict[str, Any] = {
        "trading_pair": PAIR, "anchor_mode": "grid",
        "spread_bid_pct": Decimal("0.0008"), "spread_ask_pct": Decimal("0.0008"),
        "order_amount_quote": Decimal(300), "ladder_levels": 3, "ladder_step_bp": Decimal(8),
        "margin_quote": Decimal(1000), "max_net_exposure_pct": 30.0,
        "reset_threshold_pct": Decimal("0.003"), "price_distance_tolerance": Decimal("0.0001"),
        "concession_enabled": True, "concession_escalation_ticks": 3, "leverage": 1,
        "regime_gate_enabled": 0.0,
    }
    cfg.update(extra or {})
    return cfg


def dgrid_cfg(extra: Optional[dict] = None) -> dict:
    step = Decimal("0.0008")
    cfg: Dict[str, Any] = {
        "trading_pair": PAIR, "start_price": Decimal("99"), "end_price": Decimal("100"),
        "limit_price": Decimal(0), "total_amount_quote": Decimal(1000),
        "min_spread_between_orders": step, "max_open_orders": 5, "step_pct": step,
        "levels_count": 5, "recycle_levels": True, "leverage": 1,
        "margin_quote": Decimal(1000), "max_net_exposure_pct": 30.0,
        "regime_gate_enabled": 0.0, "dgrid_trend_follow": False, "tp_pct": 0.0,
    }
    cfg.update(extra or {})
    return cfg


def rgrid_cfg(extra: Optional[dict] = None) -> dict:
    cfg: Dict[str, Any] = {
        "trading_pair": PAIR, "levels": 3, "step_pct": Decimal("0.003"),
        "order_amount_quote": Decimal("100"), "revgrid_chop_stand_down": False,
    }
    cfg.update(extra or {})
    return cfg


# ---------------------------------------------------------------- runners
def _candle_provider(clock: FakeClock, amp_bp: float) -> Callable[[str], Any]:
    async def provider(_pair: str) -> list:
        return synthetic_candles(clock.now, amp_bp=amp_bp)
    return provider


async def _drive(adapter: RecordingAdapter, clock: FakeClock, tick: Callable[[], Any],
                 *, triggers: bool = False, tape: Optional[List[str]] = None) -> None:
    for px in (tape or TAPE):
        mid = Decimal(px)
        adapter.set_mid(mid)
        if triggers:
            adapter.cross_triggers(mid)
        adapter.cross_resting(mid)
        clock.now += TICK_S
        await tick()


def run_grid(extra: Optional[dict] = None, *, amp_bp: float = 2.0,
             tape: Optional[List[str]] = None) -> List[list]:
    from src.nadobro.engine.controllers.grid_trading import GridController

    async def body() -> List[list]:
        with FakeClock() as clock:
            adapter = RecordingAdapter(mid=Decimal("100"), auto_fill_market=True)
            orch = ExecutorOrchestrator()
            cfg = grid_cfg(extra)
            cfg.setdefault("candle_provider", _candle_provider(clock, amp_bp))
            c = GridController(user_id=1, orchestrator=orch, adapter=adapter,
                               inventory=InventoryRepository(), configs=cfg, controller_id="G")
            await orch.spawn_controller(c)
            await _drive(adapter, clock, lambda: orch.tick_controller(c.id), tape=tape)
            return adapter.log
    return asyncio.run(body())


def run_fa(extra: Optional[dict] = None, *, amp_bp: float = 2.0,
           tape: Optional[List[str]] = None) -> List[list]:
    from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController

    async def body() -> List[list]:
        with FakeClock() as clock:
            adapter = RecordingAdapter(mid=Decimal("100"), auto_fill_market=True)
            orch = ExecutorOrchestrator()
            cfg = fa_cfg(extra)
            cfg.setdefault("candle_provider", _candle_provider(clock, amp_bp))
            c = FillAnchoredQuotingController(user_id=1, orchestrator=orch, adapter=adapter,
                                              inventory=InventoryRepository(), configs=cfg,
                                              controller_id="FA")
            await orch.spawn_controller(c)
            await _drive(adapter, clock, lambda: orch.tick_controller(c.id), tape=tape)
            return adapter.log
    return asyncio.run(body())


def run_dgrid(extra: Optional[dict] = None, *, amp_bp: float = 2.0,
              tape: Optional[List[str]] = None) -> List[list]:
    from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController

    async def body() -> List[list]:
        with FakeClock() as clock:
            adapter = RecordingAdapter(mid=Decimal("100"), auto_fill_market=True,
                                       venue_held={PAIR: Decimal(0)})
            orch = ExecutorOrchestrator()
            cfg = dgrid_cfg(extra)
            cfg.setdefault("candle_provider", _candle_provider(clock, amp_bp))
            c = DynamicGridController(user_id=1, orchestrator=orch, adapter=adapter,
                                      inventory=InventoryRepository(), configs=cfg,
                                      controller_id="D")
            await orch.spawn_controller(c)
            await _drive(adapter, clock, lambda: orch.tick_controller(c.id), tape=tape)
            return adapter.log
    return asyncio.run(body())


def run_rgrid(extra: Optional[dict] = None, *, amp_bp: float = 2.0,
              tape: Optional[List[str]] = None) -> List[list]:
    from src.nadobro.engine.controllers.reverse_grid import ReverseGridController

    async def body() -> List[list]:
        with FakeClock() as clock:
            adapter = RecordingAdapter(mid=Decimal("100"), lot=Decimal("0.0001"),
                                       venue_held={PAIR: Decimal(0)})
            cfg = rgrid_cfg(extra)
            cfg.setdefault("candle_provider", _candle_provider(clock, amp_bp))
            c = ReverseGridController(user_id=1, orchestrator=object(), adapter=adapter,
                                      inventory=None, configs=cfg)
            await c.on_start()
            tape_rg = tape or ["100", "100.2", "100.35", "100.7", "101.0", "100.8", "100.4",
                               "100.0", "99.6", "99.2", "98.9", "99.3", "99.8", "100.1"]
            await _drive(adapter, clock, c.on_tick, triggers=True, tape=tape_rg)
            return adapter.log
    return asyncio.run(body())


SCENARIOS = {
    "grid": run_grid,
    "grid_fill_anchored": run_fa,
    "dgrid": run_dgrid,
    "rgrid": run_rgrid,
}


if __name__ == "__main__":
    out = {name: fn() for name, fn in SCENARIOS.items()}
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
