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
from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
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


def test_chop_guard_toggle_button_is_wired_end_to_end(monkeypatch):
    """User report 2026-08-28: "R-Grid doesn't even place orders." Root cause is
    the chop guard (default ON) standing the trigger ladder down until a trend
    confirms. The config screen now exposes an On/Off toggle; this pins the whole
    path — button callback -> set-action allowlist -> engine mapping -> the flag
    the controller reads — so the toggle cannot silently rot at either end."""
    from src.nadobro.handlers import strategy_handler as sh

    src = open(sh.__file__).read()
    # both button ends exist
    assert 'callback_data="strategy:set:rgrid:rgrid_chop_stand_down:1"' in src
    assert 'callback_data="strategy:set:rgrid:rgrid_chop_stand_down:0"' in src
    # the set-action accepts the field (else the tap is silently dropped)
    assert '"rgrid_chop_stand_down"' in src.split("allowed_numeric_fields = {", 1)[1].split("}", 1)[0]
    assert '"rgrid_chop_stand_down": (0, 1)' in src          # bounds present
    # the engine honours OFF: no chop stand-down -> the ladder quotes in every regime
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    off = er.map_strategy_config("rgrid", {"levels": 4, "rgrid_chop_stand_down": 0},
                                 Decimal("79000"), product=PAIR, leverage=5)
    assert off["revgrid_chop_stand_down"] is False


def test_dgrid_auto_switch_default_on_and_wired_end_to_end(monkeypatch):
    """User directive 2026-08-28: D-Grid switches GRID<->RGRID with the regime (via
    the rebuilt trigger ReverseGridController) so it stays in-market. Enabled by
    DEFAULT once the nested-delegate net-floor backtest went green (+207bp trend /
    -107bp chop / fee_leak 0 — see test_dgrid_autoswitch_net_floor). Pin: default ON
    under the trigger flag, routes to the trigger delegate (never the old pyramiding
    RGridController), the toggle exists both ways, and an explicit 0 opts out."""
    from src.nadobro.handlers import strategy_handler as sh

    src = open(sh.__file__).read()
    assert 'callback_data="strategy:set:dgrid:dgrid_trend_follow:1"' in src
    assert 'callback_data="strategy:set:dgrid:dgrid_trend_follow:0"' in src
    assert '"dgrid_trend_follow"' in src.split("allowed_numeric_fields = {", 1)[1].split("}", 1)[0]
    assert '"dgrid_trend_follow": (0, 1)' in src

    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    # default: auto-switch ON, routing to the trigger delegate (not the legacy one)
    default = er.map_strategy_config("dgrid", {"levels": 4}, Decimal("79000"),
                                     product=PAIR, leverage=5)
    assert default["dgrid_trend_follow"] is True
    assert default["trend_uses_trigger"] is True
    # explicit opt-OUT still respected
    off = er.map_strategy_config("dgrid", {"levels": 4, "dgrid_trend_follow": 0},
                                 Decimal("79000"), product=PAIR, leverage=5)
    assert off["dgrid_trend_follow"] is False
    # with the flag OFF, trend-follow stays OFF by default (PHASE-0 guard: the OLD
    # pyramiding delegate must never spawn by default)
    monkeypatch.delenv("NADO_REVGRID_TRIGGER_ENABLED", raising=False)
    flag_off = er.map_strategy_config("dgrid", {"levels": 4}, Decimal("79000"),
                                      product=PAIR, leverage=5)
    assert flag_off["dgrid_trend_follow"] is False and flag_off["trend_uses_trigger"] is False


def test_dgrid_trend_subconfig_follows_the_flag(monkeypatch):
    """D-Grid's trend phase uses the SAME controller as standalone rgrid: legacy when
    the flag is off, the trigger ReverseGridController when on. The `_for_dgrid_trend`
    guard keeps the flag-off path legacy even though it routes through map_strategy_config."""
    # flag OFF -> legacy trend sub-config
    monkeypatch.delenv("NADO_REVGRID_TRIGGER_ENABLED", raising=False)
    dg_off = er.map_strategy_config("dgrid", {"levels": 4}, Decimal("79000"),
                                    product=PAIR, leverage=5)
    trend_off = dg_off["trend_rgrid"]
    assert "spread_ask_pct" in trend_off and "step_pct" not in trend_off
    assert dg_off["trend_uses_trigger"] is False

    # flag ON -> trigger trend sub-config, with its OWN chop gate DISABLED (D-Grid's
    # parent classifier already gates trend entry, and the delegate has no candle feed)
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    dg_on = er.map_strategy_config("dgrid", {"levels": 4}, Decimal("79000"),
                                   product=PAIR, leverage=5)
    trend_on = dg_on["trend_rgrid"]
    assert "step_pct" in trend_on and "spread_ask_pct" not in trend_on
    assert dg_on["trend_uses_trigger"] is True
    assert trend_on["revgrid_chop_stand_down"] is False


def test_dgrid_spawns_the_trigger_controller_for_its_trend_phase():
    """With trend_uses_trigger set, D-Grid's trend delegate IS the trigger
    ReverseGridController (not the legacy maker one)."""
    async def body():
        a = MockNadoAdapter(mid=Decimal("100"), venue_held={"P": Decimal(0)})
        trend_cfg = {"trading_pair": "P", "levels": 2, "step_pct": Decimal("0.01"),
                     "order_amount_quote": Decimal("100"), "revgrid_chop_stand_down": False}
        cfg = {"trading_pair": "P", "total_amount_quote": "100", "levels_count": 2,
               "dgrid_trend_follow": 1, "trend_uses_trigger": True, "trend_rgrid": trend_cfg}
        dg = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                   adapter=a, inventory=InventoryRepository(), configs=cfg)
        assert await dg._spawn_trend(Decimal("100")) is True
        assert isinstance(dg._trend, ReverseGridController)
        # the delegate's own chop gate is off, so it armed a ladder immediately
        assert len(a.placed_triggers) > 0

    asyncio.run(body())


def test_dgrid_flip_flattens_the_trigger_trend_delegate_before_dropping_it():
    """The money-critical handoff: when D-Grid flips RGRID->GRID it must FLATTEN the
    trigger delegate's venue position (flatten_now) before nulling it, or the ranging
    ladder inherits a naked position."""
    from src.nadobro.engine.routines import variance_regime

    async def body():
        a = MockNadoAdapter(mid=Decimal("100"), venue_held={"P": Decimal(0)},
                            tick=Decimal("0.01"), lot=Decimal("0.0001"),
                            min_notional=Decimal("1"))
        trend_cfg = {"trading_pair": "P", "levels": 1, "step_pct": Decimal("0.01"),
                     "order_amount_quote": Decimal("100"), "revgrid_chop_stand_down": False}
        cfg = {"trading_pair": "P", "start_price": "98", "end_price": "102",
               "total_amount_quote": "100", "min_spread_between_orders": "0.002",
               "max_open_orders": 4, "step_pct": "0.01", "levels_count": 2,
               "dgrid_trend_follow": 1, "trend_uses_trigger": True, "trend_rgrid": trend_cfg}
        dg = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                   adapter=a, inventory=InventoryRepository(), configs=cfg)
        await dg._spawn_trend(Decimal("100"))
        # open a long in the trend delegate
        a.set_mid(Decimal("101.5"))
        a.cross_triggers(Decimal("101.5"))
        await dg._trend.on_tick()
        assert a.venue_held["P"] > 0
        # flip to GRID — flatten happens BEFORE the delegate is dropped
        await dg._flip_to(variance_regime.GRID, Decimal("101.5"), reason="flip")
        assert a.venue_held["P"] == 0        # the trend position was closed
        assert dg._trend is None             # delegate dropped only after flat

    asyncio.run(body())


def test_dgrid_trend_delegate_is_legacy_when_flag_off():
    async def body():
        a = MockNadoAdapter(mid=Decimal("100"), venue_held={"P": Decimal(0)})
        legacy_trend = {"trading_pair": "P", "spread_bid_pct": Decimal("0.001"),
                        "spread_ask_pct": Decimal("0.001"), "order_amount_quote": Decimal("50"),
                        "rgrid_chop_stand_down": False}
        cfg = {"trading_pair": "P", "total_amount_quote": "100", "levels_count": 2,
               "dgrid_trend_follow": 1, "trend_uses_trigger": False, "trend_rgrid": legacy_trend}
        dg = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                   adapter=a, inventory=InventoryRepository(), configs=cfg)
        assert await dg._spawn_trend(Decimal("100")) is True
        assert isinstance(dg._trend, RGridController)

    asyncio.run(body())


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


# ── 2026-09-16: venue cap, user exits, D-Grid hysteresis sanity ─────────

def test_mapper_caps_the_rungs_per_side_at_the_venue_limit(monkeypatch):
    """Nado allows 25 pending triggers per product; a flat ladder is 2 x levels,
    so 20 levels (prod session 292: 40 rungs asked, 25 placed) map to 12."""
    from src.nadobro.quant.rgrid_sizing import REVGRID_MAX_LEVELS

    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    cfg = er.map_strategy_config("rgrid", {"levels": 20, "rgrid_spread_bp": 5,
                                           "notional_usd": 200, "mm_leverage_override": 40,
                                           "rgrid_stop_loss_pct": 10},
                                 Decimal("110000"), product=PAIR, leverage=49)
    assert cfg["levels"] == REVGRID_MAX_LEVELS == 12
    # and the per-rung size is sized against 12 rungs, not 20: budget $20 over a
    # 12-rung pyramid reaching its own stop (30bp) with the worst-case entry (15bp)
    # and stop (50bp) prints plus the taker round trip -> ~$161 per rung
    assert Decimal("155") < Decimal(str(cfg["order_amount_quote"])) < Decimal("167")
    # the D-Grid trend sub-config gets the same cap
    dg = er.map_strategy_config("dgrid", {"levels": 20}, Decimal("110000"), product=PAIR, leverage=5)
    assert dg["trend_rgrid"]["levels"] == 12


def test_user_stop_and_trail_reach_the_controller_and_auto_derives_from_the_step(monkeypatch):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    auto = er.map_strategy_config("rgrid", {"levels": 4, "rgrid_spread_bp": 20},
                                  Decimal("79000"), product=PAIR, leverage=5)
    # auto: stop = trail = 2 x step (20bp step -> 40bp)
    assert auto["step_pct"] == Decimal("0.002")
    assert auto["stop_pct"] == Decimal("0.004") and auto["trail_arm_pct"] == Decimal("0.004")
    assert auto["trail_giveback_pct"] == auto["trail_arm_pct"]
    c = _build("rgrid", auto)
    assert c.stop_pct == Decimal("0.004") and c.trail_arm_pct == Decimal("0.004")
    # user overrides (percent of price)
    user = er.map_strategy_config("rgrid", {"levels": 4, "rgrid_spread_bp": 20,
                                            "rgrid_stop_pct": 0.5, "rgrid_trail_pct": 1.0},
                                  Decimal("79000"), product=PAIR, leverage=5)
    assert user["stop_pct"] == Decimal("0.005") and user["trail_arm_pct"] == Decimal("0.01")
    c2 = _build("rgrid", user)
    assert c2.stop_pct == Decimal("0.005") and c2.trail_arm_pct == Decimal("0.01")
    assert c2.trail_giveback_pct == Decimal("0.01")
    # 0 = auto (the button writes 0)
    zero = er.map_strategy_config("rgrid", {"levels": 4, "rgrid_spread_bp": 20,
                                            "rgrid_stop_pct": 0, "rgrid_trail_pct": 0},
                                  Decimal("79000"), product=PAIR, leverage=5)
    assert zero["stop_pct"] == Decimal("0.004")


def test_dgrid_range_on_is_clamped_below_trend_on():
    """An inverted hysteresis band (range release ABOVE the trend trigger, which
    two independent buttons allow) is clamped so the classifier keeps a
    well-formed band; a normal band is untouched."""
    inv = er.map_strategy_config("dgrid", {"levels": 4, "dgrid_trend_on_variance_ratio": 1.0,
                                           "dgrid_range_on_variance_ratio": 1.15},
                                 Decimal("79000"), product=PAIR, leverage=5)
    assert inv["dgrid_range_on_vr"] == 1.0 and inv["dgrid_trend_on_vr"] == 1.0
    ok = er.map_strategy_config("dgrid", {"levels": 4, "dgrid_trend_on_variance_ratio": 1.25,
                                          "dgrid_range_on_variance_ratio": 1.15},
                                Decimal("79000"), product=PAIR, leverage=5)
    assert ok["dgrid_range_on_vr"] == 1.15 and ok["dgrid_trend_on_vr"] == 1.25


def test_new_rgrid_and_dgrid_buttons_are_wired_end_to_end():
    """Exits (stop / trail) and D-Grid regime knobs (drift / confirm): button ->
    set allowlist -> limits -> custom input -> mapper key the controller reads."""
    from src.nadobro.handlers import strategy_handler as sh

    src = open(sh.__file__).read()
    allowed = src.split("allowed_numeric_fields = {", 1)[1].split("}", 1)[0]
    inputs = src.split("allowed_inputs = (", 1)[1].split(")", 1)[0]
    for field in ("rgrid_stop_pct", "rgrid_trail_pct", "dgrid_trend_drift_pct", "dgrid_flip_confirm_ticks"):
        assert f'"{field}"' in allowed, field
        assert f'"{field}"' in inputs, field
        assert f'"{field}": (' in src, f"{field} has no limits entry"
    for cb in ("strategy:set:rgrid:rgrid_stop_pct:0", "strategy:set:rgrid:rgrid_trail_pct:0",
               "strategy:input:rgrid:rgrid_stop_pct", "strategy:input:rgrid:rgrid_trail_pct",
               "strategy:set:dgrid:dgrid_trend_drift_pct:0.3", "strategy:set:dgrid:dgrid_flip_confirm_ticks:2",
               "strategy:input:dgrid:dgrid_trend_drift_pct", "strategy:input:dgrid:dgrid_flip_confirm_ticks"):
        assert f'callback_data="{cb}"' in src, cb
    # the dead maker-only controls are gone from the Reverse Grid card
    assert 'strategy:set:rgrid:rgrid_discretion' not in src
    assert 'strategy:set:rgrid:rgrid_reset_threshold_pct' not in src
    # the engine reads the D-Grid knobs
    cfg = er.map_strategy_config("dgrid", {"levels": 4, "dgrid_trend_drift_pct": 0.5,
                                           "dgrid_flip_confirm_ticks": 3},
                                 Decimal("79000"), product=PAIR, leverage=5)
    assert cfg["dgrid_trend_drift_pct"] == 0.5 and cfg["dgrid_flip_confirm_ticks"] == 3
