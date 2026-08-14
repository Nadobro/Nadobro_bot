"""Evidence pack for Market Call — deterministic data, no user-facing prose.

Gathers quote / candles / book / features / FMP / news. Web (GPT) and X (Grok)
legs are filled by ``market_call_service`` so LLM work stays off the SDK pool.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)

_HORIZON_RE = re.compile(r"\b(15m|15\s*min|1h|1hr|4h|4hr|4hrs|4hrly|4\s*hour(?:ly)?|1d|daily)\b", re.I)


def parse_horizon(text: str, *, default: str = "4h") -> str:
    m = _HORIZON_RE.search(text or "")
    if not m:
        return default
    raw = re.sub(r"\s+", "", m.group(1).lower())
    if raw.startswith("15"):
        return "15m"
    if raw.startswith("1h") or raw == "1hr":
        return "1h"
    if raw.startswith("4"):
        return "4h"
    return "1d"


def parse_axes(text: str) -> tuple[bool, bool]:
    """(asks_event, asks_direction)."""
    q = (text or "").lower()
    asks_event = any(
        s in q
        for s in ("earnings", "beat", "miss", "eps", "guidance", "after hours", "after-hours", "after close")
    )
    asks_direction = any(
        s in q
        for s in ("up or down", "direction", "predict", "forecast", "going up", "going down", "rally", "selloff", "sell-off")
    )
    if not asks_event and not asks_direction:
        asks_direction = True
    return asks_event, asks_direction


def _pivots(values: list[float], left: int = 2, right: int = 2, kind: str = "low") -> list[float]:
    out: list[float] = []
    for i in range(left, len(values) - right):
        window = values[i - left : i + right + 1]
        v = values[i]
        if kind == "low" and v == min(window) and window.count(v) == 1:
            out.append(v)
        elif kind == "high" and v == max(window) and window.count(v) == 1:
            out.append(v)
    return out


def _sr_from_candles(candles: list[dict]) -> dict[str, Any]:
    if len(candles) < 8:
        return {"support": [], "resistance": []}
    highs = [float(c.get("high") or c.get("close") or 0) for c in candles]
    lows = [float(c.get("low") or c.get("close") or 0) for c in candles]
    sup = sorted(set(_pivots(lows, 2, 2, "low")))
    res = sorted(set(_pivots(highs, 2, 2, "high")))
    return {
        "support": [round(x, 6) for x in sup[-3:]],
        "resistance": [round(x, 6) for x in res[-3:]],
    }


def enrich_from_candles(stats: dict[str, Any], candles: list[dict]) -> dict[str, Any]:
    """Fill missing 24h volume / range / change from hourly candles."""
    out = dict(stats or {})
    last = candles[-24:] if candles else []
    if not last:
        return out
    if out.get("volume_24h_usd") is None:
        vol = sum(float(c.get("volume") or 0) for c in last)
        if vol > 0:
            out["volume_24h_usd"] = vol
    highs = [float(c.get("high") or 0) for c in last if c.get("high") is not None]
    lows = [float(c.get("low") or 0) for c in last if c.get("low") is not None]
    if out.get("high_24h") is None and highs:
        out["high_24h"] = max(highs)
    if out.get("low_24h") is None and lows:
        out["low_24h"] = min(lows)
    if out.get("change_24h_pct") is None:
        o = float(last[0].get("open") or last[0].get("close") or 0)
        c = float(last[-1].get("close") or 0)
        if o > 0 and c > 0:
            out["change_24h_pct"] = ((c - o) / o) * 100.0
    return out


def _book_imbalance(depth: dict[str, Any]) -> dict[str, Any]:
    bids = depth.get("bids") or []
    asks = depth.get("asks") or []
    bid_sz = 0.0
    ask_sz = 0.0
    for row in bids[:5]:
        try:
            bid_sz += float(row[1])
        except (TypeError, ValueError, IndexError):
            pass
    for row in asks[:5]:
        try:
            ask_sz += float(row[1])
        except (TypeError, ValueError, IndexError):
            pass
    denom = bid_sz + ask_sz
    imb = ((bid_sz - ask_sz) / denom) if denom > 0 else None
    return {
        "bid_size_top5": bid_sz or None,
        "ask_size_top5": ask_sz or None,
        "imbalance": round(imb, 4) if imb is not None else None,
        "levels": min(len(bids), len(asks)),
    }


def _compact_features(feat: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "candles", "trend", "rsi", "macd_hist", "macd_cross",
        "bb_pct_b", "atr_pct", "variance_ratio", "drift",
    )
    out = {}
    for k in keep:
        v = feat.get(k)
        if isinstance(v, float):
            out[k] = round(v, 6)
        else:
            out[k] = v
    return out


@dataclass
class EvidencePack:
    symbol: str
    asset_class: str
    horizon: str
    asks_event: bool
    asks_direction: bool
    tradeable_on_nado: bool
    quote: dict[str, Any] = field(default_factory=dict)
    chart: dict[str, Any] = field(default_factory=dict)
    event: dict[str, Any] = field(default_factory=dict)
    web: dict[str, Any] = field(default_factory=dict)
    x: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    generated_at: float = 0.0
    question: str = ""
    network: str = "mainnet"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_json(self) -> dict[str, Any]:
        """Drop empty legs so Claude is not tempted to invent them."""
        d = self.as_dict()
        for key in ("web", "x", "event", "chart", "quote"):
            if not d.get(key):
                d.pop(key, None)
        return d


def _nado_chart(asset, horizon: str, network: str) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
    chart: dict[str, Any] = {}
    quote: dict[str, Any] = {}
    sources: list[str] = []
    missing: list[str] = []
    pid = asset.nado_product_id
    if pid is None:
        missing.append("nado_product")
        return chart, quote, sources, missing
    try:
        from src.nadobro.venue.nado_client import get_or_create_readonly_client

        client = get_or_create_readonly_client(
            "0x0000000000000000000000000000000000000000", network
        )
    except Exception as exc:
        logger.warning("readonly nado client failed: %s", exc)
        missing.append("nado_client")
        return chart, quote, sources, missing

    tfs = ("15m", "1h", "4h")
    candles_by_tf: dict[str, list] = {}
    for tf in tfs:
        try:
            candles_by_tf[tf] = list(client.get_candlesticks(int(pid), tf, 200) or [])
        except Exception as exc:
            logger.warning("candles failed pid=%s tf=%s: %s", pid, tf, exc)
            candles_by_tf[tf] = []
    if not any(candles_by_tf.values()):
        missing.append("nado_candles")
    else:
        sources.append("Nado candles")

    try:
        from src.nadobro.strategy.market_features import compute_tf_features
        from src.nadobro.llm.signal_engine import build_signal

        features = {tf: _compact_features(compute_tf_features(rows)) for tf, rows in candles_by_tf.items()}
        funding = None
        try:
            stats = client.get_product_market_stats(int(pid)) or {}
        except Exception:
            stats = {}
        stats = enrich_from_candles(stats, candles_by_tf.get("1h") or [])
        quote = {
            "venue": "nado",
            "mid": stats.get("mid"),
            "bid": stats.get("bid"),
            "ask": stats.get("ask"),
            "spread_bps": stats.get("spread_bps"),
            "funding_rate": stats.get("funding_rate"),
            "volume_24h_usd": stats.get("volume_24h_usd"),
            "open_interest": stats.get("open_interest"),
            "change_24h_pct": stats.get("change_24h_pct"),
            "high_24h": stats.get("high_24h"),
            "low_24h": stats.get("low_24h"),
        }
        sources.append("Nado book")
        funding = stats.get("funding_rate")
        signal = build_signal(features, funding_rate=funding)
        horizon_rows = candles_by_tf.get(horizon) or candles_by_tf.get("4h") or candles_by_tf.get("1h") or []
        last_close = None
        last_time = None
        if horizon_rows:
            last_close = float(horizon_rows[-1].get("close") or 0) or None
            last_time = horizon_rows[-1].get("time")
        depth_summary = {}
        try:
            depth = client.get_market_liquidity(int(pid), depth=10) or {}
            depth_summary = _book_imbalance(depth)
            if depth_summary.get("levels"):
                sources.append("Nado depth")
        except Exception as exc:
            logger.debug("depth failed pid=%s: %s", pid, exc)
            missing.append("nado_depth")
        chart = {
            "venue": "nado",
            "horizon": horizon,
            "last_close": last_close,
            "last_candle_time": last_time,
            "features_by_tf": features,
            "signal": signal.as_dict(),
            "sr": _sr_from_candles(horizon_rows),
            "book": depth_summary,
            "candle_counts": {tf: len(rows) for tf, rows in candles_by_tf.items()},
        }
    except Exception as exc:
        logger.warning("nado chart pack failed: %s", exc)
        missing.append("nado_chart")
    return chart, quote, sources, missing


def _hl_chart(symbol: str, horizon: str) -> tuple[dict[str, Any], list[str], list[str]]:
    missing: list[str] = []
    sources: list[str] = []
    try:
        from src.nadobro.market_data.hl_client import get_candles_sync
        from src.nadobro.strategy.market_features import compute_tf_features
        from src.nadobro.llm.signal_engine import build_signal
    except Exception as exc:
        return {}, [], [f"hl_import:{exc}"]

    tfs = ("15m", "1h", "4h")
    candles_by_tf = {}
    for tf in tfs:
        candles_by_tf[tf] = get_candles_sync(symbol, tf) or []
    if not any(candles_by_tf.values()):
        missing.append("hl_candles")
        return {}, sources, missing
    sources.append("Hyperliquid candles")
    features = {tf: _compact_features(compute_tf_features(rows)) for tf, rows in candles_by_tf.items()}
    signal = build_signal(features)
    horizon_rows = candles_by_tf.get(horizon) or candles_by_tf.get("1h") or []
    last_close = float(horizon_rows[-1]["close"]) if horizon_rows else None
    return {
        "venue": "hyperliquid",
        "horizon": horizon,
        "last_close": last_close,
        "last_candle_time": horizon_rows[-1].get("time") if horizon_rows else None,
        "features_by_tf": features,
        "signal": signal.as_dict(),
        "sr": _sr_from_candles(horizon_rows),
        "candle_counts": {tf: len(rows) for tf, rows in candles_by_tf.items()},
    }, sources, missing


def _cmc_quote(symbol: str) -> tuple[dict[str, Any], list[str]]:
    try:
        from src.nadobro.market_data.cmc_client import get_crypto_quotes

        data = get_crypto_quotes([symbol]) or {}
        row = data.get(symbol.upper()) or {}
        if not row:
            return {}, []
        return {
            "venue": "cmc",
            "mid": row.get("price"),
            "change_24h_pct": row.get("change_24h"),
            "volume_24h_usd": row.get("volume_24h"),
            "market_cap": row.get("market_cap"),
        }, ["CoinMarketCap"]
    except Exception as exc:
        logger.debug("CMC quote failed %s: %s", symbol, exc)
        return {}, []


def _fmp_legs(symbol: str, *, want_event: bool) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
    quote: dict[str, Any] = {}
    event: dict[str, Any] = {}
    sources: list[str] = []
    missing: list[str] = []
    try:
        from src.nadobro.market_data.fmp_client import get_earnings, get_quote, get_ticker_news, is_available

        if not is_available():
            missing.append("fmp")
            return quote, event, sources, missing
        q = get_quote(symbol)
        if q:
            quote = {
                "venue": "fmp",
                "mid": q.get("price"),
                "change_24h_pct": q.get("change_pct"),
                "volume_24h_usd": q.get("volume"),
                "market_cap": q.get("market_cap"),
                "pe": q.get("pe"),
                "eps": q.get("eps"),
                "name": q.get("name"),
            }
            sources.append("FMP quote")
        if want_event:
            event = get_earnings(symbol)
            news = get_ticker_news(symbol, limit=6)
            if news:
                event["news"] = news
                sources.append("FMP news")
            if event.get("calendar") or event.get("consensus_eps") is not None:
                sources.append("FMP earnings")
            elif not news:
                missing.append("fmp_earnings")
    except Exception as exc:
        logger.debug("FMP legs failed %s: %s", symbol, exc)
        missing.append("fmp")
    return quote, event, sources, missing


def build_market_data_pack(question: str, *, network: str = "mainnet") -> EvidencePack:
    """Venue/CMC/FMP/TA legs. No LLM. Safe to run on the SDK pool."""
    from src.nadobro.llm.conversation_intent import is_event_predict_request
    from src.nadobro.market_data.asset_resolver import resolve_asset

    default_h = "1d" if is_event_predict_request(question) else "4h"
    horizon = parse_horizon(question, default=default_h)
    asks_event, asks_direction = parse_axes(question)
    asset = resolve_asset(question, network=network)
    pack = EvidencePack(
        symbol=asset.symbol,
        asset_class=asset.asset_class or "unknown",
        horizon=horizon,
        asks_event=asks_event,
        asks_direction=asks_direction,
        tradeable_on_nado=asset.tradeable_on_nado,
        generated_at=time.time(),
        question=question,
        network=network,
    )
    if not asset.symbol:
        pack.missing.append("symbol")
        return pack

    if asset.tradeable_on_nado:
        chart, quote, sources, missing = _nado_chart(asset, horizon, network)
        pack.chart = chart
        pack.quote = quote
        pack.sources.extend(sources)
        pack.missing.extend(missing)
    elif asset.asset_class in {"crypto", "nado_perp"}:
        chart, sources, missing = _hl_chart(asset.symbol, horizon)
        pack.chart = chart
        pack.sources.extend(sources)
        pack.missing.extend(missing)
        cmc_q, cmc_src = _cmc_quote(asset.symbol)
        if cmc_q:
            pack.quote = {**cmc_q, **{k: v for k, v in pack.quote.items() if v not in (None, 0, "")}}
            if not pack.quote.get("mid"):
                pack.quote = cmc_q
            pack.sources.extend(cmc_src)
        if not pack.chart:
            pack.missing.append("chart")
    else:
        fmp_q, event, sources, missing = _fmp_legs(asset.symbol, want_event=asks_event or True)
        pack.quote = fmp_q
        pack.event = event
        pack.sources.extend(sources)
        pack.missing.extend(missing)
        if asks_direction and not pack.chart:
            # Equities have no Nado candles; HL may still have a namesake.
            chart, hl_src, hl_miss = _hl_chart(asset.symbol, horizon)
            if chart:
                pack.chart = chart
                pack.sources.extend(hl_src)
            else:
                pack.missing.extend(hl_miss)
                pack.missing.append("equity_chart")

    if asset.asset_class == "equity" and not pack.event and asks_event:
        _, event, sources, missing = _fmp_legs(asset.symbol, want_event=True)
        pack.event = event
        pack.sources.extend(sources)
        pack.missing.extend(missing)

    if not pack.quote:
        cmc_q, cmc_src = _cmc_quote(asset.symbol)
        if cmc_q:
            pack.quote = cmc_q
            pack.sources.extend(cmc_src)

    # Dedupe sources / missing
    pack.sources = list(dict.fromkeys(pack.sources))
    pack.missing = list(dict.fromkeys(pack.missing))
    return pack
