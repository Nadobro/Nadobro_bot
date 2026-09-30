"""diagnose_key: a 401 is never proof that an Arcus key is dead (03 §7.10, §19.5).

Docs: place-order "401 … Missing or invalid API key" AND a timestamp outside
±30 s is also a 401; get-api-keys "a 200 is safe to treat as the complete set —
an unreadable subaccount surfaces as a 500". Only a 200 listing may yield
``key_dead``; every status write is pubkey-guarded and off the loop.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import arcus_link_helpers as H
from arcus_link_helpers import ADDR, DAY_MS, NOW_MS, PUB_B, RFC_PUB, UID, FakeClock, FakeDB, entry, ok
from src.nadobro.users import arcus_link_service as ls
from src.nadobro.users.arcus_link_service import KeyDiagnosis, KeyVerdict
from src.nadobro.venue.arcus.types import Lane


@pytest.fixture(autouse=True)
def _reset():
    ls._reset_for_tests()
    yield
    ls._reset_for_tests()


def run(coro):
    return asyncio.run(coro)


def _setup(monkeypatch, *, api_keys, skew=5.0, credential=None, **db_kw):
    credential = credential if credential is not None else H.row(name="nadobro-aaaa", until=NOW_MS + 90 * DAY_MS)
    env = H.install(monkeypatch, clock=FakeClock(skew), db=FakeDB(credential=credential, **db_kw))
    env.client.api_keys = api_keys
    return env


def test_key_ok_touches_the_row(monkeypatch):
    env = _setup(monkeypatch, api_keys=[ok([entry(until=NOW_MS + 100 * DAY_MS)])])
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.KEY_OK and d.key_dead is False and d.listing_ok
    assert env.db.touches == [
        (UID, "testnet", {"api_public_key": RFC_PUB, "valid_until_ms": NOW_MS + 100 * DAY_MS, "status": "active"})
    ]
    assert not env.db.marks
    assert env.clock.calls and env.clock.calls[0]["lane"] is Lane.L2_INTERACTIVE


def test_clock_skew_is_diagnostic_only(monkeypatch, caplog):
    env = _setup(monkeypatch, api_keys=[ok([entry()])], skew=45_000.0)
    with caplog.at_level(logging.ERROR):
        d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.CLOCK_SKEW and d.key_dead is False
    assert any("ARCUS_CLOCK_SKEW" in r.getMessage() for r in caplog.records)
    assert env.clock.calls[0]["lane"] is Lane.L2_INTERACTIVE
    assert env.db.touches and not env.db.marks


def test_clock_sync_denied_key_fine(monkeypatch):
    _setup(monkeypatch, api_keys=[ok([entry()])], skew=None)
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.KEY_OK and d.skew_ms is None


@pytest.mark.parametrize("denied", [H.THROTTLED, H.UNAVAILABLE, H.SCHEMA, H.DENIED])
def test_unreadable_listing_is_unknown_and_changes_nothing(monkeypatch, denied):
    env = _setup(monkeypatch, api_keys=[denied])
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.UNKNOWN and d.key_dead is False
    assert not env.db.marks and not env.db.touches and not env.db.audits


def test_absent_from_a_200_listing_is_revoked(monkeypatch):
    env = _setup(monkeypatch, api_keys=[ok([entry(PUB_B, name="other")])])
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.REVOKED and d.key_dead is True
    assert env.db.marks == [(UID, "testnet", "invalid", {"wipe_secret": False, "api_public_key": RFC_PUB})]
    assert [a[1] for a in env.db.audits] == ["arcus_key_invalidated"]
    assert env.db.audits[0][2] == "testnet revoked"


def test_absent_after_the_stored_expiry_is_expired_not_revoked(monkeypatch):
    env = _setup(
        monkeypatch,
        api_keys=[ok([])],
        credential=H.row(name="nadobro-aaaa", until=NOW_MS - 1000),
    )
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.EXPIRED and d.key_dead is True
    assert env.db.marks == [(UID, "testnet", "expired", {"wipe_secret": False, "api_public_key": RFC_PUB})]
    assert not env.db.audits  # expiry is not an invalidation


def test_name_reuse_is_named(monkeypatch):
    _setup(monkeypatch, api_keys=[ok([entry(PUB_B, name="NADOBRO-AAAA")])])
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.REVOKED_NAME_REUSE and d.key_dead is True
    # an INACTIVE entry with our name is not a reuse
    _setup(monkeypatch, api_keys=[ok([entry(PUB_B, name="nadobro-aaaa", status="DELETED")])])
    assert run(ls.diagnose_key(UID, "testnet")).verdict is KeyVerdict.REVOKED


def test_listed_but_past_its_validity_is_expired(monkeypatch):
    env = _setup(monkeypatch, api_keys=[ok([entry(until=NOW_MS - 1)])])
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.EXPIRED and d.valid_until_ms == NOW_MS - 1
    assert env.db.marks[0][2] == "expired" and env.db.marks[0][3]["wipe_secret"] is False


def test_listed_inactive_or_wrong_scope(monkeypatch):
    _setup(monkeypatch, api_keys=[ok([entry(status="DELETED")])])
    assert run(ls.diagnose_key(UID, "testnet")).verdict is KeyVerdict.REVOKED
    env = _setup(monkeypatch, api_keys=[ok([entry(index=4)])])
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.WRONG_SCOPE and d.key_dead is True
    assert env.db.marks[0][2] == "invalid"


def test_no_credential(monkeypatch):
    env = H.install(monkeypatch, db=FakeDB(credential=None))
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.NO_CREDENTIAL and env.client.calls == []
    env.db.credential = H.row(status="unlinked")
    assert run(ls.diagnose_key(UID, "testnet")).verdict is KeyVerdict.NO_CREDENTIAL


def test_db_error_is_unknown_and_not_cached(monkeypatch):
    env = H.install(monkeypatch, db=FakeDB(credential=RuntimeError("db down")))
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.UNKNOWN and not d.listing_ok and not d.key_dead
    assert not ls._DIAG_CACHE


def test_second_call_within_a_minute_is_cached(monkeypatch):
    env = _setup(monkeypatch, api_keys=[ok([entry()])])
    first = run(ls.diagnose_key(UID, "testnet"))
    reads = len(env.client.calls)
    second = run(ls.diagnose_key(UID, "testnet"))
    assert second.from_cache and second.verdict is first.verdict and len(env.client.calls) == reads
    env.mono.now += 61
    third = run(ls.diagnose_key(UID, "testnet"))
    assert not third.from_cache and len(env.client.calls) > reads


def test_renewal_between_read_and_write_is_harmless(monkeypatch):
    env = _setup(monkeypatch, api_keys=[ok([])], mark_result=False)
    d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.REVOKED  # verdict unchanged
    assert env.db.marks and not env.db.audits  # nothing changed -> no invalidation event


def test_status_write_failure_does_not_change_the_verdict(monkeypatch, caplog):
    env = _setup(monkeypatch, api_keys=[ok([])])

    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ls._creds, "mark_status", broken)
    with caplog.at_level(logging.WARNING):
        d = run(ls.diagnose_key(UID, "testnet"))
    assert d.verdict is KeyVerdict.REVOKED and "RuntimeError" in caplog.text


def test_listeners_on_status_changes(monkeypatch):
    events = []
    env = _setup(monkeypatch, api_keys=[ok([])])
    ls.register_credential_listener(lambda uid, net, ev: events.append(ev))
    run(ls.diagnose_key(UID, "testnet"))
    assert events == ["invalid"]
    ls._DIAG_CACHE.clear()
    env.db.credential = H.row(status="invalid", until=NOW_MS + DAY_MS)
    run(ls.diagnose_key(UID, "testnet"))
    assert events == ["invalid"]  # already invalid: no second event


def test_diagnosis_text_mapping():
    base = dict(network="testnet", skew_ms=None, valid_until_ms=1_790_000_000_000, listing_ok=True, key_name="nadobro-aaaa")
    ok_d = KeyDiagnosis(KeyVerdict.KEY_OK, **base)
    assert ls.diagnosis_text(ok_d, after_401=True) == (ls.TEXT_D_UNKNOWN_401, {})
    assert ls.diagnosis_text(ok_d, after_401=False) == (ls.TEXT_D_OK, {"until": "2026-09-21 14:13 UTC"})
    no_exp = KeyDiagnosis(KeyVerdict.KEY_OK, **{**base, "valid_until_ms": 0})
    assert ls.diagnosis_text(no_exp, after_401=False) == (ls.TEXT_D_OK_NO_EXPIRY, {})
    skew = KeyDiagnosis(KeyVerdict.CLOCK_SKEW, **{**base, "skew_ms": 45_400.0})
    assert ls.diagnosis_text(skew, after_401=True) == (ls.TEXT_D_SKEW, {"seconds": "45"})
    assert ls.diagnosis_text(KeyDiagnosis(KeyVerdict.EXPIRED, **base), after_401=True)[0] == ls.TEXT_D_EXPIRED
    assert ls.diagnosis_text(KeyDiagnosis(KeyVerdict.REVOKED, **base), after_401=True) == (ls.TEXT_D_REVOKED, {})
    assert ls.diagnosis_text(KeyDiagnosis(KeyVerdict.REVOKED_NAME_REUSE, **base), after_401=False) == (
        ls.TEXT_D_NAME_REUSE, {"key_name": "nadobro-aaaa"})
    assert ls.diagnosis_text(KeyDiagnosis(KeyVerdict.WRONG_SCOPE, **base), after_401=False) == (ls.TEXT_D_WRONG_SCOPE, {})
    assert ls.diagnosis_text(KeyDiagnosis(KeyVerdict.NO_CREDENTIAL, **base), after_401=False) == (
        ls.TEXT_D_NO_CREDENTIAL, {"network": "TESTNET"})
    assert ls.diagnosis_text(KeyDiagnosis(KeyVerdict.UNKNOWN, **{**base, "listing_ok": False}), after_401=True) == (
        ls.TEXT_BUSY, {})
    # every returned key formats with its values
    for verdict in KeyVerdict:
        key, fmt = ls.diagnosis_text(KeyDiagnosis(verdict, **{**base, "skew_ms": 12_000.0}), after_401=False)
        key.format(**fmt)


def test_key_dead_needs_a_200_listing():
    for verdict in (KeyVerdict.EXPIRED, KeyVerdict.REVOKED, KeyVerdict.REVOKED_NAME_REUSE, KeyVerdict.WRONG_SCOPE):
        assert KeyDiagnosis(verdict, "testnet", None, None, True, None).key_dead
        assert not KeyDiagnosis(verdict, "testnet", None, None, False, None).key_dead
    for verdict in (KeyVerdict.KEY_OK, KeyVerdict.CLOCK_SKEW, KeyVerdict.UNKNOWN, KeyVerdict.NO_CREDENTIAL):
        assert not KeyDiagnosis(verdict, "testnet", None, None, True, None).key_dead
