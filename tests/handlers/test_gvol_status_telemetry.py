"""Vol model telemetry (docs/grid_vol_model.md): controller ``gvol_*`` keys ->
bot_runtime state (blocklisted, copied, stale keys self-healed) -> /status
payload -> the card's Vol line; gate notices are vol-specific and throttled."""
from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController  # noqa: E402
from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController  # noqa: E402
from src.nadobro.engine.controllers.grid_trading import GridController  # noqa: E402
from src.nadobro.engine.controllers.reverse_grid import ReverseGridController  # noqa: E402
from src.nadobro.engine.inventory import InventoryRepository  # noqa: E402
from src.nadobro.engine.orchestrator import ExecutorOrchestrator  # noqa: E402
from src.nadobro.handlers.formatters import fmt_status_overview  # noqa: E402
from src.nadobro.strategy import bot_runtime as br  # noqa: E402
from tests.engine._mock_nado import MockNadoAdapter  # noqa: E402

_ONBOARDING_OK = {"onboarding_complete": True, "network": "mainnet", "has_key": True, "funded": True}

ON = {
    "gvol_gate_enabled": True, "gvol_spacing_enabled": True, "gvol_skew_enabled": True,
    "gvol_cap_hard": True, "margin_quote": Decimal(1000),
}


def _controllers():
    common = dict(orchestrator=ExecutorOrchestrator(), adapter=MockNadoAdapter(),
                  inventory=InventoryRepository())
    grid_cfg = {"trading_pair": "BTC-PERP", "start_price": 99, "end_price": 100,
                "total_amount_quote": 100, "step_pct": Decimal("0.001"), "levels_count": 3, **ON}
    yield GridController(user_id=1, configs=dict(grid_cfg), **common)
    yield FillAnchoredQuotingController(user_id=1, configs={"trading_pair": "BTC-PERP", **ON}, **common)
    yield DynamicGridController(user_id=1, configs={"trading_pair": "BTC-PERP", **ON}, **common)
    yield ReverseGridController(user_id=1, configs={"trading_pair": "BTC-PERP", "gvol_arm_enabled": True},
                                **common)


def _metrics(c):
    fn = getattr(c, "dgrid_metrics", None) if isinstance(c, DynamicGridController) else c.grid_metrics
    return fn()


def test_every_emitted_gvol_key_is_known_blocklisted_and_copied():
    src = inspect.getsource(br)
    for c in _controllers():
        c._gvol_evaluate(1_790_000_000.0)          # UNKNOWN verdicts, every key present
        keys = {k for k in _metrics(c) if k.startswith("gvol_")}
        assert keys, type(c).__name__
        assert keys <= set(br._GVOL_TELEMETRY_KEYS), keys - set(br._GVOL_TELEMETRY_KEYS)
        assert keys <= br._STRATEGY_SETTINGS_RUNTIME_BLOCKLIST
    assert "*_GVOL_TELEMETRY_KEYS):" in src          # copied into state each cycle
    assert "_k in _GVOL_TELEMETRY_KEYS" in src       # stale keys popped


def test_model_off_emits_only_an_empty_state():
    c = GridController(user_id=1, orchestrator=ExecutorOrchestrator(), adapter=MockNadoAdapter(),
                       inventory=InventoryRepository(),
                       configs={"trading_pair": "BTC-PERP", "start_price": 99, "end_price": 100,
                                "total_amount_quote": 100})
    m = c.grid_metrics()
    assert {k for k in m if k.startswith("gvol_")} == {"gvol_state"} and m["gvol_state"] == ""


def test_status_fields_coerce_types():
    out = br._gvol_status_fields({"gvol_state": "HOT", "gvol_rv60_bp": "5.6",
                                  "gvol_withdrawn": 4, "dgrid_regime_model": "vol"})
    assert out["gvol_state"] == "HOT" and out["gvol_rv60_bp"] == 5.6
    assert out["gvol_withdrawn"] == 4 and out["dgrid_regime_model"] == "vol"
    assert out["gvol_compressed_ago_min"] == -1 and out["gvol_gate_bp"] == 0.0
    assert br._gvol_status_fields({})["gvol_state"] == ""


def test_card_renders_each_state():
    base = {"running": True, "strategy": "grid", "product": "BTC", "runs": 3,
            "interval_seconds": 30, "last_cycle_result": "ok"}
    calm = fmt_status_overview({**base, "gvol_state": "CALM", "gvol_features": "gate",
                                "gvol_rv60_bp": 2.4, "gvol_gate_bp": 3.1, "gvol_base_bp": 3.8},
                               _ONBOARDING_OK)
    assert "🌡 Vol: *CALM* 2\\.4bp/min" in calm
    hot = fmt_status_overview({**base, "gvol_state": "HOT", "gvol_features": "gate,cap",
                               "gvol_rv60_bp": 5.6, "gvol_gate_bp": 3.1, "gvol_withdrawn": 4,
                               "gvol_cap_usd": 1470, "gvol_cap_used_usd": 1120,
                               "mm_gate_verdict": "PAUSE", "mm_gate_reason": "vol_hot",
                               "gvol_calm_min": 3}, _ONBOARDING_OK)
    assert "standing down: 4 entries withdrawn · exits working" in hot
    assert "New entries resume after 15 calm minutes \\(3/15\\)" in hot
    assert "Cap: *$1,120 / $1,470*" in hot
    warm = fmt_status_overview({**base, "gvol_state": "WARMING", "gvol_features": "gate",
                                "gvol_base_hours": 41}, _ONBOARDING_OK)
    assert "*LEARNING* this market \\(41h / 72h\\)" in warm
    off = fmt_status_overview(base, _ONBOARDING_OK)
    assert "🌡" not in off
    arm = fmt_status_overview({**base, "strategy": "rgrid", "gvol_state": "WAITING",
                               "gvol_features": "arm", "gvol_compress_bp": 3.4,
                               "gvol_expand_bp": 5.3, "mm_gate_verdict": "PAUSE",
                               "mm_gate_reason": "rgrid_vol_wait"}, _ONBOARDING_OK)
    assert "Vol arm: *WAITING*" in arm
    assert "Arms on a volatility breakout after a quiet spell" in arm


def test_vol_gate_notices_are_specific_and_throttled(monkeypatch):
    sent = []

    async def fake_notify(telegram_id, text, **kw):
        sent.append((text, kw))

    monkeypatch.setattr(br, "_notify", fake_notify)
    state: dict = {}
    result = {"grid_metrics": {"gvol_rv60_bp": 5.6, "gvol_gate_bp": 3.1}}

    async def body():
        ev = {"state": "PAUSE", "reason": "vol_hot"}
        assert br._is_vol_gate_event(ev)
        await br._notify_vol_gate_event(1, "mainnet", "grid", "BTC", state, ev, result)
        await br._notify_vol_gate_event(1, "mainnet", "grid", "BTC", state, ev, result)
        resume = {"state": "QUOTE", "reason": "", "prev_reason": "vol_hot"}
        assert br._is_vol_gate_event(resume)
        await br._notify_vol_gate_event(1, "mainnet", "grid", "BTC", state, resume, result)
        # WARMING / R-Grid wait are card-only.
        await br._notify_vol_gate_event(1, "mainnet", "grid", "BTC", {},
                                        {"state": "PAUSE", "reason": "vol_warming"}, result)
        await br._notify_vol_gate_event(1, "mainnet", "rgrid", "BTC", {},
                                        {"state": "PAUSE", "reason": "rgrid_vol_wait"}, result)

    asyncio.run(body())
    assert len(sent) == 2                                  # one PAUSE (throttled) + one RESUME
    assert "stood down" in sent[0][0] and sent[0][1]["rv"] == "5.6"
    assert "re-entered" in sent[1][0]
    assert not br._is_vol_gate_event({"state": "PAUSE", "reason": "trending_up"})


def test_notification_templates_are_translated_with_placeholders():
    from src.nadobro import i18n

    src = inspect.getsource(br._notify_vol_gate_event)
    import re
    templates = re.findall(r'"(⏸ \{strategy\}[^"]*(?:"\s*"[^"]*)*|▶️ \{strategy\}[^"]*(?:"\s*"[^"]*)*)"', src)
    joined = [re.sub(r'"\s*"', "", t) for t in templates]
    assert len(joined) == 3
    for t in joined:
        assert t in i18n._TEXTS, t[:60]
        for lang in ("zh", "fr", "ar", "ru", "ko"):
            assert i18n._TEXTS[t][lang].format(strategy="G", product="BTC", network="m",
                                               rv="1", gate="2")
