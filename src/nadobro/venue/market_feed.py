"""Shared mark-price feed per network — one REST/WS source, many readers.

Phase 4: strategy cycles and alerts previously each called
``get_all_market_prices`` independently. This singleton caches one
snapshot per network with a short TTL so 1000 users share one price
fetch instead of N.
"""
from __future__ import annotations

import asyncio
import logging

from src.nadobro.utils.env import env_float
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_TTL = env_float("NADO_MARKET_FEED_TTL_SECONDS", 3.0)
_lock = asyncio.Lock()
_cache: dict[str, dict[str, Any]] = {}
_ts: dict[str, float] = {}
_fetcher: Optional[Callable[[], Any]] = None


def bind_fetcher(fetcher: Callable[[], Any]) -> None:
    """Bind a callable that returns ``{product: {mid, bid, ask}}``."""
    global _fetcher
    _fetcher = fetcher


def update_from_ws(network: str, prices: dict[str, Any]) -> None:
    """Push WS-derived prices into the cache."""
    net = str(network or "mainnet").lower()
    if not prices:
        return
    _cache[net] = dict(prices)
    _ts[net] = time.monotonic()


async def get_prices(network: str = "mainnet", *, force_refresh: bool = False) -> dict[str, Any]:
    net = str(network or "mainnet").lower()
    async with _lock:
        now = time.monotonic()
        if not force_refresh and net in _cache and (now - _ts.get(net, 0)) < _TTL:
            return dict(_cache[net])
    if _fetcher is None:
        return dict(_cache.get(net, {}))
    try:
        from src.nadobro.core.async_utils import run_blocking_sdk
        prices = await run_blocking_sdk(_fetcher)
    except Exception as exc:
        logger.debug("market_feed fetch failed network=%s err=%s", net, exc)
        return dict(_cache.get(net, {}))
    async with _lock:
        _cache[net] = dict(prices or {})
        _ts[net] = time.monotonic()
        return dict(_cache[net])


def snapshot() -> dict:
    now = time.monotonic()
    return {
        net: {"age_s": round(now - ts, 2), "products": len(_cache.get(net, {}))}
        for net, ts in _ts.items()
    }


def cached_top_of_book(network: str, product: str) -> Optional[dict[str, Any]]:
    """Best-effort SYNC read of the cached ``{bid, ask, mid}`` for a product.

    Reads the shared cache the alert scanner refreshes for every product every
    few seconds — WITHOUT the async lock and WITHOUT any network call, so it is
    safe on a click/worker path (a display read tolerates a slightly stale dict).
    Keyed by the display name with ``-PERP`` stripped (e.g. ``BTC``), matching
    ``get_all_market_prices``. Returns None when the product is not cached yet.
    """
    net = str(network or "mainnet").lower()
    key = str(product or "").upper().replace("-PERP", "").strip()
    row = (_cache.get(net) or {}).get(key)
    if not isinstance(row, dict):
        return None
    return dict(row)


def cached_spread_bps(network: str, product: str) -> Optional[float]:
    """The cached top-of-book spread in basis points, or None if unavailable.

    ``(ask - bid) / mid * 1e4``. This is the number to SHOW the user so they can
    set a strategy spread that actually rests near the touch rather than far
    behind it (a quote parked well beyond the book spread rarely fills)."""
    row = cached_top_of_book(network, product)
    if not row:
        return None
    try:
        bid = float(row.get("bid") or 0.0)
        ask = float(row.get("ask") or 0.0)
        mid = float(row.get("mid") or 0.0) or ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0)
        if bid <= 0 or ask <= 0 or mid <= 0 or ask < bid:
            return None
        return (ask - bid) / mid * 10000.0
    except (TypeError, ValueError):
        return None
