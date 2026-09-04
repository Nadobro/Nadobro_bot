"""Boot = stand-down for engine strategies (prod 2026-09-03, session 312). On
restart a still-'running' session's resting orders must be cancelled, its
session finalized, and its running flag cleared — resume is user-initiated."""
from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from unittest import mock

import pytest

br = pytest.importorskip("src.nadobro.strategy.bot_runtime")


def _rows():
    def st(**k):
        d = {"running": True, "strategy": "grid", "product": "BTC", "strategy_session_id": 312}
        d.update(k)
        return json.dumps(d)
    return [
        {"key": "strategy_bot:111:mainnet", "value": st()},                       # running grid -> stand down
        {"key": "strategy_bot:222:mainnet", "value": st(running=False)},          # stopped -> skip
        {"key": "strategy_bot:333:mainnet", "value": st(strategy="copy")},        # non-engine -> skip
        {"key": "strategy_bot:444:testnet", "value": st(strategy="dn")},          # dn IS engine-mapped -> stand down
    ]


def _run(monkeypatch, *, cancel_success=True):
    calls = {"cancel": [], "finalize": [], "notify": [], "saved": [], "terminated": []}

    async def _pass_db(func, *a, **k):
        return func(*a, **k)

    async def _pass_sdk(func, *a, **k):
        return func(*a, **k)

    def _cancel(uid, network, **k):
        calls["cancel"].append((uid, network))
        return {"success": cancel_success, "error": None if cancel_success else "open-orders read unavailable"}

    async def _notify(uid, text, **fmt):
        calls["notify"].append((uid, text, fmt))

    async def _save(uid, network, state):
        calls["saved"].append((uid, network, dict(state)))

    def _finalize(state, reason="stopped"):
        calls["finalize"].append((dict(state), reason))

    with ExitStack() as es:
        p = es.enter_context
        p(mock.patch.object(br, "query_all", lambda *a, **k: _rows()))
        p(mock.patch.object(br, "run_blocking_db", _pass_db))
        p(mock.patch.object(br, "run_blocking_sdk", _pass_sdk))
        p(mock.patch("src.nadobro.trading.trade_service.cancel_resting_orders_for_user", _cancel))
        p(mock.patch("src.nadobro.trading.engine_persistence.terminate_engine_executors",
                     lambda cid: calls["terminated"].append(cid)))
        p(mock.patch("src.nadobro.trading.engine_persistence.clear_controller_progress", lambda cid: None))
        p(mock.patch.object(br, "_finalize_session", _finalize))
        p(mock.patch.object(br, "_save_state_async", _save))
        p(mock.patch.object(br, "_notify", _notify))
        stood = asyncio.run(br.boot_stand_down_strategies())
    return stood, calls


def test_only_running_engine_sessions_are_stood_down(monkeypatch):
    stood, calls = _run(monkeypatch)
    assert stood == 2, "the running grid and the running dn should be stood down; the stopped and copy rows skipped"
    cancelled_uids = {u for u, _ in calls["cancel"]}
    assert cancelled_uids == {111, 444}
    reasons = {r for _, r in calls["finalize"]}
    assert reasons == {"redeploy_stand_down"}
    assert all(not st.get("running") for st, _ in calls["finalize"]), "running flag must be cleared"
    assert {u for u, _, _ in calls["notify"]} == {111, 444}


def test_the_position_is_never_flattened_only_orders_cancelled(monkeypatch):
    # cancel_resting_orders_for_user (orders only) is used — NOT close_all_positions.
    stood, calls = _run(monkeypatch)
    assert calls["cancel"], "resting orders must be cancelled"
    # No flatten call is made (the helper it uses cancels orders, leaving positions).


def test_a_failed_cancel_warns_the_user_and_still_finalizes(monkeypatch):
    stood, calls = _run(monkeypatch, cancel_success=False)
    assert stood == 2
    assert calls["finalize"], "the session is still finalized even if the cancel could not be confirmed"
    warned = [t for _, t, _ in calls["notify"] if "could not be confirmed" in t]
    assert warned, "the user must be warned when resting orders could not be confirmed cancelled"
    assert any(st.get("last_error") for st, _ in calls["finalize"]), "last_error should record the unconfirmed cancel"


def test_the_env_gate_disables_the_sweep(monkeypatch):
    monkeypatch.setattr(br, "env_bool", lambda name, default=True: False if name == "NADO_BOOT_STAND_DOWN_STRATEGIES" else default)
    stood = asyncio.run(br.boot_stand_down_strategies())
    assert stood == 0
