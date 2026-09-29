"""CI lint: every name in the extracted handler modules must resolve.

Why this exists (2026-06-11 production incident): the callbacks.py
decomposition moved functions between modules with an AST-based extractor
whose collector handled ``Assign`` but not ``AnnAssign`` — so the annotated
module global ``_balance_cache: dict[...] = {}`` stayed in callbacks.py
while its only consumer moved to strategy_handler.py. Every strategy
preview (GRID / D-GRID / R-GRID / Mid Mode) then died with a NameError at
runtime; no test exercised the path, and import succeeded because Python
resolves globals at CALL time.

This lint resolves names STATICALLY: for each module-level function in the
extracted handler modules, any loaded name must be a builtin, a module
global (imports / defs / Assign / AnnAssign / With / For / class), one of
the function's own locals (params, assignments, comprehension targets,
``with``/``for``/except bindings, nested defs), or a global declared via
``global``. Anything left over is exactly the class of bug that shipped.
"""
from __future__ import annotations

import ast
import builtins
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
HANDLER_MODULES = [
    "src/nadobro/handlers/callbacks.py",
    "src/nadobro/handlers/strategy_handler.py",
    "src/nadobro/handlers/bro_handler.py",
    "src/nadobro/handlers/copy_handler.py",
    "src/nadobro/handlers/portfolio_handler.py",
    "src/nadobro/handlers/settings_handler.py",
    "src/nadobro/handlers/wallet_handler.py",
    "src/nadobro/handlers/alerts_handler.py",
    "src/nadobro/handlers/desk_handler.py",
    "src/nadobro/handlers/venue_gate.py",
    "src/nadobro/handlers/venue_handler.py",
]

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}


def _stored_names(node: ast.AST) -> set[str]:
    """Every name BOUND anywhere inside ``node`` (assignments, loop/with/except
    targets, comprehensions, walrus, imports, nested function/class names,
    parameters of nested functions)."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for alias in n.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
    return out


def _module_globals(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out.update(_stored_names(t))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)  # the node type the extractor missed
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For)):
            out.update(_stored_names(node))
    return out


def _unresolved_in_module(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_names = _module_globals(tree)
    problems: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _stored_names(node)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                name = sub.id
                if name in _BUILTINS or name in local or name in module_names:
                    continue
                problems.append(f"{path.name}:{sub.lineno} `{name}` (in {node.name})")
    return sorted(set(problems))


def test_extracted_handler_modules_have_no_unresolved_names():
    problems: list[str] = []
    for rel in HANDLER_MODULES:
        problems.extend(_unresolved_in_module(REPO_ROOT / rel))
    assert not problems, (
        "Unresolvable module-global names — this is the _balance_cache "
        "NameError class (a module split left a global behind):\n  "
        + "\n  ".join(problems)
    )
