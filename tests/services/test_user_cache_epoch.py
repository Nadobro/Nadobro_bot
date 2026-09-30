"""users/user_service: a read that raced a write never re-caches the old row.

Arcus P1 review BC1-STALE-CACHE / BC2-1. ``get_user`` SELECTs and THEN caches.
A SELECT that started before a write committed could store its pre-write row
AFTER the write's ``invalidate_user_cache`` and mask the write for the cache TTL
(10 s) — a venue switch read back as the old venue by the gate. Every
invalidate bumps an epoch; a read that saw it move skips the cache write.

Real threads, the real user cache, and the real ``venue_service.set_active_venue``
over an in-memory row (no DB).
"""
from __future__ import annotations

import threading

import pytest

from src.nadobro.users import user_service, venue_service

UID = 990_023_401
OTHER = 990_023_402


@pytest.fixture(autouse=True)
def _clean_cache():
    user_service.invalidate_user_cache(UID)
    user_service.invalidate_user_cache(OTHER)
    yield
    user_service.invalidate_user_cache(UID)
    user_service.invalidate_user_cache(OTHER)


class _Db:
    """One users row; ``query_one`` snapshots it at statement start (READ
    COMMITTED) and can be held open to interleave a concurrent write."""

    def __init__(self, monkeypatch, venue="nado"):
        self.row = {"telegram_id": UID, "network_mode": "mainnet", "language": "en", "active_venue": venue}
        self.hold = False
        self.started = threading.Event()
        self.release = threading.Event()
        monkeypatch.setattr(user_service, "query_one", self.query_one)
        monkeypatch.setattr(user_service, "execute", lambda *a, **k: None)

    def query_one(self, sql, params):
        snap = dict(self.row, telegram_id=params[0])
        if self.hold:
            self.started.set()
            assert self.release.wait(5)
        return snap


def _read_in_thread(fn, *args):
    t = threading.Thread(target=fn, args=args)
    t.start()
    return t


def _race(db, write, fn=user_service.get_user, uid=UID):
    """``fn(uid)`` SELECTs the pre-write row, ``write()`` commits + invalidates,
    then the read finishes and tries to cache what it saw."""
    db.hold = True
    t = _read_in_thread(fn, uid)
    assert db.started.wait(5)
    write()
    db.hold = False
    db.release.set()
    t.join(5)
    assert not t.is_alive()


def test_a_venue_switch_is_never_masked_by_a_read_that_started_before_it(monkeypatch):
    # The reviewer's probe: a get_user (language middleware / a strategy tick)
    # is mid-SELECT while the /venue compare-and-set commits.
    db = _Db(monkeypatch, venue="nado")
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))
    monkeypatch.setattr(venue_service, "record_audit_event", lambda *a, **k: None)

    def cas(sql, params):
        db.row["active_venue"] = params[0]
        return {"active_venue": params[0]}

    monkeypatch.setattr(venue_service, "execute_returning", cas)
    _race(db, lambda: venue_service.set_active_venue(UID, "arcus"))
    assert db.row["active_venue"] == "arcus"
    assert venue_service.peek_active_venue(UID) is None  # not the stale 'nado'
    assert venue_service.get_active_venue(UID) == "arcus"
    assert venue_service.peek_active_venue(UID) == "arcus"  # the fresh row is cached


def test_get_or_create_user_never_caches_a_row_read_before_an_invalidate(monkeypatch):
    db = _Db(monkeypatch)

    def write():
        db.row["language"] = "ko"
        user_service.invalidate_user_cache(UID)

    _race(db, write, fn=user_service.get_or_create_user)
    assert user_service._get_cached_user(UID) is None
    assert user_service.get_user(UID).language == "ko"


def test_a_full_clear_also_blocks_the_racing_write(monkeypatch):
    db = _Db(monkeypatch)
    _race(db, lambda: user_service.invalidate_user_cache())
    assert user_service._get_cached_user(UID) is None


def test_an_uncontended_read_is_still_cached(monkeypatch):
    db = _Db(monkeypatch)
    first = user_service.get_user(UID)
    db.row["language"] = "ko"  # not invalidated: the cache keeps serving the row
    assert user_service.get_user(UID) is first
    assert user_service._get_cached_user(UID) is first


def test_an_invalidate_before_the_read_starts_does_not_block_caching(monkeypatch):
    _Db(monkeypatch)
    user_service.invalidate_user_cache(OTHER)
    user = user_service.get_user(UID)
    assert user_service._get_cached_user(UID) is user
