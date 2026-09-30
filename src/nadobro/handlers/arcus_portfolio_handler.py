"""The Arcus home (Arcus P3b = the HOME SHELL only, 03 §10).

``ax:home`` (and every DISPATCH to it: ``/start``, ``nav:main``, the Home reply
button on the Arcus view) renders :func:`render_home`: the P1 title, the Arcus
LINK STATUS read from Postgres only, and P1's Nado banner. No Arcus venue call
and no Nado client — this is a tap path. A DB error renders "couldn't read",
never "Not linked" (DENIED != EMPTY).

Portfolio numbers, the Portfolio/Status row and the strategies hub come in later
phases (P4b / P7b extend :func:`render_home` and :func:`handle`).
"""
from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.nadobro.core.async_utils import run_blocking_db
from src.nadobro.handlers import arcus_ui
from src.nadobro.handlers import venue_handler
from src.nadobro.handlers.arcus_ui import (
    LABEL_NETWORK,
    LABEL_WALLET,
    TEXT_H_EXPIRED,
    TEXT_H_INVALID,
    TEXT_H_LINKED,
    TEXT_H_LINKING,
    TEXT_H_NOT_LINKED,
    TEXT_H_STRATEGIES_SOON,
    TEXT_H_UNREADABLE,
)
from src.nadobro.handlers.arcus_wallet_handler import link_pending
from src.nadobro.handlers.venue_handler import edit_html, tr
from src.nadobro.users import arcus_credentials
from src.nadobro.users import arcus_link_service as link_service
from src.nadobro.utils.venue_capabilities import AX_MODE, AX_WALLET
from src.nadobro.utils.venue_scope import ARCUS_NETWORK_TESTNET
from src.nadobro.utils.visual import esc

logger = logging.getLogger(__name__)

AX_REFRESH = "ax:refresh"


def _now_ms() -> int:
    """Wall clock, epoch ms — the link service's seam (one clock for the flow)."""
    return link_service._now_ms()


def _link_lines(row: Any, *, now_ms: int) -> list[str]:
    if row is None or row.status == "unlinked":
        return [tr(TEXT_H_NOT_LINKED)]
    until = row.valid_until_ms
    elapsed = bool(until) and until <= now_ms
    if row.status == "expired" or (row.status == "active" and elapsed):
        return [tr(TEXT_H_EXPIRED)]
    if row.status == "invalid":
        return [tr(TEXT_H_INVALID)]
    lines = [
        tr(
            TEXT_H_LINKED,
            address=esc(arcus_ui.addr_short(row.address)),
            until=esc(arcus_ui.fmt_until(row.valid_until_ms)),
        )
    ]
    soon = arcus_ui.key_soon_line(row.valid_until_ms, now_ms)
    if soon:
        lines.append(soon)
    return lines


def home_kb(items: list[tuple[str, dict[str, str]]], failed: bool) -> InlineKeyboardMarkup:
    """[👛 Arcus wallet][🌐 Network] above P1's rows: [🔁 Venue][❓ Help] and —
    while the Nado banner shows — the NEVER_GATE Nado stop entries (build_decisions
    #1: every Nado stop path stays reachable from the Arcus view)."""
    rows: list[list[Any]] = [[
        InlineKeyboardButton(LABEL_WALLET, callback_data=AX_WALLET),
        InlineKeyboardButton(LABEL_NETWORK, callback_data=AX_MODE),
    ]]
    rows += [list(row) for row in venue_handler.arcus_home_kb(items, failed).inline_keyboard]
    return InlineKeyboardMarkup(rows)


async def render_home(telegram_id: int, *, context: Any = None) -> tuple[str, InlineKeyboardMarkup]:
    """The Arcus home: title, link status (DB only), the Nado banner. Never a
    venue call."""
    uid = int(telegram_id)
    try:
        # Looked up on the module at call time (tests patch venue_handler's copy).
        arcus_network, items, failed = await run_blocking_db(venue_handler.nado_automation_snapshot, uid)
    except Exception:  # policy: degrade-ok(banner says it could not check — never "nothing running")
        logger.warning("arcus home: automation snapshot failed uid=%s", uid)
        arcus_network, items, failed = ARCUS_NETWORK_TESTNET, [], True
    now_ms = _now_ms()
    try:
        row = await run_blocking_db(arcus_credentials.get_credential, uid, arcus_network)
        lines = _link_lines(row, now_ms=now_ms)
    except Exception as exc:  # policy: degrade-ok(the home says it could not read the link — never "Not linked")
        logger.warning("arcus home: credential unreadable uid=%s (%s)", uid, type(exc).__name__)
        lines = [tr(TEXT_H_UNREADABLE)]
    pending = link_pending(context, uid) if context is not None else None
    if pending is not None:
        lines.append(tr(TEXT_H_LINKING))
    lines.append(tr(TEXT_H_STRATEGIES_SOON))
    text = venue_handler.arcus_home_text(arcus_network, items, failed, link_lines=lines)
    return text, home_kb(items, failed)


async def handle(query: Any, data: str, telegram_id: int, context: Any) -> None:
    """``ax:refresh`` re-renders the home in place. ``ax:home`` never reaches
    this function (P1's screen branch renders it through ``render_arcus_target``,
    which bumps the interaction sequence exactly once). Anything else: nothing."""
    if data != AX_REFRESH:
        return
    text, markup = await render_home(telegram_id, context=context)
    arcus_ui.bump_seq(query, telegram_id)
    await edit_html(query, text, markup)
