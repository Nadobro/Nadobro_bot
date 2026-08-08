"""Market snapshot for the Morning Brief.

Decision: only show what's tradeable on Nado. Equities and commodities surface
only as news headlines (via news_aggregator), not as live price rows here.
When Nado lists them, we add the rows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from src.nadobro.core.async_utils import run_blocking_bg

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 60
_SNAPSHOT_CACHE: dict[str, tuple[float, "SnapshotPayload"]] = {}


@dataclass
class SnapshotRow:
    symbol: str
    mid: float
    bid: float
    ask: float
    spread_bps: float
    funding_rate: Optional[float]
    change_24h_pct: Optional[float]
    volume_24h_usd: Optional[float]
    open_interest: Optional[float]
    high_24h: Optional[float]
    low_24h: Optional[float]
    tradeable_on_nado: bool = True


@dataclass
class SnapshotPayload:
    rows: list[SnapshotRow] = field(default_factory=list)
    fear_greed_value: Optional[int] = None
    fear_greed_label: Optional[str] = None
    network: str = "mainnet"
    generated_at: float = 0.0


def _build_row_from_stats(symbol: str, stats: dict) -> SnapshotRow:
    return SnapshotRow(
        symbol=symbol,
        mid=float(stats.get("mid") or 0),
        bid=float(stats.get("bid") or 0),
        ask=float(stats.get("ask") or 0),
        spread_bps=float(stats.get("spread_bps") or 0),
        funding_rate=stats.get("funding_rate"),
        change_24h_pct=stats.get("change_24h_pct"),
        volume_24h_usd=stats.get("volume_24h_usd"),
        open_interest=stats.get("open_interest"),
        high_24h=stats.get("high_24h"),
        low_24h=stats.get("low_24h"),
        tradeable_on_nado=True,
    )


def _augment_with_cmc(rows: list[SnapshotRow]) -> None:
    """Fill missing 24h change / mid from CoinMarketCap when available."""
    try:
        from src.nadobro.llm.knowledge_service import _is_cmc_available

        if not _is_cmc_available():
            return
        from src.nadobro.market_data.cmc_client import get_crypto_quotes

        symbols = [r.symbol for r in rows]
        if not symbols:
            return
        data = get_crypto_quotes(symbols) or {}
    except Exception as exc:
        logger.debug("snapshot CMC augment failed: %s", exc)
        return

    for row in rows:
        cmc_row = (data.get(row.symbol) or {}) if isinstance(data, dict) else {}
        quote = ((cmc_row.get("quote") or {}).get("USD") or {})
        if row.change_24h_pct is None and quote.get("percent_change_24h") is not None:
            try:
                row.change_24h_pct = float(quote.get("percent_change_24h"))
            except Exception:
                pass
        if (row.mid is None or row.mid <= 0) and quote.get("price") is not None:
            try:
                row.mid = float(quote.get("price"))
            except Exception:
                pass


def _gather_sync(network: str) -> SnapshotPayload:
    from src.nadobro.config import get_perp_products, get_product_id
    from src.nadobro.llm.knowledge_service import (
        _fetch_fear_greed_index,
        _parse_fng_snapshot,
    )
    from src.nadobro.venue.nado_client import NadoClient

    perp_symbols = []
    try:
        perp_symbols = list(get_perp_products(network=network))
    except Exception as exc:
        logger.debug("perp catalog read failed: %s", exc)

    rows: list[SnapshotRow] = []
    if perp_symbols:
        try:
            # NO_ORDERS_AUDIT-FIX-R6b: route through the digest-keyed cache.
            from src.nadobro.venue.nado_client import get_or_create_readonly_client
            client = get_or_create_readonly_client("0x0000000000000000000000000000000000000000", network)
        except Exception as exc:
            logger.warning("snapshot Nado client init failed: %s", exc)
            client = None

        if client is not None:
            for symbol in perp_symbols:
                product_id = get_product_id(symbol, network=network, client=client)
                if product_id is None:
                    continue
                try:
                    stats = client.get_product_market_stats(int(product_id))
                except Exception as exc:
                    logger.debug("snapshot stats failed for %s: %s", symbol, exc)
                    continue
                if not stats:
                    continue
                row = _build_row_from_stats(symbol, stats)
                if row.mid <= 0:
                    continue
                rows.append(row)

    _augment_with_cmc(rows)

    fng_value = None
    fng_label = None
    try:
        fng_raw = _fetch_fear_greed_index()
        fng_value, fng_label = _parse_fng_snapshot(fng_raw)
    except Exception as exc:
        logger.debug("snapshot F&G fetch failed: %s", exc)

    return SnapshotPayload(
        rows=rows,
        fear_greed_value=fng_value,
        fear_greed_label=fng_label,
        network=network,
        generated_at=time.time(),
    )


def _normalize_network(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "mainnet").lower() or "mainnet"


async def gather_snapshot(network: str = "mainnet", *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> SnapshotPayload:
    network = _normalize_network(network)
    now = time.time()
    cached = _SNAPSHOT_CACHE.get(network)
    if cached and (now - cached[0]) < ttl_seconds:
        return cached[1]

    # _gather_sync walks every perp product (one gateway stats call each) plus
    # CMC and Fear&Greed — minutes of blocking IO in the worst case. Keep it on
    # the background pool so it can never queue ahead of a click-path render.
    payload = await run_blocking_bg(_gather_sync, network)
    _SNAPSHOT_CACHE[network] = (now, payload)
    return payload


def clear_cache() -> None:
    _SNAPSHOT_CACHE.clear()
