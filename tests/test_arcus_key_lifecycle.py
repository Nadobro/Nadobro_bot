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
    until = NOW_MS + 30 * DAY_MS
    stored = {"pub": RFC_PUB, "until": until, "sent": [14, 7], "expired_notified": False}
    assert KeyNoticeState.load(stored, RFC_PUB, until).sent == {14, 7}
    fresh = KeyNoticeState("ab" * 32, frozenset(), False, until)
    assert KeyNoticeState.load(stored, "ab" * 32, until) == fresh  # a renewed key starts fresh
    for garbage in (None, [], "x", {"pub": RFC_PUB, "until": until, "sent": "14", "expired_notified": False},
                    {"pub": RFC_PUB, "until": until, "sent": [14], "expired_notified": "no"},
                    {"pub": RFC_PUB, "until": until, "sent": [True], "expired_notified": False},
                    {"pub": RFC_PUB, "until": True, "sent": [14], "expired_notified": False},
                    {"pub": RFC_PUB, "until": str(until), "sent": [14], "expired_notified": False},
                    {"pub": RFC_PUB, "sent": [14], "expired_notified": False}):  # no "until": more, never fewer
        assert KeyNoticeState.load(garbage, RFC_PUB, until) == KeyNoticeState(RFC_PUB, frozenset(), False, until)
    assert KeyNoticeState(RFC_PUB, frozenset({7, 14}), True, until).dump() == {
        "pub": RFC_PUB, "until": until, "sent": [7, 14], "expired_notified": True}


def test_a_rewritten_valid_until_of_the_same_key_starts_fresh():
    # R2-3: docs create-api-key "Public-key ownership": re-registering a key the caller
    # already owns (renew / rewrite validUntil) is allowed — same pub, new validity.
    first = NOW_MS + 10 * DAY_MS
    state = KeyNoticeState.load(None, RFC_PUB, first)
    for remaining in (13 * DAY_MS, 6 * DAY_MS, DAY_MS + HOUR_MS, 20 * HOUR_MS, -1):
        now = first - remaining
        state = decide_key_notices(valid_until_ms=first, now_ms=now, state=state, reminder_days=DAYS).new_state
    assert state.sent == set(DAYS) and state.expired_notified
    stored = state.dump()
    renewed = first + 180 * DAY_MS
    assert KeyNoticeState.load(stored, RFC_PUB, first) == state  # the old validity: nothing again
    fresh = KeyNoticeState.load(stored, RFC_PUB, renewed)
    assert fresh == KeyNoticeState(RFC_PUB, frozenset(), False, renewed)
    got = []
    for remaining in (13 * DAY_MS, 6 * DAY_MS, DAY_MS + HOUR_MS, 20 * HOUR_MS, -1):
        d = decide_key_notices(valid_until_ms=renewed, now_ms=renewed - remaining, state=fresh, reminder_days=DAYS)
        fresh = d.new_state
        got.append((d.reminder_days, d.expired_now))
    assert got == [(14, False), (7, False), (2, False), (1, False), (None, True)]


def test_collect_sends_reminders_again_after_a_same_key_validity_rewrite(monkeypatch):
    # R2-3 through collect_key_notices: the stored state from the first validity (all
    # thresholds sent, expiry announced) must not silence the rewritten validity.
    first = NOW_MS - DAY_MS
    renewed = NOW_MS + 6 * DAY_MS
    db = FakeDB(lifecycle=_rows(H.row(until=renewed)))
    db.notice_states[(UID, "testnet")] = {
        "pub": RFC_PUB, "until": first, "sent": [1, 2, 7, 14], "expired_notified": True}
    H.install(monkeypatch, db=db)
    notices = run(ls.collect_key_notices(now_ms=NOW_MS))
    assert [(n.reminder_days, n.expired_now) for n in notices] == [(7, False)]
    assert db.saves[-1][2] == {"pub": RFC_PUB, "until": renewed, "sent": [7, 14], "expired_notified": False}


def test_decision_has_no_stand_down_output():
    assert {f.name for f in dataclasses.fields(ls.KeyNoticeDecision)} == {"new_state", "reminder_days", "expired_now"}
    assert not hasattr(ls, "register_arcus_key_expiry_handler")


# --- notice text ---------------------------------------------------------------------------


def _notice(remaining_ms, *, reminder=None, expired=False, name="nadobro-ab12", network="testnet"):
    return KeyNotice(UID, network, RFC_PUB, name, NOW_MS + remaining_ms, reminder, expired)


def test_build_key_notice(monkeypatch):
    from src.nadobro.core.feature_flags import arcus_key_expiry_stop_hours

    def build(notice, **kw):
        return ls.build_key_notice(notice, now_ms=NOW_MS, actionable=True, **kw)

    key, fmt, buttons = build(_notice(23 * HOUR_MS, reminder=1), stop_hours=24.0)
    assert key == ls.TEXT_KR_HOURS and fmt["hours"] == "23" and fmt["stop_hours"] == "24"
    assert fmt["network"] == "TESTNET" and fmt["key_name"] == "nadobro-ab12"
    key, fmt, _ = build(_notice(5 * DAY_MS + 3 * HOUR_MS, reminder=7), stop_hours=24.0)
    assert key == ls.TEXT_KR_DAYS and fmt["days"] == "5" and fmt["until"].endswith("UTC")
    key, fmt, _ = build(_notice(40 * HOUR_MS, reminder=2), stop_hours=24.0)
    assert key == ls.TEXT_KR_HOURS and fmt["hours"] == "40"  # < 48 h reads in hours
    key, fmt, _ = build(_notice(-5, expired=True, name=None, network="mainnet"), stop_hours=24.0)
    assert key == ls.TEXT_KR_EXPIRED and fmt["key_name"] == "—" and fmt["network"] == "MAINNET"
    monkeypatch.setenv("ARCUS_KEY_EXPIRY_STOP_HOURS", "48")
    _, fmt, _ = build(_notice(3 * DAY_MS, reminder=7), stop_hours=arcus_key_expiry_stop_hours())
    assert fmt["stop_hours"] == "48"
    assert buttons == ((ls.LABEL_RENEW_KEY, "ax:link:start"), (ls.LABEL_VENUE_KEY, "venue:view"))
    for text_key in (ls.TEXT_KR_DAYS, ls.TEXT_KR_HOURS, ls.TEXT_KR_EXPIRED):
        for key_, fmt_, _ in (build(_notice(3 * DAY_MS, reminder=7), stop_hours=24.0),):
            key_.format(**fmt_)


def test_build_key_notice_without_a_way_to_renew_asks_for_nothing():
    # SEC-3 / R2-1: no paste instruction and NO buttons (they would reach Nado's
    # catch-all router, which edits the reminder to "Unknown action.").
    for notice, expected in ((_notice(3 * DAY_MS, reminder=7), ls.TEXT_KR_PAUSED),
                             (_notice(20 * HOUR_MS, reminder=1), ls.TEXT_KR_PAUSED),
                             (_notice(-5, expired=True), ls.TEXT_KR_EXPIRED_PAUSED)):
        key, fmt, buttons = ls.build_key_notice(notice, now_ms=NOW_MS, stop_hours=24.0, actionable=False)
        assert key == expected and buttons == ()
        text = key.format(**fmt)
        assert "paste it" not in text.lower() and "Paste a new key" not in text and "Arcus wallet" not in text
        assert "nothing to paste" in text and "<code>nadobro-ab12</code>" in text


def test_renewal_reachable_follows_the_cohort_and_the_mainnet_flag(monkeypatch):
    assert ls.renewal_reachable(UID, "testnet") is False  # flag off
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))
    assert ls.renewal_reachable(UID, "testnet") is True
    assert ls.renewal_reachable(UID + 1, "testnet") is False  # outside the cohort
    assert ls.renewal_reachable(UID, "mainnet") is False  # mainnet closed
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    assert ls.renewal_reachable(UID, "mainnet") is True


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
    assert db.saves[-1][2] == {
        "pub": RFC_PUB, "until": NOW_MS + 6 * DAY_MS, "sent": [7, 14], "expired_notified": False}
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


def _venue_ui(monkeypatch, on=True):
    """The boot state where the reminder may carry buttons: gate registered + the user
    may renew (SEC-3 / R2-1)."""
    monkeypatch.setattr(sched, "_arcus_venue_ui", on)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))


def test_tick_sends_the_expiry_notice(monkeypatch):
    db = FakeDB()
    H.install(monkeypatch, db=db)
    _venue_ui(monkeypatch)
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
    _venue_ui(monkeypatch)
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


def _paused_setup(monkeypatch, *, until_offset, network="testnet", lang="en"):
    db = FakeDB()
    H.install(monkeypatch, db=db)
    now = _real_now(monkeypatch)
    db.lifecycle = _rows(H.row(until=now + until_offset, network=network))
    _lang(monkeypatch, lang)
    bot = _Bot()
    monkeypatch.setattr(sched, "_bot_app", SimpleNamespace(bot=bot))
    return db, bot


def _boot(monkeypatch, *, venue_gate):
    """start_arcus_jobs as main.py calls it (the scheduler itself is faked)."""
    monkeypatch.setattr(sched, "scheduler", _Sched())
    monkeypatch.setattr(sched, "_arcus_venue_ui", not venue_gate)  # prove start_arcus_jobs sets it
    assert sched.start_arcus_jobs(arcus_state_present=True, venue_ui=venue_gate) is True


@pytest.mark.parametrize("expired", [False, True])
def test_flag_off_rollback_sends_the_notice_without_buttons_or_paste_instructions(monkeypatch, expired):
    # SEC-3 / R2-1 boot matrix: ARCUS_ENABLED off, an Arcus credential exists, nobody on
    # the Arcus view -> the lifecycle job runs but the venue gate (and /venue, venue:/ax:
    # callbacks) is NOT registered. The notice still reaches the user, asks for nothing
    # and carries no button that Nado's catch-all router would turn into "Unknown action.".
    db, bot = _paused_setup(monkeypatch, until_offset=-1 if expired else 5 * DAY_MS)
    _boot(monkeypatch, venue_gate=False)
    run(sched.tick_arcus_key_lifecycle())
    [msg] = bot.sent
    assert msg["reply_markup"] is None
    assert "nothing to paste" in msg["text"] and "Arcus wallet" not in msg["text"]
    assert ("has expired" in msg["text"]) is expired
    assert db.saves  # at most once, as for the actionable notice
    if expired:
        assert db.marks and db.marks[0][2] == "expired"  # the transition still happens


def test_gate_registered_but_user_outside_the_cohort_gets_no_buttons(monkeypatch):
    _db, bot = _paused_setup(monkeypatch, until_offset=5 * DAY_MS)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID + 1))
    _boot(monkeypatch, venue_gate=True)
    run(sched.tick_arcus_key_lifecycle())
    [msg] = bot.sent
    assert msg["reply_markup"] is None and "nothing to paste" in msg["text"]


def test_a_closed_mainnet_key_gets_no_buttons(monkeypatch):
    _db, bot = _paused_setup(monkeypatch, until_offset=5 * DAY_MS, network="mainnet")
    _venue_ui(monkeypatch)
    run(sched.tick_arcus_key_lifecycle())
    [msg] = bot.sent
    assert msg["reply_markup"] is None and "MAINNET" in msg["text"]


def test_gate_registered_and_renewable_keeps_the_buttons(monkeypatch):
    _db, bot = _paused_setup(monkeypatch, until_offset=5 * DAY_MS)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))
    _boot(monkeypatch, venue_gate=True)
    run(sched.tick_arcus_key_lifecycle())
    [msg] = bot.sent
    assert [b.callback_data for row in msg["reply_markup"].inline_keyboard for b in row] == [
        "ax:link:start", "venue:view"]
    assert "Arcus wallet" in msg["text"]


def test_paused_notice_is_localized(monkeypatch):
    _db, bot = _paused_setup(monkeypatch, until_offset=5 * DAY_MS, lang="ru")
    _boot(monkeypatch, venue_gate=False)
    run(sched.tick_arcus_key_lifecycle())
    assert "вставлять сюда ничего не нужно" in bot.sent[0]["text"]


def test_start_arcus_jobs_defaults_to_no_venue_ui(monkeypatch):
    monkeypatch.setattr(sched, "scheduler", _Sched())
    monkeypatch.setattr(sched, "_arcus_venue_ui", True)
    sched.start_arcus_jobs(arcus_state_present=True)
    assert sched._arcus_venue_ui is False  # fail-closed: no buttons unless told otherwise


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
