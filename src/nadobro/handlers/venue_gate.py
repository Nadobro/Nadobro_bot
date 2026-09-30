"""Per-venue gate (Arcus Phase 1): what a user on the Arcus VIEW may reach.

Nado and Arcus run IN PARALLEL; ``users.active_venue`` only picks which venue's
screens the user sees, and this gate enforces that choice on every update
BEFORE any group-0 handler runs — and again INSIDE the per-user lock, just
before the group-0 handler itself (``venue_recheck``): an update passed at
arrival can queue behind a venue switch. It never stops, cancels or resumes
anything.

Classification lives in ``utils/venue_capabilities.py``. Per update:

* Nado-view users: everything passes exactly as today, except ``ax:*``
  buttons (denied with a hint).
* Arcus-view users:
  - NEVER_GATE (every Nado stop / close / cancel / remove path, /stop_all,
    /agent_off, /revoke, the /desk list) and NEUTRAL (/venue, help, language,
    terms) pass;
  - DISPATCH views render the Arcus target instead (ax:home, ax:settings or
    the "Not on Arcus yet" card), inside the per-user lock, then stop;
  - NADO_ONLY and UNKNOWN are denied with a localized hint (fail-closed);
  - free text never reaches Nado trade parsing, the LOWIQPTS relay or the LLM
    chat: a reply-keyboard view button renders its Arcus screen, anything
    else gets a short hint. Message text is NEVER logged (a user may paste a
    secret);
  - non-text messages and edited messages stop silently.

NEVER_GATE / NEUTRAL taps pass without even reading the venue, and the gate
never answers a callback query it lets through (group 0's ``with_callback_ack``
and ``points:cancel``'s own alert are unchanged).

Venue read: the in-process user cache (the language middleware warmed it for
this very update), else Postgres off the loop. If it cannot be read, the venue
this process last saw for the user decides (``venue_service.last_known_venue``):
a user known to be on the Arcus view stays gated (fail-closed), and only a user
never seen there since boot is treated as Nado-view — failing closed for
EVERYONE on a DB blip would take the whole bot down. A crash AFTER the venue
read as Arcus denies and stops — PTB hands a handler exception to the error
handler and then carries on to the next group, so an unguarded crash would let
the update through.

Wiring (``register_venue_handlers``, called from main.setup_bot): a TypeHandler
in its OWN group, -1 — after the private-chat filter (-3) and the language
middleware (-2), ahead of group 0 — plus ``/venue`` and the ``venue:``/``ax:``
callbacks in group 0 ahead of the catch-all; and ``serialized_for`` wraps every
Nado group-0 handler with the in-lock re-check. Registered ONLY when
ARCUS_ENABLED is on or a user is already on the Arcus view; otherwise nothing is
added, ``serialized_for`` is plain ``with_user_serialized``, and every update
routes byte-identically to before.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update
from telegram.ext import CallbackContext

from src.nadobro.core.async_utils import fire_and_forget, run_blocking_db
from src.nadobro.core.feature_flags import arcus_enabled
from src.nadobro.handlers.keyboards import REPLY_BUTTON_MAP
from src.nadobro.handlers.update_serialization import with_user_serialized
from src.nadobro.handlers.venue_handler import (
    TEXT_ARCUS_BUTTON_ON_NADO,
    TEXT_ARCUS_FREE_TEXT_HINT,
    TEXT_DENIED_ON_ARCUS,
    VENUE_CALLBACK_PATTERN,
    answer_query,
    arcus_free_text_kb,
    cmd_venue,
    handle_venue_callback,
    read_active_venue,
    render_arcus_target,
    tr,
    venue_callback_ack,
)
from src.nadobro.i18n import get_active_language, localize_markup, resolve_reply_button_text
from src.nadobro.users.user_service import get_or_create_user
from src.nadobro.users.venue_service import count_users_on_venue, last_known_venue
from src.nadobro.utils.venue_capabilities import (
    ARCUS_ONLY,
    COMMANDS,
    DISPATCH,
    NEUTRAL,
    NEVER_GATE,
    classify_callback,
    classify_command,
)
from src.nadobro.utils.venue_scope import VENUE_ARCUS, VENUE_NADO

logger = logging.getLogger(__name__)

# Between the language middleware (-2) and every group-0 handler.
VENUE_GATE_GROUP = -1

_ALWAYS_PASS = frozenset({NEVER_GATE, NEUTRAL})

Handler = Callable[[Update, CallbackContext], Awaitable[Any]]


class _Probe:
    """What the gate learned before a crash: the venue (for fail-closed) and a
    log-safe tag (a callback PREFIX or a command name — never message text)."""

    __slots__ = ("venue", "tag")

    def __init__(self) -> None:
        self.venue: str | None = None
        self.tag = "?"


def command_name(message: Any) -> str | None:
    """PTB's own command test (CommandHandler / ``filters.COMMAND``): the FIRST
    entity is a bot_command at offset 0. Returns the lowercased name without
    the slash or ``@botname``; None when the message is not a command."""
    text = getattr(message, "text", None)
    entities = getattr(message, "entities", None) or ()
    if not text or not entities:
        return None
    first = entities[0]
    if getattr(first, "type", None) != "bot_command" or getattr(first, "offset", None) != 0:
        return None
    length = int(getattr(first, "length", 0) or 0)
    return text[1:length].split("@", 1)[0].lower()


async def _venue_or_last_known(telegram_id: int, probe: _Probe | None = None) -> str:
    try:
        venue = await read_active_venue(telegram_id)
    except Exception as exc:  # policy: degrade-ok(unreadable = last venue seen; never seen = Nado view; module docstring)
        venue = last_known_venue(telegram_id)
        logger.warning(
            "venue gate: venue unreadable uid=%s (%s); treating as %s (last known)",
            telegram_id, type(exc).__name__, venue,
        )
    if probe is not None:
        probe.venue = venue
    return venue


async def _touch_user(update: Update) -> None:
    """The last_active / username refresh handle_message and /start would have
    done — the Arcus view answers these updates itself. Fail-soft."""
    user = update.effective_user
    try:
        await run_blocking_db(get_or_create_user, int(user.id), getattr(user, "username", None))
    except Exception as exc:  # policy: degrade-ok(last_active bookkeeping only)
        logger.debug("venue gate: last_active refresh failed uid=%s (%s)", user.id, type(exc).__name__)


def _render_callback(target: str) -> Handler:
    async def _render(update: Update, context: CallbackContext) -> None:
        uid = int(update.effective_user.id)
        # Re-checked under the per-user lock: a tap queued behind a switch back
        # to Nado renders nothing instead of an Arcus screen.
        if await _venue_or_last_known(uid) != VENUE_ARCUS:
            return
        await render_arcus_target(target, uid, query=update.callback_query)

    return _render


def _render_reply(target: str) -> Handler:
    async def _render(update: Update, context: CallbackContext) -> None:
        uid = int(update.effective_user.id)
        await _touch_user(update)
        if await _venue_or_last_known(uid) != VENUE_ARCUS:
            return
        await render_arcus_target(target, uid, message=update.message)

    return _render


async def _arcus_free_text(update: Update, context: CallbackContext) -> None:
    """Free text on the Arcus view. A reply-keyboard label for a view renders
    that view's Arcus screen; anything else gets the hint. The text itself is
    never logged and never forwarded anywhere."""
    uid = int(update.effective_user.id)
    message = update.message
    await _touch_user(update)
    if await _venue_or_last_known(uid) != VENUE_ARCUS:
        return
    resolved = resolve_reply_button_text((message.text or "").strip(), prefer=REPLY_BUTTON_MAP.__contains__)
    button_target = REPLY_BUTTON_MAP.get(resolved)
    if button_target is not None:
        cls, target = classify_callback(button_target)
        if cls == DISPATCH and target:
            await render_arcus_target(target, uid, message=message)
            return
    await message.reply_text(
        tr(TEXT_ARCUS_FREE_TEXT_HINT),
        reply_markup=localize_markup(arcus_free_text_kb(), get_active_language()),
    )


async def _serialized(handler: Handler, update: Update, context: CallbackContext, locked: bool) -> None:
    if locked:
        # venue_recheck already holds this user's lock (asyncio.Lock is not
        # re-entrant: taking it again would wait out the timeout and drop).
        await handler(update, context)
    else:
        await with_user_serialized(handler)(update, context)


async def _gate(
    update: Update, context: CallbackContext, uid: int, probe: _Probe, *, locked: bool = False,
) -> None:
    """Pass (return) or stop (ApplicationHandlerStop) one update. ``locked``: run
    by ``venue_recheck`` with the per-user lock already held, after
    ``with_callback_ack`` has acked the tap."""
    from telegram.ext import ApplicationHandlerStop

    query = getattr(update, "callback_query", None)
    if query is not None:
        data = str(getattr(query, "data", None) or "")
        probe.tag = "cb:" + data.split(":", 1)[0][:24]
        cls, target = classify_callback(data)
        if cls in _ALWAYS_PASS:
            return
        venue = await _venue_or_last_known(uid, probe)
        if venue != VENUE_ARCUS:
            if cls == ARCUS_ONLY:
                await answer_query(query, tr(TEXT_ARCUS_BUTTON_ON_NADO), show_alert=True)
                raise ApplicationHandlerStop()
            return
        if cls == ARCUS_ONLY:
            return
        if cls == DISPATCH and target:
            # A bare ack (a "Loading portfolio…" toast would lie here), then the
            # Arcus screen in place of the Nado one.
            if not locked:
                fire_and_forget(answer_query(query), name="venue-gate-ack")
            await _serialized(_render_callback(target), update, context, locked)
            raise ApplicationHandlerStop()
        logger.info("venue gate: denied on arcus uid=%s %s%s", uid, probe.tag, " (re-check)" if locked else "")
        # Under the lock the tap was already acked, so this alert usually cannot
        # show — the queued Nado action is still dropped.
        await answer_query(query, tr(TEXT_DENIED_ON_ARCUS), show_alert=True)
        raise ApplicationHandlerStop()

    message = getattr(update, "message", None)
    if message is not None:
        name = command_name(message)
        if name is not None:
            # Only a REGISTERED command name is logged: an arbitrary "/..." token
            # could be something the user pasted.
            probe.tag = "cmd:" + (name if name in COMMANDS else "?")
            cls, target = classify_command(name)
            if cls in _ALWAYS_PASS:
                return
            if await _venue_or_last_known(uid, probe) != VENUE_ARCUS:
                return
            if cls == DISPATCH and target:
                await _serialized(_render_reply(target), update, context, locked)
                raise ApplicationHandlerStop()
            logger.info("venue gate: denied on arcus uid=%s %s%s", uid, probe.tag, " (re-check)" if locked else "")
            await message.reply_text(tr(TEXT_DENIED_ON_ARCUS))
            raise ApplicationHandlerStop()
        probe.tag = "text" if getattr(message, "text", None) else "message"
        if await _venue_or_last_known(uid, probe) != VENUE_ARCUS:
            return
        if getattr(message, "text", None):
            await _serialized(_arcus_free_text, update, context, locked)
        raise ApplicationHandlerStop()

    # Edited messages — and any other update type, should allowed_updates ever
    # widen: Nado passes, Arcus stops silently (fail-closed).
    probe.tag = "other"
    if await _venue_or_last_known(uid, probe) == VENUE_ARCUS:
        raise ApplicationHandlerStop()


async def venue_gate(update: Update, context: CallbackContext) -> None:
    """The group -1 TypeHandler callback. Raises ApplicationHandlerStop to stop
    an update; returns normally to let it through."""
    from telegram.ext import ApplicationHandlerStop

    user = getattr(update, "effective_user", None)
    if user is None:
        return
    uid = int(user.id)
    probe = _Probe()
    try:
        await _gate(update, context, uid, probe)
    except ApplicationHandlerStop:
        raise
    except Exception as exc:
        logger.warning(
            "venue gate error uid=%s %s venue=%s (%s)", uid, probe.tag, probe.venue, type(exc).__name__,
        )
        if probe.venue == VENUE_ARCUS:
            raise ApplicationHandlerStop() from None


def venue_recheck(handler: Handler) -> Handler:
    """The gate again, INSIDE the per-user lock, right before a Nado group-0
    handler. The group -1 gate decides at ARRIVAL; an update it passed can then
    wait for the lock behind a venue switch (up to the lock timeout), and must
    not run a Nado handler for a user who is now on the Arcus view. Same rules
    and replies as the gate; a stop here means ``handler`` never runs. Wrap it
    INSIDE ``with_user_serialized`` — ``serialized_for`` does."""
    from telegram.ext import ApplicationHandlerStop

    async def _wrapped(update: Update, context: CallbackContext) -> Any:
        user = getattr(update, "effective_user", None)
        if user is None:
            return await handler(update, context)
        uid = int(user.id)
        probe = _Probe()
        try:
            await _gate(update, context, uid, probe, locked=True)
        except ApplicationHandlerStop:
            return None
        except Exception as exc:
            logger.warning(
                "venue re-check error uid=%s %s venue=%s (%s)", uid, probe.tag, probe.venue, type(exc).__name__,
            )
            if probe.venue == VENUE_ARCUS:
                return None
        return await handler(update, context)

    return _wrapped


def serialized_for(enabled: bool) -> Callable[[Handler], Handler]:
    """How main.setup_bot wraps every Nado group-0 handler. Gate registered:
    ``with_user_serialized(venue_recheck(handler))``. Gate NOT registered (flag
    off, nobody on Arcus): exactly ``with_user_serialized`` — the very same
    function, so production routing stays byte-identical."""
    if not enabled:
        return with_user_serialized

    def _serialize(handler: Handler) -> Handler:
        return with_user_serialized(venue_recheck(handler))

    return _serialize


def should_register_venue_gate() -> bool:
    """Boot-time: register the gate + /venue when ARCUS_ENABLED is on (no DB
    touched), or when at least one user is already on the Arcus view (so a
    cohort user is never stranded after the flag goes off).

    If that count cannot be read the gate is NOT registered (WARNING logged):
    production stays byte-identical, and nobody is stranded — without the gate
    nothing reads ``active_venue``, so an Arcus-view user simply sees Nado.
    Blocking — call through run_blocking_db."""
    if arcus_enabled():
        logger.info("venue gate: enabled (ARCUS_ENABLED on)")
        return True
    try:
        arcus_users = count_users_on_venue(VENUE_ARCUS)
    except Exception as exc:
        logger.warning(
            "venue gate: could not count Arcus-view users (%s); gate NOT registered — every user sees Nado",
            type(exc).__name__,
        )
        return False
    if arcus_users > 0:
        logger.info("venue gate: enabled (%d user(s) on the Arcus view, ARCUS_ENABLED off)", arcus_users)
        return True
    logger.info("venue gate: not registered (ARCUS_ENABLED off, no Arcus-view users)")
    return False


def register_venue_handlers(app: Any, *, enabled: bool) -> bool:
    """Add the gate (group -1), ``/venue`` and the ``venue:``/``ax:`` callbacks
    (group 0) — or nothing at all when ``enabled`` is False. main.setup_bot calls
    this immediately BEFORE the catch-all CallbackQueryHandler: PTB runs only
    the first matching handler in a group."""
    if not enabled:
        return False
    from telegram.ext import CallbackQueryHandler, CommandHandler, TypeHandler

    app.add_handler(TypeHandler(Update, venue_gate), group=VENUE_GATE_GROUP)
    app.add_handler(CommandHandler("venue", with_user_serialized(cmd_venue)))
    app.add_handler(
        CallbackQueryHandler(
            venue_callback_ack(with_user_serialized(handle_venue_callback)),
            pattern=VENUE_CALLBACK_PATTERN,
        )
    )
    logger.info("venue gate registered (group %d) with /venue and venue:/ax: callbacks", VENUE_GATE_GROUP)
    return True
