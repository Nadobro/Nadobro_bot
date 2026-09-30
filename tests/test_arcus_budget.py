"""venue/arcus/budget.py — IP weight bucket with lanes, pool governor (02 §6 / §12.5).

Sources: docs rate-limits "1,500 weight … 25 weight/second"; weight tiers;
"a full 1,000-row page of `fills` costs `20 + 50 = 70`"; "maxing at weight 7
for a 100-level book"; batch "floor(N/40)". DENIED ≠ EMPTY: no / stale pool
reading is UNKNOWN (None), never full or empty.
"""
from __future__ import annotations

import asyncio
import logging
import math
from decimal import Decimal

import pytest

from arcus_helpers import REF, FakeMono, FakeSleep, load_fixture
from src.nadobro.venue.arcus import budget as B
from src.nadobro.venue.arcus.budget import (
    POOL_NOT_ENFORCED,
    IpBudget,
    PoolGovernor,
    PoolReserves,
    batch_addon,
    endpoint_weight,
    l2_weight,
    list_addon,
    session_reserves,
)
from src.nadobro.venue.arcus.parse import parse_rate_limit
from src.nadobro.venue.arcus.types import ArcusAccountRef, Lane, PoolReading


def _budget(mono: FakeMono | None = None, **kwargs) -> tuple[IpBudget, FakeMono, FakeSleep]:
    mono = mono or FakeMono()
    sleep = FakeSleep(mono)
    return IpBudget("testnet", clock=mono, sleep=sleep, **kwargs), mono, sleep


def _write_reading(remaining: int | None, at: float) -> PoolReading:
    return PoolReading(remaining=remaining, cap=None, used=None, next_available_ms=None, source="write", as_of_mono=at)


# --- weights ------------------------------------------------------------------------------


def test_weights_table():
    expected = {
        "health": 0, "placeOrder": 0, "cancelOrder": 0, "batchCancelOrders": 0,
        "root": 1, "time": 1, "compliance": 1,
        "bbo": 2, "mids": 2, "account": 2, "positions": 2, "order": 2, "feeTiers": 2, "leverages": 2,
        "accountStats": 2, "rateLimit": 2, "l2OrderBook": 2,
        "prices": 20, "markets": 20, "trade": 20, "trades": 20, "candles": 20, "portfolio": 20,
        "openOrders": 20, "orders": 20, "fills": 20, "funding": 20, "fundingRates": 20, "apiKeys": 20,
        "setLeverage": 125,
    }
    assert {k: endpoint_weight(k) for k in expected} == expected
    assert set(B._BASE_WEIGHT) == set(expected)
    for bad in ("nope", "", None, "cancelAllOrders"):
        with pytest.raises(ValueError):
            endpoint_weight(bad)  # type: ignore[arg-type]


def test_list_addon_l2_and_batch():
    assert list_addon("fills", 1000) == 50  # "20 + 50 = 70"
    assert list_addon("openOrders", 1000) == 20
    assert list_addon("candles", 1500) == 25  # "20 + 25 = 45"
    assert list_addon("account", 500) == 0
    assert list_addon("markets", 19) == 0 and list_addon("markets", 20) == 1
    for bad in (-1, True, 1.5):
        with pytest.raises(ValueError):
            list_addon("fills", bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        list_addon("nope", 5)
    assert l2_weight(100) == 7  # "maxing at weight 7 for a 100-level book"
    assert l2_weight(20) == 3
    assert l2_weight(19) == 2
    assert l2_weight(500) == 7 and l2_weight(0) == 2  # clamped to [1, 100]
    assert batch_addon(39) == 0 and batch_addon(40) == 1 and batch_addon(100) == 2
    with pytest.raises(ValueError):
        batch_addon(-1)


# --- IP bucket -----------------------------------------------------------------------------


def test_bucket_starts_full_and_refills():
    budget, mono, _ = _budget()
    assert budget.level() == 1500
    assert budget.try_take(1480, Lane.L0_BRAKE)
    assert budget.level() == 20
    mono.advance(1.0)
    assert budget.level() == 45  # +25/s
    mono.advance(1000)
    assert budget.level() == 1500  # never above capacity


def test_lane_floors():
    budget, _, _ = _budget()
    assert budget.try_take(1200, Lane.L1_ENGINE)  # 1500 -> 300 (L1 floor)
    assert not budget.try_take(1, Lane.L1_ENGINE)
    assert not budget.try_take(1, Lane.L2_INTERACTIVE)
    assert not budget.try_take(1, Lane.L3_BACKGROUND)
    assert budget.try_take(300, Lane.L0_BRAKE)  # L0 has no floor
    assert budget.level() == 0

    budget, _, _ = _budget()
    assert budget.try_take(800, Lane.L3_BACKGROUND)  # 1500 -> 700 (L3 floor)
    assert not budget.try_take(1, Lane.L3_BACKGROUND)
    assert budget.try_take(300, Lane.L2_INTERACTIVE)  # 700 -> 400 (L2 floor)
    assert not budget.try_take(1, Lane.L2_INTERACTIVE)
    assert budget.try_take(100, Lane.L1_ENGINE)


def test_acquire_waits_bounded():
    async def body():
        budget, mono, sleep = _budget()
        assert budget.try_take(1180, Lane.L0_BRAKE)  # level 320: L1 (floor 300) needs +10 for 30
        start = mono.now
        assert await budget.acquire(30, Lane.L1_ENGINE, max_wait_s=2)
        assert 0 < mono.now - start <= 2.0
        assert sleep.calls

        budget, mono, sleep = _budget()
        assert budget.try_take(1180, Lane.L0_BRAKE)
        assert not await budget.acquire(30, Lane.L1_ENGINE, max_wait_s=0)
        assert sleep.calls == []  # never sleeps when it cannot succeed in time
        assert budget.snapshot()["denied"]["L1_ENGINE"] == 1

        # default wait per lane: L3 never waits
        budget, mono, sleep = _budget()
        assert budget.try_take(800, Lane.L3_BACKGROUND)
        assert not await budget.acquire(1, Lane.L3_BACKGROUND)
        assert sleep.calls == []

    asyncio.run(body())


def test_acquire_impossible_weight(caplog):
    async def body():
        budget, _, sleep = _budget()
        with caplog.at_level(logging.WARNING, logger=B.__name__):
            assert not await budget.acquire(1300, Lane.L3_BACKGROUND, max_wait_s=60)
            assert not await budget.acquire(1300, Lane.L3_BACKGROUND, max_wait_s=60)
        assert sleep.calls == []
        assert sum("can never fit" in r.getMessage() for r in caplog.records) == 1
        assert await budget.acquire(1300, Lane.L0_BRAKE, max_wait_s=0)  # fits L0

    asyncio.run(body())


def test_acquire_validates_inputs():
    async def body():
        budget, _, _ = _budget()
        with pytest.raises(ValueError):
            await budget.acquire(-1, Lane.L0_BRAKE)
        with pytest.raises(ValueError):
            await budget.acquire(1, 1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await budget.acquire(1, Lane.L0_BRAKE, max_wait_s=float("nan"))
        with pytest.raises(ValueError):
            budget.try_take(True, Lane.L0_BRAKE)  # type: ignore[arg-type]

    asyncio.run(body())


def test_charge_after_can_go_negative():
    async def body():
        budget, mono, sleep = _budget()
        assert budget.try_take(1490, Lane.L0_BRAKE)
        budget.charge_after(70)
        assert budget.level() == -60
        budget.charge_after(0)
        assert budget.level() == -60
        start = mono.now
        assert await budget.acquire(2, Lane.L0_BRAKE, max_wait_s=5)
        assert mono.now - start >= 62 / 25 - 1e-9  # waited for the refill
        budget.charge_after(10**9)
        assert budget.level() == -1500  # floored at -capacity
        with pytest.raises(ValueError):
            budget.charge_after(-1)

    asyncio.run(body())


def test_server_429_blocks_all_lanes_and_writes(caplog):
    async def body():
        budget, mono, sleep = _budget()
        with caplog.at_level(logging.WARNING, logger=B.__name__):
            budget.note_server_429(2000)
            budget.note_server_429(500)  # never shortens the block
        assert budget.write_blocked()
        assert budget.level() == 0
        assert not await budget.acquire(2, Lane.L0_BRAKE, max_wait_s=0)
        assert not await budget.acquire(0, Lane.L0_BRAKE, max_wait_s=0)
        assert not budget.try_take(0, Lane.L0_BRAKE)
        assert sum("server 429" in r.getMessage() for r in caplog.records) == 1  # rate-limited
        mono.advance(2.0)
        assert not budget.write_blocked()
        assert await budget.acquire(0, Lane.L0_BRAKE, max_wait_s=0)
        assert await budget.acquire(2, Lane.L0_BRAKE, max_wait_s=0)  # 2 s of refill = 50

        # a waiting brake gets through once the block lifts
        budget, mono, sleep = _budget()
        budget.note_server_429(1000)
        assert await budget.acquire(10, Lane.L0_BRAKE, max_wait_s=2)
        assert mono.now >= 1000.0 + 1.0

    asyncio.run(body())


def test_floor_validation():
    with pytest.raises(ValueError):
        IpBudget("testnet", lane_floors={Lane.L1_ENGINE: 800})  # L1 > L2 (400)
    with pytest.raises(ValueError):
        IpBudget("testnet", lane_floors={Lane.L3_BACKGROUND: 1500})  # >= capacity
    with pytest.raises(ValueError):
        IpBudget("testnet", lane_floors={Lane.L0_BRAKE: -1})
    with pytest.raises(ValueError):
        IpBudget("arcus_testnet")
    with pytest.raises(ValueError):
        IpBudget("testnet", capacity=0)
    ok = IpBudget(
        "testnet",
        lane_floors={Lane.L0_BRAKE: 0, Lane.L1_ENGINE: 200, Lane.L2_INTERACTIVE: 300, Lane.L3_BACKGROUND: 600},
    )
    assert ok.snapshot()["floors"] == {"L0_BRAKE": 0, "L1_ENGINE": 200, "L2_INTERACTIVE": 300, "L3_BACKGROUND": 600}


def test_snapshot_has_counters_and_no_secrets():
    budget, _, _ = _budget()
    budget.try_take(5, Lane.L2_INTERACTIVE)
    snap = budget.snapshot()
    assert snap["network"] == "testnet" and snap["capacity"] == 1500
    assert snap["taken"]["L2_INTERACTIVE"] == 1
    assert snap["blocked_for_s"] == 0.0


# --- pool governor -------------------------------------------------------------------------


def test_pool_reading_age():
    mono = FakeMono(100.0)
    gov = PoolGovernor(REF, clock=mono)
    assert gov.reading_age_s("order") is None
    gov.update_from_write("order", _write_reading(19_999, at=100.0))
    mono.now = 130.0
    assert gov.reading_age_s("order") == 30.0
    order, cancel = parse_rate_limit(load_fixture("rate_limit_testnet.json"), ref=REF, now_mono=130.0)
    gov.update_from_rest(order, cancel, echoed_account_index=0)  # newest wins
    assert gov.reading_age_s("order") == 0.0
    assert gov.reading_age_s("cancel") == 0.0
    with pytest.raises(ValueError):
        gov.reading_age_s("nope")  # type: ignore[arg-type]


def test_pool_write_readings():
    mono = FakeMono(100.0)
    gov = PoolGovernor(REF, clock=mono)
    gov.update_from_write("order", _write_reading(None, at=100.0))  # -1 sentinel -> not enforced
    assert gov.headroom("order") == POOL_NOT_ENFORCED
    gov.update_from_write("order", _write_reading(-5, at=101.0))  # on the drip
    gov.update_from_write("cancel", _write_reading(40_000, at=101.0))
    assert gov.headroom("order") == -5
    assert gov.below_reserve(PoolReserves(order=0, cancel=0)) is True
    gov.update_from_write("order", _write_reading(19_000, at=99.0))  # older: ignored
    assert gov.headroom("order") == -5
    # a missing rateLimit on a write body means "no update": the caller simply
    # does not call update_from_write, and the last value stays.
    assert gov.headroom("order") == -5
    with pytest.raises(ValueError):
        gov.update_from_write("order", PoolReading(1, 1, 0, 0, "rest", 1.0))
    with pytest.raises(ValueError):
        gov.update_from_write("both", _write_reading(1, at=1.0))  # type: ignore[arg-type]


def test_pool_rest_readings():
    mono = FakeMono(10.0)
    gov = PoolGovernor(REF, clock=mono)
    order, cancel = parse_rate_limit(load_fixture("rate_limit_testnet.json"), ref=REF, now_mono=10.0)
    gov.update_from_rest(order, cancel, echoed_account_index=0)
    assert gov.headroom("order") == 20_000 and gov.headroom("cancel") == 40_000
    drip = PoolReading(remaining=0, cap=20_000, used=20_000, next_available_ms=850, source="rest", as_of_mono=11.0)
    gov.update_from_rest(drip, cancel, echoed_account_index=0)
    assert gov.headroom("order") == 0
    with pytest.raises(ValueError):
        gov.update_from_rest(order, cancel, echoed_account_index=1)  # ref index 0
    with pytest.raises(ValueError):
        gov.update_from_rest(_write_reading(1, at=1.0), cancel, echoed_account_index=0)


def test_pool_unknown_and_stale():
    mono = FakeMono(0.0)
    gov = PoolGovernor(REF, clock=mono)
    assert gov.headroom("order") is None
    assert gov.below_reserve(PoolReserves(order=1, cancel=1)) is None
    gov.update_from_write("order", _write_reading(100, at=0.0))
    gov.update_from_write("cancel", _write_reading(100, at=0.0))
    assert gov.below_reserve(PoolReserves(order=50, cancel=50)) is False
    mono.now = 600.0
    assert gov.headroom("order") == 100  # exactly at the stale bound
    mono.now = 600.5
    assert gov.headroom("order") is None  # stale -> unknown, never "full"
    assert gov.below_reserve(PoolReserves(order=1, cancel=1)) is None
    assert gov.reading_age_s("order") == 600.5  # the age is still visible


def test_cap_cooldown_hold_runway():
    mono = FakeMono(0.0)
    gov = PoolGovernor(REF, clock=mono)
    assert gov.cap_cooldown_active() is None
    gov.note_cap_cooldown("open_order_cap", 30)
    gov.note_cap_cooldown("shorter", 5)  # never shortens
    assert gov.cap_cooldown_active() == "open_order_cap"
    mono.now = 30.0
    assert gov.cap_cooldown_active() is None
    gov.note_hold(True)
    mono.now = 40.0
    gov.note_hold(True)  # stamps once
    assert gov.hold_since_mono() == 30.0
    gov.note_hold(False)
    assert gov.hold_since_mono() is None
    assert gov.runway_hours("order", 10) is None  # unknown headroom
    gov.update_from_write("order", _write_reading(1000, at=40.0))
    assert gov.runway_hours("order", 0) == math.inf
    assert gov.runway_hours("order", 100) == 10.0
    gov.update_from_write("order", _write_reading(-3, at=41.0))
    assert gov.runway_hours("order", 100) == 0.0
    with pytest.raises(ValueError):
        gov.runway_hours("order", float("nan"))
    snap = gov.snapshot()
    assert snap["account_index"] == 0 and REF.address not in repr(snap)


def test_governor_rejects_bad_ref():
    with pytest.raises(ValueError):
        PoolGovernor("0xabc")  # type: ignore[arg-type]
    other = ArcusAccountRef("testnet", REF.address, 1)
    assert PoolGovernor(other).ref.account_index == 1


def test_session_reserves_formula():
    r = session_reserves(
        resting_orders=30, reducing_requotes_per_h=120, hold_max_s=900, floor_order=0, floor_cancel=0
    )
    assert (r.order, r.cancel) == (38, 66)  # ceil(37.4), ceil(66.0) — not 67 (float would say 66.000…01)
    r = session_reserves(
        resting_orders=30, reducing_requotes_per_h=120, hold_max_s=900, floor_order=500, floor_cancel=1000
    )
    assert (r.order, r.cancel) == (500, 1000)
    r = session_reserves(
        resting_orders=0, reducing_requotes_per_h=0.0, hold_max_s=0.0, flatten_units=0, floor_order=0, floor_cancel=0
    )
    assert (r.order, r.cancel) == (0, 0)
    r = session_reserves(
        resting_orders=30, reducing_requotes_per_h=Decimal("120"), hold_max_s=Decimal("900"),
        margin_frac=Decimal("0.10"), floor_order=0, floor_cancel=0,
    )
    assert (r.order, r.cancel) == (38, 66)
    for bad in (
        {"reducing_requotes_per_h": Decimal("NaN")},
        {"resting_orders": -1},
        {"reducing_requotes_per_h": -1.0},
        {"hold_max_s": float("inf")},
        {"margin_frac": -0.1},
        {"floor_order": -1},
        {"resting_orders": True},
    ):
        kwargs = dict(resting_orders=1, reducing_requotes_per_h=1.0, hold_max_s=1.0, floor_order=0, floor_cancel=0)
        kwargs.update(bad)
        with pytest.raises(ValueError):
            session_reserves(**kwargs)
    with pytest.raises(ValueError):
        PoolReserves(order=-1, cancel=0)
