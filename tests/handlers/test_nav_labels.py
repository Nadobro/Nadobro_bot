"""Navigation labels: pagination must never wear a hierarchical-back verb.

The audit's F-04. Trade history shipped ``⬅ Back`` as its *previous page*
control, sitting beside ``Next ➡``, while ``◀ Back`` everywhere else in the bot
means "go up one level". A user on page 2 taps what reads as "leave this
screen" and instead moves one page. Performance had the identical bug.

The formatter/keyboard snapshots do not cover these modules — they build their
keyboards inline rather than through ``keyboards.py`` — so this suite is their
guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import ui  # noqa: E402
from src.nadobro.handlers.orders_view import render_orders_view  # noqa: E402
from src.nadobro.handlers.positions_view import render_positions_view  # noqa: E402

HANDLERS_DIR = Path(__file__).resolve().parents[2] / "src" / "nadobro" / "handlers"


def _order(idx: int) -> dict:
    return {
        "product_id": 1,
        "product_name": "BTC",
        "side": "LONG",
        "amount": "1",
        "price": str(90000 + idx),
        "digest": f"0x{idx:04x}",
        "created_at": f"2026-08-{10 + idx:02d}T00:00:00+00:00",
    }


def _snapshot(order_count: int) -> dict:
    return {
        "user_id": 42,
        "network": "testnet",
        "last_sync": "2026-01-01T00:00:00+00:00",
        "equity": {"spot": "1000", "cross": "500", "isolated": "0", "total": "1500"},
        "positions": [],
        "open_orders": [_order(i) for i in range(order_count)],
        "matches": [],
        "stats": {},
    }


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# --------------------------------------------------------------------------
# the invariant itself
# --------------------------------------------------------------------------


def test_pagination_and_hierarchical_labels_are_disjoint():
    """The two vocabularies must not overlap — that overlap *was* the bug."""
    pagination = {ui.NAV_NEWER, ui.NAV_OLDER, ui.NAV_PREV, ui.NAV_NEXT}
    hierarchical = {ui.NAV_BACK, ui.NAV_HOME, ui.nav_back_to("Portfolio")}
    assert not (pagination & hierarchical)
    # And they must be distinguishable at a glance: different arrow glyphs.
    assert all("‹" in p or "›" in p for p in pagination)
    assert not any("‹" in h or "›" in h for h in hierarchical)


def test_pager_labels_name_their_list():
    """A screen with two pagers needs each one labelled, or arrows are ambiguous."""
    prev, nxt = ui.pager("Positions")
    assert prev == "‹ Positions" and nxt == "Positions ›"
    assert ui.pager("Orders") != ui.pager("Positions")


# --------------------------------------------------------------------------
# rendered surfaces
# --------------------------------------------------------------------------


def test_orders_view_paginates_with_chronological_verbs():
    """Orders sort created_at DESC, so 'previous page' means more recent."""
    snap = _snapshot(8)  # page_size is 6 -> two pages

    first = _labels(render_orders_view(snap, 0)[1])
    assert ui.NAV_OLDER in first, first
    assert ui.NAV_NEWER not in first, "page 0 has nothing newer"

    second = _labels(render_orders_view(snap, 1)[1])
    assert ui.NAV_NEWER in second, second

    # The regression itself: no page control may read as hierarchical back.
    for labels in (first, second):
        assert ui.NAV_BACK not in labels
        assert not any(lbl.startswith("⬅") for lbl in labels)


def test_orders_view_back_button_names_its_destination():
    labels = _labels(render_orders_view(_snapshot(1), 0)[1])
    assert ui.nav_back_to("Portfolio") in labels, labels


def test_positions_view_back_button_is_consistent():
    labels = _labels(render_positions_view(_snapshot(1))[1])
    assert ui.nav_back_to("Portfolio") in labels, labels


# --------------------------------------------------------------------------
# regression guard against the glyph drifting back
# --------------------------------------------------------------------------

_LEFT_ARROW_BUTTON = re.compile(r'InlineKeyboardButton\(\s*"⬅')


def test_no_handler_reintroduces_the_heavy_left_arrow():
    """One left-arrow glyph, not two.

    ``⬅`` and ``◀`` were both in use for the same action, in different weights,
    which is the icon-family inconsistency in miniature. ``◀`` won because it
    is what 41 of the 49 back buttons already used.
    """
    offenders = []
    for path in sorted(HANDLERS_DIR.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _LEFT_ARROW_BUTTON.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "these buttons use ⬅ instead of the house ◀ (or ‹ for pagination):\n"
        + "\n".join(f"  {o}" for o in offenders)
    )
