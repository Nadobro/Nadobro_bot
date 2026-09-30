"""venue/arcus/errors.py — one test per ``classify_http`` row (02 §4.2 / §12.3).

DENIED ≠ EMPTY and "never a false ACK" are the properties under test: every
non-2xx is a typed denial, a 2xx body with ``status: "ERROR"`` is a
``Rejected``, and a write whose fate is unknown is ``Ambiguous`` (never resent).
"""
from __future__ import annotations

import logging

import pytest

from arcus_helpers import load_fixture
from src.nadobro.venue.arcus import errors as E
from src.nadobro.venue.arcus.errors import (
    Accepted,
    Ambiguous,
    ArcusSchemaError,
    Forbidden,
    LocalDenied,
    NoActivity,
    NotFound,
    Ok,
    Rejected,
    Throttled,
    Transmission,
    Unauthorized,
    Unavailable,
    classify_http,
    is_denied,
    pool_reading_of,
    record_schema_error,
    retry_after_ms_of,
    schema_error_counts,
)


@pytest.fixture(autouse=True)
def _fresh_schema_counts():
    E._reset_schema_errors_for_tests()
    yield
    E._reset_schema_errors_for_tests()


def _w(status, body, headers=None, **kw):
    kw.setdefault("client_id", "nb7ps_2s-1")
    return classify_http(status, body, headers or {}, is_write=True, now_mono=123.0, **kw)


def _r(status, body, headers=None):
    return classify_http(status, body, headers or {}, is_write=False, client_id=None)


# --- row 1: 2xx writes --------------------------------------------------------------


def test_place_202_is_ack_with_pool():
    out = _w(202, load_fixture("place_202.json"), expect_pool="order")
    assert isinstance(out, Accepted)
    assert (out.http_status, out.status, out.order_id, out.client_id) == (202, "ACK", "a1b2c3d4e5f67890", "nb7ps_2s-1")
    assert out.rejection_reason is None
    assert out.pool is not None
    assert (out.pool.remaining, out.pool.source, out.pool.as_of_mono) == (19999, "write", 123.0)
    assert out.pool.cap is None and out.pool.used is None and out.pool.next_available_ms is None


def test_place_200_rejected_is_accepted_with_reason():
    out = _w(200, load_fixture("place_200_rejected.json"), expect_pool="order")
    assert isinstance(out, Accepted)
    assert out.status == "REJECTED" and out.rejection_reason == "POST_ONLY_WOULD_CROSS"
    assert out.pool is None


def test_2xx_status_error_is_rejected_never_accepted():
    out = _w(200, load_fixture("place_200_error.json"), expect_pool="order")
    assert out == Rejected(200, None, "Order", "batch item validation failure", "nb7ps_2s-1")
    cancel = _w(200, {"status": "error"}, expect_pool="cancel", client_id=None)
    assert isinstance(cancel, Rejected) and cancel.error_source == "Cancel" and cancel.message == "ERROR"


def test_2xx_non_json_body_is_ack_and_counted():
    out = _w(202, None)
    assert out == Accepted(202, None, "nb7ps_2s-1", "ACK", None, None)
    assert schema_error_counts() == {"write.body": 1}


def test_2xx_missing_status_is_conservative_ack():
    out = _w(202, {"orderId": "ab"})
    assert isinstance(out, Accepted) and out.status == "ACK"
    assert schema_error_counts()["write.status"] == 1


def test_pool_not_enforced_sentinel():
    out = _w(202, load_fixture("place_202_not_enforced.json"), expect_pool="order")
    assert isinstance(out, Accepted) and out.pool is not None and out.pool.remaining is None


def test_pool_wrong_pool_dropped_and_counted():
    out = _w(202, load_fixture("place_202_wrong_pool.json"), expect_pool="order")
    assert isinstance(out, Accepted) and out.pool is None
    assert schema_error_counts()["rateLimit.pool"] == 1


def test_pool_negative_remaining_kept_verbatim():
    reading = pool_reading_of({"pool": "cancel", "remaining": -5}, expect_pool="cancel", now_mono=1.0)
    assert reading is not None and reading.remaining == -5
    assert pool_reading_of(None, expect_pool="order", now_mono=1.0) is None
    for bad in ({"pool": "order"}, {"pool": "x", "remaining": 1}, {"pool": "order", "remaining": True}, "x"):
        assert pool_reading_of(bad, expect_pool=None, now_mono=1.0) is None
    assert schema_error_counts()["rateLimit"] == 4


def test_cancel_202_ack():
    out = _w(202, load_fixture("cancel_202.json"), expect_pool="cancel", client_id=None)
    assert isinstance(out, Accepted)
    assert out.status == "CANCEL_ACKNOWLEDGED" and out.pool is not None and out.pool.remaining == 39999


def test_set_leverage_2xx():
    assert _w(200, load_fixture("set_leverage_200.json"), client_id=None).status == "APPLIED"
    assert _w(202, load_fixture("set_leverage_202.json"), client_id=None).status == "ACK"


def test_2xx_read_raises():
    with pytest.raises(ValueError):
        _r(200, {"orders": []})


# --- row 2: 429 -----------------------------------------------------------------------


def test_429_read_without_reason_is_read_ip():
    out = _r(429, load_fixture("err_429_read.json"), {"Retry-After": "2"})
    assert out == Throttled("read_ip", 2000, ())


def test_429_header_lookup_is_case_insensitive():
    assert _r(429, None, {"retry-after": "3"}) == Throttled("read_ip", 3000, ())


def test_429_account_empty_write():
    out = _w(429, load_fixture("err_429_account_empty.json"))
    assert out == Throttled("account_empty", 850, ("my-order-42",))


def test_429_batch_partial_keeps_positional_client_ids():
    out = _w(429, load_fixture("err_429_batch_partial.json"), client_id=None)
    assert isinstance(out, Throttled)
    assert out.layer == "account_partial" and out.client_ids == ("nb7ps_2s-2", "")


def test_429_ip_write():
    assert _w(429, load_fixture("err_429_ip_write.json")) == Throttled("ip", 2000, ())


def test_429_unknown_reason_and_write_without_reason():
    assert _w(429, {"error": "rate limited", "reason": "weird"}).layer == "unknown"
    assert _r(429, {"error": "rate limited", "reason": "weird"}).layer == "unknown"
    assert _w(429, {"error": "rate limited"}).layer == "unknown"
    assert _r(429, {"error": "rate limited", "reason": "ip"}).layer == "ip"


def test_429_read_has_no_client_ids():
    assert _r(429, {"error": "rate limited", "clientId": "nb1"}).client_ids == ()


# --- rows 3-12 ---------------------------------------------------------------------------


def test_geo_restricted_read_and_write():
    for out in (_w(403, load_fixture("err_403_geo.json")), _r(403, load_fixture("err_403_geo.json"))):
        assert isinstance(out, Forbidden) and out.kind == "geo"


def test_transmission():
    body = load_fixture("err_500_transmission.json")
    assert isinstance(_w(500, body), Transmission)
    assert _r(500, body) == Unavailable(500, "transmission")


def test_unavailable_503_and_error_type():
    out = _w(503, load_fixture("err_503.json"))
    assert isinstance(out, Unavailable) and out.http_status == 503
    assert isinstance(_r(503, None), Unavailable)
    assert isinstance(_w(400, {"errorType": "Unavailable", "error": "x"}), Unavailable)
    assert _r(503, None) == Unavailable(503, "unavailable")


def test_unauthorized():
    assert _w(401, load_fixture("err_401.json")) == Unauthorized("invalid signature")
    assert _r(401, None) == Unauthorized("")
    assert isinstance(_w(400, {"errorType": "Unauthorized"}), Unauthorized)


def test_forbidden_kinds():
    assert _r(403, load_fixture("err_403_whitelist.json")).kind == "whitelist"
    assert _w(403, {"code": "AddressNotOnWhitelist", "error": "x"}).kind == "whitelist"
    assert _w(403, load_fixture("err_403_scope.json")).kind == "scope"
    assert _w(403, None).kind == "unknown"
    assert _r(403, None).kind == "unknown"


def test_404_no_activity_and_not_found():
    assert _r(404, load_fixture("err_404_no_activity.json")) == NoActivity()
    assert _r(404, {"error": "  This account has no activity yet "}) == NoActivity()
    assert _r(404, load_fixture("err_404_order.json")) == NotFound("order not found")
    assert _w(404, load_fixture("err_404_no_activity.json")) == NotFound("this account has no activity yet")
    assert _r(404, {"error": "this account has no activity yet!"}) == NotFound("this account has no activity yet!")


def test_422():
    assert _w(422, load_fixture("set_leverage_422.json"), client_id=None) == Rejected(
        422, "HAS_OPEN_POSITION", None, "REJECTED", None
    )
    assert _r(422, load_fixture("set_leverage_422.json")) == Unavailable(422, "client_error")


def test_400():
    out = _w(400, load_fixture("err_400_tick.json"))
    assert out == Rejected(400, "Tick", "Order", "price is not a multiple of tick size", "nb7ps_2s-1")
    assert _w(400, load_fixture("err_400_notional.json")).error_type == "InvalidRequest"
    assert _r(400, load_fixture("err_400_tick.json")) == Unavailable(400, "client_error")


def test_read_client_error_warns(caplog):
    E._client_error_warned_at.clear()
    caplog.set_level(logging.WARNING, logger=E.__name__)
    _r(400, {"error": "bad address 0xabc", "errorType": "InvalidRequest"})
    msgs = [r.getMessage() for r in caplog.records if r.name == E.__name__]
    assert len(msgs) == 1 and "400" in msgs[0] and "0xabc" not in msgs[0]


def test_5xx_write_is_ambiguous_read_unavailable():
    assert _w(500, load_fixture("err_500_internal.json")) == Ambiguous("nb7ps_2s-1", "http_500")
    assert _r(500, load_fixture("err_500_internal.json")) == Unavailable(500, "Internal")
    for status in (502, 504):
        assert _w(status, None) == Ambiguous("nb7ps_2s-1", f"http_{status}")
        assert _r(status, None) == Unavailable(status, "server_error")


def test_unexpected_statuses():
    for status in (302, 405, 409, 413, 100):
        assert _w(status, None) == Ambiguous("nb7ps_2s-1", f"http_{status}")
        assert _r(status, None) == Unavailable(status, "unexpected_status")


def test_error_text_is_truncated_and_non_str_ignored():
    out = _w(400, {"error": "x" * 500, "errorType": "Tick"})
    assert isinstance(out, Rejected) and len(out.message) == 200
    out2 = _w(400, {"error": {"nested": "secret"}, "errorType": "Tick"})
    assert isinstance(out2, Rejected) and out2.message == ""


def test_non_mapping_body_treated_as_absent():
    assert _r(403, ["x"]).kind == "unknown"  # type: ignore[arg-type]


def test_status_must_be_int():
    with pytest.raises(ValueError):
        classify_http(True, None, {}, is_write=True, client_id=None)  # type: ignore[arg-type]


# --- helpers ----------------------------------------------------------------------------------


def test_retry_after_ms_of():
    assert retry_after_ms_of({"retryAfterMs": 850}, {"Retry-After": "9"}) == 850
    assert retry_after_ms_of({}, {"Retry-After": "2"}) == 2000
    assert retry_after_ms_of(None, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}) == 1000
    assert retry_after_ms_of(None, {"Retry-After": "-3"}) == 1000
    assert retry_after_ms_of(None, {}) == 1000
    assert retry_after_ms_of({"retryAfterMs": True}, {}) == 1000
    assert retry_after_ms_of({"retryAfterMs": -1}, {}) == 1000
    assert retry_after_ms_of({"retryAfterMs": 999999}, {}) == 120000
    assert retry_after_ms_of(None, {"Retry-After": "600"}) == 120000


def test_is_denied():
    assert is_denied(Ok([], 200, 20)) is False
    for denial in (
        Throttled("read_ip", 1000, ()),
        Unauthorized(""),
        Forbidden("whitelist", ""),
        NoActivity(),
        NotFound(""),
        Unavailable(0, "x"),
        LocalDenied("ip_budget"),
        Ambiguous(None, "x"),
        Transmission(""),
        Rejected(400, None, None, "", None),
        None,
        [],
    ):
        assert is_denied(denial) is True


def test_schema_error_message_has_no_value():
    exc = ArcusSchemaError("fills.fee")
    assert str(exc) == "fills.fee" and exc.where == "fills.fee"
    assert isinstance(exc, ValueError)


def test_record_schema_error_logs_once_per_key(caplog):
    caplog.set_level(logging.WARNING, logger=E.__name__)
    for _ in range(3):
        record_schema_error("fills.fee")
    record_schema_error("order.status")
    drift = [r.getMessage() for r in caplog.records if "schema drift" in r.getMessage()]
    assert drift == ["arcus schema drift at fills.fee", "arcus schema drift at order.status"]
    assert schema_error_counts() == {"fills.fee": 3, "order.status": 1}


def test_schema_registry_is_bounded():
    for i in range(E._SCHEMA_KEYS_MAX + 20):
        record_schema_error(f"k{i}")
    counts = schema_error_counts()
    assert len(counts) == E._SCHEMA_KEYS_MAX + 1 and counts["other"] == 20
