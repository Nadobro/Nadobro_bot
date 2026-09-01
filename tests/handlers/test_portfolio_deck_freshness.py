"""Byte-identical guard for the portfolio deck's sync line.

Phase 3 extracts the deck's inline 4-state freshness logic into a shared
``utils.visual.freshness_line`` helper so home and status can share the same
contract. The deck is HTML parse mode and only partially pinned by
test_portfolio_renderers (`"🔄 Refreshing" in text`), and it is NOT in the
formatter snapshot — so before the extraction this suite freezes all five
rendered states exactly. The refactor is behaviour-preserving only if these
strings stay byte-identical.

The clock is frozen (the deck derives ``time_ago`` from ``datetime.now``), so
each state is a pure function of its snapshot.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from _stubs import install_test_stubs

install_test_stubs()

import src.nadobro.utils.visual as visual  # noqa: E402
from src.nadobro.handlers import portfolio_deck as deck  # noqa: E402

LAST_SYNC = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin now() to 30s after LAST_SYNC (inside the 300s live threshold)."""
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(visual, "datetime", _Frozen)
    return now


def _snapshot(**over):
    snap = {
        "user_id": 42,
        "network": "mainnet",
        "last_sync": LAST_SYNC,
        "equity": {"total": "1500"},
        "positions": [],
        "open_orders": [],
        "stats": {},
        "matches": [],
    }
    snap.update(over)
    return snap


def _sync_line(text: str) -> str:
    for line in text.split("\n"):
        if any(k in line for k in ("Live", "Refreshing", "Sync issue", "Stale", "Never synced")):
            return line
    return "<no sync line>"


# The five frozen states. Do NOT edit these strings to make a refactor pass —
# a change here means the deck's rendered output changed, which the extraction
# must not do.
EXPECTED = {
    "live": "🟢 Live · synced 30s ago",
    "refreshing": "🔄 Refreshing · showing 30s ago data",
    "sync_issue": "⚠️ Sync issue · showing 30s ago data",
    "never_synced": "⚠️ Never synced",
}


def test_live(frozen_clock):
    assert _sync_line(deck.render_portfolio_deck(_snapshot())[0]) == EXPECTED["live"]


def test_refreshing(frozen_clock):
    assert _sync_line(deck.render_portfolio_deck(_snapshot(), refreshing=True)[0]) == EXPECTED["refreshing"]


def test_sync_issue(frozen_clock):
    out = deck.render_portfolio_deck(_snapshot(stale=True, error="gateway circuit open"))[0]
    assert _sync_line(out) == EXPECTED["sync_issue"]


def test_never_synced(frozen_clock):
    assert _sync_line(deck.render_portfolio_deck(_snapshot(last_sync=None))[0]) == EXPECTED["never_synced"]


def test_stale(frozen_clock, monkeypatch):
    """A last_sync beyond the threshold yields the stale banner (age from now)."""
    # 6 minutes old > 300s default threshold.
    old = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return old if tz else old.replace(tzinfo=None)

    monkeypatch.setattr(visual, "datetime", _Frozen)
    out = deck.render_portfolio_deck(_snapshot(last_sync="2025-12-31T23:54:30+00:00"))[0]
    assert _sync_line(out) == "⚠ Stale · last sync 6m ago"
