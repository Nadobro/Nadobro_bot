"""Support and resistance for a Nado product, computed from Hyperliquid candles.

Why HL candles and not Nado's
-----------------------------
Nado's ``candlesticks(limit=200)`` is the single largest query item on the
budget (weight 11) and the Mid path deliberately stopped calling it. HL
publishes the same bars for free, so the one slow-timeframe object with real
quoting value can stay without buying it back at weight 11 per product.

Why S/R and nothing else from the candle stack
----------------------------------------------
A pivot is a PRICE. EMA, RSI and MACD are directions and magnitudes, and on a
3-8s tick they cannot change between two quote decisions — a statistic that
cannot move between decisions can only set the envelope, which is what the
regime routines already use them for. A price level, by contrast, says *where*
to put a rung: a bid resting where buyers have repeatedly shown up fills more
often and gets run over less.

Cadence and safety
------------------
Pivots on 1m bars move slowly, so the answer is TTL-cached for minutes and the
fetch runs on the DB/IO thread pool. The controller must never wait on an HTTP
round trip inside a tick: a cold or expired entry serves the last known value
and refreshes behind it.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from src.nadobro.utils.env import env_float, env_int

logger = logging.getLogger(__name__)

_TTL_S = env_float("NADO_HL_LEVELS_TTL_S", 300.0)
_CANDLE_TF = "1m"
_CANDLE_BARS = env_int("NADO_HL_LEVELS_BARS", 240)
_PIVOT_WINDOW = env_int("NADO_HL_LEVELS_PIVOT_WINDOW", 3)

# coin -> (fetched_at, {"support": [...], "resistance": [...]})
_cache: Dict[str, Tuple[float, dict]] = {}
_inflight: set = set()

_EMPTY: dict = {"support": [], "resistance": []}


def reset_state() -> None:
    """Tests only."""
    _cache.clear()
    _inflight.clear()


def _fetch(coin: str) -> dict:
    """Blocking HTTP + pure pivot math. Only ever called off the event loop."""
    import asyncio

    from src.nadobro.engine.routines import support_resistance_ema
    from src.nadobro.market_data.hl_client import get_candles_sync

    candles = get_candles_sync(
        coin, interval=_CANDLE_TF, lookback_ms=_CANDLE_BARS * 60 * 1000
    )
    rows = [c for c in (candles or []) if isinstance(c, dict) and c.get("close")]
    if len(rows) < (2 * _PIVOT_WINDOW + 1):
        return dict(_EMPTY)
    # ``run`` is async but pure — no loop is running on this worker thread.
    result = asyncio.run(
        support_resistance_ema.run(rows, pivot_window=_PIVOT_WINDOW)
    )
    return {
        "support": [float(v) for v in (result.get("support") or [])],
        "resistance": [float(v) for v in (result.get("resistance") or [])],
    }


async def levels_for(coin: str) -> dict:
    """``{"support": [...], "resistance": [...]}``. Never blocks, never raises.

    An unknown coin (every Nado equity/RWA) simply yields no candles and
    therefore no levels, which leaves the ladder at its configured shape — the
    correct outcome rather than a fabricated one.
    """
    key = str(coin or "").upper().strip()
    if not key:
        return dict(_EMPTY)
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached[0]) < _TTL_S:
        return cached[1]
    if key in _inflight:
        return cached[1] if cached else dict(_EMPTY)
    _inflight.add(key)
    try:
        from src.nadobro.core.async_utils import run_blocking_bg

        levels = await run_blocking_bg(_fetch, key)
    except Exception:  # noqa: BLE001 - a dead candle feed just leaves the shape alone
        logger.debug("hl levels fetch failed coin=%s", key, exc_info=True)
        # Re-stamp so a broken feed is not retried on every tick.
        _cache[key] = (now, cached[1] if cached else dict(_EMPTY))
        return _cache[key][1]
    finally:
        _inflight.discard(key)

    _cache[key] = (now, levels)
    return levels


def cached_levels(coin: str) -> Optional[dict]:
    """Last computed levels without any IO. For telemetry."""
    entry = _cache.get(str(coin or "").upper().strip())
    return entry[1] if entry else None
