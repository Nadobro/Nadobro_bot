"""scripts/arcus_link_dryrun.py — the OWNER-RUN link dry-run (03 §22).

Runs end to end against the scripted fake venue of ``arcus_link_helpers`` (no
network: a real transport call fails the test). The key here is the public RFC
8032 test vector — never a real key. Asserts the guards and that neither the
seed nor its public key is ever printed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

import arcus_link_helpers as H
from arcus_link_helpers import ADDR, NOW_MS, RFC_PUB, RFC_SEED, WALLET_ADDR, WALLET_ED25519_PUB, WALLET_SEED, entry, ok
from src.nadobro.users import arcus_link_service as ls

REPO = Path(__file__).resolve().parents[1]


def _load() -> Any:
    name = "_arcus_script_arcus_link_dryrun"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "arcus_link_dryrun.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dry = _load()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    async def boom(self, request):  # pragma: no cover - a test that reaches it fails
        raise AssertionError("real network in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", boom)
    for name in ("FLY_APP_NAME", "FLY_MACHINE_ID", "ARCUS_TESTNET_REST_URL", "ARCUS_MAINNET_REST_URL",
                 "ARCUS_PROBE_SIGNING_KEY", "ARCUS_PROBE_NETWORK", "ARCUS_PROBE_ADDRESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARCUS_PROBE_NETWORK", "testnet")
    monkeypatch.setenv("ARCUS_PROBE_ADDRESS", ADDR)
    ls._reset_for_tests()
    yield
    ls._reset_for_tests()


def _fake(monkeypatch, **kw):
    env = H.install(monkeypatch, **kw)
    monkeypatch.setattr(ls, "_now_ms", lambda: NOW_MS)
    return env


def _secret_free(out: str) -> None:
    for s in (RFC_SEED, RFC_SEED.upper(), RFC_PUB, WALLET_SEED, WALLET_ED25519_PUB):
        assert s not in out


def test_keyless_run_prechecks_the_address(monkeypatch, capsys):
    env = _fake(monkeypatch)
    assert dry.main([]) == 0
    out = capsys.readouterr().out
    assert "precheck: eligible" in out and "keyless" in out
    assert env.db.all_args == []  # no database
    env.client.account = [H.WHITELIST]
    assert dry.main([]) == 1
    assert "not_whitelisted" in capsys.readouterr().out


def test_key_that_would_link(monkeypatch, capsys):
    env = _fake(monkeypatch)
    env.client.api_keys = [ok([entry(until=NOW_MS + 180 * H.DAY_MS)])]
    monkeypatch.setenv("ARCUS_PROBE_SIGNING_KEY", "0x" + RFC_SEED)
    assert dry.main([]) == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "key found: yes" in out and "status: ACTIVE" in out and "scope: idx 0" in out
    assert "withdraw: no" in out and "verdict: the key would link" in out
    assert "ARCUS_PROBE_SIGNING_KEY" not in __import__("os").environ  # popped at once
    _secret_free(out)
    assert dry.key_fingerprint(RFC_PUB) in out


@pytest.mark.parametrize(
    "keys, verdict",
    [
        ([ok([])], "key_not_found"),
        ([H.THROTTLED], "busy"),
        ([ok([entry(permissions=("withdraw",))])], "key_has_withdraw"),
        ([ok([entry(index=2)])], "key_wrong_subaccount"),
        ([ok([entry(until=NOW_MS + 3_600_000)])], "key_expires_too_soon"),
    ],
)
def test_key_problems_exit_1(monkeypatch, capsys, keys, verdict):
    env = _fake(monkeypatch)
    env.client.api_keys = keys
    monkeypatch.setenv("ARCUS_PROBE_SIGNING_KEY", RFC_SEED)
    assert dry.main([]) == 1
    out = capsys.readouterr().out
    assert f"verdict: {verdict}" in out
    _secret_free(out)


def test_wallet_key_is_refused_before_any_venue_call(monkeypatch, capsys):
    env = _fake(monkeypatch)
    monkeypatch.setenv("ARCUS_PROBE_ADDRESS", WALLET_ADDR)
    monkeypatch.setenv("ARCUS_PROBE_SIGNING_KEY", WALLET_SEED)
    assert dry.main([]) == 2
    captured = capsys.readouterr()
    assert ls.TEXT_R_WALLET_KEY in captured.err
    assert env.client.calls == []
    _secret_free(captured.out + captured.err)


@pytest.mark.parametrize(
    "env_name, value, needle",
    [
        ("ARCUS_PROBE_NETWORK", "mainnet", "testnet only"),
        ("ARCUS_PROBE_NETWORK", "arcus_testnet", "testnet only"),
        ("ARCUS_PROBE_ADDRESS", "0x123", "ARCUS_PROBE_ADDRESS"),
        ("ARCUS_PROBE_SIGNING_KEY", "zz" * 32, "not a 64-hex"),
        ("ARCUS_TESTNET_REST_URL", "https://api.arcus.xyz", "testnet"),
        ("FLY_APP_NAME", "nadobro", "Fly machine"),
    ],
)
def test_guards_refuse_with_exit_2(monkeypatch, capsys, env_name, value, needle):
    env = _fake(monkeypatch)
    monkeypatch.setenv(env_name, value)
    assert dry.main([]) == 2
    captured = capsys.readouterr()
    assert needle in captured.err and env.client.calls == []
    assert value not in captured.err or env_name != "ARCUS_PROBE_SIGNING_KEY"


def test_allow_fly_overrides_the_fly_guard(monkeypatch, capsys):
    _fake(monkeypatch)
    monkeypatch.setenv("FLY_MACHINE_ID", "abc")
    assert dry.main(["--allow-fly"]) == 0
