import asyncio
import json
import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.config import get_product_max_leverage
from src.nadobro.handlers.intent_handlers import _enrich_trade_payload
from src.nadobro.handlers.intent_parser import parse_interaction_intent, parse_position_management_intent
from src.nadobro.handlers import callbacks, home_card, formatters
from src.nadobro.i18n import get_active_language, language_context
from src.nadobro.strategy import bot_runtime
from src.nadobro.trading import execution_queue
from src.nadobro.runtime import runtime_supervisor
from src.nadobro.trading import trade_service
from src.nadobro.core.async_utils import run_blocking
from src.nadobro.strategy.strategy_lifecycle import cleanup_strategy_positions
from src.nadobro.trading.stop_loss_service import _should_trigger_stop_loss
from src.nadobro.trading.trade_service import _place_take_profit_order


class RuntimeAndLeverageTests(unittest.TestCase):
    def test_invite_public_code_column_is_added_before_index(self):
        sql_source = Path("src/nadobro/db.py").read_text()
        alter_idx = sql_source.index("ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS public_code TEXT;")
        index_idx = sql_source.index(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_codes_public_code ON invite_codes (public_code)"
        )

        self.assertLess(alter_idx, index_idx)

    def test_run_blocking_preserves_language_contextvars(self):
        async def _run():
            with language_context("fr"):
                self.assertEqual(get_active_language(), "fr")
                self.assertEqual(await run_blocking(get_active_language), "fr")

        asyncio.run(_run())

    def test_portfolio_view_handles_snapshot_failures(self):
        # Click path no longer awaits snapshot_for_user. A boom on the cache
        # read still degrades to the unavailable card instead of raising.
        from src.nadobro.venue import nado_sync

        with patch.object(
            home_card,
            "get_user",
            return_value=SimpleNamespace(network_mode=SimpleNamespace(value="mainnet")),
        ), patch.object(nado_sync, "mark_user_active"), patch.object(
            nado_sync, "get_cached_snapshot", side_effect=RuntimeError("boom")
        ):
            text, reply_markup = asyncio.run(home_card._view_portfolio_text(telegram_id=7))

        self.assertIn("Can't pull your portfolio right now", text)
        self.assertIsNotNone(reply_markup)

    def test_portfolio_view_cache_miss_does_not_await_venue(self):
        from src.nadobro.venue import nado_sync
        from src.nadobro.handlers import portfolio_deck

        snap = AsyncMock()
        with patch.object(
            home_card,
            "get_user",
            return_value=SimpleNamespace(network_mode=SimpleNamespace(value="mainnet")),
        ), patch.object(nado_sync, "mark_user_active"), patch.object(
            nado_sync, "get_cached_snapshot", return_value=None
        ), patch.object(portfolio_deck, "snapshot_for_user", snap), patch.object(
            home_card, "_warm_portfolio_snapshot"
        ):
            text, reply_markup = asyncio.run(home_card._view_portfolio_text(telegram_id=7))

        self.assertIn("Loading portfolio", text)
        self.assertIsNotNone(reply_markup)
        snap.assert_not_called()

    def test_resolve_home_view_uses_new_portfolio_deck_path(self):
        view_mock = AsyncMock(return_value=("portfolio", "keyboard"))
        with patch.object(home_card, "_view_portfolio_text", view_mock):
            result = asyncio.run(home_card.resolve_home_view("portfolio:view", telegram_id=5))

        self.assertEqual(result, ("portfolio", "keyboard"))
        view_mock.assert_awaited_once_with(5)

    def test_handle_portfolio_history_invalid_page_does_not_raise(self):
        # _handle_portfolio lives in handlers/portfolio_handler.py since the
        # callbacks.py decomposition; patch names where they now resolve.
        from src.nadobro.handlers import portfolio_handler

        query = SimpleNamespace()
        edit_mock = AsyncMock()

        async def _run_blocking_stub(func, *args, **kwargs):
            if func is portfolio_handler.get_trade_history:
                return [
                    {
                        "product": "BTC-PERP",
                        "side": "LONG",
                        "status": "closed",
                        "price": 60000,
                        "close_price": 60500,
                        "pnl": 25,
                        "created_at": "2026-03-27T12:00:00Z",
                    }
                ]
            return func(*args, **kwargs)

        with patch.object(portfolio_handler, "run_blocking", side_effect=_run_blocking_stub), patch.object(
            portfolio_handler, "_edit_loc", edit_mock
        ):
            asyncio.run(callbacks._handle_portfolio(query, "portfolio:history:not-a-number", telegram_id=77))

        edit_mock.assert_awaited()

    def test_stop_workers_cancels_and_awaits_worker_tasks(self):
        async def _worker():
            await asyncio.sleep(60)

        async def _run():
            old_workers = list(execution_queue._workers)
            try:
                task1 = asyncio.create_task(_worker())
                task2 = asyncio.create_task(_worker())
                execution_queue._workers = [task1, task2]
                await execution_queue.stop_workers()
                self.assertEqual(execution_queue._workers, [])
                self.assertTrue(task1.cancelled())
                self.assertTrue(task2.cancelled())
            finally:
                execution_queue._workers = old_workers

        asyncio.run(_run())

    def test_enrich_trade_payload_clamps_leverage_by_product_cap(self):
        cap = get_product_max_leverage("LINK", network="mainnet")
        payload = {
            "direction": "long",
            "order_type": "market",
            "product": "LINK",
            "size": 1.0,
            "leverage": 40,
        }
        settings = {"default_leverage": 3, "slippage": 1}
        enriched = _enrich_trade_payload(telegram_id=1, payload=payload, settings=settings)
        self.assertEqual(enriched["leverage"], min(40, cap))

    def test_enrich_trade_payload_enforces_minimum_leverage(self):
        payload = {
            "direction": "long",
            "order_type": "market",
            "product": "BTC",
            "size": 1.0,
            "leverage": 0,
        }
        settings = {"default_leverage": 0, "slippage": 1}
        enriched = _enrich_trade_payload(telegram_id=1, payload=payload, settings=settings)
        self.assertEqual(enriched["leverage"], 1)

    def test_start_user_bot_rejects_product_leverage_over_cap(self):
        cap = get_product_max_leverage("LINK", network="mainnet")
        ok, msg = bot_runtime.start_user_bot(
            telegram_id=9_999_990_001,
            strategy="grid",
            product="LINK",
            leverage=float(cap) + 1.0,
            slippage_pct=1,
        )
        self.assertFalse(ok)
        self.assertIn(f"Max leverage for LINK is {cap}x", msg)

    @unittest.skipUnless(
        os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL"),
        "PostgreSQL required once preflight runs (DATABASE_URL)",
    )
    def test_start_user_bot_skips_preflight_when_leverage_ok_but_no_wallet(self):
        cap = get_product_max_leverage("BTC", network="mainnet")
        lev = min(40.0, float(cap))
        ok, msg = bot_runtime.start_user_bot(
            telegram_id=9_999_990_002,
            strategy="grid",
            product="BTC",
            leverage=lev,
            slippage_pct=1,
        )
        self.assertFalse(ok)
        self.assertNotIn("Max leverage", msg)

    def test_start_user_bot_blocks_when_preflight_fails(self):
        fake_user = SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
        with patch.object(bot_runtime, "get_user", return_value=fake_user), patch.object(
            bot_runtime, "list_volume_spot_product_names", return_value=["KBTC"]
        ), patch.object(
            bot_runtime, "get_spot_product_id", return_value=42
        ), patch.object(
            bot_runtime, "run_strategy_start_preflight", return_value=(False, "Wallet not linked")
        ):
            ok, msg = bot_runtime.start_user_bot(
                telegram_id=1,
                strategy="vol",
                product="BTC",
                leverage=1,
                slippage_pct=1,
            )
        self.assertFalse(ok)
        self.assertEqual(msg, "Wallet not linked")

    def test_start_user_bot_forces_volume_spot_even_from_stale_perp_payload(self):
        fake_user = SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
        saved_states = []

        with patch.object(bot_runtime, "get_user", return_value=fake_user), patch.object(
            bot_runtime, "list_volume_spot_product_names", return_value=["KBTC"]
        ), patch.object(
            bot_runtime, "get_spot_product_id", return_value=42
        ), patch.object(
            bot_runtime, "run_strategy_start_preflight", return_value=(True, "")
        ), patch.object(
            bot_runtime, "_mark_previous_sessions_superseded"
        ), patch.object(
            bot_runtime, "get_strategy_settings", return_value=({}, {})
        ), patch.object(
            bot_runtime, "_create_session", return_value=123
        ), patch.object(
            bot_runtime, "_save_state", side_effect=lambda _uid, _network, state: saved_states.append(dict(state))
        ), patch.object(
            bot_runtime, "_ensure_task"
        ):
            ok, msg = bot_runtime.start_user_bot(
                telegram_id=1,
                strategy="vol",
                product="BTC",
                leverage=3,
                slippage_pct=1,
                vol_market="perp",
            )

        self.assertTrue(ok, msg)
        self.assertTrue(saved_states)
        state = saved_states[-1]
        self.assertEqual(state.get("vol_market"), "spot")
        self.assertEqual(state.get("product"), "KBTC")
        self.assertEqual(state.get("direction"), "long")
        self.assertEqual(state.get("leverage"), 1.0)

    def test_stop_user_bot_stops_engine_runtime_for_engine_strategy(self):
        fake_user = SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
        state = {"running": True, "strategy": "grid"}
        runtime = SimpleNamespace(stop=AsyncMock())

        with patch.object(bot_runtime, "_runtime_loop", None), patch.object(
            bot_runtime, "get_user", return_value=fake_user
        ), patch.object(
            bot_runtime, "_load_state", return_value=state
        ), patch.object(
            bot_runtime, "_save_state"
        ), patch.object(
            bot_runtime, "_finalize_session"
        ), patch.object(
            bot_runtime, "cleanup_strategy_positions", return_value={"success": True}
        ), patch("src.nadobro.strategy.engine_runtime.RUNTIME", runtime):
            ok, msg = bot_runtime.stop_user_bot(123, cancel_orders=True)

        self.assertTrue(ok, msg)
        runtime.stop.assert_awaited_once_with(123, "mainnet", "grid")

    def test_stop_all_user_bots_stops_each_engine_runtime(self):
        row_state = {"running": True, "strategy": "mid"}
        runtime = SimpleNamespace(stop=AsyncMock())

        with patch.object(bot_runtime, "_runtime_loop", None), patch.object(
            bot_runtime, "query_all",
            return_value=[{"key": "strategy_bot:456:testnet", "value": json.dumps(row_state)}],
        ), patch.object(
            bot_runtime, "set_bot_state"
        ), patch.object(
            bot_runtime, "_finalize_session"
        ), patch.object(
            bot_runtime, "cleanup_strategy_positions", return_value={"success": True}
        ), patch("src.nadobro.strategy.engine_runtime.RUNTIME", runtime):
            ok, msg = bot_runtime.stop_all_user_bots(456, cancel_orders=True)

        self.assertTrue(ok, msg)
        runtime.stop.assert_awaited_once_with(456, "testnet", "mid")

    def test_run_cycle_retires_legacy_volume_perp_before_spot_dispatch(self):
        telegram_id = 42
        network = "mainnet"
        state = {
            "running": True,
            "strategy": "vol",
            "product": "BTC",
            "vol_market": "perp",
            "vol_phase": "filled_wait_close",
            "interval_seconds": 1,
            "last_run_ts": 0.0,
            "strategy_session_id": 5,
        }
        fake_user = SimpleNamespace(network_mode=SimpleNamespace(value=network))
        saved_states = []

        async def _run_blocking_stub(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch(
            "src.nadobro.users.settings_service.get_strategy_settings",
            return_value=({}, {}),
        ), patch.object(
            bot_runtime, "is_trading_paused", return_value=False
        ), patch.object(
            bot_runtime, "get_user", return_value=fake_user
        ), patch.object(
            bot_runtime, "run_blocking", side_effect=_run_blocking_stub
        ), patch.object(
            bot_runtime, "_save_state", side_effect=lambda _uid, _network, new_state: saved_states.append(dict(new_state))
        ), patch.object(
            bot_runtime, "_finalize_session"
        ) as finalize_mock, patch.object(
            bot_runtime, "close_all_positions", return_value={"success": True}
        ) as close_mock, patch.object(
            bot_runtime, "_dispatch_strategy"
        ) as dispatch_mock, patch.object(
            bot_runtime, "_notify", new=AsyncMock()
        ):
            result = asyncio.run(bot_runtime._run_cycle(telegram_id, network, state))

        self.assertEqual(result, (True, None))
        self.assertFalse(state.get("running"))
        self.assertTrue(any(s.get("running") is False for s in saved_states))
        finalize_mock.assert_called_once()
        # The retirement close is attributed to the session so its close volume counts.
        close_mock.assert_called_once_with(telegram_id, network, strategy_session_id=5)
        dispatch_mock.assert_not_called()

    def test_run_cycle_retires_legacy_bro_and_cleans_exchange_state(self):
        telegram_id = 42
        network = "mainnet"
        state = {
            "running": True,
            "strategy": "bro",
            "product": "MULTI",
            "interval_seconds": 1,
            "last_run_ts": 0.0,
            "strategy_session_id": 5,
        }
        fake_user = SimpleNamespace(network_mode=SimpleNamespace(value=network))
        saved_states = []

        async def _run_blocking_stub(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch(
            "src.nadobro.users.settings_service.get_strategy_settings",
            return_value=({}, {}),
        ), patch.object(
            bot_runtime, "is_trading_paused", return_value=False
        ), patch.object(
            bot_runtime, "get_user", return_value=fake_user
        ), patch.object(
            bot_runtime, "run_blocking", side_effect=_run_blocking_stub
        ), patch.object(
            bot_runtime, "_save_state", side_effect=lambda _uid, _network, new_state: saved_states.append(dict(new_state))
        ), patch.object(
            bot_runtime, "update_strategy_session"
        ) as update_session_mock, patch.object(
            bot_runtime, "cleanup_strategy_positions", return_value={"success": True, "cancelled": 1.0}
        ) as cleanup_mock, patch.object(
            bot_runtime, "_dispatch_strategy"
        ) as dispatch_mock, patch.object(
            bot_runtime, "_notify", new=AsyncMock()
        ):
            result = asyncio.run(bot_runtime._run_cycle(telegram_id, network, state))

        self.assertEqual(result, (True, None))
        self.assertFalse(state.get("running"))
        self.assertEqual(state.get("last_action"), "migrated_off_bro")
        self.assertTrue(any(s.get("running") is False for s in saved_states))
        cleanup_mock.assert_called_once_with(telegram_id, network, state)
        update_session_mock.assert_called_once()
        dispatch_mock.assert_not_called()

    def test_restore_running_bots_retires_bro_even_when_legacy_flag_enabled(self):
        telegram_id = 42
        network = "mainnet"
        state = {
            "running": True,
            "strategy": "bro",
            "product": "MULTI",
            "strategy_session_id": 7,
        }
        rows = [
            {
                "key": f"{bot_runtime.STATE_PREFIX}{telegram_id}:{network}",
                "value": json.dumps(state),
            }
        ]

        with patch.object(bot_runtime, "query_all", return_value=rows), patch.object(
            bot_runtime, "legacy_bro_autoloop_enabled", return_value=True
        ), patch.object(
            bot_runtime, "_save_state"
        ), patch.object(
            bot_runtime, "update_strategy_session"
        ), patch.object(
            bot_runtime, "cleanup_strategy_positions", return_value={"success": True}
        ) as cleanup_mock, patch.object(
            bot_runtime, "_ensure_task"
        ) as ensure_task_mock:
            bot_runtime.restore_running_bots(enabled=True)

        cleanup_mock.assert_called_once()
        ensure_task_mock.assert_not_called()

    def test_stop_all_strategies_for_user_cleans_up_on_network_switch(self):
        telegram_id = 42
        network = "mainnet"
        rows = [
            {
                "key": f"{bot_runtime.STATE_PREFIX}{telegram_id}:{network}",
                "value": json.dumps({"running": True, "strategy": "vol", "product": "KBTC"}),
            }
        ]

        with patch.object(bot_runtime, "query_all", return_value=rows), patch.object(
            bot_runtime, "set_bot_state"
        ) as set_state_mock, patch.object(
            bot_runtime, "cleanup_strategy_positions", return_value={"success": True}
        ) as cleanup_mock, patch.object(
            bot_runtime, "_finalize_session"
        ):
            bot_runtime.stop_all_strategies_for_user(telegram_id)

        cleanup_mock.assert_called_once()
        cleanup_state = cleanup_mock.call_args.args[2]
        self.assertFalse(cleanup_state.get("running"))
        self.assertEqual(cleanup_state.get("last_error"), "Stopped due to network switch")
        set_state_mock.assert_called_once()

    def test_stop_all_user_bots_closes_each_running_network(self):
        telegram_id = 42
        rows = [
            {
                "key": f"{bot_runtime.STATE_PREFIX}{telegram_id}:mainnet",
                "value": json.dumps({"running": True}),
            },
            {
                "key": f"{bot_runtime.STATE_PREFIX}{telegram_id}:testnet",
                "value": json.dumps({"running": True}),
            },
        ]

        class DummyTask:
            def cancel(self):
                return None

        close_calls = []

        def _close_stub(user_id, network, state):
            _ = state
            close_calls.append((user_id, network))
            return {"success": True}

        main_tk = f"{telegram_id}:mainnet"
        test_tk = f"{telegram_id}:testnet"
        old_tasks = dict(bot_runtime._tasks)
        try:
            bot_runtime._tasks = {
                main_tk: DummyTask(),
                test_tk: DummyTask(),
            }
            with patch.object(bot_runtime, "query_all", return_value=rows), patch.object(
                bot_runtime, "set_bot_state"
            ), patch.object(bot_runtime, "cleanup_strategy_positions", side_effect=_close_stub):
                ok, msg = bot_runtime.stop_all_user_bots(telegram_id, cancel_orders=True)
        finally:
            bot_runtime._tasks = old_tasks

        self.assertTrue(ok)
        self.assertIn("Stopped 2 running strategy loop(s).", msg)
        self.assertCountEqual(
            close_calls,
            [(telegram_id, "mainnet"), (telegram_id, "testnet")],
        )

    def test_stop_all_user_bots_migrates_state_before_cleanup(self):
        telegram_id = 42
        rows = [
            {
                "key": f"{bot_runtime.STATE_PREFIX}{telegram_id}:mainnet",
                "value": json.dumps({"running": True, "strategy": "delta_neutral"}),
            },
        ]
        seen_state = {}

        def _cleanup_stub(_user_id, _network, state):
            seen_state.update(state)
            return {"success": True}

        with patch.object(bot_runtime, "query_all", return_value=rows), patch.object(
            bot_runtime, "set_bot_state"
        ), patch.object(bot_runtime, "cleanup_strategy_positions", side_effect=_cleanup_stub):
            ok, msg = bot_runtime.stop_all_user_bots(telegram_id, cancel_orders=True)

        self.assertTrue(ok)
        self.assertIn("Stopped 1 running strategy loop(s).", msg)
        self.assertEqual(seen_state.get("strategy"), "dn")

    def test_stop_all_user_bots_reports_row_errors(self):
        telegram_id = 42
        rows = [
            {
                "key": f"{bot_runtime.STATE_PREFIX}{telegram_id}:mainnet",
                "value": "{not json",
            },
        ]

        with patch.object(bot_runtime, "query_all", return_value=rows):
            ok, msg = bot_runtime.stop_all_user_bots(telegram_id, cancel_orders=True)

        self.assertFalse(ok)
        self.assertIn("No strategy bot was fully stopped", msg)
        self.assertIn("mainnet", msg)

    def test_stop_all_automation_for_user_merges_messages(self):
        telegram_id = 99
        with patch.object(bot_runtime, "stop_all_user_bots", return_value=(True, "Stopped 1 running strategy loop(s).")), patch(
            "src.nadobro.trading.copy_service.stop_all_copies", return_value=(True, "Stopped 1 copy mirror(s).")
        ):
            ok, msg = bot_runtime.stop_all_automation_for_user(telegram_id)
        self.assertTrue(ok)
        self.assertIn("strategy loop", msg)
        self.assertIn("copy mirror", msg)

    def test_stop_all_automation_for_user_ok_when_only_copy_stops(self):
        with patch.object(bot_runtime, "stop_all_user_bots", return_value=(False, "No running strategy bot found.")), patch(
            "src.nadobro.trading.copy_service.stop_all_copies", return_value=(True, "Stopped 1 copy mirror(s).")
        ):
            ok, msg = bot_runtime.stop_all_automation_for_user(42)
        self.assertTrue(ok)
        self.assertIn("No running strategy", msg)
        self.assertIn("copy mirror", msg)

    def test_stop_all_pending_copy_failure_reaches_result_formatter(self):
        pending = (
            "Stopped 1 copy mirror(s). "
            "1 mirror(s) remain monitored while failed closes retry."
        )
        with patch.object(
            bot_runtime,
            "stop_all_user_bots",
            return_value=(True, "Stopped 1 running strategy loop(s)."),
        ), patch(
            "src.nadobro.trading.copy_service.stop_all_copies",
            return_value=(False, pending),
        ):
            ok, msg = bot_runtime.stop_all_automation_for_user(42)

        rendered = formatters.fmt_stop_all_result(ok, msg, "Check Positions if exposure remains.")
        self.assertFalse(ok)
        self.assertIn("strategy loop", msg)
        self.assertIn("failed closes retry", msg)
        self.assertIn("⚠️", rendered)
        self.assertIn("failed closes retry", rendered)

    def test_stop_all_ignores_ordinary_no_active_copy_message(self):
        with patch.object(
            bot_runtime,
            "stop_all_user_bots",
            return_value=(True, "Stopped 1 running strategy loop(s)."),
        ), patch(
            "src.nadobro.trading.copy_service.stop_all_copies",
            return_value=(False, "No active copy mirrors."),
        ):
            ok, msg = bot_runtime.stop_all_automation_for_user(42)

        self.assertTrue(ok)
        self.assertNotIn("No active copy mirrors", msg)

    def test_stop_all_user_bots_defaults_cancel_orders_true(self):
        telegram_id = 42
        rows = [
            {"key": f"{bot_runtime.STATE_PREFIX}{telegram_id}:mainnet", "value": json.dumps({"running": True})},
        ]
        with patch.object(bot_runtime, "query_all", return_value=rows), patch.object(bot_runtime, "set_bot_state"), patch.object(
            bot_runtime, "_finalize_session"
        ), patch.object(bot_runtime, "cleanup_strategy_positions", return_value={"success": True}) as cleanup:
            bot_runtime.stop_all_user_bots(telegram_id)
        cleanup.assert_called_once()

    def test_cleanup_strategy_positions_normalizes_strategy_aliases(self):
        calls = []

        def _dn_stub(*args, **kwargs):
            calls.append(("dn", args, kwargs))
            return {"success": True}

        with patch("src.nadobro.strategy.strategy_lifecycle.close_delta_neutral_legs", side_effect=_dn_stub), patch(
            "src.nadobro.strategy.strategy_lifecycle.close_all_positions"
        ) as close_all:
            res = cleanup_strategy_positions(42, "mainnet", {"strategy": "delta_neutral", "product": "BTC"})

        self.assertTrue(res["success"])
        self.assertEqual(calls[0][0], "dn")
        close_all.assert_not_called()

    def test_cleanup_strategy_positions_treats_legacy_vol_as_spot(self):
        state = {
            "strategy": "vol",
            "product": "KBTC",
            "vol_market": "perp",
            "vol_phase": "filled_wait_close",
            "vol_entry_fill_ts": 123.0,
            "vol_entry_size": 0.001,
            "vol_close_size": 0.0005,
            "strategy_session_id": 77,
        }
        with patch("src.nadobro.strategy.strategy_lifecycle.get_spot_product_id", return_value=42), patch(
            "src.nadobro.strategy.strategy_lifecycle.get_spot_metadata", return_value={"symbol": "KBTC"}
        ), patch(
            "src.nadobro.strategy.strategy_lifecycle.stop_volume_spot_cleanup", return_value={"success": True}
        ) as spot_cleanup, patch(
            "src.nadobro.strategy.strategy_lifecycle.close_all_positions"
        ) as close_all:
            res = cleanup_strategy_positions(42, "mainnet", state)

        self.assertTrue(res["success"])
        spot_cleanup.assert_called_once()
        self.assertEqual(spot_cleanup.call_args.kwargs["max_base_size"], 0.001)
        close_all.assert_not_called()

    def test_cleanup_strategy_positions_does_not_sell_unconfirmed_pending_entry(self):
        state = {
            "strategy": "vol",
            "product": "KBTC",
            "vol_market": "spot",
            "vol_phase": "pending_fill",
            "vol_entry_fill_ts": 0.0,
            "vol_entry_size": 0.001,
        }
        with patch("src.nadobro.strategy.strategy_lifecycle.get_spot_product_id", return_value=42), patch(
            "src.nadobro.strategy.strategy_lifecycle.get_spot_metadata", return_value={"symbol": "KBTC"}
        ), patch(
            "src.nadobro.strategy.strategy_lifecycle.stop_volume_spot_cleanup", return_value={"success": True}
        ) as spot_cleanup:
            res = cleanup_strategy_positions(42, "mainnet", state)

        self.assertTrue(res["success"])
        self.assertEqual(spot_cleanup.call_args.kwargs["max_base_size"], 0.0)

    def _run_grid_cycle_with_snapshot(self, *, strategy, sl_pct, snapshot):
        """Drive _run_cycle for a grid-family strategy with a mocked live
        session snapshot, returning the close_all_positions mock + result."""
        telegram_id = 7
        network = "mainnet"
        state = {
            "running": True,
            "strategy": strategy,
            "product": "BTC",
            "reference_price": 100.0,
            "sl_pct": sl_pct,
            "tp_pct": 0.0,
            "notional_usd": 100.0,
            "interval_seconds": 1,
            "last_run_ts": 0.0,
        }

        class FakeClient:
            def get_market_price(self, _product_id):
                return {"mid": 90.0}

            def get_open_orders(self, _product_id):
                return []

        fake_user = SimpleNamespace(network_mode=SimpleNamespace(value=network))
        sess = {"id": 1, "product_id": 1, "status": "running", "started_at": None, "stopped_at": None}

        async def _run_blocking_stub(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.object(bot_runtime, "is_trading_paused", return_value=False), patch.object(
            bot_runtime, "run_blocking", side_effect=_run_blocking_stub
        ), patch.object(bot_runtime, "get_user", return_value=fake_user), patch.object(
            bot_runtime, "get_user_readonly_client", return_value=FakeClient()
        ), patch.object(
            bot_runtime, "get_user_nado_client", return_value=FakeClient()
        ), patch(
            "src.nadobro.strategy.engine_runtime.engine_v2_enabled",
            return_value=False,
        ), patch(
            "src.nadobro.strategy.engine_runtime.ENGINE_MAPPED_STRATEGIES",
            set(),
        ), patch.object(
            bot_runtime, "_dispatch_strategy", return_value={"success": True, "orders_placed": 0}
        ), patch(
            # _run_cycle merges saved strategy settings into state each cycle;
            # pin to no overrides so the test's sl_pct/tp_pct are authoritative
            # regardless of whether a DB (with default sl_pct=0.5) is present.
            "src.nadobro.users.settings_service.get_strategy_settings",
            return_value=("mainnet", {}),
        ), patch(
            # The session rail now resolves the run via session_resolver
            # (binds to THIS run's session id), not the bare
            # get_active_strategy_session. Mock what the rail actually calls so
            # the snapshot SL/TP path is exercised.
            "src.nadobro.trading.session_resolver.resolve_current_strategy_session",
            return_value=sess,
        ), patch(
            "src.nadobro.trading.live_session.get_live_session_snapshot", return_value=snapshot
        ), patch.object(
            bot_runtime, "_finalize_session"
        ), patch.object(
            bot_runtime, "_save_state"
        ), patch.object(
            bot_runtime, "close_all_positions", return_value={"success": True}
        ) as close_mock, patch.object(
            bot_runtime, "_notify"
        ):
            result = asyncio.run(bot_runtime._run_cycle(telegram_id, network, state))
        return result, close_mock

    def test_run_cycle_grid_within_pnl_band_does_not_stop(self):
        # A 10% price move (mid 90 vs ref 100) but session PnL only -0.5% of
        # margin must NOT stop — the rail measures PnL, not price distance.
        result, close_mock = self._run_grid_cycle_with_snapshot(
            strategy="grid", sl_pct=5.0,
            snapshot={"session_pnl": -0.5, "session_pnl_pct": -0.5, "margin": 100.0},
        )
        self.assertEqual(result, (True, None))
        close_mock.assert_not_called()

    def test_run_cycle_grid_stops_on_live_session_pnl(self):
        # Session PnL (realized + unrealized) crosses the SL (% of margin) →
        # the rail finalizes and closes. Replaces the dead grid_stop_loss_hit
        # action path that run_engine_cycle never emitted.
        result, close_mock = self._run_grid_cycle_with_snapshot(
            strategy="rgrid", sl_pct=0.1,
            snapshot={"session_pnl": -1.0, "session_pnl_pct": -1.0, "margin": 100.0},
        )
        self.assertEqual(result, (True, None))
        self.assertTrue(close_mock.called)

    def test_mm_start_guard_uses_cycle_notional_without_leverage_multiplier(self):
        class FakeClient:
            def get_all_positions(self):
                return []

            def get_open_orders(self, _product_id):
                return []

            def get_balance(self):
                return {"balances": {0: 205.0}}

        state = {
            "notional_usd": 100.0,
            "cycle_notional_usd": 100.0,
            "inventory_soft_limit_usd": 60.0,
        }

        with patch.object(bot_runtime, "get_user_readonly_client", return_value=FakeClient()), patch.object(
            bot_runtime, "get_product_id", return_value=2
        ):
            ok, msg = bot_runtime._run_mm_start_guard(1, "mainnet", "BTC", 3.0, state)

        self.assertTrue(ok, msg)

    def test_ensure_task_uses_cached_loop_when_called_off_loop(self):
        # Legacy per-user loop path: only taken when the central strategy
        # scheduler feature flag is OFF (it defaults ON), so pin it off here.
        from src.nadobro.core import feature_flags

        calls = []

        class FakeLoop:
            def is_running(self):
                return True

            def call_soon_threadsafe(self, fn, *args):
                calls.append((fn, args))
                fn(*args)

        old_loop = bot_runtime._runtime_loop
        old_tasks = dict(bot_runtime._tasks)
        try:
            bot_runtime._runtime_loop = FakeLoop()
            bot_runtime._tasks = {}
            with patch.object(
                feature_flags, "strategy_scheduler_enabled", return_value=False
            ), patch.object(bot_runtime, "_schedule_task_on_loop") as schedule_mock:
                bot_runtime._ensure_task(telegram_id=123, network="testnet")
                schedule_mock.assert_called_once_with(123, "testnet")
            self.assertEqual(len(calls), 1)
        finally:
            bot_runtime._runtime_loop = old_loop
            bot_runtime._tasks = old_tasks

    def test_ensure_task_registers_with_central_scheduler_when_enabled(self):
        # Default path: NADO_STRATEGY_SCHEDULER is on, so _ensure_task hands
        # the session to the central scheduler instead of spawning a loop.
        from unittest.mock import MagicMock

        from src.nadobro.core import feature_flags

        from src.nadobro.strategy import strategy_scheduler

        sched = SimpleNamespace(register=MagicMock())
        with patch.object(
            feature_flags, "strategy_scheduler_enabled", return_value=True
        ), patch.object(strategy_scheduler, "get_scheduler", return_value=sched):
            bot_runtime._ensure_task(telegram_id=9, network="mainnet")

        sched.register.assert_called_once_with(9, "mainnet")

    def test_parse_interaction_intent_routes_current_pnl_question(self):
        intent = parse_interaction_intent("What is my current pnl?")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("action"), "open_view")
        self.assertEqual(intent.get("target"), "portfolio:view")

    def test_parse_interaction_intent_does_not_hijack_generic_pnl_question(self):
        intent = parse_interaction_intent("What is pnl?")
        self.assertIsNone(intent)

    def test_parse_interaction_intent_routes_market_radar_to_points(self):
        intent = parse_interaction_intent("market radar")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("action"), "open_view")
        self.assertEqual(intent.get("target"), "points:view")

    def test_parse_position_management_close_all(self):
        intent = parse_position_management_intent("close all positions")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("action"), "close_all")
        intent2 = parse_position_management_intent("market close everything")
        self.assertIsNotNone(intent2)
        self.assertEqual(intent2.get("action"), "close_all")

    def test_parse_position_management_market_close(self):
        intent = parse_position_management_intent("close my BTC position")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("action"), "close_market")
        self.assertEqual(intent.get("product"), "BTC")
        self.assertIsNone(intent.get("size"))

    def test_parse_position_management_limit_close(self):
        intent = parse_position_management_intent("close BTC at 69500")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("action"), "limit_close")
        self.assertEqual(intent.get("product"), "BTC")
        self.assertEqual(intent.get("limit_price"), 69500.0)

    def test_parse_position_management_tp_sl(self):
        intent = parse_position_management_intent("set TP order for my BTC open position at 69500")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("action"), "set_tp_sl")
        self.assertEqual(intent.get("product"), "BTC")
        self.assertEqual(intent.get("tp_price"), 69500.0)

    def test_parse_position_management_defers_to_full_trade_intent(self):
        intent = parse_position_management_intent("long 5 BTC with 20x leverage")
        self.assertIsNone(intent)

    def test_place_take_profit_order_places_opposite_side_limit(self):
        calls = []

        class FakeClient:
            def place_limit_order(self, product_id, size, price, is_buy=True):
                calls.append((product_id, size, price, is_buy))
                return {"success": True, "digest": "tp-digest"}

        result = _place_take_profit_order(
            client=FakeClient(),
            product_id=1,
            size=2.5,
            is_long=True,
            tp_price=70000.0,
        )
        self.assertTrue(result.get("tp_set"))
        self.assertEqual(calls, [(1, 2.5, 70000.0, False)])

    def test_should_trigger_stop_loss_for_long_and_short(self):
        self.assertTrue(_should_trigger_stop_loss("LONG", 68000.0, 68500.0))
        self.assertFalse(_should_trigger_stop_loss("LONG", 69000.0, 68500.0))
        self.assertTrue(_should_trigger_stop_loss("SHORT", 69000.0, 68500.0))
        self.assertFalse(_should_trigger_stop_loss("SHORT", 68000.0, 68500.0))

    def test_runtime_supervisor_group_mapping(self):
        self.assertEqual(runtime_supervisor.strategy_worker_group("grid"), "mm_grid")
        self.assertEqual(runtime_supervisor.strategy_worker_group("rgrid"), "mm_grid")
        self.assertEqual(runtime_supervisor.strategy_worker_group("dn"), "dn")
        self.assertEqual(runtime_supervisor.strategy_worker_group("vol"), "vol")
        self.assertEqual(runtime_supervisor.strategy_worker_group("bro"), "bro")

    def test_cycle_result_label_marks_skip(self):
        self.assertEqual(bot_runtime._cycle_result_label(True, "skipped_interval"), "skipped")
        self.assertEqual(bot_runtime._cycle_result_label(True, "maintenance_pause"), "skipped")
        self.assertEqual(bot_runtime._cycle_result_label(True, None), "ok")
        self.assertEqual(bot_runtime._cycle_result_label(False, "boom"), "error")

    def test_submit_with_timeout_returns_timeout_error(self):
        def _slow():
            import time as _t
            _t.sleep(0.2)
            return {"success": True}

        ok, result = trade_service._submit_with_timeout(_slow, timeout_s=0.01)
        self.assertFalse(ok)
        self.assertIn("timed out", str(result).lower())







    def test_start_user_bot_rejects_dn_pair_when_entry_blocked(self):
        fake_user = SimpleNamespace(network_mode=SimpleNamespace(value="testnet"))
        with patch.object(bot_runtime, "get_user", return_value=fake_user), patch.object(
            bot_runtime,
            "get_dn_pair",
            return_value={
                "product": "WBSPYX",
                "perp_product_id": 117,
                "spot_product_id": 118,
                "entry_allowed": False,
                "entry_block_reason": "Spot market is currently closed.",
            },
        ):
            ok, msg = bot_runtime.start_user_bot(
                telegram_id=1,
                strategy="dn",
                product="WBSPYX",
                leverage=2,
                slippage_pct=1,
            )
        self.assertFalse(ok)
        self.assertIn("currently closed", msg)

    def test_dn_strategy_defaults_include_worker_group_and_funding_mode(self):
        from src.nadobro.runtime.runtime_supervisor import strategy_worker_group

        dn = bot_runtime._strategy_defaults("dn")
        self.assertEqual(dn.get("funding_entry_mode"), "wait")
        self.assertEqual(strategy_worker_group("dn"), "dn")

    def test_vol_strategy_defaults_enable_signal_mode(self):
        vol = bot_runtime._strategy_defaults("vol")
        self.assertEqual(vol.get("vol_direction_mode"), "signal")
        self.assertGreater(float(vol.get("vol_trade_tp_pct")), float(vol.get("vol_trade_sl_pct")))
        self.assertIn("vol_max_spread_bp", vol)





    def test_dgrid_strategy_defaults_and_worker_group(self):
        from src.nadobro.runtime.runtime_supervisor import strategy_worker_group

        dgrid = bot_runtime._strategy_defaults("dgrid")
        self.assertEqual(strategy_worker_group("dgrid"), "mm_grid")
        self.assertEqual(dgrid.get("interval_seconds"), 30)
        self.assertIn("dgrid_trend_on_variance_ratio", dgrid)

    def test_handle_strategy_job_vol_overlap_drops_extra_pending_tick(self):
        telegram_id = 77
        network = "mainnet"
        key = f"{telegram_id}:{network}"
        old_locks = dict(bot_runtime._job_locks)
        old_pending = dict(bot_runtime._job_pending_payloads)
        old_coalesced = dict(bot_runtime._job_coalesce_counts)
        old_stats = dict(bot_runtime._job_stats)
        lock = asyncio.Lock()

        async def _run():
            await lock.acquire()
            bot_runtime._job_locks[key] = lock
            bot_runtime._job_pending_payloads[key] = {"telegram_id": telegram_id, "network": network, "strategy": "vol"}
            await bot_runtime.handle_strategy_job({"telegram_id": telegram_id, "network": network, "strategy": "vol"})

        try:
            asyncio.run(_run())
            self.assertEqual(bot_runtime._job_pending_payloads[key]["strategy"], "vol")
            self.assertEqual(int(bot_runtime._job_coalesce_counts.get(key, 0)), 0)
            self.assertGreaterEqual(int(bot_runtime._job_stats.get("vol_overlap_skips", 0)), 1)
        finally:
            bot_runtime._job_locks = old_locks
            bot_runtime._job_pending_payloads = old_pending
            bot_runtime._job_coalesce_counts = old_coalesced
            bot_runtime._job_stats = old_stats

    def test_enqueue_strategy_tracks_vol_stats(self):
        old_stats = dict(execution_queue._stats)
        old_workers = list(execution_queue._workers)
        old_seen = dict(execution_queue._dedupe_seen)

        class _Worker:
            def done(self):
                return False

            def get_name(self):
                return "strategy-0"

        async def _run():
            execution_queue._stats.update({k: 0 for k in execution_queue._stats.keys()})
            execution_queue._workers = [_Worker()]
            execution_queue._dedupe_seen.clear()
            enqueued_first = await execution_queue.enqueue_strategy(
                {"telegram_id": 1, "network": "mainnet", "strategy": "vol"},
                dedupe_key="dup-key",
            )
            enqueued_second = await execution_queue.enqueue_strategy(
                {"telegram_id": 1, "network": "mainnet", "strategy": "vol"},
                dedupe_key="dup-key",
            )
            return enqueued_first, enqueued_second

        try:
            first, second = asyncio.run(_run())
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(int(execution_queue._stats.get("vol_strategy_enqueued") or 0), 1)
            self.assertEqual(int(execution_queue._stats.get("vol_strategy_deduped") or 0), 1)
        finally:
            execution_queue._stats = old_stats
            execution_queue._workers = old_workers
            execution_queue._dedupe_seen = old_seen

    def test_fmt_status_overview_surfaces_cycle_and_order_reason(self):
        status = {
            "running": True,
            "strategy": "vol",
            "product": "WGOOGLX",
            "vol_market": "spot",
            "notional_usd": 100,
            "runs": 3,
            "interval_seconds": 10,
            "started_at": "2026-01-01T00:00:00",
            "next_cycle_in": 0,
            "last_cycle_result": "error",
            "last_error": "VOL[opened_market_wait_close/execution]: order rejected",
            "last_error_category": "execution",
            "error_streak": 4,
            "runtime_diagnostics": {"queue": {"strategy_qsize": 0, "strategy_qmax": 500}, "pending_coalesced_ticks": 0},
            "order_observability": {"orders_placed": 0, "orders_filled": 0, "orders_cancelled": 0, "cycles": 3, "zero_order_cycles": 3, "last_reason": "order rejected"},
            "vol_order_attempts": 2,
            "vol_order_failures": 2,
            "last_order_error": "builder unavailable",
        }
        onboarding = {"onboarding_complete": True, "network": "mainnet", "has_key": True, "funded": True}
        text = formatters.fmt_status_overview(status, onboarding)
        self.assertIn("WGOOGLX SPOT", text)
        self.assertNotIn("WGOOGLX\\-PERP", text)
        self.assertIn("Last cycle", text)
        self.assertIn("Error class", text)
        self.assertIn("Last order error", text)


class MMDurationTests(unittest.TestCase):
    def test_hard_cap_modes(self):
        from src.nadobro.strategy.bot_runtime import _mm_duration_is_hard_cap
        # Mid / D-Grid: duration is a HARD cap. Grid / R-Grid: soft target.
        self.assertTrue(_mm_duration_is_hard_cap("mid"))
        self.assertTrue(_mm_duration_is_hard_cap("dgrid"))
        self.assertFalse(_mm_duration_is_hard_cap("grid"))
        self.assertFalse(_mm_duration_is_hard_cap("rgrid"))

    def test_resolve_run_duration(self):
        from src.nadobro.strategy.bot_runtime import _resolve_mm_run_duration_minutes
        # Opt-out: no preset, no custom → 0 (no cap, unchanged behavior).
        self.assertEqual(_resolve_mm_run_duration_minutes({}, 100.0, 1_000_000.0), 0.0)
        # Custom duration with no volume → returned as typed.
        self.assertEqual(
            _resolve_mm_run_duration_minutes({"mm_duration_minutes": 45}, 100.0, 0.0), 45.0
        )
        # Preset-derived: $1000 deployed, normal (0.05), $1.44M/24h vol → 20 min.
        d = _resolve_mm_run_duration_minutes({"participation_preset": "normal"}, 1000.0, 1_440_000.0)
        self.assertAlmostEqual(d, 20.0, places=6)
        # Custom over the bound is clamped to [Aggressive … 10×Passive] = [10, 1000].
        c = _resolve_mm_run_duration_minutes({"mm_duration_minutes": 999999}, 1000.0, 1_440_000.0)
        self.assertAlmostEqual(c, 1000.0, places=6)

    def test_resolve_cycle_notional(self):
        """THROUGHPUT FLOOR (2026-08-03): sizing purely off the participation
        rate produced cycles far too small to build volume on a venue as thin as
        Nado — 5%/min of a $14.4M pair is $500 a minute. Every sized cycle is now
        floored at MIN_CYCLE_NOTIONAL_USD, and the cadence used for sizing is
        capped at MAX_CYCLE_INTERVAL_SECONDS, so the floor means throughput
        rather than being stretched over an arbitrarily long interval."""
        from src.nadobro.quant.pov_engine import MIN_CYCLE_NOTIONAL_USD
        from src.nadobro.strategy.bot_runtime import _resolve_mm_cycle_notional_usd

        base = {"interval_seconds": 60}  # 1-minute cycle; vol 14.4M → vpm 10000
        # On a thin pair every preset lands ON the floor — that is the point.
        for preset in ("aggressive", "normal", "passive"):
            got = _resolve_mm_cycle_notional_usd(
                {**base, "participation_preset": preset}, 100000, 14_400_000, 100
            )
            self.assertAlmostEqual(got, MIN_CYCLE_NOTIONAL_USD, places=6, msg=preset)

        # ABOVE the floor, participation still orders the presets. vpm = 1e6, so
        # even passive (1%) clears the floor and the rates separate again.
        deep = {**base, "participation_preset": None}
        agg, nrm, pas = (
            _resolve_mm_cycle_notional_usd(
                {**deep, "participation_preset": p}, 10**9, 1_440_000_000, 100
            )
            for p in ("aggressive", "normal", "passive")
        )
        self.assertAlmostEqual(agg, 100_000.0, places=6)
        self.assertAlmostEqual(nrm, 50_000.0, places=6)
        self.assertAlmostEqual(pas, 10_000.0, places=6)
        self.assertTrue(agg > nrm > pas)

        # Opt-out: no preset → 0 (keep deployed-based sizing).
        self.assertEqual(_resolve_mm_cycle_notional_usd(base, 100000, 14_400_000, 100), 0.0)
        # No volume → 0 (can't size a chunk).
        self.assertEqual(
            _resolve_mm_cycle_notional_usd({**base, "participation_preset": "normal"}, 100000, 0, 100), 0.0
        )
        # The DEPLOYED BUDGET still caps everything — the floor must never place
        # money the user has not allocated. This is the invariant that matters.
        self.assertAlmostEqual(
            _resolve_mm_cycle_notional_usd({**base, "participation_preset": "aggressive"}, 50, 14_400_000, 100),
            50.0, places=6,
        )
        self.assertAlmostEqual(
            _resolve_mm_cycle_notional_usd({**base, "participation_preset": "passive"}, 250, 14400, 100),
            250.0, places=6,
        )


class TwapPauseTests(unittest.TestCase):
    def test_disabled_by_default(self):
        from src.nadobro.strategy.bot_runtime import _twap_should_pause
        st = {}  # no twap_pause_move_bp → off
        self.assertFalse(_twap_should_pause(st, "mid", 100.0))
        self.assertFalse(st.get("twap_paused"))

    def test_rgrid_never_pauses(self):
        from src.nadobro.strategy.bot_runtime import _twap_should_pause
        # Even with a tight threshold and a huge move, R-Grid is excluded
        # (its momentum entry must fire on fast moves).
        st = {"twap_pause_move_bp": 50, "twap_last_mid": 100.0}
        self.assertFalse(_twap_should_pause(st, "rgrid", 130.0))

    def test_pauses_on_fast_move_resumes_when_settled(self):
        from src.nadobro.strategy.bot_runtime import _twap_should_pause
        st = {"twap_pause_move_bp": 200}  # 2% per-cycle threshold
        # First cycle establishes the baseline — no prior mid, no pause.
        self.assertFalse(_twap_should_pause(st, "mid", 100.0))
        self.assertEqual(st["twap_last_mid"], 100.0)
        # +3% jump → pause.
        self.assertTrue(_twap_should_pause(st, "mid", 103.0))
        self.assertTrue(st["twap_paused"])
        # Next cycle moves only +0.5% off the new baseline → resume.
        self.assertFalse(_twap_should_pause(st, "mid", 103.5))
        self.assertFalse(st["twap_paused"])


if __name__ == "__main__":
    unittest.main()
