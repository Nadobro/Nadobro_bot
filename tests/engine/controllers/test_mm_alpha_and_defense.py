"""Mid Mode v3 Phase 6 — alpha actuation, degraded mode, mark-out defence, STP.

The base-class rule still governs everything: ``FillAnchoredQuotingController``
and ``RGridController`` inherit this controller, so every flag here defaults off
and the disabled path must place byte-identical prices.

Beyond that, the behaviours worth pinning are the ones that fail quietly:

* a dead signal feed must WIDEN and keep quoting, never stop;
* a market Hyperliquid does not list must not be treated as a dead feed;
* alpha moves the ANCHOR, never ``directional_bias`` — one writer per field;
* a quote must never cross our own resting quote on the other side.
"""
import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator

BASE = {
    "trading_pair": "P",
    "spread_bid_pct": "0.01",
    "spread_ask_pct": "0.01",
    "order_amount_quote": "100",
}


def _provider(components=None, *, supported=True, raises=False, payload=...):
    async def _p(_pair, _mid):
        if raises:
            raise RuntimeError("feed down")
        if payload is not ...:
            return payload
        if not supported:
            return {"supported": False}
        return {"supported": True, "components": components or {}, "trusted": False}
    return _p


def _counting_provider(sink):
    async def _p(_pair, _mid):
        sink.append(1)
        return {"supported": True, "components": _LONG, "trusted": False}
    return _p


def _markout(factor=1.0, *, raises=False):
    async def _p(_half_bp):
        if raises:
            raise RuntimeError("db down")
        return factor
    return _p


def _run(configs, *, mid=Decimal(100), ticks=1, adapter=None):
    adapter = adapter or MockNadoAdapter(mid=mid)

    async def body():
        orch = ExecutorOrchestrator()
        c = MarketMakingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=configs, controller_id="mm-a6",
        )
        await orch.spawn_controller(c)
        for _ in range(ticks):
            await orch.tick_controller(c.id)
        return c, adapter
    return asyncio.run(body())


_LONG = {"obi": 0.9, "micro_displacement": 0.9, "trade_imbalance": 0.9}
_SHORT = {k: -v for k, v in _LONG.items()}
# Present but neutral — the honest baseline. An EMPTY components dict means the
# feed had nothing to say, which the controller treats as degraded.
_NEUTRAL = {k: 0.0 for k in _LONG}


def _alpha_cfg(**kw):
    return {**BASE, "alpha_enabled": "1", "signal_provider": _provider(_LONG), **kw}


# --- the blast radius -------------------------------------------------------

def test_everything_is_off_by_default():
    c, adapter = _run(dict(BASE))
    assert c.alpha_enabled is False
    assert c.self_trade_prevention is False
    assert c.alpha == 0.0 and c.markout_widen == Decimal(1)
    assert c.signal_degraded is False


def test_the_disabled_path_never_calls_the_providers():
    calls = []
    _run({**BASE, "signal_provider": _counting_provider(calls)})   # flag off
    assert calls == []
    _run({**BASE, "alpha_enabled": "1", "signal_provider": _counting_provider(calls)})
    assert calls == [1]                          # and it IS called when on


def test_the_disabled_path_places_the_same_prices():
    plain = _run(dict(BASE))[1].placed
    gated = _run({**BASE, "signal_provider": _provider(_LONG),
                  "markout_provider": _markout(2.0)})[1].placed
    assert sorted(o.price for o in plain) == sorted(o.price for o in gated)


def test_only_the_mid_mapping_emits_the_phase_6_keys():
    from decimal import Decimal as D

    from src.nadobro.strategy.engine_runtime import map_strategy_config

    conf = {"notional_usd": 100.0, "spread_bp": 5.0, "levels": 2}
    mid_cfg = map_strategy_config("mid", dict(conf), D(100), product="BTC-PERP")
    assert mid_cfg["alpha_enabled"] == D(1)
    assert mid_cfg["self_trade_prevention"] == D(1)

    for strategy in ("grid", "rgrid", "dgrid"):
        cfg = map_strategy_config(strategy, dict(conf), D(100), product="BTC-PERP")
        for key in ("alpha_enabled", "alpha_max", "alpha_strength",
                    "self_trade_prevention", "signal_provider", "markout_provider",
                    "degraded_spread_mult"):
            assert key not in cfg, f"{strategy} leaked {key}"


def test_the_injected_providers_are_excluded_from_the_live_signature():
    # None is not callable so it lands IN the signature; the injected closure is
    # callable so it drops OUT. That flip would recenter the whole ladder once
    # for nothing — the exact churn candle_provider is excluded for.
    from src.nadobro.strategy.engine_runtime import _LIVE_CONFIG_SIGNATURE_EXCLUDE

    assert "signal_provider" in _LIVE_CONFIG_SIGNATURE_EXCLUDE
    assert "markout_provider" in _LIVE_CONFIG_SIGNATURE_EXCLUDE


# --- alpha actuation --------------------------------------------------------

def test_a_long_alpha_lifts_both_quotes_and_a_short_alpha_drops_them():
    flat, flat_ad = _run(_alpha_cfg(signal_provider=_provider(_NEUTRAL)))
    long_c, long_ad = _run(_alpha_cfg())
    short_c, short_ad = _run(_alpha_cfg(signal_provider=_provider(_SHORT)))
    assert long_c.alpha > 0 > short_c.alpha
    assert long_c.alpha_offset_bp > 0 > short_c.alpha_offset_bp
    assert max(o.price for o in long_ad.placed) > max(o.price for o in flat_ad.placed)
    assert min(o.price for o in short_ad.placed) < min(o.price for o in flat_ad.placed)


def test_alpha_is_clamped_however_strong_the_components():
    c, _ = _run(_alpha_cfg(signal_provider=_provider(
        {k: 1.0 for k in ("obi", "micro_displacement", "trade_imbalance",
                          "ofi", "momentum", "basis")})))
    assert abs(c.alpha) <= 0.35 + 1e-9


def test_alpha_never_writes_directional_bias():
    # One writer per field. directional_bias is the user's; alpha moves the
    # anchor instead, so the two compose without fighting over a dead-band.
    c, _ = _run(_alpha_cfg())
    assert c.directional_bias == Decimal(0)


def test_a_defensive_component_is_refused_at_the_controller_too():
    c, _ = _run(_alpha_cfg(signal_provider=_provider(
        {"realized_vol": 1.0, "rsi": 1.0})))
    assert c.alpha == 0.0
    assert c.alpha_detail["dropped"]["realized_vol"] == "not_directional"


# --- degraded mode ----------------------------------------------------------

def test_a_dead_feed_widens_and_keeps_quoting():
    healthy, healthy_ad = _run(_alpha_cfg())
    dead, dead_ad = _run(_alpha_cfg(signal_provider=_provider(payload=None)))
    assert dead.signal_degraded is True
    assert dead.alpha == 0.0 and dead.alpha_confidence == 0.0
    assert len(dead_ad.placed) == 2                     # still quoting
    # Wider than the healthy book on both sides.
    assert min(o.price for o in dead_ad.placed) < min(o.price for o in healthy_ad.placed)
    assert max(o.price for o in dead_ad.placed) > max(o.price for o in healthy_ad.placed)


def test_a_raising_provider_is_the_same_as_a_dead_one():
    c, adapter = _run(_alpha_cfg(signal_provider=_provider(raises=True)))
    assert c.signal_degraded is True
    assert len(adapter.placed) == 2


def test_a_missing_provider_degrades_rather_than_crashing():
    c, adapter = _run({**BASE, "alpha_enabled": "1"})
    assert c.signal_degraded is True
    assert len(adapter.placed) == 2


def test_an_unlisted_market_is_not_treated_as_degradation():
    # QQQ has no Hyperliquid equivalent. Widening it forever would punish a
    # market for a feed that was never coming.
    c, _ = _run(_alpha_cfg(signal_provider=_provider(supported=False)))
    assert c.signal_degraded is False
    assert c.alpha == 0.0
    assert c.alpha_detail == {"unsupported": True}


def test_a_degraded_feed_shortens_the_ladder():
    healthy, _ = _run(_alpha_cfg(ladder_levels="3"))
    dead, _ = _run(_alpha_cfg(ladder_levels="3", signal_provider=_provider(payload=None)))
    assert healthy._effective_levels() == 3
    assert dead._effective_levels() == 2


def test_the_ladder_never_shortens_below_one_level():
    dead, adapter = _run(_alpha_cfg(ladder_levels="1",
                                    signal_provider=_provider(payload=None)))
    assert dead._effective_levels() == 1
    assert len(adapter.placed) == 2


# --- mark-out defence -------------------------------------------------------

def test_measured_adverse_selection_widens_the_quote():
    tight, tight_ad = _run(_alpha_cfg(signal_provider=_provider(_NEUTRAL),
                                      markout_provider=_markout(1.0)))
    wide, wide_ad = _run(_alpha_cfg(signal_provider=_provider(_NEUTRAL),
                                    markout_provider=_markout(2.0)))
    assert wide.markout_widen == Decimal(2)
    assert min(o.price for o in wide_ad.placed) < min(o.price for o in tight_ad.placed)


def test_the_defence_can_only_widen():
    c, _ = _run(_alpha_cfg(markout_provider=_markout(0.25)))
    assert c.markout_widen == Decimal(1)


def test_a_grading_outage_leaves_the_quote_alone():
    c, adapter = _run(_alpha_cfg(markout_provider=_markout(raises=True)))
    assert c.markout_widen == Decimal(1)
    assert len(adapter.placed) == 2


# --- self-trade prevention --------------------------------------------------

def test_a_quote_that_would_cross_our_own_resting_order_is_refused():
    c, _ = _run({**BASE, "self_trade_prevention": "1"})
    # Fabricate a live ask below where a bid is about to go.
    slot = c._slot(False, 0)
    assert slot.ex_id is not None and slot.price is not None
    assert c._would_self_trade(True, slot.price) is True          # equal price
    assert c._would_self_trade(True, slot.price + Decimal(1)) is True
    assert c._would_self_trade(True, slot.price - Decimal(1)) is False
    assert c._stp_blocks >= 1


def test_the_same_side_is_never_a_self_trade():
    c, _ = _run({**BASE, "self_trade_prevention": "1"})
    bid = c._slot(True, 0)
    # Two bids at the same price are two bids, not a cross.
    assert c._would_self_trade(True, bid.price) is False


def test_prevention_is_off_unless_asked_for():
    c, _ = _run(dict(BASE))
    ask = c._slot(False, 0)
    assert c._would_self_trade(True, ask.price + Decimal(10)) is False


def test_the_phase_6_state_is_reported_for_the_dashboard():
    c, _ = _run(_alpha_cfg(markout_provider=_markout(1.5)))
    m = c.ladder_metrics()
    assert m["alpha"] > 0
    assert m["markout_widen"] == 1.5
    assert m["signal_degraded"] is False
    assert "self_trade_blocks" in m
