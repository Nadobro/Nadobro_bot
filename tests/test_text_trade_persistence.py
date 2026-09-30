"""Guard against the bug where typing `confirm` for a text trade silently
fell through to the AI chat because in-process `context.user_data` was lost
between the preview and the confirmation (worker restart / multi-machine).
"""
import asyncio
import sys
import time
import types
import unittest
from unittest.mock import patch

from _stubs import install_test_stubs

install_test_stubs()


class _FakeMessage:
    def __init__(self):
        self.calls = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.calls.append({"text": text, "parse_mode": parse_mode, **kwargs})


class _FakeUpdate:
    def __init__(self):
        self.message = _FakeMessage()


class _FakeContext:
    def __init__(self):
        self.user_data: dict = {}


class _InMemoryBotState:
    """Drop-in replacement for `bot_state` table backed by a dict."""

    def __init__(self):
        self.store: dict = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


_store = _InMemoryBotState()


def _patched_get_bot_state(key):
    return _store.get(key)


def _patched_set_bot_state(key, value):
    _store.set(key, value)


def _patched_execute(sql, params=()):
    if "DELETE FROM bot_state" in sql and params:
        _store.delete(params[0])
    return None


_store_patches = [
    patch("src.nadobro.trading.text_trade_pending.get_bot_state", _patched_get_bot_state),
    patch("src.nadobro.trading.text_trade_pending.set_bot_state", _patched_set_bot_state),
    patch("src.nadobro.trading.text_trade_pending.execute", _patched_execute),
]


def _start_patches():
    for p in _store_patches:
        p.start()


def _stop_patches():
    for p in _store_patches:
        p.stop()


class TextTradePendingPersistenceTests(unittest.TestCase):
    def setUp(self):
        _store.store.clear()
        _start_patches()

    def tearDown(self):
        _stop_patches()

    def test_persist_and_load_roundtrip(self):
        from src.nadobro.trading.text_trade_pending import (
            clear_text_trade_pending,
            load_text_trade_pending,
            persist_text_trade_pending,
        )

        payload = {"direction": "short", "product": "BTC", "size": 0.05,
                   "leverage": 49, "order_type": "limit", "limit_price": 81239.0,
                   "price": 81239.0, "est_margin": 82.90, "slippage_pct": 1.0}
        persist_text_trade_pending(42, payload)
        loaded = load_text_trade_pending(42)
        self.assertIsNotNone(loaded)
        for k, v in payload.items():
            self.assertEqual(loaded.get(k), v)
        # _ts should be stripped from the returned payload.
        self.assertNotIn("_ts", loaded)

        clear_text_trade_pending(42)
        self.assertIsNone(load_text_trade_pending(42))

    def test_load_returns_none_after_ttl(self):
        from src.nadobro.trading import text_trade_pending as ttp

        ttp.persist_text_trade_pending(7, {"direction": "long", "product": "ETH"})
        # Force the stored timestamp to be beyond the TTL window.
        stored = _store.get(f"text_trade_pending:7")
        stored["_ts"] = time.time() - ttp.TEXT_TRADE_PENDING_TTL_SECONDS - 5

        self.assertIsNone(ttp.load_text_trade_pending(7))
        # Stale row should have been evicted.
        self.assertNotIn(f"text_trade_pending:7", _store.store)

    def test_close_all_persistence_roundtrip(self):
        from src.nadobro.trading.text_trade_pending import (
            clear_text_close_all_pending,
            load_text_close_all_pending,
            persist_text_close_all_pending,
        )

        self.assertFalse(load_text_close_all_pending(99))
        persist_text_close_all_pending(99)
        self.assertTrue(load_text_close_all_pending(99))
        clear_text_close_all_pending(99)
        self.assertFalse(load_text_close_all_pending(99))


class HandlePendingTextTradeConfirmationHydrationTest(unittest.TestCase):
    """`confirm` should execute the trade even when user_data was wiped."""

    def setUp(self):
        _store.store.clear()
        _start_patches()

    def tearDown(self):
        _stop_patches()

    def test_confirm_hydrates_pending_from_bot_state_and_executes(self):
        from src.nadobro.handlers import intent_handlers
        from src.nadobro.trading.text_trade_pending import persist_text_trade_pending

        payload = {
            "direction": "short", "product": "BTC", "order_type": "limit",
            "size": 0.05, "leverage": 49, "limit_price": 81239.0,
            "price": 81239.0, "est_margin": 82.90, "slippage_pct": 1.0,
            # Every preview is stamped with the network it was built on
            # (PREVIEW-NETWORK-BIND); an unstamped one is refused.
            "network": "mainnet",
        }
        # Simulate a prior preview that persisted state, then the worker
        # handling the user's `confirm` reply starts with empty user_data.
        persist_text_trade_pending(123, payload)

        executed = {}

        async def fake_execute_action_directly(update, context, telegram_id, action_data):
            executed["payload"] = action_data.get("payload")
            executed["type"] = action_data.get("type")

        fake_messages = types.ModuleType("src.nadobro.handlers.messages")
        fake_messages.execute_action_directly = fake_execute_action_directly
        prior_messages_module = sys.modules.get("src.nadobro.handlers.messages")
        sys.modules["src.nadobro.handlers.messages"] = fake_messages

        try:
            async def _active_network(_telegram_id):
                return "mainnet"

            with patch.object(intent_handlers, "is_trading_paused", return_value=False), \
                 patch.object(intent_handlers, "ensure_active_wallet_ready", return_value=(True, "")), \
                 patch.object(intent_handlers, "active_network", _active_network):

                update = _FakeUpdate()
                ctx = _FakeContext()
                handled = asyncio.run(
                    intent_handlers.handle_pending_text_trade_confirmation(update, ctx, 123, "confirm")
                )
        finally:
            if prior_messages_module is not None:
                sys.modules["src.nadobro.handlers.messages"] = prior_messages_module
            else:
                sys.modules.pop("src.nadobro.handlers.messages", None)

        self.assertTrue(handled)
        self.assertEqual(executed.get("type"), "execute_trade")
        self.assertEqual(executed.get("payload"), payload)
        # State must be cleared after successful execution.
        self.assertNotIn(intent_handlers.PENDING_TEXT_TRADE_KEY, ctx.user_data)
        self.assertNotIn("text_trade_pending:123", _store.store)

    def test_cancel_clears_persisted_state(self):
        from src.nadobro.handlers import intent_handlers
        from src.nadobro.trading.text_trade_pending import persist_text_trade_pending

        persist_text_trade_pending(
            555,
            {"direction": "long", "product": "ETH", "order_type": "market", "size": 1.0},
        )

        update = _FakeUpdate()
        ctx = _FakeContext()
        handled = asyncio.run(
            intent_handlers.handle_pending_text_trade_confirmation(update, ctx, 555, "cancel")
        )

        self.assertTrue(handled)
        self.assertNotIn("text_trade_pending:555", _store.store)

    def test_missing_state_returns_false_so_other_handlers_run(self):
        from src.nadobro.handlers import intent_handlers

        update = _FakeUpdate()
        ctx = _FakeContext()
        handled = asyncio.run(
            intent_handlers.handle_pending_text_trade_confirmation(update, ctx, 999, "confirm")
        )

        # With no pending state in either user_data or bot_state, the handler
        # must yield so downstream handlers (AI chat) can take the message.
        self.assertFalse(handled)


if __name__ == "__main__":
    unittest.main()
