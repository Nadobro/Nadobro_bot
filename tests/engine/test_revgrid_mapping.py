"""Flag-gated live wiring of the trigger Reverse Grid (`rgrid` -> ReverseGridController).

The rebuild reuses the ENTIRE `rgrid` strategy slot (id, UI, session identity, SL/TP
plumbing); only the engine controller + its config change, and only when
NADO_REVGRID_TRIGGER_ENABLED is set. Default OFF — no live default flip until a
testnet paper run. These tests pin: the flag default, build_controller + map_strategy_config
routing under the flag, the step floor, and live reconfig via reload_config.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.nadobro.strategy import engine_runtime as er
from src.nadobro.engine.controllers.reverse_grid import ReverseGridController
from src.nadobro.engine.controllers.rgrid import RGridController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import RiskLimits

from tests.engine._mock_nado import MockNadoAdapter

PAIR = "BTC-PERP"


def _build(strategy, configs):
    return er.build_controller(
        strategy, user_id=1, configs=configs, orchestrator=ExecutorOrchestrator(),
        adapter=MockNadoAdapter(), inventory=InventoryRepository(), controller_id="RG",
    )


def _revgrid_controller(**cfg):
    configs = {"trading_pair": PAIR, "levels": 2, "step_pct": Decimal("0.001"),
               "order_amount_quote": Decimal("100")}
    configs.update(cfg)
    return ReverseGridController(
        user_id=1, orchestrator=ExecutorOrchestrator(), adapter=MockNadoAdapter(),
        inventory=None, configs=configs,
    )


# ── flag default + routing ─────────────────────────────────────────────

def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("NADO_REVGRID_TRIGGER_ENABLED", raising=False)
    assert er.revgrid_trigger_enabled() is False


def test_build_controller_routes_rgrid_by_flag(monkeypatch):
    off_cfg = {"trading_pair": PAIR, "spread_bid_pct": Decimal("0.001"),
               "spread_ask_pct": Decimal("0.001"), "order_amount_quote": Decimal(10),
               "rgrid_chop_stand_down": False}
    monkeypatch.delenv("NADO_REVGRID_TRIGGER_ENABLED", raising=False)
    assert isinstance(_build("rgrid", off_cfg), RGridController)
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    assert isinstance(_build("rgrid", {"trading_pair": PAIR}), ReverseGridController)


def test_map_strategy_config_routes_rgrid_by_flag(monkeypatch):
    settings = {"levels": 4, "rgrid_spread_bp": 20}
    monkeypatch.delenv("NADO_REVGRID_TRIGGER_ENABLED", raising=False)
    off = er.map_strategy_config("rgrid", settings, Decimal("79000"), product=PAIR, leverage=5)
    assert "spread_ask_pct" in off and off.get("controller_override") == "fill_anchored"

    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    on = er.map_strategy_config("rgrid", settings, Decimal("79000"), product=PAIR, leverage=5)
    assert on["trading_pair"] == PAIR
    assert on["levels"] == 4
    assert "step_pct" in on and "order_amount_quote" in on
    assert on["revgrid_chop_stand_down"] is True     # chop gate ON by default
    assert "spread_ask_pct" not in on                 # the legacy maker keys are gone
    assert on["step_pct"] == Decimal("0.002")         # a 20bp spread is honoured


def test_revgrid_step_is_floored_to_the_viable_zone(monkeypatch):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    # a too-tight 10bp spread is floored UP to the 15bp validated minimum
    on = er.map_strategy_config("rgrid", {"levels": 4, "rgrid_spread_bp": 10},
                                Decimal("79000"), product=PAIR, leverage=5)
    assert on["step_pct"] == er._REVGRID_STEP_FLOOR == Decimal("0.0015")


def test_mapped_config_builds_a_working_controller(monkeypatch):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    cfg = er.map_strategy_config("rgrid", {"levels": 4, "rgrid_spread_bp": 20},
                                 Decimal("79000"), product=PAIR, leverage=5)
    c = _build("rgrid", cfg)
    assert isinstance(c, ReverseGridController)
    assert c.trading_pair == PAIR and c.levels == 4
    assert c.step_pct == Decimal("0.002")
    assert c.chop_stand_down is True


# ── live reconfig ──────────────────────────────────────────────────────

def test_reload_config_re_reads_geometry_but_not_runtime_state():
    c = _revgrid_controller(levels=2, step_pct=Decimal("0.001"))
    assert c.levels == 2
    # simulate an OPEN position's runtime state
    c._anchor = Decimal("100")
    c._pos_base = Decimal("1")
    c._stop_digest = "stp-1"
    # edit settings and reload
    c.configs = dict(c.configs)
    c.configs["levels"] = 5
    c.configs["step_pct"] = Decimal("0.003")
    c.reload_config()
    assert c.levels == 5 and c.step_pct == Decimal("0.003")
    # the open position + its stop are untouched
    assert c._anchor == Decimal("100") and c._pos_base == Decimal("1")
    assert c._stop_digest == "stp-1"


def test_apply_live_update_routes_revgrid_to_reload_config():
    c = _revgrid_controller(levels=2)
    c._anchor = Decimal("100")             # runtime state
    new_cfg = dict(c.configs)
    new_cfg["levels"] = 6
    asyncio.run(er._apply_live_controller_update(
        "rgrid", c, ExecutorOrchestrator(), new_cfg, RiskLimits(), Decimal("100"),
    ))
    assert c.levels == 6                    # reloaded
    assert c._anchor == Decimal("100")      # runtime state preserved
