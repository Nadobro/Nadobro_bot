"""users/arcus_credentials.py without a database (03 §6, §19.2)."""
from __future__ import annotations

import base64
import copy
import dataclasses
import pickle
import re
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from src.nadobro.core import crypto
from src.nadobro.users import arcus_credentials as creds
from src.nadobro.venue.arcus import signing

RFC_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC_PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
OTHER_SEED = bytes(range(32)).hex()
ADDR = "0x" + "ab" * 20
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEYS", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    yield
    crypto._fernet_instance = None


def _fail_db(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("no DB call expected")

    for name in ("run_transaction", "query_one", "query_all", "execute_returning", "execute"):
        monkeypatch.setattr(creds._db, name, boom)


# --- sealing ------------------------------------------------------------------------------


def test_seal_derives_the_pubkey_and_encrypts_the_seed_bytes():
    sealed = creds.seal_signing_seed(RFC_SEED)
    assert sealed.api_public_key == RFC_PUB
    assert crypto.decrypt_with_server_key(sealed.token) == bytes.fromhex(RFC_SEED)
    # 0x / uppercase / whitespace are normalized like a paste
    assert creds.seal_signing_seed("0x" + RFC_SEED.upper()).api_public_key == RFC_PUB


def test_sealed_key_is_redacted_and_not_copyable():
    sealed = creds.seal_signing_seed(RFC_SEED)
    for text in (repr(sealed), str(sealed), f"{sealed}"):
        assert not _HEX64.search(text)
        assert "sealed" in text
    for fn in (pickle.dumps, copy.copy, copy.deepcopy, dataclasses.asdict):
        with pytest.raises(TypeError):
            fn(sealed)
    with pytest.raises(AttributeError):
        sealed.api_public_key = "x"  # type: ignore[misc]


def test_seal_rejects_garbage_without_echo():
    for bad in ("zz" * 32, "ab" * 31, "", "0x"):
        with pytest.raises(ValueError) as exc:
            creds.seal_signing_seed(bad)
        assert str(exc.value) == "invalid signing key"


# --- input validation before any SQL -----------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"address": ADDR.upper().replace("0X", "0x")},
        {"address": "0x" + "ab" * 19},
        {"network": "arcus_testnet"},
        {"network": "Testnet"},
        {"network": " testnet"},
        {"network": "mainnet "},
        {"valid_until_ms": -1},
        {"valid_until_ms": True},
        {"user_id": 0},
        {"user_id": True},
        {"api_wallet_name": "x" * 257},
        {"all_subaccounts": 1},
        {"attested_at": "2026-09-30"},
    ],
)
def test_upsert_validates_before_any_db_call(monkeypatch, overrides):
    _fail_db(monkeypatch)
    kwargs = dict(
        user_id=990_033_001,
        network="testnet",
        address=ADDR,
        all_subaccounts=False,
        sealed=creds.seal_signing_seed(RFC_SEED),
        api_wallet_name="nadobro-ab12",
        valid_until_ms=0,
        attested_at=NOW,
    )
    kwargs.update(overrides)
    with pytest.raises(ValueError) as exc:
        creds.upsert_active_credential(**kwargs)
    msg = str(exc.value)
    for value in overrides.values():
        if isinstance(value, str) and value.strip():
            assert value not in msg


def test_upsert_refuses_a_non_sealed_key(monkeypatch):
    _fail_db(monkeypatch)
    with pytest.raises(ValueError):
        creds.upsert_active_credential(
            user_id=1, network="testnet", address=ADDR, all_subaccounts=False,
            sealed=RFC_SEED, api_wallet_name=None, valid_until_ms=0, attested_at=NOW,  # type: ignore[arg-type]
        )


def test_sealed_key_rejects_a_bad_pubkey():
    with pytest.raises(ValueError):
        creds.SealedSigningKey("ab" * 31, b"tok")
    with pytest.raises(ValueError):
        creds.SealedSigningKey("AB" * 32, b"tok")
    with pytest.raises(ValueError):
        creds.SealedSigningKey("ab" * 32, b"")


def test_readers_validate_the_network(monkeypatch):
    _fail_db(monkeypatch)
    for bad in ("arcus_mainnet", "MAINNET", "", None):
        with pytest.raises(ValueError):
            creds.get_credential(1, bad)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            creds.owner_of(bad, ADDR, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        creds.owner_of("testnet", ADDR, 10)


def test_unique_violation_from_the_partial_index_is_address_taken(monkeypatch):
    class _UniqueViolation(Exception):
        pgcode = "23505"

    def raise_unique(_work):
        raise _UniqueViolation("duplicate key value violates unique constraint")

    monkeypatch.setattr(creds._db, "run_transaction", raise_unique)
    with pytest.raises(creds.ArcusAddressTaken):
        creds.upsert_active_credential(
            user_id=1, network="testnet", address=ADDR, all_subaccounts=False,
            sealed=creds.seal_signing_seed(RFC_SEED), api_wallet_name=None, valid_until_ms=0, attested_at=NOW,
        )

    def raise_other(_work):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(creds._db, "run_transaction", raise_other)
    with pytest.raises(RuntimeError):
        creds.upsert_active_credential(
            user_id=1, network="testnet", address=ADDR, all_subaccounts=False,
            sealed=creds.seal_signing_seed(RFC_SEED), api_wallet_name=None, valid_until_ms=0, attested_at=NOW,
        )


# --- pure helpers ---------------------------------------------------------------------------


def test_addr_short_and_format_utc_ms():
    assert creds.addr_short("0x" + "ab" * 20) == "0xabab…abab"
    assert creds.addr_short("0x1234567890abcdef1234567890abcdef1234abcd") == "0x1234…abcd"
    assert creds.addr_short(None) == "—"
    assert creds.addr_short("0x12") == "—"
    assert creds.format_utc_ms(1_790_000_000_000) == "2026-09-21 14:13 UTC"
    for bad in (0, -5, True, 1.5, "1790000000000", 10**30):
        with pytest.raises(ValueError):
            creds.format_utc_ms(bad)  # type: ignore[arg-type]


# --- mark_status / touch_verified rules ------------------------------------------------------


def test_mark_status_combinations(monkeypatch):
    _fail_db(monkeypatch)
    with pytest.raises(ValueError):
        creds.mark_status(1, "testnet", "expired", wipe_secret=True)
    with pytest.raises(ValueError):
        creds.mark_status(1, "testnet", "invalid", wipe_secret=True)
    with pytest.raises(ValueError):
        creds.mark_status(1, "testnet", "unlinked", wipe_secret=False)  # unlinked always wipes
    with pytest.raises(ValueError):
        creds.mark_status(1, "testnet", "active", wipe_secret=False)  # activation only via upsert/touch
    with pytest.raises(ValueError):
        creds.mark_status(1, "testnet", "expired", wipe_secret=False, api_public_key="nope")


def test_mark_status_sql_guards(monkeypatch):
    calls = []

    def fake(sql, params):
        calls.append((sql, params))
        return {"user_id": 1}

    monkeypatch.setattr(creds._db, "execute_returning", fake)
    assert creds.mark_status(1, "testnet", "expired", wipe_secret=False, api_public_key=RFC_PUB) is True
    sql, params = calls[-1]
    assert "status <> 'unlinked'" in sql and "api_public_key = %s" in sql
    assert params == ("expired", False, 1, "testnet", RFC_PUB)
    assert creds.mark_status(1, "mainnet", "unlinked", wipe_secret=True) is True
    sql, params = calls[-1]
    assert "status <> 'unlinked'" not in sql and "api_public_key" not in sql
    assert params == ("unlinked", True, 1, "mainnet")
    monkeypatch.setattr(creds._db, "execute_returning", lambda sql, params: None)
    assert creds.mark_status(1, "testnet", "invalid", wipe_secret=False) is False


def test_touch_verified_returns_false_on_unique_violation(monkeypatch):
    class _UniqueViolation(Exception):
        pgcode = "23505"

    def raise_unique(sql, params):
        raise _UniqueViolation()

    monkeypatch.setattr(creds._db, "execute_returning", raise_unique)
    assert creds.touch_verified(1, "testnet", api_public_key=RFC_PUB, valid_until_ms=0, status="active") is False
    with pytest.raises(ValueError):
        creds.touch_verified(1, "testnet", api_public_key=RFC_PUB, valid_until_ms=0, status="invalid")  # type: ignore[arg-type]


# --- load_auth ---------------------------------------------------------------------------------


def _raw_row(*, status="active", seed=RFC_SEED, pub=RFC_PUB, ciphertext=None):
    if ciphertext is None:
        ciphertext = base64.b64encode(crypto.encrypt_with_server_key(bytes.fromhex(seed))).decode("ascii")
    return {
        "user_id": 7,
        "network": "testnet",
        "address": ADDR,
        "account_index": 0,
        "all_subaccounts": False,
        "api_public_key": pub,
        "api_wallet_name": "nadobro-ab12",
        "valid_until_ms": 0,
        "status": status,
        "attested_at": NOW,
        "linked_at": NOW,
        "last_verified_at": NOW,
        "encrypted_signing_key": ciphertext,
    }


def test_load_auth_mismatched_ciphertext_raises_without_hex(monkeypatch):
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: _raw_row(seed=OTHER_SEED))
    with pytest.raises(creds.ArcusCredentialError) as exc:
        creds.load_auth(7, "testnet")
    assert not _HEX64.search(str(exc.value))
    assert exc.value.__cause__ is None


def test_load_auth_undecryptable_ciphertext(monkeypatch):
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: _raw_row(ciphertext="not-base64!!"))
    with pytest.raises(creds.ArcusCredentialError):
        creds.load_auth(7, "testnet")
    other = Fernet(Fernet.generate_key()).encrypt(bytes.fromhex(RFC_SEED))
    ct = base64.b64encode(other).decode("ascii")
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: _raw_row(ciphertext=ct))
    with pytest.raises(creds.ArcusCredentialError) as exc:
        creds.load_auth(7, "testnet")
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.parametrize("status", ["invalid", "unlinked"])
def test_load_auth_refuses_dead_rows(monkeypatch, status):
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: _raw_row(status=status))
    assert creds.load_auth(7, "testnet") is None


def test_load_auth_empty_ciphertext_and_missing_row(monkeypatch):
    row = _raw_row()
    row["encrypted_signing_key"] = ""
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: dict(row))
    assert creds.load_auth(7, "testnet") is None
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: None)
    assert creds.load_auth(7, "testnet") is None


@pytest.mark.parametrize("status", ["active", "expired"])
def test_load_auth_builds_through_make_auth(monkeypatch, status):
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: _raw_row(status=status))
    calls = []
    real = signing.make_auth

    def spy(ref, signer):
        calls.append(ref)
        return real(ref, signer)

    monkeypatch.setattr(creds, "make_auth", spy)
    auth = creds.load_auth(7, "testnet")
    assert auth is not None and auth.api_key_hex == RFC_PUB
    assert len(calls) == 1 and calls[0].address == ADDR and calls[0].account_index == 0
    assert not _HEX64.search(repr(auth))


def test_row_objects_never_carry_the_ciphertext(monkeypatch):
    raw = _raw_row()
    raw.pop("encrypted_signing_key")
    monkeypatch.setattr(creds._db, "query_one", lambda sql, params: dict(raw))
    row = creds.get_credential(7, "testnet")
    assert row is not None and not hasattr(row, "encrypted_signing_key")
    assert "encrypted_signing_key" not in {f.name for f in dataclasses.fields(row)}
    assert "encrypted_signing_key" not in creds._ROW_COLS
    assert row.ref().address == ADDR and row.expires_at_ms() is None


def test_row_rejects_a_foreign_network_or_status(monkeypatch):
    for bad in ({"network": "arcus_testnet"}, {"status": "weird"}):
        raw = _raw_row()
        raw.pop("encrypted_signing_key")
        raw.update(bad)
        monkeypatch.setattr(creds._db, "query_one", lambda sql, params, raw=raw: dict(raw))
        with pytest.raises(ValueError):
            creds.get_credential(7, "testnet")


def test_key_notice_key_uses_the_scope_token():
    assert creds.key_notice_key(42, "testnet") == "arcus_key_notice:42:arcus_testnet"
    assert creds.key_notice_key(42, "mainnet") == "arcus_key_notice:42:arcus_mainnet"
    with pytest.raises(ValueError):
        creds.key_notice_key(42, "arcus_testnet")  # type: ignore[arg-type]


def test_key_notice_state_round_trip_shapes(monkeypatch):
    stored = {}
    monkeypatch.setattr(creds, "set_bot_state", lambda key, value: stored.__setitem__(key, value))
    monkeypatch.setattr(creds, "get_bot_state", lambda key: stored.get(key))
    creds.save_key_notice_state(42, "testnet", {"pub": RFC_PUB, "sent": [14], "expired_notified": False})
    assert creds.get_key_notice_state(42, "testnet") == {"pub": RFC_PUB, "sent": [14], "expired_notified": False}
    assert creds.get_key_notice_state(42, "mainnet") is None
    stored["arcus_key_notice:42:arcus_mainnet"] = ["not", "a", "dict"]
    assert creds.get_key_notice_state(42, "mainnet") is None
    with pytest.raises(ValueError):
        creds.save_key_notice_state(42, "testnet", ["x"])  # type: ignore[arg-type]
