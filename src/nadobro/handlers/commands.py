import logging
import os
from telegram import Update, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import CallbackContext
from telegram.constants import ParseMode
from src.nadobro.i18n import language_context, get_user_language, localize_text, localize_markup, get_active_language
from src.nadobro.handlers.render_utils import plain_text_fallback
from src.nadobro.users.user_service import get_or_create_user, get_user

INTRO_VIDEO_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "intro_video.mov")
from src.nadobro.handlers.formatters import (
    fmt_dashboard_home,
    fmt_help,
    fmt_ink_airdrop_card,
    fmt_ops_overview,
    fmt_revoke_card,
    fmt_status_overview,
    fmt_stop_all_result,
)
from src.nadobro.handlers.keyboards import (
    persistent_menu_kb,
    onboarding_language_kb,
    onboarding_accept_tos_kb,
    home_card_kb,
    status_kb,
    compose_status_overview_kb,
    back_kb,
)
from src.nadobro.strategy.bot_runtime import get_user_bot_status, stop_all_automation_for_user
from src.nadobro.venue.nado_tooling_service import get_ops_diagnostics
from src.nadobro.users.onboarding_service import (
    is_new_onboarding_complete,
    get_new_onboarding_state,
    evaluate_readiness,
)
from src.nadobro.config import DUAL_MODE_CARD_FLOW
from src.nadobro.handlers.home_card import (
    open_home_card_from_command,
)
from src.nadobro.core.async_utils import run_blocking
from src.nadobro.users.referral_service import (
    REFERRAL_LINK_PREFIX,
    normalize_referral_payload,
    redeem_referral_code,
)
logger = logging.getLogger(__name__)


async def build_status_dashboard_parts(
    telegram_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """MarkdownV2 source text plus merged keyboard (English markup labels).

    Caller localizes text/markup with the user's active language.
    """
    status = await run_blocking(get_user_bot_status, telegram_id)
    onboarding = await run_blocking(evaluate_readiness, telegram_id)
    try:
        from src.nadobro.trading.copy_service import get_user_copies

        status["copy_mirrors"] = await run_blocking(get_user_copies, telegram_id)
    except Exception:  # noqa: BLE001 - copy section is additive; status must render
        status["copy_mirrors"] = []
    text = fmt_status_overview(status, onboarding)
    merged = compose_status_overview_kb(
        is_running=bool(status.get("running")),
        strategy_label=str(status.get("strategy") or "").upper() or None,
    )
    return text, merged


def _safe_text(text: str | None, fallback: str) -> str:
    value = str(text or "").strip()
    return value or fallback


# New onboarding messages (exact copy from spec)
WELCOME_MSG = """Step 1 of 2 · Language

Welcome to Nadobro 👋

Trade perps on Nado straight from Telegram. Type the trade, tap to confirm, done. Automation, portfolio, and AI are all here too.

Pick your language:"""

WELCOME_CARD_MSG = """Step 2 of 2 · Accept terms

🔥 You're in.

Tapping *"Let's Get It"* means you're good with the Terms of Use & Privacy Policy.

🔐 How it works:
We spin up a secure 1CT signing key for your account. Your main wallet keys are never touched. Revoke whenever you want.

Ready?"""

async def cmd_start(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    username = update.effective_user.username
    language_code = getattr(update.effective_user, "language_code", None)

    user, is_new, _ = get_or_create_user(telegram_id, username, language_code=language_code)

    # /start is the universal escape hatch: commands never reach the text
    # pipeline (the MessageHandler filters out bot commands), so any stuck
    # multi-step flow (referral claim, custom inputs, wallet flow) must be
    # cleared HERE or the user stays trapped in it.
    from src.nadobro.handlers.state_reset import clear_pending_user_state

    clear_pending_user_state(context, telegram_id)

    start_arg = context.args[0] if getattr(context, "args", None) else None
    if start_arg:
        raw = str(start_arg).strip()
        if raw.lower().startswith(REFERRAL_LINK_PREFIX) or normalize_referral_payload(raw):
            normalized = normalize_referral_payload(raw)
            if normalized:
                try:
                    ok, msg = await run_blocking(
                        redeem_referral_code, telegram_id, username, normalized
                    )
                    if ok:
                        logger.info(
                            "Referral linked on /start telegram_id=%s code_prefix=%s",
                            telegram_id,
                            normalized[:3],
                        )
                    else:
                        logger.info(
                            "Referral redemption skipped on /start telegram_id=%s reason=%s",
                            telegram_id,
                            msg,
                        )
                except Exception as exc:
                    logger.warning(
                        "Referral redemption raised on /start telegram_id=%s: %s",
                        telegram_id,
                        exc,
                    )

    if not is_new_onboarding_complete(telegram_id):
        state = get_new_onboarding_state(telegram_id)
        if not state.get("language"):
            if is_new and os.path.exists(INTRO_VIDEO_PATH):
                try:
                    with open(INTRO_VIDEO_PATH, "rb") as vf:
                        await update.message.reply_video(video=vf)
                except Exception as e:
                    logger.warning("Failed to send intro video: %s", e)
            await update.message.reply_text(
                WELCOME_MSG,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=onboarding_language_kb(),
            )
            return
        with language_context(get_user_language(telegram_id)):
            lang = get_active_language()
            await update.message.reply_text(
                localize_text(WELCOME_CARD_MSG, lang),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=localize_markup(onboarding_accept_tos_kb(), lang),
            )
        return

    with language_context(get_user_language(telegram_id)):
        lang = get_active_language()
        if DUAL_MODE_CARD_FLOW:
            await _send_dashboard_card(update, context, telegram_id)
            return
        text = _safe_text(
            localize_text(fmt_dashboard_home(), lang),
            "🤖 Nadobro Command Center online.",
        )
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(persistent_menu_kb(), lang),
        )


async def _send_dashboard_card(update: Update, context: CallbackContext, telegram_id: int):
    lang = get_active_language()
    text = _safe_text(
        localize_text(fmt_dashboard_home(), lang),
        "🤖 Nadobro Command Center online.",
    )
    try:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(home_card_kb(), lang),
        )
    except BadRequest as e:
        if "Can't parse entities" not in str(e):
            raise
        await update.message.reply_text(
            plain_text_fallback(text),
            reply_markup=localize_markup(home_card_kb(), lang),
        )


async def cmd_help(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    with language_context(get_user_language(telegram_id)):
        # Always send a visible reply — same fix as /status. The card-edit
        # path applied the guide to the remembered home-card bubble, which may
        # have scrolled far off-screen, so /help looked like a silent no-op.
        lang = get_active_language()
        localized = localize_text(fmt_help(), lang)
        try:
            await update.message.reply_text(
                localized,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=localize_markup(back_kb(), lang),
            )
        except Exception:
            await update.message.reply_text(
                plain_text_fallback(localized),
                reply_markup=localize_markup(back_kb(), lang),
            )


async def cmd_status(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    with language_context(get_user_language(telegram_id)):
        text, merged_kb = await build_status_dashboard_parts(telegram_id)

        lang = get_active_language()
        localized = localize_text(text, lang)
        reply_markup = localize_markup(merged_kb, lang)
        # Always send a visible reply so /status works even when the home card is off-screen
        # or edit-in-place fails (webhook / concurrent updates).
        try:
            await update.message.reply_text(
                localized,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup,
            )
        except Exception:
            await update.message.reply_text(
                plain_text_fallback(localized),
                reply_markup=reply_markup,
            )


async def cmd_ops(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    with language_context(get_user_language(telegram_id)):
        status = await run_blocking(get_user_bot_status, telegram_id)
        ops = await run_blocking(get_ops_diagnostics, telegram_id)
        text = fmt_ops_overview(status, ops)

        lang = get_active_language()
        localized = localize_text(text, lang)
        reply_markup = localize_markup(
            status_kb(
                is_running=bool(status.get("running")),
                strategy_label=str(status.get("strategy") or "").upper() or None,
            ),
            lang,
        )
        try:
            await update.message.reply_text(
                localized,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup,
            )
        except Exception:
            await update.message.reply_text(
                plain_text_fallback(localized),
                reply_markup=reply_markup,
            )


async def cmd_stop_all(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    with language_context(get_user_language(telegram_id)):
        lang = get_active_language()
        ok, msg = await run_blocking(stop_all_automation_for_user, telegram_id)
        hint_ok = localize_text(
            "Give Nado a few seconds to sync, then confirm open orders and positions in Positions.",
            lang,
        )
        hint_fail = localize_text(
            "Nothing was active to stop, or exchange cleanup reported errors. Check Positions if exposure remains.",
            lang,
        )
        footer = hint_ok if ok else hint_fail
        await update.message.reply_text(
            localize_text(fmt_stop_all_result(ok, msg, footer), lang),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(persistent_menu_kb(), lang),
        )


async def cmd_revoke(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    with language_context(get_user_language(telegram_id)):
        lang = get_active_language()
        await update.message.reply_text(
            localize_text(fmt_revoke_card(), lang),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=localize_markup(persistent_menu_kb(), lang),
        )


async def cmd_airdrop(update: Update, context: CallbackContext):
    """/airdrop [address] — Ink airdrop allocation from the Nado archive.

    Without an argument, checks the user's linked main wallet. The airdrop is
    a mainnet program, so the mainnet archive is queried regardless of the
    bot's selected network — testnet mode must never show a false 0.
    """
    from src.nadobro.venue.nado_archive import normalize_evm_address, query_ink_airdrop

    telegram_id = update.effective_user.id
    with language_context(get_user_language(telegram_id)):
        lang = get_active_language()

        raw_arg = ""
        if getattr(context, "args", None):
            raw_arg = str(context.args[0] or "").strip()
        address = normalize_evm_address(raw_arg) if raw_arg else ""
        if raw_arg and not address:
            await update.message.reply_text(
                localize_text(
                    "That doesn't look like a wallet address. Send /airdrop 0x… with a 40-hex-character address.",
                    lang,
                )
            )
            return
        if not address:
            user = await run_blocking(get_user, telegram_id)
            address = normalize_evm_address(getattr(user, "main_address", "") or "")
            if not address:
                await update.message.reply_text(
                    localize_text(
                        "No wallet linked yet. Link one via /start, or check any address with /airdrop 0x…",
                        lang,
                    )
                )
                return

        amount = await run_blocking(query_ink_airdrop, "mainnet", address)
        if amount is None:
            # Archive unreachable / rate-limited — never claim "0" here.
            await update.message.reply_text(
                localize_text(
                    "Couldn't reach the Nado archive to check the allocation. Try again in a minute.",
                    lang,
                )
            )
            return

        localized = localize_text(fmt_ink_airdrop_card(address, amount), lang)
        try:
            await update.message.reply_text(localized, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            await update.message.reply_text(plain_text_fallback(localized))


# ---------------------------------------------------------------------------
# Phase 3: Tread-style live MM dashboard.
# ---------------------------------------------------------------------------

# Strategy IDs the live strategy dashboard supports.
MM_STRATEGIES = ("grid", "rgrid", "dgrid", "mid", "vol")


def _mm_dashboard_keyboard():
    """Inline keyboard with Refresh + Fills shortcuts. Imported lazily so the
    module stays importable in tests without telegram primitives."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Refresh status", callback_data="mm:status:refresh"),
            InlineKeyboardButton("🧾 Fills", callback_data="mm:fills"),
        ],
    ])


def build_mm_status_text(telegram_id: int) -> tuple[str, bool]:
    """Build the /mm_status body. Returns ``(plain_text, is_mm_active)``.

    Caller is responsible for MarkdownV2-escaping when sending to Telegram.
    """
    from src.nadobro.strategy.bot_runtime import get_user_bot_state, get_user_bot_status
    from src.nadobro.strategy import mm_dashboard

    status = get_user_bot_status(telegram_id) or {}
    state = get_user_bot_state(telegram_id) or {}
    strategy_id = str(status.get("strategy") or state.get("strategy") or "").lower()

    # Copy Trading block — rendered whether or not an MM strategy runs, with
    # the rail-consistent net PnL (realized derived gross + unrealized - fees).
    copy_lines: list[str] = []
    try:
        from src.nadobro.trading.copy_service import get_user_copies

        for m in get_user_copies(telegram_id):
            net = float(m.get("net_pnl") or 0.0)
            allocated = float(m.get("total_allocated_usd") or 0.0)
            pct = (net / allocated * 100.0) if allocated > 0 else 0.0
            copy_lines.append(
                f"🔁 {m.get('trader_label')} [{'PAUSED' if m.get('paused') else 'LIVE'}] · "
                f"{int(m.get('open_positions') or 0)} position(s) · "
                f"net {net:+,.2f} USD ({pct:+.1f}% of {allocated:,.0f}) · "
                f"vol {float(m.get('cumulative_volume_usd') or 0.0):,.0f} · "
                f"fees {float(m.get('cumulative_fees_usd') or 0.0):,.2f}"
            )
    except Exception:  # noqa: BLE001 - copy block is additive
        pass
    copy_block = ("\n\nCopy Trading\n" + "\n".join(copy_lines)) if copy_lines else ""

    if strategy_id not in MM_STRATEGIES:
        return (
            "No MM strategy is currently active.\n\n"
            "Start GRID, Reverse GRID, Dynamic GRID, Mid Mode, or Volume from the strategy hub "
            "and re-run /mm_status." + copy_block,
            bool(copy_lines),
        )
    network = str(status.get("network") or state.get("network") or "mainnet")
    product = str(status.get("product") or state.get("product") or "BTC").upper()
    open_orders_count = int(status.get("open_orders_count") or 0)

    # Authoritative live Nado view of the active session (open-position uPnL,
    # realized PnL, volume, fees, fills, open orders) so the dashboard matches
    # the Nado UI instead of the engine-empty in-memory ``state``.
    live_snapshot = None
    try:
        from src.nadobro.trading.live_session import get_live_session_snapshot
        from src.nadobro.trading.session_resolver import resolve_current_strategy_session
        from src.nadobro.users.user_service import get_user_readonly_client

        sess = resolve_current_strategy_session(
            telegram_id, network, strategy_id, state=state, status=status
        )
        if sess and sess.get("id") is not None:
            client = get_user_readonly_client(telegram_id, network=network)
            live_snapshot = get_live_session_snapshot(
                telegram_id, network, sess, state=state, client=client
            )
    except Exception:
        live_snapshot = None

    dashboard_state = dict(state)
    for key in (
        "order_observability",
        "session_fees_usd",
        "session_realized_pnl_usd",
        "session_volume_usd",
        "target_volume_usd",
        "vol_closed_cycles",
        "vol_cycles_completed",
        "vol_last_order_digest",
        "vol_last_order_kind",
        "vol_market",
        "vol_phase",
        "volume_done_usd",
        "volume_remaining_usd",
    ):
        value = status.get(key)
        if value not in (None, "") and dashboard_state.get(key) in (None, "", 0, 0.0):
            dashboard_state[key] = value

    snapshot = mm_dashboard.build_status_snapshot(
        state=dashboard_state,
        strategy_id=strategy_id,
        network=network,
        product=product,
        open_orders_count=open_orders_count,
        live_snapshot=live_snapshot,
    )
    lines = mm_dashboard.render_status_lines(snapshot)
    return ("\n".join(lines) + copy_block, True)


def build_mm_fills_text(telegram_id: int, limit: int = 10) -> str:
    from src.nadobro.strategy.bot_runtime import get_user_bot_state, get_user_bot_status
    from src.nadobro.strategy import mm_dashboard

    state = get_user_bot_state(telegram_id) or {}
    # Prefer DB-recorded fills for the active session (engine strategies record
    # to the DB, not state); fall back to state for legacy MM paths.
    db_fills = None
    try:
        from src.nadobro.models.database import (
            get_session_recent_fills,
        )
        from src.nadobro.trading.session_resolver import resolve_current_strategy_session

        status = get_user_bot_status(telegram_id) or {}
        network = str(status.get("network") or state.get("network") or "mainnet")
        strategy_id = str(status.get("strategy") or state.get("strategy") or "").lower()
        sess = resolve_current_strategy_session(
            telegram_id, network, strategy_id, state=state, status=status
        )
        if sess and sess.get("id") is not None:
            # Scoped per user + per session so /mm_fills only ever shows THIS
            # user's fills for THIS run (not the whole product, not other users).
            db_fills = get_session_recent_fills(
                int(sess["id"]), network, limit=limit, user_id=int(telegram_id)
            ) or None
    except Exception:
        db_fills = None
    lines = mm_dashboard.render_fills_lines(state, limit=limit, db_fills=db_fills)
    header = f"🧾 Last {limit} fills"
    return header + "\n" + "\n".join(lines)


async def cmd_mm_status(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    text, is_active = await run_blocking(build_mm_status_text, telegram_id)
    try:
        from src.nadobro.handlers.formatters import escape_md
        body = escape_md(text)
        markup = _mm_dashboard_keyboard() if is_active else None
        header = "📊 *Strategy Status*" if is_active and text.lstrip().upper().startswith("VOL ") else "📊 *MM Status*"
        await update.message.reply_text(
            f"{header}\n\n{body}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=markup,
        )
    except Exception:
        # Plain-text fallback so the user always gets a readable response.
        await update.message.reply_text(text)


async def cmd_mm_fills(update: Update, context: CallbackContext):
    telegram_id = update.effective_user.id
    limit = 10
    if context and getattr(context, "args", None):
        try:
            limit = max(1, min(50, int(context.args[0])))
        except (TypeError, ValueError):
            pass
    text = await run_blocking(build_mm_fills_text, telegram_id, limit)
    try:
        # Code-fence renders columnar fills evenly; inside ``` only ` and \\ need
        # escaping — render_fills_lines emits neither, so no escape pass needed.
        await update.message.reply_text(
            f"```\n{text}\n```",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception:
        await update.message.reply_text(text)

