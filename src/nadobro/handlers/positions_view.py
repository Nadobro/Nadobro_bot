from __future__ import annotations

from decimal import Decimal
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.nadobro.handlers.orders_view import cancel_callback_for, order_kind_label, sorted_orders
from src.nadobro.utils.visual import b, divider, esc, money, pct, pnl_dot, signed_money
from src.nadobro.handlers import ui

# Per the workflow plan: Positions and Orders share a single screen with
# sub-headers so users see the full live exposure (open positions cross +
# isolated, plus open limit/trigger orders) in one place. Pagination is per
# section: ``portfolio:positions:pos:{n}`` for positions and
# ``portfolio:positions:ord:{n}`` for orders, while ``portfolio:positions``
# (no suffix) keeps both at page 0.


def render_positions_view(
    snapshot: dict[str, Any],
    page: int = 0,
    page_size: int = 6,
    *,
    pos_page: int | None = None,
    ord_page: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    network = str(snapshot.get("network") or "mainnet").upper()
    positions = sorted(
        list(snapshot.get("positions") or []),
        key=lambda p: (
            str(p.get("symbol") or p.get("product_name") or ""),
            int(p.get("product_id") or 0),
            bool(p.get("isolated")),
        ),
    )
    orders = sorted_orders(snapshot)

    if pos_page is None:
        pos_page = page
    if ord_page is None:
        ord_page = page

    pos_total_pages = max(1, (len(positions) + page_size - 1) // page_size)
    pos_page = max(0, min(pos_page, pos_total_pages - 1))
    visible_positions = positions[pos_page * page_size:(pos_page + 1) * page_size]

    ord_page_size = max(3, page_size - 2)  # leave room for cancel buttons
    ord_total_pages = max(1, (len(orders) + ord_page_size - 1) // ord_page_size)
    ord_page = max(0, min(ord_page, ord_total_pages - 1))
    visible_orders = orders[ord_page * ord_page_size:(ord_page + 1) * ord_page_size]

    marks = _mark_prices(snapshot)

    lines = [
        f"📍 <b>Positions & Orders</b> · {esc(network)}",
        divider(),
        f"<b>Open Positions</b> ({len(positions)})",
        "",
    ]
    for idx, pos in enumerate(visible_positions, start=pos_page * page_size + 1):
        symbol = str(pos.get("symbol") or pos.get("product_name") or f"ID:{pos.get('product_id')}")
        margin = "iso" if bool(pos.get("isolated")) else "cross"
        is_long = bool(pos.get("is_long", True))
        direction = "📈 long" if is_long else "📉 short"
        product_id = pos.get("product_id")
        mark_price = marks.get(int(product_id)) if product_id is not None else None
        mark_str = money(mark_price) if mark_price is not None else "—"
        est_pnl = _dec(pos.get("est_pnl"))
        upnl_pct = pos.get("upnl_pct")
        pct_part = f" ({pct(_dec(upnl_pct))})" if upnl_pct is not None else ""
        liq_val = pos.get("est_liq_price")
        leverage = pos.get("leverage")
        leverage_str = f"{_dec(leverage):g}x" if leverage is not None else "—"
        lines.extend([
            f"{idx}. {b(symbol)}  {direction} · {margin} · {leverage_str}",
            f"    Entry {money(_dec(pos.get('avg_entry_price')))} → Mark {mark_str}",
            f"    Size {abs(_dec(pos.get('amount')))} · Value {money(_dec(pos.get('notional_value')))}",
            f"    uPnL {pnl_dot(est_pnl)} {signed_money(est_pnl)}{pct_part}"
            f" · Liq {money(_dec(liq_val)) if liq_val is not None else '—'}",
            "",
        ])
    if not positions:
        lines.extend(["No open positions", ""])

    lines.append(f"<b>Open Orders</b> ({len(orders)})")
    order_action_rows: list[list[InlineKeyboardButton]] = []
    for idx, order in enumerate(visible_orders, start=ord_page * ord_page_size + 1):
        symbol = str(order.get("product_name") or order.get("product") or f"ID:{order.get('product_id')}")
        side = "buy" if str(order.get("side") or "").upper() in {"LONG", "BUY"} else "sell"
        kind = order_kind_label(order)
        lines.append(
            f"{idx}. {b(symbol)}  {side} · {esc(kind)} · "
            f"size {abs(_dec(order.get('amount') or order.get('size')))} @ "
            f"{money(_dec(order.get('price') or order.get('limit_price')))}"
        )
        order_action_rows.append(
            [InlineKeyboardButton(f"🗑 Cancel {idx}", callback_data=cancel_callback_for(order, idx - 1))]
        )
    if not orders:
        lines.append("No open orders")

    rows: list[list[InlineKeyboardButton]] = []
    pos_nav: list[InlineKeyboardButton] = []
    if pos_page > 0:
        pos_nav.append(InlineKeyboardButton(ui.pager("Positions")[0], callback_data=f"portfolio:positions:pos:{pos_page - 1}"))
    if pos_page + 1 < pos_total_pages:
        pos_nav.append(InlineKeyboardButton(ui.pager("Positions")[1], callback_data=f"portfolio:positions:pos:{pos_page + 1}"))
    if pos_nav:
        rows.append(pos_nav)

    ord_nav: list[InlineKeyboardButton] = []
    if ord_page > 0:
        ord_nav.append(InlineKeyboardButton(ui.pager("Orders")[0], callback_data=f"portfolio:positions:ord:{ord_page - 1}"))
    if ord_page + 1 < ord_total_pages:
        ord_nav.append(InlineKeyboardButton(ui.pager("Orders")[1], callback_data=f"portfolio:positions:ord:{ord_page + 1}"))
    if ord_nav:
        rows.append(ord_nav)

    rows.extend(order_action_rows)

    bulk_row: list[InlineKeyboardButton] = []
    if positions:
        bulk_row.append(InlineKeyboardButton("❌ Close All", callback_data="portfolio:close_all_confirm"))
    if orders:
        bulk_row.append(InlineKeyboardButton("🗑 Cancel All", callback_data="portfolio:cancel_all_confirm"))
    if bulk_row:
        rows.append(bulk_row)
    rows.append([InlineKeyboardButton(ui.nav_back_to("Portfolio"), callback_data="portfolio:view")])
    return "\n".join(lines)[:3500], InlineKeyboardMarkup(rows)


def _mark_prices(snapshot: dict[str, Any]) -> dict[int, Decimal]:
    """Best-effort extract of mark prices by product_id from the snapshot.

    Nado's ``calculate_account_summary`` returns per-position mark/oracle
    prices alongside the position rows. We index them by product_id so the
    Positions card can show a real number instead of the hardcoded ``—``.
    """
    out: dict[int, Decimal] = {}
    summary = snapshot.get("summary") or {}
    for bucket in (summary.get("cross_positions"), summary.get("isolated_positions")):
        for row in bucket or []:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("product_id") or 0)
            except (TypeError, ValueError):
                continue
            for key in ("mark_price", "oracle_price", "price", "fair_price"):
                value = row.get(key)
                if value not in (None, "", 0):
                    out[pid] = _dec(value)
                    break
    return out


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
