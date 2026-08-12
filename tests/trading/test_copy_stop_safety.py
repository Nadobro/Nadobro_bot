"""Copy-stop regressions: no hidden exposure after a failed close."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.nadobro.models import database as database_model
from src.nadobro.trading import copy_service


def test_user_stop_keeps_failed_flatten_monitored_for_retry():
    mirror = {"id": 7, "user_id": 4242, "active": True, "network": "mainnet"}
    with patch.object(copy_service, "get_copy_mirror", return_value=mirror), \
         patch.object(copy_service, "_flatten_mirror_positions", return_value=(0, 0.0, 0.0, ["BTC: venue down"])), \
         patch.object(copy_service, "request_copy_mirror_stop") as request_stop, \
         patch.object(copy_service, "stop_copy_mirror") as stop, \
         patch.object(copy_service, "_finalize_mirror_session") as finalize:
        ok, message = copy_service.stop_copy(4242, 7)

    assert not ok
    assert "remains monitored" in message
    request_stop.assert_called_once_with(7)
    stop.assert_not_called()
    finalize.assert_not_called()


def test_stop_all_reports_failure_when_every_copy_close_is_pending():
    mirrors = [
        {"id": 7, "user_id": 4242, "active": True, "network": "mainnet"},
        {"id": 8, "user_id": 4242, "active": True, "network": "testnet"},
    ]
    with patch.object(copy_service, "get_user_active_mirrors_v2", return_value=mirrors), \
         patch.object(
             copy_service,
             "_flatten_mirror_positions",
             side_effect=[
                 (0, 0.0, 0.0, ["BTC: venue down"]),
                 (0, 0.0, 0.0, ["ETH: venue down"]),
             ],
         ), \
         patch.object(copy_service, "request_copy_mirror_stop") as request_stop, \
         patch.object(copy_service, "stop_copy_mirror") as stop, \
         patch.object(copy_service, "_finalize_mirror_session") as finalize:
        ok, message = copy_service.stop_all_copies(4242)

    assert not ok
    assert "Stopped 0 copy mirror(s)" in message
    assert "2 mirror(s) remain monitored" in message
    assert request_stop.call_count == 2
    request_stop.assert_any_call(7)
    request_stop.assert_any_call(8)
    stop.assert_not_called()
    finalize.assert_not_called()


def test_stop_all_reports_failure_for_mixed_stopped_and_pending_copies():
    mirrors = [
        {"id": 7, "user_id": 4242, "active": True, "network": "mainnet"},
        {"id": 8, "user_id": 4242, "active": True, "network": "testnet"},
    ]
    with patch.object(copy_service, "get_user_active_mirrors_v2", return_value=mirrors), \
         patch.object(
             copy_service,
             "_flatten_mirror_positions",
             side_effect=[
                 (1, 4.0, 100.0, []),
                 (0, 0.0, 0.0, ["ETH: venue down"]),
             ],
         ), \
         patch.object(copy_service, "request_copy_mirror_stop") as request_stop, \
         patch.object(copy_service, "stop_copy_mirror") as stop, \
         patch.object(copy_service, "_finalize_mirror_session") as finalize:
        ok, message = copy_service.stop_all_copies(4242)

    assert not ok
    assert "Stopped 1 copy mirror(s)" in message
    assert "Flattened 1 copied position(s)" in message
    assert "1 mirror(s) remain monitored" in message
    stop.assert_called_once_with(7)
    finalize.assert_called_once_with(mirrors[0], "user_stop_all")
    # COPY-STOP-RACE: the stop intent is now set for EVERY mirror BEFORE its
    # flatten runs (not only after a failure), so a concurrent poll cycle can
    # never re-open positions mid-stop. The clean stop of mirror 7 clears the
    # flag again via stop_copy_mirror.
    assert request_stop.call_count == 2
    request_stop.assert_any_call(7)
    request_stop.assert_any_call(8)


def test_stop_request_unpauses_a_mirror_so_the_retry_poller_can_see_it():
    with patch.object(database_model, "execute") as execute:
        database_model.request_copy_mirror_stop(7)

    sql, params = execute.call_args.args
    assert "active = true" in sql
    assert "stop_requested = true" in sql
    assert "paused = false" in sql
    assert params == (7,)


def test_pending_stop_cannot_be_paused_or_resumed_by_a_stale_callback():
    mirror = {
        "id": 7,
        "user_id": 4242,
        "active": True,
        "paused": False,
        "stop_requested": True,
    }
    with patch.object(copy_service, "get_copy_mirror", return_value=mirror), \
         patch.object(copy_service, "pause_copy_mirror") as pause, \
         patch.object(copy_service, "resume_copy_mirror") as resume:
        paused, pause_message = copy_service.pause_copy(4242, 7)
        resumed, resume_message = copy_service.resume_copy(4242, 7)

    assert not paused
    assert not resumed
    assert "stopping" in pause_message
    assert "stopping" in resume_message
    pause.assert_not_called()
    resume.assert_not_called()


def test_database_pause_and_poller_queries_keep_stop_retries_visible():
    with patch.object(database_model, "execute") as execute, \
         patch.object(database_model, "query_all", return_value=[]) as query_all:
        database_model.pause_copy_mirror(7)
        database_model.get_all_active_mirrors_v2()

    assert "AND NOT stop_requested" in execute.call_args.args[0]
    assert "NOT m.paused OR m.stop_requested = true" in query_all.call_args.args[0]


def test_pending_stop_cannot_be_restarted_over_open_copy_exposure():
    user = type("User", (), {"linked_signer_address": "0xsigner"})()
    trader = {"id": 9, "wallet_address": "0xleader", "label": "leader", "active": True, "owner_user_id": 4242}
    pending = {"id": 7, "trader_id": 9, "stop_requested": True, "active": True}
    with patch.object(copy_service, "is_trading_paused", return_value=False), \
         patch.object(copy_service, "get_user", return_value=user), \
         patch.object(copy_service, "get_copy_trader", return_value=trader), \
         patch.object(copy_service, "get_user_active_mirrors_v2", return_value=[pending]), \
         patch.object(copy_service, "create_copy_mirror_v2") as create:
        ok, message = copy_service.start_copy(4242, 9, margin_per_trade=100.0)

    assert not ok
    assert "still closing" in message
    create.assert_not_called()


def test_atomic_mirror_create_refuses_pending_stop_after_locking_trader():
    class Cursor:
        def __init__(self):
            self.calls = []
            self._rows = iter(({"id": 9}, {"id": 7}))

        def execute(self, sql, params=None):
            self.calls.append((sql, params))

        def fetchone(self):
            return next(self._rows)

    cursor = Cursor()
    with patch.object(database_model, "run_transaction", side_effect=lambda work: work(cursor)):
        mirror_id = database_model.create_copy_mirror_v2(
            user_id=4242,
            trader_id=9,
            network="mainnet",
            margin_per_trade=100.0,
        )

    assert mirror_id is None
    assert "FOR UPDATE" in cursor.calls[0][0]
    assert "stop_requested = true" in cursor.calls[1][0]
    assert "copy_positions" in cursor.calls[1][0]
    assert "stop_requested = true" in cursor.calls[2][0]
    assert not any("INSERT INTO copy_mirrors" in sql for sql, _ in cursor.calls)


def test_remove_refuses_to_hide_a_trader_when_a_close_fails():
    trader = {"id": 9, "wallet_address": "0xabc", "label": "leader", "owner_user_id": None}
    mirror = {"id": 7, "user_id": 4242, "network": "mainnet", "active": True}
    with patch.object(copy_service, "get_copy_trader", return_value=trader), \
         patch.object(copy_service, "get_mirrors_for_trader", return_value=[mirror]), \
         patch.object(copy_service, "_flatten_mirror_positions", return_value=(0, 0.0, 0.0, ["BTC: venue down"])), \
         patch.object(copy_service, "request_copy_mirror_stop") as request_stop, \
         patch.object(copy_service, "stop_copy_mirror") as stop, \
         patch.object(copy_service, "_finalize_mirror_session") as finalize, \
         patch.object(copy_service, "deactivate_copy_trader") as deactivate:
        ok, message = copy_service.remove_trader(9, requester_user_id=1, is_admin=True)

    assert not ok
    assert "remains active" in message
    request_stop.assert_called_once_with(7)
    stop.assert_not_called()
    finalize.assert_not_called()
    deactivate.assert_not_called()


def test_stop_retry_does_not_depend_on_a_leader_portfolio_read():
    async def _case():
        mirror = {
            "id": 7,
            "user_id": 4242,
            "trader_id": 9,
            "network": "mainnet",
            "wallet_address": "0xleader",
            "stop_requested": True,
        }

        async def _inline(fn, *args, **kwargs):
            if fn.__name__ == "get_all_active_mirrors_v2":
                return [mirror]
            if fn.__name__ == "_load_leader_position_map":
                raise AssertionError("stop retry must not read the leader portfolio")
            raise AssertionError(f"Unexpected blocking function: {fn.__name__}")

        with patch.object(copy_service, "run_blocking", side_effect=_inline), \
             patch.object(copy_service, "_sync_mirror_positions", new_callable=AsyncMock) as sync:
            await copy_service._poll_all_mirrors()

        sync.assert_awaited_once_with(mirror, {})

    asyncio.run(_case())


def test_stop_requested_mirror_retries_flatten_without_mirroring_new_opens():
    async def _case():
        mirror = {
            "id": 7,
            "user_id": 4242,
            "network": "testnet",
            "active": True,
            "stop_requested": True,
        }

        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(copy_service, "run_blocking", side_effect=_inline), \
             patch.object(copy_service, "_flatten_mirror_positions", return_value=(1, 4.0, 100.0, [])), \
             patch.object(copy_service, "stop_copy_mirror") as stop, \
             patch.object(copy_service, "_finalize_mirror_session") as finalize, \
             patch.object(copy_service, "_notify_user", new_callable=AsyncMock) as notify:
            await copy_service._sync_mirror_positions(mirror, {1: {"side": "LONG", "size": 1.0}})

        stop.assert_called_once_with(7)
        finalize.assert_called_once_with(mirror, "user_stop")
        notify.assert_awaited_once()

    asyncio.run(_case())


def test_stop_copy_sets_the_intent_before_the_flatten_starts():
    """COPY-STOP-RACE: the poller must see stop_requested BEFORE the flatten
    begins, or a concurrent poll cycle re-opens the positions being closed."""
    order: list[str] = []
    mirror = {"id": 7, "user_id": 4242, "active": True, "network": "mainnet"}
    with patch.object(copy_service, "get_copy_mirror", return_value=mirror), \
         patch.object(copy_service, "request_copy_mirror_stop",
                      side_effect=lambda _mid: order.append("intent")), \
         patch.object(copy_service, "_flatten_mirror_positions",
                      side_effect=lambda *a, **k: order.append("flatten") or (1, 2.0, 50.0, [])), \
         patch.object(copy_service, "stop_copy_mirror",
                      side_effect=lambda _mid: order.append("stop")), \
         patch.object(copy_service, "_finalize_mirror_session"):
        ok, _ = copy_service.stop_copy(4242, 7)

    assert ok
    assert order == ["intent", "flatten", "stop"]


def test_open_loop_aborts_when_the_mirror_was_stopped_mid_sync():
    """The mirror snapshot driving a sync can be many seconds stale; the
    re-read right before an open must win over the snapshot."""
    async def _case():
        mirror = {
            "id": 7, "user_id": 4242, "network": "mainnet", "active": True,
            "margin_per_trade": 100.0, "max_leverage": 3.0,
            "total_allocated_usd": 500.0,
        }

        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        stopped_live = dict(mirror, stop_requested=True)
        with patch.object(copy_service, "run_blocking", side_effect=_inline), \
             patch.object(copy_service, "is_trading_paused", return_value=False), \
             patch.object(copy_service, "get_open_copy_positions", return_value=[]), \
             patch.object(copy_service, "get_user_nado_client", return_value=None), \
             patch.object(copy_service, "set_mirror_unrealized"), \
             patch.object(copy_service, "get_product_name", return_value="BTC-PERP"), \
             patch.object(copy_service, "get_product_max_leverage", return_value=5.0), \
             patch.object(copy_service, "get_copy_mirror", return_value=stopped_live), \
             patch.object(copy_service, "_execute_maker_open") as maker, \
             patch.object(copy_service, "execute_market_order") as market, \
             patch.object(copy_service, "insert_copy_position") as insert:
            await copy_service._sync_mirror_positions(
                mirror, {1: {"side": "LONG", "entry_price": 100.0, "size": 1.0}},
            )

        maker.assert_not_called()
        market.assert_not_called()
        insert.assert_not_called()

    asyncio.run(_case())


def test_stop_landing_during_the_open_unwinds_the_just_opened_position():
    """COPY-STOP-RACE (post-open): a stop whose flatten ran before the new
    row existed must not leave the fill orphaned behind an inactive mirror."""
    async def _case():
        mirror = {
            "id": 7, "user_id": 4242, "network": "mainnet", "active": True,
            "margin_per_trade": 100.0, "max_leverage": 3.0,
            "total_allocated_usd": 500.0, "wallet_address": "0xleader00ff",
        }

        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        live_then_stopped = [
            dict(mirror, active=True, stop_requested=False, paused=False),  # pre-open
            dict(mirror, active=False, stop_requested=False, paused=False),  # post-open
        ]
        closes = {}

        def _capture_market(**kwargs):
            closes.update(kwargs)
            return {"success": True, "digest": "0xclose", "price": 101.0, "fee": 0.01}

        # Trustworthy venue read (empty, present) so the F-4 suspect-read guard
        # permits the open whose stop-race unwind this test exercises. Mid at
        # the leader entry so the entry-deviation gate lets the open through.
        follower = MagicMock()
        follower.get_all_positions.return_value = []
        follower.get_market_price.return_value = {"mid": 100.0}

        with patch.object(copy_service, "run_blocking", side_effect=_inline), \
             patch.object(copy_service, "is_trading_paused", return_value=False), \
             patch.object(copy_service, "get_open_copy_positions", return_value=[]), \
             patch.object(copy_service, "get_user_nado_client", return_value=follower), \
             patch.object(copy_service, "_archive_reads_unreliable", return_value=False), \
             patch.object(copy_service, "set_mirror_unrealized"), \
             patch.object(copy_service, "get_product_name", return_value="BTC-PERP"), \
             patch.object(copy_service, "get_product_max_leverage", return_value=5.0), \
             patch.object(copy_service, "get_copy_mirror", side_effect=live_then_stopped), \
             patch.object(copy_service, "_execute_maker_open",
                          return_value={"success": True, "digest": "0xopen", "price": 100.0,
                                        "fee": 0.0, "filled_size": 1.0}), \
             patch.object(copy_service, "update_mirror_accounting"), \
             patch.object(copy_service, "insert_copy_position", return_value=88), \
             patch.object(copy_service, "execute_market_order", side_effect=_capture_market), \
             patch.object(copy_service, "_settle_copy_close", return_value=(1.0, 0.01, 101.0)), \
             patch.object(copy_service, "close_copy_position") as close_row, \
             patch.object(copy_service, "_place_tp_sl_orders") as brackets, \
             patch.object(copy_service, "_notify_user", new_callable=AsyncMock):
            await copy_service._sync_mirror_positions(
                mirror, {1: {"side": "LONG", "entry_price": 100.0, "size": 1.0}},
            )

        assert closes.get("reduce_only") is True
        assert closes.get("size") == 1.0
        assert closes.get("is_long") is False  # closes the long
        close_row.assert_called_once_with(88, pnl=1.0, reason="stop_race_unwind")
        brackets.assert_not_called()

    asyncio.run(_case())


def test_failed_unwind_rearms_the_stop_retry_poller():
    async def _case():
        mirror = {"id": 7, "user_id": 4242, "network": "mainnet"}
        cp = {"id": 88, "entry_price": 100.0, "side": "long", "product_name": "BTC-PERP"}

        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(copy_service, "run_blocking", side_effect=_inline), \
             patch.object(copy_service, "execute_market_order",
                          return_value={"success": False, "error": "venue down"}), \
             patch.object(copy_service, "request_copy_mirror_stop") as rearm, \
             patch.object(copy_service, "_notify_user", new_callable=AsyncMock):
            await copy_service._unwind_stop_raced_open(
                mirror, cp, "BTC", 1.0, True, 3.0, "mainnet", None,
            )

        rearm.assert_called_once_with(7)

    asyncio.run(_case())


def test_network_switch_does_not_stop_a_network_scoped_mirror():
    async def _case():
        mirror = {
            "id": 7,
            "user_id": 4242,
            "network": "testnet",
            "active": True,
            "margin_per_trade": 100.0,
            "max_leverage": 3.0,
            "total_allocated_usd": 500.0,
        }

        async def _inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch.object(copy_service, "run_blocking", side_effect=_inline), \
             patch.object(copy_service, "is_trading_paused", return_value=False), \
             patch.object(copy_service, "get_open_copy_positions", return_value=[]), \
             patch.object(copy_service, "get_user_nado_client", return_value=None) as client, \
             patch.object(copy_service, "set_mirror_unrealized"), \
             patch.object(copy_service, "auto_stop_mirror") as auto_stop, \
             patch.object(copy_service, "_flatten_mirror_positions") as flatten:
            await copy_service._sync_mirror_positions(mirror, {})

        client.assert_called_once_with(4242, network="testnet")
        auto_stop.assert_not_called()
        flatten.assert_not_called()

    asyncio.run(_case())


# ==========================================================================
# Global admin pause must cover EVERY copy arming path (audit COPY-PAUSE-*).
#
# Strategies stand down mid-flight (bot_runtime._run_cycle -> maintenance_pause).
# Copy had three uncovered arming paths; these pin all of them, plus the
# deliberate asymmetry that CLOSING stays alive while paused.
# ==========================================================================
def test_start_copy_blocked_by_global_pause():
    """COPY-PAUSE-START"""
    with patch.object(copy_service, "is_trading_paused", return_value=True), \
         patch.object(copy_service, "get_user") as user_spy, \
         patch.object(copy_service, "create_copy_mirror_v2") as create:
        ok, msg = copy_service.start_copy(4242, 9, margin_per_trade=100.0)

    assert not ok
    assert "paused" in msg.lower()
    create.assert_not_called()
    user_spy.assert_not_called()


def test_resume_copy_blocked_by_global_pause():
    """COPY-PAUSE-RESUME — the post-redeploy restart path.

    boot_stand_down_mirrors pauses every mirror and hands the user a
    "▶ Resume" button, so this is the ONLY way copy restarts after a deploy.
    A pause that does not cover it is a kill switch with a bypass button.
    """
    paused_mirror = {"id": 7, "user_id": 4242, "active": True, "paused": True}
    with patch.object(copy_service, "get_copy_mirror", return_value=paused_mirror), \
         patch.object(copy_service, "is_trading_paused", return_value=True), \
         patch.object(copy_service, "resume_copy_mirror") as resume:
        ok, msg = copy_service.resume_copy(4242, 7)

    assert not ok
    assert "paused" in msg.lower()
    resume.assert_not_called()


def test_resume_copy_still_works_when_not_paused():
    """The gate must not brick the normal resume path."""
    paused_mirror = {"id": 7, "user_id": 4242, "active": True, "paused": True}
    with patch.object(copy_service, "get_copy_mirror", return_value=paused_mirror), \
         patch.object(copy_service, "is_trading_paused", return_value=False), \
         patch.object(copy_service, "resume_copy_mirror") as resume:
        ok, msg = copy_service.resume_copy(4242, 7)

    assert ok
    assert "resumed" in msg.lower()
    resume.assert_called_once_with(7)


def test_poller_does_not_open_new_positions_while_globally_paused():
    """COPY-PAUSE-POLLER — needs no user action, so it is the widest gap.

    The pre-open liveness re-read caught stop/pause on the MIRROR but not the
    global switch, so an active mirror kept opening while every strategy was
    suspended.
    """
    mirror = {
        "id": 7, "user_id": 4242, "active": True, "network": "mainnet",
        "trader_id": 9, "margin_per_trade": 100.0, "max_leverage": 5.0,
        "total_allocated_usd": 500.0,
    }
    leader = {1: {"side": "LONG", "entry_price": 100.0, "size": 1.0, "leverage": 2.0}}

    with patch.object(copy_service, "get_copy_mirror", return_value=mirror), \
         patch.object(copy_service, "get_open_copy_positions", return_value=[]), \
         patch.object(copy_service, "_execute_maker_open") as maker, \
         patch.object(copy_service, "execute_market_order") as taker, \
         patch.object(copy_service, "_notify_user", new_callable=AsyncMock):
        asyncio.run(
            copy_service._sync_mirror_positions(mirror, leader, opens_blocked=True)
        )

    maker.assert_not_called()
    taker.assert_not_called()


def test_pause_gate_is_read_off_the_event_loop():
    """asyncio discipline: is_trading_paused is an uncached bot_state read, so it
    must be reached through run_blocking, never inline in a coroutine body. A sync
    call here would starve the loop and make APScheduler skip jobs."""
    seen = {}

    async def _fake_run_blocking(fn, *a, **kw):
        seen["fn"] = getattr(fn, "__name__", str(fn))
        return False

    with patch.object(copy_service, "run_blocking", side_effect=_fake_run_blocking):
        blocked = asyncio.run(copy_service._global_pause_blocks_opens())

    assert blocked is False
    assert seen["fn"] == "is_trading_paused"


def test_pause_gate_fails_closed_when_the_switch_cannot_be_read():
    """An unreadable kill switch must block opens, not wave them through:
    skipping an open is safe (next poll retries); trading through a pause is not."""
    async def _boom(fn, *a, **kw):
        raise RuntimeError("bot_state unreachable")

    with patch.object(copy_service, "run_blocking", side_effect=_boom):
        assert asyncio.run(copy_service._global_pause_blocks_opens()) is True


def test_poller_resolves_the_pause_once_and_passes_it_to_every_mirror():
    """The flag must actually reach _sync_mirror_positions — a default-False
    parameter nobody sets would be a silent bypass."""
    mirrors = [
        {"id": 7, "user_id": 1, "trader_id": 9, "network": "mainnet", "wallet_address": "0xa"},
        {"id": 8, "user_id": 2, "trader_id": 9, "network": "mainnet", "wallet_address": "0xa"},
    ]
    calls = []

    async def _sync(mirror, leader_map, *, opens_blocked=False, leader_trusted=True):
        calls.append((mirror["id"], opens_blocked))

    with patch.object(copy_service, "run_blocking", new_callable=AsyncMock) as rb, \
         patch.object(copy_service, "_global_pause_blocks_opens", return_value=True), \
         patch.object(copy_service, "_sync_mirror_positions", side_effect=_sync):
        rb.side_effect = lambda fn, *a, **kw: (
            mirrors if fn is copy_service.get_all_active_mirrors_v2 else {}
        )
        asyncio.run(copy_service._poll_all_mirrors())

    assert sorted(calls) == [(7, True), (8, True)]
