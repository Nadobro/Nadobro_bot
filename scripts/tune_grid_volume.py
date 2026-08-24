"""Definitive grid volume-engine sweep on a real tape (one pair per invocation).

Isolates the volume levers — level count, per-level step, recenter aggressiveness,
and ATR auto-step — to find a config that PROFITS and gives high VOLUME. Net is $
on a $100 margin book; fills = volume proxy.

  .venv/bin/python scripts/tune_grid_volume.py <csv> [nbars]
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from decimal import Decimal  # noqa: E402

from src.nadobro.engine.backtester import SimCosts, candles_from_ohlc, run_backtest  # noqa: E402
from src.nadobro.strategy.engine_runtime import map_strategy_config  # noqa: E402

csv_path = sys.argv[1] if len(sys.argv) > 1 else "btc_perp_aug2026_1m.csv"
nbars = int(sys.argv[2]) if len(sys.argv) > 2 else 8000

with open(csv_path, newline="") as fh:
    rows = list(csv.DictReader(fh))[:nbars]
candles = candles_from_ohlc(rows, interval_s=60)
span_h = (float(candles[-1].ts) - float(candles[0].ts)) / 3600.0
print(f"{os.path.basename(csv_path)}: {len(candles)} bars ~{span_h:.0f}h "
      f"px {float(candles[0].close):.1f}->{float(candles[-1].close):.1f} "
      f"({(float(candles[-1].close)/float(candles[0].close)-1)*100:+.1f}%)")


def bt(**over):
    s = dict(notional_usd=100, leverage=5, **over)
    cfg = map_strategy_config("grid", s, Decimal("100"), product="BTC-PERP")
    r = run_backtest("grid", dict(cfg), candles, costs=SimCosts())
    return float(r.net_pnl), r.fills


# NOTE: the classic grid reads the generic ``spread_bp`` (rgrid/dgrid use their
# prefixed keys; grid does not). ``grid_spread_bp`` is display-only for grid.
configs = [
    ("A baseline (2 lvl, default)",              dict(levels=2)),
    ("B 8 lvl, 10bp, recenter auto",             dict(levels=8, spread_bp=10.0)),
    ("C 8 lvl, 10bp, recenter 0.15%",            dict(levels=8, spread_bp=10.0, grid_reset_threshold_pct=0.15)),
    ("D 8 lvl, 8bp, recenter 0.15%",             dict(levels=8, spread_bp=8.0, grid_reset_threshold_pct=0.15)),
    ("E 8 lvl, 20bp, recenter 0.15%",            dict(levels=8, spread_bp=20.0, grid_reset_threshold_pct=0.15)),
    ("F 12 lvl, 10bp, recenter 0.10%",           dict(levels=12, spread_bp=10.0, grid_reset_threshold_pct=0.10)),
]
print(f"{'config':<40}{'net $':>9}{'fills':>7}")
print("-" * 56)
for label, over in configs:
    try:
        net, fills = bt(**over)
        print(f"{label:<40}{net:>9.3f}{fills:>7}")
    except Exception as e:  # noqa: BLE001
        print(f"{label:<40}  ERROR: {e}")
