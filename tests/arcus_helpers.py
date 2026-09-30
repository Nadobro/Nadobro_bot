"""Shared constants/helpers for the Arcus venue-library tests (02 §12).

Golden-vector constants are the ones of 02 §5.1: the seed is
``bytes(range(32)).hex()`` and every payload/signature was re-computed
independently (cryptography 50.0.0). ``tests/`` is on ``sys.path`` via
``tests/conftest.py``, so test files ``import arcus_helpers``.

``mock_client(routes)`` builds an ``ArcusClient`` over ``httpx.MockTransport``
(no network) with fake clocks; it returns the client and the list of captured
``httpx.Request`` objects.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from typing import Any, Callable, Union

import httpx

from src.nadobro.venue.arcus.budget import IpBudget
from src.nadobro.venue.arcus.client import ArcusClient
from src.nadobro.venue.arcus.clock import ArcusClock
from src.nadobro.venue.arcus.signing import Ed25519Signer, make_auth
from src.nadobro.venue.arcus.types import ArcusAccountRef

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "arcus"

SEED = bytes(range(32)).hex()
PUB = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
ADDR_MIXED = "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12"
ADDR = ADDR_MIXED.lower()
REF = ArcusAccountRef("testnet", ADDR_MIXED, 0)
CT0 = 1790000000123456789  # ns
GTT = 1793456000000000  # µs

# 02 §5.1 golden vectors: (payload bytes, signature hex).
V1_PAYLOAD = (
    b'{"ad":"0xabcdef1234567890abcdef1234567890abcdef12","ai":0,"c":"nb7ps_2s-1",'
    b'"ct":1790000000123456789,"g":1793456000000000000,"m":1,"op":1,"p":845173,"q":10000,'
    b'"r":0,"s":0,"t":3,"v":1}'
)
V1_SIG = (
    "1ea39cd54a290a4733f0570de3f70b400f8dd67281211e2aadbe91d3378b895d"
    "43be2a177a3d9482fbbe57ca8eb1020934ee3f5ecc11f633dc3e4ba0d2ae5000"
)
V2_PAYLOAD = (
    b'{"ad":"0xabcdef1234567890abcdef1234567890abcdef12","ai":0,"ct":1790000000123456790,'
    b'"g":1793456000000000000,"m":2,"op":1,"p":250012,"q":150000,"r":1,"s":1,"t":2,"v":1}'
)
V2_SIG = (
    "4a7afc2b82785b0e501deee477260e9b2ee10c29a32640d14142dd5598358818"
    "62c4bd9dfacd4854dcd5c9f80d4edd7ca566f97b0ca059c3ba98fa42d5379405"
)
V3_PAYLOAD = (
    b'{"ad":"0xabcdef1234567890abcdef1234567890abcdef12","ai":0,"ct":1790000000123456791,'
    b'"id":"a1b2c3d4e5f67890","m":1,"op":2,"v":1}'
)
V3_SIG = (
    "8eb4194377b1531f694415e603449a4069179d9eb792b3fe9030a462ba30ff9b"
    "bac4c234803456112e5e56012113dc401190975465f2df123969b46e94150007"
)
V4_PAYLOAD = (
    b'{"ad":"0xabcdef1234567890abcdef1234567890abcdef12","ai":0,"c":"nb7ps_2s-1",'
    b'"ct":1790000000123456792,"m":1,"op":2,"v":1}'
)
V4_SIG = (
    "4ed8640c82a0a9d792cfa71d8696ee33d28398fed638c080e7abbe24add27abb"
    "17f59fdc50fa5f0703cf139d412e370739804ef409d197b6439b1ff6ee4a470d"
)
V5_PAYLOAD = (
    b'1790000000123456793setLeverage{"accountIndex":0,'
    b'"address":"0xabcdef1234567890abcdef1234567890abcdef12","leverage":5,"marketId":1}'
)
V5_SIG = (
    "125981db6065c655ae11d558dcbeb114a36fe33f22496be1cee5f11548f31ee4"
    "6a02112c5028d8233a4e8a89ec027570288c1736a751f89f2535fe810838fa0b"
)
V6_PAYLOAD = (
    b'{"ad":"0xabcdef1234567890abcdef1234567890abcdef12","ai":0,"c":"nb7ps_2s-2",'
    b'"ct":1790000000123456794,"m":1,"op":2,"v":1}'
)
V6_SIG = (
    "ae10a139d61064ba7158aa8835361631802f851833a107a2053b16718aa07d2e"
    "57bfac94a9a9ca2c45454c21ce89a2a608e2f9082c25b783c957e6684f40a50f"
)
V7_PAYLOAD = (
    b'{"ad":"0xabcdef1234567890abcdef1234567890abcdef12","ai":0,"ct":1790000000123456794,'
    b'"id":"00000000000000ff","m":3,"op":2,"v":1}'
)
V7_SIG = (
    "586b9867c65a9bacb61453d7a0a9450232dade8882a16310b5e7150cd7cc6f32"
    "d0cc03e3ff0c811ec5594337d9845e5410b359258fd6fdd71ccdf264ff91a10e"
)


def load_fixture(name: str) -> Any:
    """A fixture decoded the way the client decodes bodies (floats -> Decimal)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"), parse_float=Decimal)


class FakeMono:
    """Injectable ``time.monotonic`` replacement."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTimeNs:
    """Injectable ``time.time_ns`` replacement; ``script`` values are served
    first (one per call), then ``now`` repeats."""

    def __init__(self, now: int = CT0, script: list[int] | None = None) -> None:
        self.now = now
        self.script = list(script or [])
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return self.now


class FakeSleep:
    """Injectable ``asyncio.sleep``: records each wait and advances a FakeMono."""

    def __init__(self, mono: FakeMono) -> None:
        self.mono = mono
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.mono.advance(seconds)


REST_BASE = "https://api.testnet.arcus.xyz"

Route = Union[httpx.Response, Callable[[httpx.Request], httpx.Response], list]


def resp(status: int, body: Any = None, *, headers: dict[str, str] | None = None, content: bytes | None = None) -> httpx.Response:
    """A response template (copied per request by ``mock_client``)."""
    if content is not None:
        return httpx.Response(status, content=content, headers=headers)
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


def fixture_resp(status: int, name: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """A response whose body is the raw bytes of a fixture file."""
    raw = (FIXTURES / name).read_bytes()
    merged = {"Content-Type": "application/json", **(headers or {})}
    return httpx.Response(status, content=raw, headers=merged)


def _fresh(template: httpx.Response) -> httpx.Response:
    return httpx.Response(template.status_code, headers=template.headers, content=template.content)


def mock_client(
    routes: dict[tuple[str, str], Route],
    *,
    mono: FakeMono | None = None,
    time_ns: FakeTimeNs | None = None,
    clock: ArcusClock | None = None,
    budget: IpBudget | None = None,
    clock_max_age_s: Callable[[], float] | None = None,
    network: str = "testnet",
) -> tuple[ArcusClient, list[httpx.Request]]:
    """An ``ArcusClient`` whose HTTP goes to ``routes`` (keyed by (METHOD, path)).

    A route is a response template (copied per call), a callable
    ``request -> Response`` (it may raise ``httpx`` errors), or a list of those
    served in order (the last one repeats). An unknown route fails the test.
    """
    calls: list[httpx.Request] = []
    queues: dict[tuple[str, str], Any] = {
        key: (list(value) if isinstance(value, list) else value) for key, value in routes.items()
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        key = (request.method, request.url.path)
        if key not in queues:
            raise AssertionError(f"unexpected request {key}")
        route = queues[key]
        if isinstance(route, list):
            if not route:
                raise AssertionError(f"no response left for {key}")
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, httpx.Response):
            return _fresh(route)
        return route(request)

    mono = mono or FakeMono()
    clock = clock or ArcusClock(network, time_ns=time_ns or FakeTimeNs(), monotonic=mono)
    budget = budget or IpBudget(network, clock=mono, sleep=FakeSleep(mono))
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=REST_BASE)
    client = ArcusClient(
        network,
        clock=clock,
        ip_budget=budget,
        http=http,
        clock_max_age_s=clock_max_age_s or (lambda: 900.0),
        monotonic=mono,
    )
    return client, calls


def golden_auth(ref: ArcusAccountRef = REF) -> Any:
    """The golden-vector auth (seed ``bytes(range(32))``)."""
    return make_auth(ref, Ed25519Signer.from_seed_hex(SEED))
