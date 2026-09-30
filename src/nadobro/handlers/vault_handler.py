"""Telegram handlers for the Nado NLP Vault (deposit / withdraw flows)."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

from src.nadobro.core.async_utils import run_blocking
from src.nadobro.handlers.network_guard import (
    active_network,
    bind_cb,
    network_unknown_message,
    network_unknown_query,
    refuse_query,
    same_network,
    unbind_cb,
)
from src.nadobro.vault.nlp_vault_service import (
    SEQUENCER_FEE_USDT0,
    VAULT_DEPOSIT_MAX_PCT,
    deposit_to_vault,
    estimate_withdraw_fee_usdt0,
    get_user_vault_snapshot,
    withdraw_from_vault,
)
from src.nadobro.vault.vault_deposit_watch_service import (
    disable_deposit_watch,
    enable_deposit_watch,
)

logger = logging.getLogger(__name__)

_DEPOSIT_PENDING_KEY = "vault_pending_amount"
# Set while a mint/burn is round-tripping to the venue; a second Confirm tap
# (or a Telegram callback redelivery) during that window is ignored instead of
# double-executing a money movement.
_OP_INFLIGHT_KEY = "vault_op_inflight"

DEPOSIT_PRESETS_USDT0 = (100.0, 500.0, 1_000.0, 5_000.0)
WITHDRAW_PRESETS_PCT = (25, 50, 75, 100)


def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_signed_usd(value: float) -> str:
    if value >= 0:
        return f"+{_fmt_usd(value)}"
    return f"-{_fmt_usd(abs(value))}"


def _fmt_lockup(seconds: int) -> str:
    if seconds <= 0:
        return "Unlocked"
    hours = seconds / 3600.0
    if hours >= 24:
        return f"{hours / 24:.1f}d remaining"
    return f"{hours:.1f}h remaining"


def _fmt_apr(snapshot: dict) -> str:
    pool = snapshot.get("pool") or {}
    apr = pool.get("apr_pct")
    if apr is None:
        return "—"
    return f"{float(apr):.2f}%"


def _vault_home_card(snapshot: dict) -> tuple[str, InlineKeyboardMarkup]:
    if snapshot.get("error"):
        text = f"💰 *Nado Vault*\n\n⚠️ {snapshot['error']}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="vault:refresh")],
            [InlineKeyboardButton("🏠 Home", callback_data="nav:main")],
        ])
        return text, kb

    usdt0 = float(snapshot.get("usdt0_balance") or 0.0)
    lp_balance = float(snapshot.get("lp_balance") or 0.0)
    position = float(snapshot.get("position_usdt0") or snapshot.get("lp_value_usdt0") or 0.0)
    all_time = float(snapshot.get("all_time_earned_usdt0") or 0.0)
    unrealized = float(snapshot.get("unrealized_pnl_usdt0") or 0.0)
    room = float(snapshot.get("deposit_room_usdt0") or 0.0)
    max_mintable = float(snapshot.get("max_mintable_usdt0") or 0.0)
    lockup = int(snapshot.get("lockup_seconds_remaining") or 0)
    pool = snapshot.get("pool") or {}
    tvl = float(pool.get("tvl_usdt0") or 0.0)
    watch_enabled = bool(snapshot.get("deposit_watch_enabled"))

    lines = [
        "💰 *Nado Liquidity Provider (NLP)*",
        "",
        f"TVL: `{_fmt_usd(tvl)}`    APR: `{_fmt_apr(snapshot)}`",
        "",
        "*Your Position*",
        f"Position: `{_fmt_usd(position)}`",
        f"Balance: `{lp_balance:.6f}` NLP",
        f"All-time Earned: `{_fmt_signed_usd(all_time)}`",
        f"Unrealized PnL: `{_fmt_signed_usd(unrealized)}`",
        "",
        # "USDT0 balance", NOT "Idle": this is the raw spot balance from
        # subaccount_info, and on a cross-margin venue it simultaneously backs
        # open positions/orders. Labeling it "Idle" contradicted the
        # margin-locked explainer directly below it and sent users hunting for
        # a phantom deposit blocker.
        f"USDT0 balance: `{_fmt_usd(usdt0)}`",
    ]
    mintable_known = bool(snapshot.get("mintable_known", True))
    deposit_max = float(snapshot.get("deposit_max_usdt0") or 0.0)
    if mintable_known:
        lines.append(f"Free to deposit (no borrow): `{_fmt_usd(max_mintable)}`")
    else:
        # A throttled/failed capacity query is UNKNOWN, not $0 — rendering it
        # as $0 once flipped this card to 🔒 Margin in use for a depositable
        # user. Deposits stay enabled; the mint itself is no-borrow-guarded.
        lines.append("Free to deposit: `checking…` (tap 🔄 Refresh in a moment)")
    lines.append(f"Deposit room: `{_fmt_usd(room)}`")
    # Lockup / withdrawable. Only claim "Unlocked" when the venue's
    # locked-balance query actually answered: a throttled read used to degrade
    # to zeros, which rendered the whole position as unlocked and made a 100%
    # withdraw fail at the venue with "Do not have enough unlocked NLP".
    if lp_balance > 0 and not bool(snapshot.get("unlocked_known")):
        lines.append("Withdrawable: `checking…` (tap 🔄 Refresh in a moment)")
    elif lp_balance > 0:
        lp_unlocked = float(snapshot.get("lp_unlocked") or 0.0)
        lp_locked = float(snapshot.get("lp_locked") or 0.0)
        lines.append(f"Withdrawable now: `{lp_unlocked:.6f}` NLP")
        if lp_locked > 0:
            lines.append(f"Still locked: `{lp_locked:.6f}` NLP · `{_fmt_lockup(lockup)}`")
    else:
        lines.append(f"Lockup: `{_fmt_lockup(lockup)}`")
    blocked_reason = snapshot.get("deposit_blocked_reason")
    margin_locked = blocked_reason == "margin_locked"
    blocked = mintable_known and max_mintable <= 1.0
    if blocked:
        borrow_mintable = float(snapshot.get("mintable_with_borrow_usdt0") or 0.0)
        lines.append("")
        if blocked_reason == "wallet_cap_reached":
            lines.append(
                f"Your vault position has reached the Private Alpha cap of "
                f"`{_fmt_usd(float(snapshot.get('private_alpha_cap_usdt0') or 0.0))}` "
                "per account — withdraw first to deposit more."
            )
        elif margin_locked:
            lines.append(
                f"Deposits are *open*, but your `{_fmt_usd(usdt0)}` USDT0 is "
                "currently backing open positions or resting orders (margin), "
                "so none of it can enter the vault without borrowing — and a "
                "vault mint never borrows against your trading account."
            )
            if borrow_mintable > 1.0:
                lines.append(
                    f"(Nado would allow `{_fmt_usd(borrow_mintable)}` *with* "
                    "borrowing — the bot keeps this off deliberately.)"
                )
            lines.append(
                "Close/reduce positions, cancel resting orders, or deposit "
                "more USDT0 to free margin for minting."
            )
        else:
            lines.append("Vault capacity is currently *closed* for new deposits.")
    text = "\n".join(lines)

    deposit_btn = InlineKeyboardButton("⬇️ Deposit", callback_data="vault:deposit")
    if blocked:
        if blocked_reason == "wallet_cap_reached":
            deposit_btn = InlineKeyboardButton("🧢 Cap reached", callback_data="vault:home")
        elif margin_locked:
            deposit_btn = InlineKeyboardButton("🔒 Margin in use", callback_data="vault:home")
        else:
            deposit_btn = InlineKeyboardButton("⛔ Deposits closed", callback_data="vault:home")

    watch_label = "🔕 Stop deposit alerts" if watch_enabled else "🔔 Notify when deposits open"
    watch_cb = "vault:watch:off" if watch_enabled else "vault:watch:on"

    kb = InlineKeyboardMarkup([
        [deposit_btn, InlineKeyboardButton("⬆️ Withdraw", callback_data="vault:withdraw")],
        [InlineKeyboardButton(watch_label, callback_data=watch_cb)],
        [InlineKeyboardButton("🔄 Refresh", callback_data="vault:refresh")],
        [InlineKeyboardButton("◀ Back", callback_data="nav:strategy_hub")],
    ])
    return text, kb


def _deposit_picker(snapshot: dict) -> tuple[str, InlineKeyboardMarkup]:
    usdt0 = float(snapshot.get("usdt0_balance") or 0.0)
    room = float(snapshot.get("deposit_room_usdt0") or 0.0)
    # 70%-of-balance ceiling (margin buffer) already folded in by the snapshot;
    # falls back to the venue-known bound when the field is absent.
    if "deposit_max_usdt0" in snapshot:
        max_deposit = float(snapshot.get("deposit_max_usdt0") or 0.0)
    else:
        max_deposit = min(usdt0, room)
    text = (
        "⬇️ *Deposit USDT0 → NLP*\n\n"
        f"USDT0 balance: `{_fmt_usd(usdt0)}`\n"
        f"Deposit room: `{_fmt_usd(room)}`\n"
        f"Max you can deposit now: `{_fmt_usd(max_deposit)}`\n"
        f"(capped at {VAULT_DEPOSIT_MAX_PCT:.0f}% of your USDT0 so trading keeps a margin buffer)\n\n"
        "Choose an amount:"
    )
    rows = []
    presets = [a for a in DEPOSIT_PRESETS_USDT0 if a <= max_deposit + 1e-9]
    if presets:
        row = []
        for amt in presets:
            row.append(InlineKeyboardButton(f"${int(amt):,}", callback_data=f"vault:deposit:preset:{amt}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    if max_deposit > 0:
        rows.append([InlineKeyboardButton(f"Max ({_fmt_usd(max_deposit)})", callback_data=f"vault:deposit:preset:{max_deposit}")])
    rows.append([InlineKeyboardButton("✍️ Custom amount", callback_data="vault:deposit:custom")])
    rows.append([InlineKeyboardButton("◀ Back", callback_data="vault:home")])
    return text, InlineKeyboardMarkup(rows)


def _withdraw_picker(snapshot: dict) -> tuple[str, InlineKeyboardMarkup]:
    lp_balance = float(snapshot.get("lp_balance") or 0.0)
    lockup = int(snapshot.get("lockup_seconds_remaining") or 0)
    unlocked_known = bool(snapshot.get("unlocked_known"))
    lp_unlocked = float(snapshot.get("lp_unlocked") or 0.0)
    lp_locked = float(snapshot.get("lp_locked") or 0.0)
    lines = [
        "⬆️ *Withdraw NLP → USDT0*",
        "",
        f"NLP balance: `{lp_balance:.6f}`",
    ]
    # Percentages are of the WITHDRAWABLE (unlocked) balance, not the total —
    # burning the total when part is still locked is exactly what the venue
    # rejects with "Do not have enough unlocked NLP".
    if lp_balance > 0 and not unlocked_known:
        lines.append("Withdrawable: `checking…`")
        lines.append("")
        lines.append("⏳ Couldn't confirm your unlocked balance just now. Tap 🔄 Refresh.")
    elif lp_locked > 0:
        lines.append(f"Withdrawable now: `{lp_unlocked:.6f}` NLP")
        lines.append(f"Still locked: `{lp_locked:.6f}` NLP · `{_fmt_lockup(lockup)}`")
    else:
        lines.append(f"Withdrawable now: `{lp_unlocked:.6f}` NLP")
    if lockup > 0 and lp_unlocked <= 0:
        lines.append("")
        lines.append("⏳ Burns are blocked until the 4-day post-mint lockup ends.")
    elif unlocked_known and lp_unlocked > 0:
        lines.append("")
        lines.append("Fees on burn: $1 sequencer + max($1, 10 bps of withdrawn).")
        lines.append("Choose a percentage of your *withdrawable* balance:")
    text = "\n".join(lines)

    rows: list[list[InlineKeyboardButton]] = []
    if unlocked_known and lp_unlocked > 0:
        row = []
        for pct in WITHDRAW_PRESETS_PCT:
            row.append(InlineKeyboardButton(f"{pct}%", callback_data=f"vault:withdraw:pct:{pct}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("✍️ Custom NLP amount", callback_data="vault:withdraw:custom")])
    rows.append([InlineKeyboardButton("◀ Back", callback_data="vault:home")])
    return text, InlineKeyboardMarkup(rows)


def _deposit_confirm_card(
    amount_usdt0: float, snapshot: dict | None = None, *, network: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """``network``: the network this card was quoted on; Confirm is bound to it."""
    # Parity with the Nado UI's deposit modal: show the $1 sequencer fee and
    # the estimated NLP received at the current NAV (final amount settles at
    # mint-time NAV — same disclaimer the venue shows).
    est_lines = f"Gas fee: `{_fmt_usd(SEQUENCER_FEE_USDT0)}`\n"
    nav = float((snapshot or {}).get("nlp_nav_usdt0") or 0.0)
    if nav > 0:
        est_nlp = max(0.0, (amount_usdt0 - SEQUENCER_FEE_USDT0)) / nav
        est_lines += f"Estimated NLP received: `{est_nlp:.6f}` (at current NAV)\n"
    text = (
        "⬇️ *Confirm Deposit*\n\n"
        f"Mint NLP using `{_fmt_usd(amount_usdt0)}` USDT0.\n"
        + est_lines +
        "Lock-up period: `4 days` (burns unlock after).\n"
        "Borrow protection is on (`spot_leverage=false`) so this will be rejected "
        "if it would force a margin borrow.\n\n"
        "Proceed?"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=bind_cb(f"vault:deposit:confirm:{amount_usdt0}", network)),
            InlineKeyboardButton("❌ Cancel", callback_data="vault:home"),
        ],
    ])
    return text, kb


def _withdraw_confirm_card(
    nlp_amount: float, usdt0_estimate: float, fee_estimate: float, *, network: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """``network``: the network this card was quoted on; Confirm is bound to it."""
    net = max(0.0, usdt0_estimate - fee_estimate)
    text = (
        "⬆️ *Confirm Withdraw*\n\n"
        f"Burn `{nlp_amount:.6f}` NLP.\n"
        f"Estimated USDT0 out (pre-fees): `{_fmt_usd(usdt0_estimate)}`\n"
        f"Estimated fees: `{_fmt_usd(fee_estimate)}`\n"
        f"Net to your account (approx.): `{_fmt_usd(net)}`\n\n"
        "Final amount is settled at the vault's NAV at burn time."
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=bind_cb(f"vault:withdraw:confirm:{nlp_amount}", network)),
            InlineKeyboardButton("❌ Cancel", callback_data="vault:home"),
        ],
    ])
    return text, kb


async def _edit_markdown_safe(query, text: str, kb) -> None:
    """Render with legacy Markdown; if the venue/user text breaks the parse
    (unbalanced backticks/underscores in an error string), fall back to plain
    text instead of letting the tap die on a Telegram 400."""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception:  # policy: degrade-ok(render fallback; plain text always displays)
        await query.edit_message_text(text, reply_markup=kb)


async def _show_home(query, telegram_id: int, *, flash: str | None = None) -> None:
    snapshot = await run_blocking(get_user_vault_snapshot, telegram_id)
    text, kb = _vault_home_card(snapshot)
    if flash:
        text = f"{flash}\n\n{text}"
    await _edit_markdown_safe(query, text, kb)


async def handle_vault_callback(query, context: CallbackContext) -> bool:
    data = (query.data or "").strip()
    if not data.startswith("vault:"):
        return False
    telegram_id = int(query.from_user.id)
    # None when it cannot be read right now: then nothing network-bound (a
    # confirm card, the deposit watch) is rendered or armed below, never
    # anything bound to a guessed network.
    current_network = await active_network(telegram_id)
    network = current_network
    # Confirm buttons carry the network their card was quoted on.
    data, built_network = unbind_cb(data)
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "home"
    back_to_vault = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="vault:home")]])

    # PREVIEW-NETWORK-BIND: a mint/burn confirm only executes on the network its
    # card was quoted on. Checked before the in-flight lock is taken and before
    # anything is read, so a stale (or untagged legacy) confirm does nothing.
    if action in ("deposit", "withdraw") and len(parts) >= 4 and parts[2] == "confirm":
        if not same_network(built_network, current_network):
            await refuse_query(
                query,
                kind=f"vault_{action}",
                built=built_network,
                current=current_network,
                telegram_id=telegram_id,
                reply_markup=back_to_vault,
            )
            return True

    if action in ("home", "refresh"):
        # Universal cancel: leaving a custom-amount prompt via ❌/◀ must also
        # forget it, or the next number typed in ordinary chat would summon a
        # deposit/withdraw confirm card out of nowhere.
        context.user_data.pop(_DEPOSIT_PENDING_KEY, None)
        await _show_home(query, telegram_id)
        return True

    if action == "watch" and len(parts) >= 3:
        if network is None:
            await network_unknown_query(query, kind="vault_watch", telegram_id=telegram_id, reply_markup=back_to_vault)
            return True
        sub = parts[2]
        if sub == "on":
            ok, msg = await run_blocking(enable_deposit_watch, telegram_id, network)
        else:
            ok, msg = await run_blocking(disable_deposit_watch, telegram_id, network)
        prefix = "✅" if ok else "⚠️"
        await _show_home(query, telegram_id, flash=f"{prefix} {msg}")
        return True

    if action == "deposit":
        snapshot = await run_blocking(get_user_vault_snapshot, telegram_id)
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "":
            # Mirror the home card's semantics exactly: block only on a
            # SUCCESSFUL capacity read of ~0. Unknown capacity (throttled
            # query) proceeds — the picker is balance-ceiling bounded and the
            # mint itself is venue-guarded (spot_leverage=false).
            blocked = bool(snapshot.get("mintable_known", True)) and (
                float(snapshot.get("max_mintable_usdt0") or 0.0) <= 1.0
            )
            if blocked:
                reason = snapshot.get("deposit_blocked_reason")
                if reason == "margin_locked":
                    flash = (
                        "🔒 Your USDT0 is backing open positions — a vault mint never "
                        "borrows against your trading account. Close/reduce positions "
                        "or deposit more USDT0, then try again."
                    )
                elif reason == "wallet_cap_reached":
                    flash = (
                        "🧢 You've reached the Private Alpha per-account cap — "
                        "withdraw first to deposit more."
                    )
                else:
                    flash = "⚠️ Vault deposit capacity is closed right now."
                await _show_home(query, telegram_id, flash=flash)
                return True
            text, kb = _deposit_picker(snapshot)
            await _edit_markdown_safe(query, text, kb)
            return True
        if sub == "preset" and len(parts) >= 4:
            if network is None:
                await network_unknown_query(
                    query, kind="vault_deposit_card", telegram_id=telegram_id, reply_markup=back_to_vault,
                )
                return True
            try:
                amount = float(parts[3])
            except ValueError:
                amount = 0.0
            text, kb = _deposit_confirm_card(amount, snapshot, network=network)
            await _edit_markdown_safe(query, text, kb)
            return True
        if sub == "custom":
            context.user_data[_DEPOSIT_PENDING_KEY] = "deposit"
            await query.edit_message_text(
                "✍️ Reply with the USDT0 amount you want to deposit (e.g. `250`).",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="vault:home")],
                ]),
            )
            return True
        if sub == "confirm" and len(parts) >= 4:
            try:
                amount = float(parts[3])
            except ValueError:
                amount = 0.0
            # In-flight guard: a double-tap (or Telegram redelivering the
            # callback) during the mint's round-trip must not mint twice.
            if context.user_data.get(_OP_INFLIGHT_KEY):
                await query.answer("⏳ Still processing your previous vault action…")
                return True
            context.user_data[_OP_INFLIGHT_KEY] = "deposit"
            try:
                result = await run_blocking(deposit_to_vault, telegram_id, amount)
            finally:
                context.user_data.pop(_OP_INFLIGHT_KEY, None)
            await _show_result(
                query, telegram_id, result,
                default_success=(
                    f"Deposited ${amount:,.2f} USDT0. Note: burns unlock after "
                    "the 4-day post-mint lockup."
                ),
            )
            return True

    if action == "withdraw":
        snapshot = await run_blocking(get_user_vault_snapshot, telegram_id)
        sub = parts[2] if len(parts) > 2 else ""
        if sub == "":
            text, kb = _withdraw_picker(snapshot)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            return True
        if sub == "pct" and len(parts) >= 4:
            if network is None:
                await network_unknown_query(
                    query, kind="vault_withdraw_card", telegram_id=telegram_id, reply_markup=back_to_vault,
                )
                return True
            try:
                pct = max(0, min(100, int(parts[3])))
            except ValueError:
                pct = 0
            lp_balance = float(snapshot.get("lp_balance") or 0.0)
            lp_value = float(snapshot.get("lp_value_usdt0") or 0.0)
            # % of the WITHDRAWABLE balance. Using the total (locked+unlocked)
            # is what produced venue error 2096 on a 100% withdraw.
            withdrawable = (
                float(snapshot.get("lp_unlocked") or 0.0)
                if snapshot.get("unlocked_known") else lp_balance
            )
            nlp_amount = withdrawable * (pct / 100.0)
            nav_per_nlp = (lp_value / lp_balance) if lp_balance > 0 else 0.0
            est_usdt0 = nlp_amount * nav_per_nlp
            est_fee = estimate_withdraw_fee_usdt0(est_usdt0)
            text, kb = _withdraw_confirm_card(nlp_amount, est_usdt0, est_fee, network=network)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            return True
        if sub == "custom":
            context.user_data[_DEPOSIT_PENDING_KEY] = "withdraw"
            await query.edit_message_text(
                "✍️ Reply with the NLP token amount to burn (e.g. `1.25`).",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="vault:home")],
                ]),
            )
            return True
        if sub == "confirm" and len(parts) >= 4:
            try:
                nlp_amount = float(parts[3])
            except ValueError:
                nlp_amount = 0.0
            if context.user_data.get(_OP_INFLIGHT_KEY):
                await query.answer("⏳ Still processing your previous vault action…")
                return True
            context.user_data[_OP_INFLIGHT_KEY] = "withdraw"
            try:
                result = await run_blocking(withdraw_from_vault, telegram_id, nlp_amount)
            finally:
                context.user_data.pop(_OP_INFLIGHT_KEY, None)
            await _show_result(
                query, telegram_id, result,
                default_success=(
                    f"Burned {nlp_amount:.6f} NLP. USDT0 settles at the vault's "
                    "NAV and lands in your trading balance."
                ),
            )
            return True

    await _show_home(query, telegram_id)
    return True


async def _show_result(query, telegram_id: int, result: dict, *, default_success: str) -> None:
    if result.get("success"):
        flash = f"✅ {default_success}"
        digest = result.get("digest")
        if digest:
            flash += f"\nDigest: `{str(digest)[:18]}…`"
    else:
        flash = f"⚠️ {result.get('error') or 'Operation failed.'}"
    await _show_home(query, telegram_id, flash=flash)


async def handle_vault_text(update: Update, context: CallbackContext) -> bool:
    pending = (context.user_data.get(_DEPOSIT_PENDING_KEY) or "").strip()
    if pending not in {"deposit", "withdraw"}:
        return False
    if not update.message or not update.effective_user:
        return False
    raw = (update.message.text or "").strip().lstrip("$").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        await update.message.reply_text("Please reply with a positive number, e.g. `250`.")
        return True
    if amount <= 0:
        await update.message.reply_text("Amount must be greater than zero.")
        return True
    context.user_data.pop(_DEPOSIT_PENDING_KEY, None)
    telegram_id = int(update.effective_user.id)
    # The confirm card is bound to the network it is quoted on; when that
    # cannot be read right now, no card (never one bound to a guess).
    network = await active_network(telegram_id)
    if network is None:
        await network_unknown_message(
            update.message,
            kind=f"vault_{pending}_card",
            telegram_id=telegram_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="vault:home")]]),
        )
        return True
    if pending == "deposit":
        snapshot = await run_blocking(get_user_vault_snapshot, telegram_id)
        text, kb = _deposit_confirm_card(amount, snapshot, network=network)
    else:
        snapshot = await run_blocking(get_user_vault_snapshot, telegram_id)
        lp_balance = float(snapshot.get("lp_balance") or 0.0)
        lp_value = float(snapshot.get("lp_value_usdt0") or 0.0)
        if lp_balance <= 0:
            await update.message.reply_text("You don't have any NLP to burn yet.")
            return True
        est_usdt0 = lp_value * (amount / lp_balance) if lp_balance > 0 else 0.0
        est_fee = estimate_withdraw_fee_usdt0(est_usdt0)
        text, kb = _withdraw_confirm_card(amount, est_usdt0, est_fee, network=network)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return True
