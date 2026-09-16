"""Reverse Grid vs the venue's trigger service (2026-09-16 fixes).

Three venue facts the controller must respect:

* Nado holds at most 25 PENDING trigger orders per product per subaccount, and
  a flat ladder is 2 x levels — so ``levels`` is capped at 12 per side (prod
  session 292 asked for 20 levels = 40 rungs; exactly 25 were placed);
* entry rungs are IOC (fill on fire or vanish) so a lagged fire never rests as
  an untracked maker limit — which means a rung can be GONE without a fill;
* the venue may drop pending triggers on its own (expiry, signer change).

So the ladder is RECONCILED against ``list_trigger_orders``: a rung the venue no
longer holds is re-armed (flat) or dropped (in a position); a vanished stop is
re-armed at once. An unreadable list changes nothing. A venue that cannot be read
at all HOLDS — and now says so on the card (gate telemetry) instead of a silent
"LIVE, 0 orders".
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.nadobro.engine.controllers.reverse_grid import ReverseGridController
from src.nadobro.engine.types import TradeType
from src.nadobro.quant.rgrid_sizing import REVGRID_MAX_LEVELS

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
        "trading_pair": PAIR, "levels": 2, "step_pct": Decimal("0.01"),
        "order_amount_quote": Decimal("100"), "revgrid_chop_stand_down": False,
    }
    configs.update(cfg)
    return ReverseGridController(
        user_id=1, orchestrator=object(), adapter=adapter, inventory=None, configs=configs,
    )


# ── venue cap: 12 rungs per side ────────────────────────────────────────

def test_levels_are_capped_at_the_venue_pending_trigger_limit():
    async def body():
        a = _adapter()
        c = _controller(a, levels=20)
        assert c.levels == REVGRID_MAX_LEVELS == 12
        await c.on_tick()
        assert len(a.placed_triggers) == 24, "a flat ladder is 2 x 12 <= the venue's 25 pending cap"
    asyncio.run(body())


# ── IOC entries ──────────────────────────────────────────────────────────

def test_entry_rungs_are_placed_ioc_so_a_lagged_fire_never_rests():
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()
        assert a.placed_triggers and all(
            a.trigger_order_types[o.id] == "ioc" for o in a.placed_triggers
        )
        # the price bound is the wider 15bp default (was 5bp)
        assert c.entry_slippage_pct == 0.15
    asyncio.run(body())


# ── reconciliation while flat ───────────────────────────────────────────

def test_a_rung_the_venue_dropped_is_re_armed_when_price_crossed_it_flat():
    """The BUY@101 rung vanished (IOC fired unfilled). Price is now past its level
    with the position unchanged -> a fire was expected -> the controller must not
    trust the hole forever: it tears the ladder down and re-anchors at the mid."""
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()                                   # ladder around 100
        buy_101 = next(o for o in a.placed_triggers if o.side is TradeType.BUY and o.price == Decimal("101"))
        a.drop_trigger(buy_101.id)                          # venue forgot it, no fill
        a.set_mid(Decimal("101.2"))                         # past the level, still flat
        await c.on_tick()
        assert c._anchor == Decimal("101.2"), "re-anchored at the current mid"
        fresh = [o for o in a.placed_triggers[4:]]
        assert len(fresh) == 4, "a complete fresh ladder was armed"
        assert sorted(o.price for o in fresh if o.side is TradeType.BUY) == [
            Decimal("101.2") * Decimal("1.01"), Decimal("101.2") * Decimal("1.02")]
        # the 3 survivors of the old ladder were cancelled; the vanished one was not
        assert buy_101.id not in a.cancelled_triggers
        assert len(a.cancelled_triggers) == 3
    asyncio.run(body())


def test_the_periodic_reconcile_also_catches_a_dropped_rung_without_a_cross():
    """No price cross, so nothing is 'expected' — the periodic reconcile (every
    ``revgrid_reconcile_every_ticks`` ticks since the last one) still notices a
    rung the venue dropped (e.g. a signer change) and re-arms the ladder."""
    async def body():
        a = _adapter()
        c = _controller(a, revgrid_reconcile_every_ticks=3)
        await c.on_tick()                                   # tick 1: ladder armed (4)
        sell_99 = next(o for o in a.placed_triggers if o.side is TradeType.SELL and o.price == Decimal("99"))
        a.drop_trigger(sell_99.id)                          # dropped by the venue
        await c.on_tick()                                   # tick 2: first periodic reconcile -> re-armed
        assert len(a.placed_triggers) == 8
        gone = next(o for o in a.placed_triggers[4:] if o.side is TradeType.BUY and o.price == Decimal("101"))
        a.drop_trigger(gone.id)
        await c.on_tick()                                   # tick 3: not due (1 tick since)
        await c.on_tick()                                   # tick 4: not due (2 ticks since)
        assert len(a.placed_triggers) == 8
        await c.on_tick()                                   # tick 5: due -> re-armed again
        assert len(a.placed_triggers) == 12
    asyncio.run(body())


def test_an_unreadable_trigger_list_changes_nothing():
    async def body():
        a = _adapter(fail_on=["list_trigger_orders"], fail_times=99)
        c = _controller(a)
        await c.on_tick()
        buy_101 = next(o for o in a.placed_triggers if o.price == Decimal("101"))
        a.drop_trigger(buy_101.id)
        a.set_mid(Decimal("101.2"))
        await c.on_tick()
        assert c._anchor == Decimal("100"), "unknown is never 'gone' — the ladder is kept"
        assert len(a.placed_triggers) == 4 and a.cancelled_triggers == []
    asyncio.run(body())


# ── reconciliation in a position ────────────────────────────────────────

def test_a_dropped_add_rung_is_forgotten_and_a_dropped_stop_is_re_armed():
    async def body():
        a = _adapter()
        c = _controller(a, revgrid_reconcile_every_ticks=1)
        await c.on_tick()
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))                  # BUY@101 fires -> long
        await c.on_tick()                                   # opens; stop armed; BUY@102 still armed
        stop = c._stop_digest
        add = next(r for r in c._rungs if not r.fired and r.side is TradeType.BUY)
        assert stop is not None
        a.drop_trigger(add.digest)                          # the add vanished (mid never reached 102)
        a.drop_trigger(stop)                                # the stop vanished too
        await c.on_tick()
        assert add not in c._rungs, "a vanished, uncrossed add is dropped — never trusted as resting"
        assert not any(r.side is TradeType.BUY and not r.fired for r in c._rungs)
        assert c._stop_digest is not None and c._stop_digest != stop, "protection re-armed at once"
        assert c._pos_base > 0 and a.venue_held[PAIR] > 0     # position untouched
    asyncio.run(body())


def test_a_crossed_add_rung_missing_from_the_list_is_a_fill_in_flight_not_a_drop():
    """The list is read BEFORE the position. A missing add whose level the mid
    has crossed is a fill landing, so it is left for the net read to attribute —
    the pyramid's average entry must not lose it."""
    async def body():
        a = _adapter()
        c = _controller(a, revgrid_reconcile_every_ticks=1)
        await c.on_tick()
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))                  # BUY@101 fires -> long
        await c.on_tick()
        add = next(r for r in c._rungs if not r.fired and r.side is TradeType.BUY)   # BUY@102
        a.set_mid(Decimal("102.5"))
        a.cross_triggers(Decimal("102.5"))                  # BUY@102 fires -> the venue list no longer has it
        await c.on_tick()                                   # net grew -> attributed as a fill
        assert add.fired is True and add in c._rungs
        assert abs(c._avg_entry - Decimal("101.5")) < Decimal("0.1"), "VWAP of both rungs"
    asyncio.run(body())


# ── unreadable venue position: hold, visibly ────────────────────────────

def test_an_unreadable_position_holds_and_surfaces_on_the_card():
    async def body():
        a = _adapter(venue_held=None)                       # held_base -> None
        c = _controller(a)
        await c.on_tick()
        assert a.placed_triggers == []
        assert c.gate_verdict == "PAUSE" and c.gate_reason == "venue_unreadable"
        assert c.gate_paused is True
        # the venue recovers: the hold clears and the ladder arms
        a.venue_held[PAIR] = Decimal(0)
        await c.on_tick()
        assert len(a.placed_triggers) == 4
        assert c.gate_verdict == "QUOTE" and c.gate_reason == ""
    asyncio.run(body())


def test_grid_metrics_carry_the_ladder_telemetry():
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()
        m = c.grid_metrics()
        assert m["grid_rungs_armed"] == 4 and m["grid_rungs_per_side"] == 2
        assert m["grid_step_bp"] == 100.0 and m["grid_trail_armed"] is False
        assert m["grid_stop_level"] == 0.0
    asyncio.run(body())


# ── audit follow-ups (2026-09-16) ────────────────────────────────────────

def test_a_partial_ioc_fill_is_attributed_at_its_real_size():
    """Rung 1 (BUY@101) fires but fills only 40% (thin book, IOC cancels the
    rest); rung 2 (BUY@102) fills in full. The average entry must weight each
    rung by what it actually filled — crediting rung 1 at full size understated
    the entry and loosened the stop by up to a step."""
    async def body():
        a = _adapter()
        c = _controller(a, revgrid_reconcile_every_ticks=99)
        await c.on_tick()
        r1 = next(o for o in a.placed_triggers if o.side is TradeType.BUY and o.price == Decimal("101"))
        r2 = next(o for o in a.placed_triggers if o.side is TradeType.BUY and o.price == Decimal("102"))
        # rung 1 fires with a 40% partial (the venue drops the remainder), rung 2 fills fully
        a.fire_trigger(r1.id, price=Decimal("101"))          # mock fills the whole rung...
        partial = a.venue_held[PAIR] * Decimal("0.4")
        a.venue_held[PAIR] = partial                        # ...so scale the venue to a 40% fill
        a.set_mid(Decimal("101.5"))
        await c.on_tick()
        assert c._pos_base == partial
        assert abs(c._avg_entry - Decimal("101")) < Decimal("0.01")
        a.set_mid(Decimal("102.5"))
        a.cross_triggers(Decimal("102.5"))                  # rung 2 fills in full
        await c.on_tick()
        full = c._pos_base - partial
        expected = (Decimal("101") * partial + Decimal("102") * full) / (partial + full)
        assert abs(c._avg_entry - expected) < Decimal("0.01"), (c._avg_entry, expected)
        assert expected > Decimal("101.5"), "the true entry is nearer 102 than a full-size credit says"
        # the protective stop sits at the designed distance from the TRUE entry
        assert abs(c._stop_level - expected * (Decimal(1) - c.stop_pct)) < Decimal("0.02")
    asyncio.run(body())


def test_on_stop_tears_the_ladder_down_but_keeps_the_protective_stop():
    """A stop runs before the session's flatten; if that flatten fails the open
    position must keep its venue stop. The session-end sweep clears it once flat."""
    async def body():
        a = _adapter()
        c = _controller(a)
        await c.on_tick()
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))
        await c.on_tick()                                   # long; stop armed; BUY@102 armed
        stop = c._stop_digest
        await c.on_stop("user stopped")
        assert stop not in a.cancelled_triggers, "the protective stop stays armed"
        assert all(o.id in a.cancelled_triggers for o in a.placed_triggers
                   if o.side is TradeType.BUY and o.price == Decimal("102")), "the add rung is cancelled"
        assert c._rungs == []
        assert c.grid_metrics()["grid_stop_digest"] == stop
        assert stop in c.grid_metrics()["grid_trigger_digests"]
    asyncio.run(body())


def test_a_superseded_stop_whose_cancel_failed_is_retried_until_gone():
    async def body():
        a = _adapter()
        c = _controller(a, levels=1, stop_min_reprice_frac=Decimal(0))
        await c.on_tick()
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))
        await c.on_tick()                                   # stop #1
        first = c._stop_digest
        # the re-arm's cancel of the old stop fails, and so does the same tick's retry
        a.fail_on = {"cancel_trigger_order"}; a.fail_remaining = 2
        a.set_mid(Decimal("104"))                           # trail arms -> stop re-armed (place, cancel fails)
        await c.on_tick()
        assert c._stop_digest != first and first in c._stale_stops
        assert first not in a.cancelled_triggers
        a.fail_on = set()
        a.set_mid(Decimal("104.05"))
        await c.on_tick()                                   # retried and cancelled
        assert first in a.cancelled_triggers and c._stale_stops == []
    asyncio.run(body())


def test_a_wrong_pending_list_never_stacks_a_second_ladder():
    """Defence in depth: if the venue list were wrong (a digest shape change), the
    rungs it calls 'gone' are still CANCELLED by digest before a fresh ladder is
    armed, so the venue never holds two live ladders."""
    async def body():
        a = _adapter()
        c = _controller(a, revgrid_reconcile_every_ticks=1)
        await c.on_tick()                                   # 4 live triggers
        real = a.list_trigger_orders

        async def _lying(_pair):                            # reports every rung gone
            return []
        a.list_trigger_orders = _lying
        await c.on_tick()
        a.list_trigger_orders = real
        live = [tid for tid, t in a._triggers.items() if not t["order"].state.is_terminal]
        assert len(live) == 4, f"exactly one live ladder, got {len(live)}"
        assert len(a.cancelled_triggers) == 4, "the 'gone' rungs were cancelled by digest"
    asyncio.run(body())
