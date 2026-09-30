"""venue/arcus/hub.py — per-network singletons, P2 part (02 §8.2 / §12.10).

The hub is loop-bound (A-10), flag-free except ``enabled_networks`` (the ONLY
``ARCUS_ENABLED`` read in the package; the master switch gates mainnet too,
02 D20), and ``start`` never raises. No real network: the client's transport
factory is replaced by an ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import logging
import threading

import httpx
import pytest

from arcus_helpers import ADDR, REF, fixture_resp
from src.nadobro.venue.arcus import client as client_mod
from src.nadobro.venue.arcus import hub
from src.nadobro.venue.arcus.budget import PoolGovernor
from src.nadobro.venue.arcus.types import ArcusAccountRef, Lane

_FLAGS = ("ARCUS_ENABLED", "ARCUS_MAINNET_ENABLED", "ARCUS_IP_L0_RESERVE", "ARCUS_ALLOWED_USER_IDS")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    async def refuse(self, request):  # pragma: no cover - only runs on a bug
        raise AssertionError("real network in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    for name in _FLAGS:
        monkeypatch.delenv(name, raising=False)
    hub._reset_for_tests()
    yield
    hub._reset_for_tests()


def _mock_transport(monkeypatch, routes: dict[tuple[str, str], httpx.Response]) -> list[httpx.Request]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        template = routes.get((request.method, request.url.path))
        if template is None:
            return httpx.Response(503, json={"error": "unavailable", "errorType": "Unavailable"})
        return httpx.Response(template.status_code, headers=template.headers, content=template.content)

    monkeypatch.setattr(client_mod, "build_transport", lambda *, force_ipv4: httpx.MockTransport(handler))
    return calls


def test_services_singleton_per_network(monkeypatch):
    monkeypatch.setenv("ARCUS_IP_L0_RESERVE", "200")

    async def body():
        a = hub.services("testnet")
        assert hub.services("testnet") is a
        m = hub.services("mainnet")
        assert m is not a and m.client is not a.client and m.ip_budget is not a.ip_budget
        assert (a.network, m.network) == ("testnet", "mainnet")
        assert a.client.network == "testnet" and a.clock.network == "testnet" and a.catalog.network == "testnet"
        assert a.client.clock is a.clock and a.client.ip_budget is a.ip_budget
        assert a.ip_budget.snapshot()["floors"] == {
            "L0_BRAKE": 0, "L1_ENGINE": 200, "L2_INTERACTIVE": 300, "L3_BACKGROUND": 600,
        }
        # P4a slots stay empty in P2
        assert (a.ws, a.router, a.ledger_writer, a.link_writer, a.sync) == (None,) * 5
        await hub.shutdown()

    asyncio.run(body())


def test_services_default_floors():
    async def body():
        floors = hub.services("testnet").ip_budget.snapshot()["floors"]
        assert floors == {"L0_BRAKE": 0, "L1_ENGINE": 300, "L2_INTERACTIVE": 400, "L3_BACKGROUND": 700}
        await hub.shutdown()

    asyncio.run(body())


def test_services_requires_running_loop():
    with pytest.raises(RuntimeError):
        hub.services("testnet")


def test_services_rebuilt_after_closed_loop():
    async def first():
        return hub.services("testnet").client

    async def second():
        return hub.services("testnet").client

    one = asyncio.run(first())
    two = asyncio.run(second())
    assert one is not two


def test_services_refuses_a_second_live_loop():
    errors: list[BaseException] = []

    async def elsewhere():
        try:
            hub.services("testnet")
        except RuntimeError as exc:
            errors.append(exc)

    async def body():
        hub.services("testnet")  # bound to THIS (still running) loop
        worker = threading.Thread(target=lambda: asyncio.run(elsewhere()))
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        await hub.shutdown()

    asyncio.run(body())
    assert len(errors) == 1 and "another running event loop" in str(errors[0])


def test_services_rejects_bad_network():
    async def body():
        for bad in ("arcus_testnet", "Testnet", "", None):
            with pytest.raises(ValueError):
                hub.services(bad)  # type: ignore[arg-type]

    asyncio.run(body())


def test_services_never_check_flags(monkeypatch):
    """Brakes must work with every flag off: services() builds regardless."""
    monkeypatch.setenv("ARCUS_ENABLED", "0")

    async def body():
        assert hub.services("mainnet").network == "mainnet"
        await hub.shutdown()

    asyncio.run(body())


def test_pool_governor_keyed_by_ref():
    g = hub.pool_governor(REF)
    assert isinstance(g, PoolGovernor)
    assert hub.pool_governor(ArcusAccountRef("testnet", ADDR.upper().replace("0X", "0x"), 0)) is g
    assert hub.pool_governor(ArcusAccountRef("testnet", ADDR, 1)) is not g
    assert hub.pool_governor(ArcusAccountRef("mainnet", ADDR, 0)) is not g
    with pytest.raises(ValueError):
        hub.pool_governor("0xabc")  # type: ignore[arg-type]


def test_enabled_networks(monkeypatch):
    assert hub.enabled_networks() == ()
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    assert hub.enabled_networks() == ("testnet",)
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    assert hub.enabled_networks() == ("testnet", "mainnet")
    monkeypatch.setenv("ARCUS_ENABLED", "0")
    assert hub.enabled_networks() == ()  # the mainnet flag alone never opens mainnet (D20)
    monkeypatch.delenv("ARCUS_MAINNET_ENABLED")
    assert hub.enabled_networks(state_networks=["mainnet"]) == ("mainnet",)
    assert hub.enabled_networks(state_networks=["testnet", "mainnet"]) == ("testnet", "mainnet")
    assert hub.enabled_networks(state_networks=("mainnet", "testnet")) == ("testnet", "mainnet")  # testnet first
    for bad in (["arcus_mainnet"], ["MAINNET"], "mainnet"):
        with pytest.raises(ValueError):
            hub.enabled_networks(state_networks=bad)  # type: ignore[arg-type]


def test_shutdown_closes_owned_clients(monkeypatch):
    _mock_transport(monkeypatch, {})

    async def body():
        closed: list[str] = []
        for net in ("testnet", "mainnet"):
            svc = hub.services(net)
            real = svc.client.aclose

            async def spy(real=real, net=net):
                closed.append(net)
                await real()

            svc.client.aclose = spy  # type: ignore[method-assign]
        await hub.shutdown()
        assert sorted(closed) == ["mainnet", "testnet"]
        assert hub._SERVICES == {} and hub._LOOPS == {} and hub._GOVERNORS == {}
        # a fresh build after shutdown
        assert hub.services("testnet") is not None
        await hub.shutdown()

    asyncio.run(body())


def test_start_never_raises(monkeypatch, caplog):
    calls = _mock_transport(monkeypatch, {})  # every request -> 503

    async def body():
        with caplog.at_level(logging.WARNING, logger=hub.__name__):
            await hub.start(["testnet", "bogus"])
        paths = [c.url.path for c in calls]
        assert "/v1/time" in paths and "/v1/markets" in paths
        messages = [r.getMessage() for r in caplog.records]
        assert any("clock sync failed" in m for m in messages)
        assert any("catalog not loaded (Unavailable)" in m for m in messages)
        assert any("start failed for one network" in m for m in messages)  # the bad token
        svc = hub.services("testnet")
        assert svc.clock.skew_ms() is None and svc.catalog.age_s() is None
        await hub.shutdown()

    asyncio.run(body())


def test_start_warms_clock_and_catalog(monkeypatch):
    calls = _mock_transport(
        monkeypatch,
        {
            ("GET", "/v1/time"): fixture_resp(200, "time.json"),
            ("GET", "/v1/markets"): fixture_resp(200, "markets_testnet_subset.json"),
        },
    )

    async def body():
        await hub.start(["testnet"])
        svc = hub.services("testnet")
        assert svc.clock.skew_ms() is not None
        assert svc.catalog.is_fresh()
        assert [m.ticker for m in svc.catalog.allowlisted()] == ["BTC-USD", "ETH-USD", "SOL-USD"]
        assert all(c.url.host == "api.testnet.arcus.xyz" for c in calls)
        assert svc.ip_budget.snapshot()["taken"]["L1_ENGINE"] == 2  # time + markets on L1
        await hub.shutdown()

    asyncio.run(body())


def test_catalog_uses_the_allowlist_flag(monkeypatch):
    monkeypatch.setenv("ARCUS_MARKET_ALLOWLIST", "BTC-USD")
    _mock_transport(
        monkeypatch,
        {("GET", "/v1/markets"): fixture_resp(200, "markets_testnet_subset.json")},
    )

    async def body():
        svc = hub.services("testnet")
        assert await svc.catalog.refresh(svc.client, lane=Lane.L1_ENGINE)
        assert [m.ticker for m in svc.catalog.allowlisted()] == ["BTC-USD"]
        await hub.shutdown()

    asyncio.run(body())
