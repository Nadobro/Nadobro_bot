import logging
import time
import httpx

logger = logging.getLogger(__name__)

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_REQUEST_TIMEOUT = 15.0
CACHE_TTL_SECONDS = 5.0


class HLClient:

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, any]] = {}
        self._cache_ttl = CACHE_TTL_SECONDS

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=HL_REQUEST_TIMEOUT)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _cache_get(self, key: str):
        entry = self._cache.get(key)
        if entry and (time.time() - entry[0]) < self._cache_ttl:
            return entry[1]
        return None

    def _cache_set(self, key: str, value):
        self._cache[key] = (time.time(), value)
        if len(self._cache) > 200:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)

    async def _post(self, payload: dict, cache_key: str | None = None) -> dict | list | None:
        if cache_key:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        client = await self._get_client()
        try:
            resp = await client.post(HL_INFO_URL, json=payload)
            resp.raise_for_status()
            result = resp.json()
            if cache_key and result is not None:
                self._cache_set(cache_key, result)
            return result
        except httpx.HTTPStatusError as e:
            logger.error("HL API HTTP error %s: %s", e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:
            logger.error("HL API request failed: %s", e)
            return None

    async def get_clearinghouse_state(self, wallet: str) -> dict | None:
        return await self._post({
            "type": "clearinghouseState",
            "user": wallet,
        }, cache_key=f"chs:{wallet}")

    async def get_user_fills(self, wallet: str) -> list | None:
        result = await self._post({
            "type": "userFills",
            "user": wallet,
        })
        return result if isinstance(result, list) else None

    async def get_user_fills_by_time(self, wallet: str, start_time_ms: int) -> list | None:
        result = await self._post({
            "type": "userFillsByTime",
            "user": wallet,
            "startTime": start_time_ms,
        })
        return result if isinstance(result, list) else None

    async def get_all_mids(self) -> dict | None:
        return await self._post({"type": "allMids"}, cache_key="allMids")

    async def get_account_equity(self, wallet: str) -> float | None:
        state = await self.get_clearinghouse_state(wallet)
        if not state:
            return None
        try:
            margin_summary = state.get("marginSummary", {})
            equity = float(margin_summary.get("accountValue", 0))
            return equity if equity > 0 else None
        except (ValueError, TypeError):
            return None

    async def get_open_positions(self, wallet: str) -> list:
        state = await self.get_clearinghouse_state(wallet)
        if not state:
            return []
        positions = []
        for pos in state.get("assetPositions", []):
            p = pos.get("position", pos)
            size = float(p.get("szi", 0))
            if abs(size) > 0:
                positions.append({
                    "coin": p.get("coin", ""),
                    "size": size,
                    "entry_price": float(p.get("entryPx", 0)),
                    "unrealized_pnl": float(p.get("unrealizedPnl", 0)),
                    "leverage_type": p.get("leverage", {}).get("type", "cross"),
                    "leverage_value": float(p.get("leverage", {}).get("value", 1)),
                })
        return positions


def get_candles_sync(coin: str, interval: str = "1h", lookback_ms: int = 7 * 24 * 3600 * 1000) -> list[dict]:
    """Blocking HL candleSnapshot. Oldest-first. Empty on any failure."""
    symbol = (coin or "").strip().upper()
    if not symbol:
        return []
    end_ms = int(time.time() * 1000)
    start_ms = max(0, end_ms - int(lookback_ms))
    try:
        resp = httpx.post(
            HL_INFO_URL,
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            },
            timeout=HL_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        logger.warning("HL candleSnapshot failed coin=%s interval=%s: %s", symbol, interval, exc)
        return []
    if not isinstance(rows, list):
        return []
    candles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            candles.append(
                {
                    "time": int(row.get("t") or row.get("T") or 0) // 1000,
                    "open": float(row.get("o") or 0),
                    "high": float(row.get("h") or 0),
                    "low": float(row.get("l") or 0),
                    "close": float(row.get("c") or 0),
                    "volume": float(row.get("v") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda c: int(c.get("time") or 0))
    return candles


_shared_client: HLClient | None = None


def get_hl_client() -> HLClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = HLClient()
    return _shared_client
