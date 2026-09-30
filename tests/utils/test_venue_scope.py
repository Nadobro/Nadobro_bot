"""utils/venue_scope.py — helper semantics.

Contract under test (Arcus P1, scope A):
1. a non-Nado scope token (stripped+lowercased form starts with ``arcus``)
   ALWAYS raises VenueScopeError, whatever the site policy;
2. every other input returns EXACTLY what the site's former ternary returned —
   each preset is compared against a verbatim copy of the legacy expression
   it replaced (the "oracle") over a broad input corpus, and against a
   hard-coded golden table;
3. inputs outside {None, '', 'testnet', 'mainnet'} keep the legacy result and
   log ONE warning per (site, value) — never a raise.

Per-site goldens (every swapped call site, end to end) live in
tests/test_venue_scope_site_goldens.py.
"""
from __future__ import annotations

import enum
import logging

import pytest

from src.nadobro.utils import venue_scope as vs
from src.nadobro.utils.venue_scope import (
    EMPTY_MAINNET_EXACT_ELSE_TESTNET,
    EXACT_ELSE_MAINNET,
    EXACT_ELSE_TESTNET,
    LOWER_ELSE_MAINNET,
    LOWER_ELSE_TESTNET,
    STR_EXACT_ELSE_TESTNET,
    STR_STRIP_LOWER_ELSE_MAINNET,
    STRIP_LOWER_ELSE_MAINNET,
    NetworkPolicy,
    VenueScopeError,
    coerce_nado_network,
    guard_nado_scope,
    is_non_nado_scope,
)


class _Mode(enum.Enum):  # stands in for models.database.NetworkMode (plain Enum)
    TESTNET = "testnet"
    MAINNET = "mainnet"


class _ArcusEnum(enum.Enum):
    MAIN = "arcus_mainnet"


@pytest.fixture(autouse=True)
def _fresh_warnings():
    vs._reset_warnings_for_tests()
    yield
    vs._reset_warnings_for_tests()


# The golden input order used across the P1 spec.
GOLDEN = [None, "", "testnet", "mainnet", "MAINNET", "Testnet", "arcus_mainnet", "arcus_testnet", "garbage"]
# Extra inputs that pin the strip and enum policies.
EXTRA = [" testnet", "testnet ", "ARCUS_MAINNET", " arcus_mainnet", _Mode.TESTNET]
ARCUS = {"arcus_mainnet", "arcus_testnet", "ARCUS_MAINNET", " arcus_mainnet"}

T, M, X = "testnet", "mainnet", "RAISE"

# Verbatim copies of the legacy expressions each preset replaced.
LEGACY = {
    "LOWER_ELSE_MAINNET": lambda v: "testnet" if str(v).lower() == "testnet" else "mainnet",
    "STRIP_LOWER_ELSE_MAINNET": lambda v: (
        "testnet" if str(v or "mainnet").strip().lower() == "testnet" else "mainnet"
    ),
    "STR_STRIP_LOWER_ELSE_MAINNET": lambda v: (
        "testnet" if isinstance(v, str) and v.strip().lower() == "testnet" else "mainnet"
    ),
    "EXACT_ELSE_MAINNET": lambda v: "testnet" if v == "testnet" else "mainnet",
    "EXACT_ELSE_TESTNET": lambda v: "mainnet" if v == "mainnet" else "testnet",
    "STR_EXACT_ELSE_TESTNET": lambda v: "mainnet" if str(v) == "mainnet" else "testnet",
    "LOWER_ELSE_TESTNET": lambda v: "mainnet" if str(v).lower() == "mainnet" else "testnet",
    "EMPTY_MAINNET_EXACT_ELSE_TESTNET": lambda v: (
        "mainnet" if (v or "mainnet") == "mainnet" else "testnet"
    ),
}
POLICIES = {
    "LOWER_ELSE_MAINNET": LOWER_ELSE_MAINNET,
    "STRIP_LOWER_ELSE_MAINNET": STRIP_LOWER_ELSE_MAINNET,
    "STR_STRIP_LOWER_ELSE_MAINNET": STR_STRIP_LOWER_ELSE_MAINNET,
    "EXACT_ELSE_MAINNET": EXACT_ELSE_MAINNET,
    "EXACT_ELSE_TESTNET": EXACT_ELSE_TESTNET,
    "STR_EXACT_ELSE_TESTNET": STR_EXACT_ELSE_TESTNET,
    "LOWER_ELSE_TESTNET": LOWER_ELSE_TESTNET,
    "EMPTY_MAINNET_EXACT_ELSE_TESTNET": EMPTY_MAINNET_EXACT_ELSE_TESTNET,
}

# Hard-coded goldens (GOLDEN order, then EXTRA order). "RAISE" = VenueScopeError.
EXPECTED = {
    "LOWER_ELSE_MAINNET": ([M, M, T, M, M, T, X, X, M], [M, M, X, X, M]),
    "STRIP_LOWER_ELSE_MAINNET": ([M, M, T, M, M, T, X, X, M], [T, T, X, X, M]),
    "STR_STRIP_LOWER_ELSE_MAINNET": ([M, M, T, M, M, T, X, X, M], [T, T, X, X, M]),
    "EXACT_ELSE_MAINNET": ([M, M, T, M, M, M, X, X, M], [M, M, X, X, M]),
    "EXACT_ELSE_TESTNET": ([T, T, T, M, T, T, X, X, T], [T, T, X, X, T]),
    "STR_EXACT_ELSE_TESTNET": ([T, T, T, M, T, T, X, X, T], [T, T, X, X, T]),
    "LOWER_ELSE_TESTNET": ([T, T, T, M, M, T, X, X, T], [T, T, X, X, T]),
    "EMPTY_MAINNET_EXACT_ELSE_TESTNET": ([M, M, T, M, T, T, X, X, T], [T, T, X, X, T]),
}


def _run(value, policy):
    try:
        return coerce_nado_network(value, policy, site="test")
    except VenueScopeError:
        return X


# --- constants ------------------------------------------------------------

def test_scope_tokens_are_underscore_only_and_disjoint_from_nado():
    assert vs.VENUES == ("nado", "arcus")
    assert vs.NADO_NETWORKS == ("testnet", "mainnet")
    assert vs.ARCUS_SCOPES == ("arcus_testnet", "arcus_mainnet")
    for tok in vs.ARCUS_SCOPES:
        assert ":" not in tok and "-" not in tok
        assert tok.isidentifier()  # f"trades_{tok}" stays a valid identifier
        assert tok not in vs.NADO_NETWORKS
        assert is_non_nado_scope(tok)
    assert vs.ARCUS_STRATEGY_BOT_PREFIX == "arcus_strategy_bot:"
    assert vs.ARCUS_USER_SETTINGS_PREFIX == "arcus_user_settings:"


def test_venue_scope_error_is_a_value_error():
    # Existing strict validators raise ValueError; callers catching that keep working.
    assert issubclass(VenueScopeError, ValueError)


# --- is_non_nado_scope / guard ---------------------------------------------

@pytest.mark.parametrize(
    "value",
    ["arcus", "arcus_mainnet", "arcus_testnet", "ARCUS_MAINNET", "Arcus_Testnet",
     " arcus_mainnet", "arcus_mainnet\n", "arcus-anything", "arcus:x", _ArcusEnum.MAIN],
)
def test_is_non_nado_scope_true(value):
    assert is_non_nado_scope(value) is True


@pytest.mark.parametrize(
    "value",
    [None, "", "testnet", "mainnet", "MAINNET", "Testnet", " testnet", "garbage",
     "xarcus", "nado", "trades_arcus_testnet", 0, 1, False, _Mode.TESTNET, _Mode.MAINNET, object()],
)
def test_is_non_nado_scope_false(value):
    assert is_non_nado_scope(value) is False


@pytest.mark.parametrize("value", [None, "", "testnet", "mainnet", "MAINNET", "garbage", _Mode.MAINNET, 7])
def test_guard_returns_value_untouched(value):
    assert guard_nado_scope(value, site="t") is value


@pytest.mark.parametrize("value", ["arcus_mainnet", "arcus_testnet", " ARCUS_MAINNET", _ArcusEnum.MAIN])
def test_guard_raises_on_non_nado_scope(value):
    with pytest.raises(VenueScopeError, match="site_x"):
        guard_nado_scope(value, site="site_x")


# --- coerce_nado_network goldens -------------------------------------------

@pytest.mark.parametrize("name", sorted(POLICIES))
def test_policy_golden_table(name):
    golden, extra = EXPECTED[name]
    assert [_run(v, POLICIES[name]) for v in GOLDEN] == golden
    assert [_run(v, POLICIES[name]) for v in EXTRA] == extra


# A broad corpus: every non-arcus value must reproduce the legacy oracle exactly.
_CORPUS = [
    None, "", " ", "  ", "testnet", "mainnet", "TESTNET", "MAINNET", "Testnet", "Mainnet",
    " testnet", "testnet ", " mainnet ", "\tmainnet\n", "test", "main", "prod", "garbage",
    "testnet:1", "mainnet_x", "nado", "None", "none", 0, 1, 0.0, False, True, [], {},
    _Mode.TESTNET, _Mode.MAINNET, object(),
]


@pytest.mark.parametrize("name", sorted(POLICIES))
def test_policy_matches_legacy_oracle_on_corpus(name):
    policy, legacy = POLICIES[name], LEGACY[name]
    for value in _CORPUS:
        assert coerce_nado_network(value, policy, site="corpus") == legacy(value), (name, value)


@pytest.mark.parametrize("name", sorted(POLICIES))
@pytest.mark.parametrize("value", sorted(ARCUS))
def test_every_policy_raises_on_arcus_scope(name, value):
    # Legacy folded these into a real Nado network (see the oracle); now they raise.
    assert LEGACY[name](value) in ("testnet", "mainnet")
    with pytest.raises(VenueScopeError):
        coerce_nado_network(value, POLICIES[name], site="arcus")


@pytest.mark.parametrize("name", sorted(POLICIES))
def test_canonical_tokens_map_to_themselves_under_every_policy(name):
    # Soundness of the canonical fast path.
    for tok in ("testnet", "mainnet"):
        assert LEGACY[name](tok) == tok
        assert coerce_nado_network(tok, POLICIES[name], site="c") == tok


def test_policy_rejects_non_nado_fallback():
    with pytest.raises(ValueError):
        NetworkPolicy(fallback="arcus_mainnet", case_sensitive=True)
    with pytest.raises(ValueError):
        NetworkPolicy(fallback="mainnet", case_sensitive=True, empty="MAINNET")
    assert LOWER_ELSE_MAINNET.match == "testnet"
    assert EXACT_ELSE_TESTNET.match == "mainnet"


# --- warnings --------------------------------------------------------------

def _scope_warnings(caplog):
    return [r for r in caplog.records if r.name == vs.__name__ and r.levelno == logging.WARNING]


def test_canonical_and_empty_inputs_never_warn(caplog):
    caplog.set_level(logging.WARNING, logger=vs.__name__)
    for v in (None, "", "testnet", "mainnet"):
        for policy in POLICIES.values():
            coerce_nado_network(v, policy, site="quiet")
    assert _scope_warnings(caplog) == []


def test_non_canonical_warns_once_per_site_and_value(caplog):
    caplog.set_level(logging.WARNING, logger=vs.__name__)
    for _ in range(3):
        assert coerce_nado_network("MAINNET", EXACT_ELSE_TESTNET, site="site_a") == "testnet"
    coerce_nado_network("MAINNET", LOWER_ELSE_MAINNET, site="site_b")
    coerce_nado_network("garbage", LOWER_ELSE_MAINNET, site="site_b")
    msgs = [r.getMessage() for r in _scope_warnings(caplog)]
    assert len(msgs) == 3
    assert "'MAINNET' at site_a -> testnet" in msgs[0]
    assert "'MAINNET' at site_b -> mainnet" in msgs[1]
    assert "'garbage' at site_b -> mainnet" in msgs[2]


def test_arcus_raise_does_not_log_the_non_canonical_warning(caplog):
    caplog.set_level(logging.WARNING, logger=vs.__name__)
    with pytest.raises(VenueScopeError):
        coerce_nado_network("arcus_mainnet", LOWER_ELSE_MAINNET, site="s")
    assert _scope_warnings(caplog) == []


def test_warning_dedupe_is_bounded(caplog):
    caplog.set_level(logging.WARNING, logger=vs.__name__)
    for i in range(vs._WARN_CAP + 50):
        coerce_nado_network(f"junk{i}", LOWER_ELSE_MAINNET, site="flood")
    assert len(vs._warned) == vs._WARN_CAP
    assert len(_scope_warnings(caplog)) == vs._WARN_CAP


def test_warning_truncates_long_values(caplog):
    caplog.set_level(logging.WARNING, logger=vs.__name__)
    coerce_nado_network("x" * 500, LOWER_ELSE_MAINNET, site="long")
    (rec,) = _scope_warnings(caplog)
    assert "x" * 60 not in rec.getMessage()


def test_module_is_a_stdlib_only_leaf():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(vs.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(("src", "nadobro")), node.module
        if isinstance(node, ast.Import):
            for a in node.names:
                assert not a.name.startswith(("src", "nadobro")), a.name


# --- Arcus network helpers (Arcus P2, 02 §3.1): appended -----------------------


class _StrSub(str):
    pass


def test_parse_arcus_net_exact_tokens_only():
    assert vs.parse_arcus_net("testnet") == "testnet"
    assert vs.parse_arcus_net("mainnet") == "mainnet"
    for bad in ("Testnet", " testnet", "testnet ", "MAINNET", "arcus_testnet", "", None,
                b"testnet", _StrSub("testnet"), 1, _Mode.TESTNET):
        with pytest.raises(ValueError) as exc:
            vs.parse_arcus_net(bad)
        assert str(exc.value) == "not an Arcus network token"  # fixed text: never echoes the input


def test_arcus_scope_for():
    assert vs.arcus_scope_for("testnet") == "arcus_testnet" == vs.ARCUS_TESTNET_SCOPE
    assert vs.arcus_scope_for("mainnet") == "arcus_mainnet" == vs.ARCUS_MAINNET_SCOPE
    for bad in ("arcus_testnet", "Mainnet", "", None):
        with pytest.raises(ValueError):
            vs.arcus_scope_for(bad)


def test_arcus_net_from_scope():
    assert vs.arcus_net_from_scope("arcus_mainnet") == "mainnet"
    assert vs.arcus_net_from_scope("arcus_testnet") == "testnet"
    for bad in ("mainnet", "ARCUS_MAINNET", " arcus_mainnet", "arcus_", None, _StrSub("arcus_mainnet")):
        with pytest.raises(ValueError) as exc:
            vs.arcus_net_from_scope(bad)
        assert str(exc.value) == "not an Arcus scope token"


def test_arcus_scope_round_trip():
    for net in vs.ARCUS_NETWORK_MODES:
        assert vs.arcus_net_from_scope(vs.arcus_scope_for(net)) == net
    for scope in vs.ARCUS_SCOPES:
        assert vs.arcus_scope_for(vs.arcus_net_from_scope(scope)) == scope


def test_arcus_helpers_are_exported_and_silent(caplog):
    for name in ("parse_arcus_net", "arcus_scope_for", "arcus_net_from_scope"):
        assert name in vs.__all__
    caplog.set_level(logging.DEBUG, logger=vs.__name__)
    with pytest.raises(ValueError):
        vs.parse_arcus_net("garbage")
    vs.arcus_scope_for("testnet")
    assert _scope_warnings(caplog) == [] and not caplog.records
