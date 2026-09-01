from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.nadobro.utils.visual import b, divider, esc, money, pnl_dot, signed_money
from src.nadobro.handlers import ui


PAGE_SIZE = 5


def render_history_view(
    snapshot: dict[str, Any],
    page: int = 0,
    page_size: int = PAGE_SIZE,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the History tab — round-trips for non-strategy trades only.

    - Strategy fills are excluded (they live in the Performance tab).
    - Manual round-trips come from the venue-synced ``positions`` table
      (:func:`database.get_manual_closed_round_trips`) — the authoritative record
      with correct leverage. The old FIFO reconstruction over the (holey) fill
      stream drifted and fabricated phantom trades / 1x leverage (2026-08-29 fix).
    - Each round-trip gets its own ``Share PnL`` button (``trip_key`` = position id).
    """
    from src.nadobro.models.database import get_manual_closed_round_trips

    network = str(snapshot.get("network") or "mainnet")
    user_id = int(snapshot.get("user_id") or 0)
    try:
        round_trips = get_manual_closed_round_trips(user_id, network, limit=200) if user_id else []
    except Exception:
        round_trips = []
    # Closed copy positions are single trades too — surface them in History
    # (DISPLAY ONLY). They are never fed into compute_round_trips (source='copy'
    # + a session id exclude them), so there is no double-count with the manual
    # stream and no aggregate total to distort — copy stays tracked solely in
    # copy accounting.
    try:
        from src.nadobro.models.database import get_closed_copy_positions

        closed_copies = get_closed_copy_positions(user_id, network, limit=200) if user_id else []
    except Exception:
        closed_copies = []

    # Unified chronological entries: (sort_ts, lines[], share_callback_data).
    entries: list[tuple[str, list[str], str]] = []
    for trip in round_trips:
        pair = _resolve_pair_name(
            trip.get("product_id"), str(trip.get("pair") or ""), network,
        )
        is_long = str(trip.get("side") or "").lower() in ("long", "buy")
        side = "📈 long" if is_long else "📉 short"
        size = abs(_dec(trip.get("size")))
        open_px = _dec(trip.get("avg_entry_price"))
        close_px = _dec(trip.get("close_price"))
        pnl = _dec(trip.get("close_realized_pnl"))
        meta = _meta(trip.get("metadata"))
        fees = _dec(meta.get("close_fees"))
        funding = _dec(meta.get("close_funding"))
        volume = size * (open_px + close_px)     # round-trip notional (both legs)
        hold = _hold_duration(trip.get("opened_at"), trip.get("closed_at"))
        margin = "iso" if bool(trip.get("isolated")) else "cross"
        closed_at = _fmt_ts(trip.get("closed_at"))
        entries.append((
            str(trip.get("closed_at") or ""),
            [
                f"{b(pair)}  {side} · {margin}" + (f" · {closed_at}" if closed_at else ""),
                f"    {_fmt_size(size)} @ {money(open_px)} → {money(close_px)} · held {hold}",
                f"    Realized {pnl_dot(pnl)} {signed_money(pnl)} · Fees -{money(abs(fees))} · "
                f"Funding {signed_money(-funding)} · Vol {money(volume)}",
            ],
            f"portfolio:share_pnl:rt:{trip.get('id')}",
        ))
    for pos in closed_copies:
        pair = _resolve_pair_name(
            pos.get("product_id"),
            str(pos.get("product_name") or ""),
            network,
        )
        is_long = str(pos.get("side") or "").lower() in ("long", "buy")
        side = "📈 long" if is_long else "📉 short"
        # Whole-trade base closed across all slices (partial + final); falls back
        # to the remaining `size` for legacy rows without closed_size. Keeps the
        # exit reconstruction in sync with the accumulated pnl. See
        # build_copy_trade_card_data.
        closed_size = _dec(pos.get("closed_size"))
        size = closed_size if closed_size > 0 else _dec(pos.get("size"))
        entry = _dec(pos.get("entry_price"))
        pnl = _dec(pos.get("pnl"))
        direction = Decimal(1) if is_long else Decimal(-1)
        exit_px = entry + (pnl / (size * direction)) if size > 0 else entry
        closed_at = _fmt_ts(pos.get("closed_at"))
        entries.append((
            str(pos.get("closed_at") or ""),
            [
                f"{b(pair)}  {side} · copy" + (f" · {closed_at}" if closed_at else ""),
                f"    {_fmt_size(abs(size))} @ {money(entry)} → {money(exit_px)}",
                f"    Realized {pnl_dot(pnl)} {signed_money(pnl)}",
            ],
            f"portfolio:share_pnl:copy:{pos.get('id')}",
        ))

    entries.sort(key=lambda e: e[0], reverse=True)
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    visible = entries[page * page_size:(page + 1) * page_size]

    lines = [
        f"📜 <b>Trade History</b> · {esc(network.upper())} · page {page + 1}/{total_pages}",
        "Single trades (desk + copy). Strategy sessions live under Performance",
        divider(),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for idx, (_ts, body_lines, share_cb) in enumerate(visible, start=page * page_size + 1):
        lines.append(f"{idx}. {body_lines[0]}")
        lines.extend(body_lines[1:])
        lines.append("")
        rows.append([InlineKeyboardButton(f"📤 Share PnL · #{idx}", callback_data=share_cb)])
    if not visible:
        lines.append("No trades yet")

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(ui.NAV_NEWER, callback_data=f"portfolio:history:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(ui.NAV_OLDER, callback_data=f"portfolio:history:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("📈 Performance", callback_data="portfolio:performance")])
    rows.append([InlineKeyboardButton(ui.nav_back_to("Portfolio"), callback_data="portfolio:view")])
    return "\n".join(lines)[:3500], InlineKeyboardMarkup(rows)


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
    return stored or (f"ID:{pid}" if pid else "?")


def _fmt_size(size: Decimal) -> str:
    """Trim float→Decimal noise (0.01535000000000003 → 0.01535): sizes come
    from float DB columns, so digits past 8 decimals are representation
    artifacts, not venue precision."""
    try:
        q = size.quantize(Decimal("0.00000001"))
        text = f"{q.normalize():f}"
        return text if text not in ("-0", "") else "0"
    except Exception:
        return str(size)


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _meta(value: Any) -> dict:
    """positions.metadata comes back as a dict (jsonb adapter) or a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        import json
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _fmt_ts(value: Any) -> str:
    """Compact close timestamp (UTC) so trades are identifiable at a glance."""
    if not isinstance(value, datetime):
        return ""
    return f"{value:%b} {value.day}, {value:%H:%M}"


def _hold_duration(open_ts: Any, close_ts: Any) -> str:
    if not (isinstance(open_ts, datetime) and isinstance(close_ts, datetime)):
        return "—"
    seconds = int((close_ts - open_ts).total_seconds())
    if seconds < 60:
        return f"{max(0, seconds)}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600:02d}h"
