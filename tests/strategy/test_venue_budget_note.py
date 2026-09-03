"""AUDIT-DENY-2026-09-02-F3: the start-time venue-budget note.

A ladder that cannot be fully re-quoted within one interval under the venue's
per-wallet execute budget (600 weight/min; place = 20, cancel = 1) is told so at
Start — a WARNING, never an override (levels and interval stay the user's).
"""
from __future__ import annotations

import inspect

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy import bot_runtime  # noqa: E402
from src.nadobro.strategy.bot_runtime import _venue_requote_budget_note  # noqa: E402


def test_a_small_ladder_at_a_normal_interval_gets_no_note():
    assert _venue_requote_budget_note("grid", {"levels": 3, "interval_seconds": 60}) is None


def test_the_fill_anchored_forty_quote_ladder_at_ten_seconds_is_warned():
    note = _venue_requote_budget_note(
        "grid", {"levels": 20, "interval_seconds": 10, "controller_override": "fill_anchored"})
    assert note and "40 resting quotes" in note and "held" in note


def test_mid_counts_both_sides():
    note = _venue_requote_budget_note("mid", {"levels": 15, "interval_seconds": 10})
    assert note and "30 resting quotes" in note


def test_garbage_state_never_raises():
    assert _venue_requote_budget_note("grid", {"levels": "x", "interval_seconds": None}) is None


def test_start_surfaces_the_note_without_overriding_anything():
    src = inspect.getsource(bot_runtime.start_user_bot)
    assert "_venue_requote_budget_note(" in src
    # No silent override (#268 lesson): the note is appended to the message only.
    assert 'state["levels"] =' not in src.split("_venue_requote_budget_note(")[1][:600]
