"""ARCUS_ENABLED / ARCUS_ALLOWED_USER_IDS (Arcus P1): default OFF, fail-closed.

Both flags are read on every call, so monkeypatch.setenv takes effect at once.
"""
from __future__ import annotations

import pytest

from src.nadobro.core.feature_flags import (
    arcus_allowed_user_ids,
    arcus_enabled,
    arcus_enabled_for,
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
