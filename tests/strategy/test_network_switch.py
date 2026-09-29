"""Fail-closed Nado network switch (NETSWITCH-FAIL-OPEN, Arcus plan research
2026-09-27, venue-switch-onboarding F2-F5).

The switch used to run the strategy teardown in a log-and-continue ``try`` and
flip ``users.network_mode`` regardless, with a weaker stop than the Stop button
(no scheduler unregister, no trigger sweep) and nothing for desk plans. Now it
stops what is live on the network being LEFT with Stop semantics, retries every
earlier stop whose venue half never confirmed (``strategy/pending_cleanup``),
cancels desk plans last, and flips only once every item is confirmed. Copy
mirrors, stop-loss rules and the managed agent are network-scoped and kept.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro import config  # noqa: E402
from src.nadobro.llm import managed_agent_state  # noqa: E402
from src.nadobro.models.database import UserRow  # noqa: E402
from src.nadobro.strategy import bot_runtime as br  # noqa: E402
from src.nadobro.strategy import network_switch as ns  # noqa: E402
from src.nadobro.strategy import pending_cleanup  # noqa: E402
from src.nadobro.trading import copy_service, desk_store, stop_loss_service  # noqa: E402
from src.nadobro.users import user_service  # noqa: E402

UID = 777
KEY_TESTNET = f"{br.STATE_PREFIX}{UID}:testnet"
PERP_IDS = {"BTC": 2, "ETH": 4}


class _Env:
    """Patches every seam the switch touches; records what it did."""

    def __init__(self, mp, store):
        self.store = store
        self.user = UserRow({"telegram_id": UID, "network_mode": "testnet", "main_address": "0x" + "cd" * 20})
        self.rows: dict[str, str] = {}
        self.flips: list = []
        self.cleanup = {"success": True}
        self.cleanup_seen_pending: list = []
        self.engine = (True, None)
        self.cancel_result = {"success": True, "cancelled_orders": 2}
        self.cancels: list = []
        self.unregistered: list = []
        self.plans: list | Exception = []
        self.cancel_plan_ok = True
        self.cancelled_plans: list = []
        self.plan_after_race = None
        self.copies: list = []
        self.sl_rules: list = []
        self.agent_enabled = False
        self.state_read_error: Exception | None = None

        mp.setattr(user_service, "get_user", lambda uid: self.user)
        mp.setattr(br, "get_user", lambda uid: self.user)

        def _execute(sql, params=None):
            if "network_mode" in str(sql):
                self.flips.append(params)

        mp.setattr(user_service, "execute", _execute)
        mp.setattr(user_service, "_invalidate_user_caches", lambda *a, **k: None)

        def _get_raw(key):
            if self.state_read_error is not None:
                raise self.state_read_error
            return self.rows.get(key)

        mp.setattr(br, "get_bot_state_raw", _get_raw)
        mp.setattr(br, "set_bot_state", lambda key, value: self.rows.__setitem__(
            key, value if isinstance(value, str) else json.dumps(value)))
        mp.setattr(br, "query_all", lambda _sql, params=(): [
            {"key": k, "value": v} for k, v in self.rows.items() if k.startswith(str(params[0]).rstrip("%"))
        ])
        mp.setattr(br, "_finalize_session", lambda *a, **k: None)
        mp.setattr(br, "_stop_engine_runtime_for_state", lambda *a, **k: self.engine)

        def _cleanup(*a, **k):
            self.cleanup_seen_pending.append({k: v["status"] for k, v in self.store.rows.items()})
            return dict(self.cleanup)

        mp.setattr(br, "cleanup_strategy_positions", _cleanup)
        resolve = lambda product, network=None, **k: PERP_IDS.get(str(product).upper())  # noqa: E731
        mp.setattr(br, "get_product_id", resolve)
        mp.setattr(config, "get_product_id", resolve)
        mp.setattr(br, "_sweep_bot_trigger_orders_sync", lambda *a, **k: None)

        def _cancel(uid, network=None, *, only_pid=None):
            self.cancels.append((uid, network, only_pid))
            return dict(self.cancel_result)

        mp.setattr("src.nadobro.trading.trade_service.cancel_resting_orders_for_user", _cancel)
        mp.setattr("src.nadobro.core.feature_flags.strategy_scheduler_enabled", lambda: True)
        mp.setattr(
            "src.nadobro.strategy.strategy_scheduler.get_scheduler",
            lambda: SimpleNamespace(unregister=lambda uid, net: self.unregistered.append((uid, net))),
        )

        mp.setattr(desk_store, "list_active_plans", self._list_plans)
        mp.setattr(desk_store, "cancel_plan", self._cancel_plan)
        mp.setattr(desk_store, "get_plan", lambda plan_id, net: self.plan_after_race)
        mp.setattr(copy_service, "get_user_copies", lambda uid, network=None: list(self.copies))
        mp.setattr(stop_loss_service, "query_all", lambda _sql, params=(): [
            {"key": f"stop_loss:{UID}:testnet:{r['product']}:x", "value": json.dumps(r)} for r in self.sl_rules
        ])
        mp.setattr(managed_agent_state, "is_managed_agent_globally_enabled", lambda: True)
        mp.setattr(managed_agent_state, "get_managed_agent_state", lambda uid: {"enabled": self.agent_enabled})

    def _list_plans(self, uid, network):
        if isinstance(self.plans, Exception):
            raise self.plans
        return list(self.plans)

    def _cancel_plan(self, plan_id, uid, network):
        self.cancelled_plans.append(plan_id)
        return self.cancel_plan_ok

    def put(self, **state):
        self.rows[KEY_TESTNET] = json.dumps(state)

    def state(self) -> dict:
        return json.loads(self.rows[KEY_TESTNET])

    def pending(self) -> list[dict]:
        return list(self.store.rows.values())

    def record(self, strategy="mid", product="BTC", run=5, **kw):
        pending_cleanup.record_failed(
            UID, "testnet", {"strategy": strategy, "product": product, "strategy_session_id": run},
            "user_stop", "rate limited", **kw,
        )

    def switch(self, target="mainnet"):
        return ns.switch_network(UID, target)


@pytest.fixture
def env(monkeypatch, pending_cleanup_store):
    return _Env(monkeypatch, pending_cleanup_store)


def _outcomes(result):
    return [(i.kind, i.outcome) for i in result.items]


def _plan(plan_id="p1", product="ETH", status="awaiting_trigger", market="perp"):
    return {"plan_id": plan_id, "status": status,
            "plan": SimpleNamespace(plan_id=plan_id, product=product, market=market)}


# -- the strategy run on the network being left ------------------------------

def test_a_running_strategy_is_stopped_with_stop_semantics_then_the_network_flips(env):
    env.put(running=True, strategy="grid", product="BTC", strategy_session_id=9)
    result = env.switch()
    assert result.switched and result.error == ""
    assert env.flips == [("mainnet", UID)]
    assert _outcomes(result) == [(ns.STRATEGY, ns.STOPPED)]
    assert env.state()["running"] is False
    assert env.pending() == [], "a confirmed stop leaves no pending cleanup"
    # Stop-button parity the old switch path skipped: the scheduler registration goes.
    assert env.unregistered == [(UID, "testnet")]


def test_the_stop_is_recorded_pending_before_its_venue_half_runs(env):
    # Crash-safety: if the process dies mid-cleanup, the record is already there.
    env.put(running=True, strategy="grid", product="BTC", strategy_session_id=9)
    env.switch()
    assert env.cleanup_seen_pending == [{
        f"{pending_cleanup.PREFIX}{UID}:testnet:grid:BTC:9": pending_cleanup.IN_PROGRESS,
    }]


def test_a_failed_cleanup_refuses_the_switch_and_a_retry_finishes_it(env):
    env.put(running=True, strategy="grid", product="BTC", strategy_session_id=9)
    env.cleanup = {"success": False, "error": "rate limited"}
    first = env.switch()
    assert not first.switched and first.error == ns.ERR_NOT_CONFIRMED
    assert env.flips == []
    [entry] = env.pending()
    assert entry["status"] == pending_cleanup.FAILED and entry["reason"] == "network_switch"
    assert (entry["strategy"], entry["product"]) == ("grid", "BTC") and entry["flatten_unconfirmed"] is True

    # Retry: nothing is running any more; the record makes the switch finish the
    # cleanup (cancel-only, scoped to the recorded product) before flipping.
    second = env.switch()
    assert second.switched
    assert _outcomes(second) == [(ns.LEFTOVER_ORDERS, ns.STOPPED)]
    assert second.items[0].warning == ns.POSITION_MAY_BE_OPEN
    assert env.cancels == [(UID, "testnet", 2)]
    assert env.pending() == []
    assert env.flips == [("mainnet", UID)]


def test_a_retry_that_still_cannot_confirm_keeps_the_user_on_the_old_network(env):
    env.record()
    env.cancel_result = {"success": False, "error": "Could not confirm the order book is clear"}
    result = env.switch()
    assert not result.switched
    assert _outcomes(result) == [(ns.LEFTOVER_ORDERS, ns.FAILED)]
    assert env.flips == []
    assert len(env.pending()) == 1, "the record must survive an unconfirmed retry"


def test_a_pending_cleanup_survives_a_new_start_on_another_product(env):
    # A new Start rebuilds the run's state from scratch; the record of the old
    # product's unconfirmed stop must not go with it.
    env.record(strategy="grid", product="BTC", run=1)
    env.put(running=True, strategy="mid", product="ETH", strategy_session_id=2)
    result = env.switch()
    assert result.switched
    assert _outcomes(result) == [(ns.STRATEGY, ns.STOPPED), (ns.LEFTOVER_ORDERS, ns.STOPPED)]
    assert (UID, "testnet", 2) in env.cancels, "the old BTC run's leftovers were swept"
    assert env.pending() == []


def test_an_unconfirmed_engine_stop_also_blocks(env):
    env.put(running=True, strategy="grid", product="BTC")
    env.engine = (False, "timeout")
    result = env.switch()
    assert not result.switched
    assert "engine cleanup failed" in result.items[0].error


def test_a_trigger_sweep_that_raises_blocks_and_stays_retriable(env, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("trigger service down")

    monkeypatch.setattr(br, "_sweep_bot_trigger_orders_sync", _boom)
    env.put(running=True, strategy="rgrid", product="BTC", strategy_session_id=3)
    result = env.switch()
    assert not result.switched
    assert "trigger" in result.items[0].error
    assert len(env.pending()) == 1


def test_a_long_stopped_run_without_a_record_is_not_swept(env):
    # No implicit cancel of a stale run's product (it could hit manual orders):
    # only a stop whose venue half never confirmed is retried.
    env.put(running=False, strategy="grid", product="BTC")
    result = env.switch()
    assert result.switched and result.items == ()
    assert env.cancels == []


def test_a_pending_vol_run_is_swept_on_its_spot_product(env, monkeypatch):
    monkeypatch.setattr(config, "get_spot_product_id", lambda name, network=None, **k: 5)
    env.record(strategy="vol", product="KBTC")
    assert env.switch().switched
    assert env.cancels == [(UID, "testnet", 5)]


def test_a_pending_dn_run_with_an_unresolvable_leg_blocks(env, monkeypatch):
    monkeypatch.setattr(config, "get_dn_pair", lambda product, network=None, **k: {"perp_product_id": 2})
    env.record(strategy="dn", product="BTC")
    result = env.switch()
    assert not result.switched
    assert env.cancels == [], "never widen to an unscoped cancel"


# -- nothing irreversible happens before every read succeeded ---------------------

def test_an_unreadable_strategy_state_refuses_with_no_side_effects(env):
    env.state_read_error = RuntimeError("db down")
    env.plans = [_plan()]
    result = env.switch()
    assert not result.switched
    assert _outcomes(result) == [(ns.STRATEGY, ns.FAILED)]
    assert env.cancelled_plans == [] and env.flips == []


def test_an_unreadable_desk_table_refuses_before_the_strategy_is_stopped(env):
    env.put(running=True, strategy="grid", product="BTC")
    env.plans = RuntimeError("db down")
    result = env.switch()
    assert not result.switched
    assert env.state()["running"] is True, "nothing may be stopped/flattened on a refused switch"


def test_an_unreadable_pending_list_refuses_before_the_strategy_is_stopped(env, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.store, "scan", _boom)
    env.put(running=True, strategy="grid", product="BTC")
    result = env.switch()
    assert not result.switched
    assert env.state()["running"] is True


def test_desk_plans_are_not_cancelled_when_the_strategy_stop_fails(env):
    env.put(running=True, strategy="grid", product="BTC")
    env.cleanup = {"success": False, "error": "rate limited"}
    env.plans = [_plan()]
    result = env.switch()
    assert not result.switched
    assert env.cancelled_plans == [], "a desk cancel cannot be undone — skip it on a refused switch"
    assert (ns.DESK_PLAN, ns.SKIPPED) in _outcomes(result)


# -- desk plans ------------------------------------------------------------------

def test_an_armed_desk_plan_is_cancelled_without_a_venue_sweep(env):
    env.plans = [_plan(status="awaiting_trigger")]
    result = env.switch()
    assert result.switched
    assert _outcomes(result) == [(ns.DESK_PLAN, ns.STOPPED)]
    assert env.cancels == [], "an armed plan has no venue orders"


def test_a_running_desk_plan_has_its_product_swept_before_the_flip(env):
    env.plans = [_plan(status="running", product="ETH")]
    result = env.switch()
    assert result.switched
    assert env.cancels == [(UID, "testnet", 4)]
    assert result.items[0].warning == ns.POSITION_MAY_BE_OPEN   # fills are kept, as for a /desk Cancel
    assert env.pending() == []


def test_an_armed_plan_whose_trigger_fired_during_the_switch_is_swept(env):
    env.plans = [_plan(status="awaiting_trigger", product="ETH")]
    env.plan_after_race = {"status": "cancelled", "started_at": "2026-09-28T08:00:00Z"}
    result = env.switch()
    assert result.switched
    assert env.cancels == [(UID, "testnet", 4)]


def test_a_running_desk_plan_whose_sweep_fails_blocks_and_stays_retriable(env):
    env.plans = [_plan(status="running", product="ETH")]
    env.cancel_result = {"success": False, "error": "rate limited"}
    result = env.switch()
    assert not result.switched
    [entry] = env.pending()
    assert entry["strategy"] == "desk" and entry["product_ids"] == [4]

    # The plan is cancelled now (not listed any more), but its record is retried.
    env.plans = []
    env.cancel_result = {"success": True, "cancelled_orders": 1}
    assert env.switch().switched
    assert env.pending() == []


def test_an_unreadable_desk_table_blocks(env):
    env.plans = RuntimeError("db down")
    result = env.switch()
    assert not result.switched and env.flips == []


def test_a_desk_plan_still_active_after_a_lost_cancel_blocks(env):
    env.plans = [_plan()]
    env.cancel_plan_ok = False
    env.plan_after_race = {"status": "running"}
    result = env.switch()
    assert not result.switched
    assert _outcomes(result) == [(ns.DESK_PLAN, ns.FAILED)]


def test_a_desk_plan_the_runner_finished_first_counts_as_stopped(env):
    env.plans = [_plan()]
    env.cancel_plan_ok = False
    env.plan_after_race = {"status": "completed"}
    assert env.switch().switched


# -- kept (network-scoped) automation ------------------------------------------

def test_copy_mirrors_stop_losses_and_agent_mode_are_kept_and_reported(env):
    env.copies = [{"mirror_id": 3, "trader_label": "whale"}]
    env.sl_rules = [{"active": True, "product": "btc"}, {"active": False, "product": "eth"}]
    env.agent_enabled = True
    result = env.switch()
    assert result.switched
    assert sorted(_outcomes(result)) == sorted([
        (ns.COPY_MIRROR, ns.KEPT), (ns.STOP_LOSS_RULE, ns.KEPT), (ns.MANAGED_AGENT, ns.KEPT),
    ])


def test_a_failed_read_of_kept_automation_does_not_block(env, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(copy_service, "get_user_copies", _boom)
    assert env.switch().switched


# -- edges ---------------------------------------------------------------------

def test_switching_to_the_current_network_stops_nothing(env):
    env.put(running=True, strategy="grid", product="BTC")
    result = env.switch("testnet")
    assert result.switched and result.items == ()
    assert env.state()["running"] is True
    assert env.flips == []


def test_invalid_network_and_unknown_user(env, monkeypatch):
    assert env.switch("devnet").error == ns.ERR_INVALID_NETWORK
    monkeypatch.setattr(user_service, "get_user", lambda uid: None)
    assert env.switch().error == ns.ERR_USER_NOT_FOUND
    assert env.flips == []


def test_a_failed_flip_is_reported(env, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(user_service, "execute", _boom)
    result = env.switch()
    assert not result.switched and result.error == ns.ERR_FLIP_FAILED


# -- Stop / /stop_all share the record ------------------------------------------

def test_stop_button_records_an_unconfirmed_cleanup_and_its_retry_clears_it(env):
    env.put(running=True, strategy="grid", product="BTC", strategy_session_id=9)
    env.cleanup = {"success": False, "error": "rate limited"}
    ok, _msg = br.stop_user_bot(UID, True)
    assert not ok
    assert len(env.pending()) == 1

    ok, msg = br.stop_user_bot(UID, True)  # repeat Stop -> cancel-only retry of the record
    assert ok, msg
    assert env.pending() == []


def test_stop_button_reports_an_unconfirmed_trigger_sweep(env, monkeypatch):
    monkeypatch.setattr(br, "_sweep_bot_trigger_orders_sync",
                        lambda *a, **k: {"success": False, "error": "trigger read throttled"})
    env.put(running=True, strategy="rgrid", product="BTC", strategy_session_id=4)
    ok, msg = br.stop_user_bot(UID, True)
    assert not ok and "trigger" in msg
    assert len(env.pending()) == 1


def test_stop_all_now_unregisters_the_scheduler_like_stop(env):
    env.put(running=True, strategy="grid", product="BTC")
    ok, _msg = br.stop_all_user_bots(UID, cancel_orders=True)
    assert ok
    assert env.unregistered == [(UID, "testnet")]


# -- rendering -----------------------------------------------------------------

def test_the_refusal_names_what_is_live_and_where_the_user_still_is(env):
    from src.nadobro.handlers.formatters import fmt_network_switch_result

    env.put(running=True, strategy="grid", product="BTC")
    env.cleanup = {"success": False, "error": "rate limited"}
    text = fmt_network_switch_result(env.switch())
    assert "still on testnet" in text
    assert "GRID BTC" in text and "rate limited" in text
    assert "Switched to" not in text


def test_the_confirmation_lists_stopped_kept_and_possibly_open_positions(env):
    from src.nadobro.handlers.formatters import fmt_network_switch_result

    env.put(running=True, strategy="grid", product="BTC")
    env.plans = [_plan(status="running", product="ETH")]
    env.copies = [{"mirror_id": 3, "trader_label": "whale"}]
    env.agent_enabled = True
    text = fmt_network_switch_result(env.switch())
    assert text.startswith("Switched to mainnet mode\\.")  # MarkdownV2, escaped
    assert "Stopped on testnet before switching: GRID BTC, desk plan ETH" in text
    assert "Still running on testnet \\(unaffected by the switch\\): copy of whale" in text
    assert "desk plan ETH" in text.split("open position")[-1]
