"""Every literal message sent as MarkdownV2 from a handler must parse.

test_markdown_v2_safety covers the fmt_* card layer. This extends the same
guarantee to the HANDLER layer — the inline error/prompt/status strings passed
directly to a MarkdownV2 send (_edit_loc, edit_message_text, reply_text,
send_message, answer). Those bypass the card formatters, so nothing else pins
them, and an unescaped reserved char there produces the same F-02 failure: the
message hits the "Can't parse entities" rescue and renders as raw text with
visible backslashes.

This suite found exactly that on the onboarding readiness wall — the first
screen a not-onboarded user sees on Trade — where "(language + accept terms)."
shipped with unescaped ( + ) . under MarkdownV2.

Method: statically walk each handler module for calls that pass
parse_mode=MARKDOWN_V2, take every string-literal argument (and text= kwarg),
build the f-string skeleton (dynamic {..} → a neutral placeholder, since those
are escaped at the interpolation site), and validate it with the MarkdownV2
oracle. A literal reaching a MarkdownV2 send is authored as MarkdownV2, so it
must be fully escaped; strings that are escaped at render time go through
escape_md BEFORE the send and never appear here as a bare literal.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# ``_edit_loc`` runs ``text.format(**fmt)`` after localizing, so a literal
# ``{name}`` is a template placeholder filled with an already-escaped value, not
# a stray brace. Treat those exactly like an f-string's {..} — a neutral token —
# so the net flags real unescaped chars in the STATIC copy, not the templating.
_FORMAT_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

from _stubs import install_test_stubs

install_test_stubs()

from tests.handlers.md2 import find_problems  # noqa: E402

HANDLERS = Path(__file__).resolve().parents[2] / "src" / "nadobro" / "handlers"

# A skeleton placeholder for an interpolated value. It is a single safe token so
# a genuine unescaped reserved char in the LITERAL portions still surfaces.
_DYN = "X"


def _skeleton(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        raw = node.value
    elif isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            parts.append(str(value.value) if isinstance(value, ast.Constant) else _DYN)
        raw = "".join(parts)
    else:
        return None
    return _FORMAT_PLACEHOLDER.sub(_DYN, raw)


def _is_markdown_v2_call(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "parse_mode" and "MARKDOWN_V2" in ast.unparse(kw.value):
            return True
    return False


def _candidate_texts(call: ast.Call):
    """Every string-literal/f-string that could be the message body of this call."""
    for arg in call.args:
        sk = _skeleton(arg)
        if sk is not None:
            yield arg, sk
    for kw in call.keywords:
        if kw.arg in ("text", None):  # text= or **{}
            sk = _skeleton(kw.value)
            if sk is not None:
                yield kw.value, sk


def _collect():
    findings = []
    for path in sorted(HANDLERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_markdown_v2_call(node)):
                continue
            for arg_node, text in _candidate_texts(node):
                # Skip trivial / non-message tokens (format keys, single words
                # with no reserved chars can't fail anyway).
                if len(text) < 3:
                    continue
                problems = find_problems(text)
                if problems:
                    findings.append((path.name, getattr(arg_node, "lineno", node.lineno), text, problems))
    return findings


def test_handler_markdown_v2_literals_parse():
    findings = _collect()
    if findings:
        lines = []
        for fname, lineno, text, problems in findings:
            lines.append(f"  {fname}:{lineno}: {text[:80]!r}")
            lines.append(f"      → {problems[0]}")
        pytest.fail(
            "handler strings sent as MarkdownV2 would be rejected by Telegram and "
            "fall back to raw text:\n" + "\n".join(lines)
            + "\n\nEscape the reserved chars (\\. \\- \\( \\) \\! \\| \\= …) or route "
            "the dynamic parts through escape_md."
        )


def test_the_oracle_still_flags_the_bug_this_suite_was_written_for():
    """Guard the guard: the readiness-wall bug must still be detectable."""
    assert find_problems("⚠️ Complete setup first (language + accept terms).")
    assert not find_problems("⚠️ Finish setup first — pick a language and accept the terms\\.")
