"""NadoClient.get_all_open_orders DENIED-vs-EMPTY contract (2026-09-02).

The portfolio sync's stale-order sweep marks every ``open_orders`` row the venue
list omits as ``cancelled_or_filled``. The batched read used to return ``[]``
for a budget-denied / SDK-unavailable / failed round, which the sweep read as
"the venue holds NO orders" — wiping a live ladder's rows on every throttled
poll (the stale-order-sweep half of the phantom-cancel storm, audit
AUDIT-DENY-2026-09-02). ``[]`` now means a SUCCESSFUL empty read only; a round
the process could not read is ``None`` and the sweep skips it.
"""
from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import pytest

from src.nadobro.venue.nado_client import NadoClient

PARENT = "0x" + "11" * 32
CHILD = "0x" + "22" * 32


def _client() -> NadoClient:
    c = NadoClient(private_key="0xabc", network="mainnet")
    c.subaccount_hex = PARENT
    return c


def _engine(product_orders=None, *, raises: Exception | None = None):
    def _read(pids, sender):
        if raises is not None:
            raise raises
        return SimpleNamespace(product_orders=list(product_orders or []))
    return SimpleNamespace(context=SimpleNamespace(engine_client=SimpleNamespace(
        get_subaccount_multi_products_open_orders=_read)))


def _stack(*, sdk: bool = True, allowed=True, isolated=()):
    st = ExitStack()
    st.enter_context(mock.patch.object(NadoClient, "_ensure_sdk_client", return_value=sdk))
    st.enter_context(mock.patch.object(NadoClient, "_gateway_allowed", side_effect=allowed)
                     if isinstance(allowed, list) else
                     mock.patch.object(NadoClient, "_gateway_allowed", return_value=allowed))
    st.enter_context(mock.patch.object(NadoClient, "_gateway_release", return_value=None))
    st.enter_context(mock.patch.object(NadoClient, "_open_order_product_ids", return_value=[1, 2]))
    st.enter_context(mock.patch.object(NadoClient, "_isolated_subaccount_hexes", return_value=list(isolated)))
    return st


def test_sdk_unavailable_is_unknown_not_empty():
    c = _client()
    with _stack(sdk=False):
        assert c.get_all_open_orders(True) is None


def test_budget_denied_is_unknown_not_empty():
    c = _client()
    c.client = _engine([])
    with _stack(allowed=False):
        assert c.get_all_open_orders(True) is None


def test_venue_failure_is_unknown_not_empty():
    c = _client()
    c.client = _engine(raises=RuntimeError("502 bad gateway"))
    with _stack(allowed=True):
        assert c.get_all_open_orders(True) is None


def test_a_successful_empty_read_is_still_an_empty_list():
    pytest.importorskip("nado_protocol")          # the parser imports from_x18
    c = _client()
    c.client = _engine([])                         # venue answered: no orders
    with _stack(allowed=True):
        assert c.get_all_open_orders(True) == []


def test_an_unknown_isolated_child_makes_the_whole_list_unknown():
    """The sweep is per user, not per subaccount: a list missing one child's
    orders would mark THAT child's live orders cancelled_or_filled."""
    pytest.importorskip("nado_protocol")
    c = _client()
    c.client = _engine([])
    with _stack(allowed=[True, False], isolated=[CHILD]):   # parent ok, child denied
        assert c.get_all_open_orders(True, include_isolated=True) is None


def test_strict_callers_still_get_the_exception():
    c = _client()
    with _stack(sdk=False), pytest.raises(RuntimeError):
        c.get_all_open_orders(True, strict=True)
