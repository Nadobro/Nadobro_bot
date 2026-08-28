"""Net-floor guardrails for D-Grid AUTO-SWITCH (GRID<->RGRID) on the REAL Aug-2026
tapes — the acceptance gate that let ``dgrid_trend_follow`` default ON.

D-Grid's trend phase is the rebuilt trigger ``ReverseGridController``: in a trend
D-Grid flips to RGRID and rides it; in chop it holds the mean-reversion GRID
ladder. This must be a real trend-follower — strongly positive in a trend, a
BOUNDED loss in chop — NOT the old pyramiding RGridController that was net-losing
in every regime (the measured August bleed that forced the PHASE-0 revert).

These run the SAME two fixtures as the standalone reverse-grid floor
(``test_revgrid_net_floor``): a clean ETH uptrend and active BTC chop. They also
pin ``fee_leak == 0`` on both — the backtester only measures this correctly once
it books the delegate's reduce-only MARKET *close* into inventory (the delegate
runs no executor); without that fix the numbers were invalid (a −342bp phantom).

Deterministic: candle-driven, no randomness.
"""
from __future__ import annotations

import csv
import os
from decimal import Decimal

import pytest

from src.nadobro.engine.backtester import BacktestEngine, SimCosts, candles_from_ohlc
from src.nadobro.strategy.engine_runtime import map_strategy_config

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
DEPLOYED = Decimal("1000")   # notional 250 * levels 4, matched to the revgrid floor


def _load(name):
    with open(os.path.join(FIX, name)) as f:
        return candles_from_ohlc(list(csv.DictReader(f)), interval_s=60.0)


def _run_dgrid(name, mid):
    # Default config under the live trigger flag => auto-switch ON, trigger delegate.
    cfg = map_strategy_config(
        "dgrid", {"notional_usd": 250.0, "levels": 4, "mm_leverage_override": 4},
        Decimal(str(mid)), product="BTC-PERP", leverage=4,
    )
    assert cfg["dgrid_trend_follow"] is True and cfg["trend_uses_trigger"] is True
    eng = BacktestEngine("dgrid", dict(cfg), _load(name), costs=SimCosts())
    rep = eng.run()
    net_bp = float(rep.net_pnl / DEPLOYED) * 10000
    return net_bp, float(eng.fee_leak_quote), rep


@pytest.fixture(autouse=True)
def _trigger_on(monkeypatch):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")


def test_autoswitch_captures_a_real_trend():
    net_bp, leak, rep = _run_dgrid("eth_trend_240.csv", 2600)
    assert abs(leak) < 1e-9, "fee leak — the report is blind to the delegate's fills"
    assert rep.fills > 0
    # Measured +207bp (vs +2bp grid-only): D-Grid flips to RGRID and rides the trend.
    # Floor +100bp catches a regression that halves trend capture or re-breaks the
    # inventory handoff, with margin for sim tweaks.
    assert net_bp >= 100, f"trend capture collapsed to {net_bp:.0f}bp (floor +100bp)"


def test_autoswitch_chop_loss_stays_bounded():
    net_bp, leak, rep = _run_dgrid("btc_chop_240.csv", 61000)
    assert abs(leak) < 1e-9
    # A trend-follower pays a premium in chop (false flips), but D-Grid holds GRID
    # most of the chop so the loss is bounded. Measured -107bp; a floor of -200bp
    # catches a blow-out (the classifier over-flipping, or the pyramiding delegate
    # sneaking back in) while tolerating small sim changes. If this ever fails, the
    # auto-switch has regressed toward the reverted August behaviour.
    assert net_bp >= -200, f"chop loss blew out to {net_bp:.0f}bp (floor -200bp)"


def test_grid_only_baseline_never_bleeds_in_chop():
    """The opt-OUT (dgrid_trend_follow=0) must stay the safe mean-reversion grid —
    a sanity anchor that the chop loss above is the trend phase's cost, not a
    broken GRID phase."""
    cfg = map_strategy_config(
        "dgrid", {"notional_usd": 250.0, "levels": 4, "mm_leverage_override": 4,
                  "dgrid_trend_follow": 0},
        Decimal("61000"), product="BTC-PERP", leverage=4,
    )
    assert cfg["dgrid_trend_follow"] is False
    eng = BacktestEngine("dgrid", dict(cfg), _load("btc_chop_240.csv"), costs=SimCosts())
    rep = eng.run()
    net_bp = float(rep.net_pnl / DEPLOYED) * 10000
    assert abs(float(eng.fee_leak_quote)) < 1e-9
    assert net_bp >= -50, f"grid-only baseline bled in chop: {net_bp:.0f}bp"
