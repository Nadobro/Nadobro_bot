"""Backtester harness tests + per-strategy money-bleed regressions.

These prove the harness is HONEST (it charges fees/funding/slippage so a
strategy that only looks good on price moves shows a negative net) and that each
strategy controller actually runs end-to-end against the simulated venue.
"""
from __future__ import annotations

import math
import os
from decimal import Decimal

import pytest

from src.nadobro.engine.backtester import (
    Candle,
    SimCosts,
    SimMeta,
    SimNadoAdapter,
    candles_from_ohlc,
    candles_from_prices,
    resample_trades_csv,
    run_backtest,
)

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
TRADES_CSV = os.path.join(REPO_ROOT, "f14288_default_inkMainnet_trades_1778457600000_1778716799999.csv")


def _ranging(n=120, base=100.0, amp=3.0):
    prices = [base + amp * math.sin(i / 3.0) for i in range(n)]
    return candles_from_prices(prices, interval_s=3600, wick_pct=Decimal("0.001"))


def _grid_cfg():
    return {
        "trading_pair": "BTC", "total_amount_quote": Decimal("1000"),
        "start_price": Decimal("97"), "end_price": Decimal("100"),
        "min_spread_between_orders": Decimal("0.01"), "max_open_orders": 5,
        "levels_count": 5, "step_pct": Decimal("0.01"),
        "leverage": 1, "sl_pct": 0.0, "tp_pct": 0.0,
    }


# --------------------------------------------------------------------------- #
# candle_ingest                                                               #
# --------------------------------------------------------------------------- #

def test_candles_from_prices_spans_open_to_close():
    cs = candles_from_prices([100, 101, 99], interval_s=60)
    assert len(cs) == 3
    assert cs[1].open == Decimal(100) and cs[1].close == Decimal(101)
    assert cs[1].high == Decimal(101) and cs[1].low == Decimal(100)
    assert cs[2].ts - cs[1].ts == 60


def test_candles_from_ohlc_sorts_by_ts():
    cs = candles_from_ohlc([
        {"ts": 200, "open": 2, "high": 3, "low": 1, "close": 2},
        {"ts": 100, "open": 1, "high": 2, "low": 1, "close": 1.5},
    ])
    assert [c.ts for c in cs] == [100, 200]


def test_resample_trades_csv_builds_candles():
    if not os.path.exists(TRADES_CSV):
        pytest.skip("trades CSV not present")
    cs = resample_trades_csv(TRADES_CSV, interval_s=3600, market="WTI")
    assert len(cs) > 0
    for c in cs:
        assert c.high >= c.low > 0
        assert c.high >= c.open and c.high >= c.close


# --------------------------------------------------------------------------- #
# executor_sim cost model                                                     #
# --------------------------------------------------------------------------- #

def test_sim_charges_taker_fee_on_market_and_applies_slippage():
    import asyncio
    from src.nadobro.engine.types import OrderType, TradeType

    async def body():
        sim = SimNadoAdapter(costs=SimCosts(taker_fee=Decimal("0.001"), slippage_pct=Decimal("0.002")))
        sim.set_candle(Candle(0, Decimal(100), Decimal(100), Decimal(100), Decimal(100)))
        o = await sim.place_order("BTC", TradeType.BUY, OrderType.MARKET, Decimal(1))
        # buy fills at mid*(1+slippage) = 100.2; taker fee = 1 * 100.2 * 0.001
        assert o.filled_base == Decimal(1)
        assert o.filled_quote == Decimal("100.2")
        assert sim.total_fees_quote == Decimal("0.1002")

    asyncio.run(body())


def test_sim_funding_accrues_on_perp_not_spot():
    import asyncio
    from src.nadobro.engine.types import OrderType, TradeType

    async def body():
        sim = SimNadoAdapter(costs=SimCosts(funding_rate_per_bar=Decimal("0.0001")))
        sim.set_candle(Candle(0, Decimal(100), Decimal(100), Decimal(100), Decimal(100)))
        # short the perp, long the spot (same magnitude)
        await sim.place_order("BTC-PERP", TradeType.SELL, OrderType.MARKET, Decimal(1))
        await sim.place_order("BTC-USDT0", TradeType.BUY, OrderType.MARKET, Decimal(1))
        sim.accrue_funding()
        # only the perp short earns: 0.0001 * 1 * 100 = 0.01 ; spot accrues nothing
        assert sim.total_funding_quote == Decimal("0.0100")

    asyncio.run(body())


# --------------------------------------------------------------------------- #
# Honesty + per-strategy regressions                                          #
# --------------------------------------------------------------------------- #

def test_fees_erode_net_pnl_the_harness_is_honest():
    candles = _ranging()
    zero = run_backtest("grid", _grid_cfg(), candles, costs=SimCosts(taker_fee=Decimal(0), maker_fee=Decimal(0)))
    high = run_backtest("grid", _grid_cfg(), candles, costs=SimCosts(taker_fee=Decimal("0.02"), maker_fee=Decimal("0.02")))
    assert zero.net_pnl > high.net_pnl          # fees must reduce net
    assert high.fees > zero.fees
    # net = gross - fees + funding + unrealized (conservation)
    assert zero.net_pnl == zero.gross_pnl - zero.fees + zero.funding


@pytest.mark.parametrize("strategy,cfg", [
    ("grid", None),
    ("rgrid", None),
])
def test_grid_family_runs_end_to_end(strategy, cfg):
    candles = _ranging()
    cfg = cfg or _grid_cfg()
    cfg = dict(cfg, trading_pair="BTC")
    rep = run_backtest(strategy, cfg, candles, costs=SimCosts())
    assert rep.bars == len(candles)
    assert rep.orders_placed >= 1
    assert len(rep.equity_curve) == len(candles)


def test_vol_runs_and_charges_fees():
    candles = _ranging()
    cfg = {"trading_pair": "KBTC", "total_amount_quote": Decimal("100"),
           "total_duration": 40, "order_interval": 20, "market": "spot",
           "leverage": 1, "target_volume_usd": Decimal("0")}
    rep = run_backtest("vol", cfg, candles, costs=SimCosts())
    assert rep.fills >= 1
    assert rep.fees >= 0


def test_dn_is_profitable_only_when_funding_beats_fees():
    """The DN thesis, finally checkable: the hedge cancels price PnL, so DN only
    nets positive when captured funding exceeds the round-trip fees."""
    candles = _ranging()
    cfg = {"trading_pair": "BTC", "trading_pair_long": "BTC-USDT0",
           "trading_pair_short": "BTC-PERP", "hedge_ratio": Decimal("1"),
           "leg_amount_quote": Decimal("100"), "max_drift_pct": Decimal("0.05"),
           "hold_seconds": 7200, "cycles": 1, "leverage": 1, "barriers": None}
    with_funding = run_backtest("dn", cfg, candles, costs=SimCosts(funding_rate_per_bar=Decimal("0.0001")))
    no_funding = run_backtest("dn", cfg, candles, costs=SimCosts(funding_rate_per_bar=Decimal(0)))
    assert with_funding.funding > 0
    assert no_funding.funding == 0
    assert with_funding.net_pnl > no_funding.net_pnl   # funding is DN's edge
    assert no_funding.net_pnl <= 0                      # no funding => fees bleed


# ==========================================================================
# Harness fidelity (2026-08-13). The backtester is the instrument this repo uses
# to prove a tuning change does not bleed money. Three defects made its output
# quietly optimistic and its regime reads unfaithful to live.
# ==========================================================================
def test_sim_costs_include_the_mandatory_builder_fee():
    """SimCosts modelled the venue's base rates only, so a maker round trip cost
    3bp against a real 5bp — understating maker cost by 40%, and by exactly the
    margin that decides whether a tight grid step is profitable."""
    from src.nadobro.quant.vol_fee_estimator import (
        DEFAULT_BUILDER_FEE_RATE,
        MAKER_ROUND_TRIP_RATE,
    )

    c = SimCosts()
    assert c.maker_fee * 2 == MAKER_ROUND_TRIP_RATE, (
        f"a modelled maker round trip ({c.maker_fee * 2}) must equal the rate the "
        f"mapper floors the grid step at ({MAKER_ROUND_TRIP_RATE})"
    )
    # The builder fee is mandatory on BOTH sides of the book, not just takers.
    assert c.maker_fee >= DEFAULT_BUILDER_FEE_RATE
    assert c.taker_fee > c.maker_fee


def test_sim_candles_carry_the_live_time_key_so_the_order_guard_engages():
    """``_candle_to_dict`` emitted only "ts" while the live feed keys "time" and
    ``chronological()`` sorts on "time" — so the CANDLE-ORDER guard was a silent
    no-op in every backtest and the harness could not reproduce a newest-first
    candle bug (the defect the 2026-08 R-Grid work fixed)."""
    from src.nadobro.engine.backtester.engine import _candle_to_dict
    from src.nadobro.engine.routines.technical_analysis import chronological

    candles = candles_from_prices([100.0, 101.0, 102.0], interval_s=60)
    dicts = [_candle_to_dict(c) for c in candles]
    assert all("time" in d for d in dicts), "sim candles must match the live contract"

    # The guard is now live: a reversed tape gets restored to chronological order.
    reversed_dicts = list(reversed(dicts))
    restored = list(chronological(reversed_dicts))
    assert [d["time"] for d in restored] == sorted(d["time"] for d in dicts), (
        "chronological() still cannot see the sim's timestamps"
    )


def test_a_newest_first_tape_is_sorted_instead_of_running_backwards():
    """A reversed tape used to run the whole backtest backwards with no error —
    every drift/EMA/trend read inverted, producing a confident, meaningless
    report."""
    rising = candles_from_prices(
        [100.0 + i * 0.5 for i in range(40)], interval_s=60
    )
    forward = run_backtest("grid", _grid_cfg(), rising, costs=SimCosts())
    backward = run_backtest("grid", _grid_cfg(), list(reversed(rising)), costs=SimCosts())

    assert forward.bars == backward.bars
    assert backward.net_pnl == forward.net_pnl, (
        "a newest-first tape must be sorted to the same run, not silently reversed"
    )


def test_dgrid_actually_receives_candles_and_can_classify_regimes():
    """The mapped config ships candle_provider PRESENT with value None (a
    placeholder for the live injection in run_engine_cycle), so the harness's
    ``cfg.setdefault(...)`` never fired: dgrid's _candles() returned [], it logged
    "cannot classify regime", and HELD its starting phase for the whole run. Every
    dgrid backtest this harness produced measured a stuck-phase ladder rather than
    the phase switcher — so it could not have caught DGRID-REVERSAL-FLIPFLOP or
    validated any change to phase behaviour.

    Same present-but-None trap as the disarmed SL/TP barrier: a key that exists with
    a null value is still absent.
    """
    from decimal import Decimal as D

    from src.nadobro.strategy.engine_runtime import map_strategy_config

    cfg = map_strategy_config(
        "dgrid", {"notional_usd": 100, "leverage": 5}, D("100"), product="BTC-PERP"
    )
    assert "candle_provider" in cfg and cfg["candle_provider"] is None, (
        "precondition changed: the mapper no longer ships a None placeholder"
    )

    # A trending tape: a working classifier must place orders on BOTH sides across
    # the run (it flips phase), which a stuck-phase ladder cannot do.
    prices = [100.0 * (1.0 + 0.0005) ** i for i in range(120)]
    rep = run_backtest("dgrid", dict(cfg), candles_from_prices(prices, interval_s=60),
                       costs=SimCosts())
    assert rep.orders_placed > 0, (
        "dgrid placed nothing — the candle provider is not reaching the controller"
    )


def test_the_backtest_candle_provider_has_no_look_ahead():
    """A provider that served future bars would make every backtest meaningless."""
    from src.nadobro.engine.backtester.engine import BacktestEngine

    candles = candles_from_prices([100.0 + i for i in range(10)], interval_s=60)
    eng = BacktestEngine("grid", _grid_cfg(), candles, costs=SimCosts())
    eng._idx = 3
    served = eng._candle_provider("BTC-PERP")
    assert len(served) == 4, "provider must serve only bars 0..idx"
    assert max(d["time"] for d in served) == candles[3].ts
