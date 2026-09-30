"""CI lint: forbidden Arcus endpoint strings never appear in code (02 §9.2).

build_decisions: "never call cancelAllOrders (lint)"; D-5: no dead man's switch
(``scheduleCancel``); contract §3.3: never modify an order (the modify hazard),
never manage API keys, never withdraw or transfer. ``setLeverage`` (heavyweight,
user-tap only) may appear only in the client, the Scheme-2 signing allowlist and
the weight table.

Scanned: every ``src/nadobro/**/*.py`` and ``scripts/*arcus*.py`` (a plain
``scripts/arcus_*.py`` glob would miss ``capture_arcus_shapes.py``). Every string
constant is checked (f-string parts included) EXCEPT docstrings, so a docstring
may explain why cancelAllOrders is never called. Bare "withdraw"/"transfer"
words are not flagged (existing Nado code uses them, 02 D13) — only path forms.
"""
from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nadobro"
SCRIPTS = REPO / "scripts"

FORBIDDEN_ACTIONS = (
    "cancelAllOrders",
    "modifyOrder",
    "batchModifyOrders",
    "scheduleCancel",
    "createApiKey",
    "revokeApiKey",
)
FORBIDDEN_PATH_WORDS = ("withdraw", "transfer")
_PATH_RE = re.compile(
    r"(?:^|/)v1/(cancelAllOrders|modifyOrder|batchModifyOrders|scheduleCancel|createApiKey|revokeApiKey|withdraw|transfer)\b"
)
_PATH_EXACT = frozenset({"/withdraw", "/transfer", "v1/withdraw", "v1/transfer"})
SET_LEVERAGE_ALLOWED = {
    "src/nadobro/venue/arcus/client.py",
    "src/nadobro/venue/arcus/signing.py",
    "src/nadobro/venue/arcus/budget.py",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _violations(source: str, rel: str) -> list[str]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str) or id(node) in docstrings:
            continue
        text = node.value
        stripped = text.strip()
        if stripped in FORBIDDEN_ACTIONS or _PATH_RE.search(text) or stripped in _PATH_EXACT:
            out.append(f"{rel}:{node.lineno}: forbidden Arcus endpoint string {stripped[:60]!r}")
        elif (stripped == "setLeverage" or "/v1/setLeverage" in text) and rel not in SET_LEVERAGE_ALLOWED:
            out.append(f"{rel}:{node.lineno}: 'setLeverage' outside the Arcus client/signing/budget")
    return out


def _scanned() -> list[pathlib.Path]:
    files = [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]
    files += sorted(SCRIPTS.glob("*arcus*.py"))
    return sorted(files)


def test_no_forbidden_arcus_endpoint_strings():
    problems: list[str] = []
    for path in _scanned():
        rel = path.relative_to(REPO).as_posix()
        source = path.read_text(encoding="utf-8")
        # Fast path: a file with none of the words cannot violate.
        if not any(w in source for w in FORBIDDEN_ACTIONS + FORBIDDEN_PATH_WORDS + ("setLeverage",)):
            continue
        problems.extend(_violations(source, rel))
    assert not problems, "\n".join(problems)


def test_set_leverage_allowlist_files_exist():
    missing = sorted(rel for rel in SET_LEVERAGE_ALLOWED if not (REPO / rel).exists())
    assert not missing, missing


def test_detector_self_check():
    other = "src/nadobro/strategy/foo.py"
    client = "src/nadobro/venue/arcus/client.py"
    flagged = [
        ('x = "/v1/cancelAllOrders"', client),
        ('x = "cancelAllOrders"', client),
        ('x = " modifyOrder "', client),
        ('x = f"{base}/v1/scheduleCancel"', client),
        ('x = "https://api.arcus.xyz/v1/withdraw"', client),
        ('x = "/v1/transfer?x=1"', client),
        ('x = "/withdraw"', other),
        ('x = "v1/transfer"', other),
        ('x = "createApiKey"', client),
        ('x = "setLeverage"', other),
        ('x = "/v1/setLeverage"', other),
        ('def f():\n    """doc"""\n    return "revokeApiKey"', client),
    ]
    for source, rel in flagged:
        assert _violations(source, rel), source
    accepted = [
        ('x = "withdraw"', other),
        ('x = "transfer"', other),
        ('x = "CANCEL_ALL_ACKNOWLEDGED"', client),
        ('"""We never call cancelAllOrders or /v1/withdraw."""', client),
        ('def f():\n    """never cancelAllOrders"""\n    return 1', client),
        ('x = "setLeverage"', client),
        ('x = "/v1/setLeverage"', client),
        ('x = "deposit/withdraw confirm"', other),
        ('x = "/v1/withdrawals-history"', other),  # \\b: a different word
    ]
    for source, rel in accepted:
        assert not _violations(source, rel), source
