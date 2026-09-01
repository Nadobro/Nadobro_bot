"""Unit coverage for the status card's heartbeat-freshness resolver.

Phase 3 adds one line to a RUNNING strategy's status card declaring whether the
worker is actually ticking (freshness contract). The card snapshot exercises
only the "live" state; this pins all three, with the clock frozen so the
state decision is deterministic.
"""

from __future__ import annotations

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import formatters as F  # noqa: E402

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: NOW)
    monkeypatch.setattr(F, "get_active_language", lambda: "en")
    monkeypatch.setattr(F, "localize_text", lambda text, _lang=None: text)
    yield


def test_warming_up_when_no_cycle_yet():
    line = F._fmt_heartbeat_freshness(0.0, 60)
    assert line == "Feed: *warming up* · first cycle pending"


def test_live_within_two_cycles():
    # interval 60 → cutoff max(120, 90)=120. 30s old → live.
    line = F._fmt_heartbeat_freshness(NOW - 30, 60)
    assert "*live*" in line and "✅" in line
    assert "stalling" not in line


def test_stalling_past_the_cutoff():
    # 300s old, interval 60 → cutoff 120 → stalling.
    line = F._fmt_heartbeat_freshness(NOW - 300, 60)
    assert "*stalling*" in line and "⚠️" in line


def test_ninety_second_floor_protects_fast_strategies():
    """A fast strategy (8s interval) must not flag stale on a 60s-old beat.

    2*8=16s would falsely trip; the 90s floor prevents it.
    """
    line = F._fmt_heartbeat_freshness(NOW - 60, 8)
    assert "*live*" in line


def test_missing_heartbeat_reads_as_warming_up():
    """None/absent (running strategy, no beat field yet) → warming up, not a lie."""
    assert F._fmt_heartbeat_freshness(None, 60) == "Feed: *warming up* · first cycle pending"


def test_non_numeric_heartbeat_drops_the_line_rather_than_lying():
    assert F._fmt_heartbeat_freshness("not-a-number", 60) is None


def test_line_is_markdown_v2_safe():
    from tests.handlers.md2 import find_problems

    for hb, interval in [(0.0, 60), (NOW - 30, 60), (NOW - 300, 60)]:
        line = F._fmt_heartbeat_freshness(hb, interval)
        assert not find_problems(line), (line, find_problems(line))
