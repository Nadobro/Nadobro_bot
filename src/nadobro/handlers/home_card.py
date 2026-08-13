import logging
import asyncio

from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackContext

from src.nadobro.i18n import localize_text, localize_markup, get_active_language
from src.nadobro.handlers.render_utils import plain_text_fallback
from src.nadobro.handlers.formatters import (
    fmt_alert_menu_intro,
    fmt_home_command_center_card,
    fmt_mode_view,
    fmt_strategy_hub_intro,
    fmt_positions,
    fmt_settings,
    fmt_wallet_info,
    fmt_points_dashboard,
    fmt_referral_dashboard,
)
from src.nadobro.handlers.keyboards import (
    home_card_kb,
    mode_kb,
    strategy_hub_kb,
    wallet_kb,
    positions_kb,
    alerts_kb,
    settings_kb,
    referral_kb,
    persistent_menu_kb,
)
from src.nadobro.users.points_ui import points_scope_kb
from src.nadobro.handlers.wallet_view import build_wallet_view_payload
from src.nadobro.users.settings_service import get_user_settings
from src.nadobro.users.user_service import get_user, get_user_readonly_client, get_user_wallet_info
from src.nadobro.users.points_service import get_points_dashboard
from src.nadobro.users.referral_service import get_referral_dashboard
from src.nadobro.core.async_utils import run_blocking, run_blocking_sdk_capped
from src.nadobro.core.perf import timed_metric

# Wall-clock ceiling for the data views (positions/wallet) that still issue a
# live SDK call on the click path. Generous enough for a warm gateway, short
# enough that a hung host yields a "refreshing" placeholder instead of a 30s
# freeze. These also run on the SDK pool (not the misc pool) so a slow gateway
# can never starve unrelated DB reads. Tunable via env.
from src.nadobro.utils.env import env_float as _env_float

_DATA_VIEW_CEILING_SECONDS = _env_float("NADO_DATA_VIEW_CEILING_SECONDS", 6.0)

logger = logging.getLogger(__name__)

HOME_CARD_KEY = "home_card_message"
KEYBOARD_REMOVED_KEY = "dual_mode_keyboard_removed"

# Dedupe in-flight balance warms so rapid taps during the refresh window don't
# pile redundant gateway fetches onto the SDK pool. {subaccount_hex: ts}.
import threading as _threading
import time as _time

_warm_lock = _threading.Lock()
_warm_inflight: dict[str, float] = {}
_WARM_DEDUPE_SECONDS = 10.0


def build_home_card_text(telegram_id: int) -> str:
    user = get_user(telegram_id)
    if not user:
        return "⚠️ User not found\\. Use /start first\\."

    network = user.network_mode.value
    balance_str = "N/A"
    try:
        client = get_user_readonly_client(telegram_id)
        if client:
            # PERF: render from the in-process balance cache only — a tap must
            # never block on the gateway (a throttled host can hang the call to
            # the full ~30s SDK timeout, which is the root cause of the slow
            # button). On a cache miss we show "updating…" and warm the cache
            # off-thread so the next render is instant + fresh.
            balance = client.get_balance(cache_only=True)
            if balance and balance.get("pending"):
                balance_str = "updating…"
                _warm_balance_async(client)
            elif balance and balance.get("exists"):
                raw = (balance.get("balances", {}) or {}).get(0, 0)
                if not raw:
                    raw = (balance.get("balances", {}) or {}).get("0", 0)
                balance_str = f"${float(raw or 0):,.2f}"
    except Exception:
        pass

    return fmt_home_command_center_card(network, balance_str)


def _warm_balance_async(client) -> None:
    """Fire-and-forget refresh of the balance cache on the SDK thread pool.

    Keeps the gateway call off the click path while ensuring the next card
    render serves a fresh value. Deduped + best-effort: a failed/slow warm just
    leaves the previous display value in place.
    """
    try:
        key = str(getattr(client, "subaccount_hex", "") or id(client))
        now = _time.time()
        with _warm_lock:
            last = _warm_inflight.get(key, 0.0)
            if now - last < _WARM_DEDUPE_SECONDS:
                return
            _warm_inflight[key] = now
        from src.nadobro.core.async_utils import _sdk_pool

        _sdk_pool.submit(_safe_warm_balance, client, key)
    except Exception:
        pass


def _safe_warm_balance(client, key: str) -> None:
    try:
        client.get_balance(force=True)
    except Exception:
        pass
    finally:
        with _warm_lock:
            _warm_inflight.pop(key, None)


async def build_home_card_text_async(telegram_id: int) -> str:
    with timed_metric("card.home.build"):
        return await run_blocking(build_home_card_text, telegram_id)


def build_positions_view(telegram_id: int):
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    client = get_user_readonly_client(telegram_id, network=network)
    if not client:
        return localize_text("⚠️ Wallet's not set up yet\\. Hit /start to link it\\.", get_active_language()), home_card_kb()
    try:
        positions = client.get_all_positions() or []
    except Exception as e:
        logger.warning("positions_view_failed user=%s err=%s", telegram_id, e)
        return localize_text(
            "⚠️ Can't pull positions right now\\. Give it a sec and tap again\\.",
            get_active_language(),
        ), home_card_kb()
    prices = None
    try:
        prices = client.get_all_market_prices()
    except Exception as e:
        logger.debug("positions_prices_failed user=%s err=%s", telegram_id, e)
    return fmt_positions(positions, prices), positions_kb(positions or [])


# NOTE: ``build_portfolio_view`` was retired during the Portfolio workflow
# redesign (2026-05). All portfolio rendering now goes through
# ``portfolio_deck.render_portfolio_deck`` driven by ``nado_sync`` snapshots.
# The legacy ``portfolio_service.get_portfolio_snapshot`` and ``fmt_portfolio``
# path duplicated the data flow and was responsible for the two-source skew
# that produced the tz-aware datetime warning seen in production logs.


def _remember_home_card(context: CallbackContext, chat_id: int, message_id: int) -> None:
    context.user_data[HOME_CARD_KEY] = {
        "chat_id": chat_id,
        "message_id": message_id,
    }


async def _edit_or_send_card(
    update,
    context: CallbackContext,
    text: str,
    reply_markup,
    *,
    prefer_reply_to_message: bool = False,
    parse_mode: str = ParseMode.MARKDOWN_V2,
):
    """Show or refresh the home / module card.

    When *prefer_reply_to_message* is True (reply-keyboard flows), we always send a
    new message replying to the user's message. Editing only the last remembered
    home-card message updates a bubble that may be far above the viewport, so
    users think the bot ignored the tap until they tap again or scroll up.
    """
    chat_id = update.effective_chat.id
    lang = get_active_language()
    text = localize_text(text, lang)
    reply_markup = localize_markup(reply_markup, lang)

    if not context.user_data.get(KEYBOARD_REMOVED_KEY):
        try:
            shortcut_text = localize_text("Keyboard's ready 👇", lang)
            shortcut_kb = localize_markup(persistent_menu_kb(), lang)
            shortcut_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=shortcut_text,
                reply_markup=shortcut_kb,
            )
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=shortcut_msg.message_id)
            except Exception:
                pass
            context.user_data[KEYBOARD_REMOVED_KEY] = True
        except Exception:
            pass

    user_message = getattr(update, "message", None)
    if prefer_reply_to_message and user_message:
        try:
            msg = await user_message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        except BadRequest as e:
            if "Can't parse entities" not in str(e):
                raise
            msg = await user_message.reply_text(
                plain_text_fallback(text),
                reply_markup=reply_markup,
            )
        _remember_home_card(context, chat_id, msg.message_id)
        return

    home = context.user_data.get(HOME_CARD_KEY) or {}
    message_id = home.get("message_id") if home.get("chat_id") == chat_id else None

    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
            return
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            if "Can't parse entities" not in str(e):
                logger.info("home_card_edit_failed_new_message chat_id=%s", chat_id)
            else:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=plain_text_fallback(text),
                        reply_markup=reply_markup,
                    )
                    return
                except BadRequest as e2:
                    if "Message is not modified" in str(e2):
                        return
                    logger.info("home_card_edit_fallback_failed_new_message chat_id=%s", chat_id)
                except Exception:
                    logger.info("home_card_edit_fallback_failed_new_message chat_id=%s", chat_id)
        except Exception:
            logger.info("home_card_edit_failed_new_message chat_id=%s", chat_id)

    try:
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except BadRequest as e:
        if "Can't parse entities" not in str(e):
            raise
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=plain_text_fallback(text),
            reply_markup=reply_markup,
        )
    _remember_home_card(context, chat_id, message.message_id)


def _view_mode_text(telegram_id: int):
    user = get_user(telegram_id)
    current_network = user.network_mode.value if user else "testnet"
    return fmt_mode_view(current_network), mode_kb(current_network)


def _view_strategy_text():
    return fmt_strategy_hub_intro(), strategy_hub_kb()


def _view_wallet_text(telegram_id: int):
    return build_wallet_view_payload(telegram_id, context=None, verify_signer=True)


def _view_positions_text(telegram_id: int):
    return build_positions_view(telegram_id)


async def _view_portfolio_text(telegram_id: int):
    from src.nadobro.handlers.portfolio_deck import (
        empty_portfolio_snapshot,
        render_portfolio_deck,
    )
    from src.nadobro.venue.nado_sync import get_cached_snapshot, mark_user_active

    try:
        user = get_user(telegram_id)
        network = user.network_mode.value if user else None
        mark_user_active(int(telegram_id))
        cached = get_cached_snapshot(int(telegram_id), network)
        if cached:
            return render_portfolio_deck(cached)
        _warm_portfolio_snapshot(telegram_id)
        return render_portfolio_deck(
            empty_portfolio_snapshot(telegram_id, network),
            refreshing=True,
        )
    except Exception as e:
        logger.warning("portfolio_deck_unavailable user=%s err=%s", telegram_id, e)
        return localize_text(
            "⚠️ Can't pull your portfolio right now\\. Give it a sec and tap again\\.",
            get_active_language(),
        ), home_card_kb()


def _warm_portfolio_snapshot(telegram_id: int) -> None:
    """Kick a venue snapshot off the tap path so the next Portfolio tap is cached."""

    async def _job():
        try:
            from src.nadobro.handlers.portfolio_deck import snapshot_for_user

            await snapshot_for_user(telegram_id, reason="ui")
        except Exception as e:
            logger.warning("portfolio snapshot warm failed user=%s: %s", telegram_id, e)

    try:
        asyncio.get_running_loop().create_task(_job())
    except RuntimeError:
        pass


def _view_points_text(telegram_id: int):
    payload = get_points_dashboard(telegram_id, scope="week")
    return fmt_points_dashboard(payload), points_scope_kb()


def _view_referral_text(telegram_id: int):
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    payload = get_referral_dashboard(telegram_id, network=network)
    return fmt_referral_dashboard(payload), referral_kb(can_generate=bool(payload.get("remaining_codes")))


def _view_alerts_text():
    return fmt_alert_menu_intro(), alerts_kb()


def _view_settings_text(telegram_id: int):
    _, settings = get_user_settings(telegram_id)
    msg = fmt_settings(settings)
    lev = settings.get("default_leverage", 1)
    slip = settings.get("slippage", 1)
    return msg, settings_kb(lev, slip)


def _refreshing_placeholder(message_key: str):
    return (
        localize_text(message_key, get_active_language()),
        home_card_kb(),
    )


async def resolve_home_view(callback_data: str, telegram_id: int):
    if callback_data in ("market:view", "nav:market_radar", "market:radar", "home:market_radar"):
        callback_data = "points:view"
    if callback_data == "home:mode":
        return _view_mode_text(telegram_id)
    if callback_data == "nav:strategy_hub":
        return _view_strategy_text()
    if callback_data == "wallet:view":
        # SDK pool + bounded cap: keep the live signer/balance read off the misc
        # pool and never let a hung gateway freeze the tap.
        return await run_blocking_sdk_capped(
            _view_wallet_text, telegram_id,
            timeout_seconds=_DATA_VIEW_CEILING_SECONDS,
            default=_refreshing_placeholder("⏳ Refreshing wallet… tap again in a sec\\."),
        )
    if callback_data == "portfolio:view":
        return await _view_portfolio_text(telegram_id)
    if callback_data == "pos:view":
        return await run_blocking_sdk_capped(
            _view_positions_text, telegram_id,
            timeout_seconds=_DATA_VIEW_CEILING_SECONDS,
            default=_refreshing_placeholder("⏳ Refreshing positions… tap again in a sec\\."),
        )
    if callback_data == "points:view":
        return await run_blocking(_view_points_text, telegram_id)
    if callback_data == "refer:view":
        return await run_blocking(_view_referral_text, telegram_id)
    if callback_data == "alert:menu":
        return _view_alerts_text()
    if callback_data == "settings:view":
        return await run_blocking(_view_settings_text, telegram_id)
    return await build_home_card_text_async(telegram_id), home_card_kb()


async def open_home_card_view_from_message(update, context: CallbackContext, telegram_id: int, callback_data: str):
    if callback_data == "wallet:view":
        text, kb = await run_blocking_sdk_capped(
            build_wallet_view_payload, telegram_id, context, True,
            timeout_seconds=_DATA_VIEW_CEILING_SECONDS,
            default=_refreshing_placeholder("⏳ Refreshing wallet… tap again in a sec\\."),
        )
        await _edit_or_send_card(update, context, text, kb, prefer_reply_to_message=True)
        return
    text, kb = await resolve_home_view(callback_data, telegram_id)
    parse_mode = ParseMode.HTML if callback_data == "portfolio:view" else ParseMode.MARKDOWN_V2
    await _edit_or_send_card(
        update, context, text, kb,
        prefer_reply_to_message=True,
        parse_mode=parse_mode,
    )


async def open_home_card_from_command(update, context: CallbackContext, telegram_id: int):
    text = await build_home_card_text_async(telegram_id)
    await _edit_or_send_card(update, context, text, home_card_kb())

