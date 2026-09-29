"""Venue selection + arcus_credentials against real Postgres (Arcus P1).

Auto-skips without a reachable local DB. conftest runs ``init_db()`` at session
start, so these exercise the SHIPPED boot DDL (db.py), and the 0022 migration
file is re-applied on top to prove the two agree and are idempotent.
"""
from __future__ import annotations

import os
import pathlib

import pytest


def _db_reachable() -> bool:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL")):
        return False
    try:
        import psycopg2

        url = os.environ.get("SUPABASE_DATABASE_URL") or os.environ["DATABASE_URL"]
        psycopg2.connect(url).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="no reachable Postgres (DATABASE_URL)")

_MIG = pathlib.Path("src/nadobro/migrations/0022_venue_selection_and_arcus_credentials.sql")
_U1, _U2, _U3 = 990_022_001, 990_022_002, 990_022_003
_USERS = (_U1, _U2, _U3)
_MISSING_USER = 990_022_009
_ADDR = "0x" + "ab" * 20
_PUB = "cd" * 32


def _cleanup():
    from src.nadobro.db import execute
    from src.nadobro.users.user_service import invalidate_user_cache

    for uid in (*_USERS, _MISSING_USER):
        execute("DELETE FROM audit_logs WHERE user_id = %s", (uid,))
        execute("DELETE FROM users WHERE telegram_id = %s", (uid,))  # cascades arcus_credentials
        invalidate_user_cache(uid)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_ALLOWED_USER_IDS", raising=False)
    _cleanup()
    yield
    _cleanup()


def _allow(monkeypatch, *uids):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", ",".join(str(u) for u in uids))


def _raw_user(uid):
    from src.nadobro.db import query_one

    return query_one("SELECT * FROM users WHERE telegram_id = %s", (uid,))


def _new_user(uid, network_mode="mainnet"):
    from src.nadobro.db import execute

    execute(
        "INSERT INTO users (telegram_id, telegram_username, language, network_mode) VALUES (%s, %s, %s, %s)",
        (uid, "pytest_venue", "en", network_mode),
    )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

def test_boot_ddl_created_columns_with_defaults_and_named_checks():
    from src.nadobro.db import query_all

    cols = {
        r["column_name"]: r
        for r in query_all(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'users' "
            "AND column_name IN ('active_venue', 'arcus_network_mode')"
        )
    }
    assert set(cols) == {"active_venue", "arcus_network_mode"}
    for name, default in (("active_venue", "'nado'"), ("arcus_network_mode", "'testnet'")):
        assert cols[name]["data_type"] == "text"
        assert cols[name]["is_nullable"] == "NO"
        assert cols[name]["column_default"].startswith(default), cols[name]["column_default"]
    checks = {
        r["conname"]
        for r in query_all(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'public.users'::regclass AND contype = 'c'"
        )
    }
    assert {"users_active_venue_check", "users_arcus_network_mode_check"} <= checks
    # The Nado-only network column stays unconstrained (legacy rows must never fail boot).
    assert not any("network_mode" in c and "arcus" not in c for c in checks)


def test_boot_ddl_created_arcus_credentials_with_constraints():
    from src.nadobro.db import query_all

    cons = {
        r["conname"]: r["contype"]
        for r in query_all(
            "SELECT conname, contype FROM pg_constraint WHERE conrelid = 'public.arcus_credentials'::regclass"
        )
    }
    for name, kind in (
        ("arcus_credentials_pkey", "p"),
        ("arcus_credentials_user_id_fkey", "f"),
        ("arcus_credentials_user_id_network_key", "u"),
        ("arcus_credentials_network_check", "c"),
        ("arcus_credentials_address_check", "c"),
        ("arcus_credentials_account_index_check", "c"),
        ("arcus_credentials_api_public_key_check", "c"),
        ("arcus_credentials_valid_until_ms_check", "c"),
        ("arcus_credentials_status_check", "c"),
    ):
        assert cons.get(name) == kind, (name, cons)
    idx = query_all(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname = 'arcus_credentials_active_subaccount_uq'"
    )
    assert len(idx) == 1
    assert "UNIQUE" in idx[0]["indexdef"] and "WHERE (status = 'active'::text)" in idx[0]["indexdef"]


def test_existing_rows_read_as_nado_and_rerun_is_idempotent():
    """0022 over a POPULATED users table (hermetic: a session-local temp `users`
    shadows the real one, all rolled back): existing rows get ('nado',
    'testnet'), network_mode is kept, and a second run changes nothing."""
    import psycopg2

    sql = _MIG.read_text()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE users (id SERIAL PRIMARY KEY, telegram_id BIGINT UNIQUE NOT NULL, "
                "network_mode TEXT DEFAULT 'mainnet')"
            )
            cur.execute(
                "INSERT INTO users (telegram_id, network_mode) VALUES (1, 'mainnet'), (2, 'testnet'), "
                "(3, NULL), (4, 'MAINNET')"
            )
            cur.execute(sql)
            cur.execute(sql)  # idempotent
            cur.execute("SELECT telegram_id, network_mode, active_venue, arcus_network_mode FROM users ORDER BY 1")
            rows = cur.fetchall()
        assert rows == [
            (1, "mainnet", "nado", "testnet"),
            (2, "testnet", "nado", "testnet"),
            (3, None, "nado", "testnet"),
            (4, "MAINNET", "nado", "testnet"),
        ]
    finally:
        conn.rollback()
        conn.close()


def test_migration_file_and_init_db_rerun_cleanly_over_the_boot_schema():
    from src.nadobro.db import execute
    from src.nadobro.models.database import init_db
    from src.nadobro.users.user_service import get_or_create_user

    user, created, _ = get_or_create_user(_U1, username="pytest_venue")
    assert created
    execute(_MIG.read_text())
    execute(_MIG.read_text())
    init_db()
    raw = _raw_user(_U1)
    assert (raw["active_venue"], raw["arcus_network_mode"]) == ("nado", "testnet")


# ---------------------------------------------------------------------------
# users.active_venue + venue_service
# ---------------------------------------------------------------------------

def test_new_user_defaults_to_nado():
    from src.nadobro.users.user_service import get_or_create_user, get_user, invalidate_user_cache
    from src.nadobro.users.venue_service import get_active_venue

    user, created, _ = get_or_create_user(_U1, username="pytest_venue")
    assert created
    raw = _raw_user(_U1)
    assert (raw["active_venue"], raw["arcus_network_mode"], raw["network_mode"]) == ("nado", "testnet", "mainnet")
    assert (user.active_venue, user.arcus_network_mode) == ("nado", "testnet")
    invalidate_user_cache(_U1)
    fresh = get_user(_U1)
    assert (fresh.active_venue, fresh.arcus_network_mode) == ("nado", "testnet")
    assert get_active_venue(_U1) == "nado"


def test_switch_round_trip_invalidates_cache_and_touches_only_active_venue(monkeypatch):
    from src.nadobro.users.user_service import get_or_create_user, get_user
    from src.nadobro.users.venue_service import get_active_venue, set_active_venue

    _allow(monkeypatch, _U1)
    get_or_create_user(_U1, username="pytest_venue")
    assert get_user(_U1).active_venue == "nado"  # primes the 10 s cache
    before = _raw_user(_U1)

    assert set_active_venue(_U1, "arcus") == "switched"
    assert get_active_venue(_U1) == "arcus"  # immediately: the cache was invalidated
    after = _raw_user(_U1)
    assert after["active_venue"] == "arcus"
    assert {k: v for k, v in after.items() if k != "active_venue"} == {
        k: v for k, v in before.items() if k != "active_venue"
    }

    assert set_active_venue(_U1, "arcus") == "unchanged"
    assert set_active_venue(_U1, "nado") == "switched"
    assert get_active_venue(_U1) == "nado"
    assert set_active_venue(_U1, "nado") == "unchanged"
    final = _raw_user(_U1)
    assert final == before  # nothing else in users changed across the round trip


def test_switch_writes_audit_rows(monkeypatch):
    from src.nadobro.db import query_all
    from src.nadobro.users.user_service import get_or_create_user
    from src.nadobro.users.venue_service import set_active_venue

    _allow(monkeypatch, _U1)
    get_or_create_user(_U1, username="pytest_venue")
    set_active_venue(_U1, "arcus")
    set_active_venue(_U1, "arcus")  # CAS miss: no audit row
    set_active_venue(_U1, "nado")
    rows = query_all(
        "SELECT action, details FROM audit_logs WHERE user_id = %s ORDER BY id", (_U1,)
    )
    assert rows == [
        {"action": "venue_switched", "details": "nado->arcus"},
        {"action": "venue_switched", "details": "arcus->nado"},
    ]


def test_switch_to_arcus_with_flag_off_leaves_row_untouched(monkeypatch):
    from src.nadobro.users.user_service import get_or_create_user
    from src.nadobro.users.venue_service import get_active_venue, set_active_venue

    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_U1))  # allowlisted, but flag OFF
    get_or_create_user(_U1, username="pytest_venue")
    before = _raw_user(_U1)
    assert set_active_venue(_U1, "arcus") == "not_allowed"
    assert _raw_user(_U1) == before
    assert get_active_venue(_U1) == "nado"


def test_switch_for_a_user_without_a_row_is_unchanged(monkeypatch):
    from src.nadobro.users.venue_service import get_active_venue, set_active_venue

    _allow(monkeypatch, _MISSING_USER)
    assert set_active_venue(_MISSING_USER, "arcus") == "unchanged"
    assert get_active_venue(_MISSING_USER) == "nado"
    assert _raw_user(_MISSING_USER) is None


def test_back_to_nado_works_with_flag_off_for_a_user_already_on_arcus():
    from src.nadobro.db import execute
    from src.nadobro.users.venue_service import get_active_venue, set_active_venue

    _new_user(_U1)
    execute("UPDATE users SET active_venue = 'arcus' WHERE telegram_id = %s", (_U1,))
    assert get_active_venue(_U1) == "arcus"
    assert set_active_venue(_U1, "nado") == "switched"
    assert get_active_venue(_U1) == "nado"


def test_count_users_on_venue_tracks_switches(monkeypatch):
    from src.nadobro.users.venue_service import count_users_on_venue, set_active_venue

    _allow(monkeypatch, _U1, _U2)
    base_arcus = count_users_on_venue("arcus")
    base_nado = count_users_on_venue("nado")
    _new_user(_U1)
    _new_user(_U2, network_mode="testnet")
    assert count_users_on_venue("nado") - base_nado == 2
    assert set_active_venue(_U1, "arcus") == "switched"
    assert count_users_on_venue("arcus") - base_arcus == 1
    assert count_users_on_venue("nado") - base_nado == 1
    assert set_active_venue(_U1, "nado") == "switched"
    assert count_users_on_venue("arcus") - base_arcus == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("active_venue", "ARCUS"),
        ("active_venue", "Nado"),
        ("active_venue", "arcus_mainnet"),
        ("active_venue", ""),
        ("arcus_network_mode", "arcus_testnet"),
        ("arcus_network_mode", "MAINNET"),
        ("arcus_network_mode", ""),
    ],
)
def test_users_checks_reject_bad_values(column, value):
    import psycopg2.errors

    from src.nadobro.db import execute

    _new_user(_U1)
    with pytest.raises(psycopg2.errors.CheckViolation):
        execute(f"UPDATE users SET {column} = %s WHERE telegram_id = %s", (value, _U1))
    raw = _raw_user(_U1)
    assert (raw["active_venue"], raw["arcus_network_mode"]) == ("nado", "testnet")


@pytest.mark.parametrize("column", ["active_venue", "arcus_network_mode"])
def test_users_venue_columns_reject_null(column):
    import psycopg2.errors

    from src.nadobro.db import execute

    _new_user(_U1)
    with pytest.raises(psycopg2.errors.NotNullViolation):
        execute(f"UPDATE users SET {column} = NULL WHERE telegram_id = %s", (_U1,))


# ---------------------------------------------------------------------------
# arcus_credentials constraint matrix
# ---------------------------------------------------------------------------

def _insert_cred(user_id, **overrides):
    from src.nadobro.db import execute_returning

    row = {
        "user_id": user_id,
        "network": "testnet",
        "address": _ADDR,
        "account_index": 0,
        "api_public_key": _PUB,
        "encrypted_signing_key": "enc-token",
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    return execute_returning(
        f"INSERT INTO arcus_credentials ({cols}) VALUES ({marks}) RETURNING *", tuple(row.values())
    )


def test_arcus_credentials_defaults():
    _new_user(_U1)
    from src.nadobro.db import execute_returning

    row = execute_returning(
        "INSERT INTO arcus_credentials (user_id, network, address, api_public_key, encrypted_signing_key) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (_U1, "mainnet", _ADDR, _PUB, "enc-token"),
    )
    assert row["account_index"] == 0
    assert row["all_subaccounts"] is False
    assert row["status"] == "active"
    assert row["linked_at"] is not None and row["updated_at"] is not None
    assert row["valid_until_ms"] is None and row["attested_at"] is None and row["last_verified_at"] is None
    assert row["api_wallet_name"] is None


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"address": "0x" + "AB" * 20}, "CheckViolation"),
        ({"address": "ab" * 20}, "CheckViolation"),
        ({"address": "0x" + "ab" * 19}, "CheckViolation"),
        ({"account_index": 10}, "CheckViolation"),
        ({"account_index": -1}, "CheckViolation"),
        ({"api_public_key": "cd" * 31 + "c"}, "CheckViolation"),
        ({"api_public_key": "0x" + "cd" * 31}, "CheckViolation"),
        ({"api_public_key": "CD" * 32}, "CheckViolation"),
        ({"status": "revoked"}, "CheckViolation"),
        ({"network": "arcus_testnet"}, "CheckViolation"),
        ({"network": "MAINNET"}, "CheckViolation"),
        ({"valid_until_ms": -1}, "CheckViolation"),
        ({"encrypted_signing_key": None}, "NotNullViolation"),
        ({"user_id": _MISSING_USER}, "ForeignKeyViolation"),
    ],
)
def test_arcus_credentials_rejects(overrides, error):
    import psycopg2.errors

    _new_user(_U1)
    fields = dict(overrides)  # never mutate the shared parametrize dict
    user_id = fields.pop("user_id", _U1)
    with pytest.raises(getattr(psycopg2.errors, error)):
        _insert_cred(user_id, **fields)


def test_arcus_credentials_uniqueness_rules():
    import psycopg2.errors

    from src.nadobro.db import execute

    for uid in (_U1, _U2, _U3):
        _new_user(uid)
    _insert_cred(_U1, valid_until_ms=0)
    # One link per (user, network).
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert_cred(_U1, account_index=1)
    # Two Telegram users can never drive the same ACTIVE subaccount.
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert_cred(_U2)
    # ...but a different subaccount of the same address is fine,
    assert _insert_cred(_U2, account_index=1, valid_until_ms=None)["account_index"] == 1
    # as is the same subaccount on the other network,
    assert _insert_cred(_U3, network="mainnet")["network"] == "mainnet"
    # and once the older link is unlinked, the subaccount can be re-linked.
    execute("DELETE FROM arcus_credentials WHERE user_id = %s", (_U2,))
    execute("UPDATE arcus_credentials SET status = 'unlinked' WHERE user_id = %s", (_U1,))
    assert _insert_cred(_U2)["status"] == "active"


def test_arcus_credentials_cascade_on_user_delete():
    from src.nadobro.db import execute, query_count

    _new_user(_U1)
    _insert_cred(_U1)
    _insert_cred(_U1, network="mainnet")
    assert query_count("SELECT COUNT(*) FROM arcus_credentials WHERE user_id = %s", (_U1,)) == 2
    execute("DELETE FROM users WHERE telegram_id = %s", (_U1,))
    assert query_count("SELECT COUNT(*) FROM arcus_credentials WHERE user_id = %s", (_U1,)) == 0
