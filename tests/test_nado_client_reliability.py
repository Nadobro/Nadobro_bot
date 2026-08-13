import unittest
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.venue.nado_client import NadoClient
from src.nadobro.trading.trade_service import _normalize_net_positions


class _FakeResponse:
    def __init__(self, payload=None, raise_json=False, text="", status_code=200):
        self._payload = payload or {}
        self._raise_json = raise_json
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "application/json" if not raise_json else "text/html"}

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class MaskPayloadTests(unittest.TestCase):
    def test_mask_payload_redacts_hex_keeps_reason_and_caps_length(self):
        from src.nadobro.venue.nado_client import _mask_payload

        raw = (
            '{"reason":"ip_query_only","blocked":true,'
            '"sender":"0x1234567890abcdef1234567890abcdef12345678",'
            '"detail":"signer 0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef not authorized"}'
        )
        out = _mask_payload(raw)
        self.assertIn("ip_query_only", out)            # full reason preserved
        self.assertIn("0x<REDACTED>", out)             # addresses redacted
        self.assertNotIn("1234567890abcdef", out)
        self.assertNotIn("deadbeef", out)
        # short 0x values (e.g. 0x0) are left intact (only 12+ hex runs masked)
        self.assertEqual(_mask_payload("status 0x0 ok"), "status 0x0 ok")
        # length is capped
        capped = _mask_payload("z" * 5000, limit=600)
        self.assertTrue(capped.endswith("…(truncated)"))
        self.assertLess(len(capped), 5000)


class NadoClientReliabilityTests(unittest.TestCase):
    def test_build_order_appendix_sets_isolated_reduce_only_and_margin(self):
        appendix = NadoClient._build_order_appendix(
            1,
            isolated=True,
            reduce_only=True,
            margin_x6=12500000,
            builder_id=4321,
            builder_fee_rate=10,
        )
        self.assertEqual(appendix & 0xFF, 1)  # version
        self.assertEqual((appendix >> 8) & 0x1, 1)  # isolated
        self.assertEqual((appendix >> 9) & 0x3, 1)  # ioc
        self.assertEqual((appendix >> 11) & 0x1, 1)  # reduce_only
        self.assertEqual((appendix >> 38) & 0x3FF, 10)  # 1 bps fee rate
        self.assertEqual((appendix >> 48) & 0xFFFF, 4321)  # builder id
        self.assertEqual((appendix >> 64), 12500000)  # margin_x6

    def test_place_order_hard_fails_when_builder_id_missing(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        client._initialized = True
        client.client = object()
        with patch.dict("os.environ", {}, clear=True):
            result = client.place_order(product_id=2, size=0.1, price=100000.0, is_buy=True)
        self.assertEqual(result.get("success"), False)
        self.assertIn("NADO_BUILDER_ID", result.get("error", ""))

    def test_place_order_hard_fails_when_builder_id_is_zero(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        client._initialized = True
        client.client = object()
        with patch.dict("os.environ", {"NADO_BUILDER_ID": "0", "NADO_BUILDER_FEE_RATE": "10"}, clear=True):
            result = client.place_order(product_id=2, size=0.1, price=100000.0, is_buy=True)
        self.assertEqual(result.get("success"), False)
        self.assertIn("NADO_BUILDER_ID", result.get("error", ""))

    def test_place_order_hard_fails_when_builder_id_out_of_range(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        client._initialized = True
        client.client = object()
        with patch.dict("os.environ", {"NADO_BUILDER_ID": "70000", "NADO_BUILDER_FEE_RATE": "10"}, clear=True):
            result = client.place_order(product_id=2, size=0.1, price=100000.0, is_buy=True)
        self.assertEqual(result.get("success"), False)
        self.assertIn("NADO_BUILDER_ID", result.get("error", ""))

    def test_place_order_hard_fails_when_builder_fee_rate_not_one_bps(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        client._initialized = True
        client.client = object()
        with patch.dict("os.environ", {"NADO_BUILDER_ID": "123", "NADO_BUILDER_FEE_RATE": "11"}, clear=True):
            result = client.place_order(product_id=2, size=0.1, price=100000.0, is_buy=True)
        self.assertEqual(result.get("success"), False)
        self.assertIn("NADO_BUILDER_FEE_RATE", result.get("error", ""))

    def test_mint_nlp_passes_sender_subaccount(self):
        """The Nado SDK validates that MintNlpParams.sender is supplied —
        omitting it triggers the ``sender field required`` pydantic error
        observed in production. Verify the helper threads the linked
        subaccount through to the SDK."""
        client = NadoClient.from_address("0x" + "2" * 40, network="mainnet")
        client._initialized = True
        captured: dict = {}

        class _MintParams:
            def __init__(self, **kwargs):
                captured["params_kwargs"] = kwargs

        class _Market:
            def mint_nlp(self, params):
                captured["params"] = params
                return SimpleNamespace(digest="0xmintdigest")

        client.client = SimpleNamespace(market=_Market())
        with patch.dict(
            sys.modules,
            {
                "nado_protocol.engine_client.types.execute": SimpleNamespace(
                    MintNlpParams=_MintParams,
                    BurnNlpParams=object,
                )
            },
        ):
            result = client.mint_nlp(usdt0_amount=10.0)

        self.assertTrue(result.get("success"), msg=result)
        self.assertEqual(captured["params_kwargs"]["sender"], client.subaccount_hex)
        self.assertGreater(captured["params_kwargs"]["quoteAmount"], 0)
        self.assertEqual(result.get("digest"), "0xmintdigest")

    def test_burn_nlp_passes_sender_subaccount(self):
        """Same contract for burn_nlp: production logs showed `1 validation
        error for BurnNlpParams sender field required` because the client
        was constructing the params without `sender`. After the fix the
        subaccount hex MUST appear in the kwargs."""
        client = NadoClient.from_address("0x" + "3" * 40, network="mainnet")
        client._initialized = True
        captured: dict = {}

        class _BurnParams:
            def __init__(self, **kwargs):
                captured["params_kwargs"] = kwargs

        class _Market:
            def burn_nlp(self, params):
                captured["params"] = params
                return SimpleNamespace(digest="0xburndigest")

        client.client = SimpleNamespace(market=_Market())
        with patch.dict(
            sys.modules,
            {
                "nado_protocol.engine_client.types.execute": SimpleNamespace(
                    MintNlpParams=object,
                    BurnNlpParams=_BurnParams,
                )
            },
        ):
            result = client.burn_nlp(nlp_amount=2.5)

        self.assertTrue(result.get("success"), msg=result)
        self.assertEqual(captured["params_kwargs"]["sender"], client.subaccount_hex)
        self.assertGreater(captured["params_kwargs"]["nlpAmount"], 0)
        self.assertEqual(result.get("digest"), "0xburndigest")

    def test_burn_nlp_refuses_without_subaccount(self):
        """If subaccount hex is unavailable the helper must short-circuit
        instead of issuing an invalid request — burns without a sender
        are pre-routing errors and would have wasted an SDK call."""
        client = NadoClient.from_address("0x" + "4" * 40, network="mainnet")
        client._initialized = True
        client.client = object()
        client.subaccount_hex = ""

        result = client.burn_nlp(nlp_amount=1.0)
        self.assertFalse(result.get("success"))
        self.assertIn("Subaccount", result.get("error", ""))

    def test_builder_routing_disabled_on_testnet(self):
        """Builder routing is a mainnet-only contract — testnet orders must
        never carry a builder id (the venue rejects with error 2118
        ``Invalid builder``). Verify the helper returns ``(0, 0)`` without
        consulting env vars and without raising when env is unset."""
        from src.nadobro.config import get_nado_builder_routing_config

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_nado_builder_routing_config("testnet"), (0, 0))
        with patch.dict(
            "os.environ", {"NADO_BUILDER_ID": "9999", "NADO_BUILDER_FEE_RATE": "10"}, clear=True
        ):
            # Even with valid mainnet config, testnet must still skip it.
            self.assertEqual(get_nado_builder_routing_config("testnet"), (0, 0))
            self.assertEqual(get_nado_builder_routing_config("mainnet"), (9999, 10))

    def test_query_rest_retries_after_non_json_response(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        responses = [
            _FakeResponse(raise_json=True, text="<html>error</html>", status_code=502),
            _FakeResponse(payload={"status": "success", "data": {"ok": True}}),
        ]

        with patch("src.nadobro.venue.nado_client._rest_session.get", side_effect=responses), patch(
            "src.nadobro.venue.nado_client.time.sleep", return_value=None
        ):
            data = client._query_rest("symbols")

        self.assertIsNotNone(data)
        self.assertEqual(data.get("status"), "success")
        self.assertEqual(data.get("data", {}).get("ok"), True)

    def test_query_rest_uses_post_for_multi_product_queries(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        response = _FakeResponse(payload={"status": "success", "data": {"market_prices": []}})
        with patch("src.nadobro.venue.nado_client._rest_session.get") as mock_get, patch(
            "src.nadobro.venue.nado_client._rest_session.post", return_value=response
        ) as mock_post:
            data = client._query_rest("market_prices", {"product_ids": [1, 2]})

        self.assertEqual(data.get("status"), "success")
        mock_get.assert_not_called()
        mock_post.assert_called_once()

    def test_from_address_client_exposes_acting_user_id_for_gateway_gate(self):
        """Regression: the gateway-budget gate reads ``acting_user_id`` on
        every REST call, but ``from_address`` builds the client via
        ``cls.__new__`` (bypassing ``__init__``). A missing attribute crashed
        every read-only client (alert price-check, price tracker, catalog)
        with ``'NadoClient' object has no attribute 'acting_user_id'`` before
        any network I/O — flooding logs every 5s."""
        client = NadoClient.from_address("0x" + "0" * 40, network="testnet")
        self.assertIsNone(client.acting_user_id)
        # Must not raise AttributeError regardless of token-bucket verdict.
        try:
            allowed = client._gateway_allowed()
        finally:
            client._gateway_release()
        self.assertIsInstance(allowed, bool)
        # The class-level default also protects raw ``__new__`` instances.
        self.assertIsNone(NadoClient.__new__(NadoClient).acting_user_id)

    def test_get_market_price_cache_only_never_queries(self):
        import src.nadobro.venue.nado_client as nc

        client = NadoClient.from_address("0x" + "a" * 40, network="mainnet")
        client._initialized = True
        engine = SimpleNamespace(get_market_price=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("gateway")))
        client.client = SimpleNamespace(context=SimpleNamespace(engine_client=engine))
        with nc._caches_lock:
            nc._price_cache.clear()
        self.assertEqual(client.get_market_price(2, cache_only=True), {"bid": 0, "ask": 0, "mid": 0})
        with nc._caches_lock:
            nc._price_cache["mainnet:2"] = {"data": {"bid": 1, "ask": 3, "mid": 2}, "ts": 0}
        self.assertEqual(client.get_market_price(2, cache_only=True)["mid"], 2)

    def test_get_funding_rate_cache_only_never_queries(self):
        import src.nadobro.venue.nado_client as nc

        client = NadoClient.from_address("0x" + "b" * 40, network="mainnet")
        client.get_perp_funding_rates = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("indexer"))
        with nc._caches_lock:
            nc._FUNDING_CACHE.clear()
        self.assertEqual(client.get_all_funding_rates(cache_only=True), {})
        self.assertIsNone(client.get_funding_rate(4, cache_only=True))

    def test_get_all_market_prices_serves_cache_without_fanout_when_blocked(self):
        """Regression (gateway contract): when the batched ``market_prices``
        request is unavailable AND the gateway is throttling/blocked, callers
        must serve cached data — never fan out to one REST call per product,
        which amplifies load exactly when we must back off."""
        import src.nadobro.venue.nado_client as nc
        from src.nadobro.venue import gateway_budget

        client = NadoClient.from_address("0x" + "0" * 40, network="testnet")

        def _tripwire(*_a, **_k):
            raise AssertionError("per-product fanout ran while gateway was blocked")

        client.get_market_price = _tripwire

        # Cache present -> serve it, no fanout.
        with patch.object(client, "_query_rest", return_value=None), patch.object(
            gateway_budget, "is_gateway_blocked", return_value=True
        ):
            with nc._caches_lock:
                nc._ALL_PRICES_CACHE["testnet"] = {
                    "data": {"BTC": {"bid": 1.0, "ask": 2.0, "mid": 1.5}},
                    "ts": 0.0,
                }
            served = client.get_all_market_prices()
        self.assertEqual(served, {"BTC": {"bid": 1.0, "ask": 2.0, "mid": 1.5}})

        # Cache empty -> return {}, still no fanout.
        with patch.object(client, "_query_rest", return_value=None), patch.object(
            gateway_budget, "is_gateway_blocked", return_value=True
        ):
            with nc._caches_lock:
                nc._ALL_PRICES_CACHE.pop("testnet", None)
            empty = client.get_all_market_prices()
        self.assertEqual(empty, {})

    def test_get_all_open_orders_sweeps_spot_products(self):
        """Portfolio open orders must include Volume-bot spot orders, not only perps."""
        from src.nadobro.venue import product_catalog as pc

        client = NadoClient.from_address("0x" + "1" * 40, network="mainnet")
        client._initialized = True
        captured = {}

        class _Engine:
            def get_subaccount_multi_products_open_orders(self, product_ids, sender):
                captured["product_ids"] = list(product_ids)
                captured["sender"] = sender
                return SimpleNamespace(product_orders=[
                    SimpleNamespace(product_id=2, orders=[]),
                    SimpleNamespace(product_id=77, orders=[
                        SimpleNamespace(
                            digest="0xspotdigest",
                            amount=str(1 * 10**18),
                            price_x18=str(700 * 10**18),
                        )
                    ]),
                ])

        client.client = SimpleNamespace(context=SimpleNamespace(engine_client=_Engine()))
        spot_catalog = {
            "spots": {
                "USDT0": {"id": 1, "symbol": "USDT0", "base": "USDT0"},
                "WGOOGLX": {"id": 77, "symbol": "WGOOGLX", "base": "WGOOGLX"},
            },
            "by_id": {1: "USDT0", 77: "WGOOGLX"},
            "aliases": {},
        }
        nado_mod = ModuleType("nado_protocol")
        utils_mod = ModuleType("nado_protocol.utils")
        math_mod = ModuleType("nado_protocol.utils.math")
        math_mod.from_x18 = lambda value: value / 1e18
        nado_mod.utils = utils_mod
        utils_mod.math = math_mod

        with patch("src.nadobro.venue.nado_client.get_perp_products", return_value=["BTC"]), \
             patch("src.nadobro.venue.nado_client.get_product_id", return_value=2), \
             patch.object(pc, "get_catalog", return_value={"perps": {}, "by_id": {}, "aliases": {}}), \
             patch.object(pc, "get_spot_catalog", return_value=spot_catalog), \
             patch.object(client, "_gateway_allowed", return_value=True), \
             patch.object(client, "_gateway_release"), \
             patch.dict(sys.modules, {
                 "nado_protocol": nado_mod,
                 "nado_protocol.utils": utils_mod,
                 "nado_protocol.utils.math": math_mod,
             }):
            rows = client.get_all_open_orders()

        self.assertEqual(captured["sender"], client.subaccount_hex)
        self.assertIn(2, captured["product_ids"])
        self.assertIn(77, captured["product_ids"])
        self.assertNotIn(1, captured["product_ids"])
        self.assertEqual(rows[0]["product_id"], 77)
        self.assertEqual(rows[0]["product_name"], "WGOOGLX")
        self.assertEqual(rows[0]["side"], "LONG")

    def test_extract_positions_from_rest_payload_supports_camel_case_fields(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        payload = {
            "perpPositions": [
                {
                    "productId": 999,
                    "balance": {
                        "amountX18": "3000000000000000000",
                        "vQuoteBalanceX18": "180000000000000000000",
                    },
                    "entryPriceX18": "60000000000000000000",
                    "isLong": True,
                }
            ]
        }
        positions = client._extract_positions_from_rest_payload(payload)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["product_id"], 999)
        self.assertEqual(positions[0]["side"], "LONG")
        self.assertGreater(positions[0]["amount"], 0)

    def test_get_all_positions_unwraps_nested_subaccount_info_payload(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        client._initialized = False
        client.client = None
        with patch.object(
            client,
            "_query_rest",
            return_value={
                "status": "success",
                "data": {
                    "subaccountInfo": {
                        "perpPositions": [
                            {
                                "productId": 111,
                                "balance": {"amountX18": "1000000000000000000"},
                                "entryPriceX18": "10000000000000000000",
                                "side": "LONG",
                            }
                        ]
                    }
                },
            },
        ):
            positions = client.get_all_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["product_id"], 111)

    def test_extract_positions_from_rest_payload_supports_balance_amount_fields(self):
        client = NadoClient(private_key="0xabc", network="mainnet")
        payload = {
            "perpBalances": [
                {
                    "productId": 222,
                    "balance": {
                        "balanceAmountX18": "-6000000000000000000",
                        "vQuoteBalanceX18": "-617880000000000000000",
                    },
                    "entryPriceX18": "102980000000000000000",
                    "side": "SHORT",
                }
            ]
        }
        positions = client._extract_positions_from_rest_payload(payload)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["product_id"], 222)
        self.assertEqual(positions[0]["side"], "SHORT")
        self.assertAlmostEqual(float(positions[0]["amount"]), 6.0, places=6)
        self.assertAlmostEqual(float(positions[0]["signed_amount"]), -6.0, places=6)

    def test_extract_positions_from_sdk_info_unwraps_nested_container(self):
        client = NadoClient(private_key="0xabc", network="mainnet")

        class _Balance:
            amountX18 = "-2000000000000000000"

        class _Position:
            productId = 333
            balance = _Balance()
            entryPriceX18 = "50000000000000000000"
            side = "SHORT"

        class _Container:
            perpBalances = [_Position()]

        class _Wrapper:
            subaccount_info = _Container()

        positions = client._extract_positions_from_sdk_info(_Wrapper())
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["product_id"], 333)
        self.assertEqual(positions[0]["side"], "SHORT")
        self.assertAlmostEqual(float(positions[0]["signed_amount"]), -2.0, places=6)

    def test_normalize_net_positions_uses_signed_amount_when_amount_missing(self):
        net = _normalize_net_positions(
            [
                {
                    "product_id": 444,
                    "product_name": "WTI-PERP",
                    "signed_amount": -6.0,
                    "amount": 0.0,
                    "side": "",
                    "price": 103.0,
                }
            ]
        )
        self.assertIn(444, net)
        self.assertAlmostEqual(float(net[444]["signed_amount"]), -6.0, places=6)

    def test_normalize_net_positions_splits_isolated_subaccounts(self):
        net = _normalize_net_positions(
            [
                {
                    "product_id": 1,
                    "product_name": "AAPL-PERP",
                    "signed_amount": 0.5,
                    "amount": 0.5,
                    "side": "LONG",
                    "price": 200.0,
                    "subaccount": "0xiso1",
                },
                {
                    "product_id": 1,
                    "product_name": "AAPL-PERP",
                    "signed_amount": 0.2,
                    "amount": 0.2,
                    "side": "LONG",
                    "price": 199.0,
                    "subaccount": "0xiso2",
                },
            ]
        )
        self.assertIn(1, net)
        self.assertIn("legs", net[1])
        self.assertEqual(len(net[1]["legs"]), 2)

    def test_isolated_subaccounts_response_parsing(self):
        from src.nadobro.venue.nado_archive import (
            _isolated_subaccounts_list_from_response,
            isolated_subaccount_from_row,
            query_isolated_subaccounts_for_parent,
        )

        rows = _isolated_subaccounts_list_from_response(
            {"isolated_subaccounts": [{"isolated_subaccount": "0xabc", "product_id": 9}]}
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("product_id"), 9)
        self.assertEqual(isolated_subaccount_from_row({"subaccountHex": "0xchild"}, "0xparent"), "0xchild")
        self.assertEqual(isolated_subaccount_from_row({"subaccount": "0xparent"}, "0xparent"), "")

        captured = {}

        def _fake_post(_url, payload):
            captured["payload"] = payload
            return {"isolated_subaccounts": []}

        # bytes32: 20-byte address + 12-byte subaccount identifier.
        parent_hex = "0x" + "ab" * 20 + "00" * 12
        with patch("src.nadobro.venue.nado_archive._post", side_effect=_fake_post):
            query_isolated_subaccounts_for_parent("mainnet", parent_hex)

        # Nado archive expects ``subaccount`` as a 32-element ``u8`` array
        # decoded from the hex (not a hex string and not a list of hex strings).
        sent = captured["payload"]["isolated_subaccounts"]["subaccount"]
        self.assertIsInstance(sent, list)
        self.assertEqual(len(sent), 32)
        self.assertTrue(all(isinstance(b, int) and 0 <= b <= 255 for b in sent))
        self.assertEqual(sent[:20], [0xAB] * 20)
        self.assertEqual(sent[20:], [0] * 12)

    def test_archive_unwraps_orders_under_data(self):
        from src.nadobro.venue.nado_archive import _orders_list_from_archive_response

        row = {"digest": "0xabc", "base_filled": 1e18, "quote_filled": 100e18}
        self.assertEqual(
            _orders_list_from_archive_response({"data": {"orders": [row]}}),
            [row],
        )

    def test_parse_order_reads_camelCase_fill_fields(self):
        from src.nadobro.venue.nado_archive import _parse_order

        o = _parse_order(
            {
                "digest": "0x1",
                "baseFilled": 5e18,
                "quoteFilled": 500e18,
            }
        )
        self.assertTrue(o["is_filled"])
        self.assertGreater(o["fill_size"], 0)


class NadoClientPortfolioWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = NadoClient.from_address("0x" + "1" * 40, network="testnet")
        self.client._initialized = True
        self.client.client = SimpleNamespace(
            context=SimpleNamespace(indexer_client=SimpleNamespace(), trigger_client=SimpleNamespace()),
            market=SimpleNamespace(),
        )

    async def test_get_matches_uses_indexer_params_and_returns_dicts(self):
        captured = {}

        class _Params:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _Indexer:
            def get_matches(self, params):
                captured["params"] = params
                return SimpleNamespace(matches=[SimpleNamespace(digest="0xabc", submission_idx="10")])

        self.client.client.context.indexer_client = _Indexer()
        with patch.dict(
            sys.modules,
            {
                "nado_protocol.indexer_client.types.query": SimpleNamespace(
                    IndexerMatchesParams=_Params
                )
            },
        ):
            rows = await self.client.get_matches(product_ids=[1], idx="7", limit=50, max_time=123)

        self.assertEqual(rows, [{"digest": "0xabc", "submission_idx": "10"}])
        self.assertEqual(captured["subaccounts"], [self.client.subaccount_hex])
        self.assertEqual(captured["product_ids"], [1])
        self.assertEqual(captured["idx"], 7)
        self.assertEqual(captured["limit"], 50)
        self.assertEqual(captured["max_time"], 123)

    async def test_get_interest_and_funding_payments_flattens_payment_types(self):
        captured = {}

        class _Params:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _Indexer:
            def get_interest_and_funding_payments(self, params):
                captured["params"] = params
                return SimpleNamespace(
                    funding_payments=[{"amount": "100", "idx": "9"}],
                    interest_payments=[{"amount": "-5", "idx": "8"}],
                    next_idx="7",
                )

        self.client.client.context.indexer_client = _Indexer()
        with patch.dict(
            sys.modules,
            {
                "nado_protocol.indexer_client.types.query": SimpleNamespace(
                    IndexerInterestAndFundingParams=_Params
                )
            },
        ):
            rows = await self.client.get_interest_and_funding_payments(
                product_ids=[2], idx="99", limit=25
            )

        self.assertEqual(rows[0]["type"], "funding")
        self.assertEqual(rows[1]["type"], "interest")
        self.assertEqual(captured["subaccount"], self.client.subaccount_hex)
        self.assertEqual(captured["product_ids"], [2])
        self.assertEqual(captured["max_idx"], "99")
        self.assertEqual(captured["limit"], 25)

    async def test_calculate_account_summary_uses_margin_manager_from_client(self):
        captured = {}

        class _Manager:
            @classmethod
            def from_client(cls, client, **kwargs):
                captured["client"] = client
                captured.update(kwargs)
                return cls()

            def calculate_account_summary(self):
                return SimpleNamespace(portfolio_value="123", cross_positions=[], isolated_positions=[])

        with patch.dict(
            sys.modules,
            {"nado_protocol.utils.margin_manager": SimpleNamespace(MarginManager=_Manager)},
        ):
            summary = await self.client.calculate_account_summary(ts=456)

        self.assertEqual(summary["portfolio_value"], "123")
        self.assertIs(captured["client"], self.client.client)
        self.assertEqual(captured["subaccount"], self.client.subaccount_hex)
        self.assertEqual(captured["snapshot_timestamp"], 456)
        self.assertTrue(captured["include_indexer_events"])

    async def test_cancel_orders_uses_multi_digest_params(self):
        captured = {}

        class _Params:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _Market:
            def cancel_orders(self, params):
                captured["params"] = params
                return SimpleNamespace(status="success")

        self.client.client.market = _Market()
        with patch.dict(
            sys.modules,
            {
                "nado_protocol.engine_client.types.execute": SimpleNamespace(
                    CancelOrdersParams=_Params,
                    # _dispatch_execute imports ExecuteResponse unconditionally
                    # (used only on the ws-v2 path; v2 is off by default here).
                    ExecuteResponse=SimpleNamespace,
                )
            },
        ):
            result = await self.client.cancel_orders(product_id=3, digests=["0xaaa", "0xbbb"])

        self.assertTrue(result["success"])
        self.assertEqual(result["cancelled"], 2)
        self.assertEqual(captured["sender"], self.client.subaccount_hex)
        self.assertEqual(captured["productIds"], [3])
        self.assertEqual(captured["digests"], ["0xaaa", "0xbbb"])

    async def test_get_trigger_orders_uses_trigger_client(self):
        captured = {}

        class _Tx:
            def __init__(self, **kwargs):
                captured["tx_kwargs"] = kwargs

        class _Params:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _Status:
            WAITING_PRICE = "waiting_price"
            WAITING_DEPENDENCY = "waiting_dependency"
            TWAP_EXECUTING = "twap_executing"

        class _TriggerClient:
            def list_trigger_orders(self, params):
                captured["params"] = params
                return SimpleNamespace(data=SimpleNamespace(orders=[SimpleNamespace(order={"digest": "0xtrg"})]))

        self.client.client.context.trigger_client = _TriggerClient()
        with patch.dict(
            sys.modules,
            {
                "nado_protocol.trigger_client.types.query": SimpleNamespace(
                    ListTriggerOrdersParams=_Params,
                    ListTriggerOrdersTx=_Tx,
                    TriggerOrderStatusType=_Status,
                )
            },
        ):
            rows = await self.client.get_trigger_orders(product_ids=[1], limit=10)

        self.assertEqual(rows, [{"order": {"digest": "0xtrg"}}])
        self.assertEqual(captured["tx_kwargs"]["sender"], self.client.subaccount_hex)
        self.assertEqual(captured["product_ids"], [1])
        self.assertEqual(captured["limit"], 10)

    async def test_cancel_trigger_orders_uses_trigger_client(self):
        captured = {}

        class _Params:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        class _TriggerClient:
            def cancel_trigger_orders(self, params):
                captured["params"] = params
                return SimpleNamespace(status="success")

        self.client.client.context.trigger_client = _TriggerClient()
        with patch.dict(
            sys.modules,
            {
                "nado_protocol.trigger_client.types.execute": SimpleNamespace(
                    CancelTriggerOrdersParams=_Params
                )
            },
        ):
            result = await self.client.cancel_trigger_orders(product_id=3, digests=["0xaaa"])

        self.assertTrue(result["success"])
        self.assertEqual(captured["productIds"], [3])
        self.assertEqual(captured["digests"], ["0xaaa"])


class NadoNlpVaultClientTests(unittest.TestCase):
    _SHARED_MIN = "100000000000000000000"  # every Nado spot shares this min_size

    @staticmethod
    def _all_products_payload():
        z = {"interest_floor_x18": "0", "interest_small_cap_x18": "0",
             "interest_large_cap_x18": "0", "withdraw_fee_x18": "0"}
        nonzero = {"interest_floor_x18": "10000000000000000",
                   "interest_small_cap_x18": "40000000000000000",
                   "interest_large_cap_x18": "1000000000000000000",
                   "withdraw_fee_x18": "10000000000000"}
        return {"data": {"spot_products": [
            {"product_id": 0, "book_info": {"min_size": "0"}, "config": dict(z)},        # USDT0 quote
            {"product_id": 1, "book_info": {"min_size": NadoNlpVaultClientTests._SHARED_MIN}, "config": dict(nonzero)},  # kBTC
            {"product_id": 3, "book_info": {"min_size": NadoNlpVaultClientTests._SHARED_MIN}, "config": dict(nonzero)},  # wETH
            {"product_id": 11, "book_info": {"min_size": NadoNlpVaultClientTests._SHARED_MIN}, "config": dict(z)},       # NLP vault token
        ]}}

    def test_resolve_nlp_product_id_picks_vault_token_not_first_spot(self):
        """Regression: every spot shares min_size=100e18, so the old
        ``min_size.startswith`` scan returned the FIRST match (kBTC, pid 1, ~$64k)
        and the vault mis-valued kBTC dust as a phantom NLP position. The resolver
        must pick the zero-interest / zero-withdraw-fee vault token (pid 11)."""
        client = NadoClient.from_address("0x" + "4" * 40, network="mainnet")
        client._initialized = True
        client._nlp_product_id = None
        client._query_rest = lambda qt, extra=None: self._all_products_payload()
        with patch.dict("os.environ", {"NADO_NLP_PRODUCT_ID": ""}, clear=False):
            self.assertEqual(client.resolve_nlp_product_id(), 11)

    def test_resolve_nlp_product_id_unique_candidate_without_default(self):
        """If the known per-network id isn't present but the config uniquely
        identifies one vault token, trust that one."""
        client = NadoClient.from_address("0x" + "7" * 40, network="mainnet")
        client._initialized = True
        client._nlp_product_id = None
        payload = self._all_products_payload()
        # Renumber the vault token to 99 so the mainnet default (11) is absent.
        for sp in payload["data"]["spot_products"]:
            if sp["product_id"] == 11:
                sp["product_id"] = 99
        client._query_rest = lambda qt, extra=None: payload
        with patch.dict("os.environ", {"NADO_NLP_PRODUCT_ID": ""}, clear=False):
            self.assertEqual(client.resolve_nlp_product_id(), 99)

    def test_get_max_nlp_mintable_includes_product_id(self):
        """The gateway requires product_id (sender alone returned 0, which the UI
        showed as 'deposits closed'). Verify it's threaded and the x18 amount parsed."""
        client = NadoClient.from_address("0x" + "5" * 40, network="mainnet")
        client._initialized = True
        captured: dict = {}

        def fake_query(qt, extra=None):
            captured["qt"] = qt
            captured["extra"] = dict(extra or {})
            return {"data": {"max_quote_amount": str(int(round(211.57 * 1e18)))}}

        client._query_rest = fake_query
        out = client.get_max_nlp_mintable(spot_leverage=False, product_id=11)
        self.assertEqual(captured["qt"], "max_nlp_mintable")
        self.assertEqual(int(captured["extra"]["product_id"]), 11)
        self.assertEqual(captured["extra"]["spot_leverage"], "false")
        self.assertEqual(captured["extra"]["sender"], client.subaccount_hex)
        self.assertAlmostEqual(out["max_mintable_usdt0"], 211.57, places=2)

    def test_get_max_nlp_mintable_resolves_product_id_when_omitted(self):
        client = NadoClient.from_address("0x" + "6" * 40, network="mainnet")
        client._initialized = True
        client._nlp_product_id = 11  # pre-seed resolver cache
        captured: dict = {}

        def fake_query(qt, extra=None):
            captured["extra"] = dict(extra or {})
            return {"data": {"max_quote_amount": "0"}}

        client._query_rest = fake_query
        client.get_max_nlp_mintable(spot_leverage=False)
        self.assertEqual(int(captured["extra"]["product_id"]), 11)


if __name__ == "__main__":
    unittest.main()


def test_align_price_post_only_never_rounds_into_the_book():
    """Post-only alignment must round AWAY from crossing: buys floor, sells
    ceil. HALF_UP could push a buy up onto the ask and the venue rejects the
    crossing post-only order (the volume bot's maker slices)."""
    from src.nadobro.venue.nado_client import NadoClient

    # increment 0.5: 100.3 would HALF_UP to 100.5 for a buy — must floor to 100.0
    assert NadoClient._align_price_to_increment(100.3, 0.5, True, "post_only") == 100.0
    # sell at 100.3 must ceil to 100.5 (away from the bids), not floor
    assert NadoClient._align_price_to_increment(100.3, 0.5, False, "post_only") == 100.5
    # ioc semantics unchanged: buy rounds up (aggressive), sell rounds down
    assert NadoClient._align_price_to_increment(100.3, 0.5, True, "ioc") == 100.5
    assert NadoClient._align_price_to_increment(100.3, 0.5, False, "ioc") == 100.0
    # default orders keep HALF_UP
    assert NadoClient._align_price_to_increment(100.3, 0.5, True, "default") == 100.5


def test_nlp_locked_balances_distinguishes_throttled_from_zero():
    """A throttled weight-20 query returns None from _query_rest. Booking that
    as "0 locked / 0 unlocked" made get_nlp_position fall back to the raw spot
    NLP balance and label ALL of it unlocked, so a 100% withdraw asked the venue
    to burn locked tokens -> error 2096. Failure must be UNKNOWN (ok=False)."""
    from unittest.mock import patch

    from src.nadobro.venue.nado_client import NadoClient

    client = NadoClient.from_address("0x" + "1" * 40, "mainnet")

    with patch.object(NadoClient, "_query_rest", return_value=None):
        throttled = client.get_nlp_locked_balances()
    assert throttled["ok"] is False

    payload = {
        "status": "success",
        "data": {
            "balance_locked": {"balance": {"amount": "2000000000000000000"}},
            "balance_unlocked": {"balance": {"amount": "777688000000000000"}},
            "locked_balances": [],
        },
    }
    with patch.object(NadoClient, "_query_rest", return_value=payload):
        answered = client.get_nlp_locked_balances()
    assert answered["ok"] is True
    assert answered["balance_unlocked"] == 0.777688
    # Exact wei preserved: a max burn must not go through float.
    assert answered["balance_unlocked_x18"] == 777688000000000000


def test_get_nlp_position_never_claims_unknown_balance_is_unlocked():
    """Spot-balance fallback is display-only: with no authoritative locked
    read, nothing may be reported as withdrawable."""
    from unittest.mock import patch

    from src.nadobro.venue.nado_client import NadoClient

    client = NadoClient.from_address("0x" + "1" * 40, "mainnet")
    with patch.object(NadoClient, "_query_rest", return_value=None), \
         patch.object(NadoClient, "resolve_nlp_product_id", return_value=11), \
         patch.object(NadoClient, "_spot_balance_amount", return_value=2.777688), \
         patch.object(NadoClient, "_nlp_oracle_price", return_value=1.0):
        pos = client.get_nlp_position()
    assert pos["lp_balance"] == 2.777688      # still displayed
    assert pos["unlocked_known"] is False     # but NOT presented as burnable
    assert pos["lp_unlocked"] == 0.0
    assert pos["lp_unlocked_x18"] == 0
