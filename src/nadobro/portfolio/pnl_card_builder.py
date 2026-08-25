"""Builds the per-user data dicts consumed by the share-card renderers.

This module is the **only** place that reads strategy/trade/referral data from
the DB for the share cards. Keeping it separate from the pure renderers means:

  * the renderers stay pure ``dict -> PNG`` functions (easy to test, no DB
    dependency)
  * Per-user scoping lives in one auditable place — every query filters by
    ``telegram_id`` so users can never see another user's stats

Card types:
  * Type A (``pnl_card_type_a``) — per-trade desk/agent + copy cards
    (:func:`build_round_trip_card_data`, :func:`build_copy_trade_card_data`)
  * Type B (``pnl_card_type_b``) — strategy-session cards
    (:func:`build_type_b_card_data`)

Inputs come from:
  * ``strategy_sessions`` (filtered by ``user_id`` + ``network``) — volume,
    realized PnL, fees, strategy, product
  * ``invite_codes`` (filtered by ``referrer_user_id`` + ``network``) — the
    user's most recent active referral ``public_code``
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from src.nadobro.db import query_one

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Number formatters — output strings the renderer can paste directly.
# ---------------------------------------------------------------------------
def _to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return Decimal("0")


# ---------------------------------------------------------------------------
# DB helpers — every query is scoped by user_id + network.
# ---------------------------------------------------------------------------
def _fetch_session(telegram_id: int, network: str, session_id: Optional[int]) -> dict:
    if session_id is not None:
        # Specific session — must belong to this user on this network.
        row = query_one(
            """
            SELECT *
            FROM strategy_sessions
            WHERE id = %s AND user_id = %s AND network = %s
            """,
            (int(session_id), int(telegram_id), network),
        )
        return dict(row or {})
    # Latest session for this user on this network. Prefer most-recently
    # completed session; fall back to any session if none have ended yet.
    row = query_one(
        """
        SELECT *
        FROM strategy_sessions
        WHERE user_id = %s AND network = %s
        ORDER BY COALESCE(stopped_at, started_at) DESC
        LIMIT 1
        """,
        (int(telegram_id), network),
    )
    return dict(row or {})


def _net_funding_usd(session: dict, network: str) -> Decimal:
    """Net funding RECEIVED (positive) on the session's perp over its lifetime,
    summed from the synced ``funding_payments_<network>`` feed (the data the
    adapter's funding endpoint produces). The indexer signs funding positive =
    *paid* by the user, so we negate to report received-positive. Scoped to the
    session's product_id and ``[started_at, stopped_at]`` window."""
    product_id = session.get("product_id")
    started_at = session.get("started_at")
    user_id = session.get("user_id")
    if product_id is None or started_at is None or user_id is None:
        return Decimal(0)
    table = "funding_payments_testnet" if str(network).lower() == "testnet" else "funding_payments_mainnet"
    try:
        row = query_one(
            f"""
            SELECT COALESCE(SUM(amount_x18), 0) AS paid_x18
            FROM {table}
            WHERE user_id = %s AND product_id = %s
              AND paid_at >= %s
              AND paid_at <= COALESCE(%s, now())
            """,
            (int(user_id), int(product_id), started_at, session.get("stopped_at")),
        )
    except Exception:  # noqa: BLE001 - funding is additive context, never fatal
        return Decimal(0)
    if not row:
        return Decimal(0)
    paid_x18 = _to_decimal(dict(row).get("paid_x18"))
    return -(paid_x18 / Decimal(10 ** 18))


def _fetch_active_referral_code(telegram_id: int, network: str) -> Optional[str]:
    """The user's most recent active referral ``public_code`` for this network.

    Returns ``None`` when the user hasn't unlocked / created a referral yet —
    the renderer omits the bottom-right line in that case.
    """
    row = query_one(
        """
        SELECT public_code
        FROM invite_codes
        WHERE code_type = 'referral'
          AND referrer_user_id = %s
          AND (network = %s OR (network IS NULL AND %s = 'testnet'))
          AND active = true
          AND revoked_at IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (int(telegram_id), network, network),
    )
    if not row:
        return None
    code = (dict(row).get("public_code") or "").strip()
    return code or None


# ---------------------------------------------------------------------------
# Type B (strategy-session) card
# ---------------------------------------------------------------------------
# Spot ticker → perp-icon key (so the KBTC/WETH volume-spot cards still show
# the right coin icon).
_ICON_ALIASES = {
    "WETH": "ETH", "KBTC": "BTC", "XBT": "BTC", "USDT0": "USDT",
    "NLP": "NADO", "WBTC": "BTC", "XAUT0": "XAUT",
}
# Real spot symbols (Volume-Bot spot) shown verbatim, never perp-qualified.
_SPOT_SYMBOLS = {"KBTC", "WETH", "USDC", "USDT", "USDT0", "XAUT", "XAUT0", "NLP", "WBTC"}


def _type_b_product(product_name: Optional[str]) -> tuple[str, str]:
    """``(icon_key, display_product)`` for the Type B session card.

    Strategy sessions store the product as a bare ticker (``BTC``), a perp
    marker (``BTC-PERP`` / ``BTC:PERP-USDC``), or — for Volume-Bot spot — a spot
    symbol/pair (``KBTC``, ``ETH-USDC``). Grid/RGrid/DGrid/Mid/DN trade perps,
    so a bare ticker means the PERP. ``MULTI`` (multi-product volume runs) shows
    ``MULTI`` with no token icon.

    ``BTC-PERP`` -> ``("BTC", "BTC:PERP-USDC")``; ``ETH`` -> ``("ETH",
    "ETH:PERP-USDC")``; ``KBTC`` -> ``("BTC", "KBTC")``; ``ETH-USDC`` ->
    ``("ETH", "ETH-USDC")``.
    """
    raw = (product_name or "").strip().upper()
    if not raw:
        return "BTC", "BTC:PERP-USDC"
    if raw == "MULTI":
        return "", "MULTI"
    base = raw.replace(":PERP-USDC", "").replace("-PERP", "").split(":")[0].split("-")[0]
    icon_key = _ICON_ALIASES.get(base, base)
    if "PERP" in raw:
        return icon_key, f"{base}:PERP-USDC"
    # A spot symbol or a spot pair (has a quote separator, no PERP) shows as-is.
    if raw in _SPOT_SYMBOLS or any(sep in raw for sep in ("-", ":", "/")):
        return icon_key, raw
    # Bare ticker on a perp strategy → the perp market.
    return icon_key, f"{base}:PERP-USDC"


def build_type_b_card_data(
    telegram_id: int,
    network: str,
    session_id: Optional[int] = None,
) -> dict:
    """Type B card data for a strategy session (badge = friendly strategy name).

    All numeric fields are derived from ``strategy_sessions`` rows owned by
    ``telegram_id`` on ``network``. The referral code comes from the user's own
    ``invite_codes`` row. If no session is found the dict still renders a valid
    card with $0 values (clear empty state instead of an error). Returned as
    raw floats — ``pnl_card_type_b.generate_type_b_card`` formats them.

    Args:
        telegram_id: Owner of the card. **Must** be the requesting user —
            callers are responsible for not accepting this from untrusted input.
        network: ``"mainnet"`` / ``"testnet"`` — scopes the session lookup.
        session_id: Optional specific session; when omitted uses the latest.
    """
    session = _fetch_session(telegram_id, network, session_id)

    volume = _to_decimal(session.get("total_volume_usd"))
    pnl = _to_decimal(session.get("realized_pnl"))
    fees = _to_decimal(session.get("total_fees_paid"))
    strategy = session.get("strategy")
    icon_key, display = _type_b_product(session.get("product_name"))

    # Delta Neutral profit IS the funding: price PnL nets ~0 across the hedged
    # legs, so fold net funding received into the displayed PnL. Scoped to DN so
    # other strategies (whose realized_pnl already reflects their economics)
    # aren't double-counted. DN-PNL-FEES fix: DN fires four taker MARKET legs
    # per cycle, so fees can equal/exceed funding — the headline must be NET of
    # fees (``realized + funding - fees``).
    if str(strategy or "").lower() in ("dn", "delta_neutral"):
        pnl = pnl + _net_funding_usd(session, network) - fees

    return {
        "strategy": str(strategy or "grid"),
        "product": display,
        "base_symbol": icon_key,
        "volume": float(volume),
        "net_fees": float(abs(fees)),
        "pnl": float(pnl),
        "referral_code": _fetch_active_referral_code(telegram_id, network) or "",
    }


def _type_a_product(product_name: Optional[str]) -> tuple[str, str, bool]:
    """``(base_symbol, display_product, is_perp)`` for the Type A card.

    ``BTC-PERP`` -> ``("BTC", "BTC:PERP-USDC", True)``. A spot pair (no PERP
    marker) returns ``is_perp=False`` so the caller can gate it out (Type A is
    perps-only for now)."""
    raw = (product_name or "").strip().upper()
    is_perp = "PERP" in raw
    base = (
        raw.replace(":PERP-USDC", "").replace("-PERP", "").split(":")[0].split("-")[0]
    )
    if not base:
        base = "BTC"
    return base, f"{base}:PERP-USDC", is_perp


def build_round_trip_card_data(
    telegram_id: int,
    network: str,
    round_trip_key: str,
) -> dict:
    """Type A card data for a History round-trip (desk/agent/manual trade).

    ``round_trip_key`` is the stable id minted by
    :func:`trade_service.compute_round_trips` (the close trade id). Returns the
    Type A contract consumed by ``pnl_card_type_a.generate_type_a_card``. A
    spot round-trip returns ``{"unsupported": "spot"}`` — Type A is perps-only
    for now.
    """
    from src.nadobro.trading.trade_service import find_round_trip

    rt = find_round_trip(int(telegram_id), network, str(round_trip_key))
    if not rt:
        return {"unsupported": "not_found"}

    product_name = rt.get("product_name") or rt.get("pair")
    base, display, is_perp = _type_a_product(product_name)
    if not is_perp:
        return {"unsupported": "spot"}

    return {
        "badge": "DESK TRADE",
        "product": display,
        "base_symbol": base,
        "side": "LONG" if str(rt.get("side") or "").lower() in ("long", "buy") else "SHORT",
        "leverage": float(rt.get("leverage") or 0.0),
        "pnl": float(_to_decimal(rt.get("realized_pnl"))),
        "entry_price": float(_to_decimal(rt.get("avg_open_price"))),
        "exit_price": float(_to_decimal(rt.get("avg_close_price"))),
        "size": float(_to_decimal(rt.get("size"))),
        "referral_code": _fetch_active_referral_code(telegram_id, network) or "",
    }


def build_copy_trade_card_data(
    telegram_id: int,
    network: str,
    position_id: int,
) -> dict:
    """Type A card data for a CLOSED copy position (badge ``COPY TRADE``).

    Whole-trade numbers even for positions the leader trimmed before fully
    closing: ``closed_size`` is the total base closed across all slices and the
    row ``pnl`` accumulates every slice (see ``reduce_copy_position`` /
    ``close_copy_position``). Exit price is not stored, but the accumulated pnl
    is the gross ``(exit-entry)*total_size*dir``, so ``exit = entry +
    pnl/(size*dir)`` recovers the size-weighted effective exit — Entry / Exit /
    Size therefore reconcile with the shown PnL over the whole trade.

    Legacy rows closed before ``closed_size`` existed have it NULL and fall back
    to the remaining ``size``; their pnl was overwritten to last-slice-only and
    can't be recovered, so those old cards stay last-slice (new closes are
    correct).
    """
    from src.nadobro.models.database import get_closed_copy_position

    pos = get_closed_copy_position(int(position_id))
    if not pos or int(pos.get("user_id") or 0) != int(telegram_id):
        return {"unsupported": "not_found"}

    base, display, _is_perp = _type_a_product(pos.get("product_name"))
    entry = float(_to_decimal(pos.get("entry_price")))
    closed_size = float(_to_decimal(pos.get("closed_size")))
    size = closed_size if closed_size > 0 else float(_to_decimal(pos.get("size")))
    pnl = float(_to_decimal(pos.get("pnl")))
    is_long = str(pos.get("side") or "").lower() in ("long", "buy")
    direction = 1.0 if is_long else -1.0
    exit_price = entry + (pnl / (size * direction)) if size > 0 else entry
    return {
        "badge": "COPY TRADE",
        "product": display,
        "base_symbol": base,
        "side": "LONG" if is_long else "SHORT",
        "leverage": float(pos.get("leverage") or 0.0),
        "pnl": pnl,
        "entry_price": entry,
        "exit_price": exit_price,
        "size": size,
        "referral_code": _fetch_active_referral_code(telegram_id, network) or "",
    }
