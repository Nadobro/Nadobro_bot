"""Mid Mode v3 Phase 7 — shaping the ladder onto support and resistance.

The asymmetry is the point: bids anchor on SUPPORT and asks on RESISTANCE.
Weighting a bid toward a resistance level would put size exactly where sellers
are waiting.

And, as with every phase, it is off unless the ``mid`` mapping turns it on —
``FillAnchoredQuotingController`` and ``RGridController`` inherit this class.
"""
import asyncio
from decimal import Decimal

import pytest

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator

BASE = {
    "trading_pair": "P",
    "spread_bid_pct": "0.01",
    "spread_ask_pct": "0.01",
    "order_amount_quote": "1000",
    "ladder_levels": "4",
    "ladder_step_bp": "20",
}


def _levels(support=(), resistance=(), *, raises=False, payload=...):
    async def _p():
        if raises:
            raise RuntimeError("candles down")
        if payload is not ...:
            return payload
        return {"support": list(support), "resistance": list(resistance)}
    return _p


def _counting_levels(sink):
    async def _p():
        sink.append(1)
        return {"support": [99.0], "resistance": []}
    return _p


def _run(configs, *, mid=Decimal(100)):
    adapter = MockNadoAdapter(mid=mid)

    async def body():
        orch = ExecutorOrchestrator()
        c = MarketMakingController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(), configs=configs, controller_id="mm-lw",
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        return c, adapter
    return asyncio.run(body())


def _sizes(adapter, side):
    """Per-rung NOTIONAL on one side, which is what the ladder redistributes."""
    return [o.amount_base * o.price for o in adapter.placed if o.side.name == side]


# --- the blast radius -------------------------------------------------------

def test_off_by_default_so_grid_and_rgrid_are_untouched():
    c, _ = _run(dict(BASE))
    assert c.level_weights_enabled is False
    assert c._sr_levels == {}


def test_the_disabled_path_never_calls_the_provider():
    calls = []
    _run({**BASE, "levels_provider": _counting_levels(calls)})   # flag off
    assert calls == []
    _run({**BASE, "level_weights_enabled": "1",
          "levels_provider": _counting_levels(calls)})
    assert calls == [1]                          # and it IS called when on


def test_only_the_mid_mapping_emits_the_phase_7_keys():
    from decimal import Decimal as D

    from src.nadobro.strategy.engine_runtime import map_strategy_config

    conf = {"notional_usd": 1000.0, "spread_bp": 20.0, "levels": 4}
    mid_cfg = map_strategy_config("mid", dict(conf), D(100), product="BTC-PERP")
    assert mid_cfg["level_weights_enabled"] == D(1)

    for strategy in ("grid", "rgrid", "dgrid"):
        cfg = map_strategy_config(strategy, dict(conf), D(100), product="BTC-PERP")
        for key in ("level_weights_enabled", "level_tolerance_bp", "level_boost",
                    "levels_provider"):
            assert key not in cfg, f"{strategy} leaked {key}"


def test_the_provider_is_excluded_from_the_live_signature():
    from src.nadobro.strategy.engine_runtime import _LIVE_CONFIG_SIGNATURE_EXCLUDE

    assert "levels_provider" in _LIVE_CONFIG_SIGNATURE_EXCLUDE


# --- shaping ----------------------------------------------------------------

def _shaped_cfg(**kw):
    return {**BASE, "level_weights_enabled": "1", "level_tolerance_bp": "80",
            "level_boost": "3", **kw}


def test_a_support_level_puts_more_size_on_the_bid_rung_that_sits_on_it():
    # Bids start at 99.00 and step down 20bp: 99.00, 98.80, 98.60, 98.40.
    plain, plain_ad = _run(_shaped_cfg(levels_provider=_levels()))
    shaped, shaped_ad = _run(_shaped_cfg(levels_provider=_levels(support=(98.60,))))
    plain_sizes = sorted(_sizes(plain_ad, "BUY"), reverse=True)
    shaped_sizes = sorted(_sizes(shaped_ad, "BUY"), reverse=True)
    assert max(shaped_sizes) > max(plain_sizes)
    assert sum(shaped_sizes) == pytest.approx(Decimal(1000))
    assert sum(plain_sizes) == pytest.approx(Decimal(1000))


def test_support_shapes_the_bid_and_leaves_the_ask_alone():
    flat, flat_ad = _run(_shaped_cfg(levels_provider=_levels()))
    shaped, shaped_ad = _run(_shaped_cfg(levels_provider=_levels(support=(98.60,))))
    assert sorted(_sizes(shaped_ad, "SELL")) == sorted(_sizes(flat_ad, "SELL"))
    assert sorted(_sizes(shaped_ad, "BUY")) != sorted(_sizes(flat_ad, "BUY"))


def test_resistance_shapes_the_ask_and_leaves_the_bid_alone():
    flat, flat_ad = _run(_shaped_cfg(levels_provider=_levels()))
    shaped, shaped_ad = _run(_shaped_cfg(levels_provider=_levels(resistance=(101.40,))))
    assert sorted(_sizes(shaped_ad, "BUY")) == sorted(_sizes(flat_ad, "BUY"))
    assert sorted(_sizes(shaped_ad, "SELL")) != sorted(_sizes(flat_ad, "SELL"))


def test_a_bid_is_never_shaped_by_a_resistance_level():
    # Putting bid size where sellers are waiting is the failure this avoids.
    flat, flat_ad = _run(_shaped_cfg(levels_provider=_levels()))
    shaped, shaped_ad = _run(_shaped_cfg(levels_provider=_levels(resistance=(98.60,))))
    assert sorted(_sizes(shaped_ad, "BUY")) == sorted(_sizes(flat_ad, "BUY"))


def test_the_side_total_is_never_changed_by_shaping():
    _, adapter = _run(_shaped_cfg(
        levels_provider=_levels(support=(98.60, 98.80), resistance=(101.0,))))
    assert sum(_sizes(adapter, "BUY")) == pytest.approx(Decimal(1000))
    assert sum(_sizes(adapter, "SELL")) == pytest.approx(Decimal(1000))


# --- robustness -------------------------------------------------------------

def test_a_failing_provider_leaves_the_ladder_at_its_configured_shape():
    plain, plain_ad = _run(dict(BASE))
    broken, broken_ad = _run(_shaped_cfg(levels_provider=_levels(raises=True)))
    assert sorted(_sizes(broken_ad, "BUY")) == sorted(_sizes(plain_ad, "BUY"))


def test_a_malformed_payload_is_ignored():
    plain, plain_ad = _run(dict(BASE))
    junk, junk_ad = _run(_shaped_cfg(levels_provider=_levels(payload="not-a-dict")))
    assert sorted(_sizes(junk_ad, "BUY")) == sorted(_sizes(plain_ad, "BUY"))


def test_junk_levels_inside_a_valid_payload_are_dropped():
    plain, plain_ad = _run(dict(BASE))
    junk, junk_ad = _run(_shaped_cfg(
        levels_provider=_levels(payload={"support": [0.0, -1.0, "x"]})))
    assert sorted(_sizes(junk_ad, "BUY")) == sorted(_sizes(plain_ad, "BUY"))


def test_no_levels_found_is_the_configured_shape_not_an_empty_book():
    _, adapter = _run(_shaped_cfg(levels_provider=_levels()))
    assert len(adapter.placed) == 8          # 4 bids + 4 asks, as configured
