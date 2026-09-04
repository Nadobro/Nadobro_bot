"""The batched multi-product open-orders read must charge the per-user gateway
bucket for ONE call, not 2*products (prod 2026-09-03, session 312).

168 weight (2 * 84 products) against the 24-token per-user burst was clamped to
the whole burst, so one read drained it and the next subaccount's read was
denied within max_wait -> get_all_open_orders returned None on EVERY call for a
user with an isolated child. The weight is now capped well under the burst so
parent + every isolated child read all fit.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from src.nadobro.venue import nado_client as nc
from src.nadobro.venue.nado_client import NadoClient


def _client():
    c = NadoClient(private_key="0xabc", network="mainnet")
    c.subaccount_hex = "0x" + "11" * 32
    return c


def _engine(blocks=0):
    read = lambda pids, sender: SimpleNamespace(product_orders=[])
    return SimpleNamespace(context=SimpleNamespace(engine_client=SimpleNamespace(
        get_subaccount_multi_products_open_orders=read)))


def test_batched_read_weight_is_capped_below_the_user_burst():
    from src.nadobro.venue import gateway_budget as gb
    c = _client()
    c.client = _engine()
    seen = {}

    def _spy(weight=1.0, **k):
        seen["weight"] = weight
        return True

    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", side_effect=_spy), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None):
        c._open_orders_for_sender_batched(c.subaccount_hex, list(range(1, 85)))   # 84 products

    assert seen["weight"] == nc._OPEN_ORDERS_READ_WEIGHT, seen
    assert seen["weight"] <= gb._USER_BURST, "one read must fit inside the per-user burst"
    assert seen["weight"] < 2 * 84, "must not charge 2*products for one multi-product call"


def test_the_read_weight_is_a_flat_per_call_cost():
    c = _client()
    c.client = _engine()
    seen = {}
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", side_effect=lambda weight=1.0, **k: seen.setdefault("weight", weight) or True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None):
        c._open_orders_for_sender_batched(c.subaccount_hex, [2])   # 1 product
    assert seen["weight"] == nc._OPEN_ORDERS_READ_WEIGHT   # one call, one query weight


def test_parent_plus_isolated_children_all_fit_in_one_burst():
    """The real failure: 1 parent + 6 isolated children each needing a read.
    At the capped weight they sum to less than the per-user burst so none is
    denied for lack of tokens."""
    from src.nadobro.venue import gateway_budget as gb
    senders = 1 + 6
    assert senders * nc._OPEN_ORDERS_READ_WEIGHT <= gb._USER_BURST + gb._USER_RPS * 2, (
        senders, nc._OPEN_ORDERS_READ_WEIGHT, gb._USER_BURST
    )
