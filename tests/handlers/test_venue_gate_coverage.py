"""Coverage pins for the venue capability table (Arcus P1, utils/venue_capabilities.py).

The gate is only as good as its table: an update it cannot classify is UNKNOWN,
and UNKNOWN is denied on the Arcus view. These tests make a new route fail CI
until it is classified on purpose:

* every ``callback_data`` the bot emits (AST scan of src/nadobro + the
  dynamically built families) classifies to a known class;
* the ``handle_callback`` router chain, ``_handle_nav``'s targets, the alias map,
  every ``action ==`` branch of the sub-routers, the registered commands and
  the reply keyboard are pinned against the table — a new branch fails here;
* the NEVER_GATE set (the Nado stop paths reachable from the Arcus view) is
  pinned exactly, and no keyword rule can smuggle a config tap into it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.utils import venue_capabilities as vc  # noqa: E402
from src.nadobro.utils.venue_capabilities import (  # noqa: E402
    ARCUS_ONLY,
    AX_HOME,
    AX_MODE,
    AX_SETTINGS,
    AX_UNAVAILABLE,
    AX_UNLINK,
    AX_WALLET,
    DISPATCH,
    NADO_ONLY,
    NEUTRAL,
    NEVER_GATE,
    UNKNOWN,
    classify_callback,
    classify_command,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nadobro"
HANDLERS = SRC / "handlers"


def _cls(data):
    return classify_callback(data)[0]


# ---------------------------------------------------------------------------
# every emitted callback_data is classified
# ---------------------------------------------------------------------------

_FILL = "1"  # an f-string placeholder: numeric ids, product names and hex digests alike


def _literal_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(v.value) if isinstance(v, ast.Constant) else _FILL for v in node.values
        )
    return None


def _emitted_callback_data() -> dict[str, list[str]]:
    """{callback_data: [file:line, ...]} for every literal / f-string passed as
    ``callback_data=`` (or as the 2nd positional arg of InlineKeyboardButton)."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            candidates = [kw.value for kw in node.keywords if kw.arg == "callback_data"]
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname == "InlineKeyboardButton" and len(node.args) >= 2:
                candidates.append(node.args[1])
            for value in candidates:
                # ``"a" if cond else "b"`` — both branches are emitted.
                branches = [value.body, value.orelse] if isinstance(value, ast.IfExp) else [value]
                for branch in branches:
                    lit = _literal_value(branch)
                    if lit is not None:
                        found.setdefault(lit, []).append(f"{path.relative_to(REPO)}:{node.lineno}")
    return found


def test_scan_sees_the_whole_callback_surface():
    # Sanity: the scan is not silently empty (≈420 values across ~24 files today).
    assert len(_emitted_callback_data()) > 300


def test_every_emitted_callback_data_is_classified():
    unknown = {
        data: locs for data, locs in _emitted_callback_data().items() if _cls(data) == UNKNOWN
    }
    assert not unknown, (
        "callback_data the venue gate cannot classify (UNKNOWN = denied on the Arcus view). "
        "Classify it in src/nadobro/utils/venue_capabilities.py:\n  "
        + "\n  ".join(f"{d!r} at {locs[0]}" for d, locs in sorted(unknown.items()))
    )


# Families built at runtime (not literals), one representative each.
DYNAMIC_FAMILIES = {
    # keyboards.trade_card_cb(session_id, action[, value])
    "card:trade:ab12cd34:home": NADO_ONLY,
    "card:trade:ab12cd34:cancel": NADO_ONLY,
    "card:trade:ab12cd34:confirm": NADO_ONLY,
    "card:trade:ab12cd34:direction:long": NADO_ONLY,
    "card:trade:ab12cd34:lev:10": NADO_ONLY,
    "card:trade:ab12cd34:size:0.05": NADO_ONLY,
    "card:trade:ab12cd34:tpsl:skip": NADO_ONLY,
    # orders_view.cancel_callback_for (digest form + legacy index form)
    "portfolio:cancel_order:d:0a1b2c3d4e5f6071": NEVER_GATE,
    "portfolio:cancel_order:4": NEVER_GATE,
    # history_view.window_share_callback / performance_view / copy_service / trade_service
    "portfolio:share_pnl:vp:7d": NADO_ONLY,
    "portfolio:share_pnl:rt:7d": NADO_ONLY,
    "portfolio:share_pnl:copy:3": NADO_ONLY,
    "portfolio:share_pnl:42": NADO_ONLY,
    # keyboards.back_kb(target)
    "nav:main": DISPATCH,
    # messages.continue_callback
    "strategy:config:grid": NADO_ONLY,
    "strategy:config_section:grid:risk": NADO_ONLY,
    "strategy:preview:bro": NADO_ONLY,
    "bro:config_section:risk": NADO_ONLY,
    # vault_handler watch_cb
    "vault:watch:on": NADO_ONLY,
    "vault:watch:off": NEVER_GATE,
    # typed-intent targets (intent_parser -> messages.py)
    "alert:menu": NADO_ONLY,
    "nav:mode": DISPATCH,
    "nav:strategy_hub": DISPATCH,
    "nav:trade": NADO_ONLY,
    "points:view": NADO_ONLY,
    "portfolio:view": DISPATCH,
    "pos:view": DISPATCH,
    "settings:view": DISPATCH,
    "wallet:view": DISPATCH,
    # boot stand-down prompt (copy_service) and vault watch alerts
    "copy:resume:7": NADO_ONLY,
    "copy:stop:7": NEVER_GATE,
    "vault:deposit": NADO_ONLY,
    "vault:home": NADO_ONLY,
}


@pytest.mark.parametrize("data,expected", sorted(DYNAMIC_FAMILIES.items()))
def test_dynamic_callback_families(data, expected):
    assert _cls(data) == expected


def test_venue_handler_keyboards_are_classified():
    from src.nadobro.handlers import venue_handler as vh

    expected = {
        "venue:view": NEUTRAL, "venue:set:nado": NEUTRAL, "venue:set:arcus": NEUTRAL,
        "ax:home": ARCUS_ONLY, "ax:help": ARCUS_ONLY,
        "nav:main": DISPATCH, "settings:language_menu": NEUTRAL,
        # the Nado stop entries on the Arcus home
        "portfolio:close_all_confirm": NEVER_GATE, "portfolio:cancel_all_confirm": NEVER_GATE,
        "desk:view": NEVER_GATE,
    }
    seen = set()
    for kb in (
        vh.venue_card_kb("nado"), vh.venue_card_kb("arcus"), vh.arcus_home_kb(), vh.arcus_home_kb([], True),
        vh.arcus_help_kb(), vh.arcus_settings_kb(), vh.arcus_unavailable_kb(), vh.arcus_free_text_kb(),
    ):
        for row in kb.inline_keyboard:
            for btn in row:
                seen.add(btn.callback_data)
                assert _cls(btn.callback_data) == expected[btn.callback_data], btn.callback_data
    assert seen == set(expected)


def _buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_every_live_nado_automation_on_the_arcus_banner_has_a_stop_entry_from_the_arcus_view():
    """BC1-STOP-ENTRY-UNREACHABLE: NEVER_GATE only helps if the Arcus view can
    REACH the stop. Each automation the Arcus home lists maps to a NEVER_GATE
    command, or to a NEVER_GATE button the home shows while it is listed."""
    from src.nadobro.handlers import venue_handler as vh

    by_command = {
        vh.TEXT_ITEM_STRATEGY: "stop_all",   # stop_all_automation_for_user: strategy loops...
        vh.TEXT_ITEM_COPY: "stop_all",       # ...and copy mirrors
        vh.TEXT_ITEM_MANAGED_AI: "agent_off",
    }
    by_button = {vh.TEXT_ITEM_DESK: "desk:view"}  # /stop_all does NOT stop desk plans
    for key, name in by_command.items():
        assert classify_command(name) == (NEVER_GATE, None), name
    for key, data in by_button.items():
        assert data in _buttons(vh.arcus_home_kb([(key, {"n": "1"})])), key
        assert _cls(data) == NEVER_GATE
    assert classify_command("desk") == (NEVER_GATE, None)
    # Close / cancel: shown whenever the banner is, each a confirm screen first.
    for items, failed in (([(vh.TEXT_ITEM_COPY, {"n": "1"})], False), ([], True)):
        buttons = _buttons(vh.arcus_home_kb(items, failed))
        assert {"portfolio:close_all_confirm", "portfolio:cancel_all_confirm"} <= set(buttons)
    # Nothing listed: the placeholder home stays [Venue] [Help].
    assert _buttons(vh.arcus_home_kb()) == ["venue:view", "ax:help"]


def test_the_stop_entry_screens_only_lead_to_stops_or_views():
    """The desk list's only actions are Stop + Refresh (so it can be NEVER_GATE);
    each confirm screen's Yes is NEVER_GATE and its way back is a DISPATCH view."""
    from src.nadobro.handlers.desk_handler import _desk_view_kb
    from src.nadobro.handlers.orders_view import render_cancel_all_confirm
    from src.nadobro.handlers.portfolio_deck import render_close_all_confirm

    plan = type("Plan", (), {"plan_id": "0123456789abcdef", "product": "BTC", "algo": "twap"})()
    for data in _buttons(_desk_view_kb([{"plan": plan}])):
        assert _cls(data) == NEVER_GATE, data
    for render in (render_close_all_confirm, render_cancel_all_confirm):
        classes = sorted(_cls(d) for d in _buttons(render()[1]))
        assert classes == sorted([NEVER_GATE, DISPATCH]), (render.__name__, classes)


# ---------------------------------------------------------------------------
# the router, nav and aliases are pinned against the table
# ---------------------------------------------------------------------------

def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _compares_on(fn: ast.AST, var: str) -> tuple[set[str], set[str]]:
    """(exact values compared with ==/!=/in, startswith prefixes) for ``var``."""
    exact: set[str] = set()
    prefixes: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == var:
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Eq, ast.NotEq)) and isinstance(comp, ast.Constant):
                    exact.add(comp.value)
                elif isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                    exact.update(e.value for e in comp.elts if isinstance(e, ast.Constant))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == var
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant):
                    prefixes.add(arg.value)
    return exact, prefixes


def test_router_prefixes_match_the_table():
    exact, prefixes = _compares_on(_function(HANDLERS / "callbacks.py", "_handle_callback_inner"), "data")
    # Every routed prefix is NADO_ONLY except resources: (NEUTRAL) and nav:
    # (classified recursively); trade_flow: is a reply-keyboard target only.
    assert prefixes == (set(vc.NADO_ONLY_PREFIXES) - {"trade_flow:"}) | {"resources:", "nav:"}, (
        "handle_callback routes a prefix the venue table does not know (or no longer routes one it lists)"
    )
    assert exact == {"cancel_trade", "home:mode"}
    assert _cls("cancel_trade") == NEVER_GATE
    # Arcus P3b (03 §11.3/§20): the Nado mode card dispatches to the Arcus network card.
    assert classify_callback("home:mode") == (DISPATCH, AX_MODE)


def test_callback_aliases_match_handle_callback():
    exact, _ = _compares_on(_function(HANDLERS / "callbacks.py", "handle_callback"), "data")
    assert exact == set(vc.CALLBACK_ALIASES)
    assert set(vc.CALLBACK_ALIASES.values()) == {"points:view"}
    for alias in vc.CALLBACK_ALIASES:
        assert _cls(alias) == NADO_ONLY  # points:view


def test_nav_targets_match_handle_nav_exactly():
    exact, prefixes = _compares_on(_function(HANDLERS / "callbacks.py", "_handle_nav"), "target")
    assert exact == set(vc.NAV_EXACT)
    assert prefixes == set(vc.NAV_FORWARDED_PREFIXES)


# ---------------------------------------------------------------------------
# every sub-router action has an explicit expected class
# ---------------------------------------------------------------------------

# module path -> (function or None for "whole module", {action: {sample callback: class}})
ACTIONS = {
    ("portfolio_handler.py", None): {
        "view": {"portfolio:view": DISPATCH, "portfolio:view:7d": DISPATCH},
        "refresh": {"portfolio:refresh": DISPATCH, "portfolio:refresh:7d": DISPATCH},
        "positions": {"portfolio:positions": DISPATCH, "portfolio:positions:1": DISPATCH,
                      "portfolio:positions:pos:1": DISPATCH, "portfolio:positions:ord:2": DISPATCH},
        "orders": {"portfolio:orders": DISPATCH, "portfolio:orders:1": DISPATCH},
        "history": {"portfolio:history": DISPATCH, "portfolio:history:2": DISPATCH},
        "session_trades": {"portfolio:session_trades:12:0": DISPATCH},
        "hours": {"portfolio:hours": DISPATCH},
        "analytics": {"portfolio:analytics": DISPATCH},
        "performance": {"portfolio:performance": DISPATCH, "portfolio:performance:1": DISPATCH},
        "share_pnl": {"portfolio:share_pnl:12": NADO_ONLY},
        "close_all_confirm": {"portfolio:close_all_confirm": NEVER_GATE},
        "close_all_yes": {"portfolio:close_all_yes": NEVER_GATE},
        "cancel_all_confirm": {"portfolio:cancel_all_confirm": NEVER_GATE},
        "cancel_all_yes": {"portfolio:cancel_all_yes": NEVER_GATE},
        "cancel_order": {"portfolio:cancel_order:d:abc123": NEVER_GATE, "portfolio:cancel_order:0": NEVER_GATE},
    },
    ("settings_handler.py", None): {
        "view": {"settings:view": DISPATCH},
        "leverage_menu": {"settings:leverage_menu": NADO_ONLY},
        "leverage": {"settings:leverage:5": NADO_ONLY},
        "risk_menu": {"settings:risk_menu": NADO_ONLY},
        "risk": {"settings:risk:balanced": NADO_ONLY},
        "slippage_menu": {"settings:slippage_menu": NADO_ONLY},
        "slippage": {"settings:slippage:1": NADO_ONLY},
        "language_menu": {"settings:language_menu": NEUTRAL},
        "language": {"settings:language:ko": NEUTRAL},
    },
    ("wallet_handler.py", None): {
        "view": {"wallet:view": DISPATCH},
        "balance": {"wallet:balance": NADO_ONLY},
        "revoke_steps": {"wallet:revoke_steps": NEVER_GATE},
        "revoke_confirm": {"wallet:revoke_confirm": NEVER_GATE},
        "remove_active": {"wallet:remove_active": NEVER_GATE},
        "network": {"wallet:network:mainnet": NADO_ONLY},
    },
    ("strategy_handler.py", "_handle_strategy"): {
        **{a: {f"strategy:{a}:grid": NADO_ONLY} for a in (
            "volmarket", "preview", "custom", "pair", "bias", "funding", "config", "config_section",
            "preset", "set", "set_text", "input", "activate", "start", "startok",
        )},
        "status": {"strategy:status": DISPATCH},
        "stop": {"strategy:stop": NEVER_GATE},
    },
    ("copy_handler.py", None): {
        **{a: {f"copy:{a}": NADO_ONLY} for a in (
            "hub", "clear", "lb", "trader", "start", "budget", "risk", "lev", "csl", "ctp", "confirm",
            "resume", "dashboard", "add_custom", "admin",
        )},
        "pause": {"copy:pause:3": NEVER_GATE},
        "stop": {"copy:stop:3": NEVER_GATE},
    },
    ("desk_handler.py", "handle_desk_callback"): {
        "view": {"desk:view": NEVER_GATE},  # the read-only list: the only entry to desk:stop
        "confirm": {"desk:confirm:0123456789abcdef": NADO_ONLY},
        "discard": {"desk:discard:0123456789abcdef": NEVER_GATE},
        "stop": {"desk:stop:0123456789abcdef": NEVER_GATE},
    },
    ("alerts_handler.py", None): {
        **{a: {f"alert:{a}": NADO_ONLY} for a in ("menu", "set", "product", "cond", "view")},
        "del": {"alert:del:17": NEVER_GATE},
    },
    ("vault_handler.py", "handle_vault_callback"): {
        "home": {"vault:home": NADO_ONLY},
        "refresh": {"vault:refresh": NADO_ONLY},
        "deposit": {"vault:deposit": NADO_ONLY, "vault:deposit:confirm:100": NADO_ONLY},
        "withdraw": {"vault:withdraw": NADO_ONLY, "vault:withdraw:confirm:50": NADO_ONLY},
        "watch": {"vault:watch:off": NEVER_GATE, "vault:watch:on": NADO_ONLY},
    },
    ("bro_handler.py", None): {
        a: {f"bro:{a}": NADO_ONLY} for a in (
            "config", "config_section", "explain", "gameplan", "howl", "profile", "risk", "set",
            "set_text", "status",
        )
    },
    ("callbacks.py", "_handle_positions"): {
        "view": {"pos:view": DISPATCH},
        "close": {"pos:close:BTC": NEVER_GATE},
        "close_all": {"pos:close_all": NEVER_GATE},
        "confirm_close_all": {"pos:confirm_close_all": NEVER_GATE},
    },
    ("callbacks.py", "_handle_status_callback"): {
        "stop": {"status:stop": NEVER_GATE},
        "refresh": {"status:refresh": DISPATCH},
    },
    ("callbacks.py", "_handle_points"): {
        "view": {"points:view": NADO_ONLY},
        "scope": {"points:scope:week": NADO_ONLY},
        "cancel": {"points:cancel": NEVER_GATE},
        "replyopt": {"points:replyopt:1": NADO_ONLY},
        "refresh": {"points:refresh": NADO_ONLY},
    },
    ("callbacks.py", "_handle_howl"): {
        "approve": {"howl:approve:0": NADO_ONLY},
        "reject": {"howl:reject:0": NEVER_GATE},
        "approve_all": {"howl:approve_all": NADO_ONLY},
        "dismiss": {"howl:dismiss": NEVER_GATE},
    },
    ("callbacks.py", "_handle_trade"): {
        "long": {"trade:long": NADO_ONLY},
        "short": {"trade:short": NADO_ONLY},
        "limit_long": {"trade:limit_long": NADO_ONLY},
        "limit_short": {"trade:limit_short": NADO_ONLY},
        "close": {"trade:close": NEVER_GATE},
        "close_all": {"trade:close_all": NEVER_GATE},
    },
    ("callbacks.py", "_handle_mm_dashboard"): {
        "status": {"mm:status": DISPATCH, "mm:status:refresh": DISPATCH},
        "fills": {"mm:fills": DISPATCH},
    },
    ("callbacks.py", "_handle_referrals"): {
        "claim": {"refer:claim": NADO_ONLY},
        "autogen": {"refer:autogen": NADO_ONLY},
    },
}


@pytest.mark.parametrize("module,func", sorted(ACTIONS, key=lambda k: (k[0], k[1] or "")))
def test_every_sub_router_action_has_an_expected_class(module, func):
    path = HANDLERS / module
    node = _function(path, func) if func else ast.parse(path.read_text(encoding="utf-8"))
    found, _ = _compares_on(node, "action")
    expected = ACTIONS[(module, func)]
    assert found == set(expected), (
        f"{module}{'::' + func if func else ''} action set changed — classify the new branch in "
        f"venue_capabilities.py and add it here. new={sorted(found - set(expected))} "
        f"gone={sorted(set(expected) - found)}"
    )
    for action, samples in expected.items():
        for data, cls in samples.items():
            assert _cls(data) == cls, (action, data)


# ---------------------------------------------------------------------------
# commands + reply keyboard
# ---------------------------------------------------------------------------

def _main_registered_commands() -> set[str]:
    fn = _function(REPO / "main.py", "setup_bot")
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "CommandHandler":
            first = node.args[0]
            assert isinstance(first, ast.Constant), "CommandHandler name must be a literal"
            names.add(first.value)
    return names


def test_registered_commands_match_the_table():
    assert _main_registered_commands() | {"venue"} == set(vc.COMMANDS)
    assert "venue" not in _main_registered_commands()  # registered by venue_gate, gated


def test_command_classes_are_pinned():
    # Updated deliberately for Arcus P3b (03 D-11/§19.11): /revoke moves from NEVER_GATE to
    # DISPATCH -> the Arcus unlink card, which carries the NEVER_GATE wallet:revoke_steps
    # button (the Nado 1CT revoke path stays reachable from the Arcus view). Nado-view users
    # pass DISPATCH untouched.
    assert {n for n, (c, _) in vc.COMMANDS.items() if c == NEVER_GATE} == {"stop_all", "agent_off", "desk"}
    assert {n for n, (c, _) in vc.COMMANDS.items() if c == NEUTRAL} == {"help", "ops", "venue"}
    assert {n: t for n, (c, t) in vc.COMMANDS.items() if c == DISPATCH} == {
        "start": AX_HOME, "status": AX_HOME, "mm_status": AX_UNAVAILABLE, "mm_fills": AX_UNAVAILABLE,
        "revoke": AX_UNLINK,
    }
    assert classify_command("STOP_ALL") == (NEVER_GATE, None)
    assert classify_command("not_a_command") == (UNKNOWN, None)


def test_every_reply_button_target_is_classified():
    from src.nadobro.handlers.keyboards import REPLY_BUTTON_MAP

    unknown = {label: t for label, t in REPLY_BUTTON_MAP.items() if _cls(t) == UNKNOWN}
    assert not unknown, unknown
    dispatch = {t: classify_callback(t)[1] for t in set(REPLY_BUTTON_MAP.values()) if _cls(t) == DISPATCH}
    assert dispatch == {
        "nav:main": AX_HOME,
        "settings:view": AX_SETTINGS,
        "portfolio:view": AX_UNAVAILABLE,
        "pos:view": AX_UNAVAILABLE,
        "wallet:view": AX_WALLET,  # Arcus P3b (03 §11.3/§20)
        "nav:mode": AX_MODE,  # Arcus P3b (03 §11.3/§20)
        "nav:strategy_hub": AX_UNAVAILABLE,
    }
    # Everything else on the reply keyboard (trade flow, products, points,
    # referrals, alerts) is Nado-only.
    for target in set(REPLY_BUTTON_MAP.values()) - set(dispatch):
        assert _cls(target) == NADO_ONLY, target


# ---------------------------------------------------------------------------
# NEVER_GATE is pinned exactly
# ---------------------------------------------------------------------------

def test_never_gate_set_is_pinned():
    assert vc.NEVER_GATE_EXACT == frozenset({
        "strategy:stop", "status:stop", "pos:close_all", "pos:confirm_close_all",
        "portfolio:close_all_confirm", "portfolio:close_all_yes", "portfolio:cancel_all_confirm",
        "portfolio:cancel_all_yes", "wallet:revoke_steps", "wallet:revoke_confirm",
        "wallet:remove_active", "cancel_trade", "points:cancel", "vault:watch:off", "howl:dismiss",
        "trade:close", "trade:close_all", "desk:view",
    })
    assert [p.pattern for p in vc.NEVER_GATE_PATTERNS] == [
        r"^copy:stop:\d+$",
        r"^copy:pause:\d+$",
        r"^desk:stop:[^:\s]+$",
        r"^desk:discard:[^:\s]+$",
        r"^pos:close:[^:\s]+$",
        r"^portfolio:cancel_order:(?:d:[0-9a-f]+|\d+)$",
        r"^alert:del:\d+$",
        r"^howl:reject:\d+$",
    ]
    for pat in vc.NEVER_GATE_PATTERNS:
        assert pat.pattern.startswith("^") and pat.pattern.endswith("$"), pat.pattern


def test_every_stop_like_emitted_callback_is_never_gate_or_reviewed():
    """A keyword sweep of emitted callbacks: anything that LOOKS like a stop /
    close / cancel / remove must be NEVER_GATE, or be on this reviewed list of
    look-alikes that open exposure, only show a screen, or change a setting."""
    reviewed_lookalikes = {
        # strategy config knobs whose NAME mentions stop / pause / close — a
        # setting for the next run, not an action on a live one
        re.compile(r"^strategy:(set|input):[a-z]+:rgrid_stop(_loss)?_pct(:[0-9.]+)?$"),
        re.compile(r"^strategy:(set|input):[a-z]+:twap_pause_move_bp(:[0-9.]+)?$"),
        re.compile(r"^strategy:set:dn:auto_close_on_maintenance:[01]$"),
        # forgets dormant saved trader selections (active mirrors untouched)
        re.compile(r"^copy:clear(:confirm)?$"),
        # ADMIN pool management (removes a curated trader for everyone) — not
        # the user's own stop path; a user stops their mirror with copy:stop:<id>
        re.compile(r"^copy:admin:remove:\d+$"),
        # the trade-card wizard (its cancel only drops the unsent card)
        re.compile(r"^card:trade:.*$"),
    }
    stopish = re.compile(r"(stop|close|cancel|remove|revoke|discard|del|dismiss|reject|pause)")
    offenders = []
    for data in _emitted_callback_data():
        if not stopish.search(data) or _cls(data) == NEVER_GATE:
            continue
        if any(p.match(data) for p in reviewed_lookalikes):
            continue
        offenders.append(data)
    assert not offenders, "stop-like callback not NEVER_GATE (classify or review it): " + ", ".join(sorted(offenders))


# ---------------------------------------------------------------------------
# spot checks, negatives, totality
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    # config taps that merely mention "stop" stay Nado-only (no keyword rules)
    ("strategy:set:rgrid:rgrid_stop_pct:0.5", NADO_ONLY),
    ("strategy:set:dgrid:rgrid_stop_loss_pct:1.0", NADO_ONLY),
    ("strategy:stop:grid", NADO_ONLY),
    ("copy:stop:x", NADO_ONLY),
    ("howl:approve_all", NADO_ONLY),
    ("vault:watch:on", NADO_ONLY),
    ("wallet:network:mainnet", NADO_ONLY),
    ("mode:mainnet", NADO_ONLY),
    ("nav:ask_nado", NADO_ONLY),
    # nav: forwards stop paths
    ("nav:strategy:stop", NEVER_GATE),
    ("nav:copy:stop:3", NEVER_GATE),
    ("nav:portfolio:cancel_all_yes", NEVER_GATE),
    ("portfolio:cancel_order:d:0a1b2c3d", NEVER_GATE),
    ("portfolio:cancel_order:3", NEVER_GATE),
    # neutral
    ("nav:settings:language:ko", NEUTRAL),
    ("nav:help", NEUTRAL),
    ("venue:anything", NEUTRAL),
    ("resources:home", NEUTRAL),
    ("onb:lang:fr", NEUTRAL),
    ("onb:accept_tos", NEUTRAL),
    # aliases go to points:view (Nado-only)
    ("nav:market_radar", NADO_ONLY),
    ("market:view", NADO_ONLY),
    ("home:market_radar", NADO_ONLY),
    # dispatch
    ("nav:quick_start", DISPATCH),
    ("onboarding:resume", DISPATCH),
    ("nav:portfolio:view", DISPATCH),
    # ax:* are Arcus-only
    ("ax:home", ARCUS_ONLY),
    ("ax:whatever", ARCUS_ONLY),
    # not routed by Nado today -> UNKNOWN (denied on Arcus)
    ("nav:pos:close:BTC", UNKNOWN),
    ("nav:status:stop", UNKNOWN),
    ("nav:desk:stop:abc", UNKNOWN),
    ("nav:nav:main", UNKNOWN),
    ("nav:", UNKNOWN),
    ("nav", UNKNOWN),
    ("home:foo", UNKNOWN),
    ("", UNKNOWN),
    ("garbage", UNKNOWN),
    ("venue", UNKNOWN),
])
def test_spot_checks(data, expected):
    assert _cls(data) == expected


def test_render_targets_only_for_dispatch():
    for data in ("strategy:stop", "venue:view", "ax:home", "trade:long", "garbage"):
        assert classify_callback(data)[1] is None
    targets = set(vc.DISPATCH_EXACT.values()) | {t for _, t in vc.DISPATCH_PATTERNS}
    targets |= {t for c, t in vc.NAV_EXACT.values() if c == DISPATCH}
    targets |= {t for c, t in vc.COMMANDS.values() if c == DISPATCH}
    # + the Arcus wallet / network / unlink cards (Arcus P3b, 03 §11.3/§20).
    assert targets == {AX_HOME, AX_SETTINGS, AX_UNAVAILABLE, AX_WALLET, AX_MODE, AX_UNLINK}


@pytest.mark.parametrize("raw", [None, "", " ", 0, 123, b"nav:main", object(), "::::", "nav:" * 50, "\x00"])
def test_classification_is_total(raw):
    cls, target = classify_callback(raw)
    assert cls in vc.CLASSES
    cls, target = classify_command(raw)
    assert cls in vc.CLASSES


def test_arcus_capabilities_are_wallet_only_in_p3b():
    # Renamed + updated deliberately for Arcus P3b (03 §11.3/§20): the Arcus wallet
    # (paste-key linking) is the only Arcus feature; no strategies yet. Later phases UNION.
    assert vc.VENUE_CAPABILITIES["arcus"] == {"strategies": frozenset(), "features": frozenset({"wallet"})}
    assert set(vc.VENUE_CAPABILITIES) == {"nado", "arcus"}
