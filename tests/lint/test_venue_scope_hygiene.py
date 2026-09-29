"""Static guards for the Nado/Arcus venue boundary (Arcus P1, AD-11 layer 7).

1. No NEW network-coercion compares: every comparison of a value against a
   ``'testnet'`` / ``'mainnet'`` literal (``==``, ``!=``, ``is``, ``in (...)``,
   ``.startswith/.endswith("testnet")``) outside ``utils/venue_scope.py`` must be
   on the allowlist below (UI labels, strict validators, enum policy checks) or
   compare the canonical output of ``coerce_nado_network(...)``. Anything else
   is a local ternary that would silently fold ``arcus_mainnet`` into a Nado
   network — route it through ``utils.venue_scope`` with the site's legacy
   policy instead.
2. The bot_state ``LIKE`` enumerators are pinned per file, and no Arcus key
   (``arcus_strategy_bot:`` / ``arcus_user_settings:``) can ever match a Nado
   LIKE pattern (``_`` is a LIKE wildcard — translated faithfully).
3. The network-less ``get_user_nado_client(uid)`` / ``get_user_readonly_client(uid)``
   fetches are pinned: they resolve to the user's NADO network even for an
   Arcus-view user, which is why the venue gate + refusal layers exist. A new
   one needs a conscious review (pass the network, or confirm it is Nado-only).
4. Every strategy-session reader caller passes a network (Arcus sessions carry
   scope tokens in ``strategy_sessions.network``; an unfiltered read would mix
   them into Nado views).

Allowlists are keyed by (file, stripped source line) with an exact count —
stale entries fail too, so the lists only ever shrink.
"""
from __future__ import annotations

import ast
import collections
import functools
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nadobro"
SCAN = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts) + [REPO / "main.py"]
HELPER = SRC / "utils" / "venue_scope.py"

_NET = {"testnet", "mainnet"}


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(REPO))


@functools.cache  # one parse + walk of the tree shared by every test here
def _nodes(path: pathlib.Path) -> tuple[tuple[ast.AST, ...], tuple[str, ...]]:
    text = path.read_text(encoding="utf-8")
    return tuple(ast.walk(ast.parse(text))), tuple(text.splitlines())


# ---------------------------------------------------------------------------
# 1. network-literal compares
# ---------------------------------------------------------------------------

def _is_net_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.strip().lower() in _NET
    ) or (isinstance(node, ast.Name) and node.id in ("NADO_TESTNET", "NADO_MAINNET"))


def _is_net_collection(node: ast.AST) -> bool:
    return isinstance(node, (ast.Tuple, ast.List, ast.Set)) and any(_is_net_literal(e) for e in node.elts)


def _is_helper_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "coerce_nado_network"


def _is_network_compare(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        if any(_is_helper_call(o) for o in operands):
            return False  # comparing the helper's canonical output is fine
        eq = any(isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)) for op in node.ops)
        member = any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
        return (eq and any(_is_net_literal(o) for o in operands)) or (
            member and any(_is_net_collection(o) for o in operands)
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("startswith", "endswith")
        and node.args
        and _is_net_literal(node.args[0])
    ):
        return True
    return False


# (file, stripped line) -> count. Each entry is a DISPLAY label, a strict
# validator, or a policy check on an already-canonical value — none of them
# coerces an unknown value into a Nado network.
ALLOWED_NETWORK_COMPARES: dict[tuple[str, str], int] = {
    # --- UI labels (display only; routing uses fixed callback data) ---
    ("src/nadobro/handlers/callbacks.py",
     'network_label = "🧪 TESTNET" if current_network == "testnet" else "🌐 MAINNET"'): 3,
    ("src/nadobro/handlers/callbacks.py",
     'network_label = "🧪 TESTNET" if target_network == "testnet" else "🌐 MAINNET"'): 1,
    ("src/nadobro/handlers/copy_handler.py",
     'net_tag = "🧪 TESTNET" if str(m.get("network", "mainnet")).lower() == "testnet" else "🌐 MAINNET"'): 1,
    ("src/nadobro/handlers/formatters.py", 'net_emoji = "🧪" if net == "testnet" else "🌐"'): 1,
    ("src/nadobro/handlers/formatters.py",
     'network_label = "🧪 TESTNET" if current_network == "testnet" else "🌐 MAINNET"'): 1,
    ("src/nadobro/handlers/formatters.py", 'net_emoji = "🧪" if network == "testnet" else "🌐"'): 1,
    ("src/nadobro/handlers/formatters.py", 'net_name = "TESTNET" if network == "testnet" else "MAINNET"'): 1,
    ("src/nadobro/handlers/keyboards.py",
     'testnet_label = "🧪 Testnet ✅" if current_network == "testnet" else "🧪 Testnet"'): 1,
    ("src/nadobro/handlers/keyboards.py",
     'mainnet_label = "🌐 Mainnet ✅" if current_network == "mainnet" else "🌐 Mainnet"'): 1,
    ("src/nadobro/handlers/strategy_handler.py",
     'network_label = "Testnet" if str(network).lower() == "testnet" else "Mainnet"'): 1,
    # --- strict validators (reject anything that is not exactly a Nado network) ---
    ("src/nadobro/handlers/callbacks.py", 'if target_network not in ("testnet", "mainnet"):'): 1,
    ("src/nadobro/handlers/wallet_handler.py", 'if net not in ("testnet", "mainnet"):'): 1,
    ("src/nadobro/venue/nado_sync.py", 'if normalized not in {"mainnet", "testnet"}:'): 1,
    # --- policy check on the enum-canonical UserRow value ---
    ("src/nadobro/users/user_service.py", 'if user.network_mode.value == "mainnet":'): 1,
    # --- maps a TABLE NAME (only ever trades_testnet/trades_mainnet, from the
    # ledger helper) back to its network; reviewed in SPEC-A §2 ---
    ("src/nadobro/models/database.py",
     'network = "testnet" if str(table).lower().endswith("testnet") else "mainnet"'): 1,
}


def _network_compares() -> collections.Counter:
    found: collections.Counter = collections.Counter()
    for path in SCAN:
        if path == HELPER:
            continue
        nodes, lines = _nodes(path)
        for node in nodes:
            if _is_network_compare(node):
                found[(_rel(path), lines[node.lineno - 1].strip())] += 1
    return found


def test_no_new_network_coercion_compares():
    found = _network_compares()
    new = {k: n for k, n in found.items() if n > ALLOWED_NETWORK_COMPARES.get(k, 0)}
    assert not new, (
        "new network-literal compare(s) outside utils/venue_scope.py — a local "
        "ternary silently folds 'arcus_mainnet' into a Nado network. Use "
        "coerce_nado_network(value, <the site's legacy policy>, site=...) instead "
        "(or, for a pure display label, add it to ALLOWED_NETWORK_COMPARES):\n  "
        + "\n  ".join(f"{f}: {line}  (x{n})" for (f, line), n in sorted(new.items()))
    )


def test_network_compare_allowlist_has_no_stale_entries():
    found = _network_compares()
    stale = {k: n for k, n in ALLOWED_NETWORK_COMPARES.items() if found.get(k, 0) != n}
    assert not stale, "ALLOWED_NETWORK_COMPARES is stale (shrink it):\n  " + "\n  ".join(
        f"{f}: {line} expected x{n}, found x{found.get((f, line), 0)}" for (f, line), n in sorted(stale.items())
    )


def test_detector_flags_the_legacy_ternary_shapes():
    # Self-test: the shapes the P1 swap removed must all be caught.
    samples = [
        'x = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"',
        'x = NADO_MAINNET_REST if self.network == "mainnet" else NADO_TESTNET_REST',
        'x = f"trades_{network}" if network in ("testnet", "mainnet") else "trades_mainnet"',
        'x = "prod" if str(network) == "mainnet" else "test"',
        'x = 11 if network == "mainnet" else 1',
        "if isinstance(n, str) and n.strip().lower() == 'testnet':\n    pass",
        'x = "t" if t.endswith("testnet") else "m"',
        'x = "t" if net == NADO_TESTNET else "m"',
    ]
    for src in samples:
        assert any(_is_network_compare(n) for n in ast.walk(ast.parse(src))), src
    ok = 'if coerce_nado_network(v, LOWER_ELSE_MAINNET, site="s") == "testnet":\n    pass'
    assert not any(_is_network_compare(n) for n in ast.walk(ast.parse(ok)))


# ---------------------------------------------------------------------------
# 2. LIKE enumerators + Arcus key disjointness
# ---------------------------------------------------------------------------

_LIKE_RE = re.compile(r"\bLIKE\b")  # \b excludes ILIKE

# Per-file count of SQL string constants that enumerate bot_state / order_intents
# by LIKE. A new enumerator must be reviewed for Arcus-key safety, then pinned.
PINNED_LIKE_SITES = {
    "src/nadobro/strategy/bot_runtime.py": 4,       # boot stand-down, stop_all, status, restore
    "src/nadobro/runtime/scheduler.py": 3,          # tick_howl, tick_night_howl, tick_sltp_safety
    "src/nadobro/trading/stop_loss_service.py": 2,  # list rules, process rules
    "src/nadobro/strategy/pending_cleanup.py": 1,   # _BotStateStore.scan
    "src/nadobro/models/database.py": 1,            # order_intents intent_id (get_bot_linked_digests)
}


def _like_sites() -> dict[str, int]:
    out: collections.Counter = collections.Counter()
    for path in SCAN:
        nodes, _ = _nodes(path)
        for node in nodes:
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _LIKE_RE.search(node.value)
                and ("bot_state" in node.value or "intent_id" in node.value)
            ):
                out[_rel(path)] += 1
    return dict(out)


def test_like_enumerator_sites_are_pinned():
    assert _like_sites() == PINNED_LIKE_SITES


def _like_to_regex(pattern: str) -> re.Pattern:
    """Postgres LIKE (default escape) → an anchored full-match regex."""
    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out), re.DOTALL)


def _nado_like_patterns() -> list[str]:
    from src.nadobro.strategy import bot_runtime, pending_cleanup
    from src.nadobro.trading import stop_loss_service

    pats: list[str] = []
    for uid in (1, 123456789):
        for net in ("testnet", "mainnet", "%", "_"):
            pats += [
                f"{bot_runtime.STATE_PREFIX}%",
                f"{bot_runtime.STATE_PREFIX}{uid}:%",
                "strategy_bot:%",  # runtime/scheduler.py literal
                f"{stop_loss_service._SL_KEY_PREFIX}%",
                f"{stop_loss_service._SL_KEY_PREFIX}{uid}:{net}:%",
                pending_cleanup._prefix(uid, net) + "%",
                f"engine:{net}:%",
                f"close:{net}:%",
            ]
    return pats


def _arcus_keys() -> list[str]:
    from src.nadobro.utils.venue_scope import (
        ARCUS_SCOPES,
        ARCUS_STRATEGY_BOT_PREFIX,
        ARCUS_USER_SETTINGS_PREFIX,
    )

    keys = []
    for uid in (1, 123456789):
        for scope in ARCUS_SCOPES:
            keys += [
                f"{ARCUS_STRATEGY_BOT_PREFIX}{uid}:{scope}",
                f"{ARCUS_USER_SETTINGS_PREFIX}{uid}:{scope}",
            ]
    return keys


def test_scheduler_literal_matches_the_bot_runtime_prefix():
    from src.nadobro.strategy import bot_runtime

    assert bot_runtime.STATE_PREFIX + "%" == "strategy_bot:%"


def test_arcus_keys_never_match_any_nado_like_pattern():
    patterns = _nado_like_patterns()
    for key in _arcus_keys():
        for pat in patterns:
            assert not _like_to_regex(pat).fullmatch(key), (key, pat)


def test_nado_like_patterns_start_with_a_literal_that_is_not_arcus():
    # Structural reason the above holds: LIKE is anchored, every Nado pattern
    # starts with a literal (non-wildcard) character, and none starts with "a".
    for pat in _nado_like_patterns():
        assert pat[0] not in "%_", pat
        assert pat[0] != "a", pat


def test_like_translation_self_test():
    assert _like_to_regex("strategy_bot:%").fullmatch("strategyXbot:1:mainnet")  # "_" wildcard
    assert not _like_to_regex("strategy_bot:%").fullmatch("arcus_strategy_bot:1:arcus_mainnet")
    assert _like_to_regex("a_c%").fullmatch("abcdef")


def test_arcus_prefixes_are_disjoint_from_nado_exact_key_prefixes():
    from src.nadobro.strategy import bot_runtime, pending_cleanup
    from src.nadobro.trading import stop_loss_service
    from src.nadobro.users import settings_service
    from src.nadobro.utils.venue_scope import ARCUS_STRATEGY_BOT_PREFIX, ARCUS_USER_SETTINGS_PREFIX

    nado = [
        bot_runtime.STATE_PREFIX,
        settings_service.SETTINGS_PREFIX,
        stop_loss_service._SL_KEY_PREFIX,
        pending_cleanup.PREFIX,
    ]
    for arcus in (ARCUS_STRATEGY_BOT_PREFIX, ARCUS_USER_SETTINGS_PREFIX):
        for prefix in nado:
            assert not arcus.startswith(prefix) and not prefix.startswith(arcus), (arcus, prefix)
    # A settings key for a Nado user can never be read as an Arcus one either.
    assert not settings_service._settings_key(1, "mainnet").startswith(ARCUS_USER_SETTINGS_PREFIX)
    assert not bot_runtime._state_key(1, "mainnet").startswith(ARCUS_STRATEGY_BOT_PREFIX)


# ---------------------------------------------------------------------------
# 3. network-less client fetches (pinned)
# ---------------------------------------------------------------------------

PINNED_NETWORKLESS_CLIENT_FETCHES = {
    "src/nadobro/handlers/home_card.py": 1,
    "src/nadobro/handlers/intent_handlers.py": 2,
    "src/nadobro/handlers/messages.py": 2,
    "src/nadobro/handlers/strategy_handler.py": 5,
    "src/nadobro/handlers/trade_card.py": 1,
    "src/nadobro/trading/budget_guard.py": 1,
    "src/nadobro/trading/trade_service.py": 2,
    "src/nadobro/users/onboarding_service.py": 1,
    "src/nadobro/users/user_service.py": 1,
    "src/nadobro/vault/nlp_vault_service.py": 3,
    "src/nadobro/venue/nado_tooling_service.py": 2,
}


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)


def test_networkless_client_fetches_are_pinned():
    found: collections.Counter = collections.Counter()
    for path in SCAN:
        nodes, _ = _nodes(path)
        for node in nodes:
            if isinstance(node, ast.Call) and _call_name(node) in (
                "get_user_nado_client",
                "get_user_readonly_client",
            ):
                passes_network = (
                    len(node.args) >= 2
                    or any(k.arg in ("network", None) for k in node.keywords)
                )
                if not passes_network:
                    found[_rel(path)] += 1
    assert dict(found) == PINNED_NETWORKLESS_CLIENT_FETCHES, (
        "network-less client fetch set changed. These resolve to the user's NADO "
        "network even while the user is viewing Arcus; pass an explicit network "
        "or confirm the path is Nado-only (gated), then update the pin."
    )


# ---------------------------------------------------------------------------
# 4. strategy-session readers are always network-scoped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fn, positional_network_index",
    [("get_strategy_sessions_by_user", 2), ("get_running_strategy_sessions", 1)],
)
def test_strategy_session_readers_always_pass_a_network(fn, positional_network_index):
    offenders = []
    calls = 0
    for path in SCAN:
        nodes, lines = _nodes(path)
        for node in nodes:
            if isinstance(node, ast.Call) and _call_name(node) == fn:
                calls += 1
                ok = len(node.args) > positional_network_index or any(
                    k.arg == "network" for k in node.keywords
                )
                if not ok:
                    offenders.append(f"{_rel(path)}:{node.lineno}: {lines[node.lineno - 1].strip()}")
    assert calls, f"no {fn} callers found — update this guard"
    assert not offenders, (
        f"{fn} called without a network: it would return sessions from every "
        "network/venue (Arcus sessions carry scope tokens in strategy_sessions."
        "network).\n  " + "\n  ".join(offenders)
    )
