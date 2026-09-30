"""The Arcus venue library imports with no DB, no Telegram and no ``requests``
(02 §12.11, acceptance 7) — and pulls in nothing from the bot's domain layers.

Runs in a fresh interpreter so this process's already-imported modules cannot
mask a dependency.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

REPO = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "nadobro" / "venue" / "arcus"

_SCRIPT = textwrap.dedent(
    """
    import importlib, sys
    for blocked in ("psycopg2", "telegram", "requests"):
        sys.modules[blocked] = None  # any import of these now raises ImportError
    names = {names!r}
    for name in names:
        importlib.import_module("src.nadobro.venue.arcus." + name)
    loaded = sorted(m for m in sys.modules if m.startswith("src.nadobro."))
    forbidden = (
        "src.nadobro.db", "src.nadobro.models", "src.nadobro.users", "src.nadobro.trading",
        "src.nadobro.strategy", "src.nadobro.engine", "src.nadobro.handlers", "src.nadobro.notify",
        "src.nadobro.runtime", "src.nadobro.llm", "src.nadobro.market_data", "src.nadobro.connectors",
        "src.nadobro.portfolio", "src.nadobro.vault", "src.nadobro.i18n",
        "src.nadobro.venue.nado", "src.nadobro.venue.gateway_budget", "src.nadobro.venue.product_catalog",
        "src.nadobro.core.http_session", "src.nadobro.core.ipv4_egress",
    )
    bad = [m for m in loaded if m.startswith(forbidden)]
    assert not bad, bad
    print("ok", len(loaded))
    """
)


def _modules() -> list[str]:
    return sorted(p.stem for p in PACKAGE.glob("*.py"))


def test_import_every_module_without_db_telegram_or_requests():
    assert {"types", "budget", "catalog", "client", "hub"} <= set(_modules())
    script = _SCRIPT.format(names=[m for m in _modules() if m != "__init__"])
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("ok")


def test_package_init_imports_nothing():
    """``import …venue.arcus.types`` must never drag httpx in via the package."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import src.nadobro.venue.arcus as a; "
            "assert 'httpx' not in sys.modules, 'httpx'; "
            "assert not [m for m in sys.modules if m.startswith('src.nadobro.venue.arcus.')]; print('ok')",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
