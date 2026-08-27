"""Net-floor regression guardrails for the Reverse Grid, on REAL Aug-2026 tapes.

Two committed 240-bar (4h) fixtures pulled from public Binance USDT-M futures 1m
klines (no key / no user data):

  * ``eth_trend_240.csv`` — a clean ETH uptrend (+8.6%, directional efficiency
    0.33). The reverse grid must MAKE money here (the whole point of a momentum
    grid), net of fees. Measured net at the reference geometry: +774bp of deployed.
  * ``btc_chop_240.csv``  — active BTC chop (+1.4% net, efficiency 0.07). The
    reverse grid LOSES a little here by design; the chop stand-down gate + tight
    stop must keep that loss BOUNDED. Measured net: -186bp of deployed.

These lock in the increment-6 acceptance gate — net >= 0 (in fact strongly +) in a
trend, a bounded loss in chop, and fee_leak == 0 on both — so a future change that
guts trend capture or lets the chop loss blow out fails here. Deterministic: the
sim is candle-driven with no randomness.
"""
from __future__ import annotations

import csv
import os
from decimal import Decimal

from src.nadobro.engine.backtester import BacktestEngine, SimCosts, candles_from_ohlc

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
DEPLOYED = Decimal("1000")   # order_amount_quote(250) * levels(4)

# The reference geometry the fixtures were measured at. Explicit (not the live
# defaults) so this guardrail is stable across default changes in increment 5.
REF_CFG = {
    "trading_pair": "BTC-PERP",
    "levels": 4,
    "step_pct": Decimal("0.002"),      # 20bp
    "stop_pct": Decimal("0.0045"),     # 45bp
    "order_amount_quote": Decimal("250"),
}


def _load(name):
    with open(os.path.join(FIX, name)) as f:
        return candles_from_ohlc(list(csv.DictReader(f)), interval_s=60.0)


def _run(name):
    eng = BacktestEngine("revgrid", dict(REF_CFG), _load(name), costs=SimCosts())
    rep = eng.run()
    net_bp = float(rep.net_pnl / DEPLOYED) * 10000
    return net_bp, float(eng.fee_leak_quote), rep


def test_reverse_grid_profits_in_a_real_trend():
    net_bp, leak, rep = _run("eth_trend_240.csv")
    assert abs(leak) < 1e-9, "fee leak — the report is blind to fills that happened"
    assert rep.fills > 0
    # Strongly positive in a clean trend (measured +774bp). A floor of +400bp still
    # catches a regression that halves trend capture, with margin for sim tweaks.
    assert net_bp >= 400, f"trend net collapsed to {net_bp:.0f}bp (floor +400bp)"


def test_reverse_grid_chop_loss_stays_bounded():
    net_bp, leak, rep = _run("btc_chop_240.csv")
    assert abs(leak) < 1e-9
    # A reverse grid loses in chop BY DESIGN; the gate + tight stop must bound it.
    # Measured -186bp; a floor of -300bp catches a blow-out (e.g. the gate breaking
    # or the stop widening) while tolerating small sim changes.
    assert net_bp >= -300, f"chop loss blew out to {net_bp:.0f}bp (floor -300bp)"
