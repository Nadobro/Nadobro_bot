"""Serialize Telegram updates per ``telegram_id`` to reduce ``user_data`` races."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackContext, ContextTypes

from src.nadobro.core.async_utils import fire_and_forget
from src.nadobro.core.perf import increment_counter
from src.nadobro.i18n import get_active_language, localize_text
from src.nadobro.utils.env import env_float

logger = logging.getLogger(__name__)

_registry_guard = asyncio.Lock()
_user_locks: dict[int, asyncio.Lock] = {}

# Head-of-line protection. Every update for a user shares one lock, so a handler
# that hangs (a wedged gateway call, a relay round-trip) used to freeze that
# user's whole UI: production saw ``callback.total`` max=84s while p50 was 371ms.
# Waiting forever is worse than declining — by the time the queue drains the user
# has tapped five more times and those stale actions all replay. Bound the wait,
# say so, and let the user retry.
_LOCK_WAIT_SECONDS = env_float("NADO_UPDATE_LOCK_WAIT_SECONDS", 20.0)
# One "still working" notice per user per window, so a tap storm gets one reply.
_BUSY_NOTICE_COOLDOWN_SECONDS = env_float("NADO_UPDATE_BUSY_NOTICE_SECONDS", 5.0)
_busy_notice_sent: dict[int, float] = {}

_BUSY_TEXT = "⏳ Still finishing your last action — try that again in a moment."


# ``points:cancel`` answers its own callback query with ``show_alert=True`` when
# no points request is pending (see handlers/callbacks.py::_handle_points). A
# pre-ack would consume the query and swallow that alert. This mirrors exactly
# the exclusion ``_handle_callback_inner`` used to apply, so which queries get a
# bare ack is unchanged — only *when* it happens moved.
_NO_PREACK_CALLBACK_DATA = frozenset({"points:cancel"})


async def _lock_for_user(telegram_id: int) -> asyncio.Lock:
    async with _registry_guard:
        lock = _user_locks.get(telegram_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[telegram_id] = lock
        return lock


async def _safe_answer(query) -> None:
    """Clear Telegram's button spinner; stale/expired queries are not an error."""
    try:
        await query.answer()
    except BadRequest as exc:
        msg = str(exc)
        if "Query is too old" not in msg and "query id is invalid" not in msg:
            logger.debug("callback pre-ack failed: %s", exc)
    except TelegramError as exc:
        logger.debug("callback pre-ack failed: %s", exc)


def with_callback_ack(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]:
    """Answer a callback query BEFORE the per-user lock, without awaiting it.

    Two wins over acking inside the handler: the spinner stops even while the
    user's previous action is still running, and the tap no longer pays a full
    Telegram round-trip before any real work starts (that RTT was a visible
    chunk of the 371ms p50 on button taps).
    """

    async def _wrapped(update: Update, context: CallbackContext):
        query = update.callback_query
        if query is not None and (query.data or "") not in _NO_PREACK_CALLBACK_DATA:
            fire_and_forget(_safe_answer(query), name="callback-ack")
        return await handler(update, context)

    return _wrapped


async def _notify_busy(update: Update, telegram_id: int) -> None:
    now = time.monotonic()
    last = _busy_notice_sent.get(telegram_id, 0.0)
    if now - last < _BUSY_NOTICE_COOLDOWN_SECONDS:
        return
    _busy_notice_sent[telegram_id] = now
    if len(_busy_notice_sent) > 5000:  # bound the bookkeeping
        for key, ts in list(_busy_notice_sent.items()):
            if now - ts > _BUSY_NOTICE_COOLDOWN_SECONDS:
                _busy_notice_sent.pop(key, None)
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text(localize_text(_BUSY_TEXT, get_active_language()))
    except TelegramError as exc:
        logger.debug("busy notice failed for %s: %s", telegram_id, exc)


def with_user_serialized(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Any]]:
    """Wrap an async handler so concurrent updates for the same user run sequentially."""

    async def _wrapped(update: Update, context: CallbackContext):
        user = update.effective_user
        if user is None:
            return await handler(update, context)
        telegram_id = int(user.id)
        ul = await _lock_for_user(telegram_id)
        try:
            await asyncio.wait_for(ul.acquire(), timeout=_LOCK_WAIT_SECONDS)
        except asyncio.TimeoutError:
            increment_counter("update.serialization.timeout")
            logger.warning(
                "Dropping update for user %s: previous handler still holding the "
                "serialization lock after %.0fs",
                telegram_id,
                _LOCK_WAIT_SECONDS,
            )
            await _notify_busy(update, telegram_id)
            return None
        try:
            return await handler(update, context)
        finally:
            ul.release()

    return _wrapped
