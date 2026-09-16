"""Per-user trading analytics — PURE aggregation over the venue fill ledger.

The portfolio card used to build Volume/Fees/Funding from
``client.get_matches(limit=200)`` — a transient, last-200, in-memory API list
that falls back to an empty prior list on a light sync — while Realized PnL was
overwritten from the FULL ``trades_<network>`` history. Two universes on one
card, which is how a user saw ``Volume $0.00`` next to ``Realized +$16.73``, and
why the "All" window was silently capped at the last 200 fills.

This module is the single aggregation point. Feed it the COMPLETE fill ledger
(``database.get_analytics_fills``) and it returns every figure the card shows,
over one consistent set of rows:

* **Nado volume**    — every fill on the account, including trades the user made
  on the Nado UI. This is "total volume as displayed on Nado".
* **Nadobro volume** — fills whose ORDER carried our builder id (decoded from
  the venue's order appendix, ``builder_id``); for rows synced before that
  column existed, ``via_nadobro`` / a strategy session / a bot source.
* **Perp vs spot**   — split per window, classified per fill.
* **Realized PnL**   — from the VENUE'S position windows (``venue_pnl``, see
  ``database.get_venue_pnl_windows``), GROSS of fees. The venue reports no
  per-fill PnL and the old fill replay fabricated numbers over a holey
  ledger (2026-09-16: -$161 shown for a +$10.50 day), so without venue
  windows the figure is reported as UNKNOWN (``None``), never guessed.

Pure: no DB, no network. ``now`` is injectable so windows are testable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from src.nadobro.quant.portfolio_calculator import (
    ZERO,
    _decimal_from_possible_x18,
    _row_time,
    funding_payment_amount,
)

WINDOW_SECONDS: dict[str, int] = {
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}
WINDOWS: tuple[str, ...] = ("24h", "7d", "30d", "all")

# Spot symbols traded on Nado's spot book. Everything with a PERP marker is a
# perp; these are the known spot instruments/quote assets.
_SPOT_SYMBOLS = frozenset({
    "KBTC", "WETH", "USDC", "USDT", "USDT0", "XAUT", "XAUT0", "NLP", "WBTC",
})


def is_perp_fill(row: Mapping[str, Any]) -> bool:
    """Classify a fill as perp (True) or spot (False).

    Nado names perps ``BTC-PERP`` / ``BTC:PERP-USDC``; the spot book trades
    bare instruments (``KBTC``, ``WETH``) and quote assets. A stored
    ``is_perp`` wins when present so historical classification cannot drift
    with the catalog.
    """
    explicit = row.get("is_perp")
    if isinstance(explicit, bool):
        return explicit
    name = str(row.get("product_name") or "").strip().upper()
    if "PERP" in name:
        return True
    if not name:
        # Unnamed fill: perps dominate the venue, but never guess for a
        # product id that is a known spot quote.
        return True
    base = name.replace(":PERP-USDC", "").split(":")[0].split("-")[0]
    if name in _SPOT_SYMBOLS or base in _SPOT_SYMBOLS:
        return False
    return True


def _fill_notional(row: Mapping[str, Any]) -> Decimal:
    """USD notional of one fill, from the venue quote (never size*limit price)."""
    quote = _decimal_from_possible_x18(row, "quote_filled_x18", "quote_filled")
    if quote != ZERO:
        return abs(quote)
    # Recorder-only rows carry human columns instead of the venue x18 quote.
    size = row.get("fill_size") if row.get("fill_size") not in (None, "") else row.get("size")
    price = row.get("fill_price") if row.get("fill_price") not in (None, "") else row.get("price")
    try:
        return abs(Decimal(str(size or 0)) * Decimal(str(price or 0)))
    except Exception:
        return ZERO


def _fill_fee(row: Mapping[str, Any]) -> Decimal:
    """Total fee for a fill. Prefers the venue ``fee_x18``; the recorder stores
    ``fill_fee == fees == fee + builder``, so those must never be summed."""
    if row.get("fee_x18") is not None:
        return abs(_decimal_from_possible_x18(row, "fee_x18", "fee"))
    for key in ("fill_fee", "fees"):
        val = row.get(key)
        if val not in (None, ""):
            try:
                return abs(Decimal(str(val)))
            except Exception:
                continue
    return ZERO


_BOT_SOURCES = frozenset({"strategy", "copy", "dn", "vol", "vol_stop", "desk", "bro", "agent"})


def our_builder_id(network: str | None = None) -> int | None:
    """Nadobro's builder id for ``network`` (mainnet only; testnet bypasses
    builder routing). ``None`` when not configured."""
    try:
        from src.nadobro.config import get_nado_builder_routing_config

        builder_id, _rate = get_nado_builder_routing_config(network)
        return int(builder_id) or None
    except Exception:
        return None


def is_nadobro_fill(row: Mapping[str, Any], builder_id: int | None = None) -> bool:
    """Was this fill routed through Nadobro (our builder code)?

    The venue-authoritative signal is the builder id decoded from the order
    appendix (``builder_id``): when both it and our configured id are known, it
    decides alone. Rows synced before that column existed fall back to the
    bot's own attribution — ``via_nadobro`` (order registered by the bot), a
    strategy session id, or a bot source. A plain ``manual`` row with none of
    those is a Nado-UI trade.
    """
    raw = row.get("builder_id")
    if raw is not None and builder_id:
        try:
            return int(raw) == int(builder_id)
        except (TypeError, ValueError):
            pass
    if row.get("via_nadobro"):
        return True
    if row.get("strategy_session_id") is not None:
        return True
    return str(row.get("source") or "").strip().lower() in _BOT_SOURCES


def _empty_bucket() -> dict[str, Decimal | int]:
    return {"perp_usd": ZERO, "spot_usd": ZERO, "total_usd": ZERO, "fills": 0}


def aggregate_user_analytics(
    fills: Iterable[Mapping[str, Any]] | None,
    funding_payments: Iterable[Mapping[str, Any]] | None = None,
    *,
    now: datetime | None = None,
    venue_pnl: Mapping[str, Any] | None = None,
    builder_id: int | None = None,
) -> dict[str, Any]:
    """Aggregate a user's COMPLETE fill ledger into the analytics payload.

    ``fills`` must be venue-confirmed, deduped rows (one per ``submission_idx``)
    carrying ``filled_at``/``created_at``, the venue x18 columns, ``product_name``
    and ``via_nadobro``.

    Returns::

        {
          "nado_volume":    {window: {perp_usd, spot_usd, total_usd, fills}},
          "nadobro_volume": {window: {...}},   # subset routed through the bot
          "fees":           {window: Decimal},
          "funding":        {window: Decimal},  # paid-positive (a cost)
          "realized_pnl":   {window: Decimal | None},  # venue windows; None = unknown
          "realized_source": "venue" | "pending",
          "wins"/"losses":  {window: int},
        }

    ``venue_pnl`` is ``database.get_venue_pnl_windows`` output; ``builder_id``
    is our configured builder id (``our_builder_id``).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    rows = list(fills or [])
    nado = {w: _empty_bucket() for w in WINDOWS}
    nadobro = {w: _empty_bucket() for w in WINDOWS}
    fees = {w: ZERO for w in WINDOWS}
    funding = {w: ZERO for w in WINDOWS}

    for row in rows:
        notional = _fill_notional(row)
        fee = _fill_fee(row)
        perp = is_perp_fill(row)
        via_bot = is_nadobro_fill(row, builder_id)
        ts = _row_time(row)
        age = None
        if ts is not None:
            age = max(0, int((now - ts.astimezone(timezone.utc)).total_seconds()))

        for window in WINDOWS:
            if window != "all":
                if age is None or age > WINDOW_SECONDS[window]:
                    continue
            for bucket, applies in ((nado, True), (nadobro, via_bot)):
                if not applies:
                    continue
                slot = bucket[window]
                slot["total_usd"] += notional
                slot["perp_usd" if perp else "spot_usd"] += notional
                slot["fills"] = int(slot["fills"]) + 1
            fees[window] += fee

    for payment in funding_payments or []:
        # Already paid-positive (see funding_payment_label: >0 == "paid"), which
        # is exactly the convention the portfolio deck renders. Do NOT negate.
        amount = funding_payment_amount(payment)
        ts = _row_time(payment)
        age = None
        if ts is not None:
            age = max(0, int((now - ts.astimezone(timezone.utc)).total_seconds()))
        for window in WINDOWS:
            if window != "all" and (age is None or age > WINDOW_SECONDS[window]):
                continue
            funding[window] += amount

    # Realized PnL from the venue's position windows — never a replay guess.
    if venue_pnl and venue_pnl.get("pnl_windows"):
        pw = venue_pnl["pnl_windows"]
        realized = {w: Decimal(str(pw.get(w, 0) or 0)) for w in WINDOWS}
        wins = dict(venue_pnl.get("wins_windows") or {})
        losses = dict(venue_pnl.get("losses_windows") or {})
        source = "venue"
    else:
        realized = {w: None for w in WINDOWS}
        wins, losses = {}, {}
        source = "pending"

    return {
        "nado_volume": nado,
        "nadobro_volume": nadobro,
        "fees": fees,
        "funding": funding,
        "realized_pnl": realized,
        "realized_source": source,
        "wins": wins,
        "losses": losses,
    }
