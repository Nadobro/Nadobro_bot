"""Unit tests for ReverseGridController — the venue-trigger momentum grid.

Driven entirely by the MockNadoAdapter: the controller arms a trigger ladder,
the test moves the mid and calls ``cross_triggers`` to fire the rungs the venue
would fire, and the controller reconciles from the net-position poll. Covers:
arming the symmetric ladder while flat, cancelling the losing side on the first
fill, arming + trailing the reduce-only stop, the stop firing and re-arming the
ladder, pyramiding adds re-sizing the stop, the flat re-anchor leash, and failing
safe on an unreadable venue.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.nadobro.engine.controllers.reverse_grid import ReverseGridController
from src.nadobro.engine.types import TradeType, _dec

from tests.engine._mock_nado import MockNadoAdapter

PAIR = "BTC-PERP"


def _adapter(**kw):
    kw.setdefault("mid", Decimal("100"))
    kw.setdefault("tick", Decimal("0.01"))
    kw.setdefault("lot", Decimal("0.0001"))
    kw.setdefault("min_notional", Decimal("1"))
    kw.setdefault("venue_held", {PAIR: Decimal(0)})
    return MockNadoAdapter(**kw)


def _controller(adapter, **cfg):
    configs = {
        "trading_pair": PAIR,
        "levels": 2,
        "step_pct": Decimal("0.01"),
        "order_amount_quote": Decimal("100"),
    }
    configs.update(cfg)
    return ReverseGridController(
        user_id=1, orchestrator=object(), adapter=adapter, inventory=None, configs=configs,
    )


def _approx(a, b, tol=Decimal("0.02")) -> bool:
    return abs(_dec(a) - _dec(b)) <= tol


# ── flat: arm the symmetric ladder ──────────────────────────────────────

def test_flat_arms_a_symmetric_trigger_ladder():
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()
        buys = [o for o in a.placed_triggers if o.side is TradeType.BUY]
        sells = [o for o in a.placed_triggers if o.side is TradeType.SELL]
        assert len(buys) == 2 and len(sells) == 2
        # BUY rungs ABOVE the anchor, SELL rungs BELOW — the reverse-grid shape.
        assert sorted(o.price for o in buys) == [Decimal("101"), Decimal("102")]
        assert sorted(o.price for o in sells) == [Decimal("98"), Decimal("99")]

    asyncio.run(body())


def test_unreadable_venue_arms_nothing():
    """held_base None (venue unreadable) must hold — never place a ladder off a
    bad read."""
    async def body():
        a = _adapter(venue_held=None)   # pair absent → held_base returns None
        c = _controller(a)
        await c.on_tick()
        assert a.placed_triggers == []

    asyncio.run(body())


# ── first fill: cancel the losing side + arm the protective stop ────────

def test_first_buy_fill_cancels_sells_and_arms_a_stop():
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()                       # arm ladder around 100
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))      # BUY@101 fires; BUY@102 and sells rest
        await c.on_tick()                        # detect long, react

        assert c._pos_base > 0                    # long
        assert _approx(c._avg_entry, Decimal("101"))
        # both SELL rungs cancelled (the break went up)
        assert len(a.cancelled_triggers) == 2
        # a reduce-only stop is armed at the protective distance (avg*(1-2*step))
        assert c._stop_digest is not None
        assert _approx(c._stop_level, Decimal("98.98"))
        # the far BUY rung stays armed for pyramiding
        assert any(not r.fired and r.side is TradeType.BUY for r in c._rungs)

    asyncio.run(body())


# ── trailing stop ratchets, fires, and the ladder re-arms ──────────────

def test_stop_trails_then_fires_then_reanchors():
    async def body():
        a = _adapter()
        c = _controller(a, levels=1)             # one rung/side: isolate the trail
        await c.on_tick()                         # arm around 100 (BUY@101, SELL@99)
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))        # BUY@101 fires → long ~0.99
        await c.on_tick()                          # open; stop protective @ 98.98
        assert _approx(c._stop_level, Decimal("98.98"))

        # Price runs to 104: +2.97% > 2% arm → trail ratchets the stop up.
        a.set_mid(Decimal("104"))
        a.cross_triggers(Decimal("104"))          # nothing left to fire on the way up
        await c.on_tick()
        assert c._trail_armed is True
        assert _approx(c._stop_level, Decimal("101.92"))   # 104*(1-0.02)

        # Pull back to 101: crosses the trailed stop (101.92) → position closes.
        a.set_mid(Decimal("101"))
        a.cross_triggers(Decimal("101"))
        assert a.venue_held[PAIR] == 0             # the reduce-only stop flattened it
        await c.on_tick()

        assert c._pos_base == 0 and c._avg_entry is None
        assert c._stop_digest is None
        assert _approx(c._anchor, Decimal("101"))  # re-anchored to the current mid
        # a fresh ladder is armed around the new anchor
        assert sum(1 for r in c._rungs if not r.fired) == 2

    asyncio.run(body())


# ── pyramiding: an add grows the position and re-sizes the stop ────────

def test_pyramid_add_grows_position_and_resizes_stop():
    async def body():
        a = _adapter()
        c = _controller(a)                        # levels=2
        await c.on_tick()
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))        # BUY@101 fires
        await c.on_tick()
        first_size = c._stop_size
        assert _approx(first_size, Decimal("0.99"), tol=Decimal("0.02"))

        # Extend the trend: BUY@102 fires too → the pyramid grows.
        a.set_mid(Decimal("102.5"))
        a.cross_triggers(Decimal("102.5"))
        await c.on_tick()

        assert abs(c._pos_base) > abs(first_size)          # position grew
        assert c._stop_size is not None and c._stop_size > first_size
        # avg entry is now the VWAP of BOTH fired rungs (~101.5)
        assert _approx(c._avg_entry, Decimal("101.5"), tol=Decimal("0.1"))

    asyncio.run(body())


# ── flat re-anchor leash ───────────────────────────────────────────────

def test_flat_ladder_reanchors_when_mid_drifts():
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()                          # ladder around 100
        assert len(a.placed_triggers) == 4
        # Mid drifts 3% (> reanchor_bands(2) * step(1%) = 2%) with no fills.
        a.set_mid(Decimal("103"))
        await c.on_tick()
        assert len(a.cancelled_triggers) == 4      # old ladder cancelled
        assert len(a.placed_triggers) == 8         # a fresh ladder around 103
        assert _approx(c._anchor, Decimal("103"))

    asyncio.run(body())


def test_flat_ladder_holds_within_the_leash():
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()
        # Drift only 1% (< 2% threshold): the ladder is NOT re-anchored.
        a.set_mid(Decimal("101"))
        await c.on_tick()
        assert a.cancelled_triggers == []
        assert len(a.placed_triggers) == 4
        assert _approx(c._anchor, Decimal("100"))

    asyncio.run(body())


# ── on_stop tears the whole ladder down ────────────────────────────────

def test_on_stop_cancels_all_triggers():
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()
        await c.on_stop("user stopped")
        assert len(a.cancelled_triggers) == 4      # every armed rung cancelled
        assert c._rungs == []

    asyncio.run(body())


# ── short side mirrors long ────────────────────────────────────────────

def test_first_sell_fill_opens_a_short_and_arms_stop_above():
    async def body():
        a = _adapter()
        c = _controller(a, levels=1)
        await c.on_tick()                          # BUY@101, SELL@99
        a.set_mid(Decimal("98.5"))
        a.cross_triggers(Decimal("98.5"))          # SELL@99 fires (mid <= 99) → short
        await c.on_tick()

        assert c._pos_base < 0                       # short
        assert _approx(c._avg_entry, Decimal("99"))
        # a short's protective stop sits ABOVE entry: 99*(1+0.02)=100.98
        assert _approx(c._stop_level, Decimal("100.98"))
        assert len(a.cancelled_triggers) == 1        # the BUY rung was cancelled

    asyncio.run(body())


def test_short_stop_fires_and_flattens():
    async def body():
        a = _adapter()
        c = _controller(a, levels=1)
        await c.on_tick()
        a.set_mid(Decimal("98.5"))
        a.cross_triggers(Decimal("98.5"))          # open short ~1.01
        await c.on_tick()
        # Rally back up through the stop (100.98) → the BUY-close stop fires.
        a.set_mid(Decimal("101"))
        a.cross_triggers(Decimal("101"))
        assert a.venue_held[PAIR] == 0
        await c.on_tick()
        assert c._pos_base == 0 and c._stop_digest is None

    asyncio.run(body())
