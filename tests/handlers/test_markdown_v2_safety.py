"""Every card must survive Telegram's MarkdownV2 entity parser.

``handlers/callbacks.py::_edit_loc`` carries a ``Can't parse entities`` rescue
that re-sends the message unformatted. That rescue existing means the failure
happens in production, and its user-visible symptom is a card collapsing into
raw text with visible backslashes — the theme breaking in front of the user.

This suite makes that failure mode a test failure instead. It caught one live
instance on the Points dashboard when it was written: ``*Est. Costs:*`` carried
an unescaped period while the identical ``Est. Margin`` label on the trade
preview was correctly routed through ``_loc_md``.

The validator lives in ``tests/handlers/md2.py`` and is deliberately
conservative: it flags only what Telegram definitely rejects, so a green run
here is a floor, not a ceiling.
"""

from __future__ import annotations

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import formatters as F  # noqa: E402
from tests.handlers.formatter_fixtures import CASES  # noqa: E402
from tests.handlers.md2 import find_problems  # noqa: E402

# ``fmt_price`` returns a bare number for interpolation into a card, not a card.
# It is escaped by its caller, so validating it standalone is meaningless.
NOT_A_CARD = {"fmt_price"}

CARD_CASES = [c for c in CASES if c[1] not in NOT_A_CARD]


@pytest.fixture(autouse=True)
def _deterministic_render(monkeypatch):
    monkeypatch.setattr(F, "_fmt_uptime", lambda s: "6h 30m" if s else "—")
    monkeypatch.setattr(F, "_fmt_age_seconds", lambda ts: "12s" if ts else "—")
    monkeypatch.setattr(F, "get_active_language", lambda: "en")
    monkeypatch.setattr(F, "localize_text", lambda text, _lang=None: text)
    yield


@pytest.mark.parametrize("case_id,fn_name,args,kwargs", CARD_CASES, ids=[c[0] for c in CARD_CASES])
def test_card_is_valid_markdown_v2(case_id, fn_name, args, kwargs):
    rendered = getattr(F, fn_name)(*args, **kwargs)
    problems = find_problems(rendered)
    assert not problems, (
        f"{case_id} ({fn_name}) would be rejected by Telegram's MarkdownV2 parser "
        f"and fall back to unformatted text:\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\n\nEscape the reserved character (use escape_md / _loc_md), "
        "or wrap the span in a code entity."
    )


def test_validator_rejects_the_bug_class_it_exists_for():
    """Guard the oracle itself — a validator that never fires protects nothing."""
    assert find_problems("Unescaped. period"), "should reject a bare period"
    assert find_problems("*unbalanced bold"), "should reject an unclosed entity"
    assert find_problems("`unclosed code"), "should reject an unclosed code span"
    assert find_problems("cost is 1.5"), "should reject a decimal point in plain text"


def test_validator_accepts_correctly_escaped_text():
    """And the oracle must not fire on the idioms the codebase actually uses."""
    assert not find_problems(r"*Bold* and \(escaped\) and 1\.5")
    assert not find_problems(r"`raw.code.here` outside\.")
    assert not find_problems(r"[label](https://t.me/nadobro_bot?start=A_B) tail\.")
    assert not find_problems(F.escape_md("$1,284,500.00 (+5.2%) — all reserved chars"))
