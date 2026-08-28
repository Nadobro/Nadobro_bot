"""Every ladder control on the Mid/Grid cards must survive the whole path:
button callback -> set/set_text validator -> mapped config -> controller attr.

This file exists because both halves of that path have failed independently
before: Mid's ``levels`` was a live BUTTON with no backend consumer (audit
2026-07-30 deleted the button), and ``grid_reset_threshold_pct`` is the mirror
image — fully validated in the backend with no button anywhere. A control is
only "wired" when both ends hold, so these tests assert both ends together.
"""
from __future__ import annotations

import re
from decimal import Decimal

from src.nadobro.handlers import strategy_handler as sh
from src.nadobro.strategy import engine_runtime as er

# Every callback_data string the strategy cards emit.
_SOURCE = open(sh.__file__).read()
_CALLBACKS = set(re.findall(r'callback_data="(strategy:[^"]+)"', _SOURCE))


def _emitted(prefix: str) -> set[str]:
    return {c for c in _CALLBACKS if c.startswith(prefix)}


# --------------------------------------------------------------------------
# The buttons exist
# --------------------------------------------------------------------------
def test_levels_button_exists_for_mid_and_grid():
    """Restored for Mid (a prior audit removed it as dead) and added for Grid."""
    for sid in ("mid", "grid"):
        assert _emitted(f"strategy:set:{sid}:levels:"), f"{sid} has no Levels button"
        assert f"strategy:input:{sid}:levels" in _CALLBACKS, f"{sid} has no custom Levels input"


def test_custom_levels_input_exists_for_every_grid_family_strategy():
    """User report 2026-08-28: D-Grid's config screen offered only fixed level
    presets (3/4/6) with no custom-input button, while grid/rgrid/mid all had one
    — so a D-Grid user could not set an arbitrary level count. Every grid-family
    card must expose BOTH the presets and the ✍️ custom input, and the input the
    button emits must be one the `input` action allowlist accepts (else the tap
    opens a prompt whose reply is silently dropped)."""
    for sid in ("grid", "rgrid", "dgrid", "mid"):
        assert _emitted(f"strategy:set:{sid}:levels:"), f"{sid} has no Levels preset buttons"
        assert f"strategy:input:{sid}:levels" in _CALLBACKS, f"{sid} has no custom Levels input"
    # the field the button names must be routable for dgrid, not just present as a string
    assert '"levels"' in _SOURCE.split("allowed_inputs = (", 1)[1].split(")", 1)[0], \
        "levels missing from the input allowlist — every custom-levels tap is dropped"


def test_size_curve_button_exists_for_mid_and_grid():
    for sid in ("mid", "grid"):
        emitted = _emitted(f"strategy:set_text:{sid}:size_curve:")
        values = {c.rsplit(":", 1)[1] for c in emitted}
        assert values == {"flat", "linear", "geometric"}, (sid, values)


def test_grid_reset_threshold_has_a_button():
    """Validated + prompt-ready in the backend since it shipped, but no button
    existed, so the soft-reset threshold could never be changed from the UI."""
    assert _emitted("strategy:set:grid:grid_reset_threshold_pct:")
    assert "strategy:input:grid:grid_reset_threshold_pct" in _CALLBACKS


# --------------------------------------------------------------------------
# The backend accepts what the buttons send
# --------------------------------------------------------------------------
def test_every_emitted_numeric_value_is_inside_its_own_bounds():
    """A button that sends an out-of-bounds value is silently swallowed by the
    validator — the tap does nothing and the user gets no feedback."""
    limits = {"levels": (1, 20), "grid_reset_threshold_pct": (0.05, 20)}
    for cb in _CALLBACKS:
        parts = cb.split(":")
        if len(parts) != 5 or parts[1] != "set":
            continue
        field, raw = parts[3], parts[4]
        if field not in limits:
            continue
        lo, hi = limits[field]
        assert lo <= float(raw) <= hi, f"{cb} is outside {field} bounds {lo}..{hi}"


def test_size_curve_values_match_the_controller_vocabulary():
    """The UI must not offer a curve the planner would silently treat as flat."""
    from src.nadobro.quant.ladder import CURVES

    emitted = {c.rsplit(":", 1)[1] for c in _emitted("strategy:set_text:mid:size_curve:")}
    assert emitted <= set(CURVES), f"UI offers curves the planner ignores: {emitted - set(CURVES)}"


def test_size_curve_is_rejected_for_strategies_without_a_ladder():
    """rgrid/dgrid/vol have no ladder; accepting the key there would store a
    setting that silently does nothing.

    SELF-AUDIT 2026-08-03: the first assertion here read
    ``assert <expr> or True`` — vacuously true, so it verified nothing at all.
    Both halves of the contract are now asserted for real: the key IS in the
    text allowlist (or no button works), AND the handler guards it by strategy.
    """
    assert '"size_curve": {"flat", "linear", "geometric"}' in _SOURCE, \
        "size_curve missing from the allowed_text allowlist — every tap is dropped"
    assert 'field == "size_curve" and strategy_id not in ("grid", "mid")' in _SOURCE, \
        "no per-strategy guard: a ladderless strategy could store a dead size_curve"
    for sid in ("rgrid", "dgrid", "dn", "vol"):
        assert not _emitted(f"strategy:set_text:{sid}:size_curve:"), sid


# --------------------------------------------------------------------------
# The values reach the controller
# --------------------------------------------------------------------------
def test_levels_and_curve_reach_the_mapped_config():
    for sid, extra in (("mid", {}), ("grid", {"fill_anchored": 1})):
        cfg = er.map_strategy_config(
            sid, {"notional_usd": 400.0, "levels": 4, "size_curve": "geometric", **extra},
            Decimal("400"), product="BTC-PERP", leverage=1,
        )
        assert cfg["ladder_levels"] == 4, sid
        assert cfg["ladder_curve"] == "geometric", sid


def test_mapped_config_drives_the_controller():
    """The last hop: mapped config -> live controller attributes."""
    from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController

    cfg = er.map_strategy_config(
        "grid", {"notional_usd": 400.0, "levels": 4, "size_curve": "linear", "fill_anchored": 1},
        Decimal("400"), product="BTC-PERP", leverage=1,
    )
    c = FillAnchoredQuotingController(
        user_id=1, configs=cfg, orchestrator=object(), adapter=object(),
        inventory=None, controller_id="grid:1:mainnet",
    )
    assert c.ladder_levels == 4
    assert c.ladder_curve == "linear"
    assert c.order_amount_quote == Decimal("400")     # per-SIDE total


def test_live_edit_refreshes_the_ladder_on_a_running_controller():
    """Without this the user's edit is stored, shown on the card, and ignored
    until a full stop/start — the failure mode this codebase keeps hitting."""
    from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController

    old = er.map_strategy_config(
        "grid", {"notional_usd": 400.0, "levels": 1, "fill_anchored": 1},
        Decimal("400"), product="BTC-PERP", leverage=1,
    )
    new = er.map_strategy_config(
        "grid", {"notional_usd": 400.0, "levels": 4, "size_curve": "geometric", "fill_anchored": 1},
        Decimal("400"), product="BTC-PERP", leverage=1,
    )
    c = FillAnchoredQuotingController(
        user_id=1, configs=old, orchestrator=object(), adapter=object(),
        inventory=None, controller_id="grid:1:mainnet",
    )
    assert c.ladder_levels == 1
    er._apply_fill_anchored_controller_config(c, new)
    assert c.ladder_levels == 4
    assert c.ladder_curve == "geometric"


# --------------------------------------------------------------------------
# The card tells the truth
# --------------------------------------------------------------------------
def test_ladder_line_reports_per_level_size_not_the_total():
    """Per-level size is the number the venue minimum acts on, so it is the one
    that has to be visible when a user raises the level count.

    SELF-AUDIT: ``"4" in line`` was near-tautological (the level count appears
    anyway). Assert the SIZE arithmetic, and that the total is never printed as
    if it were the rung size."""
    four = sh._ladder_line({"notional_usd": 400.0, "levels": 4, "mm_leverage_override": 1})
    two = sh._ladder_line({"notional_usd": 400.0, "levels": 2, "mm_leverage_override": 1})
    assert "$100" in four, four            # 400 / 4
    assert "$200" in two, two              # 400 / 2
    assert "$400" not in four, f"printed the side TOTAL as the rung size: {four}"
    assert "Levels: *4*" in four and "Levels: *2*" in two


def test_mid_card_no_longer_claims_one_bid_and_one_ask():
    """The Mid card described the pre-ladder behaviour verbatim."""
    conf = {"notional_usd": 100.0, "levels": 4, "spread_bp": 5.0}
    text = sh._strategy_config_section_text("mid", conf, "mainnet", "setup")
    assert "one bid" not in text.lower()
    assert "Levels" in text


def test_grid_card_shows_the_ladder_and_drops_the_dead_knobs():
    conf = {"notional_usd": 400.0, "levels": 4, "spread_bp": 5.0, "fill_anchored": 1}
    text = sh._strategy_config_section_text("grid", conf, "mainnet", "setup")
    assert "Levels" in text and "Curve" in text
    # The fill-anchored controller reads none of these; the legacy card showed
    # them all, which is what made the Grid card misleading.
    for dead in ("Move to quote", "Close offset", "Quote TTL"):
        assert dead not in text, f"dead knob still on the Grid card: {dead}"


# ==========================================================================
# Reset / take-profit threshold ranges (2026-08-03)
# ==========================================================================
_GRID_RANGE = (0.05, 1.0)     # grid re-anchors on SMALL drifts
_RGRID_RANGE = (0.1, 10.0)    # rgrid follows trends and banks far from entry


def _bounds(field: str) -> tuple:
    """The (lo, hi) the BUTTON path validates against, read from the source so
    the test can't drift from the dict it is asserting on."""
    block = _SOURCE.split("limits = {", 1)[1]
    for line in block.splitlines():
        if line.strip().startswith(f'"{field}"'):
            lo, hi = line.split("(", 1)[1].split(")", 1)[0].split(",")
            return float(lo), float(hi)
    raise AssertionError(f"{field} has no limits entry")


def test_threshold_ranges_match_the_specified_bands():
    assert _bounds("grid_reset_threshold_pct") == _GRID_RANGE
    assert _bounds("rgrid_reset_threshold_pct") == _RGRID_RANGE


def test_button_and_typed_input_paths_agree_on_the_bands():
    """The buttons live in strategy_handler and typed replies are validated in
    messages.py — two separate dicts. When they disagree, a value you can tap is
    rejected when typed (or vice versa)."""
    import re as _re

    msg_src = open(
        __import__("src.nadobro.handlers.messages", fromlist=["x"]).__file__
    ).read()
    for field, expected in (
        ("grid_reset_threshold_pct", _GRID_RANGE),
        ("rgrid_reset_threshold_pct", _RGRID_RANGE),
    ):
        m = _re.search(rf'"{field}":\s*\(([^)]+)\)', msg_src)
        assert m, f"{field} missing from the typed-input limits"
        lo, hi = (float(x) for x in m.group(1).split(","))
        assert (lo, hi) == expected, f"{field}: messages.py says {(lo, hi)}"


def test_every_threshold_button_lands_inside_its_band():
    for sid, field, (lo, hi) in (
        ("grid", "grid_reset_threshold_pct", _GRID_RANGE),
        ("rgrid", "rgrid_reset_threshold_pct", _RGRID_RANGE),
    ):
        emitted = _emitted(f"strategy:set:{sid}:{field}:")
        assert emitted, f"{sid} lost its {field} buttons"
        for cb in emitted:
            v = float(cb.rsplit(":", 1)[1])
            assert lo <= v <= hi, f"{cb} outside {lo}..{hi} — the tap is swallowed"
        vals = {float(c.rsplit(":", 1)[1]) for c in emitted}
        assert min(vals) == lo, f"{sid} offers no button at the {lo} floor"
        assert max(vals) == hi, f"{sid} offers no button at the {hi} ceiling"


def test_registry_defaults_sit_inside_the_new_bands():
    """A default outside the band is unreachable from the UI: the user can never
    return to it after changing the value."""
    from src.nadobro.strategy.strategy_registry import SETTINGS_STRATEGY_DEFAULTS

    grid = SETTINGS_STRATEGY_DEFAULTS["grid"]["grid_reset_threshold_pct"]
    assert _GRID_RANGE[0] <= grid <= _GRID_RANGE[1], grid
    rgrid = SETTINGS_STRATEGY_DEFAULTS["rgrid"]["rgrid_reset_threshold_pct"]
    assert _RGRID_RANGE[0] <= rgrid <= _RGRID_RANGE[1], rgrid


# ==========================================================================
# No third-party product names in the UI
# ==========================================================================
def test_no_tread_branding_in_any_user_facing_string():
    """Card/button/message text must not name another product. Comments and
    docstrings are historical references and are deliberately not scanned."""
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("src").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings:
                low = node.value.lower()
                if "tread" in low and "thread" not in low:
                    offenders.append(f"{path}:{node.lineno}: {node.value[:60]}")
    assert not offenders, "third-party name in user-facing strings:\n" + "\n".join(offenders)


# ==========================================================================
# AUDIT-F7 / F11 — the card must not describe a plan that is not the plan
# ==========================================================================
def test_ladder_line_shows_the_real_curved_sizes_not_a_flat_average():
    """AUDIT-F11: it printed deployed/levels for EVERY curve. On geometric/4 at
    $100 it claimed '4 x $25 each' when the plan is $6.67 … $53.33 — and $6.67
    is precisely the rung the venue-minimum clamp acts on."""
    base = {"notional_usd": 100.0, "levels": 4, "mm_leverage_override": 1}
    flat = sh._ladder_line({**base, "size_curve": "flat"})
    geo = sh._ladder_line({**base, "size_curve": "geometric"})
    assert "$25" in flat, flat
    assert "$25" not in geo, f"geometric still reports the flat average: {geo}"
    assert "$7" in geo and "$53" in geo, geo


def test_ladder_line_surfaces_the_venue_minimum_clamp():
    """A clamp the user cannot see is a silent truncation: they ask for 8 rungs,
    get 1, and nothing on the card says so."""
    line = sh._ladder_line({
        "notional_usd": 100.0, "levels": 8, "mm_leverage_override": 1,
        "size_curve": "geometric", "product": "BTC-PERP",
    })
    assert "Capped at" in line, line
    assert "8" in line and "venue minimum" in line, line


def test_pov_line_admits_when_the_preset_is_inoperative():
    """AUDIT-F7: below the throughput floor every preset yields the same cycle —
    the whole deployed budget — so the preset name alone stated a rate the bot
    is not running. Grid ships $75 margin and Mid $100, so this is the norm."""
    from src.nadobro.strategy.mm_dashboard import _annotate_pov

    meta = {"preset": "passive", "multiplier": 0.01, "duration_minutes": 1.0,
            "cycle_notional_usd": 100.0, "interval_seconds": 45}
    capped = _annotate_pov(dict(meta), 100.0, 1.0, 1_000_000.0)
    assert "inoperative" in capped["pacing_note"], capped["pacing_note"]

    floored = _annotate_pov({**meta, "cycle_notional_usd": 1000.0}, 4900.0, 1.0, 1_000_000.0)
    assert "floored up" in floored["pacing_note"], floored["pacing_note"]

    # A pair deep enough that the raw rate stands on its own: no note at all.
    vol = 1_920_000_000.0
    raw = 0.01 * (vol / 1440.0) * (45.0 / 60.0)
    honest = _annotate_pov({**meta, "multiplier": 0.01, "cycle_notional_usd": raw},
                           10_000_000.0, 1.0, vol)
    assert honest["pacing_note"] == "", honest["pacing_note"]
