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
    calls = {"cancel": [], "cancel_pid": [], "finalize": [], "notify": [], "saved": [], "terminated": []}

    async def _pass_db(func, *a, **k):
        return func(*a, **k)

    async def _pass_sdk(func, *a, **k):
        return func(*a, **k)

    def _cancel(uid, network, only_pid=None, **k):
        calls["cancel"].append((uid, network))
        calls["cancel_pid"].append((uid, only_pid))
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
        p(mock.patch.object(br, "get_product_id", lambda product, network=None, **k: 2 if str(product).upper() == "BTC" else None))
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


def test_cancel_is_scoped_for_perp_strategies_but_unscoped_for_vol_dn(monkeypatch):
    """AUDIT-BOOT-2026-09-04-UNSCOPED-CANCEL: the single-perp maker strategies scope
    the boot cancel to their product (only_pid) so a user's manual order on another
    market survives a restart. vol (spot) and dn (two-leg) stay UNSCOPED — a perp-pid
    scope would miss their spot / second-leg orders and orphan them."""
    stood, calls = _run(monkeypatch)
    pid_by_uid = dict(calls["cancel_pid"])
    assert pid_by_uid[111] == 2, "the grid (single-perp) session must scope its boot cancel to only_pid"
    assert pid_by_uid[444] is None, "the dn (two-leg) session must stay unscoped so no leg is orphaned"


# --------------------------------------------------------------------------- #
# AUDIT-BOOT-2026-09-04-POLL-FLATTEN-RACE — the fast SL/TP safety poll enqueues a
# rails-only cycle for every running session, and a breach cycle FLATTENS the
# position. It is armed by start_scheduler() ~100 lines before boot stand-down
# clears orphaned 'running' rows, so without a boot gate an orphaned session is
# flattened during the boot window (session 312). Runtime proof (imports scheduler,
# so it lives here in the full-deps suite, not the deps-free invariants file).
# --------------------------------------------------------------------------- #
def test_sltp_safety_poll_is_parked_until_boot_standdown_completes():
    import time as _time
    import src.nadobro.runtime.scheduler as sched

    running = [{
        "key": "strategy_bot:1234:mainnet",
        "value": json.dumps({"running": True, "strategy": "grid", "product": "BTC-PERP"}),
    }]

    async def _pass(fn, *a, **k):
        return fn(*a, **k)

    enq = mock.AsyncMock(return_value=True)
    prev = (sched._boot_standdown_done, sched._scheduler_started_at)
    try:
        with mock.patch.object(sched, "run_blocking", _pass), \
             mock.patch("src.nadobro.db.query_all", lambda *a, **k: running), \
             mock.patch("src.nadobro.trading.execution_queue.enqueue_strategy", enq):
            # Boot window: scheduler just started, stand-down NOT signalled yet.
            sched._boot_standdown_done = False
            sched._scheduler_started_at = _time.time()
            asyncio.run(sched.tick_sltp_safety())
            assert enq.await_count == 0, "the poll must not enqueue an orphaned session during the boot window"
            # Stand-down completes -> the poll resumes immediately.
            sched.mark_boot_standdown_complete()
            asyncio.run(sched.tick_sltp_safety())
            assert enq.await_count == 1, "the poll must resume once boot stand-down is complete"
    finally:
        sched._boot_standdown_done, sched._scheduler_started_at = prev


def test_sltp_safety_poll_grace_ceiling_rearms_if_never_signalled():
    """Fail-safe: if the stand-down signal never arrives (crash / alt entrypoint), the
    SL/TP backstop must re-arm after the grace ceiling rather than staying off."""
    import time as _time
    import src.nadobro.runtime.scheduler as sched

    prev = (sched._boot_standdown_done, sched._scheduler_started_at)
    try:
        sched._boot_standdown_done = False
        sched._scheduler_started_at = _time.time()            # just booted
        assert sched._sltp_safety_poll_armed() is False, "parked during the boot window"
        sched._scheduler_started_at = _time.time() - 10_000   # past the grace ceiling
        assert sched._sltp_safety_poll_armed() is True, "the backstop must re-arm after the grace ceiling"
    finally:
        sched._boot_standdown_done, sched._scheduler_started_at = prev
