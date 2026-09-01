import html as _html
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

DIVIDER = "──────────────────"


def _d(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or "0"))


def divider() -> str:
    return DIVIDER


def esc(value) -> str:
    """Escape a dynamic value for Telegram HTML parse mode."""
    return _html.escape(str(value), quote=False)


def b(text) -> str:
    """Bold a (dynamic, escaped) value for Telegram HTML parse mode."""
    return f"<b>{esc(text)}</b>"


def header(emoji: str, label: str) -> str:
    # Telegram HTML parse mode (portfolio views are sent with
    # parse_mode=HTML; the old Markdown ``*...*`` rendered as literal
    # asterisks because these screens never set a parse mode).
    return f"<b>{emoji} {esc(label)}</b>"


def kv(label: str, value: str) -> str:
    return f"{label} {value}"


def pnl_dot(value: Decimal) -> str:
    return "🟢" if _d(value) >= 0 else "🔴"


def _quantize(value: Decimal, decimals: int) -> Decimal:
    quantum = Decimal(1).scaleb(-max(0, int(decimals)))
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def qty(value, max_decimals: int = 8) -> str:
    """Format a base/token quantity as a plain, complete decimal — never
    exponent notation, never a 28-digit division tail.

    A size derived from ``quote / price`` is a Decimal with full context
    precision (e.g. ``0.0006024096385542168674698795181``); ``:g`` on it prints
    every digit (the "6^4 blaa blaa" the user saw on the Desk card). Round to
    ``max_decimals`` and strip trailing zeros so ``0.01535000000000003`` → ``0.01535``
    and ``0.7`` stays ``0.7``."""
    d = _d(value)
    try:
        q = d.quantize(Decimal(1).scaleb(-max(0, int(max_decimals))), rounding=ROUND_HALF_UP)
        # normalize() can yield exponent form for integers (7 -> 7E+0); format
        # with :f to force plain notation, then trim the fractional zeros.
        text = f"{q:f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    except Exception:
        return str(d)


def price(value, ccy: str = "USD") -> str:
    """Format a price with thousands separators and NO exponent — showing the
    complete figure (``$61,988.84``), not ``.4g``'s ``6.199e+04`` which both
    goes exponential AND drops the cents. Sub-dollar prices keep more decimals."""
    d = _d(value)
    a = abs(d)
    decimals = 2 if a >= 1 else (6 if a > 0 else 2)
    return money(d, ccy, decimals=decimals)


def signed(amount: Decimal, decimals: int = 2) -> str:
    value = _quantize(_d(amount), decimals)
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):,.{max(0, int(decimals))}f}"


def signed_money(amount: Decimal, decimals: int = 2) -> str:
    """Signed dollar amount: +$12.30 / -$19.19 (PnL, funding, fees deltas)."""
    value = _quantize(_d(amount), decimals)
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.{max(0, int(decimals))}f}"


def pct(value: Decimal) -> str:
    return f"{signed(_d(value), 2)}%"


def position_badges(isolated: bool, is_long: bool) -> str:
    margin = "🔒 ISO" if isolated else "⚖️ CROSS"
    direction = "📈 LONG" if is_long else "📉 SHORT"
    return f"{margin} {direction}"


def money(value: Decimal, ccy: str = "USDT", decimals: int = 2) -> str:
    amount = _quantize(_d(value), decimals)
    prefix = "$" if ccy.upper() in {"USD", "USDT", "USDC"} else ""
    suffix = "" if prefix else f" {ccy.upper()}"
    return f"{prefix}{amount:,.{max(0, int(decimals))}f}{suffix}"


def time_ago(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def stale_banner(last_sync: datetime, threshold_s: int) -> Optional[str]:
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    age_s = (datetime.now(timezone.utc) - last_sync.astimezone(timezone.utc)).total_seconds()
    if age_s <= int(threshold_s):
        return None
    return f"⚠ Stale · last sync {time_ago(last_sync)}"


def freshness_line(
    last_sync: Optional[datetime],
    *,
    threshold_s: int,
    refreshing: bool = False,
    degraded: bool = False,
) -> str:
    """The one-line freshness declaration for a data card backed by a sync clock.

    The freshness contract (Phase 3): a card never presents a number as live
    when it is cached or degraded. This is the shared implementation of the
    four states ``render_portfolio_deck`` pioneered, lifted here (a leaf, beside
    its only dependencies ``time_ago``/``stale_banner``, with no config or
    feature-flag import) so any renderer — MarkdownV2 or HTML — can reach it.

    Returns PLAIN text (no markup); MarkdownV2 callers escape it as usual. The
    caller computes ``threshold_s`` from its own feature flags so this stays a
    pure leaf.

    - ``refreshing`` — a cached view is shown while a background sync lands.
    - ``degraded`` — the last refresh ATTEMPT failed; don't claim "Live", say
      what the user is actually looking at.
    - otherwise: stale (past threshold) → a stale banner; fresh → live + age.

    The glyphs and wording are byte-for-byte what the deck shipped, because the
    deck is only partially test-pinned and not in the formatter snapshot; the
    ⚠️ in the degraded/never states carries U+FE0F, the bare ⚠ in the stale
    banner does not — both preserved deliberately.
    """
    if refreshing:
        return f"🔄 Refreshing · showing {time_ago(last_sync) if last_sync else 'cached'} data"
    if degraded:
        return f"⚠️ Sync issue · showing {time_ago(last_sync) if last_sync else 'cached'} data"
    if last_sync is None:
        return "⚠️ Never synced"
    stale = stale_banner(last_sync, threshold_s)
    if stale:
        return stale
    return f"🟢 Live · synced {time_ago(last_sync)}"
