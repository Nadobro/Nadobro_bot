"""Shared constants/helpers for the Arcus venue-library tests (02 §12).

Golden-vector constants are the ones of 02 §5.1: the seed is
``bytes(range(32)).hex()`` and every payload/signature was re-computed
independently (cryptography 50.0.0). ``tests/`` is on ``sys.path`` via
``tests/conftest.py``, so test files ``import arcus_helpers``.

``mock_client(...)`` (an ``ArcusClient`` over ``httpx.MockTransport``) is added
by the client stage together with ``client.py``.
"""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from typing import Any

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
