"""scripts/capture_arcus_shapes.py + scripts/arcus_testnet_probe.py (02 §11, §12.11).

No network anywhere: every client runs over ``httpx.MockTransport`` (an autouse
guard makes a real transport call fail the test). The probe is OWNER-RUN only;
here it runs end to end against ``FakeVenue`` — a small stateful model of the
documented REST semantics (202 ACK, async OPEN/CANCELED frames on a fake
WebSocket, OracleDeviation / notional / size 400s, ORDER_NOT_FOUND) — so the
real preflight, placement, cleanup and report code paths are exercised.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from arcus_helpers import (
    ADDR,
    ADDR_MIXED,
    CT0,
    GTT,
    PUB,
    REST_BASE,
    SEED,
    FakeMono,
    FakeSleep,
    FakeTimeNs,
    golden_auth,
    load_fixture,
    mock_client,
    resp,
)
from src.nadobro.venue.arcus.budget import IpBudget
from src.nadobro.venue.arcus.catalog import ArcusCatalog, parse_market
from src.nadobro.venue.arcus.client import ArcusClient
from src.nadobro.venue.arcus.clock import ArcusClock
from src.nadobro.venue.arcus.errors import Accepted, Rejected, Unauthorized, Unavailable
from src.nadobro.venue.arcus.parse import (
    BboView,
    parse_api_keys,
    parse_bbo,
    parse_candles,
    parse_compliance,
    parse_fills_payload,
    parse_funding_payload,
    parse_l2,
    parse_leverages,
    parse_markets_payload,
    parse_mids,
    parse_open_orders_payload,
    parse_positions_payload,
    parse_prices,
    parse_rate_limit,
    parse_time,
)
from src.nadobro.venue.arcus.signing import Ed25519Signer
from src.nadobro.venue.arcus.types import (
    ArcusAccountRef,
    CancelSpec,
    OrderRow,
    OrderSpec,
    Side,
    Tif,
    WireOrderType,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "arcus"
D = Decimal
DEAD = "0x000000000000000000000000000000000000dead"
ADDR_BODY = ADDR[2:]
SIG128_RE = re.compile(r"[0-9a-f]{128}")


def _load_script(name: str) -> Any:
    mod_name = f"_arcus_script_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


probe = _load_script("arcus_testnet_probe")
cap = _load_script("capture_arcus_shapes")


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    async def boom(self, request):  # pragma: no cover - a test that reaches it fails
        raise AssertionError("real network in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "FLY_APP_NAME", "FLY_MACHINE_ID", "ARCUS_TESTNET_REST_URL", "ARCUS_TESTNET_WS_URL",
        "ARCUS_MAINNET_REST_URL", "ARCUS_MAINNET_WS_URL", "ARCUS_FORCE_IPV4", "ARCUS_MARKET_ALLOWLIST",
        "ARCUS_GTT_DAYS", "ARCUS_CLOCK_MAX_AGE_S", "ARCUS_CATALOG_MAX_AGE_S", "ARCUS_PROBE_SIGNING_KEY",
        "ARCUS_PROBE_NETWORK", "ARCUS_PROBE_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)


def _markets(name: str = "markets_testnet_subset.json") -> dict[str, Any]:
    return {m.ticker: m for m in (parse_market(r) for r in parse_markets_payload(load_fixture(name)))}


MKT = _markets()
BTC, ETH, SOL = MKT["BTC-USD"], MKT["ETH-USD"], MKT["SOL-USD"]
ORACLE = D("84532.4")  # probe:markets_api.testnet.json
CAPTURED_BID, CAPTURED_ASK = D("74072.5"), D("78654.4")  # probe:probe_log_20260927.txt


def _bbo(bid: str | None, ask: str | None) -> BboView:
    return BboView(
        bid=None if bid is None else D(bid), ask=None if ask is None else D(ask),
        bid_size=None if bid is None else D(1), ask_size=None if ask is None else D(1), timestamp_us=None,
    )


# =====================================================================================
# Fake venue (documented semantics only) + fake WebSocket
# =====================================================================================

_IDS = {1: "BTC-USD", 2: "ETH-USD", 3: "SOL-USD"}


class FakeWs:
    def __init__(self, venue: "FakeVenue") -> None:
        self.q: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.venue = venue
        venue.ws = self

    async def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        if msg.get("type") == "subscribe":
            channel = msg["channel"]
            contents: dict[str, Any] = {"isSnapshot": True, "positions": self.venue.position_map()} if channel in ("account", "positions") else {}
            self.push({"type": "subscribed", "channel": channel, "id": msg["id"].lower(), "accountIndex": 0, "contents": contents})
            if channel == "account":
                for _ in range(3):
                    self.venue.push_account_snapshot()

    def push(self, frame: dict[str, Any]) -> None:
        self.q.put_nowait(json.dumps(frame))

    async def recv(self) -> str:
        return await self.q.get()

    async def close(self) -> None:
        return None


class FakeVenue:
    """Stateful model of the documented REST semantics used by the probe."""

    def __init__(
        self,
        *,
        bbo: dict[str, tuple[str | None, str | None]] | None = None,
        oracle_band_bp: int = 800,
        permissions: list[str] | None = None,
        key_status: str = "ACTIVE",
        valid_until_ms: int | None = None,
        key_account_index: int = 0,
        key_listed: bool = True,
        account: str = "ok",
        positions: dict[str, Any] | None = None,
        time_status: int = 200,
        cancel_status: int = 202,
        hide_polls: int = 0,
        rate_used: list[int] | None = None,
        reject_market: bool = False,
        reject_reduce_only: bool = False,
    ) -> None:
        self.reject_market = reject_market  # every MARKET order -> 400 (tests the LIMIT IOC fallback)
        self.reject_reduce_only = reject_reduce_only  # every reduce-only order -> 400 (a stuck position)
        self.pos: dict[int, D] = {}
        self.entry: dict[int, D] = {}
        self.fills: list[dict[str, Any]] = []
        self.markets = load_fixture("markets_testnet_subset.json")
        self.market = {m.market_id: m for m in MKT.values()}
        self.bbo = bbo or {"BTC-USD": ("84500", "84600"), "ETH-USD": ("2684", "2687"), "SOL-USD": ("122.6", "122.8")}
        self.oracle_band_bp = oracle_band_bp
        self.permissions = permissions
        self.key_status = key_status
        self.valid_until_ms = valid_until_ms if valid_until_ms is not None else int(time.time() * 1000) + 100 * 86_400_000
        self.key_account_index = key_account_index
        self.key_listed = key_listed
        self.account = account
        self.positions = positions or {}
        self.time_status = time_status
        self.cancel_status = cancel_status
        self.hide_polls = hide_polls
        self.rate_used = list(rate_used or [])
        self.order_used = 0
        self.orders: dict[str, dict[str, Any]] = {}
        self.cid_to_oid: dict[str, str] = {}
        self.polls: dict[str, int] = {}
        self.requests: list[httpx.Request] = []
        self.ws: FakeWs | None = None
        self._n = 0

    @property
    def posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST"]

    # -- helpers --
    def _now_us(self) -> int:
        return time.time_ns() // 1000

    def _row(self, o: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def _push_order(self, o: dict[str, Any]) -> None:
        if self.ws is not None:
            self.ws.push({"type": "channel_data", "channel": "orders", "id": ADDR, "accountIndex": 0, "market": o["marketDisplayName"], "contents": self._row(o)})

    def _echo(self, mid: int) -> dict[str, Any]:
        return {"address": ADDR, "accountIndex": 0, "marketId": mid, "marketDisplayName": _IDS[mid]}

    def position_row(self, mid: int) -> dict[str, Any]:
        size = self.pos.get(mid, D(0))
        return {"address": ADDR, "accountIndex": 0, "marketId": mid, "marketDisplayName": _IDS[mid],
                "side": "LONG" if size >= 0 else "SHORT", "size": str(size), "averageEntryPrice": str(self.entry.get(mid, D(0))),
                "leverage": "40", "marginMode": "CROSS", "markPx": str(self.market[mid].mark_price)}

    def position_map(self) -> dict[str, Any]:
        if self.positions:
            return self.positions
        return {str(mid): self.position_row(mid) for mid, size in self.pos.items() if size != 0}

    def push_account_snapshot(self) -> None:
        if self.ws is not None:
            self.ws.push({"type": "channel_data", "channel": "account", "id": ADDR, "accountIndex": 0,
                          "contents": {"isSnapshot": True, "positions": self.position_map()}})

    def _fill(self, order: dict[str, Any], price: D, size: D) -> None:
        mid = order["marketId"]
        signed = size if order["side"] == "BUY" else -size
        before = self.pos.get(mid, D(0))
        after = before + signed
        if before == 0 or (before > 0) == (signed > 0):
            self.entry[mid] = price if before == 0 else (self.entry[mid] * abs(before) + price * size) / abs(after)
        self.pos[mid] = after
        self._n += 1
        fill = {"tradeId": f"t{self._n}", "orderId": order["orderId"], "clientId": order["clientId"], "address": ADDR,
                "accountIndex": 0, "marketId": mid, "marketDisplayName": _IDS[mid], "side": order["side"],
                "size": str(size), "price": str(price), "fee": str((price * size * D("0.00045")).quantize(D("0.000001"))),
                "role": "TAKER", "createdAt": self._now_us()}
        self.fills.append(fill)
        remaining = D(order["originalSize"]) - size
        order.update(remainingSize=str(remaining), avgFillPrice=str(price),
                     status="FILLED" if remaining == 0 else "CANCELED", state="FILLED" if remaining == 0 else "PARTIALLY_FILLED")
        if self.ws is not None:
            rest = {k: v for k, v in fill.items() if k != "clientId"}
            self.ws.push({"type": "channel_data", "channel": "userFills", "id": ADDR, "accountIndex": 0, "contents": {**rest, "clientId": order["clientId"]}})
            self.ws.push({"type": "channel_data", "channel": "positions", "id": ADDR, "accountIndex": 0,
                          "contents": {"isSnapshot": False, "positions": [self.position_row(mid)]}})
            self.push_account_snapshot()

    def _find(self, body: dict[str, Any]) -> dict[str, Any] | None:
        oid = body.get("orderId") if body.get("kind") == "orderId" else self.cid_to_oid.get(body.get("clientId", ""))
        return self.orders.get(oid or "")

    # -- routing --
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content)
            if path == "/v1/placeOrder":
                return self._place(body)
            if path == "/v1/cancelOrder":
                return self._cancel(body)
            if path == "/v1/batchCancelOrders":
                return self._batch(body)
            if path == "/v1/setLeverage":
                return httpx.Response(200, json={"requestId": "r1", **self._echo(body["marketId"]), "leverage": body["leverage"], "status": "APPLIED"})
            raise AssertionError(f"unexpected POST {path}")
        q = request.url.params
        if path == "/v1/time":
            if self.time_status != 200:
                return httpx.Response(self.time_status, json={"error": "unavailable", "errorType": "Unavailable"})
            return httpx.Response(200, json={"timeNs": time.time_ns()})
        if path == "/v1/markets":
            return httpx.Response(200, content=json.dumps(self.markets, default=str).encode())
        if path == "/v1/apiKeys":
            keys = []
            if self.key_listed:
                entry = {"apiKey": PUB, "address": ADDR, "allSubaccounts": False, "accountIndex": self.key_account_index,
                         "apiWalletName": "nadobro-probe-20260930", "status": self.key_status,
                         "validUntil": self.valid_until_ms, "createdAt": 1790000000000000}
                if self.permissions is not None:
                    entry["permissions"] = self.permissions
                keys.append(entry)
            return httpx.Response(200, json={"apiKeys": keys})
        if path == "/v1/account":
            if self.account == "no_activity":
                return httpx.Response(404, json={"error": "this account has no activity yet"})
            if self.account == "whitelist":
                return httpx.Response(403, json={"error": "address not on access whitelist"})
            return httpx.Response(200, content=(FIXTURES / "account_ok.json").read_bytes())
        if path == "/v1/positions":
            pmap = self.position_map()
            if "market" in q:
                pmap = {k: v for k, v in pmap.items() if v["marketDisplayName"] == q["market"]}
            return httpx.Response(200, json={"positions": pmap, "total": len(pmap)})
        if path == "/v1/openOrders":
            rows = [self._row(o) for o in self.orders.values() if o["state"] == "OPEN"]
            if "market" in q:
                rows = [r for r in rows if r["marketDisplayName"] == q["market"]]
            return httpx.Response(200, json={"orders": rows, "total": len(rows)})
        if path.startswith("/v1/order/"):
            oid = path.rsplit("/", 1)[-1]
            self.polls[oid] = self.polls.get(oid, 0) + 1
            if oid not in self.orders or self.polls[oid] <= self.hide_polls:
                return httpx.Response(404, json={"error": "order not found"})
            return httpx.Response(200, json=self._row(self.orders[oid]))
        if path.startswith("/v1/bbo/"):
            bid, ask = self.bbo[path.rsplit("/", 1)[-1]]
            return httpx.Response(200, json={
                "bestBid": None if bid is None else {"price": bid, "size": "1"},
                "bestAsk": None if ask is None else {"price": ask, "size": "1"},
                "lastSequenceId": 1, "globalSequenceId": 1, "timestamp": self._now_us()})
        if path == "/v1/leverages":
            return httpx.Response(200, content=(FIXTURES / "leverages_ok.json").read_bytes())
        if path == "/v1/rateLimit":
            used = self.rate_used.pop(0) if self.rate_used else self.order_used
            return httpx.Response(200, json={"address": ADDR, "accountIndex": 0,
                                             "order": {"used": used, "cap": 20000, "nextAvailableMs": 0},
                                             "cancel": {"used": 0, "cap": 40000, "nextAvailableMs": 0}})
        if path == "/v1/fills":
            rows = [{k: v for k, v in f.items() if k != "clientId"} for f in self.fills]  # REST fills carry no clientId
            if "from" in q:
                rows = [r for r in rows if r["createdAt"] >= int(q["from"])]
            if "market" in q:
                rows = [r for r in rows if r["marketDisplayName"] == q["market"]]
            rows.sort(key=lambda r: r["createdAt"], reverse=True)
            return httpx.Response(200, json={"fills": rows, "total": len(rows)})
        raise AssertionError(f"unexpected GET {path}")

    def _place(self, body: dict[str, Any]) -> httpx.Response:
        self.order_used += 1
        mid = body["marketId"]
        market = self.market[mid]
        price, qty = D(body["price"]), D(body["quantity"])
        oracle = market.oracle_price
        reduce_only = body["reduceOnly"]
        if body["orderType"] == "MARKET" and (self.reject_market or abs(price - market.mark_price) > market.mark_price * D("0.10")):
            return httpx.Response(400, json={"error": "slippage bound", "errorSource": "Order", "errorType": "MarketPriceSlippageToleranceTooHigh"})
        if reduce_only and self.reject_reduce_only:
            return httpx.Response(400, json={"error": "reduce only", "errorSource": "Order", "errorType": "ReduceOnly"})
        if body["orderType"] == "LIMIT" and abs(price - oracle) / oracle * 10_000 > self.oracle_band_bp:
            return httpx.Response(400, json={"error": "price deviates from oracle", "errorSource": "Order", "errorType": "OracleDeviation"})
        if not reduce_only and qty * price < market.min_order_notional:
            return httpx.Response(400, json={"error": "order notional below minimum", "errorSource": "Order", "errorType": "InvalidRequest"})
        if qty < market.min_order_size:
            return httpx.Response(400, json={"error": "size below minimum", "errorSource": "Order", "errorType": "InvalidRequest"})
        self._n += 1
        oid = f"{self._n:016x}"
        bid, ask = self.bbo[_IDS[mid]]
        crosses = (body["orderSide"] == "BUY" and ask is not None and price >= D(ask)) or (
            body["orderSide"] == "SELL" and bid is not None and price <= D(bid))
        now = self._now_us()
        order = {"orderId": oid, "clientId": body["clientId"], "marketId": mid, "marketDisplayName": _IDS[mid],
                 "side": body["orderSide"], "status": "OPEN", "state": "OPEN", "price": body["price"],
                 "originalSize": body["quantity"], "remainingSize": body["quantity"], "timeInForce": body["timeInForce"],
                 "createdAt": now, "updatedAt": now}
        if reduce_only:
            order["reduceOnly"] = True
        self.orders[oid] = order
        self.cid_to_oid[body["clientId"]] = oid
        held = self.pos.get(mid, D(0))
        reduces = (body["orderSide"] == "SELL" and held > 0) or (body["orderSide"] == "BUY" and held < 0)
        if reduce_only and not reduces:
            order.update(status="REJECTED", state="REJECTED", rejectionReason="REDUCE_ONLY_WOULD_INCREASE")
        elif body["timeInForce"] == "ALO" and crosses:
            order.update(status="REJECTED", state="REJECTED", rejectionReason="POST_ONLY_WOULD_CROSS")
        elif body["timeInForce"] == "IOC":
            if crosses:  # fills at the touch; reduce-only is clipped to the position
                size = min(qty, abs(held)) if reduce_only else qty
                self._fill(order, D(ask) if body["orderSide"] == "BUY" else D(bid), size)
                if order["state"] != "FILLED":
                    order.update(state="PARTIALLY_FILLED")
            else:
                order.update(status="REJECTED", state="REJECTED", rejectionReason="IOC_CANCELED")
        self._push_order(order)
        return httpx.Response(202, json={**self._echo(mid), "orderId": oid, "clientId": body["clientId"], "status": "ACK",
                                         "rateLimit": {"pool": "order", "remaining": 20000 - self.order_used}})

    def _do_cancel(self, target: dict[str, Any]) -> dict[str, Any]:
        order = self._find(target)
        echo = {**self._echo(target["marketId"]), ("orderId" if target["kind"] == "orderId" else "clientId"): target.get("orderId") or target.get("clientId"), "updateTime": self._now_us()}
        if order is None or order["state"] != "OPEN":
            return {**echo, "status": "REJECTED", "rejectionReason": "ORDER_NOT_FOUND"}
        order.update(status="CANCELED", state="CANCELED", updatedAt=self._now_us())
        self._push_order(order)
        return {**echo, "status": "CANCEL_ACKNOWLEDGED"}

    def _cancel(self, body: dict[str, Any]) -> httpx.Response:
        if self.cancel_status != 202:
            return httpx.Response(self.cancel_status, json={"error": "unavailable", "errorType": "Unavailable"})
        row = self._do_cancel(body)
        status = 200 if row["status"] == "REJECTED" else 202
        return httpx.Response(status, json={**row, "rateLimit": {"pool": "cancel", "remaining": 39999}})

    def _batch(self, body: dict[str, Any]) -> httpx.Response:
        if self.cancel_status != 202:
            return httpx.Response(self.cancel_status, json={"error": "unavailable", "errorType": "Unavailable"})
        rows = [self._do_cancel(t) for t in body["cancels"]]
        return httpx.Response(202, json={"responses": rows, "rateLimit": {"pool": "cancel", "remaining": 39990}})


class YieldingSleep(FakeSleep):
    """Fake time, but still yields to the loop (a real sleep lets the WS reader run)."""

    async def __call__(self, seconds: float) -> None:
        await super().__call__(seconds)
        await asyncio.sleep(0)


def make_factory(venue: FakeVenue, *, ws: bool = True) -> tuple[Any, dict[str, Any]]:
    state: dict[str, Any] = {"calls": 0, "signers": 0}

    def factory(net: str) -> Any:
        state["calls"] += 1
        clock = ArcusClock(net)
        http = httpx.AsyncClient(transport=httpx.MockTransport(venue.handler), base_url=REST_BASE)
        client = ArcusClient(net, clock=clock, ip_budget=IpBudget(net), http=http, clock_max_age_s=lambda: 900.0)
        raw = httpx.AsyncClient(transport=httpx.MockTransport(venue.handler))

        def make_signer(seed: str) -> Ed25519Signer:
            state["signers"] += 1
            return Ed25519Signer.from_seed_hex(seed)

        async def connect(url: str, *, family: int) -> FakeWs:
            state["ws_url"] = url
            return FakeWs(venue)

        mono = FakeMono()
        return probe.ProbeServices(
            client=client, catalog=ArcusCatalog(net), raw_http=raw, ws_connect=connect if ws else None,
            make_signer=make_signer, monotonic=mono, sleep=YieldingSleep(mono), placement_spacing_s=0.0,
            ws_wait_s=2.0, owns_raw_http=True,
        )

    return factory, state


def run_probe(monkeypatch, tmp_path, argv, venue, *, key: str | None = SEED, address: str = ADDR_MIXED,
              network: str | None = "testnet", ws: bool = True) -> tuple[int, dict[str, Any]]:
    if network is not None:
        monkeypatch.setenv("ARCUS_PROBE_NETWORK", network)
    monkeypatch.setenv("ARCUS_PROBE_ADDRESS", address)
    if key is not None:
        monkeypatch.setenv("ARCUS_PROBE_SIGNING_KEY", key)
    factory, state = make_factory(venue, ws=ws)
    code = probe.main([*argv, "--out", str(tmp_path)], services_factory=factory)
    return code, state


def _report(tmp_path: Path, sub: str) -> tuple[str, dict[str, Any]]:
    files = sorted(tmp_path.glob(f"{sub}_*.json"))
    assert len(files) == 1, files
    text = files[0].read_text()
    return text, json.loads(text)


def _assert_no_secrets(text: str) -> None:
    low = text.lower()
    assert ADDR_BODY not in low
    assert SEED not in low and PUB not in low
    assert not SIG128_RE.search(low)


# =====================================================================================
# Import / argument parsing
# =====================================================================================


def test_scripts_import_with_empty_env_and_no_side_effects(tmp_path):
    code = (
        "import importlib.util, sys\n"
        "for n in ('arcus_testnet_probe', 'capture_arcus_shapes'):\n"
        "    s = importlib.util.spec_from_file_location('_' + n, 'scripts/%s.py' % n)\n"
        "    m = importlib.util.module_from_spec(s); sys.modules[s.name] = m; s.loader.exec_module(m)\n"
        "    assert callable(m.main)\n"
        "print('ok')\n"
    )
    before = set(REPO.joinpath("tests", "fixtures", "arcus", "g1").iterdir())
    proc = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=str(REPO), env={"PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "ok"
    assert set(REPO.joinpath("tests", "fixtures", "arcus", "g1").iterdir()) == before


def test_probe_argument_parsing():
    parser = probe.build_parser()
    args = parser.parse_args(["sign-check"])
    assert (args.market, args.far_bp, args.max, args.n, args.trades, args.allow_fly) == ("BTC-USD", 500, 60, None, False, False)
    assert args.out.endswith(os.path.join("tests", "fixtures", "arcus", "g1"))
    args = parser.parse_args(["open-order-cap", "--max", "120", "--market", " eth-usd ", "--far-bp", "300", "--i-understand-this-trades"])
    assert (args.max, args.market, args.far_bp, args.trades) == (120, "ETH-USD", 300, True)
    assert parser.parse_args(["pool-watch", "--interval", "600", "--hours", "72"]).hours == 72.0
    for bad in (["nope"], ["sign-check", "--far-bp", "10"], ["sign-check", "--far-bp", "5000"],
                ["sign-check", "--max", "301"], ["sign-check", "--max", "0"], ["pool-watch", "--hours", "0"],
                ["pool-watch", "--interval", "1"], ["sign-check", "--market", "BTC USD"], ["ack-latency", "--n", "0"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)
    assert set(probe.SUBCOMMANDS) - {"all"} == set(probe.SUB_FUNCS)
    for sub in ("alo-cross", "alo-reduce-only", "ioc-reduce-only", "ct-order", "charged-400", "default-leverage",
                "sign-check", "oracle-band", "open-order-cap", "cancel-race", "min-size", "tradeid-parity",
                "fee-sign", "entry-units", "ws-fresh", "pool-watch", "ack-latency", "ack-404-window"):
        assert sub in probe.SUBCOMMANDS
    assert [name for name, _ in probe.ALL_SEQUENCE] == [
        "sign-check", "ct-order", "ack-404-window", "cancel-race", "oracle-band", "min-size",
        "charged-400", "default-leverage", "ack-latency"]


def test_capture_argument_parsing():
    parser = cap.build_parser()
    args = parser.parse_args([])
    assert (args.network, args.market, args.address, args.out) == ("testnet", "BTC-USD", None, None)
    assert parser.parse_args(["--address", ADDR_MIXED]).address == ADDR
    for bad in (["--network", "arcus_testnet"], ["--network", "Testnet"], ["--address", "0x12"], ["--market", "a b"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


# =====================================================================================
# Pure pricing / sizing
# =====================================================================================


def test_probe_price_spec_cases():
    # Case 1: the captured testnet book sits ~7-12 % below the oracle, so a BUY at
    # oracle x 0.95 would cross the ask (ALO reject): the SELL side is used.
    q = probe.probe_price(ORACLE, _bbo(str(CAPTURED_BID), str(CAPTURED_ASK)), Side.BUY, 500, BTC)
    assert q == probe.ProbeQuote(Side.SELL, BTC.quantize_price(ORACLE * D("1.05"), Side.SELL))
    assert q.price > CAPTURED_BID
    # Case 2: no BBO -> the requested side.
    assert probe.probe_price(ORACLE, None, Side.BUY, 500, BTC) == probe.ProbeQuote(Side.BUY, BTC.quantize_price(ORACLE * D("0.95"), Side.BUY))
    assert probe.probe_price(ORACLE, None, Side.SELL, 500, BTC).side is Side.SELL
    # Case 3: both sides cross (only a crossed book can do that) -> None, nothing placed.
    assert probe.probe_price(ORACLE, _bbo("90000", "80000"), Side.BUY, 500, BTC) is None
    # A normal book around the oracle keeps the requested side, never mid-anchored.
    q = probe.probe_price(ORACLE, _bbo("84500", "84600"), Side.BUY, 500, BTC)
    assert q.side is Side.BUY and q.price == BTC.quantize_price(ORACLE * D("0.95"), Side.BUY) and q.price < D("84600")
    assert probe.strict_side_price(ORACLE, _bbo(str(CAPTURED_BID), str(CAPTURED_ASK)), Side.BUY, 500, BTC) is None


def _captured_books() -> list[tuple[str, Any, D, Any]]:
    """(label, market, oracle, bbo) from every real capture in the repo."""
    out = []
    for net in ("testnet", "mainnet"):
        markets = _markets(f"captured/{net}_markets_20260930.json")
        for run in sorted((FIXTURES / "captured").glob(f"{net}_2026*Z")):
            prices = parse_prices(load_fixture(f"captured/{run.name}/prices.json"))
            bbo = parse_bbo(load_fixture(f"captured/{run.name}/bbo_BTC-USD.json"))
            out.append((f"{run.name}", markets["BTC-USD"], prices["BTC-USD"].oracle, bbo))
        if net == "testnet":
            prices = parse_prices(load_fixture("captured/testnet_prices_20260930.json"))
            bbo = parse_bbo(load_fixture("captured/testnet_bbo_btc_20260930.json"))
            out.append(("testnet_bbo_btc_20260930", markets["BTC-USD"], prices["BTC-USD"].oracle, bbo))
    out.append(("probe_log_20260927", BTC, ORACLE, _bbo(str(CAPTURED_BID), str(CAPTURED_ASK))))
    return out


@pytest.mark.parametrize("label,market,oracle,bbo", _captured_books(), ids=lambda v: v if isinstance(v, str) else "")
def test_probe_price_never_crosses_the_captured_books(label, market, oracle, bbo):
    assert len(_captured_books()) >= 4
    for far_bp in range(probe.FAR_BP_MIN, probe.FAR_BP_MAX + 1, 25):
        for side in (Side.BUY, Side.SELL):
            q = probe.probe_price(oracle, bbo, side, far_bp, market)
            if q is None:
                continue
            assert market.quantize_price(q.price, q.side) == q.price  # band-valid, maker-rounded
            if q.side is Side.BUY:
                assert bbo.ask is None or q.price < bbo.ask
                assert q.price < oracle
            else:
                assert bbo.bid is None or q.price > bbo.bid
                assert q.price > oracle
            # oracle-anchored within the requested distance (+ one tick of rounding)
            assert abs(q.price - oracle) <= oracle * D(far_bp) / 10_000 + market.tick_for_price(q.price)


def test_sizes():
    price = BTC.quantize_price(ORACLE * D("0.95"), Side.BUY)
    size = probe.probe_size(BTC, price)
    assert size >= BTC.min_order_size and size * price >= max(BTC.min_order_notional, BTC.min_order_size * price) * D("1.1")
    assert (size - BTC.step_size) * price < max(BTC.min_order_notional, BTC.min_order_size * price) * D("1.1")
    sol = SOL.quantize_price(SOL.oracle_price * D("0.95"), Side.BUY)
    s2 = probe.probe_size(SOL, sol)
    assert s2 * sol >= D("5.5") and s2 >= SOL.min_order_size
    # taker size: tiny and bounded
    t = probe.taker_size(BTC, D("84769.2"), D("84600"))  # ask 84600, IOC at ask x 1.002
    assert t == BTC.min_order_size and t * D("84769.2") <= max(D("5.5"), BTC.min_order_size * D("84600") * D("1.1"))
    assert probe.taker_size(BTC, D("84700")) == BTC.min_order_size
    # a limit far above the touch would breach the touch-based notional bound -> refused
    assert probe.taker_size(BTC, D("95000"), D("84600")) is None
    t2 = probe.taker_size(SOL, D("122.9"))
    assert t2 >= SOL.min_order_size and D("5.25") <= t2 * D("122.9") <= D("5.5")
    # min-size cases (02 C19): BTC for "below minOrderSize", SOL for "below minOrderNotional"
    assert probe.below_min_size_size(BTC) == D("0.00009999")
    assert probe.below_min_size_size(BTC) * price >= BTC.min_order_notional
    n = probe.below_notional_size(SOL, sol)
    assert n >= SOL.min_order_size and n * sol < SOL.min_order_notional and (n + SOL.step_size) * sol >= SOL.min_order_notional
    assert probe.below_notional_size(BTC, price) is None  # 0.0001 BTC is already > $5
    assert probe._size_if_min_size_case(BTC, price) == D("0.00009999")
    assert probe._size_if_min_size_case(SOL, sol) is None  # confounded by the notional rule
    assert probe._size_if_notional_case(BTC, price) is None
    assert probe._size_if_notional_case(SOL, sol) == n


def test_taker_preflight_refuses_a_book_far_from_mark_or_oracle():
    assert probe.taker_preflight_reason(_bbo("84300", "84400"), BTC) is None
    for bbo in (_bbo(str(CAPTURED_BID), str(CAPTURED_ASK)),  # the captured testnet book (~7-12 % off)
                _bbo("84300", "89000"),  # ask > 500 bp above mark
                _bbo("80000", "84400"),  # bid > 500 bp below oracle/mark
                _bbo(None, "84400"), _bbo("84300", None), None):
        assert probe.taker_preflight_reason(bbo, BTC) is not None


def test_close_protective_price_is_always_inside_the_mark_band():
    mark = BTC.mark_price
    # normal book: opposite touch -/+ 100 bp
    sell = probe.close_protective_price(BTC, _bbo("84300", "84400"), Side.SELL)
    assert sell == BTC.quantize_price(D("84300") * D("0.99"), Side.SELL, crossing=True)
    buy = probe.close_protective_price(BTC, _bbo("84300", "84400"), Side.BUY)
    assert buy == BTC.quantize_price(D("84400") * D("1.01"), Side.BUY, crossing=True)
    # captured testnet book: bid*0.99 is ~13 % under mark -> falls back to mark - 9 %
    far = probe.close_protective_price(BTC, _bbo(str(CAPTURED_BID), str(CAPTURED_ASK)), Side.SELL)
    assert far == BTC.quantize_price(mark * D("0.91"), Side.SELL, crossing=True)
    for bbo in (_bbo("84300", "84400"), _bbo(str(CAPTURED_BID), str(CAPTURED_ASK)), _bbo("1", "999999"), None):
        for side in (Side.BUY, Side.SELL):
            price = probe.close_protective_price(BTC, bbo, side)
            assert price is not None and abs(price - mark) <= mark * D("0.10")
            assert probe.within_mark_band(price, mark)
    no_mark = parse_market({**load_fixture("markets_testnet_subset.json")["markets"][0], "markPrice": "0"})
    assert probe.close_protective_price(no_mark, _bbo("84300", "84400"), Side.SELL) is None
    assert not probe.within_mark_band(D("100"), None)
    assert not probe.within_mark_band(mark * D("1.1001"), mark)
    assert probe.mark_offset_price(BTC, Side.SELL, D(900)) == BTC.quantize_price(mark * D("0.91"), Side.SELL, crossing=True)


def test_percentile_and_recommendations():
    assert probe.percentile([], 50) is None
    assert probe.percentile([5.0], 99) == 5.0
    values = [float(v) for v in range(1, 101)]
    assert (probe.percentile(values, 50), probe.percentile(values, 95), probe.percentile(values, 99)) == (50.0, 95.0, 99.0)
    rec = probe.recommend_constants(400.0, 100.0)
    assert rec == {"ARCUS_ACK_GRACE_S": 2.0, "ARCUS_CANCEL_CONFIRM_S": 2.0, "ARCUS_CANCEL_WAIT_S": 0.5}
    rec = probe.recommend_constants(1500.0, 900.0)
    assert rec == {"ARCUS_ACK_GRACE_S": 4.5, "ARCUS_CANCEL_CONFIRM_S": 2.7, "ARCUS_CANCEL_WAIT_S": 1.8}
    assert probe.recommend_constants(None, None)["ARCUS_ACK_GRACE_S"] is None


def _order_row(state: str | None, status: str, tif: Tif | None = Tif.ALO) -> OrderRow:
    return OrderRow(order_id="a1", client_id="nb0_x-1", market_id=1, ticker="BTC-USD", side=Side.BUY, status=status,
                    state=state, price=D(1), original_size=D(1), remaining_size=D(1), filled_size=None,
                    avg_fill_price=None, tif=tif, reduce_only=False, rejection_reason=None, cancel_reason=None,
                    created_us=None, updated_us=10**15, sequence_number=None)


def test_terminal_evidence_is_strict():
    assert probe.is_terminal_row(_order_row("CANCELED", "CANCELED"))
    assert probe.is_terminal_row(_order_row("REJECTED", "REJECTED"))
    assert probe.is_terminal_row(_order_row(None, "MARGIN_CANCELED"))
    assert probe.is_terminal_row(_order_row("PARTIALLY_FILLED", "OPEN", Tif.IOC))  # terminal for IOC
    assert not probe.is_terminal_row(_order_row("PARTIALLY_FILLED", "OPEN", Tif.ALO))
    assert not probe.is_terminal_row(_order_row("OPEN", "OPEN"))
    assert not probe.is_terminal_row(_order_row(None, "ACK"))
    assert not probe.is_terminal_row(_order_row(None, "CANCEL_ACKNOWLEDGED"))  # ACK != done


def test_race_label():
    ack = Accepted(202, "o", "c", "ACK", None, None)
    nf = Accepted(200, None, "c", "REJECTED", "ORDER_NOT_FOUND", None)
    cack = Accepted(202, None, "c", "CANCEL_ACKNOWLEDGED", None, None)
    assert probe.race_label(ack, cack, "CANCELED") == "CANCELED"
    assert probe.race_label(ack, nf, "OPEN") == "OPEN_AFTER_NOT_FOUND"
    assert probe.race_label(ack, cack, "OPEN") == "OPEN_AFTER_CANCEL_ACK"
    assert probe.race_label(ack, Unavailable(503, "x"), "OPEN") == "OPEN_AFTER_CANCEL_FAILED"
    assert probe.race_label(Rejected(400, "OracleDeviation", "Order", "", "c"), cack, None) == "PLACE_REJECTED"
    assert probe.race_label(Unavailable(0, "x"), cack, None) == "PLACE_UNKNOWN"
    assert probe.race_label(ack, cack, None) == "UNKNOWN"


def test_bisect_band_brackets_the_threshold():
    def venue(threshold: int):
        calls: list[int] = []

        async def try_at(d: int):
            calls.append(d)
            return ("accepted" if d <= threshold else "oracle_deviation"), D(d)

        return try_at, calls

    async def body():
        try_at, calls = venue(812)
        r = await probe.bisect_band(try_at, lower_bp=60)
        assert r["result"] == "bracketed" and r["steps"] <= probe.BAND_MAX_STEPS
        assert r["accepted_max_bp"] <= 812 < r["rejected_min_bp"]
        assert r["rejected_min_bp"] - r["accepted_max_bp"] <= 8
        assert all(60 <= d <= 2000 for d in calls)
        try_at, calls = venue(5000)
        assert (await probe.bisect_band(try_at, lower_bp=60))["result"] == "above_upper"
        try_at, calls = venue(10)
        assert (await probe.bisect_band(try_at, lower_bp=60))["result"] == "below_lower" and calls == [60]
        r = await probe.bisect_band(try_at, lower_bp=2100)
        assert r["result"].startswith("n/a") and r["steps"] == 0

        async def flaky(d: int):
            return ("accepted", D(d)) if d == 60 else ("inconclusive:POST_ONLY_WOULD_CROSS", D(d))

        r = await probe.bisect_band(flaky, lower_bp=60)
        assert r["result"] == "inconclusive"

    asyncio.run(body())


def test_placement_gate_caps_and_essential_bypass():
    async def body():
        mono = FakeMono()
        sleep = FakeSleep(mono)
        gate = probe.PlacementGate(2, monotonic=mono, sleep=sleep, spacing_s=0.5, hard_cap=3)
        assert await gate.take() and await gate.take()
        assert sleep.calls == [0.5]  # <= 2 placements / s
        assert not await gate.take()  # per-subcommand --max
        gate.start()
        assert await gate.take()
        assert not await gate.take()  # process-wide hard cap (3)
        assert await gate.take(essential=True)  # a reduce-only safety close is never capped
        assert gate.total == 4

    asyncio.run(body())


def test_redact_report_and_final_check():
    sig = "ab" * 64
    report = {
        "a": f"order for {ADDR_MIXED} and {ADDR_MIXED.upper()[2:]}",
        ADDR: {"key": SEED, "pub": PUB.upper(), "sig": sig},
        "geo": {"country": "DE", "region": "BE"},
        "n": D("1.5"),
        "list": [ADDR, "nb0_x-1", "a1b2c3d4e5f67890"],
    }
    raw = json.dumps(report, default=str)
    with pytest.raises(RuntimeError):
        probe.assert_report_clean(raw, address=ADDR, secrets=[SEED, PUB])
    clean = probe.redact_report(report, address=ADDR, secrets=[SEED, PUB])
    text = json.dumps(clean)
    probe.assert_report_clean(text, address=ADDR, secrets=[SEED, PUB])
    _assert_no_secrets(text)
    assert clean["geo"] == {"country": "XX", "region": "XX"}
    assert DEAD in clean["a"] and clean["n"] == "1.5"
    assert "a1b2c3d4e5f67890" in clean["list"] and "nb0_x-1" in clean["list"]  # order ids / clientIds kept


def test_ws_frame_helpers():
    frame = {"type": "channel_data", "channel": "orders", "contents": {"clientId": "nb0_x-1", "state": "OPEN", "status": "OPEN"}}
    assert [probe.order_event_state(e) for e in probe.order_events(frame, "nb0_x-1")] == ["OPEN"]
    assert probe.order_events(frame, "nb0_x-2") == []
    snap = {"type": "subscribed", "channel": "orders", "contents": {"orders": [{"clientId": "nb0_x-1", "status": "CANCELED"}]}}
    assert [probe.order_event_state(e) for e in probe.order_events(snap, "nb0_x-1")] == ["CANCELED"]
    fills = {"channel": "userFills", "contents": [{"tradeId": "t1", "orderId": "o1"}, {"tradeId": "t2", "orderId": "zz"}]}
    assert probe.fill_trade_ids(fills, {"o1"}, set()) == ["t1"]
    acct = {"channel": "account", "type": "channel_data", "contents": {"isSnapshot": True, "positions": {"1": {"marketId": 1, "size": "0.0001"}}}}
    assert probe.frame_position_size(acct, 1) == D("0.0001")
    assert probe.frame_position_size(acct, 3) == D(0)  # absent from a snapshot = flat
    delta = {"channel": "positions", "type": "channel_data", "contents": {"isSnapshot": False, "positions": [{"marketId": 3, "size": "-2"}]}}
    assert probe.frame_position_size(delta, 3) == D(-2)
    assert probe.frame_position_size(delta, 1) is None  # a delta about another market says nothing
    assert probe.is_snapshot_frame({"type": "subscribed"}) and not probe.is_snapshot_frame(delta)


def test_mark_confounded():
    results = {"ct-order": {"older_ct_after_newer": {"outcome": "Unauthorized"}}, "cancel-race": {"summary": {}}}
    probe.mark_confounded(results)
    assert results["cancel-race"]["confounded"] is True
    results = {"ct-order": {"older_ct_after_newer": {"outcome": "Accepted"}}, "cancel-race": {"summary": {}}}
    probe.mark_confounded(results)
    assert "confounded" not in results["cancel-race"]


# =====================================================================================
# Testnet-only guard and every preflight refusal (exit 2, nothing placed, key popped)
# =====================================================================================


@pytest.mark.parametrize(
    "env",
    [
        {"ARCUS_TESTNET_REST_URL": "https://api.arcus.xyz"},
        {"ARCUS_TESTNET_WS_URL": "wss://api.arcus.xyz/v1/ws"},
        {"ARCUS_TESTNET_REST_URL": "https://api.arcus.xyz/  # mainnet"},
        {"ARCUS_TESTNET_REST_URL": "https://example.com"},
        {"ARCUS_TESTNET_REST_URL": "http://127.0.0.1:8080"},
        {"ARCUS_MAINNET_REST_URL": "https://api.testnet.arcus.xyz"},
        {"ARCUS_TESTNET_REST_URL": "http://evil.example"},
    ],
)
def test_testnet_guard_refuses_non_testnet_urls(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(probe.Refused):
        probe.testnet_guard("testnet")


def test_testnet_guard_refuses_mainnet_and_accepts_documented_testnet():
    with pytest.raises(probe.Refused):
        probe.testnet_guard("mainnet")
    probe.testnet_guard("testnet")  # documented defaults pass


def _wallet_address() -> str:
    from eth_account import Account

    return Account.from_key("0x" + SEED).address


@pytest.mark.parametrize(
    "case",
    ["mainnet", "no_network", "bad_network", "mainnet_url", "short_key", "no_key", "not_hex", "wallet_key",
     "fly_app", "fly_machine", "bad_address", "taker_without_flag"],
)
def test_probe_refusals_before_any_venue_call(monkeypatch, tmp_path, capsys, case):
    venue = FakeVenue()
    kwargs: dict[str, Any] = {}
    argv = ["sign-check"]
    if case == "mainnet":
        kwargs["network"] = "mainnet"
    elif case == "no_network":
        kwargs["network"] = None
    elif case == "bad_network":
        kwargs["network"] = "arcus_testnet"
    elif case == "mainnet_url":
        monkeypatch.setenv("ARCUS_TESTNET_REST_URL", "https://api.arcus.xyz")
    elif case == "short_key":
        kwargs["key"] = SEED[:-2]
    elif case == "no_key":
        kwargs["key"] = None
    elif case == "not_hex":
        kwargs["key"] = "zz" * 32
    elif case == "wallet_key":
        kwargs["address"] = _wallet_address()
    elif case == "fly_app":
        monkeypatch.setenv("FLY_APP_NAME", "nadobro-bot")
    elif case == "fly_machine":
        monkeypatch.setenv("FLY_MACHINE_ID", "abc123")
    elif case == "bad_address":
        kwargs["address"] = "0x1234"
    elif case == "taker_without_flag":
        argv = ["fee-sign"]
    code, state = run_probe(monkeypatch, tmp_path, argv, venue, **kwargs)
    err = capsys.readouterr()
    assert code == 2
    assert state["calls"] == 0 and venue.requests == []  # refused before any venue I/O
    assert "ARCUS_PROBE_SIGNING_KEY" not in os.environ
    assert list(tmp_path.iterdir()) == []
    _assert_no_secrets(err.out + err.err)
    if case == "wallet_key":
        assert probe.WALLET_KEY_REFUSAL in err.err


@pytest.mark.parametrize(
    "venue_kwargs,needle",
    [
        ({"permissions": ["withdraw"]}, "trade-only"),
        ({"key_listed": False}, "not listed"),
        ({"key_status": "DELETED"}, "not ACTIVE"),
        ({"valid_until_ms": int(time.time() * 1000) + 3_600_000}, "24 h"),
        ({"key_account_index": 1}, "subaccount 0"),
        ({"account": "no_activity"}, "Testnet Deposit"),
        ({"account": "whitelist"}, "whitelisted"),
        ({"time_status": 503}, "clock sync"),
    ],
)
def test_probe_refusals_from_venue_state(monkeypatch, tmp_path, capsys, venue_kwargs, needle):
    venue = FakeVenue(**venue_kwargs)
    code, _state = run_probe(monkeypatch, tmp_path, ["sign-check"], venue)
    err = capsys.readouterr()
    assert code == 2 and needle in err.err
    assert venue.posts == []
    assert "ARCUS_PROBE_SIGNING_KEY" not in os.environ
    _assert_no_secrets(err.out + err.err)


def test_probe_refuses_a_market_outside_the_allowlist(monkeypatch, tmp_path, capsys):
    venue = FakeVenue()
    code, _ = run_probe(monkeypatch, tmp_path, ["sign-check", "--market", "AMD-USD"], venue)
    assert code == 2 and "allowlisted" in capsys.readouterr().err and venue.posts == []


def test_taker_preflight_refuses_and_trades_nothing(monkeypatch, tmp_path, capsys):
    venue = FakeVenue(bbo={"BTC-USD": (str(CAPTURED_BID), str(CAPTURED_ASK)), "ETH-USD": ("2684", "2687"), "SOL-USD": ("122.6", "122.8")})
    code, _ = run_probe(monkeypatch, tmp_path, ["fee-sign", "--i-understand-this-trades"], venue)
    assert code == 2 and "taker preflight" in capsys.readouterr().err
    assert venue.posts == []


def test_trading_needs_a_flat_baseline(monkeypatch, tmp_path, capsys):
    pos = load_fixture("positions_ok.json")["positions"]
    venue = FakeVenue(positions=json.loads(json.dumps(pos, default=str)))
    code, _ = run_probe(monkeypatch, tmp_path, ["tradeid-parity", "--i-understand-this-trades"], venue)
    assert code == 2 and "FLAT" in capsys.readouterr().err and venue.posts == []


# =====================================================================================
# End to end against the fake venue
# =====================================================================================


def test_sign_check_end_to_end_report_is_redacted(monkeypatch, tmp_path, capsys):
    venue = FakeVenue()
    code, state = run_probe(monkeypatch, tmp_path, ["sign-check"], venue)
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "ARCUS_PROBE_SIGNING_KEY" not in os.environ and state["signers"] == 1
    text, report = _report(tmp_path, "sign-check")
    _assert_no_secrets(text)
    _assert_no_secrets(out.out + out.err)
    res = report["results"]
    assert res["all_accepted"] is True and res["failed"] == [] and res["inconclusive"] == []
    assert set(res["verdicts"]) == {"place1", "place2", "place3", "place4", "cancel_by_id", "cancel_by_cid", "batch", "set_leverage"}
    assert set(res["terminal"].values()) == {"CANCELED"}
    assert report["network"] == "testnet" and report["market"] == "BTC-USD" and len(report["key_fingerprint"]) == 8
    assert report["cleanup"]["complete"] is True and report["exit_code"] == 0
    # clientIds carry the probe prefix nb0_<run36>- (user tag 0)
    places = [json.loads(r.content) for r in venue.posts if r.url.path == "/v1/placeOrder"]
    assert len(places) == 4 and all(p["clientId"].startswith("nb0_") for p in places)
    assert all(p["timeInForce"] == "ALO" and p["orderType"] == "LIMIT" and p["reduceOnly"] is False for p in places)
    assert all(D(p["price"]) < D("84600") for p in places)  # never crosses the ask
    paths = [r.url.path for r in venue.posts]
    assert paths.count("/v1/cancelOrder") == 2 and paths.count("/v1/batchCancelOrders") >= 1
    assert all("cancelAll" not in p and "modify" not in p.lower() for p in paths)
    lev = [json.loads(r.content) for r in venue.posts if r.url.path.endswith("Leverage")]
    assert lev == [{"accountIndex": 0, "address": ADDR, "leverage": 40, "marketId": 1}]  # the CURRENT value


def test_sign_check_inconclusive_on_oracle_deviation(monkeypatch, tmp_path):
    venue = FakeVenue(oracle_band_bp=300)  # far_bp 500 is outside the (unknown) band
    code, _ = run_probe(monkeypatch, tmp_path, ["sign-check"], venue)
    assert code == 0
    _, report = _report(tmp_path, "sign-check")
    res = report["results"]
    assert res["all_accepted"] is False and "place1" in res["inconclusive"] and res["failed"] == []


def test_cleanup_incomplete_exits_3_and_lists_orders(monkeypatch, tmp_path, capsys):
    venue = FakeVenue(cancel_status=503)
    code, _ = run_probe(monkeypatch, tmp_path, ["sign-check"], venue)
    err = capsys.readouterr().err
    assert code == 3 and "PROBE CLEANUP INCOMPLETE" in err
    text, report = _report(tmp_path, "sign-check")
    _assert_no_secrets(text)
    left = report["cleanup"]["remaining_probe_orders"]
    assert isinstance(left, list) and len(left) == 4 and all(o["client_id"].startswith("nb0_") for o in left)
    assert len(report["cleanup"]["rounds"]) == probe.CLEANUP_ROUNDS
    assert not any(r.url.path == "/v1/cancelAllOrders" for r in venue.requests)


def test_pool_watch_is_keyless(monkeypatch, tmp_path, capsys):
    venue = FakeVenue(rate_used=[5, 7, 3])
    code, state = run_probe(monkeypatch, tmp_path, ["pool-watch", "--interval", "10", "--hours", "0.0075"], venue)
    out = capsys.readouterr()
    assert code == 0, out.err
    assert state["signers"] == 0 and "ARCUS_PROBE_SIGNING_KEY" not in os.environ
    assert venue.posts == [] and not any(r.url.path == "/v1/apiKeys" for r in venue.requests)
    text, report = _report(tmp_path, "pool-watch")
    _assert_no_secrets(text)
    res = report["results"]
    assert res["samples"] == 3 and res["denied"] == 0 and report["key_fingerprint"] is None
    assert res["reseed_events"] == [{"t_utc": res["reseed_events"][0]["t_utc"], "pool": "order", "used_before": 7, "used_after": 3, "cap": 20000}]
    lines = (tmp_path / res["jsonl"]).read_text().splitlines()
    assert [json.loads(line)["order"]["used"] for line in lines] == [5, 7, 3]
    _assert_no_secrets("".join(lines))


def test_default_leverage_is_keyless(monkeypatch, tmp_path):
    venue = FakeVenue()
    code, state = run_probe(monkeypatch, tmp_path, ["default-leverage"], venue, key=None)
    assert code == 0 and state["signers"] == 0 and venue.posts == []
    _, report = _report(tmp_path, "default-leverage")
    res = report["results"]
    assert res["BTC-USD"] == {"leverage": 40, "margin_mode": "CROSS", "max_leverage": 40}
    assert res["SOL-USD"].startswith("absent")


def test_allow_fly_overrides_the_fly_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("FLY_APP_NAME", "nadobro-bot")
    code, _ = run_probe(monkeypatch, tmp_path, ["default-leverage", "--allow-fly"], FakeVenue(), key=None)
    assert code == 0


def test_ack_latency_with_websocket(monkeypatch, tmp_path):
    venue = FakeVenue()
    code, state = run_probe(monkeypatch, tmp_path, ["ack-latency", "--n", "3"], venue)
    assert code == 0
    assert state["ws_url"] == "wss://api.testnet.arcus.xyz/v1/ws"
    _, report = _report(tmp_path, "ack-latency")
    res = report["results"]
    assert res["ack_to_open_ms"]["count"] == 3 and res["cancel_to_canceled_ms"]["count"] == 3
    assert res["timeouts"] == {"open": 0, "canceled": 0} and res["incomplete"] is False
    assert res["recommend"]["ARCUS_ACK_GRACE_S"] == 2.0
    subs = [m for m in venue.ws.sent if m["type"] == "subscribe"]
    assert subs == [{"type": "subscribe", "channel": "orders", "id": ADDR, "accountIndex": 0, "snapshot": False}]


def test_ack_latency_without_websocket_places_nothing(monkeypatch, tmp_path):
    venue = FakeVenue()
    code, _ = run_probe(monkeypatch, tmp_path, ["ack-latency", "--n", "3"], venue, ws=False)
    assert code == 0 and venue.posts == []
    _, report = _report(tmp_path, "ack-latency")
    assert report["results"] == {"skipped": "websocket unavailable"}


def test_ct_order_replays_identical_bytes(monkeypatch, tmp_path):
    venue = FakeVenue()
    code, _ = run_probe(monkeypatch, tmp_path, ["ct-order"], venue)
    assert code == 0
    _, report = _report(tmp_path, "ct-order")
    res = report["results"]
    assert res["newer_ct_first"]["outcome"] == "Accepted" and res["older_ct_after_newer"]["outcome"] == "Accepted"
    places = [r for r in venue.posts if r.url.path == "/v1/placeOrder"]
    cts = [int(r.headers["X-Timestamp"]) for r in places]
    assert cts[0] > cts[1]  # the NEWER ct was sent first
    cancels = [r for r in venue.posts if r.url.path == "/v1/cancelOrder"]
    assert len(cancels) == 2
    assert cancels[0].content == cancels[1].content
    for h in ("X-API-Key", "X-Timestamp", "X-Signature", "Content-Type"):
        assert cancels[0].headers[h] == cancels[1].headers[h]
    assert int(cancels[0].headers["X-Timestamp"]) > max(cts)


def test_raw_signed_requests_match_the_client():
    """ct-order's raw POST must be byte-identical to ArcusClient's (same ct)."""

    async def body():
        routes = {("GET", "/v1/time"): resp(200, {"timeNs": CT0}),
                  ("POST", "/v1/placeOrder"): resp(202, load_fixture("place_202.json")),
                  ("POST", "/v1/cancelOrder"): resp(202, load_fixture("cancel_202.json"))}
        client, calls = mock_client(routes, time_ns=FakeTimeNs())
        auth = golden_auth()
        await client.clock.sync(client, lane=probe.Lane.L1_ENGINE)
        spec = OrderSpec(market_id=1, side=Side.BUY, order_type=WireOrderType.LIMIT, tif=Tif.ALO,
                         quantity=D("0.0001"), price=D("84517.3"), reduce_only=False, client_id="nb7ps_2s-1",
                         good_til_us=GTT, tick_size=D("0.1"), step_size=D("0.00000001"))
        assert isinstance(await client.place_order(auth, spec), Accepted)
        cancel = CancelSpec(1, client_id="nb7ps_2s-1")
        await client.cancel_order(auth, cancel)
        raw = probe.RawSigner(httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                              REST_BASE, auth, client, FakeMono())
        for sent, built in ((calls[1], raw.build_place(spec, CT0)), (calls[2], raw.build_cancel(cancel, CT0 + 1))):
            assert sent.content == built.content
            assert sent.url.path == built.path and dict(sent.url.params) == dict(built.params)
            for h in ("X-API-Key", "X-Timestamp", "X-Signature", "Content-Type", "Accept", "User-Agent"):
                assert sent.headers[h] == built.headers[h]
        await client.aclose()

    asyncio.run(body())


def test_all_runs_every_step_and_cleans_up(monkeypatch, tmp_path, capsys):
    venue = FakeVenue(hide_polls=2)
    code, _ = run_probe(monkeypatch, tmp_path, ["all"], venue)
    out = capsys.readouterr()
    assert code == 0, out.err
    text, report = _report(tmp_path, "all")
    _assert_no_secrets(text)
    res = report["results"]
    assert report["steps"] == [name for name, _ in probe.ALL_SEQUENCE]
    assert res["sign-check"]["all_accepted"] is True
    assert res["ack-404-window"]["n_404_before_visible"]["values"] == [2] * 5
    band = res["oracle-band"]["sides"]
    assert band["BUY"]["result"] == "bracketed" and band["BUY"]["accepted_max_bp"] <= 800 < band["BUY"]["rejected_min_bp"]
    assert res["min-size"]["below_min_size"]["error_type"] == "InvalidRequest"
    assert res["min-size"]["below_min_size"]["market"] == "BTC-USD"
    # "first allowlisted market where minOrderSize x price < minOrderNotional":
    # ETH-USD (0.001 x ~2550 = $2.6) comes before SOL-USD on current data.
    assert res["min-size"]["below_min_notional"]["market"] == "ETH-USD"
    assert res["min-size"]["below_min_notional"]["error_type"] == "InvalidRequest"
    assert res["min-size"]["reduce_only_dust"].startswith("not run")
    assert res["charged-400"]["market"] == "ETH-USD" and res["charged-400"]["order_used_delta"] == 3
    assert res["ack-latency"]["ack_to_open_ms"]["count"] == 20
    assert "confounded" not in res["cancel-race"]
    race = res["cancel-race"]["summary"]
    assert sum(sum(v.values()) for v in race.values()) == 15
    # The fake models documented buffering-free semantics: every race settles to a
    # definite verdict (CANCELED, or OPEN after ORDER_NOT_FOUND then swept) — never UNKNOWN.
    assert all(set(v) <= {"CANCELED", "OPEN_AFTER_NOT_FOUND"} for v in race.values()), race
    assert report["cleanup"]["complete"] is True and report["placements"] <= probe.HARD_CAP_PLACEMENTS
    assert not any(r.url.path in ("/v1/cancelAllOrders", "/v1/modifyOrder") for r in venue.requests)
    assert all(not json.loads(r.content).get("reduceOnly") for r in venue.posts if r.url.path == "/v1/placeOrder")
    assert not any(o["state"] == "OPEN" for o in venue.orders.values())


TAKER_SUBS = ["tradeid-parity", "fee-sign", "entry-units", "ioc-reduce-only", "alo-reduce-only", "alo-cross", "min-size", "ws-fresh"]


@pytest.mark.parametrize("sub", TAKER_SUBS)
def test_trading_subcommands_open_and_close_their_own_position(monkeypatch, tmp_path, sub):
    venue = FakeVenue()
    code, _ = run_probe(monkeypatch, tmp_path, [sub, "--i-understand-this-trades"], venue)
    assert code == 0
    text, report = _report(tmp_path, sub)
    _assert_no_secrets(text)
    res = report["results"]
    assert all(size == 0 for size in venue.pos.values())  # flat again: only its own position, closed
    assert report["cleanup"]["complete"] is True and set(report["cleanup"]["position_delta"].values()) <= {"0"}
    assert not any(o["state"] == "OPEN" for o in venue.orders.values())
    places = [json.loads(r.content) for r in venue.posts if r.url.path == "/v1/placeOrder"]
    for p in places:
        assert p["clientId"].startswith("nb0_")
        if p["orderType"] == "MARKET":
            assert p["reduceOnly"] is True and p["timeInForce"] == "IOC"
            mark = venue.market[p["marketId"]].mark_price
            assert abs(D(p["price"]) - mark) <= mark * D("0.10")  # protective price inside the band
        crossing = (p["orderSide"] == "BUY" and D(p["price"]) >= D("84600")) or (p["orderSide"] == "SELL" and D(p["price"]) <= D("84500"))
        if p["timeInForce"] == "IOC" and not p["reduceOnly"] and p["marketId"] == 1 and crossing:
            assert D(p["quantity"]) == BTC.min_order_size  # a taker open is always the tiny minimum
        if p["timeInForce"] == "ALO":
            assert not crossing or sub == "alo-cross"  # only alo-cross places an ALO at the touch
    if sub == "tradeid-parity":
        assert res["equal"] is True and len(res["ws_trade_ids"]) == 2 and res["rest_matched_by"] == "order_id"
    elif sub == "fee-sign":
        assert res["fee_sign"] == {"TAKER": "+", "MAKER": "none observed"}
    elif sub == "entry-units":
        assert D(res["ratio"]) == 1 and res["scale_suspect"] is False
    elif sub == "ioc-reduce-only":
        assert res["market_reduce_only"]["outcome"] == "Accepted" and D(res["market_reduce_only"]["position_after"]) == 0
        assert D(res["oversize_reduce_only"]["position_after"]) == 0  # the fake clips; the probe records it
        assert any(f.get("rejectionReason") == "IOC_CANCELED" for f in res["zero_fill_ioc"]["ws"])
        reduce = [p for p in places if p["reduceOnly"] and p["orderType"] == "MARKET"]
        assert D(reduce[1]["quantity"]) == 2 * BTC.min_order_size  # the oversize leg
    elif sub == "alo-reduce-only":
        assert res["close_side"] == "SELL" and res["reduce_only_alo"]["accepted_and_open"] is True
        alo = [p for p in places if p["reduceOnly"] and p["timeInForce"] == "ALO"]
        assert len(alo) == 1 and D(alo[0]["price"]) > D("84500")  # rests on the closing side, never crosses
    elif sub == "alo-cross":
        assert any(f.get("rejectionReason") == "POST_ONLY_WOULD_CROSS" for f in res["ws"])
        assert res["order_units_charged"] == 1 and D(res["price"]) == D("84600")
    elif sub == "min-size":
        dust = res["reduce_only_dust"]
        assert dust["reduce_only_dust"]["size"] and D(dust["reduce_only_dust"]["size"]) < BTC.min_order_size
        assert dust["close"]["closed"] is True
    elif sub == "ws-fresh":
        assert res["account_resnapshot_s"]["samples"] >= 3
        assert res["positions_lead_ms"]["open"]["positions_seen"] and res["positions_lead_ms"]["open"]["account_seen"]
        assert res["positions_lead_ms"]["close"]["positions_seen"] and res["positions_lead_ms"]["close"]["account_seen"]


def test_close_falls_back_to_limit_ioc_when_market_is_rejected(monkeypatch, tmp_path):
    venue = FakeVenue(reject_market=True)
    code, _ = run_probe(monkeypatch, tmp_path, ["fee-sign", "--i-understand-this-trades"], venue)
    assert code == 0 and all(size == 0 for size in venue.pos.values())
    _, report = _report(tmp_path, "fee-sign")
    steps = report["results"]["close"]["steps"]
    assert [(s["order_type"], s["outcome"]) for s in steps] == [("MARKET", "Rejected"), ("LIMIT", "Accepted")]


def test_a_position_that_cannot_be_closed_exits_3(monkeypatch, tmp_path, capsys):
    venue = FakeVenue(reject_reduce_only=True)
    code, _ = run_probe(monkeypatch, tmp_path, ["fee-sign", "--i-understand-this-trades"], venue)
    err = capsys.readouterr().err
    assert code == 3 and "PROBE CLEANUP INCOMPLETE" in err
    _, report = _report(tmp_path, "fee-sign")
    assert report["cleanup"]["position_delta"] == {"BTC-USD": "0.0001"}
    assert report["cleanup"]["flatten"]["BTC-USD"]["closed"] is False
    assert venue.pos[1] == D("0.0001")  # nothing but reduce-only orders were tried: never doubled


async def _raise_cancelled() -> None:
    raise asyncio.CancelledError()  # what asyncio.run delivers on Ctrl-C


async def _send_sigterm() -> None:
    import signal as _signal

    os.kill(os.getpid(), _signal.SIGTERM)
    await asyncio.sleep(0.05)  # let the loop deliver it


def interrupting_factory(venue: FakeVenue, *, at: int, action: Any) -> Any:
    """A services factory whose ``sleep`` runs ``action`` on its ``at``-th call
    (the probe itself drives it through ``asyncio.run``)."""
    factory, _ = make_factory(venue)

    def build(net: str) -> Any:
        svc = factory(net)
        real_sleep = svc.sleep
        state = {"n": 0}

        async def sleep(seconds: float) -> None:
            state["n"] += 1
            if state["n"] == at:
                await action()
            await real_sleep(seconds)

        svc.sleep = sleep
        return svc

    return build


def _run_interrupted(monkeypatch, tmp_path, action: Any) -> tuple[int, FakeVenue]:
    venue = FakeVenue(hide_polls=2)  # each GET /v1/order: 404, 404, then 200 -> the loop sleeps
    monkeypatch.setenv("ARCUS_PROBE_NETWORK", "testnet")
    monkeypatch.setenv("ARCUS_PROBE_ADDRESS", ADDR_MIXED)
    monkeypatch.setenv("ARCUS_PROBE_SIGNING_KEY", SEED)
    code = probe.main(
        ["ack-404-window", "--n", "3", "--out", str(tmp_path)],
        services_factory=interrupting_factory(venue, at=3, action=action),  # inside the 2nd order's polling
    )
    return code, venue


def test_ctrl_c_mid_run_still_cleans_up_and_reports(monkeypatch, tmp_path, capsys):
    """asyncio.run turns Ctrl-C into a CancelledError at the current await: the
    cleanup still cancels every probe order and the report records the interrupt."""
    code, venue = _run_interrupted(monkeypatch, tmp_path, _raise_cancelled)
    capsys.readouterr()
    assert code == 1  # interrupted (cleanup complete, so not 3)
    _, report = _report(tmp_path, "ack-404-window")
    assert report["interrupted"] is True and report["cleanup"]["complete"] is True
    assert report["results"] is None  # the interrupted subcommand never returned
    assert len(venue.orders) == 2 and not any(o["state"] == "OPEN" for o in venue.orders.values())


def test_sigterm_is_handled_like_ctrl_c_and_restored(monkeypatch, tmp_path, capsys):
    """`kill <pid>` on a background run cancels it like one Ctrl-C (cleanup +
    report), and the previous SIGTERM handler is restored afterwards."""
    import signal as _signal

    sentinel_calls: list[int] = []

    def sentinel(signum, frame):  # pragma: no cover - never delivered here
        sentinel_calls.append(signum)

    previous = _signal.signal(_signal.SIGTERM, sentinel)
    try:
        code, venue = _run_interrupted(monkeypatch, tmp_path, _send_sigterm)
        capsys.readouterr()
        assert code == 1
        _, report = _report(tmp_path, "ack-404-window")
        assert report["interrupted"] is True and report["cleanup"]["complete"] is True
        assert not any(o["state"] == "OPEN" for o in venue.orders.values())
        assert _signal.getsignal(_signal.SIGTERM) is sentinel and sentinel_calls == []
    finally:
        _signal.signal(_signal.SIGTERM, previous)


def test_trading_is_refused_inside_all(monkeypatch, tmp_path):
    """`all` never trades, even with the flag (02 §11.2: min-size runs its non-taker part)."""
    venue = FakeVenue()
    code, _ = run_probe(monkeypatch, tmp_path, ["all", "--i-understand-this-trades"], venue)
    assert code == 0 and venue.fills == [] and venue.pos == {}


# =====================================================================================
# capture_arcus_shapes.py
# =====================================================================================


def _capture_routes() -> dict[tuple[str, str], Any]:
    markets = load_fixture("markets_testnet_subset.json")
    markets["markets"][0]["surpriseField"] = "x"
    compliance_geo = {"geo": {"country": "DE", "region": "BE", "restrictions": {"perpetuals": False, "spot": False}, "bypassed": False}}

    def compliance(request: httpx.Request) -> httpx.Response:
        body = json.loads(json.dumps(compliance_geo))
        if "address" in request.url.params:
            body["address"] = {"address": ADDR_MIXED, "status": "COMPLIANT"}
        return httpx.Response(200, json=body)

    def fx(name: str) -> httpx.Response:
        return httpx.Response(200, content=(FIXTURES / name).read_bytes())

    def upper_addr(name: str) -> httpx.Response:  # an echo in checksum-ish case must be redacted too
        return httpx.Response(200, content=(FIXTURES / name).read_bytes().replace(ADDR.encode(), ADDR_MIXED.encode()))

    return {
        ("GET", "/"): resp(200, {"service": "api-gateway", "status": "running", "version": "testnet-v1"}),
        ("GET", "/health"): resp(200, {"status": "ok"}),
        ("GET", "/v1/time"): fx("time.json"),
        ("GET", "/v1/markets"): httpx.Response(200, content=json.dumps(markets, default=str).encode()),
        ("GET", "/v1/mids"): fx("mids.json"),
        ("GET", "/v1/prices"): fx("prices.json"),
        ("GET", "/v1/bbo/BTC-USD"): fx("bbo_btc.json"),
        ("GET", "/v1/l2OrderBook/BTC-USD"): fx("l2_btc.json"),
        ("GET", "/v1/candles"): fx("candles_newest_first.json"),
        ("GET", "/v1/compliance"): compliance,
        ("GET", "/v1/feetiers"): resp(200, {"tiers": []}),
        ("GET", "/v1/account"): upper_addr("account_ok.json"),
        ("GET", "/v1/positions"): fx("positions_ok.json"),
        ("GET", "/v1/openOrders"): fx("open_orders_page.json"),
        ("GET", "/v1/fills"): fx("fills_page.json"),
        ("GET", "/v1/funding"): fx("funding_page.json"),
        ("GET", "/v1/rateLimit"): fx("rate_limit_testnet.json"),
        ("GET", "/v1/leverages"): fx("leverages_ok.json"),
        ("GET", "/v1/apiKeys"): fx("api_keys_ok.json"),
    }


def test_capture_redacts_address_and_country(tmp_path, capsys):
    client, calls = mock_client(_capture_routes())
    out_dir = tmp_path / "cap"
    code = cap.main(["--address", ADDR_MIXED, "--out", str(out_dir)], services_factory=lambda net: client)
    printed = capsys.readouterr().out
    assert code == 0
    assert all(c.method == "GET" for c in calls) and len(calls) == 20
    assert not any("X-API-Key" in c.headers or "X-Signature" in c.headers for c in calls)
    for f in out_dir.iterdir():
        low = f.read_text().lower()
        assert ADDR_BODY not in low, f.name
    assert ADDR_BODY not in printed.lower()
    for name in ("compliance.json", "compliance_address.json"):
        geo = json.loads((out_dir / name).read_text())["geo"]
        assert geo["country"] == "XX" and geo["region"] == "XX"
    assert json.loads((out_dir / "compliance_address.json").read_text())["address"]["address"] == DEAD
    assert json.loads((out_dir / "account.json").read_text())["address"] == DEAD
    report = json.loads((out_dir / "report.json").read_text())
    assert report["network"] == "testnet" and report["candles_order"] == "newest_first" and report["with_address"] is True
    eps = report["endpoints"]
    assert eps["markets"]["parse_ok"] is True and "surpriseField" in eps["markets"]["unknown_fields"]
    assert "lastTradePrice" in eps["markets"]["unknown_fields"]  # live but undocumented
    for name in ("time", "mids", "prices", "bbo_BTC-USD", "l2_BTC-USD", "candles_BTC-USD_1m", "compliance",
                 "compliance_address", "account", "positions", "openOrders", "fills", "funding", "rateLimit",
                 "leverages", "apiKeys"):
        assert eps[name]["parse_ok"] is True, name
        assert eps[name]["missing_required"] == [], name
    assert eps["root"]["parse_ok"] is None and eps["feetiers"]["http"] == 200


def test_capture_records_denials_and_refuses_on_fly(tmp_path, monkeypatch, capsys):
    routes = _capture_routes()
    routes[("GET", "/v1/time")] = resp(503, {"error": "down", "errorType": "Unavailable"})
    client, _ = mock_client(routes)
    out_dir = tmp_path / "cap"
    assert cap.main(["--out", str(out_dir)], services_factory=lambda net: client) == 0
    report = json.loads((out_dir / "report.json").read_text())
    time_ep = report["endpoints"]["time"]
    assert time_ep["http"] == 503 and time_ep["outcome"].startswith("Unavailable") and time_ep["parse_ok"] is None
    assert json.loads((out_dir / "time.json").read_text()) == {"outcome": time_ep["outcome"], "http": 503}
    assert "account" not in report["endpoints"]  # no --address, no account reads
    monkeypatch.setenv("FLY_MACHINE_ID", "x")
    called = []
    assert cap.main(["--out", str(tmp_path / "x")], services_factory=lambda net: called.append(net)) == 2
    assert called == [] and not (tmp_path / "x").exists()


def test_capture_helpers():
    assert cap.candles_order({"candles": [{"openTime": 3}, {"openTime": 2}]}) == "newest_first"
    assert cap.candles_order({"candles": [{"openTime": 2}, {"openTime": 3}]}) == "oldest_first"
    assert cap.candles_order({"candles": [{"openTime": 2}]}) == "n/a"
    assert cap.candles_order({"candles": [{"openTime": 2}, {"openTime": 3}, {"openTime": 1}]}) == "n/a"
    unknown, missing = cap.drift([{"orderId": "a", "novel": 1}], "order")
    assert unknown == ["novel"] and "marketId" in missing
    assert cap.drift([{"a": 1}], None) == ([], [])
    assert cap._jsonable({"a": D("0.1"), "b": [D("1E+2")]}) == {"a": 0.1, "b": [100.0]}


# =====================================================================================
# The committed live captures (keyless public GETs, 2026-09-30)
# =====================================================================================

_DEAD_REF = ArcusAccountRef("testnet", DEAD, 0)
_PARSERS = {
    "time.json": parse_time,
    "mids.json": parse_mids,
    "prices.json": parse_prices,
    "bbo_BTC-USD.json": parse_bbo,
    "l2_BTC-USD.json": parse_l2,
    "candles_BTC-USD_1m.json": lambda b: parse_candles(b, final_only=False),
    "compliance.json": parse_compliance,
    "compliance_address.json": parse_compliance,
    "positions.json": lambda b: parse_positions_payload(b, ref=_DEAD_REF),
    "openOrders.json": parse_open_orders_payload,
    "fills.json": lambda b: parse_fills_payload(b, ref=_DEAD_REF),
    "funding.json": parse_funding_payload,
    "rateLimit.json": lambda b: parse_rate_limit(b, ref=_DEAD_REF, now_mono=0.0),
    "leverages.json": lambda b: parse_leverages(b, ref=_DEAD_REF),
    "apiKeys.json": parse_api_keys,
}


def _capture_runs() -> list[Path]:
    return sorted(p for p in (FIXTURES / "captured").iterdir() if p.is_dir())


def test_committed_captures_parse_and_are_redacted():
    runs = _capture_runs()
    assert {p.name.split("_")[0] for p in runs} == {"testnet", "mainnet"}
    for run in runs:
        report = json.loads((run / "report.json").read_text())
        assert report["candles_order"] == "newest_first"  # live order (docs say oldest-first)
        assert report["schema_error_counts"] == {}
        for f in sorted(run.glob("*.json")):
            text = f.read_text()
            assert not re.search(r"0x(?!0{36}dead)[0-9a-fA-F]{40}", text), f  # only the dead address
            body = load_fixture(f"captured/{run.name}/{f.name}")
            if f.name.startswith("compliance"):
                assert body["geo"]["country"] == "XX" and body["geo"]["region"] == "XX"
            parser = _PARSERS.get(f.name)
            if parser is not None:
                parser(body)
        if (run / "account.json").exists():  # the dead address has no testnet activity
            assert json.loads((run / "account.json").read_text()) == {"outcome": "NoActivity", "http": 404}
        if (run / "leverages.json").exists():  # plan G1 "default leverage and margin mode"
            lev = {e.market_id: e for e in parse_leverages(load_fixture(f"captured/{run.name}/leverages.json"), ref=_DEAD_REF)}
            assert [(lev[i].leverage, lev[i].margin_mode) for i in (1, 2, 3)] == [(40, "CROSS"), (25, "CROSS"), (20, "CROSS")]
