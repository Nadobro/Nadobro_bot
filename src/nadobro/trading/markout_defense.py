"""Turn the mark-out ledger into a spread-widening decision.

This is the step that converts measurement into protection. ``fill_markouts``
records where the reference price went after every strategy fill; if the median
NET mark-out on a product is negative, the quotes are being picked off faster
than the spread pays, and the only honest response is to widen.

Three disciplines, all of them learned the hard way:

* **Widen only.** A mark-out series is evidence of harm. It is never evidence
  that quoting TIGHTER is safe — the fills that would have hurt at a tighter
  quote are, by construction, not in the sample.
* **Never block a tick on the database.** The controller asks for this every
  tick on a 3-8s cadence. The answer is served from an in-memory TTL cache and
  the refresh runs on the DB thread pool, so a slow query delays the next
  refresh and nothing else. Blocking IO inside a coroutine is what starves the
  loop and makes APScheduler skip jobs.
* **No evidence means no change.** Below ``min_samples`` the factor is 1.0.
  Widening on four fills is noise-chasing, and it would punish exactly the
  quiet products that generate few fills.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from src.nadobro.quant import markout as mk
from src.nadobro.utils.env import env_float, env_int

logger = logging.getLogger(__name__)

# How long a computed factor is reused. The ledger is graded every ~2 minutes
# and a widening decision is a slow-moving thing; re-reading per tick would add
# a query per user per product per 3s for a number that barely moves.
_TTL_S = env_float("NADO_MARKOUT_DEFENSE_TTL_S", 300.0)

# Rows considered, and how far back. A week of fills on one product is plenty
# to see a persistent bleed and recent enough to reflect current conditions.
_LOOKBACK_S = env_float("NADO_MARKOUT_DEFENSE_LOOKBACK_S", 7 * 24 * 3600.0)
_ROW_LIMIT = env_int("NADO_MARKOUT_DEFENSE_ROWS", 500)

# The horizon the decision is read from. 60s is 1-6x a Mid quote's lifetime
# (min_quote_lifetime_s = min(30, 2*cadence) = 6-16s), so it measures what
# happened while the quote was actually exposed.
DECISION_HORIZON_S = 60.0

MIN_SAMPLES = env_int("NADO_MARKOUT_DEFENSE_MIN_SAMPLES", 30)
MAX_FACTOR = env_float("NADO_MARKOUT_DEFENSE_MAX_FACTOR", 2.0)

_cache: Dict[Tuple[int, str, str], Tuple[float, float]] = {}
_inflight: set = set()

# The cache is keyed per (user, network, product) and the process is long-lived,
# so without a bound it grows with every user who ever ran Mid. Entries are
# cheap, but "cheap and unbounded" is still a leak.
_CACHE_MAX = env_int("NADO_MARKOUT_DEFENSE_CACHE_MAX", 2000)


def _prune_cache(now: float) -> None:
    if len(_cache) <= _CACHE_MAX:
        return
    # Drop anything well past its TTL first; if that is not enough, drop the
    # oldest. A dropped entry costs one refresh, never correctness.
    stale_cutoff = now - (_TTL_S * 4)
    for key in [k for k, (ts, _) in _cache.items() if ts < stale_cutoff]:
        _cache.pop(key, None)
    if len(_cache) <= _CACHE_MAX:
        return
    for key, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[: len(_cache) - _CACHE_MAX]:
        _cache.pop(key, None)


def reset_state() -> None:
    """Tests only."""
    _cache.clear()
    _inflight.clear()


def _load_samples(user_id: int, network: str, product_name: str) -> list:
    """Blocking DB read — only ever called on the DB thread pool."""
    from src.nadobro.db import query_all

    cutoff = time.time() - _LOOKBACK_S
    rows = query_all(
        """
        SELECT horizon_nominal_s, horizon_actual_s, ref_price,
               markout_bp, net_markout_bp, ref_source, basis_bp
          FROM fill_markouts
         WHERE user_id = %s AND network = %s AND product_name = %s
           AND horizon_nominal_s = %s
           AND ts_fill >= to_timestamp(%s)
         ORDER BY ts_fill DESC
         LIMIT %s
        """,
        (int(user_id), str(network), str(product_name),
         float(DECISION_HORIZON_S), cutoff, int(_ROW_LIMIT)),
    )
    out = []
    for r in rows or []:
        try:
            out.append(mk.MarkoutSample(
                horizon_nominal_s=float(r["horizon_nominal_s"]),
                horizon_actual_s=float(r["horizon_actual_s"] or r["horizon_nominal_s"]),
                ref_price=float(r["ref_price"] or 0.0),
                markout_bp=float(r["markout_bp"] or 0.0),
                net_markout_bp=float(r["net_markout_bp"] or 0.0),
                ref_source=str(r["ref_source"] or ""),
                basis_bp=None if r["basis_bp"] is None else float(r["basis_bp"]),
            ))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _compute(user_id: int, network: str, product_name: str,
             half_spread_bp: float) -> float:
    samples = _load_samples(user_id, network, product_name)
    if not samples:
        return 1.0
    summary = mk.summarize(samples)
    return mk.widen_recommendation(
        summary,
        current_half_spread_bp=half_spread_bp,
        horizon_s=DECISION_HORIZON_S,
        min_samples=MIN_SAMPLES,
        max_factor=MAX_FACTOR,
    )


async def widen_factor(
    user_id: int, network: str, product_name: str, *, half_spread_bp: float
) -> float:
    """Multiplier for the half-spread, in [1, MAX_FACTOR]. Never blocks.

    A cold or expired entry returns the last known value (1.0 when there is
    none) and refreshes in the background, so the FIRST tick after expiry is
    served instantly rather than waiting on a query.
    """
    key = (int(user_id), str(network), str(product_name))
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached[0]) < _TTL_S:
        return cached[1]

    if key in _inflight:
        return cached[1] if cached else 1.0
    _inflight.add(key)
    try:
        from src.nadobro.core.async_utils import run_blocking_db

        factor = await run_blocking_db(
            _compute, int(user_id), str(network), str(product_name),
            float(half_spread_bp),
        )
    except Exception:  # noqa: BLE001 - a grading outage must not move quotes
        logger.debug("markout defense read failed %s", product_name, exc_info=True)
        # Re-stamp so a broken DB does not retry on every single tick.
        _cache[key] = (now, cached[1] if cached else 1.0)
        return cached[1] if cached else 1.0
    finally:
        _inflight.discard(key)

    value = max(1.0, min(float(MAX_FACTOR), float(factor)))
    if cached is None or abs(value - cached[1]) > 1e-9:
        logger.info(
            "markout defense %s user=%s widen=%.2fx", product_name, user_id, value
        )
    _cache[key] = (now, value)
    _prune_cache(now)
    return value


def cached_factor(user_id: int, network: str, product_name: str) -> Optional[float]:
    """Last computed factor without touching the DB. For telemetry."""
    entry = _cache.get((int(user_id), str(network), str(product_name)))
    return entry[1] if entry else None
