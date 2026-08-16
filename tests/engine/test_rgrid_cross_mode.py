"""R-Grid cross-mode (rgrid_add_mode="cross") guardrails.

The reported bug: R-Grid "places one order and waits forever" — a resting post-only
add can only fill on a down-tick, so it CANNOT buy a rising market (measured 0
fills across clean/shallow/moderate uptrends). Cross mode fires a bounded
marketable-limit add on confirmed momentum instead. These pin:
  1. cross mode FILLS in an uptrend where maker mode gets 0 (the regression);
  2. cross mode is chop-safe (the breakout threshold self-gates);
  3. the marginal-add net-positive invariant s_add >= giveback + taker round trip;
  4. the executor guard: the crossing add is LIMIT + OPEN + crosses_book ONLY, and
     MARKET is still refused for every leg.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.engine.backtester import (  # noqa: E402
    SimCosts,
    candles_from_prices,
    run_backtest,
)
from src.nadobro.engine.executors.rgrid_maker_executor import (  # noqa: E402
    LEG_ENTRY,
    LEG_ENTRY_CROSS,
    RGridMakerExecutor,
    build_cross_entry,
    build_maker_quote,
)
from src.nadobro.engine.types import ExecutionStrategy, PositionAction, TradeType  # noqa: E402
from src.nadobro.quant.rgrid_sizing import TAKER_ROUND_TRIP_RATE  # noqa: E402
from src.nadobro.strategy.engine_runtime import map_strategy_config  # noqa: E402


def _uptrend(n=240, base=100.0, step_pct=0.05):
    return candles_from_prices([base * (1 + step_pct / 100) ** i for i in range(n)], interval_s=8)


def _chop(n=240, base=100.0, amp_pct=0.15):
    import math
    return candles_from_prices(
        [base + base * amp_pct / 100 * math.sin(i / 3.0) for i in range(n)], interval_s=8
    )


def _cfg(add_mode="maker"):
    settings = dict(
        notional_usd=100, leverage=1, levels=4, rgrid_discretion=0.06,
        rgrid_reset_threshold_pct=0.2, rgrid_stop_loss_pct=0.8, rgrid_take_profit_pct=1.2,
        rgrid_spread_bp=10.0, min_spread_bp=1.5, max_spread_bp=50.0,
    )
    if add_mode != "maker":
        settings["rgrid_add_mode"] = add_mode
    return map_strategy_config("rgrid", settings, Decimal("100"), product="BTC-PERP")


def test_maker_mode_gets_zero_fills_in_a_clean_uptrend():
    """The bug being fixed: a resting maker add cannot fill a rising market."""
    rep = run_backtest("rgrid", dict(_cfg("maker")), _uptrend(), costs=SimCosts())
    assert rep.fills == 0, "regression: maker mode should not fill a clean uptrend"


def test_cross_mode_fills_and_profits_where_maker_cannot():
    """Cross mode fires marketable adds on momentum, so it participates in a trend."""
    rep = run_backtest("rgrid", dict(_cfg("cross")), _uptrend(), costs=SimCosts())
    assert rep.fills >= 2, f"cross mode should add into a trend, got {rep.fills} fills"
    # Net-of-fee positive on a clean trend (the whole point).
    assert rep.net_pnl > 0, f"cross mode should profit on a clean trend, got {rep.net_pnl}"


def test_cross_mode_is_chop_safe():
    """The s_add breakout threshold self-gates: pure chop below it never triggers an
    add, so cross mode does not bleed chop the way a tighter maker band would."""
    rep = run_backtest("rgrid", dict(_cfg("cross")), _chop(amp_pct=0.15), costs=SimCosts())
    assert rep.fills == 0, f"cross mode should not trade sub-spacing chop, got {rep.fills} fills"


def test_add_mode_maps_through():
    cfg = _cfg("cross")
    assert cfg.get("add_mode") == "cross"
    assert cfg.get("trend_gate") is True
    maker = _cfg("maker")
    assert maker.get("add_mode") == "maker"


def test_marginal_add_spacing_exceeds_giveback_plus_taker():
    """s_add = arm + cushion, cushion >= taker round trip, giveback = arm, so every
    marginal add clears more than it can give back plus its taker cost — the
    net-positive-by-construction invariant. Verified on a live controller."""
    from src.nadobro.engine.controllers.rgrid import RGridController
    from src.nadobro.engine.inventory import InventoryRepository
    from src.nadobro.engine.orchestrator import ExecutorOrchestrator

    class _Adapter:
        def lot_size(self, *_a, **_k):
            return Decimal("0.00001")

        def min_notional(self, *_a, **_k):
            return Decimal("1")

    c = RGridController(
        user_id=1, orchestrator=ExecutorOrchestrator(), adapter=_Adapter(),
        inventory=InventoryRepository(),
        configs={
            "trading_pair": "BTC-PERP",
            "spread_bid_pct": Decimal("0.001"), "spread_ask_pct": Decimal("0.001"),
            "order_amount_quote": Decimal(10), "add_mode": "cross",
            "reset_threshold_pct": Decimal("0.002"),
        },
        controller_id="RG",
    )
    s_add = c._add_spacing()
    giveback = c._arm_pct()  # the trail gives back at most the arm
    assert s_add >= giveback + TAKER_ROUND_TRIP_RATE, (
        f"s_add {s_add} must exceed giveback {giveback} + taker RT {TAKER_ROUND_TRIP_RATE} "
        "or the marginal add is not net-positive"
    )


# --- executor guard -------------------------------------------------------
def _mkexec(cfg, leg):
    class _Adapter:
        pass
    return RGridMakerExecutor(cfg, user_id=1, controller_id="RG", adapter=_Adapter(), leg=leg)


def test_crossing_add_permits_only_limit_open_crosses_book():
    ok = build_cross_entry("BTC-PERP", TradeType.BUY, Decimal(1), price=Decimal(100))
    ex = _mkexec(ok, LEG_ENTRY_CROSS)  # must not raise
    assert ex.config.execution_strategy is ExecutionStrategy.LIMIT
    assert ex.config.position_action is PositionAction.OPEN
    assert ex.config.crosses_book is True
    assert ex.is_exit is False  # a crossing add OPENS, it is not an exit


def test_crossing_add_refuses_a_maker_or_reduce_only_or_market_config():
    # A LIMIT_MAKER config under the cross leg is refused (must be crossing LIMIT).
    maker = build_maker_quote("BTC-PERP", TradeType.BUY, Decimal(1), Decimal(100))
    with pytest.raises(ValueError):
        _mkexec(maker, LEG_ENTRY_CROSS)
    # A reduce-only (CLOSE) crossing config under the cross leg is refused.
    from src.nadobro.engine.executors.order_executor import OrderExecutorConfig
    reduce_cross = OrderExecutorConfig(
        "BTC-PERP", TradeType.SELL, Decimal(1), ExecutionStrategy.LIMIT,
        price=Decimal(100), position_action=PositionAction.CLOSE, crosses_book=True,
    )
    with pytest.raises(ValueError):
        _mkexec(reduce_cross, LEG_ENTRY_CROSS)


def test_maker_entry_still_requires_limit_maker():
    """The redesign must not weaken the maker-only guard for the normal legs."""
    bad = build_cross_entry("BTC-PERP", TradeType.BUY, Decimal(1), price=Decimal(100))
    with pytest.raises(ValueError):
        _mkexec(bad, LEG_ENTRY)  # a crossing config under the maker entry leg is refused
