"""The Reverse Grid card and the trigger engine derive ONE plan (2026-09-16).

The pre-start card sized the rung with the legacy maker geometry (raw spread +
reset threshold) while the flag-routed engine floored the step at 15bp and
ignored the reset: on the 2026-08-30 prod config the card promised $186.57 per
rung where the engine placed $119.62, and it never mentioned that 20 requested
levels become 12 (the venue's 25-pending-trigger cap). Both now call
``engine_runtime.revgrid_plan_from_settings``; this pins the agreement on the
exact prod config and on the defaults.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import strategy_handler as sh  # noqa: E402
from src.nadobro.strategy import engine_runtime as er  # noqa: E402

PROD_292 = {
    "levels": 20, "notional_usd": 200.0, "cycle_notional_usd": 200.0, "rgrid_spread_bp": 5.0,
    "rgrid_stop_loss_pct": 10.0, "mm_leverage_override": 40, "rgrid_reset_threshold_pct": 0.1,
    "leverage": 49.0,
}
DEFAULTS = {"levels": 4, "notional_usd": 100.0, "rgrid_spread_bp": 10.0,
            "rgrid_stop_loss_pct": 0.8, "mm_leverage_override": 5}


@pytest.mark.parametrize("conf,lev", [(PROD_292, 49), (DEFAULTS, 5)])
def test_card_rung_size_equals_the_engine_rung_size(monkeypatch, conf, lev):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    engine = er.map_strategy_config("rgrid", dict(conf), Decimal("110000"), product="BTC-PERP", leverage=lev)
    _margin, card = sh.rgrid_step_plan(conf, float(conf["rgrid_stop_loss_pct"]))
    assert Decimal(str(card.step)) == Decimal(str(engine["order_amount_quote"]))
    plan = sh.rgrid_trigger_plan(conf, float(conf["rgrid_stop_loss_pct"]))
    assert plan.step_pct == engine["step_pct"]
    assert plan.levels == engine["levels"]
    assert plan.stop_pct == engine["stop_pct"] and plan.trail_arm_pct == engine["trail_arm_pct"]


def test_prod_292_card_says_what_the_engine_really_does(monkeypatch):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    plan = sh.rgrid_trigger_plan(PROD_292, 10.0)
    assert plan.levels == 12 and plan.levels_requested == 20 and plan.levels_capped
    assert plan.step_floored and plan.step_pct == Decimal("0.0015")        # 5bp -> 15bp floor
    assert Decimal("155") < plan.rung_quote < Decimal("167")                # not $186.57 nor $119.62
    assert plan.stop_is_auto and plan.trail_is_auto


def test_turbo_preset_writes_the_step_the_trigger_engine_runs():
    turbo = sh._turbo_preset_settings("rgrid", 50.0)
    assert turbo["rgrid_spread_bp"] == 15.0 and turbo["spread_bp"] == 15.0


def test_the_rgrid_levels_button_path_stops_at_twelve():
    """A tapped/typed rgrid level above 12 is rejected (the ceiling the venue's
    pending-trigger cap allows); the shared (1, 20) bound still applies to dgrid."""
    src = open(sh.__file__).read()
    assert 'if field == "levels" and strategy_id == "rgrid":' in src
    assert "hi = min(hi, 12)" in src
    from src.nadobro.handlers import messages as msg
    msrc = open(msg.__file__).read()
    assert 'if field == "levels" and strategy == "rgrid":' in msrc
    assert 'limits["levels"] = (1, 12)' in msrc


def test_rgrid_sections_expose_exits_not_the_dead_soft_reset():
    sections = dict(sh._strategy_config_sections("rgrid"))
    assert "exits" in sections and "reset" not in sections
    assert sh._strategy_section_for_field("rgrid", "rgrid_stop_pct") == "exits"
    assert sh._strategy_section_for_field("rgrid", "rgrid_trail_pct") == "exits"
    assert sh._strategy_section_for_field("dgrid", "dgrid_trend_drift_pct") == "regime"
    assert sh._strategy_section_for_field("dgrid", "dgrid_flip_confirm_ticks") == "regime"


def test_section_texts_render_for_every_rgrid_and_dgrid_tab(monkeypatch):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    for strategy, conf in (("rgrid", PROD_292), ("dgrid", {"levels": 20, "notional_usd": 150.0,
                                                         "dgrid_spread_bp": 8.0, "mm_leverage_override": 40,
                                                         "rgrid_stop_loss_pct": 10.0,
                                                         "dgrid_range_on_variance_ratio": 1.15,
                                                         "dgrid_trend_on_variance_ratio": 1.0})):
        for section, _label in sh._strategy_config_sections(strategy):
            text = sh._strategy_config_section_text(strategy, dict(conf), "mainnet", section)
            assert text and "*" in text
            kb = sh._strategy_config_section_kb(strategy, section, 50)
            assert kb.inline_keyboard
    rg = sh._strategy_config_section_text("rgrid", dict(PROD_292), "mainnet", "setup")
    assert "12 max per side" in rg and "floored" in rg
    dg = sh._strategy_config_section_text("dgrid", {"dgrid_trend_on_variance_ratio": 1.0,
                                                    "dgrid_range_on_variance_ratio": 1.15},
                                          "mainnet", "regime")
    assert "clamps Range down to Trend" in dg


def test_settings_updated_card_echoes_the_strategys_own_keys():
    from src.nadobro.handlers.formatters import fmt_strategy_update

    conf = {"notional_usd": 100.0, "spread_bp": 10.0, "rgrid_spread_bp": 15.0,
            "sl_pct": 0.8, "tp_pct": 1.2, "rgrid_stop_loss_pct": 0.5, "rgrid_take_profit_pct": 2.0}
    # (MarkdownV2 output: the dot is escaped)
    text = fmt_strategy_update("rgrid", "mainnet", conf).replace("\\", "")
    assert "15.0 bp" in text and "SL: 0.50%" in text and "TP: 2.00%" in text
    assert "0.80%" not in text and "1.20%" not in text and "10.0 bp" not in text
    dg = fmt_strategy_update("dgrid", "mainnet", {"dgrid_spread_bp": 8.0, "rgrid_stop_loss_pct": 1.0,
                                                  "rgrid_take_profit_pct": 3.0, "spread_bp": 5.0}).replace("\\", "")
    assert "8.0 bp" in dg and "SL: 1.00%" in dg and "TP: 3.00%" in dg
    grid = fmt_strategy_update("grid", "mainnet", {"spread_bp": 4.0, "sl_pct": 0.5, "tp_pct": 0.6}).replace("\\", "")
    assert "4.0 bp" in grid and "SL: 0.50%" in grid and "TP: 0.60%" in grid
