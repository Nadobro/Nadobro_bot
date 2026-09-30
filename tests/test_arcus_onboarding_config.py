"""Arcus P3b config surface: the flag readers, the web-app / terms URLs, the
key-notice prefix and the venue_service Arcus network-mode helpers (unit, no DB)
(03 §11.4, §13)."""
from __future__ import annotations

import logging

import pytest

from src.nadobro import config
from src.nadobro.core import feature_flags as ff
from src.nadobro.utils import venue_scope

_UID = 990_035_001


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (
        "ARCUS_ENABLED", "ARCUS_ALLOWED_USER_IDS", "ARCUS_MAINNET_ENABLED", "ARCUS_KEY_EXPIRY_STOP_HOURS",
        "ARCUS_KEY_REMINDER_DAYS", "ARCUS_LINK_PENDING_TTL_S", "ARCUS_KEY_LIFECYCLE_INTERVAL_S",
        "ARCUS_TESTNET_APP_URL", "ARCUS_MAINNET_APP_URL", "ARCUS_TERMS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    ff._reset_arcus_flag_warnings_for_tests()
    yield
    ff._reset_arcus_flag_warnings_for_tests()


# --- flags ------------------------------------------------------------------------------------


def test_defaults():
    assert ff.arcus_key_expiry_stop_hours() == 24.0
    assert ff.arcus_key_reminder_days() == (14, 7, 2, 1)
    assert ff.arcus_link_pending_ttl_s() == 1800.0
    assert ff.arcus_key_lifecycle_interval_s() == 600


@pytest.mark.parametrize("raw, expected", [("48", 48.0), ("12.5 # half day", 12.5), ("168", 168.0),
                                            ("0", 24.0), ("-3", 24.0), ("169", 24.0), ("nan", 24.0),
                                            ("inf", 24.0), ("abc", 24.0)])
def test_stop_hours(monkeypatch, raw, expected):
    monkeypatch.setenv("ARCUS_KEY_EXPIRY_STOP_HOURS", raw)
    assert ff.arcus_key_expiry_stop_hours() == expected


def test_stop_hours_warns_once(monkeypatch, caplog):
    monkeypatch.setenv("ARCUS_KEY_EXPIRY_STOP_HOURS", "500")
    with caplog.at_level(logging.WARNING):
        ff.arcus_key_expiry_stop_hours()
        ff.arcus_key_expiry_stop_hours()
    assert sum("ARCUS_KEY_EXPIRY_STOP_HOURS" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("7,14", (14, 7)),
        ("1, 2, 2, 30 # note", (30, 2, 1)),
        ("180", (180,)),
        ("", (14, 7, 2, 1)),
        ("   ", (14, 7, 2, 1)),
        ("14,x,1", (14, 7, 2, 1)),
        ("0,7", (14, 7, 2, 1)),
        ("181", (14, 7, 2, 1)),
        (",,", (14, 7, 2, 1)),
    ],
)
def test_reminder_days(monkeypatch, raw, expected):
    monkeypatch.setenv("ARCUS_KEY_REMINDER_DAYS", raw)
    assert ff.arcus_key_reminder_days() == expected


@pytest.mark.parametrize("raw, expected", [("60", 300.0), ("600", 600.0), ("99999", 7200.0)])
def test_pending_ttl_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("ARCUS_LINK_PENDING_TTL_S", raw)
    assert ff.arcus_link_pending_ttl_s() == expected


@pytest.mark.parametrize("raw, expected", [("5", 60), ("900", 900), ("99999", 3600), ("x", 600)])
def test_lifecycle_interval_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("ARCUS_KEY_LIFECYCLE_INTERVAL_S", raw)
    assert ff.arcus_key_lifecycle_interval_s() == expected


def test_stop_hours_has_a_single_reader():
    """03 §13/§23: THE single reader of ARCUS_KEY_EXPIRY_STOP_HOURS (P5 calls it too)."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "nadobro"
    readers = [p for p in src.rglob("*.py") if "ARCUS_KEY_EXPIRY_STOP_HOURS" in p.read_text(encoding="utf-8")]
    assert [p.relative_to(src).as_posix() for p in readers] == ["core/feature_flags.py"]


# --- URLs ---------------------------------------------------------------------------------------


def test_default_urls():
    assert config.arcus_app_url("testnet") == "https://testnet.arcus.xyz"
    assert config.arcus_app_url("mainnet") == "https://app.arcus.xyz"
    assert config.arcus_api_keys_url("testnet") == "https://testnet.arcus.xyz/api-keys"
    assert config.arcus_api_keys_url("mainnet") == "https://app.arcus.xyz/api-keys"
    assert config.arcus_terms_url() == "https://arcus.xyz/legal/terms"


def test_url_overrides_are_call_time_and_https_only(monkeypatch):
    monkeypatch.setenv("ARCUS_TESTNET_APP_URL", "https://staging.arcus.example/  # staging")
    assert config.arcus_api_keys_url("testnet") == "https://staging.arcus.example/api-keys"
    for bad in ("http://testnet.arcus.xyz", "javascript:alert(1)", "https://user:pw@x.example", "ftp://x"):
        monkeypatch.setenv("ARCUS_TESTNET_APP_URL", bad)
        with pytest.raises(ValueError) as exc:
            config.arcus_app_url("testnet")
        assert bad not in str(exc.value)
    monkeypatch.setenv("ARCUS_TERMS_URL", "http://arcus.xyz/legal/terms")
    with pytest.raises(ValueError):
        config.arcus_terms_url()


def test_app_url_cannot_point_at_the_other_network(monkeypatch):
    monkeypatch.setenv("ARCUS_TESTNET_APP_URL", "https://app.arcus.xyz")
    with pytest.raises(ValueError):
        config.arcus_app_url("testnet")
    monkeypatch.delenv("ARCUS_TESTNET_APP_URL")
    monkeypatch.setenv("ARCUS_MAINNET_APP_URL", "https://testnet.arcus.xyz/")
    with pytest.raises(ValueError):
        config.arcus_app_url("mainnet")


@pytest.mark.parametrize("bad", ["arcus_testnet", "Testnet", " mainnet", "", None])
def test_app_url_needs_an_exact_network(bad):
    with pytest.raises(ValueError):
        config.arcus_app_url(bad)  # type: ignore[arg-type]


def test_key_notice_prefix():
    assert venue_scope.ARCUS_KEY_NOTICE_PREFIX == "arcus_key_notice:"
    assert "ARCUS_KEY_NOTICE_PREFIX" in venue_scope.__all__


# --- venue_service Arcus network mode (unit) ---------------------------------------------------------


class _User:
    def __init__(self, mode):
        self.arcus_network_mode = mode


def test_get_arcus_network_mode(monkeypatch):
    from src.nadobro.users import venue_service as vs

    monkeypatch.setattr(vs, "get_user", lambda uid: None)
    assert vs.get_arcus_network_mode(_UID) == "testnet"
    for stored, expected in (("mainnet", "mainnet"), ("testnet", "testnet"), ("MAINNET", "testnet"),
                             ("arcus_mainnet", "testnet"), (None, "testnet")):
        monkeypatch.setattr(vs, "get_user", lambda uid, s=stored: _User(s))
        assert vs.get_arcus_network_mode(_UID) == expected

    def boom(uid):
        raise RuntimeError("db down")

    monkeypatch.setattr(vs, "get_user", boom)
    with pytest.raises(RuntimeError):  # DENIED != EMPTY
        vs.get_arcus_network_mode(_UID)


def test_set_arcus_network_mode_validates_before_db(monkeypatch):
    from src.nadobro.users import venue_service as vs

    def no_db(*a, **k):
        raise AssertionError("no DB call expected")

    monkeypatch.setattr(vs, "execute_returning", no_db)
    monkeypatch.setattr(vs, "invalidate_user_cache", no_db)
    for bad in ("arcus_mainnet", "Mainnet", "", None):
        with pytest.raises(ValueError):
            vs.set_arcus_network_mode(_UID, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        vs.set_arcus_network_mode(0, "testnet")
    assert vs.set_arcus_network_mode(_UID, "mainnet") == "not_allowed"  # flags off: no write


def test_set_arcus_network_mode_cas(monkeypatch):
    from src.nadobro.users import venue_service as vs

    calls, audits, invalidated = [], [], []
    monkeypatch.setattr(vs, "execute_returning", lambda sql, params: calls.append((sql, params)) or {"x": 1})
    monkeypatch.setattr(vs, "invalidate_user_cache", invalidated.append)
    monkeypatch.setattr(vs, "record_audit_event", lambda *a: audits.append(a))
    assert vs.set_arcus_network_mode(_UID, "testnet") == "switched"
    sql, params = calls[-1]
    assert "arcus_network_mode = %s" in sql and "network_mode =" not in sql.replace("arcus_network_mode", "")
    assert params == ("testnet", _UID, "mainnet")
    assert audits[-1] == (_UID, "arcus_mode_switched", "mainnet->testnet")
    assert invalidated == [_UID]
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_UID))
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    monkeypatch.setattr(vs, "execute_returning", lambda sql, params: None)
    assert vs.set_arcus_network_mode(_UID, "mainnet") == "unchanged"
    assert invalidated == [_UID, _UID]
