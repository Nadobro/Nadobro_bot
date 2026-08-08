import logging
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackContext
from telegram.constants import ParseMode, ChatAction
from src.nadobro.i18n import language_context, get_user_language, localize_text, localize_markup, get_active_language, _ACTIVE_LANG
from src.nadobro.handlers.formatters import (
    escape_md, fmt_alert_menu_intro, fmt_alert_product_prompt, fmt_alert_target_prompt,
    fmt_close_all_confirm, fmt_dashboard_home, fmt_mode_view, fmt_positions,
    fmt_trade_preview, fmt_trade_result,
    fmt_wallet_balance_card, fmt_wallet_balance_error, fmt_wallet_connect_card,
    fmt_wallet_info, fmt_alerts, fmt_portfolio, fmt_wallet_revoke_steps_card,
    fmt_settings, fmt_help, fmt_price, fmt_points_dashboard,
    fmt_trade_history, fmt_analytics, fmt_strategy_hub_intro,
    fmt_referral_dashboard, fmt_getting_started,
)
from src.nadobro.handlers.keyboards import (
    persistent_menu_kb, trade_product_kb, trade_size_kb, trade_leverage_kb,
    trade_confirm_kb, positions_kb, wallet_kb, wallet_kb_not_linked, wallet_revoke_confirm_kb, alerts_kb,
    alert_product_kb, alert_condition_kb, alert_delete_kb, settings_kb, settings_leverage_kb,
    settings_slippage_kb, settings_language_kb, close_product_kb, confirm_close_all_kb, back_kb,
    risk_profile_kb, strategy_hub_kb, strategy_action_kb, strategy_product_picker_kb,
    onboarding_language_kb,
    referral_kb,
    mode_kb,     home_card_kb, status_kb, portfolio_kb, portfolio_history_kb, portfolio_analytics_kb,
    onboarding_accept_tos_kb, getting_started_kb,
    copy_hub_kb, copy_trader_preview_kb, copy_budget_kb, copy_risk_kb,
    copy_leverage_kb, copy_confirm_kb, copy_dashboard_kb, copy_admin_menu_kb,
)
from src.nadobro.users.points_ui import points_scope_kb
from src.nadobro.handlers.trade_card import handle_trade_card_callback, open_trade_card_from_callback
from src.nadobro.handlers.render_utils import plain_text_fallback
from src.nadobro.handlers.wallet_view import build_wallet_view_payload
from src.nadobro.handlers.home_card import (
    build_home_card_text_async,
    build_positions_view,
)
from src.nadobro.handlers.commands import build_status_dashboard_parts
from src.nadobro.handlers.state_reset import clear_pending_user_state
from src.nadobro.users.user_service import (
    get_or_create_user, get_user_nado_client, get_user_readonly_client, get_user_wallet_info,
    switch_network, get_user, remove_user_private_key, ensure_active_wallet_ready, update_user_language,
)
from src.nadobro.trading.trade_service import (
    execute_market_order, execute_limit_order, close_position,
    close_all_positions, get_trade_history, get_trade_analytics,
)
from src.nadobro.notify.alert_service import create_alert, get_user_alerts, delete_alert
from src.nadobro.users.admin_service import is_trading_paused, is_admin
from src.nadobro.strategy.bot_runtime import stop_user_bot, get_user_bot_status
from src.nadobro.users.settings_service import get_user_settings, update_user_settings
from src.nadobro.users.points_service import (
    get_points_dashboard,
    relay_option_reply_to_lowiqpts,
    relay_user_reply_to_lowiqpts,
    request_points_refresh,
)
from src.nadobro.users.referral_service import (
    auto_generate_referral_code,
    get_referral_dashboard,
    get_user_referral_code,
    MAX_CODE_LEN,
    MIN_CODE_LEN,
)
from src.nadobro.strategy.strategy_pending_input import persist_strategy_pending_input
from src.nadobro.users.onboarding_service import (
    get_resume_step,
    evaluate_readiness,
    set_new_onboarding_language,
    set_new_onboarding_tos_accepted,
    is_new_onboarding_complete,
    get_new_onboarding_state,
)
from src.nadobro.config import (
    PRODUCTS,
    get_dn_pair,
    get_dn_products,
    get_perp_products,
    get_product_id,
    get_product_max_leverage,
    get_product_name,
    get_spot_product_id,
    list_volume_spot_product_names,
    normalize_volume_spot_symbol,
)
from src.nadobro.core.async_utils import (
    fire_and_forget,
    run_blocking,
    run_blocking_sdk,
    run_blocking_sdk_capped,
)
from src.nadobro.core.perf import timed_metric, log_slow
from src.nadobro.trading.trading_readiness import check_trading_readiness

logger = logging.getLogger(__name__)


async def _show_trading_readiness_block(query, readiness) -> None:
    if readiness.code == "onboarding_incomplete":
        await _edit_loc(query,
            "⚠️ Complete setup first (language + accept terms).",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶ Complete setup", callback_data="onboarding:resume")],
                [InlineKeyboardButton("Exit", callback_data="nav:main")],
            ]),
        )
        return
    await _edit_loc(query,
        f"⚠️ {escape_md(readiness.reason)}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=back_kb(),
    )












async def _edit_loc(query, text, parse_mode=None, reply_markup=None, **fmt):
    lang = get_active_language()
    localized = localize_text(text, lang)
    if fmt:
        try:
            localized = localized.format(**fmt)
        except (KeyError, ValueError) as e:
            import logging as _logging
            _logging.getLogger("nadobro").warning(
                "_edit_loc template error key=%r lang=%r err=%r", text[:60], lang, e
            )
            localized = text.format(**fmt)
    kwargs = {}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    if reply_markup is not None:
        kwargs["reply_markup"] = localize_markup(reply_markup, lang)
    try:
        return await query.edit_message_text(localized, **kwargs)
    except BadRequest as e:
        msg = str(e)
        if "Message is not modified" in msg:
            return
        # Telegram refuses to edit_message_text when the source is a photo/media
        # card with no text (e.g. the LOWIQPTS report image, the home photo card).
        # Send a new message instead so navigation still works.
        if (
            "no text in the message to edit" in msg
            or "message can't be edited" in msg.lower()
            or "message to edit not found" in msg.lower()
        ):
            try:
                return await query.message.reply_text(localized, **kwargs)
            except BadRequest as e_send:
                if "Can't parse entities" in str(e_send) and kwargs.get("parse_mode") in (ParseMode.MARKDOWN_V2, ParseMode.HTML):
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs.pop("parse_mode", None)
                    return await query.message.reply_text(plain_text_fallback(localized), **fallback_kwargs)
                raise
        if "Can't parse entities" in msg and kwargs.get("parse_mode") in (ParseMode.MARKDOWN_V2, ParseMode.HTML):
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("parse_mode", None)
            try:
                return await query.edit_message_text(plain_text_fallback(localized), **fallback_kwargs)
            except BadRequest as e2:
                if "Message is not modified" in str(e2):
                    return
                raise
        raise


async def _send_typing(query) -> None:
    """Best-effort typing indicator. Cosmetic — never block a tap on it."""
    try:
        await query.message.chat.send_action(ChatAction.TYPING)
    except Exception as exc:  # noqa: BLE001 - purely cosmetic
        logger.debug("send_action(TYPING) failed: %s", exc)


async def handle_callback(update: Update, context: CallbackContext):
    started = time.perf_counter()
    query = update.callback_query
    data = query.data
    if data in ("market:view", "nav:market_radar", "market:radar", "home:market_radar"):
        data = "points:view"
    telegram_id = query.from_user.id

    with language_context(get_user_language(telegram_id)):
      return await _handle_callback_inner(update, context, query, data, telegram_id, started)


# Per-chat interaction sequence. Bumped on EVERY callback so background
# message-edit jobs (portfolio refresh) can detect that the user has since
# navigated elsewhere and must not clobber the current screen.
_CB_SEQ: dict[int, int] = {}


def interaction_seq(chat_id: int) -> int:
    return _CB_SEQ.get(int(chat_id), 0)


async def _handle_callback_inner(update, context, query, data, telegram_id, started):
    _seq_chat = getattr(getattr(query, "message", None), "chat_id", None) or int(telegram_id)
    _CB_SEQ[int(_seq_chat)] = _CB_SEQ.get(int(_seq_chat), 0) + 1
    try:
        # PERF: the callback query is already answered by ``with_callback_ack``
        # BEFORE the per-user serialization lock (main.py wires it outside
        # ``with_user_serialized``), so the spinner clears immediately and this
        # handler doesn't pay a Telegram round-trip before doing real work.
        # ``points:cancel`` is excluded there and answers itself with show_alert.
        #
        # The typing indicator is cosmetic and costs another round-trip: fire it
        # and move on. Awaiting it added ~1 RTT to every single tap.
        fire_and_forget(_send_typing(query))

        if data.startswith("onb:"):
            await _handle_onb_new(query, data, telegram_id, context)
        elif data.startswith("vault:"):
            from src.nadobro.handlers.vault_handler import handle_vault_callback

            await handle_vault_callback(query, context)
        elif data.startswith("resources:"):
            from src.nadobro.handlers.resources_handler import handle_resources_callback

            await handle_resources_callback(query, context)
        elif data.startswith("nav:"):
            await _handle_nav(query, data, telegram_id, context)
        elif data.startswith("card:trade:"):
            await handle_trade_card_callback(update, context, telegram_id, data)
        elif data.startswith("onboarding:"):
            await _handle_onboarding(query, data, telegram_id, context)
        elif data.startswith("trade:"):
            await _handle_trade(query, data, telegram_id, context)
        elif data.startswith("product:"):
            await _handle_product(query, data, telegram_id, context)
        elif data.startswith("size:"):
            await _handle_size(query, data, telegram_id, context)
        elif data.startswith("leverage:"):
            await _handle_leverage(query, data, telegram_id, context)
        elif data.startswith("exec_trade:"):
            await _handle_exec_trade(query, data, telegram_id, context)
        elif data == "cancel_trade":
            context.user_data.pop("pending_trade", None)
            await _show_dashboard(query, telegram_id)
        elif data.startswith("pos:"):
            await _handle_positions(query, data, telegram_id, context)
        elif data.startswith("portfolio:"):
            await _handle_portfolio(query, data, telegram_id)
        elif data.startswith("status:"):
            await _handle_status_callback(query, data, telegram_id)
        elif data.startswith("wallet:"):
            await _handle_wallet(query, data, telegram_id, context)
        elif data.startswith("points:"):
            await _handle_points(query, data, telegram_id, context)
        elif data.startswith("refer:"):
            await _handle_referrals(query, data, telegram_id, context)
        elif data.startswith("alert:"):
            await _handle_alert(query, data, telegram_id, context)
        elif data.startswith("settings:"):
            await _handle_settings(query, data, telegram_id, context)
        elif data.startswith("strategy:"):
            await _handle_strategy(query, data, context, telegram_id)
        elif data.startswith("copy:"):
            await _handle_copy(query, data, context, telegram_id)
        elif data.startswith("bro:"):
            await _handle_bro(query, data, telegram_id, context)
        elif data.startswith("howl:"):
            await _handle_howl(query, data, telegram_id, context)
        elif data.startswith("desk:"):
            from src.nadobro.handlers.desk_handler import handle_desk_callback

            await handle_desk_callback(query, data, telegram_id, context)
        elif data == "home:mode":
            user = get_user(telegram_id)
            current_network = user.network_mode.value if user else "testnet"
            network_label = "🧪 TESTNET" if current_network == "testnet" else "🌐 MAINNET"
            await _edit_loc(query,
                "🌐 *Execution Mode Control*\n\nCurrent Mode: *{label}*\n\nSwitch mode below:",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=mode_kb(current_network),
                label=escape_md(network_label),
            )
        elif data.startswith("mode:"):
            await _handle_mode(query, data, telegram_id, context)
        elif data.startswith("mm:"):
            await _handle_mm_dashboard(query, data, telegram_id)
        else:
            await _edit_loc(query,
                "Unknown action\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=back_kb(),
            )
    except BadRequest as e:
        # Harmless: refresh/navigation edited the message to identical text+keyboard.
        if "Message is not modified" in str(e):
            return
        logger.error(f"Callback BadRequest for '{data}': {e}", exc_info=True)
        try:
            await _edit_loc(query,
                "⚠️ An error occurred\\. Please try again\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=back_kb(),
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Callback error for '{data}': {e}", exc_info=True)
        try:
            await _edit_loc(query,
                "⚠️ An error occurred\\. Please try again\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=back_kb(),
            )
        except Exception:
            pass
    finally:
        log_slow("callback.total", threshold_ms=800.0, started_at=started)


# New onboarding (language → ToS) message text
_ONB_WELCOME_LANG_MSG = """Welcome to Nadobro 👋

Trade perps on Nado straight from Telegram. Type the trade, tap to confirm, done. Automation, portfolio, and AI are all here too.

Pick your language:"""

_ONB_WELCOME_CARD = """🔥 You're in.

Tapping *"Let's Get It"* means you're good with the Terms of Use & Privacy Policy.

🔐 How it works:
We spin up a secure 1CT signing key for your account. Your main wallet keys are never touched. Revoke whenever you want.

Ready?"""

async def _handle_onb_new(query, data, telegram_id, context):
    if data == "onb:accept_tos":
        set_new_onboarding_tos_accepted(telegram_id)
        await _edit_loc(
            query,
            fmt_getting_started(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=getting_started_kb(),
        )
        return
    if data.startswith("onb:lang:"):
        parts = data.split(":")
        if len(parts) < 3:
            return
        lang = parts[2]
        set_new_onboarding_language(telegram_id, lang)
        update_user_language(telegram_id, lang, source="onboarding")
        _ACTIVE_LANG.set(lang)
        await _edit_loc(query, _ONB_WELCOME_CARD,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=onboarding_accept_tos_kb(),
        )


async def _show_dashboard(query, telegram_id):
    await _edit_loc(query, await build_home_card_text_async(telegram_id),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=home_card_kb(),
    )


async def _handle_mode(query, data, telegram_id, context=None):
    parts = data.split(":")
    target_network = parts[1] if len(parts) > 1 else ""
    if target_network not in ("testnet", "mainnet"):
        return

    user = await run_blocking(get_user, telegram_id)
    current_network = user.network_mode.value if user else "testnet"

    if target_network == current_network:
        network_label = "🧪 TESTNET" if current_network == "testnet" else "🌐 MAINNET"
        try:
            await _edit_loc(query,
                "🌐 *Execution Mode Control*\n\nAlready on *{label}*\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=mode_kb(current_network),
                label=escape_md(network_label),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        return

    success, result_msg = await run_blocking(switch_network, telegram_id, target_network)
    if success:
        # A mode switch invalidates any in-flight confirmation flow (pending
        # text trade / close-all / trade card) — a "confirm" typed after the
        # switch must never execute a preview built against the other network.
        clear_pending_user_state(context, telegram_id)
        network_label = "🧪 TESTNET" if target_network == "testnet" else "🌐 MAINNET"
        await _edit_loc(query,
            "✅ *Switched to {label}*\n\n{msg}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=mode_kb(target_network),
            label=escape_md(network_label),
            msg=escape_md(result_msg),
        )
    else:
        await _edit_loc(query,
            "❌ {msg}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=mode_kb(current_network),
            msg=escape_md(result_msg),
        )


async def _handle_nav(query, data, telegram_id, context=None):
    target = data.split(":", 1)[1] if ":" in data else "main"

    clear_pending_user_state(context, telegram_id)

    user = await run_blocking(get_user, telegram_id)
    network = user.network_mode.value if user else "mainnet"

    if target in ("main", "refresh"):
        await _show_dashboard(query, telegram_id)
    elif target == "help":
        try:
            await _edit_loc(query, fmt_help(),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=back_kb(),
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            raise
    elif target == "quick_start":
        await _handle_onboarding(query, "onboarding:resume", telegram_id, context)
    elif target == "strategy_hub":
        await _edit_loc(query,
            fmt_strategy_hub_intro(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=strategy_hub_kb(),
        )
    elif target == "mode":
        user = await run_blocking(get_user, telegram_id)
        current_network = user.network_mode.value if user else "testnet"
        network_label = "🧪 TESTNET" if current_network == "testnet" else "🌐 MAINNET"
        await _edit_loc(query,
            "🌐 *Execution Mode Control*\n\nCurrent Mode: *{label}*\n\nSwitch mode below:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=mode_kb(current_network),
            label=escape_md(network_label),
        )
    elif target == "trade" and context is not None:
        if not is_new_onboarding_complete(telegram_id):
            await _edit_loc(query,
                "⚠️ Complete setup first (language + accept terms).",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶ Complete setup", callback_data="onboarding:resume")],
                    [InlineKeyboardButton("Exit", callback_data="nav:main")],
                ]),
            )
            return
        if await open_trade_card_from_callback(query, context, telegram_id):
            return
    elif target == "ask_nado" and context is not None:
        context.user_data["pending_question"] = True
        await _edit_loc(query,
            "🧠 *Ask NadoBro AI Console*\n\n"
            "Ask me anything about Nado \\(docs, dev docs, API, website, X updates, troubleshooting\\)\\!\n\n"
            "Examples:\n"
            "  • `What is unified margin?`\n"
            "  • `How do liquidations work?`\n"
            "  • `What order types are available?`\n"
            "  • `How do I deposit funds?`\n"
            "  • `What changed recently on Nado X?`\n"
            "  • `How do I sign gateway API requests?`\n"
            "  • `What is the NLP vault?`\n"
            "  • `What are the trading fees?`\n\n"
            "Type your question below:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_kb(),
        )
    elif target.startswith("strategy:"):
        await _handle_strategy(query, target, context, telegram_id)
    elif target.startswith("copy:"):
        await _handle_copy(query, target, context, telegram_id)
    elif target.startswith("bro:"):
        await _handle_bro(query, target, telegram_id, context)
    elif target.startswith("settings:"):
        await _handle_settings(query, target, telegram_id, context)
    elif target.startswith("alert:"):
        await _handle_alert(query, target, telegram_id, context)
    elif target.startswith("wallet:"):
        await _handle_wallet(query, target, telegram_id, context)
    elif target.startswith("portfolio:"):
        await _handle_portfolio(query, target, telegram_id)
    elif target.startswith("refer:"):
        await _handle_referrals(query, target, telegram_id, context)
    else:
        logger.warning("nav: unknown target=%r telegram_id=%s", target, telegram_id)
        await _edit_loc(query,
            "⚠️ That navigation link is not available\\. Try *🏠 Home* from the menu\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_kb(),
        )


async def _handle_referrals(query, data, telegram_id, context=None):
    action = data.split(":", 1)[1] if ":" in data else "view"
    user = await run_blocking(get_user, telegram_id)
    network = user.network_mode.value if user else "mainnet"

    notice: str | None = None

    if action == "claim":
        if context is not None:
            context.user_data["pending_referral_claim"] = True
        prompt = (
            "*🎟 Claim your custom referral code*\n\n"
            f"Type the code you want to own \\({MIN_CODE_LEN}\\-{MAX_CODE_LEN} characters, A\\-Z and 0\\-9\\)\\.\n"
            "Codes are *permanent once claimed* and unique across all of Nadobro\\.\n\n"
            "Send `cancel` to abort\\."
        )
        await _edit_loc(
            query,
            prompt,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=referral_kb(has_code=False),
        )
        return

    if action == "autogen":
        ok, msg, _row = await run_blocking(
            auto_generate_referral_code, telegram_id, network=network
        )
        notice = msg if not ok else None
        if context is not None:
            context.user_data.pop("pending_referral_claim", None)

    # Default + post-action: re-render the dashboard.
    payload = await run_blocking(get_referral_dashboard, telegram_id, network)
    body = fmt_referral_dashboard(payload)
    if notice:
        body = body + "\n\n" + f"⚠️ {escape_md(notice)}"
    await _edit_loc(
        query,
        body,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=referral_kb(has_code=bool(payload.get("has_code"))),
    )


async def _handle_trade(query, data, telegram_id, context):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    needs_wallet = action in ("long", "short", "limit_long", "limit_short")
    readiness = await run_blocking(
        check_trading_readiness,
        telegram_id,
        require_wallet=needs_wallet,
        block_when_paused=False,
    )
    if not readiness.ok:
        await _show_trading_readiness_block(query, readiness)
        return
    user = await run_blocking(get_user, telegram_id)
    network = user.network_mode.value if user else "mainnet"

    if action in ("long", "short"):
        action_label = "🟢 BUY / LONG" if action == "long" else "🔴 SELL / SHORT"
        await _edit_loc(query,
            "*{label}*\n\n{select_product}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_product_kb(action, network=network),
            label=escape_md(action_label),
            select_product=localize_text("Select a product:", get_active_language()),
        )
    elif action in ("limit_long", "limit_short"):
        context.user_data["pending_trade"] = {"action": action, "step": "product_select"}
        action_label = "LIMIT LONG" if action == "limit_long" else "LIMIT SHORT"
        await _edit_loc(query, 
            "*{label}*\n\n{select_product}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_product_kb(action, network=network),
            label=escape_md(action_label),
            select_product=localize_text("Select a product:", get_active_language()),
        )
    elif action == "close":
        await _edit_loc(query, 
            "*Close Position*\n\nSelect the product to close:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=close_product_kb(network=network),
        )
    elif action == "close_all":
        await _edit_loc(query, 
            "⚠️ *Close All Positions*\n\nAre you sure you want to close ALL open orders?",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=confirm_close_all_kb(),
        )



async def _handle_product(query, data, telegram_id, context):
    parts = data.split(":")
    if len(parts) < 3:
        return

    action = parts[1]
    product = parts[2]

    if action in ("limit_long", "limit_short"):
        context.user_data["pending_trade"] = {
            "action": action,
            "product": product,
            "step": "limit_input",
        }
        _lang = get_active_language()
        await _edit_loc(query, 
            "*{label} {product}*\n\n{enter_size_price}\n{example}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_kb(),
            label=escape_md(action.replace('_', ' ').upper()),
            product=escape_md(product),
            enter_size_price=localize_text("Enter size and price:", _lang),
            example=localize_text("Example: `0\\.01 95000`", _lang),
        )
        return

    action_label = "LONG" if action == "long" else "SHORT"
    await _edit_loc(query, 
        "*{label} {product}\\-PERP*\n\n{select_size}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=trade_size_kb(product, action),
        label=escape_md(action_label),
        product=escape_md(product),
        select_size=localize_text("Select trade size:", get_active_language()),
    )


async def _handle_size(query, data, telegram_id, context):
    parts = data.split(":")
    if len(parts) < 4:
        return

    action = parts[1]
    product = parts[2]
    size_str = parts[3]

    if size_str == "custom":
        context.user_data["pending_trade"] = {
            "action": action,
            "product": product,
            "step": "custom_size",
        }
        _lang = get_active_language()
        await _edit_loc(query, 
            "*{label} {product}\\-PERP*\n\n{prompt}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_kb(),
            label=escape_md(action.upper()),
            product=escape_md(product),
            prompt=localize_text("Type the trade size \\(e\\.g\\. `0\\.01`\\):", _lang),
        )
        return

    size = float(size_str)
    action_label = "LONG" if action == "long" else "SHORT"
    await _edit_loc(query, 
        "*{label} {size} {product}\\-PERP*\n\n{select_leverage}",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=trade_leverage_kb(product, action, size),
        label=escape_md(action_label),
        size=escape_md(str(size)),
        product=escape_md(product),
        select_leverage=localize_text("Select leverage:", get_active_language()),
    )


async def _handle_leverage(query, data, telegram_id, context):
    parts = data.split(":")
    if len(parts) < 5:
        return

    action = parts[1]
    product = parts[2]
    size = float(parts[3])
    leverage = int(parts[4])
    user = await run_blocking(get_user, telegram_id)
    network = user.network_mode.value if user else "mainnet"
    max_leverage = get_product_max_leverage(product, network=network)
    if leverage > max_leverage:
        await _edit_loc(query, 
            "⚠️ Max leverage for *{product}* is *{max_lev}x*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=trade_leverage_kb(product, action, size),
            product=escape_md(product),
            max_lev=escape_md(str(max_leverage)),
        )
        return

    price = 0
    try:
        client = await run_blocking(get_user_readonly_client, telegram_id)
        if client:
            user = await run_blocking(get_user, telegram_id)
            network = user.network_mode.value if user else "mainnet"
            pid = await run_blocking(get_product_id, product, network=network, client=client)
            if pid is not None:
                mp = await run_blocking(client.get_market_price, pid)
                price = mp.get("mid", 0)
    except Exception:
        pass

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
    await _edit_loc(query, 
        preview,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=trade_confirm_kb(),
    )


async def _handle_exec_trade(query, data, telegram_id, context):
    pending = context.user_data.get("pending_trade")
    if not pending:
        await _edit_loc(query, 
            "⚠️ No pending trade found\\. Please start again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_kb(),
        )
        return
    readiness = await run_blocking(check_trading_readiness, telegram_id)
    if not readiness.ok:
        await _show_trading_readiness_block(query, readiness)
        return

    action = pending.get("action", "long")
    product = pending.get("product", "BTC")
    size = pending.get("size", 0)
    leverage = pending.get("leverage", 1)
    slippage_pct = pending.get("slippage_pct", _get_user_settings(telegram_id, context).get("slippage", 1))

    context.user_data.pop("pending_trade", None)

    from src.nadobro.handlers.messages import execute_action_directly
    await execute_action_directly(query, context, telegram_id, {
        "type": "exec_trade_callback",
        "pending": {
            "action": action,
            "product": product,
            "size": size,
            "leverage": leverage,
            "slippage_pct": slippage_pct,
            "price": pending.get("price", 0),
        },
    })


async def _handle_positions(query, data, telegram_id, context):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "view"

    if action == "view":
        # CLICK-PATH-BLOCKING fix: build_positions_view makes two venue reads
        # (get_all_positions + get_all_market_prices). Route it to the SDK pool
        # with a wall-clock cap (matching the reply-keyboard path's
        # resolve_home_view) so it never queues behind strategy-cycle reads on
        # the shared 8-worker MISC pool and never makes a tap wait ~30s.
        with timed_metric("cb.positions.view"):
            msg, reply_markup = await run_blocking_sdk_capped(
                build_positions_view, telegram_id,
                timeout_seconds=6.0,
                default=("⏳ Refreshing positions… tap again in a sec\\.", back_kb()),
            )
        await _edit_loc(query,
            msg,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )

    elif action == "close" and len(parts) >= 3:
        product = parts[2]
        from src.nadobro.handlers.messages import execute_action_directly
        await execute_action_directly(query, context, telegram_id, {"type": "close_position", "product": product})

    elif action == "close_all":
        await _edit_loc(query, 
            fmt_close_all_confirm(),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=confirm_close_all_kb(),
        )

    elif action == "confirm_close_all":
        from src.nadobro.handlers.messages import execute_action_directly
        await execute_action_directly(query, context, telegram_id, {"type": "close_all"})


async def _handle_portfolio(query, data, telegram_id):
    """Shim: the portfolio domain lives in portfolio_handler.py (lazy import avoids a cycle)."""
    from src.nadobro.handlers.portfolio_handler import _handle_portfolio as _impl

    return await _impl(query, data, telegram_id)




async def _handle_status_callback(query, data: str, telegram_id: int):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "refresh"
    if action == "stop":
        # Instant ack so the card doesn't sit frozen while cleanup runs — the
        # stop path cancels resting orders + flattens on the venue, which can
        # take seconds even after scoping (and minutes under venue throttling).
        # Run it on the SDK pool (not the small misc pool the click path uses to
        # render cards) so other taps stay responsive while this works.
        try:
            with language_context(get_user_language(telegram_id)):
                await _edit_loc(
                    query,
                    "🛑 Stopping the strategy and closing open positions…",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
        ok, msg = await run_blocking_sdk(stop_user_bot, telegram_id, True)
        body, merged_kb = await build_status_dashboard_parts(telegram_id)
        prefix = "🛑" if ok else "⚠️"
        with language_context(get_user_language(telegram_id)):
            await _edit_loc(
                query,
                "{body}\n\n{prefix} {msg}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=merged_kb,
                body=body,
                prefix=prefix,
                msg=escape_md(msg),
            )
        return
    if action != "refresh":
        return
    body, merged_kb = await build_status_dashboard_parts(telegram_id)
    with language_context(get_user_language(telegram_id)):
        await _edit_loc(
            query,
            body,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=merged_kb,
        )


async def _handle_wallet(query, data, telegram_id, context):
    """Shim: the wallet domain lives in wallet_handler.py (lazy import avoids a cycle)."""
    from src.nadobro.handlers.wallet_handler import _handle_wallet as _impl

    return await _impl(query, data, telegram_id, context)




async def _handle_points(query, data, telegram_id, context):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "view"

    if action == "view":
        payload = await run_blocking(get_points_dashboard, telegram_id, "week")
        await _edit_loc(
            query,
            fmt_points_dashboard(payload),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=points_scope_kb(),
        )
        return

    if action == "scope":
        # Legacy keyboards used points:scope:*; relay is week-only.
        payload = await run_blocking(get_points_dashboard, telegram_id, "week")
        await _edit_loc(
            query,
            fmt_points_dashboard(payload),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=points_scope_kb(),
        )
        return

    if action == "cancel":
        relay_result = await relay_user_reply_to_lowiqpts(context, query.message.chat.id, "/cancel")
        if relay_result.get("cancelled"):
            try:
                await query.answer()
            except BadRequest:
                pass
            await _edit_loc(
                query,
                "✅ Points request closed\\. Tap *🏆 Refresh points* to start again\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=points_scope_kb(),
            )
            return
        if relay_result.get("handled") is False:
            try:
                await query.answer(
                    relay_result.get("error") or "No active LOWIQPTS request to close.",
                    show_alert=True,
                )
            except BadRequest:
                pass
            return
        try:
            await query.answer()
        except BadRequest:
            pass
        await _edit_loc(
            query,
            escape_md(relay_result.get("error", "Could not cancel LOWIQPTS request.")),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=points_scope_kb(),
        )
        return

    if action == "replyopt":
        raw_idx = parts[2] if len(parts) > 2 else "-1"
        try:
            option_index = int(raw_idx)
        except (TypeError, ValueError):
            option_index = -1
        relay_result = await relay_option_reply_to_lowiqpts(context, query.message.chat.id, option_index)
        if relay_result.get("ok"):
            return
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=str(relay_result.get("error", "Could not send selection."))[:180],
        )
        return

    if action == "refresh":
        result = await request_points_refresh(context, telegram_id, query.message.chat.id)
        if result.get("ok"):
            await _edit_loc(
                query,
                "⏳ Refresh requested\\. I will post your points update when LOWIQPTS replies\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=points_scope_kb(),
            )
            return
        await _edit_loc(
            query,
            escape_md(result.get("error", "Could not refresh points right now.")),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=points_scope_kb(),
        )
        return


async def _handle_alert(query, data, telegram_id, context):
    """Shim: the alerts domain lives in alerts_handler.py (lazy import avoids a cycle)."""
    from src.nadobro.handlers.alerts_handler import _handle_alert as _impl

    return await _impl(query, data, telegram_id, context)




async def _handle_settings(query, data, telegram_id, context):
    """Shim: the settings domain lives in settings_handler.py (lazy import avoids a cycle)."""
    from src.nadobro.handlers.settings_handler import _handle_settings as _impl

    return await _impl(query, data, telegram_id, context)




async def _handle_strategy(query, data, context, telegram_id):
    """Shim: the strategy domain lives in handlers/strategy_handler.py.

    Lazy import — strategy_handler imports shared utils back from this
    module at import time, so a top-level import here would cycle.
    """
    from src.nadobro.handlers.strategy_handler import _handle_strategy as _impl

    return await _impl(query, data, context, telegram_id)




async def _handle_bro(query, data, telegram_id, context):
    """Shim: the Bro AI domain lives in bro_handler.py (lazy import avoids a cycle)."""
    from src.nadobro.handlers.bro_handler import _handle_bro as _impl

    return await _impl(query, data, telegram_id, context)




async def _handle_howl(query, data, telegram_id, context):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"

    if action == "approve" and len(parts) >= 3:
        index = int(parts[2])
        from src.nadobro.llm.howl_service import approve_howl_suggestion, get_pending_howl, format_howl_message
        ok, msg = approve_howl_suggestion(telegram_id, network, index)
        pending = get_pending_howl(telegram_id, network)
        if pending:
            text = format_howl_message(pending)
            suggestions = pending.get("suggestions", [])
            pending_count = sum(1 for s in suggestions if s.get("status", "pending") == "pending")
            from src.nadobro.llm.howl_ui import howl_approval_kb
            # format_howl_message is HTML; escape_md-wrapping it both killed
            # the intended formatting AND (post-HTML) showed raw tags.
            from src.nadobro.utils.visual import esc as _esc
            await _edit_loc(query,
                f"{'✅' if ok else '⚠️'} {_esc(msg)}\n\n{text}",
                parse_mode=ParseMode.HTML,
                reply_markup=howl_approval_kb(len(suggestions)) if pending_count > 0 else back_kb("strategy:preview:bro"),
            )
        else:
            prefix = "✅" if ok else "⚠️"
            await _edit_loc(query, "{prefix} {msg}", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb("strategy:preview:bro"), prefix=prefix, msg=escape_md(msg))

    elif action == "reject" and len(parts) >= 3:
        index = int(parts[2])
        from src.nadobro.llm.howl_service import reject_howl_suggestion, get_pending_howl, format_howl_message
        ok, msg = reject_howl_suggestion(telegram_id, network, index)
        pending = get_pending_howl(telegram_id, network)
        if pending:
            text = format_howl_message(pending)
            suggestions = pending.get("suggestions", [])
            pending_count = sum(1 for s in suggestions if s.get("status", "pending") == "pending")
            from src.nadobro.llm.howl_ui import howl_approval_kb
            from src.nadobro.utils.visual import esc as _esc
            await _edit_loc(query,
                f"{'❌' if ok else '⚠️'} {_esc(msg)}\n\n{text}",
                parse_mode=ParseMode.HTML,
                reply_markup=howl_approval_kb(len(suggestions)) if pending_count > 0 else back_kb("strategy:preview:bro"),
            )
        else:
            prefix = "❌" if ok else "⚠️"
            await _edit_loc(query, "{prefix} {msg}", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb("strategy:preview:bro"), prefix=prefix, msg=escape_md(msg))

    elif action == "approve_all":
        from src.nadobro.llm.howl_service import approve_howl_suggestion, get_pending_howl
        pending = get_pending_howl(telegram_id, network)
        if pending:
            results = []
            for i, s in enumerate(pending.get("suggestions", [])):
                if s.get("status", "pending") == "pending":
                    ok, msg = approve_howl_suggestion(telegram_id, network, i)
                    results.append(f"{'✅' if ok else '⚠️'} {msg}")
            summary = "\n".join(results) if results else "No pending suggestions"
            await _edit_loc(query, escape_md(summary), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb("strategy:preview:bro"))
        else:
            await _edit_loc(query, "No pending HOWL suggestions\\.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb("strategy:preview:bro"))

    elif action == "dismiss":
        from src.nadobro.llm.howl_service import dismiss_all_howl
        dismiss_all_howl(telegram_id, network)
        await _edit_loc(query, "🐺 HOWL suggestions dismissed\\.", parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back_kb("strategy:preview:bro"))


def _get_user_settings(telegram_id: int, context: CallbackContext) -> dict:
    from src.nadobro.handlers import shared_get_user_settings
    return shared_get_user_settings(telegram_id, context)
























async def _handle_mm_dashboard(query, data: str, telegram_id: int):
    """Phase 3 callback dispatcher for the live MM dashboard.

    ``mm:status:refresh`` re-renders the /mm_status snapshot in place.
    ``mm:fills`` shows the most recent fills.
    """
    from src.nadobro.handlers.commands import build_mm_status_text, build_mm_fills_text
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "status":
        text, is_active = await run_blocking(build_mm_status_text, telegram_id)
        body = escape_md(text)
        markup = None
        if is_active:
            from src.nadobro.handlers.commands import _mm_dashboard_keyboard
            markup = _mm_dashboard_keyboard()
        await _edit_loc(
            query,
            f"📊 *MM Status*\n\n{body}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=markup,
        )
        return
    if action == "fills":
        text = await run_blocking(build_mm_fills_text, telegram_id, 10)
        _, is_mm_active = await run_blocking(build_mm_status_text, telegram_id)
        from src.nadobro.handlers.commands import _mm_dashboard_keyboard

        markup = (
            _mm_dashboard_keyboard()
            if is_mm_active
            else InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📊 Refresh MM board",
                            callback_data="mm:status:refresh",
                        ),
                    ],
                    [InlineKeyboardButton("🏠 Home", callback_data="nav:main")],
                ]
            )
        )
        # Use a fenced block so the columnar fills render evenly.
        await _edit_loc(
            query,
            f"```\n{text}\n```",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=markup,
        )
        return






async def _handle_onboarding(query, data, telegram_id, context):
    """Resume new onboarding (language → ToS). Only handles onboarding:resume."""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action != "resume":
        return
    if is_new_onboarding_complete(telegram_id):
        await _show_dashboard(query, telegram_id)
        return
    state = get_new_onboarding_state(telegram_id)
    if not state.get("language"):
        await _edit_loc(query, 
            _ONB_WELCOME_LANG_MSG,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=onboarding_language_kb(),
        )
    else:
        await _edit_loc(query, 
            _ONB_WELCOME_CARD,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=onboarding_accept_tos_kb(),
        )


async def _delete_message_later(query, chat_id: int, message_id: int, delay_seconds: int = 30):
    await asyncio.sleep(delay_seconds)
    try:
        await query.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _handle_copy(query, data, context, telegram_id):
    """Shim: the copy-trading domain lives in copy_handler.py (lazy import avoids a cycle)."""
    from src.nadobro.handlers.copy_handler import _handle_copy as _impl

    return await _impl(query, data, context, telegram_id)


