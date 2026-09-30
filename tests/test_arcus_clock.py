"""venue/arcus/clock.py — offset, strictly increasing per-key ct, GTT (02 §5.2 / §12.4).

DENIED ≠ EMPTY: a denied /v1/time never becomes "skew 0". A MEASURED skew is
corrected by the offset and never blocks writes (02 D2).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from arcus_helpers import CT0, FakeMono, FakeTimeNs
from src.nadobro.venue.arcus import clock as C
from src.nadobro.venue.arcus.clock import GTT_MAX_DAYS, GTT_MIN_DAYS, ArcusClock
from src.nadobro.venue.arcus.errors import LocalDenied, Ok, Throttled, Unavailable
from src.nadobro.venue.arcus.types import Lane

DAY_US = 86_400 * 1_000_000


class FakeTimeClient:
    """Stands in for ArcusClient.get_time; records calls."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[Lane, float | None]] = []

    async def get_time(self, *, lane: Lane, max_wait_s: float | None = None):
        self.calls.append((lane, max_wait_s))
        return self.result


def _clock(time_ns=None, mono=None) -> ArcusClock:
    return ArcusClock("testnet", time_ns=time_ns or FakeTimeNs(), monotonic=mono or FakeMono())


def test_rejects_non_network_tokens():
    for bad in ("arcus_testnet", "Testnet", "", None):
        with pytest.raises(ValueError):
            ArcusClock(bad)  # type: ignore[arg-type]
    assert ArcusClock("mainnet").network == "mainnet"


def test_ct_strictly_increasing_same_ns():
    clock = _clock(FakeTimeNs(now=CT0))
    assert [clock.next_ct_ns("k") for _ in range(3)] == [CT0, CT0 + 1, CT0 + 2]


def test_ct_independent_per_key():
    clock = _clock(FakeTimeNs(now=CT0))
    assert clock.next_ct_ns("A") == CT0
    assert clock.next_ct_ns("B") == CT0
    assert clock.next_ct_ns("A") == CT0 + 1


def test_ct_never_goes_back_when_clock_steps_back():
    fake = FakeTimeNs(now=CT0 + 1000)
    clock = _clock(fake)
    assert clock.next_ct_ns("k") == CT0 + 1000
    fake.now = CT0
    assert clock.next_ct_ns("k") == CT0 + 1001
    fake.now = CT0 + 5000
    assert clock.next_ct_ns("k") == CT0 + 5000


def test_ct_requires_key_and_lru_is_bounded():
    clock = _clock()
    with pytest.raises(ValueError):
        clock.next_ct_ns("")
    for i in range(C._CT_KEYS_MAX + 5):
        clock.next_ct_ns(f"k{i}")
    assert len(clock._last_ct) == C._CT_KEYS_MAX


def test_sync_sets_offset():
    async def body():
        server_ns = CT0 + 2_500_000_000  # server 2.5 s ahead
        fake = FakeTimeNs(now=CT0, script=[CT0 - 1_000_000, CT0 + 1_000_000])  # t0, t1 (2 ms RTT)
        mono = FakeMono(500.0)
        clock = ArcusClock("testnet", time_ns=fake, monotonic=mono)
        client = FakeTimeClient(Ok(server_ns, 200, 1))
        skew = await clock.sync(client, lane=Lane.L0_BRAKE, max_wait_s=2.0)
        assert client.calls == [(Lane.L0_BRAKE, 2.0)]
        assert clock.offset_ns() == server_ns - CT0
        assert skew == pytest.approx(2500.0)
        assert clock.skew_ms() == pytest.approx(2500.0)
        assert clock.last_sync_age_s() == 0.0
        mono.advance(30)
        assert clock.last_sync_age_s() == 30.0
        assert clock.synced_within(30) and not clock.synced_within(29.9)
        # next_ct_ns and now_us include the offset
        assert clock.next_ct_ns("k") == CT0 + (server_ns - CT0)
        assert clock.now_us() == (CT0 + (server_ns - CT0)) // 1000
        snap = clock.snapshot()
        assert snap["network"] == "testnet" and snap["last_rtt_ms"] == pytest.approx(2.0)

    asyncio.run(body())


@pytest.mark.parametrize(
    "denial",
    [Throttled("read_ip", 1000, ()), Unavailable(503, "x"), LocalDenied("ip_budget:L1_ENGINE")],
)
def test_sync_denied_keeps_state(denial):
    async def body():
        mono = FakeMono()
        clock = ArcusClock("testnet", time_ns=FakeTimeNs(), monotonic=mono)
        assert await clock.sync(FakeTimeClient(denial)) is None
        assert clock.skew_ms() is None and clock.last_sync_age_s() is None
        assert clock.offset_ns() == 0 and not clock.synced_within(10**9)
        # after a success, a later denial changes nothing
        await clock.sync(FakeTimeClient(Ok(CT0 + 7_000_000, 200, 1)))
        before = (clock.offset_ns(), clock.skew_ms(), clock.last_sync_age_s())
        mono.advance(1)
        assert await clock.sync(FakeTimeClient(denial)) is None
        assert (clock.offset_ns(), clock.skew_ms()) == before[:2]
        assert clock.last_sync_age_s() == before[2] + 1

    asyncio.run(body())


def test_sync_rejects_implausible_server_values():
    async def body():
        for value in (1_790_000_000_000_000, True, "1790000000000000000", 2**63):
            clock = _clock()
            assert await clock.sync(FakeTimeClient(Ok(value, 200, 1))) is None
            assert clock.skew_ms() is None

    asyncio.run(body())


def test_sync_discards_slow_rtt():
    async def body():
        fake = FakeTimeNs(script=[CT0, CT0 + 6_000_000_000])  # 6 s round trip
        clock = ArcusClock("testnet", time_ns=fake, monotonic=FakeMono())
        assert await clock.sync(FakeTimeClient(Ok(CT0, 200, 1))) is None
        assert clock.skew_ms() is None and clock.last_sync_age_s() is None
        backwards = FakeTimeNs(script=[CT0, CT0 - 1])  # local clock stepped back mid-call
        clock2 = ArcusClock("testnet", time_ns=backwards, monotonic=FakeMono())
        assert await clock2.sync(FakeTimeClient(Ok(CT0, 200, 1))) is None

    asyncio.run(body())


def test_skew_warning_logged_not_blocking(caplog):
    async def body():
        clock = _clock(FakeTimeNs(now=CT0))
        caplog.set_level(logging.WARNING, logger=C.__name__)
        skew = await clock.sync(FakeTimeClient(Ok(CT0 + 20_000_000_000, 200, 1)))
        assert skew == pytest.approx(20_000.0)
        assert clock.synced_within(900)
        warnings = [r.getMessage() for r in caplog.records if r.name == C.__name__]
        assert warnings == ["arcus testnet clock skew 20000 ms (corrected)"]

    asyncio.run(body())


def test_gtt_clamps_and_units(caplog):
    clock = _clock(FakeTimeNs(now=CT0))
    now_us = CT0 // 1000
    assert clock.gtt_us(40) == now_us + 40 * DAY_US
    caplog.set_level(logging.WARNING, logger=C.__name__)
    assert clock.gtt_us(10) == now_us + GTT_MIN_DAYS * DAY_US
    assert clock.gtt_us(365) == now_us + GTT_MAX_DAYS * DAY_US
    assert len([r for r in caplog.records if "clamped" in r.getMessage()]) == 1
    for bad in (40.0, True, "40"):
        with pytest.raises(TypeError):
            clock.gtt_us(bad)  # type: ignore[arg-type]
    assert C.GTT_MIN_AHEAD_US == 31 * DAY_US


def test_invalidate():
    async def body():
        clock = _clock(FakeTimeNs(now=CT0))
        await clock.sync(FakeTimeClient(Ok(CT0 + 3_000_000, 200, 1)))
        assert clock.synced_within(900)
        clock.invalidate()
        assert not clock.synced_within(900)
        assert clock.last_sync_age_s() is None
        assert clock.offset_ns() == 3_000_000

    asyncio.run(body())
