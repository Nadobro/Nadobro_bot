"""Mid quoting fixes (2026-08-30) — the live-log root causes of Mid placing 298
orders / 0 fills on a thin, rate-limited venue, plus the ~15-min gate flap:

1. Rate-aware ladder-depth cap (levels=20 -> a placeable few) so the ladder can
   rest and fill instead of churning one order/sec.
2. max_quote_lifetime_s floored above the enforced cadence (was 6s < 8s), so
   _should_hold stops force-refreshing every quote every tick.
3. Gate-flap fix: the overlay's arm is sticky (a decaying dwell) and an
   overlay-driven disarm emits no user "resumed" card (the latter is pinned in
   tests/engine/test_regime_gate.py::test_disabling_the_gate_clears_a_stale_pause).
4. Volume objective (opt-in, default OFF) resolves quote_mode -> touch so the near
   rung joins the live book and actually fills.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.nadobro.strategy import engine_runtime as er

_BASE = {"notional_usd": 150.0, "spread_bp": 2.0, "leverage": 40,
         "mid_execution_mode": "aggressive", "interval_seconds": 60}


def _mid(**over):
    return er.map_strategy_config("mid", {**_BASE, **over}, Decimal("78000"),
                                  product="BTC", leverage=40)


# ── Fix 1: rate-aware ladder-depth cap ──────────────────────────────────────
def test_deep_ladder_is_capped_for_mid():
    cfg = _mid(levels=20)
    # levels=20 is uncapped-infeasible at ~1 order/sec on an 8s cadence; the cap
    # brings it to a placeable few (floor(place_rate*cadence/2) bounded by the
    # absolute ceiling), well below 20 and >= 1.
    assert 1 <= cfg["ladder_levels"] <= 6
    assert cfg["ladder_levels"] < 20
    # A user asking for few levels is NOT inflated by the cap.
    assert _mid(levels=2)["ladder_levels"] == 2


def test_depth_cap_is_env_tunable(monkeypatch):
    monkeypatch.setenv("NADO_MM_MAX_LADDER_LEVELS", "2")
    assert _mid(levels=20)["ladder_levels"] == 2


def test_depth_cap_is_mid_only_grid_rgrid_untouched():
    # Grid/R-Grid map ladder depth elsewhere and must be unchanged by the cap.
    for strat in ("grid", "rgrid", "dgrid"):
        cfg = er.map_strategy_config(strat, {**_BASE, "levels": 20}, Decimal("78000"),
                                     product="BTC", leverage=40)
        # None of these route through the mid ladder_levels cap.
        assert cfg.get("ladder_levels") in (None, 20) or int(cfg.get("levels_count", 20)) == 20


# ── Fix 2: TTL must exceed the enforced cadence ─────────────────────────────
def test_quote_ttl_exceeds_cadence():
    cfg = _mid()
    # Enforced cadence for mid is min(interval, NADO_FAST_CADENCE_SECONDS=8) = 8s;
    # min_quote_lifetime_s = 2x cadence = 16s. The TTL must be floored at that so a
    # quote is not stale before its next reconcile (the churn).
    assert cfg["max_quote_lifetime_s"] >= cfg["min_quote_lifetime_s"]
    assert float(cfg["max_quote_lifetime_s"]) >= 16.0
    # The raw aggressive profile ttl (6s) sat UNDER the cadence — it must be lifted.
    assert float(cfg["max_quote_lifetime_s"]) > 8.0


# ── Fix 4: volume objective opt-in -> touch pricing ─────────────────────────
def test_quote_mode_defaults_to_mid():
    assert _mid()["quote_mode"] == "mid"


def test_volume_objective_opts_into_touch():
    cfg = _mid(mid_objective="volume")
    assert cfg["quote_mode"] == "touch"
    # touch mode uses the planner's auto step (one tick), not a spread-sized step.
    assert cfg["ladder_step_bp"] == Decimal(0)


def test_explicit_touch_mode_still_works():
    assert _mid(mm_quote_mode="touch")["quote_mode"] == "touch"
    # and an explicit mid wins over the volume objective (explicit user choice).
    assert _mid(mm_quote_mode="mid", mid_objective="volume")["quote_mode"] == "mid"


# ── Fix 3a: the overlay's gate arm is sticky (decaying dwell) ────────────────
class _FakeClient:
    def get_candlesticks(self, product_id, timeframe, limit, max_time=None):
        return [{"close": 100 + i * 0.4, "high": 100 + i * 0.4 + 1,
                 "low": 100 + i * 0.4 - 1, "volume": 10} for i in range(80)]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from src.nadobro.strategy import market_features as mf
    mf.reset_cache()
    er._OVERLAY_FUNDING_CACHE.clear()
    monkeypatch.setenv("NADO_SIGNAL_OVERLAY", "1")
    import src.nadobro.models.database as db
    monkeypatch.setattr(db, "insert_overlay_signal", lambda row: 1, raising=False)
    yield
    mf.reset_cache()
    er._OVERLAY_FUNDING_CACHE.clear()


def _overlay(cfg, state, suppress, monkeypatch):
    """Drive _maybe_apply_overlay with a CONTROLLED suppress decision so the sticky
    arm can be exercised deterministically (independent of the signal pipeline)."""
    from src.nadobro.strategy import overlay_actuator as oa

    def _fake_overrides(strategy, signal):
        return {"suppress_new_entries": bool(suppress), "regime": "chop" if suppress else "trend",
                "size_factor": 1.0, "spread_factor": 1.0, "bias": 0.0, "confidence": 0.5}

    monkeypatch.setattr(oa, "compute_overrides", _fake_overrides)
    monkeypatch.setattr(oa, "stabilize_overrides", lambda prev, ov: ov)
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))


def test_mid_gate_arm_is_sticky_then_decays(monkeypatch):
    monkeypatch.setenv("NADO_MID_GATE_ARM_DWELL_CYCLES", "2")
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}

    def _cfg():
        return {"order_amount_quote": Decimal("500"), "spread_bid_pct": Decimal("0.0005"),
                "spread_ask_pct": Decimal("0.0005"), "directional_bias": 0.0,
                "regime_gate_enabled": 0.0}

    # 1) suppress -> gate armed + dwell primed.
    c = _cfg(); _overlay(c, state, suppress=True, monkeypatch=monkeypatch)
    assert c["regime_gate_enabled"] is True
    assert int(state["mid_gate_arm_dwell"]) == 2

    # 2) suppress clears -> gate STAYS armed during the dwell (no flip).
    c = _cfg(); _overlay(c, state, suppress=False, monkeypatch=monkeypatch)
    assert c["regime_gate_enabled"] is True
    assert int(state["mid_gate_arm_dwell"]) == 1

    c = _cfg(); _overlay(c, state, suppress=False, monkeypatch=monkeypatch)
    assert c["regime_gate_enabled"] is True
    assert int(state["mid_gate_arm_dwell"]) == 0

    # 3) dwell exhausted -> the gate disarms (mapper default carries through).
    c = _cfg(); _overlay(c, state, suppress=False, monkeypatch=monkeypatch)
    assert c["regime_gate_enabled"] in (0.0, False)

    # 4) a fresh suppress re-primes the dwell (no permanent arm, no permanent off).
    c = _cfg(); _overlay(c, state, suppress=True, monkeypatch=monkeypatch)
    assert c["regime_gate_enabled"] is True
    assert int(state["mid_gate_arm_dwell"]) == 2


def test_mid_gate_arm_survives_a_transient_overlay_skip(monkeypatch):
    """A cold candle cache makes _maybe_apply_overlay early-return before it can
    re-apply the overlay. During a dwell, the gate must STAY armed (re-asserted
    before the fetch) so the transient skip cannot disarm it for one tick and churn
    a teardown; and the dwell must NOT decay on a failed cycle."""
    monkeypatch.setenv("NADO_MID_GATE_ARM_DWELL_CYCLES", "5")
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0,
             "mid_gate_arm_dwell": 3}
    cfg = {"order_amount_quote": Decimal("500"), "spread_bid_pct": Decimal("0.0005"),
           "spread_ask_pct": Decimal("0.0005"), "directional_bias": 0.0,
           "regime_gate_enabled": 0.0}

    # A cold multi-timeframe feature cache -> _maybe_apply_overlay hits
    # `if not features: return`, early-returning AFTER the top-of-function dwell
    # re-arm has run (which precedes the fetch).
    from src.nadobro.strategy import market_features as mf
    monkeypatch.setattr(mf, "multi_tf_features", lambda *a, **k: {})
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))
    assert cfg["regime_gate_enabled"] is True, "gate stays armed through a transient skip"
    assert int(state["mid_gate_arm_dwell"]) == 3, "dwell does not decay on a failed cycle"
