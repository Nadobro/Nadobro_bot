"""Hyperliquid public market-data feed — parsing, freshness and dedupe.

Nado publishes no public book/trade websocket, so HL is where Mid mode's
microstructure signals come from. These pin the three properties the rest of
the design leans on:

* the HL book is normalised into the SAME shape as
  ``nado_client.get_market_liquidity``, so the pure math in ``quant/`` is
  venue-agnostic and the caller alone decides anchor vs signal;
* staleness is enforced on READ, so a frozen socket degrades to "no data"
  instead of to confidently wrong data;
* a reconnect replaying trades cannot double-count volume.

Pure: no socket is opened. ``_dispatch`` is fed the frames HL would send.
"""
import time
import unittest

from src.nadobro.market_data import hl_ws


def _book_frame(coin="BTC", bids=((100.0, 2.0),), asks=((101.0, 3.0),), ts_ms=1_700_000_000_000):
    return {
        "channel": "l2Book",
        "data": {
            "coin": coin,
            "levels": [
                [{"px": str(p), "sz": str(s), "n": 1} for p, s in bids],
                [{"px": str(p), "sz": str(s), "n": 1} for p, s in asks],
            ],
            "time": ts_ms,
        },
    }


def _trade_frame(coin="BTC", rows=((100.5, 1.0, "B", "0xabc", 1_700_000_000_000),)):
    return {
        "channel": "trades",
        "data": [
            {"coin": coin, "px": str(px), "sz": str(sz), "side": side, "hash": h, "time": t}
            for px, sz, side, h, t in rows
        ],
    }


class HlFeedTests(unittest.TestCase):
    def setUp(self):
        hl_ws.reset_state()
        hl_ws._shape_logged.clear()
        self.addCleanup(hl_ws.reset_state)
        self.addCleanup(hl_ws._shape_logged.clear)

    # --- book normalisation -------------------------------------------------

    def test_book_is_normalised_into_the_nado_depth_shape(self):
        hl_ws._dispatch(_book_frame())
        bk = hl_ws.book("BTC")
        self.assertEqual(bk["bids"], [[100.0, 2.0]])
        self.assertEqual(bk["asks"], [[101.0, 3.0]])

    def test_levels_are_ordered_best_first(self):
        # Imbalance math depends on level 0 being the touch.
        hl_ws._dispatch(_book_frame(
            bids=((99.0, 1.0), (100.0, 1.0), (98.0, 1.0)),
            asks=((103.0, 1.0), (101.0, 1.0), (102.0, 1.0)),
        ))
        bk = hl_ws.book("BTC")
        self.assertEqual([p for p, _ in bk["bids"]], [100.0, 99.0, 98.0])
        self.assertEqual([p for p, _ in bk["asks"]], [101.0, 102.0, 103.0])

    def test_malformed_and_zero_levels_are_dropped(self):
        frame = _book_frame()
        frame["data"]["levels"][0].extend([{"px": "0", "sz": "0"}, {"px": "junk", "sz": "1"}])
        hl_ws._dispatch(frame)
        self.assertEqual(hl_ws.book("BTC")["bids"], [[100.0, 2.0]])

    def test_coin_lookup_is_case_insensitive(self):
        hl_ws._dispatch(_book_frame(coin="BTC"))
        self.assertIsNotNone(hl_ws.book("btc"))

    # --- freshness ----------------------------------------------------------

    def test_stale_book_reads_as_absent(self):
        hl_ws._dispatch(_book_frame())
        self.assertIsNotNone(hl_ws.book("BTC", max_age_s=60))
        # Age the entry rather than sleeping.
        hl_ws._books["BTC"]["received_at"] = time.time() - 120
        self.assertIsNone(hl_ws.book("BTC", max_age_s=60))
        self.assertIsNone(hl_ws.snapshot("BTC", max_age_s=60))

    def test_snapshot_is_none_when_the_coin_was_never_seen(self):
        self.assertIsNone(hl_ws.snapshot("DOGE"))

    def test_one_stalled_stream_does_not_make_another_look_fresh(self):
        hl_ws._dispatch(_book_frame())
        hl_ws._dispatch({"channel": "activeAssetCtx", "data": {"coin": "BTC", "ctx": {"funding": "0.0001"}}})
        hl_ws._ctxs["BTC"]["received_at"] = time.time() - 120
        self.assertIsNotNone(hl_ws.book("BTC", max_age_s=60))
        self.assertIsNone(hl_ws.ctx("BTC", max_age_s=60))

    # --- trades -------------------------------------------------------------

    def test_trades_are_recorded_with_side_preserved(self):
        hl_ws._dispatch(_trade_frame())
        trades = hl_ws.recent_trades("BTC")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["px"], 100.5)
        self.assertEqual(trades[0]["side"], "B")

    def test_replayed_trades_are_deduped(self):
        # A reconnect replays recent trades; counting them twice would double
        # every volume and imbalance figure downstream.
        frame = _trade_frame()
        hl_ws._dispatch(frame)
        hl_ws._dispatch(frame)
        self.assertEqual(len(hl_ws.recent_trades("BTC")), 1)

    def test_distinct_trades_are_all_kept(self):
        hl_ws._dispatch(_trade_frame(rows=(
            (100.5, 1.0, "B", "0xa", 1_700_000_000_000),
            (100.6, 2.0, "A", "0xb", 1_700_000_000_001),
        )))
        self.assertEqual(len(hl_ws.recent_trades("BTC")), 2)

    def test_recent_trades_respects_the_window(self):
        hl_ws._dispatch(_trade_frame())
        for t in hl_ws._trades["BTC"]:
            t["received_at"] = time.time() - 300
        self.assertEqual(hl_ws.recent_trades("BTC", window_s=60), [])

    # --- ctx / mids ---------------------------------------------------------

    def test_ctx_exposes_the_fields_nado_cannot_supply(self):
        hl_ws._dispatch({"channel": "activeAssetCtx", "data": {"coin": "BTC", "ctx": {
            "funding": "0.0000125", "openInterest": "1234.5", "markPx": "100.2",
            "oraclePx": "100.1", "midPx": "100.15", "premium": "0.0001",
            "dayNtlVlm": "9876543.0",
        }}})
        c = hl_ws.ctx("BTC")
        self.assertAlmostEqual(c["open_interest"], 1234.5)
        self.assertAlmostEqual(c["oracle_px"], 100.1)
        self.assertAlmostEqual(c["funding"], 0.0000125)

    def test_mid_prefers_the_book_and_falls_back_to_all_mids(self):
        hl_ws._dispatch({"channel": "allMids", "data": {"mids": {"BTC": "999.0"}}})
        self.assertAlmostEqual(hl_ws.mid("BTC"), 999.0)
        hl_ws._dispatch(_book_frame())          # bid 100 / ask 101
        self.assertAlmostEqual(hl_ws.mid("BTC"), 100.5)

    # --- robustness ---------------------------------------------------------

    def test_unknown_and_control_frames_are_ignored(self):
        for frame in (
            {"channel": "subscriptionResponse", "data": {}},
            {"channel": "pong"},
            {"channel": "somethingNew", "data": {"x": 1}},
            {"channel": "l2Book", "data": {}},          # no coin
            {"channel": "trades", "data": "not-a-list"},
        ):
            hl_ws._dispatch(frame)                       # must not raise
        self.assertEqual(hl_ws.book("BTC"), None)

    def test_a_broken_listener_cannot_kill_the_stream(self):
        seen = []
        hl_ws.register_book_listener(lambda _c: (_ for _ in ()).throw(RuntimeError("boom")))
        hl_ws.register_book_listener(seen.append)
        self.addCleanup(hl_ws._book_listeners.clear)
        hl_ws._dispatch(_book_frame())
        self.assertEqual(seen, ["BTC"])                  # the good listener still ran
        self.assertIsNotNone(hl_ws.book("BTC"))          # and state still updated

    # --- coverage reconciliation -------------------------------------------

    def test_only_coins_hyperliquid_actually_lists_are_covered(self):
        # Nado lists equities/RWAs (QQQ, wGOOGLx...) that HL does not carry.
        # Subscribing to those would earn error frames, so they must be skipped
        # and simply never produce a snapshot.
        ws = hl_ws.HyperliquidWs()
        ws.subscribe_coins(["BTC", "ETH", "QQQ"])
        self.assertEqual(ws.covered(), set())            # nothing until allMids
        hl_ws._dispatch({"channel": "allMids", "data": {"mids": {"BTC": "1", "ETH": "2"}}})
        self.assertEqual(ws.covered(), {"BTC", "ETH"})   # QQQ correctly excluded

    def test_first_frame_of_each_channel_is_logged_once(self):
        # The wire format could not be probed live from the build environment,
        # so the first deployment must self-document it — but only once per
        # channel, or a pushed feed would flood the log.
        with self.assertLogs("src.nadobro.market_data.hl_ws", level="INFO") as cap:
            hl_ws._dispatch(_book_frame())
            hl_ws._dispatch(_book_frame())
            hl_ws._dispatch(_trade_frame())
        first_frames = [r for r in cap.output if "first frame" in r]
        self.assertEqual(len(first_frames), 2)          # l2Book + trades, not 3

    def test_health_reports_the_feed_without_gating_anything(self):
        hl_ws._dispatch(_book_frame())
        h = hl_ws.health()
        self.assertIn("BTC", h["coins"])
        self.assertIn("BTC", h["book_age_s"])


if __name__ == "__main__":
    unittest.main()
