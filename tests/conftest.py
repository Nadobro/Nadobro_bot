"""Pytest env bootstrap — runs before test modules import DB code.

SAFETY: tests must never talk to production. Developer ``.env`` files
hold the production Supabase DSN (``main.py`` loads ``.env`` for local
runs), so this conftest deliberately does NOT load ``.env``. Tests read
``.env.test`` only, and any database DSN whose host is not local is
scrubbed before test modules import, so DB-backed tests skip instead of
writing to a remote database.

To run the DB-backed tests locally:

    docker compose -f compose.postgres-test.yaml up -d
    NADO_TEST_DATABASE_URL=postgresql://nadobro:nadobro@127.0.0.1:5433/nadobro_test \
        .venv/bin/python -m pytest

(or put that DSN in ``.env.test`` as DATABASE_URL). A remote test
database requires the explicit ``NADO_TESTS_ALLOW_REMOTE_DB=1`` opt-in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_TESTS = _ROOT / "tests"
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env.test", override=True)
except Exception:
    pass

_LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _dsn_host(url: str) -> str:
    """Extract the host from a postgres DSN, tolerating unencoded passwords."""
    rest = url.split("://", 1)[-1]
    at_idx = rest.rfind("@")
    if at_idx >= 0:
        rest = rest[at_idx + 1 :]
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if rest.startswith("["):  # bracketed IPv6, e.g. [::1]:5433
        return rest.split("]", 1)[0] + "]"
    return rest.rsplit(":", 1)[0] if ":" in rest else rest


def _scrub_non_local_db_env() -> None:
    # Explicit test DSN always wins.
    test_dsn = (os.environ.get("NADO_TEST_DATABASE_URL") or "").strip()
    if test_dsn:
        os.environ["DATABASE_URL"] = test_dsn

    # The production alias must never reach test code (db.py and several
    # test modules prefer it over DATABASE_URL).
    os.environ.pop("SUPABASE_DATABASE_URL", None)

    if os.environ.get("NADO_TESTS_ALLOW_REMOTE_DB", "").strip().lower() in ("1", "true", "yes", "on"):
        return

    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if dsn and _dsn_host(dsn) not in _LOCAL_DB_HOSTS:
        os.environ.pop("DATABASE_URL", None)
        print(
            "conftest: scrubbed non-local DATABASE_URL — DB tests will skip. "
            "Use NADO_TEST_DATABASE_URL (local) or NADO_TESTS_ALLOW_REMOTE_DB=1.",
            file=sys.stderr,
        )


_scrub_non_local_db_env()

# AUDIT-FIX-IS-1 test support: after the production hardening that refuses to
# use a hardcoded invite-code pepper, tests that don't already provide one
# must opt into the dev pepper (or supply ENCRYPTION_KEY themselves). Setting
# the opt-in env var here keeps the existing unit tests passing without
# weakening production safety — the dev pepper still requires this explicit
# flag, so production deployments without keys still hard-fail.
os.environ.setdefault("NADOBRO_ALLOW_DEV_INVITE_PEPPER", "true")


def pytest_sessionstart(session):
    """Bootstrap the app schema once when a (local) test DB is configured.

    DB-gated tests assume ``init_db()`` has run (bot_state, users, trades,
    ...). With the DSN scrubbing above, any DATABASE_URL surviving to this
    point is a deliberate test database, so creating the idempotent schema
    here is safe — and it is exactly what CI's postgres service needs.
    """
    if not os.environ.get("DATABASE_URL"):
        return
    try:
        from src.nadobro.models.database import init_db

        init_db()
    except Exception as exc:  # pragma: no cover - surface, don't mask
        import warnings

        warnings.warn(f"test-DB schema bootstrap failed: {exc}")


class MemoryPendingCleanupStore:
    """In-memory stand-in for ``strategy/pending_cleanup._STORE`` (bot_state rows)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def put(self, key: str, value: dict) -> None:
        self.rows[key] = dict(value)

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)

    def scan(self, prefix: str) -> list[tuple[str, dict]]:
        return [(k, dict(v)) for k, v in self.rows.items() if k.startswith(prefix)]


@pytest.fixture(autouse=True)
def pending_cleanup_store(monkeypatch):
    """Every stop path records a pending cleanup (``strategy/pending_cleanup.py``)
    in ``bot_state``. Keep each test's records in memory: against CI's real test
    database a record one test leaves behind would otherwise be retried by a
    later test that happens to share its uid/network. Tests that assert on the
    records request this fixture by name."""
    try:
        from src.nadobro.strategy import pending_cleanup
    except Exception:  # the pytest-only self-review venv cannot import the app
        yield None
        return
    store = MemoryPendingCleanupStore()
    monkeypatch.setattr(pending_cleanup, "_STORE", store)
    yield store
