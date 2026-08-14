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


def _eth_pack(**kwargs):
    base = dict(
        symbol="ETH",
        asset_class="nado_perp",
        horizon="4h",
        asks_event=False,
        asks_direction=True,
        tradeable_on_nado=True,
        question="Read the ETH chart on Nado, predict the market direction for 4hrly, up or down?",
    )
    base.update(kwargs)
    return EvidencePack(**base)


def _trend_candles(symbol="ETH", interval="1h", lookback_ms=0, limit=80):
    px = 3500.0
    rows = []
    for i in range(40):
        px *= 1.002
        rows.append(
            {
                "time": i,
                "open": px * 0.99,
                "high": px * 1.01,
                "low": px * 0.98,
                "close": px,
                "volume": 1.0,
            }
        )
    return rows


class _FakeNado:
    def __init__(self, candles=None):
        self.candle_calls = 0
        self._candles = list(candles or [])

    def get_candlesticks(self, *args, **kwargs):
        self.candle_calls += 1
        return list(self._candles)

    def get_product_market_stats(self, *args, **kwargs):
        return {"mid": 3500.0, "bid": 3499.0, "ask": 3501.0, "spread_bps": 5.0}

    def get_market_liquidity(self, *args, **kwargs):
        return {"bids": [[3499.0, 2.0]], "asks": [[3501.0, 1.0]]}


def test_x_tweets_without_grok_do_not_claim_grok_source(monkeypatch):
    from src.nadobro.llm import market_call_service as mcs

    pack = _eth_pack()
    monkeypatch.setattr(mcs, "_research_web", lambda _p: {})
    monkeypatch.setattr(mcs, "_research_x", lambda _p: {"tweets_fetched": 12})
    out = mcs.enrich_research_legs(pack)
    assert "Grok X (NanoGPT)" not in out.sources
    assert "GPT web (NanoGPT)" not in out.sources
    assert "X API tweets" in out.sources
    assert "grok_x" in out.missing
    assert "web" in out.missing


def test_claude_complete_retries_after_403_empty(monkeypatch):
    from src.nadobro.llm import market_call_service as mcs
    from src.nadobro.llm import llm_gateway
    from src.nadobro.llm import nanogpt_client

    calls: list[str] = []

    def _fake_chat(messages, *, model, temperature=0.2, timeout=90.0):
        calls.append(model)
        if model == "anthropic/claude-opus-4.8":
            return False, "", {"error": "empty", "status": 403, "model": model}
        return True, "**Call**\n- Price: **UP** for 4h — confidence 61%", {}

    monkeypatch.setattr(
        llm_gateway,
        "ta_model_candidates",
        lambda: ["anthropic/claude-opus-4.8", "anthropic/claude-sonnet-5"],
    )
    monkeypatch.setattr(nanogpt_client, "nanogpt_is_configured", lambda: True)
    monkeypatch.setattr(nanogpt_client, "nanogpt_chat_completion", _fake_chat)

    text = mcs._claude_complete(_eth_pack(), "jerry")
    assert "UP" in text
    assert "61%" in text
    assert calls == ["anthropic/claude-opus-4.8", "anthropic/claude-sonnet-5"]


def test_market_call_prefers_hl_skips_nado_candles(monkeypatch):
    from src.nadobro.llm import market_intel as mi
    from src.nadobro.market_data.asset_resolver import ResolvedAsset
    from src.nadobro.venue import nado_client
    from src.nadobro.market_data import hl_client
    from src.nadobro.market_data import binance_client

    fake = _FakeNado(candles=_trend_candles())
    bn_calls: list = []
    monkeypatch.setattr(nado_client, "get_or_create_readonly_client", lambda *a, **k: fake)
    monkeypatch.setattr(hl_client, "get_candles_sync", _trend_candles)
    monkeypatch.setattr(
        binance_client,
        "get_klines_sync",
        lambda *a, **k: bn_calls.append((a, k)) or [],
    )

    asset = ResolvedAsset("ETH", "nado_perp", nado_product_id=2, tradeable_on_nado=True)
    chart, quote, sources, missing = mi._nado_chart(asset, "4h", "mainnet")
    assert fake.candle_calls == 0
    assert bn_calls == []
    assert "nado_candles" not in missing
    assert "Hyperliquid candles" in sources
    assert "Nado book" in sources
    assert chart.get("candle_source") == "hyperliquid"
    assert chart.get("last_close")
    assert (chart.get("signal") or {}).get("confidence", 0) > 0
    assert quote.get("mid") == 3500.0


def test_market_call_uses_binance_when_hl_empty(monkeypatch):
    from src.nadobro.llm import market_intel as mi
    from src.nadobro.market_data.asset_resolver import ResolvedAsset
    from src.nadobro.venue import nado_client
    from src.nadobro.market_data import hl_client
    from src.nadobro.market_data import binance_client

    fake = _FakeNado(candles=_trend_candles())
    monkeypatch.setattr(nado_client, "get_or_create_readonly_client", lambda *a, **k: fake)
    monkeypatch.setattr(hl_client, "get_candles_sync", lambda *a, **k: [])
    monkeypatch.setattr(binance_client, "get_klines_sync", _trend_candles)

    asset = ResolvedAsset("ETH", "nado_perp", nado_product_id=2, tradeable_on_nado=True)
    chart, quote, sources, missing = mi._nado_chart(asset, "4h", "mainnet")
    assert fake.candle_calls == 0
    assert "hl_candles" in missing
    assert "Binance candles" in sources
    assert chart.get("candle_source") == "binance"
    assert (chart.get("signal") or {}).get("confidence", 0) > 0
    assert quote.get("mid") == 3500.0


def test_market_call_nado_candles_only_as_last_resort(monkeypatch):
    from src.nadobro.llm import market_intel as mi
    from src.nadobro.market_data.asset_resolver import ResolvedAsset
    from src.nadobro.venue import nado_client
    from src.nadobro.market_data import hl_client
    from src.nadobro.market_data import binance_client

    fake = _FakeNado(candles=_trend_candles())
    monkeypatch.setattr(nado_client, "get_or_create_readonly_client", lambda *a, **k: fake)
    monkeypatch.setattr(hl_client, "get_candles_sync", lambda *a, **k: [])
    monkeypatch.setattr(binance_client, "get_klines_sync", lambda *a, **k: [])

    asset = ResolvedAsset("ETH", "nado_perp", nado_product_id=2, tradeable_on_nado=True)
    chart, quote, sources, missing = mi._nado_chart(asset, "4h", "mainnet")
    assert fake.candle_calls >= 1
    assert "hl_candles" in missing
    assert "binance_candles" in missing
    assert "Nado candles" in sources
    assert chart.get("candle_source") == "nado"
    assert (chart.get("signal") or {}).get("confidence", 0) > 0


def test_binance_futures_symbol_and_kline_parse():
    from src.nadobro.market_data.binance_client import futures_symbol, _parse_klines

    assert futures_symbol("ETH") == "ETHUSDT"
    assert futures_symbol("eth-perp") == "ETHUSDT"
    assert futures_symbol("WTI") == "OILUSDT"
    rows = _parse_klines(
        [[1_700_000_000_000, "10", "12", "9", "11", "100", 1_700_000_003_999]]
    )
    assert rows == [
        {
            "time": 1_700_000_000,
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "volume": 100.0,
        }
    ]
