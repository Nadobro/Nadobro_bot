"""freshness_line: the throttled-cache state (2026-09-02, F6)."""
from datetime import datetime, timedelta, timezone

from src.nadobro.utils.visual import freshness_line


def test_cached_reason_names_why_the_figures_are_cached():
    now = datetime.now(timezone.utc)
    line = freshness_line(now - timedelta(seconds=30), threshold_s=300, cached_reason="venue throttled")
    assert line == "🕔 Cached · venue throttled · showing 30s ago data"


def test_cached_reason_ranks_below_refreshing_and_sync_issue():
    now = datetime.now(timezone.utc)
    ls = now - timedelta(seconds=30)
    assert freshness_line(ls, threshold_s=300, refreshing=True, cached_reason="venue throttled").startswith("🔄")
    assert freshness_line(ls, threshold_s=300, degraded=True, cached_reason="venue throttled").startswith("⚠️ Sync issue")
    assert freshness_line(ls, threshold_s=300).startswith("🟢 Live")   # default unchanged
