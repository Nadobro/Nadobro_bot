"""users/arcus_link_service.py — precheck, intake, verification, store, unlink,
automation probe, listeners (Arcus P3b, 03 §7, §19.4).

Every venue read is a scripted P2 outcome (no network); every DB call is a fake
that asserts it runs off the event-loop thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import threading
import types
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

import arcus_link_helpers as H
from arcus_link_helpers import (
    ADDR,
    ADDR2,
    DAY_MS,
    NOW_MS,
    PUB_B,
    RFC_PUB,
    RFC_SEED,
    SEED_B,
    UID,
    WALLET_ADDR,
    WALLET_ED25519_PUB,
    WALLET_SEED,
    FakeClient,
    FakeDB,
    compliance,
    entry,
    ok,
)
from src.nadobro.core import crypto
from src.nadobro.users import arcus_credentials as creds
from src.nadobro.users import arcus_link_service as ls
from src.nadobro.users.arcus_link_service import AddressCheck, LinkPending, LinkResult

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEYS", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))
    monkeypatch.delenv("ARCUS_MAINNET_ENABLED", raising=False)
    monkeypatch.delitem(sys.modules, "src.nadobro.strategy.arcus_runtime", raising=False)
    ls._reset_for_tests()
    yield
    ls._reset_for_tests()
    crypto._fernet_instance = None


def run(coro):
    return asyncio.run(coro)


def _pending(env, *, step="key", address=ADDR, attested=True, check=AddressCheck.ELIGIBLE,
             age_s=0.0, network="testnet", generation=None):
    gen = generation if generation is not None else ls.begin_generation(UID, network)
    return LinkPending(
        network=network,
        step=step,
        expires_mono=env.mono.now + 1800,
        generation=gen,
        attested_at=datetime(2026, 9, 21, tzinfo=timezone.utc) if attested else None,
        address=address,
        key_name="nadobro-ab12",
        address_check=check,
        address_checked_mono=env.mono.now - age_s,
        has_activity=True,
    )


def _no_automation(monkeypatch, running=False, raises=False):
    mod = types.ModuleType("src.nadobro.strategy.arcus_runtime")

    def has_arcus_automation(uid):
        assert threading.current_thread() is not threading.main_thread()
        if raises:
            raise RuntimeError("db down")
        return running

    mod.has_arcus_automation = has_arcus_automation
    monkeypatch.setitem(sys.modules, "src.nadobro.strategy.arcus_runtime", mod)


# ============================================================================================
# precheck
# ============================================================================================


def test_precheck_blocked_stops_before_account_and_keys(monkeypatch):
    env = H.install(monkeypatch)
    env.client.compliance = [ok(compliance("BLOCKED"))]
    pre = run(ls.precheck_address("testnet", ADDR, user_id=UID))
    assert pre.check is AddressCheck.BLOCKED
    assert env.client.count("account") == 0 and env.client.count("apiKeys") == 0


def test_precheck_geo_restricted_egress_alarms_once(monkeypatch, caplog):
    env = H.install(monkeypatch)
    env.client.compliance = [ok(compliance(perps=True, bypassed=False))]
    with caplog.at_level(logging.INFO, logger=ls.__name__):
        pre = run(ls.precheck_address("testnet", ADDR, user_id=UID))
    assert pre.check is AddressCheck.GEO_RESTRICTED
    assert sum("ARCUS_GEO_RESTRICTED" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR) == 1
    assert ls.egress_posture("testnet").blocked
    # bypassed -> continues to eligible
    env.client.compliance = [ok(compliance(perps=True, bypassed=True))]
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.ELIGIBLE


def test_precheck_whitelist_403_is_not_eligible_and_stores_nothing(monkeypatch):
    env = H.install(monkeypatch)
    env.client.account = [H.WHITELIST]
    pre = run(ls.precheck_address("mainnet", ADDR, user_id=UID))
    assert pre.check is AddressCheck.NOT_WHITELISTED
    assert not env.db.upserts and not env.db.marks and not env.db.saves
    assert env.client.count("apiKeys") == 0
    assert pre.existing_names == frozenset()


def test_precheck_no_activity_and_unknown_404(monkeypatch):
    env = H.install(monkeypatch)
    env.client.account = [H.NO_ACTIVITY]
    pre = run(ls.precheck_address("testnet", ADDR, user_id=UID))
    assert pre.check is AddressCheck.ELIGIBLE_NO_ACTIVITY and pre.has_activity is False
    env.client.account = [H.NOT_FOUND]  # an unknown-path 404 is NOT "no activity"
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.BUSY


@pytest.mark.parametrize("which", ["compliance", "account", "api_keys"])
@pytest.mark.parametrize("denied", [H.THROTTLED, H.UNAVAILABLE, H.DENIED, H.SCHEMA])
def test_precheck_denied_reads_are_busy_never_not_eligible(monkeypatch, which, denied):
    env = H.install(monkeypatch)
    setattr(env.client, which, [denied])
    pre = run(ls.precheck_address("testnet", ADDR, user_id=UID))
    assert pre.check is AddressCheck.BUSY
    assert pre.has_activity is None or which == "api_keys"


def test_precheck_compliance_without_address_section_is_busy(monkeypatch):
    env = H.install(monkeypatch)
    env.client.compliance = [ok(compliance(None))]
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.BUSY
    assert env.client.count("account") == 0 and env.client.count("apiKeys") == 0
    env.client.compliance = [ok(compliance("COMPLIANT"))]
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.ELIGIBLE


def test_precheck_owned_elsewhere_makes_no_venue_call(monkeypatch):
    env = H.install(monkeypatch, db=FakeDB(owner=UID + 1))
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.ALREADY_LINKED_ELSEWHERE
    assert env.client.calls == []
    env.db.owner = UID  # our own active row is fine
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.ELIGIBLE


def test_precheck_db_error_is_db_unavailable(monkeypatch):
    env = H.install(monkeypatch, db=FakeDB(credential=RuntimeError("db down")))
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.DB_UNAVAILABLE
    assert env.client.calls == []


def test_precheck_address_change_while_automation_runs(monkeypatch):
    _no_automation(monkeypatch, running=True)
    env = H.install(monkeypatch, db=FakeDB(credential=H.row(address=ADDR2)))
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.AUTOMATION_RUNNING
    assert env.client.calls == []
    # the SAME address (renewal) is fine while running
    env.db.credential = H.row(address=ADDR)
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.ELIGIBLE


def test_precheck_existing_names_include_the_stored_name(monkeypatch):
    env = H.install(monkeypatch, db=FakeDB(credential=H.row(name="Nadobro-OLD1", status="invalid")))
    env.client.api_keys = [ok([entry(PUB_B, name="Mine"), entry(RFC_PUB, name=None)])]
    pre = run(ls.precheck_address("testnet", ADDR.upper().replace("0X", "0x"), user_id=UID))
    assert pre.check is AddressCheck.ELIGIBLE
    assert pre.existing_names == frozenset({"mine", "nadobro-old1"})
    assert env.client.calls[0] == ("compliance", ADDR)  # lowercased


def test_precheck_invalid_address_does_no_io(monkeypatch):
    env = H.install(monkeypatch)
    for bad in ("0x123", "ab" * 20, "", "0x" + "zz" * 20):
        assert run(ls.precheck_address("testnet", bad, user_id=UID)).check is AddressCheck.INVALID
    assert env.client.calls == [] and env.db.all_args == []


def test_precheck_without_user_skips_the_db(monkeypatch):
    env = H.install(monkeypatch)
    assert run(ls.precheck_address("testnet", ADDR, user_id=None)).check is AddressCheck.ELIGIBLE
    assert env.db.all_args == []


def test_precheck_concurrency_cap_is_busy_at_once(monkeypatch):
    env = H.install(monkeypatch)
    monkeypatch.setattr(ls, "_IN_FLIGHT", ls._MAX_CONCURRENT)
    assert run(ls.precheck_address("testnet", ADDR, user_id=UID)).check is AddressCheck.BUSY
    assert env.client.calls == []


def test_precheck_through_the_real_p2_client(monkeypatch):
    """P2 integration: the documented bodies map to the right verdicts."""
    from types import SimpleNamespace

    from arcus_helpers import mock_client, resp

    ls._reset_for_tests()
    comp = {"geo": {"country": "XX", "region": "XX", "restrictions": {"perpetuals": False, "spot": False},
                    "bypassed": False}, "address": {"address": ADDR, "status": "COMPLIANT"}}
    cases = [
        (resp(403, {"error": "address not on access whitelist"}), AddressCheck.NOT_WHITELISTED),
        (resp(404, {"error": "this account has no activity yet"}), AddressCheck.ELIGIBLE_NO_ACTIVITY),
        (resp(404, {"error": "not found"}), AddressCheck.BUSY),
        (resp(429, {"error": "rate limited"}), AddressCheck.BUSY),
        (resp(500, {"error": "boom"}), AddressCheck.BUSY),
    ]
    for account_resp, expected in cases:
        client, calls = mock_client({
            ("GET", "/v1/compliance"): resp(200, comp),
            ("GET", "/v1/account"): account_resp,
            ("GET", "/v1/apiKeys"): resp(200, {"apiKeys": []}),
        })
        monkeypatch.setattr(ls, "_services", lambda net, c=client: SimpleNamespace(client=c, clock=c.clock))
        pre = run(ls.precheck_address("testnet", ADDR, user_id=None))
        assert pre.check is expected, (account_resp.status_code, pre.check)
        api_calls = [r for r in calls if r.url.path == "/v1/apiKeys"]
        for r in api_calls:
            assert "accountIndex" not in r.url.params  # all subaccounts
        assert all("x-api-key" not in {k.lower() for k in r.headers} for r in calls)  # public reads only


# ============================================================================================
# key names
# ============================================================================================


def test_new_key_name_shape_and_collisions(monkeypatch):
    for _ in range(20):
        assert re.fullmatch(r"nadobro-[0-9a-f]{4}", ls.new_key_name([]))
    values = iter(["ab12", "AB12", "cd34"])
    monkeypatch.setattr(ls.secrets, "token_hex", lambda n: next(values))
    assert ls.new_key_name(["NADOBRO-AB12"]) == "nadobro-cd34"  # case-insensitive skip


def test_new_key_name_widens_after_50_collisions(monkeypatch):
    calls = []

    def fake(n):
        calls.append(n)
        return "aaaa" if n == 2 else "bbbbbb"

    monkeypatch.setattr(ls.secrets, "token_hex", fake)
    assert ls.new_key_name(["nadobro-aaaa"]) == "nadobro-bbbbbb"
    assert calls.count(2) == 50 and calls[-1] == 3


# ============================================================================================
# intake
# ============================================================================================


def _no_venue(monkeypatch):
    def boom(net):
        raise AssertionError("no venue call expected")

    monkeypatch.setattr(ls, "_services", boom)


def test_intake_refuses_a_wallet_private_key_before_any_venue_call(monkeypatch):
    env = H.install(monkeypatch)
    _no_venue(monkeypatch)
    p = _pending(env, address=WALLET_ADDR)
    before = dict(ls._STASH)
    res = run(ls.intake_key(user_id=UID, pending=p, pasted_text=WALLET_SEED))
    assert res.status == "wallet_key"
    assert env.db.audits == [(UID, "arcus_wallet_key_pasted", "testnet")]
    assert ls._STASH == before
    assert not H.contains_secret(env.db.all_args, (WALLET_SEED, WALLET_ED25519_PUB))


def test_intake_invalid_scalar_is_not_a_wallet_key(monkeypatch):
    env = H.install(monkeypatch)
    p = _pending(env, address=WALLET_ADDR)
    assert run(ls.intake_key(user_id=UID, pending=p, pasted_text="ff" * 32)).status == "stashed"


def test_intake_stashes_only_ciphertext(monkeypatch):
    env = H.install(monkeypatch)
    p = _pending(env)
    assert run(ls.intake_key(user_id=UID, pending=p, pasted_text="  0x" + RFC_SEED.upper() + "\n")).status == "stashed"
    stash = ls._STASH[(UID, "testnet")]
    assert isinstance(stash.sealed, creds.SealedSigningKey) and stash.sealed.api_public_key == RFC_PUB
    assert crypto.decrypt_with_server_key(stash.sealed.token) == bytes.fromhex(RFC_SEED)
    assert stash.generation == p.generation
    assert not _HEX64.search(repr(stash))
    assert ls.has_stash(UID, "testnet", p.generation) and not ls.has_stash(UID, "testnet", p.generation + 1)


@pytest.mark.parametrize(
    "text, status",
    [
        ("-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEI\n-----END PRIVATE KEY-----", "pem"),
        ("a" * 63, "invalid"),
        ("ab" * 64, "invalid"),
        (f"key {RFC_SEED} here", "invalid"),
        ("hello", "invalid"),
    ],
)
def test_intake_rejects_non_keys(monkeypatch, text, status):
    env = H.install(monkeypatch)
    assert run(ls.intake_key(user_id=UID, pending=_pending(env), pasted_text=text)).status == status
    assert not ls._STASH


def test_intake_without_address(monkeypatch):
    env = H.install(monkeypatch)
    p = replace(_pending(env), address=None)
    assert run(ls.intake_key(user_id=UID, pending=p, pasted_text=RFC_SEED)).status == "no_address"


def test_intake_cancelled_mid_seal_writes_no_stash(monkeypatch):
    env = H.install(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    real = ls._intake_sync

    def gated(seed, address):
        entered.set()
        assert release.wait(10)
        return real(seed, address)

    monkeypatch.setattr(ls, "_intake_sync", gated)

    async def body():
        task = asyncio.get_running_loop().create_task(
            ls.intake_key(user_id=UID, pending=_pending(env), pasted_text=RFC_SEED)
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.2)

    asyncio.run(body())
    assert not ls._STASH


def test_intake_superseded_by_a_newer_generation(monkeypatch):
    env = H.install(monkeypatch)
    p = _pending(env)
    real = ls._intake_sync

    def bump(seed, address):
        out = real(seed, address)
        ls._GEN[(UID, "testnet")] += 1  # the user moved on while sealing
        return out

    monkeypatch.setattr(ls, "_intake_sync", bump)
    assert run(ls.intake_key(user_id=UID, pending=p, pasted_text=RFC_SEED)).status == "superseded"
    assert not ls._STASH


def test_intake_error_logs_type_only(monkeypatch, caplog):
    env = H.install(monkeypatch)

    def broken(seed, address):
        raise RuntimeError(f"boom {seed}")

    monkeypatch.setattr(ls, "_intake_sync", broken)
    with caplog.at_level(logging.WARNING):
        assert run(ls.intake_key(user_id=UID, pending=_pending(env), pasted_text=RFC_SEED)).status == "error"
    assert "RuntimeError" in caplog.text and RFC_SEED not in caplog.text


# ============================================================================================
# verification + store
# ============================================================================================


def _verify(env, pending=None, secret=RFC_SEED):
    pending = pending or _pending(env)
    return run(ls.verify_and_store(user_id=UID, pending=pending, pasted_secret=secret))


def test_linked_with_no_expiry_single_subaccount(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry(until=0)])]
    out = _verify(env)
    assert out.result is LinkResult.LINKED and out.has_activity is True
    assert len(env.db.upserts) == 1
    up = env.db.upserts[0]
    assert up["valid_until_ms"] == 0 and up["all_subaccounts"] is False
    assert up["address"] == ADDR and up["network"] == "testnet" and up["api_wallet_name"] == "nadobro-ab12"
    assert up["sealed"].api_public_key == RFC_PUB
    assert out.row.api_public_key == RFC_PUB and not out.renewed
    assert not ls._STASH  # dropped after the store


def test_all_subaccounts_key_is_accepted_and_pinned_to_zero(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry(all_sub=True)])]
    out = _verify(env)
    assert out.result is LinkResult.LINKED
    assert env.db.upserts[0]["all_subaccounts"] is True


def test_wrong_subaccount_is_refused(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry(index=3)])]
    out = _verify(env)
    assert out.result is LinkResult.KEY_WRONG_SUBACCOUNT and out.wrong_account_index == 3
    assert not env.db.upserts and not ls._STASH
    # defensive: a hand-built all_subaccounts=False without an index (unreachable through P2)
    weird = replace(entry(), all_subaccounts=False, account_index=None)
    assert ls.key_problem(weird, now_ms=NOW_MS) == (LinkResult.KEY_WRONG_SUBACCOUNT, None)


@pytest.mark.parametrize("hours, expected", [(23, LinkResult.KEY_EXPIRES_TOO_SOON), (25, LinkResult.LINKED)])
def test_key_validity_floor_is_24h(monkeypatch, hours, expected):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry(until=NOW_MS + hours * 3_600_000)])]
    assert _verify(env).result is expected


def test_withdraw_and_inactive_keys_are_refused(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry(permissions=("withdraw",))])]
    assert _verify(env).result is LinkResult.KEY_HAS_WITHDRAW
    env.client.api_keys = [ok([entry(status="DELETED")])]
    assert _verify(env).result is LinkResult.KEY_INACTIVE
    assert not env.db.upserts


def test_key_not_found_only_after_seven_200_reads(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry(PUB_B)])]
    out = _verify(env)
    assert out.result is LinkResult.KEY_NOT_FOUND
    assert env.client.count("apiKeys") == 7
    deltas = [b - a for a, b in zip(ls._POLL_SCHEDULE_S, ls._POLL_SCHEDULE_S[1:])]
    assert env.sleep.calls == deltas
    assert ls._STASH  # kept: the user may not have tapped Authorize yet


def test_last_read_denied_is_busy_never_not_found(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([])] * 6 + [H.THROTTLED]
    assert _verify(env).result is LinkResult.BUSY
    assert ls._STASH


def test_key_appears_on_the_third_poll(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([]), ok([]), ok([entry()])]
    assert _verify(env).result is LinkResult.LINKED
    assert env.client.count("apiKeys") == 3


def test_throttle_extends_the_next_wait(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [H.Throttled(layer="read_ip", retry_after_ms=9000, client_ids=()), ok([entry()])]
    assert _verify(env).result is LinkResult.LINKED
    assert env.sleep.calls[0] >= 9.0


def test_store_time_whitelist_refusal_stores_nothing(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry()])]
    env.client.account = [H.WHITELIST]
    assert _verify(env).result is LinkResult.NOT_WHITELISTED
    assert not env.db.upserts and not ls._STASH


def test_store_time_blocked_and_geo(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry()])]
    env.client.compliance = [ok(compliance("BLOCKED"))]
    assert _verify(env).result is LinkResult.BLOCKED
    env.client.compliance = [ok(compliance(perps=True))]
    assert _verify(env).result is LinkResult.GEO_RESTRICTED
    assert not env.db.upserts


def test_no_activity_at_store_time(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry()])]
    env.client.account = [H.NO_ACTIVITY]
    out = _verify(env)
    assert out.result is LinkResult.LINKED_NO_ACTIVITY and out.has_activity is False


def test_busy_store_reads_lean_on_a_fresh_precheck_only(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry()])]
    env.client.compliance = [H.THROTTLED]
    env.client.account = [H.UNAVAILABLE]
    out = _verify(env, _pending(env, age_s=100))
    assert out.result is LinkResult.LINKED and out.has_activity is None
    env.db.upserts.clear()
    out = _verify(env, _pending(env, age_s=700))
    assert out.result is LinkResult.BUSY and not env.db.upserts
    assert ls._STASH  # kept


def test_check_again_reuses_the_stash_and_generation_guard(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([])]
    p = _pending(env)
    assert _verify(env, p).result is LinkResult.KEY_NOT_FOUND
    env.client.api_keys = [ok([entry()])]
    env.db.all_args.clear()
    assert _verify(env, p, secret=None).result is LinkResult.LINKED  # no re-paste needed
    # a stash from another generation is expired for this pending
    assert run(ls.intake_key(user_id=UID, pending=p, pasted_text=RFC_SEED)).status == "stashed"
    newer = _pending(env)  # begin_generation bumps
    assert _verify(env, newer, secret=None).result is LinkResult.PENDING_EXPIRED


def test_generation_bump_during_poll_is_superseded(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([]), ok([entry()])]
    env.sleep.hook = lambda n: ls.begin_generation(UID, "testnet")
    assert _verify(env).result is LinkResult.SUPERSEDED
    assert not env.db.upserts


def test_newer_paste_during_poll_is_superseded(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([]), ok([entry()])]
    p = _pending(env)

    def swap(_n):
        other = ls._STASH[(UID, "testnet")]
        ls._STASH[(UID, "testnet")] = ls._StashedKey(other.sealed, other.generation, 999, other.expires_mono)

    env.sleep.hook = swap
    assert _verify(env, p).result is LinkResult.SUPERSEDED
    assert not env.db.upserts


def test_renewal_same_address_while_running_swaps_without_gap(monkeypatch):
    _no_automation(monkeypatch, running=True)
    env = H.install(monkeypatch, db=FakeDB(credential=H.row(pub=PUB_B, name="nadobro-aaaa")))
    env.client.api_keys = [ok([entry(name="nadobro-bbbb")])]
    out = _verify(env)
    assert out.result is LinkResult.LINKED and out.renewed is True
    assert out.previous.api_public_key == PUB_B and out.row.api_public_key == RFC_PUB
    assert len(env.db.upserts) == 1 and not env.db.marks  # one upsert, no unlink / delete


def test_different_address_while_running_is_refused(monkeypatch):
    _no_automation(monkeypatch, running=True)
    env = H.install(monkeypatch, db=FakeDB(credential=H.row(address=ADDR2)))
    env.client.api_keys = [ok([entry()])]
    assert _verify(env).result is LinkResult.AUTOMATION_RUNNING
    assert not env.db.upserts and not ls._STASH


def test_automation_probe_error_counts_as_running(monkeypatch):
    _no_automation(monkeypatch, raises=True)
    env = H.install(monkeypatch, db=FakeDB(credential=H.row(address=ADDR2, status="expired")))
    env.client.api_keys = [ok([entry()])]
    assert _verify(env).result is LinkResult.AUTOMATION_RUNNING


def test_flags_flipped_mid_poll_refuse_the_store(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([]), ok([entry()])]
    env.sleep.hook = lambda n: monkeypatch.delenv("ARCUS_ENABLED")
    assert _verify(env).result is LinkResult.NOT_ALLOWED
    assert not env.db.upserts


def test_mainnet_needs_its_flag(monkeypatch):
    env = H.install(monkeypatch)
    env.client.api_keys = [ok([entry()])]
    p = _pending(env, network="mainnet")
    assert _verify(env, p).result is LinkResult.NOT_ALLOWED
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    assert _verify(env, p).result is LinkResult.LINKED
    assert env.db.upserts[-1]["network"] == "mainnet"


def test_not_allowed_without_the_cohort(monkeypatch):
    env = H.install(monkeypatch)
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID + 1))
    assert _verify(env).result is LinkResult.NOT_ALLOWED
    assert env.client.calls == [] and not ls._STASH


@pytest.mark.parametrize(
    "changes",
    [{"step": "attest"}, {"step": "address_check"}, {"attested_at": None}, {"address_check": AddressCheck.BUSY},
     {"address_check": None}, {"address": None}, {"address": "0x123"}],
)
def test_nothing_is_stored_without_attestation_and_a_passed_precheck(monkeypatch, changes):
    env = H.install(monkeypatch)
    p = replace(_pending(env), **changes)
    assert _verify(env, p).result is LinkResult.NO_PENDING
    assert env.client.calls == [] and not ls._STASH


def test_address_taken_and_store_failure(monkeypatch, caplog):
    env = H.install(monkeypatch, db=FakeDB(upsert_error=creds.ArcusAddressTaken()))
    env.client.api_keys = [ok([entry()])]
    assert _verify(env).result is LinkResult.ALREADY_LINKED_ELSEWHERE
    assert not ls._STASH
    env.db.upsert_error = RuntimeError(f"insert failed near {RFC_SEED}")
    with caplog.at_level(logging.WARNING):
        out = _verify(env)
    assert out.result is LinkResult.STORE_FAILED
    assert ls._STASH  # kept for [Check again]
    assert "RuntimeError" in caplog.text and "insert failed" not in caplog.text and RFC_SEED not in caplog.text


def test_verify_results_for_bad_pastes(monkeypatch):
    env = H.install(monkeypatch)
    assert _verify(env, secret="-----BEGIN PRIVATE KEY-----\nx").result is LinkResult.INVALID_KEY
    assert _verify(env, secret="12345").result is LinkResult.INVALID_KEY
    wallet = _pending(env, address=WALLET_ADDR)
    assert _verify(env, wallet, secret=WALLET_SEED).result is LinkResult.WALLET_KEY_REFUSED
    assert env.client.calls == []  # refused before any venue call
    assert _verify(env, secret=None).result is LinkResult.PENDING_EXPIRED  # nothing stashed


def test_concurrency_cap_keeps_the_stash(monkeypatch):
    env = H.install(monkeypatch)
    p = _pending(env)
    assert run(ls.intake_key(user_id=UID, pending=p, pasted_text=RFC_SEED)).status == "stashed"
    monkeypatch.setattr(ls, "_IN_FLIGHT", ls._MAX_CONCURRENT)
    assert _verify(env, p, secret=None).result is LinkResult.BUSY
    assert ls._STASH


def test_listeners_audit_and_no_secret_anywhere(monkeypatch, caplog):
    events = []

    async def async_listener(uid, net, event):
        events.append(("async", uid, net, event))

    def sync_listener(uid, net, event):
        events.append(("sync", uid, net, event))

    def broken_listener(uid, net, event):
        raise RuntimeError(RFC_SEED)

    env = H.install(monkeypatch)
    ls.register_credential_listener(async_listener)
    ls.register_credential_listener(sync_listener)
    ls.register_credential_listener(sync_listener)  # idempotent
    ls.register_credential_listener(broken_listener)
    env.client.api_keys = [ok([entry()])]
    with caplog.at_level(logging.DEBUG):
        out = asyncio.run(ls.verify_and_store(user_id=UID, pending=_pending(env), pasted_secret=RFC_SEED))
        assert out.result is LinkResult.LINKED
        env.db.credential = out.row  # the next link is a renewal of the same address
        env.client.api_keys = [ok([entry()])]
        again = asyncio.run(ls.verify_and_store(user_id=UID, pending=_pending(env), pasted_secret=RFC_SEED))
        assert again.renewed is True
    assert events == [
        ("async", UID, "testnet", "linked"), ("sync", UID, "testnet", "linked"),
        ("async", UID, "testnet", "renewed"), ("sync", UID, "testnet", "renewed"),
    ]
    linked = [a for a in env.db.audits if a[1] == "arcus_linked"]
    assert len(linked) == 2 and "renewed=0" in linked[0][2] and "renewed=1" in linked[1][2]
    assert all(not _HEX64.search(a[2] or "") for a in env.db.audits)
    # The seed (any spelling) and the pubkey never reach a patched function, an audit or a log.
    assert not H.contains_secret(env.db.all_args, (RFC_SEED,))
    assert not H.contains_secret(env.db.audits, (RFC_SEED, RFC_PUB))
    assert RFC_SEED not in caplog.text and RFC_SEED.upper() not in caplog.text and RFC_PUB not in caplog.text


def test_shielded_store_completes_after_the_task_is_cancelled(monkeypatch):
    events = []
    gate = threading.Event()
    env = H.install(monkeypatch, db=FakeDB(upsert_gate=gate))
    ls.register_credential_listener(lambda uid, net, ev: events.append(ev))
    env.client.api_keys = [ok([entry()])]
    entered = threading.Event()
    real_upsert = env.db.upsert_active_credential

    def upsert(**kwargs):
        entered.set()
        return real_upsert(**kwargs)

    monkeypatch.setattr(ls._creds, "upsert_active_credential", upsert)

    async def body():
        task = asyncio.get_running_loop().create_task(
            ls.verify_and_store(user_id=UID, pending=_pending(env), pasted_secret=RFC_SEED)
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()  # a newer paste cancels the older verify task
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()
        for _ in range(200):
            if events:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    asyncio.run(body())
    assert events == ["linked"]
    assert [a[1] for a in env.db.audits] == ["arcus_linked"]
    assert not ls._STASH


# ============================================================================================
# unlink
# ============================================================================================


def test_unlink_refused_while_running(monkeypatch):
    _no_automation(monkeypatch, running=True)
    env = H.install(monkeypatch, db=FakeDB(credential=H.row()))
    assert run(ls.unlink(UID, "testnet")) == "refused_running"
    assert not env.db.marks and not env.db.audits


def test_unlink_none_and_unlinked(monkeypatch):
    env = H.install(monkeypatch)
    assert run(ls.unlink(UID, "testnet")) == "none"
    env.db.credential = H.row(status="unlinked")
    assert run(ls.unlink(UID, "testnet")) == "none"
    assert not env.db.marks


def test_unlink_wipes_and_announces_even_with_flags_off(monkeypatch):
    events = []
    monkeypatch.delenv("ARCUS_ENABLED")
    env = H.install(monkeypatch, db=FakeDB(credential=H.row()))
    ls.register_credential_listener(lambda uid, net, ev: events.append((uid, net, ev)))
    p = _pending(env)
    assert run(ls.intake_key(user_id=UID, pending=p, pasted_text=RFC_SEED)).status == "stashed"
    gen = ls.current_generation(UID, "testnet")
    assert run(ls.unlink(UID, "testnet")) == "unlinked"
    assert env.db.marks == [(UID, "testnet", "unlinked", {"wipe_secret": True})]
    assert not ls._STASH and ls.current_generation(UID, "testnet") == gen + 1
    assert env.db.audits == [(UID, "arcus_unlinked", "testnet 0xabab…abab")]
    assert events == [(UID, "testnet", "unlinked")]


def test_unlink_db_error_propagates(monkeypatch):
    H.install(monkeypatch, db=FakeDB(credential=RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        run(ls.unlink(UID, "testnet"))


# ============================================================================================
# automation probe
# ============================================================================================


def test_automation_probe_before_p5_ships(monkeypatch):
    monkeypatch.setattr(ls.importlib.util, "find_spec", lambda name: None)
    assert run(ls.automation_active(UID)) is False


@pytest.mark.parametrize("running", [True, False])
def test_automation_probe_honours_the_module(monkeypatch, running):
    _no_automation(monkeypatch, running=running)
    assert run(ls.automation_active(UID)) is running


def test_automation_probe_failure_is_running(monkeypatch):
    _no_automation(monkeypatch, raises=True)
    assert run(ls.automation_active(UID)) is True
    monkeypatch.setitem(sys.modules, "src.nadobro.strategy.arcus_runtime", types.ModuleType("x"))  # no function
    assert run(ls.automation_active(UID)) is True


def test_cancel_link_bumps_the_generation_and_drops_the_stash(monkeypatch):
    env = H.install(monkeypatch)
    p = _pending(env)
    run(ls.intake_key(user_id=UID, pending=p, pasted_text=RFC_SEED))
    ls.cancel_link(UID, "testnet")
    assert not ls._STASH and ls.current_generation(UID, "testnet") == p.generation + 1


def test_stash_is_bounded_and_ttl_purged(monkeypatch):
    env = H.install(monkeypatch)
    sealed = creds.seal_signing_seed(SEED_B)
    monkeypatch.setattr(ls, "_STASH_MAX", 3)
    for uid in range(1, 5):
        ls._stash_put((uid, "testnet"), ls._StashedKey(sealed, 1, uid, env.mono.now + uid))
    assert len(ls._STASH) == 3 and (1, "testnet") not in ls._STASH  # the oldest-expiring was evicted
    env.mono.now += 10
    assert ls._stash_get((4, "testnet")) is None and not ls._STASH
