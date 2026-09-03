"""get_balance: a Redis-served balance is FLAGGED when the venue read could not
be made (2026-09-02, F6) — so the portfolio sync can say "Cached · venue
throttled" instead of stamping a cached figure as freshly synced."""
from unittest import mock

from src.nadobro.venue.nado_client import NadoClient

CACHED = {"exists": True, "balances": {0: "1500.00"}}


def _client():
    c = NadoClient(private_key="0xabc", network="mainnet")
    c._initialized = True
    c.client = object()
    return c


def test_a_budget_denied_read_served_from_redis_is_flagged():
    c = _client()
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=False), \
         mock.patch.object(NadoClient, "_read_balance_cache", return_value=dict(CACHED)):
        out = c.get_balance(force=True)
    assert out["_cached"] is True and out["_cached_reason"] == "venue_throttled"
    assert out["balances"] == CACHED["balances"]


def test_a_denied_read_with_no_cache_is_still_the_legacy_shape():
    c = _client()
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=False), \
         mock.patch.object(NadoClient, "_read_balance_cache", return_value=None):
        assert c.get_balance(force=True) == {"exists": False, "balances": {}}


def test_a_ttl_read_through_hit_is_not_flagged():
    """Within NADO_BALANCE_CACHE_TTL_SECONDS the cached value IS the documented
    freshness — it must not read as throttled."""
    c = _client()
    with mock.patch.object(NadoClient, "_read_balance_cache", return_value=dict(CACHED)):
        out = c.get_balance()
    assert "_cached" not in out
