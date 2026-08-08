"""Risk Engine gate + kill-switch tests."""
from __future__ import annotations

from decimal import Decimal

from src.nadobro.engine.risk import (
    ExecutorRequest,
    InMemoryKillSwitchStore,
    RiskEngine,
)
from src.nadobro.engine.types import PositionAction, RiskLimits, RiskState


def test_pretick_daily_loss_floor():
    eng = RiskEngine(RiskLimits(daily_pnl_floor_quote=Decimal(-100)))
    ok, reason = eng.pre_tick_check("c", RiskState(daily_pnl_quote=Decimal(-100)))
    assert not ok and reason == "daily_pnl_floor"
    ok, reason = eng.pre_tick_check("c", RiskState(daily_pnl_quote=Decimal(-50)))
    assert ok and reason is None


def test_pretick_drawdown_cap():
    eng = RiskEngine(RiskLimits(max_drawdown_pct=Decimal("0.2")))
    ok, reason = eng.pre_tick_check("c", RiskState(drawdown_pct=Decimal("0.25")))
    assert not ok and reason == "max_drawdown"


def test_pretick_daily_cost_cap():
    eng = RiskEngine(RiskLimits(daily_cost_cap_usd=Decimal(10)))
    ok, reason = eng.pre_tick_check("c", RiskState(daily_cost_usd=Decimal(10)))
    assert not ok and reason == "daily_cost_cap"


def test_perexec_max_open():
    eng = RiskEngine(RiskLimits(max_open_executors=2))
    ok, reason = eng.pre_executor_check(
        "c", ExecutorRequest(Decimal(10)), RiskState(executor_count=2)
    )
    assert not ok and reason == "max_open_executors"


def test_perexec_max_single_order():
    eng = RiskEngine(RiskLimits(max_single_order_quote=Decimal(100)))
    ok, reason = eng.pre_executor_check("c", ExecutorRequest(Decimal(101)), RiskState())
    assert not ok and reason == "max_single_order_quote"
    ok, _ = eng.pre_executor_check("c", ExecutorRequest(Decimal(100)), RiskState())
    assert ok


def test_perexec_max_position_size():
    eng = RiskEngine(RiskLimits(max_position_size_quote=Decimal(500)))
    ok, reason = eng.pre_executor_check(
        "c", ExecutorRequest(Decimal(10), Decimal(501)), RiskState()
    )
    assert not ok and reason == "max_position_size_quote"


def test_kill_switch_blocks_both_gates_and_persists():
    store = InMemoryKillSwitchStore()
    eng = RiskEngine(RiskLimits(), kill_switch=store)
    eng.kill_switch_on("manual halt")
    ok, reason = eng.pre_tick_check("c", RiskState())
    assert not ok and reason.startswith("kill_switch")
    ok, reason = eng.pre_executor_check("c", ExecutorRequest(Decimal(1)), RiskState())
    assert not ok and reason.startswith("kill_switch")

    # Persistence: a fresh engine over the same store is still killed.
    eng2 = RiskEngine(RiskLimits(), kill_switch=store)
    assert eng2.is_killed()
    ok, _ = eng2.pre_tick_check("c", RiskState())
    assert not ok

    eng2.kill_switch_off()
    assert not store.is_engaged()
    ok, _ = eng2.pre_tick_check("c", RiskState())
    assert ok


def test_reduce_only_skips_the_size_gates():
    """A risk LIMIT must never block risk REDUCTION. R-Grid rests its WHOLE
    position on the reducing leg, so gating it by max_single_order_quote refused
    the exit exactly when a position had grown past one step."""
    eng = RiskEngine(
        RiskLimits(max_single_order_quote=Decimal(50),
                   max_position_size_quote=Decimal(100))
    )
    adding = ExecutorRequest(Decimal(500), Decimal(500))
    ok, reason = eng.pre_executor_check("c", adding, RiskState())
    assert not ok and reason == "max_single_order_quote"

    closing = ExecutorRequest(
        Decimal(500), Decimal(500), reduce_only=True,
        position_action=PositionAction.CLOSE,
    )
    ok, reason = eng.pre_executor_check("c", closing, RiskState())
    assert ok and reason is None


def test_reduce_only_cannot_be_claimed_by_an_order_that_opens():
    """RO-2 (self-review 2026-08-07). ``reduce_only`` buys an exemption from BOTH
    size caps, so it must be a CHECKED claim rather than a caller promise: an OPEN
    order that set it — a copy-paste, or a refactor that moved the leg logic —
    would otherwise open a position of unbounded size with no gate at all.

    Supplying ``position_action`` is optional (older call sites pass neither), but
    supplying a contradictory one is a hard error at construction, not a silent
    bypass at check time."""
    import pytest

    with pytest.raises(ValueError, match="PositionAction.CLOSE"):
        ExecutorRequest(Decimal(10_000), reduce_only=True,
                        position_action=PositionAction.OPEN)

    # The consistent combinations all build.
    ExecutorRequest(Decimal(10), reduce_only=True,
                    position_action=PositionAction.CLOSE)
    ExecutorRequest(Decimal(10), reduce_only=False,
                    position_action=PositionAction.OPEN)
    ExecutorRequest(Decimal(10), reduce_only=True)          # unspecified: allowed
