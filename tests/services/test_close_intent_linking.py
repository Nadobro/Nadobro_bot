"""Close orders must tag their digest in order_intents.

Close orders never went through the open-order intent reservation, so their
venue fills arrived UNTAGGED and ``nado_sync._write_matches`` had to guess by
product + time window:

  - a MANUAL close during a live same-product session was swallowed into the
    session (hidden from History, excluded from the rollup — invisible);
  - a SESSION close (DN perp leg via ``close_position``) that filled after
    ``stopped_at`` missed the window and orphaned as manual (phantom History
    row + missing session close volume).

These tests pin the fix: ``link_digest_intent`` writes the tag, and
``close_position`` links every close digest with its true source/session and
forwards the digest into the synthetic close row.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from _stubs import install_test_stubs  # noqa: F401

install_test_stubs()

from src.nadobro.trading import order_intents
from src.nadobro.trading import trade_service


class _FakeNetworkMode:
    value = "mainnet"


class _FakeUser:
    network_mode = _FakeNetworkMode()


class _FakeClient:
    def __init__(self):
        self.placed = []

    def get_all_positions(self):
        # First read: one long 0.5 on product 2. After the close: flat.
        if self.placed:
            return []
        return [{"product_id": 2, "signed_amount": 0.5}]

    def place_market_order(self, product_id, size, **kwargs):
        self.placed.append({"product_id": product_id, "size": size, **kwargs})
        return {"success": True, "digest": "0xC1053"}

    # Batched book read used by the post-close verify (_resting_orders_by_sender).
    # [] = READ, empty. Subclasses return None to simulate a denied read.
    def get_all_open_orders(self, *a, **k):
        return []

    def get_open_orders(self, product_id, sender=None, refresh=False):
        return []

    def get_market_price(self, product_id):
        return {"mid": 100.0}


def test_link_digest_intent_writes_source_and_session():
    calls = []
    with patch("src.nadobro.db.execute", side_effect=lambda *a, **k: calls.append(a)):
        ok = order_intents.link_digest_intent(
            "0xabc", "mainnet", source="dn", strategy_session_id=55
        )
    assert ok is True
    assert len(calls) == 1
    sql, params = calls[0]
    assert "INSERT INTO order_intents" in sql
    intent_id, value_json, digest = params
    assert intent_id == "close:mainnet:0xabc"
    assert digest == "0xabc"
    assert json.loads(value_json) == {"source": "dn", "strategy_session_id": 55}


def test_link_digest_intent_manual_omits_session():
    calls = []
    with patch("src.nadobro.db.execute", side_effect=lambda *a, **k: calls.append(a)):
        ok = order_intents.link_digest_intent("0xdef", "testnet", source="manual")
    assert ok is True
    value = json.loads(calls[0][1][1])
    assert value == {"source": "manual"}


def test_link_digest_intent_empty_digest_noops():
    calls = []
    with patch("src.nadobro.db.execute", side_effect=lambda *a, **k: calls.append(a)):
        ok = order_intents.link_digest_intent("", "mainnet", source="manual")
    assert ok is False
    assert calls == []


def _close_position_with_mocks(client=None, cancel_return=(0, []), **close_kwargs):
    """Drive close_position through one leg with a fake client; capture the
    intent link and the synthetic-row recording."""
    client = client if client is not None else _FakeClient()
    linked = []
    recorded = []

    def _fake_link(digest, network, *, source, strategy_session_id=None,
                   product_id=None, product_name=None):
        linked.append({
            "digest": digest, "network": network,
            "source": source, "strategy_session_id": strategy_session_id,
            "product_id": product_id, "product_name": product_name,
        })
        return True

    def _fake_record(*args, **kwargs):
        recorded.append((args, kwargs))

    with patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_product_id", return_value=2), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "_order_sender_params", return_value=[None]), \
         patch.object(trade_service, "_cancel_open_orders_for_product", return_value=cancel_return), \
         patch.object(trade_service, "_iter_position_legs",
                      return_value=[{"subaccount": None, "signed_amount": 0.5}]), \
         patch.object(trade_service, "_net_abs_for_subaccount", return_value=(0.5, 1)), \
         patch.object(trade_service, "is_product_id_isolated_only", return_value=False), \
         patch.object(trade_service, "_resolve_fill_data", return_value=None), \
         patch.object(trade_service, "_get_post_fill_price", return_value=100.0), \
         patch.object(trade_service, "_record_close_in_db", side_effect=_fake_record), \
         patch("src.nadobro.trading.order_intents.link_digest_intent", side_effect=_fake_link), \
         patch("src.nadobro.users.settings_service.get_user_settings",
               return_value=(True, {"default_leverage": 3})):
        result = trade_service.close_position(1234, "BTC", **close_kwargs)
    return result, linked, recorded, client


def test_close_position_links_manual_close_digest():
    result, linked, recorded, client = _close_position_with_mocks()
    assert result.get("success") is True
    assert len(client.placed) == 1
    assert client.placed[0]["reduce_only"] is True
    # The close digest is tagged manual (no session) so the venue fill can
    # never be window-swallowed into a concurrent session.
    assert linked == [{
        "digest": "0xC1053", "network": "mainnet",
        "source": "manual", "strategy_session_id": None,
        # Product travels with the tag so the instantly-filled close's venue
        # match resolves out of the product_id=0 bucket History excludes.
        "product_id": 2, "product_name": "BTC-PERP",
    }]
    # The synthetic close row carries the digest but keeps default source
    # ('manual', rollup-excluded) — the intent-tagged venue fill is the single
    # counted truth.
    assert len(recorded) == 1
    _, kwargs = recorded[0]
    assert kwargs.get("order_digest") == "0xC1053"
    assert kwargs.get("strategy_session_id") is None
    assert "source" not in kwargs


def test_close_position_links_session_close_digest():
    result, linked, recorded, _ = _close_position_with_mocks(
        source="dn", strategy_session_id=55
    )
    assert result.get("success") is True
    assert linked == [{
        "digest": "0xC1053", "network": "mainnet",
        "source": "dn", "strategy_session_id": 55,
        "product_id": 2, "product_name": "BTC-PERP",
    }]
    _, kwargs = recorded[0]
    assert kwargs.get("order_digest") == "0xC1053"
    assert kwargs.get("strategy_session_id") == 55
    # Synthetic row must NOT inherit the non-manual source (double-count guard).
    assert "source" not in kwargs


# ---------------------------------------------------------------------------
# DENIED-vs-EMPTY in close_position (single-product manual close), 2026-09-04.
# Same class as the close_all_positions / cancel_resting_orders_for_user fixes:
# a budget-denied order read must FAIL LOUD, never collapse to "book clear".
# get_all_positions uses a cache/empty fallback (never None), so only the order
# side can fail loud here — but orphaned ORDERS were the session-312 mechanism.
# ---------------------------------------------------------------------------

def test_close_position_full_close_fails_loud_on_unreadable_verify():
    """AUDIT-SWEEP-2026-09-04-CLOSE-ALL-UNKNOWN-BOOK-POS-BRANCH: after the close fills, an
    UNREADABLE post-close order verify (batched + fallback both denied) must fail loud
    instead of collapsing a denied read to '0 remaining' and reporting a clean close."""
    class _DeniedVerifyClient(_FakeClient):
        def get_all_open_orders(self, *a, **k):
            return None                                    # batched verify read denied
        def get_open_orders(self, product_id, sender=None, refresh=False):
            return None                                    # per-product fallback denied too

    result, _, _, client = _close_position_with_mocks(client=_DeniedVerifyClient())
    assert client.placed, "the close order was still placed"
    assert result.get("success") is False, "an unreadable post-close verify must fail loud"
    assert "could not confirm the order book is clear" in str(result.get("error", "")).lower()


def test_close_position_no_position_fails_loud_on_unreadable_cancel():
    """AUDIT-SWEEP-2026-09-04-PARTIAL-CANCEL-FALSE-SUCCESS: with no position to close, an
    unreadable order book during the pre-close cancel (or a rejected cancel) must fail
    loud, not report a clean cancel-only close over orders that may still rest."""
    class _FlatClient(_FakeClient):
        def get_all_positions(self):
            return []                                      # no position on this product

    result, _, _, _ = _close_position_with_mocks(
        client=_FlatClient(),
        cancel_return=(0, ["open-orders read unavailable for BTC-PERP"]),
    )
    assert result.get("success") is False, "an unreadable book during cancel must fail loud"
    assert "could not confirm the order book is clear" in str(result.get("error", "")).lower()


def test_close_position_full_close_succeeds_on_readable_empty_verify():
    """Happy path unchanged: a READABLE empty post-close book -> success (no false fail)."""
    result, _, _, client = _close_position_with_mocks()   # default _FakeClient: [] book
    assert client.placed and result.get("success") is True
