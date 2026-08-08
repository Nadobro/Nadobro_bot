"""Finance-LLM advisory tier (NanoGPT / DMind) for the financial overlay.

Before this existed the finance LLM was wired ONLY into Night HOWL's nightly
narrative — it explained trading after the fact and directed nothing. The
deterministic overlay (signal_engine → overlay_actuator) was the only thing
steering strategies.

The contract this pins: the advisor may shade conviction, never add risk, never
gate a tick, and never block a cycle on inference.
"""
from __future__ import annotations

import asyncio

import pytest

from src.nadobro.llm import signal_advisor as sa
from src.nadobro.llm.signal_engine import Signal


@pytest.fixture(autouse=True)
def _clean():
    sa.reset_cache()
    yield
    sa.reset_cache()


def _signal(**kw):
    base = dict(bias=0.6, regime="trend_up", entry_ok=True, scale=0.8, confidence=0.7)
    base.update(kw)
    return Signal(**base)


# ==========================================================================
# Never adds risk
# ==========================================================================
def test_a_verdict_can_only_shade_conviction_down_when_it_disagrees():
    out = sa._apply(_signal(), {"agree": False, "confidence_delta": 0.15, "provider": "dmind"})
    assert out.confidence <= 0.7, "disagreement must never raise confidence"
    assert out.scale < 0.8, "disagreement must reduce the appetite to add"
    assert any("disagrees" in r for r in out.risks)


def test_agreement_may_nudge_confidence_but_is_hard_clamped():
    out = sa._apply(_signal(), {"agree": True, "confidence_delta": 99.0, "provider": "nanogpt"})
    assert out.confidence == pytest.approx(0.7 + sa._MAX_CONFIDENCE_DELTA)
    assert out.scale == 0.8, "agreement must not change size appetite"


def test_a_disagreement_on_an_unconfident_read_closes_the_entry():
    out = sa._apply(_signal(confidence=0.3), {"agree": False, "confidence_delta": -0.1})
    assert out.entry_ok is False


def test_a_verdict_can_never_re_open_an_entry_the_engine_closed():
    out = sa._apply(_signal(entry_ok=False), {"agree": True, "confidence_delta": 0.15})
    assert out.entry_ok is False


def test_a_reduce_instruction_is_left_alone():
    """scale < 0 already means 'trim'. Halving it would make the bot LESS safe."""
    out = sa._apply(_signal(scale=-0.6), {"agree": False, "confidence_delta": -0.1})
    assert out.scale == -0.6


# ==========================================================================
# Never blocks, never gates
# ==========================================================================
def test_a_cold_cache_returns_the_deterministic_signal_untouched():
    """A cycle must never wait on inference — the miss schedules a refresh."""
    async def body():
        original = _signal()
        out, verdict = await sa.advise(original, {}, network="mainnet", product="BTC")
        assert out is original and verdict is None

    asyncio.run(body())


def test_a_cached_verdict_is_folded_in_on_the_next_tick():
    async def body():
        sa.store_verdict("mainnet", "BTC", {
            "ok": True, "agree": False, "confidence_delta": -0.1,
            "risks": ["funding is hostile"], "reasons": [], "provider": "dmind",
        })
        out, verdict = await sa.advise(_signal(), {}, network="mainnet", product="BTC")
        assert verdict is not None
        assert out.confidence < 0.7
        assert any("funding is hostile" in r for r in out.risks)

    asyncio.run(body())


def test_a_failed_verdict_is_cached_as_a_no_op_not_retried_every_tick():
    async def body():
        sa.store_verdict("mainnet", "BTC", {"ok": False, "reason": "finance_llm_not_configured"})
        original = _signal()
        out, verdict = await sa.advise(original, {}, network="mainnet", product="BTC")
        assert out is original and verdict is None

    asyncio.run(body())


def test_disabled_by_env_is_a_complete_no_op(monkeypatch):
    async def body():
        monkeypatch.setenv("NADO_SIGNAL_ADVISOR", "0")
        sa.store_verdict("mainnet", "BTC", {"ok": True, "agree": False, "confidence_delta": -0.15})
        original = _signal()
        out, verdict = await sa.advise(original, {}, network="mainnet", product="BTC")
        assert out is original and verdict is None

    asyncio.run(body())


def test_fetch_never_raises_when_no_finance_provider_is_configured(monkeypatch):
    from src.nadobro.llm import dmind_service

    monkeypatch.setattr(dmind_service, "is_finance_expert_configured", lambda: False)
    out = sa.fetch_verdict(_signal(), {}, "BTC")
    assert out == {"ok": False, "reason": "finance_llm_not_configured"}


def test_fetch_survives_a_provider_that_raises(monkeypatch):
    from src.nadobro.llm import dmind_service

    def _boom(*_a, **_k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(dmind_service, "is_finance_expert_configured", lambda: True)
    monkeypatch.setattr(dmind_service, "analyze_financial_context", _boom)
    assert sa.fetch_verdict(_signal(), {}, "BTC") == {"ok": False, "reason": "exception"}


# ==========================================================================
# Parsing real model replies
# ==========================================================================
def test_json_is_extracted_from_a_prose_wrapped_reply():
    verdict = sa._parse_verdict(
        'Here is my review:\n```json\n{"agree": false, "confidence_delta": -0.2, '
        '"risks": ["thin book"], "reasons": ["4h is down"]}\n```\nHope that helps.'
    )
    assert verdict is not None
    assert verdict["agree"] is False
    # -0.2 is out of bounds and is clamped, not accepted.
    assert verdict["confidence_delta"] == -sa._MAX_CONFIDENCE_DELTA
    assert verdict["risks"] == ["thin book"]


def test_unparseable_replies_degrade_to_none():
    assert sa._parse_verdict("I cannot help with that.") is None
    assert sa._parse_verdict("") is None
    assert sa._parse_verdict("{not json at all}") is None


def test_context_carries_market_state_and_no_user_identity():
    ctx = sa._build_context(
        _signal(), {"4h": {"trend": "up", "rsi": 61.0, "atr_pct": 0.4}}, "BTC"
    )
    assert '"product": "BTC"' in ctx and '"regime": "trend_up"' in ctx
    for leaked in ("user_id", "telegram", "balance", "margin", "address"):
        assert leaked not in ctx.lower()
