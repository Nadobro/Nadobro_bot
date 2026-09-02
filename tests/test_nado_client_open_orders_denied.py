"""NadoClient.get_open_orders DENIED-vs-EMPTY contract (2026-09-02).

``[]`` means a SUCCESSFUL read of an empty book. A read the venue budget denied
(or that failed) must return ``None`` to a ``refresh=True`` caller — the engine's
authoritative "is my quote still resting?" poll — and must NEVER be written into
the process cache as an empty book.

Both halves of the phantom-cancel storm are pinned here:
  * denial used to return ``[]`` (or a frozen stale list missing every quote
    placed since) -> every resting quote read as "gone";
  * a failed read used to CACHE ``{"data": []}`` in a never-evicted dict, so one
    transient venue error poisoned every later budget-denied read.
"""
from __future__ import annotations

from unittest import mock

from src.nadobro.venue import gateway_budget
from src.nadobro.venue import nado_client as nc
from src.nadobro.venue.nado_client import NadoClient

PID = 2


def _client() -> NadoClient:
    # Uninitialized (no SDK) -> takes the REST path, whose own budget check is
    # what we deny below. Same construction the reliability tests use.
    return NadoClient(private_key="0xabc", network="mainnet")


def _cache_key(c: NadoClient):
    return (c.network, str(c.subaccount_hex or ""), PID)


def test_refresh_read_denied_returns_none_and_never_poisons_cache():
    c = _client()
    key = _cache_key(c)
    nc._open_orders_cache.pop(key, None)
    with mock.patch.object(gateway_budget, "is_gateway_blocked", return_value=True):
        assert c.get_open_orders(PID, refresh=True) is None
    # The failed read must not have been recorded as a real empty book.
    assert key not in nc._open_orders_cache


def test_best_effort_read_keeps_legacy_empty_list_without_poisoning():
    c = _client()
    key = _cache_key(c)
    nc._open_orders_cache.pop(key, None)
    with mock.patch.object(gateway_budget, "is_gateway_blocked", return_value=True):
        assert c.get_open_orders(PID) == []          # refresh=False: unchanged contract
    assert key not in nc._open_orders_cache          # ...but still no poison write
