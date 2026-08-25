"""Integration guardrail for NadoClient.place_reduce_only_stop (VENUE-STOP).

Drives the REAL ``_prepare_place_order_params`` (not a mock) so a wrong method
name — like the ``_build_place_order_params`` typo that shipped the venue stop
100% dead while it was enabled in prod — fails here with an AttributeError
instead of being swallowed by the two ``except`` wrappers around the live call.
Also pins the per-side trigger + reduce-only contract.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.nadobro.venue import nado_client as nc_mod
from src.nadobro.venue.nado_client import NadoClient


def _client(monkeypatch, capture: dict):
    client = NadoClient(private_key="0xabc", network="mainnet")
    client._initialized = True
    # The dummy key can't derive an address; set a valid bytes32 sender so the
    # real _prepare_place_order_params can build the OrderParams.
    client.subaccount_hex = "0x" + "12" * 32

    class _TriggerClient:
        def place_price_trigger_order(self, **kw):
            capture.update(kw)
            return SimpleNamespace(digest="0xstopdigest")

    client.client = SimpleNamespace(context=SimpleNamespace(trigger_client=_TriggerClient()))
    monkeypatch.setattr(client, "_ensure_sdk_client", lambda: True)
    monkeypatch.setattr(client, "_gateway_allowed", lambda **kw: True)
    monkeypatch.setattr(client, "_warm_product_increment_cache", lambda pid: None)

    async def _direct_exec(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(nc_mod, "run_blocking_exec", _direct_exec)
    # Builder routing is validated inside _prepare_place_order_params.
    monkeypatch.setenv("NADO_BUILDER_ID", "123")
    monkeypatch.setenv("NADO_BUILDER_FEE_RATE", "10")
    return client


def test_long_stop_reaches_trigger_client_reduce_only_below(monkeypatch):
    cap: dict = {}
    client = _client(monkeypatch, cap)
    res = asyncio.run(client.place_reduce_only_stop(
        product_id=2, close_size=1.0, stop_price=99.0, position_is_long=True,
    ))
    assert res.get("success") is True, res           # would be False if the method call AttributeError'd
    assert cap.get("reduce_only") is True
    assert cap.get("trigger_type") == "mid_price_below"   # long stop fires on price falling
    assert cap.get("product_id") == 2
    assert int(cap.get("amount_x18")) < 0                 # SELL to close a long


def test_short_stop_reaches_trigger_client_reduce_only_above(monkeypatch):
    cap: dict = {}
    client = _client(monkeypatch, cap)
    res = asyncio.run(client.place_reduce_only_stop(
        product_id=2, close_size=1.0, stop_price=101.0, position_is_long=False,
    ))
    assert res.get("success") is True, res
    assert cap.get("reduce_only") is True
    assert cap.get("trigger_type") == "mid_price_above"   # short stop fires on price rising
    assert int(cap.get("amount_x18")) > 0                 # BUY to close a short


def test_uses_the_real_order_prep_method_not_a_typo():
    # The exact regression: place_reduce_only_stop must call a method that EXISTS.
    assert hasattr(NadoClient, "_prepare_place_order_params")
    assert not hasattr(NadoClient, "_build_place_order_params")
    import inspect
    src = inspect.getsource(NadoClient.place_reduce_only_stop)
    assert "self._prepare_place_order_params(" in src
