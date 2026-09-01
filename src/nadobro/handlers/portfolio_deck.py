from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.nadobro.handlers.orders_view import order_kind_label
from src.nadobro.core.feature_flags import portfolio_sync_enabled, portfolio_sync_interval_seconds
from src.nadobro.venue.nado_sync import sync_user
from src.nadobro.users.user_service import get_user
from src.nadobro.utils.visual import b, divider, esc, money, pct, pnl_dot, signed_money, stale_banner, time_ago


_VALID_WINDOWS = ("24h", "7d", "30d", "all")
_DEFAULT_WINDOW = "24h"


def _normalize_window(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in _VALID_WINDOWS:
        return token
    return _DEFAULT_WINDOW


def _window_label(window: str) -> str:
    return {"24h": "24h", "7d": "7d", "30d": "30d", "all": "All"}[window]


def portfolio_deck_kb(
    has_positions: bool = False,
    has_orders: bool = False,
    *,
    window: str = _DEFAULT_WINDOW,
) -> InlineKeyboardMarkup:
    window = _normalize_window(window)
    # Single shared 24h/7d/30d/All toggle — flips both Volume and PnL windows.
    toggle_row = [
        InlineKeyboardButton(
            ("• " + _window_label(w) + " •") if w == window else _window_label(w),
            callback_data=f"portfolio:view:{w}",
        )
        for w in _VALID_WINDOWS
    ]
    rows = [
        toggle_row,
        [
            InlineKeyboardButton("🚀 Positions", callback_data="portfolio:positions"),
            InlineKeyboardButton("📈 Performance & History", callback_data="portfolio:performance"),
        ],
        [InlineKeyboardButton("📊 Refresh portfolio", callback_data=f"portfolio:refresh:{window}")],
    ]
    if has_positions:
        rows.append([InlineKeyboardButton("❌ Close All", callback_data="portfolio:close_all_confirm")])
    elif not has_orders:
        # Empty book: hand the user a next step instead of a dead end.
        rows.append([
            InlineKeyboardButton("🤖 Trade Console", callback_data="card:trade:start"),
            InlineKeyboardButton("🧠 Strategy Lab", callback_data="nav:strategy_hub"),
        ])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="nav:main")])
    return InlineKeyboardMarkup(rows)


def empty_portfolio_snapshot(user_id: int, network: str | None = None) -> dict[str, Any]:
    """Instant tap placeholder: a real deck, not a loading dead-end.

    Cold cache after a restart used to leave the user on "Loading portfolio…"
    until a full venue+indexer sync finished (minutes). Render this immediately
    and let the background refresh edit the message in place.
    """
    return {
        "user_id": int(user_id),
        "network": str(network or "mainnet"),
        "positions": [],
        "open_orders": [],
        "stats": {},
        "equity": {},
        "stale": True,
        "monotonic_ts": 0.0,
    }


async def snapshot_for_user(
    user_id: int,
    *,
    force: bool = False,
    max_age_ms: int | None = 2000,
    reason: str | None = None,
) -> dict[str, Any]:
    """Return a portfolio snapshot, refreshing from Nado when stale.

    Every Portfolio render path passes ``max_age_ms=2000`` so Positions and
    Overview never show data older than ~2s when the user is actively
    navigating. Background polling keeps the cache warm between taps.

    ``reason="ui"`` is the tap-path refresh: skip the indexer heavy pull so
    the first paint is positions/orders/equity, not 200 fills + funding.
    """
    from src.nadobro.core.async_utils import run_blocking_db
    from src.nadobro.venue.nado_sync import mark_user_active

    mark_user_active(int(user_id))
    user = await run_blocking_db(get_user, user_id)
    network = user.network_mode.value if user else "mainnet"
    return await sync_user(
        user_id,
        network=network,
        reason=reason or ("refresh" if force else "cold_render"),
        force=force,
        max_age_ms=max_age_ms,
    )


def render_portfolio_deck(
    snapshot: dict[str, Any],
    *,
    window: str = _DEFAULT_WINDOW,
    refreshing: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the Portfolio overview (Telegram HTML parse mode).

    ``refreshing`` swaps the sync line for a live "Refreshing…" hint — the
    cached deck is shown instantly and the message is edited in place when
    the background sync lands.
    """
    window = _normalize_window(window)
    network = str(snapshot.get("network") or "mainnet").upper()
    positions = list(snapshot.get("positions") or [])
    orders = list(snapshot.get("open_orders") or [])
    stats = snapshot.get("stats") or {}
    equity = snapshot.get("equity") or {}
    last_sync = _as_dt(snapshot.get("last_sync"))
    threshold = (portfolio_sync_interval_seconds() * 2) if portfolio_sync_enabled() else 300
    stale = stale_banner(last_sync, threshold) if last_sync else "⚠️ Never synced"

    total_upnl = sum((_dec(p.get("est_pnl")) for p in positions if p.get("est_pnl") is not None), Decimal("0"))
    total_balance = _dec(equity.get("total")) if equity else Decimal("0")
    spot_eq = _dec(equity.get("spot")) if equity else Decimal("0")
    cross_eq = _dec(equity.get("cross")) if equity else Decimal("0")
    iso_eq = _dec(equity.get("isolated")) if equity else Decimal("0")
    if not total_balance and (spot_eq or cross_eq or iso_eq):
        total_balance = spot_eq + cross_eq + iso_eq

    vol_window = _window_value(stats, "volume_windows", window)
    pnl_window = _window_value(stats, "pnl_windows", window)
    fees_window = _window_value(stats, "fees_windows", window)
    funding_window = _window_value(stats, "funding_windows", window)

    if refreshing:
        sync_line = f"🔄 Refreshing · showing {time_ago(last_sync) if last_sync else 'cached'} data"
    elif snapshot.get("stale") and snapshot.get("error"):
        # The last refresh ATTEMPT failed (gateway circuit, venue error).
        # Don't claim "Live" — say what the user is actually looking at.
        sync_line = f"⚠️ Sync issue · showing {time_ago(last_sync) if last_sync else 'cached'} data"
    elif stale:
        sync_line = stale
    else:
        sync_line = f"🟢 Live · synced {time_ago(last_sync)}"

    # Funding sign convention: positive = paid (a cost), negative = received.
    if funding_window > 0:
        funding_line = f"Funding   -{money(funding_window)} (paid)"
    elif funding_window < 0:
        funding_line = f"Funding   +{money(abs(funding_window))} (received)"
    else:
        funding_line = "Funding   $0.00"

    lines = [
        f"📊 <b>Portfolio</b> · {esc(network)} · {_window_label(window)}",
        sync_line,
        divider(),
        f"<b>Total Balance</b>  {money(total_balance)}",
        f"Spot {money(spot_eq)} · Cross {money(cross_eq)} · Iso {money(iso_eq)}",
        "",
        f"<b>Unrealized PnL</b>  {pnl_dot(total_upnl)} {signed_money(total_upnl)}",
        f"{len(positions)} open position{'' if len(positions) == 1 else 's'} · "
        f"{len(orders)} open order{'' if len(orders) == 1 else 's'}",
        divider(),
        f"<b>Activity</b> · {_window_label(window)}",
    ]
    # Volume, split the way the product needs it: total Nado turnover (every
    # fill on the account, including trades made on the Nado UI) vs the share
    # routed through Nadobro, each broken into perp and spot.
    analytics = stats.get("analytics") or {}
    nado_vol = ((analytics.get("nado_volume") or {}).get(window)) or {}
    bot_vol = ((analytics.get("nadobro_volume") or {}).get(window)) or {}
    if nado_vol:
        perp_usd = _dec(nado_vol.get("perp_usd"))
        spot_usd = _dec(nado_vol.get("spot_usd"))
        lines.append(f"Nado Vol     {money(_dec(nado_vol.get('total_usd')))}")
        lines.append(f"  perp {money(perp_usd)} · spot {money(spot_usd)}")
        lines.append(f"Nadobro Vol  {money(_dec(bot_vol.get('total_usd')))}")
    else:
        lines.append(f"Volume    {money(vol_window)}")
    lines.extend([
        f"Realized  {signed_money(pnl_window)}",
        f"Fees      -{money(abs(fees_window))}",
        funding_line,
        divider(),
        "<b>Top Positions</b>",
    ])
    for pos in sorted(positions, key=lambda p: abs(_dec(p.get("est_pnl"))), reverse=True)[:5]:
        est_pnl = _dec(pos.get("est_pnl"))
        direction = "long" if bool(pos.get("is_long", True)) else "short"
        margin = "iso" if bool(pos.get("isolated")) else "cross"
        upnl_pct = pos.get("upnl_pct")
        pct_part = f" ({pct(_dec(upnl_pct))})" if upnl_pct is not None else ""
        lines.append(
            f"{pnl_dot(est_pnl)} {b(_pos_symbol(pos))} · {direction} · {margin}  "
            f"{signed_money(est_pnl)}{pct_part}"
        )
    if not positions:
        lines.append("No open positions")
    lines.extend(["", "<b>Open Orders</b>"])
    for order in orders[:3]:
        side = "buy" if str(order.get("side") or "").upper() in {"LONG", "BUY"} else "sell"
        lines.append(
            f"• {b(_order_symbol(order))} · {side} · {esc(order_kind_label(order))}  "
            f"{money(_dec(order.get('price') or order.get('limit_price')))}"
        )
    if len(orders) > 3:
        lines.append(f"… and {len(orders) - 3} more")
    if not orders:
        lines.append("No open orders")
    return "\n".join(lines)[:3500], portfolio_deck_kb(
        bool(positions), bool(orders), window=window
    )


def _window_value(stats: dict[str, Any], key: str, window: str) -> Decimal:
    bucket = stats.get(key) or {}
    if isinstance(bucket, dict) and window in bucket:
        return _dec(bucket.get(window))
    # Backward compat: older snapshots only carried a single total.
    legacy_map = {
        "volume_windows": "total_volume",
        "pnl_windows": "total_pnl",
        "fees_windows": "total_fees",
        "funding_windows": "total_funding",
    }
    fallback_key = legacy_map.get(key)
    if fallback_key:
        return _dec(stats.get(fallback_key))
    return Decimal("0")


def render_loading() -> str:
    return "⏳ Loading portfolio…"


def render_close_all_confirm() -> tuple[str, InlineKeyboardMarkup]:
    return (
        "❌ Close all open positions?\n\nThis will submit reduce-only market closes, then refresh Portfolio from Nado.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("◀ Keep positions", callback_data="portfolio:view")],
            [InlineKeyboardButton("❌ Yes, close all", callback_data="portfolio:close_all_yes")],
        ]),
    )


def _win_rate(stats: dict[str, Any]) -> str:
    return f"{_dec(stats.get('win_rate')):.1f}%"


def _pos_symbol(pos: dict[str, Any] | None) -> str:
    if not pos:
        return "—"
    return str(pos.get("symbol") or pos.get("product_name") or f"ID:{pos.get('product_id')}")


def _order_symbol(order: dict[str, Any]) -> str:
    return str(order.get("product_name") or order.get("product") or f"ID:{order.get('product_id')}")


def _margin(pos: dict[str, Any]) -> str:
    return "🔒" if bool(pos.get("isolated")) else "⚖️"


def _dec(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
