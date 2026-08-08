"""News connectors for the Morning Brief / multi-source news intelligence layer.

Each connector exposes a synchronous `fetch(limit: int = 10) -> list[NewsItem]`
that never raises — failures return `[]`. The aggregator runs these on the
background thread pool (`core.async_utils.run_blocking_bg`) for parallel fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    category: str
    summary: str = ""
    published_at: Optional[float] = None  # epoch seconds
    tickers: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Stable dedup key — lowercased title + host."""
        from urllib.parse import urlparse

        host = ""
        try:
            host = (urlparse(self.url).netloc or "").lower()
        except Exception:
            pass
        return (self.title or "").strip().lower() + "|" + host
