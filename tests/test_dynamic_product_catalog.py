import unittest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.config import PRODUCTS
from src.nadobro.handlers.intent_parser import parse_trade_intent
from src.nadobro.handlers.keyboards import trade_product_reply_kb
from src.nadobro.venue import product_catalog


class DynamicProductCatalogTests(unittest.TestCase):
    def test_dn_underlying_key_maps_xstock_wrapped_symbol(self):
        self.assertEqual(product_catalog._dn_underlying_key("wbNVDA"), "NVDA")
        self.assertEqual(product_catalog._dn_underlying_key("wbAAPL"), "AAPL")
        # Wrapped synthetic ticker keeps legacy WB…X stripping (e.g. SPY perp leg).
        self.assertEqual(product_catalog._dn_underlying_key("WBSPYX"), "SPY")

    def test_dn_underlying_key_maps_real_testnet_wrapped_stocks(self):
        # The live Nado testnet spot symbols use the w<TICKER>x wrapper
        # (wQQQx / wSPYx / wNVDAx). These MUST map to the bare ticker so the
        # spot leg pairs with <TICKER>-PERP — otherwise QQQ/SPY/etc. silently
        # drop out of the Delta Neutral asset list (only BTC/ETH survive).
        cases = {
            "wQQQx": "QQQ", "wSPYx": "SPY", "wNVDAx": "NVDA", "wTSLAx": "TSLA",
            "wMETAx": "META", "wGOOGLx": "GOOGL", "wAAPLx": "AAPL",
            "wMSFTx": "MSFT", "wAMZNx": "AMZN",
        }
        for spot_symbol, expected in cases.items():
            self.assertEqual(product_catalog._dn_underlying_key(spot_symbol), expected)
            # And the matching perp leg resolves to the same underlying.
            self.assertEqual(
                product_catalog._dn_underlying_key(f"{expected}-PERP"), expected
            )
        # Non-wrapped majors and an unrelated W-prefixed ticker are untouched.
        self.assertEqual(product_catalog._dn_underlying_key("WBTC"), "BTC")
        self.assertEqual(product_catalog._dn_underlying_key("WETH"), "ETH")
        self.assertEqual(product_catalog._dn_underlying_key("WTI-PERP"), "WTI")

    def test_parse_trade_intent_recognizes_listed_perp(self):
        names = product_catalog.list_perp_names(network="mainnet", refresh=True)
        self.assertTrue(names, "expected non-empty perp list from catalog")
        sym = names[0]
        intent = parse_trade_intent(
            f"long {sym} market with size 2 and leverage 10x",
            network="mainnet",
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("product"), sym)
        self.assertEqual(intent.get("missing"), [])

    def test_at_price_implies_limit_order(self):
        intent = parse_trade_intent("Long 2 SOL 5x at 83.0", network="mainnet")
        self.assertIsNotNone(intent)
        self.assertEqual(intent.get("order_type"), "limit")
        self.assertAlmostEqual(intent.get("limit_price"), 83.0)
        self.assertEqual(intent.get("size"), 2.0)
        self.assertEqual(intent.get("missing"), [])

    def test_catalog_loads_from_gateway_or_static_fallback(self):
        catalog = product_catalog.get_catalog(network="mainnet", refresh=True)
        self.assertIn("perps", catalog)
        self.assertGreater(len(catalog["perps"]), 0)
        self.assertIn("BTC", catalog["perps"])
        btc = catalog["perps"]["BTC"]
        self.assertEqual(btc["id"], PRODUCTS["BTC"]["id"])
        self.assertGreater(btc["max_leverage"], 0)
        pid = product_catalog.get_product_id("BTC-PERP", network="mainnet", refresh=True)
        self.assertEqual(pid, PRODUCTS["BTC"]["id"])

    def test_keyboard_lists_live_perp_labels(self):
        kb = trade_product_reply_kb(network="mainnet")
        labels = [btn.text for row in kb.keyboard for btn in row]
        sample = product_catalog.list_perp_names(network="mainnet", refresh=False)[:8]
        self.assertTrue(sample)
        self.assertTrue(any(s in labels for s in sample))

    def test_catalog_resolves_product_id_and_max_leverage(self):
        cap = product_catalog.get_product_max_leverage("BTC", network="mainnet", refresh=True)
        self.assertGreaterEqual(cap, 1)
        pid = product_catalog.get_product_id("BTC", network="mainnet", refresh=True)
        self.assertEqual(pid, PRODUCTS["BTC"]["id"])

    def test_x18_to_float_handles_str_int_and_invalid(self):
        # Used by min_size / size_increment / price_increment accessors.
        self.assertEqual(product_catalog._x18_to_float(str(100 * 10**18)), 100.0)
        self.assertEqual(product_catalog._x18_to_float(50 * 10**18), 50.0)
        self.assertIsNone(product_catalog._x18_to_float(None))
        self.assertIsNone(product_catalog._x18_to_float("not-a-number"))

    def test_min_quote_notional_accessor_returns_none_or_float(self):
        # Hits the live catalog path. Either the gateway returned a min_size
        # (giving a positive float) or the static fallback was used (giving None).
        # Either is acceptable; the contract is "never raise, never return junk".
        val = product_catalog.get_product_min_quote_notional_usd("BTC", network="mainnet")
        if val is not None:
            self.assertIsInstance(val, float)
            self.assertGreater(val, 0.0)

    def test_margin_fractions_worst_case_and_conservative_fallback(self):
        """LIQ-GUARD-MMF-EXPOSED: the catalog derives a maintenance-margin
        fraction (never read before). It is always a real buffer 0 < mmf < imf,
        and when the venue omits a maintenance weight the fallback is derived
        conservatively from imf."""
        # No venue weights -> conservative fallback from imf = 1/max_lev.
        imf, mmf, used_fallback = product_catalog._margin_fractions_from_weights(
            None, None, None, None, 50,
        )
        self.assertTrue(used_fallback)
        self.assertAlmostEqual(imf, 1 / 50)
        self.assertTrue(0 < mmf < imf)

        # x18 long/short weights present -> worst (larger) mmf/imf across sides.
        x18 = 10 ** 18
        long_i, long_m = int(0.95 * x18), int(0.97 * x18)    # imf .05, mmf .03
        short_i, short_m = int(1.10 * x18), int(1.06 * x18)  # imf .10, mmf .06
        imf2, mmf2, used2 = product_catalog._margin_fractions_from_weights(
            long_i, long_m, short_i, short_m, 20,
        )
        self.assertFalse(used2)
        self.assertAlmostEqual(imf2, 0.10)
        self.assertAlmostEqual(mmf2, 0.06)
        self.assertTrue(mmf2 < imf2)

    def test_static_catalog_row_carries_margin_fractions(self):
        cat = product_catalog._build_static_catalog()
        btc = cat["perps"]["BTC"]
        self.assertIn("imf", btc)
        self.assertIn("mmf", btc)
        self.assertTrue(0 < btc["mmf"] < btc["imf"])


if __name__ == "__main__":
    unittest.main()
