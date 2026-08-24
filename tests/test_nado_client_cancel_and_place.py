"""NadoClient.cancel_and_place — the atomic requote wrapper.

The properties that carry money:

* it charges BOTH legs of the execute budget (cancel weight = #digests, place
  weight = 1) in one check;
* it goes over REST, never the v2 action socket (cancel_and_place is not
  id-correlatable there);
* it reuses the SAME param builder as place_order, so the fragile increment /
  tag / appendix logic can never drift between the two;
* an empty digest list is refused (that would be a bare place, not a replace);
* failure is surfaced as a clean dict so the caller can fall back.
"""
from __future__ import annotations

import types

import pytest

from src.nadobro.venue import nado_client as nc

_D1 = "0x" + "a1" * 32
_D2 = "0x" + "b2" * 32
_D3 = "0x" + "c3" * 32


@pytest.fixture(autouse=True)
def _fake_sdk_types(monkeypatch):
    """Stub the SDK's pydantic param models so these unit tests exercise the
    WRAPPER's budget/transport/guard logic, not SDK serialization (which the
    nonce-tag and place_order tests already cover). The wrapper imports these
    function-locally, so patching them at their source module takes effect."""
    class _CancelOrdersParams:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _CancelAndPlaceParams:
        def __init__(self, *, cancel_orders, place_order):
            self.cancel_orders = cancel_orders
            self.place_order = place_order

    mod = "nado_protocol.engine_client.types.execute"
    monkeypatch.setattr(f"{mod}.CancelOrdersParams", _CancelOrdersParams, raising=False)
    monkeypatch.setattr(f"{mod}.CancelAndPlaceParams", _CancelAndPlaceParams, raising=False)


class _Resp:
    def __init__(self, digest="newd"):
        self.data = types.SimpleNamespace(digest=digest)
        self.status = "success"


class _Market:
    def __init__(self, resp=None, raises=None):
        self._resp = resp if resp is not None else _Resp()
        self._raises = raises
        self.cancel_and_place_calls = []
        self.place_order_calls = []
        self.cancel_orders_calls = []

    def cancel_and_place(self, params):
        self.cancel_and_place_calls.append(params)
        if self._raises:
            raise self._raises
        return self._resp

    def place_order(self, params):          # must NOT be used by cancel_and_place
        self.place_order_calls.append(params)
        return _Resp("shouldnotplace")


def _client(resp=None, raises=None, *, allow=True):
    c = nc.NadoClient.__new__(nc.NadoClient)
    c._initialized = True
    c.network = "mainnet"
    c.subaccount_hex = "0x" + "11" * 20 + "0000"
    c.client = types.SimpleNamespace(market=_Market(resp=resp, raises=raises))
    c._gateway_calls = []

    def _allowed(*, weight, kind, wallet, user_scoped):
        c._gateway_calls.append({"weight": weight, "kind": kind, "wallet": wallet,
                                 "user_scoped": user_scoped})
        return allow
    c._gateway_allowed = _allowed  # type: ignore[assignment]

    # Deterministic param builder: bypass increment caches / signing entirely.
    def _prep(**kwargs):
        params = types.SimpleNamespace(
            product_id=kwargs["product_id"],
            _size=kwargs["size"], _price=kwargs["price"], _clientid=kwargs["client_id"],
        )
        return params, kwargs["client_id"], kwargs["size"], kwargs["price"], None
    c._prepare_place_order_params = _prep  # type: ignore[assignment]
    c._rest_url = lambda: "https://gw.example"  # type: ignore[assignment]
    c._friendly_error = lambda s: str(s)  # type: ignore[assignment]
    return c


def _call(c, **over):
    kw = dict(product_id=2, cancel_digests=[_D1], size=0.5, price=100.0,
              is_buy=True, post_only=True, client_id=7)
    kw.update(over)
    return c.cancel_and_place(**kw)


# --- success ---------------------------------------------------------------

def test_success_returns_the_new_digest_and_records_the_cancel():
    c = _client()
    out = _call(c)
    assert out["success"] is True
    assert out["digest"] == "newd"
    assert out["client_id"] == 7
    assert out["cancelled"] == [_D1]


def test_it_uses_cancel_and_place_not_place_order():
    # The v2 socket cannot correlate a two-op request; this must be the single
    # atomic REST cancel_and_place, never a separate place.
    c = _client()
    _call(c)
    market = c.client.market
    assert len(market.cancel_and_place_calls) == 1
    assert market.place_order_calls == []


def test_both_legs_are_charged_to_the_execute_budget_once():
    c = _client()
    _call(c, cancel_digests=[_D1, _D2, _D3])
    assert len(c._gateway_calls) == 1
    charge = c._gateway_calls[0]
    assert charge["weight"] == 3 + 1          # 3 cancels + 1 place
    assert charge["kind"] == "execute"
    assert charge["user_scoped"] is False


# --- guards ----------------------------------------------------------------

def test_an_empty_digest_list_is_refused():
    # That would be a bare placement masquerading as a replace; the venue
    # rejects an empty-cancel cancel_and_place.
    c = _client()
    out = _call(c, cancel_digests=[])
    assert out["success"] is False
    assert c.client.market.cancel_and_place_calls == []


def test_blank_digests_are_stripped_and_an_all_blank_list_is_refused():
    c = _client()
    assert _call(c, cancel_digests=["  ", ""])["success"] is False


def test_a_throttled_budget_returns_rate_limited_without_calling_the_venue():
    c = _client(allow=False)
    out = _call(c)
    assert out["success"] is False and out["rate_limited"] is True
    assert c.client.market.cancel_and_place_calls == []


def test_an_uninitialised_client_is_refused():
    c = _client()
    c._initialized = False
    assert _call(c)["success"] is False


def test_a_param_build_error_is_surfaced_and_nothing_is_sent():
    c = _client()
    c._prepare_place_order_params = lambda **k: (  # type: ignore[assignment]
        None, None, k["size"], k["price"], {"success": False, "error": "bad size"})
    out = _call(c)
    assert out == {"success": False, "error": "bad size"}
    assert c.client.market.cancel_and_place_calls == []


# --- failure ---------------------------------------------------------------

def test_a_raising_venue_returns_a_clean_failure_dict():
    c = _client(raises=RuntimeError("venue boom"))
    out = _call(c)
    assert out["success"] is False
    assert out["cancelled"] == [_D1]        # so the caller knows what it tried


def test_ip_query_only_raise_is_flagged_transient(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "src.nadobro.venue.gateway_budget.record_ip_query_only",
        lambda host: recorded.append(host),
    )
    c = _client(raises=RuntimeError("request failed: ip_query_only"))
    out = _call(c)
    assert out["success"] is False
    assert out["ip_query_only"] is True and out["rate_limited"] is True
    assert recorded == ["https://gw.example"]


def test_a_missing_digest_in_the_response_is_a_failure_not_a_false_success():
    class _NoDigest:
        data = None
        status = "success"
    c = _client(resp=_NoDigest())
    out = _call(c)
    assert out["success"] is False
