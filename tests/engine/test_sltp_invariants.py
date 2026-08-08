"""Executable SL/TP & strategy-config invariants — the self-review guardrails.

This file is the machine-checkable expression of the 2026-06-20 strategy audit
(``docs/audit/STRATEGY_SLTP_AUDIT_2026-06-20.md``). Each test encodes ONE
invariant the trading strategies must satisfy so a user's configured SL/TP and
sizing are actually honored and the bot does not bleed money.

Two kinds of tests live here:

* **Green invariants** — properties that hold today. They guard against
  regression (e.g. the rgrid/dgrid SL/TP key resolution that was already fixed).
* **xfail invariants** — known-broken properties from the audit, marked
  ``@pytest.mark.xfail(strict=True)`` with the audit ID in the reason. When the
  underlying bug is fixed the test XPASSes and ``strict=True`` turns that into a
  CI failure — your signal to delete the xfail marker and lock the fix in.

Run just these::

    python -m pytest tests/engine/test_sltp_invariants.py -v

No DB or network required — these exercise pure config/resolution logic.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.nadobro.strategy.engine_runtime import (
    ENGINE_MAPPED_STRATEGIES,
    map_strategy_config,
)
from src.nadobro.strategy.strategy_registry import effective_sl_tp_pct

MID = Decimal("100")
PRODUCT = "BTC-PERP"


# --------------------------------------------------------------------------- #
# Green invariants — must always hold (guard against regression)              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "strategy,conf,expected",
    [
        ("grid", {"sl_pct": 0.5, "tp_pct": 1.0}, (0.5, 1.0)),
        ("mid", {"sl_pct": 0.3, "tp_pct": 0.7}, (0.3, 0.7)),
        # rgrid/dgrid store SL/TP under the rgrid_* keys the UI writes.
        ("rgrid", {"rgrid_stop_loss_pct": 0.8, "rgrid_take_profit_pct": 1.2}, (0.8, 1.2)),
        ("dgrid", {"rgrid_stop_loss_pct": 0.8, "rgrid_take_profit_pct": 1.2}, (0.8, 1.2)),
        ("dn", {"sl_pct": 0.6, "tp_pct": 0.8}, (0.6, 0.8)),
    ],
)
def test_user_sltp_is_resolved_to_the_field_the_user_actually_wrote(strategy, conf, expected):
    """A user's configured SL/TP must resolve back out, per strategy.

    Guards the rgrid/dgrid key-name fix (audit: 'clean / not a bug').
    """
    assert effective_sl_tp_pct(strategy, conf) == expected


def test_dgrid_falls_back_to_sl_pct_when_rgrid_keys_absent():
    """dgrid/rgrid must fall back to sl_pct/tp_pct if the rgrid_* keys are unset."""
    assert effective_sl_tp_pct("dgrid", {"sl_pct": 0.4, "tp_pct": 0.9}) == (0.4, 0.9)


def test_every_engine_strategy_resolves_some_sltp_without_crashing():
    """effective_sl_tp_pct must be total over the supported strategy set."""
    for strategy in ENGINE_MAPPED_STRATEGIES:
        sl, tp = effective_sl_tp_pct(strategy, {"sl_pct": 1.0, "tp_pct": 2.0})
        assert isinstance(sl, float) and isinstance(tp, float)


# --------------------------------------------------------------------------- #
# xfail invariants — known bugs from the audit. Fix the code, then delete the  #
# marker (strict=True makes an unexpected pass fail CI).                       #
# --------------------------------------------------------------------------- #

def test_vol_uses_user_session_margin():
    """A user's vol 'Session margin' must size the run. (VOL-MARGIN fixed:
    map_strategy_config now prefers session_margin_usd over the legacy keys.)"""
    cfg = map_strategy_config("vol", {"session_margin_usd": 500}, MID, product=PRODUCT)
    assert float(cfg["total_amount_quote"]) == pytest.approx(500.0)


def test_vol_falls_back_to_legacy_notional_keys():
    """When session_margin_usd is unset, vol still honors cycle_notional_usd /
    notional_usd and finally the $100 default."""
    assert float(map_strategy_config("vol", {"cycle_notional_usd": 250}, MID, product=PRODUCT)["total_amount_quote"]) == pytest.approx(250.0)
    assert float(map_strategy_config("vol", {}, MID, product=PRODUCT)["total_amount_quote"]) == pytest.approx(100.0)


def test_vol_stop_loss_is_enforced_by_the_session_rail():
    """VOL-DEAD-SL (reframed): the vol controller intentionally carries no SL
    barrier — the user's vol stop-loss is enforced by the session SL/TP rail,
    which reads it via effective_sl_tp_pct('vol', state). So a user-set sl_pct
    is NOT dead; it resolves and the rail (now fee-aware) acts on it."""
    sl, tp = effective_sl_tp_pct("vol", {"sl_pct": 2.0, "tp_pct": 5.0})
    assert sl == 2.0 and tp == 5.0


def test_vol_target_volume_and_cap_reach_the_controller():
    """VOL-LOOP / VOL-NO-CAP: the volume target and the safety cycle cap are
    plumbed into the controller config so the bot can loop to target and stop."""
    cfg = map_strategy_config(
        "vol", {"session_margin_usd": 100, "target_volume_usd": 5000, "vol_max_cycles": 25},
        MID, product=PRODUCT,
    )
    assert float(cfg["target_volume_usd"]) == pytest.approx(5000.0)
    assert int(cfg["max_cycles"]) == 25


def test_grid_does_not_set_fill_blind_limit_price_stop():
    """GRID-DUAL-UNIT fix: the grid config must NOT derive a hard ``limit_price``
    stop from sl_pct. That stop is mid-referenced and fill-blind, firing on a
    wick before the grid has filled — a premature stop-out on top of the
    margin-% rail. SL is the avg-entry barrier + the fee-aware session rail."""
    for strat in ("grid", "rgrid", "dgrid"):
        cfg = map_strategy_config(strat, {"sl_pct": 0.5, "tp_pct": 1.0}, MID, product=PRODUCT)
        assert float(cfg.get("limit_price") or 0) == 0.0


def test_classic_ladder_does_not_turn_a_margin_percent_into_a_level_barrier():
    """The classic ladder used to copy the user's %-of-margin sl/tp onto the
    executor's avg-entry barrier. That is a different quantity twice over — per
    LEVEL rather than per session, and leverage-blind — so it stopped out roughly
    ``levels`` times early and paid taker fees to do it. The ladder still takes
    profit per level the way a grid does (a filled BUY is closed by its paired
    SELL one step up); the SESSION stop is the rail's job."""
    cfg = map_strategy_config(
        "grid", {"sl_pct": 0.5, "tp_pct": 1.0, "fill_anchored": 0}, MID, product=PRODUCT
    )
    assert "triple_barrier_config" not in cfg
    assert float(cfg.get("limit_price") or 0) == 0.0
    # The user's numbers still reach the rail unchanged.
    assert effective_sl_tp_pct("grid", {"sl_pct": 0.5, "tp_pct": 1.0}) == (0.5, 1.0)


# Note on DN-RAIL (Critical) and SLTP-GROSS / GRID-TP-DEAD:
# These live in bot_runtime/live_session/grid_executor and need a running
# session to assert directly. They are tracked as checklist items in
# docs/audit/SELF_REVIEW_WORKFLOW.md and should get dedicated integration tests
# when the fixes land. The cheapest structural guard ships below.

def test_dn_is_an_engine_mapped_strategy_so_a_rail_can_target_it():
    """DN must be a recognized engine strategy (precondition for a session rail).

    This does NOT prove the rail exists (audit DN-RAIL: it does not). It guards
    the precondition; see SELF_REVIEW_WORKFLOW.md checklist item DN-RAIL for the
    integration test to add alongside the fix.
    """
    assert "dn" in ENGINE_MAPPED_STRATEGIES


# ── ISO-UPNL-BLIND (VERIFIED 2026-07-26, fixed in this PR) ──────────────
# Nado's IsolatedPositionMetrics carries no est_pnl and no avg_entry_price
# (nado_protocol/utils/margin_manager.py:97-110) — only CROSS positions get
# them. live_session summed `est_pnl`, so every isolated position contributed
# 0.0 and the session SL/TP rail could not see an open isolated loss at all.
# Delta Neutral and copy trading BOTH run isolated, so their stop-loss was
# effectively disarmed against unrealized moves.
#
# The live fallback was worse: get_all_positions() rows have no
# `unrealized_pnl` key at all (they carry amount / signed_amount /
# entry_price / v_quote_balance), so that path returned 0.0 for CROSS
# positions too whenever the DB row was stale.

def test_isolated_position_upnl_is_derived_not_dropped():
    """An isolated position with no venue est_pnl must still yield real uPnL."""
    from src.nadobro.quant.portfolio_calculator import derive_unrealized_pnl

    # Venue identity: uPnL = signed_size * mark + v_quote_balance.
    # Short 0.3062 BTC opened at 65475.7, mark 65485.5 -> a LOSS.
    iso = {
        "product_id": 2, "side": "short", "amount": "0.3062",
        "signed_amount": "-0.3062", "entry_price": "65475.7",
        "v_quote_balance": "20048.659340000002",
        # no est_pnl — exactly what the venue returns for isolated
    }
    upnl = derive_unrealized_pnl(iso, mark_price="65485.5")
    assert upnl is not None, "isolated uPnL must not be dropped"
    assert float(upnl) < 0, "a short below entry must report a LOSS, not 0.0"
    assert abs(float(upnl) - (-3.0)) < 0.01


def test_isolated_upnl_reaches_the_session_rail():
    """The rail reads live_session._aggregate_position_rows; an isolated row
    must contribute its loss so SL can fire."""
    from src.nadobro.trading.live_session import _aggregate_position_rows

    rows = [{
        "product_id": 2, "side": "short", "size": "1", "signed_amount": "-1",
        "entry_price": "100", "mark_price": "110",   # short at 100, now 110
        "est_pnl": None, "isolated": True, "synced_ts": 0,
    }]
    view = _aggregate_position_rows(rows)
    assert view["upnl"] < 0, "isolated loss must reach the rail (was 0.0)"
    assert abs(view["upnl"] - (-10.0)) < 1e-6


def test_cross_position_with_explicit_est_pnl_is_unchanged():
    """Regression guard: the venue's own cross est_pnl still wins."""
    from src.nadobro.quant.portfolio_calculator import derive_unrealized_pnl

    cross = {"side": "short", "amount": "10", "signed_amount": "-10",
             "avg_entry_price": "1933.9", "est_pnl": "-315.55"}
    assert float(derive_unrealized_pnl(cross)) == -315.55


# ==========================================================================
# R-Grid SL/TP coverage (2026-08-06)
# ==========================================================================
# R-Grid moved to its own controller + taker executor. Its SL/TP must still be
# the %-of-margin SESSION rail (live PnL incl. uPnL, judged net of fees) — and
# must NOT ALSO become a price-move barrier on the same user number.
def test_rgrid_sltp_is_the_session_rail_only_never_also_a_price_barrier():
    cfg = map_strategy_config(
        "rgrid",
        {"sl_pct": 0.5, "tp_pct": 1.0,
         "rgrid_stop_loss_pct": 2.0, "rgrid_take_profit_pct": 5.0},
        MID, product=PRODUCT,
    )
    assert cfg.get("triple_barrier_config") is None, (
        "the same user number must be either a barrier or a rail, never both"
    )
    assert float(cfg.get("limit_price") or 0) == 0.0
    assert effective_sl_tp_pct(
        "rgrid", {"rgrid_stop_loss_pct": 2.0, "rgrid_take_profit_pct": 5.0}
    ) == (2.0, 5.0)


def test_rgrid_is_on_the_session_rail_branch_in_the_cycle():
    """Structural guard: the rail only runs for the strategies named in
    bot_runtime._run_cycle. If rgrid ever drops out of that tuple its stop stops
    existing — silently, because nothing else enforces it for this controller."""
    from pathlib import Path

    # Read the SOURCE rather than importing bot_runtime. This is a structural
    # guard, so it must not need the module to be importable: the Strategy
    # Self-Review CI job installs only pytest, and importing bot_runtime pulls in
    # psycopg2 (models/database.py), which fails there and nowhere else.
    text = Path("src/nadobro/strategy/bot_runtime.py").read_text()
    start = text.index("async def _run_cycle")
    end = text.find("\nasync def ", start + 1)
    source = text[start:end if end != -1 else len(text)]
    assert 'if strategy in ("grid", "rgrid", "dgrid", "mid"):' in source, (
        "rgrid must stay on the session SL/TP rail branch"
    )
    assert "_evaluate_session_pnl_rail" in source


def test_rgrid_and_dgrid_are_engine_mapped_so_a_rail_can_target_them():
    from src.nadobro.strategy.engine_runtime import ENGINE_MAPPED_STRATEGIES

    assert "rgrid" in ENGINE_MAPPED_STRATEGIES
    assert "dgrid" in ENGINE_MAPPED_STRATEGIES


# ==========================================================================
# Pre-existing [VERIFIED] findings — self-review audit 2026-08-06
# ==========================================================================
# Recorded as strict xfails per the triage protocol: they were found by the
# audit, they are NOT regressions from the R-Grid/D-Grid work, and fixing either
# changes live stop behaviour for real grid/dgrid sessions — a product call, not
# a silent one. When each is fixed the marker must be deleted in the same PR
# (strict mode turns an XPASS into a failure, which is the cue).
def test_dgrid_sltp_is_not_applied_as_both_a_barrier_and_a_rail():
    """DGRID-DUAL-UNIT-SLTP — FIXED. The user's %-of-margin SL/TP no longer become
    a per-level price barrier: the session rail is the single enforcement point,
    as it already was for rgrid and mid."""
    cfg = map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "rgrid_stop_loss_pct": 0.8,
                  "rgrid_take_profit_pct": 1.2},
        MID, product=PRODUCT,
    )
    rail_sl, _ = effective_sl_tp_pct("dgrid", {"rgrid_stop_loss_pct": 0.8,
                                               "rgrid_take_profit_pct": 1.2})
    barrier = cfg.get("triple_barrier_config")
    barrier_sl = float(getattr(barrier, "stop_loss", 0) or 0)
    assert not (rail_sl > 0 and barrier_sl > 0), (
        f"the same 0.8% is a {barrier_sl} price-move barrier AND a {rail_sl}% "
        "of-margin rail"
    )


def test_overlay_cannot_touch_the_executor_barrier_at_all():
    """OVERLAY-BARRIER-UNITS. The overlay's sl_pct/tp_pct are % of MARGIN; the
    executor barrier is a PRICE-return fraction. The overlay used to convert one
    into the other with a bare /100, which both mixed units (off by ``leverage``)
    and overwrote the user's configured barrier. It must leave the barrier alone —
    its regime-adjusted numbers belong to the %-of-margin session rail."""
    from src.nadobro.llm.signal_engine import Signal
    from src.nadobro.strategy.overlay_actuator import (
        apply_overrides_to_configs, compute_overrides, rail_barriers,
    )

    from src.nadobro.engine.types import TripleBarrierConfig

    user_tp_pct, user_sl_pct = 1.2, 0.8
    chop = Signal(regime="chop", sl_pct=0.64, tp_pct=0.96, confidence=0.5)
    cfg = map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "rgrid_stop_loss_pct": user_sl_pct,
                  "rgrid_take_profit_pct": user_tp_pct},
        MID, product=PRODUCT,
    )
    # The mapping emits none at all now; a hand-set one must survive untouched too.
    assert "triple_barrier_config" not in cfg
    before = TripleBarrierConfig(take_profit=Decimal("0.01"), stop_loss=Decimal("0.005"))
    cfg["triple_barrier_config"] = before
    apply_overrides_to_configs("dgrid", cfg, compute_overrides("dgrid", chop))
    assert cfg["triple_barrier_config"] is before, "the overlay rewrote the barrier"

    # And on the rail the user's TP is still the floor (widen-only).
    _, rail_tp = rail_barriers(user_sl_pct, user_tp_pct, chop)
    assert rail_tp >= user_tp_pct


def test_no_engine_strategy_emits_a_session_sltp_as_a_price_barrier():
    """One number, one unit, one enforcement point — for every MM strategy."""
    for strategy in ("grid", "rgrid", "dgrid", "mid"):
        cfg = map_strategy_config(
            strategy,
            {"notional_usd": 100.0, "rgrid_stop_loss_pct": 0.8,
             "rgrid_take_profit_pct": 1.2, "sl_pct": 0.8, "tp_pct": 1.2,
             "mm_leverage_override": 49},
            MID, product=PRODUCT,
        )
        barrier = cfg.get("triple_barrier_config")
        assert barrier is None or (
            getattr(barrier, "stop_loss", None) is None
            and getattr(barrier, "take_profit", None) is None
        ), f"{strategy} still carries a price-move barrier for a %-of-margin number"
        assert float(cfg.get("limit_price") or 0) == 0.0, strategy


def test_dgrid_tp_tiers_receive_a_percent_not_a_fraction():
    """DGRID-TP-TIER-UNITS. ``DynamicGridController._tp_tier_ladder`` reads
    ``cfg["tp_pct"]`` as a PERCENT and compares the ladder against ``upnl_pct``,
    also a percent. The mapping handed it the /100 fraction, making every tier
    100x too small: with the shipped 1.2% TP the tiers landed at 0.004/0.008/0.012
    % of margin, so D-Grid scaled out a third of the position on the first
    favourable tick and never let a winner run."""
    cfg = map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "rgrid_stop_loss_pct": 0.8,
                  "rgrid_take_profit_pct": 1.2},
        MID, product=PRODUCT,
    )
    assert float(cfg["tp_pct"]) == 1.2, "tier ladder needs a percent"
    assert float(cfg["sl_pct"]) == 0.8
    # And the ladder it produces tops out AT the user's TP, in % of margin.
    from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
    from src.nadobro.engine.inventory import InventoryRepository
    from src.nadobro.engine.orchestrator import ExecutorOrchestrator
    from tests.engine._mock_nado import MockNadoAdapter

    c = DynamicGridController(
        user_id=1, orchestrator=ExecutorOrchestrator(),
        adapter=MockNadoAdapter(mid=MID), inventory=InventoryRepository(),
        configs={**cfg, "trading_pair": PRODUCT, "tp_margin_basis": Decimal(100)},
    )
    tiers, basis = c._tp_tier_ladder()
    assert max(tiers) == pytest.approx(1.2), tiers
    assert basis == Decimal(100)


# ==========================================================================
# Self-audit 2026-08-08 (pre-merge, whole branch). These were recorded as strict
# xfails and then FIXED, so the markers are gone and they are live regressions
# tests now — the step cap bounds the PYRAMID (levels * step) on both the fee and
# the adverse-price axis, and a disarmed TP no longer arms a phantom tier ladder.
# ==========================================================================
def test_the_rgrid_step_cap_budgets_the_exit_it_will_actually_pay():
    """R-Grid pyramids: each break adds a step, so exposure grows to
    ``levels * step`` (bounded by the net-exposure cap). The trailing stop then
    crosses ``abs(net)`` — the whole pyramid — while
    ``rgrid_sizing.resolve_step_quote`` bounds the budget against
    ``taker_round_trip_cost(step)``, a SINGLE step.

    At the shipped defaults on a 50x pair ($100 margin, 4 levels, 0.8% SL) the cap
    certifies "~3 round trips fit" while one exit on the full pyramid eats a large
    share of the whole $0.80 budget. The user sees R-Grid stop out on its own exit.
    """
    from src.nadobro.quant.rgrid_sizing import (
        TAKER_ROUND_TRIP_RATE, resolve_step_quote,
    )

    deployed, levels = Decimal(5000), 4          # $100 at 50x
    plan = resolve_step_quote(
        deployed_quote=deployed, levels=levels,
        stop_budget_usd=Decimal("0.80"),          # 0.8% of $100
        band_frac=Decimal("0.001"),
    )
    pyramid = plan.step * Decimal(levels)
    exit_fee = pyramid * (Decimal(str(TAKER_ROUND_TRIP_RATE)) / Decimal(2))
    assert exit_fee <= plan.stop_budget_usd / Decimal(2), (
        f"one exit on the {pyramid} pyramid costs {exit_fee}, over half the "
        f"{plan.stop_budget_usd} stop budget the cap sized against one "
        f"{plan.step} step"
    )


def test_the_rgrid_pyramid_can_move_a_band_before_the_rail_fires():
    """R-Grid's own exit needs a full ``band`` pullback to become postable. If the
    pyramid is large enough that ``band`` of adverse move exceeds the stop budget,
    the rail always wins and the strategy can never exit on its own terms — "the
    user never sees a losing trade, just a strategy that keeps stopping"."""
    from src.nadobro.quant.rgrid_sizing import (
        TAKER_ROUND_TRIP_RATE, resolve_step_quote,
    )

    band, budget = Decimal("0.001"), Decimal("0.80")     # 10bp band, $0.80 stop
    plan = resolve_step_quote(deployed_quote=Decimal(5000), levels=4,
                              stop_budget_usd=budget, band_frac=band)
    pyramid = plan.step * Decimal(4)
    loss_at_one_band = pyramid * (band + Decimal(str(TAKER_ROUND_TRIP_RATE)))
    assert loss_at_one_band <= budget, (
        f"a single band of adverse move on the {pyramid} pyramid loses "
        f"{loss_at_one_band}, past the {budget} rail — the exit leg can never "
        f"become postable first"
    )


def test_a_disarmed_dgrid_tp_does_not_arm_a_phantom_tier_ladder():
    """``map_strategy_config`` substitutes ``_tp_pct = _f(settings, "tp_pct", 0.6)``
    whenever the strategy's own key resolves to <= 0. For dgrid the key is
    ``rgrid_take_profit_pct``, so an explicit 0 (user disarmed TP) becomes 0.6 and
    the tier ladder scales out at a fifth of a percent of margin — while the rail's
    TP stays disarmed. It also makes dynamic_grid's documented "no TP set keeps the
    legacy tiers" branch unreachable from the mapper."""
    cfg = map_strategy_config(
        "dgrid",
        {"notional_usd": 100.0, "rgrid_take_profit_pct": 0.0,
         "rgrid_stop_loss_pct": 0.8},
        MID, product=PRODUCT,
    )
    rail_sl, rail_tp = effective_sl_tp_pct(
        "dgrid", {"rgrid_take_profit_pct": 0.0, "rgrid_stop_loss_pct": 0.8})
    assert rail_tp == 0.0, "premise: the user's TP is disarmed"
    assert float(cfg.get("tp_pct") or 0) == 0.0, (
        f"the rail is disarmed but the tier ladder got tp_pct="
        f"{cfg.get('tp_pct')}, arming a scale-out the user switched off"
    )


def test_the_dgrid_tier_denominator_is_the_margin_the_rail_measures():
    """DGRID-TIER-BASIS. The tier ladder is anchored to the user's TP, so its
    denominator must be the rail's margin. It used to be the DEPLOYMENT basis,
    which resolves cycle-first while the rail resolves notional-first: with
    cycle 250 / margin 100 the tiers demanded $1/$2/$3 while the rail took profit
    at $1.20 (ladder dead above rung one); with cycle 50 they fired at HALF the
    user's TP. Same hazard the R-Grid step cap already fixed."""
    from src.nadobro.trading.live_session import _resolve_margin

    for conf in (
        {"notional_usd": 100.0, "rgrid_take_profit_pct": 1.2},
        {"notional_usd": 100.0, "cycle_notional_usd": 250.0,
         "rgrid_take_profit_pct": 1.2},
        {"notional_usd": 100.0, "cycle_notional_usd": 50.0,
         "rgrid_take_profit_pct": 1.2},
    ):
        cfg = map_strategy_config("dgrid", conf, MID, product=PRODUCT)
        basis = float(cfg["tp_margin_basis"])
        rail = float(_resolve_margin(conf, None) or 0)
        assert basis == rail, (
            f"tier basis {basis} != rail margin {rail} for {conf} — the scale-out "
            f"and the stop measure the same percent against different dollars"
        )
