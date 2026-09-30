"""Shared UI pieces for the Arcus link flow, wallet, network and home cards
(Arcus P3b, 03 §8, §18).

Lives here — not in ``keyboards.py`` / ``formatters.py``, whose snapshot tests
pin every public builder — so the Nado surface stays byte-identical. The name
deliberately does not end in ``keyboards`` and there is no zero-arg keyboard
builder.

Card rules (03 §8):
- HTML parse mode; every dynamic value goes through ``utils.visual.esc`` BEFORE
  ``tr(key, **fmt)``; ``{network}`` is ``net_label(...)`` (uppercase, never
  translated); addresses are shown in full inside ``<code>`` (they are public and
  the user must verify them) or as ``addr_short`` in one-line summaries.
- No ``*`` or backtick character in any string (the parse-mode lint would read
  the builder as Markdown).
- Nothing here ever handles a signing key: the texts, the keyboards and the
  message helpers only see public data (addresses, key NAMES, dates).

The text keys the domain must send itself (reminders, 401 diagnosis) are
defined in ``users/arcus_link_service.py`` (``runtime/`` and ``strategy/`` may
never import ``handlers/``, 03 D-21) and re-exported here.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from src.nadobro.core.feature_flags import arcus_key_expiry_stop_hours
from src.nadobro.handlers.render_utils import plain_text_fallback
from src.nadobro.handlers.venue_handler import TEXT_HELP_LINK, tr
from src.nadobro.i18n import get_active_language, localize_markup
from src.nadobro.users import arcus_link_service as _ls
from src.nadobro.users.arcus_credentials import addr_short, format_utc_ms
from src.nadobro.users.arcus_link_service import (
    LABEL_RENEW_KEY,
    TEXT_BUSY,
    TEXT_D_EXPIRED,
    TEXT_D_NAME_REUSE,
    TEXT_D_NO_CREDENTIAL,
    TEXT_D_OK,
    TEXT_D_OK_NO_EXPIRY,
    TEXT_D_REVOKED,
    TEXT_D_SKEW,
    TEXT_D_UNKNOWN_401,
    TEXT_D_WRONG_SCOPE,
    TEXT_KR_DAYS,
    TEXT_KR_EXPIRED,
    TEXT_KR_HOURS,
    TEXT_PR_NOT_ELIGIBLE,
    TEXT_R_WALLET_KEY,
)
from src.nadobro.utils.venue_scope import parse_arcus_net
from src.nadobro.utils.visual import esc

logger = logging.getLogger(__name__)

__all__ = [
    "addr_short",
    "format_utc_ms",
    "net_label",
    "fmt_until",
    "fmt_utc",
    "days_left",
    "key_soon_line",
    "kb",
    "bump_seq",
    "current_seq",
    "send_html",
    "reply_card",
    "edit_or_send",
    "ARCUS_P3B_TEXT_KEYS",
    "ARCUS_P3B_LABEL_KEYS",
]

_DAY_MS = 86_400_000
_KEY_SOON_DAYS = 14
_CALLBACK_DATA_MAX_BYTES = 64  # Telegram's callback_data limit

# --- text keys (English i18n sources; all five translations in i18n.py) ---------------------
# Shared/domain-owned keys, re-exported (defined in users/arcus_link_service.py, 03 D-21):
# TEXT_BUSY, TEXT_PR_NOT_ELIGIBLE, TEXT_R_WALLET_KEY, TEXT_D_*, TEXT_KR_*, LABEL_RENEW_KEY.

TEXT_NO_EXPIRY = "no expiry"
TEXT_DB_BUSY = "Couldn't check right now — nothing was stored. Try again in a moment."
TEXT_MAINNET_CLOSED = "Arcus mainnet isn't open in Nadobro yet."

# Wallet card.
TEXT_W_TITLE = "👛 <b>Arcus wallet</b> · {network}"
TEXT_W_NOT_LINKED = (
    "Not linked yet. Link your Arcus account with an API Signing Key from the Arcus app — "
    "never your wallet key."
)
TEXT_W_LINK_CLOSED = "Linking is closed for your account right now."
TEXT_W_LINKED = "Linked: <code>{address}</code> · subaccount 0"
TEXT_W_KEY = "Key <code>{key_name}</code> · valid until {until}"
TEXT_W_KEY_SOON = (
    "⚠️ The key expires in {days} days. Renew it soon: Arcus strategies stop {stop_hours} hours "
    "before a key expires."
)
TEXT_W_ALL_SUBACCOUNTS = "This key can trade all your subaccounts. Nadobro uses subaccount 0 only."
TEXT_W_VERIFIED = "Last checked: {when}"
TEXT_W_EXPIRED = "⚠️ The key expired on {until}. Renew it to trade on Arcus."
TEXT_W_INVALID = "⚠️ This key is no longer active on Arcus. Link a new key."
TEXT_W_UNREADABLE = "⚠️ Couldn't read your Arcus link right now. Try again in a moment."
TEXT_W_LINKING = "🔗 Linking in progress."

# Attestation (legal text: translations need owner review, O5).
TEXT_A_TITLE = "🔗 <b>Link Arcus</b> · {network}"
TEXT_A_CONFIRM_HEAD = "Before you link, confirm that:"
TEXT_A_NOT_RESTRICTED = (
    "• You are not a resident or citizen of the United States, Canada or the United Kingdom, "
    "and you are not in a sanctioned or otherwise restricted jurisdiction."
)
TEXT_A_TERMS = "• You accept the Arcus Terms of Use: {terms_url}"
TEXT_A_NOTE_HEAD = "Good to know:"
TEXT_A_PUBLIC = (
    "• Arcus account activity is public: anyone can read the balances, positions, orders and "
    "fills of an address."
)
TEXT_A_SUBACCOUNT = (
    "• Nadobro trades subaccount 0. It is shared with anything you do there by hand: the order "
    "budget, leverage and open orders."
)
TEXT_A_TRADE_ONLY = (
    "• Nadobro stores your API key encrypted. It can trade but cannot withdraw, and you can "
    "revoke it in the Arcus app at any time."
)

# Address step.
TEXT_AD_ASK = (
    "Send the wallet address you trade with on Arcus (0x followed by 40 characters). Only the "
    "address — never a private key."
)
TEXT_AD_ASK_RENEW = "Renewing? Tap Same address to keep <code>{address}</code>, or send another address."
TEXT_AD_INVALID = "That doesn't look like an address. Send 0x followed by 40 hex characters."
TEXT_AD_CHECKING = "Checking <code>{address}</code> on Arcus {network}…"

# Precheck results.
TEXT_PR_OK = "✅ <code>{address}</code> can use Arcus {network}."
TEXT_PR_NO_ACTIVITY = "This Arcus account has no activity yet. You can deposit in the Arcus app after linking."
TEXT_PR_NOT_ELIGIBLE_MAINNET = (
    "Arcus mainnet is open only to addresses Arcus has already approved. You can still link on "
    "Arcus testnet (🌐 Network)."
)
TEXT_PR_BLOCKED = "Arcus's compliance screening blocked this address. It can't be linked."
TEXT_PR_GEO = "Arcus perpetuals aren't available from Nadobro's servers right now. The team has been alerted."
TEXT_PR_TAKEN = "This address is already linked to another Nadobro account."
TEXT_PR_AUTOMATION = "Stop your Arcus strategies before linking a different address."

# Key instructions.
TEXT_IN_TITLE = "🔑 <b>Create an API key for Nadobro</b>"
TEXT_IN_1 = "1. Open the Arcus API Keys page and connect this wallet."
TEXT_IN_2 = (
    "2. Enter the name <code>{key_name}</code> and tap Generate. Use this new name: reusing a name "
    "you already have replaces that key."
)
TEXT_IN_3 = "3. Copy the API Signing Key now — Arcus shows it only once."
TEXT_IN_4 = "4. Set Subaccount # to 0 and Days Valid to 180, tap Authorize and sign in your wallet."
TEXT_IN_5 = "5. Paste the API Signing Key here. I delete your message right away."
TEXT_IN_WARN = "Never paste your wallet's private key or seed phrase."

# Key paste / checking.
TEXT_K_ACK = (
    "🔐 Key received and removed from the chat. Checking it on Arcus — this can take up to a minute."
)
TEXT_K_CHECKING = "🔐 Checking your key on Arcus — this can take up to a minute."
TEXT_K_CHECKING_STORED = "🔐 Checking your linked key on Arcus…"
TEXT_K_WAIT_ADDRESS = (
    "🔐 Key received and removed from the chat. I'll check it as soon as the address check finishes."
)
TEXT_K_NOT_DELETED = "⚠️ I couldn't delete your message. Delete it yourself now — it contains a secret key."
TEXT_K_WAIT_PASTE = "Paste the API Signing Key (64 characters), or tap Cancel."
TEXT_K_ATTEST_FIRST = "Tap I confirm to continue, or Cancel."
TEXT_K_ADDRESS_FIRST = "🔐 I deleted your key. Send your wallet address first, then paste the key again."
TEXT_K_STILL_CHECKING = "⏳ Still checking — the result will appear here."
TEXT_K_GENERIC_DELETED = (
    "🔐 I deleted a message that looked like a secret key. Only paste a key when linking asks for it."
)
TEXT_K_GENERIC_EXPOSED = "If that was a wallet private key or seed phrase, treat it as exposed and move your funds."

# Link results.
TEXT_R_LINKED = "✅ <b>Linked</b> · <code>{address}</code> · subaccount 0 · key valid until {until}"
TEXT_R_RENEWED = (
    "✅ <b>Key renewed</b> · <code>{address}</code> · valid until {until}. Nadobro uses the new key "
    "from now on."
)
TEXT_R_OLD_KEY = (
    "Your previous key <code>{key_name}</code> still works until it expires. You can revoke it in "
    "the Arcus app."
)
TEXT_R_NO_ACTIVITY = "Your Arcus account has no activity yet. Deposit in the Arcus app to trade."
TEXT_R_SHORT_VALIDITY = "This key expires in {days} days. Use Days Valid 180 next time."
TEXT_R_WALLET_KEY_2 = (
    "I deleted it and stored nothing. Nadobro needs the API Signing Key from the Arcus app, never "
    "your wallet key."
)
TEXT_R_INVALID = "That isn't an Arcus API Signing Key. Paste only the key: 64 characters, 0-9 and a-f."
TEXT_R_PEM = "That looks like a key file. Arcus API Signing Keys are 64 characters, 0-9 and a-f."
TEXT_R_NOT_FOUND = (
    "I couldn't find that key on Arcus {network} for <code>{address}</code>. Check that you tapped "
    "Authorize and signed in your wallet, then tap Check again."
)
TEXT_R_INACTIVE = "That key isn't active on Arcus (it was revoked). Create a new key and paste it here."
TEXT_R_TOO_SOON = "That key expires in less than 24 hours. Create a new key with Days Valid 180."
TEXT_R_WRONG_SUB = "That key doesn't cover subaccount 0. Create a key with Subaccount # set to 0."
TEXT_R_WITHDRAW = (
    "That key can withdraw funds. Nadobro only accepts trade-only keys — create a normal API key "
    "in the Arcus app."
)
TEXT_R_PENDING_EXPIRED = "This linking step timed out. Paste the key again, or tap Link to start over."
TEXT_R_STORE_FAILED = "Couldn't save the link right now. Nothing changed. Tap Check again."
TEXT_R_NO_PENDING = "No link in progress. Tap Link Arcus account to start."
TEXT_R_CANCELLED = "Linking cancelled. Nothing was stored."

# Unlink card.
TEXT_U_TITLE = "🔌 <b>Unlink Arcus</b> · {network}"
TEXT_U_BODY = (
    "Nadobro deletes its encrypted copy of the key for <code>{address}</code>. The key stays valid "
    "on Arcus until you revoke it: Arcus app → API Keys → <code>{key_name}</code>."
)
TEXT_U_RUNNING_NOTE = "Unlinking is refused while Arcus strategies run or bot orders are being cleaned up."
TEXT_U_NADO_NOTE = "Looking for the Nado 1CT key? Tap Nado 1CT key."
TEXT_U_NONE = "No Arcus key is linked on {network}."
TEXT_U_DONE = "✅ Unlinked. Revoke <code>{key_name}</code> in the Arcus app to disable the key completely."
TEXT_U_REFUSED = (
    "Stop your Arcus strategies first (/stop_all). Unlinking now would leave orders Nadobro could "
    "no longer cancel."
)

# Network (mode) card.
TEXT_M_TITLE = "🌐 <b>Arcus network</b>"
TEXT_M_VIEWING = "Viewing: <b>{network}</b>"
TEXT_M_LINE = "{network}: {status}"
TEXT_M_STATUS_LINKED = "linked · <code>{address}</code>"
TEXT_M_STATUS_NOT_LINKED = "not linked"
TEXT_M_STATUS_EXPIRED = "key expired"
TEXT_M_STATUS_INVALID = "key not active"
TEXT_M_STATUS_UNKNOWN = "couldn't check"
TEXT_M_BODY = (
    "Switching changes which Arcus account you see and use. It never starts anything. Stop your "
    "Arcus strategies before switching."
)
TEXT_M_REFUSED_RUNNING = "Stop your Arcus strategies first (/stop_all), then switch."
TEXT_M_SWITCHED = "Switched to Arcus {network}."

# Arcus home shell (link status lines).
TEXT_H_NOT_LINKED = "👛 Not linked — link your Arcus account to get started."
TEXT_H_LINKED = "👛 <code>{address}</code> · subaccount 0 · key valid until {until}"
TEXT_H_EXPIRED = "⚠️ Arcus key expired — renew it in 👛 Arcus wallet."
TEXT_H_INVALID = "⚠️ Arcus key not active — link a new key in 👛 Arcus wallet."
TEXT_H_UNREADABLE = "⚠️ Couldn't read your Arcus link right now."
TEXT_H_LINKING = "🔗 Linking in progress — continue in 👛 Arcus wallet."
TEXT_H_STRATEGIES_SOON = "Strategies on Arcus are coming soon."

# --- button labels (new; "🔄 Renew key" is the domain's LABEL_RENEW_KEY) -------------------
LABEL_WALLET = "👛 Arcus wallet"
LABEL_LINK = "🔗 Link Arcus account"
LABEL_CONTINUE = "🔗 Continue linking"
LABEL_RENEW = LABEL_RENEW_KEY
LABEL_CHECK_KEY = "✅ Check key"
LABEL_CHECK_AGAIN = "🔄 Check again"
LABEL_UNLINK = "🔌 Unlink"
LABEL_NETWORK = "🌐 Network"
LABEL_CONFIRM = "✅ I confirm"
LABEL_SAME_ADDRESS = "↩️ Same address"
LABEL_START_OVER = "🔗 Start over"
LABEL_OPEN_API_KEYS = "🌐 Open Arcus API keys"
LABEL_NADO_1CT = "🔄 Nado 1CT key"
# Never "🧪 Testnet" / "🟢 Mainnet": Nado's /mode keyboard renders those untranslated,
# and a _LABELS entry would start translating it for Nado users (03 V-1).
LABEL_MODE_TESTNET_ARCUS = "🧪 Arcus testnet"
LABEL_MODE_MAINNET_ARCUS = "🟢 Arcus mainnet"
# Reused house labels (already translated).
LABEL_HOME = "🏠 Home"
LABEL_BACK = "◀ Back"
LABEL_CANCEL = "❌ Cancel"
LABEL_REFRESH = "🔄 Refresh"

# Every text key of 03 §18 (the i18n test iterates this): the handler-owned keys, the
# domain-owned keys (users/arcus_link_service.I18N_TEXT_KEYS) and TEXT_HELP_LINK.
_HANDLER_TEXT_KEYS: tuple[str, ...] = (
    TEXT_NO_EXPIRY, TEXT_DB_BUSY, TEXT_MAINNET_CLOSED,
    TEXT_W_TITLE, TEXT_W_NOT_LINKED, TEXT_W_LINK_CLOSED, TEXT_W_LINKED, TEXT_W_KEY, TEXT_W_KEY_SOON,
    TEXT_W_ALL_SUBACCOUNTS, TEXT_W_VERIFIED, TEXT_W_EXPIRED, TEXT_W_INVALID, TEXT_W_UNREADABLE,
    TEXT_W_LINKING,
    TEXT_A_TITLE, TEXT_A_CONFIRM_HEAD, TEXT_A_NOT_RESTRICTED, TEXT_A_TERMS, TEXT_A_NOTE_HEAD,
    TEXT_A_PUBLIC, TEXT_A_SUBACCOUNT, TEXT_A_TRADE_ONLY,
    TEXT_AD_ASK, TEXT_AD_ASK_RENEW, TEXT_AD_INVALID, TEXT_AD_CHECKING,
    TEXT_PR_OK, TEXT_PR_NO_ACTIVITY, TEXT_PR_NOT_ELIGIBLE_MAINNET, TEXT_PR_BLOCKED, TEXT_PR_GEO,
    TEXT_PR_TAKEN, TEXT_PR_AUTOMATION,
    TEXT_IN_TITLE, TEXT_IN_1, TEXT_IN_2, TEXT_IN_3, TEXT_IN_4, TEXT_IN_5, TEXT_IN_WARN,
    TEXT_K_ACK, TEXT_K_CHECKING, TEXT_K_CHECKING_STORED, TEXT_K_WAIT_ADDRESS, TEXT_K_NOT_DELETED,
    TEXT_K_WAIT_PASTE, TEXT_K_ATTEST_FIRST, TEXT_K_ADDRESS_FIRST, TEXT_K_STILL_CHECKING,
    TEXT_K_GENERIC_DELETED, TEXT_K_GENERIC_EXPOSED,
    TEXT_R_LINKED, TEXT_R_RENEWED, TEXT_R_OLD_KEY, TEXT_R_NO_ACTIVITY, TEXT_R_SHORT_VALIDITY,
    TEXT_R_WALLET_KEY_2, TEXT_R_INVALID, TEXT_R_PEM, TEXT_R_NOT_FOUND, TEXT_R_INACTIVE,
    TEXT_R_TOO_SOON, TEXT_R_WRONG_SUB, TEXT_R_WITHDRAW, TEXT_R_PENDING_EXPIRED, TEXT_R_STORE_FAILED,
    TEXT_R_NO_PENDING, TEXT_R_CANCELLED,
    TEXT_U_TITLE, TEXT_U_BODY, TEXT_U_RUNNING_NOTE, TEXT_U_NADO_NOTE, TEXT_U_NONE, TEXT_U_DONE,
    TEXT_U_REFUSED,
    TEXT_M_TITLE, TEXT_M_VIEWING, TEXT_M_LINE, TEXT_M_STATUS_LINKED, TEXT_M_STATUS_NOT_LINKED,
    TEXT_M_STATUS_EXPIRED, TEXT_M_STATUS_INVALID, TEXT_M_STATUS_UNKNOWN, TEXT_M_BODY,
    TEXT_M_REFUSED_RUNNING, TEXT_M_SWITCHED,
    TEXT_H_NOT_LINKED, TEXT_H_LINKED, TEXT_H_EXPIRED, TEXT_H_INVALID, TEXT_H_UNREADABLE,
    TEXT_H_LINKING, TEXT_H_STRATEGIES_SOON,
)
ARCUS_P3B_TEXT_KEYS: tuple[str, ...] = tuple(_ls.I18N_TEXT_KEYS) + _HANDLER_TEXT_KEYS + (TEXT_HELP_LINK,)
# In _LABELS order: the domain's "🔄 Renew key" (S2a) first, then the handler labels.
_HANDLER_LABEL_KEYS: tuple[str, ...] = (
    LABEL_WALLET, LABEL_LINK, LABEL_CONTINUE, LABEL_CHECK_KEY, LABEL_CHECK_AGAIN, LABEL_UNLINK,
    LABEL_NETWORK, LABEL_CONFIRM, LABEL_SAME_ADDRESS, LABEL_START_OVER, LABEL_OPEN_API_KEYS,
    LABEL_NADO_1CT, LABEL_MODE_TESTNET_ARCUS, LABEL_MODE_MAINNET_ARCUS,
)
ARCUS_P3B_LABEL_KEYS: tuple[str, ...] = tuple(_ls.I18N_LABEL_KEYS) + _HANDLER_LABEL_KEYS

# Re-exported domain keys (03 D-21), so handler code and tests have one import site.
DOMAIN_TEXT_KEYS = (
    TEXT_BUSY, TEXT_PR_NOT_ELIGIBLE, TEXT_R_WALLET_KEY,
    TEXT_D_OK, TEXT_D_OK_NO_EXPIRY, TEXT_D_SKEW, TEXT_D_EXPIRED, TEXT_D_REVOKED, TEXT_D_NAME_REUSE,
    TEXT_D_WRONG_SCOPE, TEXT_D_UNKNOWN_401, TEXT_D_NO_CREDENTIAL,
    TEXT_KR_DAYS, TEXT_KR_HOURS, TEXT_KR_EXPIRED,
)


# --- pure formatters ----------------------------------------------------------------------

def net_label(network: str) -> str:
    """``"TESTNET"`` / ``"MAINNET"`` for an exact Arcus network token; anything
    else raises ``ValueError`` (``parse_arcus_net``). Never translated."""
    return parse_arcus_net(network).upper()


def fmt_until(valid_until_ms: int | None) -> str:
    """A key's expiry for display: 0 -> "no expiry" (translated); None or an
    unusable value -> "—"; else ``"YYYY-MM-DD HH:MM UTC"``."""
    if valid_until_ms is None:
        return "—"
    if valid_until_ms == 0:
        return tr(TEXT_NO_EXPIRY)
    try:
        return format_utc_ms(int(valid_until_ms))
    except (TypeError, ValueError):
        return "—"


def fmt_utc(dt: datetime | None) -> str:
    """``"YYYY-MM-DD HH:MM UTC"`` or ``"—"``."""
    if not isinstance(dt, datetime):
        return "—"
    when = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def days_left(valid_until_ms: int, now_ms: int) -> int:
    """Whole days until ``valid_until_ms`` (floor), never below 0."""
    return max(0, (int(valid_until_ms) - int(now_ms)) // _DAY_MS)


def key_soon_line(valid_until_ms: int | None, now_ms: int) -> str | None:
    """``TEXT_W_KEY_SOON`` while a key with an expiry has 14 days or less left and
    has not expired yet; None otherwise. Under one day left still warns ("1
    days" is closer to the truth than silence). ``{stop_hours}`` comes from
    ``core.feature_flags.arcus_key_expiry_stop_hours`` (THE single reader of that
    env var), so the text always matches the actual stand-down window."""
    if not valid_until_ms or valid_until_ms <= now_ms:
        return None
    days = days_left(valid_until_ms, now_ms)
    if days > _KEY_SOON_DAYS:
        return None
    return tr(
        TEXT_W_KEY_SOON,
        days=esc(str(max(1, days))),
        stop_hours=esc(f"{arcus_key_expiry_stop_hours():g}"),
    )


def kb(rows: Sequence[Sequence[tuple[Any, ...]]]) -> InlineKeyboardMarkup:
    """An inline keyboard from ``(label, callback_data)`` or ``(label, None, url)``
    tuples. Every callback_data fits Telegram's 64-byte limit (asserted)."""
    out: list[list[InlineKeyboardButton]] = []
    for row in rows:
        buttons: list[InlineKeyboardButton] = []
        for item in row:
            if len(item) == 3:
                label, _none, url = item
                buttons.append(InlineKeyboardButton(label, url=url))
                continue
            label, data = item
            assert len(data.encode("utf-8")) <= _CALLBACK_DATA_MAX_BYTES, "callback_data too long"
            buttons.append(InlineKeyboardButton(label, callback_data=data))
        if buttons:
            out.append(buttons)
    return InlineKeyboardMarkup(out)


# --- interaction sequence (handlers/callbacks.py) --------------------------------------------

def _chat_id(query: Any, telegram_id: int) -> int:
    return int(getattr(getattr(query, "message", None), "chat_id", None) or telegram_id)


def bump_seq(query: Any, telegram_id: int) -> None:
    """Advance the chat's interaction sequence for an in-place edit made outside
    ``handle_callback``, so a background edit started from an earlier screen
    stands down instead of clobbering this one."""
    from src.nadobro.handlers.callbacks import bump_interaction_seq

    bump_interaction_seq(_chat_id(query, telegram_id))


def current_seq(chat_id: int) -> int:
    from src.nadobro.handlers.callbacks import interaction_seq

    return interaction_seq(int(chat_id))


# --- message helpers (HTML; never raise on Telegram errors) ---------------------------------

def _localized(markup: InlineKeyboardMarkup | None) -> Any:
    return localize_markup(markup, get_active_language()) if markup is not None else None


async def send_html(bot: Any, chat_id: int, text: str, markup: InlineKeyboardMarkup | None) -> Any | None:
    """A new HTML message (link previews off). A text Telegram cannot parse is
    re-sent as plain text; any other Telegram error returns None (logged by
    type only)."""
    reply_markup = _localized(markup)
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "can't parse entities" not in str(exc).lower():
            logger.warning("arcus send failed chat=%s (%s)", chat_id, type(exc).__name__)
            return None
    except TelegramError as exc:
        logger.warning("arcus send failed chat=%s (%s)", chat_id, type(exc).__name__)
        return None
    try:
        return await bot.send_message(
            chat_id=chat_id, text=plain_text_fallback(text), reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramError as exc:
        logger.warning("arcus plain send failed chat=%s (%s)", chat_id, type(exc).__name__)
        return None


async def reply_card(message: Any, text: str, markup: InlineKeyboardMarkup | None) -> Any | None:
    """Reply to ``message`` with an HTML card and return the sent message (the
    link flow edits it later). A Telegram error returns None (logged by type)."""
    reply_markup = _localized(markup)
    try:
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup, disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "can't parse entities" not in str(exc).lower():
            logger.warning("arcus reply failed (%s)", type(exc).__name__)
            return None
    except TelegramError as exc:
        logger.warning("arcus reply failed (%s)", type(exc).__name__)
        return None
    try:
        return await message.reply_text(
            plain_text_fallback(text), reply_markup=reply_markup, disable_web_page_preview=True,
        )
    except TelegramError as exc:
        logger.warning("arcus plain reply failed (%s)", type(exc).__name__)
        return None


async def edit_or_send(
    bot: Any,
    message: Any | None,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None,
    *,
    seq: int | None = None,
) -> Any | None:
    """Background edits only. With ``seq`` (the card the user TAPPED, captured
    when the task started) and a different current sequence, the user has
    navigated since: never edit (it would clobber the newer screen) — send a new
    message instead. Otherwise edit ``message`` in place; "message is not
    modified" keeps it; any other edit failure (deleted, too old, no message)
    sends a new message. Returns the message now showing ``text`` (or None)."""
    if message is not None and (seq is None or current_seq(chat_id) == seq):
        try:
            await message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=_localized(markup),
                disable_web_page_preview=True,
            )
            return message
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message
            logger.info("arcus edit failed chat=%s (%s); sending a new message", chat_id, type(exc).__name__)
        except Exception as exc:  # policy: degrade-ok(the card is re-sent as a new message)
            logger.info("arcus edit failed chat=%s (%s); sending a new message", chat_id, type(exc).__name__)
    return await send_html(bot, chat_id, text, markup)
