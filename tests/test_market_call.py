"""Market Call routing, asset resolver, and evidence-pack helpers."""
from __future__ import annotations

from src.nadobro.llm.conversation_intent import classify_conversation_intent, wants_market_call
from src.nadobro.llm.market_call_service import _deterministic_fallback
from src.nadobro.llm.market_intel import (
    EvidencePack,
    enrich_from_candles,
    parse_axes,
    parse_horizon,
)
from src.nadobro.llm.signal_engine import build_signal
from src.nadobro.market_data.asset_resolver import resolve_asset
from src.nadobro.strategy.market_features import compute_tf_features


def test_resolve_eth_is_nado_perp():
    asset = resolve_asset("Read the ETH chart on Nado", network="mainnet")
    assert asset.symbol == "ETH"
    assert asset.tradeable_on_nado is True
    assert asset.asset_class == "nado_perp"


def test_resolve_cbrs_is_equity_not_nado():
    asset = resolve_asset(
        "CBRS earnings report after market close predict if miss or beat",
        network="mainnet",
    )
    assert asset.symbol == "CBRS"
    assert asset.tradeable_on_nado is False
    assert asset.asset_class == "equity"


def test_cbrs_never_unknown_supported_perps_only():
    asset = resolve_asset("CBRS", network="mainnet")
    assert asset.symbol == "CBRS"
    assert "BTC" not in asset.symbol


def test_horizon_and_axes():
    assert parse_horizon("predict 4hrly up or down") == "4h"
    assert parse_horizon("15m chart") == "15m"
    event, direction = parse_axes(
        "CBRS earnings report after market close predict if miss or beat. predict up or down"
    )
    assert event is True
    assert direction is True
    event2, direction2 = parse_axes("Read the ETH chart, up or down?")
    assert event2 is False
    assert direction2 is True


def test_enrich_from_hourly_candles_fills_range_and_change():
    candles = []
    for i in range(24):
        candles.append(
            {
                "time": i,
                "open": 100.0 + i,
                "high": 110.0 + i,
                "low": 90.0 + i,
                "close": 101.0 + i,
                "volume": 10.0,
            }
        )
    filled = enrich_from_candles({}, candles)
    assert filled["volume_24h_usd"] == 240.0
    assert filled["high_24h"] == 110.0 + 23
    assert filled["low_24h"] == 90.0
    assert filled["change_24h_pct"] is not None


def test_signal_engine_from_features_is_deterministic():
    candles = []
    px = 100.0
    for i in range(80):
        px *= 1.004
        candles.append(
            {"time": i, "open": px * 0.99, "high": px * 1.01, "low": px * 0.98, "close": px, "volume": 1.0}
        )
    feat = compute_tf_features(candles)
    sig = build_signal({"4h": feat, "1h": feat, "15m": feat})
    assert sig.confidence >= 0
    assert sig.regime in {"trend_up", "trend_down", "range", "chop"}


def test_deterministic_fallback_is_a_call_not_mixed_signals():
    pack = EvidencePack(
        symbol="ETH",
        asset_class="nado_perp",
        horizon="4h",
        asks_event=False,
        asks_direction=True,
        tradeable_on_nado=True,
        chart={
            "signal": {
                "bias": 0.4,
                "confidence": 0.62,
                "regime": "trend_up",
                "reasons": ["4h trend up"],
                "risks": [],
            },
            "sr": {"support": [1800], "resistance": [1900]},
        },
        sources=["Nado candles"],
        question="ETH 4h up or down?",
    )
    text = _deterministic_fallback(pack)
    assert "UP" in text
    assert "Mixed signals" not in text
    assert "Not financial advice" in text
    assert wants_market_call(pack.question)
    assert classify_conversation_intent(pack.question).name == "chart_ta"


def test_market_call_system_prompt_forbids_inventing_and_requires_two_axes():
    from src.nadobro.llm.market_call_service import MARKET_CALL_SYSTEM

    assert "TWO independent calls" in MARKET_CALL_SYSTEM
    assert "Do not invent" in MARKET_CALL_SYSTEM or "Never fabricate" in MARKET_CALL_SYSTEM
    assert "mixed signals" in MARKET_CALL_SYSTEM.lower()
    assert "You do NOT search" in MARKET_CALL_SYSTEM
