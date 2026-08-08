"""Signal grading job — closes the overlay's feedback loop.

For every ``overlay_signals`` row whose horizon has elapsed, fetch what price
actually did and write one ``signal_outcomes`` row per horizon. No LLM, no
trading side effects: this job only ever reads market data and writes grades.

Why this exists
===============
The overlay has been recording its opinions since it shipped and never once
recording whether they were right. Every downstream ambition — weighting
features by evidence, letting a size multiplier off the leash, showing a user
a recommendation with a track record attached — needs a labeled history, and
there was none. This builds it.

Design notes
============
Grading is *best-effort and resumable*. A signal that cannot be graded this pass
(throttled candle fetch, missing history, dead client) is simply left ungraded
and picked up next run — the query is a LEFT JOIN on the outcome row, so there
is no cursor to corrupt and no partial state to reconcile. Never write a grade
from incomplete data; an ungraded signal is honest, a wrong one is poison.

The anchor price is the close of the candle containing the signal, not the mid
recorded at decision time. Anchor and outcome then come from the same series, so
a forward return measures the market rather than the basis difference between
two feeds.

Package placement: ``llm`` may not import ``venue``, ``engine`` or ``quant`` at
module level (``tests/lint/test_architecture_layers.py``). The Nado client is
therefore injected or resolved through ``users``, and ``chronological`` is
imported lazily inside the function that needs it — the same pattern
``night_howl_service`` uses for the backtester.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# horizon -> (seconds, grading timeframe, candles to fetch)
#
# The grading timeframe is deliberately finer than the horizon: excursions are
# the point of this table, and measuring the worst drawdown of a 4h call on 4h
# bars would report one number where sixteen happened.
#
# The fetch limit is what bounds how far back a horizon can reach, and the
# candlestick query weight is ``1 + limit/20`` — covering three days of 1m bars
# would cost weight ~217 per product per pass, which is not worth paying to
# grade a stale 15m call. So the limits stay modest and the backlog bound is
# DERIVED from them (see ``horizon_lookback_seconds``) rather than asserted.
HORIZONS: Dict[str, Tuple[int, str, int]] = {
    "15m": (900, "1m", 300),
    "1h": (3600, "5m", 300),
    "4h": (14400, "15m", 300),
}

_TF_SECONDS: Dict[str, int] = {"1m": 60, "5m": 300, "15m": 900}

# Upper cap on the backlog regardless of reach: after a long outage, grading
# stale signals burns indexer budget to produce data whose market context is
# gone. Binds only the 4h horizon, and only marginally.
BACKLOG_DAYS = 3

# Per-horizon cap on rows per pass, so one run can't monopolise the SDK pool.
DEFAULT_BATCH = 200


def horizon_lookback_seconds(horizon: str) -> float:
    """Oldest signal age this horizon can actually grade, from its fetch reach.

    Querying for signals older than this would return rows the candle window can
    never cover: every pass would re-scan them, fail to anchor, and skip — a
    permanent no-op loop that costs budget and grades nothing. Bounding the
    query instead means an unreachable signal is simply never selected.
    """
    seconds, timeframe, limit = HORIZONS[horizon]
    reach = _TF_SECONDS[timeframe] * limit
    # The whole window must sit inside the fetched span, and the anchor bar
    # itself must precede the signal — hence one extra bar of slack.
    return max(0.0, reach - seconds - _TF_SECONDS[timeframe])

def _epoch_seconds(value: object) -> Optional[float]:
    """Candle timestamps arrive as seconds or milliseconds depending on path.

    Detect by magnitude rather than trusting the feed: anything past ~2001 in
    milliseconds is far beyond a plausible second-count, so the split is
    unambiguous for any date this bot will ever see.
    """
    try:
        raw = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    return raw / 1000.0 if raw > 1e12 else raw


def _as_utc(value: object) -> Optional[datetime]:
    """Coerce a DB timestamp to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _candle_window(
    candles: Sequence[Mapping[str, Any]],
    start_ts: float,
    end_ts: float,
) -> Tuple[Optional[float], List[Mapping[str, Any]]]:
    """Anchor close at ``start_ts`` plus the bars covering ``(start, end]``.

    The anchor is the last bar opening at or before the signal — the price the
    decision was actually made against. Returns ``(None, [])`` when the series
    doesn't span the window, which the caller must treat as "cannot grade yet"
    rather than as a zero return.
    """
    anchor_close: Optional[float] = None
    anchor_index = -1
    for i, candle in enumerate(candles):
        ts = _epoch_seconds(candle.get("time"))
        # Skip an untimestamped bar rather than breaking on it: breaking would
        # silently anchor the grade to a much older price than the signal saw.
        if ts is None:
            continue
        if ts > start_ts:
            break
        anchor_index = i
    if anchor_index < 0:
        return None, []
    try:
        anchor_close = float(candles[anchor_index]["close"])
    except (KeyError, TypeError, ValueError):
        return None, []
    if anchor_close <= 0:
        return None, []

    window: List[Mapping[str, Any]] = []
    for candle in candles[anchor_index + 1:]:
        ts = _epoch_seconds(candle.get("time"))
        if ts is None:
            continue
        if ts > end_ts:
            break
        window.append(candle)
    return anchor_close, window


def _grade(
    signal: Mapping[str, Any],
    horizon: str,
    anchor: float,
    window: Sequence[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build one ``signal_outcomes`` payload. ``None`` if the window is unusable."""
    if not window or anchor <= 0:
        return None
    try:
        final_close = float(window[-1]["close"])
        highs = [float(c["high"]) for c in window]
        lows = [float(c["low"]) for c in window]
    except (KeyError, TypeError, ValueError):
        return None
    if final_close <= 0 or not highs or not lows:
        return None

    fwd_return = (final_close - anchor) / anchor
    # Direction-neutral by construction; clamped so a window that never traded
    # above the anchor reports 0 rather than a negative "up" excursion.
    excursion_up = max(0.0, (max(highs) - anchor) / anchor)
    excursion_down = min(0.0, (min(lows) - anchor) / anchor)

    bias_raw = signal.get("bias")
    try:
        bias = float(bias_raw) if bias_raw is not None else 0.0
    except (TypeError, ValueError):
        bias = 0.0
    # A neutral call cannot be right or wrong — leave the hit NULL rather than
    # scoring "no opinion" as a loss.
    directional_hit: Optional[bool] = None
    if bias != 0.0 and fwd_return != 0.0:
        directional_hit = (bias > 0) == (fwd_return > 0)

    return {
        "signal_id": signal.get("id"),
        "user_id": signal.get("user_id"),
        "network": signal.get("network"),
        "strategy": signal.get("strategy"),
        "product_id": signal.get("product_id"),
        "product_name": signal.get("product_name"),
        "ts_signal": signal.get("ts"),
        "mid_at_signal": anchor,
        "bias": bias,
        "regime": signal.get("regime"),
        "confidence": signal.get("confidence"),
        "horizon": horizon,
        "fwd_return": fwd_return,
        "excursion_up": excursion_up,
        "excursion_down": excursion_down,
        "directional_hit": directional_hit,
        "bars_used": len(window),
    }


class _CandleCache:
    """Per-run candle store keyed by (network, product_id, timeframe).

    Candles are public market data, so one fetch serves every user holding a
    signal on that product. The venue client also keeps a short shared Redis
    cache, but that TTL is far shorter than a grading pass — without this, a
    hundred users' signals on the same product would be a hundred fetches.
    """

    def __init__(self) -> None:
        self._candles: Dict[Tuple[str, int, str], List[Mapping[str, Any]]] = {}
        self._clients: Dict[Tuple[Any, str], Any] = {}
        self._network_client: Dict[str, Any] = {}

    def client_for(self, user_id: Any, network: str) -> Optional[Any]:
        from src.nadobro.users.user_service import get_user_readonly_client

        key = (user_id, network)
        if key in self._clients:
            return self._clients[key]
        client = None
        try:
            client = get_user_readonly_client(int(user_id), network)
        except Exception as exc:  # noqa: BLE001  # policy: degrade-ok(one user's client failing must not stop the sweep)
            logger.debug("signal_scorer client failed user=%s: %s", user_id, exc)
        self._clients[key] = client
        if client is not None:
            # Any initialized client can read public candles for this network;
            # keep one around so a user with a broken client still gets graded.
            self._network_client.setdefault(network, client)
        return client

    def candles(
        self, client: Any, network: str, product_id: int, timeframe: str, limit: int
    ) -> List[Mapping[str, Any]]:
        from src.nadobro.engine.routines.technical_analysis import chronological

        key = (network, int(product_id), timeframe)
        if key in self._candles:
            return self._candles[key]
        use = client or self._network_client.get(network)
        rows: List[Mapping[str, Any]] = []
        if use is not None:
            try:
                rows = list(
                    use.get_candlesticks(int(product_id), timeframe=timeframe, limit=limit)
                    or []
                )
            except Exception as exc:  # noqa: BLE001  # policy: degrade-ok(throttle/outage -> grade next pass)
                logger.debug(
                    "signal_scorer candle fetch failed pid=%s tf=%s: %s",
                    product_id, timeframe, exc,
                )
                rows = []
        # The house guardrail: the indexer serves newest-first and every
        # consumer must normalize for itself. A reversed series here would make
        # the anchor the newest bar and grade every signal against the past.
        ordered = list(chronological(rows))
        # Do NOT cache an empty result — that's a throttle, not an answer, and
        # caching it would poison every remaining signal on this product.
        if ordered:
            self._candles[key] = ordered
        return ordered


def grade_pending(
    *,
    now_utc: Optional[datetime] = None,
    batch: int = DEFAULT_BATCH,
    backlog_days: int = BACKLOG_DAYS,
) -> Dict[str, int]:
    """Grade every signal whose horizon has completed.

    Blocking (DB + SDK); call through ``run_blocking`` from the scheduler.
    Returns ``{"graded": n, "skipped": n, "scanned": n}``. Never raises.
    """
    from src.nadobro.models.database import (
        get_ungraded_signals,
        insert_signal_outcome,
    )

    now = now_utc or datetime.now(timezone.utc)
    cache = _CandleCache()
    graded = skipped = scanned = 0

    for horizon, (seconds, timeframe, limit) in HORIZONS.items():
        try:
            # Never ask for signals older than this horizon's candle reach —
            # they can't be anchored, so selecting them would just burn a pass.
            lookback = min(
                horizon_lookback_seconds(horizon), float(backlog_days) * 86400.0
            )
            pending = get_ungraded_signals(
                horizon,
                # Only grade what has fully elapsed; a partially-formed window
                # would systematically understate excursions.
                ready_before=now - timedelta(seconds=seconds),
                not_older_than=now - timedelta(seconds=lookback),
                limit=batch,
            )
        except Exception as exc:  # noqa: BLE001  # policy: degrade-ok(one horizon failing must not block the others)
            logger.warning("signal_scorer query failed horizon=%s: %s", horizon, exc)
            continue

        for signal in pending:
            scanned += 1
            try:
                ts_signal = _as_utc(signal.get("ts"))
                product_id = signal.get("product_id")
                network = str(signal.get("network") or "")
                if ts_signal is None or product_id is None or not network:
                    skipped += 1
                    continue

                client = cache.client_for(signal.get("user_id"), network)
                candles = cache.candles(
                    client, network, int(product_id), timeframe, limit
                )
                if not candles:
                    skipped += 1
                    continue

                start_ts = ts_signal.timestamp()
                anchor, window = _candle_window(
                    candles, start_ts, start_ts + float(seconds)
                )
                if anchor is None or not window:
                    skipped += 1
                    continue

                payload = _grade(signal, horizon, anchor, window)
                if payload is None:
                    skipped += 1
                    continue
                if insert_signal_outcome(payload) is not None:
                    graded += 1
                else:
                    # Already graded by a concurrent pass — the unique
                    # constraint did its job.
                    skipped += 1
            except Exception as exc:  # noqa: BLE001  # policy: degrade-ok(per-signal failure leaves the row ungraded for a later pass)
                skipped += 1
                logger.debug(
                    "signal_scorer failed signal=%s horizon=%s: %s",
                    signal.get("id"), horizon, exc,
                )

    if graded or skipped:
        logger.info(
            "signal_scorer graded=%s skipped=%s scanned=%s", graded, skipped, scanned
        )
    return {"graded": graded, "skipped": skipped, "scanned": scanned}
