"""Static guards on the deployment janitor workflow.

The janitor writes to a PRODUCTION environment's deployment records on a
schedule, with no human in the loop. The properties that keep that safe are all
declarative, so they are pinned here rather than trusted to review:

* it can only ever CLOSE a record (``inactive``), never claim a deploy succeeded;
* it cannot race a live deploy (a real Fly deploy of this app takes ~2 minutes,
  and the floor is hours);
* it holds no permission beyond the deployment statuses it exists to write.

See .github/workflows/deployment-janitor.yml for why it exists — Fly.io's GitHub
App opens deployments and frequently never closes them, and that callback lives
outside this repository.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = (Path(__file__).resolve().parents[2]
            / ".github" / "workflows" / "deployment-janitor.yml")


@pytest.fixture(scope="module")
def wf() -> dict:
    assert WORKFLOW.exists(), f"{WORKFLOW} is missing"
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def script(wf: dict) -> str:
    steps = wf["jobs"]["close-stale-deployments"]["steps"]
    run = [s["run"] for s in steps if "run" in s]
    assert run, "the janitor job has no run step"
    return "\n".join(run)


def test_the_workflow_parses_and_has_the_expected_shape(wf: dict) -> None:
    assert wf["name"] == "Deployment Janitor"
    # PyYAML parses a bare `on:` key as the boolean True — accept either.
    triggers = wf.get("on", wf.get(True))
    assert "schedule" in triggers, "the janitor must run unattended"
    assert "workflow_dispatch" in triggers, "must be runnable by hand for incidents"


def test_it_holds_no_permission_beyond_deployment_statuses(wf: dict) -> None:
    """A scheduled job with a write token is worth bounding explicitly."""
    perms = wf["permissions"]
    assert perms.get("deployments") == "write"
    assert perms.get("contents", "read") == "read", "the janitor never writes code"
    extra = set(perms) - {"deployments", "contents"}
    assert not extra, f"unexpected permissions granted: {sorted(extra)}"


def test_it_can_only_close_a_deployment_never_resurrect_one(script: str) -> None:
    """``inactive`` is the ONLY state it may post.

    Posting ``success`` would assert something about production this job cannot
    know — whether the Fly deploy behind the abandoned record actually finished.
    """
    states = set(re.findall(r"-f\s+state=(\w+)", script))
    assert states == {"inactive"}, f"janitor posts states other than inactive: {states}"


def test_it_only_touches_non_terminal_records(script: str) -> None:
    """Terminal records (success / failure / error / inactive) are history and
    must be left alone."""
    assert re.search(r"in_progress\|pending\|queued\|none", script), (
        "the non-terminal state filter is missing — this could rewrite history"
    )
    for terminal in ("success", "failure", "error"):
        assert f"|{terminal}|" not in script and f"{terminal})" not in script, (
            f"the janitor appears to match the terminal state {terminal!r}"
        )


def test_the_staleness_floor_cannot_race_a_live_deploy(wf: dict, script: str) -> None:
    """A real deploy takes ~2 minutes; the floor is hours, and a hand-supplied
    value below 1 hour is refused rather than honoured."""
    triggers = wf.get("on", wf.get(True))
    default = triggers["workflow_dispatch"]["inputs"]["stale_after_hours"]["default"]
    assert int(default) >= 2, f"default threshold {default}h is too tight"
    assert "-lt 1 ]" in script, "no guard rejecting a sub-hour threshold"
    assert "exit 1" in script, "a bad threshold must abort, not fall through"


def test_a_dry_run_changes_nothing(script: str) -> None:
    """Incident response needs a way to see the blast radius first."""
    assert 'DRY_RUN' in script
    body = script.split('if [ "$DRY_RUN" = "true" ]', 1)
    assert len(body) == 2, "no dry-run branch"
    # The dry-run branch must reach `continue` before any POST.
    branch = body[1].split("fi", 1)[0]
    assert "continue" in branch and "-X POST" not in branch, (
        "the dry-run branch can still write"
    )


def test_one_bad_record_does_not_abort_the_sweep(script: str) -> None:
    """A single un-closable deployment must not leave the rest stuck forever."""
    assert "::warning::could not close deployment" in script, (
        "a failed close is not tolerated and logged"
    )
