"""Shared fakes for the Arcus P3b handler tests (03 §19.7-§19.9).

``tests/`` is on ``sys.path`` (conftest), so test files ``import arcus_handler_helpers``.

- :class:`FakeBot` / :class:`FakeMessage` / :class:`FakeQuery` record every
  Telegram call (send / reply / edit / delete) with its text and markup.
- :func:`install` wires the link-service fakes (``arcus_link_helpers.install``:
  scripted Arcus reads, an off-loop-asserting fake DB) plus the handler-level
  seams (network mode, venue, language) and resets the handler task registry.
- :func:`drain` awaits every background link task (they may spawn more).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import arcus_link_helpers as H
from src.nadobro.core import async_utils
from src.nadobro.handlers import arcus_wallet_handler as awh
from src.nadobro.users import venue_service

UID = H.UID
CHAT = UID


def buttons(markup: Any) -> list[tuple[str, Any, Any]]:
    """[(label, callback_data, url)] of an inline keyboard (stub- and PTB-safe)."""
    if markup is None:
        return []
    return [
        (b.text, getattr(b, "callback_data", None), getattr(b, "url", None))
        for row in markup.inline_keyboard
        for b in row
    ]


def callback_data(markup: Any) -> list[Any]:
    return [data for _label, data, _url in buttons(markup) if data is not None]


class SentMessage:
    """A message the bot sent (or the tapped card): edits are recorded."""

    def __init__(self, log: list[tuple[str, Any]], chat_id: int = CHAT, text: str = "") -> None:
        self.chat_id = chat_id
        self.text = text
        self.edits: list[tuple[str, dict[str, Any]]] = []
        self._log = log

    async def edit_text(self, text: str, **kw: Any) -> "SentMessage":
        self.edits.append((text, kw))
        self._log.append(("edit", text))
        self.text = text
        return self


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict[str, Any]]] = []
        self.log: list[tuple[str, Any]] = []
        self.fail_send: BaseException | None = None

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> SentMessage:
        if self.fail_send is not None:
            raise self.fail_send
        self.sent.append((chat_id, text, kw))
        self.log.append(("send", text))
        return SentMessage(self.log, chat_id, text)

    def texts(self) -> list[str]:
        return [t for _c, t, _k in self.sent]


class FakeMessage:
    """A user message (new or edited): delete + reply are recorded."""

    def __init__(
        self,
        text: str | None = None,
        *,
        caption: str | None = None,
        log: list[tuple[str, Any]] | None = None,
        delete_error: BaseException | None = None,
        no_delete: bool = False,
    ) -> None:
        self.text = text
        self.caption = caption
        self.entities = ()
        self.chat_id = CHAT
        self.deleted = 0
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.log = log if log is not None else []
        self._delete_error = delete_error
        if no_delete:
            self.delete = None  # type: ignore[assignment]

    async def delete(self) -> bool:
        self.log.append(("delete", None))
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted += 1
        return True

    async def reply_text(self, text: str, **kw: Any) -> SentMessage:
        self.replies.append((text, kw))
        self.log.append(("reply", text))
        return SentMessage(self.log, self.chat_id, text)


class FakeQuery:
    """A tapped callback: ``edit_message_text`` edits ``message`` (the card)."""

    def __init__(self, data: str, *, card: SentMessage | None = None, log: list[tuple[str, Any]] | None = None) -> None:
        self.data = data
        self.log = log if log is not None else []
        self.message = card if card is not None else SentMessage(self.log, CHAT, "card")
        self.answers: list[tuple[Any, bool]] = []
        self.edits: list[tuple[str, dict[str, Any]]] = []

    async def answer(self, text: Any = None, show_alert: bool = False, **_kw: Any) -> None:
        self.answers.append((text, bool(show_alert)))

    async def edit_message_text(self, text: str, **kw: Any) -> None:
        self.edits.append((text, kw))
        self.log.append(("card", text))
        self.message.text = text


def ctx(bot: FakeBot | None = None, **user_data: Any) -> SimpleNamespace:
    return SimpleNamespace(user_data=dict(user_data), bot=bot if bot is not None else FakeBot())


def update_for(message: FakeMessage | None = None, *, edited: FakeMessage | None = None, uid: int = UID) -> SimpleNamespace:
    return SimpleNamespace(
        callback_query=None,
        message=message,
        edited_message=edited,
        effective_user=SimpleNamespace(id=uid, username="pytest_arcus"),
        effective_message=message if message is not None else edited,
        effective_chat=SimpleNamespace(id=CHAT, type="private"),
    )


class World:
    """Handler-level seams on top of the link-service fakes."""

    def __init__(self, monkeypatch: Any, *, network: str = "testnet", venue: Any = "arcus", env: Any = None) -> None:
        self.env = env if env is not None else H.install(monkeypatch)
        self.network = network
        self.venue = venue
        self.mode_error: BaseException | None = None
        self.credentials: Any = {}
        self.set_mode_calls: list[tuple[int, str]] = []
        self.set_mode_result: Any = "switched"
        awh._reset_for_tests()

        def get_mode(uid: int) -> str:
            H._off_loop()
            if self.mode_error is not None:
                raise self.mode_error
            return self.network

        def set_mode(uid: int, mode: str) -> Any:
            H._off_loop()
            self.set_mode_calls.append((uid, mode))
            if isinstance(self.set_mode_result, BaseException):
                raise self.set_mode_result
            if self.set_mode_result == "switched":
                self.network = mode
            return self.set_mode_result

        def for_user(uid: int) -> Any:
            H._off_loop()
            if isinstance(self.credentials, BaseException):
                raise self.credentials
            return dict(self.credentials)

        async def read_venue(uid: int) -> str:
            if isinstance(self.venue, BaseException):
                raise self.venue
            return self.venue

        monkeypatch.setattr(venue_service, "get_arcus_network_mode", get_mode)
        monkeypatch.setattr(venue_service, "set_arcus_network_mode", set_mode)
        monkeypatch.setattr(awh.creds, "get_credentials_for_user", for_user)
        monkeypatch.setattr(awh, "read_active_venue", read_venue)
        monkeypatch.setattr(awh, "get_user_language", lambda uid: "en")

    @property
    def db(self) -> Any:
        return self.env.db

    @property
    def client(self) -> Any:
        return self.env.client


async def drain(rounds: int = 60) -> None:
    """Await every running background task (link tasks may spawn more)."""
    for _ in range(rounds):
        pending = [t for t in list(awh._TASKS.values()) if not t.done()]
        pending += [t for t in list(async_utils._background_tasks) if not t.done() and t is not asyncio.current_task()]
        if not pending:
            await asyncio.sleep(0)
            if not any(not t.done() for t in list(awh._TASKS.values())):
                return
            continue
        await asyncio.gather(*pending, return_exceptions=True)
