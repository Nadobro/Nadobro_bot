import pytest

from src.nadobro.utils.env import env_bool, env_tristate


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NADO_TEST_FLAG", raising=False)
    yield


def test_env_bool_unset_uses_default(monkeypatch):
    assert env_bool("NADO_TEST_FLAG", True) is True
    assert env_bool("NADO_TEST_FLAG", False) is False


def test_env_bool_blank_uses_default(monkeypatch):
    monkeypatch.setenv("NADO_TEST_FLAG", "   ")
    assert env_bool("NADO_TEST_FLAG", True) is True
    assert env_bool("NADO_TEST_FLAG", False) is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_env_bool_truthy(monkeypatch, raw):
    monkeypatch.setenv("NADO_TEST_FLAG", raw)
    assert env_bool("NADO_TEST_FLAG", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "banana"])
def test_env_bool_non_truthy(monkeypatch, raw):
    monkeypatch.setenv("NADO_TEST_FLAG", raw)
    assert env_bool("NADO_TEST_FLAG", True) is False


def test_env_tristate_unset_is_none():
    assert env_tristate("NADO_TEST_FLAG") is None


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on"])
def test_env_tristate_truthy(monkeypatch, raw):
    monkeypatch.setenv("NADO_TEST_FLAG", raw)
    assert env_tristate("NADO_TEST_FLAG") is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_env_tristate_falsy(monkeypatch, raw):
    monkeypatch.setenv("NADO_TEST_FLAG", raw)
    assert env_tristate("NADO_TEST_FLAG") is False


@pytest.mark.parametrize("raw", ["", "   ", "banana", "maybe"])
def test_env_tristate_blank_or_unknown_is_none(monkeypatch, raw):
    monkeypatch.setenv("NADO_TEST_FLAG", raw)
    assert env_tristate("NADO_TEST_FLAG") is None


# --- inline-comment tolerance (the class that broke NanoGPT model ids) ---

from src.nadobro.utils.env import clean_env_value, env_float, env_int, env_str


def test_clean_env_value_strips_inline_comment():
    assert clean_env_value("30  # seconds") == "30"
    assert clean_env_value("  gpt-5-mini # cheap tasks ") == "gpt-5-mini"
    assert clean_env_value(None) == ""
    # no whitespace before '#' means it is part of the value, not a comment
    assert clean_env_value("a#b") == "a#b"


def test_env_bool_tolerates_inline_comment(monkeypatch):
    monkeypatch.setenv("NADO_TEST_FLAG", "true  # enable the thing")
    assert env_bool("NADO_TEST_FLAG", False) is True
    monkeypatch.setenv("NADO_TEST_FLAG", "off # disabled")
    assert env_bool("NADO_TEST_FLAG", True) is False


def test_env_tristate_tolerates_inline_comment(monkeypatch):
    monkeypatch.setenv("NADO_TEST_FLAG", "false # explicit off")
    assert env_tristate("NADO_TEST_FLAG") is False


def test_env_int_parses_and_defaults(monkeypatch):
    monkeypatch.delenv("NADO_TEST_NUM", raising=False)
    assert env_int("NADO_TEST_NUM", 7) == 7
    monkeypatch.setenv("NADO_TEST_NUM", "42")
    assert env_int("NADO_TEST_NUM", 7) == 42
    monkeypatch.setenv("NADO_TEST_NUM", "42  # workers")
    assert env_int("NADO_TEST_NUM", 7) == 42
    # garbage falls back to the default instead of raising at import time
    monkeypatch.setenv("NADO_TEST_NUM", "many")
    assert env_int("NADO_TEST_NUM", 7) == 7


def test_env_float_parses_and_defaults(monkeypatch):
    monkeypatch.delenv("NADO_TEST_NUM", raising=False)
    assert env_float("NADO_TEST_NUM", 1.5) == 1.5
    monkeypatch.setenv("NADO_TEST_NUM", "2.5 # seconds")
    assert env_float("NADO_TEST_NUM", 1.5) == 2.5
    monkeypatch.setenv("NADO_TEST_NUM", "soon")
    assert env_float("NADO_TEST_NUM", 1.5) == 1.5


def test_env_str_cleans_and_defaults(monkeypatch):
    monkeypatch.delenv("NADO_TEST_STR", raising=False)
    assert env_str("NADO_TEST_STR", "fallback") == "fallback"
    monkeypatch.setenv("NADO_TEST_STR", "value  # note")
    assert env_str("NADO_TEST_STR", "fallback") == "value"
    monkeypatch.setenv("NADO_TEST_STR", "   ")
    assert env_str("NADO_TEST_STR", "fallback") == "fallback"


# --- env_int_set (comma-separated id allowlists, e.g. ARCUS_ALLOWED_USER_IDS) ---

import logging  # noqa: E402

from src.nadobro.utils import env as env_mod  # noqa: E402
from src.nadobro.utils.env import env_int_set  # noqa: E402


@pytest.fixture()
def _int_set_env(monkeypatch):
    monkeypatch.delenv("NADO_TEST_INT_SET", raising=False)
    monkeypatch.setattr(env_mod, "_WARNED_INT_SET_TOKENS", set())
    yield


def test_env_int_set_unset_is_empty(_int_set_env):
    assert env_int_set("NADO_TEST_INT_SET") == frozenset()


@pytest.mark.parametrize("raw", ["", "   ", ",", " , ,", "  # owner only"])
def test_env_int_set_blank_or_comment_only_is_empty(monkeypatch, _int_set_env, raw):
    monkeypatch.setenv("NADO_TEST_INT_SET", raw)
    assert env_int_set("NADO_TEST_INT_SET") == frozenset()


def test_env_int_set_parses_list_with_blanks_padding_and_comment(monkeypatch, _int_set_env):
    monkeypatch.setenv("NADO_TEST_INT_SET", "111, 222 ,,333 # owner")
    result = env_int_set("NADO_TEST_INT_SET")
    assert result == frozenset({111, 222, 333})
    assert isinstance(result, frozenset)


def test_env_int_set_skips_garbage_and_warns_once(monkeypatch, _int_set_env, caplog):
    monkeypatch.setenv("NADO_TEST_INT_SET", "abc,5")
    with caplog.at_level(logging.WARNING, logger="src.nadobro.utils.env"):
        assert env_int_set("NADO_TEST_INT_SET") == frozenset({5})
        assert env_int_set("NADO_TEST_INT_SET") == frozenset({5})
    warnings = [r for r in caplog.records if "NADO_TEST_INT_SET" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "'abc'" in warnings[0].getMessage()


@pytest.mark.parametrize("raw", ["1.5", "0x10", "12a", "#,", "∞", "9" * 5000, "1 2"])
def test_env_int_set_never_raises_and_garbage_only_shrinks(monkeypatch, _int_set_env, raw):
    monkeypatch.setenv("NADO_TEST_INT_SET", f"{raw},7")
    assert env_int_set("NADO_TEST_INT_SET") == frozenset({7})
