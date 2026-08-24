"""Find an R-Grid config that both PROFITS and generates VOLUME on a real trend.

Profit needs exit widths wider than intraday noise (so the trend isn't shaken
out); volume needs PYRAMIDING adds (cross-add fires on momentum, higher exposure
lets multiple rungs fit). This sweeps the combinations on a real tape.
Net is $ on a $100 margin book; fills = trade count (volume proxy).
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
print(f"tape: {len(candles)} bars ~{span_h:.0f}h "
      f"px {float(candles[0].close):.0f}->{float(candles[-1].close):.0f} "
      f"({(float(candles[-1].close)/float(candles[0].close)-1)*100:+.1f}%)\n")

base = dict(notional_usd=100, leverage=5, levels=4, rgrid_spread_bp=10.0)


def bt(**over):
    s = dict(base, **over)
    cfg = map_strategy_config("rgrid", s, Decimal("100"), product="BTC-PERP")
    r = run_backtest("rgrid", dict(cfg), candles, costs=SimCosts())
    return float(r.net_pnl), r.fills, r.orders_placed


configs = [
    ("default (gate ON, maker, 30%, reset .2%)",
     dict(rgrid_reset_threshold_pct=0.2, rgrid_stop_loss_pct=1.0)),
    ("wide exit (reset 1%, maker, 30%)",
     dict(rgrid_reset_threshold_pct=1.0, rgrid_stop_loss_pct=3.0)),
    ("wide + pyramid (reset 1%, maker, 100%)",
     dict(rgrid_reset_threshold_pct=1.0, rgrid_stop_loss_pct=3.0, max_net_exposure_pct=100)),
    ("cross + pyramid (reset 1%, cross, 100%)",
     dict(rgrid_reset_threshold_pct=1.0, rgrid_stop_loss_pct=3.0, max_net_exposure_pct=100,
          rgrid_add_mode="cross")),
    ("cross + pyramid wider (reset 2%, cross, 100%)",
     dict(rgrid_reset_threshold_pct=2.0, rgrid_stop_loss_pct=5.0, max_net_exposure_pct=100,
          rgrid_add_mode="cross")),
]
print(f"{'config':<48}{'net $':>9}{'fills':>7}{'orders':>7}")
print("-" * 71)
for label, over in configs:
    try:
        net, fills, orders = bt(**over)
        print(f"{label:<48}{net:>9.3f}{fills:>7}{orders:>7}")
    except Exception as e:  # noqa: BLE001
        print(f"{label:<48}  ERROR: {e}")
