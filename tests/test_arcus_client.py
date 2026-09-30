"""venue/arcus/client.py — the async REST client (02 §8.1 / §12.8).

Every client runs over ``httpx.MockTransport`` (``arcus_helpers.mock_client``);
an autouse guard makes any real transport call fail the test. Assertions are
on the captured ``httpx.Request`` objects (path, query, headers, exact body
bytes) and on the typed outcomes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from arcus_helpers import (
    ADDR,
    ADDR_MIXED,
    CT0,
    GTT,
    PUB,
    REF,
    SEED,
    V1_SIG,
    V3_SIG,
    V4_SIG,
    V5_PAYLOAD,
    V5_SIG,
    V6_SIG,
    V7_SIG,
    FakeMono,
    FakeTimeNs,
    fixture_resp,
    golden_auth,
    load_fixture,
    mock_client,
    resp,
)
from src.nadobro.venue.arcus import client as C
from src.nadobro.venue.arcus import errors as E
from src.nadobro.venue.arcus.budget import IpBudget
from src.nadobro.venue.arcus.client import ArcusClient, BatchCancelResult, backward_page_cursor, build_transport
from src.nadobro.venue.arcus.clock import ArcusClock
from src.nadobro.venue.arcus.errors import (
    Accepted,
    Ambiguous,
    Forbidden,
    InexactUnitError,
    LocalDenied,
    NoActivity,
    NotFound,
    Ok,
    Rejected,
    Throttled,
    Transmission,
    Unauthorized,
    Unavailable,
    schema_error_counts,
)
from src.nadobro.venue.arcus.signing import canonical_json, legacy_message, place_payload, to_quantums, to_ticks
from src.nadobro.venue.arcus.types import (
    ArcusAccountRef,
    CancelSpec,
    Lane,
    OrderSpec,
    Side,
    Tif,
    WireOrderType,
)

D = Decimal
DAY_US = 86_400 * 1_000_000
T_PLACE = ("POST", "/v1/placeOrder")
T_CANCEL = ("POST", "/v1/cancelOrder")
T_BATCH = ("POST", "/v1/batchCancelOrders")
T_LEV = ("POST", "/v1/setLeverage")
T_TIME = ("GET", "/v1/time")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    async def refuse(self, request):  # pragma: no cover - only runs on a bug
        raise AssertionError("real network in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    E._reset_schema_errors_for_tests()
    yield
    E._reset_schema_errors_for_tests()


def v1_spec(**overrides) -> OrderSpec:
    fields = dict(
        market_id=1,
        side=Side.BUY,
        order_type=WireOrderType.LIMIT,
        tif=Tif.ALO,
        quantity=D("0.0001"),
        price=D("84517.3"),
        reduce_only=False,
        client_id="nb7ps_2s-1",
        good_til_us=GTT,
        tick_size=D("0.1"),
        step_size=D("0.00000001"),
    )
    fields.update(overrides)
    return OrderSpec(**fields)


class _TimeOk:
    """A TimeSource returning exactly the local clock (offset 0)."""

    def __init__(self, ns: int) -> None:
        self.ns = ns

    async def get_time(self, *, lane, max_wait_s=None):
        return Ok(value=self.ns, http_status=200, weight_charged=1)


def _run_now(coro):
    """Drive a coroutine that never suspends (usable inside a running loop)."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("coroutine suspended")  # pragma: no cover


def _synced_clock(mono: FakeMono, now_ns: int = CT0) -> ArcusClock:
    clock = ArcusClock("testnet", time_ns=FakeTimeNs(now=now_ns), monotonic=mono)
    assert _run_now(clock.sync(_TimeOk(now_ns))) == 0.0
    assert clock.offset_ns() == 0 and clock.synced_within(1)
    return clock


def _client(routes, *, now_ns: int = CT0, synced: bool = True, **kwargs):
    mono = kwargs.pop("mono", None) or FakeMono()
    clock = kwargs.pop("clock", None)
    if clock is None:
        clock = _synced_clock(mono, now_ns) if synced else ArcusClock(
            "testnet", time_ns=FakeTimeNs(now=now_ns), monotonic=mono
        )
    client, calls = mock_client(routes, mono=mono, clock=clock, **kwargs)
    return client, calls, mono


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _verify(pub_hex: str, sig_hex: str, message: bytes) -> None:
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(bytes.fromhex(sig_hex), message)


# --- requests / headers -------------------------------------------------------------------


def _reads_all(client: ArcusClient):
    """One call of every account-scoped read."""
    L = Lane.L2_INTERACTIVE
    return [
        client.get_account(REF, lane=L),
        client.get_positions(REF, market=None, lane=L),
        client.get_open_orders(REF, market="BTC-USD", lane=L),
        client.get_order(REF, "a1b2c3d4e5f67890", lane=L),
        client.get_fills(REF, market=None, from_us=None, to_us=None, lane=L),
        client.get_funding(REF, from_us=None, to_us=None, lane=L),
        client.get_rate_limit(REF, lane=L),
        client.get_leverages(REF, lane=L),
    ]


ACCOUNT_ROUTES = {
    ("GET", "/v1/account"): fixture_resp(200, "account_ok.json"),
    ("GET", "/v1/positions"): fixture_resp(200, "positions_ok.json"),
    ("GET", "/v1/openOrders"): fixture_resp(200, "open_orders_page.json"),
    ("GET", "/v1/order/a1b2c3d4e5f67890"): fixture_resp(200, "order_ok.json"),
    ("GET", "/v1/fills"): fixture_resp(200, "fills_page.json"),
    ("GET", "/v1/funding"): fixture_resp(200, "funding_page.json"),
    ("GET", "/v1/rateLimit"): fixture_resp(200, "rate_limit_testnet.json"),
    ("GET", "/v1/leverages"): fixture_resp(200, "leverages_ok.json"),
    ("GET", "/v1/apiKeys"): fixture_resp(200, "api_keys_ok.json"),
}


def test_reads_send_address_and_account_index():
    async def body():
        client, calls, _ = _client(ACCOUNT_ROUTES)
        results = [await c for c in _reads_all(client)]
        assert all(isinstance(r, Ok) for r in results), results
        assert len(calls) == 8
        for request in calls:
            assert request.url.params["address"] == ADDR  # lowercase
            assert request.url.params["accountIndex"] == "0"
        assert isinstance(await client.get_api_keys(ADDR_MIXED, account_index=None, lane=Lane.L2_INTERACTIVE), Ok)
        assert calls[-1].url.params["address"] == ADDR
        assert "accountIndex" not in calls[-1].url.params  # omitted != accountIndex=0
        await client.get_api_keys(ADDR, account_index=0, lane=Lane.L2_INTERACTIVE)
        assert calls[-1].url.params["accountIndex"] == "0"

    asyncio.run(body())


def test_reads_have_no_auth_and_no_browser_headers():
    async def body():
        client, calls, _ = _client(ACCOUNT_ROUTES)
        for c in _reads_all(client):
            await c
        for request in calls:
            for header in ("X-API-Key", "X-Signature", "X-Timestamp", "Origin", "Referer"):
                assert header not in request.headers
            assert not any(h.lower().startswith("sec-fetch") for h in request.headers)
            assert request.headers["User-Agent"] == "nadobro-arcus/1"
            assert request.headers["Accept"] == "application/json"
            assert request.method == "GET"

    asyncio.run(body())


def test_bbo_uses_path_form():
    async def body():
        client, calls, _ = _client({("GET", "/v1/bbo/BTC-USD"): fixture_resp(200, "bbo_btc.json")})
        r = await client.get_bbo("BTC-USD", lane=Lane.L1_ENGINE)
        assert isinstance(r, Ok) and r.value.bid == D("84594.4") and r.value.ask == D("84594.5")
        assert calls[0].url.path == "/v1/bbo/BTC-USD" and not calls[0].url.params
        assert r.weight_charged == 2

    asyncio.run(body())


def test_l2_path_and_weight():
    async def body():
        client, calls, _ = _client({("GET", "/v1/l2OrderBook/BTC-USD"): fixture_resp(200, "l2_btc.json")})
        before = client.ip_budget.level()
        r = await client.get_l2_orderbook("BTC-USD", n_levels=100, lane=Lane.L1_ENGINE)
        assert isinstance(r, Ok) and r.weight_charged == 7
        assert calls[0].url.params["nLevels"] == "100"
        assert before - client.ip_budget.level() == 7  # debited BEFORE the call
        assert r.value.bids[0][0] == D("84594.4") and r.value.asks[0][0] == D("84594.5")
        await client.get_l2_orderbook("BTC-USD", n_levels=500, lane=Lane.L1_ENGINE)
        assert calls[-1].url.params["nLevels"] == "100"  # clamped

    asyncio.run(body())


def test_ticker_validation():
    async def body():
        client, calls, _ = _client({})
        for bad in ("BTC/USD", "", "a" * 33, "BTC USD", None):
            with pytest.raises(ValueError):
                await client.get_bbo(bad, lane=Lane.L1_ENGINE)  # type: ignore[arg-type]
            with pytest.raises(ValueError):
                await client.get_l2_orderbook(bad, lane=Lane.L1_ENGINE)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await client.get_positions(REF, market="BTC/USD", lane=Lane.L1_ENGINE)
        with pytest.raises(ValueError):
            await client.get_order(REF, "../x", lane=Lane.L1_ENGINE)
        with pytest.raises(ValueError):
            await client.get_time(lane=1)  # type: ignore[arg-type]
        assert calls == []

    asyncio.run(body())


def test_market_filter_accepts_ticker_or_numeric_id():
    async def body():
        client, calls, _ = _client({("GET", "/v1/positions"): fixture_resp(200, "positions_empty.json")})
        await client.get_positions(REF, market="1", lane=Lane.L0_BRAKE)
        await client.get_positions(REF, market="BTC-USD", lane=Lane.L0_BRAKE)
        assert [c.url.params["market"] for c in calls] == ["1", "BTC-USD"]

    asyncio.run(body())


# --- signed writes: exact requests ----------------------------------------------------------------


def test_place_order_golden():
    async def body():
        client, calls, _ = _client({T_PLACE: fixture_resp(202, "place_202.json")})
        out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Accepted)
        assert len(calls) == 1
        request = calls[0]
        assert request.method == "POST" and request.url.path == "/v1/placeOrder"
        assert dict(request.url.params) == {"address": ADDR}
        assert request.headers["X-API-Key"] == PUB
        assert request.headers["X-Timestamp"] == "1790000000123456789"
        assert request.headers["X-Signature"] == V1_SIG
        assert request.headers["Content-Type"] == "application/json"
        assert request.headers["User-Agent"] == "nadobro-arcus/1"
        assert "Origin" not in request.headers and "Referer" not in request.headers
        expected = canonical_json(
            {
                "accountIndex": 0,
                "address": ADDR,
                "clientId": "nb7ps_2s-1",
                "goodTilTime": "1793456000000000",
                "marketId": 1,
                "orderSide": "BUY",
                "orderType": "LIMIT",
                "price": "84517.3",
                "quantity": "0.0001",
                "reduceOnly": False,
                "timeInForce": "ALO",
                "timestamp": 1790000000123456789,
            }
        )
        assert request.content == expected

    asyncio.run(body())


def test_place_signature_verifies_from_body():
    """Server-side re-derivation from the HTTP body alone verifies the signature."""

    async def body():
        client, calls, _ = _client({T_PLACE: fixture_resp(202, "place_202.json")})
        spec = v1_spec(side=Side.SELL, tif=Tif.GTT, price=D("84600.0"), quantity=D("0.00012345"), reduce_only=True)
        await client.place_order(golden_auth(), spec)
        request = calls[0]
        b = _body(request)
        payload = place_payload(
            address=request.url.params["address"],
            account_index=b["accountIndex"],
            client_id=b["clientId"],
            ct_ns=int(request.headers["X-Timestamp"]),
            good_til_us=int(b["goodTilTime"]),
            market_id=b["marketId"],
            price_ticks=to_ticks(D(b["price"]), D("0.1")),
            qty_quantums=to_quantums(D(b["quantity"]), D("0.00000001")),
            reduce_only=b["reduceOnly"],
            side=Side(b["orderSide"]),
            tif=Tif[b["timeInForce"]],
        )
        assert b["timestamp"] == int(request.headers["X-Timestamp"])
        assert b["price"] == "84600" and b["timeInForce"] == "GTT" and b["reduceOnly"] is True
        _verify(request.headers["X-API-Key"], request.headers["X-Signature"], payload)

    asyncio.run(body())


def test_cancel_by_id_and_by_cid_bodies():
    async def body():
        client, calls, _ = _client({T_CANCEL: fixture_resp(202, "cancel_202.json")}, now_ns=CT0 + 2)
        out = await client.cancel_order(golden_auth(), CancelSpec(1, order_id="a1b2c3d4e5f67890"))
        assert isinstance(out, Accepted) and out.status == "CANCEL_ACKNOWLEDGED" and out.pool.remaining == 39999
        request = calls[0]
        assert dict(request.url.params) == {"address": ADDR}
        assert request.headers["X-Timestamp"] == str(CT0 + 2)
        assert request.headers["X-Signature"] == V3_SIG
        assert _body(request) == {
            "accountIndex": 0,
            "address": ADDR,
            "kind": "orderId",
            "marketId": 1,
            "orderId": "a1b2c3d4e5f67890",
            "timestamp": CT0 + 2,
        }
        cid_body = load_fixture("cancel_202.json")
        cid_body.pop("orderId")
        cid_body["clientId"] = "nb7ps_2s-1"
        client, calls, _ = _client({T_CANCEL: resp(202, json.loads(json.dumps(cid_body, default=str)))}, now_ns=CT0 + 3)
        out = await client.cancel_order(golden_auth(), CancelSpec(1, client_id="nb7ps_2s-1"))
        assert isinstance(out, Accepted) and out.client_id == "nb7ps_2s-1"
        request = calls[0]
        assert request.headers["X-Signature"] == V4_SIG
        assert _body(request) == {
            "accountIndex": 0,
            "address": ADDR,
            "clientId": "nb7ps_2s-1",
            "kind": "clientId",
            "marketId": 1,
            "timestamp": CT0 + 3,
        }

    asyncio.run(body())


def test_batch_cancel_request():
    async def body():
        client, calls, _ = _client({T_BATCH: fixture_resp(202, "batch_cancel_202.json")}, now_ns=CT0 + 5)
        charges: list[int] = []
        real = client.ip_budget.charge_after
        client.ip_budget.charge_after = lambda n: (charges.append(n), real(n))[1]  # type: ignore[method-assign]
        specs = [CancelSpec(1, client_id="nb7ps_2s-2"), CancelSpec(3, order_id="00000000000000ff")]
        res = await client.batch_cancel(golden_auth(), specs)
        assert isinstance(res, BatchCancelResult) and isinstance(res.outcome, Accepted)
        assert len(calls) == 1
        request = calls[0]
        assert request.url.path == "/v1/batchCancelOrders" and dict(request.url.params) == {"address": ADDR}
        ct = CT0 + 5
        assert request.headers["X-Timestamp"] == str(ct)
        assert request.headers["X-Signature"] == V6_SIG  # element 0's signature
        b = _body(request)
        assert b == {
            "cancels": [
                {
                    "accountIndex": 0, "address": ADDR, "clientId": "nb7ps_2s-2", "kind": "clientId",
                    "marketId": 1, "signature": V6_SIG, "timestamp": ct,
                },
                {
                    "accountIndex": 0, "address": ADDR, "kind": "orderId", "marketId": 3,
                    "orderId": "00000000000000ff", "signature": V7_SIG, "timestamp": ct,
                },
            ]
        }
        assert request.content == canonical_json(b)
        assert charges == [0]  # floor(2/40)

        client, calls, _ = _client({T_BATCH: resp(202, {"responses": []})})
        charges.clear()
        real = client.ip_budget.charge_after
        client.ip_budget.charge_after = lambda n: (charges.append(n), real(n))[1]  # type: ignore[method-assign]
        forty = [CancelSpec(1, client_id=f"nb7ps_2s-{i}") for i in range(40)]
        res = await client.batch_cancel(golden_auth(), forty)
        assert res.rows == (None,) * 40  # accepted, nothing echoed: pending, never a fabricated ACK
        assert charges == [1]
        for bad in ([], [CancelSpec(1, client_id=f"nb7ps_2s-{i}") for i in range(101)]):
            with pytest.raises(ValueError):
                await client.batch_cancel(golden_auth(), bad)
        with pytest.raises(ValueError):
            await client.batch_cancel(golden_auth(), [CancelSpec(1, order_id="ab"), CancelSpec(2, order_id="ab")])
        with pytest.raises(ValueError):
            await client.batch_cancel(
                golden_auth(), [CancelSpec(1, client_id="nb1_1-1"), CancelSpec(1, client_id="nb1_1-1")]
            )
        with pytest.raises(ValueError):
            await client.batch_cancel(golden_auth(), CancelSpec(1, client_id="nb1_1-1"))  # type: ignore[arg-type]
        assert len(calls) == 1

    asyncio.run(body())


def test_set_leverage_scheme2():
    async def body():
        client, calls, _ = _client({T_LEV: fixture_resp(200, "set_leverage_200.json")}, now_ns=CT0 + 4)
        before = client.ip_budget.level()
        out = await client.set_leverage(golden_auth(), 1, 5)
        assert isinstance(out, Accepted) and out.status == "APPLIED"
        request = calls[0]
        b = _body(request)
        assert b == {"accountIndex": 0, "address": ADDR, "leverage": 5, "marketId": 1}
        assert "isolated" not in b  # never changes margin mode
        ct = int(request.headers["X-Timestamp"])
        assert ct == CT0 + 4
        assert legacy_message(ct, "setLeverage", b) == V5_PAYLOAD
        assert request.headers["X-Signature"] == V5_SIG
        _verify(PUB, request.headers["X-Signature"], V5_PAYLOAD)
        assert before - client.ip_budget.level() == 125
        assert client.ip_budget.snapshot()["taken"]["L2_INTERACTIVE"] == 1
        for bad in ((1, 0), (1, 1001), (1, True), (-1, 5), (70000, 5)):
            with pytest.raises(ValueError):
                await client.set_leverage(golden_auth(), *bad)
        assert len(calls) == 1

    asyncio.run(body())


def test_set_leverage_budget_denied_sends_nothing():
    async def body():
        client, calls, _ = _client({})
        assert client.ip_budget.try_take(1200, Lane.L0_BRAKE)  # 300 left < 400 (L2 floor) + 125
        out = await client.set_leverage(golden_auth(), 1, 5)
        assert out == LocalDenied("ip_budget:L2_INTERACTIVE")
        assert calls == []

    asyncio.run(body())


def test_writes_reject_wrong_auth_or_spec():
    async def body():
        client, calls, _ = _client({})
        mainnet_auth = golden_auth(ArcusAccountRef("mainnet", ADDR, 0))
        with pytest.raises(ValueError):
            await client.place_order(mainnet_auth, v1_spec())
        with pytest.raises(ValueError):
            await client.place_order(object(), v1_spec())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await client.place_order(golden_auth(), object())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await client.cancel_order(golden_auth(), object())  # type: ignore[arg-type]
        assert calls == []

    asyncio.run(body())


# --- signed writes: outcomes -------------------------------------------------------------------


def _place(route, **kwargs):
    client, calls, mono = _client({T_PLACE: route}, **kwargs)
    return client, calls, mono


def test_place_accepted_202():
    async def body():
        client, calls, _ = _place(fixture_resp(202, "place_202.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Accepted)
        assert (out.http_status, out.order_id, out.client_id, out.status) == (202, "a1b2c3d4e5f67890", "nb7ps_2s-1", "ACK")
        assert out.pool is not None and out.pool.remaining == 19999 and out.pool.source == "write"

    asyncio.run(body())


def test_place_rejected_200():
    async def body():
        client, _, _ = _place(fixture_resp(200, "place_200_rejected.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Accepted) and out.status == "REJECTED"
        assert out.rejection_reason == "POST_ONLY_WOULD_CROSS" and out.pool is None
        client, _, _ = _place(fixture_resp(200, "place_200_error.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert out == Rejected(200, None, "Order", "batch item validation failure", "nb7ps_2s-1")

    asyncio.run(body())


def test_place_echo_mismatch_is_ambiguous():
    async def body():
        for key, value in (("clientId", "other"), ("address", "0x" + "11" * 20), ("accountIndex", 3), ("marketId", 2)):
            E._reset_schema_errors_for_tests()
            b = load_fixture("place_202.json")
            b[key] = value
            client, _, _ = _place(resp(202, json.loads(json.dumps(b, default=str))))
            out = await client.place_order(golden_auth(), v1_spec())
            assert out == Ambiguous("nb7ps_2s-1", "echo_mismatch"), key
            assert sum(schema_error_counts().values()) == 1
        assert schema_error_counts() == {"write.marketId": 1}
        # a checksum-case address echo is fine
        b = load_fixture("place_202.json")
        b["address"] = ADDR_MIXED
        client, _, _ = _place(resp(202, json.loads(json.dumps(b, default=str))))
        assert isinstance(await client.place_order(golden_auth(), v1_spec()), Accepted)

    asyncio.run(body())


def test_write_read_timeout_is_ambiguous():
    async def body():
        def boom(request):
            raise httpx.ReadTimeout("slow", request=request)

        client, calls, _ = _place(boom)
        out = await client.place_order(golden_auth(), v1_spec())
        assert out == Ambiguous("nb7ps_2s-1", "ReadTimeout")
        assert len(calls) == 1  # never retried

    asyncio.run(body())


def test_write_connect_error_not_sent():
    async def body():
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        client, _, _ = _place(refuse)
        assert await client.place_order(golden_auth(), v1_spec()) == Unavailable(0, "not_sent:ConnectError")

    asyncio.run(body())


def test_write_500_internal_ambiguous():
    async def body():
        for status in (500, 502, 504):
            client, _, _ = _place(fixture_resp(status, "err_500_internal.json"))
            assert await client.place_order(golden_auth(), v1_spec()) == Ambiguous("nb7ps_2s-1", f"http_{status}")
        for status in (302, 405, 409):
            client, calls, _ = _place(resp(status, {"error": "x"}, headers={"Location": "https://evil.example/"}))
            assert await client.place_order(golden_auth(), v1_spec()) == Ambiguous("nb7ps_2s-1", f"http_{status}")
            assert len(calls) == 1  # a redirect is never followed

    asyncio.run(body())


def test_write_503_unavailable():
    async def body():
        client, _, _ = _place(fixture_resp(503, "err_503.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Unavailable) and out.http_status == 503

    asyncio.run(body())


def test_write_transmission():
    async def body():
        client, _, _ = _place(fixture_resp(500, "err_500_transmission.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Transmission)

    asyncio.run(body())


def test_write_400_and_403_outcomes():
    async def body():
        client, _, _ = _place(fixture_resp(400, "err_400_tick.json"))
        assert await client.place_order(golden_auth(), v1_spec()) == Rejected(
            400, "Tick", "Order", "price is not a multiple of tick size", "nb7ps_2s-1"
        )
        client, _, _ = _place(fixture_resp(403, "err_403_geo.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Forbidden) and out.kind == "geo"
        client, _, _ = _place(fixture_resp(403, "err_403_whitelist.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Forbidden) and out.kind == "whitelist"

    asyncio.run(body())


def test_write_429_ip_blocks_budget():
    async def body():
        client, calls, mono = _place(fixture_resp(429, "err_429_ip_write.json", headers={"Retry-After": "2"}))
        out = await client.place_order(golden_auth(), v1_spec())
        assert out == Throttled("ip", 2000, ())
        assert client.ip_budget.write_blocked()
        assert await client.place_order(golden_auth(), v1_spec()) == LocalDenied("ip_blocked")
        assert await client.place_order(golden_auth(), v1_spec(reduce_only=True)) == LocalDenied("ip_blocked")
        res = await client.batch_cancel(golden_auth(), [CancelSpec(1, client_id="nb7ps_2s-1")])
        assert res == BatchCancelResult(LocalDenied("ip_blocked"), ())
        assert len(calls) == 1  # nothing sent while blocked
        mono.advance(2.0)
        assert not client.ip_budget.write_blocked()

    asyncio.run(body())


def test_write_429_account_layers_do_not_block_ip():
    async def body():
        client, _, _ = _place(fixture_resp(429, "err_429_account_empty.json"))
        out = await client.place_order(golden_auth(), v1_spec())
        assert out == Throttled("account_empty", 850, ("my-order-42",))
        assert not client.ip_budget.write_blocked()  # pool layer, not the IP bucket

    asyncio.run(body())


def test_write_401_invalidates_clock():
    async def body():
        client, calls, _ = _client(
            {
                T_PLACE: [fixture_resp(401, "err_401.json"), fixture_resp(202, "place_202.json")],
                T_TIME: fixture_resp(200, "time.json"),
            }
        )
        assert isinstance(await client.place_order(golden_auth(), v1_spec()), Unauthorized)
        assert not client.clock.synced_within(10**6)
        assert isinstance(await client.place_order(golden_auth(), v1_spec()), Accepted)
        assert [(c.method, c.url.path) for c in calls] == [T_PLACE, T_TIME, T_PLACE]
        assert client.clock.synced_within(1)

    asyncio.run(body())


def test_opening_requires_recent_sync():
    async def body():
        client, calls, mono = _client(
            {
                T_TIME: fixture_resp(429, "err_429_read.json", headers={"Retry-After": "2"}),
                T_PLACE: fixture_resp(202, "place_202.json"),
                T_CANCEL: fixture_resp(202, "cancel_202.json"),
            },
            synced=False,
        )
        assert await client.place_order(golden_auth(), v1_spec()) == LocalDenied("clock_unsynced")
        assert [(c.method, c.url.path) for c in calls] == [T_TIME]  # one inline sync, no POST
        # The /v1/time 429 means the SERVER bucket is empty: writes wait it out (Retry-After 2 s).
        assert client.ip_budget.write_blocked()
        mono.advance(2.0)
        assert isinstance(await client.place_order(golden_auth(), v1_spec(reduce_only=True)), Accepted)  # D2
        assert isinstance(await client.cancel_order(golden_auth(), CancelSpec(1, order_id="a1b2c3d4e5f67890")), Accepted)
        assert [(c.method, c.url.path) for c in calls] == [T_TIME, T_PLACE, T_CANCEL]
        assert not client.clock.synced_within(10**6)  # never faked

    asyncio.run(body())


def test_stale_sync_resyncs_before_opening():
    async def body():
        client, calls, mono = _client(
            {T_TIME: fixture_resp(200, "time.json"), T_PLACE: fixture_resp(202, "place_202.json")},
            clock_max_age_s=lambda: 900.0,
        )
        mono.advance(901)
        assert isinstance(await client.place_order(golden_auth(), v1_spec()), Accepted)
        assert [(c.method, c.url.path) for c in calls] == [T_TIME, T_PLACE]

    asyncio.run(body())


def test_gtt_too_near_local():
    async def body():
        client, calls, _ = _place(fixture_resp(202, "place_202.json"))
        now_us = client.clock.now_us()
        assert await client.place_order(golden_auth(), v1_spec(good_til_us=now_us + 10 * DAY_US)) == LocalDenied(
            "gtt_too_near"
        )
        assert await client.place_order(golden_auth(), v1_spec(good_til_us=now_us + 30 * DAY_US)) == LocalDenied(
            "gtt_too_near"
        )
        assert calls == []
        assert isinstance(await client.place_order(golden_auth(), v1_spec(good_til_us=now_us + 32 * DAY_US)), Accepted)

    asyncio.run(body())


def test_inexact_spec_raises():
    async def body():
        client, calls, _ = _client({}, synced=False)  # an unsynced clock too: nothing is read first
        with pytest.raises(InexactUnitError):
            await client.place_order(golden_auth(), v1_spec(price=D("84517.35")))
        with pytest.raises(InexactUnitError):
            await client.place_order(golden_auth(), v1_spec(quantity=D("0.000000015")))
        assert calls == []

    asyncio.run(body())


def test_batch_rows_matched_by_echo():
    async def body():
        specs = [CancelSpec(1, client_id="nb7ps_2s-2"), CancelSpec(3, order_id="00000000000000ff")]
        client, _, _ = _client({T_BATCH: fixture_resp(202, "batch_cancel_202.json")})
        res = await client.batch_cancel(golden_auth(), list(reversed(specs)))
        assert isinstance(res.outcome, Accepted) and res.outcome.pool.remaining == 39997
        assert [r.status for r in res.rows] == ["CANCEL_ACKNOWLEDGED", "CANCEL_ACKNOWLEDGED"]
        assert res.rows[0].order_id == "00000000000000ff" and res.rows[1].client_id == "nb7ps_2s-2"
        assert schema_error_counts() == {}

        client, _, _ = _client({T_BATCH: fixture_resp(200, "batch_cancel_200_mixed.json")})
        extra = CancelSpec(2, order_id="0000000000000fff")  # no row echoes it
        res = await client.batch_cancel(golden_auth(), specs + [extra])
        first, second, third = res.rows
        assert isinstance(first, Accepted) and first.http_status == 200 and first.status == "CANCELED"
        assert first.client_id == "nb7ps_2s-2" and first.order_id == "0000000000000abc"
        assert isinstance(second, Accepted) and second.status == "REJECTED"
        assert second.rejection_reason == "ORDER_NOT_FOUND"
        assert third is None  # pending, never a fabricated ACK
        assert schema_error_counts().get("batch.rows") == 1  # the unmatched 00…ee row

        client, _, _ = _client({T_BATCH: fixture_resp(200, "batch_cancel_row_error.json")})
        res = await client.batch_cancel(golden_auth(), specs[:1])
        assert res.rows == (Rejected(200, None, "Cancel", "x", "nb7ps_2s-2"),)

    asyncio.run(body())


def test_batch_rows_never_trust_foreign_or_mismatched_rows():
    async def body():
        row = {
            "address": ADDR, "accountIndex": 0, "clientId": "nb7ps_2s-2", "marketId": 1,
            "marketDisplayName": "BTC-USD", "status": "CANCELED", "updateTime": 1,
        }
        cases = [
            dict(row, address="0x" + "22" * 20),  # another account
            dict(row, accountIndex=1),  # another subaccount
            dict(row, marketId=2),  # another market
            dict(row, status=None),  # no status -> no information
        ]
        for bad in cases:
            client, _, _ = _client({T_BATCH: resp(200, {"responses": [bad]})})
            res = await client.batch_cancel(golden_auth(), [CancelSpec(1, client_id="nb7ps_2s-2")])
            assert res.rows == (None,), bad
        # a clientId cancel may ALSO echo its resolved orderId; an orderId target in
        # the same batch still gets its OWN row
        rows = [
            dict(row, orderId="00000000000000aa"),
            {k: v for k, v in dict(row, orderId="00000000000000aa", status="CANCEL_ACKNOWLEDGED").items() if k != "clientId"},
        ]
        client, _, _ = _client({T_BATCH: resp(200, {"responses": rows})})
        res = await client.batch_cancel(
            golden_auth(), [CancelSpec(1, order_id="00000000000000aa"), CancelSpec(1, client_id="nb7ps_2s-2")]
        )
        assert res.rows[0].status == "CANCEL_ACKNOWLEDGED" and res.rows[0].client_id is None
        assert res.rows[1].status == "CANCELED" and res.rows[1].client_id == "nb7ps_2s-2"
        # a non-JSON 2xx: accepted, every row pending
        client, _, _ = _client({T_BATCH: resp(202, content=b"ok")})
        res = await client.batch_cancel(golden_auth(), [CancelSpec(1, client_id="nb7ps_2s-2")])
        assert isinstance(res.outcome, Accepted) and res.rows == (None,)

    asyncio.run(body())


def test_batch_429_all_or_nothing():
    async def body():
        client, _, _ = _client({T_BATCH: fixture_resp(429, "err_429_batch_partial.json")})
        res = await client.batch_cancel(
            golden_auth(), [CancelSpec(1, client_id="nb7ps_2s-2"), CancelSpec(1, order_id="00000000000000ff")]
        )
        assert res.outcome == Throttled("account_partial", 1200, ("nb7ps_2s-2", ""))
        assert res.rows == ()
        client, _, _ = _client({T_BATCH: fixture_resp(500, "err_500_internal.json")})
        res = await client.batch_cancel(golden_auth(), [CancelSpec(1, client_id="nb7ps_2s-2")])
        assert res == BatchCancelResult(Ambiguous(None, "http_500"), ())

    asyncio.run(body())


def test_set_leverage_outcomes():
    async def body():
        for status, name, expected in (
            (200, "set_leverage_200.json", "APPLIED"),
            (202, "set_leverage_202.json", "ACK"),
        ):
            client, _, _ = _client({T_LEV: fixture_resp(status, name)})
            out = await client.set_leverage(golden_auth(), 1, 5)
            assert isinstance(out, Accepted) and out.status == expected and out.http_status == status
        client, _, _ = _client({T_LEV: fixture_resp(422, "set_leverage_422.json")})
        out = await client.set_leverage(golden_auth(), 1, 10)
        assert out == Rejected(422, "HAS_OPEN_POSITION", None, "REJECTED", None)

    asyncio.run(body())


# --- reads: outcomes -------------------------------------------------------------------------


def test_get_account_ok_and_no_activity_and_whitelist():
    async def body():
        client, _, _ = _client({("GET", "/v1/account"): fixture_resp(200, "account_ok.json")})
        r = await client.get_account(REF, lane=Lane.L2_INTERACTIVE)
        assert isinstance(r, Ok) and r.value.equity == D("1000.1") and 1 in r.value.positions
        client, _, _ = _client({("GET", "/v1/account"): fixture_resp(404, "err_404_no_activity.json")})
        assert await client.get_account(REF, lane=Lane.L2_INTERACTIVE) == NoActivity()
        client, _, _ = _client({("GET", "/v1/account"): fixture_resp(403, "err_403_whitelist.json")})
        r = await client.get_account(REF, lane=Lane.L2_INTERACTIVE)
        assert isinstance(r, Forbidden) and r.kind == "whitelist"

    asyncio.run(body())


def test_get_positions_empty_is_ok_empty():
    async def body():
        client, _, _ = _client({("GET", "/v1/positions"): fixture_resp(200, "positions_empty.json")})
        r = await client.get_positions(REF, market=None, lane=Lane.L0_BRAKE)
        assert r == Ok({}, 200, 2)
        client, _, _ = _client({("GET", "/v1/positions"): fixture_resp(200, "positions_ok.json")})
        r = await client.get_positions(REF, market=None, lane=Lane.L0_BRAKE)
        assert isinstance(r, Ok) and r.value[3].size == D("-0.5") and r.value[3].mark_px is None

    asyncio.run(body())


def _order_row(i: int, created: int) -> dict:
    row = load_fixture("order_ok.json")
    row["orderId"] = f"{i:016x}"
    row["clientId"] = f"nb1_1-{i}"
    row["createdAt"] = created
    return json.loads(json.dumps(row, default=str))


def test_get_open_orders_paging():
    async def body():
        base = 1_790_000_000_000_000
        page1 = [_order_row(i, base - i) for i in range(1000)]  # newest-first
        oldest = page1[-1]["createdAt"]
        page2 = [_order_row(999, oldest), _order_row(1000, oldest - 1), _order_row(1001, oldest - 2)]
        client, calls, _ = _client(
            {("GET", "/v1/openOrders"): [resp(200, {"orders": page1, "total": 1000}), resp(200, {"orders": page2})]}
        )
        r = await client.get_open_orders(REF, market=None, status=("OPEN", "UNTRIGGERED"), lane=Lane.L1_ENGINE)
        assert isinstance(r, Ok)
        assert len(r.value) == 1002 and len({o.order_id for o in r.value}) == 1002
        assert r.weight_charged == (20 + 20) + (20 + 0)
        assert "to" not in calls[0].url.params
        assert calls[1].url.params["to"] == str(oldest)
        assert calls[0].url.params["status"] == "OPEN,UNTRIGGERED" and calls[0].url.params["limit"] == "1000"
        assert [o.order_id for o in r.value[:2]] == [f"{0:016x}", f"{1:016x}"]  # newest-first kept

    asyncio.run(body())


def test_get_open_orders_truncated():
    async def body():
        base = 1_790_000_000_000_000
        page1 = [_order_row(i, base - i) for i in range(1000)]
        page2 = [_order_row(i, base - i) for i in range(999, 1999)]
        client, calls, _ = _client(
            {("GET", "/v1/openOrders"): [resp(200, {"orders": page1}), resp(200, {"orders": page2})]}
        )
        r = await client.get_open_orders(REF, market=None, max_pages=2, lane=Lane.L1_ENGINE)
        assert r == Unavailable(200, "truncated")  # never a partial list
        assert len(calls) == 2
        # a full page that cannot move the cursor back (all in one microsecond)
        same = [_order_row(i, base) for i in range(5)]
        client, calls, _ = _client({("GET", "/v1/openOrders"): resp(200, {"orders": same})})
        r = await client.get_open_orders(REF, market=None, limit=5, max_pages=5, lane=Lane.L1_ENGINE)
        assert r == Unavailable(200, "truncated") and len(calls) == 2

    asyncio.run(body())


def test_get_open_orders_second_page_denied():
    async def body():
        base = 1_790_000_000_000_000
        page1 = [_order_row(i, base - i) for i in range(1000)]
        client, _, _ = _client(
            {
                ("GET", "/v1/openOrders"): [
                    resp(200, {"orders": page1}),
                    fixture_resp(429, "err_429_read.json", headers={"Retry-After": "2"}),
                ]
            }
        )
        r = await client.get_open_orders(REF, market=None, lane=Lane.L1_ENGINE)
        assert r == Throttled("read_ip", 2000, ())
        # a full page whose rows lack createdAt cannot be paged: DENIED
        rows = [_order_row(i, base - i) for i in range(3)]
        for row in rows:
            row.pop("createdAt")
        client, _, _ = _client({("GET", "/v1/openOrders"): resp(200, {"orders": rows})})
        r = await client.get_open_orders(REF, market=None, limit=3, lane=Lane.L1_ENGINE)
        assert r == Unavailable(200, "schema")

    asyncio.run(body())


def test_get_open_orders_status_validation():
    async def body():
        client, calls, _ = _client({})
        for bad in (("FILLED",), (), "OPEN", ("open",), ("OPEN", "CANCELED")):
            with pytest.raises(ValueError):
                await client.get_open_orders(REF, market=None, status=bad, lane=Lane.L1_ENGINE)
        for kwargs in ({"limit": 0}, {"limit": 1001}, {"max_pages": 0}, {"limit": True}):
            with pytest.raises(ValueError):
                await client.get_open_orders(REF, market=None, lane=Lane.L1_ENGINE, **kwargs)
        assert calls == []

    asyncio.run(body())


def test_get_fills_bounds_validation():
    async def body():
        client, calls, _ = _client({("GET", "/v1/fills"): fixture_resp(200, "fills_page.json")})
        for kwargs in (
            {"from_us": 1_700_000_000_000, "to_us": None},  # milliseconds
            {"from_us": None, "to_us": 1_700_000_000},  # seconds
            {"from_us": True, "to_us": None},
            {"from_us": 1_790_000_000_000_001, "to_us": 1_790_000_000_000_000},  # from > to
        ):
            with pytest.raises(ValueError):
                await client.get_fills(REF, market=None, lane=Lane.L3_BACKGROUND, **kwargs)
        assert calls == []
        r = await client.get_fills(
            REF, market="SOL-USD", from_us=1_790_000_000_000_000, to_us=1_790_000_000_200_000, limit=500,
            lane=Lane.L3_BACKGROUND,
        )
        assert isinstance(r, Ok)
        params = calls[0].url.params
        assert (params["from"], params["to"], params["limit"], params["market"]) == (
            "1790000000000000", "1790000000200000", "500", "SOL-USD",
        )
        assert [f.created_us for f in r.value] == sorted((f.created_us for f in r.value), reverse=True)
        assert all(f.client_id is None and f.source == "rest" for f in r.value)

    asyncio.run(body())


def test_get_funding_container_and_params():
    async def body():
        client, calls, _ = _client({("GET", "/v1/funding"): fixture_resp(200, "funding_page.json")})
        r = await client.get_funding(REF, from_us=1_789_000_000_000_000, to_us=None, lane=Lane.L3_BACKGROUND)
        assert isinstance(r, Ok) and [f.payment for f in r.value] == [D("-0.0030"), D("0.0012")]
        assert calls[0].url.params["from"] == "1789000000000000" and "to" not in calls[0].url.params
        client, _, _ = _client({("GET", "/v1/funding"): resp(200, {"funding": []})})
        assert await client.get_funding(REF, from_us=None, to_us=None, lane=Lane.L3_BACKGROUND) == Unavailable(
            200, "schema"
        )

    asyncio.run(body())


def test_get_candles_sorted_final_only():
    async def body():
        client, calls, _ = _client({("GET", "/v1/candles"): fixture_resp(200, "candles_newest_first.json")})
        to_us = 1_790_000_200_000_000
        r = await client.get_candles("BTC-USD", "1m", to_us=to_us, countback=4, lane=Lane.L3_BACKGROUND)
        assert isinstance(r, Ok)
        times = [c.open_time_us for c in r.value]
        assert times == sorted(times) and all(c.is_final for c in r.value)
        params = calls[0].url.params
        assert (params["market"], params["timeframe"], params["to"], params["countback"]) == (
            "BTC-USD", "1m", str(to_us), "4",
        )
        r = await client.get_candles("BTC-USD", "1m", to_us=to_us, final_only=False, lane=Lane.L3_BACKGROUND)
        assert isinstance(r, Ok) and not r.value[-1].is_final
        for kwargs in (
            {"timeframe": "2m"},
            {"from_us": 1_790_000_000_000_000, "countback": 5},
            {"countback": 0},
            {"countback": 1501},
            {"to_us": 1_790_000_000_000},  # ms
            {"from_us": to_us + 1},
        ):
            args = {"timeframe": "1m", "to_us": to_us}
            args.update(kwargs)
            timeframe = args.pop("timeframe")
            with pytest.raises(ValueError):
                await client.get_candles("BTC-USD", timeframe, lane=Lane.L3_BACKGROUND, **args)

    asyncio.run(body())


def test_get_mids_prices_compliance():
    async def body():
        client, calls, _ = _client(
            {
                ("GET", "/v1/mids"): fixture_resp(200, "mids.json"),
                ("GET", "/v1/prices"): fixture_resp(200, "prices.json"),
                ("GET", "/v1/compliance"): fixture_resp(200, "compliance_blocked.json"),
            }
        )
        r = await client.get_mids(lane=Lane.L1_ENGINE)
        assert r == Ok({"BTC-USD": D("84636.65"), "SOL-USD": D("122.6")}, 200, 2)  # "" omitted, never 0
        r = await client.get_prices(market="BTC-USD", lane=Lane.L1_ENGINE)
        assert isinstance(r, Ok) and r.value["ETH-USD"].oracle is None and r.value["BTC-USD"].mark == D("84321.1")
        assert calls[-1].url.params["market"] == "BTC-USD"
        r = await client.get_compliance(ADDR_MIXED, lane=Lane.L2_INTERACTIVE)
        assert isinstance(r, Ok) and r.value.address_status == "BLOCKED"
        assert calls[-1].url.params["address"] == ADDR
        await client.get_compliance(None, lane=Lane.L2_INTERACTIVE)
        assert "address" not in calls[-1].url.params

    asyncio.run(body())


def test_get_rate_limit_echo_mismatch_denied():
    async def body():
        b = load_fixture("rate_limit_testnet.json")
        b["accountIndex"] = 1
        client, _, _ = _client({("GET", "/v1/rateLimit"): resp(200, b)})
        assert await client.get_rate_limit(REF, lane=Lane.L1_ENGINE) == Unavailable(200, "schema")
        client, _, mono = _client({("GET", "/v1/rateLimit"): fixture_resp(200, "rate_limit_testnet.json")})
        r = await client.get_rate_limit(REF, lane=Lane.L1_ENGINE)
        assert isinstance(r, Ok) and r.value[0].cap == 20000 and r.value[0].as_of_mono == mono.now

    asyncio.run(body())


def test_get_order_id_mismatch_denied():
    async def body():
        client, calls, _ = _client({("GET", "/v1/order/00000000000000aa"): fixture_resp(200, "order_ok.json")})
        assert await client.get_order(REF, "00000000000000aa", lane=Lane.L0_BRAKE) == Unavailable(200, "schema")
        assert schema_error_counts().get("order.orderId") == 1
        client, _, _ = _client({("GET", "/v1/order/a1b2c3d4e5f67890"): fixture_resp(404, "err_404_order.json")})
        r = await client.get_order(REF, "a1b2c3d4e5f67890", lane=Lane.L0_BRAKE)
        assert r == NotFound("order not found")  # absent is never "gone" by itself

    asyncio.run(body())


def test_get_api_keys_foreign_entry_denied():
    async def body():
        client, _, _ = _client({("GET", "/v1/apiKeys"): fixture_resp(200, "api_keys_ok.json")})
        r = await client.get_api_keys("0x" + "33" * 20, account_index=None, lane=Lane.L2_INTERACTIVE)
        assert r == Unavailable(200, "schema")
        client, _, _ = _client({("GET", "/v1/apiKeys"): fixture_resp(200, "api_keys_ok.json")})
        r = await client.get_api_keys(ADDR, account_index=None, lane=Lane.L2_INTERACTIVE)
        assert isinstance(r, Ok) and r.value[0].api_key == PUB and r.value[0].covers_account(0)
        with pytest.raises(ValueError):
            await client.get_api_keys(ADDR, account_index=10, lane=Lane.L2_INTERACTIVE)

    asyncio.run(body())


def test_read_429_notes_server_block():
    async def body():
        client, _, _ = _client({T_TIME: fixture_resp(429, "err_429_read.json", headers={"Retry-After": "2"})})
        r = await client.get_time(lane=Lane.L1_ENGINE)
        assert r == Throttled("read_ip", 2000, ())
        assert client.ip_budget.write_blocked()

    asyncio.run(body())


def test_budget_denial_makes_no_request():
    async def body():
        client, calls, _ = _client({("GET", "/v1/markets"): resp(200, {"markets": []})})
        assert client.ip_budget.try_take(800, Lane.L0_BRAKE)  # level 700 = L3 floor
        r = await client.get_markets(lane=Lane.L3_BACKGROUND)
        assert r == LocalDenied("ip_budget:L3_BACKGROUND")
        assert calls == []
        assert client.stats()["counts"] == {"markets:LocalDenied": 1}

    asyncio.run(body())


def _fill_row(i: int) -> dict:
    return {
        "tradeId": f"t-{i}", "orderId": f"{i:016x}", "marketId": 1, "marketDisplayName": "BTC-USD",
        "side": "BUY", "size": "0.0001", "price": "84500", "fee": "0.001", "role": "MAKER",
        "createdAt": 1_790_000_000_000_000 + i,
    }


def test_list_addon_charged_after():
    async def body():
        client, _, _ = _client({("GET", "/v1/fills"): resp(200, {"fills": [_fill_row(i) for i in range(1000)]})})
        before = client.ip_budget.level()
        r = await client.get_fills(REF, market=None, from_us=None, to_us=None, lane=Lane.L3_BACKGROUND)
        assert isinstance(r, Ok) and r.weight_charged == 70  # "20 + 50 = 70"
        assert before - client.ip_budget.level() == 70
        # the add-on is owed even when the page fails to parse (the venue charged it)
        rows = [_fill_row(i) for i in range(1000)]
        rows[5]["fee"] = "NaN"
        client, _, _ = _client({("GET", "/v1/fills"): resp(200, {"fills": rows})})
        before = client.ip_budget.level()
        r = await client.get_fills(REF, market=None, from_us=None, to_us=None, lane=Lane.L3_BACKGROUND)
        assert r == Unavailable(200, "schema")
        assert before - client.ip_budget.level() == 70

    asyncio.run(body())


def test_schema_drift_is_denied():
    async def body():
        client, _, _ = _client({("GET", "/v1/openOrders"): resp(200, {"orders": "x"})})
        assert await client.get_open_orders(REF, market=None, lane=Lane.L1_ENGINE) == Unavailable(200, "schema")
        client, _, _ = _client({("GET", "/v1/time"): resp(200, content=b"<html>not json</html>")})
        assert await client.get_time(lane=Lane.L1_ENGINE) == Unavailable(200, "schema")
        assert schema_error_counts().get("time.body") == 1
        client, _, _ = _client({("GET", "/v1/time"): resp(200, content=b'{"timeNs": NaN}')})
        assert await client.get_time(lane=Lane.L1_ENGINE) == Unavailable(200, "schema")

    asyncio.run(body())


def test_read_transport_errors_and_redirects_are_denied():
    async def body():
        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        client, _, _ = _client({T_TIME: slow})
        assert await client.get_time(lane=Lane.L1_ENGINE) == Unavailable(0, "ReadTimeout")
        client, calls, _ = _client({T_TIME: resp(302, headers={"Location": "https://evil.example/v1/time"})})
        assert await client.get_time(lane=Lane.L1_ENGINE) == Unavailable(302, "unexpected_status")
        assert len(calls) == 1

    asyncio.run(body())


_LINE_429 = re.compile(r"^arcus_http_429 net=testnet path=\S+ reason=(none|ip) retry_after_ms=\d+$")


def test_429_log_line(caplog):
    async def body():
        client, calls, mono = _client(
            {
                T_TIME: fixture_resp(429, "err_429_read.json", headers={"Retry-After": "2"}),
                T_PLACE: fixture_resp(429, "err_429_ip_write.json"),
            }
        )
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            await client.get_time(lane=Lane.L0_BRAKE)
            mono.advance(2.0)  # the block expires; the next call reaches the server again
            await client.get_time(lane=Lane.L0_BRAKE)  # same (path, layer) within 10 s: silent
        assert len(calls) == 2
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("arcus_http_429")]
        assert lines == ["arcus_http_429 net=testnet path=time reason=none retry_after_ms=2000"]
        caplog.clear()
        mono.advance(2.0)  # let the write through to the server
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            await client.place_order(golden_auth(), v1_spec(reduce_only=True))
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("arcus_http_429")]
        assert lines == ["arcus_http_429 net=testnet path=placeOrder reason=ip retry_after_ms=2000"]
        for line in lines:
            assert _LINE_429.match(line)
            assert ADDR not in line and "nb7ps" not in line and PUB not in line

    asyncio.run(body())


def test_429_log_line_never_echoes_unknown_reason(caplog):
    async def body():
        client, _, _ = _client({T_PLACE: resp(429, {"error": "rate limited", "reason": "x\ninjected"})})
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            out = await client.place_order(golden_auth(), v1_spec(reduce_only=True))
        assert isinstance(out, Throttled) and out.layer == "unknown"
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("arcus_http_429")]
        assert lines == ["arcus_http_429 net=testnet path=placeOrder reason=unknown retry_after_ms=1000"]

    asyncio.run(body())


def test_logs_never_contain_secrets(caplog):
    async def body():
        client, _, _ = _client(
            {
                T_PLACE: fixture_resp(202, "place_202.json"),
                T_CANCEL: fixture_resp(202, "cancel_202.json"),
                T_BATCH: fixture_resp(202, "batch_cancel_202.json"),
                T_LEV: fixture_resp(200, "set_leverage_200.json"),
                ("GET", "/v1/account"): fixture_resp(200, "account_ok.json"),
                ("GET", "/v1/positions"): fixture_resp(503, "err_503.json"),
            }
        )
        auth = golden_auth()
        with caplog.at_level(logging.DEBUG):
            await client.place_order(auth, v1_spec())
            await client.cancel_order(auth, CancelSpec(1, order_id="a1b2c3d4e5f67890"))
            await client.batch_cancel(
                auth, [CancelSpec(1, client_id="nb7ps_2s-2"), CancelSpec(3, order_id="00000000000000ff")]
            )
            await client.set_leverage(auth, 1, 5)
            await client.get_account(REF, lane=Lane.L2_INTERACTIVE)
            await client.get_positions(REF, market=None, lane=Lane.L2_INTERACTIVE)
        # main.py pins httpx's own request logger to WARNING; the contract here is
        # that OUR package never logs a secret, at any level.
        text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("src.nadobro.venue.arcus"))
        assert "arcus testnet placeOrder" in text  # the INFO lines exist
        for secret in (SEED, PUB, ADDR, ADDR_MIXED, V1_SIG, V3_SIG, V5_SIG, V6_SIG, V7_SIG, "X-Signature", "orderSide",
                       "temporarily disabled"):
            assert secret not in text, secret
        assert repr(auth) == "ArcusAuth(<redacted>)"

    asyncio.run(body())


def test_build_transport_ipv4(monkeypatch):
    seen: list[dict] = []

    class Recorder:
        def __init__(self, **kwargs) -> None:
            seen.append(kwargs)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", Recorder)
    build_transport(force_ipv4=True)
    build_transport(force_ipv4=False)
    assert seen[0]["local_address"] == "0.0.0.0" and seen[0]["retries"] == 0
    assert seen[1]["local_address"] is None and seen[1]["retries"] == 0
    assert all(kw["http2"] is False for kw in seen)


def test_client_does_not_follow_redirects_or_trust_env(monkeypatch):
    async def body():
        mono = FakeMono()
        monkeypatch.setenv("ARCUS_FORCE_IPV4", "1")
        pinned: list[bool] = []
        real = C.build_transport

        def spy(*, force_ipv4: bool):
            pinned.append(force_ipv4)
            return real(force_ipv4=force_ipv4)

        monkeypatch.setattr(C, "build_transport", spy)
        client = ArcusClient(
            "testnet",
            clock=ArcusClock("testnet", monotonic=mono),
            ip_budget=IpBudget("testnet", clock=mono),
        )
        http = client._http
        assert http.follow_redirects is False
        assert http.trust_env is False
        assert http.headers["User-Agent"] == "nadobro-arcus/1"
        assert "Origin" not in http.headers and "Referer" not in http.headers
        assert pinned == [True]  # ARCUS_FORCE_IPV4 read through feature_flags
        assert client._base == "https://api.testnet.arcus.xyz"
        await client.aclose()
        assert http.is_closed

    asyncio.run(body())


def test_client_construction_validation():
    mono = FakeMono()
    clock = ArcusClock("testnet", monotonic=mono)
    budget = IpBudget("testnet", clock=mono)
    with pytest.raises(ValueError):
        ArcusClient("mainnet", clock=clock, ip_budget=IpBudget("mainnet"))  # clock of another network
    with pytest.raises(ValueError):
        ArcusClient("testnet", clock=clock, ip_budget=IpBudget("mainnet"))
    with pytest.raises(ValueError):
        ArcusClient("arcus_testnet", clock=clock, ip_budget=budget)
    for bad in ("http://api.testnet.arcus.xyz", "https://u:p@api.arcus.xyz", "http://127.0.0.1.evil.example",
                "https://api.arcus.xyz/?x=1"):
        with pytest.raises(ValueError):
            ArcusClient("testnet", clock=clock, ip_budget=budget, http=httpx.AsyncClient(), base_url=bad)
    ok = ArcusClient("testnet", clock=clock, ip_budget=budget, http=httpx.AsyncClient(), base_url="http://127.0.0.1:8080/")
    assert ok._base == "http://127.0.0.1:8080"


def test_get_raw_weights():
    async def body():
        client, calls, _ = _client(
            {
                ("GET", "/v1/l2OrderBook/BTC-USD"): fixture_resp(200, "l2_btc.json"),
                ("GET", "/v1/fills"): resp(200, {"fills": [_fill_row(i) for i in range(1000)]}),
                ("GET", "/health"): resp(200, {"status": "ok"}),
            }
        )
        before = client.ip_budget.level()
        r = await client.get_raw("l2OrderBook", "/v1/l2OrderBook/BTC-USD", {"nLevels": "100"})
        assert isinstance(r, Ok) and r.weight_charged == 7 and before - client.ip_budget.level() == 7
        before = client.ip_budget.level()
        r = await client.get_raw("fills", "/v1/fills", {"address": ADDR, "accountIndex": "0", "limit": "1000"},
                                 max_wait_s=5)
        assert isinstance(r, Ok) and r.weight_charged == 70 and before - client.ip_budget.level() == 70
        assert isinstance(r.value, dict) and len(r.value["fills"]) == 1000  # raw JSON as-is
        r = await client.get_raw("health", "/health")
        assert r == Ok({"status": "ok"}, 200, 0)
        n = len(calls)
        for args in (
            ("placeOrder", "/v1/placeOrder"),
            ("setLeverage", "/v1/setLeverage"),
            ("nope", "/v1/time"),
            ("time", "/v2/time"),
            ("time", "/v1/../admin"),
            ("time", "https://evil.example/v1/time"),
            ("time", "/v1/time?x=1"),
        ):
            with pytest.raises(ValueError):
                await client.get_raw(*args)
        with pytest.raises(ValueError):
            await client.get_raw("time", "/v1/time", {"a": 1})  # type: ignore[dict-item]
        assert len(calls) == n

    asyncio.run(body())


def test_backward_page_cursor():
    assert backward_page_cursor([5, 4, 3], limit=4, prev_to_us=None) is None
    assert backward_page_cursor([5, 4, 3], limit=3, prev_to_us=None) == 3
    assert backward_page_cursor([5, 4, 3], limit=3, prev_to_us=9) == 3
    assert backward_page_cursor([3, 3, 3], limit=3, prev_to_us=3) == "stuck"
    assert backward_page_cursor([7, 6, 5], limit=3, prev_to_us=5) == "stuck"
    assert backward_page_cursor([], limit=1, prev_to_us=None) is None
    with pytest.raises(ValueError):
        backward_page_cursor([1], limit=0, prev_to_us=None)


def test_reexports_view_types():
    from src.nadobro.venue.arcus import parse as P

    for name in ("ComplianceView", "ApiKeyEntry", "LeverageEntry", "BboView", "L2BookView", "PriceView", "CandleRow"):
        assert getattr(C, name) is getattr(P, name)
        assert name in C.__all__
    for forbidden in ("cancel_all_orders", "modify_order", "batch_place", "schedule_cancel", "create_api_key",
                      "revoke_api_key", "withdraw", "transfer", "adjust_isolated_margin"):
        assert not hasattr(ArcusClient, forbidden)


def test_write_log_line_keeps_only_enum_tokens(caplog):
    async def body():
        b = load_fixture("place_200_rejected.json")
        b["rejectionReason"] = "sent by 0xabcdef1234567890abcdef1234567890abcdef12"
        client, _, _ = _place(resp(200, json.loads(json.dumps(b, default=str))))
        with caplog.at_level(logging.INFO, logger=C.__name__):
            out = await client.place_order(golden_auth(), v1_spec())
        assert isinstance(out, Accepted) and out.status == "REJECTED"  # the outcome keeps the text
        lines = [r.getMessage() for r in caplog.records if r.name == C.__name__]
        assert lines == ["arcus testnet placeOrder m=1 cid=nb7ps_2s-1 -> 200 Accepted REJECTED ?"]

    asyncio.run(body())
