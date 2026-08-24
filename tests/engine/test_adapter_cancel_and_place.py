"""NadoAdapter.cancel_and_place — the venue-side of the atomic requote.

The adapter is the single choke point for engine orders, so it owns the tag +
registry + hook bookkeeping. The properties that must hold:

* the NEW order is tagged, registered, and linked (on_place) exactly like a
  normal placement;
* the OLD order's local bookkeeping is left ALONE until forget_cancelled — the
  caller settles the replaced executor (capturing a racing fill) while the ref
  still resolves;
* a venue rejection raises AdapterError WITHOUT leaking the new tag, so the
  caller can fall back with no double order.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.nadobro.engine import order_tags
from src.nadobro.engine.adapter.nado import AdapterError, NadoAdapter, ProductMeta
from src.nadobro.engine.types import OrderType, TradeType

PAIR = "BTC-PERP"
META = {PAIR: ProductMeta(product_id=3, tick_size=Decimal("0.1"),
                          lot_size=Decimal("0.001"), min_notional=Decimal(1),
                          is_perp=True)}


class _FakeClient:
    def __init__(self, *, resp=None, raises=None):
        self._resp = resp if resp is not None else {"digest": "newdigest", "status": "open"}
        self._raises = raises
        self.calls = []

    def cancel_and_place(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._resp


@pytest.fixture(autouse=True)
def _clean_tags():
    order_tags.clear()
    yield
    order_tags.clear()


def _adapter(client):
    return NadoAdapter(client, META)


def _seed_old(adapter, digest="olddigest"):
    """Pretend an order is already resting so forget_cancelled has something."""
    from src.nadobro.engine.adapter.nado import _OrderRef
    ref = _OrderRef(PAIR, 3, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("0.01"), Decimal("100"))
    adapter._orders[digest] = ref
    adapter._registry.record(digest, ref)
    return digest


# --- success ---------------------------------------------------------------

def test_returns_the_new_order_and_tags_it():
    async def body():
        client = _FakeClient()
        a = _adapter(client)
        old = _seed_old(a)
        order = await a.cancel_and_place(
            old, PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("0.01"), Decimal("100"),
        )
        assert order.id == "newdigest"
        assert order.trading_pair == PAIR
        # New order is registered and its digest resolves to a tag.
        assert a._orders.get("newdigest") is not None
        assert order_tags.resolve_digest("newdigest") is not None
        # Exactly one atomic venue call, carrying the old digest as the cancel.
        assert len(client.calls) == 1
        assert client.calls[0]["cancel_digests"] == [old]
        assert client.calls[0]["post_only"] is True
    asyncio.run(body())


def test_the_old_bookkeeping_survives_until_forget_cancelled():
    # The caller must be able to settle the replaced executor (order_status on
    # the old digest) BEFORE the ref is dropped.
    async def body():
        a = _adapter(_FakeClient())
        old = _seed_old(a)
        await a.cancel_and_place(
            old, PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("0.01"), Decimal("100"),
        )
        assert a._orders.get(old) is not None      # still resolvable
        a.forget_cancelled(old)
        assert a._orders.get(old) is None          # now dropped
    asyncio.run(body())


def test_on_place_hook_links_the_new_digest_only():
    async def body():
        linked = []
        client = _FakeClient()
        a = NadoAdapter(client, META, on_place=lambda d: linked.append(d))
        old = _seed_old(a)
        await a.cancel_and_place(
            old, PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("0.01"), Decimal("100"),
        )
        assert linked == ["newdigest"]
    asyncio.run(body())


# --- failure ---------------------------------------------------------------

def test_a_venue_reject_raises_and_leaks_no_tag():
    async def body():
        before = order_tags.stats()["tags"]
        # The real nado_client.cancel_and_place returns a success-keyed dict.
        client = _FakeClient(resp={"success": False, "error": "nope"})
        a = _adapter(client)
        old = _seed_old(a)
        with pytest.raises(AdapterError):
            await a.cancel_and_place(
                old, PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("0.01"), Decimal("100"),
            )
        # Old order untouched (atomic failure) and no new tag leaked.
        assert a._orders.get(old) is not None
        assert order_tags.stats()["tags"] == before
    asyncio.run(body())


def test_a_raising_client_raises_adaptererror_and_leaks_no_tag():
    async def body():
        before = order_tags.stats()["tags"]
        a = _adapter(_FakeClient(raises=RuntimeError("boom")))
        old = _seed_old(a)
        with pytest.raises(AdapterError):
            await a.cancel_and_place(
                old, PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("0.01"), Decimal("100"),
            )
        assert order_tags.stats()["tags"] == before
    asyncio.run(body())


def test_a_missing_cancel_id_is_rejected():
    async def body():
        a = _adapter(_FakeClient())
        with pytest.raises(AdapterError):
            await a.cancel_and_place(
                "", PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal("0.01"), Decimal("100"),
            )
    asyncio.run(body())


def test_forget_cancelled_is_a_safe_noop_for_unknown_ids():
    a = _adapter(_FakeClient())
    a.forget_cancelled("never-seen")      # must not raise
    a.forget_cancelled("")


# --- settle fallback against a non-atomic venue (audit finding 2) -----------

def test_settle_cancels_the_old_order_if_the_venue_left_it_resting():
    """The no-orphan guarantee assumes cancel_and_place cancelled the old order.
    If a refresh shows it STILL RESTING, settle must issue an explicit cancel
    rather than terminate and forget a live resting order."""
    import copy

    from src.nadobro.engine.adapter.base import NadoOrder, OrderState
    from src.nadobro.engine.executors.order_executor import (
        OrderExecutor, OrderExecutorConfig,
    )
    from src.nadobro.engine.inventory import InventoryRepository
    from src.nadobro.engine.types import ExecutionStrategy
    from tests.engine._mock_nado import MockNadoAdapter

    async def body():
        # order_status reports the old order STILL OPEN (the venue kept it alive
        # after a non-atomic cancel_and_place); cancel_order then flips it to
        # CANCELLED, as a real cancel would.
        adapter = MockNadoAdapter(mid=Decimal(100), auto_fill_market=False)
        # A resting order the executor "owns".
        resting = NadoOrder(
            id="old1", trading_pair="P", side=TradeType.BUY,
            order_type=OrderType.LIMIT_MAKER, amount_base=Decimal("1"),
            price=Decimal("99"), state=OrderState.OPEN,
        )
        adapter._orders["old1"] = resting
        ex = OrderExecutor(
            OrderExecutorConfig("P", TradeType.BUY, Decimal("1"),
                                ExecutionStrategy.LIMIT_MAKER, price=Decimal("99")),
            user_id=1, controller_id="c", adapter=adapter,
            inventory=InventoryRepository(),
        )
        ex.adopt_order(copy.copy(resting))     # executor now tracks old1, OPEN
        await ex.settle_after_external_cancel()
        # The still-resting order was rescued by an explicit cancel.
        assert "old1" in adapter.cancelled
        assert adapter._orders["old1"].state is OrderState.CANCELLED
        assert ex.is_terminated

    asyncio.run(body())
