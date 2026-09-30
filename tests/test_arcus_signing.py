"""venue/arcus/signing.py (+ the types it validates) — 02 §5.1 / §12.2.

Golden vectors V1–V7 are byte-exact: payload bytes AND Ed25519 signatures
(Ed25519 is deterministic, RFC 8032). Venue acceptance is the owner-run G1
``sign-check``; these tests pin the library to the self-consistent vectors.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import pickle
import re
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from arcus_helpers import (
    ADDR,
    ADDR_MIXED,
    CT0,
    GTT,
    PUB,
    REF,
    SEED,
    V1_PAYLOAD,
    V1_SIG,
    V2_PAYLOAD,
    V2_SIG,
    V3_PAYLOAD,
    V3_SIG,
    V4_PAYLOAD,
    V4_SIG,
    V5_PAYLOAD,
    V5_SIG,
    V6_PAYLOAD,
    V6_SIG,
    V7_PAYLOAD,
    V7_SIG,
)
from src.nadobro.venue.arcus.errors import InexactUnitError
from src.nadobro.venue.arcus.signing import (
    SCHEME2_ACTIONS,
    ArcusAuth,
    Ed25519Signer,
    canonical_json,
    cancel_payload,
    derive_public_key_hex,
    legacy_message,
    make_auth,
    normalize_seed_hex,
    place_payload,
    to_quantums,
    to_ticks,
    wire_decimal,
)
from src.nadobro.venue.arcus.types import (
    ArcusAccountRef,
    CancelSpec,
    OrderSpec,
    Side,
    Tif,
    WireOrderType,
    base36,
    client_id_for,
    normalize_address,
    session_client_prefix,
    user_client_prefix,
)

D = Decimal


def _auth() -> ArcusAuth:
    return make_auth(REF, Ed25519Signer.from_seed_hex(SEED))


def _v1() -> bytes:
    return place_payload(
        address=ADDR_MIXED,
        account_index=0,
        client_id=client_id_for(10000, 100, 1),
        ct_ns=CT0,
        good_til_us=GTT,
        market_id=1,
        price_ticks=to_ticks(D("84517.3"), D("0.1")),
        qty_quantums=to_quantums(D("0.0001"), D("0.00000001")),
        reduce_only=False,
        side=Side.BUY,
        tif=Tif.ALO,
    )


def _v2() -> bytes:
    return place_payload(
        address=ADDR_MIXED,
        account_index=0,
        client_id=None,
        ct_ns=CT0 + 1,
        good_til_us=GTT,
        market_id=2,
        price_ticks=to_ticks(D("2500.12"), D("0.01")),
        qty_quantums=to_quantums(D("0.015"), D("0.0000001")),
        reduce_only=True,
        side=Side.SELL,
        tif=Tif.IOC,
    )


def _v3() -> bytes:
    return cancel_payload(address=ADDR_MIXED, account_index=0, ct_ns=CT0 + 2, market_id=1, order_id="a1b2c3d4e5f67890")


def _v4() -> bytes:
    return cancel_payload(address=ADDR_MIXED, account_index=0, ct_ns=CT0 + 3, market_id=1, client_id="nb7ps_2s-1")


def _v5() -> bytes:
    body = {"accountIndex": 0, "address": ADDR, "leverage": 5, "marketId": 1}
    return legacy_message(CT0 + 4, "setLeverage", body)


def _v6() -> bytes:
    return cancel_payload(address=ADDR_MIXED, account_index=0, ct_ns=CT0 + 5, market_id=1, client_id="nb7ps_2s-2")


def _v7() -> bytes:
    return cancel_payload(address=ADDR_MIXED, account_index=0, ct_ns=CT0 + 5, market_id=3, order_id="00000000000000ff")


VECTORS = {
    "V1": (_v1, V1_PAYLOAD, V1_SIG),
    "V2": (_v2, V2_PAYLOAD, V2_SIG),
    "V3": (_v3, V3_PAYLOAD, V3_SIG),
    "V4": (_v4, V4_PAYLOAD, V4_SIG),
    "V5": (_v5, V5_PAYLOAD, V5_SIG),
    "V6": (_v6, V6_PAYLOAD, V6_SIG),
    "V7": (_v7, V7_PAYLOAD, V7_SIG),
}


# --- golden vectors -------------------------------------------------------------


def test_public_key_matches_golden():
    assert derive_public_key_hex(SEED) == PUB
    assert Ed25519Signer.from_seed_hex(SEED).public_key_hex == PUB


@pytest.mark.parametrize("vid", sorted(VECTORS))
def test_golden_vectors(vid):
    build, payload, sig = VECTORS[vid]
    built = build()
    assert built == payload
    assert _auth().sign_hex(built) == sig


@pytest.mark.parametrize("vid", sorted(VECTORS))
def test_signature_verifies(vid):
    _build, payload, sig = VECTORS[vid]
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUB)).verify(bytes.fromhex(sig), payload)


def test_payload_keys_sorted_no_whitespace():
    raw = _v1()
    decoded = json.loads(raw)
    assert list(decoded) == sorted(decoded)
    assert b" " not in raw and b"\n" not in raw


def test_client_id_omitted_when_none_or_empty():
    kwargs = dict(
        address=ADDR, account_index=0, ct_ns=CT0, good_til_us=GTT, market_id=1, price_ticks=1,
        qty_quantums=1, reduce_only=False, side=Side.BUY, tif=Tif.ALO,
    )
    for cid in (None, ""):
        assert "c" not in json.loads(place_payload(client_id=cid, **kwargs))


def test_client_id_signed_verbatim_address_lowercased():
    obj = json.loads(
        place_payload(
            address=ADDR_MIXED, account_index=0, client_id="Ab-C_1", ct_ns=CT0, good_til_us=GTT,
            market_id=1, price_ticks=1, qty_quantums=1, reduce_only=False, side=Side.BUY, tif=Tif.ALO,
        )
    )
    assert obj["c"] == "Ab-C_1"
    assert obj["ad"] == ADDR


def test_reduce_only_is_int():
    obj = json.loads(_v2())
    assert obj["r"] == 1 and type(obj["r"]) is int
    assert json.loads(_v1())["r"] == 0
    with pytest.raises((TypeError, ValueError)):
        place_payload(
            address=ADDR, account_index=0, client_id=None, ct_ns=CT0, good_til_us=GTT, market_id=1,
            price_ticks=1, qty_quantums=1, reduce_only=1, side=Side.BUY, tif=Tif.ALO,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "override",
    [
        {"account_index": True},
        {"account_index": 10},
        {"market_id": -1},
        {"market_id": 65536},
        {"price_ticks": 1.0},
        {"price_ticks": 0},
        {"qty_quantums": 0},
        {"ct_ns": -1},
        {"ct_ns": 2**63},
        {"good_til_us": 2**63 // 1000 + 1},
        {"side": "BUY"},
        {"tif": 3},
    ],
)
def test_int_fields_reject_bool_and_negative(override):
    kwargs = dict(
        address=ADDR, account_index=0, client_id=None, ct_ns=CT0, good_til_us=GTT, market_id=1,
        price_ticks=1, qty_quantums=1, reduce_only=False, side=Side.BUY, tif=Tif.ALO,
    )
    kwargs.update(override)
    with pytest.raises((TypeError, ValueError)):
        place_payload(**kwargs)


def test_payload_rejects_bad_client_id_and_address():
    kwargs = dict(
        account_index=0, ct_ns=CT0, good_til_us=GTT, market_id=1, price_ticks=1, qty_quantums=1,
        reduce_only=False, side=Side.BUY, tif=Tif.ALO,
    )
    with pytest.raises(ValueError):
        place_payload(address=ADDR, client_id="bad id", **kwargs)
    with pytest.raises(ValueError):
        place_payload(address=ADDR, client_id="x" * 37, **kwargs)
    with pytest.raises(ValueError):
        place_payload(address="0x1234", client_id=None, **kwargs)


def test_cancel_payload_requires_exactly_one():
    with pytest.raises(ValueError):
        cancel_payload(address=ADDR, account_index=0, ct_ns=CT0, market_id=1)
    with pytest.raises(ValueError):
        cancel_payload(address=ADDR, account_index=0, ct_ns=CT0, market_id=1, order_id="ab", client_id="nb1")
    with pytest.raises(ValueError):
        cancel_payload(address=ADDR, account_index=0, ct_ns=CT0, market_id=1, order_id="", client_id="")
    # by clientId: no "id"; by orderId: no "c".
    assert "id" not in json.loads(_v4()) and "c" not in json.loads(_v3())


def test_legacy_message_action_allowlist():
    assert SCHEME2_ACTIONS == frozenset({"setLeverage"})
    for action in ("cancelAllOrders", "placeOrder", "", "setleverage"):
        with pytest.raises(ValueError):
            legacy_message(CT0, action, {"a": 1})


def test_canonical_json_rejects_non_json_safe_values():
    for bad in (1.5, D("1.5"), b"x", None, {1: "x"}, [1.0]):
        with pytest.raises(TypeError):
            canonical_json({"k": bad})
    assert canonical_json({"b": True, "a": [1, "x", {"z": False}]}) == b'{"a":[1,"x",{"z":false}],"b":true}'


# --- units -----------------------------------------------------------------------------


def test_to_ticks_exact():
    assert to_ticks(D("84517.3"), D("0.1")) == 845173
    # Tiers never change the divisor: a 0.2-band price still divides by tickSize 0.1.
    assert to_ticks(D("600000.2"), D("0.1")) == 6000002
    assert to_quantums(D("0.0001"), D("0.00000001")) == 10000
    assert to_ticks(D("2500.12"), D("0.01")) == 250012
    assert to_ticks(D("84517.30000"), D("0.10")) == 845173
    assert to_ticks(D("5E+4"), D("0.1")) == 500000


@pytest.mark.parametrize(
    "value,unit",
    [(D("84517.35"), D("0.1")), (D("0.000000015"), D("0.00000001")), (D("1"), D("0.3"))],
)
def test_to_ticks_inexact_raises(value, unit):
    with pytest.raises(InexactUnitError) as exc:
        to_ticks(value, unit)
    assert not re.search(r"\d", str(exc.value))


@pytest.mark.parametrize(
    "value,unit,exc",
    [
        (D("0"), D("0.1"), ValueError),
        (D("-1"), D("0.1"), ValueError),
        (D("NaN"), D("0.1"), ValueError),
        (D("Infinity"), D("0.1"), ValueError),
        (D("1"), D("0"), ValueError),
        (D("1"), D("-0.1"), ValueError),
        (1.0, D("0.1"), TypeError),
        (1, D("0.1"), TypeError),
        ("1", D("0.1"), TypeError),
        (D("1"), 0.1, TypeError),
        (D(2**63), D("1"), InexactUnitError),
        (D("1E+80"), D("1"), InexactUnitError),
    ],
)
def test_units_reject_bad_input(value, unit, exc):
    with pytest.raises(exc):
        to_quantums(value, unit)


def test_wire_decimal():
    assert wire_decimal(D("84517.30")) == "84517.3"
    assert wire_decimal(D("5E+4")) == "50000"
    assert wire_decimal(D("1E-8")) == "0.00000001"
    assert wire_decimal(D("0.00010000")) == "0.0001"
    assert wire_decimal(D("7")) == "7"
    assert wire_decimal(D("100.000")) == "100"
    for bad in (D("0"), D("-1"), D("NaN"), D("Infinity")):
        with pytest.raises(ValueError):
            wire_decimal(bad)
    with pytest.raises(TypeError):
        wire_decimal(1.5)  # type: ignore[arg-type]
    doc_re = re.compile(r"^(0|0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*\.?[0-9]*)$")
    for v in ("84517.3", "0.0001", "1E-8", "123456789.123456789", "2E+3"):
        assert doc_re.match(wire_decimal(D(v)))


# --- keys and secrets ------------------------------------------------------------------


def test_normalize_seed_hex():
    assert normalize_seed_hex("0x" + SEED.upper()) == SEED
    assert normalize_seed_hex("  " + SEED[:32] + "\n" + SEED[32:]) == SEED
    assert normalize_seed_hex(SEED) == SEED
    for bad in (SEED[:-1], SEED + "0", SEED * 2, "zz" * 32, "", None, 123, b"00" * 32):
        assert normalize_seed_hex(bad) is None


def test_signer_error_and_repr_never_leak():
    for bad in ("zz" * 32, SEED[:-2]):
        with pytest.raises(ValueError) as exc:
            Ed25519Signer.from_seed_hex(bad)
        assert str(exc.value) == "invalid signing key"
        assert SEED[:8] not in str(exc.value) and "zz" not in str(exc.value)
    signer = Ed25519Signer.from_seed_hex(SEED)
    auth = make_auth(REF, signer)
    for text in (repr(signer), str(signer)):
        assert text == "Ed25519Signer(<redacted>)"
    for text in (repr(auth), str(auth), f"{auth}", f"{signer!r}"):
        assert SEED not in text and PUB not in text
    assert repr(auth) == "ArcusAuth(<redacted>)" and str(auth) == "ArcusAuth(<redacted>)"
    # The seed string is never an attribute of the signer.
    assert not hasattr(signer, "__dict__")


def test_signer_not_serializable():
    signer = Ed25519Signer.from_seed_hex(SEED)
    auth = make_auth(REF, signer)
    for attempt in (
        lambda: pickle.dumps(signer),
        lambda: copy.copy(signer),
        lambda: copy.deepcopy(signer),
        lambda: dataclasses.asdict(auth),
        lambda: pickle.dumps(auth),
        lambda: copy.deepcopy(auth),
    ):
        with pytest.raises(TypeError):
            attempt()


def test_auth_key_mismatch():
    signer = Ed25519Signer.from_seed_hex(SEED)
    with pytest.raises(ValueError, match="auth key mismatch"):
        ArcusAuth(REF, "00" * 32, signer)
    auth = make_auth(REF, signer)
    assert auth.api_key_hex == PUB and auth.ref == REF


def test_sign_hex_needs_bytes():
    with pytest.raises(TypeError):
        _auth().sign_hex("text")  # type: ignore[arg-type]


def test_batch_elements_share_ct():
    v6, v7 = json.loads(_v6()), json.loads(_v7())
    assert v6["ct"] == v7["ct"] == CT0 + 5
    auth = _auth()
    assert auth.sign_hex(_v6()) != auth.sign_hex(_v7())


# --- types validated by the signing path ------------------------------------------------


def _spec(**override) -> OrderSpec:
    kwargs = dict(
        market_id=1, side=Side.BUY, order_type=WireOrderType.LIMIT, tif=Tif.ALO,
        quantity=D("0.0001"), price=D("84517.3"), reduce_only=False, client_id="nb7ps_2s-1",
        good_til_us=GTT, tick_size=D("0.1"), step_size=D("0.00000001"),
    )
    kwargs.update(override)
    return OrderSpec(**kwargs)


def test_order_spec_validation():
    spec = _spec()
    assert spec.client_id == "nb7ps_2s-1"
    market = _spec(order_type=WireOrderType.MARKET, tif=Tif.IOC, reduce_only=True)
    assert market.tif is Tif.IOC
    bad = [
        {"client_id": "xx7ps_2s-1"},  # no bot prefix (02 D19)
        {"client_id": "nb bad"},
        {"client_id": "nb" + "x" * 35},  # 37 chars
        {"order_type": WireOrderType.MARKET, "tif": Tif.ALO},
        {"quantity": D("0")},
        {"price": D("NaN")},
        {"price": 84517.3},
        {"tick_size": D("0")},
        {"step_size": D("-1")},
        {"market_id": True},
        {"market_id": 70000},
        {"good_til_us": 1_700_000_000_000},  # milliseconds
        {"good_til_us": 1_793_456_000_000_000_000},  # nanoseconds: g would overflow int64
        {"reduce_only": 0},
        {"side": "BUY"},
        {"tif": 3},
    ]
    for override in bad:
        with pytest.raises(ValueError):
            _spec(**override)


def test_cancel_spec_validation():
    assert CancelSpec(1, order_id="a1b2c3d4e5f67890").client_id is None
    assert CancelSpec(1, client_id="nb7ps_2s-1").order_id is None
    for kwargs in (
        {},
        {"order_id": "a1", "client_id": "nb1"},
        {"order_id": "bad id"},
        {"client_id": "nb bad"},
        {"client_id": "user-order-1"},  # not bot-owned: never cancelled by clientId
        {"order_id": ""},
    ):
        with pytest.raises(ValueError):
            CancelSpec(1, **kwargs)
    with pytest.raises(ValueError):
        CancelSpec(True, order_id="a1")  # type: ignore[arg-type]


def test_account_ref_validation():
    ref = ArcusAccountRef("mainnet", "  " + ADDR_MIXED.upper().replace("0X", "0x") + " ", 9)
    assert ref.address == ADDR and ref.network == "mainnet" and ref.scope == "arcus_mainnet"
    assert REF.scope == "arcus_testnet"
    for args in (
        ("arcus_testnet", ADDR, 0),
        ("Testnet", ADDR, 0),
        ("testnet", ADDR[:-1], 0),
        ("testnet", ADDR, 10),
        ("testnet", ADDR, -1),
        ("testnet", ADDR, True),
    ):
        with pytest.raises(ValueError):
            ArcusAccountRef(*args)


def test_normalize_address_never_echoes():
    assert normalize_address(ADDR[2:].upper()) == ADDR
    with pytest.raises(ValueError) as exc:
        normalize_address("0xnothex-secretish")
    assert "secretish" not in str(exc.value)
    with pytest.raises(ValueError):
        normalize_address(None)


def test_client_id_format():
    assert base36(0) == "0" and base36(35) == "z" and base36(36) == "10"
    assert base36(10000) == "7ps" and base36(100) == "2s"
    assert client_id_for(10000, 100, 1) == "nb7ps_2s-1"
    assert user_client_prefix(10000) == "nb7ps_"
    assert session_client_prefix(10000, 100) == "nb7ps_2s-"
    worst = client_id_for(36**8 - 1, 36**7 - 1, 36**9 - 1)
    assert len(worst) == 28
    with pytest.raises(ValueError):
        client_id_for(36**20, 36**20, 36**20)
    for bad in (-1, True, 1.0):
        with pytest.raises(ValueError):
            base36(bad)  # type: ignore[arg-type]
