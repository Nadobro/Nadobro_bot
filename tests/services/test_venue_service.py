"""users/venue_service + UserRow venue fields (Arcus P1) — no DB.

Pins: the venue read is 'arcus' only on an exact match (no stray value routes a
user to Arcus); the switch is a pure compare-and-set with cache invalidation and
NO other side effect (venues run in parallel — nothing is stopped or started);
switching to Arcus is flag + allowlist gated, switching back never is.
"""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from src.nadobro.models.database import NetworkMode, UserRow
from src.nadobro.users import venue_service as vs
from src.nadobro.utils.venue_scope import active_venue_from_db, arcus_network_from_db

_UID = 990_022_101
_MISSING = object()


# ---------------------------------------------------------------------------
# UserRow mapping
# ---------------------------------------------------------------------------

def _row(**fields) -> dict:
    data = {"telegram_id": _UID, "network_mode": "mainnet"}
    for key, value in fields.items():
        if value is _MISSING:
            data.pop(key, None)
        else:
            data[key] = value
    return data


@pytest.mark.parametrize(
    "raw",
    [None, "", "nado", "ARCUS", "Arcus", " arcus", "arcus ", "arcus_mainnet", "arcus_testnet",
     "garbage", 1, True, b"arcus", _MISSING],
)
def test_userrow_active_venue_is_nado_unless_exactly_arcus(raw):
    user = UserRow(_row(active_venue=raw))
    assert user.active_venue == "nado"
    assert active_venue_from_db(None if raw is _MISSING else raw) == "nado"


def test_userrow_active_venue_exact_arcus():
    assert UserRow(_row(active_venue="arcus")).active_venue == "arcus"
    assert active_venue_from_db("arcus") == "arcus"


@pytest.mark.parametrize(
    "raw",
    [None, "", "testnet", "MAINNET", "Mainnet", " mainnet", "arcus_mainnet", "garbage", 1, _MISSING],
)
def test_userrow_arcus_network_mode_is_testnet_unless_exactly_mainnet(raw):
    assert UserRow(_row(arcus_network_mode=raw)).arcus_network_mode == "testnet"
    assert arcus_network_from_db(None if raw is _MISSING else raw) == "testnet"


def test_userrow_arcus_network_mode_exact_mainnet():
    user = UserRow(_row(arcus_network_mode="mainnet"))
    assert user.arcus_network_mode == "mainnet"
    # A plain str, deliberately NOT a NetworkMode: it can never be passed where
    # Nado code reads `.network_mode.value`.
    assert type(user.arcus_network_mode) is str
    assert not isinstance(user.arcus_network_mode, NetworkMode)


@pytest.mark.parametrize("nado_net", ["mainnet", "testnet"])
@pytest.mark.parametrize("arcus_net", ["mainnet", "testnet"])
@pytest.mark.parametrize("venue", ["nado", "arcus"])
def test_userrow_nado_network_mode_is_independent_of_the_venue_fields(nado_net, arcus_net, venue):
    user = UserRow(_row(network_mode=nado_net, arcus_network_mode=arcus_net, active_venue=venue))
    assert user.network_mode is NetworkMode(nado_net)
    assert user.arcus_network_mode == arcus_net
    assert user.active_venue == venue


def test_userrow_legacy_row_without_venue_columns_reads_as_nado():
    # A row read before the 0022 DDL ran (or with the Arcus block failed).
    user = UserRow({"telegram_id": _UID, "network_mode": "mainnet", "language": "en"})
    assert (user.active_venue, user.arcus_network_mode) == ("nado", "testnet")
    assert user.network_mode is NetworkMode.MAINNET
    # get_or_create_user's DB-miss fallback row
    fallback = UserRow({"telegram_id": _UID, "network_mode": "mainnet"})
    assert fallback.active_venue == "nado"


# ---------------------------------------------------------------------------
# get_active_venue
# ---------------------------------------------------------------------------

def _patch_user(monkeypatch, user):
    calls = []

    def _get_user(uid):
        calls.append(uid)
        if isinstance(user, Exception):
            raise user
        return user

    monkeypatch.setattr(vs, "get_user", _get_user)
    return calls


def test_get_active_venue_no_user_is_nado(monkeypatch):
    calls = _patch_user(monkeypatch, None)
    assert vs.get_active_venue(_UID) == "nado"
    assert calls == [_UID]


def test_get_active_venue_fake_without_attribute_is_nado(monkeypatch):
    # Existing tests stub get_user with SimpleNamespace(network_mode=...) only.
    _patch_user(monkeypatch, SimpleNamespace(network_mode=NetworkMode.MAINNET))
    assert vs.get_active_venue(_UID) == "nado"


@pytest.mark.parametrize(("stored", "expected"), [
    ("arcus", "arcus"), ("nado", "nado"), ("ARCUS", "nado"), ("arcus_mainnet", "nado"), (None, "nado"),
])
def test_get_active_venue_exact_match_only(monkeypatch, stored, expected):
    _patch_user(monkeypatch, SimpleNamespace(active_venue=stored))
    assert vs.get_active_venue(_UID) == expected


def test_get_active_venue_db_error_propagates(monkeypatch):
    # DENIED != EMPTY: an unreadable venue must not silently read as 'nado'.
    _patch_user(monkeypatch, RuntimeError("db down"))
    with pytest.raises(RuntimeError, match="db down"):
        vs.get_active_venue(_UID)


def test_get_active_venue_accepts_str_id(monkeypatch):
    calls = _patch_user(monkeypatch, SimpleNamespace(active_venue="arcus"))
    assert vs.get_active_venue(str(_UID)) == "arcus"
    assert calls == [_UID]


# ---------------------------------------------------------------------------
# peek_active_venue (the venue gate's no-IO fast path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("cached", "expected"), [
    (None, None),  # a cache miss is "unknown", never 'nado'
    (SimpleNamespace(active_venue="arcus"), "arcus"),
    (SimpleNamespace(active_venue="nado"), "nado"),
    (SimpleNamespace(active_venue="ARCUS"), "nado"),
    (SimpleNamespace(network_mode=NetworkMode.MAINNET), "nado"),
])
def test_peek_active_venue_reads_the_cache_only(monkeypatch, cached, expected):
    seen = []
    monkeypatch.setattr(vs, "_get_cached_user", lambda uid: seen.append(uid) or cached)
    monkeypatch.setattr(vs, "get_user", lambda *a: pytest.fail("peek must never touch Postgres"))
    assert vs.peek_active_venue(str(_UID)) == expected
    assert seen == [_UID]


# ---------------------------------------------------------------------------
# set_active_venue
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self, returning=None):
        self.returning = returning
        self.sql_calls: list[tuple[str, tuple]] = []
        self.invalidated: list = []
        self.audits: list[tuple] = []


@pytest.fixture()
def rec(monkeypatch):
    r = _Recorder()

    def _execute_returning(sql, params=None):
        r.sql_calls.append((sql, params))
        if isinstance(r.returning, Exception):
            raise r.returning
        return r.returning

    monkeypatch.setattr(vs, "execute_returning", _execute_returning)
    monkeypatch.setattr(vs, "invalidate_user_cache", lambda uid=None: r.invalidated.append(uid))
    monkeypatch.setattr(vs, "record_audit_event", lambda *a: r.audits.append(a))
    monkeypatch.setattr(vs, "query_count", lambda *a: pytest.fail("unexpected query_count"))
    monkeypatch.setattr(vs, "get_user", lambda *a: pytest.fail("set_active_venue must not read the user"))
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_ALLOWED_USER_IDS", raising=False)
    return r


def _allow(monkeypatch, uid=_UID):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(uid))


@pytest.mark.parametrize("bad", ["ARCUS", "Arcus", "arcus_mainnet", "arcus_testnet", "NADO", " nado", None, "", 1])
def test_set_active_venue_unknown_venue_raises_before_any_db_or_cache(monkeypatch, rec, bad):
    _allow(monkeypatch)
    with pytest.raises(ValueError, match="unknown venue"):
        vs.set_active_venue(_UID, bad)
    assert rec.sql_calls == [] and rec.invalidated == [] and rec.audits == []


@pytest.mark.parametrize("bad_uid", [0, -5, "0"])
def test_set_active_venue_non_positive_id_raises_before_any_db_or_cache(monkeypatch, rec, bad_uid):
    # invalidate_user_cache(0) would wipe EVERY user's cache entry.
    _allow(monkeypatch, uid=0)
    with pytest.raises(ValueError, match="invalid telegram_id"):
        vs.set_active_venue(bad_uid, "nado")
    assert rec.sql_calls == [] and rec.invalidated == []


def test_set_active_venue_arcus_with_flag_off_is_not_allowed(monkeypatch, rec):
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_UID))
    assert vs.set_active_venue(_UID, "arcus") == "not_allowed"
    assert rec.sql_calls == [] and rec.audits == []


def test_set_active_venue_arcus_not_allowlisted_is_not_allowed(monkeypatch, rec):
    _allow(monkeypatch, uid=_UID + 1)
    assert vs.set_active_venue(_UID, "arcus") == "not_allowed"
    assert rec.sql_calls == [] and rec.audits == []


def test_set_active_venue_to_arcus_switches_via_compare_and_set(monkeypatch, rec):
    _allow(monkeypatch)
    rec.returning = {"active_venue": "arcus"}
    assert vs.set_active_venue(_UID, "arcus") == "switched"
    assert len(rec.sql_calls) == 1
    sql, params = rec.sql_calls[0]
    assert params == ("arcus", _UID, "nado")
    flat = " ".join(sql.split())
    assert flat == (
        "UPDATE users SET active_venue = %s WHERE telegram_id = %s AND active_venue = %s "
        "RETURNING active_venue"
    )
    assert rec.invalidated == [_UID]
    assert rec.audits == [(_UID, "venue_switched", "nado->arcus")]


def test_set_active_venue_cas_miss_is_unchanged_without_audit(monkeypatch, rec):
    _allow(monkeypatch)
    rec.returning = None
    assert vs.set_active_venue(_UID, "arcus") == "unchanged"
    assert rec.invalidated == [_UID]
    assert rec.audits == []


def test_set_active_venue_back_to_nado_is_always_allowed(monkeypatch, rec):
    # Flag OFF and not allowlisted: a user already on Arcus is never stranded.
    rec.returning = {"active_venue": "nado"}
    assert vs.set_active_venue(_UID, "nado") == "switched"
    assert rec.sql_calls[0][1] == ("nado", _UID, "arcus")
    assert rec.invalidated == [_UID]
    assert rec.audits == [(_UID, "venue_switched", "arcus->nado")]


def test_set_active_venue_invalidates_cache_even_when_the_update_raises(monkeypatch, rec):
    _allow(monkeypatch)
    rec.returning = RuntimeError("UndefinedColumn: active_venue")
    with pytest.raises(RuntimeError, match="UndefinedColumn"):
        vs.set_active_venue(_UID, "arcus")
    assert rec.invalidated == [_UID]
    assert rec.audits == []


def test_set_active_venue_accepts_str_id(monkeypatch, rec):
    _allow(monkeypatch)
    rec.returning = {"active_venue": "arcus"}
    assert vs.set_active_venue(str(_UID), "arcus") == "switched"
    assert rec.sql_calls[0][1] == ("arcus", _UID, "nado")


# ---------------------------------------------------------------------------
# get_active_venue_fresh (the switch paths) + last_known_venue (the gate's
# unreadable-venue fallback)
# ---------------------------------------------------------------------------

@pytest.fixture()
def seen(monkeypatch):
    s: set[int] = set()
    monkeypatch.setattr(vs, "_last_seen_arcus", s)
    return s


def test_get_active_venue_fresh_drops_the_cached_row_before_reading(monkeypatch):
    order = []
    monkeypatch.setattr(vs, "invalidate_user_cache", lambda uid=None: order.append(("invalidate", uid)))
    monkeypatch.setattr(vs, "get_user", lambda uid: order.append(("read", uid)) or SimpleNamespace(active_venue="arcus"))
    assert vs.get_active_venue_fresh(str(_UID)) == "arcus"
    assert order == [("invalidate", _UID), ("read", _UID)]


@pytest.mark.parametrize("bad_uid", [0, -5, "0"])
def test_get_active_venue_fresh_rejects_non_positive_ids(monkeypatch, bad_uid):
    # invalidate_user_cache(0) would wipe EVERY user's cache entry.
    monkeypatch.setattr(vs, "invalidate_user_cache", lambda uid=None: pytest.fail("no invalidate"))
    with pytest.raises(ValueError, match="invalid telegram_id"):
        vs.get_active_venue_fresh(bad_uid)


def test_get_active_venue_fresh_db_error_propagates(monkeypatch):
    monkeypatch.setattr(vs, "invalidate_user_cache", lambda uid=None: None)
    _patch_user(monkeypatch, RuntimeError("db down"))
    with pytest.raises(RuntimeError, match="db down"):
        vs.get_active_venue_fresh(_UID)


def test_last_known_venue_is_nado_for_a_user_never_seen(seen, monkeypatch):
    monkeypatch.setattr(vs, "get_user", lambda *a: pytest.fail("last_known_venue does no IO"))
    monkeypatch.setattr(vs, "_get_cached_user", lambda *a: pytest.fail("last_known_venue does no IO"))
    assert vs.last_known_venue(_UID) == "nado"


def test_last_known_venue_follows_every_successful_read(seen, monkeypatch):
    _patch_user(monkeypatch, SimpleNamespace(active_venue="arcus"))
    vs.get_active_venue(_UID)
    assert vs.last_known_venue(str(_UID)) == "arcus"
    _patch_user(monkeypatch, SimpleNamespace(active_venue="nado"))
    vs.get_active_venue(_UID)
    assert vs.last_known_venue(_UID) == "nado"
    monkeypatch.setattr(vs, "_get_cached_user", lambda uid: SimpleNamespace(active_venue="arcus"))
    vs.peek_active_venue(_UID)
    assert vs.last_known_venue(_UID) == "arcus"
    monkeypatch.setattr(vs, "_get_cached_user", lambda uid: None)
    assert vs.peek_active_venue(_UID) is None  # a miss teaches nothing
    assert vs.last_known_venue(_UID) == "arcus"
    _patch_user(monkeypatch, None)  # no row -> nado
    vs.get_active_venue(_UID)
    assert vs.last_known_venue(_UID) == "nado"


def test_a_failed_read_keeps_the_last_known_venue(seen, monkeypatch):
    seen.add(_UID)
    _patch_user(monkeypatch, RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        vs.get_active_venue(_UID)
    assert vs.last_known_venue(_UID) == "arcus"


def test_last_known_venue_follows_a_switch_but_not_a_cas_miss(seen, monkeypatch, rec):
    _allow(monkeypatch)
    rec.returning = {"active_venue": "arcus"}
    assert vs.set_active_venue(_UID, "arcus") == "switched"
    assert vs.last_known_venue(_UID) == "arcus"
    rec.returning = None
    assert vs.set_active_venue(_UID, "nado") == "unchanged"
    assert vs.last_known_venue(_UID) == "arcus"  # a miss proves nothing
    rec.returning = {"active_venue": "nado"}
    assert vs.set_active_venue(_UID, "nado") == "switched"
    assert vs.last_known_venue(_UID) == "nado"


# ---------------------------------------------------------------------------
# count_users_on_venue
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["ARCUS", "arcus_mainnet", "", None])
def test_count_users_on_venue_rejects_unknown_venue(monkeypatch, bad):
    monkeypatch.setattr(vs, "query_count", lambda *a: pytest.fail("no DB for a bad venue"))
    with pytest.raises(ValueError, match="unknown venue"):
        vs.count_users_on_venue(bad)


def test_count_users_on_venue_queries_exact_venue(monkeypatch):
    seen = []
    monkeypatch.setattr(vs, "query_count", lambda sql, params: seen.append((sql, params)) or 3)
    assert vs.count_users_on_venue("arcus") == 3
    assert seen == [("SELECT COUNT(*) FROM users WHERE active_venue = %s", ("arcus",))]


def test_count_users_on_venue_db_error_propagates(monkeypatch):
    def _boom(*_a):
        raise RuntimeError("column active_venue does not exist")

    monkeypatch.setattr(vs, "query_count", _boom)
    with pytest.raises(RuntimeError):
        vs.count_users_on_venue("arcus")


# ---------------------------------------------------------------------------
# Side-effect pin: the switch can only ever flip one column
# ---------------------------------------------------------------------------

_SRC = pathlib.Path(vs.__file__)
_ALLOWED_MODULE_IMPORTS = {
    "__future__",
    "logging",
    "typing",
    "src.nadobro.core.feature_flags",
    "src.nadobro.db",
    "src.nadobro.users.audit_log",
    "src.nadobro.users.user_service",
    "src.nadobro.utils.venue_scope",
}
_FORBIDDEN_PACKAGES = ("strategy", "trading", "venue", "engine", "runtime", "notify", "portfolio", "llm")


def _imported_modules(nodes):
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_venue_service_module_imports_are_pinned():
    tree = ast.parse(_SRC.read_text())
    top = set(_imported_modules(tree.body))
    assert top <= _ALLOWED_MODULE_IMPORTS, sorted(top - _ALLOWED_MODULE_IMPORTS)


def test_venue_service_never_reaches_a_stop_or_start_path():
    tree = ast.parse(_SRC.read_text())
    every = set(_imported_modules(ast.walk(tree)))
    for mod in every:
        parts = mod.split(".")
        pkg = parts[2] if len(parts) >= 3 and mod.startswith("src.nadobro.") else None
        assert pkg not in _FORBIDDEN_PACKAGES, f"venue_service imports {mod}"
    # And it writes exactly one thing: users.active_venue (plus the audit row).
    sql = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.lstrip().upper().startswith(
            ("UPDATE", "INSERT", "DELETE")
        )
    ]
    assert sql == [
        "UPDATE users SET active_venue = %s WHERE telegram_id = %s AND active_venue = %s "
        "RETURNING active_venue"
    ], sql


def test_venue_service_has_no_coroutines():
    # Every function is sync and is called through run_blocking_db from async code.
    tree = ast.parse(_SRC.read_text())
    assert not any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree))


def test_venue_mappers_are_silent(caplog):
    # They run inside UserRow.__init__ on every update: never log, never raise.
    import logging

    with caplog.at_level(logging.DEBUG):
        for raw in (None, "", "ARCUS", "arcus_mainnet", "garbage", 3, object(), "arcus", "mainnet"):
            active_venue_from_db(raw)
            arcus_network_from_db(raw)
            UserRow(_row(active_venue=raw, arcus_network_mode=raw))
    assert caplog.records == []
