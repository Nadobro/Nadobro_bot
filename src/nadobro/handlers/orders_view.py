from __future__ import annotations

from decimal import Decimal
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.nadobro.utils.visual import b, divider, esc, money
from src.nadobro.handlers import ui
from src.nadobro.handlers.network_guard import bind_cb


def cancel_callback_for(order, fallback_index: int, *, network: str) -> str:
    """Cancel callback that identifies the order by DIGEST, not list position.

    A positional index re-resolved against a fresh snapshot can point at a
    DIFFERENT order if the list shifted (a fill or external cancel) between
    render and tap — and then we cancel the wrong order. The digest prefix
    (16 hex chars, unique per user in practice) survives list reordering;
    the numeric form remains only for orders that carry no digest and for
    buttons rendered before this upgrade.

    Both forms are bound to ``network``, the network the order list was read
    from (PREVIEW-NETWORK-BIND): an index re-resolved on the OTHER network
    would cancel an unrelated order there.
    """
    digest = str(order.get("digest") or order.get("order_digest") or "")
    short = digest.lower().removeprefix("0x")[:16]
    if short:
        return bind_cb(f"portfolio:cancel_order:d:{short}", network)
    return bind_cb(f"portfolio:cancel_order:{fallback_index}", network)


def order_kind_label(order: dict[str, Any]) -> str:
    kind = str(order.get("type") or order.get("order_type") or "LIMIT").upper()
    if bool(order.get("is_trigger")):
        kind = f"⚡ {kind}"
    return kind


def sorted_orders(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the same stable order used by the UI and callback indices."""
    return sorted(
        list(snapshot.get("open_orders") or []),
        key=lambda o: (str(o.get("created_at") or ""), str(o.get("digest") or o.get("order_digest") or "")),
        reverse=True,
    )


def render_orders_view(snapshot: dict[str, Any], page: int = 0, page_size: int = 6) -> tuple[str, InlineKeyboardMarkup]:
    snapshot_network = str(snapshot.get("network") or "mainnet").lower()
    network = snapshot_network.upper()
    orders = sorted_orders(snapshot)
    total_pages = max(1, (len(orders) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    visible = orders[page * page_size:(page + 1) * page_size]

    lines = [f"📋 <b>Open Orders</b> ({len(orders)}) · {esc(network)}", divider()]
    rows = []
    for idx, order in enumerate(visible, start=page * page_size + 1):
        symbol = str(order.get("product_name") or order.get("product") or f"ID:{order.get('product_id')}")
        side = "📈" if str(order.get("side") or "").upper() in {"LONG", "BUY"} else "📉"
        kind = order_kind_label(order)
        lines.extend([
            f"{idx}. {b(symbol)}  {side} · {esc(kind)}",
            f"    size {abs(_dec(order.get('amount') or order.get('size')))} @ "
            f"{money(_dec(order.get('price') or order.get('limit_price')))}"
            f" · {esc(str(order.get('created_at') or '—'))}",
            "",
        ])
        rows.append([InlineKeyboardButton(
            f"🗑 Cancel {idx}", callback_data=cancel_callback_for(order, idx - 1, network=snapshot_network),
        )])
    if not visible:
        lines.append("No open orders")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(ui.NAV_NEWER, callback_data=f"portfolio:orders:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(ui.NAV_OLDER, callback_data=f"portfolio:orders:{page + 1}"))
    if nav:
        rows.insert(0, nav)
    if orders:
        rows.append([InlineKeyboardButton("🗑 Cancel All", callback_data="portfolio:cancel_all_confirm")])
    rows.append([InlineKeyboardButton(ui.nav_back_to("Portfolio"), callback_data="portfolio:view")])
    return "\n".join(lines)[:3500], InlineKeyboardMarkup(rows)


def render_cancel_all_confirm(*, network: str) -> tuple[str, InlineKeyboardMarkup]:
    """"Yes, cancel all" is bound to the network this confirm was rendered on."""
    return (
        "🗑 Cancel all open orders?\n\nThis will cancel known open plain orders, then refresh Portfolio from Nado.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Keep orders", callback_data="portfolio:positions")],
            [InlineKeyboardButton("🗑 Yes, cancel all", callback_data=bind_cb("portfolio:cancel_all_yes", network))],
        ]),
    )


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))
