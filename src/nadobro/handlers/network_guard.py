"""Bind every preview/confirm to the Nado network it was built on.

PREVIEW-NETWORK-BIND: a preview the user reviewed on TESTNET (product list,
size, leverage, price, settings, fee quote, confirm dialog) must never execute
on MAINNET, or the other way round. Every executor resolves the network from
``users.network_mode`` at confirm time, so a preview left on screen across an
Execution Mode switch used to confirm as a REAL order on the new network.

The rule, applied at every confirm site:

* A stateful preview (``context.user_data`` / ``bot_state``) stores the network
  it was built on and is checked against the user's CURRENT network right
  before it executes.
* A stateless confirm button carries the network it was rendered on as a
  trailing ``callback_data`` segment (``bind_cb`` / ``unbind_cb``), because
  nothing server-side survives for it to be checked against.
* The check fails CLOSED: a missing or unknown stamp, or an unknown current
  network, is a mismatch. The preview is discarded and the user is told
  "Nothing was sent".

Clearing previews on a successful switch (``state_reset.
clear_state_after_network_switch``) is hygiene only. This confirm-time check is
the safety control, so it holds even when the switch was served by another
process whose ``user_data`` this one cannot see.

Stop / kill paths are deliberately NOT bound: refusing a Stop is worse than the
bug this prevents.
"""
from __future__ import annotations

import logging

from telegram.error import BadRequest

from src.nadobro.core.async_utils import run_blocking_db
from src.nadobro.i18n import get_active_language, localize_markup, localize_text
from src.nadobro.users.user_service import get_user

logger = logging.getLogger(__name__)

NETWORKS = ("testnet", "mainnet")

# Telegram rejects callback_data longer than 64 bytes.
_CALLBACK_DATA_MAX_BYTES = 64

# Plain text (no parse mode): nothing here needs escaping, and a refusal must
# render even when the surrounding card used Markdown that no longer parses.
STALE_TRADE_TEXT = (
    "⚠️ This trade was prepared on {built}; you're now on {current}. "
    "Nothing was sent. Start again."
)
STALE_ACTION_TEXT = (
    "⚠️ This was prepared on {built}; you're now on {current}. "
    "Nothing was sent. Start again."
)
STALE_UNKNOWN_TEXT = "⚠️ This confirmation is out of date. Nothing was sent. Start again."


def normalize_network(value) -> str | None:
    """``"testnet"`` / ``"mainnet"``, or None for anything else (None, "", junk)."""
    net = str(value or "").strip().lower()
    return net if net in NETWORKS else None


def network_of(user) -> str | None:
    """The network of a ``UserRow`` (``network_mode`` enum), or None."""
    if user is None:
        return None
    mode = getattr(user, "network_mode", None)
    return normalize_network(getattr(mode, "value", mode))


async def active_network(telegram_id: int) -> str | None:
    """The user's CURRENT network, read on the DB pool (never on the loop).

    None when it cannot be determined; callers treat that as a mismatch."""
    try:
        user = await run_blocking_db(get_user, int(telegram_id))
    except Exception:
        logger.warning(
            "network_guard: could not read the active network user=%s; failing closed",
            telegram_id,
            exc_info=True,
        )
        return None
    return network_of(user)


def same_network(built, current) -> bool:
    """True only when both are valid networks and equal. Fails closed."""
    built_net = normalize_network(built)
    return built_net is not None and built_net == normalize_network(current)


def bind_cb(data: str, network: str) -> str:
    """``data`` tagged with the network its button was rendered on."""
    net = normalize_network(network)
    if net is None:
        raise ValueError(f"cannot bind callback {data!r} to network {network!r}")
    bound = f"{data}:{net}"
    if len(bound.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
        raise ValueError(f"bound callback_data exceeds {_CALLBACK_DATA_MAX_BYTES} bytes: {bound!r}")
    return bound


def unbind_cb(data: str) -> tuple[str, str | None]:
    """Split a trailing network tag off ``data``: ``(untagged, network|None)``.

    Only an exact trailing ``:testnet`` / ``:mainnet`` segment is a tag, so an
    untagged callback comes back unchanged with None (a legacy button)."""
    raw = str(data or "")
    base, sep, tail = raw.rpartition(":")
    if sep and base and tail in NETWORKS:
        return base, tail
    return raw, None


def stale_preview_text(built, current, *, notice: str = "action") -> str:
    """Localized "prepared on X, now on Y, nothing was sent" notice (plain text).

    ``notice="trade"`` words it for an order preview. When either network is
    unknown (an unstamped preview, an untagged legacy button) the notice cannot
    name them and says the confirmation is out of date instead."""
    lang = get_active_language()
    built_net = normalize_network(built)
    current_net = normalize_network(current)
    if built_net is None or current_net is None:
        return localize_text(STALE_UNKNOWN_TEXT, lang)
    template = STALE_TRADE_TEXT if notice == "trade" else STALE_ACTION_TEXT
    fmt = {"built": built_net.upper(), "current": current_net.upper()}
    try:
        return localize_text(template, lang).format(**fmt)
    except (KeyError, IndexError, ValueError):
        logger.warning("network_guard: bad translation for lang=%s; using English", lang)
        return template.format(**fmt)


def _log_refusal(kind: str, built, current, telegram_id) -> None:
    logger.info(
        "network_bound_preview_refused kind=%s built=%s current=%s user=%s",
        kind,
        normalize_network(built) or "unknown",
        normalize_network(current) or "unknown",
        telegram_id,
    )


async def refuse_query(
    query, *, kind: str, built, current, telegram_id, reply_markup=None, notice: str = "action",
) -> None:
    """Replace a stale inline card with the refusal notice. Executes nothing.

    ``kind`` names the preview in the log; ``notice`` picks the wording
    (``"trade"`` for an order preview, ``"action"`` for everything else)."""
    _log_refusal(kind, built, current, telegram_id)
    text = stale_preview_text(built, current, notice=notice)
    markup = localize_markup(reply_markup, get_active_language()) if reply_markup is not None else None
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return
        # A media card (no text to edit) or a deleted message: say it in a new
        # message instead, so the user is never left without the notice.
        message = getattr(query, "message", None)
        if message is None:
            raise
        await message.reply_text(text, reply_markup=markup)


async def refuse_message(
    message, *, kind: str, built, current, telegram_id, reply_markup=None, notice: str = "action",
) -> None:
    """Answer a stale typed confirmation with the refusal notice. Executes nothing."""
    _log_refusal(kind, built, current, telegram_id)
    text = stale_preview_text(built, current, notice=notice)
    markup = localize_markup(reply_markup, get_active_language()) if reply_markup is not None else None
    await message.reply_text(text, reply_markup=markup)
