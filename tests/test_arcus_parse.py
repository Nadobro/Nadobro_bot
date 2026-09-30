"""venue/arcus/parse.py — tolerant, fail-closed parsers (02 §7.1 / §12.6).

Every parser is exercised over its fixture (every field), plus the fail-closed
cases: a missing/invalid required field -> ``ArcusSchemaError`` with the dotted
``where`` AND a schema count; never an empty result. Real public testnet
captures (``fixtures/arcus/captured``, 2026-09-30) must parse too.
"""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from arcus_helpers import ADDR, ADDR_MIXED, PUB, REF, load_fixture
from src.nadobro.venue.arcus import errors as E
from src.nadobro.venue.arcus import parse as P
from src.nadobro.venue.arcus.errors import ArcusSchemaError, schema_error_counts
from src.nadobro.venue.arcus.types import ArcusAccountRef, Side, Tif

D = Decimal


@pytest.fixture(autouse=True)
def _fresh_schema_counts():
    E._reset_schema_errors_for_tests()
    yield
    E._reset_schema_errors_for_tests()


def _raises(where: str, fn, *args, **kwargs):
    with pytest.raises(ArcusSchemaError) as exc:
        fn(*args, **kwargs)
    assert exc.value.where == where, exc.value.where
    assert schema_error_counts().get(where, 0) >= 1
    return exc.value


# --- primitives ------------------------------------------------------------------------


def test_dec_accepts_strings_ints_decimals_only():
    assert P._dec("84517.3", "x") == D("84517.3")
    assert P._dec("-0.0001", "x") == D("-0.0001")
    assert P._dec("1E-8", "x") == D("1E-8")
    assert P._dec(5, "x") == D(5)
    assert P._dec(D("2.5"), "x") == D("2.5")
    for bad in ("NaN", "Infinity", "-inf", "sNaN", "", " 1", "1,000", "0x10", True, 1.5, None, [], "1e999"):
        with pytest.raises(ArcusSchemaError):
            P._dec(bad, "x")
    with pytest.raises(ArcusSchemaError):
        P._dec("0", "x", gt=0)
    with pytest.raises(ArcusSchemaError):
        P._dec("-1", "x", ge=0)


def test_dec_opt_zero_rule_is_numeric():
    assert P._dec_opt(None, "x") is None and P._dec_opt("", "x") is None
    assert P._dec_opt("0.0000", "x", zero_is_none=True) is None
    assert P._dec_opt("0", "x", zero_is_none=True) is None
    assert P._dec_opt("0.0001", "x", zero_is_none=True) == D("0.0001")
    assert P._dec_opt("0", "x") == D("0")


def test_int_rules():
    assert P._int(5, "x") == 5 and P._int("1793456000000000", "x") == 1793456000000000
    assert P._int(D("1790000000000000"), "x") == 1790000000000000
    for bad in (True, False, 1.0, D("1.5"), "1.0", "-5", "", None, "12a"):
        with pytest.raises(ArcusSchemaError):
            P._int(bad, "x")
    with pytest.raises(ArcusSchemaError):
        P._int(10, "x", le=9)


def test_tif_mapping():
    assert P.tif_from_wire("GTC", "o.t") is Tif.GTT and P.tif_from_wire("GTT", "o.t") is Tif.GTT
    assert P.tif_from_wire("IOC", "o.t") is Tif.IOC and P.tif_from_wire("ALO", "o.t") is Tif.ALO
    assert P.tif_from_wire("FOK", "o.t") is Tif.FOK and P.tif_from_wire(None, "o.t") is None
    assert P.tif_from_wire("DAY", "o.t") is None and P.tif_from_wire(3, "o.t") is None
    assert schema_error_counts() == {"o.t": 2}


# --- time / markets -----------------------------------------------------------------------


def test_parse_time():
    assert P.parse_time(load_fixture("time.json")) == 1790000000000000000
    assert P.parse_time(load_fixture("captured/testnet_time_20260930.json")) > 10**18
    _raises("time.timeNs", P.parse_time, {"timeNs": 1790000000000000})  # µs, not ns
    _raises("time.timeNs", P.parse_time, {})
    _raises("time", P.parse_time, None)


def test_parse_markets_payload():
    rows = P.parse_markets_payload(load_fixture("markets_testnet_subset.json"))
    assert [r["marketDisplayName"] for r in rows] == ["BTC-USD", "ETH-USD", "SOL-USD", "AMD-USD", "F-USD"]
    _raises("markets.markets", P.parse_markets_payload, {"markets": {}})
    _raises("markets.markets", P.parse_markets_payload, {})
    _raises("markets.row", P.parse_markets_payload, {"markets": ["x"]})
    assert P.parse_markets_payload({"markets": []}) == []


def test_field_tables_cover_required():
    for shape, required in P.REQUIRED_FIELDS.items():
        # every field a parser requires is a documented field of that shape
        assert required <= P.FIELD_SETS[shape], shape
    live = load_fixture("markets_testnet_subset.json")["markets"][0]
    assert "lastTradePrice" not in P.FIELD_SETS["market"] and "lastTradePrice" in live  # undocumented live field


# --- orders ---------------------------------------------------------------------------------


def test_parse_open_orders_page():
    rows = P.parse_open_orders_payload(load_fixture("open_orders_page.json"))
    gtc, ro = rows
    assert gtc.order_id == "a1b2c3d4e5f67890" and gtc.client_id == "nb7ps_2s-1"
    assert (gtc.market_id, gtc.ticker, gtc.side, gtc.status, gtc.state) == (1, "BTC-USD", Side.BUY, "OPEN", "OPEN")
    assert gtc.tif is Tif.GTT and gtc.reduce_only is False
    assert (gtc.price, gtc.original_size, gtc.remaining_size, gtc.filled_size) == (
        D("84517.3"), D("0.0001"), D("0.0001"), D("0"),
    )
    assert gtc.avg_fill_price is None and gtc.rejection_reason is None and gtc.cancel_reason is None
    assert (gtc.created_us, gtc.updated_us, gtc.sequence_number) == (1790000000123456, 1790000000150000, None)
    assert ro.reduce_only is True and ro.state == "PARTIALLY_FILLED" and ro.status == "PARTIALLY_FILLED"
    assert ro.tif is Tif.ALO and ro.side is Side.SELL and ro.avg_fill_price == D("130.25")
    assert ro.filled_size == D("0.4") and schema_error_counts() == {}


def test_open_orders_empty_and_containers():
    assert P.parse_open_orders_payload(load_fixture("open_orders_empty.json")) == []
    assert P.parse_open_orders_payload({"orders": []}) == []  # no `total` (mainnet <= testnet-v1.10.13)
    many = load_fixture("open_orders_page.json")
    many["total"] = 99  # a disagreeing total is ignored
    assert len(P.parse_open_orders_payload(many)) == 2
    for body in ({}, {"orders": "x"}, {"total": 0}, None, []):
        with pytest.raises(ArcusSchemaError):
            P.parse_open_orders_payload(body)


def test_order_row_tolerance_and_drift():
    row = load_fixture("order_ok.json")
    row["newField"] = {"anything": 1}
    row["timeInForce"] = "DAY"
    row["status"] = "someNewStatus"
    out = P.parse_order_row(row)
    assert out.tif is None and out.status == "SOMENEWSTATUS"
    assert schema_error_counts() == {"order.timeInForce": 1}
    bad_filled = load_fixture("order_ok.json")
    bad_filled["filledSize"] = "0.5"
    assert P.parse_order_row(bad_filled).filled_size == D("0.5")
    assert schema_error_counts()["order.filledSize"] == 1


@pytest.mark.parametrize(
    "field,value,where",
    [
        ("orderId", None, "order.orderId"),
        ("orderId", "bad id", "order.orderId"),
        ("clientId", "bad id!", "order.clientId"),
        ("marketId", True, "order.marketId"),
        ("marketDisplayName", "", "order.marketDisplayName"),
        ("side", "LONG", "order.side"),
        ("status", None, "order.status"),
        ("price", "-1", "order.price"),
        ("originalSize", "0", "order.originalSize"),
        ("remainingSize", None, "order.remainingSize"),
        ("updatedAt", 1790000000150, "order.updatedAt"),
        ("updatedAt", None, "order.updatedAt"),
        ("reduceOnly", "true", "order.reduceOnly"),
        ("createdAt", 1790000000, "order.createdAt"),
        ("price", "NaN", "order.price"),
    ],
)
def test_order_row_fail_closed(field, value, where):
    row = load_fixture("order_ok.json")
    if value is None:
        row.pop(field, None)
    else:
        row[field] = value
    _raises(where, P.parse_order_row, row)


# --- fills / funding --------------------------------------------------------------------------


def test_parse_fills_page():
    fills = P.parse_fills_payload(load_fixture("fills_page.json"), ref=REF)
    assert [f.trade_id for f in fills] == ["t-0000000000000003", "t-0000000000000002", "t-0000000000000001"]
    maker, taker, liq = fills
    assert all(f.client_id is None and f.source == "rest" for f in fills)
    assert maker.role == "MAKER" and maker.fee == D("-0.0001") and maker.fee < 0
    assert taker.role == "TAKER" and taker.fee == D("0.0038") and taker.closed_pnl == D("0.12")
    assert taker.position_effect == "CLOSE_LONG" and taker.side is Side.SELL
    assert (taker.size, taker.price, taker.market_id, taker.ticker) == (D("0.4"), D("130.25"), 3, "SOL-USD")
    assert liq.liquidation_method == "LIQUIDATION" and liq.closed_pnl == D("0")
    assert liq.order_id == "liq:00000000000000aa"  # synthetic liq ids parse (no ORDER_ID_RE)
    assert maker.liquidation_method is None and maker.closed_pnl is None
    assert maker.created_us == 1790000000170000 and maker.sequence_number is None


def test_fill_row_ws_keeps_client_id_rest_drops_it():
    row = dict(load_fixture("fills_page.json")["fills"][0], clientId="nb7ps_2s-3", sequenceNumber=7)
    ws = P.parse_fill_row(row, source="ws", ref=REF)
    assert ws.client_id == "nb7ps_2s-3" and ws.sequence_number == 7 and ws.source == "ws"
    assert P.parse_fill_row(row, source="rest", ref=REF).client_id is None
    with pytest.raises(ValueError):
        P.parse_fill_row(row, source="other", ref=REF)  # type: ignore[arg-type]


def test_fill_row_echo_and_fail_closed():
    base = load_fixture("fills_page.json")["fills"][0]
    other_ref = ArcusAccountRef("testnet", ADDR, 1)
    _raises("fill.accountIndex", P.parse_fill_row, base, source="rest", ref=other_ref)
    _raises("fill.address", P.parse_fill_row, dict(base, address="0x" + "1" * 40), source="rest", ref=REF)
    # echo absent -> accepted ("Only present on REST and snapshot responses")
    no_echo = {k: v for k, v in base.items() if k not in ("address", "accountIndex")}
    assert P.parse_fill_row(no_echo, source="ws", ref=REF).trade_id == base["tradeId"]
    # checksum-case echo is fine
    assert P.parse_fill_row(dict(base, address=ADDR_MIXED), source="rest", ref=REF).trade_id
    for field, value, where in [
        ("size", "0", "fill.size"),
        ("price", "0", "fill.price"),
        ("fee", None, "fill.fee"),
        ("fee", "NaN", "fill.fee"),
        ("role", "HOLDER", "fill.role"),
        ("createdAt", 1790000000160, "fill.createdAt"),
        ("tradeId", "", "fill.tradeId"),
        ("liquidation", {"liquidatedUser": "x"}, "fill.liquidation.method"),
        ("liquidation", "LIQUIDATION", "fill.liquidation"),
    ]:
        row = dict(base)
        if value is None:
            row.pop(field)
        else:
            row[field] = value
        _raises(where, P.parse_fill_row, row, source="rest", ref=REF)


def test_parse_funding_page():
    rows = P.parse_funding_payload(load_fixture("funding_page.json"))
    assert [r.time_us for r in rows] == [1790003600000000, 1790000000000000]  # newest-first
    sol, btc = rows
    assert sol.payment == D("-0.0030") and sol.size == D("-0.5") and sol.funding_rate == D("-0.00005")
    assert btc.payment == D("0.0012") and btc.market_id == 1 and btc.ticker == "BTC-USD"


def test_list_containers():
    assert P.parse_funding_payload({"fundingPayments": []}) == []
    _raises("funding.fundingPayments", P.parse_funding_payload, {"funding": []})
    _raises("fills.fills", P.parse_fills_payload, {"total": 0}, ref=REF)
    assert P.parse_fills_payload({"fills": []}, ref=REF) == []
    assert P.parse_candles({"candles": []}, final_only=True) == []
    assert P.parse_api_keys({"apiKeys": []}) == []
    assert P.parse_leverages({"leverages": [], "address": ADDR, "accountIndex": 0}, ref=REF) == []
    assert P.parse_mids({"mids": {}, "globalSequenceId": 1}) == {}
    for fn in (P.parse_funding_payload, P.parse_open_orders_payload):
        for body in (None, {}, {"total": 0}):
            with pytest.raises(ArcusSchemaError):
                fn(body)


# --- positions / account / rate limit ----------------------------------------------------------


def test_parse_positions_ok():
    pos = P.parse_positions_payload(load_fixture("positions_ok.json"), ref=REF)
    assert set(pos) == {1, 3}
    btc, sol = pos[1], pos[3]
    assert btc.size == D("0.0001") and btc.average_entry_price == D("84500") and btc.leverage == D("40")
    assert btc.margin_mode == "CROSS" and btc.margin_used == D("0.21")
    assert btc.position_value_notional == D("8.45") and btc.venue_margin_delta == D("0.01")
    assert btc.mark_px == D("84510.2") and btc.sequence_number == 40
    assert sol.size == D("-0.5") and sol.mark_px is None  # numeric "0" -> no mark


def test_positions_empty_and_flat_rows():
    assert P.parse_positions_payload(load_fixture("positions_empty.json"), ref=REF) == {}
    body = load_fixture("positions_ok.json")
    body["positions"]["1"]["size"] = "0"
    assert set(P.parse_positions_payload(body, ref=REF)) == {3}
    _raises("positions.positions", P.parse_positions_payload, {"total": 0}, ref=REF)


def test_positions_fail_closed():
    good = load_fixture("positions_ok.json")
    cases = []
    b = copy.deepcopy(good); b["positions"]["1"]["accountIndex"] = 1; cases.append((b, "position.accountIndex"))
    b = copy.deepcopy(good); b["positions"]["1"]["size"] = "-0.0001"; cases.append((b, "position.sign"))
    b = copy.deepcopy(good); b["positions"]["3"]["size"] = "0.5"; cases.append((b, "position.sign"))
    b = copy.deepcopy(good); b["positions"]["2"] = b["positions"].pop("1"); cases.append((b, "positions.key"))
    b = copy.deepcopy(good); b["positions"]["abc"] = b["positions"].pop("1"); cases.append((b, "positions.key"))
    b = copy.deepcopy(good); b["positions"]["1"]["side"] = "FLAT"; cases.append((b, "position.side"))
    b = copy.deepcopy(good); b["positions"]["1"]["marginMode"] = "PORTFOLIO"; cases.append((b, "position.marginMode"))
    b = copy.deepcopy(good); del b["positions"]["1"]["address"]; cases.append((b, "position.address"))
    b = copy.deepcopy(good); b["positions"]["1"]["address"] = "0x" + "2" * 40; cases.append((b, "position.address"))
    b = copy.deepcopy(good); b["positions"]["1"]["averageEntryPrice"] = "-1"; cases.append((b, "position.averageEntryPrice"))
    for body, where in cases:
        _raises(where, P.parse_positions_payload, body, ref=REF)


def test_parse_account_ok():
    acct = P.parse_account(load_fixture("account_ok.json"), ref=REF, now_mono=77.0)
    assert (acct.equity, acct.free_collateral, acct.net_quote_balance, acct.net_deposits) == (
        D("1000.1"), D("950"), D("990.5"), D("1000"),
    )
    assert acct.sequence_number == 42 and acct.as_of_mono == 77.0
    assert set(acct.positions) == {1} and acct.positions[1].size == D("0.0001")
    with pytest.raises(TypeError):
        acct.positions[2] = acct.positions[1]  # type: ignore[index]


def test_account_fail_closed():
    good = load_fixture("account_ok.json")
    for field, value, where in [
        ("accountIndex", 1, "account.accountIndex"),
        ("address", "0x" + "3" * 40, "account.address"),
        ("address", "not-an-address", "account.address"),
        ("equity", None, "account.equity"),
        ("sequenceNumber", -1, "account.sequenceNumber"),
        ("positions", None, "account.positions"),
    ]:
        body = copy.deepcopy(good)
        if value is None:
            body.pop(field)
        else:
            body[field] = value
        _raises(where, P.parse_account, body, ref=REF, now_mono=1.0)


def test_parse_rate_limit():
    order, cancel = P.parse_rate_limit(load_fixture("rate_limit_testnet.json"), ref=REF, now_mono=5.0)
    assert (order.remaining, order.cap, order.used, order.next_available_ms, order.source) == (20000, 20000, 0, 0, "rest")
    assert (cancel.remaining, cancel.cap, cancel.as_of_mono) == (40000, 40000, 5.0)
    body = load_fixture("rate_limit_testnet.json")
    body["order"]["used"] = 25000  # over the cap: remaining goes negative, kept
    assert P.parse_rate_limit(body, ref=REF, now_mono=1.0)[0].remaining == -5000


def test_rate_limit_echo_mismatch_and_drift():
    good = load_fixture("rate_limit_testnet.json")
    _raises("rateLimit.accountIndex", P.parse_rate_limit, dict(good, accountIndex=1), ref=REF, now_mono=1.0)
    _raises("rateLimit.address", P.parse_rate_limit, dict(good, address="0x" + "4" * 40), ref=REF, now_mono=1.0)
    _raises("rateLimit.address", P.parse_rate_limit, dict(good, address="zz"), ref=REF, now_mono=1.0)
    bad = copy.deepcopy(good); bad["cancel"]["cap"] = "x"
    _raises("rateLimit.cancel.cap", P.parse_rate_limit, bad, ref=REF, now_mono=1.0)
    bad = copy.deepcopy(good); del bad["order"]
    _raises("rateLimit.order", P.parse_rate_limit, bad, ref=REF, now_mono=1.0)


def test_address_echo_case_insensitive():
    for name, fn, kwargs in [
        ("rate_limit_testnet.json", P.parse_rate_limit, {"now_mono": 1.0}),
        ("account_ok.json", P.parse_account, {"now_mono": 1.0}),
    ]:
        body = load_fixture(name)
        body["address"] = ADDR_MIXED
        fn(body, ref=REF, **kwargs)
    body = load_fixture("positions_ok.json")
    for row in body["positions"].values():
        row["address"] = ADDR_MIXED
    assert set(P.parse_positions_payload(body, ref=REF)) == {1, 3}


# --- api keys / compliance / leverages ------------------------------------------------------------


def test_parse_api_keys_ok():
    ours, all_key, deleted = P.parse_api_keys(load_fixture("api_keys_ok.json"))
    assert ours.api_key == PUB and ours.address == ADDR and ours.status == "ACTIVE"
    assert ours.all_subaccounts is False and ours.account_index == 0 and ours.covers_account(0)
    assert not ours.covers_account(1) and ours.permissions == () and ours.api_wallet_name == "nadobro-ab12"
    assert ours.valid_until_ms == 1798640000000 and ours.created_us == 1790000000000000
    assert all_key.all_subaccounts is True and all_key.account_index is None and all_key.covers_account(0)
    assert all_key.api_wallet_name is None and all_key.valid_until_ms == 0
    assert deleted.status == "DELETED" and deleted.permissions == ("withdraw",) and not deleted.covers_account(0)
    assert P.parse_api_keys(load_fixture("api_keys_empty.json")) == []


def test_api_key_scope_combinations():
    base = load_fixture("api_keys_ok.json")["apiKeys"][0]

    def entry(**scope):
        row = {k: v for k, v in base.items() if k not in ("allSubaccounts", "accountIndex")}
        row.update(scope)
        return {"apiKeys": [row]}

    assert P.parse_api_keys(entry(allSubaccounts=True))[0].all_subaccounts is True
    assert P.parse_api_keys(entry(allSubaccounts=False, accountIndex=0))[0].account_index == 0
    assert P.parse_api_keys(entry(accountIndex=0))[0].account_index == 0
    for scope in ({}, {"allSubaccounts": True, "accountIndex": 0}, {"allSubaccounts": False},
                  {"allSubaccounts": "yes"}, {"accountIndex": 10}):
        _raises("apiKeys.scope", P.parse_api_keys, entry(**scope))


def test_api_keys_whole_list_fails_on_one_bad_entry():
    body = load_fixture("api_keys_ok.json")
    body["apiKeys"][2]["status"] = "REVOKED"  # closed enum
    _raises("apiKeys.status", P.parse_api_keys, body)
    body = load_fixture("api_keys_ok.json")
    body["apiKeys"][1]["apiKey"] = "xyz"
    _raises("apiKeys.apiKey", P.parse_api_keys, body)
    body = load_fixture("api_keys_ok.json")
    body["apiKeys"][0]["permissions"] = "withdraw"
    _raises("apiKeys.permissions", P.parse_api_keys, body)
    body = load_fixture("api_keys_ok.json")
    body["apiKeys"][0]["validUntil"] = -1
    _raises("apiKeys.validUntil", P.parse_api_keys, body)
    body = load_fixture("api_keys_ok.json")
    body["apiKeys"][0]["apiKey"] = PUB.upper()  # hex case is normalised
    assert P.parse_api_keys(body)[0].api_key == PUB


def test_parse_compliance():
    geo_only = P.parse_compliance(load_fixture("compliance_geo_only.json"))
    assert geo_only == P.ComplianceView("XX", False, False, None, None)
    blocked = P.parse_compliance(load_fixture("compliance_blocked.json"))
    assert blocked.address_status == "BLOCKED" and blocked.reason == "screening"
    body = load_fixture("compliance_blocked.json")
    body["address"]["status"] = "PENDING"
    _raises("compliance.address.status", P.parse_compliance, body)
    body = load_fixture("compliance_geo_only.json")
    body["geo"]["country"] = ""
    assert P.parse_compliance(body).country == ""
    del body["geo"]["bypassed"]
    _raises("compliance.geo.bypassed", P.parse_compliance, body)
    _raises("compliance.geo", P.parse_compliance, {})


def test_parse_leverages():
    (entry,) = P.parse_leverages(load_fixture("leverages_ok.json"), ref=REF)
    assert entry == P.LeverageEntry(1, 40, False, "CROSS")
    body = load_fixture("leverages_ok.json")
    body["leverages"][0]["marginMode"] = "ISOLATED"  # disagrees with isolated=false
    _raises("leverages.marginMode", P.parse_leverages, body, ref=REF)
    _raises("leverages.accountIndex", P.parse_leverages, dict(load_fixture("leverages_ok.json"), accountIndex=2), ref=REF)
    body = load_fixture("leverages_ok.json")
    body["leverages"][0]["leverage"] = 0
    _raises("leverages.leverage", P.parse_leverages, body, ref=REF)


# --- market data ------------------------------------------------------------------------------------


def test_parse_bbo():
    bbo = P.parse_bbo(load_fixture("bbo_btc.json"))
    assert (bbo.bid, bbo.ask, bbo.bid_size, bbo.ask_size, bbo.timestamp_us) == (
        D("84594.4"), D("84594.5"), D("0.5"), D("0.4"), 1790000000000000,
    )
    empty = P.parse_bbo(load_fixture("bbo_empty_side.json"))
    assert empty.ask is None and empty.ask_size is None and empty.bid == D("84594.4")
    _raises("bbo.crossed", P.parse_bbo, load_fixture("bbo_crossed.json"))
    _raises("bbo.bestBid.price", P.parse_bbo, {"bestBid": {"price": "0", "size": "1"}, "bestAsk": None})
    assert P.parse_bbo({}).bid is None


def test_parse_mids_drops_empty():
    mids = P.parse_mids(load_fixture("mids.json"))
    assert mids == {"BTC-USD": D("84636.65"), "SOL-USD": D("122.6")}
    _raises("mids.value", P.parse_mids, {"mids": {"BTC-USD": "0"}})
    _raises("mids.value", P.parse_mids, {"mids": {"BTC-USD": 1}})
    _raises("mids.mids", P.parse_mids, {"globalSequenceId": 1})


def test_parse_prices():
    prices = P.parse_prices(load_fixture("prices.json"))
    btc, eth = prices["BTC-USD"], prices["ETH-USD"]
    assert (btc.oracle, btc.mark, btc.sequencer, btc.market_key) == (D("84532.4"), D("84321.1"), 42, 1)
    assert eth.oracle is None and eth.mark is None
    _raises("prices.key", P.parse_prices, {"x": {"marketDisplayName": "BTC-USD", "oraclePrice": "1", "markPrice": "1", "sequencer": 1}})
    dup = load_fixture("prices.json")
    dup["9"] = dict(dup["1"])
    _raises("prices.duplicate", P.parse_prices, dup)


def test_zero_prices_numeric():
    body = load_fixture("prices.json")
    body["1"]["oraclePrice"] = "0.0000"
    assert P.parse_prices(body)["BTC-USD"].oracle is None
    body["1"]["oraclePrice"] = "0.0001"
    assert P.parse_prices(body)["BTC-USD"].oracle == D("0.0001")
    positions = load_fixture("positions_ok.json")
    positions["positions"]["1"]["markPx"] = "0.0000"
    assert P.parse_positions_payload(positions, ref=REF)[1].mark_px is None


def test_parse_l2_sorted():
    book = P.parse_l2(load_fixture("l2_btc.json"))
    assert [p for p, _ in book.bids] == [D("84594.4"), D("84592.1"), D("84590.0")]
    assert [p for p, _ in book.asks] == [D("84594.5"), D("84596.0"), D("84599.9")]
    assert book.last_sequence_id == 10 and book.timestamp_us == 1790000000000000
    _raises("l2.bids", P.parse_l2, {"bids": [["1"]], "asks": []})
    _raises("l2.asks.size", P.parse_l2, {"bids": [], "asks": [["1", "0"]]})
    _raises("l2.asks", P.parse_l2, {"bids": []})


def test_parse_candles_sorted_final_only():
    body = load_fixture("candles_newest_first.json")
    final = P.parse_candles(body, final_only=True)
    assert [c.open_time_us for c in final] == [1790000000000000, 1790000060000000, 1790000120000000]
    assert all(c.is_final for c in final)
    everything = P.parse_candles(body, final_only=False)
    assert len(everything) == 4 and everything[-1].is_final is False
    first = everything[0]
    assert (first.market_id, first.ticker, first.timeframe, first.close, first.trade_count) == (
        1, "BTC-USD", "1m", D("84510.0"), 7,
    )
    assert first.volume == D("0.012") and first.notional_volume == D("1014.2")


def test_candles_duplicate_keeps_final():
    body = load_fixture("candles_newest_first.json")
    forming = dict(body["candles"][1], isFinal=False, close="1.0")
    body["candles"].append(forming)  # duplicate openTime, not final -> the final row wins
    bars = P.parse_candles(body, final_only=False)
    same = [c for c in bars if c.open_time_us == 1790000120000000]
    assert len(same) == 1 and same[0].is_final and same[0].close == D("84590.0")
    assert schema_error_counts()["candles.dup"] == 1
    body2 = load_fixture("candles_newest_first.json")
    body2["candles"].append(dict(body2["candles"][0], close="99.0"))  # both non-final -> the later one
    later = [c for c in P.parse_candles(body2, final_only=False) if c.open_time_us == 1790000180000000]
    assert later[0].close == D("99.0")


def test_real_testnet_captures_parse():
    """Public testnet data captured 2026-09-30 (unauthenticated GETs)."""
    prices = P.parse_prices(load_fixture("captured/testnet_prices_20260930.json"))
    assert {"BTC-USD", "ETH-USD", "SOL-USD"} <= set(prices)
    assert prices["BTC-USD"].market_key == 1  # live keys ARE market ids on testnet today
    mids = P.parse_mids(load_fixture("captured/testnet_mids_20260930.json"))
    assert mids["BTC-USD"] > 0
    bbo = P.parse_bbo(load_fixture("captured/testnet_bbo_btc_20260930.json"))
    assert bbo.bid is not None and bbo.ask is not None and bbo.bid < bbo.ask
    book = P.parse_l2(load_fixture("captured/testnet_l2_btc_n5_20260930.json"))
    assert len(book.bids) == 5 and book.bids[0][0] < book.asks[0][0]
    raw = load_fixture("captured/testnet_candles_btc_1m_20260930.json")["candles"]
    assert raw[0]["openTime"] > raw[-1]["openTime"]  # live order is newest-first (docs say oldest-first)
    bars = P.parse_candles({"candles": raw}, final_only=False)
    assert [c.open_time_us for c in bars] == sorted(c["openTime"] for c in raw)
    assert all(c.is_final for c in P.parse_candles({"candles": raw}, final_only=True))
    assert schema_error_counts() == {}
