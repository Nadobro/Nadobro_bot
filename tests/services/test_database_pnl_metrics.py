from __future__ import annotations

from datetime import datetime, timezone

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.models import database as db  # noqa: E402


def test_session_live_metrics_realizes_partial_close_with_open_remainder():
    original_query_one = db.query_one
    original_query_all = db.query_all

    def fake_query_one(_sql, _params):
        return {
            "fills": 2,
            "volume": 290,
            "fees": 0,
            "net_base": 1,
            "signed_cash": -110,
        }

    def fake_query_all(_sql, _params):
        ts = datetime(2026, 6, 21, tzinfo=timezone.utc)
        return [
            {"product_id": 2, "side": "long", "fill_size": "2", "fill_price": "100", "filled_at": ts},
            {"product_id": 2, "side": "short", "fill_size": "1", "fill_price": "90", "filled_at": ts},
        ]

    try:
        db.query_one = fake_query_one
        db.query_all = fake_query_all

        metrics = db.get_session_live_metrics(123, "mainnet", user_id=7)
    finally:
        db.query_one = original_query_one
        db.query_all = original_query_all

    assert metrics["net_base"] == 1
    assert metrics["realized_pnl"] == -10


def test_account_realized_pnl_query_excludes_unpairable_productless_rows():
    original_query_all = db.query_all
    captured = {}

    def fake_query_all(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return []

    try:
        db.query_all = fake_query_all

        result = db.get_account_realized_pnl_windows(7, "mainnet")
    finally:
        db.query_all = original_query_all

    assert result["total_pnl"] == 0
    assert "submission_idx IS NOT NULL" in captured["sql"]
    assert "COALESCE(product_id, 0) <> 0" in captured["sql"]


# ==========================================================================
# get_signal_outcomes: cross-user reads must be asked for
# ==========================================================================
def _capture_signal_outcome_sql(**kwargs):
    """Call get_signal_outcomes with query_all stubbed; return (sql, params)."""
    captured: dict = {}

    def fake_query_all(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return []

    original = db.query_all
    try:
        db.query_all = fake_query_all
        db.get_signal_outcomes(**kwargs)
    finally:
        db.query_all = original
    return captured["sql"], captured["params"]


def test_signal_outcomes_refuses_an_unscoped_read_by_default():
    """A caller that forgets to scope by user must fail closed, not silently
    receive every user's graded signals. The raise sits OUTSIDE the try/except
    so it propagates instead of being swallowed into an empty list."""
    import pytest

    with pytest.raises(ValueError, match="across_users"):
        db.get_signal_outcomes()

    with pytest.raises(ValueError):
        db.get_signal_outcomes(network="mainnet", horizon="4h")


def test_signal_outcomes_scoped_by_user_emits_the_user_predicate():
    sql, params = _capture_signal_outcome_sql(user_id=4242)
    assert "user_id = %s" in sql
    assert params[0] == 4242


def test_signal_outcomes_allows_the_aggregate_read_when_asked():
    """The nightly weight fit legitimately reads across users."""
    sql, params = _capture_signal_outcome_sql(across_users=True, limit=10)
    assert "user_id = %s" not in sql
    assert "WHERE" not in sql
    assert params == (10,)


def test_across_users_does_not_widen_an_explicitly_scoped_read():
    """Passing both must stay scoped — across_users only unlocks the unscoped
    read, it never removes a user predicate the caller supplied."""
    sql, params = _capture_signal_outcome_sql(user_id=7, across_users=True)
    assert "user_id = %s" in sql
    assert params[0] == 7
