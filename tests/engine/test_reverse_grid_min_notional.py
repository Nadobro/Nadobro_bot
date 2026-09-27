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
