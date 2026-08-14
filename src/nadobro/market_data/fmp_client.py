"""Financial Modeling Prep — equity quote, earnings, consensus, ticker news.

Fail-open. Used by Market Call event/quote packs, not by the trade loop.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

_BASE_URL = os.environ.get("FMP_BASE_URL", "https://financialmodelingprep.com/api/v3").rstrip("/")
_TIMEOUT = 8.0
_CACHE_TTL = 60.0
_CACHE: dict[str, tuple[float, Any]] = {}


def _api_key() -> str:
    return (os.environ.get("FMP_API_KEY") or "").strip()


def is_available() -> bool:
    return bool(_api_key())


def _get_cached(key: str):
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None


def _set_cached(key: str, value):
    _CACHE[key] = (time.time(), value)
    if len(_CACHE) > 64:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)


def _get(path: str, params: Optional[dict] = None) -> Any:
    key = _api_key()
    if not key:
        return None
    q = dict(params or {})
    q["apikey"] = key
    cache_key = f"{path}:{sorted(q.items())}"
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached
    try:
        resp = requests.get(f"{_BASE_URL}{path}", params=q, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("FMP GET %s failed: %s", path, exc)
        return None
    _set_cached(cache_key, data)
    return data


def get_quote(symbol: str) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return {}
    data = _get(f"/quote/{sym}")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        row = data[0]
        return {
            "symbol": row.get("symbol") or sym,
            "name": row.get("name"),
            "price": row.get("price"),
            "change_pct": row.get("changesPercentage"),
            "volume": row.get("volume"),
            "market_cap": row.get("marketCap"),
            "pe": row.get("pe"),
            "eps": row.get("eps"),
            "timestamp": row.get("timestamp"),
        }
    return {}


def get_earnings(symbol: str) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return {}
    data = _get("/earning_calendar", {"symbol": sym})
    rows = data if isinstance(data, list) else []
    upcoming = [r for r in rows if isinstance(r, dict)][:4]
    estimates = _get(f"/analyst-estimates/{sym}")
    est_row = estimates[0] if isinstance(estimates, list) and estimates else {}
    return {
        "calendar": upcoming,
        "consensus_eps": (est_row or {}).get("estimatedEpsAvg") if isinstance(est_row, dict) else None,
        "consensus_revenue": (est_row or {}).get("estimatedRevenueAvg") if isinstance(est_row, dict) else None,
    }


def get_ticker_news(symbol: str, limit: int = 8) -> list[dict[str, str]]:
    sym = (symbol or "").strip().upper()
    if not sym:
        return []
    data = _get("/stock_news", {"tickers": sym, "limit": int(limit)})
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for row in data[:limit]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if title and url:
            out.append({"title": title, "url": url, "published": str(row.get("publishedDate") or "")})
    return out
