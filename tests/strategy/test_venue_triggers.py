"""The bot's venue TRIGGER orders are cleaned up on every stop path (2026-09-16).

A Reverse Grid (standalone or D-Grid's trend phase) arms entry rungs + a
trailing stop as venue trigger orders. The resting-order cancel never sees
them, so the boot stand-down / cross-process stop / leftover sweep used to leave
armed, NON-reduce-only entry rungs on the venue after a restart. The sweep here
cancels ONLY digests the intent registry vouches for (a user's own manual
TP/SL triggers on the same product are never touched) and fails loud on an
unreadable trigger service.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from unittest import mock

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy import venue_triggers as vt  # noqa: E402


def _row(digest, status="waiting_price"):
    return {"order": {"digest": digest, "product_id": 2}, "status": status}


class _Client:
    def __init__(self, rows, *, list_raises=False, cancel_ok=True):
        self._rows = rows
        self._list_raises = list_raises
        self._cancel_ok = cancel_ok
        self.listed = []
        self.cancelled = []

    async def get_trigger_orders(self, *, product_ids=None, limit=100, strict=False, **kw):
        self.listed.append((product_ids, strict))
        if self._list_raises:
            raise RuntimeError("trigger service 503")
        return None if self._rows is None else list(self._rows)

    async def cancel_trigger_orders(self, *, product_id, digests):
        self.cancelled.append((product_id, list(digests)))
        return {"success": self._cancel_ok, "cancelled": len(digests)} if self._cancel_ok else {"success": False, "error": "rate limited"}


def _linked(*digests):
    async def _db(fn, *a, **k):
        return fn(*a, **k)
    return _db, (lambda network, ds, session_id=None: {d for d in ds if d in digests})


def _run(coro, linked):
    passthrough, lookup = linked
    with ExitStack() as es:
        es.enter_context(mock.patch("src.nadobro.core.async_utils.run_blocking_db", passthrough))
        es.enter_context(mock.patch("src.nadobro.models.database.get_bot_linked_digests", lookup))
        return asyncio.run(coro)


def test_cancels_only_the_bots_own_pending_triggers():
    c = _Client([_row("0xAAA"), _row("0xbbb"), _row("0xccc", status="cancelled"), _row("0xddd", status="triggered")])
    res = _run(vt.cancel_bot_trigger_orders(c, "mainnet", 2, session_id=7), _linked("0xaaa"))
    assert res["success"] is True and res["cancelled"] == 1
    assert c.cancelled == [(2, ["0xaaa"])], "the manual trigger 0xbbb and non-pending rows are never cancelled"
    assert c.listed == [([2], True)]


def test_nothing_pending_is_a_clean_success():
    c = _Client([])
    res = _run(vt.cancel_bot_trigger_orders(c, "mainnet", 2), _linked())
    assert res == {"success": True, "cancelled": 0} and not c.cancelled


def test_only_foreign_triggers_pending_cancels_nothing():
    c = _Client([_row("0xman")])
    res = _run(vt.cancel_bot_trigger_orders(c, "mainnet", 2), _linked())
    assert res["success"] is True and res["cancelled"] == 0 and res["pending_foreign"] == 1
    assert not c.cancelled


def test_an_unreadable_trigger_service_is_not_clear():
    c = _Client([], list_raises=True)
    res = _run(vt.cancel_bot_trigger_orders(c, "mainnet", 2), _linked())
    assert res["success"] is False and "unavailable" in res["error"]


def test_a_budget_denied_list_is_not_clear_either():
    """DENIED-vs-EMPTY: the client returns None when the gateway budget denies the
    read; the sweep must not report the product clear off that."""
    c = _Client(None)
    res = _run(vt.cancel_bot_trigger_orders(c, "mainnet", 2), _linked())
    assert res["success"] is False and "denied" in res["error"]
    assert not c.cancelled


def test_the_runs_persisted_digests_vouch_when_the_db_link_is_missing():
    """A placement whose session link was missed (no order_intents row) is still
    cancelled when the run's own telemetry lists the digest; case/prefix differences
    are normalised; a protective stop is kept when the position stays open."""
    c = _Client([_row("0xAAA"), _row("0xbbb"), _row("0xstop")])
    res = _run(vt.cancel_bot_trigger_orders(c, "mainnet", 2, extra_digests=["0XBBB", "0xstop"],
                                            exclude_digests=["0xSTOP"]), _linked("0xaaa"))
    assert res["success"] is True and res["cancelled"] == 2
    assert c.cancelled == [(2, ["0xaaa", "0xbbb"])]
    assert res["kept_protective"] == ["0xstop"]


def test_a_rejected_cancel_is_not_clear():
    c = _Client([_row("0xaaa")], cancel_ok=False)
    res = _run(vt.cancel_bot_trigger_orders(c, "mainnet", 2), _linked("0xaaa"))
    assert res["success"] is False and res["cancelled"] == 0


def test_state_wrapper_scopes_to_the_strategys_product_and_session():
    c = _Client([_row("0xaaa"), _row("0xrung"), _row("0xstop")])
    state = {"strategy": "rgrid", "product": "BTC", "strategy_session_id": 9,
             "grid_trigger_digests": ["0xrung", "0xstop"], "grid_stop_digest": "0xstop"}
    with mock.patch("src.nadobro.config.get_product_id", lambda product, network=None, **k: 2):
        res = _run(vt.cancel_session_trigger_orders_for_state(c, "mainnet", state, keep_protective=True),
                   _linked("0xaaa"))
        assert res["cancelled"] == 2 and c.cancelled == [(2, ["0xaaa", "0xrung"])]
        assert res["kept_protective"] == ["0xstop"], "the protective stop stays when the position is left open"
        c2 = _Client([_row("0xaaa"), _row("0xrung"), _row("0xstop")])
        res2 = _run(vt.cancel_session_trigger_orders_for_state(c2, "mainnet", state), _linked("0xaaa"))
        assert res2["cancelled"] == 3, "flat: the stale stop goes too"
    skipped = asyncio.run(vt.cancel_session_trigger_orders_for_state(c, "mainnet", {"strategy": "vol", "product": "BTC"}))
    assert skipped["cancelled"] == 0 and "skipped" in skipped


# ── boot stand-down runs the sweep ──────────────────────────────────────

def test_boot_stand_down_sweeps_the_sessions_venue_triggers(monkeypatch):
    br = pytest.importorskip("src.nadobro.strategy.bot_runtime")
    calls = {"sweep": [], "venue_stop": []}

    async def _pass_db(func, *a, **k):
        return func(*a, **k)

    async def _pass_sdk(func, *a, **k):
        return func(*a, **k)

    async def _sweep(client, network, state, *, keep_protective=False):
        calls["sweep"].append((network, state.get("product"), state.get("strategy_session_id"), keep_protective))
        return {"success": True, "cancelled": 3, "kept_protective": ["0xstop"]}

    async def _venue_stop(client, state, **kw):
        calls["venue_stop"].append(kw.get("product_id"))
        return False

    async def _noop(*a, **k):
        return None

    rows = [{"key": "strategy_bot:111:mainnet", "value": json.dumps(
        {"running": True, "strategy": "rgrid", "product": "BTC", "strategy_session_id": 501})}]
    with ExitStack() as es:
        p = es.enter_context
        p(mock.patch.object(br, "query_all", lambda *a, **k: rows))
        p(mock.patch.object(br, "get_product_id", lambda product, network=None, **k: 2))
        p(mock.patch.object(br, "run_blocking_db", _pass_db))
        p(mock.patch.object(br, "run_blocking_sdk", _pass_sdk))
        p(mock.patch("src.nadobro.trading.trade_service.cancel_resting_orders_for_user",
                     lambda uid, network, only_pid=None, **k: {"success": True}))
        p(mock.patch("src.nadobro.trading.trade_service.get_user_nado_client", lambda uid, network=None: object()))
        p(mock.patch("src.nadobro.strategy.venue_triggers.cancel_session_trigger_orders_for_state", _sweep))
        p(mock.patch("src.nadobro.strategy.venue_stop.cancel_session_venue_stop", _venue_stop))
        p(mock.patch("src.nadobro.trading.engine_persistence.terminate_engine_executors", lambda cid: None))
        p(mock.patch("src.nadobro.trading.engine_persistence.clear_controller_progress", lambda cid: None))
        p(mock.patch.object(br, "_finalize_session", lambda state, reason="stopped": None))
        p(mock.patch.object(br, "_save_state_async", _noop))
        notices = []

        async def _notify(uid, text, **fmt):
            notices.append(text.format(**fmt))
        p(mock.patch.object(br, "_notify", _notify))
        stood = asyncio.run(br.boot_stand_down_strategies())
    assert stood == 1
    assert calls["sweep"] == [("mainnet", "BTC", 501, True)], "entry rungs swept; protective stop KEPT"
    assert calls["venue_stop"] == [], "the rail's reduce-only stop is left with the open position"
    assert any("protective venue stop is left in place" in n for n in notices)


# ── SL/TP trace follow-ups (2026-09-16) ─────────────────────────────────

def test_a_user_widened_stop_shrinks_the_rung_never_grows_it():
    """The stop budget must cover the distance to the ladder's OWN exit. With a
    user stop wider than the step geometry, a full pyramid could lose more than
    the budget before the venue stop fires — so the rung shrinks; a tighter user
    stop never grows it past the step-sized rung."""
    from decimal import Decimal

    from src.nadobro.quant.rgrid_sizing import trigger_ladder_plan

    from src.nadobro.quant.rgrid_sizing import (
        REVGRID_ENTRY_SLIP, REVGRID_STOP_SLIP, TAKER_ROUND_TRIP_RATE, step_band_frac,
    )

    kw = dict(deployed_quote=5000, levels=4, spread_frac=Decimal("0.0015"), stop_budget_usd=2.0, min_step_usd=5)
    auto = trigger_ladder_plan(**kw)
    wide = trigger_ladder_plan(**kw, user_stop_pct=3.0)     # 3% of price
    tight = trigger_ladder_plan(**kw, user_stop_pct=0.1)    # 10bp (floored at the round trip)
    assert wide.rung_quote < auto.rung_quote, "a wider stop must shrink the rung"
    assert tight.rung_quote >= auto.rung_quote, "a tighter stop never shrinks it below auto"
    # every plan's full pyramid, reaching its own stop with the worst-case entry and
    # stop prints (the controller's real bounds) plus the taker round trip, fits the budget
    for plan in (auto, wide, tight):
        band = max(step_band_frac(plan.step_pct, Decimal(0)),
                   plan.stop_pct + REVGRID_STOP_SLIP + REVGRID_ENTRY_SLIP)
        assert plan.rung_quote * 4 * (band + TAKER_ROUND_TRIP_RATE) <= Decimal("2.0") + Decimal("1e-9")


def test_the_rails_orphan_sweep_keeps_the_engines_linked_stop(monkeypatch):
    """`reconcile_venue_stops` cancels reduce-only mid-price stops it did not place;
    the Reverse Grid's trailing stop is one too — but it is intent-linked, so it
    must survive the sweep (the rail's own unlinked stop is still swept)."""
    monkeypatch.setenv("NADO_VENUE_STOP_ENABLED", "1")
    from src.nadobro.strategy.venue_stop import reconcile_venue_stops

    class _C:
        network = "mainnet"

        def __init__(self):
            self.cancelled = []

        async def get_trigger_orders(self, *, product_ids=None, **kw):
            return [
                {"digest": "0xengine", "reduce_only": True, "trigger": {"mid_price_below": "1"}},
                {"digest": "0xrail", "reduce_only": True, "trigger": {"mid_price_below": "1"}},
            ]

        async def cancel_trigger_orders(self, *, product_id, digests):
            self.cancelled.append(list(digests))
            return {"success": True}

    async def _db(fn, *a, **k):
        return fn(*a, **k)

    c = _C()
    with ExitStack() as es:
        es.enter_context(mock.patch("src.nadobro.core.async_utils.run_blocking_db", _db))
        es.enter_context(mock.patch("src.nadobro.models.database.get_bot_linked_digests",
                                    lambda network, ds, session_id=None: {"0xengine"}))
        n = asyncio.run(reconcile_venue_stops(c, 2))
    assert n == 1 and c.cancelled == [["0xrail"]]


def _recording_sweep_mocks(calls: dict):
    """Async doubles for the sweep + the rail's stop cancel, driven by the SYNC
    stop path under test (which runs them through its own event loop)."""
    async def _sweep(client, network, state, *, keep_protective=False):
        calls["sweep"].append((network, state.get("product"), keep_protective))
        return {"success": True, "cancelled": 2}

    async def _venue_stop(client, state, **kw):
        calls["venue_stop"].append(kw.get("product_id"))
        return True

    return _sweep, _venue_stop


def test_manual_stop_cancels_the_rails_venue_stop_and_sweeps_triggers(monkeypatch):
    """After a SUCCESSFUL flatten the run's stale stop and the rail's stop go too;
    when the position is LEFT open (failed flatten / leftover retry) the
    protective stops are kept. (The sync stop helper drives the async doubles
    through its own loop — no asyncio.run here by design.)"""
    br = pytest.importorskip("src.nadobro.strategy.bot_runtime")
    calls = {"sweep": [], "venue_stop": []}
    _sweep, _venue_stop = _recording_sweep_mocks(calls)

    state = {"strategy": "grid", "product": "BTC", "strategy_session_id": 77}
    with ExitStack() as es:
        es.enter_context(mock.patch("src.nadobro.trading.trade_service.get_user_nado_client", lambda uid, network=None: object()))
        es.enter_context(mock.patch("src.nadobro.strategy.venue_triggers.cancel_session_trigger_orders_for_state", _sweep))
        es.enter_context(mock.patch("src.nadobro.strategy.venue_stop.cancel_session_venue_stop", _venue_stop))
        flat = br._sweep_bot_trigger_orders_sync(111, "mainnet", state, 2)
        kept = br._sweep_bot_trigger_orders_sync(111, "mainnet", state, 2, keep_protective=True)
    assert flat == {"success": True, "cancelled": 2} and kept == {"success": True, "cancelled": 2}
    assert calls["sweep"] == [("mainnet", "BTC", False), ("mainnet", "BTC", True)]
    assert calls["venue_stop"] == [2], "the rail's stop is cancelled only on the flat sweep"
    # stop_user_bot sweeps AFTER the flatten, for every single-perp strategy, keeping the
    # protective stops when the flatten failed
    src = open(br.__file__).read()
    assert "keep_protective=not bool(close_res.get(\"success\"))" in src
    assert src.index("close_res = cleanup_strategy_positions(telegram_id, network, state)") < src.index(
        "keep_protective=not bool(close_res.get(\"success\"))")
