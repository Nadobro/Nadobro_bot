#!/usr/bin/env python3
"""Validate the grid-family strategies on a REAL price tape (OHLC or trades CSV).

This is the Phase-5 acceptance instrument: run grid / rgrid / dgrid through the
real controllers on the cost-aware backtester (honest fills + all-in fees) over a
real Nado tape, and report net PnL / fills for the current defaults and the key
variants. Net is $ on a $100 margin book.

Two input kinds:
  --kind ohlc    an OHLC CSV (time,open,high,low,close[,volume]) — what
                 scripts/fetch_backtest_candles.py produces.
  --kind trades  a Nado trade/fill export (Time,Market,Price,Amount,...) resampled
                 to OHLC; pass --market to pick the symbol and --interval seconds.

Example:
  .venv/bin/python scripts/backtest_real_tape.py \
      --csv btc_perp_aug2026_1m.csv --kind ohlc --interval 60
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decimal import Decimal  # noqa: E402

from src.nadobro.engine.backtester import (  # noqa: E402
    SimCosts,
    candles_from_ohlc,
    resample_trades_csv,
    run_backtest,
)
from src.nadobro.strategy.engine_runtime import map_strategy_config  # noqa: E402


def _load(path: str, kind: str, market: str | None, interval: float):
    if kind == "trades":
        return resample_trades_csv(path, interval_s=interval, market=market)
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    return candles_from_ohlc(rows, interval_s=interval)


def _bt(strategy, settings, candles, product):
    cfg = map_strategy_config(strategy, settings, Decimal("100"), product=product)
    rep = run_backtest(strategy, dict(cfg), candles, costs=SimCosts())
    return float(rep.net_pnl), rep.fills, rep.orders_placed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--kind", choices=["ohlc", "trades"], default="ohlc")
    ap.add_argument("--market", default=None, help="trades kind: symbol filter (e.g. WTI)")
    ap.add_argument("--interval", type=float, default=60.0, help="bar seconds")
    ap.add_argument("--product", default="BTC-PERP", help="product passed to the mapper")
    ap.add_argument("--leverage", type=int, default=5)
    args = ap.parse_args()

    candles = _load(args.csv, args.kind, args.market, args.interval)
    if len(candles) < 30:
        sys.exit(f"only {len(candles)} candles — need a longer tape to be meaningful")
    span_h = (float(candles[-1].ts) - float(candles[0].ts)) / 3600.0
    print(f"tape: {len(candles)} bars, ~{span_h:.1f}h, "
          f"px {float(candles[0].close):.4f} -> {float(candles[-1].close):.4f}\n")

    base = dict(notional_usd=100, leverage=args.leverage, levels=4)
    rg = dict(base, rgrid_spread_bp=10.0, rgrid_stop_loss_pct=1.0, rgrid_reset_threshold_pct=0.2)

    variants = [
        ("grid            (default MR)",   "grid",  dict(base)),
        ("rgrid  gate ON  (P1 default)",   "rgrid", dict(rg)),
        ("rgrid  gate OFF (pre-fix)",      "rgrid", dict(rg, rgrid_chop_stand_down=0)),
        ("rgrid  gate ON + pyramid opt-in","rgrid", dict(rg, max_net_exposure_pct=100)),
        ("dgrid  trend OFF (P0 default)",  "dgrid", dict(base)),
        ("dgrid  trend ON  (pre-fix)",     "dgrid", dict(base, dgrid_trend_follow=1)),
    ]
    print(f"{'variant':<34}{'net $':>10}{'fills':>8}{'orders':>8}")
    print("-" * 60)
    for label, strat, settings in variants:
        try:
            net, fills, orders = _bt(strat, settings, candles, args.product)
            print(f"{label:<34}{net:>10.3f}{fills:>8}{orders:>8}")
        except Exception as e:  # noqa: BLE001
            print(f"{label:<34}  ERROR: {e}")


if __name__ == "__main__":
    main()
