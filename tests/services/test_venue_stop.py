"""Decision-logic coverage for the venue-side reduce-only stop lifecycle
(``strategy/venue_stop.py``). Audit: VENUE-STOP.

Exercises place / refresh / cancel / idempotency against a mock client — the
SDK placement + reduce-only encoding live in ``NadoClient.place_reduce_only_stop``
and are validated on testnet (the live trigger-service payloads can't be
faithfully simulated here). The feature is OFF by default.
"""
from __future__ import annotations

import asyncio

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy.venue_stop import (
    cancel_session_venue_stop,
    sync_session_venue_stop,
)


class _MockClient:
    def __init__(self, place_ok: bool = True, digest: str = "0xdead") -> None:
        self.placed: list[dict] = []
        self.cancelled: list[dict] = []
        self._place_ok = place_ok
        self._digest = digest

    async def place_reduce_only_stop(self, **kw):
        self.placed.append(kw)
        if self._place_ok:
            return {"success": True, "response": {"digest": self._digest}}
        return {"success": False, "error": "boom"}

    async def cancel_trigger_orders(self, *, product_id, digests):
        self.cancelled.append({"product_id": product_id, "digests": list(digests)})
        return {"success": True}


def _snap(side="long", size=1.0, entry=100.0, lev=10.0, pid=2, has_pos=True):
    return {
        "product_id": pid, "has_position": has_pos, "position_size": size,
        "position_side": side, "entry_price": entry, "leverage": lev,
    }


def test_disabled_by_default_is_a_noop(monkeypatch):
    monkeypatch.delenv("NADO_VENUE_STOP_ENABLED", raising=False)
    c = _MockClient()
    changed = asyncio.run(sync_session_venue_stop(c, _snap(), 10.0, {}))
    assert changed is False and not c.placed


def test_places_reduce_only_stop_below_entry_for_a_long(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    changed = asyncio.run(sync_session_venue_stop(c, _snap("long", 1.0, 100.0, 10.0), 10.0, state))
    assert changed is True and len(c.placed) == 1
    kw = c.placed[0]
    assert kw["position_is_long"] is True
    assert kw["stop_price"] == pytest.approx(99.0)     # 10% / 10x = 1% below entry
    assert kw["close_size"] == pytest.approx(1.0)
    assert state["_venue_stop"]["digests"] == ["0xdead"]


def test_places_above_entry_for_a_short(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    asyncio.run(sync_session_venue_stop(c, _snap("short", 2.0, 100.0, 10.0), 10.0, state))
    kw = c.placed[0]
    assert kw["position_is_long"] is False
    assert kw["stop_price"] == pytest.approx(101.0)    # 1% above entry


def test_idempotent_when_position_unchanged(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    asyncio.run(sync_session_venue_stop(c, _snap(), 10.0, state))
    changed = asyncio.run(sync_session_venue_stop(c, _snap(), 10.0, state))
    assert changed is False and len(c.placed) == 1     # no second placement


def test_replaces_when_position_size_changes(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    asyncio.run(sync_session_venue_stop(c, _snap(size=1.0), 10.0, state))
    asyncio.run(sync_session_venue_stop(c, _snap(size=2.0), 10.0, state))
    assert len(c.placed) == 2 and len(c.cancelled) == 1   # cancels the prior first


def test_cancels_and_forgets_when_flat(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    asyncio.run(sync_session_venue_stop(c, _snap(), 10.0, state))
    changed = asyncio.run(sync_session_venue_stop(c, _snap(has_pos=False, size=0.0), 10.0, state))
    assert changed is True and len(c.cancelled) == 1 and "_venue_stop" not in state


def test_explicit_cancel_helper(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    asyncio.run(sync_session_venue_stop(c, _snap(), 10.0, state))
    changed = asyncio.run(cancel_session_venue_stop(c, state))
    assert changed is True and len(c.cancelled) == 1 and "_venue_stop" not in state


def test_place_failure_clears_tracker_so_next_poll_retries(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    asyncio.run(sync_session_venue_stop(c, _snap(size=1.0), 10.0, state))
    c._place_ok = False
    asyncio.run(sync_session_venue_stop(c, _snap(size=3.0), 10.0, state))
    assert "_venue_stop" not in state                  # dropped -> next poll retries


def test_disarmed_sl_cancels_any_existing_stop(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    asyncio.run(sync_session_venue_stop(c, _snap(), 10.0, state))
    changed = asyncio.run(sync_session_venue_stop(c, _snap(), 0.0, state))   # SL disarmed
    assert changed is True and len(c.cancelled) == 1 and "_venue_stop" not in state
