"""The multi-product open-orders read is charged what the venue charges:
``2 * len(product_ids)`` (docs: rate-limits "Orders: IP weight = 2 *
product_ids.length"; MEASURED 2026-09-16 from an independent IP — two
82-product reads pass, the third is rejected; 25 eight-product reads all pass).

The flat per-call charge that #282 introduced under-counted ~40x: the bot
believed it was under budget while the venue rejected every read, the
portfolio deck went "Cached · venue throttled" and the fail-loud cancel sweep
could never read the book (18 quotes + a short left resting). The right fix
for the per-user burst clamp #282 was chasing is a SMALL N: the regular poll
scopes the parent read, and an isolated child reads exactly its own product.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from src.nadobro.venue.nado_client import NadoClient

PARENT = "0x" + "11" * 32
CHILD_A = "0x" + "22" * 32
CHILD_B = "0x" + "33" * 32


def _client():
    c = NadoClient(private_key="0xabc", network="mainnet")
    c.subaccount_hex = PARENT
    return c


def _engine(calls: list):
    def _read(pids, sender):
        calls.append((sender, list(pids)))
        return SimpleNamespace(product_orders=[])
    return SimpleNamespace(context=SimpleNamespace(engine_client=SimpleNamespace(
        get_subaccount_multi_products_open_orders=_read)))


def _spy(seen: list):
    def _allowed(weight=1.0, **k):
        seen.append(weight)
        return True
    return _allowed


def test_batched_read_charges_two_per_product():
    c = _client()
    calls: list = []
    c.client = _engine(calls)
    seen: list = []
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", side_effect=_spy(seen)), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None):
        c._open_orders_for_sender_batched(PARENT, list(range(1, 85)))   # 84 products
        c._open_orders_for_sender_batched(PARENT, [2])                  # 1 product
    assert seen == [168, 2], seen


def test_scoped_parent_read_queries_only_the_scope():
    pytest.importorskip("nado_protocol")
    c = _client()
    calls: list = []
    c.client = _engine(calls)
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_open_order_product_ids", side_effect=AssertionError("catalog must not be consulted for a scoped read")), \
         mock.patch.object(NadoClient, "_isolated_subaccounts", return_value=[]):
        assert c.get_all_open_orders(True, product_ids=[5, 2, 5]) == []
    assert calls == [(PARENT, [2, 5])]


def test_isolated_children_read_exactly_their_own_product():
    """1 parent (scoped to 2 products) + 2 children = 4 + 2 + 2 weight, not 3 x 192."""
    pytest.importorskip("nado_protocol")
    c = _client()
    calls: list = []
    c.client = _engine(calls)
    seen: list = []
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", side_effect=_spy(seen)), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_isolated_subaccounts", return_value=[(CHILD_A, 7), (CHILD_B, 9)]):
        assert c.get_all_open_orders(True, product_ids=[1, 2]) == []
    assert calls == [(PARENT, [1, 2]), (CHILD_A, [7]), (CHILD_B, [9])]
    assert seen == [4, 2, 2]


def test_a_child_without_a_known_product_falls_back_to_the_parent_scope():
    pytest.importorskip("nado_protocol")
    c = _client()
    calls: list = []
    c.client = _engine(calls)
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_isolated_subaccounts", return_value=[(CHILD_A, None)]):
        c.get_all_open_orders(True, product_ids=[3])
    assert calls == [(PARENT, [3]), (CHILD_A, [3])]


def test_an_empty_scope_skips_the_parent_read_but_still_reads_children():
    pytest.importorskip("nado_protocol")
    c = _client()
    calls: list = []
    c.client = _engine(calls)
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_isolated_subaccounts", return_value=[(CHILD_A, 7)]):
        assert c.get_all_open_orders(True, product_ids=[]) == []
    assert calls == [(CHILD_A, [7])]


def test_unscoped_read_still_sweeps_the_whole_catalog():
    pytest.importorskip("nado_protocol")
    c = _client()
    calls: list = []
    c.client = _engine(calls)
    with mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch.object(NadoClient, "_gateway_release", return_value=None), \
         mock.patch.object(NadoClient, "_open_order_product_ids", return_value=[1, 2, 3]), \
         mock.patch.object(NadoClient, "_isolated_subaccounts", return_value=[]):
        c.get_all_open_orders(True)
    assert calls == [(PARENT, [1, 2, 3])]


def test_no_flat_read_weight_knob_remains():
    from src.nadobro.venue import nado_client as nc
    assert not hasattr(nc, "_OPEN_ORDERS_READ_WEIGHT")
