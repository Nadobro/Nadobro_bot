"""Arcus key lifecycle: reminders T-14/7/2/1 d, the active->expired transition,
the scheduler jobs and the egress compliance probe (03 §7.13-7.14, §12, §19.6).

No stand-down here: the T-24 h cancel-only stand-down is P5's (03 V-2).
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import logging
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from _stubs import install_test_stubs

install_test_stubs()

import arcus_link_helpers as H  # noqa: E402
from arcus_link_helpers import DAY_MS, NOW_MS, RFC_PUB, UID, FakeDB  # noqa: E402
from src.nadobro.runtime import scheduler as sched  # noqa: E402
from src.nadobro.users import arcus_link_service as ls  # noqa: E402
from src.nadobro.users.arcus_link_service import KeyNotice, KeyNoticeState, decide_key_notices  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HOUR_MS = 3_600_000
DAYS = (14, 7, 2, 1)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_MAINNET_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_KEY_REMINDER_DAYS", raising=False)
    monkeypatch.delenv("ARCUS_KEY_EXPIRY_STOP_HOURS", raising=False)
    ls._reset_for_tests()
    yield
    ls._reset_for_tests()


def run(coro):
    return asyncio.run(coro)


def _fresh(pub=RFC_PUB):
    return KeyNoticeState(pub=pub, sent=frozenset(), expired_notified=False)


def _decide(remaining_ms, state=None):
    return decide_key_notices(
        valid_until_ms=NOW_MS + remaining_ms, now_ms=NOW_MS, state=state or _fresh(), reminder_days=DAYS
    )


# --- pure decision table ------------------------------------------------------------------


def test_decision_table():
    assert _decide(15 * DAY_MS).reminder_days is None
    d = _decide(int(13.9 * DAY_MS))
    assert d.reminder_days == 14 and d.new_state.sent == {14}
    assert _decide(13 * DAY_MS, KeyNoticeState(RFC_PUB, frozenset({14}), False)).reminder_days is None
    assert _decide(int(6.5 * DAY_MS), KeyNoticeState(RFC_PUB, frozenset({14}), False)).reminder_days == 7
    assert _decide(int(1.5 * DAY_MS), KeyNoticeState(RFC_PUB, frozenset({14, 7}), False)).reminder_days == 2
    assert _decide(23 * HOUR_MS, KeyNoticeState(RFC_PUB, frozenset({14, 7, 2}), False)).reminder_days == 1


def test_a_long_outage_sends_one_reminder():
    d = _decide(int(1.5 * DAY_MS))  # down from T-20 d to T-1.5 d
    assert d.reminder_days == 2 and d.new_state.sent == {14, 7, 2} and not d.expired_now
    again = _decide(int(1.4 * DAY_MS), d.new_state)
    assert again.reminder_days is None


def test_expired_announced_once():
    d = _decide(-1)
    assert d.expired_now and d.reminder_days is None
    assert d.new_state.expired_notified and d.new_state.sent == set(DAYS)
    assert not _decide(-DAY_MS, d.new_state).expired_now


def test_no_expiry_key_never_notifies():
    d = decide_key_notices(valid_until_ms=0, now_ms=NOW_MS, state=_fresh(), reminder_days=DAYS)
    assert d.reminder_days is None and not d.expired_now and d.new_state == _fresh()


def test_state_load_resets_on_renewal_and_garbage():
    stored = {"pub": RFC_PUB, "sent": [14, 7], "expired_notified": False}
    assert KeyNoticeState.load(stored, RFC_PUB).sent == {14, 7}
    assert KeyNoticeState.load(stored, "ab" * 32) == _fresh("ab" * 32)  # a renewed key starts fresh
    for garbage in (None, [], "x", {"pub": RFC_PUB, "sent": "14", "expired_notified": False},
                    {"pub": RFC_PUB, "sent": [14], "expired_notified": "no"},
                    {"pub": RFC_PUB, "sent": [True], "expired_notified": False}):
        assert KeyNoticeState.load(garbage, RFC_PUB) == _fresh()
    assert KeyNoticeState(RFC_PUB, frozenset({7, 14}), True).dump() == {
        "pub": RFC_PUB, "sent": [7, 14], "expired_notified": True}


def test_decision_has_no_stand_down_output():
    assert {f.name for f in dataclasses.fields(ls.KeyNoticeDecision)} == {"new_state", "reminder_days", "expired_now"}
    assert not hasattr(ls, "register_arcus_key_expiry_handler")


# --- notice text ---------------------------------------------------------------------------


def _notice(remaining_ms, *, reminder=None, expired=False, name="nadobro-ab12", network="testnet"):
    return KeyNotice(UID, network, RFC_PUB, name, NOW_MS + remaining_ms, reminder, expired)


def test_build_key_notice(monkeypatch):
    from src.nadobro.core.feature_flags import arcus_key_expiry_stop_hours

    key, fmt, buttons = ls.build_key_notice(_notice(23 * HOUR_MS, reminder=1), now_ms=NOW_MS, stop_hours=24.0)
    assert key == ls.TEXT_KR_HOURS and fmt["hours"] == "23" and fmt["stop_hours"] == "24"
    assert fmt["network"] == "TESTNET" and fmt["key_name"] == "nadobro-ab12"
    key, fmt, _ = ls.build_key_notice(_notice(5 * DAY_MS + 3 * HOUR_MS, reminder=7), now_ms=NOW_MS, stop_hours=24.0)
    assert key == ls.TEXT_KR_DAYS and fmt["days"] == "5" and fmt["until"].endswith("UTC")
    key, fmt, _ = ls.build_key_notice(_notice(40 * HOUR_MS, reminder=2), now_ms=NOW_MS, stop_hours=24.0)
    assert key == ls.TEXT_KR_HOURS and fmt["hours"] == "40"  # < 48 h reads in hours
    key, fmt, _ = ls.build_key_notice(_notice(-5, expired=True, name=None, network="mainnet"), now_ms=NOW_MS, stop_hours=24.0)
    assert key == ls.TEXT_KR_EXPIRED and fmt["key_name"] == "—" and fmt["network"] == "MAINNET"
    monkeypatch.setenv("ARCUS_KEY_EXPIRY_STOP_HOURS", "48")
    _, fmt, _ = ls.build_key_notice(_notice(3 * DAY_MS, reminder=7), now_ms=NOW_MS, stop_hours=arcus_key_expiry_stop_hours())
    assert fmt["stop_hours"] == "48"
    assert buttons == ((ls.LABEL_RENEW_KEY, "ax:link:start"), (ls.LABEL_VENUE_KEY, "venue:view"))
    for text_key in (ls.TEXT_KR_DAYS, ls.TEXT_KR_HOURS, ls.TEXT_KR_EXPIRED):
        for key_, fmt_, _ in (ls.build_key_notice(_notice(3 * DAY_MS, reminder=7), now_ms=NOW_MS, stop_hours=24.0),):
            key_.format(**fmt_)


def test_venue_label_matches_p1():
    from src.nadobro.handlers import venue_handler

    assert ls.LABEL_VENUE_KEY == venue_handler.LABEL_VENUE


# --- collect_key_notices ---------------------------------------------------------------------


def _rows(*rows):
    return list(rows)


def test_expired_active_row_flips_and_is_announced_once(monkeypatch):
    events = []
    db = FakeDB(lifecycle=_rows(H.row(until=NOW_MS - 1, status="active")))
    H.install(monkeypatch, db=db)
    ls.register_credential_listener(lambda uid, net, ev: events.append(ev))
    notices = run(ls.collect_key_notices(now_ms=NOW_MS))
    assert [(n.expired_now, n.reminder_days) for n in notices] == [(True, None)]
    assert db.marks == [(UID, "testnet", "expired", {"wipe_secret": False, "api_public_key": RFC_PUB})]
    assert events == ["expired"]
    assert db.saves and db.saves[0][2]["expired_notified"] is True
    assert run(ls.collect_key_notices(now_ms=NOW_MS)) == []  # at most once


def test_reminder_state_is_saved_before_it_is_returned(monkeypatch):
    db = FakeDB(lifecycle=_rows(H.row(until=NOW_MS + 6 * DAY_MS)))
    H.install(monkeypatch, db=db)
    notices = run(ls.collect_key_notices(now_ms=NOW_MS))
    assert [n.reminder_days for n in notices] == [7]
    assert db.saves[-1][2] == {"pub": RFC_PUB, "sent": [7, 14], "expired_notified": False}
    assert not db.marks


def test_no_expiry_and_invalid_rows_are_skipped(monkeypatch):
    db = FakeDB(lifecycle=_rows(H.row(until=0), H.row(uid=UID + 1, until=None)))
    H.install(monkeypatch, db=db)
    assert run(ls.collect_key_notices(now_ms=NOW_MS)) == []
    assert not db.saves


def test_list_error_propagates(monkeypatch):
    H.install(monkeypatch, db=FakeDB(lifecycle=RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        run(ls.collect_key_notices(now_ms=NOW_MS))


def test_per_row_error_skips_only_that_row(monkeypatch, caplog):
    db = FakeDB(lifecycle=_rows(H.row(uid=UID, until=NOW_MS + DAY_MS), H.row(uid=UID + 1, until=NOW_MS + DAY_MS)))
    db.notice_errors = {(UID, "testnet")}
    H.install(monkeypatch, db=db)
    with caplog.at_level(logging.WARNING):
        notices = run(ls.collect_key_notices(now_ms=NOW_MS))
    assert [n.user_id for n in notices] == [UID + 1]
    assert "RuntimeError" in caplog.text


# --- scheduler tick ---------------------------------------------------------------------------


class _Bot:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        if self.error is not None:
            raise self.error


def _real_now(monkeypatch):
    """The scheduler stamps notices with the wall clock; align the service seam."""
    import time as _t

    now = _t.time_ns() // 1_000_000
    monkeypatch.setattr(ls, "_now_ms", lambda: now)
    return now


def _lang(monkeypatch, lang="en"):
    from src.nadobro import i18n

    def fake(uid):
        assert threading.current_thread() is not threading.main_thread()
        return lang

    monkeypatch.setattr(i18n, "get_user_language", fake)


def test_tick_sends_the_expiry_notice(monkeypatch):
    db = FakeDB()
    H.install(monkeypatch, db=db)
    now = _real_now(monkeypatch)
    db.lifecycle = _rows(H.row(until=now - 1))
    _lang(monkeypatch)
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot_app", SimpleNamespace(bot=bot))
    run(sched.tick_arcus_key_lifecycle())
    assert len(bot.sent) == 1
    msg = bot.sent[0]
    assert msg["chat_id"] == UID and "has expired" in msg["text"] and "<code>nadobro-ab12</code>" in msg["text"]
    assert str(msg["parse_mode"]).upper().endswith("HTML")
    buttons = [b for row in msg["reply_markup"].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ["ax:link:start", "venue:view"]
    assert db.marks and db.marks[0][2] == "expired"
    assert RFC_PUB not in msg["text"]


def test_tick_localizes_and_escapes(monkeypatch):
    db = FakeDB()
    H.install(monkeypatch, db=db)
    now = _real_now(monkeypatch)
    db.lifecycle = _rows(H.row(until=now + 5 * DAY_MS + 3 * HOUR_MS, name="<b>x</b>"))
    _lang(monkeypatch, "fr")
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot_app", SimpleNamespace(bot=bot))
    run(sched.tick_arcus_key_lifecycle())
    text = bot.sent[0]["text"]
    assert "Votre clé Arcus TESTNET" in text and "expire dans 5 jours" in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "<b>x</b>" not in text
    labels = [b.text for row in bot.sent[0]["reply_markup"].inline_keyboard for b in row]
    assert labels == ["🔄 Renouveler la clé", "🔁 Plateforme"]


def test_state_saved_before_send_so_a_failed_send_is_not_repeated(monkeypatch):
    from telegram.error import TelegramError

    db = FakeDB()
    H.install(monkeypatch, db=db)
    now = _real_now(monkeypatch)
    db.lifecycle = _rows(H.row(until=now + DAY_MS // 2))
    _lang(monkeypatch)

    class _Blocked(TelegramError):
        pass

    bot = _Bot(error=_Blocked("Forbidden: bot was blocked by the user"))
    monkeypatch.setattr(sched, "_bot_app", SimpleNamespace(bot=bot))
    run(sched.tick_arcus_key_lifecycle())
    run(sched.tick_arcus_key_lifecycle())
    assert len(bot.sent) == 1  # never resent
    assert db.saves


def test_tick_list_failure_sends_nothing(monkeypatch, caplog):
    db = FakeDB(lifecycle=RuntimeError("db down"))
    H.install(monkeypatch, db=db)
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot_app", SimpleNamespace(bot=bot))
    with caplog.at_level(logging.WARNING):
        run(sched.tick_arcus_key_lifecycle())
    assert not bot.sent and not db.saves
    assert "arcus key lifecycle tick failed (RuntimeError)" in caplog.text


def test_tick_without_bot_is_a_noop(monkeypatch):
    db = FakeDB(lifecycle=_rows(H.row(until=NOW_MS - 1)))
    H.install(monkeypatch, db=db)
    monkeypatch.setattr(sched, "_bot_app", None)
    run(sched.tick_arcus_key_lifecycle())
    assert not db.saves and not db.marks


def test_scheduler_arcus_code_never_imports_handlers_or_strategy():
    tree = ast.parse((REPO / "src/nadobro/runtime/scheduler.py").read_text(encoding="utf-8"))

    def imports(node):
        out = []
        for n in ast.walk(node):
            if isinstance(n, ast.Import):
                out += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.append(n.module)
        return out

    # whole file: never handlers (module level or function-local)
    assert not [m for m in imports(tree) if m.startswith("src.nadobro.handlers")]
    arcus_fns = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and "arcus" in n.name
    ]
    assert {f.name for f in arcus_fns} >= {
        "start_arcus_jobs", "tick_arcus_key_lifecycle", "_send_arcus_key_notice", "tick_arcus_egress_compliance"}
    for fn in arcus_fns:
        bad = [m for m in imports(fn) if m.startswith(("src.nadobro.handlers", "src.nadobro.strategy", "src.nadobro.venue"))]
        assert not bad, (fn.name, bad)


# --- start_arcus_jobs ---------------------------------------------------------------------------


class _Sched:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))

    def get_jobs(self):
        return list(self.jobs)


def test_flag_off_and_no_credentials_adds_nothing(monkeypatch):
    fake = _Sched()
    monkeypatch.setattr(sched, "scheduler", fake)
    assert sched.start_arcus_jobs(arcus_state_present=False) is False
    assert fake.get_jobs() == []


def test_flag_off_with_credentials_adds_only_the_lifecycle_job(monkeypatch):
    fake = _Sched()
    monkeypatch.setattr(sched, "scheduler", fake)
    monkeypatch.setenv("ARCUS_KEY_LIFECYCLE_INTERVAL_S", "30")  # clamped to 60
    assert sched.start_arcus_jobs(arcus_state_present=True) is True
    assert [(j[0], j[1], j[2]["id"]) for j in fake.jobs] == [
        (sched.tick_arcus_key_lifecycle, "interval", "arcus_key_lifecycle")]
    assert fake.jobs[0][2]["seconds"] == 60 and fake.jobs[0][2]["max_instances"] == 1


def test_flag_on_adds_lifecycle_and_egress_jobs(monkeypatch):
    fake = _Sched()
    monkeypatch.setattr(sched, "scheduler", fake)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    assert sched.start_arcus_jobs(arcus_state_present=False) is True
    ids = [j[2]["id"] for j in fake.jobs]
    assert ids == ["arcus_key_lifecycle", "arcus_egress_boot", "arcus_egress_check"]
    assert fake.jobs[1][1] == "date" and fake.jobs[2][2]["hours"] == 6
    assert fake.jobs[0][2]["seconds"] == 600


def test_boot_without_arcus_imports_no_arcus_library():
    """03 §19.12 / acceptance §21.1: flag off + no rows -> no venue.arcus import at boot."""
    script = textwrap.dedent(
        """
        import sys
        import src.nadobro.handlers.venue_gate
        import src.nadobro.runtime.scheduler as s
        import src.nadobro.users.venue_service
        pre = [m for m in sys.modules if m.startswith("src.nadobro.venue.arcus")]
        assert not pre, pre
        assert s.start_arcus_jobs(arcus_state_present=False) is False
        post = [m for m in sys.modules if m.startswith("src.nadobro.venue.arcus")]
        assert not post, post
        print("ok")
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("ARCUS_")}
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("ok")


# --- egress posture --------------------------------------------------------------------------------


def test_egress_posture_transitions_log_once(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger=ls.__name__):
        ls.note_egress_posture("testnet", H.compliance(perps=False))
        ls.note_egress_posture("testnet", H.compliance(perps=True, country="US"))
        ls.note_egress_posture("testnet", H.compliance(perps=True, country="US"))
        ls.note_egress_posture("testnet", H.compliance(perps=True, bypassed=True))
    errors = [r for r in caplog.records if "ARCUS_GEO_RESTRICTED" in r.getMessage()]
    oks = [r for r in caplog.records if "egress geo ok" in r.getMessage()]
    assert len(errors) == 1 and errors[0].levelno == logging.ERROR
    assert len(oks) == 1
    assert ls.egress_posture("testnet").blocked is False and ls.egress_posture("mainnet") is None


def test_egress_tick_probes_enabled_networks(monkeypatch):
    env = H.install(monkeypatch, client=H.FakeClient(lane=None))
    env.client.compliance = [H.ok(H.compliance(perps=True))]
    run(sched.tick_arcus_egress_compliance())
    assert env.client.calls == [("compliance", None)]  # keyless, testnet only
    assert ls.egress_posture("testnet").blocked
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    env.client.calls.clear()
    run(sched.tick_arcus_egress_compliance())
    assert env.client.calls == [("compliance", None), ("compliance", None)]


def test_refresh_egress_denied_keeps_the_last_posture(monkeypatch):
    env = H.install(monkeypatch, client=H.FakeClient(lane=None))
    env.client.compliance = [H.ok(H.compliance(perps=True))]
    assert run(ls.refresh_egress_posture("testnet")).blocked
    env.client.compliance = [H.THROTTLED]
    assert run(ls.refresh_egress_posture("testnet")) is None
    assert ls.egress_posture("testnet").blocked
