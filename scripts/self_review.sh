#!/usr/bin/env bash
# Strategy self-review guardrails — run before merging any strategy / SL-TP /
# config change. Part of the loop in docs/self_review/SELF_REVIEW_WORKFLOW.md.
#
# Exit 0 only if the SL/TP & strategy-config invariants hold (known bugs are
# xfail, so they pass until fixed; a fixed bug XPASSes and -> fails CI, your
# cue to delete the xfail marker).
#
# Usage:
#   bash scripts/self_review.sh            # invariants + type-check
#   bash scripts/self_review.sh --full     # also run the engine test suite
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

PY="${PYTHON:-python3}"
rc=0

echo "==> [1/3] SL/TP & strategy-config invariants"
$PY -m pytest tests/engine/test_sltp_invariants.py -q -p no:cacheprovider || rc=1

# ...and again in a PYTEST-ONLY interpreter, mirroring the required CI job.
#
# .github/workflows/self-review.yml runs this same file after `pip install pytest`
# and NOTHING else, deliberately: these invariants are the cheap gate that runs
# fast and everywhere. A dev venv HAS psycopg2, so a new guardrail that reaches the
# DB layer passes the run above and turns that required job red only after push —
# which is exactly what happened on 2026-08-12 (three behavioural guardrails hit
# `ModuleNotFoundError: No module named 'psycopg2'` while the full pytest job,
# which has psycopg2, passed).
#
# This is a real venv rather than static import analysis or a meta-path block: the
# repo satisfies some heavy imports with sys.modules stubs (tests/_stubs.py) and
# defers others into function bodies, so any model of the import graph reports
# failures CI does not have. Only a genuine minimal interpreter is authoritative.
# Built once and cached; same precedent as the mypy step below (when a required CI
# job runs a command the local gate does not mirror, the local gate is the bug).
CI_VENV=".self_review_ci_venv"
if [[ ! -x "$CI_VENV/bin/python" ]]; then
  echo "   building cached pytest-only venv ($CI_VENV) to mirror CI..."
  if ! $PY -m venv "$CI_VENV" >/dev/null 2>&1; then
    echo "   could not create venv; skipping the minimal-env check (CI still enforces it)"
    CI_VENV=""
  else
    "$CI_VENV/bin/python" -m pip -q install --upgrade pip pytest >/dev/null 2>&1 \
      || { echo "   pip install failed (offline?); skipping (CI still enforces it)"; CI_VENV=""; }
  fi
fi
if [[ -n "$CI_VENV" && -x "$CI_VENV/bin/python" ]]; then
  echo "   [1b] same invariants under pytest-only (mirrors self-review.yml)"
  if ! "$CI_VENV/bin/python" -m pytest tests/engine/test_sltp_invariants.py -q -p no:cacheprovider; then
    rc=1
    echo
    echo "   ^ FAILS with pytest alone but passes in the dev venv: a test in"
    echo "     tests/engine/test_sltp_invariants.py now needs a dependency the"
    echo "     required 'SL/TP & config invariants' CI job does not install."
    echo "     Move it next to the harness it needs (tests/services/"
    echo "     test_overlay_wiring.py or test_session_safety_rails.py) and leave a"
    echo "     pointer comment. Do NOT use pytest.importorskip — that silently"
    echo "     skips the guardrail in the very job meant to enforce it."
  fi
fi

echo
echo "==> [2/3] Type-check engine (mypy — BLOCKING, mirrors CI ci.yml)"
if $PY -m mypy --version >/dev/null 2>&1; then
  # CI's ci.yml runs this exact command as a REQUIRED job, so the local gate
  # must fail on it too — treating it as advisory here let a type error reach
  # a PR and block the merge (2026-07-11).
  $PY -m mypy src/nadobro/engine || rc=1
else
  echo "   mypy not installed; skipping (CI still enforces it)"
fi

echo
if [[ "${1:-}" == "--full" ]]; then
  echo "==> [3/3] Engine test suite"
  $PY -m pytest tests/engine -q -p no:cacheprovider || rc=1
else
  echo "==> [3/3] Strategy checklist reminder"
  echo "   Review open items in docs/self_review/SELF_REVIEW_WORKFLOW.md"
  echo "   Run with --full to execute the whole engine suite."
fi

echo
if [[ $rc -eq 0 ]]; then
  echo "SELF-REVIEW PASS — invariants hold. (Open checklist items remain; see workflow doc.)"
else
  echo "SELF-REVIEW FAIL — an invariant regressed or a known bug was unexpectedly fixed."
  echo "If a guardrail XPASSed: delete its @pytest.mark.xfail in tests/engine/test_sltp_invariants.py."
fi
exit $rc
