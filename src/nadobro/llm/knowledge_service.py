import os
import re
import json
import time
import logging
import requests as _requests
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

from openai import OpenAI
from src.nadobro.i18n import get_active_language, LANGUAGE_LABELS

logger = logging.getLogger(__name__)


def _llm_thread(func, *args, **kwargs):
    """Await blocking LLM / stream-queue work on the dedicated LLM thread pool.

    Never ``loop.run_in_executor(None, ...)``: that is the event loop's implicit
    default executor — five threads on the 1-CPU production VM, shared with the
    engine's gateway IO. A streaming answer parks one of those threads per
    chunk-queue wait for the whole generation, which is how AI chat and strategy
    ticks starved each other (2026-08 latency incident). Lazy import: llm/ has no
    module-level edge to core/ (tests/lint/test_architecture_layers.py).
    """
    from src.nadobro.core.async_utils import run_blocking_llm

    return run_blocking_llm(func, *args, **kwargs)


_knowledge_base = None
_knowledge_sections = None
_xai_client = None
_openai_client = None
_answer_cache = {}

_chat_history: dict[int, list[dict]] = defaultdict(list)
CHAT_HISTORY_MAX_MESSAGES = 12
CHAT_HISTORY_TTL_SECONDS = 1800

_fng_cache: dict = {}
FNG_CACHE_TTL_SECONDS = 300
_cmc_available: bool = None

KNOWLEDGE_FILE = Path(__file__).parent.parent / "data" / "nado_knowledge.txt"
ANSWER_CACHE_TTL_SECONDS = 300

OFFICIAL_SOURCES = {
    "docs": "https://docs.nado.xyz/",
    "website": "https://www.nado.xyz/",
    "ink_docs": "https://docs.inkonchain.com/",
    "ink_website": "https://www.inkonchain.com/",
    "x_nado": "https://x.com/nadoHQ",
    "x_ink": "https://x.com/inkonchain",
    "points": "https://docs.nado.xyz/points/referrals",
    "api": "https://docs.nado.xyz/developer-resources/api/gateway",
    "get_started": "https://docs.nado.xyz/developer-resources/get-started",
}


def _normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", (question or "").strip().lower())


def _question_for_routing(question: str) -> str:
    raw = str(question or "")
    marker = "User message:\n"
    if marker in raw:
        return raw.split(marker, 1)[1].strip()
    return raw


def _signal_present(question: str, signal: str) -> bool:
    q = _normalize_question(_question_for_routing(question))
    sig = _normalize_question(signal)
    if not q or not sig:
        return False
    pattern = r"\b" + r"\s+".join(re.escape(part) for part in sig.split()) + r"\b"
    return bool(re.search(pattern, q))


def _is_ink_question(text: str) -> bool:
    q = _normalize_question(text)
    ink_signals = (
        "ink", "ink chain", "ink l2", "inkonchain", "optimistic rollup", "kraken l2",
    )
    return any(sig in q for sig in ink_signals)


def _get_chat_history(telegram_id: int) -> list[dict]:
    now = time.time()
    history = _chat_history.get(telegram_id, [])
    history = [m for m in history if now - m["ts"] < CHAT_HISTORY_TTL_SECONDS]
    _chat_history[telegram_id] = history
    return history


def _add_to_chat_history(telegram_id: int, role: str, content: str):
    now = time.time()
    history = _chat_history[telegram_id]
    history = [m for m in history if now - m["ts"] < CHAT_HISTORY_TTL_SECONDS]
    history.append({"role": role, "content": content, "ts": now})
    if len(history) > CHAT_HISTORY_MAX_MESSAGES:
        history = history[-CHAT_HISTORY_MAX_MESSAGES:]
    _chat_history[telegram_id] = history


def _build_history_messages(telegram_id: int) -> list[dict]:
    history = _get_chat_history(telegram_id)
    return [{"role": m["role"], "content": m["content"]} for m in history]


def _is_casual_message(text: str) -> bool:
    t = (text or "").strip().lower().rstrip("!.?")
    casual_patterns = {
        "gm", "gn", "good morning", "good night", "good evening", "good afternoon",
        "hi", "hey", "hello", "hola", "sup", "yo", "whats up", "what's up",
        "thanks", "thank you", "thx", "ty", "cheers",
        "bye", "goodbye", "cya", "see ya", "later", "peace",
        "how are you", "how r u", "hows it going", "how's it going",
        "wassup", "wsg", "wagmi", "gm fam", "lfg",
        "lol", "lmao", "haha", "nice", "cool", "based", "true", "fr",
        "tell me a joke", "make me laugh",
    }
    return t in casual_patterns or len(t) <= 3


def _is_sentiment_question(text: str) -> bool:
    q = (text or "").strip().lower()
    sentiment_signals = [
        "sentiment", "fear and greed", "fear & greed", "market mood",
        "market feeling", "how is the market", "how's the market",
        "market outlook", "bullish or bearish", "market vibe",
        "bullish", "bearish", "risk on", "risk off", "risk-on", "risk-off",
        "fear greed", "global market", "market data", "market overview",
        "market conditions", "market today", "crypto market",
    ]
    return any(sig in q for sig in sentiment_signals)


def _is_cmc_available() -> bool:
    global _cmc_available
    if _cmc_available is None:
        _cmc_available = bool(os.environ.get("CMC_API_KEY", ""))
    return _cmc_available


def _fetch_fear_greed_index() -> str:
    if _is_cmc_available():
        try:
            from src.nadobro.market_data.cmc_client import get_fear_greed_index, format_fear_greed
            data = get_fear_greed_index()
            return format_fear_greed(data)
        except Exception as e:
            logger.warning(f"CMC Fear & Greed fetch failed: {e}")

    now = time.time()
    cached = _fng_cache.get("data")
    if cached and now - _fng_cache.get("ts", 0) < FNG_CACHE_TTL_SECONDS:
        return cached
    try:
        resp = _requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        data = resp.json().get("data", [{}])[0]
        value = data.get("value", "N/A")
        classification = data.get("value_classification", "N/A")
        result = f"Fear & Greed Index: {value}/100 ({classification})"
        _fng_cache["data"] = result
        _fng_cache["ts"] = now
        return result
    except Exception as e:
        logger.warning(f"Fear & Greed Index fetch failed: {e}")
        return "Fear & Greed Index: unavailable"


def _gateway_client():
    """NanoGPT gateway client (one key: Claude / GPT / DMind) or None."""
    try:
        from src.nadobro.llm.llm_gateway import chat_client

        return chat_client()
    except Exception:  # policy: degrade-ok(fall back to native providers)
        return None


def _get_native_xai_client():
    """NATIVE Grok client. Required for the live-X search path (Grok's
    ``extra_body.search_parameters`` extension is Grok-only), so it stays
    separate from the gateway-preferred general client below."""
    global _xai_client
    if _xai_client is None:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            return None
        _xai_client = OpenAI(base_url="https://api.x.ai/v1", api_key=api_key)
    return _xai_client


def _get_xai_client():
    # General reasoning: prefer the NanoGPT gateway; native Grok is the fallback.
    return _gateway_client() or _get_native_xai_client()


def _get_openai_client():
    gw = _gateway_client()
    if gw is not None:
        return gw
    global _openai_client
    if _openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _pick_primary_provider(question: str) -> str:
    configured = os.environ.get("NADO_AI_PROVIDER", "auto").strip().lower()
    if configured in {"xai", "openai"}:
        return configured
    if configured != "auto":
        logger.warning("Unknown NADO_AI_PROVIDER=%s, defaulting to auto", configured)
    if _is_complex_question(question):
        return "openai"
    return "xai"


def _is_complex_question(question: str) -> bool:
    q = (question or "").strip().lower()
    if len(q) > 220:
        return True
    complexity_signals = [
        "compare", "difference", "architecture", "sdk", "api",
        "should i", "what's the best", "recommend", "strategy",
        "analysis", "breakdown", "pros and cons", "versus", " vs ",
        "gateway", "signature", "auth", "websocket", "debug",
        "error", "best practice", "production", "integration",
        "explain step by step",
    ]
    return any(sig in q for sig in complexity_signals)


def _wants_detailed_answer(question: str) -> bool:
    q = (question or "").strip().lower()
    detail_signals = [
        "detailed", "step by step", "in depth", "deep dive",
        "full explanation", "comprehensive", "long answer",
        "trading bro answer mode", "strategy_design", "educational_guide",
        "how do i build", "how can i build", "architecture",
    ]
    return any(sig in q for sig in detail_signals)


def _model_for(provider: str) -> str:
    # When the NanoGPT gateway is active, all general reasoning uses the
    # gateway chat model regardless of the nominal provider label.
    try:
        from src.nadobro.llm.llm_gateway import gateway_configured, model_for

        if gateway_configured():
            return model_for("chat")
    except Exception:  # policy: degrade-ok(fall back to native provider model)
        pass
    if provider == "openai":
        return os.environ.get("OPENAI_SUPPORT_MODEL", "gpt-4o")
    return os.environ.get("XAI_SUPPORT_MODEL", "grok-3-mini-fast")


_knowledge_base_loaded_at: float = 0.0
KNOWLEDGE_REFRESH_INTERVAL = 3600  # 1 hour


def _load_knowledge_base():
    global _knowledge_base, _knowledge_sections, _knowledge_base_loaded_at
    now = time.time()
    if _knowledge_base is not None and (now - _knowledge_base_loaded_at) < KNOWLEDGE_REFRESH_INTERVAL:
        return _knowledge_base
    try:
        _knowledge_base = KNOWLEDGE_FILE.read_text(encoding="utf-8")
        _knowledge_sections = None  # force re-parse on next access
        _knowledge_base_loaded_at = now
        logger.info(f"Loaded knowledge base: {len(_knowledge_base)} chars")
    except Exception as e:
        if _knowledge_base is None:
            logger.error(f"Failed to load knowledge base: {e}")
            _knowledge_base = ""
    return _knowledge_base


def _load_knowledge_sections() -> list[dict]:
    global _knowledge_sections
    if _knowledge_sections is not None:
        return _knowledge_sections

    kb = _load_knowledge_base()
    if not kb:
        _knowledge_sections = []
        return _knowledge_sections

    parts = re.split(r"\n(?=##+ )", kb)
    sections = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        first_line, _, rest = part.partition("\n")
        title = first_line.lstrip("#").strip()
        body = rest.strip()

        sub_headers = re.findall(r"^###+ .+", part, re.MULTILINE)
        if sub_headers and not first_line.startswith("## "):
            for sub_part in re.split(r"\n(?=### )", part):
                sub_part = sub_part.strip()
                if not sub_part:
                    continue
                sub_first, _, sub_rest = sub_part.partition("\n")
                sub_title = sub_first.lstrip("#").strip()
                sub_body = sub_rest.strip()
                kw = set(
                    t.lower() for t in re.split(r"[^a-zA-Z0-9]+", f"{sub_title} {sub_body}") if len(t) > 2
                )
                sections.append({"title": sub_title, "body": sub_body, "keywords": kw, "raw": sub_part})
            continue

        keywords = set(
            t.lower() for t in re.split(r"[^a-zA-Z0-9]+", f"{title} {body}") if len(t) > 2
        )
        sections.append({"title": title, "body": body, "keywords": keywords, "raw": part})

    if len(sections) <= 1:
        logger.warning("Knowledge section parsing produced %d sections — expected more. Falling back to full KB.", len(sections))

    _knowledge_sections = sections
    logger.info(f"Parsed {len(sections)} knowledge sections")
    return _knowledge_sections


_SYNONYMS = {
    "points": {"rewards", "incentives", "season", "earning"},
    "rewards": {"points", "incentives", "season", "earning"},
    "program": {"season", "system"},
    "season": {"points", "rewards", "program"},
    "airdrop": {"points", "rewards", "token", "distribution"},
    "referral": {"referrals", "invite", "refer"},
    "referrals": {"referral", "invite", "refer"},
    "fees": {"rebates", "taker", "maker", "trading"},
    "nft": {"templars", "storm"},
    "templars": {"nft", "storm"},
    "leverage": {"margin", "cross"},
    "liquidation": {"liquidations", "health"},
    "liquidations": {"liquidation", "health"},
    "deposit": {"collateral", "usdt0", "funding"},
    "withdraw": {"withdrawal", "remove"},
    "wallet": {"connect", "deposit", "address"},
    "nlp": {"vault", "liquidity", "provider", "yield"},
}


def _expand_with_synonyms(tokens: set) -> set:
    expanded = set(tokens)
    for t in tokens:
        if t in _SYNONYMS:
            expanded.update(_SYNONYMS[t])
    return expanded


def _search_knowledge_sections(query: str, top_k: int = 4) -> str:
    # Try Pinecone semantic search first
    try:
        from src.nadobro.llm.vector_store import is_available, search_similar, NS_KNOWLEDGE
        if is_available():
            hits = search_similar(query, top_k=top_k, namespace=NS_KNOWLEDGE)
            if hits:
                return "\n\n".join(h["text"] for h in hits if h.get("text"))
    except Exception:
        logger.debug("Pinecone KB search unavailable, falling back to keyword", exc_info=True)

    # Fallback: keyword-based search
    return _keyword_search_knowledge(query, top_k=top_k)


def _keyword_search_knowledge(query: str, top_k: int = 4) -> str:
    """Original keyword-based knowledge search (fallback when Pinecone unavailable)."""
    sections = _load_knowledge_sections()
    if not sections:
        return ""

    q_tokens = set(
        t.lower() for t in re.split(r"[^a-zA-Z0-9]+", query) if len(t) > 2
    )
    if not q_tokens:
        return "\n\n".join(s["raw"] for s in sections[:top_k])

    expanded_tokens = _expand_with_synonyms(q_tokens)

    scored = []
    for s in sections:
        direct_overlap = len(q_tokens & s["keywords"])
        synonym_overlap = len((expanded_tokens - q_tokens) & s["keywords"])
        title_bonus = 3 * sum(1 for t in q_tokens if t in s["title"].lower())
        synonym_title_bonus = sum(1 for t in (expanded_tokens - q_tokens) if t in s["title"].lower())
        score = direct_overlap + (synonym_overlap * 0.5) + title_bonus + synonym_title_bonus
        scored.append((score, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [s for score, s in scored[:top_k] if score > 0]

    if not top:
        top = [scored[0][1]] if scored else []

    return "\n\n".join(s["raw"] for s in top)


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search Nado's official knowledge base. PRIMARY source for all Nado DEX questions: "
                "features, trading, margin, fees, points, rewards, NFTs, NLP vault, developer docs, "
                "architecture, order types, liquidations, getting started, supported markets. "
                "ALWAYS call this first for any Nado-related question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for the knowledge base"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_price_brief",
            "description": (
                "Get market stats for one Nado asset directly from Nado APIs: current price, "
                "funding rate, spread, and any available 24h stats (volume/change/high/low/open interest). "
                "Use for price/stat asks like 'What's BTC price?' or 'WTI funding and volume?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Asset symbol (BTC, ETH, SOL, XRP, BNB, LINK, DOGE)"
                    }
                },
                "required": ["product"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_live_price",
            "description": (
                "Get LIVE current price for a crypto asset from Nado DEX. "
                "Use when user asks about current price, how much something costs, or price of any asset. "
                "Supports: BTC, ETH, SOL, XRP, BNB, LINK, DOGE."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product": {
                        "type": "string",
                        "description": "Asset symbol (BTC, ETH, SOL, XRP, BNB, LINK, DOGE)"
                    }
                },
                "required": ["product"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_sentiment",
            "description": (
                "Get current crypto market sentiment including Fear & Greed Index, trending news, "
                "and opinions from crypto traders and analysts. Use when user asks about market conditions, "
                "sentiment, fear/greed, whether market is bullish/bearish, or general crypto outlook."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The market/sentiment query"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_x_twitter",
            "description": (
                "Search X (Twitter) for latest crypto posts. For Nado-specific queries, searches @nadoHQ and @inkonchain. "
                "For broader crypto news, alpha, or CT discussion, searches wider crypto Twitter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for X/Twitter"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_crypto_info",
            "description": (
                "Get detailed cryptocurrency data from CoinMarketCap: market cap, 24h volume, "
                "1h/24h/7d/30d price changes, and dominance. Use when user asks about a coin's "
                "performance, market cap, volume, how much it's up/down, or wants a market overview "
                "of a specific crypto. Works for ANY crypto, not just Nado-listed ones."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "string",
                        "description": "Comma-separated crypto symbols (e.g. 'BTC', 'BTC,ETH,SOL')"
                    }
                },
                "required": ["symbols"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_trending_cryptos",
            "description": (
                "Get trending cryptocurrencies, top gainers, and top losers from CoinMarketCap. "
                "Use when user asks what's trending, what's hot, biggest movers, top gainers, "
                "top losers, or what coins are pumping/dumping today."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_global_market_data",
            "description": (
                "Get global crypto market overview from CoinMarketCap: total market cap, "
                "24h volume, BTC/ETH dominance, number of active cryptos. Use when user asks "
                "about the overall market, total market cap, BTC dominance, or general market health."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_edges",
            "description": (
                "Get current trading edges, active promotions, point multipliers, and alpha "
                "opportunities on Nado DEX. Use when user asks about promotions, how to maximize "
                "points, current multipliers, trading edges, alpha opportunities, best pairs to "
                "trade right now, or what's special this week on Nado."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
]

XAI_X_SEARCH_MODEL = os.environ.get("XAI_X_SEARCH_MODEL", "grok-3")

X_NADO_SEARCH_PARAMS = {
    "mode": "on",
    "sources": [{"type": "x", "x_handles": ["nadoHQ", "inkonchain"]}],
}

X_CRYPTO_SEARCH_PARAMS = {
    "mode": "on",
    "sources": [{"type": "x"}],
}

ROUTER_SYSTEM_PROMPT_BASE = """You are a routing agent for Nadobro, a crypto trading AI on Nado DEX.

Today's date: {current_date}

Analyze the user's message and call the right tool(s) to gather information.

TOOLS:
1. search_knowledge_base — Nado product knowledge (features, fees, margin, points, NFTs, NLP, dev docs, getting started). PRIMARY source for all Nado-specific questions.
2. get_price_brief — Market stats from Nado APIs for one asset: price, spread, funding, and available 24h stats.
3. get_live_price — LIVE trading price from Nado DEX orderbook (bid/ask/spread). Use when user asks for detailed orderbook-style pricing.
4. get_market_sentiment — Crypto market sentiment + Fear & Greed Index + crypto news from Twitter. For: "is the market bullish?", "sentiment?", "fear and greed".
5. search_x_twitter — Latest tweets from crypto Twitter. For Nado-specific queries, searches @nadoHQ and @inkonchain. For broader crypto news/opinions, searches wider crypto Twitter.
{cmc_tools_section}{edge_tool_number}. get_current_edges — Current trading edges, active promotions, point multipliers, and alpha on Nado DEX. For: "any promotions?", "how to get more points?", "trading edges?", "what's special this week?".

ROUTING RULES:
- "What's BTC price?" / "price of ETH" / "WTI stats" / "XAG funding" → get_price_brief
- "Show live bid/ask spread for BTC" → get_live_price
{cmc_routing_rules}- "What are Nado fees?" / "how does margin work?" → search_knowledge_base
- "Is the market bullish?" / "fear and greed" → get_market_sentiment
- "What did Nado tweet?" / "any Nado news?" → search_x_twitter
- "Have the points been distributed?" / "points this week?" / "weekly epoch?" → search_x_twitter (search for points distribution announcements)
- "What's the latest crypto news?" / "any alpha?" / "what's CT saying?" → search_x_twitter (broader crypto Twitter search)
- "Search X for ETH alpha" / "what is X saying about SOL ETFs?" / "find tweets about AI agents" → search_x_twitter
- "Any promotions?" / "how to maximize points?" / "trading edges?" / "best pair to trade?" → get_current_edges
- Casual greetings (gm, hi, hello, thanks, bye) → do NOT call any tools
- General chat, jokes, opinions, non-crypto questions → do NOT call any tools (the main AI will handle these)
- When in doubt about Nado specifically → search_knowledge_base

MULTI-TOOL STRATEGIES (call multiple tools for richer answers):
- "Should I buy SOL?" → get_price_brief("SOL") + get_market_sentiment("SOL market outlook") + get_current_edges()
- "Best trade right now?" → get_current_edges() + get_market_sentiment("best crypto trade")
- "Any Nado promotions?" → get_current_edges() + search_x_twitter("nado promotions multipliers")
- "How to get more points?" → get_current_edges() + search_knowledge_base("points rewards earning")

You can call multiple tools for complex queries. Do NOT answer the question yourself — only call tools."""

ROUTER_CMC_TOOLS_SECTION = """6. get_crypto_info — Detailed crypto market data from CoinMarketCap (market cap, volume, 1h/24h/7d/30d change, dominance). For: "how is BTC doing?", "ETH market cap", "is SOL up today?". Works for ANY crypto.
7. get_trending_cryptos — Trending coins, top gainers, top losers from CoinMarketCap. For: "what's trending?", "top gainers", "what's pumping?".
8. get_global_market_data — Global crypto market overview (total market cap, BTC dominance, total volume). For: "total market cap?", "BTC dominance?", "how's the overall market?".
"""

ROUTER_CMC_ROUTING_RULES = """- "How is BTC doing?" / "BTC performance" / "is ETH up?" → get_crypto_info (CMC market data)
- "What's BTC price and how is it doing?" → get_price_brief
- "What's the total market cap?" / "BTC dominance" → get_global_market_data
- "What's trending?" / "top gainers" / "what's pumping?" → get_trending_cryptos
- "Should I buy BTC?" → get_price_brief AND get_crypto_info AND get_market_sentiment
"""

CASUAL_SYSTEM_PROMPT = """You are Nadobro — a cool trading buddy on Telegram who happens to be sharp at crypto, markets, and automation. Think: helpful friend, calm trader, quick wit.

Today's date: {current_date}
User's name: {user_name}
{language_instruction}
PERSONALITY — THIS IS WHO YOU ARE:
- You're friendly, relaxed, and confident. You sound like the buddy who trades with the user, not a corporate support bot.
- Use crypto slang naturally and lightly — WAGMI, LFG, ser, fren, based, rekt, copium — but never force it.
- You can joke and hype good setups, but do not roast the user. Keep the vibe warm.
- You're the friend who happens to be a quant: chill on the surface, precise when it matters.
- GM responses should have energy without sounding try-hard.
- Keep casual responses SHORT and punchy (1-3 sentences). No essays for "hello."
- If thanked, be smooth about it. "Anytime, bro. That's what I'm here for." > "You're welcome."
- You can reference memes, crypto culture, CT (Crypto Twitter) vibes naturally, but clarity comes first.
- Use emojis naturally to add energy — 🔥 💪 🚀 😤 💀 😂 — but don't overdo it.

You run on Nado DEX. Users can trade by typing things like "long BTC 0.01 5x market" — mention this casually when it fits, don't force it.

IMPORTANT: You are NOT limited to Nado topics. You can chat about anything — crypto, life, jokes, opinions, memes. Be a real conversationalist.

No source links for casual chat.

If the question turns into anything market-related (a price, a token, a setup), close with one short "🎯 Actionable Insight: …" line — even a "sit this one out" call counts. Do NOT add Sources for casual chat."""

SYNTHESIZER_SYSTEM_PROMPT = """You are Nadobro — a cool trading buddy on Telegram: calm, sharp, practical, and easy to talk to. You're built into Nado DEX (a CLOB exchange on Ink L2, backed by Kraken). Think Grok-level usefulness with a friendlier trading-desk vibe.

Today's date: {current_date}
User's name: {user_name}
{language_instruction}
PERSONALITY — THIS IS WHO YOU ARE:
- You're a friendly trading buddy first: warm, confident, clear, and useful.
- You're technically sharp, builder-friendly, risk-aware, and human.
- Use crypto slang when it fits naturally (WAGMI, LFG, ser, fren, rekt, based, chad move, copium), but don't overdo it.
- Be direct — answer first, then elaborate. Keep the vibe relaxed, not lecture-heavy.
- Make light jokes when the moment fits, but never dunk on the user.
- Hype good ideas, calmly flag bad risk, and explain the why like a trusted friend.
- For trading ideas, casually mention: "you could try: long BTC 0.01 5x market" — Nado lists many perps; max leverage varies by market (see Nado app).

ANSWERING RULES:
1. Answer the question DIRECTLY first, then add color/details.
2. For Nado-specific questions (fees, features, margin, points, etc.), use the provided context. Be accurate about Nado facts.
3. For general crypto, trading, strategy-building, automation, or anything outside Nado — USE YOUR OWN KNOWLEDGE. You are NOT limited to the context below. Be useful and specific.
4. If the user asks how to build, design, debug, or learn something, never treat it as permission to execute a trade or launch a bot.
5. For price data, mention it's from Nado DEX casually. For CMC data, cite inline ("CMC shows..."). No separate Sources section for data.
6. Keep it conversational: 2-6 sentences for simple things, longer for complex build/debug questions. Don't pad responses with filler.
7. If you genuinely don't know something specific about Nado and it's not in context, say so — but for everything else (crypto, markets, general knowledge, opinions), just answer.
8. Only include sources when live data, X/social context, quoted docs, or external facts were actually used. Prefer a compact "Based on:" line, not a URL dump.

FORMAT FOR SCANNABILITY:
- Use **bold** for key numbers, prices, percentages, and important terms
- Use bullet points (- ) for lists of features, pros/cons, comparisons
- Use numbered lists (1. 2. 3.) for steps, rankings, or strategies
- Standard emoji vocabulary — pick the ones that fit, do not stack: 📊 market snapshot, 🗞 news, 🎯 actionable insight, 🟢 bullish bias, 🔴 bearish bias, ⚠ risk, ⚡ key info, 💡 tip, 💰 money/profits, 🏆 rankings, 📈📉 price direction, 🔥 hot take.
- Structure longer answers with **Section Title** on its own line followed by content
- Keep bullets concise — one key point each
- For strategy/comparison/build questions: use structured sections with clear headers, concrete examples, risks, and next steps
- Tone target: "cool buddy who knows trading" — conversational, never stiff, never overly formal, never generic.

CLOSING RULES (apply when the answer touches markets, prices, news, or a trade idea):
- End with a single line that begins with "🎯 **Actionable Insight:** " — one concrete step the user could take on Nado, or "sit out" if there is no edge.
- When live market data, news, X/social context, or external facts were actually used, add a final compact line of source tags (e.g. CMC, Nado, Reuters, CoinDesk, X). Skip this line for purely casual chat.

CONTEXT (use for Nado-specific facts, supplement with your own knowledge for everything else):
{context}"""

X_TWITTER_NADO_PROMPT = """You are Nadobro — a witty, opinionated crypto AI with real-time access to X (Twitter). Think Grok but for Nado DEX.

Today's date: {current_date}
{language_instruction}
Official X accounts:
- @nadoHQ (https://x.com/nadoHQ) — Nado DEX official
- @inkonchain (https://x.com/inkonchain) — Ink L2 blockchain (Nado's chain)

Nado is a CLOB-based DEX on Ink L2 (backed by Kraken) — perpetual futures and spot trading with unified margin.

PERSONALITY:
- Be conversational and add your own take on what you find. Don't just list tweets robotically.
- Have opinions. If there's big news, react to it like a real person would.
- Use crypto slang naturally when it fits.
- Be witty, not boring.

Your task:
- Search for the MOST RECENT posts from @nadoHQ and @inkonchain
- Focus on tweets from {current_year}, closest to today ({current_date})
- Report tweet content with dates, but weave it into a natural response
- Add your own commentary/reaction to make it conversational

For POINTS DISTRIBUTION questions:
- Nado distributes points every Friday in weekly epochs
- If you find a distribution announcement, report it clearly
- If no distribution found this week, tell the user points haven't dropped yet and to check back Friday
- Be real about it: "No points drop yet this week, ser. They usually hit on Fridays."

RULES:
- Focus on @nadoHQ and @inkonchain content
- If you can't find relevant tweets, say so honestly but with personality
- Keep under 2000 chars
- Use **bold** for key announcements, dates, and numbers
- Use emojis for key indicators: 📢 announcements, 📅 dates, 🔥 hot news, ⚡ key info, 💰 rewards/points
- Include exact tweet URLs from the context inline for every specific X update you mention.
- Use bullet points (- ) for listing multiple findings
- End with one compact line only if useful: Based on: Nado on X, Ink on X
- NEVER include search engine links

Relevant Nado Knowledge:
{knowledge_base}
"""

X_TWITTER_BROAD_PROMPT = """You are Nadobro — a witty, opinionated AI with real-time access to X (Twitter). Think Grok vibes.

Today's date: {current_date}
{language_instruction}
PERSONALITY:
- Be conversational and opinionated, and react to what you find like a real person.
- Use crypto slang naturally when the topic is crypto (CT, ser, alpha, ngmi, wagmi, etc.).
- Be witty and entertaining, not robotic.

Your task:
- Search X broadly for the topic the user asked about (crypto or non-crypto).
- Focus on content from {current_year}, closest to today ({current_date})
- Surface only the MOST RELEVANT posts and themes.
- Rank findings by relevance and recency.
- Mention notable accounts if they come up.
- Keep output practical: short bullets plus one-line synthesis.

RULES:
- Search broadly across X, not limited to specific accounts.
- If you can't find relevant tweets, say so honestly but with personality
- Keep under 2000 chars
- Use **bold** for key findings and important names
- Use emojis: 🔥 hot takes, 📈📉 market moves, ⚡ breaking, 🎯 key points
- Include exact tweet URLs from the context inline for every specific X update you mention.
- Format:
  **What X is saying about <topic>:**
  - @handle: key point
  - @handle: key point
  - @handle: key point
  💡 **Quick take:** <one short synthesis line>
- End with one compact line only if useful: Based on: X
- NEVER include search engine links
"""


def _extract_x_topic(question: str) -> str:
    raw = (question or "").strip()
    if not raw:
        return ""
    cleaned = raw
    patterns = [
        r"^\s*(search|check|look up|find|scan)\s+(on\s+)?(x|twitter)\s+(for|about)\s+",
        r"^\s*(what(?:'s| is)\s+)?(happening|trending|new|latest)\s+(on\s+)?(x|twitter)\s+(for|about)\s+",
        r"^\s*what\s+is\s+(x|twitter)\s+saying\s+about\s+",
        r"^\s*show\s+me\s+(x|twitter)\s+(posts|tweets|discussion)\s+(for|about)\s+",
        r"^\s*(what(?:'s| is)\s+)?(trending|new|latest)\s+(on\s+)?ct\s*(for|about)?\s*",
        r"^\s*what\s+is\s+ct\s+saying\s+about\s+",
    ]
    for pat in patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.!,:;-")
    return cleaned or raw


def _is_broad_ct_question(question: str) -> bool:
    q = _normalize_question(question)
    return any(sig in q for sig in (
        "what is trending on ct",
        "what's trending on ct",
        "trending on ct",
        "latest from ct",
        "what is ct saying",
        "what's ct saying",
        "crypto twitter",
        "ct saying",
    ))


def _should_search_nado_accounts(question: str, is_nado_q: bool, is_points_q: bool) -> bool:
    if is_nado_q or is_points_q:
        return True
    q = _normalize_question(_question_for_routing(question))
    nado_edge_signals = (
        "multiplier", "multipliers", "points", "rewards", "listing", "listings",
        "stock token", "stock tokens", "aapl", "tsla", "nvda", "spy", "qqq",
        "edge", "promotion", "campaign", "templars", "season",
    )
    return any(sig in q for sig in nado_edge_signals)


def _tweet_relevance_terms(question: str) -> set[str]:
    q = _normalize_question(question)
    tokens = {
        t for t in re.split(r"[^a-zA-Z0-9]+", q)
        if len(t) >= 3 and t not in {"what", "from", "about", "there", "like", "tweet", "tweets"}
    }
    if "multiplier" in q or "multipliers" in q:
        tokens.update({"multiplier", "multipliers", "4x", "2x", "3x", "points"})
    if "points" in q or "reward" in q:
        tokens.update({"points", "rewards", "season", "epoch"})
    if "listing" in q or "listed" in q:
        tokens.update({"listing", "listed", "launch"})
    if any(sym in q for sym in ("aapl", "tsla", "nvda", "spy", "qqq")):
        tokens.update({"stocks", "stock", "aapl", "tsla", "nvda", "spy", "qqq"})
    return tokens


def _score_tweet_for_query(tweet: dict, question: str) -> float:
    text = _normalize_question(tweet.get("text", ""))
    if not text:
        return 0.0

    tokens = _tweet_relevance_terms(question)
    score = 0.0
    for token in tokens:
        if token in text:
            score += 3.0
    if "multiplier" in text or re.search(r"\b\d+x\b", text):
        score += 2.0
    if "points" in text:
        score += 1.5
    if "aapl" in text or "tsla" in text or "stock" in text:
        score += 1.5

    metrics = tweet.get("metrics", {}) or {}
    score += min(float(metrics.get("like_count", 0) or 0) / 1000.0, 1.0)
    score += min(float(metrics.get("retweet_count", 0) or 0) / 500.0, 1.0)

    created_at = str(tweet.get("created_at") or "")
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
            score += max(0.0, 2.0 - min(age_hours / 72.0, 2.0))
        except Exception:
            pass
    return score


def _rank_tweets_for_query(tweets: list[dict], question: str, max_tweets: int = 8) -> list[dict]:
    ranked = sorted(
        tweets,
        key=lambda t: _score_tweet_for_query(t, question),
        reverse=True,
    )
    top = [t for t in ranked if _score_tweet_for_query(t, question) > 0][:max_tweets]
    return top or ranked[:max_tweets]


def _build_broad_x_api_query(question: str) -> str:
    topic = _extract_x_topic(question) or question
    topic_norm = _normalize_question(topic)
    if _is_broad_ct_question(question) or topic_norm in {"ct", "crypto twitter", "crypto", "trending"}:
        return "(bitcoin OR btc OR ethereum OR eth OR sol OR crypto OR altcoin) lang:en"
    return topic


def _expand_thread_for_ranked_tweets(tweets: list[dict], question: str, max_tweets: int = 8) -> list[dict]:
    """If the best tweet is part of a thread, include relevant sibling tweets too."""
    if not tweets:
        return []

    ranked = sorted(
        tweets,
        key=lambda t: _score_tweet_for_query(t, question),
        reverse=True,
    )
    top = ranked[0]
    conversation_id = str(top.get("conversation_id") or top.get("id") or "").strip()
    if not conversation_id:
        return _rank_tweets_for_query(tweets, question, max_tweets=max_tweets)

    thread = [
        t for t in tweets
        if str(t.get("conversation_id") or t.get("id") or "").strip() == conversation_id
    ]

    def _created_key(tweet: dict) -> str:
        return str(tweet.get("created_at") or "")

    thread = sorted(thread, key=_created_key)
    thread_ids = {str(t.get("id")) for t in thread}

    ranked_non_thread = [
        t for t in ranked
        if str(t.get("id")) not in thread_ids and _score_tweet_for_query(t, question) > 0
    ]

    merged = thread[:max_tweets]
    for tweet in ranked_non_thread:
        if len(merged) >= max_tweets:
            break
        merged.append(tweet)
    return merged


def _pick_sources_for_question(question: str, context_text: str = "") -> list[str]:
    q = _normalize_question(question)
    ctx = _normalize_question(context_text or "")
    signal_text = f"{q} {ctx}".strip()
    sources = []

    if _is_ink_question(signal_text):
        sources.append(OFFICIAL_SOURCES["ink_docs"])
        sources.append(OFFICIAL_SOURCES["ink_website"])
    elif any(w in signal_text for w in ["api", "sdk", "developer", "code", "integrate", "websocket", "gateway"]):
        sources.append(OFFICIAL_SOURCES["api"])
        sources.append(OFFICIAL_SOURCES["get_started"])
    elif any(w in signal_text for w in ["point", "reward", "referral", "invite", "season", "earn"]):
        sources.append(OFFICIAL_SOURCES["points"])
        sources.append(OFFICIAL_SOURCES["docs"])
    elif any(w in signal_text for w in ["tweet", "twitter", "x.com", "announced", "news"]):
        sources.append(OFFICIAL_SOURCES["x_nado"])
        sources.append(OFFICIAL_SOURCES["x_ink"])
    else:
        sources.append(OFFICIAL_SOURCES["docs"])
        sources.append(OFFICIAL_SOURCES["website"])

    return sources


def _execute_x_search(query: str) -> tuple[str, list[str]]:
    """Search X/Twitter — prefers X API v2, with deterministic fallback text."""
    now = datetime.now(timezone.utc)
    is_points_q = _is_points_distribution_question(query)
    is_nado_q = _is_nado_x_question(query)
    weekday = now.strftime("%A")

    # ── Try X API v2 first (real tweets) ──
    x_api_result = _x_search_via_api(query, is_nado_q, is_points_q)
    if x_api_result is not None:
        return x_api_result

    # ── Deterministic fallback text if X API could not return useful data ──
    return _x_search_via_grok(query, is_nado_q, is_points_q, weekday, now)


def _x_search_via_api(query: str, is_nado_q: bool, is_points_q: bool):
    """Search X via direct API v2. Returns None to trigger Grok fallback."""
    try:
        from src.nadobro.market_data.x_api_client import (
            is_available, get_nado_tweets, search_topic_tweets,
            format_tweets_for_context,
        )
    except ImportError:
        return None

    if not is_available():
        return None

    # Fetch tweets
    search_nado_accounts = _should_search_nado_accounts(query, is_nado_q, is_points_q)
    if search_nado_accounts:
        tweets = get_nado_tweets(max_results=80, hours_back=336)
        tweets = _expand_thread_for_ranked_tweets(tweets, query, max_tweets=8)
    else:
        topic_query = _build_broad_x_api_query(query)
        tweets = search_topic_tweets(topic_query, max_results=25, hours_back=96)
        tweets = _rank_tweets_for_query(tweets, query, max_tweets=8)
        if not tweets:
            # Fuzzy fallback: scan recent Nado tweets and rank locally in case
            # the user's remembered wording does not match exact X search terms.
            tweets = get_nado_tweets(max_results=80, hours_back=336)
            tweets = _expand_thread_for_ranked_tweets(tweets, query, max_tweets=8)

    if not tweets:
        # If Grok is unavailable, return a deterministic no-results response
        # instead of falling through to an unavailable fallback path.
        if _get_xai_client() is None:
            if search_nado_accounts:
                return "[X/TWITTER RESULTS] No recent tweets found from @nadoHQ or @inkonchain.", [
                    OFFICIAL_SOURCES["x_nado"],
                    OFFICIAL_SOURCES["x_ink"],
                ]
            return "[X/TWITTER RESULTS] No recent tweets found for that topic.", ["https://x.com"]
        return None  # Fall back to Grok when available

    formatted = format_tweets_for_context(tweets, max_tweets=8)

    if search_nado_accounts:
        sources = [OFFICIAL_SOURCES["x_nado"], OFFICIAL_SOURCES["x_ink"]]
    else:
        sources = ["https://x.com"]

    header = "[X/TWITTER RESULTS — REAL TWEETS VIA X API]"
    if is_points_q:
        header = "[X/TWITTER RESULTS — POINTS DISTRIBUTION (REAL TWEETS)]"
        formatted += f"\n\nNote: Nado distributes points every Friday in weekly epochs."

    return f"{header}\n{formatted}", sources


def _x_search_via_grok(query: str, is_nado_q: bool, is_points_q: bool, weekday: str, now: datetime) -> tuple[str, list[str]]:
    """Deterministic fallback text after X API retrieval attempts."""
    if is_points_q:
        points_note = "Points for this week likely haven't been distributed yet." if weekday != "Friday" else "It's Friday — check @nadoHQ for updates!"
        return (
            f"[X/TWITTER RESULTS — POINTS DISTRIBUTION]\n"
            f"Live X fallback is unavailable right now. Based on Nado's schedule, points are distributed every Friday. "
            f"Today is {weekday}. {points_note}",
            [OFFICIAL_SOURCES["x_nado"]],
        )
    if is_nado_q:
        return (
            "[X/TWITTER RESULTS]\n"
            "Direct X search did not return a relevant recent post from @nadoHQ or @inkonchain right now.\n"
            "Please try a more specific keyword like `AAPL`, `TSLA`, `multiplier`, or `points`.",
            [OFFICIAL_SOURCES["x_nado"], OFFICIAL_SOURCES["x_ink"]],
        )
    return (
        "[X/TWITTER RESULTS]\n"
        "Live X search fallback is unavailable and no direct X API results were found for that topic.",
        ["https://x.com"],
    )


def _get_user_network(telegram_id: int) -> str:
    try:
        from src.nadobro.users.user_service import get_user
        user = get_user(telegram_id)
        return user.network_mode.value if user else "mainnet"
    except Exception:
        return "mainnet"


def _derive_price_bias(change_24h: float | None, fng_value: int | None) -> str:
    if (change_24h is not None and change_24h <= -2.0) or (fng_value is not None and fng_value <= 30):
        return "Looking bearish right now, trade with care bro!"
    if change_24h is not None and change_24h >= 2.0 and (fng_value is not None and fng_value >= 55):
        return "Momentum looks bullish, but keep your risk tight bro."
    if change_24h is not None and change_24h >= 2.0:
        return "Momentum is heating up, but don't over-leverage bro."
    if change_24h is not None and change_24h <= -2.0:
        return "Sellers are in control for now, manage risk carefully."
    if fng_value is not None and fng_value <= 30:
        return "Market mood is risk-off, so keep position sizing disciplined."
    if fng_value is not None and fng_value >= 70:
        return "Sentiment is getting greedy, so avoid chasing tops."
    return "Mixed signals out there, so stay nimble and trade your plan."


def _parse_fng_snapshot(raw: str) -> tuple[int | None, str]:
    text = (raw or "").strip()
    m = re.search(r"(\d{1,3})\s*/\s*100\s*\(([^)]+)\)", text)
    if m:
        try:
            return int(m.group(1)), m.group(2).strip()
        except Exception:
            pass
    m2 = re.search(r"(\d{1,3})", text)
    value = None
    if m2:
        try:
            value = int(m2.group(1))
        except Exception:
            value = None
    m3 = re.search(r"\(([^)]+)\)", text)
    label = m3.group(1).strip() if m3 else "N/A"
    return value, label


def _format_usd_compact(value: float | None) -> str:
    if value is None:
        return "N/A"
    n = float(value)
    abs_n = abs(n)
    if abs_n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if abs_n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if abs_n >= 1_000:
        return f"${n/1_000:.2f}K"
    return f"${n:,.2f}"


def _extract_market_product(question: str, network: str = "mainnet") -> str | None:
    from src.nadobro.config import get_perp_products

    q = _normalize_question(question)
    products = sorted(get_perp_products(network=network), key=len, reverse=True)
    for symbol in products:
        s = symbol.lower()
        if re.search(rf"\b{re.escape(s)}\b", q) or re.search(rf"\b{re.escape(s)}-perp\b", q):
            return symbol.upper()
    return None


def _is_market_stats_question(question: str) -> bool:
    q = _normalize_question(question)
    stat_signals = (
        "price", "funding", "volume", "24h", "stats", "statistic",
        "open interest", "oi", "bid", "ask", "spread", "mark", "index",
    )
    return any(sig in q for sig in stat_signals)


def _requested_market_fields(question: str) -> set[str]:
    q = _normalize_question(question)
    generic_full_signals = ("stats", "statistic", "overview", "snapshot", "all", "everything")
    if any(sig in q for sig in generic_full_signals):
        return set()

    fields = set()
    if any(sig in q for sig in ("price", "mid", "mark", "index", "quote", "trading at", "how much")):
        fields.add("price")
    if "funding" in q:
        fields.add("funding")
    if "volume" in q:
        fields.add("volume")
    if "open interest" in q or re.search(r"\boi\b", q):
        fields.add("open_interest")
    if "spread" in q or "bid" in q or "ask" in q:
        fields.add("spread")
    if "24h" in q or "24hr" in q or "24 hour" in q:
        fields.add("change_24h")
    if "high" in q or "low" in q or "range" in q:
        fields.add("range_24h")

    return fields


def _execute_price_brief(
    product: str,
    network: str = "mainnet",
    requested_fields: set[str] | None = None,
) -> tuple[str, list[str]]:
    from src.nadobro.config import get_product_id, get_perp_products
    from src.nadobro.venue.nado_client import NadoClient

    symbol = (product or "").strip().upper().replace("-PERP", "")
    product_id = get_product_id(symbol, network=network)
    if product_id is None:
        supported = get_perp_products(network=network)
        return f"[PRICE BRIEF] Unknown asset '{product}'. Supported: {', '.join(supported)}", []

    mid = None
    bid = 0.0
    ask = 0.0
    spread = 0.0
    spread_bps = 0.0
    funding_rate = None
    volume_24h_usd = None
    open_interest = None
    high_24h = None
    low_24h = None
    change_24h = None
    try:
        # NO_ORDERS_AUDIT-FIX-R6b: route through the digest-keyed cache.
        from src.nadobro.venue.nado_client import get_or_create_readonly_client
        client = get_or_create_readonly_client("0x0000000000000000000000000000000000000000", network)
        stats = client.get_product_market_stats(product_id)
        mid = float(stats.get("mid") or 0) or None
        bid = float(stats.get("bid") or 0)
        ask = float(stats.get("ask") or 0)
        spread = float(stats.get("spread") or 0)
        spread_bps = float(stats.get("spread_bps") or 0)
        funding_rate = stats.get("funding_rate")
        volume_24h_usd = stats.get("volume_24h_usd")
        open_interest = stats.get("open_interest")
        high_24h = stats.get("high_24h")
        low_24h = stats.get("low_24h")
        change_24h = stats.get("change_24h_pct")
    except Exception as e:
        logger.warning("Price brief live price fetch failed for %s: %s", symbol, e)

    cmc_sources = []
    if _is_cmc_available():
        try:
            from src.nadobro.market_data.cmc_client import get_crypto_quotes
            data = get_crypto_quotes([symbol]) or {}
            row = data.get(symbol) or {}
            quote_usd = ((row.get("quote") or {}).get("USD") or {})
            if change_24h is None:
                raw_change = quote_usd.get("percent_change_24h")
                if raw_change is not None:
                    change_24h = float(raw_change)
            if (mid is None or mid <= 0) and quote_usd.get("price") is not None:
                mid = float(quote_usd.get("price"))
            cmc_sources = ["https://coinmarketcap.com"]
        except Exception as e:
            logger.warning("Price brief CMC fetch failed for %s: %s", symbol, e)

    if mid is None or mid <= 0:
        return f"[PRICE BRIEF] Could not fetch current price for {symbol} right now.", []

    if change_24h is None:
        change_text = "unchanged"
    elif change_24h >= 0:
        change_text = f"up {abs(change_24h):.1f}%"
    else:
        change_text = f"down {abs(change_24h):.1f}%"

    fng_raw = _fetch_fear_greed_index()
    fng_value, fng_label = _parse_fng_snapshot(fng_raw)
    if fng_value is not None:
        fng_line = f"Fear & Greed Index is at {fng_value} ({fng_label})."
    else:
        fng_line = "Fear & Greed Index is unavailable right now."

    bias_line = _derive_price_bias(change_24h, fng_value)
    funding_line = (
        f"Funding: {float(funding_rate) * 100:.4f}%"
        if funding_rate is not None
        else "Funding: N/A"
    )
    volume_line = f"24h Volume: {_format_usd_compact(volume_24h_usd)}"
    oi_line = f"Open Interest: {_format_usd_compact(open_interest)}"

    range_parts = []
    if high_24h is not None:
        range_parts.append(f"H ${float(high_24h):,.2f}")
    if low_24h is not None:
        range_parts.append(f"L ${float(low_24h):,.2f}")
    range_line = f"24h Range: {' | '.join(range_parts)}" if range_parts else "24h Range: N/A"

    requested_fields = set(requested_fields or set())
    if requested_fields and len(requested_fields) <= 2:
        concise_lines = [f"[NADO MARKET STAT]", f"{symbol}-PERP on Nado"]
        if "price" in requested_fields:
            concise_lines.append(f"Price: ${mid:,.2f} ({change_text} 24h)")
        if "funding" in requested_fields:
            concise_lines.append(
                f"Funding: {float(funding_rate) * 100:.4f}%"
                if funding_rate is not None
                else "Funding: N/A"
            )
        if "volume" in requested_fields:
            concise_lines.append(f"24h Volume: {_format_usd_compact(volume_24h_usd)}")
        if "open_interest" in requested_fields:
            concise_lines.append(f"Open Interest: {_format_usd_compact(open_interest)}")
        if "spread" in requested_fields:
            concise_lines.append(
                f"Bid ${bid:,.2f} | Ask ${ask:,.2f} | Spread ${spread:,.2f} ({spread_bps:.1f} bps)"
            )
        if "change_24h" in requested_fields and "price" not in requested_fields:
            concise_lines.append(f"24h Change: {change_text}")
        if "range_24h" in requested_fields:
            concise_lines.append(range_line)
        result = "\n".join(concise_lines)
    else:
        result = (
            f"[NADO MARKET STATS]\n"
            f"{symbol}-PERP on Nado: ${mid:,.2f} ({change_text} 24h)\n"
            f"Bid ${bid:,.2f} | Ask ${ask:,.2f} | Spread ${spread:,.2f} ({spread_bps:.1f} bps)\n"
            f"{funding_line} | {volume_line} | {oi_line}\n"
            f"{range_line}\n"
            f"{fng_line}\n"
            f"{bias_line}"
        )
    return result, [OFFICIAL_SOURCES["website"], *cmc_sources]


def _execute_live_price(product: str, network: str = "mainnet") -> tuple[str, list[str]]:
    from src.nadobro.config import get_product_id, get_perp_products
    from src.nadobro.venue.nado_client import NadoClient

    symbol = product.strip().upper().replace("-PERP", "")
    product_id = get_product_id(symbol, network=network)

    if product_id is None:
        if symbol == "ALL":
            try:
                # NO_ORDERS_AUDIT-FIX-R6b: cached.
                from src.nadobro.venue.nado_client import get_or_create_readonly_client
                client = get_or_create_readonly_client("0x0000000000000000000000000000000000000000", network)
                prices = client.get_all_market_prices()
                lines = ["[LIVE PRICES FROM NADO DEX]"]
                for name, p in sorted(prices.items()):
                    if p.get("mid", 0) > 0:
                        lines.append(f"{name}-PERP: ${p['mid']:,.2f} (Bid: ${p['bid']:,.2f} / Ask: ${p['ask']:,.2f})")
                return "\n".join(lines), [OFFICIAL_SOURCES["website"]]
            except Exception as e:
                logger.warning(f"All prices fetch failed: {e}")
                return "[LIVE PRICE] Could not fetch prices right now.", []

        supported = get_perp_products(network=network)
        return f"[LIVE PRICE] Unknown asset '{product}'. Supported: {', '.join(supported)}", []

    try:
        # NO_ORDERS_AUDIT-FIX-R6b: cached.
        from src.nadobro.venue.nado_client import get_or_create_readonly_client
        client = get_or_create_readonly_client("0x0000000000000000000000000000000000000000", network)
        price_data = client.get_market_price(product_id)
        mid = price_data.get("mid", 0)
        bid = price_data.get("bid", 0)
        ask = price_data.get("ask", 0)
        spread = ask - bid if ask and bid else 0
        spread_bps = (spread / mid * 10000) if mid else 0

        result = (
            f"[LIVE PRICE FROM NADO DEX]\n"
            f"{symbol}-PERP:\n"
            f"  Mid Price: ${mid:,.2f}\n"
            f"  Bid: ${bid:,.2f}\n"
            f"  Ask: ${ask:,.2f}\n"
            f"  Spread: ${spread:,.2f} ({spread_bps:.1f} bps)"
        )
        return result, [OFFICIAL_SOURCES["website"]]
    except Exception as e:
        logger.warning(f"Live price fetch failed for {symbol}: {e}")
        return f"[LIVE PRICE] Could not fetch price for {symbol} right now.", []


def _execute_market_sentiment(query: str) -> tuple[str, list[str]]:
    fng = _fetch_fear_greed_index()

    client = _get_native_xai_client()  # Grok live-X sentiment needs native client
    if not client:
        return f"[MARKET SENTIMENT]\n{fng}\n\nxAI client not available for detailed sentiment.", []

    now = datetime.now(timezone.utc)
    try:
        response = client.chat.completions.create(
            model=XAI_X_SEARCH_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Today is {now.strftime('%Y-%m-%d')}. "
                        "Search crypto Twitter for the latest market sentiment, breaking news, and analysis. "
                        "Focus on posts from major crypto news accounts like @WatcherGuru, @CoinDesk, "
                        "@TheBlock__, @Cointelegraph, @whale_alert, and prominent crypto analysts. "
                        "Report key developments, price movements, regulatory news, and market-moving events. "
                        "Include tweet content with dates and handles. Plain text only."
                    ),
                },
                {"role": "user", "content": query},
            ],
            max_tokens=700,
            temperature=0.1,
            extra_body={"search_parameters": X_CRYPTO_SEARCH_PARAMS},
        )
        content = response.choices[0].message.content
        if content and content.strip():
            return (
                f"[MARKET SENTIMENT]\n{fng}\n\n{content.strip()}",
                [OFFICIAL_SOURCES["x_nado"]],
            )
    except Exception as e:
        logger.warning(f"Crypto Twitter sentiment search failed: {e}")

    return f"[MARKET SENTIMENT]\n{fng}\n\nNo additional sentiment data available.", []


def _execute_crypto_info(symbols_str: str) -> tuple[str, list[str]]:
    if not _is_cmc_available():
        return "[CRYPTO INFO] CoinMarketCap data not available — CMC_API_KEY not set.", []
    try:
        from src.nadobro.market_data.cmc_client import get_crypto_quotes, format_crypto_quote
        symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
        if not symbols:
            symbols = ["BTC"]
        data = get_crypto_quotes(symbols)
        if not data:
            return f"[CRYPTO INFO] No data found for: {', '.join(symbols)}", []
        lines = ["[CRYPTO MARKET DATA FROM COINMARKETCAP]"]
        for sym in symbols:
            if sym in data:
                lines.append(format_crypto_quote(data[sym]))
                lines.append("")
        return "\n".join(lines).strip(), ["https://coinmarketcap.com"]
    except Exception as e:
        logger.warning(f"CMC crypto info failed: {e}")
        return f"[CRYPTO INFO] Could not fetch data right now: {e}", []


def _execute_trending_cryptos() -> tuple[str, list[str]]:
    if not _is_cmc_available():
        return "[TRENDING] CoinMarketCap data not available — CMC_API_KEY not set.", []
    try:
        from src.nadobro.market_data.cmc_client import get_trending, format_trending
        data = get_trending()
        formatted = format_trending(data)
        if formatted and "No trending data" not in formatted:
            return f"[TRENDING CRYPTO DATA FROM COINMARKETCAP]\n{formatted}", ["https://coinmarketcap.com"]
        return "[TRENDING] Trending data requires a CoinMarketCap paid plan. Try asking about specific coins instead (e.g. 'how is BTC doing?').", []
    except Exception as e:
        logger.warning(f"CMC trending failed: {e}")
        return "[TRENDING] Trending data not available. Try asking about specific coins instead.", []


def _execute_global_market_data() -> tuple[str, list[str]]:
    if not _is_cmc_available():
        return "[GLOBAL MARKET] CoinMarketCap data not available — CMC_API_KEY not set.", []
    try:
        from src.nadobro.market_data.cmc_client import get_global_metrics, format_global_metrics
        data = get_global_metrics()
        formatted = format_global_metrics(data)
        fng = _fetch_fear_greed_index()
        return (
            f"[GLOBAL CRYPTO MARKET DATA FROM COINMARKETCAP]\n{formatted}\n{fng}",
            ["https://coinmarketcap.com"],
        )
    except Exception as e:
        logger.warning(f"CMC global market data failed: {e}")
        return f"[GLOBAL MARKET] Could not fetch global data right now: {e}", []


def _execute_agent_tool(tool_name: str, args: dict, question: str, network: str = "mainnet") -> tuple[str, list[str]]:
    if tool_name == "search_knowledge_base":
        query = args.get("query", question)
        sections = _search_knowledge_sections(query, top_k=5)

        # Supplement with relevant past Q&A from vector store
        qa_supplement = ""
        try:
            from src.nadobro.llm.vector_store import search_qa_history, is_available as _vs_ok
            if _vs_ok():
                qa_hits = search_qa_history(query, top_k=2)
                qa_parts = []
                for h in qa_hits:
                    if h.get("score", 0) > 0.8 and h.get("text"):
                        qa_parts.append(f"Q: {h.get('title', '')}\nA: {h['text']}")
                if qa_parts:
                    qa_supplement = "\n\n[RELATED PAST ANSWERS]\n" + "\n\n".join(qa_parts)
        except Exception:
            pass

        if sections:
            return f"[KNOWLEDGE BASE RESULTS]\n{sections}{qa_supplement}", _pick_sources_for_question(query, context_text=sections)
        return "[KNOWLEDGE BASE] No matching sections found." + qa_supplement, [OFFICIAL_SOURCES["docs"]]

    elif tool_name == "get_price_brief":
        product = args.get("product", "BTC")
        return _execute_price_brief(product, network=network)

    elif tool_name == "get_live_price":
        product = args.get("product", "BTC")
        return _execute_live_price(product, network=network)

    elif tool_name == "search_x_twitter":
        query = args.get("query", question)
        return _execute_x_search(query)

    elif tool_name == "get_market_sentiment":
        query = args.get("query", question)
        return _execute_market_sentiment(query)

    elif tool_name == "get_crypto_info":
        symbols_str = args.get("symbols", "BTC")
        return _execute_crypto_info(symbols_str)

    elif tool_name == "get_trending_cryptos":
        return _execute_trending_cryptos()

    elif tool_name == "get_global_market_data":
        return _execute_global_market_data()

    elif tool_name == "get_current_edges":
        return _execute_current_edges()

    return f"[ERROR] Unknown tool: {tool_name}", []


def _execute_current_edges() -> tuple[str, list[str]]:
    """Return cached edges from the edge scanner."""
    try:
        from src.nadobro.llm.edge_scanner import get_edges_context, get_cached_edges
        context = get_edges_context()
        if context:
            return context, [OFFICIAL_SOURCES["x_nado"]]
        return "[CURRENT EDGES] No active promotions or edges found right now.", [OFFICIAL_SOURCES["x_nado"]]
    except Exception as e:
        logger.warning(f"Edge tool failed: {e}")
        return "[CURRENT EDGES] Edge scanner unavailable.", []


def _run_agent_pipeline(question: str, provider: str, network: str = "mainnet") -> tuple[str, list[str]]:
    client = _get_xai_client() if provider == "xai" else _get_openai_client()
    if not client:
        raise RuntimeError(f"{provider.upper()} client not configured")

    now = datetime.now(timezone.utc)
    current_date = now.strftime("%Y-%m-%d")

    active_tools = AGENT_TOOLS
    cmc_enabled = _is_cmc_available()
    if not cmc_enabled:
        cmc_tool_names = {"get_crypto_info", "get_trending_cryptos", "get_global_market_data"}
        active_tools = [t for t in AGENT_TOOLS if t["function"]["name"] not in cmc_tool_names]
    router_system = ROUTER_SYSTEM_PROMPT_BASE.format(
        current_date=current_date,
        cmc_tools_section=ROUTER_CMC_TOOLS_SECTION if cmc_enabled else "",
        cmc_routing_rules=ROUTER_CMC_ROUTING_RULES if cmc_enabled else "",
        edge_tool_number=9 if cmc_enabled else 6,
    )
    router_model = _model_for(provider)

    try:
        router_response = client.chat.completions.create(
            model=router_model,
            messages=[
                {"role": "system", "content": router_system},
                {"role": "user", "content": question},
            ],
            tools=active_tools,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.0,
        )
    except Exception as e:
        logger.warning(f"Agent router call failed ({provider}): {e}")
        kb_context = _search_knowledge_sections(question, top_k=5)
        fallback_context = kb_context or _load_knowledge_base()[:6000]
        return fallback_context, _pick_sources_for_question(question, context_text=fallback_context)

    tool_calls = []
    if router_response.choices and router_response.choices[0].message.tool_calls:
        tool_calls = router_response.choices[0].message.tool_calls

    if not tool_calls:
        if _is_nado_specific_question(question):
            kb_context = _search_knowledge_sections(question, top_k=5)
            packed_context = f"[KNOWLEDGE BASE]\n{kb_context}"
            return packed_context, _pick_sources_for_question(question, context_text=packed_context)
        return "", []

    all_context_parts = []
    all_sources = []
    for tc in tool_calls:
        try:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except (json.JSONDecodeError, AttributeError):
            continue

        ctx, sources = _execute_agent_tool(fn_name, fn_args, question, network=network)
        all_context_parts.append(ctx)
        all_sources.extend(sources)

    combined_context = "\n\n".join(all_context_parts) if all_context_parts else ""

    if not combined_context.strip():
        kb_context = _search_knowledge_sections(question, top_k=5)
        combined_context = f"[KNOWLEDGE BASE]\n{kb_context}"

    if not all_sources:
        all_sources = _pick_sources_for_question(question, context_text=combined_context)

    return combined_context, list(dict.fromkeys(all_sources))


def _filter_official_sources(sources: list[str]) -> list[str]:
    allowed = set(OFFICIAL_SOURCES.values()) | {"https://coinmarketcap.com", "https://x.com"}
    filtered = [s for s in sources if s in allowed]
    return filtered[:3]


def _append_freshness(answer: str, limit: int = 3) -> str:
    try:
        from src.nadobro.connectors.source_registry import freshness_footer

        footer = freshness_footer(limit=limit)
    except Exception:
        footer = ""
    if not footer or "Data freshness:" in (answer or ""):
        return answer
    return f"{answer}\n\n{footer}"


_LIVE_CONTEXT_MARKERS = (
    "[PRICE BRIEF]",
    "[MARKET SENTIMENT]",
    "[LIVE PRICE]",
    "[CRYPTO INFO]",
    "[GLOBAL MARKET]",
    "[TRENDING]",
    "[CURRENT EDGES",
    "[X/TWITTER RESULTS]",
)


def _context_uses_live_data(question: str, context_text: str = "") -> bool:
    routed = _question_for_routing(question)
    return (
        _is_price_question(routed)
        or _is_sentiment_question(routed)
        or _is_x_twitter_question(routed)
        or any(marker in (context_text or "") for marker in _LIVE_CONTEXT_MARKERS)
    )


def _should_include_provenance(question: str, context_text: str, sources: list[str]) -> bool:
    if not sources:
        return False
    routed = _question_for_routing(question)
    return (
        _context_uses_live_data(routed, context_text)
        or _is_nado_specific_question(routed)
        or _is_ink_question(routed)
    )


def _format_provenance_line(sources: list[str]) -> str:
    labels = {
        "https://docs.nado.xyz": "Nado docs",
        "https://docs.nado.xyz/points/referrals": "Nado docs",
        "https://docs.nado.xyz/developer-resources/api/gateway": "Nado docs",
        "https://docs.nado.xyz/developer-resources/get-started": "Nado docs",
        "https://x.com/nadoHQ": "Nado on X",
        "https://x.com/inkonchain": "Ink on X",
        "https://x.com": "X",
        "https://coinmarketcap.com": "CoinMarketCap",
    }
    seen: list[str] = []
    for source in sources:
        label = labels.get(source, source.replace("https://", "").rstrip("/"))
        if label not in seen:
            seen.append(label)
    if not seen:
        return ""
    return "Based on: " + ", ".join(seen[:3])


def _append_freshness_if_live(answer: str, question: str, context_text: str = "") -> str:
    return answer


def _extract_x_status_links(context_text: str, limit: int = 3) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"https://x\.com/[A-Za-z0-9_]+/status/\d+(?:\?s=\d+)?", context_text or ""):
        url = match.group(0)
        if url not in links:
            links.append(url)
        if len(links) >= limit:
            break
    return links


def _append_x_links_if_needed(answer: str, context_text: str) -> str:
    if "x.com/" in (answer or "") and "/status/" in (answer or ""):
        return answer
    links = _extract_x_status_links(context_text)
    if not links:
        return answer
    label = "Tweet link" if len(links) == 1 else "Tweet links"
    return f"{answer}\n\n{label}: " + " | ".join(links)


def _stream_support_llm(provider: str, system: str, question: str, x_search: bool = False, history: list[dict] = None):
    # Grok live-X search only works on a NATIVE Grok client; everything else
    # goes through the gateway-preferred general client.
    native_x = x_search and provider == "xai" and _get_native_xai_client() is not None
    if native_x:
        client = _get_native_xai_client()
    elif provider == "openai":
        client = _get_openai_client()
    else:
        client = _get_xai_client()

    if not client:
        raise RuntimeError(f"{provider.upper()} client not configured")

    max_tokens = 1800 if (_wants_detailed_answer(question) or x_search) else 1200

    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history[-CHAT_HISTORY_MAX_MESSAGES:])
    messages.append({"role": "user", "content": question})

    kwargs = dict(
        model=XAI_X_SEARCH_MODEL if native_x else _model_for(provider),
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.5,
        stream=True,
    )
    if native_x:
        kwargs["extra_body"] = {"search_parameters": _pick_x_search_params(question)}

    stream = client.chat.completions.create(**kwargs)
    for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            yield delta.content


def _build_x_context_via_tool(question: str) -> tuple[str, list[str]]:
    """Fetch X context via the direct X tool path (X API first, Grok fallback)."""
    try:
        return _execute_x_search(question)
    except Exception as e:
        logger.warning("X context tool failed: %s", e)
        return "", []


def _is_points_distribution_question(question: str) -> bool:
    q = _normalize_question(question)
    points_signals = [
        "points distributed", "points been distributed", "points dropped",
        "points this week", "weekly points", "points yet", "when are points",
        "when do points", "point distribution", "points distribution",
        "points come", "points arrived", "got my points", "received points",
        "points update", "epoch", "weekly epoch", "points epoch",
        "rewards distributed", "rewards this week", "rewards dropped",
    ]
    return any(sig in q for sig in points_signals)


def _is_x_twitter_question(question: str) -> bool:
    q = _normalize_question(question)
    nado_signals = ["tweet", "tweets", "x.com", "twitter", "post on x", "posted on x", "nadohq", "inkonchain"]
    broad_signals = ["crypto news", "latest news", "ct saying", "crypto twitter", "any alpha", "what's alpha",
                     "whats alpha", "news today", "breaking news", "latest from ct"]
    explicit_x_patterns = [
        r"\bsearch\s+(on\s+)?(x|twitter)\b",
        r"\b(x|twitter)\s+(for|about)\b",
        r"\bwhat\s+is\s+(x|twitter)\s+saying\s+about\b",
        r"\btrending\s+on\s+(x|twitter)\b",
        r"\bfind\s+(tweets|posts)\s+about\b",
    ]
    if any(sig in q for sig in nado_signals):
        return True
    if any(sig in q for sig in broad_signals):
        return True
    if any(re.search(pat, q) for pat in explicit_x_patterns):
        return True
    if _is_points_distribution_question(question):
        return True
    return False


def _is_nado_x_question(question: str) -> bool:
    nado_signals = ["nado", "nadohq", "inkonchain", "ink l2", "nado dex"]
    if _is_points_distribution_question(question):
        return True
    return any(_signal_present(question, sig) for sig in nado_signals)


def _pick_x_search_params(question: str) -> dict:
    if _is_nado_x_question(question):
        return X_NADO_SEARCH_PARAMS
    return X_CRYPTO_SEARCH_PARAMS


def _is_price_question(question: str) -> bool:
    q = _normalize_question(question)
    price_signals = ["price of", "price for", "how much is", "what's btc", "what's eth",
                     "btc price", "eth price", "sol price", "current price", "live price",
                     "market price", "what is btc at", "what is eth at",
                     "market cap", "trending", "gainers", "losers", "pumping", "dumping",
                     "dominance", "total market", "how is", "performing"]
    return any(sig in q for sig in price_signals)


def _is_nado_specific_question(question: str) -> bool:
    nado_signals = (
        "nado", "ink l2", "inkonchain", "nado dex",
        "fee", "fees", "margin", "leverage", "liquidation", "deposit", "withdraw",
        "wallet", "funding", "unified margin", "nlp", "vault",
        "points", "rewards", "referral", "invite code", "season",
        "templars", "nft", "templar", "subaccount",
        "1-click", "1ct", "linked signer", "how to trade on nado",
    )
    return any(_signal_present(question, sig) for sig in nado_signals)


def _should_skip_router(question: str) -> bool:
    q = _normalize_question(_question_for_routing(question))
    if not q:
        return True
    if _is_price_question(q) or _is_sentiment_question(q) or _is_x_twitter_question(q):
        return False
    if _is_nado_specific_question(q):
        return True
    if _is_ink_question(q):
        return True
    return False


async def stream_nado_answer(question: str, telegram_id: int = None, user_name: str = None):
    started_at = time.time()
    xai_client = _get_xai_client()
    openai_client = _get_openai_client()
    if not xai_client and not openai_client:
        yield "AI service is not configured. Add XAI_API_KEY and/or OPENAI_API_KEY then restart the bot."
        return

    display_name = user_name or "trader"
    history_msgs = _build_history_messages(telegram_id) if telegram_id else []

    if telegram_id:
        _add_to_chat_history(telegram_id, "user", question)

    now = datetime.now(timezone.utc)
    current_date = now.strftime("%Y-%m-%d")

    lang = get_active_language()
    lang_name = LANGUAGE_LABELS.get(lang, "English")
    lang_instruction = (
        f"IMPORTANT: The user's preferred language is {lang} ({lang_name}). "
        f"Always respond entirely in {lang_name}, including headings and the actionable insight line. "
        f"Keep ticker symbols, numbers, prices, code, and wallet addresses unchanged."
    ) if lang != "en" else ""

    if _is_casual_message(question):
        system = CASUAL_SYSTEM_PROMPT.format(
            current_date=current_date,
            user_name=display_name,
            language_instruction=lang_instruction,
        )
        primary = _pick_primary_provider(question)
        try:
            full_answer = ""
            import queue, threading
            chunk_queue = queue.Queue()

            def _run_casual(p=primary):
                try:
                    for chunk_text in _stream_support_llm(p, system, question, history=history_msgs):
                        chunk_queue.put(chunk_text)
                    chunk_queue.put(None)
                except Exception as e:
                    chunk_queue.put(e)

            thread = threading.Thread(target=_run_casual, daemon=True)
            thread.start()

            while True:
                try:
                    item = await _llm_thread(chunk_queue.get, timeout=15)
                except Exception:
                    break
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                full_answer += item
                yield item

            if full_answer.strip() and telegram_id:
                _add_to_chat_history(telegram_id, "assistant", full_answer.strip())
            if full_answer.strip():
                logger.info("Casual response via %s in %.1fs", primary, time.time() - started_at)
                return
        except Exception as e:
            logger.warning(f"Casual response failed: {e}")

        from src.nadobro.i18n import localize_text as _lt
        fallback_msg = _lt(f"Yo {display_name}! What's good? Ready to talk crypto, drop alpha, or just vibe. What do you need?", lang)
        yield fallback_msg
        if telegram_id:
            _add_to_chat_history(telegram_id, "assistant", fallback_msg)
        return

    # Check if X questions can be handled: either via Grok (xai_client) or X API v2
    _x_api_ready = False
    try:
        from src.nadobro.market_data.x_api_client import is_available as _x_is_available
        _x_api_ready = _x_is_available()
    except Exception:
        pass
    if _is_x_twitter_question(question) and not xai_client and not _x_api_ready:
        from src.nadobro.i18n import localize_text as _lt
        yield _lt("X/Twitter search needs my xAI connection, and it's not set up right now. Hit me with a different question!", lang)
        return

    user_network = _get_user_network(telegram_id) if telegram_id else "mainnet"
    market_product = _extract_market_product(question, network=user_network)
    if market_product and _is_market_stats_question(question):
        requested_fields = _requested_market_fields(question)
        direct_answer, _ = _execute_price_brief(
            market_product,
            network=user_network,
            requested_fields=requested_fields,
        )
        if telegram_id:
            _add_to_chat_history(telegram_id, "assistant", direct_answer)
        yield _append_freshness(direct_answer)
        return

    _load_knowledge_base()

    is_x_question = _is_x_twitter_question(question)
    # Prefer the direct X tool path (X API first, then Grok fallback) for real
    # X questions. Grok's live-X search needs a NATIVE Grok client (the
    # extra_body extension is Grok-only), so gate on that — never on the
    # gateway client, or NanoGPT would receive a param it doesn't understand.
    use_x_prompt = is_x_question and _get_native_xai_client() is not None and not _x_api_ready

    gathered_context = ""
    if is_x_question and _x_api_ready:
        gathered_context, used_sources = _build_x_context_via_tool(question)
        if not gathered_context.strip():
            gathered_context = "[X/TWITTER RESULTS]\nNo recent X results found."
        system = SYNTHESIZER_SYSTEM_PROMPT.format(
            current_date=current_date,
            user_name=display_name,
            context=gathered_context[:14000],
            language_instruction=lang_instruction,
        )
    elif use_x_prompt:
        is_nado_x = _is_nado_x_question(question)
        if is_nado_x:
            system = X_TWITTER_NADO_PROMPT.format(
                knowledge_base=_search_knowledge_sections(question, top_k=2),
                current_date=current_date,
                current_year=str(now.year),
                language_instruction=lang_instruction,
            )
            used_sources = [OFFICIAL_SOURCES["x_nado"], OFFICIAL_SOURCES["x_ink"]]
        else:
            system = X_TWITTER_BROAD_PROMPT.format(
                current_date=current_date,
                current_year=str(now.year),
                language_instruction=lang_instruction,
            )
            used_sources = ["https://x.com"]
        gathered_context = "[X/TWITTER RESULTS]"
    else:
        primary = _pick_primary_provider(question)
        if _should_skip_router(question):
            gathered_context = _search_knowledge_sections(question, top_k=5)
            used_sources = _pick_sources_for_question(question, context_text=gathered_context)
        else:
            try:
                gathered_context, used_sources = await _llm_thread(
                    _run_agent_pipeline, question, primary, user_network
                )
            except Exception as e:
                logger.warning(f"Agent pipeline failed: {e}")
                gathered_context = _search_knowledge_sections(question, top_k=5)
                used_sources = _pick_sources_for_question(question, context_text=gathered_context)

        # Inject active edges/promotions into context when relevant
        if "[CURRENT EDGES" not in gathered_context:
            try:
                from src.nadobro.llm.edge_scanner import get_edges_context
                edges_ctx = get_edges_context()
                if edges_ctx:
                    gathered_context = gathered_context + "\n\n" + edges_ctx
            except Exception:
                pass

        system = SYNTHESIZER_SYSTEM_PROMPT.format(
            current_date=current_date,
            user_name=display_name,
            context=gathered_context[:14000],
            language_instruction=lang_instruction,
        )

    used_sources = _filter_official_sources(used_sources)
    provenance_line = (
        _format_provenance_line(used_sources)
        if _should_include_provenance(question, gathered_context, used_sources)
        else ""
    )

    primary = _pick_primary_provider(question)
    if use_x_prompt:
        primary = "xai"
    secondary = "openai" if primary == "xai" else "xai"
    providers = [primary, secondary]
    providers = [
        p for p in providers
        if (p == "xai" and xai_client) or (p == "openai" and openai_client)
    ]

    import queue, threading

    for provider in providers:
        try:
            chunk_queue = queue.Queue()

            def _run_stream(p=provider, xs=use_x_prompt):
                try:
                    for chunk_text in _stream_support_llm(p, system, question, x_search=xs, history=history_msgs):
                        chunk_queue.put(chunk_text)
                    chunk_queue.put(None)
                except Exception as e:
                    chunk_queue.put(e)

            thread = threading.Thread(target=_run_stream, daemon=True)
            thread.start()

            full_answer = ""
            while True:
                try:
                    item = await _llm_thread(chunk_queue.get, timeout=30)
                except Exception:
                    break
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                full_answer += item
                yield item

            if not full_answer.strip():
                continue

            if provenance_line and "Based on:" not in full_answer and "Sources:" not in full_answer:
                addition = "\n\n" + provenance_line
                yield addition
                full_answer += addition
            freshened = _append_freshness_if_live(full_answer, question, gathered_context)
            if freshened != full_answer:
                freshness_addition = freshened[len(full_answer):]
                yield freshness_addition
                full_answer = freshened
            if "[X/TWITTER RESULTS" in gathered_context:
                linked = _append_x_links_if_needed(full_answer, gathered_context)
                if linked != full_answer:
                    link_addition = linked[len(full_answer):]
                    yield link_addition
                    full_answer = linked

            if telegram_id:
                _add_to_chat_history(telegram_id, "assistant", full_answer.strip())

            _answer_cache[_normalize_question(question)] = {"ts": time.time(), "answer": full_answer}

            # Index unique Q&A pairs into Pinecone for future retrieval
            try:
                from src.nadobro.llm.vector_store import index_qa_if_unique, is_available as _vs_available
                if _vs_available() and not _is_casual_message(question):
                    import threading
                    threading.Thread(
                        target=index_qa_if_unique,
                        args=(question, full_answer),
                        daemon=True,
                    ).start()
            except Exception:
                pass

            logger.info("Streamed answer via %s in %.1fs", provider, time.time() - started_at)
            return
        except Exception as provider_error:
            logger.warning("Stream answer failed on provider=%s: %s", provider, provider_error)
            continue

    from src.nadobro.i18n import localize_text as _lt
    yield _lt("I couldn't generate an answer. Please try again.", lang)


async def answer_nado_question(question: str, telegram_id: int = None, user_name: str = None) -> str:
    started_at = time.time()
    xai_client = _get_xai_client()
    openai_client = _get_openai_client()
    if not xai_client and not openai_client:
        return (
            "AI service is not configured. Add XAI_API_KEY and/or OPENAI_API_KEY "
            "then restart the bot."
        )

    display_name = user_name or "trader"
    history_msgs = _build_history_messages(telegram_id) if telegram_id else []

    if telegram_id:
        _add_to_chat_history(telegram_id, "user", question)

    now = datetime.now(timezone.utc)
    current_date = now.strftime("%Y-%m-%d")

    lang = get_active_language()
    lang_name = LANGUAGE_LABELS.get(lang, "English")
    lang_instruction = (
        f"IMPORTANT: The user's preferred language is {lang} ({lang_name}). "
        f"Always respond entirely in {lang_name}, including headings and the actionable insight line. "
        f"Keep ticker symbols, numbers, prices, code, and wallet addresses unchanged."
    ) if lang != "en" else ""

    if _is_casual_message(question):
        system = CASUAL_SYSTEM_PROMPT.format(current_date=current_date, user_name=display_name, language_instruction=lang_instruction)
        primary = _pick_primary_provider(question)
        try:

            def _call_casual(p=primary):
                client = _get_xai_client() if p == "xai" else _get_openai_client()
                messages = [{"role": "system", "content": system}]
                if history_msgs:
                    messages.extend(history_msgs[-CHAT_HISTORY_MAX_MESSAGES:])
                messages.append({"role": "user", "content": question})
                resp = client.chat.completions.create(
                    model=_model_for(p), messages=messages, max_tokens=500, temperature=0.6,
                )
                return resp.choices[0].message.content.strip()

            answer = await _llm_thread(_call_casual)
            if answer and telegram_id:
                _add_to_chat_history(telegram_id, "assistant", answer)
            return answer
        except Exception as e:
            logger.warning(f"Casual answer failed: {e}")
            from src.nadobro.i18n import localize_text as _lt
            fallback = _lt(f"Yo {display_name}! What's good? I'm ready to talk crypto, drop alpha, or just vibe. What do you need?", lang)
            if telegram_id:
                _add_to_chat_history(telegram_id, "assistant", fallback)
            return fallback

    # Check if X questions can be handled: either via Grok (xai_client) or X API v2
    _x_api_ready2 = False
    try:
        from src.nadobro.market_data.x_api_client import is_available as _x_is_available2
        _x_api_ready2 = _x_is_available2()
    except Exception:
        pass
    if _is_x_twitter_question(question) and not xai_client and not _x_api_ready2:
        from src.nadobro.i18n import localize_text as _lt
        return _lt("X/Twitter search needs my xAI connection, and it's not set up right now. Hit me with a different question!", lang)

    user_network = _get_user_network(telegram_id) if telegram_id else "mainnet"
    market_product = _extract_market_product(question, network=user_network)
    if market_product and _is_market_stats_question(question):
        requested_fields = _requested_market_fields(question)
        direct_answer, _ = _execute_price_brief(
            market_product,
            network=user_network,
            requested_fields=requested_fields,
        )
        if telegram_id:
            _add_to_chat_history(telegram_id, "assistant", direct_answer)
        return _append_freshness(direct_answer)

    _load_knowledge_base()

    is_x_question = _is_x_twitter_question(question)
    # Prefer the direct X tool path (X API first, then Grok fallback) for
    # real X questions. This avoids generic answers when xAI credentials exist
    # but the selected support model is not permitted.
    use_x_prompt = is_x_question and xai_client is not None and not _x_api_ready2

    gathered_context = ""
    if is_x_question and _x_api_ready2:
        gathered_context, used_sources = _build_x_context_via_tool(question)
        if not gathered_context.strip():
            gathered_context = "[X/TWITTER RESULTS]\nNo recent X results found."
        system = SYNTHESIZER_SYSTEM_PROMPT.format(
            current_date=current_date,
            user_name=display_name,
            context=gathered_context[:14000],
            language_instruction=lang_instruction,
        )
    elif use_x_prompt:
        is_nado_x = _is_nado_x_question(question)
        if is_nado_x:
            system = X_TWITTER_NADO_PROMPT.format(
                knowledge_base=_search_knowledge_sections(question, top_k=2),
                current_date=current_date,
                current_year=str(now.year),
                language_instruction=lang_instruction,
            )
            used_sources = [OFFICIAL_SOURCES["x_nado"], OFFICIAL_SOURCES["x_ink"]]
        else:
            system = X_TWITTER_BROAD_PROMPT.format(
                current_date=current_date,
                current_year=str(now.year),
                language_instruction=lang_instruction,
            )
            used_sources = ["https://x.com"]
        gathered_context = "[X/TWITTER RESULTS]"
    else:
        primary = _pick_primary_provider(question)
        if _should_skip_router(question):
            gathered_context = _search_knowledge_sections(question, top_k=5)
            used_sources = _pick_sources_for_question(question, context_text=gathered_context)
        else:
            try:
                gathered_context, used_sources = await _llm_thread(
                    _run_agent_pipeline, question, primary, user_network
                )
            except Exception as e:
                logger.warning(f"Agent pipeline failed: {e}")
                gathered_context = _search_knowledge_sections(question, top_k=5)
                used_sources = _pick_sources_for_question(question, context_text=gathered_context)

        # Inject active edges/promotions into context when relevant
        if "[CURRENT EDGES" not in gathered_context:
            try:
                from src.nadobro.llm.edge_scanner import get_edges_context
                edges_ctx = get_edges_context()
                if edges_ctx:
                    gathered_context = gathered_context + "\n\n" + edges_ctx
            except Exception:
                pass

        system = SYNTHESIZER_SYSTEM_PROMPT.format(
            current_date=current_date,
            user_name=display_name,
            context=gathered_context[:14000],
            language_instruction=lang_instruction,
        )

    used_sources = _filter_official_sources(used_sources)
    provenance_line = (
        _format_provenance_line(used_sources)
        if _should_include_provenance(question, gathered_context, used_sources)
        else ""
    )

    try:
        primary = _pick_primary_provider(question)
        if use_x_prompt:
            primary = "xai"
        secondary = "openai" if primary == "xai" else "xai"

        providers = [primary, secondary]
        providers = [
            p for p in providers
            if (p == "xai" and xai_client) or (p == "openai" and openai_client)
        ]

        last_error = None
        answer = None
        used_provider = None
        for provider in providers:
            try:
                def _call(p=provider):
                    # X-search (extra_body below) needs the NATIVE Grok client;
                    # all other reasoning uses the gateway-preferred client.
                    if use_x_prompt and p == "xai":
                        client = _get_native_xai_client()
                    else:
                        client = _get_xai_client() if p == "xai" else _get_openai_client()
                    max_tokens = 1400 if (_wants_detailed_answer(question) or use_x_prompt) else 650
                    messages = [{"role": "system", "content": system}]
                    if history_msgs:
                        messages.extend(history_msgs[-CHAT_HISTORY_MAX_MESSAGES:])
                    messages.append({"role": "user", "content": question})
                    kwargs = dict(
                        model=XAI_X_SEARCH_MODEL if (use_x_prompt and p == "xai") else _model_for(p),
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=0.5,
                    )
                    if use_x_prompt and p == "xai":
                        kwargs["extra_body"] = {"search_parameters": _pick_x_search_params(question)}
                    resp = client.chat.completions.create(**kwargs)
                    content = resp.choices[0].message.content
                    if not content or not content.strip():
                        raise RuntimeError(f"{p.upper()} returned empty response")
                    return content.strip()

                answer = await _llm_thread(_call)
                used_provider = provider
                break
            except Exception as provider_error:
                last_error = provider_error
                logger.warning("Support answer failed on provider=%s: %s", provider, provider_error)
                continue

        if not answer:
            if last_error:
                raise last_error
            from src.nadobro.i18n import localize_text as _lt
            return _lt("I couldn't generate an answer. Please try again.", lang)

        logger.info("Support answer generated via provider=%s in %.1fs", used_provider, time.time() - started_at)
        if provenance_line and "Sources:" not in answer and "Based on:" not in answer:
            answer = f"{answer}\n\n{provenance_line}"
        answer = _append_freshness_if_live(answer, question, gathered_context)
        if "[X/TWITTER RESULTS" in gathered_context:
            answer = _append_x_links_if_needed(answer, gathered_context)

        if telegram_id:
            _add_to_chat_history(telegram_id, "assistant", answer)

        _answer_cache[_normalize_question(question)] = {"ts": time.time(), "answer": answer}
        return answer
    except Exception as e:
        logger.error(f"Knowledge Q&A failed: {e}", exc_info=True)
        return "Something went wrong while answering your question. Please try again."
