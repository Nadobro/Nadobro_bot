"""Background edge scanner that finds trading edges, promotions, and multipliers.

Primary: Fetches tweets from @nadoHQ and @inkonchain via X API v2, then
         feeds them to an LLM (Grok/GPT-4o) to extract structured edges.
Fallback: Uses Grok's built-in X search when X API is unavailable.

Runs every 30 minutes via the scheduler.
Stores findings in an in-memory cache and optionally indexes them in Pinecone.
"""

import json
import logging
import os

from src.nadobro.utils.env import env_int
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── In-memory edge cache ─────────────────────────────────────────────

_edge_cache: dict = {
    "edges": [],
    "last_scan": 0.0,
}

SCAN_INTERVAL_SECONDS = env_int("EDGE_SCAN_INTERVAL_SECONDS", 1800)  # 30 min


def get_recent_findings(limit: int = 10) -> list[dict]:
    """Return the most recent edge findings from the in-memory cache.

    Used by the morning brief composer to surface live Nado promos /
    multipliers (e.g. "4x WTI this week") without re-running a scan.
    """
    edges = _edge_cache.get("edges") or []
    if not isinstance(edges, list):
        return []
    return list(edges[: max(0, int(limit))])


def last_scan_timestamp() -> float:
    try:
        return float(_edge_cache.get("last_scan") or 0.0)
    except Exception:
        return 0.0

# ── LLM analysis prompt ─────────────────────────────────────────────

EDGE_ANALYSIS_PROMPT = """You are an alpha scanner for Nado DEX traders.

Today's date: {current_date}

Below are the latest tweets from @nadoHQ and @inkonchain. Analyze them and extract ANY trading edges:

1. Point multipliers (e.g., "2x points on ETH", "4x multiplier on WTI")
2. New trading pair listings
3. Campaigns or promotions (trading competitions, fee discounts, bonus programs)
4. Points distribution announcements (weekly epoch drops)
5. Fee changes or special rebates
6. Partnership announcements that affect trading
7. Any other trading edge or alpha opportunity

For EACH finding, output a JSON array of objects with these fields:
- "type": one of "multiplier", "listing", "campaign", "points", "fees", "partnership", "alpha"
- "title": short headline (under 80 chars)
- "detail": 1-2 sentence description with specific numbers/dates
- "source_url": the tweet URL

If NONE of the tweets contain actionable edges, return an empty array: []

IMPORTANT: Only extract REAL edges from the tweets below. Do NOT make up information.
Output ONLY the JSON array, nothing else.

--- TWEETS ---
{tweets}"""

# Fallback prompt for when X API is unavailable (Grok's built-in X search)
EDGE_SCAN_GROK_PROMPT = """You are an alpha scanner. Search the latest posts from @nadoHQ and @inkonchain on X (Twitter).

Today's date: {current_date}

Find and extract ANY of the following:
1. Point multipliers (e.g., "2x points on ETH", "4x multiplier on WTI")
2. New trading pair listings
3. Campaigns or promotions (trading competitions, fee discounts, bonus programs)
4. Points distribution announcements (weekly epoch drops)
5. Fee changes or special rebates
6. Partnership announcements that affect trading
7. Any other trading edge or alpha opportunity

For EACH finding, output a JSON array of objects with these fields:
- "type": one of "multiplier", "listing", "campaign", "points", "fees", "partnership", "alpha"
- "title": short headline (under 80 chars)
- "detail": 1-2 sentence description with specific numbers/dates
- "source_url": tweet URL if available, otherwise "https://x.com/nadoHQ"

If you find NOTHING relevant or recent, return an empty array: []

IMPORTANT: Only include findings from the past 7 days. Output ONLY the JSON array, nothing else."""

GROK_SEARCH_PARAMS = {
    "mode": "on",
    "sources": [{"type": "x", "x_handles": ["nadoHQ", "inkonchain"]}],
}


# ── LLM client (cached) ─────────────────────────────────────────────

_xai_client = None
_openai_client = None


def _get_xai_client():
    """Get xAI client (cached)."""
    global _xai_client
    if _xai_client is not None:
        return _xai_client
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return None
    from openai import OpenAI
    _xai_client = OpenAI(base_url="https://api.x.ai/v1", api_key=api_key)
    return _xai_client


def _get_openai_client():
    """Get OpenAI client (cached)."""
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    from openai import OpenAI
    _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _get_analysis_client():
    """Pick the best available LLM client for edge analysis. NanoGPT gateway
    (one key: Claude / GPT / DMind) is primary; native Grok/OpenAI fall back."""
    try:
        from src.nadobro.llm.llm_gateway import chat_client, model_for

        gw = chat_client()
        if gw is not None:
            return gw, model_for("scan")
    except Exception:  # policy: degrade-ok(fall back to native providers)
        pass
    xai = _get_xai_client()
    if xai:
        return xai, os.environ.get("BRO_SCAN_MODEL", "grok-3-mini-fast")
    openai = _get_openai_client()
    if openai:
        return openai, os.environ.get("OPENAI_SUPPORT_MODEL", "gpt-4o")
    return None, None


# ── Core scanning logic ──────────────────────────────────────────────

def _parse_llm_json(raw: str) -> list[dict]:
    """Parse JSON from LLM response, handling markdown code blocks."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

    findings = json.loads(raw)
    if not isinstance(findings, list):
        return []
    return findings


def _scan_via_x_api():
    """Fetch tweets via X API v2 and analyze with LLM.

    Returns list of edges, or None if X API is unavailable (triggers fallback).
    """
    try:
        from src.nadobro.market_data.x_api_client import is_available, get_nado_tweets, format_tweets_for_edge_analysis
    except ImportError:
        return None

    if not is_available():
        return None

    # Step 1: Fetch raw tweets via X API
    tweets = get_nado_tweets(max_results=30, hours_back=168)
    if not tweets:
        logger.info("Edge scanner: X API returned no tweets")
        return []

    formatted_tweets = format_tweets_for_edge_analysis(tweets)
    logger.info("Edge scanner: fetched %d tweets via X API, analyzing...", len(tweets))

    # Step 2: Feed tweets to LLM for structured edge extraction
    client, model = _get_analysis_client()
    if not client:
        logger.warning("Edge scanner: no LLM client for analysis")
        return None

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EDGE_ANALYSIS_PROMPT.format(
                    current_date=current_date,
                    tweets=formatted_tweets,
                )},
                {"role": "user", "content": "Extract all trading edges from these tweets."},
            ],
            max_tokens=1500,
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()
        return _parse_llm_json(raw)
    except json.JSONDecodeError:
        logger.warning("Edge scanner: LLM returned invalid JSON for X API tweets")
        return []
    except Exception as e:
        status = getattr(e, "status_code", None)
        if status == 403:
            logger.warning("Edge scanner: LLM analysis permission denied (403); skipping scan cycle")
            return []
        logger.warning("Edge scanner: LLM analysis failed: %s", e)
        return None


def _scan_via_grok_search():
    """Fallback: Use Grok's built-in X search to find edges."""
    client = _get_xai_client()
    if not client:
        return None

    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scan_model = os.environ.get("BRO_SCAN_MODEL", "grok-3-mini-fast")

    try:
        response = client.chat.completions.create(
            model=scan_model,
            messages=[
                {"role": "system", "content": EDGE_SCAN_GROK_PROMPT.format(current_date=current_date)},
                {"role": "user", "content": "Scan @nadoHQ and @inkonchain for the latest trading edges, promotions, multipliers, and alpha from the past 7 days."},
            ],
            max_tokens=1500,
            temperature=0.1,
            extra_body={"search_parameters": GROK_SEARCH_PARAMS},
        )
        raw = response.choices[0].message.content.strip()
        return _parse_llm_json(raw)
    except json.JSONDecodeError:
        logger.warning("Edge scanner: Grok returned invalid JSON")
        return None
    except Exception as e:
        status = getattr(e, "status_code", None)
        if status == 410:
            logger.warning("Edge scanner: Grok live search deprecated (410); disabling fallback search")
            return []
        if status == 403:
            logger.warning("Edge scanner: Grok search permission denied (403)")
            return []
        logger.warning("Edge scanner: Grok search failed: %s", e)
        return None


def scan_edges() -> list[dict]:
    """Run a scan for trading edges. X API first, Grok fallback.

    Returns list of edge dicts and updates the in-memory cache.
    """
    global _edge_cache

    # Try X API first (real tweets → LLM analysis)
    findings = _scan_via_x_api()

    # Fallback to Grok's built-in X search. NOTE: _scan_via_x_api returns None
    # both when the tweet source is down AND when the LLM analysis failed, so log
    # the true reason instead of always blaming "X API unavailable" (which had
    # the operator chasing a healthy X API while the real fault was the model id).
    if findings is None:
        if _is_x_api_available():
            logger.info(
                "Edge scanner: X API is up but tweet analysis produced no result "
                "(see the preceding warning for the real cause); trying Grok search"
            )
        else:
            logger.info("Edge scanner: X API unavailable, falling back to Grok search")
        findings = _scan_via_grok_search()

    if findings is None:
        logger.warning("Edge scanner: all methods failed")
        return _edge_cache.get("edges", [])

    # Stamp each finding
    now = time.time()
    for f in findings:
        f["found_at"] = now

    # Update cache
    _edge_cache = {
        "edges": findings,
        "last_scan": now,
    }

    source = "X API" if _is_x_api_available() else "Grok"
    logger.info("Edge scanner found %d edges via %s", len(findings), source)

    # Index into Pinecone if available
    if findings:
        try:
            from src.nadobro.llm.vector_store import index_x_findings_batch, is_available
            if is_available():
                index_x_findings_batch(findings)
        except Exception:
            logger.debug("Failed to index edges to Pinecone", exc_info=True)

    return findings


def _is_x_api_available() -> bool:
    try:
        from src.nadobro.market_data.x_api_client import is_available
        return is_available()
    except Exception:
        return False


# ── Public API ───────────────────────────────────────────────────────

def get_cached_edges() -> list[dict]:
    """Return cached edges."""
    return _edge_cache.get("edges", [])


def get_edges_context() -> str:
    """Format cached edges as context string for the synthesizer LLM."""
    edges = get_cached_edges()
    if not edges:
        return ""

    lines = ["[CURRENT EDGES & PROMOTIONS ON NADO]"]
    for e in edges:
        emoji = {
            "multiplier": "🔥",
            "listing": "🆕",
            "campaign": "🏆",
            "points": "💰",
            "fees": "💸",
            "partnership": "🤝",
            "alpha": "⚡",
        }.get(e.get("type", ""), "📢")
        lines.append(f"{emoji} {e.get('title', 'Edge')}: {e.get('detail', '')}")
        if e.get("source_url"):
            lines.append(f"  Source: {e['source_url']}")

    return "\n".join(lines)


# Scans and KB indexing are long, blocking, and purely background: they belong on
# the background pool, never on the loop's implicit default executor (five threads
# on the 1-CPU production VM, shared with the engine's gateway IO). Lazy import:
# llm/ has no module-level edge to core/ (tests/lint/test_architecture_layers.py).
def _bg_thread(func, *args, **kwargs):
    from src.nadobro.core.async_utils import run_blocking_bg

    return run_blocking_bg(func, *args, **kwargs)


async def async_scan_edges():
    """Async wrapper for the scheduler."""
    await _bg_thread(scan_edges)


async def async_initial_scan():
    """Run initial scan + KB indexing on bot startup."""
    # Index knowledge base into Pinecone
    try:
        from src.nadobro.llm.vector_store import index_knowledge_base, is_available
        if is_available():
            await _bg_thread(index_knowledge_base)
    except Exception:
        logger.debug("KB indexing skipped", exc_info=True)

    # Run initial edge scan
    await _bg_thread(scan_edges)
