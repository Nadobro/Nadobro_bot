"""strategy/vol_baseline — the per-product 7-day rv60 baseline provider."""
from __future__ import annotations

import asyncio
import math
import threading

import pytest

from src.nadobro.quant import vol_model as vm
from src.nadobro.strategy import vol_baseline as vb

NOW = 1_790_000_000 - (1_790_000_000 % 60)


def _rows(end_open_ts: int, n: int, amp: float = 3.0, px0: float = 100.0):
    """n closed 1m rows ending at end_open_ts, newest FIRST (raw indexer order)."""
    rows = []
    px = px0
    for k in range(n):
        t = end_open_ts - 60 * (n - 1 - k)
        px *= math.exp((amp if (t // 60) % 2 == 0 else -amp) / 1e4)
        rows.append({"time": t, "close": px})
    return list(reversed(rows))


class FakeClient:
    def __init__(self, *, history_minutes: int = 20000, deny: bool = False):
        self.calls = []
        self.deny = deny
        self.history_start = NOW - 60 * history_minutes
        self.threads = []

    def get_candlesticks(self, product_id, timeframe="1h", limit=200, max_time=None):
        self.threads.append(threading.current_thread() is threading.main_thread())
        self.calls.append((product_id, timeframe, limit, max_time))
        if self.deny:
            return []
        end = int(max_time) // 60 * 60
        n = min(limit, max(0, (end - self.history_start) // 60 + 1))
        return _rows(end, n) if n > 0 else []


@pytest.fixture(autouse=True)
def _clean():
    vb._reset_for_tests()
    yield
    vb._reset_for_tests()


def test_pages_at_most_three_per_call_then_warms_to_ready():
    cli = FakeClient()
    recent = _rows(NOW - 120, 200)
    b = vb.refresh_sync(cli, "mainnet", 2, recent, now_s=NOW)
    assert len(cli.calls) == 3
    assert all(c[1] == "1m" and c[2] == vb.PAGE_LIMIT for c in cli.calls)
    # 200 + 3000 minutes < 72h: still warming, but coverage is reported.
    assert b is not None and not b.ready and b.coverage_h > 40
    b = vb.refresh_sync(cli, "mainnet", 2, recent, now_s=NOW + 60)
    assert len(cli.calls) == 6
    assert b is not None and b.ready
    assert b.median_rv60_bp == pytest.approx(3.0, rel=1e-6)
    # 7 days reached after a few more calls; then steady state costs ZERO pages.
    for i in range(3):
        vb.refresh_sync(cli, "mainnet", 2, recent, now_s=NOW + 120 + 60 * i)
    n = len(cli.calls)
    vb.refresh_sync(cli, "mainnet", 2, _rows(NOW, 200), now_s=NOW + 600)
    assert len(cli.calls) == n
    assert vb.peek_baseline("mainnet", 2).ready


def test_denied_page_stops_paging_and_is_not_end_of_history():
    cli = FakeClient(deny=True)
    b = vb.refresh_sync(cli, "mainnet", 5, _rows(NOW - 120, 200), now_s=NOW)
    assert len(cli.calls) == 1                     # stops at the first empty page
    assert b is not None and not b.ready
    entry = vb._MEMO[("mainnet", 5)]
    assert entry.history_start_reached is False    # DENIED != EMPTY
    # Backs off, then retries once the backoff expires.
    vb.refresh_sync(cli, "mainnet", 5, [], now_s=NOW + 60)
    assert len(cli.calls) == 1
    cli.deny = False
    vb.refresh_sync(cli, "mainnet", 5, [], now_s=NOW + vb.EMPTY_PAGE_BACKOFF_S + 1)
    assert len(cli.calls) > 1


def test_short_successful_page_marks_the_start_of_history():
    cli = FakeClient(history_minutes=500)
    vb.refresh_sync(cli, "mainnet", 7, _rows(NOW - 120, 200), now_s=NOW)
    assert vb._MEMO[("mainnet", 7)].history_start_reached
    n = len(cli.calls)
    vb.refresh_sync(cli, "mainnet", 7, _rows(NOW - 60, 200), now_s=NOW + 60)
    assert len(cli.calls) == n                     # no endless paging on a young product


def test_get_baseline_runs_all_io_off_the_event_loop():
    cli = FakeClient()

    async def body():
        return await vb.get_baseline(cli, "mainnet", 9, _rows(NOW - 120, 200))

    b = asyncio.run(body())
    assert isinstance(b, vm.VolBaseline)
    assert cli.threads and not any(cli.threads), "venue pages must run on a worker thread"


def test_products_and_networks_are_isolated():
    cli = FakeClient()
    vb.refresh_sync(cli, "mainnet", 2, _rows(NOW - 120, 200), now_s=NOW)
    assert vb.peek_baseline("testnet", 2) is None
    assert vb.peek_baseline("mainnet", 3) is None
