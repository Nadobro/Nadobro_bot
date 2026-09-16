"""READ-FREE cleanup (2026-09-16 storm): when the book cannot be READ, the
cancel-only paths clear the scoped products with the venue's
``cancel_product_orders`` execute instead of failing loud forever.

The fail-loud sweep (DENIED-vs-EMPTY, #283) needs to read the book before it
can cancel by digest. Under the per-IP query storm that read was denied on
every attempt, so "MID MODE session SL triggered ... cleanup failed ... could
not confirm the order book is clear" repeated while 18 quotes + a short kept
resting on Nado. A confirmed ``cancel_product_orders`` is the venue's own
statement that the product holds no orders for that sender.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import pytest

from src.nadobro.trading import trade_service
from src.nadobro.venue.nado_client import NadoClient

BTC = 2
SENDERS = [None, "0xiso1"]


class _Net:
    value = "mainnet"


class _FakeUser:
    network_mode = _Net()


class _UnreadableClient:
    """Every read is denied; only the read-free execute can clear the book."""

    subaccount_hex = "0xparent"

    def __init__(self, *, product_cancel_ok=True):
        self.calls: list = []
        self._ok = product_cancel_ok

    def get_all_open_orders(self, *a, **k):
        self.calls.append("get_all_open_orders")
        return None                                  # unknown, not empty

    def get_open_orders(self, product_id, sender=None, refresh=False):
        self.calls.append("get_open_orders")
        return None                                  # unknown, not empty

    def cancel_order(self, *a, **k):
        raise AssertionError("no digest to cancel on an unreadable book")

    def cancel_product_orders(self, product_ids, sender=None):
        self.calls.append(("cancel_product_orders", tuple(product_ids), sender))
        return {"success": self._ok} if self._ok else {"success": False, "error": "Rate limited"}

    def get_all_positions(self):
        return []


def _run(client, **kwargs):
    with patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch.object(trade_service, "_order_sender_params", return_value=list(SENDERS)), \
         patch("src.nadobro.models.database.get_open_order_product_ids", return_value=[BTC]), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[]):
        return trade_service.cancel_resting_orders_for_user(1, "mainnet", **kwargs)


def test_unreadable_book_is_cleared_by_product_on_every_sender():
    client = _UnreadableClient()
    out = _run(client, only_pid=BTC)
    assert out["success"] is True, out
    assert out["cleared_by_product_cancel"] == [BTC]
    assert out["order_errors"] == []
    assert [c for c in client.calls if isinstance(c, tuple)] == [
        ("cancel_product_orders", (BTC,), None),
        ("cancel_product_orders", (BTC,), "0xiso1"),
    ]


def test_a_failed_product_cancel_still_fails_loud():
    client = _UnreadableClient(product_cancel_ok=False)
    out = _run(client, only_pid=BTC)
    assert out["success"] is False
    assert "Could not confirm the order book is clear" in out["error"]
    assert any(e.startswith(trade_service._UNKNOWN_BOOK) for e in out["order_errors"])


def test_no_scope_means_no_product_cancel_and_still_fails_loud():
    client = _UnreadableClient()
    with patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "_order_sender_params", return_value=list(SENDERS)), \
         patch("src.nadobro.models.database.get_open_order_product_ids", return_value=[]), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[]):
        out = trade_service.cancel_resting_orders_for_user(1, "mainnet")
    assert out["success"] is False
    assert not [c for c in client.calls if isinstance(c, tuple)]


class _LegacyClient(_UnreadableClient):
    """A client object without the read-free execute (old adapters / fakes)."""

    cancel_product_orders = None  # type: ignore[assignment]


def test_a_client_without_the_execute_keeps_the_fail_loud_contract():
    client = _LegacyClient()
    out = _run(client, only_pid=BTC)
    assert out["success"] is False


def test_close_all_clears_the_scope_by_product_before_flattening():
    """Pre-close cancel-only: an unreadable book must not leave quotes resting
    that fill into the position we are about to flatten."""
    client = _UnreadableClient()
    with patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "get_product_id", return_value=BTC), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch.object(trade_service, "_order_sender_params", return_value=list(SENDERS)), \
         patch("src.nadobro.models.database.get_open_order_product_ids", return_value=[BTC]), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[]):
        out = trade_service.close_all_positions(1, "mainnet", only_product="BTC-PERP")
    # Flat account + book cleared by the venue -> clean result, no fail-loud.
    assert out["success"] is True, out
    assert ("cancel_product_orders", (BTC,), None) in client.calls


def test_helper_reports_partial_sender_failure_as_not_cleared():
    calls = []

    class _C:
        subaccount_hex = "0xp"

        def cancel_product_orders(self, pids, sender=None):
            calls.append(sender)
            return {"success": sender is None, "error": "child rate limited"}

    with patch.object(trade_service, "_order_sender_params", return_value=[None, "0xiso"]):
        cleared, errors = trade_service._clear_book_by_product_cancel(_C(), [BTC, BTC], "mainnet")
    assert cleared == []
    assert calls == [None, "0xiso"]
    assert errors and "0xiso" in errors[0]


# --------------------------------------------------- NadoClient execute ---

def _client():
    c = NadoClient(private_key="0xabc", network="mainnet")
    c.subaccount_hex = "0x" + "11" * 32
    c._initialized = True
    return c


def test_client_cancel_product_orders_charges_the_wallet_budget_and_sends_one_execute():
    pytest.importorskip("nado_protocol")
    c = _client()
    sent: list = []
    c.client = SimpleNamespace(market=SimpleNamespace(
        cancel_product_orders=lambda params: sent.append(params) or SimpleNamespace(status="success")))
    seen = {}

    def _allowed(**k):
        seen.update(k)
        return True

    with mock.patch.object(NadoClient, "_gateway_allowed", side_effect=_allowed):
        out = c.cancel_product_orders([5, 2, 5], sender="0x" + "22" * 32)
    assert out == {"success": True, "product_ids": [2, 5], "sender": "0x" + "22" * 32}
    assert seen["kind"] == "execute" and seen["wallet"] == "0x" + "22" * 32
    assert seen["weight"] == 10                       # 5 per product, documented execute weight
    assert sent[0].productIds == [2, 5]
    assert sent[0].sender == bytes.fromhex("22" * 32)   # the SDK validates the hex sender into bytes32


def test_client_cancel_product_orders_denied_budget_is_a_retriable_failure():
    c = _client()
    c.client = SimpleNamespace(market=SimpleNamespace(
        cancel_product_orders=lambda params: (_ for _ in ()).throw(AssertionError("must not reach the venue"))))
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=False):
        out = c.cancel_product_orders([2])
    assert out["success"] is False and out.get("rate_limited") is True


def test_client_cancel_product_orders_ip_query_only_arms_the_write_circuit():
    pytest.importorskip("nado_protocol")
    c = _client()
    c.client = SimpleNamespace(market=SimpleNamespace(
        cancel_product_orders=lambda params: {"status": "failure", "reason": "ip_query_only", "blocked": True}))
    with mock.patch.object(NadoClient, "_gateway_allowed", return_value=True), \
         mock.patch("src.nadobro.venue.gateway_budget.record_ip_query_only") as armed:
        out = c.cancel_product_orders([2])
    assert out["success"] is False and out.get("ip_query_only") is True
    assert armed.called
