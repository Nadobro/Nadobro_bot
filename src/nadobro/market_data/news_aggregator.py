"""News aggregator — fans out across all configured news connectors in parallel.

Each connector's `fetch()` runs on the background thread pool (`run_blocking_bg`)
for parallel execution. Results are deduplicated and grouped by category. A
module-level TTL cache mirrors the `_chat_history` pattern in knowledge_service.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from src.nadobro.core.async_utils import run_blocking_bg
from src.nadobro.connectors.news import NewsItem
from src.nadobro.connectors.news import (
    ap_rss,
    bbc_rss,
    coindesk_rss,
    coingecko_news,
    cryptopanic,
    ecb_rss,
    fed_rss,
    fmp_news,
    ft_via_googlenews,
    gdelt,
    reuters_rss,
    rwa_rss,
    theblock_rss,
)

logger = logging.getLogger(__name__)


# (connector_module, default_category) — every module exposes `fetch(limit) -> list[NewsItem]`.
_CONNECTORS: list[tuple[Callable[[int], list[NewsItem]], str]] = [
    (cryptopanic.fetch, "crypto"),
    (coindesk_rss.fetch, "crypto"),
    (theblock_rss.fetch, "crypto"),
    (coingecko_news.fetch, "crypto"),
    (fmp_news.fetch, "stocks"),
    (reuters_rss.fetch, "tradfi"),
    (ap_rss.fetch, "tradfi"),
    (rwa_rss.fetch, "rwa"),
    (gdelt.fetch, "geopolitics"),
    (bbc_rss.fetch, "geopolitics"),
    (fed_rss.fetch, "economics"),
    (ecb_rss.fetch, "economics"),
    (ft_via_googlenews.fetch, "ft"),
]

ALL_CATEGORIES = ("crypto", "stocks", "tradfi", "rwa", "geopolitics", "economics", "ft")


@dataclass
class NewsBundle:
    items: list[NewsItem]
    by_category: dict[str, list[NewsItem]] = field(default_factory=dict)
    sources_used: list[str] = field(default_factory=list)
    generated_at: float = 0.0


# Cache key = sorted-categories tuple → (timestamp, NewsBundle)
_BUNDLE_CACHE: dict[tuple, tuple[float, NewsBundle]] = {}
_DEFAULT_TTL_SECONDS = 300


def _cache_key(categories: list[str] | None) -> tuple:
    if not categories:
        return ("__all__",)
    return tuple(sorted(set(c.lower() for c in categories)))


def _dedupe(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    out: list[NewsItem] = []
    for item in items:
        fp = item.fingerprint()
        if fp in seen:
            continue
        seen.add(fp)
        out.append(item)
    return out


async def _safe_fetch(fetcher: Callable[[int], list[NewsItem]], limit: int) -> list[NewsItem]:
    try:
        return await run_blocking_bg(fetcher, limit)
    except Exception as exc:
        logger.debug("news connector %s raised: %s", getattr(fetcher, "__module__", "?"), exc)
        return []


async def fetch_news_bundle(
    *,
    categories: list[str] | None = None,
    per_source_limit: int = 8,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> NewsBundle:
    key = _cache_key(categories)
    now = time.time()
    cached = _BUNDLE_CACHE.get(key)
    if cached and (now - cached[0]) < ttl_seconds:
        logger.debug("news_aggregator: cache hit for %s", key)
        return cached[1]

    wanted = None
    if categories:
        wanted = {c.lower() for c in categories}

    tasks = [
        _safe_fetch(fetcher, per_source_limit)
        for fetcher, default_cat in _CONNECTORS
        if wanted is None or default_cat in wanted
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False) if tasks else []

    flat: list[NewsItem] = []
    for batch in results:
        if isinstance(batch, list):
            flat.extend(batch)
    flat = _dedupe(flat)

    flat.sort(
        key=lambda it: it.published_at if it.published_at is not None else 0.0,
        reverse=True,
    )

    by_category: dict[str, list[NewsItem]] = {}
    for item in flat:
        by_category.setdefault(item.category, []).append(item)

    sources_used = sorted({it.source for it in flat})

    bundle = NewsBundle(
        items=flat,
        by_category=by_category,
        sources_used=sources_used,
        generated_at=now,
    )
    _BUNDLE_CACHE[key] = (now, bundle)
    return bundle


def clear_cache() -> None:
    """Test/admin helper to drop cached bundles."""
    _BUNDLE_CACHE.clear()
