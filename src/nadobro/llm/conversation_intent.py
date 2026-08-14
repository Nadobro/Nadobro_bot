"""Lightweight conversation intent classification for chat routing.

The goal is to separate "teach/analyze/debug" messages from commands that
should place trades or start strategy loops. Keep this deterministic so it can
run before any LLM calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ConversationIntentName = Literal[
    "execute",
    "learn",
    "debug",
    "quote",
    "chart_ta",
    "event_predict",
    "market",
    "product_support",
    "casual",
    "unknown",
]


@dataclass(frozen=True)
class ConversationIntent:
    name: ConversationIntentName
    confidence: float
    reason: str = ""


_EXECUTE_VERBS = (
    "start",
    "run",
    "launch",
    "activate",
    "enable",
    "buy",
    "sell",
    "long",
    "short",
    "close",
    "stop",
    "cancel",
)
_STRATEGY_TERMS = (
    "grid",
    "rgrid",
    "r-grid",
    "reverse grid",
    "dynamic grid",
    "dgrid",
    "d-grid",
    "delta neutral",
    "volume bot",
    "vol bot",
    "alpha agent",
    "strategy",
)
_EDUCATIONAL_OPENERS = (
    "how can i",
    "how do i",
    "how would i",
    "how to",
    "what is",
    "what are",
    "explain",
    "teach me",
    "walk me through",
    "guide me",
    "help me understand",
    "can you explain",
    "i want to learn",
)
_BUILD_ANALYSIS_TERMS = (
    "build",
    "create",
    "design",
    "architecture",
    "implement",
    "code",
    "working",
    "compare",
    "pros and cons",
    "best practice",
    "framework",
)
_DEBUG_TERMS = (
    "debug",
    "root cause",
    "why did",
    "why doesn't",
    "why is",
    "failed",
    "error",
    "logs",
    "traceback",
    "not working",
    "didn't work",
)
_MARKET_TERMS = (
    "price",
    "market",
    "sentiment",
    "news",
    "latest",
    "trending",
    "ct saying",
    "twitter",
    "x saying",
    "fear and greed",
    "dominance",
    "gainers",
    "losers",
)
_CHART_TA_TERMS = (
    "chart",
    "candle",
    "candlestick",
    "ohlc",
    "ohlcv",
    "orderbook",
    "order book",
    "technical analysis",
    "read the tape",
    "next move",
    "market direction",
    "direction for",
    "up or down",
    "upside or downside",
    "4hrly",
    "4h ",
    " 4h",
    "15m",
    "1h ",
    " 1h",
    "timeframe",
    "rsi",
    "macd",
    "bollinger",
    "support and resistance",
)
_EVENT_PREDICT_TERMS = (
    "earnings",
    "beat or miss",
    "miss or beat",
    "after hours",
    "after-hours",
    "after market close",
    "after close",
    "eps",
    "guidance",
    "revenue print",
    "catalyst",
)
_PREDICT_TERMS = (
    "predict",
    "prediction",
    "forecast",
    "call the",
    "will it go",
    "going up",
    "going down",
)
_QUOTE_TERMS = (
    "price of",
    "price for",
    "how much is",
    "how much does",
    "current price",
    "live price",
    "what's the price",
    "whats the price",
    "trading at",
    "quote for",
    "mark price",
)
_HOWTO_OPENERS = (
    "how can i",
    "how do i",
    "how would i",
    "how to",
    "teach me",
    "walk me through",
    "guide me",
    "help me understand",
    "can you explain",
    "i want to learn",
    "explain how",
)
_PRODUCT_TERMS = (
    "nado",
    "nadobro",
    "ink",
    "points",
    "referral",
    "invite",
    "wallet",
    "deposit",
    "withdraw",
    "funding",
    "margin",
    "liquidation",
)
_CASUAL_TERMS = {
    "gm",
    "gn",
    "hi",
    "hey",
    "hello",
    "yo",
    "thanks",
    "thank you",
    "bro",
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _has_any(q: str, terms: tuple[str, ...]) -> bool:
    return any(term in q for term in terms)


def _starts_with_any(q: str, terms: tuple[str, ...]) -> bool:
    return any(q.startswith(term) for term in terms)


def is_educational_request(text: str) -> bool:
    """True for questions that mention trading verbs but are asking to learn."""
    q = _norm(text)
    if not q:
        return False
    if _starts_with_any(q, _EDUCATIONAL_OPENERS):
        return True
    if "?" in q and _has_any(q, _BUILD_ANALYSIS_TERMS):
        return True
    return _has_any(q, _BUILD_ANALYSIS_TERMS) and _has_any(q, ("how", "guide", "explain", "learn"))


def _is_howto(q: str) -> bool:
    return _starts_with_any(q, _HOWTO_OPENERS)


def is_event_predict_request(text: str) -> bool:
    q = _norm(text)
    if not q or _is_howto(q):
        return False
    if _has_any(q, _EVENT_PREDICT_TERMS):
        return True
    return _has_any(q, _PREDICT_TERMS) and _has_any(q, ("beat", "miss", "earnings", "print"))


def is_chart_ta_request(text: str) -> bool:
    q = _norm(text)
    if not q or _is_howto(q):
        return False
    if is_event_predict_request(q):
        return False
    if _has_any(q, _CHART_TA_TERMS):
        return True
    if _has_any(q, _PREDICT_TERMS) and _has_any(q, ("up", "down", "direction", "chart", "move")):
        return True
    return bool(re.search(r"\b(15m|1h|4h|1d|4hr|4hrs)\b", q) and _has_any(q, ("up", "down", "predict", "direction", "trend")))


def is_quote_lookup(text: str) -> bool:
    """True for a live quote/stats ask that is NOT a chart or event call."""
    q = _norm(text)
    if not q or is_chart_ta_request(q) or is_event_predict_request(q):
        return False
    if _has_any(q, _QUOTE_TERMS):
        return True
    if re.search(r"\b(price|funding|spread|volume|open interest|\boi\b|bid|ask)\b", q) and not _has_any(
        q, _PREDICT_TERMS + ("chart", "candle", "direction")
    ):
        return True
    return False


def wants_market_call(text: str) -> bool:
    """Chart TA or event-predict — must reach Claude with an evidence pack."""
    return is_chart_ta_request(text) or is_event_predict_request(text)


def classify_conversation_intent(text: str) -> ConversationIntent:
    q = _norm(text)
    if not q:
        return ConversationIntent("unknown", 0.0, "empty")

    if q.rstrip("!?.,") in _CASUAL_TERMS or len(q) <= 3:
        return ConversationIntent("casual", 0.9, "short casual phrase")

    if _has_any(q, _DEBUG_TERMS):
        return ConversationIntent("debug", 0.82, "debugging/error language")

    if is_event_predict_request(q):
        return ConversationIntent("event_predict", 0.9, "earnings/event prediction")

    if is_chart_ta_request(q):
        return ConversationIntent("chart_ta", 0.9, "chart/TA/direction call")

    if is_educational_request(q):
        if _has_any(q, _STRATEGY_TERMS):
            return ConversationIntent("learn", 0.92, "educational strategy wording")
        return ConversationIntent("learn", 0.82, "educational wording")

    if is_quote_lookup(q):
        return ConversationIntent("quote", 0.86, "live quote/stats lookup")

    if _has_any(q, _MARKET_TERMS):
        return ConversationIntent("market", 0.72, "market/current-data language")

    if _has_any(q, _PRODUCT_TERMS):
        return ConversationIntent("product_support", 0.68, "Nado/Nadobro product language")

    if _has_any(q, _EXECUTE_VERBS) and (
        _has_any(q, _STRATEGY_TERMS)
        or re.search(r"\b(btc|eth|sol|xrp|aapl|tsla|nvda|doge|bnb|link)\b", q)
    ):
        return ConversationIntent("execute", 0.78, "execution verb plus market/strategy target")

    return ConversationIntent("unknown", 0.35, "no strong deterministic signal")
