"""Static wiring of the Arcus P1 venue gate in main.py (it cannot be imported in
tests: it exits without ENCRYPTION_KEY), plus the new i18n strings.

* group order: private-chat (-3) < language (-2) < venue gate (-1) < handlers (0);
* ``register_venue_handlers`` runs after every Nado CommandHandler and BEFORE
  the catch-all CallbackQueryHandler (PTB runs the first match per group);
* the decision is made once at boot, off the loop, after init_db, and the
  default is "not registered" (production byte-identical);
* the command menu is unchanged in Phase 1 (no /venue entry);
* every new user-facing string has all five translations, keeps its
  placeholders and HTML tags, and fits a callback answer where it is one.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro import i18n  # noqa: E402
from src.nadobro.handlers import venue_gate as vg  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402

MAIN = Path(__file__).resolve().parents[2] / "main.py"
_LANGS = {"zh", "fr", "ar", "ru", "ko"}


def _fn(name):
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _calls_in_order(fn):
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    return sorted(calls, key=lambda c: (c.lineno, c.col_offset))


def _add_handler_calls(fn):
    return [c for c in _calls_in_order(fn) if isinstance(c.func, ast.Attribute) and c.func.attr == "add_handler"]


def _group(call):
    for kw in call.keywords:
        if kw.arg == "group":
            return ast.literal_eval(kw.value)
    return 0


def test_group_order_private_language_gate_handlers():
    setup = _fn("setup_bot")
    groups = {}
    for call in _add_handler_calls(setup):
        src = ast.unparse(call.args[0])
        if "_private_chat_only" in src:
            groups["private"] = _group(call)
        elif "_language_middleware" in src:
            groups["language"] = _group(call)
    assert groups == {"private": -3, "language": -2}
    assert groups["language"] < vg.VENUE_GATE_GROUP < 0
    # Every other handler main.py registers is in group 0.
    others = [c for c in _add_handler_calls(setup)
              if not any(k in ast.unparse(c.args[0]) for k in ("_private_chat_only", "_language_middleware"))]
    assert others and all(_group(c) == 0 for c in others)


def test_venue_handlers_register_before_the_catch_all_and_after_the_commands():
    setup = _fn("setup_bot")
    calls = _calls_in_order(setup)
    reg = [c for c in calls if getattr(c.func, "id", None) == "register_venue_handlers"]
    assert len(reg) == 1
    assert ast.unparse(reg[0]) == "register_venue_handlers(app, enabled=venue_gate)"
    adds = _add_handler_calls(setup)
    catch_all = [c for c in adds if ast.unparse(c.args[0]).startswith("CallbackQueryHandler(")]
    assert len(catch_all) == 1
    # The catch-all itself is unchanged.
    assert ast.unparse(catch_all[0].args[0]) == (
        "CallbackQueryHandler(with_callback_ack(with_user_serialized(handle_callback)))"
    )
    commands = [c for c in adds if ast.unparse(c.args[0]).startswith("CommandHandler(")]
    assert commands and all(c.lineno < reg[0].lineno for c in commands)
    assert reg[0].lineno < catch_all[0].lineno


def test_setup_bot_defaults_to_no_gate():
    setup = _fn("setup_bot")
    args = setup.args
    assert [a.arg for a in args.args] == ["venue_gate"]
    assert ast.literal_eval(args.defaults[0]) is False


def test_run_bot_decides_once_off_the_loop_after_init_db():
    run_bot = _fn("run_bot")
    src = ast.unparse(run_bot)
    assert "venue_gate_enabled = await run_blocking_db(should_register_venue_gate)" in src
    assert "bot_app = setup_bot(venue_gate=venue_gate_enabled)" in src
    assert src.index("init_db()") < src.index("should_register_venue_gate)") < src.index("setup_bot(")


def test_command_menu_is_unchanged_in_phase_1():
    run_bot = _fn("run_bot")
    menu = [c for c in _calls_in_order(run_bot) if getattr(c.func, "id", None) == "BotCommand"]
    names = [ast.literal_eval(c.args[0]) for c in menu]
    assert "venue" not in names
    assert names == [
        "start", "help", "desk", "status", "ops", "brief", "howl", "news", "airdrop", "mm_status",
        "mm_fills", "stop_all", "revoke", "agent_on", "agent_off", "agent_status",
    ]


# ---------------------------------------------------------------------------
# i18n for every new string
# ---------------------------------------------------------------------------

_PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_TAG = re.compile(r"</?[a-z]+>")


@pytest.mark.parametrize("key", vh.I18N_TEXT_KEYS)
def test_every_new_text_has_all_five_translations(key):
    entry = i18n._TEXTS.get(key)
    assert entry is not None, key
    assert set(entry) == _LANGS, key
    for lang, text in entry.items():
        assert text.strip(), (lang, key)
        assert set(_PH.findall(text)) == set(_PH.findall(key)), (lang, key)
        assert _TAG.findall(text) == _TAG.findall(key), (lang, key)  # same HTML tags, same order


@pytest.mark.parametrize("key", vh.I18N_LABEL_KEYS)
def test_every_new_label_has_all_five_translations(key):
    entry = i18n._LABELS.get(key)
    assert entry is not None and set(entry) == _LANGS, key


def test_new_entries_are_appended_at_the_end():
    # Appending keeps every existing label first in _REVERSE_LABEL_MAP.
    assert list(i18n._LABELS)[-len(vh.I18N_LABEL_KEYS):] == list(vh.I18N_LABEL_KEYS)
    assert set(list(i18n._TEXTS)[-len(vh.I18N_TEXT_KEYS):]) == set(vh.I18N_TEXT_KEYS)


def test_reused_house_labels_exist():
    for label in (vh.LABEL_LANGUAGE, "🏠 Home"):
        assert set(i18n._LABELS[label]) == _LANGS


def test_callback_answers_fit_telegrams_200_char_limit():
    for key in vh.CALLBACK_ANSWER_TEXT_KEYS:
        assert len(key) <= 200
        for lang, text in i18n._TEXTS[key].items():
            assert len(text) <= 200, (lang, key)


def test_new_labels_never_route_as_a_reply_button():
    from src.nadobro.handlers.keyboards import REPLY_BUTTON_MAP

    for label in vh.I18N_LABEL_KEYS:
        for lang in sorted(_LANGS):
            shown = i18n.localize_label(label, lang)
            resolved = i18n.resolve_reply_button_text(shown, prefer=REPLY_BUTTON_MAP.__contains__)
            assert resolved not in REPLY_BUTTON_MAP, (label, lang, resolved)


@pytest.mark.parametrize("lang", ["en", *sorted(_LANGS)])
def test_arcus_screens_render_in_every_language_with_valid_html(lang):
    token = i18n._ACTIVE_LANG.set(lang)
    try:
        texts = [
            vh.arcus_home_text("testnet", [], False),
            vh.arcus_home_text("mainnet", [(vh.TEXT_ITEM_STRATEGY, {"strategy": "GRID BTC", "network": "MAINNET"}),
                                           (vh.TEXT_ITEM_COPY, {"n": "2"}), (vh.TEXT_ITEM_MANAGED_AI, {})], True),
            vh.arcus_help_text(), vh.arcus_settings_text(), vh.arcus_unavailable_text(),
            vh.venue_card_text("nado", "mainnet", "testnet"), vh.venue_card_text("arcus", None, "testnet"),
        ]
    finally:
        i18n._ACTIVE_LANG.reset(token)
    for text in texts:
        assert "{" not in text and "}" not in text, text  # every placeholder filled
        depth = 0
        for tag in _TAG.findall(text):
            assert tag in ("<b>", "</b>"), tag
            depth += 1 if tag == "<b>" else -1
            assert depth in (0, 1)
        assert depth == 0, text
