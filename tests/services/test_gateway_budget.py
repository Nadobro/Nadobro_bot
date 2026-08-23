"""Tests for the unified Nado gateway budget layer."""
from __future__ import annotations

import importlib
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    """Clear the gateway module's process-global state after each test.

    ``record_gateway_failure`` opens a rate-limit circuit keyed by host that
    otherwise leaks into other test files (which share the same host name) and
    makes ``cf_request`` short-circuit to None. Reset on the *current* module in
    sys.modules so reload-based tests are covered too.
    """
    yield
    mod = sys.modules.get("src.nadobro.venue.gateway_budget")
    if mod is None:
        return
    with mod._lock:
        mod._gateway_rl.clear()
        mod._user_buckets.clear()
        mod._user_inflight.clear()
        mod._wallet_buckets.clear()
        mod._write_ban.clear()


def _fresh_module(monkeypatch):
    monkeypatch.setenv("NADO_USER_GATEWAY_RPS", "100")
    monkeypatch.setenv("NADO_USER_GATEWAY_BURST", "100")
    monkeypatch.setenv("NADO_USER_MAX_INFLIGHT", "4")
    monkeypatch.setenv("NADO_GATEWAY_RL_THRESHOLD", "2")
    monkeypatch.setenv("NADO_GATEWAY_RL_WINDOW_SECONDS", "30")
    monkeypatch.setenv("NADO_GATEWAY_RL_COOLDOWN_SECONDS", "60")
    from src.nadobro.venue import gateway_budget

    importlib.reload(gateway_budget)
    return gateway_budget


def test_try_acquire_releases_on_host_throttle(monkeypatch):
    mod = _fresh_module(monkeypatch)
    url = "https://gateway.mainnet.nado.xyz/query"

    def _deny(_url, *, cost=1.0, max_wait=None):
        return False

    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", _deny)
    assert mod.try_acquire(url, user_id=42) is False
    snap = mod.snapshot()
    assert 42 not in snap["user_inflight"]


def test_query_weight_forwarded_to_host_bucket(monkeypatch):
    """A query's documented weight is charged against the host bucket as cost."""
    mod = _fresh_module(monkeypatch)
    url = "https://gateway.mainnet.nado.xyz/query"
    seen: dict = {}

    def _record(_url, *, cost=1.0, max_wait=None):
        seen["cost"] = cost
        return True

    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", _record)
    assert mod.try_acquire(url, user_id=1, weight=10) is True
    assert seen["cost"] == 10
    mod.release(1)


def test_per_user_bucket_drains_by_weight(monkeypatch):
    """The per-user fair-share bucket consumes weight, not a flat 1/call."""
    monkeypatch.setenv("NADO_USER_GATEWAY_RPS", "0.0001")  # negligible refill
    monkeypatch.setenv("NADO_USER_GATEWAY_BURST", "10")
    monkeypatch.setenv("NADO_USER_MAX_INFLIGHT", "100")
    import importlib
    from src.nadobro.venue import gateway_budget as mod
    importlib.reload(mod)
    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", lambda *_a, **_k: True)
    url = "https://gateway.mainnet.nado.xyz/query"
    # First weight-8 call fits in the burst-10 bucket; the next weight-8 call
    # cannot (only ~2 tokens left) and is denied with a tiny wait budget.
    assert mod.try_acquire(url, user_id=99, weight=8, max_wait=0.01) is True
    assert mod.try_acquire(url, user_id=99, weight=8, max_wait=0.01) is False


def test_execute_lane_uses_wallet_budget(monkeypatch):
    """kind='execute' charges a per-wallet bucket, independent of host/IP."""
    monkeypatch.setenv("NADO_WALLET_EXECUTE_RPS", "0.0001")
    monkeypatch.setenv("NADO_WALLET_EXECUTE_BURST", "5")
    import importlib
    from src.nadobro.venue import gateway_budget as mod
    importlib.reload(mod)
    # Host throttle must NOT be consulted for executes.
    def _boom(*_a, **_k):
        raise AssertionError("execute lane must not touch the host bucket")
    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", _boom)
    url = "https://gateway.mainnet.nado.xyz/execute"
    wallet = "0xabc"
    assert mod.try_acquire(url, kind="execute", wallet=wallet, weight=5, max_wait=0.01) is True
    assert mod.try_acquire(url, kind="execute", wallet=wallet, weight=5, max_wait=0.01) is False
    # A different wallet has its own budget.
    assert mod.try_acquire(url, kind="execute", wallet="0xdef", weight=5, max_wait=0.01) is True


def test_execute_lane_needs_no_release(monkeypatch):
    """Executes take no in-flight slot, so they never need release()."""
    mod = _fresh_module(monkeypatch)
    url = "https://gateway.mainnet.nado.xyz/execute"
    assert mod.try_acquire(url, kind="execute", wallet="0x1", weight=1) is True
    snap = mod.snapshot()
    assert snap["user_inflight"] == {}


def test_per_user_inflight_cap(monkeypatch):
    mod = _fresh_module(monkeypatch)
    url = "https://gateway.mainnet.nado.xyz/query"
    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", lambda *_a, **_k: True)
    assert mod.try_acquire(url, user_id=7) is True
    assert mod.try_acquire(url, user_id=7) is True
    assert mod.try_acquire(url, user_id=7) is True
    assert mod.try_acquire(url, user_id=7) is True
    assert mod.try_acquire(url, user_id=7) is False
    mod.release(7)
    assert mod.try_acquire(url, user_id=7) is True
    mod.release(7)
    mod.release(7)
    mod.release(7)
    mod.release(7)


def test_rate_limit_circuit_opens(monkeypatch):
    mod = _fresh_module(monkeypatch)
    url = "https://gateway.mainnet.nado.xyz/query"
    mod.record_gateway_failure(url, 'Too many requests "error_code":1000')
    mod.record_gateway_failure(url, "error_code=1000")
    assert mod.is_gateway_rate_limited(url) is True
    assert mod.is_gateway_blocked(url) is True


def test_is_rate_limit_error():
    from src.nadobro.venue.gateway_budget import is_rate_limit_error

    assert is_rate_limit_error('Too many requests "error_code":1000')
    assert is_rate_limit_error(Exception("error_code=1000"))
    assert not is_rate_limit_error("connection reset")


def test_write_circuit_blocks_executes_not_queries(monkeypatch):
    """An ip_query_only ban parks executes but leaves the read lane open.

    This is the core of the fix: the venue's per-IP write-ban (reads allowed,
    orders rejected) must short-circuit the execute lane so we stop firing
    doomed orders into the ban, while queries keep flowing.
    """
    mod = _fresh_module(monkeypatch)
    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", lambda *_a, **_k: True)
    query_url = "https://gateway.mainnet.nado.xyz/query"
    execute_url = "https://gateway.mainnet.nado.xyz/execute"

    # Before the ban, an execute is allowed.
    assert mod.try_acquire(execute_url, kind="execute", wallet="0xabc", weight=1) is True

    # Venue returns ip_query_only -> arm the write circuit.
    mod.record_ip_query_only(execute_url)
    assert mod.is_write_blocked(execute_url) is True

    # Executes are now parked (host shares the same name).
    assert mod.try_acquire(execute_url, kind="execute", wallet="0xabc", weight=1) is False
    # ...but the read/query lane is unaffected.
    assert mod.try_acquire(query_url, user_id=1, weight=1) is True
    mod.release(1)


def test_write_circuit_clears_on_success(monkeypatch):
    """A successful write clears the ban so executes resume immediately."""
    mod = _fresh_module(monkeypatch)
    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", lambda *_a, **_k: True)
    url = "https://gateway.mainnet.nado.xyz/execute"

    mod.record_ip_query_only(url)
    assert mod.is_write_blocked(url) is True
    assert mod.try_acquire(url, kind="execute", wallet="0xabc", weight=1) is False

    mod.clear_write_ban(url)
    assert mod.is_write_blocked(url) is False
    assert mod.try_acquire(url, kind="execute", wallet="0xabc", weight=1) is True


def test_write_circuit_expires_after_cooldown(monkeypatch):
    """The ban auto-expires once the cooldown window elapses."""
    monkeypatch.setenv("NADO_WRITE_BAN_COOLDOWN_SECONDS", "0.05")
    mod = _fresh_module(monkeypatch)
    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", lambda *_a, **_k: True)
    url = "https://gateway.mainnet.nado.xyz/execute"

    mod.record_ip_query_only(url)
    assert mod.is_write_blocked(url) is True
    time.sleep(0.06)
    assert mod.is_write_blocked(url) is False
    assert mod.try_acquire(url, kind="execute", wallet="0xabc", weight=1) is True


def test_write_circuit_is_per_host(monkeypatch):
    """The ban is keyed by host and does not leak across venues."""
    mod = _fresh_module(monkeypatch)
    monkeypatch.setattr("src.nadobro.core.http_session.throttle_host", lambda *_a, **_k: True)
    banned = "https://gateway.mainnet.nado.xyz/execute"
    other = "https://gateway.testnet.nado.xyz/execute"

    mod.record_ip_query_only(banned)
    assert mod.is_write_blocked(banned) is True
    assert mod.is_write_blocked(other) is False
    assert mod.try_acquire(other, kind="execute", wallet="0xabc", weight=1) is True


# ---------------------------------------------------------------------------
# GATEWAY-INFLIGHT-LEAK regression (2026-08)
# ---------------------------------------------------------------------------
# ``nado_client.get_market_liquidity`` charges the budget with the default
# ``user_scoped=True``, which takes a per-user in-flight slot that only
# ``release()`` frees — but the function had no ``finally: _gateway_release()``.
# So four cache-missing depth polls pinned ``_user_inflight`` at
# ``NADO_USER_MAX_INFLIGHT`` and EVERY later user-scoped query for that user
# (mid price, open orders, balance) was denied for the life of the process.
# Mid mode polls depth once per tick, so this bricked a user in ~12s. The test
# asserts the user-visible symptom, not just that release() is called.

class _EmptyMarket:
    """Returns an empty book, which ``get_market_liquidity`` deliberately does
    NOT cache — so every call is a real acquire/release round trip."""

    def __init__(self):
        self.calls = 0

    def get_market_liquidity(self, product_id, depth):
        self.calls += 1
        return type("Data", (), {"bids": [], "asks": []})()


def test_depth_polls_do_not_exhaust_the_user_inflight_cap(monkeypatch):
    mod = _fresh_module(monkeypatch)
    monkeypatch.setattr(
        "src.nadobro.core.http_session.throttle_host",
        lambda _url, *, cost=1.0, max_wait=None: True,
    )
    from src.nadobro.venue import nado_client as nc

    nc._liquidity_cache.clear()
    market = _EmptyMarket()
    client = nc.NadoClient(private_key="0xabc", network="mainnet")
    client._initialized = True
    client.client = type("SDK", (), {"market": market})()
    client.acting_user_id = 4242

    # More polls than the cap (4). Every one must reach the venue.
    for _ in range(8):
        client.get_market_liquidity(2)
    assert market.calls == 8, "a leaked in-flight slot starves later polls"

    # And the user must still be able to issue any other user-scoped query.
    assert mod.snapshot()["user_inflight"].get(4242, 0) == 0
    url = "https://gateway.mainnet.nado.xyz/query"
    assert mod.try_acquire(url, user_id=4242) is True
    mod.release(4242)
