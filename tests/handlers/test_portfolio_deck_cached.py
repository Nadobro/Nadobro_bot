"""Portfolio deck: the throttled-cache freshness state (2026-09-02, F6).

A refresh whose balance the venue would not serve (budget-denied / down) came
from Redis; the deck must say so instead of "Live · synced just now".
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
    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(visual, "datetime", _Frozen)
    return now


def _snapshot(**over):
    snap = {"user_id": 42, "network": "mainnet", "last_sync": LAST_SYNC, "equity": {"total": "1500"},
            "positions": [], "open_orders": [], "stats": {}, "matches": []}
    snap.update(over)
    return snap


def test_a_throttled_round_says_cached_not_live(frozen_clock):
    text = deck.render_portfolio_deck(_snapshot(venue_throttled=True))[0]
    assert "🕔 Cached · venue throttled · showing 30s ago data" in text
    assert "🟢 Live" not in text


def test_a_normal_round_is_unchanged(frozen_clock):
    assert "🟢 Live · synced 30s ago" in deck.render_portfolio_deck(_snapshot())[0]
