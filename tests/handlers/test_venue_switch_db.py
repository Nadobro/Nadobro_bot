"""The /venue switch against real Postgres (Arcus P1). Auto-skips without a DB.

Drives the real handler stack (venue_callback_ack -> with_user_serialized ->
handle_venue_callback -> users/venue_service CAS -> run_blocking_db) and checks
the row, the persisted pending Nado flows and the boot-time registration count.
Every Nado stop / start / cancel entry point is trapped: a switch touches none.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest


def _db_reachable() -> bool:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL")):
        return False
    try:
        import psycopg2

        url = os.environ.get("SUPABASE_DATABASE_URL") or os.environ["DATABASE_URL"]
        psycopg2.connect(url).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="no reachable Postgres (DATABASE_URL)")

UID = 990_023_401


def _cleanup():
    from src.nadobro.db import execute
    from src.nadobro.users.user_service import invalidate_user_cache

    execute("DELETE FROM bot_state WHERE key LIKE %s", (f"%:{UID}",))
    execute("DELETE FROM audit_logs WHERE user_id = %s", (UID,))
    execute("DELETE FROM users WHERE telegram_id = %s", (UID,))
    invalidate_user_cache(UID)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    from _stubs import install_test_stubs

    install_test_stubs()
    from src.nadobro.handlers import callbacks
    from src.nadobro.handlers import update_serialization as us
    from src.nadobro.i18n import _ACTIVE_LANG
    from src.nadobro.strategy import bot_runtime, network_switch
    from src.nadobro.trading import copy_service, desk_store
    from src.nadobro.users import user_service

    def trap(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"venue switch must never call {name}")
        return _raise

    for module, names in {
        bot_runtime: ("stop_user_bot", "stop_all_user_bots", "start_user_bot", "stop_all_automation_for_user"),
        copy_service: ("stop_copy", "pause_copy", "resume_copy", "stop_all_copies"),
        desk_store: ("cancel_plan",),
        network_switch: ("switch_network",),
        user_service: ("set_network_mode", "get_user_nado_client", "get_user_readonly_client"),
    }.items():
        for name in names:
            monkeypatch.setattr(module, name, trap(name))

    async def home_text(_uid):
        return "🏠 *Home*"

    monkeypatch.setattr(callbacks, "build_home_card_text_async", home_text)
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_ALLOWED_USER_IDS", raising=False)
    us._user_locks.clear()
    token = _ACTIVE_LANG.set("en")
    _cleanup()
    yield
    _cleanup()
    _ACTIVE_LANG.reset(token)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(chat_id=UID)

    async def answer(self, text=None, show_alert=False, **_kw):
        self.answers.append((text, bool(show_alert)))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


def _tap(data, context):
    from src.nadobro.handlers import update_serialization as us
    from src.nadobro.handlers import venue_handler as vh

    update = SimpleNamespace(callback_query=FakeQuery(data), effective_user=SimpleNamespace(id=UID),
                             effective_message=None)

    async def body():
        await vh.venue_callback_ack(us.with_user_serialized(vh.handle_venue_callback))(update, context)
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(body())
    return update.callback_query


def _row():
    from src.nadobro.db import query_one

    return query_one("SELECT active_venue, network_mode FROM users WHERE telegram_id = %s", (UID,))


def test_round_trip_against_postgres(monkeypatch):
    from src.nadobro.db import execute
    from src.nadobro.handlers import venue_gate
    from src.nadobro.handlers import venue_handler as vh
    from src.nadobro.strategy.strategy_pending_input import (
        load_strategy_pending_input,
        persist_strategy_pending_input,
    )
    from src.nadobro.trading.text_trade_pending import (
        load_text_close_all_pending,
        load_text_trade_pending,
        persist_text_close_all_pending,
        persist_text_trade_pending,
    )

    execute(
        "INSERT INTO users (telegram_id, telegram_username, language, network_mode) VALUES (%s, %s, %s, %s)",
        (UID, "pytest_venue_switch", "en", "mainnet"),
    )
    monkeypatch.setattr(vh, "is_new_onboarding_complete", lambda _uid: True)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))

    # A text-trade preview + close-all confirm + strategy input pending on Nado.
    persist_text_trade_pending(UID, {"product": "BTC", "side": "long", "size": 0.01})
    persist_text_close_all_pending(UID)
    persist_strategy_pending_input(UID, {"strategy": "grid", "field": "levels"})
    assert load_text_trade_pending(UID) and load_text_close_all_pending(UID) and load_strategy_pending_input(UID)

    context = SimpleNamespace(user_data={"pending_text_trade": {"x": 1}, "vault_op_inflight": True})
    q = _tap("venue:set:arcus", context)
    assert q.answers == [(vh.TEXT_SWITCHED_TO_ARCUS, False)]
    assert _row() == {"active_venue": "arcus", "network_mode": "mainnet"}
    assert "ARCUS · beta" in q.edits[0][0]
    # The persisted previews are gone, so a "yes" after switching back can
    # never execute a trade previewed before the switch.
    assert load_text_trade_pending(UID) is None
    assert load_text_close_all_pending(UID) is False
    assert load_strategy_pending_input(UID) is None
    assert context.user_data == {"vault_op_inflight": True}
    # The gate reads the new venue straight away (the user cache was invalidated).
    assert asyncio.run(vh.read_active_venue(UID)) == "arcus"
    # A user on the Arcus view keeps the gate registered after the flag goes off.
    monkeypatch.delenv("ARCUS_ENABLED")
    assert venue_gate.should_register_venue_gate() is True

    q = _tap("venue:set:nado", SimpleNamespace(user_data={}))  # flag off: never gated
    assert q.answers == [(vh.TEXT_SWITCHED_TO_NADO, False)]
    assert _row() == {"active_venue": "nado", "network_mode": "mainnet"}
    assert q.edits[0][0] == "🏠 *Home*"
    assert asyncio.run(vh.read_active_venue(UID)) == "nado"


def test_refused_switch_leaves_the_row_alone(monkeypatch):
    from src.nadobro.db import execute
    from src.nadobro.handlers import venue_handler as vh

    execute(
        "INSERT INTO users (telegram_id, telegram_username, language, network_mode) VALUES (%s, %s, %s, %s)",
        (UID, "pytest_venue_switch", "en", "testnet"),
    )
    q = _tap("venue:set:arcus", SimpleNamespace(user_data={}))  # flag off
    assert q.answers == [(vh.TEXT_ARCUS_NOT_ALLOWED, True)]
    assert _row() == {"active_venue": "nado", "network_mode": "testnet"}
    assert q.edits == []


def test_home_banner_reads_real_tables_without_error():
    from src.nadobro.db import execute
    from src.nadobro.handlers import venue_handler as vh

    execute(
        "INSERT INTO users (telegram_id, telegram_username, language, network_mode) VALUES (%s, %s, %s, %s)",
        (UID, "pytest_venue_switch", "en", "mainnet"),
    )
    net, items, failed = vh.nado_automation_snapshot(UID)
    assert (net, items, failed) == ("testnet", [], False)
