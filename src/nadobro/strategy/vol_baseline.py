"""Per-product volatility BASELINE for the grid family's opt-in vol model.

The vol model (``quant/vol_model``, ``docs/grid_vol_model.md``) expresses every
threshold as a multiple of the product's own trailing 7-day median rv60, so a
BTC-tuned gate means "the calmer part of THIS market's range" on any product.
This module keeps that baseline: a minute-grid close series per
(network, product), merged from the 200 1m candles the controller already
fetched each minute (zero extra venue weight in steady state) and warmed up by
paging older 1m candles.

IO discipline (CLAUDE.md asyncio rule): every venue page and every piece of
real work runs inside ONE sync function executed via
``core.async_utils.run_blocking_sdk``. The only on-loop work is a dict lookup.

Persistence: the process-level memo is shared by every worker task in the
process (users on the same product share one series). The repo has no
cross-process cache any more (``venue/nado_client._shared_cache`` is an
in-process dict), so a redeploy re-warms by paging — at most
``MAX_PAGES_PER_CALL`` pages per call, one call per closed minute: 72h of
coverage in about two minutes, 7 days in about four. The strategy shows
LEARNING (WARMING) meanwhile and does not trade on an unknown baseline.

DENIED != EMPTY: ``get_candlesticks`` returns ``[]`` both for a budget denial
and for a failure, so an empty page is "retry later", never "history ends
here". Only a SHORT successful page (fewer rows than asked) proves the start
of the product's history.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from src.nadobro.quant import vol_model

logger = logging.getLogger(__name__)

PAGE_LIMIT = 1000                 # 1m candles per warm-up page (weight 1 + 1000/20 = 51)
MAX_PAGES_PER_CALL = 3
RECOMPUTE_EVERY_S = 30 * 60       # steady-state baseline refresh
EMPTY_PAGE_BACKOFF_S = 10 * 60    # after a denied / empty page, wait before paging again
KEEP_MINUTES = int(vol_model.BASELINE_LOOKBACK_H * 60) + vol_model.RV_WINDOW + 60


@dataclass
class _Entry:
    series: Optional[vol_model.MinuteSeries] = None
    baseline: Optional[vol_model.VolBaseline] = None
    computed_at: float = 0.0
    history_start_reached: bool = False
    paging_backoff_until: float = 0.0


_MEMO: Dict[Tuple[str, int], _Entry] = {}
_MEMO_LOCK = threading.Lock()
_KEY_LOCKS: Dict[Tuple[str, int], threading.Lock] = {}


def _key_lock(key: Tuple[str, int]) -> threading.Lock:
    with _MEMO_LOCK:
        lk = _KEY_LOCKS.get(key)
        if lk is None:
            lk = _KEY_LOCKS[key] = threading.Lock()
        return lk


def peek_baseline(network: str, product_id: int) -> Optional[vol_model.VolBaseline]:
    """Memo-only read for tap paths (the card's "BTC ≈ 3.1 bp/min" example):
    no IO, never blocks on the venue."""
    with _MEMO_LOCK:
        entry = _MEMO.get((str(network), int(product_id)))
        return entry.baseline if entry is not None else None


def _coverage_minutes(series: Optional[vol_model.MinuteSeries]) -> int:
    return len(series) if series is not None else 0


def refresh_sync(client: Any, network: str, product_id: int, recent_candles: Any,
                 *, now_s: Optional[float] = None) -> Optional[vol_model.VolBaseline]:
    """Merge the controller's recent candles, page older history while the
    series covers less than 7 days (bounded), and recompute the baseline when
    due. Runs in a worker thread — never call it on the event loop."""
    now = float(now_s if now_s is not None else time.time())
    key = (str(network), int(product_id))
    with _key_lock(key):
        with _MEMO_LOCK:
            entry = _MEMO.get(key)
            if entry is None:
                entry = _MEMO[key] = _Entry()
        changed = False
        recent = vol_model.closed_minute_series(recent_candles or [], now_s=now)
        if recent is not None:
            merged = vol_model.merge_series(entry.series, recent, max_minutes=KEEP_MINUTES)
            changed = merged is not entry.series
            entry.series = merged
        pages = 0
        want = int(vol_model.BASELINE_LOOKBACK_H * 60)
        while (_coverage_minutes(entry.series) < want and pages < MAX_PAGES_PER_CALL
               and not entry.history_start_reached and now >= entry.paging_backoff_until):
            oldest = entry.series.start_ts if entry.series is not None else int(now)
            try:
                rows = client.get_candlesticks(
                    int(product_id), timeframe="1m", limit=PAGE_LIMIT, max_time=int(oldest) - 1,
                ) or []
            except Exception:  # noqa: BLE001  # policy: degrade-ok(a failed page is "retry later")
                logger.debug("vol baseline page failed product=%s", product_id, exc_info=True)
                rows = []
            pages += 1
            if not rows:
                # Denied or failed (indistinguishable): retry later, never "history ends".
                entry.paging_backoff_until = now + EMPTY_PAGE_BACKOFF_S
                break
            older = vol_model.closed_minute_series(rows, now_s=now)
            if older is None or (entry.series is not None and older.start_ts >= entry.series.start_ts):
                entry.history_start_reached = True
                break
            entry.series = vol_model.merge_series(older, entry.series, max_minutes=KEEP_MINUTES)
            changed = True
            if len(rows) < PAGE_LIMIT:
                entry.history_start_reached = True   # a short SUCCESSFUL page: start of history
        due = (entry.baseline is None
               or (not entry.baseline.ready and changed)
               or now - entry.computed_at >= RECOMPUTE_EVERY_S)
        if entry.series is not None and due:
            entry.baseline = vol_model.baseline_from_series(entry.series)
            entry.computed_at = now
        return entry.baseline


async def get_baseline(client: Any, network: str, product_id: int,
                       recent_candles: Any) -> Optional[vol_model.VolBaseline]:
    """Async entry point for the engine's injected ``gvol_baseline_provider``.
    All work (venue pages included) runs on the SDK thread pool."""
    from src.nadobro.core.async_utils import run_blocking_sdk

    try:
        return await run_blocking_sdk(
            refresh_sync, client, str(network), int(product_id), list(recent_candles or []),
        )
    except Exception:  # noqa: BLE001  # policy: degrade-ok(no baseline -> WARMING, the safe side)
        logger.warning("vol baseline refresh failed product=%s", product_id, exc_info=True)
        return peek_baseline(network, product_id)


def _reset_for_tests() -> None:
    with _MEMO_LOCK:
        _MEMO.clear()
        _KEY_LOCKS.clear()
