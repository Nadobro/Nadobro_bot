"""Decision-logic coverage for the venue-side reduce-only stop lifecycle
(``strategy/venue_stop.py``). Audit: VENUE-STOP.

Exercises place / refresh / cancel / idempotency / churn / orphan-reconcile
against a mock client — the SDK placement + reduce-only encoding live in
``NadoClient.place_reduce_only_stop`` and are validated on testnet (the live
trigger-service payloads can't be faithfully simulated here). The feature is OFF
by default.
"""
from __future__ import annotations

import asyncio

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy.venue_stop import (
    cancel_session_venue_stop,
    reconcile_venue_stops,
    sync_session_venue_stop,
)


class _MockClient:
    def __init__(self, place_ok: bool = True, digest: str = "0xdead", trigger_rows=None) -> None:
        self.placed: list[dict] = []
        self.cancelled: list[dict] = []
        self.listed: list[dict] = []
        self._place_ok = place_ok
        self._digest = digest
        self._rows = trigger_rows or []

    async def place_reduce_only_stop(self, **kw):
        self.placed.append(kw)
        if self._place_ok:
            return {"success": True, "response": {"digest": self._digest}}
        return {"success": False, "error": "boom"}

    async def cancel_trigger_orders(self, *, product_id, digests):
        self.cancelled.append({"product_id": product_id, "digests": list(digests)})
        return {"success": True}

    async def get_trigger_orders(self, *, product_ids=None, **kw):
        self.listed.append({"product_ids": product_ids})
        return list(self._rows)


def _snap(side="long", size=1.0, entry=100.0, lev=10.0, pid=2, has_pos=True,
          margin=0.0, pos_value=0.0):
    return {
        "product_id": pid, "has_position": has_pos, "position_size": size,
        "position_side": side, "entry_price": entry, "leverage": lev,
        "margin": margin, "position_value": pos_value,
    }


def _run(coro):
    return asyncio.run(coro)


def test_disabled_by_default_is_a_noop(monkeypatch):
    monkeypatch.delenv("NADO_VENUE_STOP_ENABLED", raising=False)
    c = _MockClient()
    changed = _run(sync_session_venue_stop(c, _snap(), 10.0, {}))
    assert changed is False and not c.placed and not c.listed


def test_places_reduce_only_stop_below_entry_for_a_long(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    changed = _run(sync_session_venue_stop(c, _snap("long", 1.0, 100.0, 10.0), 10.0, state, now=1000.0))
    assert changed is True and len(c.placed) == 1
    kw = c.placed[0]
    assert kw["position_is_long"] is True
    assert kw["stop_price"] == pytest.approx(99.0)     # 10% / 10x = 1% below entry
    assert kw["close_size"] == pytest.approx(1.0)
    assert state["_venue_stop"]["digests"] == ["0xdead"]
    assert state["_venue_stop"]["placed_ts"] == 1000.0


def test_prices_off_effective_leverage_when_venue_leverage_is_zero(monkeypatch):
    # VENUE-STOP-REENTRY fix: price off notional/margin (1000/100 = 10x) so the
    # stop fires at sl_pct of the SAME margin the software rail measures — even
    # when the venue-reported leverage is 0.
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(
        c, _snap("long", 1.0, 100.0, lev=0.0, margin=100.0, pos_value=1000.0), 10.0, state, now=1.0))
    assert c.placed[0]["stop_price"] == pytest.approx(99.0)   # 10% / 10x -> 1% below


def test_places_above_entry_for_a_short(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    _run(sync_session_venue_stop(c, _snap("short", 2.0, 100.0, 10.0), 10.0, {}, now=1.0))
    kw = c.placed[0]
    assert kw["position_is_long"] is False
    assert kw["stop_price"] == pytest.approx(101.0)    # 1% above entry


def test_idempotent_when_position_unchanged(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(), 10.0, state, now=1.0))
    changed = _run(sync_session_venue_stop(c, _snap(), 10.0, state, now=2.0))
    assert changed is False and len(c.placed) == 1     # no second placement


def test_size_increase_always_reprices_immediately(monkeypatch):
    # A growing position must re-cover NOW (never under-cover), even inside the
    # min-reprice window.
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    monkeypatch.setenv("NADO_VENUE_STOP_MIN_REPRICE_SECONDS", "60")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(size=1.0), 10.0, state, now=1000.0))
    _run(sync_session_venue_stop(c, _snap(size=2.0), 10.0, state, now=1001.0))  # +1s, inside window
    assert len(c.placed) == 2 and len(c.cancelled) == 1   # repriced despite the window


def test_min_reprice_interval_defers_a_non_urgent_refresh(monkeypatch):
    # A small price drift (position shrank / entry nudged) within the window is
    # deferred — over-coverage is safe because reduce_only clamps to live size.
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    monkeypatch.setenv("NADO_VENUE_STOP_MIN_REPRICE_SECONDS", "60")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(entry=100.0), 10.0, state, now=1000.0))
    # entry moved enough to change the stop price, position SHRANK slightly, 1s later.
    changed = _run(sync_session_venue_stop(c, _snap(entry=100.5, size=0.9), 10.0, state, now=1001.0))
    assert changed is False and len(c.placed) == 1        # deferred within the window
    # ...but after the interval elapses it reprices.
    _run(sync_session_venue_stop(c, _snap(entry=100.5, size=0.9), 10.0, state, now=1100.0))
    assert len(c.placed) == 2


def test_side_flip_always_reprices_immediately(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    monkeypatch.setenv("NADO_VENUE_STOP_MIN_REPRICE_SECONDS", "60")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap("long"), 10.0, state, now=1000.0))
    _run(sync_session_venue_stop(c, _snap("short"), 10.0, state, now=1001.0))  # flip inside window
    assert len(c.placed) == 2 and c.placed[1]["position_is_long"] is False


def test_cancels_and_forgets_when_flat(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(), 10.0, state, now=1.0))
    changed = _run(sync_session_venue_stop(c, _snap(has_pos=False, size=0.0), 10.0, state, now=2.0))
    assert changed is True and len(c.cancelled) == 1 and "_venue_stop" not in state


def test_explicit_cancel_also_reconciles_by_product(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(), 10.0, state, now=1.0))
    changed = _run(cancel_session_venue_stop(c, state))
    assert changed is True and "_venue_stop" not in state
    assert len(c.cancelled) == 1                          # tracked digest cancelled
    assert c.listed                                       # reconcile-by-product listed too


def test_reconcile_sweeps_only_our_reduce_only_stops(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    rows = [
        {"digest": "0xours", "reduce_only": True, "trigger": {"price_trigger": {"price_requirement": {"mid_price_below": "1"}}}},
        {"digest": "0xtheirs", "reduce_only": False, "trigger": {"limit": 1}},   # not ours -> keep
        {"digest": "0xkeep", "reduce_only": True, "trigger": {"mid_price_above": "2"}},
    ]
    c = _MockClient(trigger_rows=rows)
    swept = _run(reconcile_venue_stops(c, 2, keep_digests=["0xkeep"]))
    assert swept == 1
    assert c.cancelled == [{"product_id": 2, "digests": ["0xours"]}]


def test_place_failure_clears_tracker_so_next_poll_retries(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(size=1.0), 10.0, state, now=1.0))
    c._place_ok = False
    _run(sync_session_venue_stop(c, _snap(size=3.0), 10.0, state, now=2.0))
    assert "_venue_stop" not in state                    # dropped -> next poll retries


def test_disarmed_sl_cancels_any_existing_stop(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(), 10.0, state, now=1.0))
    changed = _run(sync_session_venue_stop(c, _snap(), 0.0, state, now=2.0))   # SL disarmed
    assert changed is True and len(c.cancelled) == 1 and "_venue_stop" not in state


def test_orphan_sweep_runs_once_per_session_on_first_manage(monkeypatch):
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    c = _MockClient()
    state: dict = {}
    _run(sync_session_venue_stop(c, _snap(), 10.0, state, now=1.0))
    assert state.get("_venue_stop_reconciled") is True
    assert len(c.listed) == 1                             # reconciled on first manage
    _run(sync_session_venue_stop(c, _snap(size=2.0), 10.0, state, now=2.0))
    assert len(c.listed) == 1                             # not re-listed on later syncs
