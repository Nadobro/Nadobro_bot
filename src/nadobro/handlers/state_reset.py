import logging

from telegram.ext import CallbackContext

from src.nadobro.core.async_utils import run_blocking_db
from src.nadobro.strategy.strategy_pending_input import clear_strategy_pending_input
from src.nadobro.trading.text_trade_pending import (
    clear_text_close_all_pending,
    clear_text_trade_pending,
)
from src.nadobro.users.wallet_pending_flow import clear_wallet_pending_flow

logger = logging.getLogger(__name__)

# Any in-progress conversational/multi-step state that should be discarded
# when users intentionally navigate back home.
_TRANSIENT_USER_DATA_KEYS = (
    "pending_trade",
    "pending_question",
    "pending_alert",
    "pending_strategy_input",
    "pending_bro_input",
    "pending_copy_wallet",
    "pending_admin_copy_wallet",
    "pending_referral_claim",
    "wallet_flow",
    "wallet_linked_signer_pk",
    "wallet_main_address",
    "wallet_linked_signer_address",
    "pending_text_trade",
    "pending_text_close_all",
    "trade_flow",
    "trade_flow_custom_size",
    "trade_flow_tp_input",
    "trade_flow_sl_input",
    "trade_flow_limit_price_input",
    "copy_setup",
    "active_setup",
    "vault_pending_amount",
)

# Previews that survive ordinary navigation (the guided trade card owns its own
# Home/Cancel exits; the Volume fee consent is re-checked at ack time) but are
# meaningless after a testnet<->mainnet switch. PREVIEW-NETWORK-BIND: every
# confirm is also bound to its network at confirm time (handlers/
# network_guard.py), so this clear is hygiene, not the safety control.
# Never listed here: ``vault_op_inflight`` (guards a mint/burn still in flight)
# and the home card's own message key (``home_card.HOME_CARD_KEY``).
_NETWORK_SWITCH_EXTRA_KEYS = (
    "trade_card_session",
    "vol_fee_quote",
)


def _clear_persisted_pending(telegram_user_id: int) -> None:
    """Drop the bot_state copies of in-progress flows (sync psycopg2 IO).

    Each clearer is independent: one failing must not keep the others."""
    uid = int(telegram_user_id)
    for clearer in (
        clear_strategy_pending_input,
        clear_text_trade_pending,
        clear_text_close_all_pending,
        clear_wallet_pending_flow,
    ):
        try:
            clearer(uid)
        except Exception:  # policy: degrade-ok(stale pending state expires by TTL; confirm-time checks still apply)
            # Debug, not warning: this also runs on every nav tap, where the
            # old code swallowed these silently.
            logger.debug("state_reset: %s failed for uid=%s", getattr(clearer, "__name__", clearer), uid, exc_info=True)


def clear_pending_user_state(context: CallbackContext | None, telegram_user_id: int | None = None) -> None:
    if context is None:
        return
    for key in _TRANSIENT_USER_DATA_KEYS:
        context.user_data.pop(key, None)
    if telegram_user_id is not None:
        _clear_persisted_pending(int(telegram_user_id))


async def clear_state_after_network_switch(context: CallbackContext | None, telegram_user_id: int) -> None:
    """Discard every in-flight preview after a SUCCESSFUL network switch.

    Call it only once the switch has actually happened: a refused or failed
    switch leaves the user on the network their previews were built on, so
    those previews are still valid. The in-memory pops run on the loop (dict
    ops); the persisted bot_state deletes run on the DB pool."""
    if context is not None:
        for key in _TRANSIENT_USER_DATA_KEYS + _NETWORK_SWITCH_EXTRA_KEYS:
            context.user_data.pop(key, None)
    await run_blocking_db(_clear_persisted_pending, int(telegram_user_id))
