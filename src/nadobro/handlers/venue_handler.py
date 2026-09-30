"""/venue view switch + the Arcus placeholder screens (Arcus Phase 1).

Nado and Arcus run IN PARALLEL. ``users.active_venue`` only picks which venue's
screens this Telegram UI shows. Switching NEVER stops, flattens, cancels, starts
or resumes anything on either venue: Nado strategies, copy mirrors, desk plans,
stop-loss rules, alerts and the managed agent keep running exactly as before
(tests/handlers/test_venue_switch.py makes every stop/start function raise).
Nado testnet <-> mainnet is a different thing ("stop, then switch") and lives in
``strategy/network_switch.py``.

Phase 1 is plumbing only: no Arcus client, no Arcus trading. The Arcus view is a
placeholder home plus a banner listing what is still live on Nado — read from
Postgres only, never a Nado client, because it sits on the tap path. While that
banner shows, the home also carries the Nado stop entries /stop_all and
/agent_off do not cover (desk plans, positions, open orders); every one is
NEVER_GATE, so it works from the Arcus view.

A switch drops the pending Nado conversational flows (in-memory AND their
persisted ``bot_state`` twins — a text-trade preview reloads from Postgres, so an
in-memory-only clear would let a "yes" typed after switching back execute a
preview built before the switch). It never touches anything live.

Wiring: ``handlers/venue_gate.register_venue_handlers`` adds ``/venue`` and the
``venue:`` / ``ax:`` callbacks only when ARCUS_ENABLED is on or a user is already
on the Arcus view. Keyboards and renderers live HERE, not in keyboards.py /
formatters.py, whose snapshot tests pin every public builder: the Nado surface
stays byte-identical. Cards are HTML; every dynamic value goes through ``esc``.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackContext

from src.nadobro.core.async_utils import fire_and_forget, run_blocking_db
from src.nadobro.core.feature_flags import arcus_enabled_for
from src.nadobro.handlers import ui
from src.nadobro.handlers.render_utils import plain_text_fallback
from src.nadobro.handlers.state_reset import clear_pending_user_state
from src.nadobro.handlers.trade_card import TRADE_CARD_SESSION_KEY
from src.nadobro.i18n import get_active_language, localize_label, localize_markup, localize_text
from src.nadobro.strategy.strategy_pending_input import clear_strategy_pending_input
from src.nadobro.trading.text_trade_pending import (
    clear_text_close_all_pending,
    clear_text_trade_pending,
)
from src.nadobro.users.onboarding_service import is_new_onboarding_complete
from src.nadobro.users.user_service import get_user
from src.nadobro.users.venue_service import (
    get_active_venue,
    get_active_venue_fresh,
    peek_active_venue,
    set_active_venue,
)
from src.nadobro.users.wallet_pending_flow import clear_wallet_pending_flow
from src.nadobro.utils.venue_capabilities import AX_HELP, AX_HOME, AX_SETTINGS, AX_UNAVAILABLE
from src.nadobro.utils.venue_scope import ARCUS_NETWORK_TESTNET, NADO_NETWORKS, VENUE_ARCUS, VENUE_NADO
from src.nadobro.utils.visual import esc

logger = logging.getLogger(__name__)

# --- callback data -----------------------------------------------------------
CB_VENUE_VIEW = "venue:view"
CB_SET_NADO = "venue:set:nado"
CB_SET_ARCUS = "venue:set:arcus"
VENUE_CALLBACK_PATTERN = r"^(?:venue|ax):"
# ax:* screens reachable by callback (ax:unavailable is render-only).
AX_CALLBACK_SCREENS = frozenset({AX_HOME, AX_HELP, AX_SETTINGS})
# Nado stop entries on the Arcus home — all NEVER_GATE (they pass the gate on the
# Arcus view). They cover what /stop_all (strategies + copy trades) and
# /agent_off do not: the desk list (its only actions are Stop and Refresh) and
# the close-all / cancel-all CONFIRM screens (nothing happens without a Yes).
CB_NADO_DESK = "desk:view"
CB_NADO_CLOSE_ALL = "portfolio:close_all_confirm"
CB_NADO_CANCEL_ALL = "portfolio:cancel_all_confirm"

# --- i18n keys (English source; every one has zh/fr/ar/ru/ko in i18n.py) ------
# Callback alerts / toasts and plain-text replies (each <= 200 chars, every language).
TEXT_DENIED_ON_ARCUS = "Not available on Arcus yet — use /venue to switch back to Nado."
TEXT_ARCUS_BUTTON_ON_NADO = "That's an Arcus button — use /venue to switch to Arcus."
TEXT_ARCUS_FREE_TEXT_HINT = (
    "You're viewing Arcus (beta). Chat, trading and Nado tools are off in this view — "
    "use /venue to switch back to Nado."
)
TEXT_ARCUS_NOT_ALLOWED = "Arcus isn't available for your account yet."
TEXT_FINISH_SETUP_FIRST = "Finish setup first (language + terms), then switch."
TEXT_SWITCH_FAILED = "Couldn't switch right now — nothing changed. Try again in a moment."
TEXT_SWITCHED_TO_ARCUS = "Switched to Arcus (beta)"
TEXT_SWITCHED_TO_NADO = "Switched to Nado"
# Venue card.
TEXT_VENUE_TITLE = "🔁 <b>Trading venue</b>"
TEXT_VENUE_VIEWING = "Viewing: <b>{venue}</b>"
TEXT_VENUE_PARALLEL = (
    "Nado and Arcus run side by side. Switching only changes what you see here — "
    "it never stops, closes or starts anything."
)
TEXT_VENUE_KEEPS_RUNNING = (
    "Your Nado strategies and copy trades keep running while you view Arcus. /stop_all stops them."
)
# Arcus home.
TEXT_HOME_TITLE = "🟣 <b>ARCUS · beta</b> · {network}"
TEXT_HOME_LINK_SOON = "Link your Arcus account — coming soon."
TEXT_HOME_NADO_RUNNING = "⚠️ <b>Nado is still running:</b> {items}"
TEXT_HOME_NADO_KEEPS_RUNNING = (
    "It keeps running while you view Arcus. /stop_all stops Nado strategies and copy trades; "
    "/venue switches back to manage the rest."
)
TEXT_HOME_NADO_UNREADABLE = (
    "⚠️ Couldn't check your Nado automation right now. /stop_all stops Nado strategies and copy trades."
)
TEXT_ITEM_STRATEGY = "{strategy} on {network}"
TEXT_ITEM_COPY = "Copy trades: {n}"
TEXT_ITEM_DESK = "Desk plans: {n}"
TEXT_ITEM_STOP_LOSS = "Stop-loss rules: {n}"
TEXT_ITEM_MANAGED_AI = "Managed AI on"
TEXT_ITEM_CLEANUP = "Order cleanup pending on {network}"
# "Not on Arcus yet" card.
TEXT_UNAVAILABLE_TITLE = "🚧 <b>Not on Arcus yet</b>"
TEXT_UNAVAILABLE_BODY = (
    "This screen is Nado-only for now. Use /venue to switch back — switching never stops anything."
)
# Arcus help.
TEXT_HELP_TITLE = "❓ <b>Arcus (beta)</b>"
TEXT_HELP_BETA = "• Arcus is in closed beta. Linking your Arcus account is coming soon."
TEXT_HELP_VENUE = "• /venue switches between Nado and Arcus. Switching never stops, closes or starts anything."
TEXT_HELP_AUTOMATION = (
    "• Your Nado automation keeps running in the background. /stop_all stops Nado strategies and copy trades."
)
# Arcus settings.
TEXT_SETTINGS_TITLE = "⚙️ <b>Settings · Arcus (beta)</b>"
TEXT_SETTINGS_BODY = "Language applies to both venues. Nado trading settings live on the Nado view."

# Button labels (new) + reused house labels.
LABEL_VENUE = "🔁 Venue"
LABEL_HELP = "❓ Help"
LABEL_NADO = "Nado"
LABEL_ARCUS = "Arcus (beta)"
LABEL_LANGUAGE = "🌐 Language"
LABEL_NADO_CLOSE = "❌ Close Nado positions"
LABEL_NADO_CANCEL = "🗑 Cancel Nado orders"
LABEL_NADO_DESK = "🧾 Nado desk plans"

CALLBACK_ANSWER_TEXT_KEYS = (
    TEXT_DENIED_ON_ARCUS, TEXT_ARCUS_BUTTON_ON_NADO, TEXT_ARCUS_NOT_ALLOWED,
    TEXT_FINISH_SETUP_FIRST, TEXT_SWITCH_FAILED, TEXT_SWITCHED_TO_ARCUS, TEXT_SWITCHED_TO_NADO,
)
I18N_TEXT_KEYS = CALLBACK_ANSWER_TEXT_KEYS + (
    TEXT_ARCUS_FREE_TEXT_HINT,
    TEXT_VENUE_TITLE, TEXT_VENUE_VIEWING, TEXT_VENUE_PARALLEL, TEXT_VENUE_KEEPS_RUNNING,
    TEXT_HOME_TITLE, TEXT_HOME_LINK_SOON, TEXT_HOME_NADO_RUNNING, TEXT_HOME_NADO_KEEPS_RUNNING,
    TEXT_HOME_NADO_UNREADABLE,
    TEXT_ITEM_STRATEGY, TEXT_ITEM_COPY, TEXT_ITEM_DESK, TEXT_ITEM_STOP_LOSS, TEXT_ITEM_MANAGED_AI,
    TEXT_ITEM_CLEANUP,
    TEXT_UNAVAILABLE_TITLE, TEXT_UNAVAILABLE_BODY,
    TEXT_HELP_TITLE, TEXT_HELP_BETA, TEXT_HELP_VENUE, TEXT_HELP_AUTOMATION,
    TEXT_SETTINGS_TITLE, TEXT_SETTINGS_BODY,
)
I18N_LABEL_KEYS = (
    LABEL_VENUE, LABEL_HELP, LABEL_NADO, LABEL_ARCUS, LABEL_NADO_CLOSE, LABEL_NADO_CANCEL, LABEL_NADO_DESK,
)


# --- small helpers ------------------------------------------------------------

def tr(key: str, **fmt: str) -> str:
    """Translate ONE piece, then fill its placeholders (values already escaped).

    A translation whose template breaks falls back to the English source, as
    callbacks._edit_loc does."""
    lang = get_active_language()
    text = localize_text(key, lang)
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError, ValueError):
        logger.warning("venue i18n template error key=%r lang=%s", key[:40], lang)
        return key.format(**fmt)


def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


def venue_card_kb(current_venue: str) -> InlineKeyboardMarkup:
    """[Nado] [Arcus (beta)] with a ✅ on the current one, then Home. ``nav:main``
    is the Nado home for a Nado-view user and becomes ax:home at the gate for an
    Arcus-view user, so one button serves both."""
    on_arcus = current_venue == VENUE_ARCUS
    return _markup([
        [
            (LABEL_NADO + ("" if on_arcus else " ✅"), CB_SET_NADO),
            (LABEL_ARCUS + (" ✅" if on_arcus else ""), CB_SET_ARCUS),
        ],
        [(ui.NAV_HOME, "nav:main")],
    ])


def arcus_home_kb(
    items: list[tuple[str, dict[str, str]]] | None = None, failed: bool = False,
) -> InlineKeyboardMarkup:
    """[Venue] [Help]; while the banner shows (something live on Nado, or it
    could not be checked) also the Nado stop entries — the desk list only when
    desk plans were counted or the check failed."""
    rows = [[(LABEL_VENUE, CB_VENUE_VIEW), (LABEL_HELP, AX_HELP)]]
    if items or failed:
        rows.append([(LABEL_NADO_CLOSE, CB_NADO_CLOSE_ALL), (LABEL_NADO_CANCEL, CB_NADO_CANCEL_ALL)])
        if failed or any(key == TEXT_ITEM_DESK for key, _ in (items or ())):
            rows.append([(LABEL_NADO_DESK, CB_NADO_DESK)])
    return _markup(rows)


def arcus_help_kb() -> InlineKeyboardMarkup:
    return _markup([[(ui.NAV_HOME, AX_HOME), (LABEL_VENUE, CB_VENUE_VIEW)]])


def arcus_settings_kb() -> InlineKeyboardMarkup:
    return _markup([
        [(LABEL_LANGUAGE, "settings:language_menu")],
        [(LABEL_VENUE, CB_VENUE_VIEW), (ui.NAV_HOME, AX_HOME)],
    ])


def arcus_unavailable_kb() -> InlineKeyboardMarkup:
    return _markup([[(ui.NAV_HOME, AX_HOME), (LABEL_VENUE, CB_VENUE_VIEW)]])


def arcus_free_text_kb() -> InlineKeyboardMarkup:
    return _markup([[(LABEL_VENUE, CB_VENUE_VIEW)]])


def _chat_id(query: Any, telegram_id: int) -> int:
    return int(getattr(getattr(query, "message", None), "chat_id", None) or telegram_id)


def _bump_seq(query: Any, telegram_id: int) -> None:
    # Every in-place edit made outside handle_callback must advance the chat's
    # interaction sequence, or a Nado portfolio refresh still running in the
    # background (portfolio_handler) could overwrite this screen.
    from src.nadobro.handlers.callbacks import bump_interaction_seq

    bump_interaction_seq(_chat_id(query, telegram_id))


async def answer_query(query: Any, text: str | None = None, *, show_alert: bool = False) -> None:
    """Answer a callback query; a stale / already-answered query is not an error."""
    try:
        if text:
            await query.answer(text=text, show_alert=show_alert)
        else:
            await query.answer()
    except TelegramError as exc:  # BadRequest included
        logger.debug("venue callback answer failed: %s", exc)


async def read_active_venue(telegram_id: int) -> str:
    """The user's venue: the in-process cache first (no IO), else Postgres OFF
    the event loop. Raises on a DB error — the caller picks the policy."""
    venue = peek_active_venue(telegram_id)
    if venue is not None:
        return venue
    return await run_blocking_db(get_active_venue, telegram_id)


async def edit_html(query: Any, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit the tapped message in place (HTML). A media message cannot take a
    text edit, so that case sends a new message instead, as _edit_loc does."""
    markup = localize_markup(kb, get_active_language())
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as exc:
        low = str(exc).lower()
        if "message is not modified" in low:
            return
        if (
            "no text in the message to edit" in low
            or "message can't be edited" in low
            or "message to edit not found" in low
        ):
            message = getattr(query, "message", None)
            if message is not None:
                await reply_html(message, text, kb)
            return
        if "can't parse entities" in low:
            await query.edit_message_text(plain_text_fallback(text), reply_markup=markup)
            return
        raise


async def reply_html(message: Any, text: str, kb: InlineKeyboardMarkup) -> None:
    markup = localize_markup(kb, get_active_language())
    try:
        await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as exc:
        if "can't parse entities" not in str(exc).lower():
            raise
        await message.reply_text(plain_text_fallback(text), reply_markup=markup)


# --- Arcus screens --------------------------------------------------------------

def _strategy_label(state: dict) -> str:
    strategy = str(state.get("strategy") or "").strip().upper() or "STRATEGY"
    product = str(state.get("product") or "").strip().upper()
    return f"{strategy} {product}".strip()


def nado_automation_snapshot(telegram_id: int) -> tuple[str, list[tuple[str, dict[str, str]]], bool]:
    """``(arcus_network, banner_items, any_read_failed)`` for the Arcus home.

    Postgres reads ONLY — the same sources the fail-closed network switch reads
    (strategy/network_switch.py) — and never a Nado client: the Arcus home is a
    tap path. A source that cannot be read sets ``any_read_failed`` so the banner
    says it could not check, never "nothing running" (DENIED != EMPTY). Read-only:
    nothing here stops, starts or changes anything. Blocking — call through
    ``run_blocking_db``.
    """
    from src.nadobro.llm import managed_agent_state
    from src.nadobro.models.database import get_user_active_mirrors_v2
    from src.nadobro.strategy import bot_runtime, pending_cleanup
    from src.nadobro.trading import desk_store, stop_loss_service

    uid = int(telegram_id)
    failed = False
    arcus_network = ARCUS_NETWORK_TESTNET
    try:
        user = get_user(uid)
        arcus_network = str(getattr(user, "arcus_network_mode", None) or ARCUS_NETWORK_TESTNET)
    except Exception:  # policy: degrade-ok(header falls back to the default Arcus network; banner says it could not check)
        failed = True

    items: list[tuple[str, dict[str, str]]] = []
    desk_plans = 0
    stop_loss_rules = 0
    for net in NADO_NETWORKS:
        label = net.upper()
        try:
            state = bot_runtime.get_user_bot_state(uid, net)
            if state.get("running"):
                items.append((TEXT_ITEM_STRATEGY, {"strategy": _strategy_label(state), "network": label}))
        except Exception:  # policy: degrade-ok(an unreadable run is reported as "couldn't check", never "not running")
            failed = True
        try:
            if pending_cleanup.list_entries(uid, net):
                items.append((TEXT_ITEM_CLEANUP, {"network": label}))
        except Exception:  # policy: degrade-ok(reported as "couldn't check")
            failed = True
        try:
            desk_plans += len(desk_store.list_active_plans(uid, net) or [])
        except Exception:  # policy: degrade-ok(reported as "couldn't check")
            failed = True
        try:
            stop_loss_rules += len(stop_loss_service.list_active_stop_loss_rules(uid, net) or [])
        except Exception:  # policy: degrade-ok(reported as "couldn't check")
            failed = True
    try:
        copies = len(get_user_active_mirrors_v2(uid) or [])  # every network, paused included
        if copies:
            items.append((TEXT_ITEM_COPY, {"n": str(copies)}))
    except Exception:  # policy: degrade-ok(reported as "couldn't check")
        failed = True
    if desk_plans:
        items.append((TEXT_ITEM_DESK, {"n": str(desk_plans)}))
    if stop_loss_rules:
        items.append((TEXT_ITEM_STOP_LOSS, {"n": str(stop_loss_rules)}))
    try:
        if (
            managed_agent_state.is_managed_agent_globally_enabled()
            and managed_agent_state.get_managed_agent_state(uid).get("enabled")
        ):
            items.append((TEXT_ITEM_MANAGED_AI, {}))
    except Exception:  # policy: degrade-ok(reported as "couldn't check")
        failed = True
    return arcus_network, items, failed


def arcus_home_text(arcus_network: str, items: list[tuple[str, dict[str, str]]], failed: bool) -> str:
    lines = [tr(TEXT_HOME_TITLE, network=esc(str(arcus_network).upper())), "", tr(TEXT_HOME_LINK_SOON)]
    if items:
        rendered = ", ".join(
            tr(key, **{name: esc(value) for name, value in fmt.items()}) for key, fmt in items
        )
        lines += ["", tr(TEXT_HOME_NADO_RUNNING, items=rendered), tr(TEXT_HOME_NADO_KEEPS_RUNNING)]
    if failed:
        lines += ["", tr(TEXT_HOME_NADO_UNREADABLE)]
    return "\n".join(lines)


def arcus_help_text() -> str:
    return "\n".join(
        [tr(TEXT_HELP_TITLE), "", tr(TEXT_HELP_BETA), tr(TEXT_HELP_VENUE), tr(TEXT_HELP_AUTOMATION)]
    )


def arcus_settings_text() -> str:
    return "\n\n".join([tr(TEXT_SETTINGS_TITLE), tr(TEXT_SETTINGS_BODY)])


def arcus_unavailable_text() -> str:
    return "\n\n".join([tr(TEXT_UNAVAILABLE_TITLE), tr(TEXT_UNAVAILABLE_BODY)])


async def build_arcus_screen(target: str, telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Text + keyboard for an Arcus render target. Unknown targets get the
    "Not on Arcus yet" card (never a Nado screen)."""
    if target == AX_HOME:
        try:
            arcus_network, items, failed = await run_blocking_db(nado_automation_snapshot, telegram_id)
        except Exception:  # policy: degrade-ok(banner says it could not check — never "nothing running")
            logger.warning("arcus home: automation snapshot failed uid=%s", telegram_id)
            arcus_network, items, failed = ARCUS_NETWORK_TESTNET, [], True
        return arcus_home_text(arcus_network, items, failed), arcus_home_kb(items, failed)
    if target == AX_HELP:
        return arcus_help_text(), arcus_help_kb()
    if target == AX_SETTINGS:
        return arcus_settings_text(), arcus_settings_kb()
    return arcus_unavailable_text(), arcus_unavailable_kb()


async def render_arcus_target(target: str, telegram_id: int, *, query: Any = None, message: Any = None) -> None:
    """Render an Arcus screen: edit the tapped message in place, or reply to a
    command / text message. The caller has checked the user is on Arcus."""
    if query is not None:
        _bump_seq(query, telegram_id)
    text, kb = await build_arcus_screen(target, telegram_id)
    if query is not None:
        await edit_html(query, text, kb)
    elif message is not None:
        await reply_html(message, text, kb)


# --- the venue card ---------------------------------------------------------------

def venue_snapshot(telegram_id: int) -> tuple[str, str | None, str]:
    """``(active_venue, nado_network_or_None, arcus_network)``. Blocking (cached
    get_user; Postgres on a miss) — call through run_blocking_db."""
    uid = int(telegram_id)
    venue = get_active_venue(uid)
    user = get_user(uid)
    network_mode = getattr(user, "network_mode", None) if user is not None else None
    nado_network = str(getattr(network_mode, "value", network_mode)) if network_mode is not None else None
    arcus_network = str(getattr(user, "arcus_network_mode", None) or ARCUS_NETWORK_TESTNET)
    return venue, nado_network, arcus_network


def venue_card_text(venue: str, nado_network: str | None, arcus_network: str) -> str:
    lang = get_active_language()
    if venue == VENUE_ARCUS:
        name = f"{localize_label(LABEL_ARCUS, lang)} · {arcus_network.upper()}"
    else:
        name = localize_label(LABEL_NADO, lang)
        if nado_network:
            name = f"{name} · {nado_network.upper()}"
    return "\n".join([
        tr(TEXT_VENUE_TITLE),
        "",
        tr(TEXT_VENUE_VIEWING, venue=esc(name)),
        "",
        tr(TEXT_VENUE_PARALLEL),
        tr(TEXT_VENUE_KEEPS_RUNNING),
    ])


async def cmd_venue(update: Update, context: CallbackContext) -> None:
    """/venue — the venue card.

    Silent for a Nado-view user outside the Arcus cohort (flag off or not on the
    allowlist) — today's behaviour for an unknown command. A user on the Arcus
    view ALWAYS gets the card, so nobody can be stranded there.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    uid = int(user.id)
    try:
        venue, nado_network, arcus_network = await run_blocking_db(venue_snapshot, uid)
    except Exception as exc:  # policy: degrade-ok(no card; the gate treats an unreadable venue as Nado)
        logger.warning("/venue: venue unreadable uid=%s (%s)", uid, type(exc).__name__)
        return
    if venue != VENUE_ARCUS and not arcus_enabled_for(uid):
        return
    await reply_html(message, venue_card_text(venue, nado_network, arcus_network), venue_card_kb(venue))


# --- venue:* / ax:* callbacks ----------------------------------------------------

# venue:set:* answer their own query: a refusal is an alert, a switch a toast.
# Every other venue:/ax: callback is pre-acked like with_callback_ack does.
_SELF_ANSWERING_CALLBACKS = frozenset({CB_SET_ARCUS, CB_SET_NADO})
_awaiting_self_answer: set[int] = set()


async def _answer_self(query: Any, text: str | None = None, *, show_alert: bool = False) -> None:
    _awaiting_self_answer.discard(id(query))
    await answer_query(query, text, show_alert=show_alert)


def venue_callback_ack(
    handler: Callable[[Update, CallbackContext], Awaitable[Any]],
) -> Callable[[Update, CallbackContext], Awaitable[Any]]:
    """Pre-ack venue:/ax: taps BEFORE the per-user lock (the spinner stops at
    once), except venue:set:*, which answers itself. If one of those never got
    answered — dropped behind a busy lock, or it raised — it gets a bare ack on
    the way out so the spinner cannot hang."""

    async def _wrapped(update: Update, context: CallbackContext) -> Any:
        query = update.callback_query
        if query is None:
            return await handler(update, context)
        if str(query.data or "") not in _SELF_ANSWERING_CALLBACKS:
            fire_and_forget(answer_query(query), name="venue-callback-ack")
            return await handler(update, context)
        key = id(query)
        _awaiting_self_answer.add(key)
        try:
            return await handler(update, context)
        finally:
            if key in _awaiting_self_answer:
                _awaiting_self_answer.discard(key)
                await answer_query(query)

    return _wrapped


def clear_persisted_nado_pending(telegram_id: int) -> None:
    """Delete the persisted twins of the pending Nado flows (strategy input, text
    trade / close-all previews, wallet linking) — exactly the four clearers the
    Nado Home path runs. Pending input only: nothing live is touched. Blocking."""
    uid = int(telegram_id)
    for clearer in (
        clear_strategy_pending_input,
        clear_text_trade_pending,
        clear_text_close_all_pending,
        clear_wallet_pending_flow,
    ):
        try:
            clearer(uid)
        except Exception:  # policy: degrade-ok(a stale preview still expires on its own TTL)
            logger.warning("venue switch: %s failed uid=%s", getattr(clearer, "__name__", "clearer"), uid)


async def clear_nado_pending_flows(context: CallbackContext | None, telegram_id: int) -> None:
    """Drop every pending Nado conversational flow on a venue switch (both ways):
    the in-memory keys (on the loop — no uid, so no DB there), the trade-card
    session (not in the state_reset list), then the persisted twins off the loop.
    UI selections (pair pickers, config sections) and in-flight guards
    (``vault_op_inflight``) are deliberately kept."""
    clear_pending_user_state(context)
    user_data = getattr(context, "user_data", None)
    if user_data is not None:
        user_data.pop(TRADE_CARD_SESSION_KEY, None)
    await run_blocking_db(clear_persisted_nado_pending, telegram_id)


async def handle_venue_callback(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    uid = int(user.id)
    data = str(query.data or "")
    if data == CB_VENUE_VIEW:
        await _venue_view(query, uid)
    elif data == CB_SET_ARCUS:
        await _switch_to_arcus(query, context, uid)
    elif data == CB_SET_NADO:
        await _switch_to_nado(query, context, uid)
    elif data in AX_CALLBACK_SCREENS:
        # Re-checked under the lock: a tap queued behind a switch back to Nado
        # renders nothing (the gate already denied ax:* for Nado-view users).
        try:
            venue = await read_active_venue(uid)
        except Exception as exc:  # policy: degrade-ok(nothing renders; the tap was acked)
            logger.warning("ax screen: venue unreadable uid=%s (%s)", uid, type(exc).__name__)
            return
        if venue == VENUE_ARCUS:
            await render_arcus_target(data, uid, query=query)
    # Any other venue:/ax: value was acked by venue_callback_ack; nothing renders.


async def _venue_view(query: Any, uid: int) -> None:
    try:
        venue, nado_network, arcus_network = await run_blocking_db(venue_snapshot, uid)
    except Exception as exc:  # policy: degrade-ok(no edit; the tap was acked)
        logger.warning("venue:view: venue unreadable uid=%s (%s)", uid, type(exc).__name__)
        return
    if venue != VENUE_ARCUS and not arcus_enabled_for(uid):
        return
    _bump_seq(query, uid)
    await edit_html(query, venue_card_text(venue, nado_network, arcus_network), venue_card_kb(venue))


async def _refuse(query: Any, key: str) -> None:
    await _answer_self(query, tr(key), show_alert=True)


async def _switch_to_arcus(query: Any, context: CallbackContext, uid: int) -> None:
    """Nado -> Arcus view. Order: read, eligibility, onboarding, compare-and-set,
    clear pending Nado flows, toast, render ax:home. A refusal or a DB error
    changes nothing and never renders the Arcus home (DENIED != EMPTY). The read
    is FRESH: a cached row may predate a switch."""
    try:
        current = await run_blocking_db(get_active_venue_fresh, uid)
    except Exception as exc:
        logger.warning("venue switch to arcus: venue unreadable uid=%s (%s)", uid, type(exc).__name__)
        await _refuse(query, TEXT_SWITCH_FAILED)
        return
    if current == VENUE_ARCUS:
        await _answer_self(query)
        await render_arcus_target(AX_HOME, uid, query=query)
        return
    if not arcus_enabled_for(uid):
        await _refuse(query, TEXT_ARCUS_NOT_ALLOWED)
        return
    try:
        onboarded = await run_blocking_db(is_new_onboarding_complete, uid)
    except Exception as exc:
        logger.warning("venue switch to arcus: onboarding unreadable uid=%s (%s)", uid, type(exc).__name__)
        await _refuse(query, TEXT_SWITCH_FAILED)
        return
    if not onboarded:
        await _refuse(query, TEXT_FINISH_SETUP_FIRST)
        return
    try:
        outcome = await run_blocking_db(set_active_venue, uid, VENUE_ARCUS)
    except Exception as exc:
        logger.warning("venue switch to arcus failed uid=%s (%s)", uid, type(exc).__name__)
        await _refuse(query, TEXT_SWITCH_FAILED)
        return
    if outcome == "not_allowed":
        await _refuse(query, TEXT_ARCUS_NOT_ALLOWED)
        return
    if outcome != "switched" and not await _venue_is(uid, VENUE_ARCUS):
        # CAS miss that did NOT leave the row on Arcus (no row, or unreadable).
        await _refuse(query, TEXT_SWITCH_FAILED)
        return
    await clear_nado_pending_flows(context, uid)
    await _answer_self(query, tr(TEXT_SWITCHED_TO_ARCUS))
    await render_arcus_target(AX_HOME, uid, query=query)


async def _switch_to_nado(query: Any, context: CallbackContext, uid: int) -> None:
    """Arcus -> Nado view. Never gated by the flag or the allowlist, so nobody
    can be stranded on Arcus. Renders exactly today's Nado home.

    The compare-and-set is ALWAYS attempted (a no-op on a row already on Nado):
    deciding off a read first could skip it on a stale 'nado' while the row
    still says Arcus. Only a flip clears the pending flows and toasts."""
    try:
        outcome = await run_blocking_db(set_active_venue, uid, VENUE_NADO)
    except Exception as exc:
        logger.warning("venue switch to nado failed uid=%s (%s)", uid, type(exc).__name__)
        await _refuse(query, TEXT_SWITCH_FAILED)
        return
    toast = None
    if outcome == "switched":
        await clear_nado_pending_flows(context, uid)
        toast = tr(TEXT_SWITCHED_TO_NADO)
    elif await _venue_is(uid, VENUE_ARCUS, unreadable=True):
        # CAS miss with the row still on Arcus (or unreadable): nothing changed.
        await _refuse(query, TEXT_SWITCH_FAILED)
        return
    await _answer_self(query, toast)
    from src.nadobro.handlers.callbacks import _show_dashboard

    _bump_seq(query, uid)
    await _show_dashboard(query, uid)


async def _venue_is(uid: int, venue: str, *, unreadable: bool = False) -> bool:
    """Fresh read after a CAS miss. An unreadable venue answers ``unreadable``."""
    try:
        return (await run_blocking_db(get_active_venue_fresh, uid)) == venue
    except Exception as exc:
        logger.warning("venue re-read failed uid=%s (%s)", uid, type(exc).__name__)
        return unreadable
