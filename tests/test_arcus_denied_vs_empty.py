"""DENIED ≠ EMPTY across every public Arcus read (02 §10.1 / §12.9).

Every read × every denial (429 with and without ``reason``, 500, 503, read
timeout, connect error, non-JSON 200, a missing required field, a local
budget denial, 403 whitelist) is a non-``Ok`` outcome — never an ``Ok`` holding
``[]`` / ``{}``. The inverse: an empty ``Ok`` comes ONLY from a 2xx body that
carries the endpoint's documented container present-and-empty; a 2xx body
without the container (``{}``, ``{"total": 0}``, ``null``, the wrong key, a bare
array) is always ``Unavailable(200, "schema")``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx
import pytest

from arcus_helpers import ADDR, REF, fixture_resp, load_fixture, mock_client, resp
from src.nadobro.venue.arcus import errors as E
from src.nadobro.venue.arcus.client import ArcusClient
from src.nadobro.venue.arcus.errors import Ok, Unavailable, is_denied
from src.nadobro.venue.arcus.types import Lane

L = Lane.L1_ENGINE
TO_US = 1_790_000_200_000_000


@pytest.fixture(autouse=True)
def _guard(monkeypatch):
    async def refuse(self, request):  # pragma: no cover - only runs on a bug
        raise AssertionError("real network in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    E._reset_schema_errors_for_tests()
    yield
    E._reset_schema_errors_for_tests()


# name -> (path, call, a 2xx body missing a REQUIRED field)
READS: dict[str, tuple[str, Callable[[ArcusClient, dict[str, Any]], Any], Any]] = {
    "time": ("/v1/time", lambda c, kw: c.get_time(**kw), {}),
    "markets": ("/v1/markets", lambda c, kw: c.get_markets(**kw), {"total": 3}),
    "compliance": ("/v1/compliance", lambda c, kw: c.get_compliance(ADDR, **kw), {"geo": {"country": "XX"}}),
    "account": ("/v1/account", lambda c, kw: c.get_account(REF, **kw), {"accountIndex": 0, "address": ADDR}),
    "positions": ("/v1/positions", lambda c, kw: c.get_positions(REF, market=None, **kw), {"total": 0}),
    "openOrders": ("/v1/openOrders", lambda c, kw: c.get_open_orders(REF, market=None, **kw), {"total": 0}),
    "order": ("/v1/order/a1b2c3d4e5f67890", lambda c, kw: c.get_order(REF, "a1b2c3d4e5f67890", **kw), {"orderId": "a1b2c3d4e5f67890"}),
    "fills": ("/v1/fills", lambda c, kw: c.get_fills(REF, market=None, from_us=None, to_us=None, **kw), {"total": 0}),
    "funding": ("/v1/funding", lambda c, kw: c.get_funding(REF, from_us=None, to_us=None, **kw), {"funding": []}),
    "rateLimit": ("/v1/rateLimit", lambda c, kw: c.get_rate_limit(REF, **kw), {"address": ADDR, "accountIndex": 0}),
    "apiKeys": ("/v1/apiKeys", lambda c, kw: c.get_api_keys(ADDR, account_index=None, **kw), {"total": 0}),
    "leverages": ("/v1/leverages", lambda c, kw: c.get_leverages(REF, **kw), {"leverages": []}),
    "bbo": ("/v1/bbo/BTC-USD", lambda c, kw: c.get_bbo("BTC-USD", **kw), {"bestBid": {"price": "1"}, "bestAsk": None}),
    "mids": ("/v1/mids", lambda c, kw: c.get_mids(**kw), {"globalSequenceId": 1}),
    "prices": ("/v1/prices", lambda c, kw: c.get_prices(**kw), {"1": {"oraclePrice": "1"}}),
    "l2": ("/v1/l2OrderBook/BTC-USD", lambda c, kw: c.get_l2_orderbook("BTC-USD", **kw), {"bids": []}),
    "candles": ("/v1/candles", lambda c, kw: c.get_candles("BTC-USD", "1m", to_us=TO_US, **kw), {"total": 0}),
}


def _raise(exc_type: type[Exception]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("boom", request=request)  # type: ignore[call-arg]

    return handler


DENIALS: dict[str, Any] = {
    "429_no_reason": resp(429, {"error": "rate limited"}, headers={"Retry-After": "2"}),
    "429_ip": resp(429, {"error": "rate limited", "reason": "ip", "retryAfterMs": 2000}),
    "500": resp(500, {"error": "internal error", "errorType": "Internal"}),
    "503": fixture_resp(503, "err_503.json"),
    "read_timeout": _raise(httpx.ReadTimeout),
    "connect_error": _raise(httpx.ConnectError),
    "non_json_200": resp(200, content=b"<html>maintenance</html>"),
    "missing_required": None,  # per-endpoint body from READS
    "budget_denied": None,  # no request at all
    "403_whitelist": fixture_resp(403, "err_403_whitelist.json"),
}


@pytest.mark.parametrize("denial", sorted(DENIALS))
@pytest.mark.parametrize("read", sorted(READS))
def test_every_read_denial_is_denied_never_empty(read, denial):
    path, call, bad_body = READS[read]

    async def body():
        route = resp(200, bad_body) if denial == "missing_required" else DENIALS[denial]
        routes = {} if denial == "budget_denied" else {("GET", path): route}
        client, calls = mock_client(routes)
        kwargs: dict[str, Any] = {"lane": L}
        if denial == "budget_denied":
            assert client.ip_budget.try_take(1500, Lane.L0_BRAKE)
            kwargs["max_wait_s"] = 0
        result = await call(client, kwargs)
        assert is_denied(result), (read, denial, result)
        assert not isinstance(result, Ok)
        if denial == "budget_denied":
            assert calls == []
        else:
            assert len(calls) == 1  # one attempt, never retried

    asyncio.run(body())


# endpoint -> (path, call, a 2xx body whose documented container is present and EMPTY, expected value)
EMPTY_OK: dict[str, tuple[str, Callable[[ArcusClient], Any], Any, Any]] = {
    "openOrders": ("/v1/openOrders", lambda c: c.get_open_orders(REF, market=None, lane=L), load_fixture("open_orders_empty.json"), []),
    "positions": ("/v1/positions", lambda c: c.get_positions(REF, market=None, lane=L), load_fixture("positions_empty.json"), {}),
    "apiKeys": ("/v1/apiKeys", lambda c: c.get_api_keys(ADDR, account_index=None, lane=L), load_fixture("api_keys_empty.json"), []),
    "fills": ("/v1/fills", lambda c: c.get_fills(REF, market=None, from_us=None, to_us=None, lane=L), {"fills": []}, []),
    "funding": ("/v1/funding", lambda c: c.get_funding(REF, from_us=None, to_us=None, lane=L), {"fundingPayments": []}, []),
    "candles": ("/v1/candles", lambda c: c.get_candles("BTC-USD", "1m", to_us=TO_US, lane=L), {"candles": []}, []),
    "leverages": ("/v1/leverages", lambda c: c.get_leverages(REF, lane=L), {"leverages": [], "address": ADDR, "accountIndex": 0}, []),
    "mids": ("/v1/mids", lambda c: c.get_mids(lane=L), {"mids": {}, "globalSequenceId": 1}, {}),
    "markets": ("/v1/markets", lambda c: c.get_markets(lane=L), {"markets": []}, []),  # the catalog refuses [] itself
    "prices": ("/v1/prices", lambda c: c.get_prices(lane=L), {}, {}),  # the map IS the container
}


@pytest.mark.parametrize("name", sorted(EMPTY_OK))
def test_empty_ok_only_from_present_empty_container(name):
    path, call, body_json, expected = EMPTY_OK[name]

    async def body():
        client, _ = mock_client({("GET", path): resp(200, body_json)})
        result = await call(client)
        assert isinstance(result, Ok) and result.value == expected, result

    asyncio.run(body())


MISSING_CONTAINER_BODIES = {
    "empty_object": {},
    "total_only": {"total": 0},
    "null": None,
    "wrong_key": {"wrong": []},
    "bare_array": [],
}


@pytest.mark.parametrize("bad", sorted(MISSING_CONTAINER_BODIES))
@pytest.mark.parametrize("name", sorted(n for n in EMPTY_OK if n != "prices"))
def test_missing_container_is_schema_denial(name, bad):
    path, call, _, _ = EMPTY_OK[name]
    payload = MISSING_CONTAINER_BODIES[bad]

    async def body():
        route = resp(200, content=b"null") if payload is None else resp(200, payload)
        client, _ = mock_client({("GET", path): route})
        result = await call(client)
        assert result == Unavailable(200, "schema"), result

    asyncio.run(body())


def test_prices_non_map_bodies_are_denied():
    async def body():
        for payload in (b"null", b"[]", b'{"1": null}', b'{"x": {}}'):
            client, _ = mock_client({("GET", "/v1/prices"): resp(200, content=payload)})
            assert await client.get_prices(lane=L) == Unavailable(200, "schema"), payload

    asyncio.run(body())


def test_no_activity_is_its_own_type_not_empty():
    async def body():
        client, _ = mock_client({("GET", "/v1/account"): fixture_resp(404, "err_404_no_activity.json")})
        result = await client.get_account(REF, lane=L)
        assert is_denied(result) and type(result).__name__ == "NoActivity"

    asyncio.run(body())
