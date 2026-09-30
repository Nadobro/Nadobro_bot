"""ARCUS_ENABLED / ARCUS_ALLOWED_USER_IDS (Arcus P1): default OFF, fail-closed.

Both flags are read on every call, so monkeypatch.setenv takes effect at once.
"""
from __future__ import annotations

import logging

import pytest

from src.nadobro.core import feature_flags as ff
from src.nadobro.core.feature_flags import (
    arcus_allowed_user_ids,
    arcus_catalog_max_age_s,
    arcus_catalog_refresh_s,
    arcus_clock_max_age_s,
    arcus_enabled,
    arcus_enabled_for,
    arcus_force_ipv4,
    arcus_gtt_days,
    arcus_ip_l0_reserve,
    arcus_mainnet_enabled,
    arcus_market_allowlist,
)

_UID = 123


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_ALLOWED_USER_IDS", raising=False)
    yield


def test_defaults_are_off_and_nobody():
    assert arcus_enabled() is False
    assert arcus_allowed_user_ids() == frozenset()
    assert arcus_enabled_for(_UID) is False


def test_allowlist_alone_does_not_enable(monkeypatch):
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_UID))
    assert arcus_allowed_user_ids() == frozenset({_UID})
    assert arcus_enabled_for(_UID) is False


def test_flag_on_with_empty_allowlist_is_nobody(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    assert arcus_enabled() is True
    assert arcus_enabled_for(_UID) is False
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", "   ")
    assert arcus_enabled_for(_UID) is False


def test_flag_on_and_allowlisted(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", f"{_UID}, 456 # beta cohort")
    assert arcus_enabled_for(_UID) is True
    assert arcus_enabled_for(456) is True
    assert arcus_enabled_for(_UID + 1) is False


def test_flag_value_with_inline_comment_counts_as_on(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "true  # beta")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_UID))
    assert arcus_enabled() is True
    assert arcus_enabled_for(_UID) is True


@pytest.mark.parametrize("raw", ["0", "off", "false", "no", "banana", ""])
def test_flag_off_values_refuse_even_an_allowlisted_user(monkeypatch, raw):
    monkeypatch.setenv("ARCUS_ENABLED", raw)
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_UID))
    assert arcus_enabled() is False
    assert arcus_enabled_for(_UID) is False


@pytest.mark.parametrize("bad", [None, "x", "", True, False, 123.0, 123.9, [123], object()])
def test_bad_ids_are_refused(monkeypatch, bad):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", "0,1,123")
    assert arcus_enabled_for(bad) is False


def test_decimal_string_id_is_accepted(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(_UID))
    assert arcus_enabled_for(str(_UID)) is True


def test_garbage_allowlist_entry_only_shrinks_the_set(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", "*,all,123")
    assert arcus_allowed_user_ids() == frozenset({123})
    assert arcus_enabled_for(999) is False


# --- P2 readers (02 §3.3): appended; the P1 cases above are unchanged ---------------

_P2_ENVS = (
    "ARCUS_MAINNET_ENABLED",
    "ARCUS_FORCE_IPV4",
    "ARCUS_MARKET_ALLOWLIST",
    "ARCUS_IP_L0_RESERVE",
    "ARCUS_CATALOG_REFRESH_S",
    "ARCUS_CATALOG_MAX_AGE_S",
    "ARCUS_GTT_DAYS",
    "ARCUS_CLOCK_MAX_AGE_S",
)


@pytest.fixture
def _p2_env(monkeypatch):
    for name in _P2_ENVS:
        monkeypatch.delenv(name, raising=False)
    ff._reset_arcus_flag_warnings_for_tests()
    yield
    ff._reset_arcus_flag_warnings_for_tests()


def _clamp_warnings(caplog):
    return [r for r in caplog.records if r.name == ff.__name__ and "out of range" in r.getMessage()]


@pytest.mark.usefixtures("_p2_env")
def test_p2_readers_defaults():
    assert arcus_mainnet_enabled() is False
    assert arcus_force_ipv4() is False
    assert arcus_market_allowlist() == frozenset({"BTC-USD", "ETH-USD", "SOL-USD"})
    assert arcus_ip_l0_reserve() == 300
    assert arcus_catalog_refresh_s() == 60.0
    assert arcus_catalog_max_age_s() == 300.0
    assert arcus_gtt_days() == 40
    assert arcus_clock_max_age_s() == 900.0


@pytest.mark.usefixtures("_p2_env")
def test_mainnet_flag_requires_the_master_switch(monkeypatch):
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    assert arcus_mainnet_enabled() is False  # master ARCUS_ENABLED off
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    assert arcus_mainnet_enabled() is True
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "0")
    assert arcus_mainnet_enabled() is False  # master alone never opens mainnet


@pytest.mark.usefixtures("_p2_env")
def test_p2_readers_honour_inline_comments(monkeypatch):
    monkeypatch.setenv("ARCUS_FORCE_IPV4", "1  # pinned egress")
    monkeypatch.setenv("ARCUS_IP_L0_RESERVE", "200  # ops")
    monkeypatch.setenv("ARCUS_CATALOG_MAX_AGE_S", "120.5 # seconds")
    monkeypatch.setenv("ARCUS_MARKET_ALLOWLIST", "BTC-USD # btc only")
    assert arcus_force_ipv4() is True
    assert arcus_ip_l0_reserve() == 200
    assert arcus_catalog_max_age_s() == 120.5
    assert arcus_market_allowlist() == frozenset({"BTC-USD"})


@pytest.mark.usefixtures("_p2_env")
@pytest.mark.parametrize(
    "name,raw,reader,expected",
    [
        ("ARCUS_GTT_DAYS", "10", arcus_gtt_days, 32),
        ("ARCUS_GTT_DAYS", "365", arcus_gtt_days, 180),
        ("ARCUS_IP_L0_RESERVE", "5000", arcus_ip_l0_reserve, 1000),
        ("ARCUS_IP_L0_RESERVE", "-3", arcus_ip_l0_reserve, 0),
        ("ARCUS_CLOCK_MAX_AGE_S", "5", arcus_clock_max_age_s, 120.0),
        ("ARCUS_CATALOG_REFRESH_S", "1", arcus_catalog_refresh_s, 15.0),
        ("ARCUS_CATALOG_MAX_AGE_S", "99999", arcus_catalog_max_age_s, 3600.0),
        ("ARCUS_CLOCK_MAX_AGE_S", "nan", arcus_clock_max_age_s, 900.0),
        ("ARCUS_CATALOG_REFRESH_S", "inf", arcus_catalog_refresh_s, 60.0),
    ],
)
def test_p2_readers_clamp_with_one_warning(monkeypatch, caplog, name, raw, reader, expected):
    caplog.set_level(logging.WARNING, logger=ff.__name__)
    monkeypatch.setenv(name, raw)
    assert reader() == expected
    assert reader() == expected
    warnings = _clamp_warnings(caplog)
    assert len(warnings) == 1 and name in warnings[0].getMessage()


@pytest.mark.usefixtures("_p2_env")
def test_p2_readers_in_range_values_do_not_warn(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=ff.__name__)
    monkeypatch.setenv("ARCUS_GTT_DAYS", "32")
    monkeypatch.setenv("ARCUS_CLOCK_MAX_AGE_S", "3600")
    assert arcus_gtt_days() == 32 and arcus_clock_max_age_s() == 3600.0
    assert _clamp_warnings(caplog) == []


@pytest.mark.usefixtures("_p2_env")
def test_p2_garbage_numeric_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ARCUS_GTT_DAYS", "forty")
    monkeypatch.setenv("ARCUS_CATALOG_REFRESH_S", "soon")
    assert arcus_gtt_days() == 40
    assert arcus_catalog_refresh_s() == 60.0


@pytest.mark.usefixtures("_p2_env")
def test_market_allowlist_parse(monkeypatch):
    monkeypatch.setenv("ARCUS_MARKET_ALLOWLIST", " btc-usd, ,DOGE-USD ")
    assert arcus_market_allowlist() == frozenset({"BTC-USD", "DOGE-USD"})
    monkeypatch.setenv("ARCUS_MARKET_ALLOWLIST", " , ")
    assert arcus_market_allowlist() == frozenset()  # only ever shrinks: nothing tradable


@pytest.mark.usefixtures("_p2_env")
def test_p2_readers_reread_env_every_call(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    assert arcus_force_ipv4() is False and arcus_gtt_days() == 40 and arcus_mainnet_enabled() is False
    monkeypatch.setenv("ARCUS_FORCE_IPV4", "true")
    monkeypatch.setenv("ARCUS_GTT_DAYS", "45")
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "yes")
    monkeypatch.setenv("ARCUS_CATALOG_REFRESH_S", "30")
    assert arcus_force_ipv4() is True and arcus_gtt_days() == 45 and arcus_mainnet_enabled() is True
    assert arcus_catalog_refresh_s() == 30.0
    monkeypatch.delenv("ARCUS_GTT_DAYS")
    assert arcus_gtt_days() == 40
