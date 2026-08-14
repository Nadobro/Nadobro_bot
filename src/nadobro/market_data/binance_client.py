"""Public Binance klines for Market Call TA. Fail-open. Not used by live engine."""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.nadobro.utils.env import env_float, env_str

logger = logging.getLogger(__name__)

_FAPI_KLINES = env_str(
    "BINANCE_FAPI_KLINES_URL",
    "https://fapi.binance.com/fapi/v1/klines",
)
_SPOT_KLINES = env_str(
    "BINANCE_SPOT_KLINES_URL",
    "https://api.binance.com/api/v3/klines",
)
_TIMEOUT = env_float("BINANCE_TIMEOUT_SECONDS", 8.0)
_CACHE_TTL = 60.0
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_INTERVALS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})
_ALIASES = {
    "WTI": "OILUSDT",
    "BRENT": "OILUSDT",
    "XAU": "XAUUSDT",
    "XAG": "XAGUSDT",
    "CL": "OILUSDT",
    "GC": "XAUUSDT",
    "SI": "XAGUSDT",
}


def futures_symbol(coin: str) -> str:
    raw = (coin or "").strip().upper().replace("-PERP", "").replace("-", "")
    if not raw:
        return ""
    if raw in _ALIASES:
        return _ALIASES[raw]
    if raw.endswith("USDT"):
        return raw
    return f"{raw}USDT"


def _cache_get(key: str) -> list[dict[str, Any]] | None:
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None


def _cache_set(key: str, rows: list[dict[str, Any]]) -> None:
    _CACHE[key] = (time.time(), rows)
    if len(_CACHE) > 64:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)


def _parse_klines(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    candles: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            candles.append(
                {
                    "time": int(row[0]) // 1000,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                }
            )
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda c: int(c.get("time") or 0))
    return candles


def _fetch_klines(url: str, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
    try:
        resp = httpx.get(
            url,
            params={"symbol": symbol, "interval": interval, "limit": int(limit)},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return _parse_klines(resp.json())
    except Exception as exc:
        logger.debug("Binance klines %s %s %s failed: %s", url, symbol, interval, exc)
        return []


def get_klines_sync(coin: str, interval: str = "1h", limit: int = 80) -> list[dict[str, Any]]:
    """USDT-M futures klines, then spot. Oldest-first. Empty on any failure."""
    symbol = futures_symbol(coin)
    tf = (interval or "").strip()
    if not symbol or tf not in _INTERVALS:
        return []
    lim = max(8, min(int(limit or 80), 500))
    cache_key = f"{symbol}:{tf}:{lim}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return list(cached)
    rows = _fetch_klines(_FAPI_KLINES, symbol, tf, lim)
    if not rows:
        rows = _fetch_klines(_SPOT_KLINES, symbol, tf, lim)
    if rows:
        _cache_set(cache_key, rows)
    return rows
