"""Mid Mode UX fixes (2026-07-31 session-178 post-mortem).

1. GATE-VISIBILITY: while the regime gate pauses quoting, /status used to read
   "LIVE … Last cycle: OK" with zero orders — the dark state was invisible.
   The card must name the paused state, the human reason, and how long.
2. BIAS-ON-CARD: direction was only reachable via Advanced → Core (row 7 of
   13) and users reported Mid had "no long/short setting". The start card now
   carries a one-tap Short / Neutral / Long row with a ✅ on the active lean.
"""

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers.formatters import fmt_status_overview  # noqa: E402
from src.nadobro.handlers.keyboards import strategy_action_kb  # noqa: E402

_ONBOARDING_OK = {
    "onboarding_complete": True,
    "network": "mainnet",
    "has_key": True,
    "funded": True,
}


def _mid_status(**extra) -> dict:
    base = {
        "running": True,
        "strategy": "mid",
        "product": "BTC",
        "runs": 21,
        "interval_seconds": 15,
        "spread_bp": 5.0,
        "notional_usd": 100.0,
        "cycle_notional_usd": 100.0,
        "last_cycle_result": "ok",
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------
# 1. Gate visibility on /status
# --------------------------------------------------------------------------
def test_status_names_paused_quoting_with_human_reason():
    text = fmt_status_overview(
        _mid_status(
            mm_gate_verdict="PAUSE",
            mm_gate_reason="expansion",
            mm_gate_since_ts=0.0,  # no age line; the state line must still show
        ),
        _ONBOARDING_OK,
    )
    assert "PAUSED" in text
    assert "range is expanding" in text
    assert "resume" in text.lower()


def test_status_shows_quoting_active_when_gate_quotes():
    text = fmt_status_overview(
        _mid_status(mm_gate_verdict="QUOTE", mm_gate_reason=""),
        _ONBOARDING_OK,
    )
    assert "PAUSED" not in text
    assert "active" in text


def test_status_surfaces_mid_execution_inventory_and_budget_stop():
    text = fmt_status_overview(
        _mid_status(
            mid_execution_mode="passive",
            inventory_state="hard_long",
            expected_budget_usd=10.0,
            expected_budget_used_usd=10.0,
            budget_stop_reason="expected_budget_exhausted",
        ),
        _ONBOARDING_OK,
    )
    assert "PASSIVE" in text
    assert "hard\\_long" in text
    assert "Expected budget" in text
    assert "expected budget exhausted" in text


def test_status_without_gate_keys_renders_no_quoting_line():
    text = fmt_status_overview(_mid_status(), _ONBOARDING_OK)
    assert "Quoting" not in text


def test_status_unknown_gate_reason_falls_back_to_generic_copy():
    text = fmt_status_overview(
        _mid_status(mm_gate_verdict="PAUSE", mm_gate_reason="mystery_state"),
        _ONBOARDING_OK,
    )
    assert "PAUSED" in text
    assert "unfavourable regime" in text


# --------------------------------------------------------------------------
# 2. Bias row on the Mid start card
# --------------------------------------------------------------------------
def _flat_buttons(kb):
    return [btn for row in kb.inline_keyboard for btn in row]


def test_mid_start_card_carries_bias_row():
    kb = strategy_action_kb("mid", "BTC", ["BTC", "ETH", "SOL"], mid_bias=0.0)
    callbacks = [btn.callback_data for btn in _flat_buttons(kb)]
    assert "strategy:bias:mid:-0.5" in callbacks
    assert "strategy:bias:mid:0" in callbacks
    assert "strategy:bias:mid:0.5" in callbacks


def test_mid_bias_row_marks_the_active_lean():
    kb = strategy_action_kb("mid", "BTC", ["BTC"], mid_bias=0.5)
    by_cb = {btn.callback_data: btn.text for btn in _flat_buttons(kb)}
    assert by_cb["strategy:bias:mid:0.5"].startswith("✅")
    assert not by_cb["strategy:bias:mid:0"].startswith("✅")
    assert not by_cb["strategy:bias:mid:-0.5"].startswith("✅")

    kb_short = strategy_action_kb("mid", "BTC", ["BTC"], mid_bias=-1.0)
    by_cb_short = {btn.callback_data: btn.text for btn in _flat_buttons(kb_short)}
    assert by_cb_short["strategy:bias:mid:-0.5"].startswith("✅")


def test_other_strategies_have_no_bias_row():
    for sid in ("grid", "rgrid", "dgrid", "vol", "dn"):
        kb = strategy_action_kb(sid, "BTC", ["BTC"])
        callbacks = [btn.callback_data for btn in _flat_buttons(kb)]
        assert not any(cb.startswith("strategy:bias:") for cb in callbacks), sid


# --------------------------------------------------------------------------
# MID-GATE-STATUS-DEAD (audit 2026-07-31): the formatter read mm_gate_* keys,
# but get_user_bot_status — its only production feed — never exported them, so
# the "Quoting: PAUSED" line could never render. Pin the WHOLE path: worker
# state → get_user_bot_status → fmt_status_overview.
# --------------------------------------------------------------------------
def test_gate_pause_renders_end_to_end_through_get_user_bot_status(monkeypatch):
    from src.nadobro.strategy import bot_runtime as br

    worker_state = {
        "running": True,
        "strategy": "mid",
        "product": "BTC",
        "runs": 21,
        "interval_seconds": 15,
        "spread_bp": 5.0,
        "notional_usd": 100.0,
        "cycle_notional_usd": 100.0,
        "last_run_ts": 0.0,
        "mm_gate_verdict": "PAUSE",
        "mm_gate_reason": "expansion",
        "mm_gate_since_ts": 0.0,
    }
    monkeypatch.setattr(br, "get_user", lambda _tid: None)
    monkeypatch.setattr(br, "_load_state", lambda _tid, _net: dict(worker_state))
    monkeypatch.setattr(br, "is_trading_paused", lambda: False)
    monkeypatch.setattr(br, "get_running_strategy_sessions", lambda *_a, **_k: [])
    monkeypatch.setattr(br, "get_trade_analytics", lambda *_a, **_k: {"total_trades": 0})

    status = br.get_user_bot_status(1)
    assert status["mm_gate_verdict"] == "PAUSE"
    assert status["mm_gate_reason"] == "expansion"

    text = fmt_status_overview(status, _ONBOARDING_OK)
    assert "PAUSED" in text
    assert "range is expanding" in text
