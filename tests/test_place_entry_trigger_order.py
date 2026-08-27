"""Integration guardrail for NadoClient.place_entry_trigger_order (REVERSE-GRID rung).

Drives the REAL ``_prepare_place_order_params`` (not a mock) so a wrong method name
fails here with an AttributeError instead of being swallowed by the ``except``
wrappers. Pins the per-side trigger contract that a Reverse Grid depends on:
a BUY rung fires on a RISE (mid_price_above, positive amount, NOT reduce-only), a
SELL rung fires on a FALL (mid_price_below, negative amount, NOT reduce-only).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.nadobro.venue import nado_client as nc_mod
from src.nadobro.venue.nado_client import NadoClient


def _client(monkeypatch, capture: dict):
    client = NadoClient(private_key="0xabc", network="mainnet")
    client._initialized = True
    client.subaccount_hex = "0x" + "12" * 32

    class _TriggerClient:
        def place_price_trigger_order(self, **kw):
            capture.update(kw)
            return SimpleNamespace(digest="0xrungdigest")

    client.client = SimpleNamespace(context=SimpleNamespace(trigger_client=_TriggerClient()))
    monkeypatch.setattr(client, "_ensure_sdk_client", lambda: True)
    monkeypatch.setattr(client, "_gateway_allowed", lambda **kw: True)
    monkeypatch.setattr(client, "_warm_product_increment_cache", lambda pid: None)

    async def _direct_exec(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(nc_mod, "run_blocking_exec", _direct_exec)
    monkeypatch.setenv("NADO_BUILDER_ID", "123")
    monkeypatch.setenv("NADO_BUILDER_FEE_RATE", "10")
    return client


def test_buy_rung_fires_on_a_rise_not_reduce_only(monkeypatch):
    cap: dict = {}
    client = _client(monkeypatch, cap)
    res = asyncio.run(client.place_entry_trigger_order(
        product_id=2, size=1.0, trigger_price=79000.0, direction_is_buy=True,
    ))
    assert res.get("success") is True, res
    assert cap.get("reduce_only") is False                 # ENTRY — opens/grows the book
    assert cap.get("trigger_type") == "mid_price_above"    # a buy rung fires as price RISES
    assert cap.get("product_id") == 2
    assert int(cap.get("amount_x18")) > 0                  # BUY = positive signed amount
    # The resting limit is priced THROUGH the level (above it) so it crosses on fire.
    assert int(cap.get("price_x18")) > int(cap.get("trigger_price_x18"))


def test_sell_rung_fires_on_a_fall_not_reduce_only(monkeypatch):
    cap: dict = {}
    client = _client(monkeypatch, cap)
    res = asyncio.run(client.place_entry_trigger_order(
        product_id=2, size=1.0, trigger_price=78000.0, direction_is_buy=False,
    ))
    assert res.get("success") is True, res
    assert cap.get("reduce_only") is False
    assert cap.get("trigger_type") == "mid_price_below"    # a sell rung fires as price FALLS
    assert int(cap.get("amount_x18")) < 0                  # SELL = negative signed amount
    assert int(cap.get("price_x18")) < int(cap.get("trigger_price_x18"))


def test_dependency_is_passed_through_for_pyramiding(monkeypatch):
    cap: dict = {}
    client = _client(monkeypatch, cap)
    dep = SimpleNamespace(digest="0xrung1")
    res = asyncio.run(client.place_entry_trigger_order(
        product_id=2, size=1.0, trigger_price=79500.0, direction_is_buy=True, dependency=dep,
    ))
    assert res.get("success") is True, res
    assert cap.get("dependency") is dep                    # chains rung k+1 to rung k's fill


def test_invalid_params_never_raise(monkeypatch):
    cap: dict = {}
    client = _client(monkeypatch, cap)
    for kw in (dict(product_id=2, size=0.0, trigger_price=79000.0, direction_is_buy=True),
               dict(product_id=2, size=1.0, trigger_price=0.0, direction_is_buy=True)):
        res = asyncio.run(client.place_entry_trigger_order(**kw))
        assert res.get("success") is False
    assert cap == {}, "no trigger call should have been made for invalid params"


def test_uses_the_real_order_prep_method_not_a_typo():
    assert hasattr(NadoClient, "place_entry_trigger_order")
    import inspect
    src = inspect.getsource(NadoClient.place_entry_trigger_order)
    assert "self._prepare_place_order_params(" in src
    assert "reduce_only=False" in src        # entry, never a reduce-only stop
    assert "never_grow=False" in src         # a rung must be allowed to grow the book
