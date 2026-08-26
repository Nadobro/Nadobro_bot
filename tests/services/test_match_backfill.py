"""Backward match-ledger backfill — nado_sync._backfill_older_matches.

get_matches returns only the newest 200 fills, so the account realized-PnL
replay fabricated PnL from a phantom entry basis when older/opening fills were
missing. This pages backward a few pages per heavy sync (bounded, persisted
cursor, one-time per account) to complete the ledger. These pin: it collects the
older fills, advances + persists the cursor, terminates on the earliest fill,
survives a transient throttle, and no-ops once done or when disabled.
"""
import asyncio

from unittest.mock import patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.venue import nado_sync


class _Pager:
    """Newest-first fill history 1..total. get_matches(idx) returns up to `limit`
    fills with submission_idx <= idx, newest first."""

    def __init__(self, total: int):
        self.all = [
            {"submission_idx": str(i), "product_id": 1,
             "base_filled": "1", "quote_filled": "-100", "fee": "0"}
            for i in range(1, total + 1)
        ]
        self.idx_calls: list = []

    async def get_matches(self, *, limit=200, idx=None):
        self.idx_calls.append(idx)
        pool = self.all if idx is None else [m for m in self.all if int(m["submission_idx"]) <= int(idx)]
        pool = sorted(pool, key=lambda m: int(m["submission_idx"]), reverse=True)
        return pool[:limit]


class _ThrottledPager(_Pager):
    async def get_matches(self, *, limit=200, idx=None):
        self.idx_calls.append(idx)
        return []                    # gateway throttle: always empty


async def _rbd(fn, *a, **k):          # run_blocking_db passthrough
    return fn(*a, **k)


def _run(newest, pager, store, *, enabled=True, pages=2, page_limit=2):
    def _get(key):
        return dict(store) if store else {}

    def _set(key, value):
        store.clear()
        store.update(value)

    with patch.object(nado_sync, "run_blocking_db", _rbd), \
         patch.object(nado_sync, "_BACKFILL_PAGES_PER_SYNC", pages), \
         patch.object(nado_sync, "_BACKFILL_PAGE_LIMIT", page_limit), \
         patch("src.nadobro.core.feature_flags.match_ledger_backfill_enabled", return_value=enabled), \
         patch("src.nadobro.models.database.get_bot_state", side_effect=_get), \
         patch("src.nadobro.models.database.set_bot_state", side_effect=_set):
        return asyncio.run(nado_sync._backfill_older_matches(pager, 42, "mainnet", newest))


def test_backfill_walks_to_the_earliest_fill_and_marks_done():
    # 5 fills; newest page shows idx 5,4 → seed cursor 4. Page backward by 2s:
    #   idx=3 → [3,2] (full page, keep going); idx=1 → [1] (partial → done).
    store: dict = {}
    pager = _Pager(5)
    newest = [{"submission_idx": "5"}, {"submission_idx": "4"}]
    older = _run(newest, pager, store)
    got = sorted(int(m["submission_idx"]) for m in older)
    assert got == [1, 2, 3]                 # every older fill collected, none missed
    assert store.get("done") is True
    assert store.get("oldest_idx") == 1


def test_backfill_resumes_from_persisted_cursor_across_syncs():
    # 5 fills, ONE page per sync. Sync 1 seeds 4, fetches idx=3 → [3,2], cursor 2.
    store: dict = {}
    pager = _Pager(5)
    newest = [{"submission_idx": "5"}, {"submission_idx": "4"}]
    first = _run(newest, pager, store, pages=1)
    assert sorted(int(m["submission_idx"]) for m in first) == [2, 3]
    assert store.get("done") is False and store.get("oldest_idx") == 2
    # Sync 2 resumes from cursor 2: idx=1 → [1] (partial → done). newest ignored.
    second = _run(None, pager, store, pages=1)
    assert sorted(int(m["submission_idx"]) for m in second) == [1]
    assert store.get("done") is True


def test_backfill_is_a_noop_once_done():
    store = {"done": True, "oldest_idx": 1}
    pager = _Pager(5)
    older = _run([{"submission_idx": "5"}], pager, store)
    assert older == [] and pager.idx_calls == []     # no venue calls in steady state


def test_backfill_disabled_flag_does_nothing():
    store: dict = {}
    pager = _Pager(5)
    older = _run([{"submission_idx": "5"}], pager, store, enabled=False)
    assert older == [] and pager.idx_calls == []


def test_transient_throttle_delays_but_does_not_falsely_finish():
    # Empty pages (throttle) increment a stall counter; a couple of syncs must
    # NOT conclude "done" (so we retry once the throttle clears).
    store: dict = {}
    pager = _ThrottledPager(5)
    newest = [{"submission_idx": "5"}, {"submission_idx": "4"}]
    _run(newest, pager, store, pages=1)
    assert store.get("done") is False and store.get("stalls") == 1
    _run(None, pager, store, pages=1)
    assert store.get("done") is False and store.get("stalls") == 2
    # Third stall reaches the cap → give up (treat as reached start).
    _run(None, pager, store, pages=1)
    assert store.get("done") is True and store.get("stalls") == 3


def test_backfill_no_seed_when_no_matches_yet():
    store: dict = {}
    pager = _Pager(5)
    assert _run([], pager, store) == []
    assert pager.idx_calls == []            # nothing to page from → no calls
