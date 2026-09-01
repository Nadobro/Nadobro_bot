"""Golden snapshots of every keyboard's labels, layout and callback wiring.

The companion to ``test_formatter_snapshots.py``. Cards and keyboards ship
together, so freezing one without the other leaves half the surface unguarded.

This is the gate for two later phases specifically:

* Phase 1 rewrites nav labels (six back/home variants collapse to one) and
  splits pagination verbs away from hierarchical back. Those are pure label
  edits — and a label edit that accidentally moves a ``callback_data`` is a
  dead button, which this snapshot makes impossible to merge unnoticed.
* Phase 4 re-weights the home card by row width. Its stated gate is that no
  ``callback_data`` value is added, removed or renamed — only labels, order and
  row widths. ``test_home_card_destinations_are_stable`` enforces exactly that.

Regenerate after an intended change::

    NADO_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest \
        tests/handlers/test_keyboard_snapshots.py
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import keyboards as K  # noqa: E402
from tests.handlers.formatter_fixtures import ALERTS, POSITION_LONG, POSITION_SHORT  # noqa: E402

SNAPSHOT_PATH = Path(__file__).parent / "__snapshots__" / "keyboards.snap.md"
UPDATE = os.environ.get("NADO_UPDATE_SNAPSHOTS") == "1"

# Keyboards that take arguments, with fixtures chosen to exercise the branch a
# user actually lands on. Anything reaching the venue or the DB is left out —
# those belong in the click-path tests, not a layout snapshot.
PARAMETERISED: list[tuple[str, str, tuple, dict]] = [
    ("alert_condition_kb__btc", "alert_condition_kb", ("BTC",), {}),
    ("alert_delete_kb__two", "alert_delete_kb", (ALERTS,), {}),
    ("alert_delete_kb__empty", "alert_delete_kb", ([],), {}),
    ("positions_kb__two", "positions_kb", ([POSITION_LONG, POSITION_SHORT],), {}),
    ("positions_kb__empty", "positions_kb", ([],), {}),
    ("strategy_action_kb__grid", "strategy_action_kb", ("grid",), {}),
    ("trade_card_direction_kb", "trade_card_direction_kb", ("sess-1",), {}),
    ("trade_card_order_type_kb", "trade_card_order_type_kb", ("sess-1",), {}),
    ("trade_card_leverage_kb", "trade_card_leverage_kb", ("sess-1",), {}),
    ("trade_card_confirm_kb", "trade_card_confirm_kb", ("sess-1",), {}),
    ("trade_card_tpsl_kb", "trade_card_tpsl_kb", ("sess-1",), {}),
    ("trade_card_size_kb__btc", "trade_card_size_kb", ("sess-1", "BTC"), {}),
    ("status_overview_kb__running", "compose_status_overview_kb", (), {"is_running": True, "strategy_label": "grid"}),
    ("status_overview_kb__stopped", "compose_status_overview_kb", (), {"is_running": False, "strategy_label": None}),
]


def _zero_arg_builders() -> list[str]:
    names = []
    for name, fn in sorted(vars(K).items()):
        if name.startswith("_") or not callable(fn):
            continue
        if not getattr(fn, "__module__", "").endswith("keyboards"):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        required = [
            p
            for p in sig.parameters.values()
            if p.default is p.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        if not required:
            names.append(name)
    return names


def _render_markup(markup) -> str:
    """Serialise a keyboard as label → destination, one row per line."""
    rows = getattr(markup, "inline_keyboard", None)
    if rows is not None:
        out = []
        for row in rows:
            cells = []
            for btn in row:
                dest = getattr(btn, "callback_data", None)
                if dest is None:
                    url = getattr(btn, "url", None)
                    web_app = getattr(btn, "web_app", None)
                    if url:
                        dest = f"url:{url}"
                    elif web_app is not None:
                        dest = f"web_app:{getattr(web_app, 'url', '')}"
                    else:
                        dest = "<none>"
                cells.append(f"[{btn.text}] → {dest}")
            out.append("  |  ".join(cells))
        return "\n".join(out) or "<no rows>"

    rows = getattr(markup, "keyboard", None)
    if rows is not None:
        out = []
        for row in rows:
            cells = [getattr(b, "text", str(b)) for b in row]
            out.append("  |  ".join(f"[{c}]" for c in cells))
        return f"({_kind(markup)})\n" + ("\n".join(out) or "<no rows>")

    return f"<{_kind(markup)}>"


def _kind(obj) -> str:
    """Class name, normalised across the real telegram lib and ``_stubs``.

    ``install_test_stubs`` reuses real telegram classes if the module was
    already imported and installs ``_``-prefixed stand-ins otherwise, so the
    bare class name depends on test collection order. Strip the prefix to keep
    the snapshot stable either way.
    """
    return type(obj).__name__.lstrip("_")


def _cases() -> list[tuple[str, str, tuple, dict]]:
    cases = [(name, name, (), {}) for name in _zero_arg_builders()]
    cases.extend(PARAMETERISED)
    return sorted(cases, key=lambda c: c[0])


def _render_all() -> str:
    cases = _cases()
    chunks = [
        "# Keyboard snapshots",
        "",
        "Generated by `tests/handlers/test_keyboard_snapshots.py`. Do not hand-edit.",
        "Each cell is `[label] → callback_data`.",
        "",
        f"{len(cases)} keyboards.",
        "",
    ]
    for case_id, fn_name, args, kwargs in cases:
        chunks.append(f"## {case_id}")
        chunks.append(f"`{fn_name}`")
        chunks.append("")
        chunks.append("```")
        chunks.append(_render_markup(getattr(K, fn_name)(*args, **kwargs)))
        chunks.append("```")
        chunks.append("")
    return "\n".join(chunks) + "\n"


def test_no_keyboard_raises():
    failures = []
    for case_id, fn_name, args, kwargs in _cases():
        try:
            getattr(K, fn_name)(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - the assert reports it
            failures.append(f"{case_id} ({fn_name}): {type(exc).__name__}: {exc}")
    assert not failures, "keyboards raised while building:\n" + "\n".join(failures)


def test_home_card_destinations_are_stable():
    """Phase 4 may reorder and re-weight the home card. It may not re-wire it.

    Row width and label are presentation. ``callback_data`` is the contract with
    the router, and changing one here without changing the router is precisely
    how a button goes dead.
    """
    expected = {
        "card:trade:start",
        "desk:view",
        "portfolio:view",
        "nav:strategy_hub",
        "wallet:view",
        "points:view",
        "vault:home",
        "alert:menu",
        "refer:view",
        "settings:view",
        "resources:home",
        "home:mode",
    }
    actual = {
        btn.callback_data
        for row in K.home_card_kb().inline_keyboard
        for btn in row
        if btn.callback_data is not None
    }
    assert actual == expected, (
        "the home card's set of destinations changed.\n"
        f"  added:   {sorted(actual - expected)}\n"
        f"  removed: {sorted(expected - actual)}\n"
        "Re-weighting rows is fine; re-wiring them needs a router change too."
    )


def test_rendered_keyboards_match_snapshot():
    current = _render_all()
    if UPDATE or not SNAPSHOT_PATH.exists():
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(current, encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"snapshot did not exist and was created at {SNAPSHOT_PATH}. Review it and re-run.")
        return

    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if current == expected:
        return

    import difflib

    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            current.splitlines(),
            fromfile="snapshot (committed)",
            tofile="render (current code)",
            lineterm="",
            n=3,
        )
    )
    pytest.fail(
        "keyboards drifted from the committed snapshot.\n\n"
        "If intended, regenerate with:\n"
        "  NADO_UPDATE_SNAPSHOTS=1 .venv/bin/python -m pytest "
        "tests/handlers/test_keyboard_snapshots.py\n"
        "and review the diff before committing.\n\n" + diff
    )
