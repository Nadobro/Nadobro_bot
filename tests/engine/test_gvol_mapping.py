"""Vol model (docs/grid_vol_model.md): registry -> mapper -> controller wiring,
live refresh, the D-Grid trend sub-config, and the baseline-provider injection.

With default settings every ``gvol_*`` feature maps OFF and every pre-existing
engine key is unchanged; each user key reaches the controller attribute it
drives; a live edit re-reads the switches without a rebuild.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tests.engine._mock_nado import MockNadoAdapter
from tests.engine.test_gvol_controllers import GVOL_ALL_OFF, RGRID_ALL_OFF

from src.nadobro.engine.controllers.controller_base import VolModelConfig
from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController
from src.nadobro.engine.controllers.grid_trading import GridController
from src.nadobro.engine.controllers.reverse_grid import ReverseGridController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import RiskLimits
from src.nadobro.strategy import engine_runtime as er
from src.nadobro.strategy.strategy_registry import (
    RUNTIME_STRATEGY_DEFAULTS,
    SETTINGS_STRATEGY_DEFAULTS,
)

PAIR = "BTC-PERP"
MID = Decimal("79000")

GRID_KEYS = set(GVOL_ALL_OFF)
RGRID_KEYS = set(RGRID_ALL_OFF)


def _map(strategy, settings):
    return er.map_strategy_config(strategy, dict(settings), MID, product=PAIR, leverage=5)


@pytest.mark.parametrize("strategy,settings,keys", [
    ("grid", {"levels": 4}, GRID_KEYS),
    ("grid", {"levels": 4, "fill_anchored": 1}, GRID_KEYS),
    ("dgrid", {"levels": 4}, GRID_KEYS),
    ("rgrid", {"levels": 4}, RGRID_KEYS),
])
def test_defaults_map_every_feature_off(strategy, settings, keys):
    cfg = _map(strategy, settings)
    assert keys <= set(cfg), keys - set(cfg)
    vc = VolModelConfig.from_configs(cfg)
    assert not vc.any_enabled
    if strategy == "dgrid":
        assert cfg["dgrid_regime_model"] == "vr"


@pytest.mark.parametrize("strategy,settings", [
    ("grid", {"levels": 4}),
    ("grid", {"levels": 4, "fill_anchored": 1}),
    ("dgrid", {"levels": 4}),
    ("rgrid", {"levels": 4}),
])
def test_registry_defaults_do_not_change_any_pre_existing_engine_key(strategy, settings):
    """A user who never touched the Vol settings (registry defaults stored) gets
    exactly the config of a user whose settings predate the keys, plus inert
    gvol_* keys — nothing else moves."""
    plain = _map(strategy, settings)
    with_defaults = _map(strategy, {**SETTINGS_STRATEGY_DEFAULTS[strategy], **settings})
    bare_defaults = _map(strategy, {k: v for k, v in SETTINGS_STRATEGY_DEFAULTS[strategy].items()
                                    if not (k.startswith(("grid_vol_", "grid_inv_", "dgrid_vol_",
                                                           "dgrid_inv_", "rgrid_vol_"))
                                            or k == "dgrid_regime_model")} | settings)

    def strip(c):
        return {k: v for k, v in c.items()
                if not k.startswith("gvol_") and k != "dgrid_regime_model"
                and not callable(v)}
    assert strip(with_defaults) == strip(bare_defaults)
    assert {k for k in plain if k.startswith("gvol_")} == {k for k in with_defaults if k.startswith("gvol_")}


def test_registry_defaults_are_off_and_inside_the_bounds():
    s = SETTINGS_STRATEGY_DEFAULTS
    r = RUNTIME_STRATEGY_DEFAULTS
    for table in (s, r):
        for key in ("grid_vol_gate", "grid_vol_spacing", "grid_inv_skew", "grid_inv_cap_hard"):
            assert table["grid"][key] == 0
        for key in ("dgrid_vol_spacing", "dgrid_inv_skew", "dgrid_inv_cap_hard"):
            assert table["dgrid"][key] == 0
        assert table["dgrid"]["dgrid_regime_model"] == "vr"
        assert table["rgrid"]["rgrid_vol_arm"] == 0
        assert 0.3 <= table["grid"]["grid_vol_gate_mult"] <= 2.0
        assert 0.5 <= table["grid"]["grid_vol_spacing_k"] <= 6.0
        assert 5 <= table["grid"]["grid_inv_cap_pct"] <= 100
        assert 0.3 <= table["rgrid"]["rgrid_vol_compress_mult"] <= 1.5
        assert 0.8 <= table["rgrid"]["rgrid_vol_expand_mult"] <= 4.0


def test_user_keys_reach_the_engine_keys():
    cfg = _map("grid", {"levels": 4, "grid_vol_gate": 1, "grid_vol_gate_mult": 0.91,
                        "grid_vol_spacing": 1, "grid_vol_spacing_k": 3, "grid_inv_skew": 1,
                        "grid_inv_cap_hard": 1, "grid_inv_cap_pct": 20,
                        "min_spread_bp": 5, "max_spread_bp": 30})
    vc = VolModelConfig.from_configs(cfg)
    assert vc.gate_enabled and vc.gate_mult == pytest.approx(0.91)
    assert vc.spacing_enabled and vc.spacing_k == pytest.approx(3.0)
    assert vc.spacing_floor_bp == pytest.approx(6.8)
    assert vc.spacing_min_bp == pytest.approx(5.0) and vc.spacing_max_bp == pytest.approx(30.0)
    assert vc.skew_enabled and vc.cap_hard and vc.cap_pct == pytest.approx(20.0)
    fa = _map("grid", {"levels": 4, "fill_anchored": 1, "grid_vol_spacing": 1})
    assert VolModelConfig.from_configs(fa).spacing_floor_bp == pytest.approx(6.0)
    # Out-of-band stored values are clamped to the validator bounds.
    wild = _map("grid", {"levels": 4, "grid_vol_gate_mult": 9, "grid_inv_cap_pct": 0.1})
    assert wild["gvol_gate_mult"] == 2.0 and wild["gvol_cap_pct"] == 5.0


def test_dgrid_regime_model_maps_to_the_gate_and_trend_follow_effective():
    cfg = _map("dgrid", {"levels": 4, "dgrid_regime_model": "vol", "dgrid_trend_follow": 1,
                         "dgrid_vol_spacing": 1, "dgrid_min_spread_bp": 3,
                         "dgrid_max_spread_bp": 40})
    assert cfg["dgrid_regime_model"] == "vol" and cfg["gvol_gate_enabled"] is True
    assert cfg["dgrid_trend_follow"] is True          # the user's switch is NOT rewritten
    assert cfg["gvol_spacing_min_bp"] == 3.0 and cfg["gvol_spacing_max_bp"] == 40.0
    c = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                              adapter=MockNadoAdapter(), inventory=InventoryRepository(),
                              configs=dict(cfg), controller_id="D")
    assert c.trend_follow_enabled and not c.trend_follow_effective
    assert c.vol_cfg.gate_enabled
    junk = _map("dgrid", {"levels": 4, "dgrid_regime_model": "banana"})
    assert junk["dgrid_regime_model"] == "vr" and junk["gvol_gate_enabled"] is False


def test_dgrid_trend_subconfig_never_carries_gvol_keys(monkeypatch):
    for flag in ("0", "1"):
        monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", flag)
        cfg = _map("dgrid", {"levels": 4, "rgrid_vol_arm": 1, "dgrid_regime_model": "vol"})
        trend = cfg["trend_rgrid"]
        assert not [k for k in trend if k.startswith("gvol_")], (flag, trend.keys())


def test_rgrid_arm_keys_on_both_rgrid_engines(monkeypatch):
    monkeypatch.delenv("NADO_REVGRID_TRIGGER_ENABLED", raising=False)
    legacy = _map("rgrid", {"levels": 4, "rgrid_vol_arm": 1, "rgrid_vol_expand_mult": 1.8})
    assert legacy["gvol_arm_enabled"] is True and legacy["gvol_arm_expand_mult"] == 1.8
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    trig = _map("rgrid", {"levels": 4, "rgrid_vol_arm": 1, "rgrid_vol_compress_mult": 0.82})
    assert trig["gvol_arm_enabled"] is True and trig["gvol_arm_compress_mult"] == 0.82
    c = ReverseGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                              adapter=MockNadoAdapter(), inventory=None, configs=trig)
    assert c.vol_cfg.arm_enabled and c.vol_cfg.arm_compress_mult == pytest.approx(0.82)


def test_baseline_provider_is_excluded_from_the_live_signature():
    assert "gvol_baseline_provider" in er._LIVE_CONFIG_SIGNATURE_EXCLUDE


def _grid_controller(cfg):
    return GridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                          adapter=MockNadoAdapter(), inventory=InventoryRepository(),
                          configs=dict(cfg), controller_id="G")


@pytest.mark.parametrize("kind", ["grid", "fa", "dgrid", "rgrid"])
def test_live_update_refreshes_vol_cfg_without_a_rebuild(kind, monkeypatch):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    if kind == "grid":
        cfg = _map("grid", {"levels": 4})
        c = _grid_controller(cfg)
        on = _map("grid", {"levels": 4, "grid_vol_gate": 1, "grid_inv_cap_hard": 1})
        strategy = "grid"
    elif kind == "fa":
        cfg = _map("grid", {"levels": 4, "fill_anchored": 1})
        c = FillAnchoredQuotingController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                          adapter=MockNadoAdapter(),
                                          inventory=InventoryRepository(), configs=dict(cfg))
        on = _map("grid", {"levels": 4, "fill_anchored": 1, "grid_vol_gate": 1,
                           "grid_inv_cap_hard": 1})
        strategy = "grid"
    elif kind == "dgrid":
        cfg = _map("dgrid", {"levels": 4})
        c = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                  adapter=MockNadoAdapter(), inventory=InventoryRepository(),
                                  configs=dict(cfg), controller_id="D")
        on = _map("dgrid", {"levels": 4, "dgrid_regime_model": "vol", "dgrid_inv_cap_hard": 1})
        strategy = "dgrid"
    else:
        cfg = _map("rgrid", {"levels": 4})
        c = ReverseGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                  adapter=MockNadoAdapter(), inventory=None, configs=dict(cfg))
        on = _map("rgrid", {"levels": 4, "rgrid_vol_arm": 1})
        strategy = "rgrid"
    assert not c.vol_cfg.any_enabled
    asyncio.run(er._apply_live_controller_update(
        strategy, c, ExecutorOrchestrator(), on, RiskLimits(), MID))
    assert c.vol_cfg.any_enabled
    if kind == "dgrid":
        assert c.regime_model == "vol" and not c.trend_follow_effective
    # ... and back OFF.
    asyncio.run(er._apply_live_controller_update(
        strategy, c, ExecutorOrchestrator(), cfg, RiskLimits(), MID))
    assert not c.vol_cfg.any_enabled
    if kind == "dgrid":
        assert c.regime_model == "vr"


def test_toggle_off_clears_spacing_override_skew_and_cap():
    async def body():
        cfg = _map("grid", {"levels": 4})
        c = _grid_controller(cfg)
        c._step_override_bp = 12.0
        c._skew_bp = -2.0
        c.gvol_cap_usd = 100.0
        c._gvol_apply_spacing(MID)
        c._gvol_apply_skew(MID, [])
        await c._gvol_apply_cap(MID, [])
        assert c._step_override_bp is None and c._gvol_recenter_pending
        assert c._skew_bp == 0.0 and c.gvol_cap_usd == 0.0
        assert c.gvol_metrics() == {"gvol_state": ""}
    asyncio.run(body())
