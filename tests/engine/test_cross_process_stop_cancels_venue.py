"""A cross-process engine stop (no local controller — a redeploy stand-down, a
stop handled by another process) must cancel the strategy's resting VENUE
orders, not only terminate DB rows (prod 2026-09-03, session 312: a stood-down
ask ladder kept filling for hours after its "completion")."""
from __future__ import annotations

import asyncio

from src.nadobro.strategy import engine_runtime as er


def test_a_cross_process_stop_cancels_resting_venue_orders(monkeypatch):
    calls: list = []
    monkeypatch.setattr(er, "_cancel_resting_for_stop",
                        lambda user_id, network, strategy: calls.append((user_id, network, strategy)) or {"success": True})
    monkeypatch.setattr("src.nadobro.trading.engine_persistence.terminate_engine_executors", lambda cid: 0)
    monkeypatch.setattr("src.nadobro.trading.engine_persistence.clear_controller_progress", lambda cid: None)
    rt = er.EngineRuntime()
    asyncio.run(rt.stop(1, "mainnet", "grid"))           # no local controller -> cross-process branch
    assert calls == [(1, "mainnet", "grid")]


def test_an_unknown_venue_book_on_stop_is_a_warning_not_a_crash(monkeypatch, caplog):
    monkeypatch.setattr(er, "_cancel_resting_for_stop",
                        lambda *a: {"success": False, "error": "open-orders read unavailable for BTC-PERP"})
    monkeypatch.setattr("src.nadobro.trading.engine_persistence.terminate_engine_executors", lambda cid: 0)
    monkeypatch.setattr("src.nadobro.trading.engine_persistence.clear_controller_progress", lambda cid: None)
    rt = er.EngineRuntime()
    with caplog.at_level("WARNING"):
        asyncio.run(rt.stop(1, "mainnet", "grid"))
    assert any("could not confirm the venue book is clear" in r.getMessage() for r in caplog.records)
