import re
import logging

from telegram.constants import ParseMode
from src.nadobro.handlers.keyboards import confirm_close_all_kb
from telegram.ext import CallbackContext
from telegram.error import BadRequest

from src.nadobro.i18n import localize_text, get_active_language
from src.nadobro.handlers.render_utils import plain_text_fallback
from src.nadobro.handlers.network_guard import (
    active_network,
    network_of,
    network_unknown_message,
    normalize_network,
    refuse_message,
    same_network,
)
from src.nadobro.handlers.formatters import (
    escape_md,
    build_trade_preview_text,
    fmt_close_all_confirm,
    fmt_bracket_result,
    fmt_limit_close_result,
    humanize_exchange_error,
)
from src.nadobro.handlers.intent_parser import parse_trade_intent, parse_position_management_intent
from src.nadobro.users.admin_service import is_trading_paused
from src.nadobro.users.onboarding_service import get_resume_step
from src.nadobro.users.settings_service import get_user_settings
from src.nadobro.trading.trade_service import (
    apply_tp_sl_to_open_position,
    close_position,
    execute_market_order,
    execute_limit_order,
    get_account_and_performance_snapshot,
    limit_close_position,
)
from src.nadobro.users.user_service import ensure_active_wallet_ready, get_user_readonly_client, get_user
from src.nadobro.config import get_product_id, get_product_max_leverage
from src.nadobro.venue.nado_tooling_service import tooling_enabled
from src.nadobro.core.async_utils import run_blocking
from src.nadobro.trading.text_trade_pending import (
    clear_text_trade_pending,
    load_text_trade_pending,
    persist_text_close_all_pending,
    persist_text_trade_pending,
)

PENDING_TEXT_TRADE_KEY = "pending_text_trade"
logger = logging.getLogger(__name__)


async def _reply_md_safe(message, text: str) -> None:
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    except BadRequest as e:
        if "Can't parse entities" not in str(e):
            raise
        logger.warning("intent_handlers: markdown parse fallback triggered: %s", e)
        await message.reply_text(plain_text_fallback(text))


def _auto_execute_requested(text: str) -> bool:
    return bool(re.search(r"\b(now|execute|confirm|place)\b", text.lower()))


def _settings_for_user(telegram_id: int) -> dict:
    _, settings = get_user_settings(telegram_id)
    return settings


def _enrich_trade_payload(
    telegram_id: int, payload: dict, settings: dict, *, network: str | None = None,
) -> dict:
    """Price and clamp a parsed trade, and stamp the network it is built on.

    ``network``: the network the caller parsed the intent on. The stamp is
    never a guess (PREVIEW-NETWORK-BIND): without a caller network it is read
    here, and when that read fails the payload carries no stamp, so a typed
    "confirm" fails closed. It used to fall back to "mainnet", and since the
    executor is handed the stamp explicitly, one failed re-read placed a
    TESTNET user's auto-executed ("... now") trade on MAINNET."""
    result = dict(payload)
    network = normalize_network(network)
    if network is None:
        try:
            network = network_of(get_user(telegram_id))
        except Exception:  # policy: degrade-ok(unstamped: a typed confirm fails closed; lookups use the catalog default)
            logger.warning("text trade: could not read the network for uid=%s; leaving the preview unstamped", telegram_id)
            network = None
    requested_leverage = int(payload.get("leverage") or settings.get("default_leverage", 3))
    product = str(result.get("product") or "BTC")
    max_leverage = get_product_max_leverage(product, network=network)
    result["leverage"] = max(1, min(requested_leverage, max_leverage))
    result["slippage_pct"] = float(settings.get("slippage", 1))

    price = 0.0
    try:
        if result.get("order_type") == "limit":
            price = float(result.get("limit_price") or 0)
        else:
            client = get_user_readonly_client(telegram_id, network=network)
            if client:
                pid = get_product_id(result.get("product", "BTC"), network=network, client=client)
                if pid is not None:
                    mp = client.get_market_price(pid)
                    price = float(mp.get("mid", 0) or 0)
    except Exception:
        price = 0.0

    size = float(result.get("size") or 0)
    leverage = int(result.get("leverage") or 1)
    est_margin = (size * price) / leverage if leverage > 0 and price else None
    result["price"] = price
    result["est_margin"] = est_margin
    # Stamp the network the preview was built against so a "confirm" typed
    # after a testnet<->mainnet switch is rejected instead of executing the
    # trade on the other network (the payload may hydrate from bot_state in a
    # different process, where the handler-level state reset can't reach).
    result["network"] = network
    return result


def _preview_text(payload: dict) -> str:
    direction = payload.get("direction", "long")
    order_type = payload.get("order_type", "market")
    product = payload.get("product", "BTC")
    size = float(payload.get("size") or 0)
    leverage = int(payload.get("leverage") or 1)
    price = float(payload.get("price") or 0)
    est_margin = payload.get("est_margin")
    action = "limit_long" if (order_type == "limit" and direction == "long") else "limit_short" if order_type == "limit" else direction
    return build_trade_preview_text(
        action=action,
        product=product,
        size=size,
        price=price,
        leverage=leverage,
        est_margin=est_margin,
        tp=payload.get("tp"),
        sl=payload.get("sl"),
    )


def _execute_trade_payload(telegram_id: int, payload: dict, **kwargs) -> dict:
    direction = payload.get("direction", "long")
    order_type = payload.get("order_type", "market")
    product = payload.get("product", "BTC")
    size = float(payload.get("size") or 0)
    leverage = int(payload.get("leverage") or 1)
    slippage_pct = float(payload.get("slippage_pct") or 1)

    # Execute on the network the preview was built on (PREVIEW-NETWORK-BIND).
    network = payload.get("network")
    if order_type == "limit":
        return execute_limit_order(
            telegram_id,
            product,
            size,
            float(payload.get("limit_price") or 0),
            is_long=(direction == "long"),
            leverage=leverage,
            tp_price=payload.get("tp"),
            sl_price=payload.get("sl"),
            network=network,
        )
    return execute_market_order(
        telegram_id,
        product,
        size,
        is_long=(direction == "long"),
        leverage=leverage,
        slippage_pct=slippage_pct,
        tp_price=payload.get("tp"),
        sl_price=payload.get("sl"),
        network=network,
    )


async def handle_pending_text_trade_confirmation(update, context: CallbackContext, telegram_id: int, text: str) -> bool:
    pending = context.user_data.get(PENDING_TEXT_TRADE_KEY)
    if not pending:
        pending = await run_blocking(load_text_trade_pending, int(telegram_id))
        if pending:
            context.user_data[PENDING_TEXT_TRADE_KEY] = pending
    if not pending:
        return False

    lang = get_active_language()
    normalized = text.strip().lower()
    if normalized in ("cancel", "no", "n", "abort"):
        context.user_data.pop(PENDING_TEXT_TRADE_KEY, None)
        await run_blocking(clear_text_trade_pending, int(telegram_id))
        await _reply_md_safe(update.message, localize_text("❌ Trade cancelled\\.", lang))
        return True

    if normalized not in ("confirm", "yes", "y", "execute", "place"):
        await _reply_md_safe(
            update.message,
            localize_text("Type `confirm` to execute this trade or `cancel` to discard it\\.", lang),
        )
        return True

    # PREVIEW-NETWORK-BIND: the preview was priced and validated on the network
    # stamped into it. If the user is on another network now (or the stamp is
    # missing: fail closed), discard it. Never execute a testnet preview as a
    # real mainnet order, or the other way round. The payload may have been
    # hydrated from bot_state in a process the switch's clean-up never reached.
    pending_network = pending.get("network")
    current_network = await active_network(telegram_id)
    if not same_network(pending_network, current_network):
        context.user_data.pop(PENDING_TEXT_TRADE_KEY, None)
        await run_blocking(clear_text_trade_pending, int(telegram_id))
        await refuse_message(
            update.message,
            kind="text_trade",
            notice="trade",
            built=pending_network,
            current=current_network,
            telegram_id=telegram_id,
        )
        return True

    if is_trading_paused():
        await _reply_md_safe(update.message, localize_text("⏸ Trading is temporarily paused by admin\\.", lang))
        return True
    wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ready:
        await _reply_md_safe(update.message, f"⚠️ {escape_md(wallet_msg)}")
        return True

    context.user_data.pop(PENDING_TEXT_TRADE_KEY, None)
    await run_blocking(clear_text_trade_pending, int(telegram_id))
    from src.nadobro.handlers.messages import execute_action_directly
    await execute_action_directly(update, context, telegram_id, {"type": "execute_trade", "payload": pending})
    return True


async def handle_trade_intent_message(update, context: CallbackContext, telegram_id: int, text: str) -> bool:
    normalized_text = str(text or "").strip().lower()
    if tooling_enabled() and any(token in normalized_text for token in ("account snapshot", "performance snapshot", "portfolio snapshot")):
        snap = get_account_and_performance_snapshot(telegram_id, prefer_cli=True)
        perf = snap.get("performance") or {}
        account_src = str(snap.get("account_source") or "sdk").upper()
        account_ok = bool(snap.get("success"))
        total_trades = int(perf.get("total_trades") or 0)
        win_rate = float(perf.get("win_rate") or 0.0)
        total_pnl = float(perf.get("total_pnl") or 0.0)
        total_volume = float(perf.get("total_volume") or 0.0)
        pnl_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
        msg = (
            "📈 *Account & Performance Snapshot*\\n\\n"
            f"• Account source: *{escape_md(account_src)}*\\n"
            f"• Account status: *{escape_md('OK' if account_ok else 'UNAVAILABLE')}*\\n"
            f"• Total trades: *{escape_md(str(total_trades))}*\\n"
            f"• Win rate: *{escape_md(f'{win_rate:.1f}%')}*\\n"
            f"• Total PnL: *{escape_md(pnl_str)}*\\n"
            f"• Total volume: *{escape_md(f'${total_volume:,.2f}')}*"
        )
        if not account_ok and snap.get("account_error"):
            msg += f"\\n\\n⚠️ {escape_md(str(snap.get('account_error')))}"
        await _reply_md_safe(update.message, msg)
        return True

    # The preview-only TWAP/trigger scaffold that used to live here was
    # superseded by the Desk pipeline (handlers/desk_handler.py), which parses
    # the same phrasing AND executes behind a confirm card. messages.py routes
    # Desk before this handler, so those messages never reach this point.

    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    intent = parse_trade_intent(text, network=network)
    if not intent:
        return False
    logger.info(
        "trade_intent_detected telegram_id=%s direction=%s product=%s order_type=%s missing=%s",
        telegram_id,
        intent.get("direction"),
        intent.get("product"),
        intent.get("order_type"),
        intent.get("missing"),
    )

    lang = get_active_language()
    step = get_resume_step(telegram_id)
    if step != "complete":
        await _reply_md_safe(
            update.message,
            localize_text("⚠️ Setup incomplete\\. Resume onboarding at *{step}*\\.", lang).format(step=escape_md(step.upper())),
        )
        return True

    wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ready:
        await _reply_md_safe(update.message, f"⚠️ {escape_md(wallet_msg)}")
        return True

    if intent.get("missing"):
        missing = ", ".join(intent["missing"])
        await _reply_md_safe(
            update.message,
            localize_text("I can place this trade from text, but I need a bit more info\\.", lang) + "\n\n"
            f"Missing: *{escape_md(missing)}*\n\n"
            "Example: `buy 0\\.01 BTC 5x market` or `sell 0\\.2 ETH limit 3200`",
        )
        return True

    settings = _settings_for_user(telegram_id)
    # Stamped with the network the intent was parsed on (one read for the whole
    # preview), never re-read and never guessed (PREVIEW-NETWORK-BIND).
    payload = _enrich_trade_payload(telegram_id, intent, settings, network=network_of(user))
    if payload.get("network") is None:
        # No network could be read: neither preview nor auto-execute a trade
        # that would have to be bound to a guess.
        await network_unknown_message(update.message, kind="text_trade", telegram_id=telegram_id)
        return True
    preview = _preview_text(payload)

    if _auto_execute_requested(text):
        if is_trading_paused():
            await _reply_md_safe(update.message, localize_text("⏸ Trading is temporarily paused by admin\\.", lang))
            return True
        from src.nadobro.handlers.messages import execute_action_directly
        await execute_action_directly(update, context, telegram_id, {"type": "execute_trade", "payload": payload})
        return True

    context.user_data[PENDING_TEXT_TRADE_KEY] = payload
    try:
        await run_blocking(persist_text_trade_pending, int(telegram_id), payload)
    except Exception:
        logger.exception("Failed to persist pending text trade for uid=%s", telegram_id)
    confirm_prompt = localize_text("Type `confirm` to execute or `cancel` to discard\\.", lang)
    await _reply_md_safe(update.message, f"{preview}\n\n{confirm_prompt}")
    return True


async def handle_position_management_intent(update, context: CallbackContext, telegram_id: int, text: str) -> bool:
    """NL: TP/SL, market close, limit close, close all — must run before trade intents."""
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    client = get_user_readonly_client(telegram_id)
    intent = parse_position_management_intent(text, network=network, client=client)
    if not intent:
        return False

    logger.info(
        "position_management_intent telegram_id=%s action=%s product=%s",
        telegram_id,
        intent.get("action"),
        intent.get("product"),
    )

    lang = get_active_language()
    step = get_resume_step(telegram_id)
    if step != "complete":
        await _reply_md_safe(
            update.message,
            localize_text("⚠️ Setup incomplete\\. Resume onboarding at *{step}*\\.", lang).format(step=escape_md(step.upper())),
        )
        return True

    wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ready:
        await _reply_md_safe(update.message, f"⚠️ {escape_md(wallet_msg)}")
        return True

    action = intent.get("action")

    if action == "close_all":
        if is_trading_paused():
            await _reply_md_safe(update.message, localize_text("⏸ Trading is temporarily paused by admin\\.", lang))
            return True
        # Bound to the network this prompt was built on (PREVIEW-NETWORK-BIND):
        # both the typed "confirm" and the button check it before closing.
        context.user_data["pending_text_close_all"] = {"network": network}
        try:
            await run_blocking(persist_text_close_all_pending, int(telegram_id))
        except Exception:
            logger.exception("Failed to persist pending text close_all for uid=%s", telegram_id)
        try:
            await update.message.reply_text(
                fmt_close_all_confirm() + "\n\nType `confirm` to execute or `cancel` to discard\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=confirm_close_all_kb(network=network),
            )
        except BadRequest:
            await update.message.reply_text(
                "⚠️ Close all positions? Type confirm or cancel.",
                reply_markup=confirm_close_all_kb(network=network),
            )
        return True

    if is_trading_paused():
        await _reply_md_safe(update.message, localize_text("⏸ Trading is temporarily paused by admin\\.", lang))
        return True

    if action == "set_tp_sl":
        tp = intent.get("tp_price")
        sl = intent.get("sl_price")
        product = intent.get("product") or "BTC"
        if tp is None and sl is None:
            await _reply_md_safe(
                update.message,
                localize_text("I need a TP and/or SL price \\(e\\.g\\. *set TP on BTC at 69500*\\)\\.", lang),
            )
            return True
        result = await run_blocking(apply_tp_sl_to_open_position, telegram_id, product, tp, sl)
        await _reply_md_safe(update.message, fmt_bracket_result(result))
        return True

    if action == "limit_close":
        product = intent.get("product")
        price = intent.get("limit_price")
        size = intent.get("size")
        result = await run_blocking(limit_close_position, telegram_id, product, price, size)
        await _reply_md_safe(update.message, fmt_limit_close_result(result))
        return True

    if action == "close_market":
        product = intent.get("product")
        size = intent.get("size")
        result = await run_blocking(close_position, telegram_id, product, size)
        if result.get("success"):
            msg = (
                f"✅ *Market close*\n\n"
                f"Closed {escape_md(str(result.get('cancelled', 0)))} "
                f"{escape_md(str(result.get('product', product)))}\\."
            )
        else:
            err = humanize_exchange_error(result.get("error", "unknown"))
            msg = f"❌ *Close failed*\n\n{escape_md(err)}"
        await _reply_md_safe(update.message, msg)
        return True

    return False
