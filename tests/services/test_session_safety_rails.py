"""Regression coverage for the live-PnL session safety rails + snapshot.

These guard the bug where a 1%-of-margin stop-loss rode all the way to a ~$32
loss: the rail in ``bot_runtime._run_cycle`` was gated on engine result actions
(``grid_stop_loss_hit`` etc.) that ``run_engine_cycle`` never emits, so it never
fired. The rail now reads live Nado session PnL (realized + unrealized) measured
as a percentage of the configured margin.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy import bot_runtime, engine_runtime, mm_dashboard

from src.nadobro.trading import live_session


async def _fake_run_blocking(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class SessionPnlRailTests(unittest.IsolatedAsyncioTestCase):
    async def _run_rail(self, snap, *, sl=1.0, tp=2.0):
        state = {
            "sl_pct": sl,
            "tp_pct": tp,
            "strategy": "dgrid",
            "strategy_session_id": 11,
            "running": True,
        }
        closed = {}

        async def close_coro():
            closed["called"] = True
            return {"success": True}

        sess = {"id": 11, "product_id": 2, "status": "running", "started_at": None, "stopped_at": None}
        self._engine_stop = AsyncMock()
        with patch.object(bot_runtime, "run_blocking", _fake_run_blocking), \
             patch("src.nadobro.models.database.get_strategy_session_by_id", return_value=sess), \
             patch("src.nadobro.models.database.get_active_strategy_session_for_strategy") as active_sess, \
             patch("src.nadobro.trading.live_session.get_live_session_snapshot", return_value=snap), \
             patch.object(engine_runtime.RUNTIME, "stop", new=self._engine_stop), \
             patch.object(bot_runtime, "_finalize_session") as fin, \
             patch.object(bot_runtime, "_save_state"), \
             patch.object(bot_runtime, "_notify", new=AsyncMock()), \
             patch.object(bot_runtime, "_strategy_display_name", return_value="DGRID"):
            res = await bot_runtime._evaluate_session_pnl_rail(
                42, "mainnet", state, "dgrid", "BTC",
                client=None, close_coro=close_coro,
            )
        active_sess.assert_not_called()
        return res, closed, state, fin

    async def test_sl_fires_on_unrealized_drawdown(self):
        # The screenshot scenario: -$32 on $100 margin = -32% of margin, SL=1%.
        snap = {"session_pnl": -32.0, "session_pnl_pct": -32.0, "margin": 100.0}
        res, closed, state, fin = await self._run_rail(snap, sl=1.0)
        self.assertEqual(res, (True, None))
        self.assertTrue(closed.get("called"))
        self.assertFalse(state["running"])
        fin.assert_called_once()
        self.assertEqual(fin.call_args.kwargs.get("stop_reason"), "sl_hit")
        # INVARIANT (Cleanup): the engine controller is stopped (resting orders
        # cancelled via _stop_out) BEFORE the position is flattened.
        self._engine_stop.assert_awaited_once_with(42, "mainnet", "dgrid")

    async def test_tp_fires_when_pct_above_target(self):
        snap = {"session_pnl": 2.5, "session_pnl_pct": 2.5, "margin": 100.0}
        res, closed, state, fin = await self._run_rail(snap, tp=2.0)
        self.assertEqual(res, (True, None))
        self.assertTrue(closed.get("called"))
        self.assertEqual(fin.call_args.kwargs.get("stop_reason"), "tp_hit")
        self.assertIsNone(state["last_error"])

    async def test_no_stop_within_band(self):
        snap = {"session_pnl": -0.5, "session_pnl_pct": -0.5, "margin": 100.0}
        res, closed, _state, fin = await self._run_rail(snap, sl=1.0, tp=2.0)
        self.assertIsNone(res)
        self.assertFalse(closed.get("called"))
        fin.assert_not_called()

    async def test_buffer_tightens_sl_only_on_a_fast_move_not_leverage_alone(self):
        # SLTP-FEE-BLEED rebalance (prod #253): a -6% PRICE draw at 50x in a CALM
        # market must NOT fire a 10% SL — the old leverage-only buffer tightened to
        # ~5% on leverage alone and stopped the user at half their budget. The buffer
        # now keeps the budget when calm and only tightens on a genuinely fast move.
        bot_runtime._SLTP_MARK_CACHE.clear()
        calm = {"session_pnl": -6.0, "session_pnl_pct": -6.0, "margin": 100.0,
                "leverage": 50.0, "position_value": 5000.0, "mark": 79000.0}
        res, _c, _s, fin = await self._run_rail(calm, sl=10.0, tp=50.0)
        self.assertIsNone(res)                 # calm: keeps the budget
        fin.assert_not_called()
        # A fast ~50bp per-poll move at the same leverage reserves for the overshoot,
        # so the SAME -6% draw now fires (protection where it is actually warranted).
        fast = {"session_pnl": -6.0, "session_pnl_pct": -6.0, "margin": 100.0,
                "leverage": 50.0, "position_value": 5000.0, "mark": 78600.0}
        res2, _c2, _s2, fin2 = await self._run_rail(fast, sl=10.0, tp=50.0)
        self.assertEqual(res2, (True, None))
        self.assertEqual(fin2.call_args.kwargs.get("stop_reason"), "sl_hit")

    async def test_buffer_absent_at_low_leverage_same_draw_holds(self):
        # The SAME -6% draw does NOT fire without leverage (buffer ~0, raw -10%
        # barrier governs) — proving it's the leverage-scaled buffer that fired
        # above, not a blanket tightening of every stop.
        snap = {"session_pnl": -6.0, "session_pnl_pct": -6.0, "margin": 100.0, "leverage": 1.0}
        res, _closed, _state, fin = await self._run_rail(snap, sl=10.0, tp=50.0)
        self.assertIsNone(res)
        fin.assert_not_called()

    async def test_buffer_uses_effective_leverage_from_position_over_margin(self):
        # SLTP-tracer MED: the stale-DB -> fresh-venue fallback hard-codes
        # leverage 0, which would no-op the buffer's volatility term (leverage x
        # move) exactly when the read is freshest. The rail derives effective
        # leverage = position_value / margin ($5,000 / $100 = 50x). Prove it: seed
        # a mark, then a fast move fires the -6% draw ONLY because eff-leverage 50x
        # (not snap.leverage 0) scaled the overshoot reserve.
        bot_runtime._SLTP_MARK_CACHE.clear()
        seed = {"session_pnl": 0.0, "session_pnl_pct": 0.0, "margin": 100.0,
                "leverage": 0.0, "position_value": 5000.0, "mark": 79000.0}
        await self._run_rail(seed, sl=10.0, tp=50.0)
        fast = {"session_pnl": -6.0, "session_pnl_pct": -6.0, "margin": 100.0,
                "leverage": 0.0, "position_value": 5000.0, "mark": 78600.0}
        res, _closed, _state, fin = await self._run_rail(fast, sl=10.0, tp=50.0)
        self.assertEqual(res, (True, None))
        self.assertEqual(fin.call_args.kwargs.get("stop_reason"), "sl_hit")

    async def test_safety_rails_dispatch_runs_duration_then_session_rail(self):
        # SLTP-FAST-POLL: _run_sltp_safety_rails runs the MM duration rail then
        # the %-of-margin session rail for an engine strategy and returns the
        # session rail's stop result — the same rails a normal cycle runs, minus
        # the trading tick.
        state = {"sl_pct": 10.0, "tp_pct": 50.0, "strategy": "dgrid",
                 "strategy_session_id": 11, "running": True}
        with patch.object(bot_runtime, "_evaluate_mm_duration_rail", new=AsyncMock(return_value=None)) as dur, \
             patch.object(bot_runtime, "_evaluate_session_pnl_rail", new=AsyncMock(return_value=(True, None))) as rail:
            res = await bot_runtime._run_sltp_safety_rails(42, "mainnet", state, "dgrid", "BTC", None)
        self.assertEqual(res, (True, None))
        dur.assert_awaited_once()
        rail.assert_awaited_once()

    async def test_safety_rails_noop_when_nothing_trips(self):
        state = {"sl_pct": 10.0, "strategy": "grid", "strategy_session_id": 11, "running": True}
        with patch.object(bot_runtime, "_evaluate_mm_duration_rail", new=AsyncMock(return_value=None)), \
             patch.object(bot_runtime, "_evaluate_session_pnl_rail", new=AsyncMock(return_value=None)):
            res = await bot_runtime._run_sltp_safety_rails(42, "mainnet", state, "grid", "BTC", None)
        self.assertEqual(res, (True, "safety_noop"))

    async def test_safety_rails_skip_dn(self):
        # DN is a two-leg hedge; the single-product rail would misread it, so the
        # safety pass is a no-op for DN (matches _run_cycle's DN handling).
        with patch.object(bot_runtime, "_evaluate_session_pnl_rail", new=AsyncMock()) as rail:
            res = await bot_runtime._run_sltp_safety_rails(42, "mainnet", {"strategy": "dn"}, "dn", "BTC", None)
        self.assertEqual(res, (True, "safety_noop"))
        rail.assert_not_awaited()

    async def _run_rail_with_state(self, snap, state):
        closed = {}

        async def close_coro():
            closed["called"] = True
            return {"success": True}

        sess = {"id": 11, "product_id": 2, "status": "running", "started_at": None, "stopped_at": None}
        self._engine_stop = AsyncMock()
        with patch.object(bot_runtime, "run_blocking", _fake_run_blocking), \
             patch("src.nadobro.models.database.get_strategy_session_by_id", return_value=sess), \
             patch("src.nadobro.models.database.get_active_strategy_session_for_strategy"), \
             patch("src.nadobro.trading.live_session.get_live_session_snapshot", return_value=snap), \
             patch.object(engine_runtime.RUNTIME, "stop", new=self._engine_stop), \
             patch.object(bot_runtime, "_finalize_session") as fin, \
             patch.object(bot_runtime, "_save_state"), \
             patch.object(bot_runtime, "_notify", new=AsyncMock()), \
             patch.object(bot_runtime, "_strategy_display_name", return_value="DGRID"):
            res = await bot_runtime._evaluate_session_pnl_rail(
                42, "mainnet", state, "dgrid", "BTC", client=None, close_coro=close_coro,
            )
        return res, closed, fin

    async def test_rail_prefers_overlay_sl_when_present(self):
        # User SL loose (20%), but the overlay wrote a tight 1% SL for this
        # regime -> the rail fires on the overlay SL at -1.5%.
        state = {
            "sl_pct": 20.0, "tp_pct": 50.0, "strategy": "dgrid",
            "strategy_session_id": 11, "running": True,
            "overlay_sl_pct": 1.0, "overlay_tp_pct": 2.0,
        }
        snap = {"session_pnl": -1.5, "session_pnl_pct": -1.5, "margin": 100.0}
        res, closed, fin = await self._run_rail_with_state(snap, state)
        self.assertEqual(res, (True, None))
        self.assertTrue(closed.get("called"))
        self.assertEqual(fin.call_args.kwargs.get("stop_reason"), "sl_hit")

    async def test_rail_uses_overlay_tp_widened_in_trend(self):
        # Overlay widened TP to 3% in a trend; +2.5% does NOT hit it even though
        # the user's static TP was 2%.
        state = {
            "sl_pct": 1.0, "tp_pct": 2.0, "strategy": "dgrid",
            "strategy_session_id": 11, "running": True,
            "overlay_sl_pct": 1.3, "overlay_tp_pct": 3.0,
        }
        snap = {"session_pnl": 2.5, "session_pnl_pct": 2.5, "margin": 100.0}
        res, closed, _fin = await self._run_rail_with_state(snap, state)
        self.assertIsNone(res)               # 2.5% < overlay TP 3% -> no stop
        self.assertFalse(closed.get("called"))

    async def test_overlay_drawdown_kill_switch_fires_when_user_sl_loose(self):
        # User SL is loose (20%) so it does NOT fire at -12%, but the overlay's
        # separate 10% drawdown cap trips flatten + stand-down.
        snap = {"session_pnl": -12.0, "session_pnl_pct": -12.0, "margin": 100.0}
        res, closed, state, fin = await self._run_rail(snap, sl=20.0, tp=50.0)
        self.assertEqual(res, (True, None))
        self.assertTrue(closed.get("called"))
        self.assertFalse(state["running"])
        self.assertEqual(fin.call_args.kwargs.get("stop_reason"), "overlay_drawdown")
        self.assertIn("overlay drawdown", (state["last_error"] or "").lower())
        self._engine_stop.assert_awaited_once_with(42, "mainnet", "dgrid")

    async def test_overlay_drawdown_not_fired_within_cap(self):
        # -8% is inside both the loose user SL (20%) and the 10% overlay cap.
        snap = {"session_pnl": -8.0, "session_pnl_pct": -8.0, "margin": 100.0}
        res, closed, _state, fin = await self._run_rail(snap, sl=20.0, tp=50.0)
        self.assertIsNone(res)
        self.assertFalse(closed.get("called"))
        fin.assert_not_called()

    async def test_no_basis_when_margin_zero(self):
        snap = {"session_pnl": -50.0, "session_pnl_pct": 0.0, "margin": 0.0}
        res, closed, _state, fin = await self._run_rail(snap, sl=1.0)
        self.assertIsNone(res)
        self.assertFalse(closed.get("called"))

    async def test_sl_judged_on_net_total_loss(self):
        # SLTP-EXACT: the SL is a TOTAL-LOSS contract. Gross price PnL is -0.5%
        # (within a 1% stop) but NET of fees the user is actually down -1.5% — past
        # their 1% budget — so the stop fires (their real loss reached the number).
        snap = {
            "session_pnl": -0.5, "session_pnl_pct": -0.5,
            "session_pnl_net": -1.5, "session_pnl_pct_net": -1.5,
            "margin": 100.0,
        }
        res, closed, _state, fin = await self._run_rail(snap, sl=1.0)
        self.assertEqual(res, (True, None))
        self.assertEqual(fin.call_args.kwargs.get("stop_reason"), "sl_hit")
        # And it does NOT fire while the user's NET loss is still inside the stop,
        # even if the gross price move is larger.
        inside = {"session_pnl": -0.9, "session_pnl_pct": -0.9,
                  "session_pnl_net": -0.6, "session_pnl_pct_net": -0.6, "margin": 100.0}
        res2, _c2, _s2, fin2 = await self._run_rail(inside, sl=1.0)
        self.assertIsNone(res2)
        fin2.assert_not_called()

    async def test_tp_not_triggered_when_fees_eat_the_gross_gain(self):
        # Gross +2.1% would trip a 2% TP, but net of fees it's only +1.0% — the
        # TP must NOT fire on a gain the fees already ate.
        snap = {
            "session_pnl": 2.1, "session_pnl_pct": 2.1,
            "session_pnl_net": 1.0, "session_pnl_pct_net": 1.0,
            "margin": 100.0,
        }
        res, closed, _state, fin = await self._run_rail(snap, sl=1.0, tp=2.0)
        self.assertIsNone(res)
        self.assertFalse(closed.get("called"))


class LiveSnapshotMathTests(unittest.TestCase):
    """Unrealized PnL + position come from the live VENUE position (baseline-
    adjusted) so the strategy SL agrees with Portfolio; realized/fees from the
    run's own tagged fills; volume from venue turnover."""

    def _snap(self, *, venue, metrics=None, mark, margin=100.0, baseline=None,
              turnover=None, client=None):
        metrics = metrics or {"fills": 0, "volume": 0.0, "fees": 0.0, "realized_pnl": 0.0}
        turnover = turnover or {"volume": 0.0, "fills": 0}
        sess = {"id": 1, "product_id": 2, "started_at": None, "stopped_at": None}
        if baseline:
            import json as _json
            sess["config_snapshot"] = _json.dumps(baseline)
        with patch.object(live_session, "_venue_position", return_value=venue), \
             patch("src.nadobro.models.database.get_session_live_metrics", return_value=metrics), \
             patch("src.nadobro.models.database.get_session_turnover", return_value=turnover), \
             patch("src.nadobro.models.database.count_open_orders_for_product", return_value=1):
            return live_session.get_live_session_snapshot(
                42, "mainnet", sess,
                state={"notional_usd": margin}, client=client, mark=mark,
            )

    def test_session_pnl_is_venue_upnl(self):
        # The screenshot SL scenario: venue position uPnL = -$10.38 on $100
        # margin, SL 10%. Session PnL must reflect the REAL -10.38% so the rail
        # fires (the bug: reconstructed fills read ~-0.9%).
        venue = {"size_signed": 0.08, "entry": 63266.0, "liq": 60953.0,
                 "leverage": 49.0, "margin_used": 100.0, "upnl": -10.38, "synced_ts": 9e18}
        snap = self._snap(venue=venue, mark=63135.0)
        self.assertAlmostEqual(snap["unrealized_pnl"], -10.38)
        self.assertAlmostEqual(snap["session_pnl"], -10.38)
        self.assertAlmostEqual(snap["session_pnl_pct"], -10.38)
        self.assertTrue(snap["has_position"])
        self.assertEqual(snap["position_side"], "long")
        self.assertAlmostEqual(snap["position_size"], 0.08)
        self.assertAlmostEqual(snap["position_value"], 0.08 * 63135.0)
        self.assertAlmostEqual(snap["liq_price"], 60953.0)

    def test_baseline_excludes_preexisting_position(self):
        # A position pre-existed at run start (5.0 BTC @ 60050). Venue now shows
        # 5.02 BTC with -$302 total uPnL. The run only added 0.02 — its PnL must
        # EXCLUDE the baseline's uPnL (no contamination from a manual position).
        mark = 60000.0
        baseline = {"baseline_size": 5.0, "baseline_entry": 60050.0}
        venue = {"size_signed": 5.02, "entry": 60048.0, "liq": 0.0,
                 "leverage": 0.0, "margin_used": 0.0, "upnl": -302.0, "synced_ts": 9e18}
        snap = self._snap(venue=venue, mark=mark, baseline=baseline)
        baseline_upnl = 5.0 * (mark - 60050.0)        # = -250
        self.assertAlmostEqual(snap["unrealized_pnl"], -302.0 - baseline_upnl)  # run-only
        self.assertGreater(snap["session_pnl"], -302.0)   # nowhere near the full -302
        self.assertAlmostEqual(snap["position_size"], 0.02)

    def test_no_position_reports_zero_unrealized(self):
        # Flat venue position -> unrealized 0, session_pnl == realized.
        venue = {"size_signed": 0.0, "entry": 0.0, "liq": 0.0, "leverage": 0.0,
                 "margin_used": 0.0, "upnl": 0.0, "synced_ts": 9e18}
        snap = self._snap(venue=venue, mark=64000.0,
                          metrics={"fills": 8, "volume": 0.0, "fees": 2.0, "realized_pnl": 5.0})
        self.assertAlmostEqual(snap["unrealized_pnl"], 0.0)
        self.assertAlmostEqual(snap["session_pnl"], 5.0)
        self.assertFalse(snap["has_position"])
        self.assertEqual(snap["position_side"], "")

    def test_net_pnl_subtracts_fees_gross_does_not(self):
        # SLTP-GROSS fix: the snapshot exposes BOTH a gross session_pnl (for the
        # status/share cards) and a net-of-fees basis (for the SL/TP rail).
        venue = {"size_signed": 0.0, "entry": 0.0, "liq": 0.0, "leverage": 0.0,
                 "margin_used": 0.0, "upnl": 0.0, "synced_ts": 9e18}
        snap = self._snap(venue=venue, mark=64000.0, margin=100.0,
                          metrics={"fills": 8, "volume": 0.0, "fees": 2.0, "realized_pnl": 5.0})
        self.assertAlmostEqual(snap["session_pnl"], 5.0)          # gross unchanged
        self.assertAlmostEqual(snap["session_pnl_net"], 3.0)     # 5.0 - 2.0 fees
        self.assertAlmostEqual(snap["session_pnl_pct"], 5.0)
        self.assertAlmostEqual(snap["session_pnl_pct_net"], 3.0)

    def test_conservation_pnl_is_realized_plus_unrealized(self):
        venue = {"size_signed": 0.02, "entry": 63000.0, "liq": 0.0, "leverage": 0.0,
                 "margin_used": 0.0, "upnl": 7.5, "synced_ts": 9e18}
        snap = self._snap(venue=venue, mark=63375.0,
                          metrics={"fills": 6, "volume": 0.0, "fees": 1.0, "realized_pnl": 3.5})
        self.assertAlmostEqual(snap["session_pnl"],
                               snap["realized_pnl"] + snap["unrealized_pnl"])

    def test_volume_uses_venue_turnover(self):
        # Session volume = real turnover on the product (matches Nado), not the
        # under-counted tagged-fill sum.
        venue = {"size_signed": 0.08, "entry": 63266.0, "liq": 0.0, "leverage": 0.0,
                 "margin_used": 0.0, "upnl": -10.0, "synced_ts": 9e18}
        snap = self._snap(venue=venue, mark=63135.0,
                          metrics={"fills": 4, "volume": 2330.0, "fees": 0.5, "realized_pnl": 0.0},
                          turnover={"volume": 6100.0, "fills": 40})
        self.assertAlmostEqual(snap["volume"], 6100.0)
        self.assertEqual(snap["fills"], 40)

    def test_dn_volume_uses_spot_and_perp_turnover(self):
        venue = {"size_signed": -0.5, "entry": 100.0, "liq": 0.0, "leverage": 1.0,
                 "margin_used": 100.0, "upnl": 0.0, "synced_ts": 9e18}
        sess = {
            "id": 10,
            "strategy": "dn",
            "product_id": 117,
            "product_name": "WGOOGLX",
            "started_at": None,
            "stopped_at": None,
        }
        turnovers = {
            117: {"volume": 200.0, "fills": 2},
            118: {"volume": 300.0, "fills": 2},
        }
        open_orders = {117: 1, 118: 2}
        seen_turnover_products = []
        seen_open_products = []

        def fake_turnover(_user, _network, product_id, *_args):
            seen_turnover_products.append(int(product_id))
            return turnovers[int(product_id)]

        def fake_open_orders(_user, _network, product_id):
            seen_open_products.append(int(product_id))
            return open_orders[int(product_id)]

        with patch.object(live_session, "_venue_position", return_value=venue), \
             patch("src.nadobro.models.database.get_session_live_metrics",
                   return_value={"fills": 0, "volume": 0.0, "fees": 0.0, "realized_pnl": 0.0}), \
             patch("src.nadobro.models.database.get_session_turnover", side_effect=fake_turnover), \
             patch("src.nadobro.models.database.count_open_orders_for_product", side_effect=fake_open_orders), \
             patch("src.nadobro.venue.product_catalog.get_dn_pair",
                   return_value={"perp_product_id": 117, "spot_product_id": 118}):
            snap = live_session.get_live_session_snapshot(
                42, "mainnet", sess, state={"strategy": "dn", "notional_usd": 100.0}, client=None, mark=100.0,
            )

        self.assertEqual(sorted(seen_turnover_products), [117, 118])
        self.assertEqual(sorted(seen_open_products), [117, 118])
        self.assertAlmostEqual(snap["volume"], 500.0)
        self.assertEqual(snap["fills"], 4)
        self.assertEqual(snap["open_orders"], 3)


class StatusRenderTests(unittest.TestCase):
    def test_status_lines_show_upnl_and_session_pnl(self):
        snap = {
            "unrealized_pnl": -30.0, "session_pnl": -32.0, "session_pnl_pct": -32.0,
            "margin": 100.0, "realized_pnl": -2.0, "volume": 1000.0, "fees": 0.5,
            "fills": 4, "open_orders": 1, "has_position": True, "position_size": 0.0527,
            "position_side": "long", "entry_price": 65558.0, "liq_price": 61765.0,
        }
        s = mm_dashboard.build_status_snapshot(
            state={"running": True}, strategy_id="dgrid", network="mainnet",
            product="BTC", open_orders_count=0, live_snapshot=snap,
        )
        text = "\n".join(mm_dashboard.render_status_lines(s))
        # PnL leads with the per-run realized+unrealized session PnL, then a
        # realized/unrealized breakdown.
        self.assertIn("PnL (realized+unrealized): $-32.00", text)
        self.assertIn("-32.00%", text)
        self.assertIn("realized $-2.00 | unrealized $-30.00", text)
        self.assertIn("Position: LONG", text)


class DashboardSessionResolverTests(unittest.TestCase):
    def test_mm_status_uses_state_session_id(self):
        from src.nadobro.handlers import commands

        state = {
            "running": True,
            "strategy": "dgrid",
            "strategy_session_id": 22,
            "notional_usd": 100.0,
        }
        status = {
            "running": True,
            "strategy": "dgrid",
            "network": "mainnet",
            "product": "BTC",
            "open_orders_count": 0,
            "strategy_session_id": 22,
        }
        state_sess = {"id": 22, "product_id": 2, "status": "running"}
        wrong_newest = {"id": 99, "product_id": 3, "status": "running"}
        chosen = {}

        def fake_snapshot(_user, _network, sess, **_kwargs):
            chosen["id"] = sess["id"]
            return {
                "unrealized_pnl": -3.0,
                "session_pnl": -5.0,
                "session_pnl_pct": -5.0,
                "margin": 100.0,
                "realized_pnl": -2.0,
                "volume": 1000.0,
                "fees": 0.5,
                "fills": 4,
                "open_orders": 1,
                "has_position": True,
                "position_size": 0.01,
                "position_side": "long",
                "entry_price": 65000.0,
                "liq_price": 0.0,
            }

        with patch("src.nadobro.strategy.bot_runtime.get_user_bot_status", return_value=status), \
             patch("src.nadobro.strategy.bot_runtime.get_user_bot_state", return_value=state), \
             patch("src.nadobro.models.database.get_strategy_session_by_id", return_value=state_sess), \
             patch("src.nadobro.models.database.get_active_strategy_session_for_strategy", return_value=wrong_newest), \
             patch("src.nadobro.users.user_service.get_user_readonly_client", return_value=None), \
             patch("src.nadobro.trading.live_session.get_live_session_snapshot", side_effect=fake_snapshot):
            text, is_active = commands.build_mm_status_text(42)

        self.assertTrue(is_active)
        self.assertEqual(chosen["id"], 22)
        self.assertIn("PnL (realized+unrealized): $-5.00", text)

    def test_mm_status_includes_volume_spot_session(self):
        from src.nadobro.handlers import commands

        state = {
            "running": True,
            "strategy": "vol",
            "strategy_session_id": 55,
            "product": "WGOOGLX",
            "network": "mainnet",
            "vol_market": "spot",
            "vol_phase": "pending_fill",
            "target_volume_usd": 10_000.0,
            "volume_done_usd": 200.0,
            "volume_remaining_usd": 9_800.0,
            "session_realized_pnl_usd": 0.8,
            "order_observability": {"orders_placed": 1, "orders_filled": 0, "orders_cancelled": 0},
        }
        status = {
            "running": True,
            "strategy": "vol",
            "network": "mainnet",
            "product": "WGOOGLX",
            "vol_market": "spot",
            "strategy_session_id": 55,
        }
        live_snap = {
            "volume": 200.0,
            "realized_pnl": 1.2,
            "fees": 0.4,
            "fills": 0,
            "open_orders": 1,
        }

        with patch("src.nadobro.strategy.bot_runtime.get_user_bot_status", return_value=status), \
             patch("src.nadobro.strategy.bot_runtime.get_user_bot_state", return_value=state), \
             patch("src.nadobro.trading.session_resolver.resolve_current_strategy_session",
                   return_value={"id": 55, "product_id": 77, "status": "running"}), \
             patch("src.nadobro.users.user_service.get_user_readonly_client", return_value=None), \
             patch("src.nadobro.trading.live_session.get_live_session_snapshot", return_value=live_snap):
            text, is_active = commands.build_mm_status_text(42)

        self.assertTrue(is_active)
        self.assertIn("VOL WGOOGLX SPOT (mainnet) — LIVE", text)
        self.assertIn("Phase: pending_fill", text)
        self.assertIn("Volume: $200.00 / $10,000.00 (2.0%)", text)
        self.assertIn("PnL: realized $+1.20", text)
        self.assertIn("Orders: 1 open / 1 placed / 0 filled / 0 cancelled", text)


class MultiprocessTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegated_timeout_does_not_run_local_fallback(self):
        state = {
            "running": True,
            "strategy": "dgrid",
            "product": "BTC",
            "interval_seconds": 60,
        }
        saved = []
        local_run = AsyncMock(return_value=(True, None))
        mark_error = AsyncMock()

        def load_state(_user, _network):
            return dict(state)

        def save_state(_user, _network, updated):
            saved.append(dict(updated))

        async def timed_out_submit(_payload):
            raise asyncio.TimeoutError()

        with patch.object(bot_runtime, "_load_state", side_effect=load_state), \
             patch.object(bot_runtime, "_save_state", side_effect=save_state), \
             patch.object(bot_runtime, "_run_cycle", new=local_run), \
             patch.object(bot_runtime, "_mark_cycle_error", new=mark_error), \
             patch.object(bot_runtime, "_strategy_use_multiprocess", return_value=True), \
             patch.object(bot_runtime, "_strategy_cycle_timeout_seconds", return_value=0.01), \
             patch("src.nadobro.runtime.runtime_supervisor.is_multiprocess_enabled", return_value=True), \
             patch("src.nadobro.runtime.runtime_supervisor.strategy_worker_group", return_value="mm_grid"), \
             patch("src.nadobro.runtime.runtime_supervisor.submit_cycle_job", side_effect=timed_out_submit), \
             patch("src.nadobro.trading.execution_queue.get_queue_diagnostics", return_value={}):
            await bot_runtime.handle_strategy_job({"telegram_id": 42, "network": "mainnet"})

        local_run.assert_not_called()
        mark_error.assert_awaited_once()
        self.assertTrue(any(s.get("last_cycle_result") == "error" for s in saved))


if __name__ == "__main__":
    unittest.main()


class OverlayDisarmedBarrierTests(unittest.IsolatedAsyncioTestCase):
    """OVERLAY-DISARMED-BARRIER-ARMS (audit 2026-08-12, FIXED).

    Lives here, not in tests/engine/test_sltp_invariants.py, because it drives the
    real rail and therefore needs psycopg2 — and the invariants file is run by
    self-review.yml with `pip install pytest` and nothing else.

    The rail folded the overlay barrier in as
    ``tp_pct = max(ov_tp, tp_pct) if tp_pct > 0 else float(ov_tp)``. The ``else``
    half ADOPTED the overlay value when the user's own number was 0 — i.e. when they
    deliberately disarmed it. Reachable: the Turbo Volume preset writes
    ``tp_pct: 0.0`` for mid, mid is in OVERLAY_STRATEGIES, and ``overlay_tp_pct``
    persists in bot_state across degraded overlay cycles and restarts. Reproduced a
    session closing at +0.96% with TP explicitly off. Now gated on
    ``sltp_is_explicit``: present-and-zero is a choice, not an absence.
    """

    async def _run(self, *, sl, tp, overlay_sl=None, overlay_tp=None, snap):
        state = {
            "sl_pct": sl, "tp_pct": tp, "strategy": "mid",
            "strategy_session_id": 11, "running": True,
        }
        if overlay_sl is not None:
            state["overlay_sl_pct"] = overlay_sl
        if overlay_tp is not None:
            state["overlay_tp_pct"] = overlay_tp
        closed = {}

        async def close_coro():
            closed["called"] = True
            return {"success": True}

        sess = {"id": 11, "product_id": 2, "status": "running",
                "started_at": None, "stopped_at": None}
        with patch.object(bot_runtime, "run_blocking", _fake_run_blocking), \
             patch("src.nadobro.models.database.get_strategy_session_by_id", return_value=sess), \
             patch("src.nadobro.trading.live_session.get_live_session_snapshot", return_value=snap), \
             patch.object(engine_runtime.RUNTIME, "stop", new=AsyncMock()), \
             patch.object(bot_runtime, "_finalize_session"), \
             patch.object(bot_runtime, "_save_state"), \
             patch.object(bot_runtime, "_notify", new=AsyncMock()), \
             patch.object(bot_runtime, "_strategy_display_name", return_value="MID"):
            res = await bot_runtime._evaluate_session_pnl_rail(
                42, "mainnet", state, "mid", "BTC",
                client=None, close_coro=close_coro,
            )
        return res, closed

    async def test_a_disarmed_tp_is_never_armed_by_a_stale_overlay_value(self):
        snap = {"session_pnl": 1.0, "session_pnl_pct": 1.0,
                "session_pnl_pct_net": 1.0, "margin": 100.0}
        res, closed = await self._run(sl=10.0, tp=0.0, overlay_tp=0.96, snap=snap)
        self.assertFalse(
            closed.get("called"),
            "a stale overlay TP closed a session whose TP the user disarmed",
        )
        self.assertTrue(res is None or res[0] is not True)

    async def test_a_disarmed_sl_is_never_armed_by_a_stale_overlay_value(self):
        snap = {"session_pnl": -1.0, "session_pnl_pct": -1.0,
                "session_pnl_pct_net": -1.0, "margin": 100.0}
        res, closed = await self._run(sl=0.0, tp=50.0, overlay_sl=0.5, snap=snap)
        self.assertFalse(closed.get("called"))
        self.assertTrue(res is None or res[0] is not True)

    async def test_mid_stop_loss_is_not_tightened_by_the_overlay(self):
        """Mid's selected stop is a session contract, not an overlay input.

        The overlay may change Mid quoting behavior, but a 2% Mid stop must not
        become a 0.5% stop merely because the current regime is choppy.
        """
        snap = {"session_pnl": -0.7, "session_pnl_pct": -0.7,
                "session_pnl_pct_net": -0.7, "margin": 100.0}
        res, closed = await self._run(sl=2.0, tp=50.0, overlay_sl=0.5, snap=snap)
        self.assertIsNone(res)
        self.assertFalse(closed.get("called"))

    async def test_mid_take_profit_is_not_widened_by_the_overlay(self):
        """Mid must close at the user's TP rather than waiting for a
        regime-adjusted overlay target."""
        snap = {"session_pnl": 1.5, "session_pnl_pct": 1.5,
                "session_pnl_pct_net": 1.5, "margin": 100.0}
        res, closed = await self._run(sl=50.0, tp=1.0, overlay_tp=3.0, snap=snap)
        self.assertEqual(res, (True, None))
        self.assertTrue(closed.get("called"))


class LiqProximityRailTests(unittest.IsolatedAsyncioTestCase):
    """Live protective-flatten guard (per-asset leverage, 2026-08).

    The rail must protectively flatten when the position approaches venue
    liquidation, and — crucially — that check must run EVEN WHEN the user
    disarmed SL/TP (the reorder that moved the snapshot fetch ahead of the
    ``sl<=0 and tp<=0`` short-circuit: LIQ-GUARD-PROXIMITY-ALWAYS-ON).
    """

    async def _run(self, snap, *, sl=0.0, tp=0.0, strategy="grid"):
        state = {
            "sl_pct": sl, "tp_pct": tp, "strategy": strategy,
            "strategy_session_id": 11, "running": True,
        }
        closed = {}

        async def close_coro():
            closed["called"] = True
            return {"success": True}

        sess = {"id": 11, "product_id": 2, "status": "running",
                "started_at": None, "stopped_at": None}
        with patch.object(bot_runtime, "run_blocking", _fake_run_blocking), \
             patch("src.nadobro.models.database.get_strategy_session_by_id", return_value=sess), \
             patch("src.nadobro.trading.live_session.get_live_session_snapshot", return_value=snap), \
             patch.object(engine_runtime.RUNTIME, "stop", new=AsyncMock()), \
             patch.object(bot_runtime, "_finalize_session") as fin, \
             patch.object(bot_runtime, "_save_state"), \
             patch.object(bot_runtime, "_notify", new=AsyncMock()), \
             patch.object(bot_runtime, "_strategy_display_name", return_value="GRID"):
            res = await bot_runtime._evaluate_session_pnl_rail(
                42, "mainnet", state, strategy, "BTC",
                client=None, close_coro=close_coro,
            )
        return res, closed, fin

    def _near_liq_snap(self):
        # long: entry 100, liq 98 (runway 2), mark 98.4 -> 0.8 consumed (>= 0.75).
        return {
            "session_pnl": -80.0, "session_pnl_pct": -80.0, "session_pnl_pct_net": -80.0,
            "margin": 100.0, "entry_price": 100.0, "mark": 98.4, "liq_price": 98.0,
            "net_base": 1.0, "leverage": 50.0,
        }

    async def test_flattens_near_liquidation_even_with_sltp_disarmed(self):
        res, closed, fin = await self._run(self._near_liq_snap(), sl=0.0, tp=0.0)
        self.assertEqual(res, (True, None))
        self.assertTrue(closed.get("called"))
        self.assertEqual(fin.call_args.kwargs.get("stop_reason"), "liq_guard")

    async def test_does_not_flatten_when_runway_remains(self):
        snap = self._near_liq_snap()
        snap["mark"] = 99.0            # 0.5 consumed (< 0.75), SL/TP disarmed
        res, closed, fin = await self._run(snap, sl=0.0, tp=0.0)
        self.assertIsNone(res)
        self.assertFalse(closed.get("called"))
        fin.assert_not_called()

    async def test_skips_on_missing_or_wrong_side_liq(self):
        for bad in ({"liq_price": 0.0}, {"liq_price": 101.0}):   # missing; wrong-side (above mark on long)
            snap = self._near_liq_snap()
            snap.update(bad)
            res, closed, _fin = await self._run(snap, sl=0.0, tp=0.0)
            self.assertIsNone(res, bad)
            self.assertFalse(closed.get("called"), bad)

    async def test_disabled_by_env_kill_switch(self):
        import os
        os.environ["NADO_LIQ_GUARD_ENABLED"] = "false"
        try:
            res, closed, _fin = await self._run(self._near_liq_snap(), sl=0.0, tp=0.0)
            self.assertIsNone(res)
            self.assertFalse(closed.get("called"))
        finally:
            del os.environ["NADO_LIQ_GUARD_ENABLED"]
