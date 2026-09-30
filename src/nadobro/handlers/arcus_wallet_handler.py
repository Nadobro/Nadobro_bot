"""Arcus wallet: paste-key linking, key status, unlink, network card and the
Arcus-scoped secret interceptor (Arcus P3b, 03 §9).

Owner decisions encoded (build_decisions #2, #3, #5, D-11):
- Link flow: attestation (not US/CA/UK or a sanctioned / restricted
  jurisdiction; Arcus account data is public by address; subaccount 0 is shared
  with manual trading) -> the wallet ADDRESS first (whitelist + compliance
  checked before the user creates a key) -> instructions with a bot-generated
  unique key name, Subaccount # 0, Days Valid 180 and the API Keys page ->
  the user pastes the "API Signing Key" -> the message is DELETED at once ->
  verification in the background (off the per-user lock) with a follow-up
  message and [🔄 Check again] -> the Linked card. Renewal = the same flow with a
  NEW generated name; the stored key is swapped in one statement (no gap).
- The secret interceptor (called by ``handlers/venue_gate.py`` FIRST, for new
  AND edited messages of a user on the Arcus view, with an unreadable venue, or
  with an Arcus link pending) deletes the message before anything else, never
  logs it and never lets it reach the LOWIQPTS relay or the LLM.
- DENIED != EMPTY: a failed read renders "couldn't read / couldn't check",
  never "Not linked" / "No key"; no unlink or switch button on an unknown state.

Secrets: the pasted text lives only in the Telegram ``Update`` and in the
``_process_paste`` coroutine argument until ``arcus_link_service.intake_key``
has sealed it (Fernet). ``user_data`` holds only :class:`LinkPending` (no seed,
no ciphertext, no public key). Nothing here logs message text; exceptions on a
key path are logged by type only, and no exception escapes a background task
(``fire_and_forget``'s reaper would log it with its message).

Render functions (``render_wallet`` / ``render_mode`` / ``render_unlink``) are
DB-only (``run_blocking_db``) and never call the Arcus venue: they sit on the
tap path.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Coroutine
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from telegram import InlineKeyboardMarkup

from src.nadobro.config import ARCUS_TERMS_DEFAULT, arcus_api_keys_url, arcus_terms_url
from src.nadobro.core.async_utils import fire_and_forget, run_blocking_db
from src.nadobro.core.feature_flags import (
    arcus_enabled_for,
    arcus_link_pending_ttl_s,
    arcus_mainnet_enabled,
)
from src.nadobro.handlers import arcus_ui
from src.nadobro.handlers.arcus_ui import (
    LABEL_BACK,
    LABEL_CANCEL,
    LABEL_CHECK_AGAIN,
    LABEL_CHECK_KEY,
    LABEL_CONFIRM,
    LABEL_CONTINUE,
    LABEL_HOME,
    LABEL_LINK,
    LABEL_MODE_MAINNET_ARCUS,
    LABEL_MODE_TESTNET_ARCUS,
    LABEL_NADO_1CT,
    LABEL_NETWORK,
    LABEL_OPEN_API_KEYS,
    LABEL_REFRESH,
    LABEL_RENEW,
    LABEL_SAME_ADDRESS,
    LABEL_START_OVER,
    LABEL_UNLINK,
    LABEL_WALLET,
    TEXT_A_CONFIRM_HEAD,
    TEXT_A_NOT_RESTRICTED,
    TEXT_A_NOTE_HEAD,
    TEXT_A_PUBLIC,
    TEXT_A_SUBACCOUNT,
    TEXT_A_TERMS,
    TEXT_A_TITLE,
    TEXT_A_TRADE_ONLY,
    TEXT_AD_ASK,
    TEXT_AD_ASK_RENEW,
    TEXT_AD_CHECKING,
    TEXT_AD_INVALID,
    TEXT_BUSY,
    TEXT_DB_BUSY,
    TEXT_IN_1,
    TEXT_IN_2,
    TEXT_IN_3,
    TEXT_IN_4,
    TEXT_IN_5,
    TEXT_IN_TITLE,
    TEXT_IN_WARN,
    TEXT_K_ACK,
    TEXT_K_ADDRESS_FIRST,
    TEXT_K_ATTEST_FIRST,
    TEXT_K_CHECKING,
    TEXT_K_CHECKING_STORED,
    TEXT_K_GENERIC_DELETED,
    TEXT_K_GENERIC_EXPOSED,
    TEXT_K_NOT_DELETED,
    TEXT_K_STILL_CHECKING,
    TEXT_K_WAIT_ADDRESS,
    TEXT_K_WAIT_PASTE,
    TEXT_M_BODY,
    TEXT_M_LINE,
    TEXT_M_REFUSED_RUNNING,
    TEXT_M_STATUS_EXPIRED,
    TEXT_M_STATUS_INVALID,
    TEXT_M_STATUS_LINKED,
    TEXT_M_STATUS_NOT_LINKED,
    TEXT_M_STATUS_UNKNOWN,
    TEXT_M_SWITCHED,
    TEXT_M_TITLE,
    TEXT_M_VIEWING,
    TEXT_MAINNET_CLOSED,
    TEXT_PR_AUTOMATION,
    TEXT_PR_BLOCKED,
    TEXT_PR_GEO,
    TEXT_PR_NO_ACTIVITY,
    TEXT_PR_NOT_ELIGIBLE,
    TEXT_PR_NOT_ELIGIBLE_MAINNET,
    TEXT_PR_OK,
    TEXT_PR_TAKEN,
    TEXT_R_CANCELLED,
    TEXT_R_INACTIVE,
    TEXT_R_INVALID,
    TEXT_R_LINKED,
    TEXT_R_NO_ACTIVITY,
    TEXT_R_NO_PENDING,
    TEXT_R_NOT_FOUND,
    TEXT_R_OLD_KEY,
    TEXT_R_PEM,
    TEXT_R_PENDING_EXPIRED,
    TEXT_R_RENEWED,
    TEXT_R_SHORT_VALIDITY,
    TEXT_R_STORE_FAILED,
    TEXT_R_TOO_SOON,
    TEXT_R_WALLET_KEY,
    TEXT_R_WALLET_KEY_2,
    TEXT_R_WITHDRAW,
    TEXT_R_WRONG_SUB,
    TEXT_U_BODY,
    TEXT_U_DONE,
    TEXT_U_NADO_NOTE,
    TEXT_U_NONE,
    TEXT_U_REFUSED,
    TEXT_U_RUNNING_NOTE,
    TEXT_U_TITLE,
    TEXT_W_ALL_SUBACCOUNTS,
    TEXT_W_EXPIRED,
    TEXT_W_INVALID,
    TEXT_W_KEY,
    TEXT_W_LINK_CLOSED,
    TEXT_W_LINKED,
    TEXT_W_LINKING,
    TEXT_W_NOT_LINKED,
    TEXT_W_TITLE,
    TEXT_W_UNREADABLE,
    TEXT_W_VERIFIED,
)
from src.nadobro.handlers.venue_handler import (
    LABEL_VENUE,
    TEXT_ARCUS_NOT_ALLOWED,
    TEXT_SWITCH_FAILED,
    edit_html,
    read_active_venue,
    tr,
)
from src.nadobro.i18n import get_active_language, get_user_language, language_context
from src.nadobro.users import arcus_credentials as creds
from src.nadobro.users import arcus_link_service as link_service
from src.nadobro.users import audit_log, venue_service
from src.nadobro.users.arcus_link_service import (
    ARCUS_ATTESTATION_VERSION,
    AddressCheck,
    KeyDiagnosis,
    KeyIntake,
    KeyVerdict,
    LinkOutcome,
    LinkPending,
    LinkResult,
)
from src.nadobro.utils.secret_text import SecretShape
from src.nadobro.utils.venue_scope import (
    ARCUS_MAINNET_SCOPE,
    ARCUS_NETWORK_MAINNET,
    ARCUS_NETWORK_MODES,
    ARCUS_NETWORK_TESTNET,
    VENUE_ARCUS,
    arcus_scope_for,
    parse_arcus_net,
)
from src.nadobro.utils.visual import esc

logger = logging.getLogger(__name__)

# --- callback data (all ax:* are ARCUS_ONLY at the gate; each <= 64 bytes) -------------------
CB_WALLET = "ax:wallet"
CB_LINK_START = "ax:link:start"
CB_LINK_ATTEST = "ax:link:attest"
CB_LINK_SAME = "ax:link:same"
CB_LINK_CHECK = "ax:link:check"
CB_LINK_CANCEL = "ax:link:cancel"
CB_UNLINK = "ax:unlink"
CB_UNLINK_CONFIRM_PREFIX = "ax:unlink:confirm:"  # + the network the card showed
CB_MODE = "ax:mode"
CB_MODE_SET_PREFIX = "ax:mode:"  # + the target network
CB_HOME = "ax:home"
# The only foreign callbacks emitted: the venue card (NEUTRAL) and the Nado 1CT
# revoke steps (NEVER_GATE — the Nado revoke path stays reachable from Arcus).
CB_VENUE_VIEW = "venue:view"
CB_NADO_REVOKE_STEPS = "wallet:revoke_steps"

PENDING_KEY = "arcus_link_pending"

_ADDRESS_STEPS = frozenset({"address", "address_check", "key", "verifying"})
_PASTE_STEPS = frozenset({"address_check", "key", "verifying"})
_ADDRESS_INPUT_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ELIGIBLE = frozenset({AddressCheck.ELIGIBLE, AddressCheck.ELIGIBLE_NO_ACTIVITY})
_SHORT_VALIDITY_DAYS = 14

# asyncio.wait_for bounds for the background tasks (03 §9.3).
_PRECHECK_TIMEOUT_S = 30.0
_VERIFY_TIMEOUT_S = 90.0
_DIAGNOSE_TIMEOUT_S = 30.0

# How a link result moves the flow (03 §14.1) and which buttons it gets.
_LINKED = "linked"
_RETRY = "retry"  # stash kept: [🔄 Check again]
_PASTE = "paste"  # this key is final: paste another one (step "key")
_TERMINAL = "terminal"  # the flow ends
_RESULT_KIND: dict[LinkResult, str] = {
    LinkResult.LINKED: _LINKED,
    LinkResult.LINKED_NO_ACTIVITY: _LINKED,
    LinkResult.BUSY: _RETRY,
    LinkResult.KEY_NOT_FOUND: _RETRY,
    LinkResult.STORE_FAILED: _RETRY,
    LinkResult.WALLET_KEY_REFUSED: _PASTE,
    LinkResult.INVALID_KEY: _PASTE,
    LinkResult.KEY_INACTIVE: _PASTE,
    LinkResult.KEY_EXPIRES_TOO_SOON: _PASTE,
    LinkResult.KEY_WRONG_SUBACCOUNT: _PASTE,
    LinkResult.KEY_HAS_WITHDRAW: _PASTE,
    LinkResult.PENDING_EXPIRED: _PASTE,
    LinkResult.NOT_WHITELISTED: _TERMINAL,
    LinkResult.BLOCKED: _TERMINAL,
    LinkResult.GEO_RESTRICTED: _TERMINAL,
    LinkResult.ALREADY_LINKED_ELSEWHERE: _TERMINAL,
    LinkResult.NOT_ALLOWED: _TERMINAL,
    LinkResult.AUTOMATION_RUNNING: _TERMINAL,
    LinkResult.NO_PENDING: _TERMINAL,
    LinkResult.SUPERSEDED: _TERMINAL,  # never rendered (returned silently)
}
_SIMPLE_RESULT_TEXT: dict[LinkResult, str] = {
    LinkResult.INVALID_KEY: TEXT_R_INVALID,
    LinkResult.KEY_INACTIVE: TEXT_R_INACTIVE,
    LinkResult.KEY_EXPIRES_TOO_SOON: TEXT_R_TOO_SOON,
    LinkResult.KEY_WRONG_SUBACCOUNT: TEXT_R_WRONG_SUB,
    LinkResult.KEY_HAS_WITHDRAW: TEXT_R_WITHDRAW,
    LinkResult.BLOCKED: TEXT_PR_BLOCKED,
    LinkResult.GEO_RESTRICTED: TEXT_PR_GEO,
    LinkResult.ALREADY_LINKED_ELSEWHERE: TEXT_PR_TAKEN,
    LinkResult.AUTOMATION_RUNNING: TEXT_PR_AUTOMATION,
    LinkResult.BUSY: TEXT_BUSY,
    LinkResult.NO_PENDING: TEXT_R_NO_PENDING,
    LinkResult.PENDING_EXPIRED: TEXT_R_PENDING_EXPIRED,
    LinkResult.STORE_FAILED: TEXT_R_STORE_FAILED,
}
# Precheck refusals (the flow ends; nothing was stored).
_PRECHECK_REFUSAL_TEXT: dict[AddressCheck, str] = {
    AddressCheck.NOT_WHITELISTED: TEXT_PR_NOT_ELIGIBLE,
    AddressCheck.BLOCKED: TEXT_PR_BLOCKED,
    AddressCheck.GEO_RESTRICTED: TEXT_PR_GEO,
    AddressCheck.ALREADY_LINKED_ELSEWHERE: TEXT_PR_TAKEN,
    AddressCheck.AUTOMATION_RUNNING: TEXT_PR_AUTOMATION,
    AddressCheck.INVALID: TEXT_AD_INVALID,
}


# --- clocks (the link service's seams: ONE clock for the pending TTL and the stash) ----------

def _mono() -> float:
    return link_service._mono()


def _now_ms() -> int:
    return link_service._now_ms()


# --- pending link state (context.user_data only; no secret, no public key) -------------------

def link_pending(context: Any, telegram_id: int) -> LinkPending | None:
    """The user's in-progress link flow, or None. A malformed entry is dropped;
    an expired one is dropped AND ends the flow (generation bumped, stash
    dropped, tasks cancelled)."""
    user_data = getattr(context, "user_data", None)
    if user_data is None:
        return None
    raw = user_data.get(PENDING_KEY)
    if raw is None:
        return None
    if not isinstance(raw, LinkPending):
        user_data.pop(PENDING_KEY, None)
        return None
    if _mono() >= raw.expires_mono:
        user_data.pop(PENDING_KEY, None)
        link_service.cancel_link(int(telegram_id), raw.network)
        _cancel_tasks(int(telegram_id), raw.network)
        return None
    return raw


def set_link_pending(context: Any, pending: LinkPending) -> None:
    user_data = getattr(context, "user_data", None)
    if user_data is not None:
        user_data[PENDING_KEY] = pending


def clear_link_pending(context: Any, telegram_id: int) -> None:
    """End the flow: drop the entry, bump the generation (in-flight work ends
    SUPERSEDED), drop the stash, cancel the precheck/verify tasks (never the
    calling task itself)."""
    uid = int(telegram_id)
    user_data = getattr(context, "user_data", None)
    raw = user_data.pop(PENDING_KEY, None) if user_data is not None else None
    if isinstance(raw, LinkPending):
        link_service.cancel_link(uid, raw.network)
        _cancel_tasks(uid, raw.network)
    else:
        _cancel_tasks(uid)


def _ttl_expiry() -> float:
    return _mono() + arcus_link_pending_ttl_s()


# --- background tasks (off the per-user lock; 03 §9.3) ---------------------------------------

class _Target:
    """Where a background task reports: the message it edits and — for a card
    the user TAPPED — the interaction sequence captured right after that tap
    (a later tap elsewhere makes the result arrive as a NEW message instead of
    clobbering the newer screen). ``[🔄 Check again]`` on another card while a
    task runs re-points the target there, so "the result will appear here" is
    true."""

    __slots__ = ("message", "chat_id", "seq")

    def __init__(self, message: Any, chat_id: int, seq: int | None) -> None:
        self.message = message
        self.chat_id = int(chat_id)
        self.seq = seq


_TASKS: dict[tuple[int, str, str], asyncio.Task[Any]] = {}
_TARGETS: dict[tuple[int, str, str], _Target] = {}


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _start_task(
    uid: int, network: str, kind: str, coro: Coroutine[Any, Any, None], target: _Target | None = None,
) -> None:
    """One slot per (user, network, kind): a still-running task of the same
    slot is cancelled first (a newer paste supersedes the older one)."""
    key = (int(uid), network, kind)
    old = _TASKS.get(key)
    if old is not None and not old.done() and old is not _current_task():
        old.cancel()
    task = fire_and_forget(coro, name=f"arcus-{kind}:{uid}")
    _TASKS[key] = task
    if target is not None:
        _TARGETS[key] = target

    def _forget(done: asyncio.Task[Any], k: tuple[int, str, str] = key) -> None:
        if _TASKS.get(k) is done:
            _TASKS.pop(k, None)
            _TARGETS.pop(k, None)

    task.add_done_callback(_forget)


def _task_running(uid: int, network: str, kind: str) -> bool:
    task = _TASKS.get((int(uid), network, kind))
    return task is not None and not task.done()


def _cancel_tasks(uid: int, network: str | None = None, kinds: tuple[str, ...] = ("precheck", "verify")) -> None:
    current = _current_task()
    for key, task in list(_TASKS.items()):
        k_uid, k_net, k_kind = key
        if k_uid != int(uid) or k_kind not in kinds or (network is not None and k_net != network):
            continue
        if task is not current and not task.done():
            task.cancel()


def _reset_for_tests() -> None:
    _TASKS.clear()
    _TARGETS.clear()


async def _task_language(uid: int) -> str:
    try:
        return await run_blocking_db(get_user_language, uid)
    except Exception as exc:  # policy: degrade-ok(the task still reports, in the ambient language)
        logger.warning("arcus task language read failed uid=%s (%s)", uid, type(exc).__name__)
        return get_active_language()


async def _report(context: Any, target: _Target, text: str, markup: InlineKeyboardMarkup | None) -> None:
    shown = await arcus_ui.edit_or_send(
        getattr(context, "bot", None), target.message, target.chat_id, text, markup, seq=target.seq,
    )
    if shown is not None and shown is not target.message:
        target.message, target.seq = shown, None  # our own message from now on


def _chat_id(query: Any, telegram_id: int) -> int:
    return int(getattr(getattr(query, "message", None), "chat_id", None) or telegram_id)


# --- small helpers ------------------------------------------------------------------------

async def _show(query: Any, uid: int, text: str, markup: InlineKeyboardMarkup) -> None:
    """Every in-place edit made by :func:`handle`: bump the interaction sequence
    exactly once, then edit the tapped card."""
    arcus_ui.bump_seq(query, uid)
    await edit_html(query, text, markup)


async def _audit(uid: int, action: str, details: str) -> None:
    try:
        await run_blocking_db(audit_log.record_audit_event, uid, action, details)
    except Exception as exc:  # policy: degrade-ok(audit is best-effort; record_audit_event itself never raises)
        logger.warning("arcus audit %s failed uid=%s (%s)", action, uid, type(exc).__name__)


def _fmt(values: dict[str, str]) -> dict[str, str]:
    return {name: esc(value) for name, value in values.items()}


def _net_or_dash(network: str | None) -> str:
    try:
        return arcus_ui.net_label(network) if network is not None else "—"
    except ValueError:
        return "—"


def _is_mainnet(network: str) -> bool:
    return arcus_scope_for(network) == ARCUS_MAINNET_SCOPE


def _link_gate_note(uid: int, network: str) -> str | None:
    """Why a link flow may not start / continue on ``network`` (a text key), or
    None. The egress posture is the BOT's (docs get-compliance-status: "geo is
    derived from the request origin")."""
    if not arcus_enabled_for(uid):
        return TEXT_ARCUS_NOT_ALLOWED
    if _is_mainnet(network) and not arcus_mainnet_enabled():
        return TEXT_MAINNET_CLOSED
    posture = link_service.egress_posture(network)
    if posture is not None and posture.blocked:
        return TEXT_PR_GEO
    return None


async def _viewing_arcus(uid: int) -> bool:
    """Is the user on the Arcus view now? Unreadable counts as Nado (the result
    card then offers only [🔁 Venue], which works on both views)."""
    try:
        return (await read_active_venue(uid)) == VENUE_ARCUS
    except Exception as exc:  # policy: degrade-ok(unreadable venue = Nado view for the result buttons)
        logger.warning("arcus result: venue unreadable uid=%s (%s)", uid, type(exc).__name__)
        return False


# --- cards ----------------------------------------------------------------------------------

async def render_wallet(
    telegram_id: int, *, context: Any = None, note: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """The Arcus wallet card (link status, key, expiry). DB reads only — never a
    venue call. A failed read says so; it never says "Not linked"."""
    uid = int(telegram_id)
    net: str | None = None
    try:
        net = await run_blocking_db(venue_service.get_arcus_network_mode, uid)
        row = await run_blocking_db(creds.get_credential, uid, net)
    except Exception as exc:  # policy: degrade-ok(the card says it could not read the link — never "Not linked")
        logger.warning("arcus wallet: link unreadable uid=%s (%s)", uid, type(exc).__name__)
        lines = [tr(TEXT_W_TITLE, network=esc(_net_or_dash(net))), "", tr(TEXT_W_UNREADABLE)]
        if note:
            lines += ["", note]
        return "\n".join(lines), arcus_ui.kb([[(LABEL_REFRESH, CB_WALLET), (LABEL_HOME, CB_HOME)]])
    assert net is not None
    enabled = arcus_enabled_for(uid)
    now_ms = _now_ms()
    linked = row is not None and row.status != "unlinked"
    lines = [tr(TEXT_W_TITLE, network=esc(arcus_ui.net_label(net))), ""]
    if not linked:
        lines.append(tr(TEXT_W_NOT_LINKED))
        if not enabled:
            lines.append(tr(TEXT_W_LINK_CLOSED))
    else:
        assert row is not None
        lines.append(tr(TEXT_W_LINKED, address=esc(row.address)))
        until = row.valid_until_ms
        if row.status == "active":
            lines.append(
                tr(TEXT_W_KEY, key_name=esc(row.api_wallet_name or "—"), until=esc(arcus_ui.fmt_until(until)))
            )
            if until and until <= now_ms:
                # Past its validUntil but not flipped yet (the lifecycle job runs every
                # few minutes): never show an expired key as healthy.
                lines.append(tr(TEXT_W_EXPIRED, until=esc(arcus_ui.fmt_until(until))))
            else:
                soon = arcus_ui.key_soon_line(until, now_ms)
                if soon:
                    lines.append(soon)
            if row.all_subaccounts:
                lines.append(tr(TEXT_W_ALL_SUBACCOUNTS))
            lines.append(tr(TEXT_W_VERIFIED, when=esc(arcus_ui.fmt_utc(row.last_verified_at))))
        elif row.status == "expired":
            lines.append(tr(TEXT_W_EXPIRED, until=esc(arcus_ui.fmt_until(until))))
        else:  # invalid
            lines.append(tr(TEXT_W_INVALID))
    pending = link_pending(context, uid) if context is not None else None
    if pending is not None and pending.network != net:
        pending = None  # a flow for the other network (a switch clears it; never shown here)
    if pending is not None:
        lines.append(tr(TEXT_W_LINKING))
    if note:
        lines += ["", note]
    rows: list[list[tuple[str, str]]] = []
    if enabled:
        if pending is not None:
            rows.append([(LABEL_CONTINUE, CB_LINK_CHECK)])
        else:
            rows.append([(LABEL_RENEW if linked else LABEL_LINK, CB_LINK_START)])
    if linked:
        rows.append([(LABEL_CHECK_KEY, CB_LINK_CHECK), (LABEL_UNLINK, CB_UNLINK)])
    rows.append([(LABEL_NETWORK, CB_MODE), (LABEL_HOME, CB_HOME)])
    return "\n".join(lines), arcus_ui.kb(rows)


async def render_unlink(telegram_id: int, *, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """The unlink card for the CURRENT Arcus network. The confirm button carries
    that network, so a card tapped after a mode switch unlinks nothing."""
    uid = int(telegram_id)
    back_rows = [[(LABEL_NADO_1CT, CB_NADO_REVOKE_STEPS)], [(LABEL_BACK, CB_WALLET)]]
    net: str | None = None
    try:
        net = await run_blocking_db(venue_service.get_arcus_network_mode, uid)
        row = await run_blocking_db(creds.get_credential, uid, net)
    except Exception as exc:  # policy: degrade-ok(no unlink button on an unknown state — never "no key linked")
        logger.warning("arcus unlink card: link unreadable uid=%s (%s)", uid, type(exc).__name__)
        lines = [tr(TEXT_U_TITLE, network=esc(_net_or_dash(net))), "", tr(TEXT_W_UNREADABLE)]
        if note:
            lines += ["", note]
        return "\n".join(lines), arcus_ui.kb(back_rows)
    assert net is not None
    label = arcus_ui.net_label(net)
    linked = row is not None and row.status != "unlinked"
    lines = [tr(TEXT_U_TITLE, network=esc(label)), ""]
    if linked:
        assert row is not None
        lines.append(tr(TEXT_U_BODY, address=esc(row.address), key_name=esc(row.api_wallet_name or "—")))
    else:
        lines.append(tr(TEXT_U_NONE, network=esc(label)))
    lines += ["", tr(TEXT_U_RUNNING_NOTE), tr(TEXT_U_NADO_NOTE)]
    if note:
        lines += ["", note]
    rows: list[list[tuple[str, str]]] = []
    if linked:
        rows.append([(LABEL_UNLINK, CB_UNLINK_CONFIRM_PREFIX + parse_arcus_net(net))])
    rows += back_rows
    return "\n".join(lines), arcus_ui.kb(rows)


def _mode_status(row: Any) -> str:
    if row is None or row.status == "unlinked":
        return tr(TEXT_M_STATUS_NOT_LINKED)
    if row.status == "expired":
        return tr(TEXT_M_STATUS_EXPIRED)
    if row.status == "invalid":
        return tr(TEXT_M_STATUS_INVALID)
    return tr(TEXT_M_STATUS_LINKED, address=esc(arcus_ui.addr_short(row.address)))


async def render_mode(telegram_id: int, *, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """The Arcus network card. No switch button while the current mode is
    unknown."""
    uid = int(telegram_id)
    try:
        current = await run_blocking_db(venue_service.get_arcus_network_mode, uid)
    except Exception as exc:  # policy: degrade-ok(no switch buttons on an unknown mode)
        logger.warning("arcus mode card: mode unreadable uid=%s (%s)", uid, type(exc).__name__)
        text = "\n".join([tr(TEXT_M_TITLE), "", tr(TEXT_DB_BUSY)])
        return text, arcus_ui.kb([[(LABEL_REFRESH, CB_MODE), (LABEL_HOME, CB_HOME)]])
    rows_by_net: dict[str, Any] | None
    try:
        rows_by_net = await run_blocking_db(creds.get_credentials_for_user, uid)
    except Exception as exc:  # policy: degrade-ok(each network line says "couldn't check")
        logger.warning("arcus mode card: credentials unreadable uid=%s (%s)", uid, type(exc).__name__)
        rows_by_net = None
    lines = [tr(TEXT_M_TITLE), "", tr(TEXT_M_VIEWING, network=esc(arcus_ui.net_label(current))), ""]
    for net in ARCUS_NETWORK_MODES:
        status = tr(TEXT_M_STATUS_UNKNOWN) if rows_by_net is None else _mode_status(rows_by_net.get(net))
        # ``status`` is already translated HTML with escaped values.
        lines.append(tr(TEXT_M_LINE, network=esc(arcus_ui.net_label(net)), status=status))
    lines += ["", tr(TEXT_M_BODY)]
    if note:
        lines += ["", note]
    labels = {ARCUS_NETWORK_TESTNET: LABEL_MODE_TESTNET_ARCUS, ARCUS_NETWORK_MAINNET: LABEL_MODE_MAINNET_ARCUS}
    buttons = [
        (labels[net] + (" ✅" if net == current else ""), CB_MODE_SET_PREFIX + net) for net in ARCUS_NETWORK_MODES
    ]
    return "\n".join(lines), arcus_ui.kb([buttons, [(LABEL_HOME, CB_HOME)]])


def _attestation_card(network: str) -> tuple[str, InlineKeyboardMarkup]:
    try:
        terms_url = arcus_terms_url()
    except ValueError:
        logger.warning("ARCUS_TERMS_URL is not https; showing the documented default")
        terms_url = ARCUS_TERMS_DEFAULT
    lines = [
        tr(TEXT_A_TITLE, network=esc(arcus_ui.net_label(network))),
        "",
        tr(TEXT_A_CONFIRM_HEAD),
        tr(TEXT_A_NOT_RESTRICTED),
        tr(TEXT_A_TERMS, terms_url=esc(terms_url)),
        "",
        tr(TEXT_A_NOTE_HEAD),
        tr(TEXT_A_PUBLIC),
        tr(TEXT_A_SUBACCOUNT),
        tr(TEXT_A_TRADE_ONLY),
    ]
    return "\n".join(lines), arcus_ui.kb([[(LABEL_CONFIRM, CB_LINK_ATTEST), (LABEL_CANCEL, CB_LINK_CANCEL)]])


def _address_card(pending: LinkPending) -> tuple[str, InlineKeyboardMarkup]:
    lines = [tr(TEXT_A_TITLE, network=esc(arcus_ui.net_label(pending.network))), "", tr(TEXT_AD_ASK)]
    rows: list[list[tuple[str, str]]] = []
    if pending.previous_address:
        lines.append(tr(TEXT_AD_ASK_RENEW, address=esc(pending.previous_address)))
        rows.append([(LABEL_SAME_ADDRESS, CB_LINK_SAME)])
    rows.append([(LABEL_CANCEL, CB_LINK_CANCEL)])
    return "\n".join(lines), arcus_ui.kb(rows)


def _pr_ok_line(pending: LinkPending) -> str:
    return tr(TEXT_PR_OK, address=esc(pending.address or "—"), network=esc(arcus_ui.net_label(pending.network)))


def _instructions_card(pending: LinkPending) -> tuple[str, InlineKeyboardMarkup]:
    lines = [_pr_ok_line(pending)]
    if pending.address_check is AddressCheck.ELIGIBLE_NO_ACTIVITY:
        lines.append(tr(TEXT_PR_NO_ACTIVITY))
    lines += [
        "",
        tr(TEXT_IN_TITLE),
        tr(TEXT_IN_1),
        tr(TEXT_IN_2, key_name=esc(pending.key_name or "—")),
        tr(TEXT_IN_3),
        tr(TEXT_IN_4),
        tr(TEXT_IN_5),
        "",
        tr(TEXT_IN_WARN),
    ]
    rows: list[list[tuple[Any, ...]]] = []
    try:
        rows.append([(LABEL_OPEN_API_KEYS, None, arcus_api_keys_url(pending.network))])
    except ValueError:
        # A non-https URL button would make Telegram reject the whole edit and
        # strand the user; the card goes out without it.
        logger.warning("Arcus API keys URL is not usable for %s; instructions sent without the button",
                       arcus_ui.net_label(pending.network))
    rows.append([(LABEL_CANCEL, CB_LINK_CANCEL)])
    return "\n".join(lines), arcus_ui.kb(rows)


def _checking_address_card(pending: LinkPending) -> tuple[str, InlineKeyboardMarkup]:
    text = tr(
        TEXT_AD_CHECKING,
        address=esc(pending.address or "—"),
        network=esc(arcus_ui.net_label(pending.network)),
    )
    return text, arcus_ui.kb([[(LABEL_CANCEL, CB_LINK_CANCEL)]])


def _cancel_only() -> InlineKeyboardMarkup:
    return arcus_ui.kb([[(LABEL_CANCEL, CB_LINK_CANCEL)]])


def _retry_markup() -> InlineKeyboardMarkup:
    return arcus_ui.kb([[(LABEL_CHECK_AGAIN, CB_LINK_CHECK), (LABEL_CANCEL, CB_LINK_CANCEL)]])


def _terminal_markup() -> InlineKeyboardMarkup:
    return arcus_ui.kb([[(LABEL_START_OVER, CB_LINK_START), (LABEL_HOME, CB_HOME)]])


def _step_card(pending: LinkPending) -> tuple[str, InlineKeyboardMarkup]:
    """The card for the step the flow is at (re-rendered on [Continue linking])."""
    if pending.step == "attest":
        return _attestation_card(pending.network)
    if pending.step == "address":
        return _address_card(pending)
    if pending.step == "address_check":
        return _checking_address_card(pending)
    return _instructions_card(pending)


# --- link results ---------------------------------------------------------------------------

def _not_allowed_text(uid: int, network: str) -> str:
    if arcus_enabled_for(uid) and _is_mainnet(network) and not arcus_mainnet_enabled():
        return TEXT_MAINNET_CLOSED
    return TEXT_ARCUS_NOT_ALLOWED


def _outcome_lines(uid: int, out: LinkOutcome) -> list[str]:
    result = out.result
    if result in (LinkResult.LINKED, LinkResult.LINKED_NO_ACTIVITY):
        row = out.row
        address = row.address if row is not None else (out.address or "—")
        valid_until = row.valid_until_ms if row is not None else out.valid_until_ms
        until = esc(arcus_ui.fmt_until(valid_until))
        if out.renewed:
            lines = [tr(TEXT_R_RENEWED, address=esc(address), until=until)]
            prev = out.previous
            if (
                prev is not None
                and row is not None
                and prev.api_public_key != row.api_public_key
                and prev.api_wallet_name is not None
                and prev.api_wallet_name.lower() != (row.api_wallet_name or "").lower()
            ):
                # Only when the NAMES differ: re-creating a key under the same name
                # revokes the old one (docs changelog), so "still works" would be false.
                lines.append(tr(TEXT_R_OLD_KEY, key_name=esc(prev.api_wallet_name)))
        else:
            lines = [tr(TEXT_R_LINKED, address=esc(address), until=until)]
        if result is LinkResult.LINKED_NO_ACTIVITY:
            lines.append(tr(TEXT_R_NO_ACTIVITY))
        if valid_until:
            left = arcus_ui.days_left(valid_until, _now_ms())
            if 0 < left < _SHORT_VALIDITY_DAYS:
                lines.append(tr(TEXT_R_SHORT_VALIDITY, days=esc(str(left))))
        return lines
    if result is LinkResult.WALLET_KEY_REFUSED:
        return [tr(TEXT_R_WALLET_KEY), tr(TEXT_R_WALLET_KEY_2)]
    if result is LinkResult.KEY_NOT_FOUND:
        return [tr(TEXT_R_NOT_FOUND, network=esc(_net_or_dash(out.network)), address=esc(out.address or "—"))]
    if result is LinkResult.NOT_WHITELISTED:
        lines = [tr(TEXT_PR_NOT_ELIGIBLE)]
        if _is_mainnet(out.network):
            lines.append(tr(TEXT_PR_NOT_ELIGIBLE_MAINNET))
        return lines
    if result is LinkResult.NOT_ALLOWED:
        return [tr(_not_allowed_text(uid, out.network))]
    return [tr(_SIMPLE_RESULT_TEXT.get(result, TEXT_BUSY))]


def _outcome_markup(kind: str, in_arcus: bool) -> InlineKeyboardMarkup:
    if not in_arcus:
        # A user who pasted from the Nado view: the gate would deny ax:* there.
        return arcus_ui.kb([[(LABEL_VENUE, CB_VENUE_VIEW)]])
    if kind == _LINKED:
        return arcus_ui.kb([[(LABEL_WALLET, CB_WALLET), (LABEL_HOME, CB_HOME)]])
    if kind == _RETRY:
        return _retry_markup()
    if kind == _PASTE:
        return _cancel_only()
    return _terminal_markup()


def _apply_result(context: Any, uid: int, pending: LinkPending, kind: str) -> None:
    """Move the flow after a result (03 §14.1). No await: the caller has just
    re-read ``pending`` and checked its generation."""
    if kind in (_LINKED, _TERMINAL):
        clear_link_pending(context, uid)
    elif kind == _RETRY:
        set_link_pending(context, replace(pending, step="verifying"))
    else:
        set_link_pending(context, replace(pending, step="key"))


async def _render_outcome(context: Any, uid: int, out: LinkOutcome, target: _Target, kind: str) -> None:
    in_arcus = await _viewing_arcus(uid)
    text = "\n".join(_outcome_lines(uid, out))
    await _report(context, target, text, _outcome_markup(kind, in_arcus))


async def _verify_body(context: Any, uid: int, pending: LinkPending, target: _Target) -> None:
    """Verify the stashed key and report. Runs inside a background task."""
    try:
        out = await asyncio.wait_for(
            link_service.verify_and_store(user_id=uid, pending=pending), _VERIFY_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        out = LinkOutcome(result=LinkResult.BUSY, network=pending.network, address=pending.address)
    except Exception as exc:  # policy: degrade-ok(shown as busy with [Check again]; the stash is kept)
        logger.warning("arcus verify failed uid=%s (%s)", uid, type(exc).__name__)
        out = LinkOutcome(result=LinkResult.BUSY, network=pending.network, address=pending.address)
    if out.result is LinkResult.SUPERSEDED:
        return
    current = link_pending(context, uid)
    if current is None or current.generation != pending.generation:
        return
    kind = _RESULT_KIND[out.result]
    _apply_result(context, uid, current, kind)
    logger.info("arcus link result uid=%s net=%s -> %s", uid, pending.network, out.result.value)
    await _render_outcome(context, uid, out, target, kind)


async def _verify_and_report(
    context: Any, uid: int, pending: LinkPending, target: _Target, *, announce: bool = False,
) -> None:
    """``announce``: first show "✅ address can use Arcus · Checking your key"
    (the precheck passed with a key already pasted) — from THIS task, so that
    edit can never land after the result."""
    try:
        lang = await _task_language(uid)
        with language_context(lang):
            if announce:
                text = "\n".join([_pr_ok_line(pending), "", tr(TEXT_K_CHECKING)])
                await _report(context, target, text, _cancel_only())
            await _verify_body(context, uid, pending, target)
    except asyncio.CancelledError:
        return
    except Exception as exc:  # policy: degrade-ok(background link task; the user gets [Check again])
        logger.warning("arcus verify task failed uid=%s (%s)", uid, type(exc).__name__)


async def _process_paste(context: Any, uid: int, pending: LinkPending, text: str | None, target: _Target) -> None:
    """Seal + stash the pasted key (the only place its plaintext is handed on),
    then verify it — or wait for the address check still running."""
    try:
        try:
            intake = await link_service.intake_key(user_id=uid, pending=pending, pasted_text=text or "")
        finally:
            text = None  # best effort: drop this frame's reference to the plaintext
        lang = await _task_language(uid)
        with language_context(lang):
            await _after_intake(context, uid, pending, intake, target)
    except asyncio.CancelledError:
        return
    except Exception as exc:  # policy: degrade-ok(background link task; logged by type, never the text)
        logger.warning("arcus paste task failed uid=%s (%s)", uid, type(exc).__name__)


async def _after_intake(
    context: Any, uid: int, pending: LinkPending, intake: KeyIntake, target: _Target,
) -> None:
    if intake.status == "superseded":
        return  # the flow moved on while sealing: like a generation mismatch
    current = link_pending(context, uid)
    if current is None or current.generation != pending.generation:
        return
    status = intake.status
    if status == "stashed":
        if current.step == "address_check":
            # The precheck task starts the verification when the address passes.
            await _report(context, target, tr(TEXT_K_WAIT_ADDRESS), _cancel_only())
        elif current.step in ("key", "verifying"):
            # The precheck may have finished while intake ran (and found no stash).
            current = replace(current, step="verifying")
            set_link_pending(context, current)
            await _verify_body(context, uid, current, target)
        return
    # Not stashed: this key is not usable; the step stays (paste another key).
    in_arcus = await _viewing_arcus(uid)
    if status == "pem":
        await _report(context, target, tr(TEXT_R_PEM), _outcome_markup(_PASTE, in_arcus))
        return
    if status == "wallet_key":
        result = LinkResult.WALLET_KEY_REFUSED
    elif status == "error":
        result = LinkResult.STORE_FAILED
    else:  # invalid / no_address
        result = LinkResult.INVALID_KEY
    out = LinkOutcome(result=result, network=pending.network, address=pending.address)
    logger.info("arcus key intake uid=%s net=%s -> %s", uid, pending.network, status)
    text = "\n".join(_outcome_lines(uid, out))
    await _report(context, target, text, _outcome_markup(_RESULT_KIND[result], in_arcus))


# --- address precheck -----------------------------------------------------------------------

async def _submit_address(
    uid: int, context: Any, pending: LinkPending, address: str, *, message: Any = None, query: Any = None,
) -> None:
    """A new address (typed or [Same address]): new generation, "Checking…",
    then the precheck in the background. Returns at once (the per-user lock is
    released; the precheck may take up to three reads)."""
    network = pending.network
    generation = link_service.begin_generation(uid, network)
    _cancel_tasks(uid, network)
    current = replace(
        pending,
        step="address_check",
        address=address.strip().lower(),
        key_name=None,
        address_check=None,
        address_checked_mono=None,
        has_activity=None,
        generation=generation,
        expires_mono=_ttl_expiry(),
    )
    set_link_pending(context, current)
    text, markup = _checking_address_card(current)
    if query is not None:
        await _show(query, uid, text, markup)
        chat_id = _chat_id(query, uid)
        target = _Target(getattr(query, "message", None), chat_id, arcus_ui.current_seq(chat_id))
    else:
        sent = await arcus_ui.reply_card(message, text, markup)
        target = _Target(sent, int(getattr(message, "chat_id", None) or uid), None)
    _start_task(uid, network, "precheck", _precheck_and_report(context, uid, current, target), target)


async def _precheck_and_report(context: Any, uid: int, pending: LinkPending, target: _Target) -> None:
    try:
        lang = await _task_language(uid)
        with language_context(lang):
            await _precheck_body(context, uid, pending, target)
    except asyncio.CancelledError:
        return
    except Exception as exc:  # policy: degrade-ok(background link task; the user gets [Check again])
        logger.warning("arcus precheck task failed uid=%s (%s)", uid, type(exc).__name__)


async def _precheck_body(context: Any, uid: int, pending: LinkPending, target: _Target) -> None:
    network, address = pending.network, pending.address or ""
    pre = None
    try:
        pre = await asyncio.wait_for(
            link_service.precheck_address(network, address, user_id=uid), _PRECHECK_TIMEOUT_S
        )
        check = pre.check
    except asyncio.TimeoutError:
        check = AddressCheck.BUSY
    except Exception as exc:  # policy: degrade-ok(an unknown answer is BUSY, never "not eligible")
        logger.warning("arcus precheck failed uid=%s (%s)", uid, type(exc).__name__)
        check = AddressCheck.BUSY
    current = link_pending(context, uid)
    if current is None or current.generation != pending.generation:
        return
    # No await between the re-read above and the pending update below.
    if check in _ELIGIBLE and pre is not None:
        name = link_service.new_key_name(pre.existing_names)
        current = replace(
            current,
            step="key",
            key_name=name,
            address_check=check,
            address_checked_mono=pre.checked_mono,
            has_activity=pre.has_activity,
            expires_mono=_ttl_expiry(),
        )
        if link_service.has_stash(uid, network, current.generation):
            # The key was pasted during the address check: verify it now. No await
            # between the step change and the task start: a paste task still running
            # in the "verify" slot is cancelled before it can see "verifying" and
            # verify the same stash a second time.
            current = replace(current, step="verifying")
            set_link_pending(context, current)
            _start_task(
                uid, network, "verify",
                _verify_and_report(context, uid, current, target, announce=True), target,
            )
            return
        set_link_pending(context, current)
        await _report(context, target, *_instructions_card(current))
        return
    if check in (AddressCheck.BUSY, AddressCheck.DB_UNAVAILABLE):
        # The step stays address_check; [Check again] re-runs the precheck.
        busy = TEXT_DB_BUSY if check is AddressCheck.DB_UNAVAILABLE else TEXT_BUSY
        await _report(context, target, tr(busy), _retry_markup())
        return
    # Terminal refusal: the flow ends; nothing was stored.
    clear_link_pending(context, uid)
    if check is not AddressCheck.INVALID:
        await _audit(uid, "arcus_link_refused", f"{network} {check.value}")
    lines = [tr(_PRECHECK_REFUSAL_TEXT.get(check, TEXT_BUSY))]
    if check is AddressCheck.NOT_WHITELISTED and _is_mainnet(network):
        lines.append(tr(TEXT_PR_NOT_ELIGIBLE_MAINNET))
    await _report(context, target, "\n".join(lines), _terminal_markup())


# --- stored-key diagnosis ([✅ Check key] with no link in progress) -------------------------

async def _diagnose_and_report(context: Any, uid: int, network: str, target: _Target) -> None:
    try:
        lang = await _task_language(uid)
        with language_context(lang):
            try:
                diagnosis = await asyncio.wait_for(link_service.diagnose_key(uid, network), _DIAGNOSE_TIMEOUT_S)
            except asyncio.TimeoutError:
                diagnosis = KeyDiagnosis(KeyVerdict.UNKNOWN, network, None, None, False, None)
            except Exception as exc:  # policy: degrade-ok(an unknown answer: "Arcus is busy", no status change)
                logger.warning("arcus diagnose failed uid=%s (%s)", uid, type(exc).__name__)
                diagnosis = KeyDiagnosis(KeyVerdict.UNKNOWN, network, None, None, False, None)
            key, values = link_service.diagnosis_text(diagnosis, after_401=False)
            note = tr(key, **_fmt(values))
            text, markup = await render_wallet(uid, context=context, note=note)
            await _report(context, target, text, markup)
    except asyncio.CancelledError:
        return
    except Exception as exc:  # policy: degrade-ok(background diagnosis; the wallet card offers Check key again)
        logger.warning("arcus diagnose task failed uid=%s (%s)", uid, type(exc).__name__)


# --- callback routes (inside with_user_serialized, after the venue re-check) ----------------

async def handle(query: Any, data: str, telegram_id: int, context: Any) -> None:
    """``ax:wallet`` / ``ax:link:*`` / ``ax:unlink*`` / ``ax:mode*``. Called by
    ``venue_handler.handle_venue_callback`` under the per-user lock once the
    venue re-check said Arcus; the tap was already acked."""
    uid = int(telegram_id)
    if data == CB_WALLET:
        await _show(query, uid, *await render_wallet(uid, context=context))
    elif data == CB_LINK_START:
        await _link_start(query, uid, context)
    elif data == CB_LINK_ATTEST:
        await _link_attest(query, uid, context)
    elif data == CB_LINK_SAME:
        await _link_same(query, uid, context)
    elif data == CB_LINK_CHECK:
        await _link_check(query, uid, context)
    elif data == CB_LINK_CANCEL:
        clear_link_pending(context, uid)
        await _show(query, uid, *await render_wallet(uid, context=context, note=tr(TEXT_R_CANCELLED)))
    elif data == CB_UNLINK:
        await _show(query, uid, *await render_unlink(uid))
    elif data.startswith(CB_UNLINK_CONFIRM_PREFIX):
        await _unlink_confirm(query, uid, context, data[len(CB_UNLINK_CONFIRM_PREFIX):])
    elif data == CB_MODE:
        await _show(query, uid, *await render_mode(uid))
    elif data.startswith(CB_MODE_SET_PREFIX):
        await _mode_set(query, uid, context, data[len(CB_MODE_SET_PREFIX):])
    # any other ax: value: acked already, nothing renders


async def _wallet_note(query: Any, uid: int, context: Any, key: str) -> None:
    await _show(query, uid, *await render_wallet(uid, context=context, note=tr(key)))


async def _link_start(query: Any, uid: int, context: Any) -> None:
    if not arcus_enabled_for(uid):
        await _wallet_note(query, uid, context, TEXT_ARCUS_NOT_ALLOWED)
        return
    try:
        network = await run_blocking_db(venue_service.get_arcus_network_mode, uid)
    except Exception as exc:  # policy: degrade-ok(nothing starts; the card says it could not check)
        logger.warning("arcus link start: mode unreadable uid=%s (%s)", uid, type(exc).__name__)
        await _wallet_note(query, uid, context, TEXT_DB_BUSY)
        return
    refusal = _link_gate_note(uid, network)
    if refusal is not None:
        await _wallet_note(query, uid, context, refusal)
        return
    clear_link_pending(context, uid)
    try:
        row = await run_blocking_db(creds.get_credential, uid, network)
    except Exception as exc:  # policy: degrade-ok(nothing starts; the card says it could not check)
        logger.warning("arcus link start: credential unreadable uid=%s (%s)", uid, type(exc).__name__)
        await _wallet_note(query, uid, context, TEXT_DB_BUSY)
        return
    renewal = row is not None and row.status in ("active", "expired")
    pending = LinkPending(
        network=network,
        step="attest",
        expires_mono=_ttl_expiry(),
        generation=link_service.begin_generation(uid, network),
        renewal=renewal,
        previous_address=row.address if renewal and row is not None else None,
    )
    set_link_pending(context, pending)
    logger.info("arcus link started uid=%s net=%s renewal=%s", uid, network, renewal)
    await _show(query, uid, *_attestation_card(network))


async def _link_attest(query: Any, uid: int, context: Any) -> None:
    pending = link_pending(context, uid)
    if pending is None or pending.step != "attest":
        await _show(query, uid, *await render_wallet(uid, context=context))
        return
    refusal = _link_gate_note(uid, pending.network)
    if refusal is not None:
        clear_link_pending(context, uid)
        await _wallet_note(query, uid, context, refusal)
        return
    attested_at = datetime.now(timezone.utc)
    await _audit(uid, "arcus_attested", f"{pending.network} {ARCUS_ATTESTATION_VERSION}")
    current = link_pending(context, uid)
    if current is None or current.generation != pending.generation:
        await _show(query, uid, *await render_wallet(uid, context=context))
        return
    current = replace(current, step="address", attested_at=attested_at, expires_mono=_ttl_expiry())
    set_link_pending(context, current)
    await _show(query, uid, *_address_card(current))


async def _link_same(query: Any, uid: int, context: Any) -> None:
    pending = link_pending(context, uid)
    if pending is None or pending.step not in _ADDRESS_STEPS or not pending.previous_address:
        await _show(query, uid, *await render_wallet(uid, context=context))
        return
    await _submit_address(uid, context, pending, pending.previous_address, query=query)


def _card_text(query: Any) -> str:
    """The tapped card's current text as HTML (PTB rebuilds it from the entities);
    escaped plain text when that is not available."""
    message = getattr(query, "message", None)
    try:
        html = getattr(message, "text_html", None)
    except Exception:  # policy: degrade-ok(fall back to the escaped plain text)
        html = None
    if isinstance(html, str) and html:
        return html
    return esc(str(getattr(message, "text", None) or ""))


async def _still_checking(
    query: Any, uid: int, network: str, kinds: tuple[str, ...], markup: InlineKeyboardMarkup,
) -> None:
    """A task is already running: point its result at THIS card and say so. No new work."""
    base = _card_text(query)
    still = tr(TEXT_K_STILL_CHECKING)
    if base.endswith(still):
        base = base[: -len(still)].rstrip()
    text = "\n\n".join(part for part in (base, still) if part)
    await _show(query, uid, text, markup)
    chat_id = _chat_id(query, uid)
    seq = arcus_ui.current_seq(chat_id)
    for kind in kinds:
        target = _TARGETS.get((uid, network, kind))
        if target is not None and _task_running(uid, network, kind):
            target.message, target.chat_id, target.seq = getattr(query, "message", None), chat_id, seq


async def _link_check(query: Any, uid: int, context: Any) -> None:
    """[🔄 Check again] / [🔗 Continue linking] / [✅ Check key] (03 §9.7)."""
    pending = link_pending(context, uid)
    chat_id = _chat_id(query, uid)
    if pending is not None:
        network = pending.network
        running = tuple(k for k in ("precheck", "verify") if _task_running(uid, network, k))
        if running:
            await _still_checking(query, uid, network, running, _cancel_only())
            return
        if pending.step == "address_check":
            # Re-run the precheck on the same address. The generation is NOT bumped,
            # so a key pasted during the check keeps its stash.
            await _show(query, uid, *_checking_address_card(pending))
            target = _Target(getattr(query, "message", None), chat_id, arcus_ui.current_seq(chat_id))
            _start_task(uid, network, "precheck", _precheck_and_report(context, uid, pending, target), target)
            return
        if pending.step in ("key", "verifying"):
            if link_service.has_stash(uid, network, pending.generation):
                current = replace(pending, step="verifying")
                set_link_pending(context, current)
                await _show(query, uid, tr(TEXT_K_CHECKING), _cancel_only())
                target = _Target(getattr(query, "message", None), chat_id, arcus_ui.current_seq(chat_id))
                _start_task(uid, network, "verify", _verify_and_report(context, uid, current, target), target)
                return
            if pending.step == "verifying":
                set_link_pending(context, replace(pending, step="key"))
                await _show(query, uid, tr(TEXT_R_PENDING_EXPIRED), _cancel_only())
                return
        # attest / address / key without a pasted key: that step's card again.
        await _show(query, uid, *_step_card(pending))
        return
    try:
        network = await run_blocking_db(venue_service.get_arcus_network_mode, uid)
        row = await run_blocking_db(creds.get_credential, uid, network)
    except Exception as exc:  # policy: degrade-ok(the wallet card says it could not read the link)
        logger.warning("arcus check key: link unreadable uid=%s (%s)", uid, type(exc).__name__)
        await _show(query, uid, *await render_wallet(uid, context=context))
        return
    if row is None or row.status == "unlinked":
        await _show(query, uid, *await render_wallet(uid, context=context))
        return
    if _task_running(uid, network, "diagnose"):
        await _still_checking(query, uid, network, ("diagnose",), _wallet_only())
        return
    await _show(query, uid, tr(TEXT_K_CHECKING_STORED), _wallet_only())
    target = _Target(getattr(query, "message", None), chat_id, arcus_ui.current_seq(chat_id))
    _start_task(uid, network, "diagnose", _diagnose_and_report(context, uid, network, target), target)


def _wallet_only() -> InlineKeyboardMarkup:
    return arcus_ui.kb([[(LABEL_WALLET, CB_WALLET)]])


async def _unlink_confirm(query: Any, uid: int, context: Any, suffix: str) -> None:
    try:
        card_net = parse_arcus_net(suffix)
    except ValueError:
        return  # malformed: acked already, nothing happens
    try:
        network = await run_blocking_db(venue_service.get_arcus_network_mode, uid)
    except Exception as exc:  # policy: degrade-ok(nothing is unlinked; the card says it could not check)
        logger.warning("arcus unlink: mode unreadable uid=%s (%s)", uid, type(exc).__name__)
        await _show(query, uid, *await render_unlink(uid, note=tr(TEXT_DB_BUSY)))
        return
    if card_net != network:
        # A stale card (the network was switched after it was rendered): unlink nothing.
        await _show(query, uid, *await render_unlink(uid))
        return
    try:
        previous = await run_blocking_db(creds.get_credential, uid, network)
        outcome = await link_service.unlink(uid, network)
    except Exception as exc:  # policy: degrade-ok(the card says it could not check; nothing half-done is claimed)
        logger.warning("arcus unlink failed uid=%s (%s)", uid, type(exc).__name__)
        await _show(query, uid, *await render_unlink(uid, note=tr(TEXT_DB_BUSY)))
        return
    clear_link_pending(context, uid)
    if outcome == "unlinked":
        key_name = previous.api_wallet_name if previous is not None else None
        note = tr(TEXT_U_DONE, key_name=esc(key_name or "—"))
        await _show(query, uid, *await render_wallet(uid, context=context, note=note))
    elif outcome == "refused_running":
        await _show(query, uid, *await render_unlink(uid, note=tr(TEXT_U_REFUSED)))
    else:
        note = tr(TEXT_U_NONE, network=esc(arcus_ui.net_label(network)))
        await _show(query, uid, *await render_wallet(uid, context=context, note=note))


async def _mode_set(query: Any, uid: int, context: Any, suffix: str) -> None:
    try:
        target = parse_arcus_net(suffix)
    except ValueError:
        return  # "ax:mode:Mainnet" / "ax:mode:arcus_mainnet": ignored
    try:
        current = await run_blocking_db(venue_service.get_arcus_network_mode, uid)
    except Exception as exc:  # policy: degrade-ok(nothing switches; the card says it could not check)
        logger.warning("arcus mode switch: mode unreadable uid=%s (%s)", uid, type(exc).__name__)
        await _show(query, uid, *await render_mode(uid, note=tr(TEXT_DB_BUSY)))
        return
    if target == current:
        await _show(query, uid, *await render_mode(uid))
        return
    if _is_mainnet(target) and not (arcus_enabled_for(uid) and arcus_mainnet_enabled()):
        await _show(query, uid, *await render_mode(uid, note=tr(TEXT_MAINNET_CLOSED)))
        return
    if await link_service.automation_active(uid):
        # "Stop, then switch" — the same rule as Nado networks.
        await _show(query, uid, *await render_mode(uid, note=tr(TEXT_M_REFUSED_RUNNING)))
        return
    try:
        outcome = await run_blocking_db(venue_service.set_arcus_network_mode, uid, target)
    except Exception as exc:  # policy: degrade-ok(nothing changed; the card says so)
        logger.warning("arcus mode switch failed uid=%s (%s)", uid, type(exc).__name__)
        await _show(query, uid, *await render_mode(uid, note=tr(TEXT_SWITCH_FAILED)))
        return
    if outcome == "switched":
        clear_link_pending(context, uid)
        note = tr(TEXT_M_SWITCHED, network=esc(arcus_ui.net_label(target)))
        await _show(query, uid, *await render_mode(uid, note=note))
    elif outcome == "not_allowed":
        await _show(query, uid, *await render_mode(uid, note=tr(TEXT_MAINNET_CLOSED)))
    else:
        await _show(query, uid, *await render_mode(uid))


# --- Arcus-view free text (the address step) -------------------------------------------------

async def arcus_text_router(update: Any, context: Any, text: str) -> bool:
    """Free text on the Arcus view while a link flow is pending (called by the
    gate's ``_arcus_free_text`` under ``with_user_serialized``). True = consumed.
    Secret-shaped text never gets here: the gate intercepts it first."""
    uid = int(update.effective_user.id)
    pending = link_pending(context, uid)
    if pending is None:
        return False
    message = update.message
    if pending.step == "attest":
        markup = arcus_ui.kb([[(LABEL_CONFIRM, CB_LINK_ATTEST), (LABEL_CANCEL, CB_LINK_CANCEL)]])
        await arcus_ui.reply_card(message, tr(TEXT_K_ATTEST_FIRST), markup)
        return True
    address = (text or "").strip()
    if _ADDRESS_INPUT_RE.match(address):
        await _submit_address(uid, context, pending, address, message=message)
        return True
    if pending.step == "address":
        await arcus_ui.reply_card(message, tr(TEXT_AD_INVALID), _cancel_only())
        return True
    await arcus_ui.reply_card(message, tr(TEXT_K_WAIT_PASTE), _cancel_only())
    return True


# --- the Arcus-scoped secret interceptor (03 §9.8) -------------------------------------------

async def _try_delete(message: Any) -> bool:
    """Delete the message; ANY failure is logged by type and returns False (the
    reply then asks the user to delete it). A catch-all on purpose: a
    non-Telegram error must not skip the warning."""
    try:
        result = await message.delete()
    except Exception as exc:  # policy: degrade-ok(the reply tells the user to delete it themselves)
        logger.warning("arcus interceptor: delete failed (%s)", type(exc).__name__)
        return False
    return result is not False


async def arcus_secret_interceptor(update: Any, context: Any, *, shape: SecretShape, venue: str | None) -> None:
    """A secret-shaped message or EDITED message of an Arcus-scoped user (the
    gate decided the scope and stops the update afterwards, whatever happens
    here). Deletes it FIRST, then: a pending key step verifies it in the
    background; anything else gets a warning. Never raises; never logs the text."""
    message = getattr(update, "message", None)
    edited = message is None
    if message is None:
        message = getattr(update, "edited_message", None)
    user = getattr(update, "effective_user", None)
    uid = int(getattr(user, "id", 0) or 0)
    pending: LinkPending | None = None  # bound before the try: the final log line reads it
    deleted = await _try_delete(message)
    try:
        chat_id = int(getattr(message, "chat_id", None) or uid)
        bot = getattr(context, "bot", None)
        not_deleted = "" if deleted else "\n\n" + tr(TEXT_K_NOT_DELETED)
        pending = link_pending(context, uid)
        if pending is not None and pending.step in _PASTE_STEPS and shape is SecretShape.HEX_KEY:
            text = getattr(message, "text", None)
            if text is None:
                text = getattr(message, "caption", None)
            ack = await arcus_ui.send_html(bot, chat_id, tr(TEXT_K_ACK) + not_deleted, None)
            target = _Target(ack, chat_id, None)
            # One "verify" slot per (user, network): a newer paste cancels the older one.
            _start_task(uid, pending.network, "verify", _process_paste(context, uid, pending, text, target), target)
            text = None
        elif pending is not None and pending.step in _PASTE_STEPS:  # PEM / HEX_OTHER / HEX_EMBEDDED
            key = TEXT_R_PEM if shape is SecretShape.PEM else TEXT_R_INVALID
            await arcus_ui.send_html(bot, chat_id, tr(key) + not_deleted, _cancel_only())
        elif pending is not None:  # attest / address
            await arcus_ui.send_html(bot, chat_id, tr(TEXT_K_ADDRESS_FIRST) + not_deleted, None)
        else:  # the Arcus view (or an unreadable venue) with no link in progress
            warning = tr(TEXT_K_GENERIC_DELETED) + "\n" + tr(TEXT_K_GENERIC_EXPOSED) + not_deleted
            await arcus_ui.send_html(bot, chat_id, warning, None)
    except Exception as exc:  # policy: degrade-ok(message already deleted; the gate stops the update)
        logger.warning("arcus interceptor error uid=%s (%s)", uid, type(exc).__name__)
    logger.info(
        "arcus secret intercepted uid=%s shape=%s edited=%s deleted=%s step=%s view=%s",
        uid, shape.value, edited, deleted, pending.step if pending is not None else "-", venue or "?",
    )
