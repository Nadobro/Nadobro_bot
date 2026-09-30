"""i18n for every Arcus P3b string (03 §18, §19.10).

Every key: all five translations, identical placeholders, the same HTML tags in
the same order, no ``*`` / backtick, appended as the last block, present exactly
once in the dict literal, never equal to a string a Nado surface already
renders (it would start being translated for Nado users: 03 V-1), and no new
label ever routes as a reply-keyboard button.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro import i18n  # noqa: E402
from src.nadobro.handlers import arcus_ui  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402
from src.nadobro.users import arcus_link_service as ls  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nadobro"
_LANGS = {"zh", "fr", "ar", "ru", "ko"}
_PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_TAG = re.compile(r"</?[a-z]+>")


def test_key_sets_cover_both_halves():
    assert set(ls.I18N_TEXT_KEYS) <= set(arcus_ui.ARCUS_P3B_TEXT_KEYS)
    assert tuple(arcus_ui.ARCUS_P3B_LABEL_KEYS[: len(ls.I18N_LABEL_KEYS)]) == tuple(ls.I18N_LABEL_KEYS)
    assert vh.TEXT_HELP_LINK in arcus_ui.ARCUS_P3B_TEXT_KEYS
    assert vh.TEXT_HELP_LINK not in vh.I18N_TEXT_KEYS  # P1's tuple is pinned by its own test
    assert len(set(arcus_ui.ARCUS_P3B_TEXT_KEYS)) == len(arcus_ui.ARCUS_P3B_TEXT_KEYS)
    assert len(set(arcus_ui.ARCUS_P3B_LABEL_KEYS)) == len(arcus_ui.ARCUS_P3B_LABEL_KEYS)
    # every TEXT_* constant of arcus_ui is listed (a new text cannot ship untranslated)
    constants = {getattr(arcus_ui, n) for n in dir(arcus_ui) if n.startswith("TEXT_")}
    assert constants <= set(arcus_ui.ARCUS_P3B_TEXT_KEYS) | {vh.TEXT_ARCUS_NOT_ALLOWED}


@pytest.mark.parametrize("key", arcus_ui.ARCUS_P3B_TEXT_KEYS)
def test_texts_have_all_five_translations(key):
    entry = i18n._TEXTS.get(key)
    assert entry is not None, key
    assert set(entry) == _LANGS, key
    for lang, text in entry.items():
        assert text.strip(), (lang, key)
        assert sorted(_PH.findall(text)) == sorted(_PH.findall(key)), (lang, key)
        assert _TAG.findall(text) == _TAG.findall(key), (lang, key)  # same tags, same order


@pytest.mark.parametrize("key", arcus_ui.ARCUS_P3B_LABEL_KEYS)
def test_labels_have_all_five_translations(key):
    entry = i18n._LABELS.get(key)
    assert entry is not None and set(entry) == _LANGS, key
    assert all(t.strip() for t in entry.values()), key


def test_no_markdown_markers_anywhere():
    for key in arcus_ui.ARCUS_P3B_TEXT_KEYS + arcus_ui.ARCUS_P3B_LABEL_KEYS:
        source = i18n._TEXTS.get(key) or i18n._LABELS.get(key)
        for text in (key, *source.values()):
            assert "*" not in text and "`" not in text, text


def test_every_text_formats_with_its_placeholders():
    for key in arcus_ui.ARCUS_P3B_TEXT_KEYS:
        fmt = {name: "v" for name in _PH.findall(key)}
        key.format(**fmt)
        for text in i18n._TEXTS[key].values():
            text.format(**fmt)


def test_labels_are_the_last_block_right_after_p1():
    block = list(arcus_ui.ARCUS_P3B_LABEL_KEYS)
    tail = list(i18n._LABELS)[-(len(vh.I18N_LABEL_KEYS) + len(block)):]
    assert tail == list(vh.I18N_LABEL_KEYS) + block


def test_texts_are_the_last_block_right_after_p1():
    p1, p3b = set(vh.I18N_TEXT_KEYS), set(arcus_ui.ARCUS_P3B_TEXT_KEYS)
    tail = list(i18n._TEXTS)[-(len(p1) + len(p3b)):]
    assert set(tail[: len(p1)]) == p1 and set(tail[len(p1):]) == p3b


def _dict_literal_keys(name):
    tree = ast.parse((SRC / "i18n.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError(name)


def test_each_key_appears_exactly_once_in_the_dict_literals():
    # A second literal occurrence (e.g. a later phase re-adding "Arcus is busy, try again in
    # a moment.") would silently override this entry.
    texts, labels = _dict_literal_keys("_TEXTS"), _dict_literal_keys("_LABELS")
    for key in arcus_ui.ARCUS_P3B_TEXT_KEYS:
        assert texts.count(key) == 1, key
    for key in arcus_ui.ARCUS_P3B_LABEL_KEYS:
        assert labels.count(key) == 1, key


def _nado_string_constants():
    out = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel in ("i18n.py", "handlers/venue_handler.py") or rel.startswith(("users/arcus_", "handlers/arcus_")):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
    return out


def test_no_nado_string_is_newly_translated():
    nado = _nado_string_constants()
    assert "🧪 Testnet" in nado  # the scan sees Nado's /mode keyboard (the V-1 case)
    for key in arcus_ui.ARCUS_P3B_TEXT_KEYS:
        assert key not in nado, key
    for label in arcus_ui.ARCUS_P3B_LABEL_KEYS:
        assert label not in nado and f"{label} ✅" not in nado, label


def test_nado_mode_labels_stay_untranslated():
    for lang in sorted(_LANGS):
        assert i18n.localize_label("🧪 Testnet ✅", lang) == "🧪 Testnet ✅"
        assert i18n.localize_label("🟢 Mainnet", lang) == "🟢 Mainnet"


def test_new_labels_never_route_as_a_reply_button():
    from src.nadobro.handlers.keyboards import REPLY_BUTTON_MAP

    for label in arcus_ui.ARCUS_P3B_LABEL_KEYS:
        assert label not in REPLY_BUTTON_MAP
        for lang in sorted(_LANGS):
            shown = i18n.localize_label(label, lang)
            resolved = i18n.resolve_reply_button_text(shown, prefer=REPLY_BUTTON_MAP.__contains__)
            assert resolved not in REPLY_BUTTON_MAP, (label, lang, shown, resolved)


def test_wallet_label_matches_the_domain_texts():
    # The domain texts (reminders, diagnosis) name the button: keep one translation.
    expected = {"zh": "👛 Arcus 钱包", "fr": "👛 Portefeuille Arcus", "ar": "👛 محفظة Arcus",
                "ru": "👛 Кошелёк Arcus", "ko": "👛 Arcus 지갑"}
    assert i18n._LABELS[arcus_ui.LABEL_WALLET] == expected
    for lang, name in expected.items():
        assert name in i18n._TEXTS[ls.TEXT_KR_EXPIRED][lang]
        assert name in i18n._TEXTS[arcus_ui.TEXT_H_EXPIRED][lang]


def test_shared_vocabulary_is_pinned():
    assert i18n._TEXTS[ls.TEXT_BUSY] == {
        "zh": "Arcus 繁忙，请稍后再试。",
        "fr": "Arcus est occupé, réessayez dans un instant.",
        "ar": "Arcus مشغول، حاول مرة أخرى بعد لحظات.",
        "ru": "Arcus сейчас занят, попробуйте чуть позже.",
        "ko": "Arcus가 혼잡합니다. 잠시 후 다시 시도하세요.",
    }
    assert i18n._TEXTS[ls.TEXT_PR_NOT_ELIGIBLE]["ru"] == "Этот адрес пока не допущен к Arcus."
    assert i18n._TEXTS[ls.TEXT_R_WALLET_KEY]["ko"] == "이것은 지갑 개인 키입니다. 노출된 것으로 간주하고 자금을 옮기세요."


@pytest.mark.parametrize("lang", ["en", *sorted(_LANGS)])
def test_cards_render_in_every_language_with_balanced_html(lang, monkeypatch):
    """The link / wallet / network texts render with every placeholder filled and
    balanced <b>/<code> tags in every language."""
    token = i18n._ACTIVE_LANG.set(lang)
    try:
        fmt = {"network": "TESTNET", "address": "0x" + "ab" * 20, "key_name": "nadobro-ab12", "until": "2027",
               "days": "3", "stop_hours": "24", "when": "2026", "terms_url": "https://arcus.xyz/legal/terms",
               "status": "ok", "seconds": "12", "hours": "5"}
        texts = [vh.tr(key, **{k: v for k, v in fmt.items() if "{" + k + "}" in key})
                 for key in arcus_ui.ARCUS_P3B_TEXT_KEYS]
    finally:
        i18n._ACTIVE_LANG.reset(token)
    for text in texts:
        assert "{" not in text and "}" not in text, text
        stack = []
        for tag in _TAG.findall(text):
            if tag.startswith("</"):
                assert stack and stack.pop() == tag[2:-1], text
            else:
                stack.append(tag[1:-1])
        assert not stack, text
