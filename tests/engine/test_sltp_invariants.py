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


# --------------------------------------------------------------------------- #
# Per-asset strategy leverage — maintenance-margin liquidation guard          #
# (2026-08). Higher leverage is only safe with a maintenance-margin-aware      #
# distance check; these pin that the guard blocks the dangerous configs and    #
# leaves normal ones alone.                                                     #
# --------------------------------------------------------------------------- #

def test_liq_guard_blocks_loose_and_disarmed_high_lev_but_not_tight():
    """LIQ-GUARD-SAFE-SL-MATH + LIQ-GUARD-DISARMED-SL-HIGH-LEV: at pair-max
    leverage a loose or disarmed session stop must be rejected (with an
    actionable safe-SL / safe-leverage), while a tight armed stop passes."""
    from src.nadobro.quant.liquidation import fallback_mmf, liquidation_safety

    mmf = fallback_mmf(1 / 50)
    tight = liquidation_safety(leverage=50, mmf=mmf, sl_pct=5.0, sl_armed=True)
    loose = liquidation_safety(leverage=50, mmf=mmf, sl_pct=40.0, sl_armed=True)
    disarmed = liquidation_safety(leverage=50, mmf=mmf, sl_pct=0.0, sl_armed=False)
    assert tight.ok
    assert not loose.ok and loose.reason == "sl_too_loose"
    assert 0 < loose.safe_max_sl_pct < 40.0 and 1.0 <= loose.max_safe_leverage < 50.0
    assert not disarmed.ok and disarmed.reason == "disarmed_sl_high_lev"


def test_liq_guard_leaves_default_configs_untouched():
    """Regression: default grid/rgrid/dgrid/mid ship an armed session stop
    (0.5–0.8% of margin). At pair-max leverage that must stay safe, or enabling
    per-asset leverage would block every default strategy start."""
    from src.nadobro.quant.liquidation import fallback_mmf, liquidation_safety

    for max_lev, default_sl in ((50, 0.5), (40, 0.8), (20, 0.8)):
        mmf = fallback_mmf(1 / max_lev)
        v = liquidation_safety(leverage=max_lev, mmf=mmf, sl_pct=default_sl, sl_armed=True)
        assert v.ok, (max_lev, default_sl, v.reason)


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


# --------------------------------------------------------------------------- #
# SLTP-OVERSHOOT-BUFFER (2026-08-25 incident: a 10%-of-$100 session stop        #
# realized > -$20 at ~20-50x leverage). The session rail fired on a bare        #
# `pct_net <= -sl_pct`, reserving nothing for the loss that accrues between      #
# polls and during the flatten round-trip, so the realized exit overshot the    #
# user's number under leverage. The rail now tightens the SL trigger by a        #
# leverage-scaled reserve (`quant/sltp_overshoot.effective_sl_trigger`), gated   #
# by NADO_SLTP_BUFFER_ENABLED and fail-safe back to the raw sl_pct. The buffer   #
# only ever TIGHTENS the stop — it can never loosen, invert, or disarm it, and   #
# never touches TP.                                                              #
# --------------------------------------------------------------------------- #

def test_sltp_overshoot_buffer_only_ever_tightens_the_stop():
    """SLTP-OVERSHOOT-BUFFER: the effective SL trigger is <= the user's sl_pct at
    every leverage (fires at/before the user's number, never after), and equals
    it exactly when disarmed. High leverage fires meaningfully earlier so the
    realized loss lands at/under the configured %."""
    from src.nadobro.quant.sltp_overshoot import effective_sl_trigger

    for lev in (1, 5, 10, 20, 50, 100):
        eff = effective_sl_trigger(10.0, float(lev))
        assert 0.0 < eff <= 10.0, (lev, eff)          # only tightens, never disarms
    # Disarmed stays disarmed (buffer must not manufacture a stop).
    assert effective_sl_trigger(0.0, 50.0) == 0.0
    # The incident leverage band trips well before the raw -10% barrier.
    assert effective_sl_trigger(10.0, 50.0) <= 6.0
    # Low leverage stays effectively at the user's number.
    assert effective_sl_trigger(10.0, 2.0) >= 9.0


def test_sltp_overshoot_buffer_never_fires_a_take_profit_early():
    """The buffer is SL-only: the rail applies the buffered trigger to the SL
    branch but still fires TP at the exact user tp_pct (tightening a take-profit
    would leave profit on the table). Asserted by reading the rail source as text
    (no import) so this stays runnable in the pytest-only CI invariant job."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    src = (repo / "src" / "nadobro" / "strategy" / "bot_runtime.py").read_text()
    # SL compare uses the buffered trigger; TP compare uses the raw user tp_pct.
    assert "pct_net <= -sl_trigger" in src
    assert "pct_net >= tp_pct" in src


def test_sltp_fast_poll_is_wired_into_the_cycle_and_scheduler():
    """SLTP-FAST-POLL: a decoupled safety poll enqueues rails-only "safety"
    cycles so a drawdown is caught between the strategy's slower trading ticks.
    Verified by reading source (no import) so it runs in the pytest-only CI job:
    the cycle honours safety_only (bypasses the interval gate, runs only the
    rails via _run_sltp_safety_rails, threads the flag to the worker), and the
    scheduler registers the poll, gates it, and skips DN/bro."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    br = (repo / "src" / "nadobro" / "strategy" / "bot_runtime.py").read_text()
    sched = (repo / "src" / "nadobro" / "runtime" / "scheduler.py").read_text()
    # Cycle: safety_only bypasses the trading interval gate and runs only rails.
    assert "safety_only: bool = False" in br
    assert "not safety_only and last_run > 0" in br
    assert "_run_sltp_safety_rails(" in br
    assert '"safety_only": _safety' in br            # threaded to the worker path
    # Scheduler: the poll is registered, kill-switchable, and skips DN/bro/vol.
    assert "tick_sltp_safety" in sched
    assert "NADO_SLTP_FAST_POLL_ENABLED" in sched
    assert '"safety_only": True' in sched
    # vol is SPOT (no leverage overshoot) and its high-cadence poll amplified the
    # flat-in-cycle_gap spot-sweep risk — it must be skipped by the fast poll.
    assert '{"dn", "bro", "vol", ""}' in sched


def test_vol_open_base_reaches_state_so_the_spot_sweep_guard_is_live():
    """VOL-OPEN-BASE-MERGE: the vol controller publishes ``vol_open_base``
    (still-held base; 0 when flat) so the spot-sweep sizer sells the exact held
    amount and 0 when flat. If it never reaches ``state`` the guard is dead and a
    stop firing while vol is flat can market-sell the user's OWN spot. Pin that
    the merge whitelist carries it (source read; no import for the pytest-only
    CI job)."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    br = (repo / "src" / "nadobro" / "strategy" / "bot_runtime.py").read_text()
    # It must be inside the _merge_vol_order_counters whitelist.
    start = br.index("def _merge_vol_order_counters")
    end = br.index("def ", start + 1)
    assert '"vol_open_base"' in br[start:end], "vol_open_base missing from the merge whitelist"


def test_venue_stop_is_gated_off_and_wired_as_a_reduce_only_backstop():
    """VENUE-STOP: an exchange-enforced reduce-only trigger order backstops the
    software rail (fires even if the bot lags/disconnects). Verified by reading
    source (no import) so it runs in the pytest-only CI job: the feature defaults
    OFF, the client wrapper is reduce-only with the correct per-side trigger, the
    price geometry puts a long stop below entry / short above, and the rail syncs
    it on the no-stop path and cancels it on the fired path."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    vs = (repo / "src" / "nadobro" / "strategy" / "venue_stop.py").read_text()
    nc = (repo / "src" / "nadobro" / "venue" / "nado_client.py").read_text()
    br = (repo / "src" / "nadobro" / "strategy" / "bot_runtime.py").read_text()
    geo = (repo / "src" / "nadobro" / "quant" / "stop_geometry.py").read_text()
    # Default OFF until testnet-validated.
    assert 'env_bool("NADO_VENUE_STOP_ENABLED", False)' in vs
    # Wrapper: reduce-only order + per-side trigger direction (long below, short above).
    assert "reduce_only=True" in nc
    assert '"mid_price_below" if position_is_long else "mid_price_above"' in nc
    # Geometry: long stop below entry, short above.
    assert "e * (1.0 - move) if is_long else e * (1.0 + move)" in geo
    # Rail wiring: sync on the no-stop path, cancel on the fired path.
    assert "sync_session_venue_stop(" in br
    assert "cancel_session_venue_stop(" in br


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
    nested = cfg.get("trend_rgrid") or {}
    assert "triple_barrier_config" not in nested, (
        "the nested R-Grid mapping reintroduced a price barrier on dgrid's trend leg"
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


# ==========================================================================
# Self-review audit 2026-08-12 — [VERIFIED] findings, recorded not fixed
# ==========================================================================
# Strict xfails per the triage protocol. These came out of the audit fan-out on
# the signal-advisor / copy-pause branch. None is fixed here: each changes live
# order sizing or live stop behaviour for real sessions, which is a product call.
# When one is fixed, strict mode turns the XPASS into a failure — delete the
# marker in the same PR as the fix.

@pytest.mark.xfail(strict=True, reason="ADVISOR-SIZE-SIGN: confidence is a magnitude "
                                      "multiplier on a SIGNED scale, so LOWERING it "
                                      "weakens a trim instead of deepening it")
def test_lowering_advisor_confidence_never_increases_the_overlay_size_factor():
    """ADVISOR-SIZE-SIGN — pre-existing, on the risk-REDUCING path.

    ``signal_advisor`` promises "Nothing here can raise size". But
    ``overlay_actuator`` computes ``size_factor = 1 + 0.25 * scale * confidence``
    where ``scale`` carries the DIRECTION. So when the engine wants to reduce
    (``scale < 0``), lowering confidence moves the factor back toward 1.0 — a
    SHALLOWER cut, i.e. MORE notional. The tier increases risk precisely when it
    is trying to reduce it.

    This is the disagree / negative-delta path, which the 2026-08 conviction clamp
    did not touch: ``min(confidence, signal.confidence)`` already discarded
    positive deltas there. Bounded by the 0.05 dead-band in ``stabilize_overrides``
    and by the downstream ``max_single_order_quote`` clamp.

    Invariant that should hold: a LOWER advisory confidence never yields a larger
    ``size_factor``. Fix: derive size from ``abs(scale)`` so confidence is an
    unambiguous risk dial, or floor the factor at its pre-advisor value.
    """
    from src.nadobro.llm.signal_advisor import _apply
    from src.nadobro.llm.signal_engine import Signal
    from src.nadobro.strategy.overlay_actuator import compute_overrides

    # Engine reads a downtrend and wants to TRIM: bias < 0 => scale < 0.
    base = Signal(bias=-0.8, regime="trend_down", entry_ok=True, scale=-0.48,
                  confidence=0.60)
    shaded = _apply(base, {"agree": False, "confidence_delta": -0.15,
                           "provider": "test"})
    assert shaded.confidence < base.confidence, "precondition: confidence dropped"

    before = float(compute_overrides("grid", base)["size_factor"])
    after = float(compute_overrides("grid", shaded)["size_factor"])
    assert after <= before, (
        f"lowering confidence raised size_factor {before:.4f} -> {after:.4f}"
    )


# ── Moved out, deliberately ─────────────────────────────────────────────────
# Two guardrails from the 2026-08-12 audit are BEHAVIOURAL — they drive the real
# _maybe_apply_overlay and _evaluate_session_pnl_rail — so they transitively import
# models.database and need psycopg2. This file is run by .github/workflows/
# self-review.yml with `pip install pytest` and NOTHING else, on purpose: the
# invariants must stay runnable with zero project dependencies. Guarding them with
# importorskip would silently skip them in the very job meant to enforce them, so
# they live next to the harnesses they use instead:
#
#   GRID-TOTALQUOTE-UNCAPPED / DGRID-TOTALQUOTE-UNCAPPED (FIXED)
#     -> tests/services/test_overlay_wiring.py
#        test_no_overlay_scaled_size_key_can_exceed_the_risk_cap[grid|dgrid]
#   OVERLAY-DISARMED-BARRIER-ARMS (FIXED)
#     -> tests/services/test_session_safety_rails.py
#        test_a_disarmed_user_barrier_is_never_armed_by_a_stale_overlay_value
#
# Both run in the full pytest job. Keep this pointer in step with them.


def test_rgrid_band_exit_always_sits_outside_the_trail_arm_point():
    """RGRID-EXITBAND-INVERT — FIXED.

    rgrid's docstring states the invariant that makes it profitable: the band exit
    fires at ``avg_entry x (1 - exit_band)`` and is LOSS-ONLY by construction, so the
    trail must arm strictly INSIDE it. Shipped the other way round, "the loss-only
    exit always won" — the configuration that measured -85.27.

    ``exit_band_cap`` is computed once in ``map_strategy_config`` from the UNSCALED
    band, while the overlay rescales ``spread_ask_pct`` live by up to 3x and pushes
    it onto the controller. The exit was ceilinged by that stale cap and the arm was
    not, so they crossed — and because the overlay only widens the spread BECAUSE the
    tape is volatile, the inversion armed itself exactly when noise was largest.

    The two are now derived jointly (``_exit_geometry``). Per the 2026-08-13 product
    ruling ("when the strategy is in profit, the wins shouldn't be capped"), the
    reconciliation WIDENS THE EXIT to arm + band and leaves the arm where the
    geometry derives it — the user's %-of-margin session rail is the backstop when
    that exceeds the stop budget. The arm is never shrunk to fit the cap.
    """
    from src.nadobro.engine.controllers.rgrid import RGridController
    from src.nadobro.quant.rgrid_sizing import TAKER_ROUND_TRIP_RATE, arm_pct

    def _ctrl(band_bp, reset_pct, cap_bp):
        c = object.__new__(RGridController)
        c.spread_ask_pct = Decimal(str(band_bp)) / Decimal(10000)
        c.spread_floor_half_pct = Decimal("0.00015")
        c.reset_threshold_pct = Decimal(str(reset_pct))
        c._cfg = {"exit_band_cap": str(Decimal(str(cap_bp)) / Decimal(10000)),
                  "exit_band_mult": "0"}
        c.cfg = lambda k, d=None: c._cfg.get(k, d)
        c.user_id = 1
        c.trading_pair = "BTC-PERP"
        return c

    # Sweep the overlay's full scaling range against a cap sized from the UNSCALED
    # band — the exact seam that produced the inversion.
    violations = []
    for base_bp in (5.0, 10.0, 20.0):
        for reset in (0.002, 0.005, 0.01):
            for cap_bp in (15.0, 30.0, 60.0):
                for mult in (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0):
                    c = _ctrl(base_bp * mult, reset, cap_bp)
                    exit_band, arm = c._exit_geometry()
                    if not exit_band > arm:
                        violations.append((base_bp, reset, cap_bp, mult, exit_band, arm))
                    assert arm >= TAKER_ROUND_TRIP_RATE, (
                        "an arm under the round-trip cost books a 'profit' smaller "
                        "than the cost of taking it"
                    )
                    # The ruling: the arm is the profit-taking trigger and is NEVER
                    # pulled inward to fit the cap — that is what capped winners.
                    assert arm == arm_pct(c._band(), c.reset_threshold_pct), (
                        "the arm was shrunk to fit the cap instead of widening exit"
                    )
    assert not violations, f"exit_band <= arm at {len(violations)} points: {violations[:3]}"


def test_the_exit_geometry_fix_is_a_no_op_where_the_invariant_already_held():
    """The +487 measurement from commit #222 must still describe live code.

    Whenever the cap does not bind, the derived pair is returned UNMODIFIED — so the
    fix cannot have moved the geometry that measurement was taken on. Pinned rather
    than argued: with A = arm_pct and D = exit_band_frac = A + band, D > A always,
    so cap <= 0 or cap >= D takes the identity branch.
    """
    from src.nadobro.engine.controllers.rgrid import RGridController
    from src.nadobro.quant.rgrid_sizing import arm_pct, exit_band_frac

    c = object.__new__(RGridController)
    c.spread_ask_pct = Decimal("0.0010")          # 10bp, the measured config
    c.spread_floor_half_pct = Decimal("0.00015")
    c.reset_threshold_pct = Decimal("0.002")
    c._cfg = {"exit_band_cap": "0.003", "exit_band_mult": "0"}   # 30bp, does not bind
    c.cfg = lambda k, d=None: c._cfg.get(k, d)
    c.user_id = 1
    c.trading_pair = "BTC-PERP"

    band = c._band()
    exit_band, arm = c._exit_geometry()
    assert arm == arm_pct(band, c.reset_threshold_pct), "arm was altered at x1.0"
    assert exit_band == exit_band_frac(band, c.reset_threshold_pct), "exit altered at x1.0"


def test_the_trail_giveback_tracks_the_clamped_arm():
    """Give-back == arm is what puts the armed stop at breakeven. It used to be a
    second INDEPENDENT derivation, so once the arm could be clamped the two would
    diverge and the stop would sit below entry."""
    from src.nadobro.engine.controllers.rgrid import RGridController

    c = object.__new__(RGridController)
    c.spread_ask_pct = Decimal("0.0030")          # 30bp: overlay-scaled 3x
    c.spread_floor_half_pct = Decimal("0.00015")
    c.reset_threshold_pct = Decimal("0.002")
    c._cfg = {"exit_band_cap": "0.003", "exit_band_mult": "0", "trail_giveback_mult": "0"}
    c.cfg = lambda k, d=None: c._cfg.get(k, d)
    c.user_id = 1
    c.trading_pair = "BTC-PERP"

    assert c._trail_giveback() == c._arm_pct()



def test_dgrid_reversal_flip_respects_the_classifier_hysteresis():
    """DGRID-REVERSAL-FLIPFLOP — FIXED.

    ``variance_regime`` carries hysteresis and a directional release added
    specifically to stop side-flapping. The reversal path used to ignore
    ``last_direction`` / ``last_is_trend`` and arm a counter-trend ladder
    inside a declared trend (0.4% retrace → SHORT inside a 1.46-VR uptrend,
    classifier flipped it back ~60s later). It now consults that state and
    stays. Behavioural pin:
    ``test_dgrid_reversal_does_not_arm_a_short_inside_an_uptrend``.
    """
    import inspect

    from src.nadobro.engine.controllers import dynamic_grid

    src = inspect.getsource(dynamic_grid.DynamicGridController._maybe_reversal_flip)
    assert ("last_direction" in src) and ("last_is_trend" in src), (
        "_maybe_reversal_flip changes side without consulting the classifier's "
        "hysteresis, so it can arm a counter-trend ladder inside a declared trend"
    )


@pytest.mark.parametrize("strategy", ["grid", "dgrid"])
@pytest.mark.parametrize("spread_bp", [0.0, 0.1, 2.0, 3.0])
def test_a_grid_level_round_trip_can_never_complete_at_a_loss(strategy, spread_bp):
    """DGRID-FEE-FLOOR — FIXED.

    Each level's close leg sits exactly one step from its open leg, so a completed
    level earns ``step`` GROSS while paying a resting round trip: 1.5bp maker per
    side plus the 1bp builder routing policy locks on to both legs = 5bp. The
    mapper floored the step only when it was ZERO, so any positive value passed
    through raw — and the UI shipped a "Spread 2bp" preset button (plus a 3bp Turbo
    preset) with a settable range down to 0.1bp. Every completed level at those
    settings was a guaranteed net loss: the bot paid to trade.
    ``dgrid_min_spread_bp`` looked like the floor but only reaches
    ``spread_floor_half_pct``, which this path never reads.

    The invariant is "no level can complete at a LOSS", i.e. step >= fee round
    trip. Break-even is the boundary; how far ABOVE it a level should sit is a
    profit target and therefore a tuning decision for the operator, not something
    this floor should invent. ``dgrid_min_spread_bp`` still raises the floor when
    the user sets it higher.
    """
    from src.nadobro.quant.vol_fee_estimator import MIXED_ROUND_TRIP_RATE

    cfg = map_strategy_config(
        strategy, {"dgrid_spread_bp": spread_bp, "spread_bp": spread_bp},
        MID, product=PRODUCT,
    )
    step = Decimal(str(cfg.get("step_pct") or cfg.get("min_spread_between_orders") or 0))
    assert step >= MIXED_ROUND_TRIP_RATE, (
        f"{strategy} level round trip {step * 10000:.2f}bp is under the "
        f"{MIXED_ROUND_TRIP_RATE * 10000:.2f}bp worst-case round trip — a completed "
        "level loses money"
    )


def test_a_user_spread_above_the_fee_floor_is_left_alone():
    """The floor must not flatten a deliberate wider spread into itself."""
    cfg = map_strategy_config("dgrid", {"dgrid_spread_bp": 25.0}, MID, product=PRODUCT)
    step = Decimal(str(cfg.get("step_pct") or cfg.get("min_spread_between_orders") or 0))
    assert step == Decimal("0.0025"), f"25bp spread was altered to {step}"


def test_dgrid_min_spread_bp_now_raises_the_step_floor():
    """``dgrid_min_spread_bp`` had its own button and card row but only reached
    ``spread_floor_half_pct``, which the manual-step path never read — a dead
    input. It must now bind when it is above the fee floor."""
    cfg = map_strategy_config(
        "dgrid", {"dgrid_spread_bp": 1.0, "dgrid_min_spread_bp": 12.0},
        MID, product=PRODUCT,
    )
    step = Decimal(str(cfg.get("step_pct") or cfg.get("min_spread_between_orders") or 0))
    assert step == Decimal("0.0012"), f"dgrid_min_spread_bp ignored (step={step})"


# ==========================================================================
# OVERLAY-UNDOES-STEP-FLOOR (audit 2026-08-13, FIXED)
# ==========================================================================
@pytest.mark.parametrize("strategy", ["grid", "dgrid"])
def test_the_overlay_can_never_shrink_a_level_step_below_the_round_trip(strategy):
    """The mapper floors the per-level step at the worst-case round trip (6.8bp),
    but the overlay then scales spread keys by as little as 0.75x and clamped the
    result against a stale 1.5bp PER-SIDE constant — so a floored 6.8bp step became
    5.1bp, under the very cost the floor exists to clear. The mapped floor now
    travels in ``step_floor_pct`` and the step is clamped against ITS OWN quantity
    (a whole round trip) rather than a half-spread.
    """
    from src.nadobro.quant.vol_fee_estimator import MIXED_ROUND_TRIP_RATE
    from src.nadobro.strategy.overlay_actuator import apply_overrides_to_configs

    cfg = map_strategy_config(
        strategy, {"notional_usd": 100, "leverage": 5, "spread_bp": 2.0,
                   "dgrid_spread_bp": 2.0, "levels": 4, "fill_anchored": 0},
        MID, product=PRODUCT,
    )
    apply_overrides_to_configs(strategy, cfg, {"spread_factor": 0.75})

    step = cfg.get("min_spread_between_orders")
    assert step is not None
    assert Decimal(str(step)) >= MIXED_ROUND_TRIP_RATE, (
        f"{strategy}: overlay shrank the level step to {Decimal(str(step)) * 10000:.2f}bp, "
        f"under the {MIXED_ROUND_TRIP_RATE * 10000:.2f}bp round trip it must clear"
    )


def test_the_overlay_respects_a_user_raised_step_floor():
    """dgrid_min_spread_bp can raise the floor above the fee minimum; the overlay
    must honour that too, which is why the mapper ships the resolved value."""
    from src.nadobro.strategy.overlay_actuator import apply_overrides_to_configs

    cfg = map_strategy_config(
        "dgrid", {"notional_usd": 100, "leverage": 5, "dgrid_spread_bp": 20.0,
                  "dgrid_min_spread_bp": 18.0, "levels": 4},
        MID, product=PRODUCT,
    )
    apply_overrides_to_configs("dgrid", cfg, {"spread_factor": 0.75})
    assert Decimal(str(cfg["min_spread_between_orders"])) >= Decimal("0.0018")


def test_the_overlay_can_never_quote_a_side_through_the_fee_floor():
    """The per-SIDE floor was also stale: 1.5bp predated the mandatory 1bp builder
    leg, so the overlay could quote a 3bp round trip against a 5bp cost."""
    from src.nadobro.quant.vol_fee_estimator import MAKER_ROUND_TRIP_RATE
    from src.nadobro.strategy.overlay_actuator import apply_overrides_to_configs

    cfg = map_strategy_config(
        "mid", {"notional_usd": 100, "spread_bp": 2.0}, MID, product=PRODUCT,
    )
    apply_overrides_to_configs("mid", cfg, {"spread_factor": 0.75})
    half = MAKER_ROUND_TRIP_RATE / Decimal(2)
    for key in ("spread_bid_pct", "spread_ask_pct"):
        assert Decimal(str(cfg[key])) >= half, (
            f"{key} quoted through the per-side fee floor"
        )


def test_an_armed_rgrid_trail_latches_its_giveback():
    """RGRID-TRAIL-LOOSENS — FIXED.

    ``_trail_price`` used to re-derive give-back from the LIVE band, so an
    overlay/ATR widening moved an already-armed stop further from the peak.
    The give-back is now latched at arm. Behavioural pin:
    ``test_an_armed_trail_does_not_loosen_when_the_band_widens``.
    """
    import inspect

    from src.nadobro.engine.controllers import rgrid

    track = inspect.getsource(rgrid.RGridController._track_trail)
    price = inspect.getsource(rgrid.RGridController._trail_price)
    reset = inspect.getsource(rgrid.RGridController._reset_exposure_window)
    assert "_latched_giveback" in track and "_latched_giveback" in price, (
        "the trailing stop still re-derives give-back from the live band after arm"
    )
    assert "_latched_giveback" in reset, (
        "going flat must clear the latched give-back with the rest of the window"
    )


def test_dgrid_trend_phase_delegates_to_the_rgrid_follower():
    """D-Grid trend phase is R-Grid (add with the move), not ReverseGridExecutor."""
    import inspect

    from src.nadobro.engine.controllers import dynamic_grid

    src = inspect.getsource(dynamic_grid)
    assert "from src.nadobro.engine.controllers.rgrid import RGridController" in src
    assert "reverse_grid_executor" not in src
    assert "flatten_now" in inspect.getsource(
        dynamic_grid.DynamicGridController._flip_to
    )
