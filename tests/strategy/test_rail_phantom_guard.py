"""Session-rail sanity guard (prod 2026-09-03, session 312 — the phantom
+195%-of-margin TP). Pure-helper unit tests: no DB, no network."""
from __future__ import annotations

import pytest

bot_runtime = pytest.importorskip("src.nadobro.strategy.bot_runtime")


def test_a_physically_impossible_entry_is_unreliable():
    # session 312: 0.0108 short, avg_entry 105,635 while BTC marked 78,260 (35%).
    snap = {"position_size": 0.0108, "entry_price": 105635.0, "mark": 78260.0, "unrealized_pnl": 295.65}
    assert bot_runtime._position_pnl_reliable(snap) is False


def test_a_transient_mark_spike_away_from_a_true_entry_is_unreliable():
    # A correct entry with a bad/spiked mark also diverges -> caught.
    snap = {"position_size": 0.02, "entry_price": 78000.0, "mark": 60000.0, "unrealized_pnl": 360.0}
    assert bot_runtime._position_pnl_reliable(snap) is False


def test_a_normal_leveraged_position_is_reliable():
    snap = {"position_size": 0.0266, "entry_price": 78660.0, "mark": 78856.0, "unrealized_pnl": -5.2}
    assert bot_runtime._position_pnl_reliable(snap) is True


def test_a_real_armed_tp_move_stays_reliable():
    # A genuine ~2% favorable move (a real +100%-of-margin TP at 50x) is well
    # inside the divergence bound -> the TP still fires.
    snap = {"position_size": 0.02, "entry_price": 78000.0, "mark": 79560.0, "unrealized_pnl": 31.2}
    assert bot_runtime._position_pnl_reliable(snap) is True


def test_a_flat_or_baseless_snapshot_is_reliable():
    assert bot_runtime._position_pnl_reliable({"position_size": 0.0, "entry_price": 0.0, "mark": 78000.0}) is True
    assert bot_runtime._position_pnl_reliable({"position_size": 0.02, "entry_price": 78000.0, "mark": 0.0}) is True
    assert bot_runtime._position_pnl_reliable({}) is True


def test_the_divergence_threshold_is_honoured(monkeypatch):
    monkeypatch.setattr(bot_runtime, "_SLTP_MAX_ENTRY_MARK_DIVERGENCE", 0.20)
    assert bot_runtime._position_pnl_reliable({"position_size": 1, "entry_price": 88000, "mark": 80000}) is True   # 10%
    assert bot_runtime._position_pnl_reliable({"position_size": 1, "entry_price": 100000, "mark": 80000}) is False  # 25%
