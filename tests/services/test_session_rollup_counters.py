"""The trade-row rollup must not wipe the engine's cancelled-order counter
(audit 2026-09-27, efficiency_audit A5).

Engine cycles add their per-cycle cancelled deltas to
``strategy_sessions.total_orders_cancelled`` via ``increment_session_metrics``.
``rollup_session_from_trades`` (every 6th cycle, after each fill sync, and at
finalize) then OVERWROTE the column with a count of ``status='cancelled'`` trade
rows — which engine quotes never produce — so the stored counter read 0 for every
strategy over 60 days and order churn was invisible. The rollup may raise the
counter to the row count (legacy/non-engine sessions record cancel rows), never
lower it.
"""
from __future__ import annotations

from src.nadobro.models import database as dbm


_AGG = {"filled": 5, "cancelled": 0, "realized_pnl": 0.0, "fees": 1.5, "volume": 3000.0,
        "funding": 0.0, "wins": 0, "losses": 0}


def _run_rollup(monkeypatch, agg=None):
    updates: list = []
    executes: list = []
    monkeypatch.setattr(dbm, "query_one", lambda *_a, **_k: dict(agg or _AGG))
    monkeypatch.setattr(dbm, "update_strategy_session", lambda sid, data: updates.append((sid, dict(data))))
    monkeypatch.setattr(dbm, "execute", lambda *a, **_k: executes.append(a))
    totals = dbm.rollup_session_from_trades(321, "mainnet")
    return totals, updates, executes


def test_rollup_never_overwrites_the_engine_cancelled_counter_with_the_row_count(monkeypatch):
    _totals, updates, executes = _run_rollup(monkeypatch)
    for _sid, data in updates:
        assert "total_orders_cancelled" not in data, (
            "rollup overwrote total_orders_cancelled with a trade-row count (always 0 for engine quotes)"
        )
    # ...but it still raises the counter to the row count when rows say more.
    raised = [a for a in executes if "total_orders_cancelled" in str(a[0])]
    assert raised, "the cancelled row count must still be merged into the counter"
    sql, params = raised[0][0], raised[0][1]
    assert "GREATEST" in str(sql).upper()
    assert tuple(params) == (0, 321)


def test_rollup_still_writes_the_other_totals(monkeypatch):
    totals, updates, _executes = _run_rollup(monkeypatch)
    assert updates and updates[0][0] == 321
    data = updates[0][1]
    assert data["total_fees_paid"] == 1.5 and data["total_volume_usd"] == 3000.0
    assert data["total_orders_filled"] == 5
    # The returned totals still report the row count for callers that log it.
    assert totals["total_orders_cancelled"] == 0


def test_stored_session_realized_pnl_is_gross_so_stored_cost_counts_fees_once(monkeypatch):
    """prod_ground_truth §6 / GRIDFAM-2026-09-27-RAIL-FEE-2X: the stored
    ``realized_pnl`` was replayed from fee-inclusive venue prices, so the stored
    (realized_pnl - total_fees_paid)/volume Cost/$1M subtracted fees twice
    (dgrid stored -27 vs true +174). A zero-move round trip must store 0."""
    x18 = 10 ** 18
    buy = {"product_id": 2, "side": "long", "fill_size": 0.001, "size": 0.001,
           "fill_price": 100000.0, "price": 100000.0, "isolated": False, "source": "strategy",
           "submission_idx": 1, "base_filled_x18": x18 // 1000,
           "quote_filled_x18": -100_045 * x18 // 1000, "fee_x18": 45 * x18 // 1000,
           "filled_at": None}
    sell = dict(buy, side="short", submission_idx=2, base_filled_x18=-(x18 // 1000),
                quote_filled_x18=99_955 * x18 // 1000)

    def _query_one(sql, *_a, **_k):
        text = str(sql)
        if "SELECT user_id, product_id, started_at" in text:
            return {"user_id": 7, "product_id": 2, "started_at": None, "stopped_at": None}
        if "funding_payments" in text:
            return {"funding": 0}
        return {"fills": 2, "volume": 200.0, "fees": 0.09, "net_base": 0.0,
                "signed_cash": -0.09}

    updates: list = []
    monkeypatch.setattr(dbm, "query_one", _query_one)
    monkeypatch.setattr(dbm, "query_all", lambda *_a, **_k: [dict(buy), dict(sell)])
    monkeypatch.setattr(dbm, "update_strategy_session", lambda sid, data: updates.append(dict(data)))
    totals = dbm.rollup_engine_session_pnl_funding(321, "mainnet")
    assert abs(totals["realized_pnl"]) < 1e-9, totals
    assert updates and abs(updates[-1]["realized_pnl"]) < 1e-9
