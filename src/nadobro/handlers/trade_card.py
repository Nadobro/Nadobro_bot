import logging
import time
import uuid
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackContext

from src.nadobro.config import DUAL_MODE_CARD_FLOW, get_product_id, get_product_max_leverage
from src.nadobro.i18n import localize_text, localize_markup, get_active_language
from src.nadobro.handlers.formatters import escape_md, fmt_trade_preview, fmt_trade_result
from src.nadobro.handlers.render_utils import plain_text_fallback


def _loc(text: str) -> str:
    return localize_text(text, get_active_language())


from src.nadobro.handlers.keyboards import (
    home_card_kb,
    trade_card_direction_kb,
    trade_card_order_type_kb,
    trade_card_product_kb,
    trade_card_leverage_kb,
    trade_card_size_kb,
    trade_card_limit_price_input_kb,
    trade_card_tpsl_kb,
    trade_card_tpsl_edit_kb,
    trade_card_text_input_kb,
    trade_card_confirm_kb,
)
from src.nadobro.handlers.home_card import build_home_card_text_async
from src.nadobro.users.admin_service import is_trading_paused
from src.nadobro.users.onboarding_service import get_resume_step, is_new_onboarding_complete
from src.nadobro.users.settings_service import get_user_settings
from src.nadobro.trading.trade_service import execute_market_order, execute_limit_order
from src.nadobro.users.user_service import ensure_active_wallet_ready, get_user_readonly_client, get_user

logger = logging.getLogger(__name__)

TRADE_CARD_SESSION_KEY = "trade_card_session"
TRADE_CARD_TTL_SECONDS = 900


def is_trade_card_mode_enabled() -> bool:
    return bool(DUAL_MODE_CARD_FLOW)


def _new_session_id() -> str:
    return uuid.uuid4().hex[:8]


def _clear_trade_card_session(context: CallbackContext) -> None:
    context.user_data.pop(TRADE_CARD_SESSION_KEY, None)


def _get_trade_card_session(context: CallbackContext, touch: bool = False) -> Optional[dict]:
    session = context.user_data.get(TRADE_CARD_SESSION_KEY)
    if not session:
        return None
    last_updated = float(session.get("last_updated") or 0)
    if not last_updated or time.time() - last_updated > TRADE_CARD_TTL_SECONDS:
        _clear_trade_card_session(context)
        return None
    if touch:
        session["last_updated"] = time.time()
        context.user_data[TRADE_CARD_SESSION_KEY] = session
    return session


def _set_trade_card_session(context: CallbackContext, session: dict) -> None:
    session["last_updated"] = time.time()
    context.user_data[TRADE_CARD_SESSION_KEY] = session


def _trade_step_summary(session: dict) -> str:
    direction = session.get("direction")
    order_type = session.get("order_type")
    product = session.get("product")
    leverage = session.get("leverage")
    size = session.get("size")
    limit_price = session.get("limit_price")
    tp = session.get("tp")
    sl = session.get("sl")

    lines = []
    if direction:
        lines.append(f"{_loc('Side')}: *{escape_md(direction.upper())}*")
    if order_type:
        lines.append(f"{_loc('Order')}: *{escape_md(order_type.upper())}*")
    if product:
        lines.append(f"{_loc('Product')}: *{escape_md(product)}\\-PERP*")
    if leverage:
        lines.append(f"{_loc('Leverage')}: *{escape_md(str(leverage))}x*")
    if size:
        lines.append(f"{_loc('Size')}: *{escape_md(str(size))}*")
    if limit_price:
        lines.append(f"{_loc('Limit')}: *{escape_md(str(limit_price))}*")
    if tp:
        lines.append(f"{_loc('TP')}: *{escape_md(str(tp))}*")
    if sl:
        lines.append(f"{_loc('SL')}: *{escape_md(str(sl))}*")
    return "\n".join(lines)


def _build_trade_card_text(session: dict) -> str:
    state = session.get("state", "direction")
    summary = _trade_step_summary(session)
    error = session.get("error")
    error_block = f"\n\n⚠️ {escape_md(_loc(error) if error else '')}" if error else ""
    header = _loc("📊 *Trade Card*")

    if state == "direction":
        return f"{header}\n\n{_loc('Select direction:')}" + error_block
    if state == "order_type":
        return f"{header}\n\n{summary}\n\n{_loc('Select order type:')}{error_block}"
    if state == "product":
        return f"{header}\n\n{summary}\n\n{_loc('Select product:')}{error_block}"
    if state == "leverage":
        return f"{header}\n\n{summary}\n\n{_loc('Select leverage:')}{error_block}"
    if state == "size":
        return f"{header}\n\n{summary}\n\n{_loc('Select size:')}{error_block}"
    if state == "size_custom_input":
        return (
            f"{header}\n\n{summary}\n\n"
            f"{_loc('Type your custom size in chat')} \\(e\\.g\\. `0\\.01`\\)\\."
            f"{error_block}"
        )
    if state == "limit_price":
        return (
            f"{header}\n\n{summary}\n\n"
            f"{_loc('Type your limit price in chat')} \\(e\\.g\\. `95000`\\)\\."
            f"{error_block}"
        )
    if state == "tpsl":
        return f"{header}\n\n{summary}\n\n{_loc('Set TP/SL or skip:')}{error_block}"
    if state == "tp_input":
        tp_prompt = _loc("Type take profit price in chat\\.")
        return (
            f"{header}\n\n{summary}\n\n"
            f"{tp_prompt}"
            f"{error_block}"
        )
    if state == "sl_input":
        sl_prompt = _loc("Type stop loss price in chat\\.")
        return (
            f"{header}\n\n{summary}\n\n"
            f"{sl_prompt}"
            f"{error_block}"
        )
    if state == "tpsl_edit":
        tp_val = session.get("tp")
        sl_val = session.get("sl")
        tp_str = f"{_loc('TP')}: {escape_md(str(tp_val))}" if tp_val else f"{_loc('TP')}: {_loc('not set')}"
        sl_str = f"{_loc('SL')}: {escape_md(str(sl_val))}" if sl_val else f"{_loc('SL')}: {_loc('not set')}"
        return (
            f"{header}\n\n"
            f"{summary}\n\n"
            f"📐 *{_loc('TP/SL Settings')}*\n{tp_str} \\| {sl_str}{error_block}"
        )
    return f"{header}\n\n{summary}{error_block}"


def _card_keyboard(session: dict):
    session_id = session["session_id"]
    state = session.get("state", "direction")
    network = session.get("network", "mainnet")
    if state == "direction":
        return trade_card_direction_kb(session_id)
    if state == "order_type":
        return trade_card_order_type_kb(session_id)
    if state == "product":
        return trade_card_product_kb(session_id, network=network)
    if state == "leverage":
        return trade_card_leverage_kb(session_id, session.get("product", "BTC"))
    if state == "size":
        return trade_card_size_kb(session_id, session.get("product", "BTC"))
    if state == "limit_price":
        return trade_card_limit_price_input_kb(session_id)
    if state == "tpsl":
        return trade_card_tpsl_kb(session_id)
    if state == "tpsl_edit":
        return trade_card_tpsl_edit_kb(session_id)
    if state in ("size_custom_input", "tp_input", "sl_input"):
        return trade_card_text_input_kb(session_id)
    return trade_card_confirm_kb(session_id)


def _back_state(current_state: str) -> str:
    back_map = {
        "order_type": "direction",
        "product": "order_type",
        "leverage": "product",
        "size": "leverage",
        "size_custom_input": "size",
        "limit_price": "size",
        "tpsl": "size",
        "tpsl_edit": "tpsl",
        "tp_input": "tpsl_edit",
        "sl_input": "tpsl_edit",
        "confirm": "tpsl",
    }
    return back_map.get(current_state, "direction")


async def _load_preview_fields(session: dict, telegram_id: int) -> None:
    product = session.get("product", "BTC")
    size = float(session.get("size", 0) or 0)
    leverage = int(session.get("leverage", 1) or 1)
    order_type = session.get("order_type", "market")
    price = 0.0
    try:
        if order_type == "limit":
            price = float(session.get("limit_price", 0) or 0)
        else:
            client = get_user_readonly_client(telegram_id)
            if client:
                user = get_user(telegram_id)
                network = user.network_mode.value if user else "mainnet"
                pid = get_product_id(product, network=network, client=client)
                if pid is not None:
                    mp = client.get_market_price(pid)
                    price = float(mp.get("mid", 0) or 0)
    except Exception:
        price = 0.0
    est_margin = (size * price) / leverage if leverage > 0 and price > 0 else None
    session["price"] = price
    session["est_margin"] = est_margin


def _build_confirm_preview(session: dict) -> str:
    product = session.get("product", "BTC")
    size = float(session.get("size", 0) or 0)
    leverage = int(session.get("leverage", 1) or 1)
    direction = session.get("direction", "long")
    order_type = session.get("order_type", "market")
    price = float(session.get("price", 0) or 0)
    est_margin = session.get("est_margin")

    action = direction
    if order_type == "limit":
        action = "limit_long" if direction == "long" else "limit_short"

    preview = fmt_trade_preview(action, product, size, price, leverage, est_margin)
    tp = session.get("tp")
    sl = session.get("sl")
    if tp:
        preview += f"\n\n📈 *Take Profit:* {escape_md(str(tp))}"
    if sl:
        preview += f"\n📉 *Stop Loss:* {escape_md(str(sl))}"
    return preview


async def _edit_or_send_trade_card(
    context: CallbackContext,
    telegram_id: int,
    session: dict,
    chat_id: int,
    message_id: Optional[int] = None,
):
    lang = get_active_language()
    text = localize_text(_build_trade_card_text(session), lang)
    kb = localize_markup(_card_keyboard(session), lang)
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb,
            )
            return message_id
        except BadRequest as e:
            if "Can't parse entities" not in str(e):
                raise
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=plain_text_fallback(text),
                reply_markup=kb,
            )
            return message_id
        except Exception:
            pass

    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb,
        )
    except BadRequest as e:
        if "Can't parse entities" not in str(e):
            raise
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=plain_text_fallback(text),
            reply_markup=kb,
        )
    return message.message_id


async def _edit_message_safely(query, text: str, reply_markup=None):
    lang = get_active_language()
    try:
        await query.edit_message_text(
            localize_text(text, lang),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(reply_markup, lang) if reply_markup else reply_markup,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        if "Can't parse entities" in str(e):
            await query.edit_message_text(
                plain_text_fallback(localize_text(text, lang)),
                reply_markup=localize_markup(reply_markup, lang) if reply_markup else reply_markup,
            )
            return
        raise


async def open_trade_card_from_message(
    update: Update,
    context: CallbackContext,
    telegram_id: int,
    *,
    prefer_reply_to_message: bool = False,
) -> bool:
    if not is_trade_card_mode_enabled() or not update.message:
        return False
    session = _get_trade_card_session(context, touch=True)
    chat_id = update.effective_chat.id

    if prefer_reply_to_message:
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        session = {
            "session_id": _new_session_id(),
            "state": "direction",
            "origin_chat_id": chat_id,
            "network": network,
        }
        lang = get_active_language()
        try:
            message = await update.message.reply_text(
                localize_text(_build_trade_card_text(session), lang),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=localize_markup(_card_keyboard(session), lang),
            )
        except BadRequest as e:
            if "Can't parse entities" not in str(e):
                raise
            message = await update.message.reply_text(
                plain_text_fallback(localize_text(_build_trade_card_text(session), lang)),
                reply_markup=localize_markup(_card_keyboard(session), lang),
            )
        session["origin_chat_id"] = chat_id
        session["origin_message_id"] = message.message_id
        _set_trade_card_session(context, session)
        return True

    if session and session.get("origin_chat_id") == chat_id and session.get("origin_message_id"):
        await _edit_or_send_trade_card(
            context,
            telegram_id,
            session,
            chat_id=chat_id,
            message_id=session.get("origin_message_id"),
        )
        return True

    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    session = {
        "session_id": _new_session_id(),
        "state": "direction",
        "origin_chat_id": chat_id,
        "network": network,
    }
    message_id = await _edit_or_send_trade_card(context, telegram_id, session, chat_id=chat_id)
    session["origin_message_id"] = message_id
    _set_trade_card_session(context, session)
    return True


async def open_trade_card_from_callback(query, context: CallbackContext, telegram_id: int) -> bool:
    if not is_trade_card_mode_enabled():
        return False
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    session = {
        "session_id": _new_session_id(),
        "state": "direction",
        "origin_chat_id": query.message.chat_id,
        "origin_message_id": query.message.message_id,
        "network": network,
    }
    _set_trade_card_session(context, session)
    await _edit_message_safely(query, _build_trade_card_text(session), _card_keyboard(session))
    return True


async def _render_home_on_card(query, telegram_id: int):
    text = await build_home_card_text_async(telegram_id)
    await _edit_message_safely(query, text, home_card_kb())


def _session_matches_query(session: dict, query, session_id: str) -> bool:
    if session.get("session_id") != session_id:
        return False
    if session.get("origin_chat_id") != query.message.chat_id:
        return False
    if session.get("origin_message_id") != query.message.message_id:
        return False
    return True


async def _execute_card_trade(query, context: CallbackContext, telegram_id: int, session: dict):
    direction = session.get("direction", "long")
    order_type = session.get("order_type", "market")
    product = session.get("product", "BTC")
    size = float(session.get("size", 0) or 0)
    leverage = int(session.get("leverage", 1) or 1)
    slippage_pct = get_user_settings(telegram_id)[1].get("slippage", 1)

    if is_trading_paused():
        await _edit_message_safely(query, "⏸ Trading is temporarily paused by admin\\.", home_card_kb())
        _clear_trade_card_session(context)
        return
    wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ready:
        await _edit_message_safely(query, f"⚠️ {escape_md(wallet_msg)}", home_card_kb())
        _clear_trade_card_session(context)
        return

    _clear_trade_card_session(context)
    from src.nadobro.handlers.messages import execute_action_directly
    await execute_action_directly(query, context, telegram_id, {
        "type": "trade_card",
        "flow": {
            "order_type": order_type,
            "product": product,
            "size": size,
            "direction": direction,
            "leverage": leverage,
            "slippage_pct": slippage_pct,
            "limit_price": session.get("limit_price", session.get("price", 0)),
            "tp": session.get("tp"),
            "sl": session.get("sl"),
        },
    })


async def handle_trade_card_callback(update: Update, context: CallbackContext, telegram_id: int, data: str) -> bool:
    query = update.callback_query
    if not query or not query.message:
        return False

    if data == "card:trade:start":
        if not is_new_onboarding_complete(telegram_id):
            await _edit_message_safely(
                query,
                "⚠️ Complete setup first (language + accept terms).",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶ Complete setup", callback_data="onboarding:resume")],
                    [InlineKeyboardButton("Exit", callback_data="nav:main")],
                ]),
            )
            return True
        return await open_trade_card_from_callback(query, context, telegram_id)

    parts = data.split(":")
    if len(parts) < 4:
        return False

    session_id = parts[2]
    action = parts[3]
    value = parts[4] if len(parts) > 4 else ""

    session = _get_trade_card_session(context, touch=True)
    if not session:
        logger.info("trade_card_expired telegram_id=%s", telegram_id)
        await _edit_message_safely(query, "⌛ Trade card expired\\. Start a new guided trade\\.", home_card_kb())
        return True
    if not _session_matches_query(session, query, session_id):
        logger.info(
            "trade_card_stale_callback telegram_id=%s active_session=%s callback_session=%s",
            telegram_id,
            session.get("session_id"),
            session_id,
        )
        await _edit_message_safely(
            query,
            "⌛ This trade panel is outdated\\. Open a new guided trade to continue\\.",
            home_card_kb(),
        )
        return True

    user = get_user(telegram_id)
    session["network"] = user.network_mode.value if user else session.get("network", "mainnet")

    session.pop("error", None)
    state = session.get("state", "direction")

    if action in ("home", "cancel"):
        _clear_trade_card_session(context)
        await _render_home_on_card(query, telegram_id)
        return True

    if action == "back":
        session["state"] = _back_state(state)
        _set_trade_card_session(context, session)
        await _edit_message_safely(query, _build_trade_card_text(session), _card_keyboard(session))
        return True

    if action == "direction":
        step = get_resume_step(telegram_id)
        if step != "complete":
            session["error"] = f"Setup incomplete. Resume onboarding at {step.upper()}."
            _set_trade_card_session(context, session)
            await _edit_message_safely(query, _build_trade_card_text(session), _card_keyboard(session))
            return True
        wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
        if not wallet_ready:
            session["error"] = wallet_msg
            _set_trade_card_session(context, session)
            await _edit_message_safely(query, _build_trade_card_text(session), _card_keyboard(session))
            return True
        session["direction"] = value
        session["state"] = "order_type"
    elif action == "order":
        session["order_type"] = value
        session["state"] = "product"
    elif action == "product":
        session["product"] = value
        session["state"] = "leverage"
    elif action == "lev":
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        max_leverage = get_product_max_leverage(session.get("product", "BTC"), network=network)
        try:
            selected = int(value)
        except (TypeError, ValueError):
            selected = 1
        if selected > max_leverage:
            session["error"] = f"Max leverage for {session.get('product', 'BTC')} is {max_leverage}x."
            _set_trade_card_session(context, session)
            await _edit_message_safely(query, _build_trade_card_text(session), _card_keyboard(session))
            return True
        session["leverage"] = selected
        session["state"] = "size"
    elif action == "size":
        try:
            session["size"] = float(value)
        except (TypeError, ValueError):
            session["error"] = "Invalid size selected."
            _set_trade_card_session(context, session)
            await _edit_message_safely(query, _build_trade_card_text(session), _card_keyboard(session))
            return True
        session["state"] = "limit_price" if session.get("order_type") == "limit" else "tpsl"
    elif action == "size_custom":
        session["state"] = "size_custom_input"
    elif action == "tpsl":
        if value == "skip":
            await _load_preview_fields(session, telegram_id)
            session["state"] = "confirm"
            _set_trade_card_session(context, session)
            await _edit_message_safely(query, _build_confirm_preview(session), trade_card_confirm_kb(session["session_id"]))
            return True
        session["state"] = "tpsl_edit"
    elif action == "tp_prompt":
        session["state"] = "tp_input"
    elif action == "sl_prompt":
        session["state"] = "sl_input"
    elif action == "tpsl_done":
        await _load_preview_fields(session, telegram_id)
        session["state"] = "confirm"
        _set_trade_card_session(context, session)
        await _edit_message_safely(query, _build_confirm_preview(session), trade_card_confirm_kb(session["session_id"]))
        return True
    elif action == "confirm":
        await _execute_card_trade(query, context, telegram_id, session)
        return True
    else:
        return False

    _set_trade_card_session(context, session)
    await _edit_message_safely(query, _build_trade_card_text(session), _card_keyboard(session))
    return True


async def handle_trade_card_text_input(update: Update, context: CallbackContext, telegram_id: int, text: str) -> bool:
    if not is_trade_card_mode_enabled():
        return False
    session = _get_trade_card_session(context, touch=True)
    if not session:
        return False

    state = session.get("state")
    if state not in ("size_custom_input", "limit_price", "tp_input", "sl_input"):
        return False

    try:
        value = float(text.strip())
    except (TypeError, ValueError):
        session["error"] = "Invalid number. Try again."
        _set_trade_card_session(context, session)
        lang = get_active_language()
        try:
            await context.bot.edit_message_text(
                chat_id=session["origin_chat_id"],
                message_id=session["origin_message_id"],
                text=localize_text(_build_trade_card_text(session), lang),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=localize_markup(_card_keyboard(session), lang),
            )
        except Exception:
            pass
        return True

    session.pop("error", None)
    if state == "size_custom_input":
        session["size"] = value
        session["state"] = "limit_price" if session.get("order_type") == "limit" else "tpsl"
    elif state == "limit_price":
        session["limit_price"] = value
        session["state"] = "tpsl"
    elif state == "tp_input":
        session["tp"] = value
        session["state"] = "tpsl_edit"
    elif state == "sl_input":
        session["sl"] = value
        session["state"] = "tpsl_edit"

    _set_trade_card_session(context, session)
    lang = get_active_language()
    try:
        await context.bot.edit_message_text(
            chat_id=session["origin_chat_id"],
            message_id=session["origin_message_id"],
            text=localize_text(_build_trade_card_text(session), lang),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(_card_keyboard(session), lang),
        )
    except Exception:
        logger.exception("Failed to edit trade card after text input")
    return True
