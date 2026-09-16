"""Venue position ledger (archive ``positions``) — the source of realized PnL,
History and per-session Performance since 2026-09-16.

Measured that day on a live account: the card said Realized -$161.36 (a fill
replay over a holey ledger) while the venue's own windows summed to +$10.50,
Nado volume dropped every Nado-UI fill (product_id 0), and fees followed.
These tests pin the new pipeline end to end with mocked I/O.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest

from src.nadobro.venue import nado_archive
from src.nadobro.venue.nado_archive import (
    ArchiveReadUnavailable,
    _parse_position,
    builder_id_from_appendix,
    enrich_matches_from_archive,
    query_positions,
)

SUB = "0x" + "ac" * 20 + "64656661756c74" + "00" * 5
OURS = 2900


# ------------------------------------------------------------- parsing ---

def test_parse_position_normalises_x18_and_open_state():
    raw = {
        "subaccount": SUB, "product_id": 2, "isolated": False, "direction": False,
        "open_id": "81984913", "close_id": "81995848", "submission_idx": "81995848",
        "amount": "0", "max_amount": "37900000000000000", "total_open_amount": "55550000000000000",
        "total_close_amount": "55550000000000000",
        "average_entry_price": "75755064806480648064806", "average_exit_price": "75571183618361836183618",
        "liquidated_amount": "0", "open_fee": "841638770000000000", "close_fee": "993202649107940573",
        "realized_pnl": "10214599999999999990", "open_timestamp": "1789566024", "update_timestamp": "1789567903",
        "open_reason": "match_orders", "close_reason": "match_orders",
        "net_funding_payment": "32245810368749996", "net_interest_payment": "0",
        "open_digest": "0xEBDA6F34", "close_digest": "0x2d6a00e1",
    }
    p = _parse_position(raw)
    assert p["is_open"] is False and p["is_long"] is False and p["close_id"] == 81995848
    assert abs(p["realized_pnl"] - 10.2146) < 1e-6
    assert abs(p["total_close_amount"] - 0.05555) < 1e-12
    assert abs(p["avg_entry_price"] - 75755.0648) < 1e-3
    assert p["open_digest"] == "0xebda6f34"                        # normalised for the digest join
    assert p["open_ts"] == 1789566024 and p["update_ts"] == 1789567903
    still_open = _parse_position({**raw, "close_id": "-1", "amount": "37900000000000000"})
    assert still_open["is_open"] is True and still_open["close_id"] is None


def test_query_positions_raises_on_a_denied_read_and_returns_windows_otherwise():
    with mock.patch.object(nado_archive, "_post", return_value=None):
        with pytest.raises(ArchiveReadUnavailable):
            query_positions("mainnet", SUB, limit=25)
    with mock.patch.object(nado_archive, "_post", return_value={"positions": [], "events": [], "txs": []}):
        assert query_positions("mainnet", SUB) == []
    with mock.patch.object(nado_archive, "_post", return_value={"positions": [{"product_id": 2, "open_id": "5", "close_id": "-1", "amount": "1"}]}) as p:
        rows = query_positions("mainnet", SUB, limit=500, idx=41, open=True)
    assert rows[0]["is_open"] is True and rows[0]["open_id"] == 5
    assert p.call_args.args[1] == {"positions": {"subaccount": SUB, "limit": 500, "idx": "41", "open": True}}


def test_builder_id_is_decoded_from_the_order_appendix():
    assert builder_id_from_appendix("2561") == 0                       # a Nado-UI order
    assert builder_id_from_appendix(str((OURS << 48) | 2561)) == OURS  # our builder bits
    assert builder_id_from_appendix("x") is None


def test_enrich_matches_joins_product_and_time_from_txs():
    envelope = {
        "matches": [
            {"submission_idx": "10", "digest": "0xA", "base_filled": "1", "quote_filled": "-2", "fee": "0",
             "builder_fee": "3", "order": {"appendix": str((OURS << 48) | 1)}},
            {"submission_idx": "11", "digest": "0xB", "base_filled": "1", "quote_filled": "-2", "fee": "0",
             "builder_fee": "0", "order": {"appendix": "2561"}},
        ],
        "txs": [
            {"submission_idx": "10", "timestamp": "1789566024", "tx": {"match_orders": {"product_id": 2}}},
            {"submission_idx": "11", "timestamp": "1789566030", "tx": {"match_orders": {"product_id": 4}}},
        ],
    }
    rows = enrich_matches_from_archive(envelope)
    assert [r["product_id"] for r in rows] == [2, 4]
    assert [r["timestamp"] for r in rows] == [1789566024, 1789566030]
    assert [r["builder_id"] for r in rows] == [OURS, 0]
    assert enrich_matches_from_archive({"matches": [{"submission_idx": "1"}]})[0].get("product_id") is None


# ------------------------------------------------------- client matches ---

def _client():
    from src.nadobro.venue.nado_client import NadoClient

    c = NadoClient(private_key="0xabc", network="mainnet")
    c.subaccount_hex = SUB
    return c


def test_get_matches_reads_the_raw_archive_envelope_and_enriches():
    from src.nadobro.venue import nado_client as nc
    from src.nadobro.venue.nado_client import NadoClient

    c = _client()
    envelope = {"matches": [{"submission_idx": "10", "order": {"appendix": "2561"}}],
                "txs": [{"submission_idx": "10", "timestamp": "5", "tx": {"match_orders": {"product_id": 2}}}]}
    resp = SimpleNamespace(json=lambda: envelope, status_code=200, headers={}, text="", url="u")
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True) as allowed, \
         mock.patch.object(nc._rest_session, "post", return_value=resp) as post:
        rows = asyncio.run(c.get_matches(limit=200, idx="99"))
    assert rows[0]["product_id"] == 2 and rows[0]["timestamp"] == 5 and rows[0]["builder_id"] == 0
    assert post.call_args.kwargs["json"] == {"matches": {"subaccounts": [SUB], "limit": 200, "idx": "99"}}
    assert allowed.call_args.kwargs["url"] == c._archive_url()


def test_get_matches_is_unknown_when_denied_or_rejected():
    from src.nadobro.venue import nado_client as nc
    from src.nadobro.venue.nado_client import NadoClient

    c = _client()
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=False), \
         mock.patch.object(nc._rest_session, "post", side_effect=AssertionError("denied read must not hit the network")):
        assert asyncio.run(c.get_matches(limit=5)) is None
    rejected = SimpleNamespace(json=lambda: {"status": "failure", "error_code": 1000, "error": "Too Many Requests"},
                               status_code=200, headers={}, text="", url="u")
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(nc._rest_session, "post", return_value=rejected), \
         mock.patch("src.nadobro.venue.gateway_budget.record_gateway_failure") as rec:
        assert asyncio.run(c.get_matches(limit=5)) is None
    assert rec.called and rec.call_args.args[0] == c._archive_url()      # recorded on the ARCHIVE host
    http429 = SimpleNamespace(json=lambda: {}, status_code=429, headers={}, text="", url="u")
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(nc._rest_session, "post", return_value=http429):
        assert asyncio.run(c.get_matches(limit=5)) is None


# ------------------------------------------------------------ database ---

def test_upsert_venue_positions_writes_one_conflict_free_row_per_window():
    from src.nadobro.models import database as db

    calls = []
    rows = [
        {"product_id": 2, "isolated": False, "is_long": False, "open_id": 5, "close_id": 9, "submission_idx": 9,
         "amount": 0, "max_amount": 0.0379, "total_open_amount": 0.05555, "total_close_amount": 0.05555,
         "avg_entry_price": 75755.06, "avg_exit_price": 75571.18, "liquidated_amount": 0,
         "open_fee": 0.84, "close_fee": 0.99, "realized_pnl": 10.2146, "net_funding": 0.03, "net_interest": 0,
         "open_ts": 1789566024, "update_ts": 1789567903, "open_reason": "match_orders", "close_reason": "match_orders",
         "open_digest": "0xebda", "close_digest": "0x2d6a", "is_open": False, "subaccount": SUB, "product_name": "BTC-PERP"},
        {"product_id": 0, "open_id": 1},                       # unusable: skipped, never raises
    ]
    with mock.patch.object(db, "execute", side_effect=lambda sql, params: calls.append((sql, params))):
        assert db.upsert_venue_positions(7, "mainnet", rows) == 1
    sql, params = calls[0]
    assert "ON CONFLICT (user_id, network, product_id, isolated, open_id) DO UPDATE" in sql
    assert params[0] == 7 and params[3] == 2 and params[7] == 5
    assert params[22] == datetime.fromtimestamp(1789566024, tz=timezone.utc)   # open_ts as a datetime
    assert params[-1] is False                                                  # is_open


def test_venue_pnl_windows_map_the_aggregate_row_and_report_empty_as_unknown():
    from src.nadobro.models import database as db

    with mock.patch.object(db, "query_one", return_value={"n": 0}):
        assert db.get_venue_pnl_windows(7, "mainnet") == {}
    agg = {"n": 4, "p24": "10.4981", "p7": "10.4981", "p30": "-91.3672", "pall": "18.5359",
           "wins": 300, "losses": 192, "wins_24h": 4, "losses_24h": 0, "wins_7d": 4, "losses_7d": 0,
           "wins_30d": 120, "losses_30d": 125}
    with mock.patch.object(db, "query_one", return_value=agg):
        out = db.get_venue_pnl_windows(7, "mainnet")
    assert out["pnl_windows"]["24h"] == Decimal("10.4981") and out["pnl_windows"]["all"] == Decimal("18.5359")
    assert out["wins"] == 300 and out["losses_windows"]["30d"] == 125 and out["count"] == 4


def test_attribute_venue_positions_runs_digest_window_default_and_session_rollup():
    from src.nadobro.models import database as db

    calls = []
    with mock.patch.object(db, "execute", side_effect=lambda sql, params: calls.append(sql)):
        db.attribute_venue_positions(7, "mainnet")
    assert len(calls) == 5
    assert "attributed_by = 'open_digest'" in calls[0] and "trades_mainnet" in calls[0]
    assert "attributed_by = 'session_window'" in calls[2] and "m.n = 1" in calls[2]
    assert "source = 'manual'" in calls[3] and "interval '10 minutes'" in calls[3]
    assert "venue_realized_pnl = agg.pnl" in calls[4]


def test_stamp_fill_venue_fields_only_fills_missing_values():
    from src.nadobro.models import database as db

    calls = []
    with mock.patch.object(db, "execute", side_effect=lambda sql, params: calls.append((sql, params))):
        db.stamp_fill_venue_fields("mainnet", 99, product_id=2, builder_id=2900, builder_fee_x18="3", filled_at=None)
    sql, params = calls[0]
    assert "COALESCE(product_id, 0) = 0" in sql and "COALESCE(builder_id, %s)" in sql
    assert params[-1] == 99 and params[2] == 2900


# ----------------------------------------------------------------- sync ---

def test_fetch_venue_positions_is_unknown_when_the_archive_is_denied():
    from src.nadobro.venue import nado_sync

    client = SimpleNamespace(subaccount_hex=SUB)
    with mock.patch("src.nadobro.venue.nado_archive.query_positions", side_effect=ArchiveReadUnavailable("denied")):
        assert nado_sync._fetch_venue_positions(client, "mainnet", False) is None
    with mock.patch("src.nadobro.venue.nado_archive.query_positions", return_value=[{"product_id": 2, "open_id": 5}]) as q, \
         mock.patch("src.nadobro.config.get_product_name", return_value="BTC-PERP"):
        rows = nado_sync._fetch_venue_positions(client, "mainnet", True)
    assert rows[0]["product_name"] == "BTC-PERP"
    assert q.call_args.kwargs["limit"] == nado_sync._VENUE_POSITIONS_HEAVY_LIMIT
    assert nado_sync._fetch_venue_positions(SimpleNamespace(subaccount_hex=None), "mainnet", False) is None


def test_write_matches_stamps_venue_fields_on_an_existing_product_less_row():
    """A fill already in the ledger (synced through the SDK model, product 0)
    gets its product / builder id / venue time stamped instead of skipped."""
    from src.nadobro.venue import nado_sync

    match = {"submission_idx": "81995847", "product_id": 2, "timestamp": 1789567903, "builder_id": 0,
             "builder_fee": "0", "digest": "0x2d6a", "base_filled": "1", "quote_filled": "-2", "fee": "0"}
    with mock.patch.object(nado_sync, "query_one", return_value={"id": 6636, "product_id": 0, "builder_id": None}), \
         mock.patch("src.nadobro.models.database.stamp_fill_venue_fields") as stamp, \
         mock.patch.object(nado_sync, "execute", side_effect=AssertionError("existing rows are not re-inserted")):
        inserted = nado_sync._write_matches(5776741680, "mainnet", [match])
    assert inserted == 0
    assert stamp.call_args.kwargs["product_id"] == 2 and stamp.call_args.kwargs["builder_id"] == 0
    assert stamp.call_args.kwargs["filled_at"] == datetime.fromtimestamp(1789567903, tz=timezone.utc)


def test_write_matches_inserts_builder_columns_before_leverage():
    from src.nadobro.venue import nado_sync

    execute_calls = []

    def _q(sql, *params):
        return None                                   # no recorder row, no intent, no open order

    match = {"submission_idx": "42", "product_id": 2, "product_name": "BTC-PERP", "timestamp": 1789567903,
             "builder_id": OURS, "builder_fee": "879000000000000000", "digest": "0xabc",
             "base_filled": "1000000000000000000", "quote_filled": "-75000000000000000000000",
             "fee": "1000000000000000000", "isolated": False}
    with mock.patch.object(nado_sync, "query_one", side_effect=_q), \
         mock.patch.object(nado_sync, "query_all", return_value=[]), \
         mock.patch.object(nado_sync, "execute", side_effect=lambda *a, **k: execute_calls.append(a)), \
         mock.patch.object(nado_sync, "_back_link_intent", return_value=(None, "manual", False, None, None)), \
         mock.patch.object(nado_sync, "_resolve_session_by_window", return_value=None, create=True), \
         mock.patch.object(nado_sync, "_fill_leverage_from_positions", return_value=None, create=True):
        try:
            inserted = nado_sync._write_matches(7, "mainnet", [match])
        except Exception:
            inserted = None
    insert = [c for c in execute_calls if "INSERT INTO trades_mainnet" in c[0]]
    assert insert, "no insert issued"
    sql, params = insert[0][0], insert[0][1]
    assert sql.index("builder_id") < sql.index("leverage")
    assert params[-3] == OURS and params[-2] == "879000000000000000"
    assert params[-1] is None                                         # leverage stays LAST


# ------------------------------------------------------------ renderers ---

def test_session_card_prefers_the_venue_attributed_pnl_and_hides_funding():
    from src.nadobro.handlers.performance_view import _render_session_card, session_realized_pnl

    session = {"id": 314, "strategy": "mid", "product_name": "BTC", "status": "stopped",
               "total_volume_usd": 8790.34, "total_fees_paid": 1.758, "total_funding_paid": 3.0,
               "realized_pnl": 5.256, "venue_realized_pnl": 10.4981, "venue_trade_count": 4}
    assert session_realized_pnl(session) == (Decimal("10.4981"), "venue")
    lines: list = []
    rows = _render_session_card(lines, session, 1)
    text = "\n".join(lines)
    assert "gross +$10.50" in text and "Trades 4" in text and "funding" not in text.lower()
    assert "bot est." not in text
    cbs = [btn.callback_data for row in rows for btn in row]
    assert "portfolio:session_trades:314:0" in cbs and "portfolio:share_pnl:314" in cbs
    fallback = {**session, "venue_realized_pnl": None, "venue_trade_count": None}
    lines2: list = []
    _render_session_card(lines2, fallback, 2)
    assert "gross +$5.26 (bot est.)" in "\n".join(lines2)


def test_session_trades_view_lists_the_sessions_windows():
    from src.nadobro.handlers import performance_view as pv

    session = {"id": 314, "user_id": 7, "network": "mainnet", "strategy": "mid", "product_name": "BTC",
               "status": "stopped", "started_at": datetime(2026, 9, 16, 13, 29, tzinfo=timezone.utc),
               "stopped_at": datetime(2026, 9, 16, 13, 47, tzinfo=timezone.utc),
               "total_fees_paid": 1.758, "realized_pnl": 5.256, "venue_realized_pnl": 10.4981}
    window = {"id": 9, "product_id": 2, "product_name": "BTC-PERP", "is_long": False, "isolated": False,
              "total_close_amount": "0.05555", "max_amount": "0.0379", "amount": "0",
              "avg_entry_price": "75755.06", "avg_exit_price": "75571.18", "realized_pnl": "10.2146",
              "open_fee": "0.84", "close_fee": "0.99", "is_open": False,
              "open_ts": datetime(2026, 9, 16, 13, 40, tzinfo=timezone.utc),
              "update_ts": datetime(2026, 9, 16, 14, 15, tzinfo=timezone.utc),
              "strategy_session_id": 314, "session_strategy": "mid"}
    with mock.patch.object(pv, "query_all", return_value=[session]), \
         mock.patch("src.nadobro.models.database.get_venue_positions", return_value=[window]) as g:
        text, kb = pv.render_session_trades_view(7, "mainnet", 314, 0)
    assert g.call_args.kwargs["session_id"] == 314
    assert "MID" in text and "1 trade" in text and "Realized 🟢 +$10.50" in text
    assert "$75,755.06 → $75,571.18" in text and "mid #314" in text
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "portfolio:share_pnl:vp:9" in cbs and "portfolio:performance" in cbs
    with mock.patch.object(pv, "query_all", return_value=[]):
        text, _ = pv.render_session_trades_view(7, "mainnet", 999, 0)
    assert "Session not found" in text
