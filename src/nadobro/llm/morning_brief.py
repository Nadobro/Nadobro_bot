"""Morning Brief composer — single Grok call over snapshot + news + edges + F&G.

Pipeline:
  gather_snapshot()  ──┐
  fetch_news_bundle() ─┼─→ build_payload() ─→ Grok JSON ─→ render_markdown_v2()
  edge_findings    ────┤
  fear & greed     ────┘
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_BRIEF_CACHE: dict[tuple, tuple[float, "BriefRenderResult"]] = {}
_BRIEF_TTL_SECONDS = 300


_SYSTEM_PROMPT = """You are NadoBro composing a Morning Brief for a Telegram trading audience: calm, sharp, actionable.

OUTPUT STRUCTURE (always all three sections, in this order):
1. snapshot_lines — one bullet per Nado-tradeable instrument from the provided market data. Lead with the symbol, mid price, and 24h change. Add one short qualitative phrase. Reference Fear & Greed once if relevant.
2. news_drivers — 3 to 5 numbered items. Each summarises ONE story drawn from the provided news bundle. Paraphrase headlines (max 12-word direct quote, in quotation marks).
3. insight — one paragraph (2 to 3 sentences) ending with a concrete suggestion the user could act on within Nado DEX (smart volume, BTC/ETH/SOL perp, hedge, or sit-out).

HARD RULES:
- Every $ price and % figure MUST come from the provided market_data JSON. Never invent numbers.
- Cite source tags inline at the end of each news item, e.g. [Reuters], [CoinDesk], [Federal Reserve].
- If edge_findings include live Nado promos or multipliers, weave the strongest one into the insight section.
- Stay under 25 lines total.
- Tone: warm trading buddy, no hype, no fabricated quotes.
"""


@dataclass
class BriefRenderResult:
    text: str
    sources: list[str]
    generated_at: float


def _format_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    sign = "" if value < 0 else "+"
    return f"{sign}{value:.2f}%"


def _format_usd(value: Optional[float]) -> str:
    if value is None or value <= 0:
        return "—"
    if value >= 1_000_000_000:
        return f"${value/1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value/1_000:.2f}K"
    return f"${value:,.2f}"


def _serialize_snapshot(snapshot) -> dict:
    rows = []
    for r in snapshot.rows:
        rows.append({
            "symbol": r.symbol,
            "mid_usd": round(r.mid, 4),
            "change_24h_pct": round(r.change_24h_pct, 2) if r.change_24h_pct is not None else None,
            "funding_rate_pct": (round(r.funding_rate * 100, 4) if r.funding_rate is not None else None),
            "spread_bps": round(r.spread_bps, 1),
            "volume_24h_usd": _format_usd(r.volume_24h_usd),
            "open_interest_usd": _format_usd(r.open_interest),
            "high_24h": r.high_24h,
            "low_24h": r.low_24h,
        })
    network_value = getattr(snapshot.network, "value", snapshot.network)
    return {
        "rows": rows,
        "fear_greed": {
            "value": snapshot.fear_greed_value,
            "label": snapshot.fear_greed_label,
        },
        "network": str(network_value),
    }


def _serialize_news(bundle) -> dict:
    by_cat: dict[str, list[dict]] = {}
    for cat, items in bundle.by_category.items():
        by_cat[cat] = [
            {
                "title": it.title,
                "url": it.url,
                "source": it.source,
                "summary": (it.summary or "")[:280],
                "tickers": it.tickers,
            }
            for it in items[:5]
        ]
    return {"by_category": by_cat, "sources_used": bundle.sources_used}


def _serialize_edges(edges: list[dict]) -> list[dict]:
    out = []
    for e in (edges or [])[:10]:
        if not isinstance(e, dict):
            continue
        out.append({
            "title": e.get("title") or e.get("type") or "edge",
            "detail": (e.get("detail") or e.get("description") or "")[:280],
            "ticker": e.get("ticker"),
            "edge_type": e.get("type") or e.get("edge_type"),
        })
    return out


def _build_messages(payload: dict, *, today_str: str) -> list[dict]:
    # `default=str` is a defensive net: any future enum or dataclass leaking into
    # the payload renders as its string repr instead of crashing the brief.
    user_payload = json.dumps(payload, ensure_ascii=False, default=str)[:14000]
    user_message = (
        f"Today is {today_str}. Compose a Morning Brief from the market_data, news_bundle, and edge_findings below.\n\n"
        f"INPUT:\n{user_payload}\n\n"
        "Return STRICT JSON with this schema:\n"
        '{ "snapshot_lines": [string, ...], "news_drivers": [{"title": string, "tag": string, "category": string}, ...], "insight": string }'
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]


def _validate_numbers(rendered: str, payload: dict) -> str:
    """Strip any $ figures the model invented that aren't in the input payload."""
    allowed: set[str] = set()
    for row in payload.get("market_data", {}).get("rows", []):
        mid = row.get("mid_usd")
        if isinstance(mid, (int, float)):
            allowed.add(f"{mid:.2f}")
            allowed.add(f"{mid:,.2f}")
            allowed.add(f"{mid:.0f}")
    if not allowed:
        return rendered

    def _check(match: re.Match) -> str:
        amount = match.group(1).replace(",", "")
        try:
            value = float(amount)
        except ValueError:
            return match.group(0)
        for ok in allowed:
            ok_value = float(ok.replace(",", ""))
            if abs(value - ok_value) / max(ok_value, 1.0) < 0.05:
                return match.group(0)
        return "$—"

    return re.sub(r"\$([\d,]+(?:\.\d+)?)", _check, rendered)


def _render_body(parsed: dict, payload: dict, *, today_str: str) -> str:
    """Render the brief body using standard markdown (**bold**) for the existing
    `format_ai_response` converter to translate to Telegram MarkdownV2."""
    snapshot_lines = parsed.get("snapshot_lines") or []
    news_drivers = parsed.get("news_drivers") or []
    insight = (parsed.get("insight") or "").strip()

    lines: list[str] = [f"📊 **Market Snapshot** ({today_str})"]
    if isinstance(snapshot_lines, list):
        for ln in snapshot_lines[:10]:
            if not isinstance(ln, str):
                continue
            lines.append(f"- {ln.strip()}")

    if news_drivers:
        lines.append("")
        lines.append("🗞 **Key News Drivers**")
        for idx, item in enumerate(news_drivers[:5], start=1):
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            tag = (item.get("tag") or "").strip()
            tag_part = f" [{tag}]" if tag else ""
            lines.append(f"{idx}. {title}{tag_part}")

    if insight:
        lines.append("")
        lines.append(f"🎯 **Actionable Insight**: {insight}")

    body = "\n".join(lines)
    body = _validate_numbers(body, payload)
    return body


async def render_morning_brief(
    *,
    telegram_id: Optional[int] = None,
    user_name: Optional[str] = None,
    network: str = "mainnet",
    categories: Optional[list[str]] = None,
    ttl_seconds: int = _BRIEF_TTL_SECONDS,
    language: str = "en",
) -> tuple[str, list[str]]:
    """Return (markdown_v2_text, source_tags)."""
    cache_key = (network, language, tuple(sorted(categories)) if categories else None)
    now = time.time()
    cached = _BRIEF_CACHE.get(cache_key)
    if cached and (now - cached[0]) < ttl_seconds:
        return cached[1].text, cached[1].sources

    from src.nadobro.market_data.market_snapshot import gather_snapshot
    from src.nadobro.market_data.news_aggregator import fetch_news_bundle

    edge_findings: list[dict] = []
    try:
        from src.nadobro.llm.edge_scanner import get_recent_findings

        edge_findings = get_recent_findings(limit=10)
    except Exception as exc:
        logger.debug("edge findings unavailable: %s", exc)

    snapshot, news_bundle = await asyncio.gather(
        gather_snapshot(network),
        fetch_news_bundle(categories=categories),
    )

    payload = {
        "market_data": _serialize_snapshot(snapshot),
        "news_bundle": _serialize_news(news_bundle),
        "edge_findings": _serialize_edges(edge_findings),
    }

    # AUDIT-FIX-DT-1: utcnow() is deprecated in Py 3.12+; use aware UTC.
    today_str = _dt.datetime.now(_dt.timezone.utc).strftime("%a, %d %b %Y")
    messages = _build_messages(payload, today_str=today_str)

    parsed: dict = {}
    try:
        from src.nadobro.llm.bro_llm import chat_json

        # Background pool, not asyncio.to_thread: the implicit default executor is
        # only cpu_count+4 (=5) threads on the production VM and is shared with
        # the engine's gateway IO. A minutes-long brief call parked there starved
        # strategy ticks and Telegram taps. (Lazy import: llm/ has no
        # module-level edge to core/ — tests/lint/test_architecture_layers.py.)
        from src.nadobro.core.async_utils import run_blocking_bg

        parsed, _provider = await run_blocking_bg(chat_json, messages, None, None)
    except Exception as exc:
        logger.warning("morning brief LLM call failed: %s", exc)

    if not isinstance(parsed, dict) or not parsed:
        parsed = _fallback_render(snapshot, news_bundle)

    sources = list(news_bundle.sources_used)
    if snapshot.fear_greed_value is not None:
        sources.append("F&G")

    body = _render_body(parsed, payload, today_str=today_str)
    result = BriefRenderResult(text=body, sources=sources, generated_at=now)
    _BRIEF_CACHE[cache_key] = (now, result)
    return result.text, result.sources


def _fallback_render(snapshot, news_bundle) -> dict:
    """When the LLM is unavailable, return a minimally-structured payload from raw data."""
    snapshot_lines = []
    for r in snapshot.rows[:8]:
        change = _format_pct(r.change_24h_pct)
        snapshot_lines.append(f"{r.symbol}: ${r.mid:,.2f} ({change} 24h)")
    if snapshot.fear_greed_value is not None:
        snapshot_lines.append(f"Fear & Greed: {snapshot.fear_greed_value} ({snapshot.fear_greed_label or '—'})")

    news_drivers = []
    seen_titles: set[str] = set()
    for cat in ("crypto", "stocks", "tradfi", "rwa", "geopolitics", "economics", "ft"):
        for item in news_bundle.by_category.get(cat, [])[:2]:
            if item.title in seen_titles:
                continue
            seen_titles.add(item.title)
            news_drivers.append({
                "title": item.title,
                "tag": item.source,
                "category": cat,
            })
            if len(news_drivers) >= 5:
                break
        if len(news_drivers) >= 5:
            break

    return {
        "snapshot_lines": snapshot_lines,
        "news_drivers": news_drivers,
        "insight": "News sources reachable, market data live. Pick a Nado-tradeable instrument that matches your risk tolerance and size accordingly.",
    }


def clear_cache() -> None:
    _BRIEF_CACHE.clear()
