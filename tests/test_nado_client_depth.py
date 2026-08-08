"""Sized order-book depth — venue/nado_client.get_market_liquidity.

The venue has always served a full ladder at the cheapest rate-limit weight and
the bot never asked for it, so every strategy quoted blind to depth. These pin
the decode and the fail-open contract: depth is an enrichment, and a throttle or
an SDK change must degrade to "no book", never to an exception or a poisoned
cache.
"""
import unittest
from unittest.mock import patch

from src.nadobro.venue import nado_client as nc
from src.nadobro.venue.nado_client import NadoClient

X18 = 10 ** 18


def _lvl(price, size):
    """The SDK models a level as a 2-element list of x18 strings."""
    return [str(int(price * X18)), str(int(size * X18))]


class _FakeMarket:
    def __init__(self, bids, asks, raises=False):
        self._bids, self._asks, self._raises = bids, asks, raises
        self.calls = []

    def get_market_liquidity(self, product_id, depth):
        self.calls.append((product_id, depth))
        if self._raises:
            raise RuntimeError("engine unavailable")
        return type("Data", (), {"bids": self._bids, "asks": self._asks})()


class _FakeSDK:
    def __init__(self, market):
        self.market = market


def _client(market):
    client = NadoClient(private_key="0xabc", network="mainnet")
    client._initialized = True
    client.client = _FakeSDK(market)
    return client


class MarketLiquidityTests(unittest.TestCase):
    def setUp(self):
        nc._liquidity_cache.clear()
        self.addCleanup(nc._liquidity_cache.clear)

    def test_decodes_x18_into_floats(self):
        market = _FakeMarket([_lvl(100.5, 2.0)], [_lvl(101.5, 3.0)])
        with patch.object(NadoClient, "_gateway_allowed", return_value=True):
            book = _client(market).get_market_liquidity(2, depth=5)
        self.assertEqual(book["bids"], [[100.5, 2.0]])
        self.assertEqual(book["asks"], [[101.5, 3.0]])

    def test_levels_are_ordered_best_first(self):
        # Feed them scrambled: imbalance math depends on level 0 being the touch.
        market = _FakeMarket(
            [_lvl(99.0, 1.0), _lvl(100.0, 1.0), _lvl(98.0, 1.0)],
            [_lvl(103.0, 1.0), _lvl(101.0, 1.0), _lvl(102.0, 1.0)],
        )
        with patch.object(NadoClient, "_gateway_allowed", return_value=True):
            book = _client(market).get_market_liquidity(2)
        self.assertEqual([p for p, _ in book["bids"]], [100.0, 99.0, 98.0])
        self.assertEqual([p for p, _ in book["asks"]], [101.0, 102.0, 103.0])

    def test_zero_and_malformed_levels_are_dropped(self):
        market = _FakeMarket(
            [_lvl(100.0, 1.0), ["0", "0"], ["garbage"]],
            [_lvl(101.0, 1.0)],
        )
        with patch.object(NadoClient, "_gateway_allowed", return_value=True):
            book = _client(market).get_market_liquidity(2)
        self.assertEqual(book["bids"], [[100.0, 1.0]])

    def test_sdk_failure_returns_empty_sides_rather_than_raising(self):
        market = _FakeMarket([], [], raises=True)
        with patch.object(NadoClient, "_gateway_allowed", return_value=True):
            book = _client(market).get_market_liquidity(2)
        self.assertEqual(book, {"bids": [], "asks": [], "timestamp": 0.0})

    def test_gateway_throttle_returns_empty_without_calling_the_sdk(self):
        market = _FakeMarket([_lvl(100.0, 1.0)], [_lvl(101.0, 1.0)])
        with patch.object(NadoClient, "_gateway_allowed", return_value=False):
            book = _client(market).get_market_liquidity(2)
        self.assertEqual(book["bids"], [])
        self.assertEqual(market.calls, [])

    def test_uninitialized_client_returns_empty(self):
        client = NadoClient(private_key=None, network="mainnet")
        client._initialized = False
        client.client = None
        self.assertEqual(
            client.get_market_liquidity(2), {"bids": [], "asks": [], "timestamp": 0.0}
        )

    def test_populated_book_is_cached(self):
        market = _FakeMarket([_lvl(100.0, 1.0)], [_lvl(101.0, 1.0)])
        client = _client(market)
        with patch.object(NadoClient, "_gateway_allowed", return_value=True):
            client.get_market_liquidity(2)
            client.get_market_liquidity(2)
        self.assertEqual(len(market.calls), 1)

    def test_empty_book_is_not_cached(self):
        # A throttle or a blip is not an answer. Caching it would starve every
        # subsequent read for the whole TTL.
        market = _FakeMarket([], [])
        client = _client(market)
        with patch.object(NadoClient, "_gateway_allowed", return_value=True):
            client.get_market_liquidity(2)
            client.get_market_liquidity(2)
        self.assertEqual(len(market.calls), 2)

    def test_depth_is_part_of_the_cache_key(self):
        market = _FakeMarket([_lvl(100.0, 1.0)], [_lvl(101.0, 1.0)])
        client = _client(market)
        with patch.object(NadoClient, "_gateway_allowed", return_value=True):
            client.get_market_liquidity(2, depth=5)
            client.get_market_liquidity(2, depth=20)
        self.assertEqual([d for _, d in market.calls], [5, 20])


if __name__ == "__main__":
    unittest.main()
