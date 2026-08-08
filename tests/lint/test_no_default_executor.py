"""CI lint: no blocking work on the event loop's IMPLICIT default executor.

``asyncio.to_thread(...)`` and ``loop.run_in_executor(None, ...)`` both dispatch
to the loop's default ThreadPoolExecutor, sized ``min(32, cpu_count + 4)``. On
the production Fly VM (``cpus = 1``, fly.toml) that is FIVE threads — shared
process-wide by every caller.

Production incident 2026-08-06: the engine adapter issued 12 kinds of gateway
call per strategy tick through ``asyncio.to_thread``, alongside
``venue/nado_sync``'s per-user snapshot writes and ``market_data``'s snapshot
gather. Those five threads saturated, and the symptoms were:

    Strategy cycle end ... elapsed_ms=45324.7
    apscheduler: Execution of job "sync_active_users" skipped:
        maximum number of running instances reached (1)
    SLO breach callback.total: p95=4579ms (target 1000ms) max=84374ms

The fix routed every site to a purpose-built, independently sized pool in
``core/async_utils`` (``run_blocking_db`` / ``run_blocking_sdk`` /
``run_blocking`` / ``run_blocking_bg``). This lint keeps it that way: pick the
pool that matches the work instead of sharing an unnamed five-thread bucket.

Baseline is ZERO. Escape hatch (use sparingly, with a reason):
``# policy: default-executor-ok(<reason>)`` on the call line or the line above.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    REPO_ROOT / "src" / "nadobro",
    REPO_ROOT / "relay",
    REPO_ROOT / "main.py",
)

OK_MARKER = "policy: default-executor-ok"


def _is_to_thread(node: ast.Call) -> bool:
    f = node.func
    return (
        isinstance(f, ast.Attribute)
        and f.attr == "to_thread"
        and isinstance(f.value, ast.Name)
        and f.value.id == "asyncio"
    )


def _is_default_run_in_executor(node: ast.Call) -> bool:
    """``<anything>.run_in_executor(None, ...)`` — None means the default pool."""
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "run_in_executor"):
        return False
    if not node.args:
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and first.value is None


def _violations(py_file: pathlib.Path) -> list[tuple[int, str]]:
    source = py_file.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_to_thread(node):
            what = "asyncio.to_thread"
        elif _is_default_run_in_executor(node):
            what = "run_in_executor(None, ...)"
        else:
            continue
        context = lines[max(node.lineno - 2, 0) : node.lineno]
        if any(OK_MARKER in line for line in context):
            continue
        out.append((node.lineno, what))
    return out


def _iter_python_files():
    for root in SCAN_ROOTS:
        if root.is_file():
            yield root
            continue
        if not root.exists():
            continue
        for py_file in sorted(root.rglob("*.py")):
            if "__pycache__" in py_file.parts:
                continue
            yield py_file


def test_no_default_executor_dispatch():
    problems = []
    for py_file in _iter_python_files():
        found = _violations(py_file)
        if found:
            rel = py_file.relative_to(REPO_ROOT)
            detail = ", ".join(f"line {ln}: {what}" for ln, what in found)
            problems.append(f"{rel}: {detail}")
    assert not problems, (
        "Blocking work dispatched to the loop's implicit default executor "
        "(5 threads on the 1-CPU production VM — this caused 45s strategy "
        "cycles and an 84s tap p-max in 2026-08). Use run_blocking_db / "
        "run_blocking_sdk / run_blocking / run_blocking_bg from "
        "src/nadobro/core/async_utils.py:\n  " + "\n  ".join(problems)
    )
