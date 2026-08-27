"""Unit tests for the engine adapter's PRICE-TRIGGER surface (Reverse Grid rungs).

Two layers are covered:
  * the live ``NadoAdapter.place_trigger_order`` / ``cancel_trigger_order`` —
    that they route to the venue's TRIGGER methods (``place_entry_trigger_order``
    / ``cancel_trigger_orders``), map the engine ``TradeType`` to the client's
    ``direction_is_buy`` flag, surface the trigger digest as the order id, track
    it in the SEPARATE trigger registry, and never touch the resting-order cancel
    path;
  * the ``MockNadoAdapter`` double — that a placed trigger rests until the mid
    crosses its level, fires with the correct per-side semantics, arms a
    dependent (pyramiding) rung only after its parent fires, and cancels cleanly.

No live venue is used; the live-adapter tests drive a fake NadoClient whose
trigger methods are ``async`` (mirroring the real client, which offloads the
blocking SDK call to the execution pool itself).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.nadobro.engine.adapter.base import OrderState
from src.nadobro.engine.adapter.nado import AdapterError, NadoAdapter, ProductMeta
from src.nadobro.engine.types import OrderType, TradeType

from tests.engine._mock_nado import MockNadoAdapter

PAIR = "BTC-PERP"
META = {PAIR: ProductMeta(product_id=2, tick_size=Decimal("0.5"),
                          lot_size=Decimal("0.001"), min_notional=Decimal(1),
                          is_perp=True, isolated_only=False)}


class _FakeTriggerClient:
    """A NadoClient stub exposing the async trigger surface the adapter awaits."""

    def __init__(self, place_result=None):
        self.place_calls = []
        self.cancel_calls = []
        self.regular_cancels = []      # cancel_orders — MUST stay empty for triggers
        self._place_result = place_result or {"success": True, "response": {"digest": "0xrung"}}

    async def place_entry_trigger_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return self._place_result

    async def cancel_trigger_orders(self, *, product_id, digests):
        self.cancel_calls.append({"product_id": product_id, "digests": list(digests)})
        return {"success": True, "cancelled": len(digests)}

    async def cancel_orders(self, *, product_id, digests):
        # The trigger path must NEVER land here (it hits the wrong venue service).
        self.regular_cancels.append({"product_id": product_id, "digests": list(digests)})
        return {"success": True}


def _adapter(place_result=None):
    client = _FakeTriggerClient(place_result)
    return NadoAdapter(client, META), client


# ── live adapter: placement ────────────────────────────────────────────

def test_buy_rung_maps_to_direction_is_buy_and_returns_the_digest():
    async def body():
        a, c = _adapter({"success": True, "response": {"digest": "0xbuy"}})
        o = await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"),
                                        Decimal("79000"), slippage_pct=0.05)
        assert o.id == "0xbuy"
        assert o.state is OrderState.OPEN
        assert o.price == Decimal("79000")
        assert len(c.place_calls) == 1
        call = c.place_calls[0]
        assert call["direction_is_buy"] is True
        assert call["product_id"] == 2
        assert call["size"] == 1.0
        assert call["trigger_price"] == 79000.0
        assert abs(call["slippage_pct"] - 0.05) < 1e-9
        assert call["isolated"] is False
        assert call["dependency"] is None
        # tracked in the SEPARATE trigger registry, not the resting-order one
        assert "0xbuy" in a._trigger_orders and "0xbuy" not in a._orders

    asyncio.run(body())


def test_sell_rung_maps_to_direction_is_buy_false():
    async def body():
        a, c = _adapter({"success": True, "response": {"digest": "0xsell"}})
        o = await a.place_trigger_order(PAIR, TradeType.SELL, Decimal("2"),
                                        Decimal("77000"))
        assert o.id == "0xsell" and o.side is TradeType.SELL
        assert c.place_calls[0]["direction_is_buy"] is False
        assert c.place_calls[0]["size"] == 2.0

    asyncio.run(body())


def test_amount_is_taken_as_a_magnitude_even_if_negative():
    """The engine passes a POSITIVE base size and a side; the sign lives in the
    side. A stray negative must not flip the size sent to the venue."""
    async def body():
        a, c = _adapter()
        await a.place_trigger_order(PAIR, TradeType.SELL, Decimal("-1.5"),
                                    Decimal("77000"))
        assert c.place_calls[0]["size"] == 1.5

    asyncio.run(body())


def test_dependency_is_passed_through_for_pyramiding():
    async def body():
        a, c = _adapter()
        await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"),
                                    Decimal("79500"), dependency="0xparent")
        assert c.place_calls[0]["dependency"] == "0xparent"

    asyncio.run(body())


def test_digest_extracted_from_a_nested_response_data_shape():
    """The trigger service sometimes nests the digest under response.data."""
    async def body():
        a, _ = _adapter({"success": True, "response": {"data": {"digest": "0xdeep"}}})
        o = await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("79000"))
        assert o.id == "0xdeep"

    asyncio.run(body())


def test_venue_success_false_raises_adapter_error():
    async def body():
        a, _ = _adapter({"success": False, "error": "rate limited"})
        with pytest.raises(AdapterError):
            await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("79000"))

    asyncio.run(body())


def test_missing_digest_raises_rather_than_leaking_an_untracked_rung():
    async def body():
        a, _ = _adapter({"success": True, "response": {}})
        with pytest.raises(AdapterError):
            await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("79000"))

    asyncio.run(body())


def test_invalid_params_raise_before_any_venue_call():
    async def body():
        a, c = _adapter()
        for amt, px in ((Decimal("0"), Decimal("79000")), (Decimal("1"), Decimal("0"))):
            with pytest.raises(AdapterError):
                await a.place_trigger_order(PAIR, TradeType.BUY, amt, px)
        assert c.place_calls == []

    asyncio.run(body())


# ── live adapter: cancellation ─────────────────────────────────────────

def test_cancel_targets_the_trigger_service_not_the_regular_book():
    async def body():
        a, c = _adapter({"success": True, "response": {"digest": "0xbuy"}})
        await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("79000"))
        assert await a.cancel_trigger_order("0xbuy") is True
        assert c.cancel_calls == [{"product_id": 2, "digests": ["0xbuy"]}]
        assert c.regular_cancels == [], "a trigger must not be cancelled via cancel_orders"
        assert "0xbuy" not in a._trigger_orders     # forgotten after cancel

    asyncio.run(body())


def test_placement_links_the_trigger_digest_to_the_session():
    """The trigger digest MUST be linked via on_place so the venue fill is attributed
    to the run (turnover / realized PnL) — a Nado price trigger IS the order, so its
    fill carries this digest."""
    async def body():
        linked = []
        c = _FakeTriggerClient({"success": True, "response": {"digest": "0xbuy"}})
        a = NadoAdapter(c, META, on_place=lambda d: linked.append(d))
        await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("79000"))
        assert linked == ["0xbuy"]

    asyncio.run(body())


def test_stop_placement_links_the_stop_digest_to_the_session():
    async def body():
        linked = []
        c = _FakeTriggerClient()
        # place_stop_order goes through place_reduce_only_stop on the real client;
        # give the fake that method returning a stop digest.
        async def _stop(**kw):
            return {"success": True, "response": {"digest": "0xstop"}}
        c.place_reduce_only_stop = _stop
        a = NadoAdapter(c, META, on_place=lambda d: linked.append(d))
        await a.place_stop_order(PAIR, Decimal("1"), Decimal("77000"), position_is_long=True)
        assert linked == ["0xstop"]

    asyncio.run(body())


def test_cancel_unknown_trigger_is_idempotent_and_makes_no_venue_call():
    async def body():
        a, c = _adapter()
        assert await a.cancel_trigger_order("nope") is False
        assert await a.cancel_trigger_order("") is False
        assert c.cancel_calls == []

    asyncio.run(body())


def test_cancel_surfaces_a_venue_rejection():
    class _RejectingClient(_FakeTriggerClient):
        async def cancel_trigger_orders(self, *, product_id, digests):
            return {"success": False, "error": "no such trigger"}

    async def body():
        c = _RejectingClient({"success": True, "response": {"digest": "0xbuy"}})
        a = NadoAdapter(c, META)
        await a.place_trigger_order(PAIR, TradeType.BUY, Decimal("1"), Decimal("79000"))
        with pytest.raises(AdapterError):
            await a.cancel_trigger_order("0xbuy")

    asyncio.run(body())


# ── MockNadoAdapter double: the venue "watches the mid" ────────────────

def test_mock_trigger_rests_until_the_mid_crosses():
    async def body():
        a = MockNadoAdapter(mid=Decimal("100"))
        buy = await a.place_trigger_order("P", TradeType.BUY, Decimal("1"), Decimal("110"))
        # Placement does not fill — a trigger only fires on a cross.
        assert buy.state is OrderState.OPEN
        assert a.cross_triggers(Decimal("105")) == []          # below the buy level
        fills = a.cross_triggers(Decimal("111"))               # crosses 110 upward
        assert len(fills) == 1
        assert fills[0].side is TradeType.BUY
        assert fills[0].price == Decimal("111")

    asyncio.run(body())


def test_mock_sell_rung_fires_on_a_fall():
    async def body():
        a = MockNadoAdapter(mid=Decimal("100"))
        await a.place_trigger_order("P", TradeType.SELL, Decimal("1"), Decimal("90"))
        assert a.cross_triggers(Decimal("95")) == []           # above the sell level
        fills = a.cross_triggers(Decimal("89"))                # crosses 90 downward
        assert len(fills) == 1 and fills[0].side is TradeType.SELL

    asyncio.run(body())


def test_mock_opposite_rung_can_be_cancelled_before_it_fires():
    """The Reverse Grid arms both sides while flat and cancels the loser on the
    first fill — exercise that a not-yet-fired trigger cancels and then never
    fires."""
    async def body():
        a = MockNadoAdapter(mid=Decimal("100"))
        sell = await a.place_trigger_order("P", TradeType.SELL, Decimal("1"), Decimal("90"))
        assert await a.cancel_trigger_order(sell.id) is True
        assert sell.id in a.cancelled_triggers
        # cancelled → never fires even when the mid later crosses its level
        assert a.cross_triggers(Decimal("80")) == []
        # idempotent second cancel
        assert await a.cancel_trigger_order(sell.id) is False

    asyncio.run(body())


def test_mock_dependent_rung_is_armed_only_after_its_parent_fires():
    async def body():
        a = MockNadoAdapter(mid=Decimal("100"))
        parent = await a.place_trigger_order("P", TradeType.BUY, Decimal("1"), Decimal("110"))
        await a.place_trigger_order("P", TradeType.BUY, Decimal("1"), Decimal("120"),
                                    dependency=parent.id)
        # A jump straight past BOTH levels fires only the armed parent this tick;
        # the dependent is armed by that fire and becomes eligible next tick.
        first = a.cross_triggers(Decimal("125"))
        assert len(first) == 1 and first[0].order_id == parent.id
        second = a.cross_triggers(Decimal("125"))
        assert len(second) == 1                    # the now-armed dependent fires

    asyncio.run(body())


def test_mock_fill_stream_yields_triggered_fills():
    async def body():
        a = MockNadoAdapter(mid=Decimal("100"))
        await a.place_trigger_order("P", TradeType.BUY, Decimal("1"), Decimal("110"))
        a.cross_triggers(Decimal("112"))
        streamed = [f async for f in a.fill_stream("P")]
        assert len(streamed) == 1 and streamed[0].side is TradeType.BUY

    asyncio.run(body())
