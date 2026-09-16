"""History tab — every trade on the account, from the venue's own ledger.

A "trade" is a venue POSITION WINDOW (archive ``positions``): one open->close
cycle on a product with the venue's volume-weighted entry/exit, fees and
realized PnL — for EVERY source (strategy sessions, copy, desk, and trades made
on the Nado app). Windows are synced into ``venue_positions`` by the portfolio
sync and attributed to the strategy session that OPENED them.

Why not the bot's own reconstruction: the old tab paired fills FIFO / listed
the bot's ``positions`` rows, which drifted (a Sep-2026 row showed a BTC entry
at $105k that never traded) and hid strategy trades. The venue record is the
truth; this tab only renders it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.nadobro.utils.visual import b, divider, esc, money, pnl_dot, signed_money
from src.nadobro.handlers import ui

PAGE_SIZE = 5
_FETCH_LIMIT = 200


def render_history_view(
    snapshot: dict[str, Any],
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the History tab: all venue position windows, newest first."""
    from src.nadobro.models.database import get_venue_positions

    network = str(snapshot.get("network") or "mainnet")
    user_id = int(snapshot.get("user_id") or 0)
    try:
        rows = get_venue_positions(user_id, network, limit=_FETCH_LIMIT) if user_id else []
    except Exception:
        rows = []

    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    visible = rows[page * page_size:(page + 1) * page_size]

    lines = [
        f"📜 <b>Trade History</b> · {esc(network.upper())} · page {page + 1}/{total_pages}",
        "Every trade on your account, from Nado's own records",
        divider(),
    ]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for idx, row in enumerate(visible, start=page * page_size + 1):
        body = window_lines(row, network)
        lines.append(f"{idx}. {body[0]}")
        lines.extend(body[1:])
        lines.append("")
        share = window_share_callback(row)
        if share:
            kb_rows.append([InlineKeyboardButton(f"📤 Share PnL · #{idx}", callback_data=share)])
    if not visible:
        # F-06: the first screen a new user lands on must not be a dead end.
        lines.append("No trades yet.")
        lines.append("Your trades land here once you've opened and closed a position.")
        kb_rows.append([InlineKeyboardButton("🤖 Make your first trade", callback_data="card:trade:start")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(ui.NAV_NEWER, callback_data=f"portfolio:history:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(ui.NAV_OLDER, callback_data=f"portfolio:history:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("📈 Performance", callback_data="portfolio:performance")])
    kb_rows.append([InlineKeyboardButton(ui.nav_back_to("Portfolio"), callback_data="portfolio:view")])
    return "\n".join(lines)[:3500], InlineKeyboardMarkup(kb_rows)


def window_lines(row: dict[str, Any], network: str) -> list[str]:
    """Three display lines for one venue position window (shared with the
    per-session trades view)."""
    pair = _resolve_pair_name(row.get("product_id"), str(row.get("product_name") or ""), network)
    is_long = row.get("is_long")
    side = "📈 long" if is_long else ("📉 short" if is_long is not None else "trade")
    margin = "iso" if bool(row.get("isolated")) else "cross"
    is_open = bool(row.get("is_open"))
    entry = _dec(row.get("avg_entry_price"))
    exit_px = _dec(row.get("avg_exit_price"))
    size = _dec(row.get("total_close_amount")) if not is_open else _dec(row.get("amount"))
    if size <= 0:
        size = _dec(row.get("max_amount"))
    pnl = _dec(row.get("realized_pnl"))
    fees = _dec(row.get("open_fee")) + _dec(row.get("close_fee"))
    when = _fmt_ts(row.get("update_ts")) if not is_open else "open"
    tag = _source_tag(row)
    head = f"{b(pair)}  {side} · {margin} · {tag}" + (f" · {when}" if when else "")
    if is_open:
        detail = f"    {_fmt_size(size)} @ {money(entry)} · OPEN · held {_hold_duration(row.get('open_ts'), None)}"
    else:
        detail = (
            f"    {_fmt_size(size)} @ {money(entry)} → {money(exit_px)} · "
            f"held {_hold_duration(row.get('open_ts'), row.get('update_ts'))}"
        )
    result = f"    Realized {pnl_dot(pnl)} {signed_money(pnl)} · Fees -{money(abs(fees))}"
    if _dec(row.get("liquidated_amount")) > 0:
        result += " · ⚠ liquidated"
    return [head, detail, result]


def window_share_callback(row: dict[str, Any]) -> str | None:
    """Share-card callback for a CLOSED window (an open one has no exit yet)."""
    if bool(row.get("is_open")) or row.get("id") is None:
        return None
    return f"portfolio:share_pnl:vp:{int(row['id'])}"


def _source_tag(row: dict[str, Any]) -> str:
    strategy = str(row.get("session_strategy") or "").strip().lower()
    sid = row.get("strategy_session_id")
    if strategy and sid:
        return f"{esc(strategy)} #{int(sid)}"
    if sid:
        return f"session #{int(sid)}"
    source = str(row.get("source") or "").strip().lower()
    if source in ("", "manual", "default"):
        return "manual"
    return esc(source)


def _resolve_pair_name(product_id: Any, stored: str, network: str) -> str:
    """Resolve a display pair name from the product id, falling back to the
    stored name — never surface a raw ``ID:0`` / ``ID:5`` to the user."""
    stored = (stored or "").strip()
    if stored and not stored.startswith("ID:"):
        return stored
    try:
        pid = int(product_id) if product_id is not None else 0
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        try:
            from src.nadobro.config import get_product_name

            name = get_product_name(pid, network=network)
            if name and not str(name).startswith("ID:"):
                return name
        except Exception:
            pass
    return stored or "—"


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _fmt_size(value: Decimal) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _fmt_ts(value: Any) -> str:
    dt = _as_dt(value)
    return dt.strftime("%b %d %H:%M") if dt else ""


def _hold_duration(opened: Any, closed: Any) -> str:
    start = _as_dt(opened)
    end = _as_dt(closed) or datetime.now(timezone.utc)
    if not start:
        return "—"
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
