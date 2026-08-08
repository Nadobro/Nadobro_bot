import logging
import re
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackContext
from telegram.constants import ParseMode, ChatAction
from src.nadobro.i18n import language_context, get_user_language, localize_text, localize_markup, get_active_language, resolve_reply_button_text
from src.nadobro.users.user_service import (
    get_or_create_user, get_user_readonly_client, get_user_wallet_info, get_user,
    ensure_active_wallet_ready, save_linked_signer, get_user_nado_client,
)
from src.nadobro.trading.trade_service import execute_market_order, execute_limit_order
from src.nadobro.trading.trade_service import close_position, close_all_positions, get_trade_analytics
from src.nadobro.notify.alert_service import create_alert
from src.nadobro.llm.conversation_intent import classify_conversation_intent
from src.nadobro.llm.trading_bro_service import answer_mode_for_text, stream_trading_bro_answer
from src.nadobro.users.settings_service import get_user_settings, update_user_settings
from src.nadobro.strategy.bot_runtime import start_user_bot
from src.nadobro.users.onboarding_service import get_resume_step, evaluate_readiness, is_new_onboarding_complete
from src.nadobro.core.crypto import encrypt_with_server_key
from src.nadobro.users.admin_service import is_trading_paused
from src.nadobro.config import get_product_id, get_product_max_leverage, get_perp_products
from src.nadobro.handlers.formatters import (
    escape_md, format_ai_response, fmt_positions, fmt_trade_preview, fmt_strategy_update,
    fmt_trade_result, fmt_wallet_info, fmt_settings, fmt_portfolio, build_trade_preview_text,
    fmt_alert_menu_intro, fmt_close_all_confirm, fmt_mode_view, fmt_strategy_hub_intro,
    fmt_bro_answer_card, fmt_referral_dashboard,
)
from src.nadobro.handlers.keyboards import (
    persistent_menu_kb, trade_confirm_kb, REPLY_BUTTON_MAP,
    trade_direction_kb, trade_order_type_kb, trade_product_reply_kb,
    trade_leverage_reply_kb, trade_size_reply_kb, trade_tpsl_kb,
    trade_tpsl_edit_kb, trade_confirm_reply_kb, SIZE_PRESETS,
    mode_kb, strategy_hub_kb, wallet_kb, positions_kb,
    alerts_kb, settings_kb, close_product_kb, confirm_close_all_kb, portfolio_kb,
    bro_answer_kb, referral_kb,
)
from src.nadobro.users.points_ui import points_scope_kb
from src.nadobro.handlers.trade_card import (
    open_trade_card_from_message,
    handle_trade_card_text_input,
    is_trade_card_mode_enabled,
)
from src.nadobro.handlers.home_card import (
    build_positions_view,
    open_home_card_view_from_message,
)
from src.nadobro.handlers.render_utils import plain_text_fallback
from src.nadobro.handlers.state_reset import clear_pending_user_state
from src.nadobro.handlers.wallet_view import build_wallet_view_payload, hydrate_wallet_flow_context
from src.nadobro.handlers.formatters import fmt_points_dashboard
from src.nadobro.users.points_service import (
    get_points_dashboard,
    looks_like_orphan_lowiqpts_reply,
    request_points_refresh,
    relay_user_reply_to_lowiqpts,
)
from src.nadobro.strategy.strategy_pending_input import (
    clear_strategy_pending_input,
    load_strategy_pending_input,
)
from src.nadobro.users.wallet_pending_flow import clear_wallet_pending_flow
from src.nadobro.users.referral_service import (
    auto_generate_referral_code,
    claim_referral_code,
    get_referral_dashboard,
    get_user_referral_code,
    normalize_referral_payload,
    redeem_referral_code,
    validate_custom_code,
)
from src.nadobro.llm.managed_agent_state import is_managed_agent_enabled
from src.nadobro.llm.managed_agent_service import handle_managed_agent_turn


async def _reply_loc(message, text, parse_mode=None, reply_markup=None, **fmt):
    lang = get_active_language()
    localized = localize_text(text, lang)
    if fmt:
        try:
            localized = localized.format(**fmt)
        except (KeyError, ValueError) as e:
            import logging as _logging
            _logging.getLogger("nadobro").warning(
                "_reply_loc template error key=%r lang=%r err=%r", text[:60], lang, e
            )
            localized = text.format(**fmt)
    kwargs = {}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if reply_markup is not None:
        kwargs["reply_markup"] = localize_markup(reply_markup, lang)
    try:
        return await message.reply_text(localized, **kwargs)
    except BadRequest as e:
        if "Can't parse entities" not in str(e) or kwargs.get("parse_mode") != ParseMode.MARKDOWN_V2:
            raise
        logger.warning("messages._reply_loc markdown parse fallback triggered: %s", e)
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("parse_mode", None)
        return await message.reply_text(plain_text_fallback(localized), **fallback_kwargs)


from src.nadobro.handlers.intent_handlers import (
    handle_pending_text_trade_confirmation,
    handle_position_management_intent,
    handle_trade_intent_message,
)
from src.nadobro.handlers.intent_parser import parse_interaction_intent
from src.nadobro.core.async_utils import run_blocking, run_blocking_db, run_blocking_sdk
from src.nadobro.core.perf import timed_metric, log_slow, increment_counter

logger = logging.getLogger(__name__)

TRADE_FLOW_STEPS = ["direction", "order_type", "product", "leverage", "size", "limit_price", "tpsl", "confirm"]
PENDING_TEXT_CLOSE_ALL_KEY = "pending_text_close_all"
STREAM_DRAFT_MIN_CHARS = 24
STREAM_DRAFT_MIN_SECONDS = 0.35


_STATE_REQUIRED_ACTIONS = {
    "trade_flow:direction:long": "direction",
    "trade_flow:direction:short": "direction",
    "trade_flow:order_type:market": "order_type",
    "trade_flow:order_type:limit": "order_type",
    "trade_flow:tpsl:set": "tpsl",
    "trade_flow:tpsl:skip": "tpsl",
    "trade_flow:tpsl:set_tp": "tpsl_edit",
    "trade_flow:tpsl:set_sl": "tpsl_edit",
    "trade_flow:tpsl:done": "tpsl_edit",
    "trade_flow:confirm": "confirm",
}



def _is_contextual_button(callback_data: str, context) -> bool:
    if not callback_data.startswith("trade_flow:"):
        return True

    if callback_data in ("trade_flow:home", "trade_flow:back", "trade_flow:cancel"):
        return True

    flow = context.user_data.get("trade_flow")
    if not flow:
        return callback_data in ("trade_flow:home", "trade_flow:back", "trade_flow:cancel")

    state = flow.get("state", "")

    if callback_data in _STATE_REQUIRED_ACTIONS:
        return state == _STATE_REQUIRED_ACTIONS[callback_data]

    if callback_data.startswith("trade_flow:product:"):
        return state == "product"
    if callback_data.startswith("trade_flow:leverage:"):
        return state == "leverage"
    if callback_data.startswith("trade_flow:size:"):
        return state == "size"

    return True


async def _execute_authorized_action(message, context, telegram_id: int, action_data: dict) -> tuple[bool, str]:
    action_type = action_data.get("type")

    if action_type == "execute_trade":
        payload = action_data.get("payload", {})
        from src.nadobro.handlers.intent_handlers import _execute_trade_payload
        result = await run_blocking(_execute_trade_payload, telegram_id, payload)
        await _reply_loc(message, 
            fmt_trade_result(result),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return bool(result.get("success")), str(result.get("error", ""))

    if action_type == "execute_trade_flow":
        flow = action_data.get("flow", {})
        direction = flow.get("direction", "long")
        order_type = flow.get("order_type", "market")
        product = flow.get("product", "BTC")
        size = flow.get("size", 0)
        leverage = flow.get("leverage", 1)
        slippage_pct = flow.get("slippage_pct", 1)

        if order_type == "limit":
            price = flow.get("limit_price", flow.get("price", 0))
            is_long = direction == "long"
            result = await run_blocking(
                execute_limit_order, telegram_id, product, size, price,
                is_long=is_long, leverage=leverage,
                tp_price=flow.get("tp"), sl_price=flow.get("sl"),
            )
        else:
            is_long = direction == "long"
            result = await run_blocking(
                execute_market_order, telegram_id, product, size,
                is_long=is_long, leverage=leverage, slippage_pct=slippage_pct,
                tp_price=flow.get("tp"), sl_price=flow.get("sl"),
            )
        await _reply_loc(message, 
            fmt_trade_result(result),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return bool(result.get("success")), str(result.get("error", ""))

    if action_type == "exec_trade_callback":
        pending = action_data.get("pending", {})
        action = pending.get("action", "long")
        product = pending.get("product", "BTC")
        size = pending.get("size", 0)
        leverage = pending.get("leverage", 1)
        slippage_pct = pending.get("slippage_pct", 1)

        if action in ("limit_long", "limit_short"):
            price = pending.get("price", 0)
            is_long = action == "limit_long"
            result = await run_blocking(
                execute_limit_order, telegram_id, product, size, price,
                is_long=is_long, leverage=leverage,
            )
        else:
            is_long = action == "long"
            result = await run_blocking(
                execute_market_order, telegram_id, product, size,
                is_long=is_long, leverage=leverage, slippage_pct=slippage_pct,
            )
        await _reply_loc(message, 
            fmt_trade_result(result),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return bool(result.get("success")), str(result.get("error", ""))

    if action_type == "close_position":
        product = action_data.get("product")
        # Off the 8-thread misc pool (which the click/card-render path shares) so
        # a throttled venue close can't starve unrelated taps. SDK pool = 24.
        result = await run_blocking_sdk(close_position, telegram_id, product)
        if result.get("success"):
            msg = f"✅ Closed {escape_md(str(result.get('cancelled', 0)))} {escape_md(result.get('product', product))} position size\\."
        else:
            msg = f"❌ Close failed: {escape_md(result.get('error', 'unknown error'))}"
        await _reply_loc(message, msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=persistent_menu_kb())
        return bool(result.get("success")), str(result.get("error", ""))

    if action_type == "close_all":
        # SDK pool, not the misc pool the click path renders cards on.
        result = await run_blocking_sdk(close_all_positions, telegram_id)
        if result.get("success"):
            products = ", ".join(result.get("products", []))
            msg = f"✅ Closed total size {escape_md(str(result.get('cancelled', 0)))} across {escape_md(products)}\\."
        else:
            msg = f"❌ Close failed: {escape_md(result.get('error', 'unknown error'))}"
        await _reply_loc(message, msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=persistent_menu_kb())
        return bool(result.get("success")), str(result.get("error", ""))

    if action_type == "trade_card":
        flow = action_data.get("flow", {})
        order_type = flow.get("order_type", "market")
        product = flow.get("product", "BTC")
        size = flow.get("size", 0)
        direction = flow.get("direction", "long")
        leverage = flow.get("leverage", 1)
        slippage_pct = flow.get("slippage_pct", 1)

        if order_type == "limit":
            price = float(flow.get("limit_price", flow.get("price", 0)) or 0)
            result = await run_blocking(
                execute_limit_order, telegram_id, product, size, price,
                is_long=(direction == "long"), leverage=leverage,
                tp_price=flow.get("tp"), sl_price=flow.get("sl"),
            )
        else:
            result = await run_blocking(
                execute_market_order, telegram_id, product, size,
                is_long=(direction == "long"), leverage=leverage, slippage_pct=slippage_pct,
                tp_price=flow.get("tp"), sl_price=flow.get("sl"),
            )
        result_msg = fmt_trade_result(result)
        await _reply_loc(message, 
            f"{result_msg}\n\nUse the menu for your next action\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return bool(result.get("success")), str(result.get("error", ""))

    if action_type == "start_copy":
        trader_id = action_data.get("trader_id")
        budget_usd = float(action_data.get("budget_usd", 100))
        risk_factor = float(action_data.get("risk_factor", 1.0))
        max_leverage = float(action_data.get("max_leverage", 10))
        cumulative_stop_loss_pct = action_data.get("cumulative_stop_loss_pct")
        cumulative_take_profit_pct = action_data.get("cumulative_take_profit_pct")
        from src.nadobro.trading.copy_service import (
            MAX_MARGIN_PER_TRADE,
            MIN_MARGIN_PER_TRADE,
            start_copy,
        )
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        if budget_usd < MIN_MARGIN_PER_TRADE:
            reply = (
                f"⚠️ Copy trading needs a margin of at least "
                f"${MIN_MARGIN_PER_TRADE:,.0f}\\."
            )
            await _reply_loc(message,
                reply,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=persistent_menu_kb(),
            )
            return False, f"margin below ${MIN_MARGIN_PER_TRADE:,.0f} minimum"
        # Risk factor scales the per-trade budget slice inside the user's total
        # allocation, floored at the product's $100 minimum margin.
        margin_per_trade = max(
            MIN_MARGIN_PER_TRADE,
            min(MAX_MARGIN_PER_TRADE, budget_usd, budget_usd * max(risk_factor, 0.1)),
        )
        start_kwargs = {
            "network": network,
            "margin_per_trade": margin_per_trade,
            "max_leverage": max_leverage,
            "total_allocated_usd": budget_usd,
        }
        start_kwargs["cumulative_stop_loss_pct"] = float(cumulative_stop_loss_pct or 0.0)
        start_kwargs["cumulative_take_profit_pct"] = float(cumulative_take_profit_pct or 0.0)
        ok, msg = await run_blocking(start_copy, telegram_id, trader_id, **start_kwargs)
        if ok:
            reply = f"🔁 {escape_md(msg)}"
        else:
            reply = f"⚠️ {escape_md(msg)}"
        await _reply_loc(message,
            reply,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return bool(ok), "" if ok else str(msg)

    if action_type == "start_strategy":
        strategy = action_data.get("strategy")
        product = action_data.get("product")
        leverage = float(action_data.get("leverage", 3))
        slippage_pct = float(action_data.get("slippage_pct", 1))
        direction = str(action_data.get("direction") or "long")
        start_kwargs = {
            "direction": direction,
        }
        if str(strategy or "").lower() == "vol":
            # Volume is spot-only; stale NLP payloads may omit vol_market or carry
            # the retired perp value from older prompts.
            start_kwargs["vol_market"] = "spot"
        ok, msg = await run_blocking(
            start_user_bot,
            telegram_id,
            strategy=strategy,
            product=product,
            leverage=leverage,
            slippage_pct=slippage_pct,
            **start_kwargs,
        )
        if ok:
            reply = f"🚀 {escape_md(msg)}\n\nUse /status to monitor live loop health\\."
        else:
            reply = f"❌ {escape_md(msg)}"
        await _reply_loc(message, 
            reply,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return bool(ok), "" if ok else str(msg)

    await _reply_loc(message, 
        "⚠️ Unknown action\\. Please try again\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=persistent_menu_kb(),
    )
    return False, "unknown action"


async def execute_action_directly(update_or_query, context, telegram_id: int, action_data: dict):
    target_message = getattr(update_or_query, "message", None)
    if target_message:
        await _execute_authorized_action(target_message, context, telegram_id, action_data)


def _get_trade_flow(context):
    return context.user_data.get("trade_flow")


def _set_trade_flow(context, flow):
    context.user_data["trade_flow"] = flow


def _clear_trade_flow(context):
    context.user_data.pop("trade_flow", None)
    context.user_data.pop("trade_flow_custom_size", None)
    context.user_data.pop("trade_flow_tp_input", None)
    context.user_data.pop("trade_flow_sl_input", None)
    context.user_data.pop("trade_flow_limit_price_input", None)


async def handle_message(update: Update, context: CallbackContext):
    started = time.perf_counter()
    try:
        if not update.message or not update.message.text:
            return

        telegram_id = update.effective_user.id
        username = update.effective_user.username
        text = update.message.text.strip()

        # Always two psycopg2 round-trips (SELECT + last_active UPDATE) with no
        # cache in front of them. On the event loop that blocked every other
        # user's strategy tick and tap for the duration — hand it to the DB pool.
        await run_blocking_db(get_or_create_user, telegram_id, username)

        with language_context(get_user_language(telegram_id)):
            return await _handle_message_inner(update, context, telegram_id, username, text, started)
    finally:
        log_slow("message.total", threshold_ms=1000.0, started_at=started)


async def _handle_message_inner(update, context, telegram_id, username, text, started):
    if context.user_data.get("pending_referral_claim"):
        if await _handle_pending_referral_claim(update, context, telegram_id, text):
            return

    # Soft fallback: explicit "ref_<CODE>" pastes work outside the deep link
    # too, so a user who shares the raw start-payload string still gets linked.
    stripped = (text or "").strip()
    if stripped.lower().startswith("ref_"):
        normalized = normalize_referral_payload(stripped)
        if normalized:
            try:
                ok, msg = await run_blocking(
                    redeem_referral_code, telegram_id, username, normalized
                )
                if ok:
                    await update.message.reply_text(f"✅ {msg}")
                else:
                    await update.message.reply_text(f"ℹ️ {msg}")
            except Exception as exc:
                logger.warning("ref_ free-text redeem failed for %s: %s", telegram_id, exc)
            return

    resolved_text = resolve_reply_button_text(text, prefer=REPLY_BUTTON_MAP.__contains__)
    flow = context.user_data.get("trade_flow") or {}
    current_state = flow.get("state")
    if current_state == "product":
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        dynamic_products = {p.upper() for p in get_perp_products(network=network)}
        if resolved_text.upper() in dynamic_products and resolved_text not in REPLY_BUTTON_MAP:
            callback_data = f"trade_flow:product:{resolved_text.upper()}"
            try:
                await _dispatch_reply_button(update, context, telegram_id, callback_data, text)
            except Exception as e:
                logger.error(f"Dynamic button dispatch error for '{text}': {e}", exc_info=True)
            return
    if resolved_text in REPLY_BUTTON_MAP:
        callback_data = REPLY_BUTTON_MAP[resolved_text]
        if _is_contextual_button(callback_data, context):
            try:
                await _dispatch_reply_button(update, context, telegram_id, callback_data, text)
            except Exception as e:
                logger.error(f"Button dispatch error for '{text}': {e}", exc_info=True)
                lang = get_active_language()
                try:
                    await _reply_loc(update.message, 
                        localize_text("⚠️ Something went wrong\\. Please try again\\.", lang),
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=localize_markup(persistent_menu_kb(), lang),
                    )
                except Exception:
                    pass
            return

    # Wallet linking must win over Bro/Strategy/AI handlers. Hydrate from bot_state
    # when user_data was lost (multi-worker webhooks).
    if await _handle_wallet_flow(update, context, telegram_id, text):
        return

    # Strategy Lab / Bro FAQ / support-question text beats trade-card numerics, LOWIQPTS relay,
    # and chat agents. Hydrate from bot_state when user_data was lost (multi-worker webhooks).
    if await _handle_pending_strategy_input(update, context, telegram_id, text):
        return
    if await _handle_pending_bro_input(update, context, telegram_id, text):
        return
    if await _handle_pending_question(update, context, telegram_id, text):
        return

    if await handle_trade_card_text_input(update, context, telegram_id, text):
        return

    if await handle_pending_text_trade_confirmation(update, context, telegram_id, text):
        return

    if await _handle_pending_text_close_all_confirmation(update, context, telegram_id, text):
        return

    if await _handle_trade_flow_free_text(update, context, telegram_id, text):
        return

    if await _handle_pending_trade(update, context, telegram_id, text):
        return

    if await _handle_pending_alert(update, context, telegram_id, text):
        return

    if await _handle_pending_copy_wallet(update, context, telegram_id, text):
        return

    if await _handle_pending_admin_copy_wallet(update, context, telegram_id, text):
        return

    relay_result = await relay_user_reply_to_lowiqpts(context, update.effective_chat.id, text)
    if relay_result.get("handled"):
        if relay_result.get("cancelled"):
            await _reply_loc(
                update.message,
                "✅ Points request closed\\. Tap *🔄 Refresh* to start again\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=points_scope_kb(),
            )
        elif not relay_result.get("ok", False):
            await _reply_loc(
                update.message,
                escape_md(relay_result.get("error", "Could not relay your reply right now.")),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=points_scope_kb(),
            )
        return

    from src.nadobro.handlers.vault_handler import handle_vault_text
    if await handle_vault_text(update, context):
        return

    from src.nadobro.llm.brief_intent import is_brief_request
    if is_brief_request(text):
        from src.nadobro.handlers.brief_commands import _send_brief
        await _send_brief(update, context)
        return

    conversation_intent = classify_conversation_intent(text)
    allow_operational_intents = conversation_intent.name not in {"learn", "debug"}

    if allow_operational_intents and await handle_position_management_intent(update, context, telegram_id, text):
        return

    # Desk text-to-trade: TWAP / triggers / chained exits / spot, behind a
    # confirm card. Owns Buy/Sell (spot) and Long/Short (perp) phrasing; the
    # legacy one-shot parser below remains the fallback when Desk declines.
    if allow_operational_intents:
        from src.nadobro.handlers.desk_handler import handle_desk_text

        if await handle_desk_text(update, context, telegram_id, text):
            return

    if allow_operational_intents and await handle_trade_intent_message(update, context, telegram_id, text):
        return

    if await _handle_interaction_intent_message(update, context, telegram_id, text):
        return

    # Abuse / cost guard: the managed-agent path below makes LLM + market/DB
    # calls. Throttle it per-user so a single account can't spam expensive
    # inference. Quick paths above (buttons, trade flows) are intentionally not
    # rate-limited so normal UX stays snappy.
    from src.nadobro.core.user_rate_limit import check_rate_limit as _llm_rate_limit

    allowed, retry_after = _llm_rate_limit(telegram_id, "llm")
    if not allowed:
        wait = max(1, int(retry_after + 0.5))
        await _reply_loc(
            update.message,
            "⏳ Easy bro, you're firing faster than I can think. "
            f"Give me ~{wait}s and hit me again.",
        )
        return

    if await _handle_managed_agent_message(update, context, telegram_id, username, text):
        return

    # Safety net: if every other handler refused the message and it looks like a
    # LOWIQPTS reply (bare number / yes / no), the user almost certainly intended
    # it for a points refresh whose pending state was lost. Surface that instead
    # of dumping the user into the AI chat handler.
    if looks_like_orphan_lowiqpts_reply(text):
        await _reply_loc(
            update.message,
            "⚠️ No points refresh in progress\\. Tap *🏆 Refresh points* to start a new one\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=points_scope_kb(),
        )
        return

    await _handle_nado_question(update, context, text)


async def _dispatch_reply_button(update, context, telegram_id, callback_data, text):
    lang = get_active_language()
    if callback_data in ("market:view", "nav:market_radar", "market:radar", "home:market_radar"):
        callback_data = "points:view"

    if callback_data == "nav:main":
        clear_pending_user_state(context, telegram_id)
        if is_trade_card_mode_enabled():
            await open_home_card_view_from_message(update, context, telegram_id, "nav:main")
            return
        await _reply_loc(update.message, 
            localize_text("Use /start to open the dashboard\\.", lang),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(persistent_menu_kb(), lang),
        )
        return

    if is_trade_card_mode_enabled() and callback_data in (
        "pos:view",
        "portfolio:view",
        "wallet:view",
        "points:view",
        "refer:view",
        "nav:strategy_hub",
        "alert:menu",
        "settings:view",
        "nav:mode",
    ):
        clear_pending_user_state(context, telegram_id)
        target = "home:mode" if callback_data == "nav:mode" else callback_data
        await open_home_card_view_from_message(update, context, telegram_id, target)
        return

    if callback_data == "nav:trade":
        clear_pending_user_state(context, telegram_id)
        if not is_new_onboarding_complete(telegram_id):
            await _reply_loc(update.message,
                "⚠️ Complete setup first (language + accept terms).",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶ Complete setup", callback_data="onboarding:resume")],
                    [InlineKeyboardButton("Exit", callback_data="nav:main")],
                ]),
            )
            return
        if is_trade_card_mode_enabled():
            await open_trade_card_from_message(
                update,
                context,
                telegram_id,
                prefer_reply_to_message=True,
            )
            return
        await _reply_loc(update.message, 
            "🤖 *Trade Console*\n\nSelect direction:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(trade_direction_kb(), lang),
        )
        _clear_trade_flow(context)
        _set_trade_flow(context, {"state": "direction"})
        return

    if callback_data.startswith("trade_flow:"):
        if is_trade_card_mode_enabled():
            if callback_data in ("trade_flow:home", "trade_flow:cancel"):
                _clear_trade_flow(context)
                await _reply_loc(update.message, 
                    localize_text("↩️ Returned to home\\.", lang),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=localize_markup(persistent_menu_kb(), lang),
                )
                return
            await open_trade_card_from_message(
                update,
                context,
                telegram_id,
                prefer_reply_to_message=True,
            )
            return
        await _handle_trade_flow_button(update, context, telegram_id, callback_data)
        return

    if callback_data == "nav:mode":
        user = get_user(telegram_id)
        current_network = user.network_mode.value if user else "testnet"
        await _reply_loc(update.message, 
            fmt_mode_view(current_network),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(mode_kb(current_network), lang),
        )
        return

    if callback_data == "nav:strategy_hub":
        await _reply_loc(update.message, 
            fmt_strategy_hub_intro(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(strategy_hub_kb(), lang),
        )
        return

    if callback_data == "wallet:view":
        await update.message.chat.send_action(ChatAction.TYPING)
        with timed_metric("msg.wallet.view"):
            msg, reply_markup = await run_blocking(build_wallet_view_payload, telegram_id, context, True)
        await _reply_loc(update.message, 
            msg,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(reply_markup, lang),
        )
        return

    if callback_data == "pos:view":
        await update.message.chat.send_action(ChatAction.TYPING)
        with timed_metric("msg.positions.view"):
            msg, reply_markup = await run_blocking(build_positions_view, telegram_id)
        await _reply_loc(update.message, 
            msg,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(reply_markup, lang),
        )
        return

    if callback_data == "portfolio:view":
        await update.message.chat.send_action(ChatAction.TYPING)
        with timed_metric("msg.portfolio.view"):
            from src.nadobro.handlers.portfolio_deck import render_portfolio_deck, snapshot_for_user

            try:
                snapshot = await snapshot_for_user(telegram_id)
                msg, reply_markup = render_portfolio_deck(snapshot)
            except Exception as e:
                logging.getLogger(__name__).warning("message_portfolio_deck_failed user=%s err=%s", telegram_id, e)
                msg, reply_markup = "⚠️ Portfolio refresh is temporarily unavailable. Try again shortly.", persistent_menu_kb()
        await _reply_loc(update.message, 
            msg,
            reply_markup=localize_markup(reply_markup, lang),
        )
        return

    if callback_data == "points:view":
        payload = await run_blocking(get_points_dashboard, telegram_id, "week")
        await _reply_loc(
            update.message,
            fmt_points_dashboard(payload),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(points_scope_kb(), lang),
        )
        return

    if callback_data == "refer:view":
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        payload = await run_blocking(get_referral_dashboard, telegram_id, network)
        await _reply_loc(
            update.message,
            fmt_referral_dashboard(payload),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(referral_kb(can_generate=bool(payload.get("remaining_codes"))), lang),
        )
        return

    if callback_data == "points:refresh":
        result = await request_points_refresh(context, telegram_id, update.effective_chat.id)
        if result.get("ok"):
            await _reply_loc(
                update.message,
                "⏳ Refresh requested\\. I will post your points update as soon as LOWIQPTS replies\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=localize_markup(points_scope_kb(), lang),
            )
        else:
            await _reply_loc(
                update.message,
                escape_md(result.get("error", "Could not refresh points right now.")),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=localize_markup(points_scope_kb(), lang),
            )
        return

    if callback_data == "alert:menu":
        await _reply_loc(update.message, 
            fmt_alert_menu_intro(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(alerts_kb(), lang),
        )
        return

    if callback_data == "settings:view":
        user_settings = _get_user_settings(telegram_id, context)
        msg = fmt_settings(user_settings)
        lev = user_settings.get("default_leverage", 1)
        slip = user_settings.get("slippage", 1)
        await _reply_loc(update.message, 
            msg,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(settings_kb(lev, slip), lang),
        )
        return

    await _reply_loc(update.message, 
        localize_text("Use the menu for your next action\\.", lang),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_trade_flow_button(update, context, telegram_id, callback_data):
    parts = callback_data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""

    flow = _get_trade_flow(context)

    if action == "home" or action == "cancel":
        _clear_trade_flow(context)
        await _reply_loc(update.message, 
            "↩️ Returned to home\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return

    if action == "back":
        if not flow:
            _clear_trade_flow(context)
            await _reply_loc(update.message, 
                "↩️ Returned to home\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=persistent_menu_kb(),
            )
            return
        state = flow.get("state", "direction")
        await _go_back(update, context, flow, state, telegram_id)
        return

    if action == "direction":
        step = get_resume_step(telegram_id)
        if step != "complete":
            await _reply_loc(update.message, 
                f"⚠️ Setup incomplete\\. Resume onboarding at *{escape_md(step.upper())}*\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=persistent_menu_kb(),
            )
            _clear_trade_flow(context)
            return
        wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
        if not wallet_ready:
            await _reply_loc(update.message, 
                f"⚠️ {escape_md(wallet_msg)}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=persistent_menu_kb(),
            )
            _clear_trade_flow(context)
            return

        direction = value
        direction_label = "🟢 LONG" if direction == "long" else "🔴 SHORT"
        _set_trade_flow(context, {"state": "order_type", "direction": direction})
        await _reply_loc(update.message, 
            f"{direction_label} → Select order type:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_order_type_kb(),
        )
        return

    if action == "order_type":
        if not flow:
            return
        flow["order_type"] = value
        flow["state"] = "product"
        _set_trade_flow(context, flow)
        order_label = "📈 MARKET" if value == "market" else "📉 LIMIT"
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        await _reply_loc(update.message, 
            f"{order_label} → Select product:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_product_reply_kb(network=network),
        )
        return

    if action == "product":
        if not flow:
            return
        flow["product"] = value
        flow["state"] = "leverage"
        _set_trade_flow(context, flow)
        await _reply_loc(update.message, 
            f"🪙 {escape_md(value)}\\-PERP → Select leverage:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_leverage_reply_kb(value),
        )
        return

    if action == "leverage":
        if not flow:
            return
        lev_str = value.replace("x", "").replace("X", "")
        try:
            leverage = int(lev_str)
        except ValueError:
            leverage = 1
        product = flow.get("product", "BTC")
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        max_leverage = get_product_max_leverage(product, network=network)
        if leverage > max_leverage:
            await _reply_loc(update.message, 
                f"⚠️ Max leverage for {escape_md(product)} is {escape_md(str(max_leverage))}x\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=trade_leverage_reply_kb(product),
            )
            return
        flow["leverage"] = leverage
        flow["state"] = "size"
        _set_trade_flow(context, flow)
        await _reply_loc(update.message, 
            f"⚡ {escape_md(str(leverage))}x → Select size for {escape_md(product)}:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_size_reply_kb(product),
        )
        return

    if action == "size":
        if not flow:
            return
        if value == "custom":
            context.user_data["trade_flow_custom_size"] = True
            await _reply_loc(update.message, 
                "✏️ Enter custom size \\(e\\.g\\. `0\\.01`\\):",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        try:
            size = float(value)
        except ValueError:
            return
        flow["size"] = size
        if flow.get("order_type") == "limit":
            flow["state"] = "limit_price"
            _set_trade_flow(context, flow)
            context.user_data["trade_flow_limit_price_input"] = True
            await _reply_loc(update.message, 
                f"📏 Size: {escape_md(str(size))} → Enter limit price:",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            flow["state"] = "tpsl"
            _set_trade_flow(context, flow)
            await _reply_loc(update.message, 
                f"📏 Size: {escape_md(str(size))} → Set TP/SL or skip:",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=trade_tpsl_kb(),
            )
        return

    if action == "tpsl":
        if not flow:
            return
        if value == "skip":
            await _move_to_confirm(update, context, telegram_id, flow)
            return
        if value == "set":
            flow["state"] = "tpsl_edit"
            _set_trade_flow(context, flow)
            tp_val = flow.get("tp")
            sl_val = flow.get("sl")
            tp_str = f"TP: {escape_md(str(tp_val))}" if tp_val else "TP: not set"
            sl_str = f"SL: {escape_md(str(sl_val))}" if sl_val else "SL: not set"
            await _reply_loc(update.message, 
                f"📐 *TP/SL Settings*\n{tp_str} \\| {sl_str}\n\nTap Set TP or Set SL to enter values:",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=trade_tpsl_edit_kb(),
            )
            return
        if value == "set_tp":
            context.user_data["trade_flow_tp_input"] = True
            await _reply_loc(update.message, 
                "Enter take profit price:",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        if value == "set_sl":
            context.user_data["trade_flow_sl_input"] = True
            await _reply_loc(update.message, 
                "Enter stop loss price:",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        if value == "done":
            await _move_to_confirm(update, context, telegram_id, flow)
            return
        return

    if action == "confirm":
        if not flow:
            return
        await _execute_trade_flow(update, context, telegram_id, flow)
        return


async def _go_back(update, context, flow, state, telegram_id):
    if state in ("direction", "order_type"):
        _clear_trade_flow(context)
        await _reply_loc(update.message, 
            "↩️ Returned to home\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return

    back_map = {
        "product": ("order_type", trade_order_type_kb, "Select order type:"),
        "leverage": ("product", trade_product_reply_kb, "Select product:"),
        "size": ("leverage", None, "Select leverage:"),
        "limit_price": ("size", None, None),
        "tpsl": ("size", None, None),
        "tpsl_edit": ("tpsl", trade_tpsl_kb, "Set TP/SL or skip:"),
        "confirm": ("tpsl", trade_tpsl_kb, "Set TP/SL or skip:"),
    }

    if state in back_map:
        prev_state, kb_fn, prompt = back_map[state]
        flow["state"] = prev_state
        context.user_data.pop("trade_flow_custom_size", None)
        context.user_data.pop("trade_flow_tp_input", None)
        context.user_data.pop("trade_flow_sl_input", None)
        context.user_data.pop("trade_flow_limit_price_input", None)
        _set_trade_flow(context, flow)

        if prev_state == "size":
            product = flow.get("product", "BTC")
            await _reply_loc(update.message, 
                f"Select size for {escape_md(product)}:",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=trade_size_reply_kb(product),
            )
        elif kb_fn and prompt:
            await _reply_loc(update.message, 
                prompt,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb_fn(),
            )
        else:
            if prev_state == "leverage":
                product = flow.get("product", "BTC")
                await _reply_loc(update.message, 
                    prompt,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=trade_leverage_reply_kb(product),
                )
    else:
        _clear_trade_flow(context)
        await _reply_loc(update.message, 
            "↩️ Returned to home\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )


async def _move_to_confirm(update, context, telegram_id, flow):
    product = flow.get("product", "BTC")
    size = flow.get("size", 0)
    leverage = flow.get("leverage", 1)
    direction = flow.get("direction", "long")
    order_type = flow.get("order_type", "market")

    if order_type == "limit":
        action = "limit_long" if direction == "long" else "limit_short"
    else:
        action = direction

    price = 0
    try:
        if order_type == "limit":
            price = flow.get("limit_price", 0)
        else:
            client = get_user_readonly_client(telegram_id)
            if client:
                user = get_user(telegram_id)
                network = user.network_mode.value if user else "mainnet"
                pid = get_product_id(product, network=network, client=client)
                if pid is not None:
                    mp = client.get_market_price(pid)
                    price = mp.get("mid", 0)
    except Exception:
        # Preview falls through with price=0 and est_margin=None below — safe
        # for the user, but we want a log line so a broken price endpoint
        # isn't invisible to operators. (Audit 2026-05.)
        logger.warning(
            "trade preview: market-price fetch failed user=%s product=%s",
            telegram_id, product, exc_info=True,
        )

    est_margin = (size * price) / leverage if leverage > 0 and price else None

    flow["state"] = "confirm"
    flow["price"] = price
    flow["est_margin"] = est_margin
    _set_trade_flow(context, flow)

    preview = build_trade_preview_text(
        action=action,
        product=product,
        size=size,
        price=price,
        leverage=leverage,
        est_margin=est_margin,
        tp=flow.get("tp"),
        sl=flow.get("sl"),
    )

    await _reply_loc(update.message, 
        preview,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=trade_confirm_reply_kb(),
    )


async def _execute_trade_flow(update, context, telegram_id, flow):
    direction = flow.get("direction", "long")
    order_type = flow.get("order_type", "market")
    product = flow.get("product", "BTC")
    size = flow.get("size", 0)
    leverage = flow.get("leverage", 1)
    slippage_pct = _get_user_settings(telegram_id, context).get("slippage", 1)

    _clear_trade_flow(context)

    if is_trading_paused():
        await _reply_loc(update.message, 
            "⏸ Trading is temporarily paused by admin\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return

    wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ready:
        await _reply_loc(update.message, 
            f"⚠️ {escape_md(wallet_msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return

    await execute_action_directly(update, context, telegram_id, {
        "type": "execute_trade_flow",
        "flow": {
            "direction": direction,
            "order_type": order_type,
            "product": product,
            "size": size,
            "leverage": leverage,
            "slippage_pct": slippage_pct,
            "limit_price": flow.get("limit_price", flow.get("price", 0)),
            "tp": flow.get("tp"),
            "sl": flow.get("sl"),
        },
    })


async def _handle_trade_flow_free_text(update, context, telegram_id, text):
    if context.user_data.get("trade_flow_custom_size"):
        context.user_data.pop("trade_flow_custom_size", None)
        flow = _get_trade_flow(context)
        if not flow:
            return False
        try:
            size = float(text.strip())
        except ValueError:
            await _reply_loc(update.message, 
                "⚠️ Invalid size\\. Enter a number \\(e\\.g\\. `0\\.01`\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            context.user_data["trade_flow_custom_size"] = True
            return True
        flow["size"] = size
        if flow.get("order_type") == "limit":
            flow["state"] = "limit_price"
            _set_trade_flow(context, flow)
            context.user_data["trade_flow_limit_price_input"] = True
            await _reply_loc(update.message, 
                f"📏 Size: {escape_md(str(size))} → Enter limit price:",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            flow["state"] = "tpsl"
            _set_trade_flow(context, flow)
            await _reply_loc(update.message, 
                f"📏 Size: {escape_md(str(size))} → Set TP/SL or skip:",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=trade_tpsl_kb(),
            )
        return True

    if context.user_data.get("trade_flow_limit_price_input"):
        context.user_data.pop("trade_flow_limit_price_input", None)
        flow = _get_trade_flow(context)
        if not flow:
            return False
        try:
            price = float(text.strip())
        except ValueError:
            await _reply_loc(update.message, 
                "⚠️ Invalid price\\. Enter a number \\(e\\.g\\. `95000`\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            context.user_data["trade_flow_limit_price_input"] = True
            return True
        flow["limit_price"] = price
        flow["state"] = "tpsl"
        _set_trade_flow(context, flow)
        await _reply_loc(update.message, 
            f"💲 Limit price: {escape_md(str(price))} → Set TP/SL or skip:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_tpsl_kb(),
        )
        return True

    if context.user_data.get("trade_flow_tp_input"):
        context.user_data.pop("trade_flow_tp_input", None)
        flow = _get_trade_flow(context)
        if not flow:
            return False
        try:
            tp = float(text.strip())
        except ValueError:
            await _reply_loc(update.message, 
                "⚠️ Invalid price\\. Enter a number\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            context.user_data["trade_flow_tp_input"] = True
            return True
        flow["tp"] = tp
        _set_trade_flow(context, flow)
        tp_str = f"TP: {escape_md(str(tp))}"
        sl_val = flow.get("sl")
        sl_str = f"SL: {escape_md(str(sl_val))}" if sl_val else "SL: not set"
        await _reply_loc(update.message, 
            f"✅ TP set\\!\n{tp_str} \\| {sl_str}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_tpsl_edit_kb(),
        )
        return True

    if context.user_data.get("trade_flow_sl_input"):
        context.user_data.pop("trade_flow_sl_input", None)
        flow = _get_trade_flow(context)
        if not flow:
            return False
        try:
            sl = float(text.strip())
        except ValueError:
            await _reply_loc(update.message, 
                "⚠️ Invalid price\\. Enter a number\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            context.user_data["trade_flow_sl_input"] = True
            return True
        flow["sl"] = sl
        _set_trade_flow(context, flow)
        tp_val = flow.get("tp")
        tp_str = f"TP: {escape_md(str(tp_val))}" if tp_val else "TP: not set"
        sl_str = f"SL: {escape_md(str(sl))}"
        await _reply_loc(update.message, 
            f"✅ SL set\\!\n{tp_str} \\| {sl_str}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_tpsl_edit_kb(),
        )
        return True

    return False


async def _handle_pending_trade(update, context, telegram_id, text):
    pending = context.user_data.get("pending_trade")
    if not pending:
        return False

    step = pending.get("step", "")

    if step == "custom_size":
        try:
            parts = text.split()
            size = float(parts[0])
            leverage = _get_user_settings(telegram_id, context).get("default_leverage", 3)
            explicit_leverage = False
            if len(parts) >= 2:
                lev_str = parts[1].replace("x", "").replace("X", "")
                leverage = int(float(lev_str))
                explicit_leverage = True
        except (ValueError, IndexError):
            await _reply_loc(update.message, 
                "⚠️ Invalid size\\. Enter a number \\(e\\.g\\. `0\\.01`\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return True

        action = pending["action"]
        product = pending["product"]
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        max_leverage = get_product_max_leverage(product, network=network)
        if leverage > max_leverage:
            if explicit_leverage:
                await _reply_loc(update.message, 
                    f"⚠️ Max leverage for {escape_md(product)} is {escape_md(str(max_leverage))}x\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return True
            leverage = max_leverage

        price = 0
        try:
            client = get_user_readonly_client(telegram_id)
            if client:
                pid = get_product_id(product, network=network, client=client)
                if pid is not None:
                    mp = client.get_market_price(pid)
                    price = mp.get("mid", 0)
        except Exception:
            # Same fallback as the other preview branch — est_margin=None is
            # safe; we only need the log line for visibility. (Audit 2026-05.)
            logger.warning(
                "trade preview: market-price fetch failed user=%s product=%s",
                telegram_id, product, exc_info=True,
            )

        est_margin = (size * price) / leverage if leverage > 0 and price else None

        context.user_data["pending_trade"] = {
            "action": action,
            "product": product,
            "size": size,
            "leverage": leverage,
            "price": price,
            "est_margin": est_margin,
            "slippage_pct": _get_user_settings(telegram_id, context).get("slippage", 1),
        }

        preview = fmt_trade_preview(action, product, size, price, leverage, est_margin)
        await _reply_loc(update.message, 
            preview,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_confirm_kb(),
        )
        return True

    if step == "limit_input":
        try:
            parts = text.split()
            size = float(parts[0])
            price = float(parts[1])
        except (ValueError, IndexError):
            await _reply_loc(update.message, 
                "⚠️ Enter size and price \\(e\\.g\\. `0\\.01 95000`\\)\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return True

        action = pending["action"]
        product = pending["product"]
        leverage = _get_user_settings(telegram_id, context).get("default_leverage", 3)
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        max_leverage = get_product_max_leverage(product, network=network)
        if leverage > max_leverage:
            leverage = max_leverage

        context.user_data["pending_trade"] = {
            "action": action,
            "product": product,
            "size": size,
            "leverage": leverage,
            "price": price,
            "slippage_pct": _get_user_settings(telegram_id, context).get("slippage", 1),
        }

        preview = fmt_trade_preview(
            action,
            product,
            size,
            price,
            leverage,
        )
        await _reply_loc(update.message, 
            preview,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_confirm_kb(),
        )
        return True

    return False


def _is_valid_main_address(text: str) -> bool:
    t = (text or "").strip()
    if not t.startswith("0x"):
        return False
    t = t[2:]
    return len(t) == 40 and bool(re.fullmatch(r"[0-9a-fA-F]{40}", t))


async def _handle_wallet_flow(update, context, telegram_id, text):
    await run_blocking(hydrate_wallet_flow_context, context, int(telegram_id))
    flow = context.user_data.get("wallet_flow")
    if not flow:
        return False

    if flow == "awaiting_main_address":
        if not _is_valid_main_address(text):
            await _reply_loc(update.message, 
                "❌ That doesn't look right. Send your main wallet address. It starts with 0x followed by 40 hex characters.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return True
        main_addr = text.strip()
        if main_addr.startswith("0x"):
            main_addr = main_addr[:2] + main_addr[2:].lower()
        context.user_data["wallet_main_address"] = main_addr
        pk_hex = context.user_data.get("wallet_linked_signer_pk")
        linked_addr = context.user_data.get("wallet_linked_signer_address")
        if not pk_hex or not linked_addr:
            context.user_data.pop("wallet_flow", None)
            await run_blocking(clear_wallet_pending_flow, int(telegram_id))
            await _reply_loc(update.message, "⚠️ Session expired. Tap the Wallet button to start again.")
            return True
        pk_bytes = pk_hex.encode("utf-8")
        ciphertext = encrypt_with_server_key(pk_bytes)
        save_linked_signer(telegram_id, main_addr, linked_addr, ciphertext)
        # SECURITY: append-only audit trail for a sensitive key event.
        try:
            from src.nadobro.users.audit_log import record_audit_event

            record_audit_event(telegram_id, "wallet_linked", f"signer={linked_addr}")
        except Exception:
            pass
        # SECURITY: delete the chat message that displayed the 1CT private key —
        # it's now encrypted at rest and registered on Nado, so leaving the
        # plaintext key in chat history is pure downside.
        connect_msg = context.user_data.pop("wallet_connect_msg", None)
        if connect_msg:
            try:
                await context.bot.delete_message(
                    chat_id=connect_msg.get("chat_id"),
                    message_id=connect_msg.get("message_id"),
                )
            except Exception:
                pass
        for key in ("wallet_flow", "wallet_linked_signer_pk", "wallet_main_address", "wallet_linked_signer_address"):
            context.user_data.pop(key, None)
        await run_blocking(clear_wallet_pending_flow, int(telegram_id))
        await _delete_user_message(update)
        await _reply_loc(update.message,
            "✅ 1CT linked\\! Your trading key is encrypted and stored\\.\n\n"
            "You can now trade directly from this bot\\. Revoke anytime with /revoke\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return True

    return False


async def _delete_user_message(update: Update):
    try:
        if update.message:
            await update.message.delete()
    except Exception:
        pass


async def _typing_heartbeat(chat, stop_event: asyncio.Event, interval_seconds: float = 3.0):
    """Keep Telegram's typing indicator alive while a response is being prepared."""
    while not stop_event.is_set():
        try:
            await chat.send_action(ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


def _should_update_streaming_draft(text: str, last_len: int, last_ts: float, now_ts: float) -> bool:
    delta = len(text) - int(last_len or 0)
    if delta < STREAM_DRAFT_MIN_CHARS or (now_ts - float(last_ts or 0.0)) < STREAM_DRAFT_MIN_SECONDS:
        return False
    if not text:
        return False
    # Prefer word-like updates so the draft feels like it is typing, not dumping paragraphs.
    return text[-1].isspace() or text[-1] in ".,;:!?)]}\n"


async def _handle_pending_alert(update, context, telegram_id, text):
    pending = context.user_data.get("pending_alert")
    if not pending:
        return False

    product = pending["product"]
    pre_selected_condition = pending.get("condition")
    context.user_data.pop("pending_alert", None)

    valid_conditions = ("above", "below", "funding_above", "funding_below", "pnl_above", "pnl_below")

    if pre_selected_condition:
        # Condition was picked via button — user only enters target value
        condition = pre_selected_condition
        try:
            target = float(text.strip())
        except ValueError:
            await _reply_loc(update.message,
                "⚠️ Invalid value\\. Please enter a number\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return True
    else:
        # Legacy flow: user types "above 100000"
        try:
            parts = text.lower().split()
            condition = parts[0]
            target = float(parts[1])
        except (ValueError, IndexError):
            await _reply_loc(update.message,
                "⚠️ Invalid format\\. Use: `above 100000` or `below 90000`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return True

    if condition not in valid_conditions:
        await _reply_loc(update.message,
            "⚠️ Invalid condition\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return True

    # Defensive wrap: an unhandled exception here was the root cause of the
    # "I can't create alerts" bug — any DB failure crashed the handler with
    # no user-visible reply. (Audit 2026-05.)
    try:
        result = create_alert(telegram_id, product, condition, target)
    except Exception:
        logger.exception(
            "alert create failed user=%s product=%s condition=%s",
            telegram_id, product, condition,
        )
        await _reply_loc(update.message,
            "❌ Could not create alert\\. Please try again in a moment\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return True
    if result["success"]:
        if condition.startswith("funding"):
            target_str = f"{target:,.4f}%"
        elif condition.startswith("pnl"):
            target_str = f"${target:,.2f}"
        else:
            target_str = f"${target:,.2f}"
        await _reply_loc(update.message, 
            f"✅ Alert set\\!\n"
            f"{escape_md(result['product'])} {escape_md(condition)} "
            f"{escape_md(target_str)}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
    else:
        await _reply_loc(update.message, 
            f"❌ {escape_md(result['error'])}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
    return True


async def _handle_pending_strategy_input(update, context, telegram_id, text):
    pending = context.user_data.get("pending_strategy_input")
    if not pending:
        pending = await run_blocking(load_strategy_pending_input, int(telegram_id))
        if pending:
            context.user_data["pending_strategy_input"] = pending
    if not pending:
        return False

    strategy = pending.get("strategy")
    field = pending.get("field")
    section = pending.get("section")
    supported = ("grid", "rgrid", "dgrid", "mid", "dn", "vol")
    supported_fields = (
        "notional_usd", "spread_bp", "interval_seconds", "tp_pct", "sl_pct",
        "levels", "min_range_pct", "max_range_pct", "threshold_bp", "close_offset_bp",
        "cycle_notional_usd", "session_notional_cap_usd", "inventory_soft_limit_usd",
        "quote_ttl_seconds", "min_spread_bp", "max_spread_bp", "vol_sensitivity",
        "dgrid_trend_on_variance_ratio", "dgrid_range_on_variance_ratio",
        "dgrid_spread_bp", "dgrid_min_spread_bp", "dgrid_max_spread_bp",
        "dgrid_short_window_points", "dgrid_long_window_points",
        "grid_reset_threshold_pct", "grid_reset_timeout_seconds",
        "rgrid_spread_bp", "rgrid_stop_loss_pct", "rgrid_take_profit_pct",
        "rgrid_reset_threshold_pct", "rgrid_reset_timeout_seconds", "rgrid_discretion",
        # Mid Mode accepts directional_bias as a continuous float in [-1, +1].
        "directional_bias",
        # MM/grid leverage (position size = margin × leverage).
        "mm_leverage_override",
        # MM run duration in minutes (participation/TWAP).
        "mm_duration_minutes",
        # TWAP fast-move pause threshold (bp/cycle; 0 = off).
        "twap_pause_move_bp",
        # Volume Bot (spot) custom inputs.
        "session_margin_usd", "target_volume_usd",
        # Delta Neutral (engine v2) custom inputs. Without these here, the DN
        # custom-size/hold/cycles reply was rejected, the pending state cleared,
        # and the typed number fell through to the LOWIQPTS points relay.
        "fixed_margin_usd", "dn_hold_seconds", "dn_cycles",
    )
    if strategy not in supported or field not in supported_fields:
        context.user_data.pop("pending_strategy_input", None)
        await run_blocking(clear_strategy_pending_input, int(telegram_id))
        return False
    # directional_bias is only a numeric field for Mid Mode; other strategies
    # use the discrete neutral/long/short string set via set_text.
    if field == "directional_bias" and strategy != "mid":
        context.user_data.pop("pending_strategy_input", None)
        return False

    try:
        value = float(text.strip())
    except (TypeError, ValueError):
        await _reply_loc(update.message,
            "⚠️ Invalid value\\. Please enter a number\\. Example: `1\\.2`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return True

    # Mid Mode spread is capped at 0 (a post-only LIMIT_MAKER can't cross the
    # book, so a negative spread can't be honored). 0 = tightest fee-floored quote.
    if field == "spread_bp" and strategy == "mid":
        spread_lo, spread_hi = 0.0, 100.0
    else:
        spread_lo, spread_hi = 0.1, 200.0
    limits = {
        "notional_usd": (1, 1000000),
        "spread_bp": (spread_lo, spread_hi),
        "directional_bias": (-1.0, 1.0),
        # Floor 5, matching the button path. It was 10 here, which silently
        # rejected a typed "5" while the 5s PRESET tapped fine — and the button
        # path's own comment says floor 5 exists so Turbo's 5s cadence can be
        # re-entered MANUALLY, which this dict was preventing. The engine
        # hard-floors everything at 3s (core/cadence.effective_interval_seconds).
        "interval_seconds": (5, 3600),
        "tp_pct": (0.05, 100),
        "sl_pct": (0.05, 100),
        "levels": (1, 20),
        "min_range_pct": (0.1, 20),
        "max_range_pct": (0.1, 40),
        "threshold_bp": (0, 500),
        "close_offset_bp": (1, 1000),
        "cycle_notional_usd": (1, 1000000),
        "session_notional_cap_usd": (0, 10000000),
        "inventory_soft_limit_usd": (1, 1000000),
        "quote_ttl_seconds": (5, 86400),
        "min_spread_bp": (0.1, 200),
        "max_spread_bp": (0.1, 500),
        "vol_sensitivity": (0.0, 1.0),
        # Soft-reset drift band: grid re-anchors on small drifts (1% ceiling);
        # rgrid follows trends and needs a far wider band. Must match the
        # button path in strategy_handler.py or the two disagree.
        "grid_reset_threshold_pct": (0.05, 1.0),
        "grid_reset_timeout_seconds": (15, 86400),
        "rgrid_spread_bp": (0.1, 200),
        "rgrid_stop_loss_pct": (0.05, 100),
        "rgrid_take_profit_pct": (0.05, 200),
        "rgrid_reset_threshold_pct": (0.1, 10.0),
        "rgrid_reset_timeout_seconds": (15, 86400),
        "rgrid_discretion": (0.01, 0.5),
        "dgrid_trend_on_variance_ratio": (1.0, 5.0),
        "dgrid_range_on_variance_ratio": (0.1, 2.0),
        "dgrid_spread_bp": (0.1, 200.0),
        "dgrid_min_spread_bp": (0.0, 50.0),
        "dgrid_max_spread_bp": (1.0, 200.0),
        "dgrid_short_window_points": (2, 50),
        "dgrid_long_window_points": (4, 200),
        # Volume Bot (spot) custom inputs — mirror strategy_handler set limits.
        # Band $100-$500 per docs/volume_bot_taker_v4.md (was 10..1_000_000,
        # which disagreed with the handler's own floor of 100).
        "session_margin_usd": (100, 500),
        "target_volume_usd": (100, 100000000),
        # Delta Neutral (engine v2) custom inputs.
        "fixed_margin_usd": (1, 1000000),
        "dn_hold_seconds": (60, 86400),
        "dn_cycles": (1, 100),
        "mm_leverage_override": (1, 50),
        "mm_duration_minutes": (1, 14400),
        "twap_pause_move_bp": (0, 5000),
    }
    if field not in limits:
        logger.error("Missing strategy limit for field=%s (strategy=%s)", field, strategy)
        await _reply_loc(
            update.message,
            "⚠️ This setting is not wired for chat input yet\\. Use the buttons or report to support\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return True

    lo, hi = limits[field]
    if value < lo or value > hi:
        await _reply_loc(update.message, 
            f"⚠️ Value out of range\\. Allowed: {escape_md(str(lo))} to {escape_md(str(hi))}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return True

    def _mutate(s):
        strategies = s.setdefault("strategies", {})
        cfg = strategies.setdefault(strategy, {})
        int_fields = {
            "interval_seconds", "levels", "quote_ttl_seconds", "rgrid_reset_timeout_seconds",
            "grid_reset_timeout_seconds", "dgrid_short_window_points", "dgrid_long_window_points",
            "dn_hold_seconds", "dn_cycles", "mm_leverage_override",
        }
        if field in int_fields:
            cfg[field] = int(value)
        else:
            cfg[field] = value
        if field == "notional_usd":
            from src.nadobro.users.settings_service import sync_cycle_notional_with_margin

            sync_cycle_notional_with_margin(strategies, strategy)

    network, settings = update_user_settings(telegram_id, _mutate)
    conf = settings.get("strategies", {}).get(strategy, {})
    context.user_data.pop("pending_strategy_input", None)
    await run_blocking(clear_strategy_pending_input, int(telegram_id))
    continue_callback = f"strategy:config:{strategy}"
    if section:
        continue_callback = f"strategy:config_section:{strategy}:{section}"
    await _reply_loc(update.message, 
        fmt_strategy_update(strategy, network, conf),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Continue Editing", callback_data=continue_callback)],
            [InlineKeyboardButton("🧠 Strategy Lab", callback_data="nav:strategy_hub")],
        ]),
    )
    return True


async def _handle_pending_bro_input(update, context, telegram_id, text):
    pending = context.user_data.get("pending_bro_input")
    if not pending:
        return False

    field = pending.get("field")
    section = pending.get("section")
    context.user_data.pop("pending_bro_input", None)

    if field == "tp_sl":
        try:
            parts = text.strip().split(",")
            tp = float(parts[0].strip())
            sl = float(parts[1].strip())
        except (ValueError, IndexError):
            await _reply_loc(update.message, 
                "⚠️ Invalid format\\. Use `TP,SL` \\(example: `2.0,1.5`\\)",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return True
        if tp < 0.3 or tp > 20 or sl < 0.3 or sl > 10:
            await _reply_loc(update.message, "⚠️ TP: 0\\.3\\-20%, SL: 0\\.3\\-10%", parse_mode=ParseMode.MARKDOWN_V2)
            return True
        def _mutate(s):
            bro = s.setdefault("strategies", {}).setdefault("bro", {})
            bro["tp_pct"] = tp
            bro["sl_pct"] = sl
        update_user_settings(telegram_id, _mutate)
        continue_callback = "strategy:preview:bro"
        if section:
            continue_callback = f"bro:config_section:{section}"
        await _reply_loc(update.message, 
            f"✅ Alpha Agent TP/SL set to {tp:.1f}%/{sl:.1f}%",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Continue Editing", callback_data=continue_callback)],
                [InlineKeyboardButton("🧠 Alpha Agent", callback_data="strategy:preview:bro")],
            ]),
        )
        return True

    elif field == "risk_level":
        val = text.strip().lower()
        if val not in ("conservative", "balanced", "aggressive"):
            await _reply_loc(update.message, "⚠️ Choose: conservative, balanced, or aggressive")
            return True
        def _mutate(s):
            s.setdefault("strategies", {}).setdefault("bro", {})["risk_level"] = val
        update_user_settings(telegram_id, _mutate)
        continue_callback = "strategy:preview:bro"
        if section:
            continue_callback = f"bro:config_section:{section}"
        await _reply_loc(update.message, 
            f"✅ Alpha Agent risk set to {val.upper()}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Continue Editing", callback_data=continue_callback)],
                [InlineKeyboardButton("🧠 Alpha Agent", callback_data="strategy:preview:bro")],
            ]),
        )
        return True

    else:
        try:
            value = float(text.strip())
        except ValueError:
            await _reply_loc(update.message, "⚠️ Please enter a number\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return True
        limits = {
            "budget_usd": (10, 100000),
            "min_confidence": (0.1, 1.0),
            "leverage_cap": (1, 40),
            "max_positions": (1, 10),
        }
        lo, hi = limits.get(field, (0, 99999))
        if value < lo or value > hi:
            await _reply_loc(update.message, f"⚠️ Range: {lo} to {hi}")
            return True
        int_fields = {"leverage_cap", "max_positions"}
        def _mutate(s):
            bro = s.setdefault("strategies", {}).setdefault("bro", {})
            bro[field] = int(value) if field in int_fields else value
        update_user_settings(telegram_id, _mutate)
        continue_callback = "strategy:preview:bro"
        if section:
            continue_callback = f"bro:config_section:{section}"
        await _reply_loc(update.message, 
            f"✅ Alpha Agent {field} set to {value}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Continue Editing", callback_data=continue_callback)],
                [InlineKeyboardButton("🧠 Alpha Agent", callback_data="strategy:preview:bro")],
            ]),
        )
        return True


async def _handle_pending_referral_claim(update, context, telegram_id, text):
    """User tapped 'Claim Custom Code' and is now typing their desired code."""
    raw = (text or "").strip()
    if raw.lower() in ("cancel", "❌ cancel"):
        context.user_data.pop("pending_referral_claim", None)
        await _reply_loc(
            update.message,
            "Referral claim cancelled.",
            reply_markup=localize_markup(persistent_menu_kb(), get_active_language()),
        )
        return True

    # Codes are permanent once claimed, so only treat the message as a claim
    # attempt when it PLAUSIBLY is one: a single bare alphanumeric token. Any
    # other text (a trade like "long btc 0.01", a menu button, a question)
    # exits the claim flow and falls through to normal handling — previously
    # the flow hijacked every message, and normalize_code stripping
    # punctuation meant "long btc 0.01" was silently claimed as "LONGBTC001".
    if not raw.isalnum():
        context.user_data.pop("pending_referral_claim", None)
        return False

    normalized, error = validate_custom_code(raw)
    if error:
        await _reply_loc(
            update.message,
            f"⚠️ {error}\n\nType another code, or send `cancel` to abort.",
        )
        return True

    username = update.effective_user.username if update.effective_user else None
    user = await run_blocking(get_user, telegram_id)
    network = user.network_mode.value if user else "mainnet"
    ok, msg, row = await run_blocking(
        claim_referral_code, telegram_id, normalized, network=network
    )
    if not ok:
        await _reply_loc(
            update.message,
            f"⚠️ {msg}\n\nType another code, or send `cancel` to abort.",
        )
        return True

    context.user_data.pop("pending_referral_claim", None)
    payload = await run_blocking(get_referral_dashboard, telegram_id, network)
    text_out = fmt_referral_dashboard(payload)
    has_code = bool(payload.get("has_code"))
    try:
        await update.message.reply_text(
            text_out,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(referral_kb(has_code=has_code), get_active_language()),
        )
    except BadRequest:
        await update.message.reply_text(
            plain_text_fallback(text_out),
            reply_markup=localize_markup(referral_kb(has_code=has_code), get_active_language()),
        )
    return True


async def _handle_pending_question(update, context, telegram_id, text):
    if not context.user_data.get("pending_question"):
        return False
    context.user_data.pop("pending_question", None)
    await _handle_nado_question(update, context, text)
    return True


async def _handle_nado_question(update, context, question):
    import random
    chat_id = update.effective_chat.id
    draft_id = random.randint(1, 2**31 - 1)
    telegram_id = update.effective_user.id
    user = update.effective_user
    user_name = user.first_name or user.username or "trader"
    answer_mode = answer_mode_for_text(question)

    try:
        await update.message.chat.send_action(ChatAction.TYPING)
    except Exception:
        pass

    typing_task = None
    stop_typing = asyncio.Event()

    full_text = ""
    last_draft_len = 0
    last_draft_ts = 0.0
    draft_ok = True

    try:
        typing_task = asyncio.create_task(_typing_heartbeat(update.message.chat, stop_typing))
        try:
            # Show immediate visual feedback while retrieval/LLM warm-up happens.
            await context.bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=localize_text("🧠 Trading Bro\n\nThinking..."),
            )
            last_draft_ts = time.time()
        except Exception:
            draft_ok = False

        async for chunk in stream_trading_bro_answer(question, telegram_id=telegram_id, user_name=user_name):
            full_text += chunk
            now_ts = time.time()
            if draft_ok and _should_update_streaming_draft(full_text, last_draft_len, last_draft_ts, now_ts):
                try:
                    await context.bot.send_message_draft(
                        chat_id=chat_id,
                        draft_id=draft_id,
                        text=f"🧠 Trading Bro\n\n{full_text}",
                    )
                    last_draft_len = len(full_text)
                    last_draft_ts = now_ts
                except Exception:
                    draft_ok = False

        if full_text.strip():
            await _reply_loc(update.message,
                fmt_bro_answer_card(full_text, mode=answer_mode),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=bro_answer_kb(answer_mode),
            )
        else:
            await _reply_loc(update.message,
                "⚠️ I couldn't generate an answer\\. Please try again\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:main")],
                ]),
            )
    except Exception as e:
        logger.error(f"Nado Q&A error: {e}", exc_info=True)
        if full_text.strip():
            try:
                await _reply_loc(update.message,
                    fmt_bro_answer_card(full_text, mode=answer_mode),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=bro_answer_kb(answer_mode),
                )
                return
            except Exception:
                pass
        await _reply_loc(update.message, 
            "⚠️ Something went wrong answering your question\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Home", callback_data="nav:main")],
            ]),
        )
    finally:
        stop_typing.set()
        if typing_task:
            try:
                await typing_task
            except Exception:
                pass


async def _handle_managed_agent_message(update, context, telegram_id, username, text):
    if not is_managed_agent_enabled(telegram_id):
        return False

    increment_counter("managed_agent.turn.received")
    stop_typing = asyncio.Event()
    typing_task = None
    try:
        typing_task = asyncio.create_task(_typing_heartbeat(update.message.chat, stop_typing))
        with timed_metric("managed_agent.turn"):
            result = await handle_managed_agent_turn(
                telegram_id=telegram_id,
                text=text,
                username=username,
            )
    finally:
        stop_typing.set()
        if typing_task:
            try:
                await typing_task
            except Exception:
                pass
    if not result.get("handled"):
        increment_counter("managed_agent.turn.unhandled")
        return False

    increment_counter("managed_agent.turn.handled")
    route = str(result.get("route") or "unknown")
    increment_counter(f"managed_agent.route.{route}")
    reply_markup = persistent_menu_kb() if result.get("show_menu") else None
    raw = (result.get("response") or "").strip() or "Done."
    if route == "analysis":
        answer_mode = str(result.get("mode") or answer_mode_for_text(text))
        formatted = fmt_bro_answer_card(raw, mode=answer_mode)
        reply_markup = bro_answer_kb(answer_mode)
    else:
        formatted = format_ai_response(raw)
    try:
        await _reply_loc(
            update.message,
            formatted,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )
    except Exception:
        await _reply_loc(update.message, escape_md(raw), reply_markup=reply_markup)
    return True


async def _handle_pending_text_close_all_confirmation(update, context, telegram_id, text):
    if not context.user_data.get(PENDING_TEXT_CLOSE_ALL_KEY):
        return False

    normalized = (text or "").strip().lower()
    if normalized in ("cancel", "no", "n", "abort"):
        context.user_data.pop(PENDING_TEXT_CLOSE_ALL_KEY, None)
        await _reply_loc(update.message, 
            "❌ Close-all request cancelled\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return True

    if normalized not in ("confirm", "yes", "y", "execute", "close all"):
        await _reply_loc(update.message, 
            "Type `confirm` to close all positions or `cancel` to discard\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=confirm_close_all_kb(),
        )
        return True

    context.user_data.pop(PENDING_TEXT_CLOSE_ALL_KEY, None)
    await execute_action_directly(update, context, telegram_id, {"type": "close_all"})
    return True


async def _handle_interaction_intent_message(update, context, telegram_id, text):
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    intent = parse_interaction_intent(text, network=network)
    if not intent:
        return False

    action = intent.get("action")
    if action == "open_view":
        callback_data = intent.get("target")
        if not callback_data:
            return False
        await _dispatch_reply_button(update, context, telegram_id, callback_data, text)
        return True

    if action == "close_menu":
        await _reply_loc(update.message, 
            "*Close Position*\n\nSelect the product to close:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=close_product_kb(network=network),
        )
        return True

    if action == "close_all":
        context.user_data[PENDING_TEXT_CLOSE_ALL_KEY] = True
        await _reply_loc(update.message, 
            fmt_close_all_confirm() + "\n\nType `confirm` to execute or `cancel` to discard\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=confirm_close_all_kb(),
        )
        return True

    if action == "close_product":
        product = intent.get("product")
        if not product:
            return False
        await execute_action_directly(update, context, telegram_id, {"type": "close_position", "product": product})
        return True

    return False


async def _handle_pending_copy_wallet(update, context, telegram_id, text):
    if not context.user_data.get("pending_copy_wallet"):
        return False
    context.user_data.pop("pending_copy_wallet", None)

    import re
    wallet = text.strip()
    if not re.match(r'^0x[0-9a-fA-F]{40}$', wallet):
        await _reply_loc(update.message,
            "⚠️ Invalid wallet address\\. Must be 42 characters starting with `0x`\\.\n\nPlease try again from the Copy Trading menu\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return True

    from src.nadobro.trading.copy_service import add_trader
    ok, msg, trader_id = add_trader(
        wallet,
        label=wallet[:10],
        is_curated=False,
        owner_user_id=int(telegram_id),
    )
    if ok and trader_id:
        from src.nadobro.handlers.keyboards import copy_trader_preview_kb
        wallet_snip = wallet[:6] + "..." + wallet[-4:]
        await _reply_loc(update.message,
            f"✅ {escape_md(msg)}\n\nWallet: `{escape_md(wallet_snip)}`\n\nTap Start Copying to configure your copy settings\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=copy_trader_preview_kb(trader_id),
        )
    else:
        await _reply_loc(update.message,
            f"⚠️ {escape_md(msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
    return True


async def _handle_pending_admin_copy_wallet(update, context, telegram_id, text):
    if not context.user_data.get("pending_admin_copy_wallet"):
        return False
    context.user_data.pop("pending_admin_copy_wallet", None)

    from src.nadobro.users.admin_service import is_admin as check_admin, add_copy_trader
    if not check_admin(telegram_id):
        await _reply_loc(update.message, "⚠️ Admin access required\\.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=persistent_menu_kb())
        return True

    import re
    parts = text.strip().split(None, 1)
    wallet = parts[0]
    label = parts[1] if len(parts) > 1 else wallet[:10]

    if not re.match(r'^0x[0-9a-fA-F]{40}$', wallet):
        await _reply_loc(update.message,
            "⚠️ Invalid wallet address\\. Must be 42 characters starting with `0x`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=persistent_menu_kb(),
        )
        return True

    ok, msg = add_copy_trader(telegram_id, wallet, label=label, is_curated=True)
    prefix = "✅" if ok else "⚠️"
    await _reply_loc(update.message,
        f"{prefix} {escape_md(msg)}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=persistent_menu_kb(),
    )
    return True


def _get_user_settings(telegram_id: int, context: CallbackContext) -> dict:
    from src.nadobro.handlers import shared_get_user_settings
    return shared_get_user_settings(telegram_id, context)
