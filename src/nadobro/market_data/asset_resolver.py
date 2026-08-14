"""Resolve a ticker from free text without requiring a Nado product id.

Nado is the execution venue when the name is listed. Analysis still proceeds
for equities, unlisted crypto, and commodities.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_STOP = {
    "the", "and", "for", "or", "on", "to", "of", "a", "an", "is", "it", "be",
    "up", "down", "vs", "ta", "oi", "pm", "am", "hr", "ask", "bid", "buy",
    "sell", "long", "short", "call", "puts", "put", "why", "how", "what",
    "read", "next", "move", "after", "hours", "close", "open", "miss", "beat",
    "nado", "nadobro", "ink", "perp", "chart", "price", "market",
}

_COMMODITIES = {"WTI", "XAG", "XAU", "CL", "GC", "SI", "BRENT", "NG", "HG"}
_CRYPTO_NAMES = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "dogecoin": "DOGE",
    "ripple": "XRP",
    "binance": "BNB",
}
_KNOWN_CRYPTO = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "LINK", "DOGE", "AVAX", "SUI", "APT",
    "NEAR", "ADA", "DOT", "ATOM", "OP", "ARB", "PEPE", "WIF", "TAO", "HYPE",
    "INK",
}

_TICKER_RE = re.compile(r"\$([A-Za-z]{1,6})\b")
_PERP_RE = re.compile(r"\b([A-Za-z]{1,6})-PERP\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\b([A-Za-z]{2,6})\b")


@dataclass(frozen=True)
class ResolvedAsset:
    symbol: str
    asset_class: str  # nado_perp | crypto | equity | commodity | unknown
    nado_product_id: Optional[int] = None
    tradeable_on_nado: bool = False


def _nado_lookup(symbol: str, network: str) -> tuple[Optional[int], bool]:
    try:
        from src.nadobro.config import get_product_id, get_perp_products

        pid = get_product_id(symbol, network=network)
        if pid is not None:
            return int(pid), True
        listed = {p.upper() for p in (get_perp_products(network=network) or [])}
        return None, symbol.upper() in listed
    except Exception:
        return None, False


def _classify(symbol: str, *, nado_pid: Optional[int], nado_listed: bool) -> str:
    if nado_listed or nado_pid is not None:
        return "nado_perp"
    up = symbol.upper()
    if up in _COMMODITIES:
        return "commodity"
    if up in _KNOWN_CRYPTO:
        return "crypto"
    return "equity"


def resolve_asset(text: str, *, network: str = "mainnet") -> ResolvedAsset:
    raw = str(text or "").strip()
    if not raw:
        return ResolvedAsset("", "unknown")

    candidates: list[str] = []
    for m in _TICKER_RE.finditer(raw):
        candidates.append(m.group(1).upper())
    for m in _PERP_RE.finditer(raw):
        candidates.append(m.group(1).upper())

    q = raw.lower()
    for name, sym in _CRYPTO_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", q):
            candidates.append(sym)

    try:
        from src.nadobro.config import get_perp_products

        products = sorted(get_perp_products(network=network) or [], key=len, reverse=True)
    except Exception:
        products = []
    for symbol in products:
        s = symbol.lower()
        if re.search(rf"\b{re.escape(s)}\b", q) or re.search(rf"\b{re.escape(s)}-perp\b", q):
            candidates.append(symbol.upper())

    for m in _TOKEN_RE.finditer(raw):
        tok = m.group(1)
        if tok.lower() in _STOP:
            continue
        candidates.append(tok.upper())

    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        if c in seen or c.lower() in _STOP:
            continue
        seen.add(c)
        ordered.append(c)

    if not ordered:
        return ResolvedAsset("", "unknown")

    # Prefer a Nado-listed name when present, else the first extracted ticker.
    nado_hit: Optional[ResolvedAsset] = None
    first: Optional[ResolvedAsset] = None
    for sym in ordered:
        pid, listed = _nado_lookup(sym, network)
        asset = ResolvedAsset(
            symbol=sym,
            asset_class=_classify(sym, nado_pid=pid, nado_listed=listed),
            nado_product_id=pid,
            tradeable_on_nado=bool(listed or pid is not None),
        )
        if first is None:
            first = asset
        if asset.tradeable_on_nado and nado_hit is None:
            nado_hit = asset
    return nado_hit or first or ResolvedAsset("", "unknown")
