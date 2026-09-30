"""CI lint: the Arcus venue library stays isolated (02 §9.1).

1. No relative imports under ``src/nadobro/venue/arcus/`` (absolute
   ``src.nadobro.…`` only, so rules 2-4 see real module names).
2. No module there imports (ANY import: module-level, function-local,
   ``TYPE_CHECKING``) a Nado venue module, a domain / UI package, Nado's HTTP
   session or IPv4 flag, or ``requests`` / ``urllib3`` / ``nado_protocol`` /
   ``eth_account`` / ``psycopg2`` / ``threading``.
3. The intra-package import DAG (module-level, non-``TYPE_CHECKING``) follows
   ``ARCUS_DAG`` — a module missing from the map fails, so a new module is added
   consciously — and every import is stdlib, ``src.nadobro.utils``, the package
   itself, or an explicitly allowed extra (``cryptography`` for signing,
   ``httpx`` for the client, ``config`` / ``core.feature_flags`` for client/hub).
4. No ``time.sleep`` and no synchronous HTTP (``httpx.Client``, ``httpx.get``…,
   ``httpx.HTTPTransport``) anywhere in the package: it runs on the event loop.
5. Outside the package, only ``ARCUS_IMPORTERS_ALLOWED`` may import
   ``venue.arcus`` in ANY form (absolute, ``from src.nadobro.venue import
   arcus``, relative, ``importlib``). P2 value: empty — the library is unwired
   and no Nado module can reach it (Nado byte-identity).
6. Detector self-checks on synthetic sources.
"""
from __future__ import annotations

import ast
import functools
import pathlib
import sys
from dataclasses import dataclass

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "nadobro"
PKG_DIR = SRC / "venue" / "arcus"
PKG = "src.nadobro.venue.arcus"

FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "src.nadobro.venue.nado_client",
    "src.nadobro.venue.nado_sync",
    "src.nadobro.venue.nado_ws",
    "src.nadobro.venue.nado_ws_actions",
    "src.nadobro.venue.nado_archive",
    "src.nadobro.venue.nado_tooling_service",
    "src.nadobro.venue.nado_weights",
    "src.nadobro.venue.product_catalog",
    "src.nadobro.venue.gateway_budget",
    "src.nadobro.venue.market_feed",
    "src.nadobro.venue.ws_health",
    "src.nadobro.models",
    "src.nadobro.users",
    "src.nadobro.trading",
    "src.nadobro.strategy",
    "src.nadobro.engine",
    "src.nadobro.handlers",
    "src.nadobro.notify",
    "src.nadobro.runtime",
    "src.nadobro.llm",
    "src.nadobro.market_data",
    "src.nadobro.connectors",
    "src.nadobro.portfolio",
    "src.nadobro.vault",
    "src.nadobro.i18n",
    "src.nadobro.core.http_session",
    "src.nadobro.core.ipv4_egress",
    "requests",
    "urllib3",
    "nado_protocol",
    "eth_account",
    "psycopg2",
    "threading",
)
# Later phases add explicit per-file exceptions to rule 2 here (contract §3.1),
# e.g. "ledger": ("src.nadobro.db",). P2: none.
FORBIDDEN_EXCEPTIONS: dict[str, tuple[str, ...]] = {}

ARCUS_DAG: dict[str, set[str]] = {
    "__init__": set(),  # the package docstring file; it must import nothing
    "types": set(),
    "errors": {"types"},
    "signing": {"types", "errors"},
    "parse": {"types", "errors"},
    "clock": {"types", "errors"},
    "budget": {"types"},
    "catalog": {"types", "errors", "parse"},
    "client": {"types", "errors", "signing", "clock", "budget", "parse", "catalog"},
    "hub": {"types", "errors", "signing", "clock", "budget", "catalog", "client",
            "ws", "order_store", "sync", "ledger"},  # P4a completes hub
    # contract §3.2, for P4a/P5 (these files do not exist in P2):
    "order_store": {"types", "errors", "catalog", "parse"},
    "ws": {"types", "errors", "order_store", "catalog", "parse"},
    "ledger": {"types"},
    "sync": {"types", "ledger", "order_store", "client", "budget"},
    "sweep": {"types", "errors", "client", "order_store", "budget", "ledger", "hub"},
}
# Imports beyond stdlib + src.nadobro.utils + the package itself.
EXTRA_ALLOWED: dict[str, tuple[str, ...]] = {
    "signing": ("cryptography",),
    "client": ("httpx", "src.nadobro.config", "src.nadobro.core.feature_flags"),
    "hub": ("src.nadobro.config", "src.nadobro.core.feature_flags"),
}
ALWAYS_ALLOWED: tuple[str, ...] = ("src.nadobro.utils", PKG)

# Rule 5: modules outside the package that may import it. P2: none (unwired).
ARCUS_IMPORTERS_ALLOWED: set[str] = set()

SYNC_HTTPX_ATTRS = frozenset(
    {"Client", "HTTPTransport", "get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
)
_STDLIB = frozenset(sys.stdlib_module_names) | {"__future__"}


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _module_name(rel: str) -> str:
    parts = pathlib.PurePosixPath(rel).with_suffix("").parts
    dotted = ".".join(parts)
    return dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


@dataclass(frozen=True)
class _Imp:
    targets: tuple[str, ...]  # resolved absolute candidates (module, module.name…)
    lineno: int
    module_level: bool
    type_checking: bool
    relative: bool


def _resolve_from(node: ast.ImportFrom, module: str, is_init: bool) -> str:
    if node.level == 0:
        return node.module or ""
    package = module if is_init else module.rpartition(".")[0]
    parts = package.split(".") if package else []
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    base = ".".join(parts)
    if node.module:
        return f"{base}.{node.module}" if base else node.module
    return base


def _imports(source: str, rel: str) -> list[_Imp]:
    tree = ast.parse(source)
    module = _module_name(rel)
    is_init = rel.endswith("__init__.py")
    out: list[_Imp] = []

    def visit(node: ast.AST, *, func: bool, tc: bool) -> None:
        if isinstance(node, ast.Import):
            out.append(_Imp(tuple(a.name for a in node.names), node.lineno, not func, tc, False))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, module, is_init)
            names = tuple(f"{base}.{a.name}" for a in node.names if a.name != "*")
            out.append(_Imp((base,) + names, node.lineno, not func, tc, node.level > 0))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for child in ast.iter_child_nodes(node):
                visit(child, func=True, tc=tc)
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.body:
                visit(child, func=func, tc=True)
            for child in node.orelse:
                visit(child, func=func, tc=tc)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child, func=func, tc=tc)

    visit(tree, func=False, tc=False)
    return out


def _stem(rel: str) -> str:
    return pathlib.PurePosixPath(rel).stem


def _package_violations(source: str, rel: str) -> list[str]:
    """Rules 1-4 for one file inside the package."""
    stem = _stem(rel)
    problems: list[str] = []
    if stem not in ARCUS_DAG:
        problems.append(f"{rel}: module '{stem}' is not in ARCUS_DAG (add it consciously)")
    allowed_subs = ARCUS_DAG.get(stem, set())
    forbidden = tuple(p for p in FORBIDDEN_IMPORT_PREFIXES if p not in FORBIDDEN_EXCEPTIONS.get(stem, ()))
    allowed_extra = ALWAYS_ALLOWED + EXTRA_ALLOWED.get(stem, ())
    for imp in _imports(source, rel):
        where = f"{rel}:{imp.lineno}"
        if imp.relative:
            problems.append(f"{where}: relative import (rule 1)")
        module_target = imp.targets[0]
        if any(_matches(t, forbidden) for t in imp.targets):
            problems.append(f"{where}: forbidden import {module_target} (rule 2)")
        if module_target.startswith("src.nadobro"):
            if not _matches(module_target, allowed_extra):
                problems.append(f"{where}: import {module_target} outside the allow-set (rule 3)")
        elif module_target.split(".")[0] not in _STDLIB and not _matches(module_target, allowed_extra):
            problems.append(f"{where}: third-party import {module_target} not allowed here (rule 3)")
        if imp.module_level and not imp.type_checking:
            for target in imp.targets:
                if not target.startswith(PKG + "."):
                    continue
                sub = target[len(PKG) + 1 :].split(".")[0]
                if sub != stem and sub not in allowed_subs:
                    problems.append(f"{where}: '{stem}' may not import '{sub}' at module level (DAG)")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            owner, attr = node.func.value.id, node.func.attr
            if owner == "time" and attr == "sleep":
                problems.append(f"{rel}:{node.lineno}: time.sleep (rule 4)")
            if owner == "httpx" and attr in SYNC_HTTPX_ATTRS:
                problems.append(f"{rel}:{node.lineno}: synchronous httpx.{attr} (rule 4)")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = {a.name for a in node.names}
            if node.module == "time" and "sleep" in names:
                problems.append(f"{rel}:{node.lineno}: from time import sleep (rule 4)")
            if node.module == "httpx" and names & SYNC_HTTPX_ATTRS:
                problems.append(f"{rel}:{node.lineno}: synchronous httpx import (rule 4)")
    return problems


def _imports_arcus(source: str, rel: str) -> bool:
    """Rule 5 detector: ANY spelling that loads ``venue.arcus``."""
    if "arcus" not in source:  # every spelling below contains it: a sound fast path
        return False
    for imp in _imports(source, rel):
        for target in imp.targets:
            if _matches(target, (PKG, "nadobro.venue.arcus", "venue.arcus")):
                return True
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        first = node.args[0]
        if (
            name in ("import_module", "__import__")
            and isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and "venue.arcus" in first.value
        ):
            return True
    return False


def _package_files() -> list[pathlib.Path]:
    return sorted(p for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(REPO).as_posix()


@functools.cache  # one parse per package file shared by every rule below
def _file_problems(path: pathlib.Path) -> tuple[str, ...]:
    return tuple(_package_violations(path.read_text(encoding="utf-8"), _rel(path)))


def _problems(marker: str) -> list[str]:
    return [p for f in _package_files() for p in _file_problems(f) if marker in p]


# --- rules ---------------------------------------------------------------------------------


def test_arcus_package_exists():
    assert PKG_DIR.is_dir() and _package_files()


def test_arcus_package_has_no_relative_imports():
    problems = _problems("rule 1")
    assert not problems, "\n".join(problems)


def test_arcus_package_forbidden_imports():
    problems = _problems("rule 2") + _problems("rule 3")
    assert not problems, "\n".join(problems)


def test_arcus_intra_package_dag():
    problems = _problems("DAG")
    assert not problems, "\n".join(problems)


def test_no_time_sleep_in_arcus():
    problems = _problems("rule 4")
    assert not problems, "\n".join(problems)


def test_only_allowed_modules_import_venue_arcus():
    offenders: list[str] = []
    candidates = [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts] + [REPO / "main.py"]
    for path in sorted(candidates):
        if PKG_DIR in path.parents:
            continue
        rel = _rel(path)
        if rel in ARCUS_IMPORTERS_ALLOWED:
            continue
        if _imports_arcus(path.read_text(encoding="utf-8"), rel):
            offenders.append(rel)
    assert not offenders, (
        "modules outside src/nadobro/venue/arcus import it but are not in ARCUS_IMPORTERS_ALLOWED "
        "(P2: the library is unwired):\n  " + "\n  ".join(offenders)
    )


def test_importer_allowlist_has_no_stale_entries():
    stale = sorted(rel for rel in ARCUS_IMPORTERS_ALLOWED if not (REPO / rel).exists())
    assert not stale, stale


# --- 6. detector self-checks -------------------------------------------------------------------

_CLIENT = "src/nadobro/venue/arcus/client.py"
_TYPES = "src/nadobro/venue/arcus/types.py"


def _flags(source: str, rel: str = _CLIENT) -> list[str]:
    return _package_violations(source, rel)


def test_detector_self_check():
    assert any("rule 1" in p for p in _flags("from ..nado_client import X"))
    assert any("rule 2" in p for p in _flags("from ..nado_client import X"))  # resolves to venue.nado_client
    assert any("rule 2" in p for p in _flags("import requests"))
    assert any("rule 2" in p for p in _flags("def f():\n    import threading\n"))  # function-local too
    assert any("rule 2" in p for p in _flags("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from src.nadobro.users import x\n"))
    assert any("rule 2" in p for p in _flags("from src.nadobro.users import x"))
    assert any("rule 2" in p for p in _flags("from src.nadobro.venue import gateway_budget"))
    assert any("rule 3" in p for p in _flags("import src.nadobro.db"))
    assert any("rule 3" in p for p in _flags("import websockets"))
    assert any("rule 3" in p for p in _flags("import httpx", _TYPES))  # httpx only in client
    assert any("rule 4" in p for p in _flags("import time\ntime.sleep(1)"))
    assert any("rule 4" in p for p in _flags("from time import sleep"))
    assert any("rule 4" in p for p in _flags("import httpx\nhttpx.Client()"))
    assert any("rule 4" in p for p in _flags("import httpx\nhttpx.get(u)"))
    assert any("rule 4" in p for p in _flags("from httpx import Client"))
    assert any("DAG" in p for p in _flags("from src.nadobro.venue.arcus.client import X", _TYPES))
    assert any("DAG" in p for p in _flags("from src.nadobro.venue.arcus import hub"))
    assert any("ARCUS_DAG" in p for p in _flags("x = 1", "src/nadobro/venue/arcus/newmod.py"))
    # accepted
    assert _flags("from src.nadobro.venue.arcus.types import Side") == []
    assert _flags("import httpx\nhttpx.AsyncClient()\nhttpx.AsyncHTTPTransport()") == []
    assert _flags("from src.nadobro.config import arcus_rest_url\nimport asyncio, json") == []
    assert _flags("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from src.nadobro.venue.arcus.client import C\n", _TYPES) == []
    assert _flags("def f():\n    from src.nadobro.venue.arcus.client import C\n", _TYPES) == []  # function-local: DAG-exempt
    # rule 5
    other = "src/nadobro/users/foo.py"
    assert _imports_arcus("from src.nadobro.venue import arcus", other)
    assert _imports_arcus("import src.nadobro.venue.arcus.client", other)
    assert _imports_arcus("from src.nadobro.venue.arcus.types import Side", other)
    assert _imports_arcus("def f():\n    from src.nadobro.venue.arcus import hub\n", other)
    assert _imports_arcus("from ..venue.arcus import client", other)
    assert _imports_arcus("from ..venue import arcus", other)
    assert _imports_arcus("from .arcus import client", "src/nadobro/venue/foo.py")
    assert _imports_arcus("import importlib\nimportlib.import_module('src.nadobro.venue.arcus.hub')", other)
    assert not _imports_arcus("from src.nadobro.venue import nado_client", other)
    assert not _imports_arcus("from src.nadobro import venue", other)
