"""R-Grid at DEFAULT settings must actually place its rungs (audit B1, 2026-09-27).

The plan (``rgrid_sizing.resolve_step_quote``) floors a stop-budget-capped rung to
exactly the venue minimum ($100). The controller then rounded the base DOWN to the
lot, which always lands a hair UNDER $100, and refused every rung — the session read
LIVE with 0 orders and nothing said why. The existing controller tests never saw it
because they run with ``min_notional=1``; these run on the real venue shape
(BTC-PERP: $100 minimum, 0.00005 lot) and the real registry defaults.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.engine.controllers.reverse_grid import ReverseGridController  # noqa: E402
from src.nadobro.engine.types import TradeType  # noqa: E402
from src.nadobro.strategy import engine_runtime as er  # noqa: E402

from tests.engine._mock_nado import MockNadoAdapter  # noqa: E402

PAIR = "BTC-PERP"
MID = Decimal("110000")
LOT = Decimal("0.00005")
MIN_NOTIONAL = Decimal("100")
# strategy_registry "rgrid" defaults (+ a leverage the card offers).
DEFAULTS = {"levels": 4, "notional_usd": 100.0, "rgrid_spread_bp": 10.0,
            "rgrid_stop_loss_pct": 0.8, "mm_leverage_override": 5}


def _adapter():
    return MockNadoAdapter(mid=MID, tick=Decimal("1"), lot=LOT, min_notional=MIN_NOTIONAL,
                           venue_held={PAIR: Decimal(0)})


def _controller(adapter, cfg):
    cfg = dict(cfg)
    cfg.setdefault("trading_pair", PAIR)
    return ReverseGridController(
        user_id=1, orchestrator=object(), adapter=adapter, inventory=None, configs=cfg,
    )


def _engine_cfg(monkeypatch, conf, lev):
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    return er.map_strategy_config("rgrid", dict(conf), MID, product=PAIR, leverage=lev)


@pytest.mark.parametrize("lev", [5, 10, 49])
def test_default_rgrid_plan_is_floored_and_the_controller_still_places_every_rung(monkeypatch, lev):
    conf = dict(DEFAULTS, mm_leverage_override=lev)
    cfg = _engine_cfg(monkeypatch, conf, lev)
    # Precondition (the B1 shape): the stop budget floors the rung at the venue minimum.
    assert Decimal(str(cfg["order_amount_quote"])) == MIN_NOTIONAL

    async def body():
        a = _adapter()
        c = _controller(a, cfg)
        await c.on_tick()
        return a, c

    a, c = asyncio.run(body())
    levels = int(cfg["levels"])
    buys = [o for o in a.placed_triggers if o.side is TradeType.BUY]
    sells = [o for o in a.placed_triggers if o.side is TradeType.SELL]
    assert len(buys) == levels and len(sells) == levels, "R-Grid armed no rungs at default settings"
    for o in a.placed_triggers:
        # Whole lots, at/above the venue minimum at the rung's level AND at its IOC
        # limit price (a SELL rung's limit sits 15bp under its level — the client
        # would otherwise silently grow it by a lot past the size the controller tracks).
        assert (o.amount_base / LOT) == (o.amount_base / LOT).to_integral_value()
        limit_px = o.price * (Decimal(1) - Decimal(str(c.entry_slippage_pct)) / Decimal(100)) \
            if o.side is TradeType.SELL else o.price
        assert o.amount_base * limit_px >= MIN_NOTIONAL
        # ...and never more than one lot above the plan (no silent oversizing).
        assert o.amount_base * o.price < MIN_NOTIONAL + LOT * o.price * Decimal(2)
        assert (o.amount_base - LOT) * min(o.price, limit_px) < MIN_NOTIONAL
    assert c.gate_reason == ""


def test_card_and_engine_agree_on_the_floored_default_rung(monkeypatch):
    from src.nadobro.handlers import strategy_handler as sh

    cfg = _engine_cfg(monkeypatch, DEFAULTS, 5)
    plan = sh.rgrid_trigger_plan(DEFAULTS, float(DEFAULTS["rgrid_stop_loss_pct"]))
    assert plan.sizing.floored
    assert plan.rung_quote == Decimal(str(cfg["order_amount_quote"])) == MIN_NOTIONAL


def test_a_rung_that_cannot_meet_the_venue_minimum_is_refused_visibly():
    """A per-rung notional genuinely below the venue minimum (e.g. a directly-built
    config) is still refused — never silently grown past the approved size — but the
    refusal is VISIBLE: gate telemetry, like the venue_* holds, not 'LIVE, 0 orders'."""
    from src.nadobro.engine.routines.regime_gate import GATE_REASON_HUMAN

    async def body():
        a = _adapter()
        c = _controller(a, {"levels": 2, "step_pct": Decimal("0.0015"),
                            "order_amount_quote": Decimal("60"),
                            "revgrid_chop_stand_down": False})
        await c.on_tick()
        return a, c

    a, c = asyncio.run(body())
    assert a.placed_triggers == []
    assert c.gate_verdict == "PAUSE"
    assert c.gate_reason == "venue_min_notional"
    assert "venue_min_notional" in GATE_REASON_HUMAN


def test_min_notional_hold_clears_once_rungs_place():
    async def body():
        a = _adapter()
        c = _controller(a, {"levels": 1, "step_pct": Decimal("0.0015"),
                            "order_amount_quote": Decimal("60"),
                            "revgrid_chop_stand_down": False})
        await c.on_tick()
        assert c.gate_reason == "venue_min_notional"
        c.configs["order_amount_quote"] = Decimal("150")
        c.reload_config()
        await c.on_tick()
        assert len(a.placed_triggers) == 2
        assert c.gate_verdict == "QUOTE" and c.gate_reason == ""

    asyncio.run(body())


# --------------------------------------------------------------------------- #
# Review follow-ups (2026-09-27): the SELL check happens at the tick-floored     #
# price the client sends; a coarse lot can never balloon a rung; a plan whose   #
# fees alone spend the stop budget holds visibly; D-Grid surfaces the hold.     #
# --------------------------------------------------------------------------- #
def _flat_cfg(**over):
    cfg = {"levels": 4, "step_pct": Decimal("0.0015"), "order_amount_quote": Decimal("100"),
           "revgrid_chop_stand_down": False}
    cfg.update(over)
    return cfg


def test_a_sell_rung_clears_the_minimum_at_the_tick_floored_price_the_client_sends():
    """The client floors a SELL limit to the tick and re-checks the venue minimum
    there, growing the order by a lot (untracked) if it falls short. At this mid
    (BTC, $1 tick) the unfloored check passed by less than one tick of notional, so
    the venue order would have been a lot bigger than the rung the controller books."""
    from decimal import ROUND_DOWN

    mid = Decimal("111613")

    async def body():
        a = MockNadoAdapter(mid=mid, tick=Decimal("1"), lot=LOT, min_notional=MIN_NOTIONAL,
                            venue_held={PAIR: Decimal(0)})
        c = _controller(a, _flat_cfg())
        await c.on_tick()
        return a, c

    a, c = asyncio.run(body())
    sells = [o for o in a.placed_triggers if o.side is TradeType.SELL]
    assert len(sells) == 4
    slip = Decimal(str(c.entry_slippage_pct)) / Decimal(100)
    for o in sells:
        sent_px = (o.price * (Decimal(1) - slip)).to_integral_value(rounding=ROUND_DOWN)
        assert o.amount_base * sent_px >= MIN_NOTIONAL, (o.price, o.amount_base)


def test_a_coarse_lot_never_balloons_a_rung_past_the_round_up_cap():
    """One lot worth ~$33 at BTC: meeting $100 needs 4 lots ($132, +32%) — past the
    10% round-up bound, so the rung is refused and the hold is visible, never placed
    silently a third bigger than the plan."""
    from src.nadobro.quant.rgrid_sizing import REVGRID_RUNG_ROUND_UP_MAX_FRAC

    async def body():
        a = MockNadoAdapter(mid=MID, tick=Decimal("1"), lot=Decimal("0.0003"),
                            min_notional=MIN_NOTIONAL, venue_held={PAIR: Decimal(0)})
        c = _controller(a, _flat_cfg(levels=2))
        await c.on_tick()
        return a, c

    a, c = asyncio.run(body())
    assert REVGRID_RUNG_ROUND_UP_MAX_FRAC == Decimal("0.10")
    assert a.placed_triggers == []
    assert (c.gate_verdict, c.gate_reason) == ("PAUSE", "venue_min_notional")


def test_every_placed_rung_stays_within_the_round_up_cap_of_the_plan():
    from src.nadobro.quant.rgrid_sizing import REVGRID_RUNG_ROUND_UP_MAX_FRAC

    async def body():
        a = _adapter()
        c = _controller(a, _flat_cfg())
        await c.on_tick()
        return a

    a = asyncio.run(body())
    assert len(a.placed_triggers) == 8
    for o in a.placed_triggers:
        assert o.amount_base * o.price <= MIN_NOTIONAL * (Decimal(1) + REVGRID_RUNG_ROUND_UP_MAX_FRAC)


def test_a_plan_whose_fees_alone_spend_the_stop_budget_holds_visibly(monkeypatch):
    """SL 0.3% of $100 = $0.30 budget; the venue-minimum pyramid (4 x $100) costs
    $0.34 in taker fees per round trip — the card says "Stop too tight to trade".
    Before RGRID-B1 it placed nothing by accident; it must not now pay fees into a
    guaranteed session stop: the ladder holds with a named reason, and card and
    engine agree on the verdict."""
    from src.nadobro.engine.routines.regime_gate import GATE_REASON_HUMAN, VENUE_GATE_REASONS
    from src.nadobro.handlers import strategy_handler as sh

    tight = dict(DEFAULTS, rgrid_stop_loss_pct=0.3)
    cfg = _engine_cfg(monkeypatch, tight, 5)
    assert cfg["stop_budget_unfundable"] is True
    assert sh.rgrid_stop_headroom(tight, 0.3)["fees_exceed_budget"] is True
    # Defaults are "Thin", not unfundable: they still trade (RGRID-B1 stays fixed).
    assert _engine_cfg(monkeypatch, DEFAULTS, 5)["stop_budget_unfundable"] is False
    assert sh.rgrid_stop_headroom(DEFAULTS, 0.8)["fees_exceed_budget"] is False

    async def body():
        a = _adapter()
        c = _controller(a, cfg)
        await c.on_tick()
        held = (list(a.placed_triggers), c.gate_verdict, c.gate_reason)
        # A live settings edit that funds the stop re-arms on the next tick.
        c.configs["stop_budget_unfundable"] = False
        c.reload_config()
        await c.on_tick()
        return a, c, held

    a, c, held = asyncio.run(body())
    assert held == ([], "PAUSE", "stop_budget_too_tight")
    assert "stop_budget_too_tight" in GATE_REASON_HUMAN
    assert "stop_budget_too_tight" in VENUE_GATE_REASONS      # no gate-event storm
    assert len(a.placed_triggers) == 2 * int(cfg["levels"])
    assert (c.gate_verdict, c.gate_reason) == ("QUOTE", "")


@pytest.mark.parametrize("trend_over, reason", [
    ({"order_amount_quote": Decimal("60")}, "venue_min_notional"),
    ({"stop_budget_unfundable": True}, "stop_budget_too_tight"),
])
def test_dgrid_surfaces_its_trend_phase_rung_hold_on_its_own_card(trend_over, reason):
    """engine_diag reads only D-Grid's own gate, so a trend phase whose rungs are
    held used to show "Quoting: active" with 0 orders. The hold is mirrored up."""
    from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
    from src.nadobro.engine.inventory import InventoryRepository
    from src.nadobro.engine.orchestrator import ExecutorOrchestrator
    from src.nadobro.engine.routines import variance_regime

    async def body():
        a = _adapter()
        trend_cfg = dict(_flat_cfg(levels=2), trading_pair=PAIR, **trend_over)
        cfg = {"trading_pair": PAIR, "total_amount_quote": "500", "levels_count": 2,
               "dgrid_trend_follow": 1, "trend_uses_trigger": True, "trend_rgrid": trend_cfg}
        dg = DynamicGridController(user_id=1, orchestrator=ExecutorOrchestrator(),
                                   adapter=a, inventory=InventoryRepository(), configs=cfg)
        assert await dg._spawn_trend(MID) is True
        spawned = (dg.gate_verdict, dg.gate_reason)
        await dg._tick_trend_phase(variance_regime.RGRID, MID)
        return a, dg, spawned

    a, dg, spawned = asyncio.run(body())
    assert a.placed_triggers == []
    assert spawned == ("PAUSE", reason)
    assert (dg.gate_verdict, dg.gate_reason) == ("PAUSE", reason)


def test_dgrid_does_not_mirror_a_delegate_regime_reason():
    from types import SimpleNamespace

    from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
    from src.nadobro.engine.inventory import InventoryRepository
    from src.nadobro.engine.orchestrator import ExecutorOrchestrator

    dg = DynamicGridController(
        user_id=1, orchestrator=ExecutorOrchestrator(), adapter=_adapter(),
        inventory=InventoryRepository(),
        configs={"trading_pair": PAIR, "total_amount_quote": "500", "levels_count": 2},
    )
    dg._trend = SimpleNamespace(gate_verdict="PAUSE", gate_reason="revgrid_chop")
    dg._mirror_trend_hold()
    assert getattr(dg, "gate_reason", "") != "revgrid_chop"


def test_the_plan_reports_a_floored_pyramid_that_outgrows_its_stop_budget(monkeypatch):
    """At defaults the rung is floored at $100, so a full 4-rung pyramid reaching the
    ladder's own stop (worst case: entry slip + 2 x step stop + stop slip + taker
    round trip) costs ~$4.14 against a $0.80 budget. The plan says so (the card no
    longer promises the pyramid fits); a roomy stop fits."""
    from src.nadobro.handlers import strategy_handler as sh

    plan = sh.rgrid_trigger_plan(DEFAULTS, 0.8)
    assert plan.sizing.floored and not plan.sizing.fees_exceed_budget
    assert float(plan.pyramid_stop_cost) == pytest.approx(4.144, abs=0.01)
    assert not plan.pyramid_fits_budget
    monkeypatch.setenv("NADO_REVGRID_TRIGGER_ENABLED", "1")
    room = sh.rgrid_stop_headroom(DEFAULTS, 0.8)
    assert room["pyramid_fits"] is False and room["levels"] == 4
    assert sh.rgrid_trigger_plan(DEFAULTS, 10.0).pyramid_fits_budget
