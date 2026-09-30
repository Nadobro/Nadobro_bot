"""CI lint: only src/nadobro/engine/adapter/nado.py may import from
src/nadobro/connectors/nado/. This guarantees the 1CT Linked Signer path
has a single source of truth.
"""
from __future__ import annotations

import ast
import pathlib
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "nadobro"
ALLOWED_IMPORTER = SRC_ROOT / "engine" / "adapter" / "nado.py"
FORBIDDEN_PREFIXES = (
    "nadobro.connectors.nado",
    "src.nadobro.connectors.nado",
    "connectors.nado",
)


def _is_forbidden(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


def _imports_connectors_nado(py_file: pathlib.Path) -> bool:
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and _is_forbidden(node.module):
                return True
    return False


def test_only_adapter_may_import_connectors_nado() -> None:
    violations: list[str] = []
    for py in SRC_ROOT.rglob("*.py"):
        if py.resolve() == ALLOWED_IMPORTER.resolve():
            continue
        if _imports_connectors_nado(py):
            violations.append(str(py.relative_to(REPO_ROOT)))
    assert not violations, (
        "Forbidden imports of connectors/nado outside engine/adapter/nado.py:\n  "
        + "\n  ".join(violations)
    )


# --- Engine-scoped guard ---------------------------------------------------
# The live venue client is ``venue/nado_client``. Code across the repo imports
# it freely, so this guard is scoped to ``src/nadobro/engine`` only: within the
# engine, just ``adapter/nado.py`` may touch the venue client (single source of
# truth for the 1CT Linked Signer path). All three spellings matter — a
# RELATIVE import (``from ...venue.nado_client import X``) shows up in the AST
# as the bare ``venue.nado_client``, without the ``src.`` prefix.
ENGINE_ROOT = SRC_ROOT / "engine"
ENGINE_VENUE_FORBIDDEN = (
    "src.nadobro.venue.nado_client",
    "nadobro.venue.nado_client",
    "venue.nado_client",
)


def _imports_any(py_file: pathlib.Path, prefixes: tuple[str, ...]) -> bool:
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except SyntaxError:
        return False

    def forbidden(name: str) -> bool:
        return any(name == p or name.startswith(p + ".") for p in prefixes)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if forbidden(alias.name):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and forbidden(node.module):
                return True
    return False


def test_only_engine_adapter_may_import_nado_client() -> None:
    violations: list[str] = []
    for py in ENGINE_ROOT.rglob("*.py"):
        if py.resolve() == ALLOWED_IMPORTER.resolve():
            continue
        if _imports_any(py, ENGINE_VENUE_FORBIDDEN):
            violations.append(str(py.relative_to(REPO_ROOT)))
    assert not violations, (
        "Engine modules other than adapter/nado.py import the venue client "
        "(services/nado_client):\n  " + "\n  ".join(violations)
    )


# --- Arcus venue (02 §9.3) -----------------------------------------------------
# Inside the engine, only adapter/arcus.py (lands in P6a) may import the Arcus
# venue library; the Arcus adapter never touches the Nado paths; the Nado adapter
# never touches Arcus. All three spellings (absolute, package-relative, bare).
ARCUS_ADAPTER = ENGINE_ROOT / "adapter" / "arcus.py"
NADO_ADAPTER = ALLOWED_IMPORTER  # engine/adapter/nado.py
ARCUS_VENUE_PREFIXES = ("src.nadobro.venue.arcus", "nadobro.venue.arcus", "venue.arcus")


def test_only_arcus_adapter_may_import_venue_arcus_in_engine() -> None:
    violations: list[str] = []
    for py in ENGINE_ROOT.rglob("*.py"):
        if py.resolve() == ARCUS_ADAPTER.resolve():
            continue
        if _imports_any(py, ARCUS_VENUE_PREFIXES):
            violations.append(str(py.relative_to(REPO_ROOT)))
    assert not violations, (
        "Engine modules other than adapter/arcus.py import the Arcus venue library:\n  "
        + "\n  ".join(violations)
    )


def test_arcus_adapter_does_not_import_nado_paths() -> None:
    # Vacuously green until P6a creates engine/adapter/arcus.py.
    if not ARCUS_ADAPTER.exists():
        return
    forbidden = ENGINE_VENUE_FORBIDDEN + FORBIDDEN_PREFIXES + (
        "src.nadobro.engine.order_tags",
        "src.nadobro.engine.order_lifecycle",
        "engine.order_tags",
        "engine.order_lifecycle",
    )
    assert not _imports_any(ARCUS_ADAPTER, forbidden), (
        "engine/adapter/arcus.py imports a Nado-only path (nado_client / connectors.nado / "
        "order_tags / order_lifecycle)"
    )


def test_nado_adapter_does_not_import_venue_arcus() -> None:
    assert not _imports_any(NADO_ADAPTER, ARCUS_VENUE_PREFIXES), (
        "engine/adapter/nado.py must not import the Arcus venue library"
    )
