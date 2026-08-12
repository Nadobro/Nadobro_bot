"""
Nado-native Copy Trading Service.

Monitors copied trader addresses on Nado DEX and mirrors their positions
with user-defined parameters (margin, leverage, TP/SL, cumulative limits).
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Optional

from src.nadobro.config import get_product_name, get_product_max_leverage
from src.nadobro.models.database import (
    upsert_copy_trader,
    get_copy_trader,
    get_copy_trader_by_wallet,
    get_active_copy_traders,
    get_curated_copy_traders,
    deactivate_copy_trader,
    clear_saved_copy_trader_selections,
    get_copy_mirror,
    stop_copy_mirror,
    request_copy_mirror_stop,
    pause_copy_mirror,
    resume_copy_mirror,
    count_user_active_mirrors,
    get_mirrors_for_trader,
    create_copy_mirror_v2,
    get_user_active_mirrors_v2,
    get_all_active_mirrors_v2,
    auto_stop_mirror,
    insert_copy_position,
    get_open_copy_positions,
    get_open_copy_position_for_product,
    close_copy_position,
    reduce_copy_position,
    grow_copy_position,
    update_mirror_accounting,
    set_mirror_unrealized,
    set_mirror_session,
    insert_strategy_session,
    update_strategy_session,
    rollup_session_from_trades,
    rollup_engine_session_pnl_funding,
    save_copy_snapshot,
    get_latest_copy_snapshot,
    get_copy_trades_by_mirror,
    get_bot_state,
    set_bot_state,
)
from src.nadobro.users.admin_service import is_trading_paused
from src.nadobro.users.user_service import get_user, get_user_nado_client
from src.nadobro.trading.trade_service import execute_market_order, execute_limit_order
from src.nadobro.venue.nado_client import NadoClient
from src.nadobro.venue.nado_archive import query_order_by_digest, query_orders_by_digests
from src.nadobro.core.async_utils import run_blocking
from src.nadobro.utils.env import env_bool, env_float

logger = logging.getLogger(__name__)

MAX_MIRRORS_PER_USER = 5
# Product floor: copy trading requires at least $100 margin per trade so the
# proportional mirror of a serious leader position clears venue minimums.
MIN_MARGIN_PER_TRADE = 100.0
MAX_MARGIN_PER_TRADE = 5000.0
POLL_INTERVAL_SECONDS = 30
# Partial-close mirroring: when the leader trims a position below the baseline
# we copied by more than this fraction, the same fraction of the copy is
# closed reduce-only. Kill switch: NADO_COPY_PARTIAL_CLOSES=false.
PARTIAL_CLOSES_ENABLED = env_bool("NADO_COPY_PARTIAL_CLOSES", True)
PARTIAL_CLOSE_MIN_DELTA_PCT = env_float("NADO_COPY_PARTIAL_MIN_DELTA_PCT", 5.0)
# Below this remaining fraction a partial becomes a full close (avoids dust).
PARTIAL_CLOSE_DUST_FRACTION = 0.05

# COPY-NO-SCALE-UP fix: mirror a leader's ADD to an existing position. Without
# this the bot copied only the leader's FIRST entry — a leader going 16.6 -> 36.6
# HYPE was never mirrored, so the follower silently under-tracked the trade.
SCALE_UPS_ENABLED = env_bool("NADO_COPY_SCALE_UPS", True)
# Ignore adds smaller than this % of the tracked baseline (noise / dust adds).
SCALE_UP_MIN_DELTA_PCT = env_float("NADO_COPY_SCALE_UP_MIN_DELTA_PCT", 5.0)
# Share of margin_per_trade committed on the FIRST entry. The rest is HEADROOM
# for mirroring the leader scaling in. Without it the conviction weight pinned
# the follower at 100% of budget on the leader's first fill, so a later add had
# no room to be mirrored and the copy silently stopped tracking the trade.
# margin_per_trade remains a HARD cap: scaling in can reach it, never exceed it.
COPY_INITIAL_ENTRY_FRACTION = min(1.0, max(0.05, env_float(
    "NADO_COPY_INITIAL_ENTRY_FRACTION", 0.5)))
# COPY-MAKER: opens are maker-first post-only limit orders joined at the touch
# (market opens paid taker fees + up to 1.5% slippage on every mirrored
# entry). Closes stay reduce-only market on purpose: a close that doesn't
# fill leaves exposure the leader no longer has. Kill switch reverts opens to
# the old market path.
MAKER_OPENS_ENABLED = env_bool("NADO_COPY_MAKER_OPENS", True)
MAKER_FILL_WAIT_SECONDS = max(2.0, env_float("NADO_COPY_MAKER_WAIT_SECONDS", 20.0))
# How long a maker order with unknown fate (cancel failed / archive silent)
# may stay pending before a confirmed-cancel is declared unfilled anyway.
MAKER_PENDING_GIVE_UP_SECONDS = max(60.0, env_float("NADO_COPY_MAKER_PENDING_GIVE_UP_SECONDS", 600.0))
# COPY-SWEEP-CONFIRM: an external close with no bracket-fill evidence must be
# observed on two polls at least this far apart before it books. A transient
# venue/archive read flap (isolated-subaccount discovery returns [] whenever
# the archive is rate-limited) must not fabricate closes — the phantom close
# both corrupts cumulative PnL and lets the open loop re-open a duplicate.
EXTERNAL_CLOSE_CONFIRM_SECONDS = max(
    5.0, env_float("NADO_COPY_EXTERNAL_CLOSE_CONFIRM_SECONDS", POLL_INTERVAL_SECONDS * 0.7)
)
# COPY-TP-CONFIRM: the profit-side rail must see the target breached on two
# polls at least this far apart before it flattens (SL is deliberately
# immediate — cutting a loss late is worse than cutting it early).
TP_CONFIRM_GAP_SECONDS = max(5.0, env_float("NADO_COPY_TP_CONFIRM_SECONDS", POLL_INTERVAL_SECONDS * 0.5))
# COPY-MASS-VANISH (audit F-4): when EVERY tracked position of a mirror
# disappears from the venue read at once with no bracket-fill evidence, that
# is far more likely a read outage (engine cross-margin read failed, host
# token bucket starved — sources that don't trip the archive rate-limit flag)
# than the leader/user closing everything simultaneously. Require such an
# unproven whole-book vanish to persist this much longer before booking, so a
# transient outage self-heals without fabricating realized PnL. A real event
# (liquidation cascade, manual mass-close) simply books after the delay.
MASS_VANISH_CONFIRM_SECONDS = max(
    EXTERNAL_CLOSE_CONFIRM_SECONDS,
    env_float("NADO_COPY_MASS_VANISH_CONFIRM_SECONDS", POLL_INTERVAL_SECONDS * 3.0),
)

# (mirror_id, product_id) -> first-seen ts of an unverified external close.
_EXTERNAL_CLOSE_SUSPECTS: dict[tuple[int, int], float] = {}
# mirror_id -> first ts the TP target was breached (cleared on any non-breach).
_TP_BREACH_FIRST_SEEN: dict[int, float] = {}
# (mirror_id, product_id) -> maker open whose fate is unresolved. While a
# product has an entry here the open loop must never place another order on
# it (a live resting order + a fresh order = double exposure).
_PENDING_MAKER_OPENS: dict[tuple[int, int], dict] = {}
_bot_app = None
_poll_task: Optional[asyncio.Task] = None


# AUDIT F3 (copy auditor): pendings guard live-order exposure, so unlike the
# TP marker they must survive a redeploy — a forgotten pending means a resting
# order that can fill behind a stopped/paused mirror with nobody watching.
_PENDING_STATE_KEY = "copy_pending_maker_opens"


def _persist_pending_maker_opens() -> None:
    try:
        set_bot_state(
            _PENDING_STATE_KEY,
            {f"{k[0]}:{k[1]}": v for k, v in _PENDING_MAKER_OPENS.items()},
        )
    except Exception:  # noqa: BLE001 - persistence is best-effort; memory stays authoritative
        logger.warning("copy pending-maker persist failed", exc_info=True)


def _register_pending_maker_open(mirror_id: int, pid: int, pending: dict) -> None:
    _PENDING_MAKER_OPENS[(int(mirror_id), int(pid))] = pending
    _persist_pending_maker_opens()


def _pop_pending_maker_open(mirror_id: int, pid: int) -> None:
    if _PENDING_MAKER_OPENS.pop((int(mirror_id), int(pid)), None) is not None:
        _persist_pending_maker_opens()


def _load_pending_maker_opens() -> None:
    """Restore unresolved maker opens across a restart. This is stand-down
    hygiene (reconcile/cancel orders whose fate was unknown), NOT a strategy
    resume — booking a fill that already happened is accounting, not trading."""
    try:
        data = get_bot_state(_PENDING_STATE_KEY) or {}
        if isinstance(data, str):
            data = json.loads(data)
        for key, value in (data or {}).items():
            try:
                m, p = str(key).split(":", 1)
                _PENDING_MAKER_OPENS.setdefault((int(m), int(p)), dict(value))
            except (TypeError, ValueError):
                continue
        if _PENDING_MAKER_OPENS:
            logger.warning(
                "copy pending-maker opens restored after restart: %s",
                sorted(_PENDING_MAKER_OPENS),
            )
    except Exception:  # noqa: BLE001 - a failed restore must not block polling
        logger.warning("copy pending-maker restore failed", exc_info=True)


def _close_result_ok(result: Optional[dict]) -> tuple[bool, str]:
    """Did THIS submit actually close anything?

    AUDIT COPY-DUP-SETTLE (copy auditor F-1): a duplicate-suppressed submit
    comes back ``success=True`` carrying ANOTHER submit's digest (or none).
    Settling it books a phantom 0-PnL close — the row closes while the venue
    position stays open — or double-books the winner's fill. A duplicate is a
    retryable failure here, never a fill.
    """
    result = result or {}
    if result.get("duplicate"):
        return False, "duplicate order intent suppressed — close will retry"
    if not result.get("success"):
        return False, str(result.get("error", "close failed"))
    return True, ""


def _clear_mirror_runtime_state(mirror_id: int) -> None:
    """Drop in-memory poller state when a mirror stops (any reason)."""
    _TP_BREACH_FIRST_SEEN.pop(int(mirror_id), None)
    for key in [k for k in _EXTERNAL_CLOSE_SUSPECTS if k[0] == int(mirror_id)]:
        _EXTERNAL_CLOSE_SUSPECTS.pop(key, None)
    popped = False
    for key in [k for k in _PENDING_MAKER_OPENS if k[0] == int(mirror_id)]:
        _PENDING_MAKER_OPENS.pop(key, None)
        popped = True
    if popped:
        _persist_pending_maker_opens()


def set_copy_bot_app(app):
    global _bot_app
    _bot_app = app


async def _notify_user(telegram_id: int, text: str, reply_markup=None):
    if not _bot_app:
        return
    try:
        await _bot_app.bot.send_message(chat_id=telegram_id, text=text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning("Copy notify failed for %s: %s", telegram_id, e)


async def _post_copy_close_card(
    telegram_id: int, network: str, position_id: int, caption: str
) -> bool:
    """Auto-post the Type A PnL card for a just-closed copy position, with a
    Share button. Returns True if the card was sent, False to fall back to a
    plain text notification. Best-effort — the position must already be marked
    closed (this runs after ``close_copy_position``)."""
    if not _bot_app or not position_id:
        return False
    try:
        import io as _io

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from src.nadobro.portfolio.pnl_card_builder import build_copy_trade_card_data
        from src.nadobro.portfolio.pnl_card_type_a import generate_type_a_card

        data = await run_blocking(
            build_copy_trade_card_data, telegram_id, str(network), int(position_id)
        )
        if not data or data.get("unsupported"):
            return False
        png = await run_blocking(generate_type_a_card, data)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(
            "📤 Share", callback_data=f"portfolio:share_pnl:copy:{int(position_id)}"
        )]])
        # Full-quality PNG upload — media-sized timeouts (the ~1.5MB card was
        # tripping the default 5s write timeout).
        await _bot_app.bot.send_photo(
            chat_id=telegram_id, photo=_io.BytesIO(png), caption=caption, reply_markup=kb,
            read_timeout=60, write_timeout=120, connect_timeout=30, pool_timeout=30,
        )
        return True
    except Exception as e:  # noqa: BLE001 - card is best-effort; caller falls back to text
        logger.warning("copy close card failed for user %s pos %s: %s", telegram_id, position_id, e)
        return False


# ─── Public API ────────────────────────────────────────────────

def add_trader(
    wallet_address: str,
    label: str = "",
    is_curated: bool = False,
    owner_user_id: int | None = None,
) -> tuple[bool, str, int | None]:
    """Add a copy-trade target.

    Curated entries (admin-only) get owner_user_id=None and are visible to
    everyone. Personal entries require an owner_user_id and remain private
    to that telegram user.
    """
    if not wallet_address or len(wallet_address) < 10:
        return False, "Invalid wallet address.", None
    wallet = wallet_address.strip()
    if not wallet.startswith("0x"):
        return False, "Wallet address must start with 0x.", None
    if not is_curated and owner_user_id is None:
        return False, "Owner required for personal trader.", None
    trader_id = upsert_copy_trader(
        wallet,
        label=label,
        is_curated=is_curated,
        owner_user_id=None if is_curated else owner_user_id,
    )
    if not trader_id:
        return False, "Failed to save trader.", None
    return True, f"Trader added: {label or wallet[:10]}...", trader_id


def remove_trader(trader_id: int, requester_user_id: int | None = None, is_admin: bool = False) -> tuple[bool, str]:
    """Remove a copy trader.

    A non-admin requester may only remove a trader they personally own
    (owner_user_id == requester). Curated entries are removable only by
    admins. This prevents one user from deactivating another user's
    private copy target.
    """
    trader = get_copy_trader(trader_id)
    if not trader:
        return False, "Trader not found."
    owner_id = trader.get("owner_user_id")
    if not is_admin:
        if owner_id is None:
            return False, "Curated traders are admin-managed."
        if requester_user_id is None or int(owner_id) != int(requester_user_id):
            return False, "Trader not found."
    mirrors = get_mirrors_for_trader(trader_id)
    pending = 0
    stopped = 0
    for m in mirrors:
        # COPY-STOP-RACE: intent first (see stop_copy). Also reactivates a
        # legacy inactive mirror with tracked exposure into flatten-only mode.
        request_copy_mirror_stop(int(m["id"]))
        _closed, _pnl, _volume, errors = _flatten_mirror_positions(
            int(m["id"]),
            int(m["user_id"]),
            str(m.get("network", "mainnet")),
            reason="trader_removed",
        )
        if errors:
            # Never hide a trader/mirror while a copied position still needs a
            # close. ``stop_requested`` keeps the poller retrying flatten-only.
            pending += 1
            continue
        stop_copy_mirror(m["id"])
        _clear_mirror_runtime_state(int(m["id"]))
        _finalize_mirror_session(m, "trader_removed")
        stopped += 1
    label = trader.get("label") or trader["wallet_address"][:10]
    if pending:
        return (
            False,
            f"Trader {label} remains active while {pending} mirror(s) retry failed position closes. "
            "Remove it again after they are flat.",
        )
    deactivate_copy_trader(trader_id)
    return True, f"Trader {label} removed. {stopped} mirror(s) stopped."


def clear_saved_copy_traders(telegram_id: int) -> tuple[int, int]:
    """Clear only dormant personal selections, never active copy exposure.

    This is intentionally separate from ``remove_trader``: removing a trader
    stops and flattens mirrors, while the user-facing clear button is a safe
    housekeeping action for abandoned selections.
    """
    return clear_saved_copy_trader_selections(int(telegram_id))


def start_copy(
    telegram_id: int,
    trader_id: int,
    network: str = "mainnet",
    margin_per_trade: float = 50.0,
    max_leverage: float = 10.0,
    cumulative_stop_loss_pct: float = 0.0,
    cumulative_take_profit_pct: float = 0.0,
    total_allocated_usd: float = 500.0,
) -> tuple[bool, str]:
    # Global admin pause, checked FIRST and at the service layer rather than in
    # the callback: starting a mirror arms an automated order path, so it belongs
    # behind the same kill switch as manual trades (trade_card), typed intents
    # (messages/intent_handlers), and strategy start (bot_runtime). Copy had
    # THREE uncovered arming paths, all now gated: this one, ``resume_copy``,
    # and the poller's own open/scale-up commits (which need no user action).
    if is_trading_paused():
        return False, "Trading is temporarily paused by admin controls."

    user = get_user(telegram_id)
    if not user:
        return False, "User not registered."
    if not user.linked_signer_address:
        return False, "Please link a wallet first."

    trader = get_copy_trader(trader_id)
    if not trader or not trader.get("active"):
        return False, "Trader not found or inactive."

    owner_id = trader.get("owner_user_id")
    if owner_id is not None and int(owner_id) != int(telegram_id):
        return False, "Trader not found or inactive."

    existing = get_user_active_mirrors_v2(telegram_id, network)
    if any(
        int(m.get("trader_id") or 0) == int(trader_id) and m.get("stop_requested")
        for m in existing
    ):
        return False, "Your earlier copy is still closing positions. Wait for it to finish before restarting."

    if margin_per_trade < MIN_MARGIN_PER_TRADE or margin_per_trade > MAX_MARGIN_PER_TRADE:
        return False, f"Margin per trade must be between ${MIN_MARGIN_PER_TRADE} and ${MAX_MARGIN_PER_TRADE}."

    count = count_user_active_mirrors(telegram_id)
    if count >= MAX_MIRRORS_PER_USER:
        return False, f"Maximum {MAX_MIRRORS_PER_USER} simultaneous copy traders allowed."

    mirror_id = create_copy_mirror_v2(
        user_id=telegram_id,
        trader_id=trader_id,
        network=network,
        margin_per_trade=margin_per_trade,
        max_leverage=max_leverage,
        cumulative_stop_loss_pct=cumulative_stop_loss_pct,
        cumulative_take_profit_pct=cumulative_take_profit_pct,
        total_allocated_usd=total_allocated_usd,
    )
    if not mirror_id:
        return False, "Trader is no longer available, or an earlier copy is still closing."

    label = trader.get("label") or trader["wallet_address"][:10]
    # COPY-SESSION: every mirror run is a strategy_sessions row (strategy=
    # 'copy') so Performance history, PnL cards, and the venue-authoritative
    # rollup (volume/fees/funding from trades tagged strategy_session_id)
    # work exactly like any other session. Best-effort: copying still starts
    # if the session insert fails — accounting then degrades to mirror rows.
    try:
        session_id = insert_strategy_session({
            "user_id": telegram_id,
            "strategy": "copy",
            "product_name": label,
            "network": network,
            "config_snapshot": json.dumps({
                "trader_id": trader_id,
                "wallet": trader["wallet_address"],
                "margin_per_trade": margin_per_trade,
                "max_leverage": max_leverage,
                "cumulative_stop_loss_pct": cumulative_stop_loss_pct,
                "cumulative_take_profit_pct": cumulative_take_profit_pct,
                "total_allocated_usd": total_allocated_usd,
            }),
        })
        if session_id:
            set_mirror_session(mirror_id, int(session_id))
    except Exception:  # noqa: BLE001 - session row is reporting infra, not the mirror
        logger.warning("copy session row create failed for mirror %s", mirror_id, exc_info=True)

    return True, (
        f"🔗 Now copying {label}\n"
        f"💰 Margin/Trade: ${margin_per_trade:.0f}\n"
        f"📊 Max Leverage: {max_leverage}x\n"
        f"🛑 Stop Loss: {cumulative_stop_loss_pct}% of ${total_allocated_usd:.0f}\n"
        f"🎯 Take Profit: {cumulative_take_profit_pct}% of ${total_allocated_usd:.0f}"
    )


def _finalize_mirror_session(mirror: dict, stop_reason: str) -> None:
    """Finalize the mirror's strategy_sessions row: venue-authoritative rollup
    (volume/fees from x18-enriched trades tagged with the session id, funding
    window-summed like engine sessions), then mark it stopped. Best-effort —
    a rollup failure never blocks the stop itself."""
    session_id = mirror.get("strategy_session_id")
    if not session_id:
        return
    network = str(mirror.get("network", "mainnet"))
    try:
        rollup_session_from_trades(int(session_id), network)
        rollup_engine_session_pnl_funding(int(session_id), network)
        update_strategy_session(int(session_id), {
            "status": "stopped",
            "stopped_at": datetime.utcnow().isoformat(),
            "stop_reason": stop_reason,
        })
    except Exception:  # noqa: BLE001 - reporting must not block the stop
        logger.warning("copy session finalize failed for session %s", session_id, exc_info=True)


def stop_copy(telegram_id: int, mirror_id: int) -> tuple[bool, str]:
    mirror = get_copy_mirror(mirror_id)
    if not mirror:
        return False, "Mirror not found."
    if mirror["user_id"] != telegram_id:
        return False, "Not your mirror."
    if not mirror.get("active"):
        return False, "Mirror already stopped."
    # COPY-STOP-RACE: make the stop intent visible to the poller BEFORE the
    # flatten starts. The flatten takes seconds (market closes + archive
    # lookups); without the intent a concurrent poll cycle still sees an
    # active, copyable mirror and can re-open the very positions this stop is
    # closing — orphaned the moment stop_copy_mirror() lands below.
    # stop_requested switches the sync path to flatten-only, and the clean
    # stop_copy_mirror() clears it again.
    request_copy_mirror_stop(mirror_id)
    closed_count, _, _, errors = _flatten_mirror_positions(mirror_id, telegram_id, str(mirror.get("network", "mainnet")), reason="user_stop")
    if errors:
        # Do not hide a failed close by marking this mirror inactive. The poller
        # keeps a stop-requested mirror alive solely to retry its flatten; it
        # will not mirror new leader opens while that intent is set.
        return False, (
            "Copy stop is pending because some copied positions could not be closed. "
            "The mirror remains monitored and will retry the close automatically. "
            + "; ".join(errors[:2])
        )
    stop_copy_mirror(mirror_id)
    _clear_mirror_runtime_state(mirror_id)
    _finalize_mirror_session(mirror, "user_stop")
    suffix = f" Flattened {closed_count} copied position(s)." if closed_count > 0 else ""
    return True, f"Copy trading stopped for this trader.{suffix}"


def pause_copy(telegram_id: int, mirror_id: int) -> tuple[bool, str]:
    mirror = get_copy_mirror(mirror_id)
    if not mirror:
        return False, "Mirror not found."
    if mirror["user_id"] != telegram_id:
        return False, "Not your mirror."
    if not mirror.get("active"):
        return False, "Mirror is stopped."
    if mirror.get("stop_requested"):
        return False, "Copy trading is stopping while existing positions are closed."
    if mirror.get("paused"):
        return False, "Mirror is already paused."
    pause_copy_mirror(mirror_id)
    # AUDIT F1 disclosure: a paused mirror is excluded from polling entirely —
    # no leader-close mirroring and NO SL/TP rail. The redeploy stand-down
    # message already says so; the user-initiated pause must too.
    try:
        open_rows = get_open_copy_positions(mirror_id) or []
    except Exception:  # noqa: BLE001 - disclosure is best-effort
        open_rows = []
    if open_rows:
        return True, (
            f"Copy trading paused. ⚠️ {len(open_rows)} open copied position(s) are NOT "
            "monitored while paused — no stop-loss/take-profit and no leader-close "
            "mirroring until you resume or stop."
        )
    return True, "Copy trading paused. New trades will not be mirrored until resumed."


def resume_copy(telegram_id: int, mirror_id: int) -> tuple[bool, str]:
    mirror = get_copy_mirror(mirror_id)
    if not mirror:
        return False, "Mirror not found."
    if mirror["user_id"] != telegram_id:
        return False, "Not your mirror."
    if not mirror.get("active"):
        return False, "Mirror is stopped."
    if mirror.get("stop_requested"):
        return False, "Copy trading is stopping while existing positions are closed."
    if not mirror.get("paused"):
        return False, "Mirror is not paused."
    # Resuming re-arms an automated order path exactly like start_copy, so it
    # needs the same kill switch. This is the path that matters most: boot
    # stand-down pauses EVERY mirror and the notification hands the user a
    # "▶ Resume" button, so post-redeploy this is the only way copy restarts.
    if is_trading_paused():
        return False, "Trading is temporarily paused by admin controls."
    resume_copy_mirror(mirror_id)
    return True, "Copy trading resumed."


def stop_all_copies(telegram_id: int) -> tuple[bool, str]:
    mirrors = get_user_active_mirrors_v2(telegram_id)
    if not mirrors:
        return False, "No active copy mirrors."
    total_closed = 0
    stopped = 0
    pending = 0
    for m in mirrors:
        # COPY-STOP-RACE: intent first (see stop_copy) — a concurrent poll
        # cycle must never re-open positions while this flatten runs.
        request_copy_mirror_stop(int(m["id"]))
        closed_count, _, _, errors = _flatten_mirror_positions(int(m["id"]), telegram_id, str(m.get("network", "mainnet")), reason="user_stop_all")
        total_closed += closed_count
        if errors:
            pending += 1
            continue
        stop_copy_mirror(m["id"])
        _clear_mirror_runtime_state(int(m["id"]))
        _finalize_mirror_session(m, "user_stop_all")
        stopped += 1
    suffix = f" Flattened {total_closed} copied position(s)." if total_closed > 0 else ""
    if pending:
        suffix += f" {pending} mirror(s) remain monitored while failed closes retry."
        # The stop request is retry-safe, but it is not complete. Propagate a
        # non-success result so /stop_all cannot render an all-clear while any
        # copied exposure is still waiting to close.
        return False, f"Stopped {stopped} copy mirror(s).{suffix}"
    return True, f"Stopped {stopped} copy mirror(s).{suffix}"


def get_user_copies(telegram_id: int, network: str = None) -> list[dict]:
    mirrors = get_user_active_mirrors_v2(telegram_id, network=network)
    result = []
    for m in mirrors:
        open_positions = get_open_copy_positions(m["id"])
        wallet = m.get("wallet_address", "")
        wallet_snip = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) >= 10 else wallet
        result.append({
            "mirror_id": m["id"],
            "trader_label": m.get("label") or wallet_snip,
            "wallet": wallet,
            "budget_usd": m.get("total_allocated_usd", 500.0),
            "margin_per_trade": m.get("margin_per_trade", 50.0),
            "max_leverage": m.get("max_leverage", 10.0),
            "cumulative_stop_loss_pct": m.get("cumulative_stop_loss_pct", 50.0),
            "cumulative_take_profit_pct": m.get("cumulative_take_profit_pct", 100.0),
            "total_allocated_usd": m.get("total_allocated_usd", 500.0),
            "cumulative_pnl": float(m.get("cumulative_pnl", 0)),
            "cumulative_fees_usd": float(m.get("cumulative_fees_usd") or 0.0),
            "cumulative_volume_usd": float(m.get("cumulative_volume_usd") or 0.0),
            "unrealized_pnl": float(m.get("last_unrealized_pnl_usd") or 0.0),
            # Rail-consistent net: realized (gross, derived) + last unrealized
            # snapshot - fees. This is exactly the number the SL rail judges
            # (TP judges a conservative uPnL variant and needs two polls).
            "net_pnl": (
                float(m.get("cumulative_pnl", 0))
                + float(m.get("last_unrealized_pnl_usd") or 0.0)
                - float(m.get("cumulative_fees_usd") or 0.0)
            ),
            "strategy_session_id": m.get("strategy_session_id"),
            "open_positions": len(open_positions),
            "paused": bool(m.get("paused")),
            "stop_requested": bool(m.get("stop_requested")),
            "network": m.get("network", "mainnet"),
            "created_at": m.get("created_at"),
        })
    return result


def get_user_mirrors(telegram_id: int) -> list[dict]:
    return get_user_copies(telegram_id)


def _flatten_mirror_positions(mirror_id: int, user_id: int, network: str, reason: str = "user_stop") -> tuple[int, float, float, list[str]]:
    mirror = get_copy_mirror(mirror_id) or {}
    session_id = mirror.get("strategy_session_id")
    open_positions = get_open_copy_positions(mirror_id) or []
    closed = 0
    total_pnl = 0.0
    total_notional = 0.0
    errors: list[str] = []
    # A maker open whose fate is unknown (cancel failed / archive silent) is
    # exposure this flatten cannot see. Refuse to report a clean flatten —
    # every stop path treats errors as "keep retrying via stop_requested",
    # and the poller resolves pendings at the top of each sync.
    for key in list(_PENDING_MAKER_OPENS):
        if key[0] == int(mirror_id):
            errors.append(
                f"product {key[1]}: a maker open is still unresolved "
                "(order fate unknown) — stop will retry"
            )
    for cp in open_positions:
        product_name = cp.get("product_name", "")
        product_key = product_name.replace("-PERP", "")
        size = float(cp.get("size", 0) or 0.0)
        if size <= 0:
            continue
        try:
            result = execute_market_order(
                telegram_id=user_id,
                product=product_key,
                size=size,
                is_long=cp["side"].upper() != "LONG",
                leverage=float(cp.get("leverage", 1.0) or 1.0),
                slippage_pct=1.5,
                enforce_rate_limit=False,
                reduce_only=True,
                source="copy",
                network=network,
                strategy_session_id=session_id,
            )
            ok, close_err = _close_result_ok(result)
            if not ok:
                errors.append(f"{product_name}: {close_err}")
                continue
            pnl, _fee, close_price = _settle_copy_close(mirror_id, cp, result, network, size)
            close_copy_position(cp["id"], pnl=pnl, reason=reason)
            closed += 1
            total_pnl += pnl
            total_notional += size * float(close_price or cp.get("entry_price") or 0.0)
        except Exception as e:
            errors.append(f"{product_name}: {e}")
    return closed, total_pnl, total_notional, errors


def get_available_traders(user_id: int | None = None) -> list[dict]:
    """Traders visible to `user_id`: curated entries + their own privates.

    Callers MUST pass the requesting user's telegram_id. The old signature
    (no user_id) returned the full table and leaked custom wallets between
    users; we now treat the public path as curated-only and require an
    explicit caller for the personal hub.
    """
    traders = get_active_copy_traders(user_id=user_id)
    return [
        {
            "id": t["id"],
            "wallet": t["wallet_address"],
            "label": t.get("label") or t["wallet_address"][:10],
            "is_curated": t.get("is_curated", False),
            "owner_user_id": t.get("owner_user_id"),
            # Snapshot of public leader performance at selection time. It is
            # deliberately separate from get_trader_stats(), which is the
            # viewer's own copy result and must stay private.
            "leader_pnl_usd": t.get("total_pnl_usd"),
            "leader_volume_usd": t.get("total_volume_usd"),
            "leader_win_rate": t.get("win_rate"),
            "leader_roi": t.get("leader_roi"),
            "leader_active_days": t.get("leader_active_days"),
            "leader_period_days": t.get("leader_period_days"),
            "leader_last_activity_at": t.get("leader_last_activity_at"),
            "leader_closed_trades": t.get("leader_closed_trades"),
            "leader_max_drawdown_pct": t.get("leader_max_drawdown_pct"),
        }
        for t in traders
    ]


def get_trader_stats(trader_id: int, user_id: int | None = None) -> dict:
    """Aggregate display metrics for a copy trader across active mirrors.

    ``user_id`` scopes the aggregation to THAT user's mirrors — the hub and
    preview must show each viewer their own results with a trader, never a
    pool of every follower's PnL (curated traders are shared; results are
    not). ``None`` keeps the global aggregate for admin/ops callers.
    """
    mirrors = get_mirrors_for_trader(trader_id) or []
    if user_id is not None:
        mirrors = [m for m in mirrors if int(m.get("user_id") or 0) == int(user_id)]
    stats = {
        "pnl_usd": 0.0,
        "volume_usd": 0.0,
        "win_rate": 0.0,
        "total_trades": 0,
        "filled": 0,
        "failed": 0,
    }
    if not mirrors:
        return stats

    # v2 accounting: the legacy copy_trades table (Hyperliquid-era) is never
    # written by this path, so aggregate from what the mirrors actually track —
    # derived realized PnL net of fees, execution volume, and closed copy
    # positions (win = positive derived PnL on the closed row).
    from src.nadobro.db import query_all as _query_all

    for mirror in mirrors:
        mirror_id = int(mirror.get("id") or 0)
        if mirror_id <= 0:
            continue
        stats["pnl_usd"] += (
            float(mirror.get("cumulative_pnl") or 0.0)
            - float(mirror.get("cumulative_fees_usd") or 0.0)
        )
        stats["volume_usd"] += float(mirror.get("cumulative_volume_usd") or 0.0)
        try:
            rows = _query_all(
                """SELECT pnl FROM copy_positions
                   WHERE mirror_id = %s AND status = 'closed'""",
                (mirror_id,),
            ) or []
        except Exception:  # policy: degrade-ok(stats query; dashboard tolerates undercount)
            rows = []
        for r in rows:
            stats["total_trades"] += 1
            stats["filled"] += 1
            if float(r.get("pnl") or 0.0) > 0:
                stats["wins"] = stats.get("wins", 0) + 1

    if stats["total_trades"] > 0:
        stats["win_rate"] = (stats.get("wins", 0) / stats["total_trades"]) * 100.0
    return stats


def get_trader_preview(trader_id: int, network: str = "mainnet", requester_user_id: int | None = None) -> dict:
    trader = get_copy_trader(trader_id)
    if not trader:
        return {"found": False}
    owner_id = trader.get("owner_user_id")
    if owner_id is not None and (requester_user_id is None or int(owner_id) != int(requester_user_id)):
        return {"found": False}
    wallet = str(trader.get("wallet_address") or "")
    # NO_ORDERS_AUDIT-FIX-R6b: cached.
    from src.nadobro.venue.nado_client import get_or_create_readonly_client
    client = get_or_create_readonly_client(wallet, network)
    positions = client.get_all_positions() or []
    balance = client.get_balance() or {}
    balances = balance.get("balances", {}) or {}
    usdt_balance = float(balances.get(0, balances.get("0", 0.0)) or 0.0)
    total_notional = 0.0
    for pos in positions:
        try:
            size = abs(float(pos.get("amount", 0) or 0.0))
            entry = float(
                pos.get("entry_price") or pos.get("avg_entry_price")
                or pos.get("price") or 0.0
            )
            total_notional += size * entry
        except Exception:  # policy: degrade-ok(malformed position row; preview tolerates undercount)
            continue
    return {
        "found": True,
        "wallet": wallet,
        "label": trader.get("label") or wallet[:10],
        "equity_usd": usdt_balance,
        "open_positions": len([p for p in positions if abs(float(p.get("amount", 0) or 0.0)) > 0]),
        "gross_notional_usd": total_notional,
        "network": network,
    }


# ─── Position Monitoring Loop ─────────────────────────────────

async def start_copy_polling():
    """Start the background polling loop for copy trading."""
    global _poll_task
    if _poll_task and not _poll_task.done():
        return
    _poll_task = asyncio.create_task(_poll_loop(), name="nadobro-copy-poll")
    logger.info("Copy trading polling loop started (interval=%ds)", POLL_INTERVAL_SECONDS)


async def stop_copy_polling():
    global _poll_task
    task = _poll_task
    _poll_task = None
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        logger.info("Copy trading polling loop stopped")


def get_copy_polling_diagnostics() -> dict:
    task = _poll_task
    return {
        "enabled": bool(task),
        "running": bool(task and not task.done()),
        "interval_seconds": int(POLL_INTERVAL_SECONDS),
        "task_name": str(task.get_name()) if task else "",
    }


async def boot_stand_down_mirrors() -> int:
    """Redeploy rule: boot NEVER auto-resumes trading — pause every active,
    unpaused mirror so nothing mirrors until its owner taps Resume.

    Paused mirrors are excluded from polling entirely, which also means any
    still-open copied positions are unmonitored (no leader-close mirroring,
    no rail) until resumed — the notification says so explicitly and carries
    the same Resume/Stop controls as the copy dashboard. Returns the number
    of mirrors paused.
    """
    # F-3: restore maker opens whose fate was unknown at the last shutdown into
    # the in-memory set. A restored pending is a possibly-resting live order;
    # the poll loop's pending-resolution pass (_poll_all_mirrors) cancels it or
    # books its fill regardless of whether the mirror ends up paused here — so
    # pausing below is safe and does NOT strand the order (the earlier boot
    # re-arm only covered mirrors the active-query returned, missing paused
    # ones — audit F3-1).
    await run_blocking(_load_pending_maker_opens)
    if _PENDING_MAKER_OPENS:
        logger.warning(
            "boot: %d mirror(s) carry restored pending maker opens — the poller "
            "will resolve them: %s",
            len({k[0] for k in _PENDING_MAKER_OPENS}), sorted(_PENDING_MAKER_OPENS),
        )

    mirrors = await run_blocking(get_all_active_mirrors_v2)
    # A failed user stop is intentionally included in the poller even if it
    # used to be paused. It must never be paused again on redeploy: its
    # flatten-only retry path is the remaining safety mechanism.
    mirrors = [m for m in (mirrors or []) if not m.get("stop_requested")]

    if not mirrors:
        return 0

    by_user: dict[int, list[dict]] = {}
    for m in mirrors:
        by_user.setdefault(int(m["user_id"]), []).append(m)

    paused = 0
    for user_id, user_mirrors in by_user.items():
        for m in user_mirrors:
            try:
                await run_blocking(pause_copy_mirror, int(m["id"]))
                paused += 1
            except Exception:  # noqa: BLE001 - one bad row must not block the boot
                logger.warning("boot stand-down: pause failed for mirror %s", m.get("id"), exc_info=True)
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            lines = [
                "⏸ Copy trading paused (bot redeploy)",
                "Redeploys never auto-resume trading. Your copy mirror(s) are "
                "paused until you resume them:",
            ]
            rows = []
            for m in user_mirrors:
                wallet = str(m.get("wallet_address") or "")
                label = m.get("label") or (f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) >= 10 else wallet)
                open_rows = await run_blocking(get_open_copy_positions, int(m["id"])) or []
                lines.append(
                    f"• {label} ({str(m.get('network', 'mainnet')).upper()})"
                    + (f" — ⚠️ {len(open_rows)} open position(s) NOT monitored while paused" if open_rows else "")
                )
                rows.append([
                    InlineKeyboardButton(f"▶ Resume {label}"[:60], callback_data=f"copy:resume:{m['id']}"),
                    InlineKeyboardButton("🛑 Stop", callback_data=f"copy:stop:{m['id']}"),
                ])
            await _notify_user(user_id, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        except Exception:  # noqa: BLE001 - notify is best-effort; the stand-down already happened
            logger.warning("boot stand-down: notify failed for user %s", user_id, exc_info=True)
    return paused


async def _poll_loop():
    """Main polling loop: check all active mirrors and sync positions."""
    while True:
        try:
            await _poll_all_mirrors()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Copy polling error: %s", e, exc_info=True)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _global_pause_blocks_opens() -> bool:
    """True when this poll cycle must not commit any NEW or INCREASED position.

    Read ONCE per poll cycle and threaded down, rather than at each commit point:
    ``is_trading_paused`` is an uncached ``bot_state`` read, and the open path runs
    per product per mirror. The cost is up to one poll interval of lag on a pause
    that lands mid-cycle, which is acceptable for an operator kill switch (unlike
    a user's stop, which still gets its own commit-point re-read — COPY-STOP-RACE).

    Fail-CLOSED: an unreadable switch counts as paused. Skipping an open is safe
    (the next poll retries); trading through the kill switch is not.
    """
    try:
        return bool(await run_blocking(is_trading_paused))
    except Exception:  # noqa: BLE001 - unreadable switch is treated as paused
        logger.warning("copy opens blocked: global pause state unreadable")
        return True


async def _poll_all_mirrors():
    """Poll all active mirrors and process position changes."""
    synced_ids: set[int] = set()
    mirrors = await run_blocking(get_all_active_mirrors_v2)
    # One read per cycle, applied to every mirror below. Blocks opens/scale-ups
    # only; the stop-retry pass above and leader-close mirroring stay alive.
    opens_blocked = await _global_pause_blocks_opens()

    # Group mirrors by trader+network for efficient polling
    trader_groups: dict[str, list[dict]] = {}
    for m in mirrors or []:
        key = f"{m['trader_id']}:{m.get('network', 'mainnet')}"
        trader_groups.setdefault(key, []).append(m)

    for group_key, group_mirrors in trader_groups.items():
        trader_id = group_mirrors[0]["trader_id"]
        network = group_mirrors[0].get("network", "mainnet")
        wallet = group_mirrors[0].get("wallet_address", "")

        # A requested stop does not need the leader's current portfolio. Run
        # its flatten-only retry first so a leader-read outage cannot strand
        # the follower's open exposure.
        stopping_mirrors = [m for m in group_mirrors if m.get("stop_requested")]
        for mirror in stopping_mirrors:
            synced_ids.add(int(mirror["id"]))
            try:
                await _sync_mirror_positions(mirror, {})
            except Exception as e:
                logger.error(
                    "Copy stop retry failed for mirror %s user %s: %s",
                    mirror["id"], mirror["user_id"], e, exc_info=True,
                )

        copying_mirrors = [m for m in group_mirrors if not m.get("stop_requested")]
        if not copying_mirrors:
            continue

        try:
            leader_pos_map = await run_blocking(_load_leader_position_map, trader_id, wallet, network)
            # COPY-LEADER-READ-FLAP: decide ONCE per trader group whether an empty
            # read may be believed. Untrusted => mirror no closes this cycle.
            leader_trusted = await run_blocking(
                _leader_read_is_trustworthy, trader_id, network, leader_pos_map,
            )

            # Process each mirror
            for mirror in copying_mirrors:
                synced_ids.add(int(mirror["id"]))
                try:
                    await _sync_mirror_positions(
                        mirror, leader_pos_map, opens_blocked=opens_blocked,
                        leader_trusted=leader_trusted,
                    )
                except Exception as e:
                    logger.error(
                        "Copy sync failed for mirror %s user %s: %s",
                        mirror["id"], mirror["user_id"], e, exc_info=True,
                    )

        except Exception as e:
            logger.error("Failed to poll trader %s on %s: %s", wallet[:10], network, e)

    # AUDIT F3-1 / F2-PAUSE-PENDING-ORPHAN: resolve pending maker opens for any
    # mirror that carries one but was NOT synced above — a PAUSED mirror (the
    # active-mirror query excludes it) or one whose trader was removed. A
    # pending is a possibly-resting live order; leaving it unresolved because
    # the mirror is paused is the exact naked-fill risk the persistence exists
    # to prevent. Resolution cancels the order or books its fill; it never
    # mirrors new leader opens, so it respects pause semantics.
    orphan_pending_ids = {k[0] for k in _PENDING_MAKER_OPENS} - synced_ids
    for mirror_id in orphan_pending_ids:
        try:
            mirror = await run_blocking(get_copy_mirror, mirror_id)
            if not mirror:
                # No mirror row at all — drop the dangling pending so it can't
                # block forever. (A real orphan is still flagged by reconcile.)
                await run_blocking(_clear_mirror_runtime_state, mirror_id)
                continue
            await _resolve_pending_maker_opens_for_mirror(mirror)
        except Exception as e:
            logger.error(
                "Copy pending-only resolution failed for mirror %s: %s",
                mirror_id, e, exc_info=True,
            )


def _leader_snapshot_had_positions(trader_id: int, network: str) -> bool:
    """Did the last persisted leader snapshot carry any open positions?

    Fail-CLOSED for the caller's purpose: on any read/parse error return True, i.e.
    "assume the leader had positions", so an unreadable snapshot makes the current
    empty read UNTRUSTED rather than licensing a mass close.
    """
    try:
        snap = get_latest_copy_snapshot(int(trader_id), str(network))
        if not snap:
            return False
        raw = snap.get("positions") if isinstance(snap, dict) else None
        if raw is None:
            raw = snap.get("snapshot") if isinstance(snap, dict) else None
        if isinstance(raw, str):
            raw = json.loads(raw or "[]")
        return bool(raw)
    except Exception:  # noqa: BLE001 - unreadable basis => treat as "had positions"
        logger.debug("leader snapshot read failed", exc_info=True)
        return True


def _leader_read_is_trustworthy(trader_id: int, network: str, leader_pos_map: dict) -> bool:
    """Can ``pid not in leader_pos_map`` be believed to mean "leader closed"?

    A NON-empty read is self-evidently real. An EMPTY read is only believable when
    the archive is healthy AND the leader had nothing last time — otherwise it is
    indistinguishable from the two silent ``[]`` failure modes in
    ``NadoClient.get_all_positions``. The follower side already has exactly these
    guards (``_archive_reads_unreliable``, the mass-vanish window); the leader side
    had none, which is COPY-LEADER-READ-FLAP.
    """
    if leader_pos_map:
        return True
    if _archive_reads_unreliable(str(network)):
        return False
    return not _leader_snapshot_had_positions(trader_id, network)


def _load_leader_position_map(trader_id: int, wallet: str, network: str) -> dict:
    # NO_ORDERS_AUDIT-FIX-R6b: cached.
    from src.nadobro.venue.nado_client import get_or_create_readonly_client
    leader_client = get_or_create_readonly_client(wallet, network)
    leader_positions = leader_client.get_all_positions() or []
    leader_pos_map = {}
    for pos in leader_positions:
        pid = int(pos.get("product_id", -1))
        amount = float(pos.get("amount", 0) or 0)
        if abs(amount) <= 0:
            continue
        side = pos.get("side", "").upper()
        if not side:
            side = "LONG" if amount > 0 else "SHORT"
        leader_pos_map[pid] = {
            "product_id": pid,
            "side": side,
            "size": abs(amount),
            # The venue client publishes the average entry under "price"
            # ("entry_price" is a newer alias) — read both. Every open is
            # gated on entry > 0, so missing this key silently killed all
            # copy opens.
            "entry_price": float(pos.get("entry_price") or pos.get("price") or 0),
            "unrealized_pnl": float(pos.get("unrealized_pnl", 0) or 0),
            # COPY-LEVERAGE fix: capture the leader's leverage so the follower can
            # mirror the leader's risk profile instead of always using its own
            # max. 0.0 when the venue doesn't report it (we then fall back).
            "leverage": float(pos.get("leverage", 0) or 0),
        }
        try:
            # Venue open orders currently expose no order type (rows carry
            # digest/amount/price/side only) and trigger orders don't appear
            # in the book at all, so leader TP/SL is undetectable today —
            # this scan engages only if the venue starts publishing a type.
            # Followers stay protected by close/partial mirroring + the
            # cumulative rail regardless.
            orders = leader_client.get_open_orders(pid) or []
            tp_price = None
            sl_price = None
            for o in orders:
                otype = (o.get("order_type") or o.get("type") or "").lower()
                if "take_profit" in otype or "tp" in otype:
                    tp_price = float(o.get("price", 0) or 0)
                elif "stop_loss" in otype or "sl" in otype:
                    sl_price = float(o.get("price", 0) or 0)
            leader_pos_map[pid]["tp_price"] = tp_price
            leader_pos_map[pid]["sl_price"] = sl_price
        except Exception as e:
            logger.debug("Failed to fetch orders for product %s: %s", pid, e)
    # COPY-LEADER-READ-FLAP: never overwrite a non-empty snapshot with an EMPTY
    # read. ``get_all_positions()`` returns [] WITHOUT raising on a total read
    # failure and again when isolated-subaccount discovery fails, and downstream
    # ``pid not in leader_pos_map`` is read as "the leader closed this" — so one
    # flapped read market-flattens the follower's entire book. The persisted
    # snapshot is the only basis for telling "leader is genuinely flat" apart from
    # "we cannot see the leader right now", so it must survive the suspect read.
    if not leader_pos_map and _leader_snapshot_had_positions(trader_id, network):
        logger.warning(
            "copy: leader %s (%s) read EMPTY but the last snapshot had positions — "
            "keeping the snapshot and treating this read as untrusted",
            str(wallet)[:10], network,
        )
        return leader_pos_map
    try:
        save_copy_snapshot(trader_id, network, json.dumps(list(leader_pos_map.values())))
    except Exception as e:
        logger.warning(
            "copy snapshot not persisted for trader %s (%s) — dashboard may show stale leader state: %s",
            trader_id, network, e,
        )
    return leader_pos_map


def _compute_copy_sizing(
    *,
    leader_size: float,
    leader_entry: float,
    leader_leverage: float,
    leader_max_notional: float,
    margin_per_trade: float,
    max_leverage: float,
    product_max_leverage: float,
    entry_fraction: float = 1.0,
) -> tuple[float, float]:
    """Return ``(copy_size_base, leverage)`` for a mirrored position.

    COPY-SIZE fix: the old formula was ``margin_per_trade * leverage / entry`` —
    a FIXED notional that ignored the leader's actual size, so a leader's tiny
    probe and their max-conviction position were copied identically. We now scale
    the committed margin by this position's conviction = its notional as a
    fraction of the leader's LARGEST open position (their biggest bet = full
    ``margin_per_trade``; a 10%-size probe = 10% of it), capped at
    ``margin_per_trade``.

    COPY-LEVERAGE fix: leverage mirrors the leader's, capped by the user's max
    and the product max; falls back to ``min(max, product_max)`` when the venue
    doesn't report the leader's leverage.
    """
    if leader_entry <= 0 or leader_size <= 0:
        return 0.0, 0.0
    lev = min(float(max_leverage), float(product_max_leverage))
    if leader_leverage and leader_leverage > 0:
        lev = min(float(leader_leverage), float(max_leverage), float(product_max_leverage))
    lev = max(1.0, lev)
    leader_notional = leader_size * leader_entry
    if leader_max_notional and leader_max_notional > 0:
        weight = min(1.0, leader_notional / leader_max_notional)
    else:
        weight = 1.0
    # ``entry_fraction`` < 1.0 holds budget back for mirroring leader adds
    # (COPY-NO-SCALE-UP). The full-budget ceiling is entry_fraction=1.0.
    copy_margin = max(0.0, float(margin_per_trade) * weight * float(entry_fraction))
    copy_size = (copy_margin * lev) / leader_entry
    return copy_size, lev


def _entry_deviation_too_far(mid: float, leader_entry: float, max_dev_pct: float) -> bool:
    """COPY-NO-SLIPPAGE gate: True when the follower would enter too far from the
    leader's entry. With a 30s poll, price can move a lot between the leader's
    fill and ours; opening anyway means systematically buying higher / selling
    lower than the trader we copy. We skip (and retry next poll) when the current
    mid deviates from ``leader_entry`` by more than ``max_dev_pct`` percent.
    Unknown/zero inputs => allow (can't assess, don't block)."""
    try:
        mid = float(mid)
        leader_entry = float(leader_entry)
        max_dev_pct = float(max_dev_pct)
    except (TypeError, ValueError):
        return False
    if mid <= 0 or leader_entry <= 0 or max_dev_pct <= 0:
        return False
    return abs(mid - leader_entry) / leader_entry * 100.0 > max_dev_pct


def _leader_max_notional(leader_pos_map: dict) -> float:
    """Largest single-position notional in the leader's book (the conviction
    anchor for proportional copy sizing)."""
    best = 0.0
    for p in (leader_pos_map or {}).values():
        try:
            n = float(p.get("size", 0) or 0) * float(p.get("entry_price", 0) or 0)
        except (TypeError, ValueError):
            n = 0.0
        if n > best:
            best = n
    return best


async def _sync_mirror_positions(
    mirror: dict, leader_pos_map: dict, *, opens_blocked: bool = False,
    leader_trusted: bool = True,
) -> None:
    """Sync a single mirror's positions with the leader's current positions.

    ``opens_blocked`` (global admin pause, resolved once per poll cycle) stops
    NEW and INCREASED positions only. Leader-close mirroring, the SL/TP rail and
    pending-maker resolution stay alive — the same "closing is safety" asymmetry
    ``stop_requested`` already uses. Defaults False so a direct call (tests,
    stop-retry path) behaves exactly as before.

    ``leader_trusted`` (COPY-LEADER-READ-FLAP) is False when an EMPTY leader read
    cannot be distinguished from a silent venue read failure. It suppresses
    leader-CLOSE mirroring only — a genuine leader close is re-detected on the next
    healthy poll, whereas acting on a phantom one flattens the whole book at market.
    """
    mirror_id = mirror["id"]
    user_id = mirror["user_id"]
    network = mirror.get("network", "mainnet")
    margin_per_trade = float(mirror.get("margin_per_trade", 50.0))
    max_leverage = float(mirror.get("max_leverage", 10.0))
    cumulative_stop_loss_pct = float(mirror.get("cumulative_stop_loss_pct", 50.0))
    cumulative_take_profit_pct = float(mirror.get("cumulative_take_profit_pct", 100.0))
    total_allocated = float(mirror.get("total_allocated_usd", 500.0))
    cumulative_pnl = float(mirror.get("cumulative_pnl", 0.0))

    if mirror.get("stop_requested"):
        # A maker open with an unknown fate must resolve (booking any fill as
        # a tracked row) BEFORE the flatten reads open positions — otherwise
        # the stop completes while an order/fill is still out there.
        await _resolve_pending_maker_opens_for_mirror(mirror)
        closed_count, total_pnl, _, errors = await run_blocking(
            _flatten_mirror_positions,
            mirror_id,
            user_id,
            str(network),
            "user_stop",
        )
        if errors:
            logger.error(
                "copy stop retry incomplete for mirror %s (%s)",
                mirror_id,
                "; ".join(errors[:3]),
            )
            return
        await run_blocking(stop_copy_mirror, mirror_id)
        await run_blocking(_clear_mirror_runtime_state, mirror_id)
        await run_blocking(_finalize_mirror_session, mirror, "user_stop")
        await _notify_user(
            user_id,
            "✅ Copy trading stopped after closing "
            f"{closed_count} copied position(s) for ${total_pnl:+,.2f}.",
        )
        return

    session_id = mirror.get("strategy_session_id")

    # Resolve maker opens with an unknown fate FIRST: a fill that landed after
    # the wait window books here as a tracked row, so the sweep, the uPnL and
    # the rail below all see it this same poll.
    await _resolve_pending_maker_opens_for_mirror(mirror)

    # Load tracked rows and the follower's REAL on-venue positions FIRST — the
    # bracket-fill sweep and the rail's unrealized PnL both need them (the old
    # code evaluated the rail before any live data was loaded).
    open_copy_positions = await run_blocking(get_open_copy_positions, mirror_id)
    copy_pos_by_product = {}
    for cp in open_copy_positions:
        copy_pos_by_product[cp["product_id"]] = cp

    # COPY-VENUE-RECONCILE: best-effort — if the client/read is unavailable we
    # degrade to DB-only behavior (no sweep, realized-only rail) rather than
    # block copying.
    max_entry_deviation_pct = float(mirror.get("max_entry_deviation_pct", 1.5) or 1.5)
    venue_pos_by_product: dict = {}
    venue_read_ok = False
    try:
        # A mirror is explicitly network-scoped. A user changing the app's
        # current network must not disable another mirror; use the mirror's
        # persisted network for its read/reconcile client as well as orders.
        follower_client = await run_blocking(get_user_nado_client, user_id, network=network)
    except Exception as e:  # noqa: BLE001 - reconcile is best-effort; degrade to DB-only
        logger.debug("copy follower client unavailable for reconcile: %s", e)
        follower_client = None
    if follower_client is not None:
        try:
            for vp in (await run_blocking(follower_client.get_all_positions) or []):
                try:
                    if abs(float(vp.get("amount", 0) or 0)) > 0:
                        venue_pos_by_product[int(vp.get("product_id", -1))] = vp
                except (TypeError, ValueError):
                    continue
            venue_read_ok = True
        except Exception as e:  # noqa: BLE001 - reconcile is best-effort
            logger.debug("copy venue-position reconcile read failed: %s", e)

    # F-4: is this venue read trustworthy enough to OPEN against? The
    # duplicate-open guard below keys off ``venue_pos_by_product``; a silent
    # empty read (client missing, read raised, or every tracked position
    # absent when we believe we hold them) defeats it and would stack a
    # duplicate. Opening is the only irreversible action gated on the read, so
    # it gets the strict test; the sweep/rail have their own guards.
    # F4-1: an archive rate-limit/circuit-open also makes ISOLATED positions
    # silently vanish from get_all_positions (isolated-subaccount discovery
    # returns [] then), so the follower could already hold the product we're
    # about to open on an isolated child — the read is untrustworthy for opens
    # regardless of how many cross-margin rows came back.
    venue_read_suspect = (
        follower_client is None
        or not venue_read_ok
        or _archive_reads_unreliable(str(network))
        or (bool(copy_pos_by_product) and not venue_pos_by_product)
    )

    # COPY-BRACKET-SWEEP: book venue-side closes nobody reported — a TP/SL
    # bracket that FILLED on the venue, a manual close, or a liquidation.
    # Without this, a filled stop-loss left a phantom open row, zero booked
    # PnL, and a rail that never saw the loss.
    swept_fees = 0.0
    if venue_read_ok:
        swept_pnl, swept_fees = await _sweep_external_closes(
            mirror, copy_pos_by_product, venue_pos_by_product, follower_client
        )
        cumulative_pnl += swept_pnl

    # Unrealized PnL of the surviving open copy rows — venue uPnL scaled to
    # the copy's share of the venue position, mid-price fallback. The
    # conservative estimate is only computed when a TP rail can consume it.
    upnl, upnl_conservative = await _compute_open_upnl(
        copy_pos_by_product, venue_pos_by_product, follower_client,
        conservative=cumulative_take_profit_pct > 0,
    )
    try:
        await run_blocking(set_mirror_unrealized, mirror_id, upnl)
    except Exception:  # noqa: BLE001 - display cache only
        logger.debug("copy set_mirror_unrealized failed", exc_info=True)

    # COPY-RAIL: judged like every session rail in this product — live PnL
    # INCLUDING unrealized, NET of fees, as % of the allocated amount.
    # AUDIT F3: swept closes booked THIS poll already added their pnl to
    # cumulative_pnl above; their fees must land in the same poll's basis too,
    # or net is overstated by exactly one close's fee until the next poll.
    cumulative_fees = float(mirror.get("cumulative_fees_usd") or 0.0) + swept_fees
    net_pnl = cumulative_pnl + upnl - cumulative_fees
    if total_allocated > 0:
        # SL: unchanged — immediate, on the primary estimator. Firing a stop
        # early costs opportunity; firing it late costs money.
        reason = _rail_decision(net_pnl, total_allocated, cumulative_stop_loss_pct, 0.0)
        rail_net = net_pnl
        rail_upnl = upnl
        if reason is None and cumulative_take_profit_pct > 0:
            # COPY-TP-CONFIRM: taking profit is IRREVERSIBLE and the numerator
            # rests on venue reads that can spike or misparse for one poll
            # (x18 heuristics, alias probing, mark blips). Judge TP on the
            # conservative numerator (per-position min of venue-scaled and
            # mid-derived uPnL) and require the breach to survive two polls.
            # A real +50% doesn't vanish in 30 seconds; a phantom one does.
            net_conservative = cumulative_pnl + upnl_conservative - cumulative_fees
            tp_hit = _rail_decision(
                net_conservative, total_allocated, 0.0, cumulative_take_profit_pct
            ) == "auto_take_profit"
            if _tp_breach_confirmed(mirror_id, tp_hit):
                reason = "auto_take_profit"
                rail_net = net_conservative
                rail_upnl = upnl_conservative
            elif tp_hit:
                logger.info(
                    "copy TP breach pending confirmation for mirror %s "
                    "(net $%.2f conservative, $%.2f primary, target %.1f%% of $%.0f)",
                    mirror_id, net_conservative, net_pnl,
                    cumulative_take_profit_pct, total_allocated,
                )
        else:
            _tp_breach_confirmed(mirror_id, False)
        pnl_pct = (rail_net / total_allocated) * 100
        if reason is not None:
            if reason == "auto_stop_loss":
                title = "🛑 Copy Trading Auto-Stopped"
                detail = f"Cumulative loss hit {abs(pnl_pct):.1f}% (limit: {cumulative_stop_loss_pct}%)"
            else:
                title = "🎯 Copy Trading Auto-Stopped — Target Hit!"
                detail = f"Cumulative profit hit {pnl_pct:.1f}% (target: {cumulative_take_profit_pct}%)"
            closed_count, total_pnl, _, errors = await run_blocking(
                _flatten_mirror_positions, mirror_id, user_id, str(network), reason,
            )
            if errors:
                # A rail fired but the flatten is incomplete: keep the mirror
                # ACTIVE so the next poll retries the flatten (stopping now
                # would orphan live positions with no monitor).
                logger.error(
                    "copy rail %s flatten incomplete for mirror %s (%s) — retrying next poll",
                    reason, mirror_id, "; ".join(errors[:3]),
                )
            else:
                await run_blocking(auto_stop_mirror, mirror_id, detail)
                await run_blocking(_clear_mirror_runtime_state, mirror_id)
                await run_blocking(_finalize_mirror_session, mirror, reason)
            await _notify_user(
                user_id,
                (
                    f"{title}\n{detail}\n"
                    f"Net P&L: ${rail_net:+,.2f} (realized ${cumulative_pnl:+,.2f}, "
                    f"unrealized ${rail_upnl:+,.2f}, fees ${cumulative_fees:,.2f}) "
                    f"/ ${total_allocated:,.0f} allocated\n"
                    + (f"Closed {closed_count} copied position(s) for ${total_pnl:+,.2f}.\n" if closed_count > 0 else "")
                    + (f"⚠️ Some closes failed and will be retried: {'; '.join(errors[:2])}" if errors else "")
                )
            )
            return

    # 1. Close positions that leader has closed
    #
    # COPY-LEADER-READ-FLAP: absence from leader_pos_map is only evidence of a
    # close when the read itself is believable. An untrusted (empty, unconfirmable)
    # read would otherwise flatten EVERY copied position at 1.5% slippage market
    # and book phantom leader_closed PnL — then re-open the same book on the next
    # good poll. Skip the close pass entirely; a real close is picked up next cycle.
    if not leader_trusted:
        logger.warning(
            "copy: skipping leader-close mirroring for mirror %s — leader read "
            "untrusted this cycle", mirror_id,
        )
    for pid, cp in ([] if not leader_trusted else list(copy_pos_by_product.items())):
        if pid not in leader_pos_map:
            # Leader closed this position — close ours too
            product_name = cp.get("product_name") or get_product_name(pid, network=network)
            product_key = product_name.replace("-PERP", "")
            try:
                # Close by opening opposite side
                is_close_long = cp["side"].upper() != "LONG"
                result = await run_blocking(
                    execute_market_order,
                    telegram_id=user_id,
                    product=product_key,
                    size=float(cp.get("size", 0)),
                    is_long=is_close_long,
                    leverage=float(cp.get("leverage", 1.0)),
                    slippage_pct=1.5,
                    enforce_rate_limit=False,
                    reduce_only=True,
                    source="copy",
                    network=network,
                    strategy_session_id=session_id,
                )
                ok, close_err = _close_result_ok(result)
                if not ok:
                    # A duplicate-suppressed result must NOT be settled (it
                    # closed nothing / carries another submit's fill). Leave
                    # the row open; next poll retries once the intent frees.
                    logger.warning("Failed to close copy position %s: %s", cp["id"], close_err)
                    continue
                pnl, fee, _price = await run_blocking(
                    _settle_copy_close, mirror_id, cp, result, network, float(cp.get("size", 0))
                )
                await run_blocking(close_copy_position, cp["id"], pnl=pnl, reason="leader_closed")
                # Auto-post the Type A PnL card (+ Share button); fall back to
                # the plain text notification if the card can't render.
                _caption = (
                    f"📋 Copy trade closed — {product_name}\n"
                    f"P&L: ${pnl:+,.2f}" + (f" (fees ${fee:,.2f})" if fee else "")
                )
                if not await _post_copy_close_card(user_id, network, cp["id"], _caption):
                    await _notify_user(
                        user_id,
                        f"📋 Copy Position Closed\n"
                        f"{product_name}: Leader closed → Your position closed\n"
                        f"P&L: ${pnl:+,.2f}" + (f" (fees ${fee:,.2f})" if fee else "")
                    )
            except Exception as e:
                logger.error("Failed to close copy position %s: %s", cp["id"], e)

    # 2. Open new positions that leader has opened
    for pid, leader_pos in leader_pos_map.items():
        if pid in copy_pos_by_product:
            # Already tracking this position — update TP/SL if changed
            existing = copy_pos_by_product[pid]
            if existing["side"].upper() != leader_pos["side"].upper():
                product_name = existing.get("product_name") or get_product_name(pid, network=network)
                product_key = product_name.replace("-PERP", "")
                try:
                    close_res = await run_blocking(
                        execute_market_order,
                        telegram_id=user_id,
                        product=product_key,
                        size=float(existing.get("size", 0)),
                        is_long=existing["side"].upper() != "LONG",
                        leverage=float(existing.get("leverage", 1.0)),
                        slippage_pct=1.5,
                        enforce_rate_limit=False,
                        reduce_only=True,
                        source="copy",
                        network=network,
                        strategy_session_id=session_id,
                    )
                    ok, close_err = _close_result_ok(close_res)
                    if not ok:
                        logger.error("Failed to flip copy position %s: %s", existing["id"], close_err)
                        continue
                    pnl, _fee, _price = await run_blocking(
                        _settle_copy_close, mirror_id, existing, close_res, network,
                        float(existing.get("size", 0)),
                    )
                    await run_blocking(close_copy_position, existing["id"], pnl=pnl, reason="leader_flipped_side")
                except Exception as e:
                    logger.error("Failed to flip copy position %s: %s", existing["id"], e)
                    continue
                copy_pos_by_product.pop(pid, None)
            else:
                resized = await _mirror_partial_close_if_needed(
                    existing, leader_pos, user_id, mirror_id, network,
                    session_id=session_id,
                )
                if resized == "closed":
                    copy_pos_by_product.pop(pid, None)
                    continue
                if not resized:
                    # COPY-NO-SCALE-UP: the leader may have ADDED. Nothing else
                    # in this loop reacted to a growing leader position, so the
                    # bot mirrored only their first entry.
                    resized = await _mirror_leader_add_if_needed(
                        existing, leader_pos, user_id, mirror_id, network,
                        margin_per_trade=margin_per_trade,
                        max_leverage=max_leverage,
                        leader_max_notional=_leader_max_notional(leader_pos_map),
                        session_id=session_id,
                        opens_blocked=opens_blocked,
                    )
                await run_blocking(
                    _update_tp_sl_if_changed, existing, leader_pos, user_id, network,
                    force=(resized in ("reduced", "added")), session_id=session_id,
                )
                continue

        # New position from leader — open a copy
        product_name = get_product_name(pid, network=network)
        product_key = product_name.replace("-PERP", "")
        is_long = leader_pos["side"] == "LONG"
        leader_entry = leader_pos.get("entry_price", 0)

        if leader_entry <= 0:
            continue

        # F-4: never open against a venue read we can't trust — the
        # duplicate-open guard below relies on it, so a silent empty read would
        # let us stack a duplicate on a product we may already hold.
        if venue_read_suspect:
            logger.warning(
                "copy skip open: venue position read unavailable/suspect for mirror "
                "%s — refusing to open %s blind (duplicate risk); retry next poll",
                mirror_id, product_key,
            )
            continue

        # COPY-VENUE-RECONCILE: the follower already holds this product on-venue
        # but we have no DB record of it — opening again would stack a duplicate /
        # orphaned position outside cumulative accounting. Skip; a manual/orphan
        # position must be reconciled before we copy it.
        if pid in venue_pos_by_product:
            logger.warning(
                "copy skip open: follower already holds product %s on-venue with no "
                "tracked copy row (mirror %s) — reconcile before copying",
                pid, mirror_id,
            )
            continue

        # COPY-NO-SLIPPAGE: skip a late entry that's drifted too far from the
        # leader's fill (we'd buy higher / sell lower than the trader we copy).
        if follower_client is not None:
            try:
                _mp = await run_blocking(follower_client.get_market_price, pid)
                _mid = float((_mp or {}).get("mid") or 0.0)
            except Exception:  # noqa: BLE001 - price read best-effort; allow if unknown
                _mid = 0.0
            if _entry_deviation_too_far(_mid, float(leader_entry or 0), max_entry_deviation_pct):
                logger.info(
                    "copy skip open: %s mid %.6f deviates >%.2f%% from leader entry %.6f "
                    "(mirror %s) — waiting for a closer price",
                    product_key, _mid, max_entry_deviation_pct, float(leader_entry), mirror_id,
                )
                continue

        # COPY-SIZE + COPY-LEVERAGE fix: mirror the leader's conviction (size as a
        # fraction of their largest position) and leverage, capped by the user's
        # per-trade budget and max leverage — not a fixed max-leverage notional.
        product_max_lev = get_product_max_leverage(product_key, network=network)
        copy_size, leverage = _compute_copy_sizing(
            leader_size=float(leader_pos.get("size", 0) or 0),
            leader_entry=float(leader_entry or 0),
            leader_leverage=float(leader_pos.get("leverage", 0) or 0),
            leader_max_notional=_leader_max_notional(leader_pos_map),
            margin_per_trade=margin_per_trade,
            max_leverage=max_leverage,
            product_max_leverage=product_max_lev,
            # Hold budget back so a leader scaling in can actually be mirrored.
            entry_fraction=COPY_INITIAL_ENTRY_FRACTION,
        )

        if copy_size <= 0:
            continue

        # COPY-PENDING-MAKER: a previous maker order on this product has an
        # unresolved fate (cancel failed / archive silent). Never place a new
        # order while it exists — a live resting order plus a fresh order is
        # double exposure. Resolution already ran earlier this sync.
        if (int(mirror_id), int(pid)) in _PENDING_MAKER_OPENS:
            continue

        try:
            # COPY-STOP-RACE: the mirror snapshot driving this sync can be many
            # seconds stale by now (venue reads above). Re-read immediately
            # before committing an open — a stop/pause that landed mid-sync
            # must win, or the stop's flatten races the very order we place.
            live = await run_blocking(get_copy_mirror, mirror_id) or {}
            if not live.get("active") or live.get("stop_requested") or live.get("paused"):
                logger.info(
                    "copy opens aborted: mirror %s stopped/paused mid-sync", mirror_id
                )
                return
            # GLOBAL PAUSE: strategies stand down mid-flight (bot_runtime
            # _run_cycle -> "maintenance_pause"), so copy must too, or the admin
            # kill switch suspends every strategy while active mirrors keep
            # opening — which needs no user action at all.
            if opens_blocked:
                logger.info(
                    "copy opens blocked: global trading pause active (mirror %s)",
                    mirror_id,
                )
                return

            if MAKER_OPENS_ENABLED:
                result = await run_blocking(
                    _execute_maker_open,
                    user_id=user_id,
                    product_key=product_key,
                    pid=pid,
                    size=copy_size,
                    is_long=is_long,
                    leverage=leverage,
                    network=str(network),
                    session_id=session_id,
                )
                if result.get("pending"):
                    await run_blocking(_register_pending_maker_open, mirror_id, pid, {
                        "digest": str(result.get("digest") or ""),
                        "user_id": user_id,
                        "product_key": product_key,
                        "product_name": product_name,
                        "is_long": is_long,
                        "leverage": leverage,
                        "leader_entry": float(leader_entry or 0.0),
                        "leader_size": float(leader_pos.get("size") or 0.0),
                        "created_ts": time.time(),
                        "cancel_confirmed": bool(result.get("cancel_confirmed")),
                    })
                    logger.warning(
                        "copy maker open PENDING for mirror %s product %s digest %s — "
                        "fate unresolved (%s); no new orders on this product until resolved",
                        mirror_id, pid, str(result.get("digest") or "")[:16],
                        result.get("error"),
                    )
                    # COPY-STOP-RACE (F-2): the maker open ran for up to ~25s;
                    # a stop that landed in that window already flattened (it
                    # saw no row and no pending yet) and marked the mirror
                    # inactive — which would exclude it from polling forever
                    # while this order may still be resting. Re-arm so the
                    # flatten-retry poller owns the pending until it resolves.
                    live = await run_blocking(get_copy_mirror, mirror_id) or {}
                    if not live.get("active") or live.get("stop_requested"):
                        await run_blocking(request_copy_mirror_stop, mirror_id)
                        logger.warning(
                            "copy maker open PENDING re-armed stop for mirror %s "
                            "product %s — a stop landed during the open",
                            mirror_id, pid,
                        )
                    continue
                if not result.get("success"):
                    if result.get("error") == "maker_unfilled":
                        logger.info(
                            "copy maker open unfilled for %s (mirror %s) — retrying next poll",
                            product_key, mirror_id,
                        )
                    else:
                        logger.warning(
                            "Copy maker open failed for user %s: %s",
                            user_id, result.get("error", "Unknown error"),
                        )
                    continue
            else:
                result = await run_blocking(
                    execute_market_order,
                    telegram_id=user_id,
                    product=product_key,
                    size=copy_size,
                    is_long=is_long,
                    leverage=leverage,
                    slippage_pct=1.5,
                    enforce_rate_limit=False,
                    source="copy",
                    network=network,
                    strategy_session_id=session_id,
                )

            if result.get("success"):
                # Maker fills report the exact filled base (partials keep what
                # filled); the market path fills the whole request.
                open_size = float(result.get("filled_size") or copy_size)
                fill_price = float(result.get("price") or 0.0)
                if fill_price <= 0:
                    # Last resort. Booking the LEADER's entry as ours corrupts
                    # PnL pairing quietly — make it loud when it happens.
                    logger.warning(
                        "copy open for mirror %s product %s has no resolvable fill "
                        "price — falling back to leader entry %.6f for pairing",
                        mirror_id, pid, float(leader_entry),
                    )
                    fill_price = float(leader_entry)
                # Open-side accounting: fee (exact from the archive on the
                # maker path) and entry notional as volume.
                await run_blocking(
                    update_mirror_accounting, mirror_id,
                    fees_delta=float(result.get("fee") or 0.0),
                    volume_delta=fill_price * open_size,
                )
                # Record the copy position
                copy_position_id = await run_blocking(
                    insert_copy_position,
                    {
                        "mirror_id": mirror_id,
                        "user_id": user_id,
                        "product_id": pid,
                        "product_name": product_name,
                        "side": "long" if is_long else "short",
                        "entry_price": fill_price,
                        "size": open_size,
                        "leverage": leverage,
                        "tp_price": leader_pos.get("tp_price"),
                        "sl_price": leader_pos.get("sl_price"),
                        "leader_entry_price": leader_entry,
                        "leader_size": leader_pos["size"],
                    },
                )

                # COPY-STOP-RACE (post-open): a stop that landed while the
                # order was in flight ran its flatten before this row existed.
                # Unwind immediately — the user asked to be flat.
                live = await run_blocking(get_copy_mirror, mirror_id) or {}
                if not live.get("active") or live.get("stop_requested"):
                    await _unwind_stop_raced_open(
                        mirror,
                        {
                            "id": copy_position_id,
                            "entry_price": fill_price,
                            "side": "long" if is_long else "short",
                            "product_name": product_name,
                        },
                        product_key, open_size, is_long, leverage,
                        str(network), session_id,
                    )
                    continue

                # Place TP/SL orders if leader has them
                bracket_digests = await run_blocking(
                    _place_tp_sl_orders,
                    user_id,
                    product_key,
                    pid,
                    session_id=session_id,
                    size=open_size,
                    is_long=is_long,
                    leverage=leverage,
                    tp_price=leader_pos.get("tp_price"),
                    sl_price=leader_pos.get("sl_price"),
                    network=network,
                )
                if copy_position_id and bracket_digests:
                    from src.nadobro.db import execute as db_execute

                    await run_blocking(
                        db_execute,
                        """
                        UPDATE copy_positions
                        SET tp_order_digest = %s,
                            sl_order_digest = %s
                        WHERE id = %s
                        """,
                        (
                            bracket_digests.get("tp_order_digest"),
                            bracket_digests.get("sl_order_digest"),
                            copy_position_id,
                        ),
                    )

                side_emoji = "🟢 LONG" if is_long else "🔴 SHORT"
                wallet = mirror.get("wallet_address", "")
                wallet_snip = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) >= 10 else wallet
                await _notify_user(
                    user_id,
                    f"📋 Copy Trade Opened\n"
                    f"Trader: {mirror.get('label') or wallet_snip}\n"
                    f"{side_emoji} {product_name}\n"
                    f"Size: {open_size:.6f} @ ${fill_price:,.2f}\n"
                    f"Leverage: {leverage}x"
                    + (f"\nTP: ${leader_pos['tp_price']:,.2f}" if leader_pos.get("tp_price") else "")
                    + (f"\nSL: ${leader_pos['sl_price']:,.2f}" if leader_pos.get("sl_price") else "")
                )
            else:
                error = result.get("error", "Unknown error")
                logger.warning("Copy trade failed for user %s: %s", user_id, error)

        except Exception as e:
            logger.error("Copy trade exception for user %s product %s: %s", user_id, pid, e)


def _place_tp_sl_orders(user_id: int, product_key: str, product_id: int,
                        size: float, is_long: bool, leverage: float,
                        tp_price: Optional[float], sl_price: Optional[float],
                        network: str, session_id=None):
    """Place TP and SL orders for a copy position."""
    digests = {}
    if tp_price and tp_price > 0:
        try:
            result = execute_limit_order(
                telegram_id=user_id,
                product=product_key,
                size=size,
                is_long=not is_long,  # TP closes the position
                leverage=leverage,
                price=tp_price,
                enforce_rate_limit=False,
                reduce_only=True,
                order_type_override="TAKE_PROFIT",
                source="copy",
                network=network,
                strategy_session_id=session_id,
            )
            if result.get("success") and result.get("digest"):
                digests["tp_order_digest"] = result.get("digest")
        except Exception as e:
            logger.warning("Failed to place TP for copy user %s: %s", user_id, e)

    if sl_price and sl_price > 0:
        try:
            result = execute_limit_order(
                telegram_id=user_id,
                product=product_key,
                size=size,
                is_long=not is_long,  # SL closes the position
                leverage=leverage,
                price=sl_price,
                enforce_rate_limit=False,
                reduce_only=True,
                order_type_override="STOP_LOSS",
                source="copy",
                network=network,
                strategy_session_id=session_id,
            )
            if result.get("success") and result.get("digest"):
                digests["sl_order_digest"] = result.get("digest")
        except Exception as e:
            logger.warning("Failed to place SL for copy user %s: %s", user_id, e)
    return digests


# ─── Maker-first opens ─────────────────────────────────────────


def _archive_reads_unreliable(network: str) -> bool:
    """True while the archive is rate-limited or circuit-broken.

    Isolated-subaccount discovery silently returns [] in that state, so a
    venue positions read may be missing every isolated position. An absence
    observed then is NOT evidence that a position closed or that an order
    didn't fill.
    """
    try:
        from src.nadobro.venue.nado_archive import (
            archive_url_for_network,
            is_archive_rate_limited,
        )

        if is_archive_rate_limited():
            return True
        from src.nadobro.core.http_session import is_circuit_open

        return bool(is_circuit_open(archive_url_for_network(network)))
    except Exception:  # noqa: BLE001 - health probe must never break the poll
        return False


def _follower_flat_on_product(client, pid: int) -> Optional[bool]:
    """True when the follower holds no position in ``pid``; None when the
    read itself failed (unknown). Engine state — no archive indexing lag."""
    if client is None:
        return None
    try:
        for vp in client.get_all_positions() or []:
            try:
                if int(vp.get("product_id", -1)) == int(pid) and abs(
                    float(vp.get("amount", 0) or 0)
                ) > 0:
                    return False
            except (TypeError, ValueError):
                continue
        return True
    except Exception:  # noqa: BLE001 - unknown, caller keeps the order pending
        return None


def _maker_fee_from_parsed(parsed: dict) -> float:
    return float(parsed.get("fee") or 0.0) + float(parsed.get("builder_fee") or 0.0)


def _maker_result_from_parsed(parsed: dict, digest: str, fallback_price: float = 0.0) -> dict:
    fill_size = float(parsed.get("fill_size") or 0.0)
    return {
        "success": True,
        "digest": digest,
        "price": float(parsed.get("fill_price") or fallback_price or 0.0),
        "fee": _maker_fee_from_parsed(parsed),
        "filled_size": fill_size,
    }


def _execute_maker_open(
    *,
    user_id: int,
    product_key: str,
    pid: int,
    size: float,
    is_long: bool,
    leverage: float,
    network: str,
    session_id=None,
) -> dict:
    """Maker-first copy open: post-only limit joined at the touch, short fill
    wait, cancel the remainder. Never pays taker fees or crosses the spread —
    the market path paid taker fees plus up to 1.5% slippage on every entry.

    The touch (best bid for a long, best ask for a short) is an
    exchange-quoted, tick-valid price, and post-only guarantees the order
    can only rest. Fills resolve from the archive by digest, so the booked
    entry price and fee are EXACT (the market path guessed the leader's entry
    when the payload price was missing, and usually missed the open fee).

    Returns an execute_market_order-shaped dict plus:
      filled_size      — base actually filled (a partial keeps what filled)
      pending=True     — the order's fate could not be confirmed (cancel
                         failed and/or archive silent). The CALLER MUST
                         register the digest in _PENDING_MAKER_OPENS and must
                         not place another order on this product until it
                         resolves.
      cancel_confirmed — whether the venue acknowledged the cancel.
    error == "maker_unfilled" means confirmed zero fill: retry next poll (the
    entry-deviation gate decides whether the entry is still worth taking).
    """
    client = get_user_nado_client(user_id, network=network)
    if not client:
        return {"success": False, "error": "Wallet not initialized."}
    try:
        mp = client.get_market_price(pid) or {}
    except Exception as e:  # noqa: BLE001 - no touch price = no maker order
        return {"success": False, "error": f"price read failed: {e}"}
    touch = float((mp.get("bid") if is_long else mp.get("ask")) or 0.0)
    if touch <= 0:
        return {"success": False, "error": "no touch price"}

    res = execute_limit_order(
        telegram_id=user_id,
        product=product_key,
        size=size,
        price=touch,
        is_long=is_long,
        leverage=leverage,
        enforce_rate_limit=False,
        post_only=True,
        source="copy",
        strategy_session_id=session_id,
        network=network,
    )
    if not res.get("success"):
        # Includes post-only would-cross rejections when the touch moved —
        # a clean no-op; the next poll re-quotes.
        return {"success": False, "error": res.get("error", "limit order failed")}
    digest = str(res.get("digest") or "").strip()
    if not digest:
        # Cannot track an order without its digest. The venue-reconcile guard
        # ("already holds on-venue") will surface any untracked fill.
        logger.error(
            "copy maker open for user %s product %s returned success without a "
            "digest — order untrackable",
            user_id, pid,
        )
        return {"success": False, "error": "order digest missing"}

    # Fill wait: poll the archive; break early only on a full fill so a
    # partial keeps collecting maker fills until the deadline.
    deadline = time.time() + MAKER_FILL_WAIT_SECONDS
    parsed: Optional[dict] = None
    while True:
        rows = query_orders_by_digests(network, [digest])
        row = rows.get(digest)
        if row is not None:
            parsed = row
            filled = float(row.get("fill_size") or 0.0)
            wanted = abs(float(row.get("original_amount") or 0.0)) or float(size)
            if filled > 0 and filled >= wanted * 0.999:
                return _maker_result_from_parsed(row, digest, touch)
        if time.time() >= deadline:
            break
        time.sleep(2.0)

    # Deadline passed with the order (at best) partially filled: cancel the
    # remainder, then read the archive once more for fills that landed while
    # the cancel was in flight.
    cancel_confirmed = False
    try:
        cancel_res = client.cancel_order(pid, digest) or {}
        cancel_confirmed = bool(cancel_res.get("success"))
        if not cancel_confirmed:
            err = str(cancel_res.get("error") or "").lower()
            # An already-gone order (fully filled or venue-pruned) is a
            # confirmed-dead order for our purposes.
            cancel_confirmed = "not found" in err or "filled" in err or "expired" in err
    except Exception as e:  # noqa: BLE001 - unresolved cancel handled below
        logger.warning("copy maker cancel failed for digest %s: %s", digest[:16], e)

    rows = query_orders_by_digests(network, [digest])
    row = rows.get(digest) or parsed
    if row is not None and float(row.get("fill_size") or 0.0) > 0:
        return _maker_result_from_parsed(row, digest, touch)
    # The archive does NOT index zero-fill orders (verified live 2026-07-18:
    # an active trader's order history contains no base_filled=0 rows), so a
    # missing row can't distinguish "never filled" from "fill not indexed
    # yet". Engine state can: the open loop only places when the follower is
    # FLAT on this product, so cancel-acked + still flat ⇒ nothing filled.
    if cancel_confirmed and not _archive_reads_unreliable(network):
        if _follower_flat_on_product(client, pid) is True:
            return {"success": False, "error": "maker_unfilled"}
    return {
        "success": False,
        "pending": True,
        "digest": digest,
        "cancel_confirmed": cancel_confirmed,
        "error": "maker order fate unknown (cancel/archive/positions unavailable)",
    }


def _resolve_pending_maker_open(network: str, pid: int, pending: dict) -> tuple[str, Optional[dict]]:
    """Resolve a maker open whose fate was unknown.

    Returns ("filled", result_dict) when the archive shows fills (the caller
    books the position), ("gone", None) when the order is confirmed dead with
    zero fill, ("unknown", None) when it still cannot be confirmed — the
    product stays locked against new orders and the mirror cannot finish a
    stop while this state persists.
    """
    digest = str(pending.get("digest") or "")
    if not digest:
        return "gone", None
    user_id = int(pending.get("user_id") or 0)
    rows = query_orders_by_digests(network, [digest])
    row = rows.get(digest)
    if row is not None and float(row.get("fill_size") or 0.0) > 0:
        if not pending.get("cancel_confirmed") and float(row.get("fill_size") or 0.0) < abs(
            float(row.get("original_amount") or 0.0)
        ) * 0.999:
            # Partially filled and possibly still resting — kill the rest
            # before booking, else the position keeps growing untracked.
            try:
                client = get_user_nado_client(user_id, network=network)
                if client:
                    cancel_res = client.cancel_order(int(pid), digest) or {}
                    if not cancel_res.get("success"):
                        err = str(cancel_res.get("error") or "").lower()
                        if not ("not found" in err or "filled" in err or "expired" in err):
                            return "unknown", None
                    pending["cancel_confirmed"] = True
            except Exception as e:  # noqa: BLE001 - retry next poll
                logger.warning("pending maker cancel retry failed for %s: %s", digest[:16], e)
                return "unknown", None
        return "filled", _maker_result_from_parsed(row, digest)
    if not pending.get("cancel_confirmed"):
        try:
            client = get_user_nado_client(user_id, network=network)
            cancel_res = (client.cancel_order(int(pid), digest) if client else {}) or {}
            if cancel_res.get("success"):
                pending["cancel_confirmed"] = True
            else:
                err = str(cancel_res.get("error") or "").lower()
                if "not found" in err or "filled" in err or "expired" in err:
                    pending["cancel_confirmed"] = True
        except Exception as e:  # noqa: BLE001 - retry next poll
            logger.warning("pending maker cancel retry failed for %s: %s", digest[:16], e)
    age = time.time() - float(pending.get("created_ts") or 0.0)
    if not pending.get("cancel_confirmed"):
        # F-10: a cancel that NEVER acks would otherwise keep the stop pending
        # forever (a stuck stop is a definite harm — the user can't get flat).
        # Hard backstop: past 2× the give-up window, declare it gone CRITICALLY
        # regardless. A post-only order resting that long is almost certainly
        # venue-reaped, and any real fill keeps being flagged by the
        # venue-reconcile guard every poll.
        if age >= (MAKER_PENDING_GIVE_UP_SECONDS * 2):
            logger.critical(
                "copy pending maker open HARD GIVE-UP for product %s digest %s after "
                "%.0fs — cancel never acked; unblocking the stop. VERIFY no untracked "
                "resting order/fill on the venue.",
                pid, digest[:16], age,
            )
            return "gone", None
        return "unknown", None
    # Cancel is acked, archive shows no fills. Flat-on-venue is the decisive
    # signal (the archive never indexes zero-fill orders): the open loop only
    # placed this order while the follower was flat on the product, so still
    # flat ⇒ confirmed zero fill.
    if not _archive_reads_unreliable(network):
        client = get_user_nado_client(user_id, network=network) if user_id else None
        if _follower_flat_on_product(client, pid) is True:
            return "gone", None
    if age >= MAKER_PENDING_GIVE_UP_SECONDS:
        # Cancel confirmed long ago; archive and/or position reads have stayed
        # inconclusive (or a manual position in the same product masks the
        # flat check). Declare it gone LOUDLY — an unbooked fill, if any,
        # keeps being flagged by the venue-reconcile guard on every poll.
        logger.error(
            "copy pending maker open GIVEN UP for product %s digest %s after %.0fs — "
            "cancel confirmed, fills unconfirmable; verify no untracked fill",
            pid, digest[:16], MAKER_PENDING_GIVE_UP_SECONDS,
        )
        return "gone", None
    return "unknown", None


async def _resolve_pending_maker_opens_for_mirror(mirror: dict) -> bool:
    """Resolve every pending maker open for this mirror, booking filled ones
    as tracked positions. Returns True when nothing is left pending."""
    mirror_id = int(mirror["id"])
    user_id = mirror["user_id"]
    network = str(mirror.get("network", "mainnet"))
    session_id = mirror.get("strategy_session_id")
    all_resolved = True
    for key in [k for k in _PENDING_MAKER_OPENS if k[0] == mirror_id]:
        pending = _PENDING_MAKER_OPENS.get(key)
        if pending is None:
            continue
        pid = key[1]
        try:
            state, result = await run_blocking(_resolve_pending_maker_open, network, pid, pending)
        except Exception as e:  # noqa: BLE001 - keep the lock until resolved
            logger.error("pending maker resolution failed for %s: %s", key, e)
            all_resolved = False
            continue
        if state == "unknown":
            all_resolved = False
            continue
        await run_blocking(_pop_pending_maker_open, mirror_id, pid)
        if state != "filled" or not result:
            continue
        fill_price = float(result.get("price") or 0.0)
        open_size = float(result.get("filled_size") or 0.0)
        if fill_price <= 0 or open_size <= 0:
            # A fill with an unparsable price would silently vanish here. Keep
            # it pending so the venue-reconcile guard / next resolution retries
            # rather than dropping a real position.
            await run_blocking(_register_pending_maker_open, mirror_id, pid, pending)
            all_resolved = False
            logger.error(
                "copy pending maker fill for mirror %s product %s has no usable "
                "price/size (%.6f @ %.6f) — kept pending for retry",
                mirror_id, pid, open_size, fill_price,
            )
            continue
        await run_blocking(
            update_mirror_accounting, mirror_id,
            fees_delta=float(result.get("fee") or 0.0),
            volume_delta=fill_price * open_size,
        )
        await run_blocking(
            insert_copy_position,
            {
                "mirror_id": mirror_id,
                "user_id": user_id,
                "product_id": pid,
                "product_name": pending.get("product_name")
                or get_product_name(pid, network=network),
                "side": "long" if pending.get("is_long") else "short",
                "entry_price": fill_price,
                "size": open_size,
                "leverage": float(pending.get("leverage") or 1.0),
                "leader_entry_price": float(pending.get("leader_entry") or 0.0) or None,
                "leader_size": float(pending.get("leader_size") or 0.0) or None,
            },
        )
        logger.info(
            "copy pending maker open RESOLVED as filled for mirror %s product %s: "
            "%.6f @ %.6f",
            mirror_id, pid, open_size, fill_price,
        )
        # COPY-STOP-RACE (F-2, second window): a stop that landed while this
        # pending was unresolved already flattened (there was no row yet) and
        # marked the mirror inactive. We just booked a real tracked row — the
        # user asked to be flat, so re-arm; the flatten-retry poller now sees
        # the row and closes it.
        live = await run_blocking(get_copy_mirror, mirror_id) or {}
        if not live.get("active") or live.get("stop_requested"):
            await run_blocking(request_copy_mirror_stop, mirror_id)
            logger.warning(
                "copy late maker fill booked under a stopped mirror %s product %s "
                "— re-armed stop to flatten it",
                mirror_id, pid,
            )
        await _notify_user(
            user_id,
            f"📋 Copy Trade Opened (late fill)\n"
            f"{pending.get('product_name') or pid}: {open_size:.6f} @ ${fill_price:,.2f}",
        )
    return all_resolved


async def _unwind_stop_raced_open(
    mirror: dict, cp: dict, product_key: str, open_size: float, is_long: bool,
    leverage: float, network: str, session_id,
) -> None:
    """A stop landed while an open was in flight, so the stop's flatten ran
    before this row existed. Close it immediately — the user asked to be
    flat. If the close fails, re-arm stop_requested so the flatten-retry
    poller owns the position instead of it orphaning behind an inactive
    mirror (get_user_copies only shows active mirrors)."""
    mirror_id = mirror["id"]
    user_id = mirror["user_id"]
    product_name = cp.get("product_name") or product_key
    try:
        result = await run_blocking(
            execute_market_order,
            telegram_id=user_id,
            product=product_key,
            size=open_size,
            is_long=not is_long,
            leverage=leverage,
            slippage_pct=1.5,
            enforce_rate_limit=False,
            reduce_only=True,
            source="copy",
            network=network,
            strategy_session_id=session_id,
        )
    except Exception as e:  # noqa: BLE001 - handled as a failed close below
        result = {"success": False, "error": str(e)}
    ok, close_err = _close_result_ok(result)
    if ok:
        pnl, _fee, _price = await run_blocking(
            _settle_copy_close, mirror_id, cp, result, network, open_size
        )
        if cp.get("id"):
            await run_blocking(close_copy_position, cp["id"], pnl=pnl, reason="stop_race_unwind")
        await _notify_user(
            user_id,
            f"📋 Copy Entry Unwound\n"
            f"{product_name}: an entry filled while you were stopping — "
            f"closed immediately.\nP&L: ${pnl:+,.2f}",
        )
        return
    logger.error(
        "copy stop-race unwind close FAILED for mirror %s position %s: %s — "
        "re-arming stop retry",
        mirror_id, cp.get("id"), close_err,
    )
    await run_blocking(request_copy_mirror_stop, mirror_id)
    await _notify_user(
        user_id,
        f"⚠️ {product_name}: a copy entry filled while you were stopping and "
        "could not be closed yet. The stop will keep retrying automatically.",
    )


def _rail_decision(
    net_pnl: float, total_allocated: float, stop_loss_pct: float, take_profit_pct: float
) -> Optional[str]:
    """The cumulative rail, as a pure decision: "auto_stop_loss",
    "auto_take_profit", or None. ``net_pnl`` is realized (derived gross)
    + unrealized - fees — the same net-of-fees, uPnL-inclusive basis every
    session rail in this product uses."""
    if total_allocated <= 0:
        return None
    pnl_pct = (net_pnl / total_allocated) * 100.0
    if stop_loss_pct > 0 and net_pnl < 0 and abs(pnl_pct) >= stop_loss_pct:
        return "auto_stop_loss"
    if take_profit_pct > 0 and net_pnl > 0 and pnl_pct >= take_profit_pct:
        return "auto_take_profit"
    return None


def _tp_breach_confirmed(mirror_id: int, breached: bool, now: Optional[float] = None) -> bool:
    """Two-poll confirmation for the profit-side rail (SL never comes here).

    First breached poll arms the marker and returns False; a later poll still
    breached and at least TP_CONFIRM_GAP_SECONDS after the first returns True.
    Any non-breached evaluation disarms. The marker survives a confirmed fire
    (it is only cleared by _clear_mirror_runtime_state when the mirror stops)
    so a failed flatten retries next poll without re-waiting the gap.
    In-memory on purpose: a restart merely costs one extra confirmation poll.
    """
    key = int(mirror_id)
    if now is None:
        now = time.time()
    if not breached:
        _TP_BREACH_FIRST_SEEN.pop(key, None)
        return False
    first = _TP_BREACH_FIRST_SEEN.get(key)
    if first is None:
        _TP_BREACH_FIRST_SEEN[key] = now
        return False
    return (now - first) >= TP_CONFIRM_GAP_SECONDS


def _classify_external_change(cp: dict, venue_pos: Optional[dict]) -> tuple[str, float]:
    """How does the venue's reality compare to a tracked open copy row?

    Returns ("closed", 0.0) when the venue no longer holds the product on the
    copy's side; ("reduced", venue_size) when the venue holds the same side
    but LESS than the copy size (externally reduced — bracket partial, manual
    trim); ("ok", venue_size) otherwise. A 2% tolerance absorbs venue rounding.
    """
    copy_size = float(cp.get("size") or 0.0)
    if venue_pos is None:
        return "closed", 0.0
    try:
        venue_size = abs(float(venue_pos.get("signed_amount") or venue_pos.get("amount") or 0.0))
        venue_side = str(venue_pos.get("side") or "").upper()
    except (TypeError, ValueError):
        return "ok", copy_size
    if venue_size <= 0 or (venue_side and venue_side != str(cp.get("side", "")).upper()):
        return "closed", 0.0
    if venue_size < copy_size * 0.98:
        return "reduced", venue_size
    return "ok", venue_size


def _resolve_bracket_fill(cp: dict, network: str) -> tuple[float, float, str]:
    """(close_price, fee, reason) for an externally-vanished copy position.

    Checks the position's own TP/SL bracket digests in the archive — a filled
    bracket gives the exact fill price and fee; otherwise the close happened
    outside our orders (manual/liquidation) and the caller falls back to mid.
    """
    for digest, reason in (
        (str(cp.get("tp_order_digest") or "").strip(), "bracket_take_profit"),
        (str(cp.get("sl_order_digest") or "").strip(), "bracket_stop_loss"),
    ):
        if not digest:
            continue
        try:
            parsed = query_order_by_digest(network, digest, 1.0, 0.25)
        except Exception:  # noqa: BLE001 - archive lookup best-effort
            parsed = None
        if parsed and parsed.get("is_filled"):
            price = float(parsed.get("fill_price") or 0.0)
            fee = float(parsed.get("fee") or 0.0) + float(parsed.get("builder_fee") or 0.0)
            if price > 0:
                return price, fee, reason
    return 0.0, 0.0, "external_close"


async def _sweep_external_closes(
    mirror: dict,
    copy_pos_by_product: dict,
    venue_pos_by_product: dict,
    follower_client,
) -> tuple[float, float]:
    """Book copy closes that happened ON the venue without us placing them:
    filled TP/SL brackets, manual closes, liquidations. Returns
    (pnl_booked, fees_booked) so the caller's rail sees them this poll."""
    mirror_id = mirror["id"]
    user_id = mirror["user_id"]
    network = str(mirror.get("network", "mainnet"))
    pnl_total = 0.0
    fees_total = 0.0
    if _archive_reads_unreliable(network):
        # Isolated positions are invisible while the archive is limited — any
        # "missing position" observed now is likely a read artifact, and a
        # bracket-fill lookup can't run either. Booking waits; delay ≠ loss.
        logger.warning(
            "copy sweep skipped for mirror %s: archive rate-limited/circuit open — "
            "venue read may be missing isolated positions",
            mirror_id,
        )
        return 0.0, 0.0
    # F-4: whole-book vanish signature. If every tracked position is missing
    # from the venue read, treat unproven (no-bracket) closes with a much
    # longer confirmation window — a read outage that doesn't trip the archive
    # flag (engine cross-margin read fail, host starvation) looks exactly like
    # this and would otherwise fabricate closes on every position at once.
    tracked = [cp for cp in copy_pos_by_product.values() if float(cp.get("size") or 0.0) > 0]
    vanished = [
        cp for cp in tracked
        if _classify_external_change(cp, venue_pos_by_product.get(cp["product_id"]))[0] == "closed"
    ]
    mass_vanish = len(tracked) >= 2 and len(vanished) == len(tracked)
    if mass_vanish:
        logger.warning(
            "copy mass-vanish for mirror %s: all %d tracked positions missing from "
            "the venue read at once — likely a read outage; unproven closes need "
            "%.0fs confirmation before booking",
            mirror_id, len(tracked), MASS_VANISH_CONFIRM_SECONDS,
        )
    for pid, cp in list(copy_pos_by_product.items()):
        kind, venue_size = _classify_external_change(cp, venue_pos_by_product.get(pid))
        suspect_key = (int(mirror_id), int(pid))
        if kind == "ok":
            _EXTERNAL_CLOSE_SUSPECTS.pop(suspect_key, None)
            continue
        copy_size = float(cp.get("size") or 0.0)
        closed_qty = copy_size if kind == "closed" else max(0.0, copy_size - venue_size)
        if closed_qty <= 0:
            continue
        close_price, fee, reason = await run_blocking(_resolve_bracket_fill, cp, network)
        if kind == "closed" and reason == "external_close":
            # COPY-SWEEP-CONFIRM: no bracket fill proves this close happened.
            # "Position missing from the venue read" is also what a transient
            # read flap looks like — isolated-subaccount discovery silently
            # returns [] whenever the archive is rate-limited, vanishing every
            # isolated position for a poll. Booking that would fabricate
            # realized PnL AND let the open loop re-open a duplicate. Require
            # the same observation on a later poll before booking — longer when
            # the whole book vanished at once (F-4 outage signature).
            confirm_window = MASS_VANISH_CONFIRM_SECONDS if mass_vanish else EXTERNAL_CLOSE_CONFIRM_SECONDS
            now = time.time()
            first_seen = _EXTERNAL_CLOSE_SUSPECTS.get(suspect_key)
            if first_seen is None:
                _EXTERNAL_CLOSE_SUSPECTS[suspect_key] = now
                logger.warning(
                    "copy external close SUSPECTED for mirror %s product %s — "
                    "no bracket fill; awaiting confirmation next poll",
                    mirror_id, pid,
                )
                continue
            if (now - first_seen) < confirm_window:
                continue
        _EXTERNAL_CLOSE_SUSPECTS.pop(suspect_key, None)
        if close_price <= 0 and follower_client is not None:
            try:
                mp = await run_blocking(follower_client.get_market_price, pid)
                close_price = float((mp or {}).get("mid") or 0.0)
            except Exception:  # noqa: BLE001 - price best-effort
                close_price = 0.0
        pnl = _close_pnl_gross(cp.get("entry_price"), close_price, closed_qty, cp.get("side", ""))
        await run_blocking(
            update_mirror_accounting, mirror_id,
            pnl_delta=pnl, fees_delta=fee, volume_delta=close_price * closed_qty,
        )
        pnl_total += pnl
        fees_total += fee
        product_name = cp.get("product_name") or get_product_name(pid, network=network)
        if kind == "closed":
            await run_blocking(close_copy_position, cp["id"], pnl=pnl, reason=reason)
            copy_pos_by_product.pop(pid, None)
            # Cancel the surviving sibling bracket so it can't fire on a
            # position that no longer exists (reduce-only protects funds, but
            # a stale resting order confuses the venue reconcile).
            if follower_client is not None:
                for digest in {
                    str(cp.get("tp_order_digest") or "").strip(),
                    str(cp.get("sl_order_digest") or "").strip(),
                }:
                    if digest:
                        try:
                            await run_blocking(follower_client.cancel_order, pid, digest)
                        except Exception:  # policy: degrade-ok(sibling bracket usually already gone with the position; reduce-only means a stale survivor cannot open exposure)
                            pass
            label = {
                "bracket_take_profit": "Take-profit filled on venue",
                "bracket_stop_loss": "Stop-loss filled on venue",
            }.get(reason, "Position closed on venue")
            _caption = f"📋 Copy trade closed — {product_name}\n{label}\nP&L: ${pnl:+,.2f}"
            if not await _post_copy_close_card(user_id, network, cp["id"], _caption):
                await _notify_user(
                    user_id,
                    f"📋 Copy Position Closed\n{product_name}: {label}\nP&L: ${pnl:+,.2f}",
                )
        else:
            await run_blocking(
                reduce_copy_position, cp["id"], venue_size,
                float(cp.get("leader_size") or 0.0), pnl,
            )
            cp["size"] = venue_size
            await _notify_user(
                user_id,
                f"📋 Copy Position Reduced on venue\n"
                f"{product_name}: now {venue_size:g}\nRealized P&L: ${pnl:+,.2f}",
            )
    return pnl_total, fees_total


async def _compute_open_upnl(
    copy_pos_by_product: dict,
    venue_pos_by_product: dict,
    follower_client,
    *,
    conservative: bool = False,
) -> tuple[float, float]:
    """Unrealized PnL of the open copy rows, as ``(primary, conservative)``.

    ``primary`` — the venue's own uPnL scaled to the copy's share of the venue
    position (exact when only the copy holds the product), mid-price pairing
    fallback. This is the dashboard number and the SL rail basis — unchanged.

    ``conservative`` — per-position ``min(venue-scaled, mid-derived)`` when
    both exist (COPY-TP-CONFIRM: never take profit off a number that a plain
    mid-vs-entry pairing can't reproduce; the venue field goes through x18
    heuristics and alias probing and can misparse for a poll). Equal to
    ``primary`` when ``conservative=False`` — the extra mid reads are skipped
    entirely so a mirror with no TP keeps today's IO profile.
    """
    primary = 0.0
    conservative_total = 0.0
    for pid, cp in copy_pos_by_product.items():
        copy_size = float(cp.get("size") or 0.0)
        if copy_size <= 0:
            continue
        venue_est: Optional[float] = None
        vp = venue_pos_by_product.get(pid)
        if vp is not None and vp.get("unrealized_pnl") is not None:
            try:
                venue_size = abs(float(vp.get("signed_amount") or vp.get("amount") or 0.0))
                if venue_size > 0:
                    share = min(1.0, copy_size / venue_size)
                    venue_est = float(vp["unrealized_pnl"]) * share
            except (TypeError, ValueError):
                venue_est = None
        mid_est: Optional[float] = None
        if follower_client is not None and (venue_est is None or conservative):
            try:
                mp = await run_blocking(follower_client.get_market_price, pid)
                mid = float((mp or {}).get("mid") or 0.0)
            except Exception:  # noqa: BLE001 - price best-effort
                mid = 0.0
            if mid > 0:
                mid_est = _close_pnl_gross(cp.get("entry_price"), mid, copy_size, cp.get("side", ""))
        p = venue_est if venue_est is not None else (mid_est or 0.0)
        primary += p
        if venue_est is not None and mid_est is not None:
            conservative_total += min(venue_est, mid_est)
        else:
            conservative_total += p
    return primary, conservative_total


def _close_pnl_gross(entry_price: float, close_price: float, size: float, side: str) -> float:
    """Derived realized PnL for a close, GROSS of fees.

    The venue reports NO per-fill realized PnL (realized_pnl_x18 is always 0),
    so copy accounting pairs the close fill price against the recorded entry —
    the same derivation discipline as quant/portfolio_calculator. Fees are
    tracked separately (cumulative_fees_usd) so rails judge NET.
    """
    entry = float(entry_price or 0.0)
    close = float(close_price or 0.0)
    qty = float(size or 0.0)
    if entry <= 0 or close <= 0 or qty <= 0:
        return 0.0
    direction = 1.0 if str(side).upper() == "LONG" else -1.0
    return (close - entry) * qty * direction


def _resolve_close_fill(result: Optional[dict], network: str) -> tuple[float, float]:
    """(close_price, fee) for a close order — result payload first (price is
    fill→post-fill→mid; fee present when the archive resolved inline), then
    one archive lookup by digest. A zero price means 'unknown' and the caller
    must fall back rather than book a bogus 0-PnL pairing."""
    result = result or {}
    close_price = float(result.get("price") or 0.0)
    fee = float(result.get("fee") or 0.0)
    if close_price > 0 and fee > 0:
        return close_price, fee
    digest = str(result.get("digest") or "").strip()
    if digest:
        try:
            parsed = query_order_by_digest(network, digest, 2.0, 0.25)
        except Exception:  # noqa: BLE001 - archive lookup is best-effort
            parsed = None
        if parsed:
            if close_price <= 0:
                close_price = float(parsed.get("fill_price") or 0.0)
            if fee <= 0:
                fee = float(parsed.get("fee") or 0.0) + float(parsed.get("builder_fee") or 0.0)
    return close_price, fee


def _settle_copy_close(
    mirror_id: int, cp: dict, result: Optional[dict], network: str, closed_size: float
) -> tuple[float, float, float]:
    """Book one copy close into the mirror accounting.

    Returns (pnl_gross, fee, close_price). Books derived gross PnL + fee +
    close-side volume additively onto copy_mirrors. When the close price is
    unresolvable (archive miss AND no payload price) books fee/volume=0 and
    pnl=0 with a loud log — never a silently wrong pairing.
    """
    close_price, fee = _resolve_close_fill(result, network)
    if close_price <= 0:
        logger.warning(
            "copy close for position %s has no resolvable fill price — "
            "booked 0 PnL; session rollup will correct totals at stop",
            cp.get("id"),
        )
        return 0.0, 0.0, 0.0
    pnl = _close_pnl_gross(cp.get("entry_price"), close_price, closed_size, cp.get("side", ""))
    update_mirror_accounting(
        mirror_id,
        pnl_delta=pnl,
        fees_delta=fee,
        volume_delta=close_price * float(closed_size or 0.0),
    )
    return pnl, fee, close_price


def _partial_close_fraction(baseline_leader_size: float, new_leader_size: float) -> float:
    """Fraction of the copy to close when the leader trims below our baseline.

    Baseline is the leader size we last mirrored (at open, or after the last
    partial). A leader ADD does not move the baseline — so add-then-trim back
    to the baseline correctly mirrors nothing, and only a trim BELOW the
    baseline reduces the copy. Returns 0.0 when the delta is under the
    PARTIAL_CLOSE_MIN_DELTA_PCT noise threshold, 1.0 for a dust remainder.
    """
    baseline = float(baseline_leader_size or 0.0)
    new = float(new_leader_size or 0.0)
    if baseline <= 0 or new >= baseline:
        return 0.0
    fraction = (baseline - new) / baseline
    if fraction * 100.0 < PARTIAL_CLOSE_MIN_DELTA_PCT:
        return 0.0
    if (1.0 - fraction) <= PARTIAL_CLOSE_DUST_FRACTION:
        return 1.0
    return fraction


async def _mirror_leader_add_if_needed(
    existing_cp: dict, leader_pos: dict, user_id: int, mirror_id: int, network: str,
    *, margin_per_trade: float, max_leverage: float, leader_max_notional: float,
    session_id=None, opens_blocked: bool = False,
) -> str:
    """Mirror a leader ADDING to a position we already copy.

    COPY-NO-SCALE-UP: ``_partial_close_fraction`` returns 0.0 whenever
    ``new >= baseline``, and nothing else in ``_sync_mirror_positions`` reacts to
    a growing leader position — so the bot mirrored a leader's FIRST entry and
    then ignored every subsequent add. A leader going 16.6 -> 36.6 HYPE left the
    follower stuck at the original size.

    Sizing reuses ``_compute_copy_sizing`` with the leader's NEW size, so the
    copy is re-derived from the same conviction model that sized the open and
    stays bounded by ``margin_per_trade`` (the weight term is capped at 1.0).
    That means a leader who doubles down cannot push the follower past their
    per-trade budget.

    Returns "added" when the copy grew, else "".
    """
    if not SCALE_UPS_ENABLED:
        return ""
    baseline = float(existing_cp.get("leader_size") or 0.0)
    new_leader = float(leader_pos.get("size") or 0.0)
    if baseline <= 0 or new_leader <= baseline:
        return ""
    growth_pct = (new_leader - baseline) / baseline * 100.0
    if growth_pct < SCALE_UP_MIN_DELTA_PCT:
        return ""

    current_size = float(existing_cp.get("size") or 0.0)
    if current_size <= 0:
        return ""
    pid = int(existing_cp["product_id"])
    product_name = existing_cp.get("product_name") or get_product_name(pid, network=network)
    product_key = product_name.replace("-PERP", "")
    is_long = str(existing_cp["side"]).upper() == "LONG"

    # Leader's CURRENT average entry after the add (their notional moved).
    leader_entry = float(leader_pos.get("entry_price") or 0.0)
    if leader_entry <= 0:
        return ""

    # Mirror the leader's growth RATIO, capped by the full per-trade budget.
    # full_size is the budget ceiling (entry_fraction=1.0); the open committed
    # only COPY_INITIAL_ENTRY_FRACTION of it, and that reserve is what lets a
    # leader add be mirrored at all.
    full_size, leverage = _compute_copy_sizing(
        leader_size=new_leader,
        leader_entry=leader_entry,
        leader_leverage=float(leader_pos.get("leverage", 0) or 0),
        leader_max_notional=leader_max_notional,
        margin_per_trade=margin_per_trade,
        max_leverage=max_leverage,
        product_max_leverage=get_product_max_leverage(product_key, network=network),
        entry_fraction=1.0,
    )
    target_size = min(full_size, current_size * (new_leader / baseline))
    add_size = target_size - current_size
    if add_size <= 0 or (add_size / current_size) * 100.0 < SCALE_UP_MIN_DELTA_PCT:
        # Already at (or above) the budget-capped target — the leader grew but
        # our conviction weight is pinned at margin_per_trade. Move the baseline
        # so a later trim measures from the leader's real size instead of
        # re-triggering this branch every poll.
        existing_cp["leader_size"] = new_leader
        await run_blocking(
            grow_copy_position, existing_cp["id"], current_size,
            float(existing_cp.get("entry_price") or 0.0), new_leader,
        )
        return ""

    # Never stack an order while a maker open on this product is unresolved.
    if (int(mirror_id), pid) in _PENDING_MAKER_OPENS:
        return ""

    # A scale-up INCREASES exposure, so the global pause blocks it exactly like a
    # fresh open (see the open path in _sync_mirror_positions).
    if opens_blocked:
        logger.info(
            "copy scale-up blocked: global trading pause active (mirror %s)", mirror_id
        )
        return ""

    try:
        # A stop/pause landing mid-sync must win over a new order.
        live = await run_blocking(get_copy_mirror, mirror_id) or {}
        if not live.get("active") or live.get("stop_requested") or live.get("paused"):
            return ""

        result = await run_blocking(
            execute_market_order,
            telegram_id=user_id,
            product=product_key,
            size=add_size,
            is_long=is_long,
            leverage=leverage,
            slippage_pct=1.5,
            enforce_rate_limit=False,
            source="copy",
            network=network,
            strategy_session_id=session_id,
        )
        if not (result or {}).get("success"):
            logger.warning(
                "copy scale-up failed for position %s: %s",
                existing_cp["id"], (result or {}).get("error"),
            )
            return ""

        fill_price = float(
            (result or {}).get("fill_price")
            or (result or {}).get("price")
            or leader_entry
        )
        filled = float((result or {}).get("fill_size") or add_size)
        new_size = current_size + filled
        prev_entry = float(existing_cp.get("entry_price") or fill_price)
        # Size-weighted average entry so the position's PnL stays correct.
        new_entry = ((prev_entry * current_size) + (fill_price * filled)) / new_size

        await run_blocking(
            grow_copy_position, existing_cp["id"], new_size, new_entry, new_leader
        )
        existing_cp["size"] = new_size
        existing_cp["entry_price"] = new_entry
        existing_cp["leader_size"] = new_leader
        await _notify_user(
            user_id,
            f"📋 Copy Position Increased\n"
            f"{product_name}: Leader added {growth_pct:.0f}% → "
            f"Your position grew to {new_size:g} @ {new_entry:,.4f}",
        )
        return "added"
    except Exception as e:  # noqa: BLE001 - one mirror's failure must not stall the poll
        logger.error("copy scale-up error for position %s: %s", existing_cp["id"], e)
        return ""


async def _mirror_partial_close_if_needed(
    existing_cp: dict, leader_pos: dict, user_id: int, mirror_id: int, network: str,
    session_id=None,
) -> str:
    """Mirror a leader's partial close onto the copy position.

    Returns "reduced" (copy shrunk; caller re-places TP/SL for the new size),
    "closed" (dust remainder -> fully closed), or "" (no action).
    """
    if not PARTIAL_CLOSES_ENABLED:
        return ""
    baseline = float(existing_cp.get("leader_size") or 0.0)
    new_leader = float(leader_pos.get("size") or 0.0)
    fraction = _partial_close_fraction(baseline, new_leader)
    if fraction <= 0.0:
        return ""

    copy_size = float(existing_cp.get("size") or 0.0)
    if copy_size <= 0:
        return ""
    product_name = existing_cp.get("product_name") or get_product_name(
        existing_cp["product_id"], network=network
    )
    product_key = product_name.replace("-PERP", "")
    is_close_long = existing_cp["side"].upper() != "LONG"
    close_size = copy_size if fraction >= 1.0 else copy_size * fraction

    try:
        result = await run_blocking(
            execute_market_order,
            telegram_id=user_id,
            product=product_key,
            size=close_size,
            is_long=is_close_long,
            leverage=float(existing_cp.get("leverage", 1.0)),
            slippage_pct=1.5,
            enforce_rate_limit=False,
            reduce_only=True,
            source="copy",
            network=network,
            strategy_session_id=session_id,
        )
        ok, close_err = _close_result_ok(result)
        if not ok:
            logger.warning(
                "copy partial close failed for position %s: %s",
                existing_cp["id"], close_err,
            )
            return ""
        pnl, _fee, _price = await run_blocking(
            _settle_copy_close, mirror_id, existing_cp, result, network, close_size
        )

        if fraction >= 1.0:
            await run_blocking(
                close_copy_position, existing_cp["id"], pnl=pnl, reason="leader_partial_dust"
            )
            await _notify_user(
                user_id,
                f"📋 Copy Position Closed\n"
                f"{product_name}: Leader trimmed to dust → Your position closed\n"
                f"P&L: ${pnl:+,.2f}",
            )
            return "closed"

        remaining = copy_size - close_size
        await run_blocking(
            reduce_copy_position, existing_cp["id"], remaining, new_leader, pnl
        )
        # Keep the in-memory row consistent for the TP/SL resize that follows.
        existing_cp["size"] = remaining
        existing_cp["leader_size"] = new_leader
        await _notify_user(
            user_id,
            f"📋 Copy Position Reduced\n"
            f"{product_name}: Leader closed {fraction * 100.0:.0f}% → "
            f"Your position reduced to {remaining:g}\n"
            f"Realized P&L: ${pnl:+,.2f}",
        )
        return "reduced"
    except Exception as e:  # noqa: BLE001 - one mirror's failure must not stall the poll
        logger.error("copy partial close error for position %s: %s", existing_cp["id"], e)
        return ""


def _update_tp_sl_if_changed(
    existing_cp: dict, leader_pos: dict, user_id: int, network: str, force: bool = False,
    session_id=None,
):
    """Check if leader's TP/SL changed and update the copy position's orders.

    ``force`` re-places the bracket even at unchanged prices — used after a
    partial close so the resting reduce-only orders match the shrunken size
    (they were placed for the pre-reduction size).
    """
    new_tp = leader_pos.get("tp_price")
    new_sl = leader_pos.get("sl_price")
    old_tp = existing_cp.get("tp_price")
    old_sl = existing_cp.get("sl_price")

    # Only update if there's a meaningful change
    tp_changed = (new_tp or 0) != (old_tp or 0)
    sl_changed = (new_sl or 0) != (old_sl or 0)

    if not tp_changed and not sl_changed and not force:
        return
    if force and not (new_tp or new_sl or old_tp or old_sl):
        return  # nothing bracketed — nothing to resize

    # Update the copy_positions record. On a pure force-resize (partial close
    # with unchanged bracket prices) there are no price columns to update —
    # skip the UPDATE entirely (an empty SET is a SQL syntax error) and go
    # straight to cancel + re-place for the new size.
    from src.nadobro.db import execute as db_execute
    updates = []
    params = []
    if tp_changed:
        updates.append("tp_price = %s")
        params.append(new_tp)
    if sl_changed:
        updates.append("sl_price = %s")
        params.append(new_sl)
    if updates:
        params.append(existing_cp["id"])
        db_execute(f"UPDATE copy_positions SET {', '.join(updates)} WHERE id = %s", params)

    # Cancel only the TP/SL orders this copy position created.
    product_name = existing_cp.get("product_name", "")
    product_key = product_name.replace("-PERP", "")
    pid = existing_cp["product_id"]
    size = float(existing_cp.get("size", 0))
    is_long = existing_cp["side"].upper() == "LONG"
    leverage = float(existing_cp.get("leverage", 1.0))

    # Cancel existing TP/SL orders for this product
    try:
        client = get_user_nado_client(user_id, network)
        if client:
            for digest in {
                str(existing_cp.get("tp_order_digest") or "").strip(),
                str(existing_cp.get("sl_order_digest") or "").strip(),
            }:
                if digest:
                    try:
                        client.cancel_order(pid, digest)
                    except Exception as e:
                        logger.warning(
                            "old TP/SL cancel failed for product %s digest %s — "
                            "stale protective order may still be resting: %s",
                            pid, digest, e,
                        )
    except Exception as e:
        logger.debug("Failed to cancel old TP/SL: %s", e)

    # Place updated TP/SL
    bracket_digests = _place_tp_sl_orders(
        user_id, product_key, pid, session_id=session_id, size=size,
        is_long=is_long, leverage=leverage, tp_price=new_tp, sl_price=new_sl,
        network=network,
    )
    db_execute(
        """
        UPDATE copy_positions
        SET tp_order_digest = %s,
            sl_order_digest = %s
        WHERE id = %s
        """,
        (
            bracket_digests.get("tp_order_digest"),
            bracket_digests.get("sl_order_digest"),
            existing_cp["id"],
        ),
    )
