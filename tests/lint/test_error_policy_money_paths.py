"""CI lint: no new silent broad exception swallows in money paths.

Policy: docs/ERROR_POLICY.md. A "silent broad swallow" is an
``except:`` / ``except Exception:`` / ``except BaseException:`` handler
whose body is only ``pass`` / ``continue`` / ``...``. In modules that
place, cancel, or account for orders, that pattern hides lost-money
failure modes (orders left resting on the venue, unbooked fills).

The engine baseline is zero. Services modules carry a frozen baseline —
counts may go DOWN (lower the number here when you clean a file up),
never up. Deliberate silent degradation must be tagged with
``# policy: degrade-ok(<reason>)`` on the ``except`` line or the line
above it; tagged handlers are not counted.
"""
from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Engine: every file, zero tolerance.
ENGINE_ROOT = REPO_ROOT / "src" / "nadobro" / "engine"

# Services money modules: frozen debt counts as of 2026-06-10 (the date the
# policy landed). Lower these as files are cleaned up. NEVER raise one.
SERVICES_BASELINE: dict[str, int] = {
    # Cleaned 2026-06-10 (warnings added / degrade-ok tags) — keep at zero.
    "src/nadobro/venue/nado_client.py": 0,
    "src/nadobro/trading/trade_service.py": 0,
    "src/nadobro/strategy/bot_runtime.py": 0,
    "src/nadobro/strategy/engine_runtime.py": 0,
    "src/nadobro/trading/copy_service.py": 0,
    "src/nadobro/venue/nado_sync.py": 0,
    # Already clean — keep them that way.
    "src/nadobro/trading/order_intents.py": 0,
    "src/nadobro/strategy/strategy_runtime.py": 0,
    "src/nadobro/core/crypto.py": 0,
    "src/nadobro/trading/execution_queue.py": 0,
    "src/nadobro/strategy/strategy_scheduler.py": 0,
    # Arcus venue library (P2, 02 §9.4 / D14): born clean — keep at zero. Later
    # phases add their own files when they create them.
    "src/nadobro/venue/arcus/types.py": 0,
    "src/nadobro/venue/arcus/errors.py": 0,
    "src/nadobro/venue/arcus/signing.py": 0,
    "src/nadobro/venue/arcus/clock.py": 0,
    "src/nadobro/venue/arcus/budget.py": 0,
    "src/nadobro/venue/arcus/parse.py": 0,
    "src/nadobro/venue/arcus/catalog.py": 0,
    "src/nadobro/venue/arcus/client.py": 0,
    "src/nadobro/venue/arcus/hub.py": 0,
    # Arcus onboarding (P3b, 03 §19.13): born clean — keep at zero.
    "src/nadobro/users/arcus_credentials.py": 0,
    "src/nadobro/users/arcus_link_service.py": 0,
    "src/nadobro/handlers/arcus_wallet_handler.py": 0,
    "src/nadobro/handlers/arcus_ui.py": 0,
    "src/nadobro/handlers/arcus_portfolio_handler.py": 0,
}

DEGRADE_OK_MARKER = "policy: degrade-ok"


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:  # bare except:
        return True
    nodes = (
        list(handler.type.elts)
        if isinstance(handler.type, ast.Tuple)
        else [handler.type]
    )
    for n in nodes:
        if isinstance(n, ast.Name) and n.id in ("Exception", "BaseException"):
            return True
        if isinstance(n, ast.Attribute) and n.attr in ("Exception", "BaseException"):
            return True
    return False


def _is_silent(handler: ast.ExceptHandler) -> bool:
    return all(
        isinstance(stmt, (ast.Pass, ast.Continue))
        or (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        )
        for stmt in handler.body
    )


def _violations(py_file: pathlib.Path) -> list[int]:
    source = py_file.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    out: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not (_is_broad(node) and _is_silent(node)):
            continue
        # Honor the degrade-ok marker on the except line or the line above.
        context = lines[max(node.lineno - 2, 0) : node.lineno]
        if any(DEGRADE_OK_MARKER in line for line in context):
            continue
        out.append(node.lineno)
    return out


def _fail_message(rel: str, found: list[int], allowed: int) -> str:
    return (
        f"{rel}: {len(found)} silent broad exception swallow(s) at lines "
        f"{found} exceeds the baseline of {allowed}.\n"
        f"Per docs/ERROR_POLICY.md: re-raise, log at WARNING+ naming the "
        f"consequence, or tag the handler with "
        f"`# policy: degrade-ok(<reason>)`. Do not raise the baseline."
    )


def test_engine_has_no_silent_broad_swallows():
    problems = []
    for py_file in sorted(ENGINE_ROOT.rglob("*.py")):
        found = _violations(py_file)
        if found:
            rel = str(py_file.relative_to(REPO_ROOT))
            problems.append(_fail_message(rel, found, 0))
    assert not problems, "\n\n".join(problems)


def test_services_money_modules_do_not_regress():
    problems = []
    for rel, allowed in SERVICES_BASELINE.items():
        py_file = REPO_ROOT / rel
        assert py_file.exists(), (
            f"{rel} is in the error-policy baseline but no longer exists — "
            f"update SERVICES_BASELINE (renamed/removed file?)."
        )
        found = _violations(py_file)
        if len(found) > allowed:
            problems.append(_fail_message(rel, found, allowed))
    assert not problems, "\n\n".join(problems)
