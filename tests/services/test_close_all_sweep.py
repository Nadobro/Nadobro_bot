"""CLOSE-ALL-SWEEP (prod 2026-07-31): closing a position must not probe the
whole product catalog.

``close_all_positions`` used to cancel via ``products x senders`` single-product
``get_open_orders`` reads — twice (once before placing the close, once to
verify). For ONE open BTC position on a 30-perp catalog with 2 isolated
subaccounts that is 90 serial gateway round-trips before the close order is
even sent and 90 after, so the tap sat "Closing all positions…" for minutes
while holding a worker. The venue's batched multi-product read collapses each
sweep to one call per sender.
"""
from __future__ import annotations

from unittest.mock import patch

from src.nadobro.trading import trade_service

BTC_PID = 2
PERPS = [f"P{i}" for i in range(30)]


class _Net:
    value = "mainnet"


class _FakeUser:
    network_mode = _Net()


class _FakeClient:
    """Records every venue round-trip so the test can assert the call shape."""

    subaccount_hex = "0xparent"

    def __init__(self, resting=None):
        self.calls: list[str] = []
        self.cancelled: list[tuple[int, str, str | None]] = []
        self._resting = resting or []
        self._flat = False

    # Batched read: ONE call covering every product for every sender.
    def get_all_open_orders(self, *a, **k):
        self.calls.append("get_all_open_orders")
        return [] if self._flat else list(self._resting)

    # The per-product read the old sweep used; must not be reached.
    def get_open_orders(self, product_id, sender=None, refresh=False):
        self.calls.append("get_open_orders")
        return []

    def cancel_order(self, product_id, digest, sender=None):
        self.calls.append("cancel_order")
        self.cancelled.append((product_id, digest, sender))
        return {"success": True}

    def get_all_positions(self):
        self.calls.append("get_all_positions")
        if self._flat:
            return []
        return [{"product_id": BTC_PID, "signed_amount": 0.01, "product_name": "BTC-PERP"}]

    def place_market_order(self, pid, size, **kw):
        self.calls.append("place_market_order")
        self._flat = True          # venue is flat once the close lands
        self._resting = []
        return {"success": True, "digest": "0xC1053"}

    def get_market_price(self, pid):
        return {"mid": 64000.0}


def _close_all(client, **kwargs):
    with patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "get_perp_products", return_value=list(PERPS)), \
         patch.object(trade_service, "get_product_id", return_value=BTC_PID), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch.object(trade_service, "_order_sender_params",
                      return_value=[None, "0xiso1", "0xiso2"]), \
         patch.object(trade_service, "is_product_id_isolated_only", return_value=False), \
         patch.object(trade_service, "_resolve_fill_data", return_value=None), \
         patch.object(trade_service, "_get_post_fill_price", return_value=64000.0), \
         patch.object(trade_service, "_record_close_in_db", return_value=None), \
         patch("src.nadobro.trading.order_intents.link_digest_intent", return_value=True), \
         patch("src.nadobro.users.settings_service.get_user_settings",
               return_value=(True, {"default_leverage": 3})):
        return trade_service.close_all_positions(1234, network="mainnet", **kwargs)


def test_close_all_never_probes_the_product_catalog():
    client = _FakeClient()
    result = _close_all(client)

    assert result.get("success") is True
    # Zero single-product probes: the catalog fan-out is the bug.
    assert "get_open_orders" not in client.calls
    # One batched read to cancel against + one to verify.
    assert client.calls.count("get_all_open_orders") == 2


def test_close_order_is_sent_almost_immediately():
    """The user-visible symptom: the close request itself was queued behind ~90
    catalog probes. It must now be one of the first calls of the tap."""
    client = _FakeClient()
    _close_all(client)

    placed_at = client.calls.index("place_market_order")
    assert placed_at <= 2, client.calls


def test_close_all_still_cancels_resting_orders_on_every_sender():
    """Collapsing the read must not lose coverage: parent AND isolated
    subaccount orders still get cancelled, on their own sender."""
    client = _FakeClient(resting=[
        {"product_id": BTC_PID, "digest": "0xaaa"},                          # parent
        {"product_id": BTC_PID, "digest": "0xbbb", "subaccount": "0xiso1"},  # isolated
    ])
    result = _close_all(client)

    assert result.get("success") is True
    assert result.get("cancelled_orders") == 2
    assert sorted(client.cancelled) == sorted([
        (BTC_PID, "0xaaa", None),
        (BTC_PID, "0xbbb", "0xiso1"),
    ])


def test_scoped_close_ignores_other_products_resting_orders():
    """A strategy stop scoped to one product must not cancel a user's unrelated
    orders on another market."""
    client = _FakeClient(resting=[
        {"product_id": BTC_PID, "digest": "0xaaa"},
        {"product_id": 999, "digest": "0xzzz"},   # different market
    ])
    _close_all(client, only_product="BTC")

    assert client.cancelled == [(BTC_PID, "0xaaa", None)]


def test_remaining_orders_fail_the_close():
    """Verification still has teeth: an order the venue never cancelled must
    surface as a failure rather than a silent success."""
    class _StubbornClient(_FakeClient):
        def get_all_open_orders(self, *a, **k):
            self.calls.append("get_all_open_orders")
            return [{"product_id": BTC_PID, "digest": "0xstuck"}]

    result = _close_all(_StubbornClient())

    assert result.get("success") is False
    assert "open orders remain" in str(result.get("error"))


# ---------------------------------------------------------------------------
# DENIED-vs-EMPTY at the cancel sweep (prod 2026-09-03, session 312).
# ``get_all_open_orders`` returned None on EVERY call for a user with isolated
# children; ``None or []`` read that as an empty book -> cancelled nothing ->
# success=True -> the session rail sent "TP hit" over orders that kept filling.
# ---------------------------------------------------------------------------

class _DeniedBatchClient(_FakeClient):
    """The whole-account read is UNKNOWN; the per-product single read works for
    the default sender and holds the resting asks."""

    def get_all_open_orders(self, *a, **k):
        self.calls.append("get_all_open_orders")
        return None

    def get_open_orders(self, product_id, sender=None, refresh=False):
        self.calls.append("get_open_orders")
        if sender is not None or self._flat:
            return []
        return [dict(o) for o in self._resting if int(o.get("product_id")) == int(product_id)]


def _known_pids(pids):
    return patch("src.nadobro.models.database.get_open_order_product_ids", return_value=list(pids)), \
           patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[])


def test_an_unknown_book_falls_back_to_per_product_reads_and_cancels():
    client = _DeniedBatchClient(resting=[
        {"product_id": BTC_PID, "digest": "0xA"}, {"product_id": BTC_PID, "digest": "0xB"},
    ])
    p1, p2 = _known_pids([BTC_PID])
    with p1, p2:
        result = _close_all(client)
    assert result.get("success") is True
    assert client.cancelled and {d for _, d, _ in client.cancelled} == {"0xA", "0xB"}
    assert "get_open_orders" in client.calls, "the per-product fallback must run when the batched read is unknown"


def test_an_unreadable_book_with_an_unreadable_fallback_fails_loud():
    class _AllDenied(_DeniedBatchClient):
        def get_open_orders(self, product_id, sender=None, refresh=False):
            self.calls.append("get_open_orders")
            return None

    client = _AllDenied()
    client._flat = True                                   # venue reports no position
    p1, p2 = _known_pids([BTC_PID])
    with p1, p2:
        result = _close_all(client)
    assert result.get("success") is False
    assert "unavailable" in str(result.get("error", ""))
    assert "cancel_order" not in client.calls


def test_cancelling_nothing_with_no_known_products_is_not_success():
    client = _DeniedBatchClient()
    client._flat = True
    p1, p2 = _known_pids([])
    with p1, p2:
        result = _close_all(client)
    assert result.get("success") is False


def test_a_readable_empty_book_on_a_flat_account_is_still_success():
    client = _FakeClient()
    client._flat = True                                   # batched read -> [] (READ, empty)
    p1, p2 = _known_pids([BTC_PID])
    with p1, p2:
        result = _close_all(client)
    assert result.get("success") is True and "cancel_order" not in client.calls


def test_cancel_only_entry_point_reports_an_unknown_book():
    class _AllDenied(_DeniedBatchClient):
        def get_open_orders(self, product_id, sender=None, refresh=False):
            return None

    client = _AllDenied()
    p1, p2 = _known_pids([BTC_PID])
    with p1, p2, patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch.object(trade_service, "_order_sender_params", return_value=[None]):
        out = trade_service.cancel_resting_orders_for_user(1234, "mainnet", only_pid=BTC_PID)
    assert out["success"] is False and "unavailable" in out["error"]


# ---------------------------------------------------------------------------
# PARTIAL-CANCEL & VERIFY-UNREADABLE (audit 2026-09-04).
# The book reads fine but a per-digest cancel is rejected/rate-limited and stays
# resting, OR the post-close verify read is denied. Either way the stop must NOT
# report the book clear — the session-312 residual class.
# ---------------------------------------------------------------------------

def test_partial_cancel_failure_fails_loud_cancel_only():
    """AUDIT-SWEEP-2026-09-04-PARTIAL-CANCEL-FALSE-SUCCESS: two digests read from a
    readable book; the first cancels, the second is denied mid-sweep and stays
    resting. cancel_resting_orders_for_user must report success=False. The old gate
    (``not _book_unknown and not (cancelled == 0 and errors)``) passed because
    cancelled>=1 and the cancel-failed string is not an UNKNOWN_BOOK marker."""
    class _PartialCancel(_FakeClient):
        def cancel_order(self, product_id, digest, sender=None):
            self.calls.append("cancel_order")
            self.cancelled.append((product_id, digest, sender))
            if len(self.cancelled) >= 2:      # first ok, second denied
                return {"success": False, "error": "Rate limited — please retry in a moment.", "rate_limited": True}
            return {"success": True}

    client = _PartialCancel(resting=[
        {"product_id": BTC_PID, "digest": "0xA"}, {"product_id": BTC_PID, "digest": "0xB"},
    ])
    p1, p2 = _known_pids([BTC_PID])
    with p1, p2, patch.object(trade_service, "get_user", return_value=_FakeUser()), \
         patch.object(trade_service, "get_user_nado_client", return_value=client), \
         patch.object(trade_service, "get_product_name", return_value="BTC-PERP"), \
         patch.object(trade_service, "_order_sender_params", return_value=[None]):
        out = trade_service.cancel_resting_orders_for_user(1234, "mainnet", only_pid=BTC_PID)
    assert out["success"] is False, "a partial cancel must fail loud, not report the book clear"
    assert out.get("order_errors"), "the rejected cancel must be surfaced"


def test_partial_cancel_failure_fails_loud_close_all_no_positions():
    """AUDIT-SWEEP-2026-09-04-PARTIAL-CANCEL-FALSE-SUCCESS: same defect on the
    close_all_positions no-positions branch — a partial cancel used to fall through
    to ``if cancelled_orders > 0: return success=True`` with order_errors attached."""
    class _PartialCancelNoPos(_FakeClient):
        def get_all_positions(self):               # no open position -> no-positions branch
            self.calls.append("get_all_positions")
            return []
        def cancel_order(self, product_id, digest, sender=None):
            self.calls.append("cancel_order")
            self.cancelled.append((product_id, digest, sender))
            if len(self.cancelled) >= 2:
                return {"success": False, "error": "venue rejected", "rate_limited": False}
            return {"success": True}

    # _flat stays False so the batched read returns the resting book (readable);
    # only the position is absent.
    client = _PartialCancelNoPos(resting=[
        {"product_id": BTC_PID, "digest": "0xA"}, {"product_id": BTC_PID, "digest": "0xB"},
    ])
    p1, p2 = _known_pids([BTC_PID])
    with p1, p2:
        result = _close_all(client)
    assert result.get("success") is False, "a partial cancel must fail the close-all"
    assert result.get("order_errors")


def test_positions_found_unreadable_verify_fails_loud():
    """AUDIT-SWEEP-2026-09-04-CLOSE-ALL-UNKNOWN-BOOK-POS-BRANCH: with a position to
    flatten, the initial book reads empty-and-readable and the close lands, but the
    post-close VERIFY read is DENIED on every sender. Before the fix the verify call
    passed no known_pids/network, so its fallback returned empty and the denied read
    collapsed to '0 remaining' -> success=True. It must now fail loud."""
    class _DeniedVerifyClient(_FakeClient):
        def get_all_open_orders(self, *a, **k):
            self.calls.append("get_all_open_orders")
            return None if self._flat else []          # readable-empty pre-close, denied post-close
        def get_open_orders(self, product_id, sender=None, refresh=False):
            self.calls.append("get_open_orders")
            return None                                # fallback also denied -> unknown book

    client = _DeniedVerifyClient()
    p1, p2 = _known_pids([BTC_PID])
    with p1, p2:
        result = _close_all(client)
    assert client.calls.count("place_market_order") == 1, "the position was flattened"
    assert result.get("success") is False, "an unreadable post-close verify must fail loud"
    assert "clear after close" in str(result.get("error", ""))
