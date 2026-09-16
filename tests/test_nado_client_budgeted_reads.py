"""Every gateway read goes through the budget AND feeds the breaker (2026-09-16
storm). Three SDK paths bypassed both — positions per subaccount, the single
market price, and the MarginManager account summary — so they kept firing
into a 429 storm and never opened the circuit; a JSON-bodied 429 on the REST
path was returned as data and never recorded either.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

from src.nadobro.venue import nado_client as nc
from src.nadobro.venue.nado_client import NadoClient

SUB = "0x" + "11" * 32


def _client():
    c = NadoClient(private_key="0xabc", network="mainnet")
    c.subaccount_hex = SUB
    c._initialized = True
    return c


# ---------------------------------------------------------------- JSON 429 ---

def test_a_json_rate_limit_body_on_the_rest_path_is_recorded():
    c = _client()
    resp = SimpleNamespace(json=lambda: {"status": "failure", "error_code": 1000, "error": "Too Many Requests"},
                           status_code=200, headers={}, text="", url="u")
    recorded: list = []
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_record_gateway_error", side_effect=lambda e: recorded.append(str(e))), \
         mock.patch.object(nc._rest_session, "get", return_value=resp):
        data = c._query_rest("status")
    assert data["status"] == "failure"
    assert recorded and "error_code=1000" in recorded[0]


def test_payload_recogniser():
    assert nc._payload_is_rate_limited({"status": "failure", "error_code": 1000, "error": "Too Many Requests"})
    assert nc._payload_is_rate_limited({"status": "failure", "error": "Too Many Requests"})
    assert not nc._payload_is_rate_limited({"status": "success", "data": {}})
    assert not nc._payload_is_rate_limited({"status": "failure", "error_code": 2000, "error": "bad order"})
    assert not nc._payload_is_rate_limited(None)


# ------------------------------------------------------------ market price ---

def test_market_price_sdk_read_is_budgeted_and_denied_reads_are_unknown():
    c = _client()
    hits: list = []
    c.client = SimpleNamespace(context=SimpleNamespace(engine_client=SimpleNamespace(
        get_market_price=lambda pid: hits.append(pid) or SimpleNamespace(bid_x18=10**18, ask_x18=2 * 10**18))))
    with nc._caches_lock:
        nc._price_cache.pop("mainnet:1", None)
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=False) as allowed, \
         mock.patch.object(NadoClient, "_query_rest", side_effect=AssertionError("REST must not run on a denied read")):
        assert c.get_market_price(1) == {"bid": 0, "ask": 0, "mid": 0}
    assert allowed.call_args.kwargs.get("weight") == 1
    assert hits == []


def test_market_price_sdk_failure_feeds_the_breaker():
    pytest.importorskip("nado_protocol")
    c = _client()
    c.client = SimpleNamespace(context=SimpleNamespace(engine_client=SimpleNamespace(
        get_market_price=mock.Mock(side_effect=RuntimeError('{"error_code":1000,"error":"Too Many Requests"}')))))
    with nc._caches_lock:
        nc._price_cache.pop("mainnet:1", None)
    recorded: list = []
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None) as released, \
         mock.patch.object(NadoClient, "_record_gateway_error", side_effect=lambda e: recorded.append(e)), \
         mock.patch.object(NadoClient, "_query_rest", return_value=None):
        c.get_market_price(1)
    assert len(recorded) == 1
    assert released.called


# --------------------------------------------------------------- positions ---

def _positions_engine(info, calls: list):
    def _read(sub):
        calls.append(sub)
        if isinstance(info, Exception):
            raise info
        return info
    return SimpleNamespace(context=SimpleNamespace(engine_client=SimpleNamespace(get_subaccount_info=_read)))


def test_positions_read_is_budgeted_and_a_denied_read_never_hits_the_venue():
    c = _client()
    calls: list = []
    c.client = _positions_engine(SimpleNamespace(perp_balances=[]), calls)
    nc._positions_fallback_cache.clear()
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=False) as allowed, \
         mock.patch.object(NadoClient, "_query_rest", side_effect=AssertionError("REST must not run on a denied read")):
        c._positions_for_subaccount_hex(SUB, allow_empty_cache_fallback=True)
    assert allowed.call_args.kwargs.get("weight") == 2
    assert calls == []


def test_a_successful_empty_sdk_positions_read_is_not_re_asked_over_rest():
    c = _client()
    calls: list = []
    c.client = _positions_engine(SimpleNamespace(perp_balances=[]), calls)
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_extract_positions_from_sdk_info", return_value=[]), \
         mock.patch.object(NadoClient, "_query_rest", side_effect=AssertionError("redundant REST re-read")):
        assert c._positions_for_subaccount_hex(SUB, allow_empty_cache_fallback=True) == []
    assert calls == [SUB]


def test_positions_sdk_failure_feeds_the_breaker_then_tries_rest():
    c = _client()
    calls: list = []
    c.client = _positions_engine(RuntimeError('{"error_code":1000,"error":"Too Many Requests"}'), calls)
    nc._positions_fallback_cache.clear()
    recorded: list = []
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_record_gateway_error", side_effect=lambda e: recorded.append(e)), \
         mock.patch.object(NadoClient, "_query_rest", return_value=None) as rest:
        c._positions_for_subaccount_hex(SUB, allow_empty_cache_fallback=True)
    assert len(recorded) == 1
    assert rest.called


# --------------------------------------------------------- account summary ---

def test_account_summary_reserves_the_margin_managers_hidden_reads():
    c = _client()
    seen: list = []

    def _allowed(weight=1.0, **k):
        seen.append((weight, k.get("url")))
        return True

    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", side_effect=_allowed), \
         mock.patch("nado_protocol.utils.margin_manager.MarginManager") as mm:
        mm.from_client.return_value.calculate_account_summary.return_value = {"ok": 1}
        out = asyncio.run(c.calculate_account_summary(ts=1))
    assert out == {"ok": 1}
    assert seen[0][0] == 12                      # subaccount_info (2) + isolated_positions (10)
    assert seen[1][1] == c._archive_url()        # the indexer snapshots query, archive lane


def test_account_summary_denied_budget_raises_a_throttle_without_calling_the_venue():
    c = _client()
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", return_value=False), \
         mock.patch("nado_protocol.utils.margin_manager.MarginManager") as mm:
        with pytest.raises(RuntimeError, match="venue throttled"):
            asyncio.run(c.calculate_account_summary(ts=1))
        assert not mm.from_client.called


def test_account_summary_failure_feeds_the_breaker():
    c = _client()
    recorded: list = []
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_record_gateway_error", side_effect=lambda e: recorded.append(e)), \
         mock.patch("nado_protocol.utils.margin_manager.MarginManager") as mm:
        mm.from_client.side_effect = RuntimeError("Too Many Requests")
        with pytest.raises(RuntimeError):
            asyncio.run(c.calculate_account_summary(ts=1))
    assert len(recorded) == 1


# ------------------------------------------------------ isolated discovery ---

def test_isolated_discovery_is_cached_and_serves_the_last_list_on_failure():
    c = _client()
    nc._isolated_subaccounts_cache.clear()
    rows = [{"subaccount": SUB, "isolated_subaccount": "0x" + "22" * 32, "product_id": 7}]
    with mock.patch("src.nadobro.venue.nado_archive.query_isolated_subaccounts_for_parent", return_value=rows) as q:
        assert c._isolated_subaccounts() == [("0x" + "22" * 32, 7)]
        assert c._isolated_subaccounts() == [("0x" + "22" * 32, 7)]      # cached: one archive call
        assert q.call_count == 1
        c._isolated_subaccounts(refresh=True)
        assert q.call_count == 2
    with mock.patch("src.nadobro.venue.nado_archive.query_isolated_subaccounts_for_parent",
                    side_effect=RuntimeError("archive 429")):
        assert c._isolated_subaccounts(refresh=True) == [("0x" + "22" * 32, 7)]   # last known list
        nc._isolated_subaccounts_cache.clear()
        with pytest.raises(RuntimeError):
            c._isolated_subaccounts(refresh=True)                              # nothing cached: unknown
        assert c._isolated_subaccount_hexes(refresh=True) == []               # positions readers keep []


# ------------------------------------------------------------- bulk prices ---

def _all_prices_client():
    c = NadoClient.from_address("0x" + "b" * 40, network="mainnet")
    with nc._caches_lock:
        nc._ALL_PRICES_CACHE.pop("mainnet", None)
    return c


def _envelope(pid=1, bid=10**18, ask=2 * 10**18):
    return {"status": "success", "data": {"market_prices": [{"product_id": pid, "bid_x18": str(bid), "ask_x18": str(ask)}]}}


def test_bulk_prices_use_the_edge_lane_first_and_never_the_live_lane_when_it_answers():
    c = _all_prices_client()
    with mock.patch("src.nadobro.venue.nado_client.get_perp_products", return_value=["BTC-PERP"]), \
         mock.patch("src.nadobro.venue.nado_client.get_product_id", return_value=1), \
         mock.patch("src.nadobro.venue.nado_client.get_product_name", return_value="BTC-PERP"), \
         mock.patch.object(NadoClient, "_query_edge", return_value=_envelope()) as edge, \
         mock.patch.object(NadoClient, "_query_rest", side_effect=AssertionError("live lane must not be used")):
        out = c.get_all_market_prices()
    assert out == {"BTC": {"bid": 1.0, "ask": 2.0, "mid": 1.5}}
    assert edge.call_args.args == ("cached_prices", {"product_ids": [1]})


def test_bulk_prices_fall_back_to_the_live_lane_only_when_edge_is_unavailable():
    c = _all_prices_client()
    with mock.patch("src.nadobro.venue.nado_client.get_perp_products", return_value=["BTC-PERP"]), \
         mock.patch("src.nadobro.venue.nado_client.get_product_id", return_value=1), \
         mock.patch("src.nadobro.venue.nado_client.get_product_name", return_value="BTC-PERP"), \
         mock.patch.object(NadoClient, "_query_edge", return_value=None), \
         mock.patch.object(NadoClient, "_query_rest", return_value=_envelope(bid=3 * 10**18, ask=3 * 10**18)) as live:
        out = c.get_all_market_prices()
    assert out == {"BTC": {"bid": 3.0, "ask": 3.0, "mid": 3.0}}
    assert live.call_args.args[0] == "market_prices"


def test_a_rate_limited_bulk_read_serves_the_last_snapshot_and_never_fans_out():
    c = _all_prices_client()
    with nc._caches_lock:
        nc._ALL_PRICES_CACHE["mainnet"] = {"data": {"BTC": {"bid": 9.0, "ask": 9.0, "mid": 9.0}}, "ts": 0.0}
    rejected = {"status": "failure", "error_code": 1000, "error": "Too Many Requests"}
    with mock.patch("src.nadobro.venue.nado_client.get_perp_products", return_value=["BTC-PERP"]), \
         mock.patch("src.nadobro.venue.nado_client.get_product_id", return_value=1), \
         mock.patch.object(NadoClient, "_query_edge", return_value=rejected), \
         mock.patch.object(NadoClient, "_query_rest", return_value=rejected), \
         mock.patch.object(NadoClient, "get_market_price", side_effect=AssertionError("per-product fan-out ran")):
        out = c.get_all_market_prices()
    assert out == {"BTC": {"bid": 9.0, "ask": 9.0, "mid": 9.0}}


def test_bulk_prices_are_read_through_cached():
    c = _all_prices_client()
    with mock.patch("src.nadobro.venue.nado_client.get_perp_products", return_value=["BTC-PERP"]), \
         mock.patch("src.nadobro.venue.nado_client.get_product_id", return_value=1), \
         mock.patch("src.nadobro.venue.nado_client.get_product_name", return_value="BTC-PERP"), \
         mock.patch.object(NadoClient, "_query_edge", return_value=_envelope()) as edge:
        c.get_all_market_prices()
        c.get_all_market_prices()
    assert edge.call_count == 1


# ------------------------------------------------------------ edge budget ---

def test_edge_lane_ignores_the_live_rate_limit_circuit_but_honours_cloudflare():
    from src.nadobro.venue import gateway_budget as gb
    url = "https://gateway.prod.nado.xyz/edge/query"
    gb._edge_buckets.clear()
    with mock.patch.object(gb, "is_gateway_blocked", return_value=True):
        assert gb.try_acquire(url, kind="edge") is True          # live breaker does not apply
        assert gb.try_acquire(url, kind="query") is False
    with mock.patch("src.nadobro.core.http_session.is_circuit_open", return_value=True):
        assert gb.try_acquire(url, kind="edge") is False         # a Cloudflare challenge does
    assert "edge_buckets" in gb.snapshot()


def test_edge_query_posts_to_the_edge_endpoint_with_weight_one_budget():
    c = _client()
    resp = SimpleNamespace(json=lambda: {"status": "success", "data": {"market_prices": []}},
                           status_code=200, headers={}, text="", url="u")
    with mock.patch("src.nadobro.venue.gateway_budget.try_acquire", return_value=True) as acq, \
         mock.patch.object(nc._rest_session, "post", return_value=resp) as post:
        out = c._query_edge("cached_prices", {"product_ids": [1, 2]})
    assert out["status"] == "success"
    assert acq.call_args.kwargs.get("kind") == "edge"
    assert post.call_args.args[0].endswith("/edge/query")
    assert post.call_args.kwargs["json"] == {"type": "cached_prices", "product_ids": [1, 2]}
    with mock.patch("src.nadobro.venue.gateway_budget.try_acquire", return_value=False), \
         mock.patch.object(nc._rest_session, "post", side_effect=AssertionError("denied edge read must not hit the network")):
        assert c._query_edge("cached_prices", {"product_ids": [1]}) is None
