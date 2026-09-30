"""Wallet callback handlers (wallet:* callback data). Rendering helpers live in wallet_view.py.

Extracted from callbacks.py (decomposition slice, 2026-06). May import
shared utils from callbacks at module level — callbacks only imports this
module lazily inside its _handle_wallet shim, so there is no cycle.
"""
from __future__ import annotations

import logging

from src.nadobro.handlers.formatters import escape_md, fmt_network_switch_result, fmt_wallet_balance_card, fmt_wallet_balance_error, fmt_wallet_connect_card, fmt_wallet_info, fmt_wallet_revoke_steps_card
from src.nadobro.handlers.keyboards import wallet_kb, wallet_kb_not_linked, wallet_revoke_confirm_kb
from src.nadobro.core.async_utils import run_blocking, run_blocking_sdk_capped
from src.nadobro.strategy.network_switch import switch_network
from src.nadobro.users.user_service import get_user_readonly_client, get_user_wallet_info, get_user, remove_user_private_key
from telegram.constants import ParseMode

from src.nadobro.handlers.callbacks import _edit_loc  # noqa: E402

logger = logging.getLogger(__name__)

# CLICK-PATH-BLOCKING fix: wallet:view and wallet:balance used to run their
# venue reads (get_user_wallet_info / get_balance) BARE on the event-loop
# thread — one hung gateway read froze every user's taps and the APScheduler
# jobs for the full SDK timeout (~12-30s), the documented "sync get_balance
# hung taps" incident on the inline-button path the earlier home-card fix never
# reached. These reads now go through run_blocking_sdk_capped (SDK pool + a
# short wall-clock cap): the tap returns a placeholder instead of blocking the
# loop, and the orphaned thread warms the cache for the next render.
_WALLET_VIEW_CEILING_SECONDS = 6.0
_TIMED_OUT = object()
_WALLET_REFRESHING = "⏳ Refreshing wallet… tap again in a sec\\."
_BALANCE_REFRESHING = "⏳ Refreshing balance… tap again in a sec\\."


async def _handle_wallet(query, data, telegram_id, context):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "view"

    if action == "view":
        show_connect = False
        # The verify_signer read hits the venue — offload + cap so a slow
        # gateway can't freeze the loop. On timeout show a refreshing
        # placeholder (the orphaned thread warms the cache for the retry).
        try:
            info = await run_blocking_sdk_capped(
                get_user_wallet_info, telegram_id, verify_signer=True,
                timeout_seconds=_WALLET_VIEW_CEILING_SECONDS, default=_TIMED_OUT,
            )
        except Exception:
            info = _TIMED_OUT
        if info is _TIMED_OUT:
            await _edit_loc(query, _WALLET_REFRESHING,
                            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=wallet_kb())
            return
        is_linked = bool(info and info.get("is_linked"))
        if not is_linked and context is not None:
            # Local key generation — fast, no venue read, safe on the loop.
            from src.nadobro.handlers.wallet_view import _ensure_pending_wallet_signer

            pk_hex, _ = _ensure_pending_wallet_signer(context, telegram_id)
            msg, kb = fmt_wallet_connect_card(pk_hex), wallet_kb_not_linked()
            show_connect = True
        else:
            msg, kb = fmt_wallet_info(info), (wallet_kb() if is_linked else wallet_kb_not_linked())
        await _edit_loc(query,
            msg,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=kb,
        )
        # SECURITY: this message renders the 1CT signer private key. Remember its
        # coordinates so we can delete it from chat history the moment the wallet
        # is linked (the key is no longer needed once it's on Nado + encrypted).
        if show_connect and context is not None and getattr(query, "message", None) is not None:
            context.user_data["wallet_connect_msg"] = {
                "chat_id": query.message.chat_id,
                "message_id": query.message.message_id,
            }
    elif action == "balance":
        # Client construction + get_balance() both hit the venue — offload +
        # cap so a throttled gateway can't freeze the loop (the documented
        # "sync get_balance hung taps 30-60s" incident).
        client = await run_blocking_sdk_capped(
            get_user_readonly_client, telegram_id,
            timeout_seconds=_WALLET_VIEW_CEILING_SECONDS, default=None,
        )
        if not client:
            await _edit_loc(query,
                fmt_wallet_balance_error(),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=wallet_kb(),
            )
            return
        bal = await run_blocking_sdk_capped(
            client.get_balance,
            timeout_seconds=_WALLET_VIEW_CEILING_SECONDS, default=_TIMED_OUT,
        )
        if bal is _TIMED_OUT:
            await _edit_loc(query, _BALANCE_REFRESHING,
                            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=wallet_kb())
            return
        try:
            usdt = (bal.get("balances") or {}).get(0, 0) or (bal.get("balances") or {}).get("0", 0)
            msg = fmt_wallet_balance_card(float(usdt or 0))
        except Exception:
            msg = fmt_wallet_balance_error()
        await _edit_loc(query, msg, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=wallet_kb())
    elif action == "revoke_steps":
        await _edit_loc(query, fmt_wallet_revoke_steps_card(), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=wallet_revoke_confirm_kb())
    elif action == "revoke_confirm":
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "testnet"
        ok, msg = remove_user_private_key(telegram_id, network)
        success_msg = "✅ Key reset! Your stored signer has been cleared. Tap 👛 Wallet to link a new 1CT key."
        fail_msg = "❌ {msg}"
        if ok:
            await _edit_loc(query, success_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=wallet_kb_not_linked())
        else:
            await _edit_loc(query, fail_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=wallet_kb(), msg=escape_md(msg))
    elif action == "remove_active":
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "testnet"
        ok, msg = remove_user_private_key(telegram_id, network)
        prefix = "✅" if ok else "❌"
        await _edit_loc(query, 
            "{prefix} {msg}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=wallet_kb(),
            prefix=prefix,
            msg=escape_md(msg),
        )
    elif action == "network" and len(parts) >= 3:
        net = parts[2]
        if net not in ("testnet", "mainnet"):
            return

        # Stale-message path (no keyboard emits wallet:network:* anymore) — same
        # fail-closed switch as Execution Mode.
        result = await run_blocking(switch_network, telegram_id, net)
        result_msg = fmt_network_switch_result(result)

        if result.switched:
            # Mode switch invalidates every in-flight preview. Hygiene only:
            # each confirm is also bound to its network (network_guard.py).
            from src.nadobro.handlers.state_reset import clear_state_after_network_switch

            await clear_state_after_network_switch(context, telegram_id)
            info = get_user_wallet_info(telegram_id)
            msg = fmt_wallet_info(info)
            await _edit_loc(query, 
                "{switch_msg}\n\n{wallet_info}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=wallet_kb(),
                switch_msg=result_msg,
                wallet_info=msg,
            )
        else:
            await _edit_loc(query, 
                "❌ {msg}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=wallet_kb(),
                msg=result_msg,
            )
