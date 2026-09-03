"""2026-09-02: HL DROPS THE SOCKET (no error frame, no close frame, ~1s after
the subscribe) on a subscription naming a coin it does not list under that
exact spelling. Our stores are upper-cased ("KBONK") but HL lists "kBONK", so
the covered check passed and every (re)connect resubscribed a name HL rejects —
the ~65s reconnect loop in production. Reproduced from a clean IP.
"""
import asyncio
import time
import unittest

from src.nadobro.market_data import hl_ws


class HlSubscribeSafetyTests(unittest.TestCase):
    def setUp(self):
        hl_ws.reset_state()
        self.addCleanup(hl_ws.reset_state)

    def test_subscriptions_go_out_with_hyperliquids_exact_spelling(self):
        ws = hl_ws.HyperliquidWs()
        ws.subscribe_coins(["KBONK", "BTC"])
        hl_ws._dispatch({"channel": "allMids", "data": {"mids": {"kBONK": "0.00002", "BTC": "60000"}}})
        self.assertEqual(ws.covered(), {"KBONK", "BTC"})       # our key stays upper-case
        sent = []

        async def _capture(payload):
            sent.append(payload)

        ws._send = _capture
        ws._ws = object()
        asyncio.run(ws._reconcile_subs())
        coins = {p["subscription"]["coin"] for p in sent}
        self.assertEqual(coins, {"kBONK", "BTC"})              # HL's spelling on the wire
        self.assertEqual(len(sent), 2 * len(hl_ws._COIN_STREAMS))

    def test_a_coin_that_drops_the_socket_twice_is_quarantined(self):
        ws = hl_ws.HyperliquidWs()
        ws.subscribe_coins(["BAD", "BTC"])
        hl_ws._dispatch({"channel": "allMids", "data": {"mids": {"BAD": "1", "BTC": "2"}}})
        with self.assertLogs("src.nadobro.market_data.hl_ws", level="WARNING"):
            for _ in range(2):
                ws._recent_subs.append(("BAD", time.monotonic()))
                ws._attribute_drop()
        self.assertIn("BAD", ws._quarantined)
        self.assertEqual(ws.covered(), {"BTC"})                # never subscribed again

    def test_one_strike_is_not_enough(self):
        # An innocent neighbour of the killer is a suspect once, not twice
        # (the subscribe order is shuffled per connect).
        ws = hl_ws.HyperliquidWs()
        ws.subscribe_coins(["ETH"])
        hl_ws._dispatch({"channel": "allMids", "data": {"mids": {"ETH": "1"}}})
        ws._recent_subs.append(("ETH", time.monotonic()))
        ws._attribute_drop()
        self.assertEqual(ws.covered(), {"ETH"})

    def test_only_recent_subscriptions_are_suspects(self):
        ws = hl_ws.HyperliquidWs()
        ws._recent_subs.append(("OLD", time.monotonic() - 100.0))
        ws._attribute_drop()
        ws._attribute_drop()
        self.assertNotIn("OLD", ws._quarantined)

    def test_reset_state_forgets_the_spelling_map(self):
        hl_ws._dispatch({"channel": "allMids", "data": {"mids": {"kPEPE": "1"}}})
        self.assertEqual(hl_ws._hl_symbol.get("KPEPE"), "kPEPE")
        hl_ws.reset_state()
        self.assertEqual(hl_ws._hl_symbol, {})
