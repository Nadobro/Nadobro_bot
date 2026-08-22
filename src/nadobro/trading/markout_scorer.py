"""Mark-out grading job — closes the market maker's feedback loop.

For every strategy fill whose horizon has elapsed, look up where the reference
price actually was and write one ``fill_markouts`` row per horizon. No trading
side effects: this job only ever reads market data and writes grades.

Why this exists
===============
Mid mode has been quoting since it shipped without ever measuring whether its
fills were toxic. Spread capture is the GROSS edge; adverse selection is what
you keep. Until there is a labeled history of "we filled here, the market was
there a minute later", every instruction to widen or tighten is a guess, and
the strategy cannot tell a bad market from a bad configuration.

Design notes
============
Grading is *best-effort and resumable*, deliberately mirroring
``llm/signal_scorer``: a fill that cannot be graded this pass (no candles yet,
dead feed) is simply left ungraded and picked up next run. The query is a LEFT
JOIN on the outcome row, so there is no cursor to corrupt and no partial state
to reconcile. **Never write a grade from incomplete data** — an ungraded fill
is honest, a wrong one is poison.

The reference is HYPERLIQUID, not Nado. Nado publishes no public tape, and its
poll cadence (3-8s) is coarser than the horizons worth measuring. HL's 1m
closes are an exact grid that survives restarts, which is why they are the
durable primary here; the sub-minute horizons are served live from the pushed
mid ring and are not graded by this job.

That choice has one hazard, and the schema carries the antidote: the fill
happened on Nado while the reference is HL, so a persistent basis would
masquerade as toxicity. ``basis_bp`` is recorded per row so the two can always
be separated after the fact.

Package placement: ``trading`` may import ``quant``, ``venue``, ``models``,
``db`` and ``market_data`` at module level (tests/lint/test_architecture_layers),
which is exactly the set this needs — ``llm`` may not, which is why this does
not live beside ``signal_scorer``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from src.nadobro.db import execute, query_all
from src.nadobro.quant import markout as mk
from src.nadobro.quant.vol_fee_estimator import MAKER_ROUND_TRIP_RATE
from src.nadobro.utils.env import env_int

logger = logging.getLogger(__name__)

# Horizons this job grades, in seconds. Sub-minute horizons (1/5/30s) are
# served live from the in-process HL mid ring and are NOT graded here: a
# periodic job cannot reconstruct a sub-minute reference from 1m candles
# without inventing data, and inventing it is exactly what this module refuses
# to do.
CANDLE_HORIZONS_S = (60.0, 300.0)

# HL 1m candles fetched per product per pass. 300 bars = 5h of reach, which
# bounds how stale a fill can be and still be gradeable.
_CANDLE_LIMIT = 300
_CANDLE_TF = "1m"
_TF_SECONDS = 60

# Rows per pass, so one run cannot monopolise the thread pool.
DEFAULT_BATCH = env_int("NADO_MARKOUT_BATCH", 200)

_VALID_NETWORKS = frozenset({"testnet", "mainnet"})

# Fee assumption when the fill carries none. Maker round trip (5bp) is the
# conservative direction for a maker-only strategy: over-charging the fee can
# only make mark-out look WORSE, so it never manufactures a false all-clear.
_DEFAULT_FEE_BP = float(MAKER_ROUND_TRIP_RATE) * 10_000.0


def lookback_seconds() -> float:
    """Oldest fill this job can still grade, derived from the candle reach.

    Querying beyond it would return rows the candle window can never cover:
    every pass would re-scan them, fail, and skip — a permanent no-op loop that
    burns budget and grades nothing. Bounding the query means an unreachable
    fill is simply never selected.
    """
    reach = _TF_SECONDS * _CANDLE_LIMIT
    return max(0.0, reach - max(CANDLE_HORIZONS_S) - _TF_SECONDS)


def _trades_table(network: str) -> str:
    if network not in _VALID_NETWORKS:
        raise ValueError(f"Invalid network: {network}")
    return f"trades_{network}"


def select_ungraded_fills(network: str, *, limit: int = DEFAULT_BATCH) -> List[Dict[str, Any]]:
    """Strategy fills old enough to grade and not yet graded at every horizon.

    LEFT JOIN + HAVING rather than a cursor: a fill graded at 60s but not yet
    at 300s stays selected until both exist, and a transient failure costs
    nothing but a retry next pass.
    """
    table = _trades_table(network)
    now = datetime.now(timezone.utc)
    newest = now - timedelta(seconds=max(CANDLE_HORIZONS_S))
    oldest = now - timedelta(seconds=lookback_seconds())
    rows = query_all(
        f"""
        SELECT t.id, t.user_id, t.product_name, t.side,
               t.fill_price, t.price, t.fill_size, t.size,
               t.fill_fee, t.builder_fee, t.is_taker, t.strategy_session_id,
               COALESCE(t.filled_at, t.created_at) AS ts_fill
          FROM {table} t
          LEFT JOIN fill_markouts m
                 ON m.trade_id = t.id AND m.network = %s
         WHERE COALESCE(t.filled_at, t.created_at) <= %s
           AND COALESCE(t.filled_at, t.created_at) >= %s
           AND t.source = 'strategy'
           AND COALESCE(t.fill_price, t.price) > 0
         GROUP BY t.id, t.user_id, t.product_name, t.side,
                  t.fill_price, t.price, t.fill_size, t.size,
                  t.fill_fee, t.builder_fee, t.is_taker,
                  t.strategy_session_id, t.filled_at, t.created_at
        HAVING COUNT(m.id) < %s
         ORDER BY COALESCE(t.filled_at, t.created_at) ASC
         LIMIT %s
        """,
        (network, newest, oldest, len(CANDLE_HORIZONS_S), int(limit)),
    )
    return [dict(r) for r in (rows or [])]


def _hl_close_series(coin: str) -> List[Sequence[float]]:
    """HL 1m closes as ``[(epoch_seconds, close), ...]`` oldest-first.

    Blocking HTTP — callers must run this off the event loop.
    """
    from src.nadobro.market_data.hl_client import get_candles_sync

    candles = get_candles_sync(coin, interval=_CANDLE_TF,
                               lookback_ms=_CANDLE_LIMIT * _TF_SECONDS * 1000)
    series: List[Sequence[float]] = []
    for c in candles or []:
        try:
            ts, close = float(c.get("time") or 0), float(c.get("close") or 0)
        except (TypeError, ValueError):
            continue
        if ts > 0 and close > 0:
            series.append((ts, close))
    series.sort(key=lambda r: r[0])
    return series


def _coin_for(product_name: Optional[str]) -> str:
    """Nado product -> HL coin. Strips the -PERP suffix; wrapped equity/RWA
    tickers simply will not resolve on HL and yield no candles, which is the
    correct outcome rather than a wrong grade."""
    base = str(product_name or "").upper().strip()
    for suffix in ("-PERP", "-USD", "-USDT"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def _side_sign(side: Optional[str]) -> int:
    return mk.SELL if str(side or "").upper().startswith("S") else mk.BUY


def _fee_bp(row: Dict[str, Any]) -> float:
    """Actual round-trip fee in bp when the fill records one, else the maker
    round-trip default. The recorded fee is ONE leg, so it is doubled to make a
    round trip — mark-out grades a position that has to be closed."""
    try:
        price = float(row.get("fill_price") or row.get("price") or 0.0)
        size = float(row.get("fill_size") or row.get("size") or 0.0)
        fee = abs(float(row.get("fill_fee") or 0.0)) + abs(float(row.get("builder_fee") or 0.0))
    except (TypeError, ValueError):
        return _DEFAULT_FEE_BP
    notional = price * size
    if notional <= 0 or fee <= 0:
        return _DEFAULT_FEE_BP
    return fee / notional * 10_000.0 * 2.0


def grade_fill(row: Dict[str, Any], series: Sequence[Sequence[float]]) -> List[mk.MarkoutSample]:
    """Pure: grade one fill row against a close series. No I/O."""
    ts_fill = row.get("ts_fill")
    if isinstance(ts_fill, datetime):
        ts = ts_fill.timestamp()
    else:
        try:
            ts = float(ts_fill)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return []
    price = row.get("fill_price") or row.get("price")
    try:
        fill_price = float(price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []
    if fill_price <= 0:
        return []
    fill = mk.FillRef(
        fill_id=str(row.get("id")),
        ts=ts,
        side=_side_sign(row.get("side")),
        fill_price=fill_price,
        size_base=float(row.get("fill_size") or row.get("size") or 0.0),
    )
    fee_bp = _fee_bp(row)
    out: List[mk.MarkoutSample] = []
    for horizon in CANDLE_HORIZONS_S:
        sample = mk.build_sample(
            fill, series,
            horizon_s=horizon,
            fee_bp=fee_bp,
            ref_source=mk.REF_CANDLE_1M,
            # One bar of tolerance: closes land on a 60s grid, so a horizon
            # that is not a multiple of the bar can never hit it exactly.
            max_jitter_s=float(_TF_SECONDS),
        )
        if sample is not None:
            out.append(sample)
    return out


def _insert(row: Dict[str, Any], network: str, sample: mk.MarkoutSample) -> None:
    execute(
        """
        INSERT INTO fill_markouts (
            trade_id, network, user_id, strategy, product_name,
            strategy_session_id, ts_fill, side, fill_price, fill_size,
            fee_bp, is_taker, horizon_nominal_s, horizon_actual_s,
            ref_price, ref_source, markout_bp, net_markout_bp, basis_bp
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (trade_id, network, horizon_nominal_s) DO NOTHING
        """,
        (
            row.get("id"), network, row.get("user_id"), row.get("strategy"),
            row.get("product_name"), row.get("strategy_session_id"), row.get("ts_fill"),
            row.get("side"), row.get("fill_price") or row.get("price"),
            row.get("fill_size") or row.get("size"), _fee_bp(row),
            row.get("is_taker"), sample.horizon_nominal_s, sample.horizon_actual_s,
            sample.ref_price, sample.ref_source, sample.markout_bp,
            sample.net_markout_bp, sample.basis_bp,
        ),
    )


def grade_pending_markouts(network: str, *, limit: int = DEFAULT_BATCH) -> Dict[str, int]:
    """Grade one batch. Returns counts for the log; never raises into the
    scheduler — a grading outage must not disturb trading."""
    stats = {"fills": 0, "samples": 0, "skipped": 0}
    try:
        rows = select_ungraded_fills(network, limit=limit)
    except Exception:  # noqa: BLE001
        logger.warning("markout: could not select fills network=%s", network, exc_info=True)
        return stats
    if not rows:
        return stats

    series_by_coin: Dict[str, List[Sequence[float]]] = {}
    for row in rows:
        coin = _coin_for(row.get("product_name"))
        if not coin:
            stats["skipped"] += 1
            continue
        if coin not in series_by_coin:
            try:
                series_by_coin[coin] = _hl_close_series(coin)
            except Exception:  # noqa: BLE001 - a dead feed just defers grading
                logger.debug("markout: candle fetch failed coin=%s", coin, exc_info=True)
                series_by_coin[coin] = []
        series = series_by_coin[coin]
        if not series:
            stats["skipped"] += 1
            continue
        samples = grade_fill(row, series)
        if not samples:
            stats["skipped"] += 1
            continue
        stats["fills"] += 1
        for sample in samples:
            try:
                _insert(row, network, sample)
                stats["samples"] += 1
            except Exception:  # noqa: BLE001
                logger.debug("markout: insert failed trade=%s", row.get("id"), exc_info=True)
    if stats["samples"]:
        logger.info(
            "markout graded network=%s fills=%s samples=%s skipped=%s",
            network, stats["fills"], stats["samples"], stats["skipped"],
        )
    return stats
