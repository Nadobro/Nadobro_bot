"""i18n for the texts / labels users/arcus_link_service.py owns (03 §18, §19.10 —
the part this stage adds; the handler-owned keys join in the UI stage).

Every key: all five translations, identical placeholders, the same HTML tags in
the same order, no ``*`` or backtick, no duplicate dict-literal key, and no
string a Nado surface already renders (it would start being translated for
Nado users: 03 V-1).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro import i18n  # noqa: E402
from src.nadobro.users import arcus_link_service as ls  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nadobro"
_LANGS = {"zh", "fr", "ar", "ru", "ko"}
_PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_TAG = re.compile(r"</?[a-z]+>")


@pytest.mark.parametrize("key", ls.I18N_TEXT_KEYS)
def test_texts_have_all_five_translations(key):
    entry = i18n._TEXTS.get(key)
    assert entry is not None, key
    assert set(entry) == _LANGS, key
    for lang, text in entry.items():
        assert text.strip(), (lang, key)
        assert sorted(_PH.findall(text)) == sorted(_PH.findall(key)), (lang, key)
        assert _TAG.findall(text) == _TAG.findall(key), (lang, key)


@pytest.mark.parametrize("key", ls.I18N_LABEL_KEYS)
def test_labels_have_all_five_translations(key):
    entry = i18n._LABELS.get(key)
    assert entry is not None and set(entry) == _LANGS, key


def test_no_markdown_markers():
    for key in ls.I18N_TEXT_KEYS + ls.I18N_LABEL_KEYS:
        source = i18n._TEXTS.get(key) or i18n._LABELS.get(key)
        for text in (key, *source.values()):
            assert "*" not in text and "`" not in text, text


def test_every_text_formats_with_its_placeholders():
    for key in ls.I18N_TEXT_KEYS:
        fmt = {name: "v" for name in _PH.findall(key)}
        key.format(**fmt)
        for text in i18n._TEXTS[key].values():
            text.format(**fmt)


def test_shared_vocabulary_is_pinned():
    assert i18n._TEXTS[ls.TEXT_BUSY] == {
        "zh": "Arcus 繁忙，请稍后再试。",
        "fr": "Arcus est occupé, réessayez dans un instant.",
        "ar": "Arcus مشغول، حاول مرة أخرى بعد لحظات.",
        "ru": "Arcus сейчас занят, попробуйте чуть позже.",
        "ko": "Arcus가 혼잡합니다. 잠시 후 다시 시도하세요.",
    }
    assert i18n._TEXTS[ls.TEXT_PR_NOT_ELIGIBLE] == {
        "zh": "该地址暂不符合 Arcus 使用资格。",
        "fr": "Cette adresse n'est pas encore éligible à Arcus.",
        "ar": "هذا العنوان غير مؤهل لـ Arcus بعد.",
        "ru": "Этот адрес пока не допущен к Arcus.",
        "ko": "이 주소는 아직 Arcus 이용 대상이 아닙니다.",
    }
    assert i18n._TEXTS[ls.TEXT_R_WALLET_KEY] == {
        "zh": "这是钱包私钥。请视其为已泄露，并立即转移您的资金。",
        "fr": "C'est une clé privée de PORTEFEUILLE. Considérez-la comme compromise et déplacez vos fonds.",
        "ar": "هذا مفتاح خاص لمحفظة. اعتبره مكشوفاً وانقل أموالك.",
        "ru": "Это приватный ключ КОШЕЛЬКА. Считайте его скомпрометированным и переведите средства.",
        "ko": "이것은 지갑 개인 키입니다. 노출된 것으로 간주하고 자금을 옮기세요.",
    }


def _dict_literal_keys(name: str) -> list[str]:
    tree = ast.parse((SRC / "i18n.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            assert isinstance(node.value, ast.Dict)
            return [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError(name)


def test_each_key_appears_exactly_once_in_the_dict_literals():
    texts, labels = _dict_literal_keys("_TEXTS"), _dict_literal_keys("_LABELS")
    for key in ls.I18N_TEXT_KEYS:
        assert texts.count(key) == 1, key
    for key in ls.I18N_LABEL_KEYS:
        assert labels.count(key) == 1, key


def test_labels_are_appended_after_the_p1_block():
    # The link service's label opens the P3b block; the UI half's labels follow it
    # (handlers/arcus_ui.ARCUS_P3B_LABEL_KEYS lists the whole block in order).
    from src.nadobro.handlers import arcus_ui
    from src.nadobro.handlers import venue_handler as vh

    block = list(arcus_ui.ARCUS_P3B_LABEL_KEYS)
    assert block[: len(ls.I18N_LABEL_KEYS)] == list(ls.I18N_LABEL_KEYS)
    tail = list(i18n._LABELS)[-(len(vh.I18N_LABEL_KEYS) + len(block)):]
    assert tail == list(vh.I18N_LABEL_KEYS) + block


def _nado_string_constants() -> set[str]:
    skip_names = {"i18n.py", "venue_handler.py"}
    out: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts or path.name in skip_names:
            continue
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(("users/arcus_", "handlers/arcus_")):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
    return out


def test_no_nado_string_is_newly_translated():
    nado = _nado_string_constants()
    for key in ls.I18N_TEXT_KEYS:
        assert key not in nado, key
    for label in ls.I18N_LABEL_KEYS:
        assert label not in nado and f"{label} ✅" not in nado, label


def test_new_labels_never_route_as_a_reply_button():
    from src.nadobro.handlers.keyboards import REPLY_BUTTON_MAP

    for label in ls.I18N_LABEL_KEYS:
        assert label not in REPLY_BUTTON_MAP
        for lang in sorted(_LANGS):
            shown = i18n.localize_label(label, lang)
            resolved = i18n.resolve_reply_button_text(shown, prefer=REPLY_BUTTON_MAP.__contains__)
            assert resolved not in REPLY_BUTTON_MAP, (label, lang, shown, resolved)
