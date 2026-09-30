"""main.py wiring for Arcus P3b (03 §11.5, §19.12; 10_build_order §3).

main.py cannot be imported in tests (it exits without ENCRYPTION_KEY and loads
.env), so it is checked statically: ``edited_message`` is requested ONLY when
the venue gate is registered, at BOTH ``allowed_updates`` sites; the Arcus jobs
are registered after ``start_scheduler()`` with a boot check that never imports
the Arcus venue library; and — in a fresh interpreter — an Arcus-less boot
(flag off, no Arcus rows) imports no ``src.nadobro.venue.arcus*`` module and
registers no job.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAIN = REPO / "main.py"


def _tree():
    return ast.parse(MAIN.read_text(encoding="utf-8"))


def _fn(name):
    return next(
        n for n in ast.walk(_tree()) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def _allowed_updates_fn():
    node = _fn("_allowed_updates")
    namespace: dict = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace["_allowed_updates"]


def test_allowed_updates_add_edited_messages_only_with_the_gate():
    fn = _allowed_updates_fn()
    assert fn(False) == ["message", "callback_query"]
    assert fn(True) == ["message", "callback_query", "edited_message"]


def test_both_allowed_updates_sites_use_the_helper():
    values = [
        ast.unparse(kw.value)
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "allowed_updates"
    ]
    assert values == ["_allowed_updates(venue_gate_enabled)"] * 2


def test_arcus_jobs_start_after_the_scheduler_with_a_safe_boot_check():
    run_bot = _fn("run_bot")
    src = ast.unparse(run_bot)
    assert src.index("start_scheduler()") < src.index("start_arcus_jobs(arcus_state_present=bool(arcus_state))")
    assert "arcus_state = await run_blocking_db(has_live_arcus_credentials)" in src
    assert "arcus_state = True" in src  # a DB error still registers the reminder job
    imports = [
        (n.module, [a.name for a in n.names]) for n in ast.walk(run_bot) if isinstance(n, ast.ImportFrom)
    ]
    assert ("src.nadobro.users.venue_service", ["has_live_arcus_credentials"]) in imports
    assert ("src.nadobro.runtime.scheduler", ["start_arcus_jobs"]) in imports
    assert not any("arcus_credentials" in (m or "") or "venue.arcus" in (m or "") for m, _ in imports)
    assert not any("arcus_link_service" in (m or "") for m, _ in imports)


def test_the_gate_decision_precedes_the_polling_and_webhook_sites():
    src = ast.unparse(_fn("run_bot"))
    first_site = src.index("_allowed_updates(venue_gate_enabled)")
    assert src.index("venue_gate_enabled = await run_blocking_db(should_register_venue_gate)") < first_site


_SCRIPT = r"""
import json, os, sys
sys.path.insert(0, REPO)
os.environ.pop("ARCUS_ENABLED", None)
os.environ.pop("ARCUS_ALLOWED_USER_IDS", None)
before = {m for m in sys.modules if m.startswith("src.nadobro.venue.arcus")}
from src.nadobro.handlers import venue_gate  # noqa: F401  (the gate + venue_handler)
from src.nadobro.runtime import scheduler
from src.nadobro.users import venue_service  # noqa: F401
from src.nadobro.utils.secret_text import classify_secret_text
jobs_before = len(scheduler.scheduler.get_jobs())
added = scheduler.start_arcus_jobs(arcus_state_present=False)
shape = classify_secret_text("ab" * 32)
after = sorted(m for m in sys.modules if m.startswith("src.nadobro.venue.arcus"))
print("RESULT " + json.dumps({
    "before": sorted(before), "after": after, "added": added,
    "jobs": len(scheduler.scheduler.get_jobs()) - jobs_before, "shape": shape.value,
}))
"""


def test_an_arcus_less_boot_imports_no_arcus_venue_module():
    script = _SCRIPT.replace("REPO)", repr(str(REPO)) + ")", 1)
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("ARCUS_")}
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, f"STDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-2000:]}"
    result = json.loads(lines[-1][len("RESULT "):])
    assert result["before"] == [] and result["after"] == []
    assert result["added"] is False and result["jobs"] == 0
    assert result["shape"] == "hex_key"  # the gate's classifier works without the Arcus library
