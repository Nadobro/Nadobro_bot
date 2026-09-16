"""Guardrails from the 2026-09-16 review of the rate-limit accounting fix.

Each test pins one defect the reviewers found in the first cut so the money
paths cannot regress silently: the execute lane parked by a query breaker,
the wallet bucket keyed per subaccount instead of per wallet, an archive
denial read as "no children", a scope-limited product cancel erasing
account-wide unknowns, a throttled account summary failing the whole sync,
and a fresh strategy's product missing from the poll scope.
"""
from __future__ import annotations

import asyncio
from unittest import mock
from unittest.mock import patch

import pytest

from src.nadobro.trading import trade_service
from src.nadobro.venue import gateway_budget as gb
from src.nadobro.venue import nado_archive
from src.nadobro.venue import nado_sync

BTC = 2
ETH = 4
PARENT = "0x" + "11" * 32
CHILD = "0x" + "22" * 32


# ---------------------------------------------------------------- budget ---

def test_execute_lane_is_not_parked_by_the_query_rate_limit_breaker():
    """A query 429 storm (error_code=1000 breaker) must not block place/cancel:
    executes are limited per wallet by the venue, and a stop has to reach it
    exactly then. Only the Cloudflare circuit and the ip_query_only write ban do."""
    url = "https://gateway.prod.nado.xyz/v1/execute"
    gb._wallet_buckets.clear()
    with mock.patch.object(gb, "is_gateway_rate_limited", return_value=True):
        assert gb.try_acquire(url, kind="execute", wallet=PARENT, weight=1) is True
        assert gb.try_acquire(url, kind="query", weight=1) is False
    with mock.patch("src.nadobro.core.http_session.is_circuit_open", return_value=True):
        assert gb.try_acquire(url, kind="execute", wallet=PARENT, weight=1) is False
    with mock.patch.object(gb, "is_write_blocked", return_value=True):
        assert gb.try_acquire(url, kind="execute", wallet=PARENT, weight=1) is False


def test_wallet_execute_bucket_is_keyed_by_wallet_address_not_subaccount():
    """The venue's 600/min execute budget is per WALLET: the parent and every
    isolated child (same address, different name bytes) share ONE bucket."""
    assert gb._wallet_key(PARENT) == PARENT[:42]
    assert gb._wallet_key("0x" + "11" * 20 + "64656661756c74" + "00" * 5) == PARENT[:42]
    assert gb._wallet_key("0xabc") == "0xabc"
    gb._wallet_buckets.clear()
    url = "https://gateway.prod.nado.xyz/v1/execute"
    child = "0x" + "11" * 20 + "00" * 11 + "01"          # same address, a different subaccount name
    assert len(child) == 66
    assert gb.try_acquire(url, kind="execute", wallet=PARENT, weight=gb._WALLET_BURST, max_wait=0.01) is True
    assert gb.try_acquire(url, kind="execute", wallet=child, weight=1, max_wait=0.01) is False   # same wallet, drained


def test_edge_rejection_parks_only_the_edge_lane_briefly():
    url = "https://gateway.prod.nado.xyz/edge/query"
    gb._edge_rl.clear()
    gb._edge_buckets.clear()
    gb.record_edge_rate_limited(url)
    assert gb.is_edge_rate_limited(url) is True
    assert gb.try_acquire(url, kind="edge") is False
    assert gb.is_gateway_rate_limited("https://gateway.prod.nado.xyz/v1/query") is False
    gb._edge_rl.clear()
    assert gb.try_acquire(url, kind="edge") is True


# --------------------------------------------------------------- archive ---

def test_archive_denial_raises_instead_of_reading_as_no_children():
    with patch.object(nado_archive, "_post", return_value=None):
        with pytest.raises(nado_archive.ArchiveReadUnavailable):
            nado_archive.query_isolated_subaccounts_for_parent("mainnet", PARENT)
    with patch.object(nado_archive, "_post", return_value={"isolated_subaccounts": []}):
        assert nado_archive.query_isolated_subaccounts_for_parent("mainnet", PARENT) == []


# ---------------------------------------------------------- trade service ---

class _Net:
    value = "mainnet"


class _FakeUser:
    network_mode = _Net()


class _Client:
    subaccount_hex = PARENT

    def __init__(self, children=None, *, discovery_error=None):
        self.calls: list = []
        self._children = children or []
        self._discovery_error = discovery_error

    def _isolated_subaccounts(self, *, refresh=False):
        if self._discovery_error:
            raise self._discovery_error
        return list(self._children)

    def get_all_open_orders(self, *a, **k):
        return None                                              # unknown book

    def get_open_orders(self, product_id, sender=None, refresh=False):
        return None                                              # unknown book

    def cancel_product_orders(self, product_ids, sender=None):
        self.calls.append((tuple(product_ids), sender))
        return {"success": True}

    def get_all_positions(self):
        return []


def test_product_cancel_targets_a_child_only_for_its_own_product():
    client = _Client(children=[(CHILD, BTC), ("0x" + "33" * 32, ETH), ("0x" + "44" * 32, None)])
    targets = trade_service._product_cancel_targets(client, "mainnet", [BTC, 9])
    assert targets == [(None, [BTC, 9]), (CHILD, [BTC]), ("0x" + "44" * 32, [BTC, 9])]


def test_unknown_children_make_the_read_free_cancel_not_cleared():
    client = _Client(discovery_error=RuntimeError("archive denied"))
    cleared, errors = trade_service._clear_book_by_product_cancel(client, [BTC], "mainnet")
    assert cleared == [] and errors and "isolated senders" in errors[0]
    assert client.calls == []


def test_unknown_children_in_the_per_product_fallback_keep_the_book_unknown():
    client = _Client(discovery_error=RuntimeError("archive denied"))
    grouped, errors = trade_service._resting_orders_fallback(client, "mainnet", BTC, [BTC], [])
    assert trade_service._book_unknown(errors)


def test_scoped_cleanup_resolves_only_the_errors_it_cleared():
    client = _Client(children=[(CHILD, BTC)])
    errors = [
        f"{trade_service._UNKNOWN_BOOK} (batched read raised: 429)",
        f"{trade_service._UNKNOWN_BOOK} for BTC-PERP (sender=default)",
        f"{trade_service._UNKNOWN_BOOK} for ETH-PERP (sender=default)",
    ]
    with patch.object(trade_service, "get_product_name", side_effect=lambda pid, **k: {BTC: "BTC-PERP", ETH: "ETH-PERP"}[int(pid)]):
        cleared, left = trade_service._resolve_unknown_book(client, list(errors), [BTC], "mainnet", scoped=True)
    assert cleared == [BTC]
    assert left == [f"{trade_service._UNKNOWN_BOOK} for ETH-PERP (sender=default)"]
    # Unscoped: the account-wide unknown survives — an order outside the scope may rest.
    with patch.object(trade_service, "get_product_name", side_effect=lambda pid, **k: {BTC: "BTC-PERP", ETH: "ETH-PERP"}[int(pid)]):
        cleared, left = trade_service._resolve_unknown_book(client, list(errors), [BTC], "mainnet", scoped=False)
    assert cleared == [BTC]
    assert f"{trade_service._UNKNOWN_BOOK} (batched read raised: 429)" in left
    assert client.calls[-2:] == [((BTC,), None), ((BTC,), CHILD)]     # parent for the scope, the child for its own product


def test_cancel_resting_orders_unknown_children_fail_loud():
    client = _Client(discovery_error=RuntimeError("archive denied"))
    with patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch("src.nadobro.models.database.get_open_order_product_ids", return_value=[BTC]), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[]):
        out = trade_service.cancel_resting_orders_for_user(1, "mainnet", only_pid=BTC)
    assert out["success"] is False
    assert client.calls == []


def test_close_all_with_unreadable_positions_fails_loud_not_flat():
    class _Unreadable(_Client):
        def get_all_positions(self):
            raise RuntimeError("positions read budget-denied")

    client = _Unreadable(children=[])
    with patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "get_product_id", return_value=BTC), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch("src.nadobro.models.database.get_open_order_product_ids", return_value=[BTC]), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[]):
        with pytest.raises(RuntimeError):
            trade_service.close_all_positions(1, "mainnet", only_product="BTC-PERP")


# -------------------------------------------------------------- nado_sync ---

def test_scope_includes_the_running_strategy_product_and_recent_trades():
    with patch("src.nadobro.models.database.get_open_order_product_ids", return_value=[]), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[]), \
         patch("src.nadobro.models.database.get_recent_trade_product_ids", return_value=[ETH]), \
         patch.object(nado_sync, "_running_strategy_product_id", return_value=BTC):
        assert nado_sync._open_orders_scope_for_user(1, "mainnet", {}) == [BTC, ETH]


def test_running_strategy_product_is_read_from_bot_state():
    import json
    row = {"value": json.dumps({"running": True, "product": "BTC-PERP"})}
    with patch.object(nado_sync, "query_one", return_value=row), \
         patch("src.nadobro.config.get_product_id", return_value=BTC):
        assert nado_sync._running_strategy_product_id(1, "mainnet") == BTC
    with patch.object(nado_sync, "query_one", return_value={"value": json.dumps({"running": False, "product": "BTC-PERP"})}):
        assert nado_sync._running_strategy_product_id(1, "mainnet") is None
