"""Backtester trigger-sim: the SimNadoAdapter fires venue price-trigger orders on
a bar cross and books the fills into inventory, so the ReverseGridController can be
validated end-to-end through run_backtest.

Two layers:
  * SimNadoAdapter mechanics — a trigger fires when the bar's RANGE crosses its
    level (BUY on high>=level, SELL on low<=level), fills at its level + taker
    slippage, books into inventory, moves held_base, and a reduce-only stop clamps
    to the position; cancel prevents firing; a dependent arms only after its parent.
  * A full run_backtest("revgrid", ...) on synthetic trend tapes — the reverse grid
    goes with the trend, fills reach inventory (fee_leak == 0), and the report is
    coherent.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.nadobro.engine.backtester import (
    BacktestEngine,
    Candle,
    SimCosts,
    SimNadoAdapter,
    candles_from_ohlc,
    run_backtest,
)
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import TradeType, _dec

PAIR = "BTC-PERP"


def _bar(o, h, l, c, ts=0):
    return Candle(ts=ts, open=_dec(o), high=_dec(h), low=_dec(l), close=_dec(c))


def _sim():
    inv = InventoryRepository()
    a = SimNadoAdapter(costs=SimCosts(), inventory=inv, user_id=1, controller_id="bt")
    return a, inv


# ── SimNadoAdapter trigger mechanics ───────────────────────────────────

def test_buy_trigger_fires_on_bar_high_and_books_into_inventory():
    async def body():
        a, inv = _sim()
        await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("101"))
        a.set_candle(_bar(100, 100.5, 99.5, 100.4))     # high 100.5 < 101 → no fire
        a.match_triggers()
        assert await a.held_base(PAIR) == 0
        a.set_candle(_bar(100.4, 101.2, 100.3, 101.0))  # high 101.2 >= 101 → fires
        a.match_triggers()
        assert await a.held_base(PAIR) == Decimal("1")   # long 1
        hold = inv.get(1, PAIR, "bt")
        assert hold.net_amount_base == Decimal("1")
        assert hold.cum_fees_quote > 0                    # taker fee charged
        # every charged fee reached inventory (no fee leak on the trigger path)
        assert abs(a.total_fees_quote - hold.cum_fees_quote) < Decimal("1e-12")

    asyncio.run(body())


def test_sell_trigger_fires_on_bar_low():
    async def body():
        a, inv = _sim()
        await a.place_trigger_order(PAIR, TradeType.SELL, Decimal("1"), Decimal("99"))
        a.set_candle(_bar(100, 100.5, 98.5, 99.2))       # low 98.5 <= 99 → fires
        a.match_triggers()
        assert await a.held_base(PAIR) == Decimal("-1")  # short 1

    asyncio.run(body())


def test_reduce_only_stop_flattens_a_long_and_cannot_flip():
    async def body():
        a, inv = _sim()
        # open a long of 1
        await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("101"))
        a.set_candle(_bar(100, 101.5, 100, 101))
        a.match_triggers()
        assert await a.held_base(PAIR) == Decimal("1")
        # a long's stop (close SELL) sized ABOVE the position must not over-close
        await a.place_stop_order(PAIR, Decimal("5"), Decimal("98"), position_is_long=True)
        a.set_candle(_bar(101, 101, 97, 97.5))           # low 97 <= 98 → stop fires
        a.match_triggers()
        assert await a.held_base(PAIR) == 0               # flat, not flipped short
        hold = inv.get(1, PAIR, "bt")
        assert hold.realized_pnl != 0                     # the round trip realized PnL

    asyncio.run(body())


def test_cancel_trigger_prevents_firing():
    async def body():
        a, _ = _sim()
        o = await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("101"))
        assert await a.cancel_trigger_order(o.id) is True
        a.set_candle(_bar(100, 105, 100, 104))           # would have crossed 101
        a.match_triggers()
        assert await a.held_base(PAIR) == 0
        assert await a.cancel_trigger_order(o.id) is False  # idempotent

    asyncio.run(body())


def test_dependent_rung_arms_only_after_parent_fires():
    async def body():
        a, _ = _sim()
        parent = await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("101"))
        await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("102"),
                                    dependency=parent.id)
        # A bar that spans both levels fires only the armed parent this bar.
        a.set_candle(_bar(100, 103, 100, 102.5))
        a.match_triggers()
        assert await a.held_base(PAIR) == Decimal("1")
        # Next bar, still above 102, the now-armed dependent fires.
        a.set_candle(_bar(102.5, 103, 102, 102.8))
        a.match_triggers()
        assert await a.held_base(PAIR) == Decimal("2")

    asyncio.run(body())


# ── end-to-end run_backtest("revgrid", ...) ────────────────────────────

def _trend_tape(start: float, end: float, bars: int):
    """A monotonic price path as OHLC bars (high/low span open→close)."""
    rows = []
    prev = start
    for i in range(bars):
        frac = i / (bars - 1)
        c = start + (end - start) * frac
        o = prev
        rows.append({
            "ts": i * 60, "open": o, "high": max(o, c), "low": min(o, c), "close": c,
        })
        prev = c
    return candles_from_ohlc(rows, interval_s=60)


def _configs(**over):
    cfg = {
        "trading_pair": PAIR,
        "levels": 3,
        "step_pct": Decimal("0.005"),
        "order_amount_quote": Decimal("50"),
    }
    cfg.update(over)
    return cfg


def test_revgrid_backtest_goes_long_in_an_uptrend_no_fee_leak():
    candles = _trend_tape(100.0, 112.0, 40)
    eng = BacktestEngine("revgrid", _configs(), candles, costs=SimCosts())
    rep = eng.run()
    # The sim booked every trigger fill into inventory — the report is not blind.
    assert abs(eng.fee_leak_quote) < Decimal("1e-9")
    assert rep.orders_placed > 0 and rep.fills > 0
    # A reverse grid rode the uptrend long and is in profit at the tape's end.
    assert rep.final_unrealized > 0
    # Report coherence: net = realized - fees + funding + unrealized.
    assert (rep.net_pnl
            == rep.realized_pnl - rep.fees + rep.funding + rep.final_unrealized)


def test_revgrid_backtest_goes_short_in_a_downtrend():
    candles = _trend_tape(100.0, 90.0, 40)
    eng = BacktestEngine("revgrid", _configs(), candles, costs=SimCosts())
    rep = eng.run()
    assert abs(eng.fee_leak_quote) < Decimal("1e-9")
    assert rep.fills > 0
    # The short leg profits as price falls: an open short in a downtrend is in profit.
    assert rep.final_unrealized > 0


def test_revgrid_backtest_runs_via_run_backtest_entrypoint():
    candles = _trend_tape(100.0, 108.0, 30)
    rep = run_backtest("revgrid", _configs(), candles, costs=SimCosts())
    assert rep.strategy == "revgrid"
    assert rep.fills > 0
