import asyncio
import sys
import time
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from _stubs import install_test_stubs

install_test_stubs()

if "asyncpg" not in sys.modules:
    asyncpg_mod = types.ModuleType("asyncpg")

    class _AsyncpgPool:
        pass

    class _UniqueViolationError(Exception):
        pass

    asyncpg_mod.Pool = _AsyncpgPool
    asyncpg_mod.UniqueViolationError = _UniqueViolationError
    sys.modules["asyncpg"] = asyncpg_mod


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


async def _noop_async(*_args, **_kwargs):
    return None


def test_relay_poll_events_is_scoped_to_session(monkeypatch):
    from relay import event_store

    captured = {}

    class _Conn:
        async def fetch(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return [
                {
                    "cursor_id": 11,
                    "session_id": "sess_a",
                    "text": "hello",
                    "options_json": None,
                    "source_message_id": None,
                    "photo_bytes": None,
                    "photo_mime": None,
                    "created_at": datetime.now(timezone.utc),
                }
            ]

        async def execute(self, sql, *params):
            captured.setdefault("executed", []).append((sql, params))

        async def fetchrow(self, sql, *params):
            captured.setdefault("fetchrow", []).append((sql, params))
            return {"status": "active"}

    monkeypatch.setattr(event_store, "get_pool", lambda: _Pool(_Conn()))

    result = asyncio.run(event_store.poll_events(session_id="sess_a", cursor="10", limit=5))

    assert result["events"][0]["session_id"] == "sess_a"
    assert "e.session_id = $1" in captured["sql"]
    assert captured["params"] == ("sess_a", 10, 5)
    # Polling records liveness so cleanup_idle_sessions does not expire a session still in use.
    assert any(
        "last_polled_at = now()" in sql and params == ("sess_a",)
        for sql, params in captured.get("executed", [])
    )
    # Session status is surfaced so the bot can stop polling a dead session.
    assert result["session_status"] == "active"


def test_points_relay_poll_uses_session_scoped_cursor(monkeypatch):
    from src.nadobro.users import points_service

    captured = {}

    async def _poll_events(*, session_id, cursor):
        captured["session_id"] = session_id
        captured["cursor"] = cursor
        return {"ok": True, "events": [], "next_cursor": "12"}

    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_poll_events", _poll_events)
    monkeypatch.setattr(points_service, "_persist_relay_state", _noop_async)
    bot_app = type("BotApp", (), {"bot_data": {
        "lowiqpts_pending_queue": [{"relay_session_id": "sess_a"}],
        "lowiqpts_relay_cursor:sess_a": "11",
    }})()

    asyncio.run(points_service.poll_lowiqpts_relay_events(bot_app))

    assert captured == {"session_id": "sess_a", "cursor": "11"}
    assert bot_app.bot_data["lowiqpts_relay_cursor:sess_a"] == "12"


def test_points_relay_polls_all_pending_sessions(monkeypatch):
    from src.nadobro.users import points_service

    calls: list[tuple[str, object]] = []

    async def _poll_events(*, session_id, cursor):
        calls.append((session_id, cursor))
        return {"ok": True, "events": [], "next_cursor": None}

    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_poll_events", _poll_events)
    monkeypatch.setattr(points_service, "_persist_relay_state", _noop_async)
    bot_app = type("BotApp", (), {"bot_data": {
        "lowiqpts_pending_queue": [
            {"relay_session_id": "sess_a"},
            {"relay_session_id": "sess_b"},
        ],
    }})()

    asyncio.run(points_service.poll_lowiqpts_relay_events(bot_app))

    assert calls == [("sess_a", None), ("sess_b", None)]


def test_claim_pending_never_fallback_to_foreign_queue():
    """Dropped queue[0] fallback: unattributed relay text must not bind another user's pending row."""
    from src.nadobro.users import points_service

    w = "0x" + "ab" * 20
    row = {"req_id": "a", "chat_id": 1, "telegram_id": 1, "wallet": w, "ts": time.time(), "relay_session_id": ""}
    bd = {
        points_service._PENDING_QUEUE_KEY: [row],
        points_service._PENDING_BY_WALLET_KEY: {},
    }

    assert points_service._claim_pending_for_event(bd, "", "LOWIQPTS said something with no wallet and no session") is None


def test_claim_pending_matches_wallet_in_reply_text():
    from src.nadobro.users import points_service

    w = "0x" + "cd" * 20
    row = {"req_id": "b", "chat_id": 2, "telegram_id": 99, "wallet": w, "ts": time.time(), "relay_session_id": ""}
    wl = str(w).lower()
    bd = {
        points_service._PENDING_QUEUE_KEY: [row],
        points_service._PENDING_BY_WALLET_KEY: {wl: [row]},
    }

    claimed = points_service._claim_pending_for_event(bd, "", f"Points overview for {w}")
    assert claimed is row


def test_claim_pending_prefers_newest_wallet_row_when_stacked():
    """Multiple pending rows for one wallet: wallet fallback must bind to latest refresh, not queue[0]."""
    from src.nadobro.users import points_service

    w = "0x" + "ee" * 20
    wl = str(w).lower()
    now = time.time()
    old = {"req_id": "old", "chat_id": 1, "telegram_id": 1, "wallet": w, "ts": now - 30.0, "relay_session_id": ""}
    new = {"req_id": "new", "chat_id": 2, "telegram_id": 2, "wallet": w, "ts": now, "relay_session_id": ""}
    bd = {
        points_service._PENDING_QUEUE_KEY: [old, new],
        points_service._PENDING_BY_WALLET_KEY: {wl: [old, new]},
    }

    claimed = points_service._claim_pending_for_event(bd, "", f"Status for {w}")
    assert claimed is new


def test_lowiqpts_option_prompt_does_not_complete_pending_when_parse_matches(monkeypatch):
    """Regression: metrics embedded alongside Yes/No options must not drop pending before the user taps."""
    from src.nadobro.users import points_service

    completed: list[dict] = []
    monkeypatch.setattr(points_service, "_schedule_timeout", lambda *a, **k: None)
    monkeypatch.setattr(
        points_service,
        "complete_pending_request",
        lambda bd, req: completed.append(req),
    )

    wallet = "0x" + "de" * 20
    req_row = {
        "req_id": "req1",
        "chat_id": 42,
        "telegram_id": 7,
        "wallet": wallet.lower(),
        "ts": time.time(),
        "relay_session_id": "sess_z",
    }
    bot_data = {
        points_service._PENDING_QUEUE_KEY: [req_row],
        points_service._ACTIVE_BY_CHAT_KEY: {42: "req1"},
    }
    send_mock = AsyncMock()
    bot_app = SimpleNamespace(bot=SimpleNamespace(send_message=send_mock))
    event = {
        "session_id": "sess_z",
        "text": "Include extras?\n\nPoints: 100\nVolume: $50",
        "options": ["Yes", "No"],
    }

    asyncio.run(points_service._process_relay_event(bot_app, bot_data, event))

    assert completed == []
    assert send_mock.await_count == 1


def test_lowiqpts_mid_prompt_metrics_without_options_or_hints_does_not_complete(monkeypatch):
    """Extra-cost style prompts can embed Points:/Volume: without inline options; stay pending."""
    from src.nadobro.users import points_service

    completed: list[dict] = []
    monkeypatch.setattr(points_service, "_schedule_timeout", lambda *a, **k: None)
    monkeypatch.setattr(
        points_service,
        "complete_pending_request",
        lambda bd, req: completed.append(req),
    )

    wallet = "0x" + "de" * 20
    req_row = {
        "req_id": "req_mid",
        "chat_id": 42,
        "telegram_id": 7,
        "wallet": wallet.lower(),
        "ts": time.time(),
        "relay_session_id": "sess_mid",
    }
    bot_data = {
        points_service._PENDING_QUEUE_KEY: [req_row],
        points_service._ACTIVE_BY_CHAT_KEY: {42: "req_mid"},
    }
    send_mock = AsyncMock()
    bot_app = SimpleNamespace(bot=SimpleNamespace(send_message=send_mock))
    event = {
        "session_id": "sess_mid",
        "text": (
            "🐝 Nado data is already fetched from API.\n"
            "Points: 100\n"
            "Volume: $50\n"
            "Enter extra costs to include them in the report. If there were none, send 0."
        ),
        "options": [],
    }

    asyncio.run(points_service._process_relay_event(bot_app, bot_data, event))

    assert completed == []
    assert send_mock.await_count == 1


def test_parse_lowiq_points_reply_no_infer_zero_from_no_trades_phrase():
    """Loose 'no trades' copy must not produce a parsed snapshot (was prematurely completing relay)."""
    from src.nadobro.users import points_service

    assert (
        points_service.parse_lowiq_points_reply(
            "There were no trades last week.\nEnter extra costs.\nSend 0 if none."
        )
        is None
    )


def test_points_refresh_timeout_rearms_while_heartbeat_keeps_request_fresh(monkeypatch):
    """LOWIQPTS takes 20+ min per step; the poll heartbeat keeps req['ts'] fresh, so the
    timeout job must re-arm instead of dropping a still-live pending request."""
    from src.nadobro.users import points_service

    rescheduled: list[str] = []
    monkeypatch.setattr(
        points_service,
        "_schedule_timeout",
        lambda app, req_id: rescheduled.append(req_id),
    )

    req_row = {
        "req_id": "req_live",
        "chat_id": 42,
        "telegram_id": 7,
        "wallet": ("0x" + "ab" * 20),
        "ts": time.time(),  # heartbeat touched it this cycle
        "relay_session_id": "sess_live",
    }
    bot_data = {
        points_service._PENDING_QUEUE_KEY: [req_row],
        points_service._ACTIVE_BY_CHAT_KEY: {42: "req_live"},
    }
    send_mock = AsyncMock()
    context = SimpleNamespace(
        job=SimpleNamespace(data={"req_id": "req_live"}),
        application=SimpleNamespace(bot_data=bot_data),
        bot=SimpleNamespace(send_message=send_mock),
    )

    asyncio.run(points_service._on_points_refresh_timeout(context))

    assert rescheduled == ["req_live"]
    assert req_row in bot_data[points_service._PENDING_QUEUE_KEY]
    assert send_mock.await_count == 0


def test_points_refresh_timeout_drops_request_when_genuinely_stale(monkeypatch):
    """No heartbeat for the full window: the request is expired and the user is notified."""
    from src.nadobro.users import points_service

    monkeypatch.setattr(points_service, "_schedule_timeout", lambda *a, **k: None)
    close_mock = AsyncMock()
    monkeypatch.setattr(points_service, "relay_close_session", close_mock)

    req_row = {
        "req_id": "req_stale",
        "chat_id": 42,
        "telegram_id": 7,
        "wallet": ("0x" + "cd" * 20),
        "ts": time.time() - points_service._POINTS_REPLY_TIMEOUT_SECONDS - 60,
        "relay_session_id": "sess_stale",
    }
    bot_data = {
        points_service._PENDING_QUEUE_KEY: [req_row],
        points_service._ACTIVE_BY_CHAT_KEY: {42: "req_stale"},
    }
    send_mock = AsyncMock()
    context = SimpleNamespace(
        job=SimpleNamespace(data={"req_id": "req_stale"}),
        application=SimpleNamespace(bot_data=bot_data),
        bot=SimpleNamespace(send_message=send_mock),
    )

    asyncio.run(points_service._on_points_refresh_timeout(context))

    assert req_row not in bot_data[points_service._PENDING_QUEUE_KEY]
    close_mock.assert_awaited_once()
    assert send_mock.await_count == 1


def test_orphan_lowiqpts_reply_pattern_matches_bare_number_and_yes_no():
    """Safety net classifier for messages that almost certainly wanted to reach the relay."""
    from src.nadobro.users.points_service import looks_like_orphan_lowiqpts_reply

    for s in ["0", " 0 ", "12", "0.5", "yes", "Yes", "YES", "no", "n", "y"]:
        assert looks_like_orphan_lowiqpts_reply(s), f"expected match: {s!r}"
    for s in ["", "hello", "0 not", "yesplease", "no thanks", "/start", "buy 100"]:
        assert not looks_like_orphan_lowiqpts_reply(s), f"expected no match: {s!r}"


def test_serialize_relay_state_captures_queue_and_cursors():
    from src.nadobro.users import points_service

    req = {"req_id": "r1", "chat_id": 1, "wallet": "0xabc", "relay_session_id": "sess_1"}
    bot_data = {
        points_service._PENDING_QUEUE_KEY: [req],
        f"{points_service._RELAY_CURSOR_KEY}:sess_1": "42",
        "unrelated_key": "ignore-me",
    }

    state = points_service._serialize_relay_state(bot_data)

    assert state["queue"] == [req]
    assert state["queue"][0] is not req  # serialized as a copy, not the live object
    assert state["cursors"] == {"sess_1": "42"}


def test_rehydrate_lowiqpts_pending_state_round_trip(monkeypatch):
    """A bot restart must resume in-flight refreshes from bot_state, not drop them."""
    from src.nadobro.users import points_service

    wallet_a = "0x" + "ab" * 20
    wallet_b = "0x" + "cd" * 20
    saved = {
        "queue": [
            {"req_id": "r1", "telegram_id": 1, "chat_id": 100, "wallet": wallet_a,
             "ts": 1000.0, "relay_session_id": "sess_1"},
            {"req_id": "r2", "telegram_id": 2, "chat_id": 200, "wallet": wallet_b,
             "ts": 1001.0, "relay_session_id": "sess_2"},
            # No relay session => was mid-start at crash; unrecoverable, must be dropped.
            {"req_id": "r3", "telegram_id": 3, "chat_id": 300, "wallet": "0x" + "ef" * 20,
             "ts": 1002.0},
        ],
        "cursors": {"sess_1": "55", "sess_2": "66", "sess_dead": "77"},
    }
    monkeypatch.setattr(points_service, "get_bot_state", lambda key: saved)
    rearmed: list[str] = []
    monkeypatch.setattr(
        points_service, "_schedule_timeout",
        lambda application, req_id: rearmed.append(req_id),
    )

    application = SimpleNamespace(bot_data={})
    points_service.rehydrate_lowiqpts_pending_state(application)
    bot_data = application.bot_data

    queue = bot_data[points_service._PENDING_QUEUE_KEY]
    assert sorted(r["req_id"] for r in queue) == ["r1", "r2"]  # r3 dropped
    # Grace period: ts is refreshed to "now" so prune/timeout do not fire on resume.
    assert all(r["ts"] > 1002.0 for r in queue)
    by_wallet = bot_data[points_service._PENDING_BY_WALLET_KEY]
    assert set(by_wallet.keys()) == {wallet_a.lower(), wallet_b.lower()}
    active = bot_data[points_service._ACTIVE_BY_CHAT_KEY]
    assert active == {100: "r1", 200: "r2"}
    # Cursors restored only for sessions we are actually resuming.
    assert bot_data[f"{points_service._RELAY_CURSOR_KEY}:sess_1"] == "55"
    assert bot_data[f"{points_service._RELAY_CURSOR_KEY}:sess_2"] == "66"
    assert f"{points_service._RELAY_CURSOR_KEY}:sess_dead" not in bot_data
    # Timeout jobs re-armed so each resumed flow stays bounded.
    assert sorted(rearmed) == ["r1", "r2"]


def test_poll_finalizes_dead_relay_session(monkeypatch):
    """If the relay session expired underneath us, stop polling it and notify the user."""
    from src.nadobro.users import points_service

    async def _poll_events(*, session_id, cursor):
        return {"ok": True, "events": [], "next_cursor": None, "session_status": "expired"}

    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_poll_events", _poll_events)
    monkeypatch.setattr(points_service, "_persist_relay_state", _noop_async)

    req_row = {
        "req_id": "req_dead",
        "chat_id": 77,
        "telegram_id": 7,
        "wallet": "0x" + "ab" * 20,
        "ts": time.time(),
        "relay_session_id": "sess_dead",
    }
    bot_data = {
        points_service._PENDING_QUEUE_KEY: [req_row],
        points_service._ACTIVE_BY_CHAT_KEY: {77: "req_dead"},
        f"{points_service._RELAY_CURSOR_KEY}:sess_dead": "9",
    }
    send_mock = AsyncMock()
    bot_app = SimpleNamespace(bot_data=bot_data, bot=SimpleNamespace(send_message=send_mock))

    asyncio.run(points_service.poll_lowiqpts_relay_events(bot_app))

    assert bot_data[points_service._PENDING_QUEUE_KEY] == []  # dead req finalized
    assert f"{points_service._RELAY_CURSOR_KEY}:sess_dead" not in bot_data  # cursor dropped
    assert send_mock.await_count == 1


def test_poll_skips_persist_on_heartbeat_only(monkeypatch):
    """Empty poll must not write bot_state every 2s (nrt→Postgres log/slot storm)."""
    from src.nadobro.users import points_service

    persist_calls = []

    async def _poll_events(*, session_id, cursor):
        return {"ok": True, "events": [], "next_cursor": None}

    async def _persist(bot_data):
        persist_calls.append(True)

    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_poll_events", _poll_events)
    monkeypatch.setattr(points_service, "_persist_relay_state", _persist)
    bot_app = type("BotApp", (), {"bot_data": {
        "lowiqpts_pending_queue": [{"relay_session_id": "sess_a", "req_id": "r1", "ts": 1.0}],
        "lowiqpts_relay_cursor:sess_a": "11",
    }})()

    asyncio.run(points_service.poll_lowiqpts_relay_events(bot_app))

    assert persist_calls == []
    # Heartbeat still advances in-memory ts so the timeout job does not fire.
    assert bot_app.bot_data["lowiqpts_pending_queue"][0]["ts"] > 1.0


def test_poll_persists_when_cursor_advances(monkeypatch):
    from src.nadobro.users import points_service

    persist_calls = []

    async def _poll_events(*, session_id, cursor):
        return {"ok": True, "events": [], "next_cursor": "12"}

    async def _persist(bot_data):
        persist_calls.append(True)

    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_poll_events", _poll_events)
    monkeypatch.setattr(points_service, "_persist_relay_state", _persist)
    bot_app = type("BotApp", (), {"bot_data": {
        "lowiqpts_pending_queue": [{"relay_session_id": "sess_a", "req_id": "r1", "ts": 1.0}],
        "lowiqpts_relay_cursor:sess_a": "11",
    }})()

    asyncio.run(points_service.poll_lowiqpts_relay_events(bot_app))

    assert persist_calls == [True]
    assert bot_app.bot_data["lowiqpts_relay_cursor:sess_a"] == "12"


def test_parse_lowiq_points_reply_handles_markdown_volume_lines():
    from src.nadobro.users import points_service

    blob = """📊 NADO REPORT
Period: 1 May - 8 May Epoch 14
**Core:**
**Volume:** 1 134.62
**Points:** 502.14
**Cost/Point:** -0.03
"""
    parsed = points_service.parse_lowiq_points_reply(blob)
    assert parsed is not None
    assert abs(parsed["volume_usd"] - 1134.62) < 0.01
    assert abs(parsed["points"] - 502.14) < 0.01
    assert abs(parsed["cost_per_point"] - (-0.03)) < 0.0001
    assert points_service._looks_like_nado_report_summary_text(blob)
