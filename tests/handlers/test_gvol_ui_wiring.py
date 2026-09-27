"""Vol model (docs/grid_vol_model.md) UI wiring: every new control survives
button -> validator (tapped AND typed, identical bounds) -> mapper key, lands
on the right card tab, and the card copy tells the truth (state, honest R-Grid
disclaimer, D-Grid "Auto-switch ignored" coupling)."""
from __future__ import annotations

import re

import pytest

from src.nadobro.handlers import strategy_handler as sh
from src.nadobro.strategy import engine_runtime as er
from src.nadobro.strategy.strategy_registry import SETTINGS_STRATEGY_DEFAULTS, gvol_field_allowed

_SRC = open(sh.__file__).read()
_MSG_SRC = open(__import__("src.nadobro.handlers.messages", fromlist=["x"]).__file__).read()
_CALLBACKS = set(re.findall(r'callback_data="(strategy:[^"]+)"', _SRC))

NUMERIC = {
    "grid": ["grid_vol_gate", "grid_vol_gate_mult", "grid_vol_spacing", "grid_vol_spacing_k",
             "grid_inv_skew", "grid_inv_cap_hard", "grid_inv_cap_pct"],
    "dgrid": ["dgrid_vol_gate_mult", "dgrid_vol_spacing", "dgrid_vol_spacing_k",
              "dgrid_inv_skew", "dgrid_inv_cap_hard", "dgrid_inv_cap_pct"],
    "rgrid": ["rgrid_vol_arm", "rgrid_vol_compress_mult", "rgrid_vol_expand_mult"],
}
TOGGLES = {"grid_vol_gate", "grid_vol_spacing", "grid_inv_skew", "grid_inv_cap_hard",
           "dgrid_vol_spacing", "dgrid_inv_skew", "dgrid_inv_cap_hard", "rgrid_vol_arm"}
TYPED = [f for fs in NUMERIC.values() for f in fs if f not in TOGGLES]


def _block(src: str, start: str, end: str) -> str:
    return src.split(start, 1)[1].split(end, 1)[0]


def _bounds(src: str, field: str):
    m = re.search(rf'"{field}":\s*\(([^)]+)\)', src)
    assert m, f"{field} has no limits entry"
    return tuple(float(x) for x in m.group(1).split(","))


def test_every_numeric_key_is_whitelisted_bounded_and_typed_where_needed():
    allowed = _block(_SRC, "allowed_numeric_fields = {", "}")
    inputs = _block(_SRC, "allowed_inputs = (", "\n        )\n")
    msg_fields = _block(_MSG_SRC, "supported_fields = (", "\n    )\n")
    for sid, fields in NUMERIC.items():
        for f in fields:
            assert f'"{f}"' in allowed, f
            assert _bounds(_SRC, f) == _bounds(_MSG_SRC, f), f"{f}: button and typed bounds differ"
    for f in TYPED:
        assert f'"{f}"' in inputs, f
        assert f'"{f}"' in msg_fields, f
        assert f'"{f}":' in _block(_SRC, "help_text = {", "\n        }\n"), f"{f} has no help text"


def test_toggles_are_int_fields_on_both_paths():
    sh_ints = _block(_SRC, "int_fields = {", "}")
    msg_ints = _block(_MSG_SRC, "int_fields = {", "}")
    for f in TOGGLES:
        assert f'"{f}"' in sh_ints, f
        assert f'"{f}"' in msg_ints, f
        assert _bounds(_SRC, f) == (0.0, 1.0)


def test_every_button_value_is_inside_its_bounds_and_callbacks_fit_64_bytes():
    for sid, fields in NUMERIC.items():
        for f in fields:
            emitted = {c for c in _CALLBACKS if c.startswith(f"strategy:set:{sid}:{f}:")}
            assert emitted, f"{sid}:{f} has no button"
            lo, hi = _bounds(_SRC, f)
            for cb in emitted:
                v = float(cb.rsplit(":", 1)[1])
                assert lo <= v <= hi, cb
                assert len(cb.encode()) <= 64, cb
            if f in TYPED:
                assert f"strategy:input:{sid}:{f}" in _CALLBACKS, f
    for val in ("vr", "vol"):
        assert f"strategy:set_text:dgrid:dgrid_regime_model:{val}" in _CALLBACKS
    allowed_text = _block(_SRC, "allowed_text = {", "\n        }\n")
    assert '"dgrid_regime_model": {"vr", "vol"}' in allowed_text


def test_cross_strategy_stores_are_rejected():
    assert gvol_field_allowed("grid", "grid_vol_gate")
    assert not gvol_field_allowed("dgrid", "grid_vol_gate")
    assert not gvol_field_allowed("mid", "grid_inv_cap_hard")
    assert gvol_field_allowed("dgrid", "dgrid_inv_skew")
    assert not gvol_field_allowed("grid", "dgrid_inv_skew")
    assert gvol_field_allowed("dgrid", "dgrid_regime_model")
    assert not gvol_field_allowed("grid", "dgrid_regime_model")
    assert gvol_field_allowed("rgrid", "rgrid_vol_arm")
    assert not gvol_field_allowed("dgrid", "rgrid_vol_arm")
    assert gvol_field_allowed("grid", "spread_bp")          # unrelated fields untouched
    # the guard is on all three handler paths + the typed path
    assert _SRC.count("gvol_field_allowed(strategy_id, field)") == 3
    assert "_gvol_ok(strategy, field)" in _MSG_SRC


def test_section_routing():
    for f in NUMERIC["grid"]:
        assert sh._strategy_section_for_field("grid", f) == "vol"
    for f in NUMERIC["dgrid"] + ["dgrid_regime_model"]:
        assert sh._strategy_section_for_field("dgrid", f) == "regime"
    for f in NUMERIC["rgrid"]:
        assert sh._strategy_section_for_field("rgrid", f) == "setup"
    assert ("vol", "🌡 Vol") in sh._strategy_config_sections("grid")


def test_registry_defaults_are_off_and_in_bounds():
    for sid, fields in NUMERIC.items():
        d = SETTINGS_STRATEGY_DEFAULTS[sid]
        for f in fields:
            lo, hi = _bounds(_SRC, f)
            assert lo <= float(d[f]) <= hi, f
            if f in TOGGLES:
                assert d[f] == 0, f
    assert SETTINGS_STRATEGY_DEFAULTS["dgrid"]["dgrid_regime_model"] == "vr"


def test_every_new_button_reaches_the_engine():
    from decimal import Decimal

    cfg = er.map_strategy_config("grid", {"levels": 4, "grid_vol_gate": 1, "grid_inv_skew": 1},
                                 Decimal("79000"), product="BTC-PERP", leverage=5)
    assert cfg["gvol_gate_enabled"] is True and cfg["gvol_skew_enabled"] is True


@pytest.mark.parametrize("conf,needle", [
    ({}, "Gate: *OFF*"),
    ({"grid_vol_gate": 1}, "Gate: *ON*"),
    ({"grid_vol_spacing": 1, "fill_anchored": 1}, "overlay spread scaling off"),
])
def test_grid_vol_tab_copy(conf, needle):
    txt = sh._strategy_config_section_text("grid", conf, "mainnet", "vol")
    assert needle in txt
    assert "Not yet confirmed out\\-of\\-sample" in txt
    assert "expect much lower volume" in txt
    assert "does not enter until volatility is calm" in txt


def test_grid_core_and_risk_tabs_show_the_model():
    core = sh._strategy_config_section_text("grid", {}, "mainnet", "setup")
    assert "Vol model: *off*" in core
    core_on = sh._strategy_config_section_text("grid", {"grid_vol_gate": 1}, "mainnet", "setup")
    assert "gate *ON*" in core_on
    risk = sh._strategy_config_section_text("grid", {"grid_inv_cap_hard": 1}, "mainnet", "risk")
    assert "Hard cap ON" in risk


def test_rgrid_card_is_honest_about_the_vol_arm():
    txt = sh._strategy_config_section_text("rgrid", {"rgrid_vol_arm": 1}, "mainnet", "setup")
    assert "Vol arm: *On*" in txt
    assert "never gated" in txt
    assert "no setting has made it profitable in testing" in txt
    assert "These thresholds are untested" in txt
    assert "Includes the first arm" in txt


def test_dgrid_cards_disclose_the_auto_switch_coupling():
    core = sh._strategy_config_section_text(
        "dgrid", {"dgrid_regime_model": "vol", "dgrid_trend_follow": 1}, "mainnet", "setup")
    assert "Auto\\-switch ignored under Vol model" in core
    regime = sh._strategy_config_section_text("dgrid", {"dgrid_regime_model": "vol"},
                                              "mainnet", "regime")
    assert "Model: *Vol \\(calm\\-only\\)*" in regime
    assert "Auto\\-switch is ignored" in regime
    vr = sh._strategy_config_section_text("dgrid", {}, "mainnet", "regime")
    assert "Model: *Variance ratio*" in vr and "Trend switch: *off" not in vr
