"""/status Cost/$1M for the grid family comes from the live session PnL (audit
2026-09-27, efficiency_audit A2/F7).

For GRID / RGRID / DGRID the card read ``rgrid_last_cycle_pnl_usd`` <-
``grid_last_cycle_pnl_usd``, a key NOTHING ever writes, so it fell back to the
per-row trade PnL of ``get_trade_analytics`` — NULL for engine fills. The card
therefore showed ~ -fees/volume: about -200/$1M for every D-Grid session whose
true net was +174 (sign wrong in 7 of 8). The fix feeds the card the same
per-run live snapshot the SL/TP rail and the strategy dashboard use
(``live_session.get_live_session_snapshot``: realized + uPnL - funding, gross of
fees), and the card subtracts the fees exactly once.
"""
from __future__ import annotations

import asyncio

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers.formatters import fmt_status_overview  # noqa: E402

_ONBOARDING_OK = {"onboarding_complete": True, "network": "mainnet", "has_key": True, "funded": True}


def _dgrid_status(**extra) -> dict:
    base = {
        "running": True, "strategy": "dgrid", "product": "BTC", "runs": 40,
        "interval_seconds": 30, "spread_bp": 8.0, "notional_usd": 100.0,
        "last_cycle_result": "ok",
        # get_trade_analytics for engine fills: row pnl NULL -> 0, fees/volume real.
        "session_analytics_pnl_usd": 0.0,
        "session_fees_usd": 2.0,
        "session_volume_usd": 10_000.0,
    }
    base.update(extra)
    return base


def test_grid_family_cost_per_million_uses_the_live_session_pnl():
    text = fmt_status_overview(
        _dgrid_status(
            session_live_pnl_usd=3.74,      # realized + uPnL - funding, gross of fees
            session_live_fees_usd=2.0,
            session_live_volume_usd=10_000.0,
        ),
        _ONBOARDING_OK,
    )
    # (3.74 - 2.00) / 10,000 x 1e6 = +174 — not (0 - 2) / 10,000 x 1e6 = -200.
    assert "+$174\\.00" in text, text
    assert "-$200\\.00" not in text


def test_grid_family_card_without_a_live_snapshot_keeps_the_old_fallback():
    text = fmt_status_overview(_dgrid_status(), _ONBOARDING_OK)
    assert "-$200\\.00" in text


def test_status_dashboard_attaches_the_live_session_figures_for_the_grid_family(monkeypatch):
    from src.nadobro.handlers import commands

    snap = {"session_pnl": 3.74, "fees": 2.0, "volume": 10_000.0, "session_pnl_net": 1.74}
    calls = {}

    def _resolve(telegram_id, network, strategy, *, state=None, status=None):
        calls["resolve"] = (telegram_id, network, strategy)
        return {"id": 5, "product_id": 2, "status": "running"}

    def _snapshot(telegram_id, network, session, *, state=None, client=None, mark=None):
        calls["client"] = client
        return dict(snap)

    monkeypatch.setattr(
        "src.nadobro.trading.session_resolver.resolve_current_strategy_session", _resolve,
    )
    monkeypatch.setattr("src.nadobro.trading.live_session.get_live_session_snapshot", _snapshot)

    figures = commands._grid_family_live_session_figures(
        7, {"running": True, "strategy": "dgrid", "network": "mainnet"},
    )
    assert figures == {
        "session_live_pnl_usd": 3.74,
        "session_live_fees_usd": 2.0,
        "session_live_volume_usd": 10_000.0,
    }
    assert calls["resolve"] == (7, "mainnet", "dgrid")
    # Tap-driven path: DB-only snapshot, never a live venue read.
    assert calls["client"] is None


def test_live_session_figures_are_only_attached_for_a_running_grid_family_strategy(monkeypatch):
    from src.nadobro.handlers import commands

    def _boom(*_a, **_k):
        raise AssertionError("must not read a snapshot")

    monkeypatch.setattr("src.nadobro.trading.live_session.get_live_session_snapshot", _boom)
    assert commands._grid_family_live_session_figures(7, {"running": True, "strategy": "mid"}) == {}
    assert commands._grid_family_live_session_figures(7, {"running": False, "strategy": "grid"}) == {}


def test_a_failed_snapshot_never_breaks_the_status_card(monkeypatch):
    from src.nadobro.handlers import commands

    monkeypatch.setattr(
        "src.nadobro.trading.session_resolver.resolve_current_strategy_session",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    assert commands._grid_family_live_session_figures(
        7, {"running": True, "strategy": "rgrid", "network": "mainnet"},
    ) == {}
    assert asyncio.iscoroutinefunction(commands.build_status_dashboard_parts)
