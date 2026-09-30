"""users/arcus_credentials.py + the Arcus bits of users/venue_service.py against
real Postgres (Arcus P3b, 03 §19.3).

Auto-skips without a reachable local DB (conftest scrubs remote DSNs and runs
init_db, so the shipped 0022 DDL is what is exercised). Ids 990_033_001-020;
``DELETE FROM users`` cascades the credential rows.
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet


def _db_reachable() -> bool:
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        import psycopg2

        psycopg2.connect(os.environ["DATABASE_URL"]).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="no reachable Postgres (DATABASE_URL)")

_U1, _U2, _U3 = 990_033_001, 990_033_002, 990_033_003
_USERS = (_U1, _U2, _U3)
_ADDR = "0x" + "ab" * 20
_ADDR2 = "0x" + "cd" * 20
_SEED_A = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
_SEED_B = bytes(range(32)).hex()
_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _cleanup():
    from src.nadobro.db import execute
    from src.nadobro.users.user_service import invalidate_user_cache

    for uid in _USERS:
        execute("DELETE FROM audit_logs WHERE user_id = %s", (uid,))
        execute("DELETE FROM users WHERE telegram_id = %s", (uid,))
        execute("DELETE FROM bot_state WHERE key LIKE %s", (f"arcus_key_notice:{uid}:%",))
        invalidate_user_cache(uid)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    from src.nadobro.core import crypto

    monkeypatch.delenv("ENCRYPTION_KEYS", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_ALLOWED_USER_IDS", raising=False)
    monkeypatch.delenv("ARCUS_MAINNET_ENABLED", raising=False)
    _cleanup()
    for uid in _USERS:
        _new_user(uid)
    yield
    _cleanup()
    crypto._fernet_instance = None


def _new_user(uid):
    from src.nadobro.db import execute

    execute(
        "INSERT INTO users (telegram_id, telegram_username, language, network_mode) VALUES (%s, %s, %s, %s)",
        (uid, "pytest_arcus_creds", "en", "mainnet"),
    )


def _upsert(uid, *, seed=_SEED_A, address=_ADDR, network="testnet", name="nadobro-ab12", until=0, all_sub=False):
    from src.nadobro.users import arcus_credentials as creds

    return creds.upsert_active_credential(
        user_id=uid,
        network=network,
        address=address,
        all_subaccounts=all_sub,
        sealed=creds.seal_signing_seed(seed),
        api_wallet_name=name,
        valid_until_ms=until,
        attested_at=_NOW,
    )


def _raw(uid, network="testnet"):
    from src.nadobro.db import query_one

    return query_one("SELECT * FROM arcus_credentials WHERE user_id = %s AND network = %s", (uid, network))


def _count(uid):
    from src.nadobro.db import query_count

    return query_count("SELECT COUNT(*) FROM arcus_credentials WHERE user_id = %s", (uid,))


# ---------------------------------------------------------------------------


def test_first_upsert_inserts_an_active_row_with_double_base64_fernet():
    from src.nadobro.core.crypto import decrypt_with_server_key
    from src.nadobro.venue.arcus.signing import derive_public_key_hex

    row = _upsert(_U1)
    assert row.status == "active" and row.account_index == 0 and row.address == _ADDR
    assert row.api_public_key == derive_public_key_hex(_SEED_A)
    assert not hasattr(row, "encrypted_signing_key")
    raw = _raw(_U1)
    token = base64.b64decode(raw["encrypted_signing_key"])
    assert decrypt_with_server_key(token) == bytes.fromhex(_SEED_A)
    assert raw["attested_at"] == _NOW and raw["last_verified_at"] is not None


def test_renewal_is_one_row_swapped_in_place():
    first = _upsert(_U1)
    raw1 = _raw(_U1)
    second = _upsert(_U1, seed=_SEED_B, name="nadobro-cd34", until=1_800_000_000_000)
    raw2 = _raw(_U1)
    assert raw2["id"] == raw1["id"]
    assert second.api_public_key != first.api_public_key
    assert second.status == "active" and second.valid_until_ms == 1_800_000_000_000
    assert raw2["linked_at"] >= raw1["linked_at"]
    assert _count(_U1) == 1


def test_address_change_moves_the_row_and_frees_the_old_address():
    from src.nadobro.users import arcus_credentials as creds

    _upsert(_U1, address=_ADDR)
    _upsert(_U1, address=_ADDR2)
    assert creds.owner_of("testnet", _ADDR, 0) is None
    assert creds.owner_of("testnet", _ADDR2, 0) == _U1
    _upsert(_U2, address=_ADDR, seed=_SEED_B)  # the old address is free for another user
    assert creds.owner_of("testnet", _ADDR, 0) == _U2


def test_second_user_on_an_active_address_is_refused_by_the_precheck():
    from src.nadobro.users import arcus_credentials as creds

    _upsert(_U1)
    with pytest.raises(creds.ArcusAddressTaken):
        _upsert(_U2, seed=_SEED_B)
    assert _raw(_U2) is None
    # The same address on MAINNET for the second user is allowed.
    row = _upsert(_U2, seed=_SEED_B, network="mainnet")
    assert row.network == "mainnet" and creds.owner_of("mainnet", _ADDR, 0) == _U2


def test_second_user_race_hits_the_partial_unique_index(monkeypatch):
    """Force the race: another user's ACTIVE row appears between the pre-check
    and the upsert (inside the same transaction's view) -> pgcode 23505."""
    from src.nadobro import db
    from src.nadobro.users import arcus_credentials as creds

    _upsert(_U1)  # U1 holds (testnet, _ADDR, 0)
    real = db.run_transaction

    def racing(work):
        class _Cur:
            def __init__(self, cur):
                self._cur = cur
                self._first = True

            def execute(self, sql, params=None):
                if self._first and sql.lstrip().startswith("SELECT user_id"):
                    self._first = False
                    # the pre-check "misses" the concurrent owner
                    return self._cur.execute("SELECT 1 WHERE false")
                return self._cur.execute(sql, params)

            def fetchone(self):
                return self._cur.fetchone()

        return real(lambda cur: work(_Cur(cur)))

    monkeypatch.setattr(creds._db, "run_transaction", racing)
    with pytest.raises(creds.ArcusAddressTaken):
        _upsert(_U2, seed=_SEED_B)
    monkeypatch.setattr(creds._db, "run_transaction", real)
    assert _raw(_U2) is None
    assert creds.owner_of("testnet", _ADDR, 0) == _U1


def test_owner_of_returns_only_active_owners():
    from src.nadobro.users import arcus_credentials as creds

    _upsert(_U1)
    assert creds.owner_of("testnet", _ADDR, 0) == _U1
    assert creds.owner_of("testnet", _ADDR, 1) is None
    assert creds.mark_status(_U1, "testnet", "expired", wipe_secret=False) is True
    assert creds.owner_of("testnet", _ADDR, 0) is None


def test_mark_status_is_pubkey_guarded_and_unlink_wipes():
    from src.nadobro.users import arcus_credentials as creds

    old = _upsert(_U1)
    new = _upsert(_U1, seed=_SEED_B, name="nadobro-cd34")  # renewal
    # a sweep that read the OLD key cannot invalidate the renewed one
    assert creds.mark_status(_U1, "testnet", "invalid", wipe_secret=False, api_public_key=old.api_public_key) is False
    assert creds.get_credential(_U1, "testnet").status == "active"
    assert creds.mark_status(_U1, "testnet", "invalid", wipe_secret=False, api_public_key=new.api_public_key) is True
    raw = _raw(_U1)
    assert raw["status"] == "invalid" and raw["encrypted_signing_key"] != ""  # invalid keeps ciphertext
    assert creds.load_auth(_U1, "testnet") is None  # invalid is refused
    assert creds.mark_status(_U1, "testnet", "unlinked", wipe_secret=True) is True
    raw = _raw(_U1)
    assert raw["status"] == "unlinked" and raw["encrypted_signing_key"] == ""
    assert creds.load_auth(_U1, "testnet") is None
    # an unlinked row never moves back to expired / invalid
    assert creds.mark_status(_U1, "testnet", "expired", wipe_secret=False) is False
    assert _raw(_U1)["status"] == "unlinked"


def test_load_auth_for_active_and_expired_rows():
    from src.nadobro.users import arcus_credentials as creds

    row = _upsert(_U1)
    auth = creds.load_auth(_U1, "testnet")
    assert auth is not None and auth.api_key_hex == row.api_public_key and auth.ref.account_index == 0
    creds.mark_status(_U1, "testnet", "expired", wipe_secret=False, api_public_key=row.api_public_key)
    assert creds.load_auth(_U1, "testnet") is not None  # expired keys load for cancel-only use


def test_touch_verified_guards_and_revival():
    from src.nadobro.users import arcus_credentials as creds

    row = _upsert(_U1, until=1_800_000_000_000)
    other_pub = creds.seal_signing_seed(_SEED_B).api_public_key
    assert creds.touch_verified(_U1, "testnet", api_public_key=other_pub, valid_until_ms=0, status="active") is False
    creds.mark_status(_U1, "testnet", "expired", wipe_secret=False)
    assert creds.touch_verified(
        _U1, "testnet", api_public_key=row.api_public_key, valid_until_ms=1_900_000_000_000, status="active"
    ) is True
    revived = creds.get_credential(_U1, "testnet")
    assert revived.status == "active" and revived.valid_until_ms == 1_900_000_000_000


def test_invalid_revival_conflicting_with_another_active_owner_returns_false():
    from src.nadobro.users import arcus_credentials as creds

    row1 = _upsert(_U1)
    creds.mark_status(_U1, "testnet", "invalid", wipe_secret=False, api_public_key=row1.api_public_key)
    _upsert(_U2, seed=_SEED_B)  # U2 takes the address while U1's row is invalid
    assert creds.touch_verified(
        _U1, "testnet", api_public_key=row1.api_public_key, valid_until_ms=0, status="active"
    ) is False
    assert creds.get_credential(_U1, "testnet").status == "invalid"


def test_lifecycle_listing_and_boot_check():
    from src.nadobro.users import arcus_credentials as creds
    from src.nadobro.users.venue_service import has_live_arcus_credentials

    def ours(rows):
        return {(r.user_id, r.network, r.status) for r in rows if r.user_id in _USERS}

    assert not ours(creds.list_lifecycle_credentials())
    a = _upsert(_U1)
    b = _upsert(_U2, seed=_SEED_B, address=_ADDR2)
    c = _upsert(_U3, seed=_SEED_A, address=_ADDR, network="mainnet")
    creds.mark_status(_U2, "testnet", "expired", wipe_secret=False, api_public_key=b.api_public_key)
    creds.mark_status(_U3, "mainnet", "invalid", wipe_secret=False, api_public_key=c.api_public_key)
    assert ours(creds.list_lifecycle_credentials()) == {(_U1, "testnet", "active"), (_U2, "testnet", "expired")}
    assert {(r.user_id, r.network) for r in creds.list_active_credentials() if r.user_id in _USERS} == {(_U1, "testnet")}
    assert not [r for r in creds.list_active_credentials("mainnet") if r.user_id in _USERS]
    assert set(creds.get_credentials_for_user(_U3)) == {"mainnet"}
    assert creds.get_active_credential(_U2, "testnet") is None
    assert creds.get_active_credential(_U1, "testnet").api_public_key == a.api_public_key
    assert has_live_arcus_credentials() is True
    creds.mark_status(_U1, "testnet", "unlinked", wipe_secret=True)
    creds.mark_status(_U2, "testnet", "unlinked", wipe_secret=True)
    assert not ours(creds.list_lifecycle_credentials())


def test_has_live_arcus_credentials_flips(monkeypatch):
    from src.nadobro.users import arcus_credentials as creds
    from src.nadobro.users.venue_service import has_live_arcus_credentials

    others = [r for r in creds.list_lifecycle_credentials() if r.user_id not in _USERS]
    if others:
        pytest.skip("the test database holds other live Arcus rows")
    assert has_live_arcus_credentials() is False
    row = _upsert(_U1)
    creds.mark_status(_U1, "testnet", "expired", wipe_secret=False, api_public_key=row.api_public_key)
    assert has_live_arcus_credentials() is True  # one expired row
    creds.mark_status(_U1, "testnet", "unlinked", wipe_secret=True)
    assert has_live_arcus_credentials() is False


def test_key_notice_state_round_trip_per_scope():
    from src.nadobro.db import query_one
    from src.nadobro.users import arcus_credentials as creds

    state_t = {"pub": "ab" * 32, "sent": [14, 7], "expired_notified": False}
    state_m = {"pub": "cd" * 32, "sent": [], "expired_notified": True}
    creds.save_key_notice_state(_U1, "testnet", state_t)
    creds.save_key_notice_state(_U1, "mainnet", state_m)
    assert creds.get_key_notice_state(_U1, "testnet") == state_t
    assert creds.get_key_notice_state(_U1, "mainnet") == state_m
    assert query_one("SELECT key FROM bot_state WHERE key = %s", (f"arcus_key_notice:{_U1}:arcus_testnet",))
    assert creds.get_key_notice_state(_U2, "testnet") is None


# --- users.venue_service Arcus network mode -----------------------------------------------------


def test_arcus_network_mode_cas_and_gates(monkeypatch):
    from src.nadobro.db import query_one
    from src.nadobro.users import venue_service as vs

    assert vs.get_arcus_network_mode(_U1) == "testnet"
    # mainnet needs cohort + ARCUS_MAINNET_ENABLED
    assert vs.set_arcus_network_mode(_U1, "mainnet") == "not_allowed"
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_U1))
    assert vs.set_arcus_network_mode(_U1, "mainnet") == "not_allowed"
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    assert vs.set_arcus_network_mode(_U1, "mainnet") == "switched"
    assert vs.get_arcus_network_mode(_U1) == "mainnet"
    assert vs.set_arcus_network_mode(_U1, "mainnet") == "unchanged"
    # Nado's network_mode is untouched
    assert query_one("SELECT network_mode FROM users WHERE telegram_id = %s", (_U1,))["network_mode"] == "mainnet"
    audit = query_one(
        "SELECT details FROM audit_logs WHERE user_id = %s AND action = 'arcus_mode_switched'", (_U1,)
    )
    assert audit and audit["details"] == "testnet->mainnet"
    # back to testnet is never gated
    monkeypatch.delenv("ARCUS_ENABLED")
    assert vs.set_arcus_network_mode(_U1, "testnet") == "switched"
    assert vs.get_arcus_network_mode(_U1) == "testnet"
    # a user without a row
    assert vs.get_arcus_network_mode(990_033_019) == "testnet"
    assert vs.set_arcus_network_mode(990_033_019, "testnet") == "unchanged"
