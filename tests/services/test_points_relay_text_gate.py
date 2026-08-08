"""A pending LOWIQPTS points refresh must not swallow trade text.

Production bug (2026-08-06): the points relay claimed EVERY free-text message
while a refresh was pending (TTL 30 min) and sits ABOVE the trade parsers in
``handlers/messages.py``. A user typing "Short 0.3 ETH 49x" had it forwarded to
LOWIQPTS, which answered "Data error: Текст должен быть JSON массивом/объектом
или JSONL" — the trade never reached the venue.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from _stubs import install_test_stubs

install_test_stubs()


TRADE_TEXTS = [
    "Short 0.3 ETH 49x",
    "long 0.5 btc 10x",
    "buy 100 usd of eth",
    "close all",
    "sell 2 sol market",
    "limit 3000 eth",
    "twap 500 eth over 10m",
]

ANSWER_TEXTS = ["0", "0.5", "0,5", "0,1,2", "yes", "No", "Y", "n", "ok", "да", "нет", "skip"]


def test_trade_text_is_not_a_lowiqpts_answer():
    from src.nadobro.users.points_service import text_is_lowiqpts_answer

    for text in TRADE_TEXTS:
        assert not text_is_lowiqpts_answer(text), text


def test_bare_numbers_yes_no_and_wallets_still_reach_the_relay():
    from src.nadobro.users.points_service import text_is_lowiqpts_answer

    for text in ANSWER_TEXTS:
        assert text_is_lowiqpts_answer(text), text
    assert text_is_lowiqpts_answer("0x" + "ab" * 20)


def test_relay_option_labels_pass_even_when_they_look_like_prose():
    from src.nadobro.users.points_service import text_is_lowiqpts_answer

    req = {"relay_options": ["Только Nado", "Все площадки"]}
    assert text_is_lowiqpts_answer("Только Nado", req)
    assert text_is_lowiqpts_answer("все площадки", req)  # case-insensitive
    assert not text_is_lowiqpts_answer("Short 0.3 ETH 49x", req)


def _pending_bot_data(chat_id: int = 42):
    req_row = {
        "req_id": "req_live",
        "chat_id": chat_id,
        "telegram_id": 7,
        "wallet": ("0x" + "ab" * 20),
        "ts": time.time(),
        "relay_session_id": "sess_live",
    }
    from src.nadobro.users import points_service

    return req_row, {
        points_service._PENDING_QUEUE_KEY: [req_row],
        points_service._ACTIVE_BY_CHAT_KEY: {chat_id: "req_live"},
    }


def test_relay_declines_trade_text_and_leaves_the_request_pending(monkeypatch):
    """handled=False makes handlers/messages.py fall through to the trade parsers."""
    from src.nadobro.users import points_service

    req_row, bot_data = _pending_bot_data()
    send_mock = AsyncMock()
    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_send_user_reply", send_mock)
    monkeypatch.setattr(points_service, "_schedule_timeout", lambda *a, **k: None)

    context = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))
    result = asyncio.run(
        points_service.relay_user_reply_to_lowiqpts(context, 42, "Short 0.3 ETH 49x")
    )

    assert result == {"ok": False, "handled": False}
    assert send_mock.await_count == 0, "trade text must never be forwarded to LOWIQPTS"
    # The points request is untouched — a later "0" still resolves it.
    assert req_row in bot_data[points_service._PENDING_QUEUE_KEY]


def test_relay_still_forwards_a_bare_number(monkeypatch):
    from src.nadobro.users import points_service

    _, bot_data = _pending_bot_data()
    send_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_send_user_reply", send_mock)
    monkeypatch.setattr(points_service, "_schedule_timeout", lambda *a, **k: None)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(points_service, "_persist_relay_state", _noop)

    context = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))
    result = asyncio.run(points_service.relay_user_reply_to_lowiqpts(context, 42, "0"))

    assert result["handled"] is True and result["ok"] is True
    send_mock.assert_awaited_once()
    assert send_mock.await_args.kwargs["text"] == "0"


def test_cancel_words_still_close_the_session(monkeypatch):
    """/cancel must be handled ABOVE the answer-shape gate."""
    from src.nadobro.users import points_service

    _, bot_data = _pending_bot_data()
    close_mock = AsyncMock()
    monkeypatch.setattr(points_service, "relay_is_configured", lambda: True)
    monkeypatch.setattr(points_service, "relay_close_session", close_mock)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(points_service, "_persist_relay_state", _noop)

    context = SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))
    result = asyncio.run(points_service.relay_user_reply_to_lowiqpts(context, 42, "/cancel"))

    assert result == {"ok": True, "handled": True, "cancelled": True}
    close_mock.assert_awaited_once()
