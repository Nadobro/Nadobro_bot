from __future__ import annotations

import asyncio
import logging
import re
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.nadobro.utils.env import env_float
# Blocking work goes to the purpose-built pools, never to the event loop's
# implicit default executor (only ``cpu_count + 4`` = 5 workers on the 1-CPU
# production VM, shared with the engine's gateway IO — that contention showed up
# as skipped APScheduler ticks and multi-second Telegram taps).
from src.nadobro.core.async_utils import run_blocking_db, run_blocking_sdk
from src.nadobro.db import execute, query_all, query_one
from src.nadobro.config import NADO_MAINNET_REST, NADO_TESTNET_REST
from src.nadobro.core.feature_flags import (
    fill_nudge_enabled,
    portfolio_heavy_sync_seconds,
    portfolio_poll_cache_seconds,
    portfolio_sync_interval_seconds,
    portfolio_sync_users_per_tick,
    portfolio_ws_enabled,
)
from src.nadobro.quant.portfolio_calculator import (
    aggregate_trading_stats,
    compute_total_equity,
    positions_from_account_summary,
)
from src.nadobro.users.user_service import get_user, get_user_nado_client
from src.nadobro.utils.x18 import from_x18

logger = logging.getLogger(__name__)

_snapshot_cache: dict[tuple[int, str], dict[str, Any]] = {}
_inflight: dict[tuple[int, str], asyncio.Lock] = {}

# Backstop for the SDK-session timeouts in nado_client: even if a single SDK
# call slips past its socket timeout, no one user may pin the portfolio_sync
# tick (and its apscheduler max_instances=1 slot) indefinitely. A breach here
# means a worker thread is wedged on a dead connection — we log + skip the user
# rather than letting the whole tick hang for 40 minutes.
_SYNC_USER_TIMEOUT_SECONDS = env_float("NADO_SYNC_USER_TIMEOUT_SECONDS", 30.0)
# Don't start a new user with less than this much budget left in the tick:
# a sub-second slice would time out even a cache hit and get mislabelled as a
# "wedged" sync. Such users are simply deferred to the next tick (the cursor is
# not advanced past them). Clamped so a deliberately tiny per-user timeout still
# allows a user to be attempted.
_MIN_USER_SYNC_BUDGET_SECONDS = min(
    env_float("NADO_SYNC_MIN_USER_BUDGET_SECONDS", 2.0),
    _SYNC_USER_TIMEOUT_SECONDS,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cache_key(user_id: int, network: str) -> tuple[int, str]:
    return int(user_id), _normalize_network(network)


def clear_cache(user_id: int | None = None, network: str | None = None) -> None:
    if user_id is None:
        _snapshot_cache.clear()
        return
    normalized_network = _normalize_network(network) if network is not None else None
    for key in list(_snapshot_cache.keys()):
        if key[0] == int(user_id) and (normalized_network is None or key[1] == normalized_network):
            _snapshot_cache.pop(key, None)


def get_cached_snapshot(user_id: int, network: str | None = None) -> dict[str, Any] | None:
    if network is None:
        user = get_user(user_id)
        network = user.network_mode.value if user else "mainnet"
    snapshot = _snapshot_cache.get(_cache_key(user_id, network))
    return deepcopy(snapshot) if snapshot else None


def set_cached_snapshot(user_id: int, network: str, snapshot: dict[str, Any]) -> None:
    cached = dict(snapshot)
    cached.setdefault("user_id", int(user_id))
    cached.setdefault("network", network)
    _snapshot_cache[_cache_key(user_id, network)] = deepcopy(cached)


def _user_has_isolated_artifacts(snapshot: dict[str, Any] | None) -> bool:
    """True if the prior snapshot shows any isolated-margin position or order.
    Used by the poller to decide whether to pay for the isolated-subaccount
    fan-out on this tick."""
    if not snapshot:
        return False
    for position in snapshot.get("positions") or []:
        if isinstance(position, dict) and (position.get("isolated") or position.get("subaccount")):
            return True
    for order in snapshot.get("open_orders") or []:
        if isinstance(order, dict) and (order.get("isolated") or order.get("subaccount")):
            return True
    return False


def _gateway_circuit_open(network: str) -> bool:
    try:
        from src.nadobro.core.http_session import is_circuit_open

        gateway = NADO_MAINNET_REST if _normalize_network(network) == "mainnet" else NADO_TESTNET_REST
        return bool(is_circuit_open(gateway))
    except Exception:
        return False


def mark_user_active(user_id: int) -> None:
    try:
        execute("UPDATE users SET last_active = now() WHERE telegram_id = %s", (int(user_id),))
    except Exception:
        logger.debug("mark_user_active failed user=%s", user_id, exc_info=True)


_ACTIVE_USERS_PAGE_SIZE = portfolio_sync_users_per_tick()
# Module-level cursor for active_users pagination. Lives in the process so the
# scheduler walks all active users across ticks without re-syncing the same
# top-N every iteration. Reset to 0 when we reach the end (handled below).
_active_users_cursor: int = 0


def active_users(limit: int | None = None, after_user_id: int | None = None) -> list[dict[str, Any]]:
    """Return active users on this network, paginated.

    SCALE: previously hard-capped at LIMIT 200, which silently dropped every
    other user once we crossed that count. Now we paginate by ``telegram_id``
    (stable across calls) and the scheduler walks the full set across ticks.
    """
    page = int(limit if limit is not None else _ACTIVE_USERS_PAGE_SIZE)
    cursor = int(after_user_id or 0)
    return query_all(
        """
        SELECT u.telegram_id, u.network_mode AS network, u.last_active
        FROM users u
        WHERE u.main_address IS NOT NULL
          AND u.telegram_id > %s
          AND (
            u.last_active >= now() - interval '60 minutes'
            OR EXISTS (
              SELECT 1 FROM positions p
              WHERE p.user_id = u.telegram_id
                AND p.network = u.network_mode
                AND p.closed_at IS NULL
            )
            OR EXISTS (
              SELECT 1 FROM open_orders o
              WHERE o.user_id = u.telegram_id
                AND o.network = u.network_mode
                AND o.status IN ('open', 'pending', 'armed')
            )
            OR EXISTS (
              SELECT 1 FROM strategy_sessions s
              WHERE s.user_id = u.telegram_id
                AND s.network = u.network_mode
                AND s.status = 'running'
            )
          )
        ORDER BY u.telegram_id ASC
        LIMIT %s
        """,
        (cursor, page),
    )


async def sync_active_users(reason: str = "poll") -> None:
    """Walk active users in stable cursor order, one page per tick.

    Each scheduler invocation advances ``_active_users_cursor`` so we never
    re-sync the same top-N users every tick. Page size and poll interval are
    tunable via env (defaults: 8 users / 30s) to stay under Cloudflare limits.

    A Redis-backed lock guards the tick: APScheduler ``max_instances=1`` only
    protects *within* a process, but a slow tick (we saw 99s callbacks on
    2026-05-27) still made subsequent ticks pile up and skip. With the
    distributed lock + tick budget, only one tick runs at a time across the
    fleet, and ticks that would pile up are silently dropped (apscheduler
    ``coalesce=True`` already collapses missed runs).
    """
    global _active_users_cursor

    if reason == "poll":
        try:
            from src.nadobro.core.http_session import is_circuit_open

            if is_circuit_open(NADO_MAINNET_REST) and is_circuit_open(NADO_TESTNET_REST):
                logger.debug("portfolio sync skipped: Cloudflare circuit open on both gateways")
                return
        except Exception:  # policy: degrade-ok(circuit probe is best-effort; sync proceeds)
            pass

    tick_budget = max(5.0, portfolio_sync_interval_seconds() * 0.85)

    # No cross-process lock: a single machine runs this job, and APScheduler's
    # ``max_instances=1`` already prevents overlapping ticks in-process. (The
    # former Upstash RedisLock was removed with the rest of the Upstash layer;
    # its synchronous REST round-trips were a source of event-loop starvation.)
    try:
        rows = await run_blocking_db(active_users, _ACTIVE_USERS_PAGE_SIZE, _active_users_cursor)
    except Exception as exc:
        logger.warning("portfolio active user query failed: %s", exc)
        return
    if not rows:
        _active_users_cursor = 0
        return

    poll_cache_ms = portfolio_poll_cache_seconds() * 1000 if reason == "poll" else None
    deadline = time.monotonic() + tick_budget
    synced = 0

    for row in rows:
        remaining = deadline - time.monotonic()
        if remaining < _MIN_USER_SYNC_BUDGET_SECONDS:
            logger.debug(
                "portfolio sync tick budget exhausted after %d users (budget=%.0fs)",
                synced,
                tick_budget,
            )
            break
        user_id = int(row.get("telegram_id"))
        network = str(row.get("network") or "mainnet")
        # Cap the per-user timeout to the budget left in this tick. With a
        # bare 30s per-user timeout (>= the 30s sync interval and > the
        # ~25.5s tick budget), a single user wedged on a dead SDK connection
        # ran the whole tick past its interval and APScheduler skipped the
        # next sync_active_users run ("maximum number of running instances
        # reached"). Bounding by the remaining budget means the tick can
        # never overrun, so ticks stop piling up.
        user_timeout = min(_SYNC_USER_TIMEOUT_SECONDS, remaining)
        try:
            await asyncio.wait_for(
                sync_user(
                    user_id,
                    network=network,
                    reason=reason,
                    max_age_ms=poll_cache_ms,
                ),
                timeout=user_timeout,
            )
        except asyncio.TimeoutError:
            # Only the FULL per-user timeout signals a wedged connection.
            # A budget-truncated timeout (user_timeout < the configured
            # per-user timeout) just means the tick ran out of room — the
            # user is retried next tick, so don't cry "dead connection".
            if user_timeout >= _SYNC_USER_TIMEOUT_SECONDS:
                logger.warning(
                    "portfolio sync user=%s network=%s exceeded %.0fs budget — "
                    "likely a wedged SDK call on a dead connection; skipping user "
                    "so the tick does not stall",
                    user_id, network, _SYNC_USER_TIMEOUT_SECONDS,
                )
            else:
                logger.debug(
                    "portfolio sync user=%s network=%s truncated at %.1fs (tick "
                    "budget); will retry next tick",
                    user_id, network, user_timeout,
                )
        _active_users_cursor = max(_active_users_cursor, user_id)
        synced += 1


async def sync_user(
    user_id: int,
    *,
    network: str | None = None,
    reason: str = "manual",
    force: bool = False,
    max_age_ms: int | None = None,
) -> dict[str, Any]:
    user = await run_blocking_db(get_user, int(user_id))
    network = _normalize_network(network or (user.network_mode.value if user else "mainnet"))
    key = _cache_key(user_id, network)
    if key not in _inflight and len(_inflight) > 1000:
        _inflight.clear()
    lock = _inflight.setdefault(key, asyncio.Lock())
    async with lock:
        if max_age_ms is None:
            if reason == "poll":
                max_age_ms = portfolio_poll_cache_seconds() * 1000
            elif reason in ("cold_render", "refresh"):
                max_age_ms = 5000
            else:
                max_age_ms = 2000

        cache_ttl = max(0.0, float(max_age_ms) / 1000.0) if max_age_ms is not None else 0.0
        if not force and cache_ttl > 0:
            cached = _snapshot_cache.get(key)
            if cached and time.time() - float(cached.get("monotonic_ts", 0)) < cache_ttl:
                return deepcopy(cached)

        if reason == "poll" and not force:
            try:
                from src.nadobro.venue.gateway_budget import is_gateway_blocked
                from src.nadobro.venue.ws_health import is_healthy, reconcile_due

                gateway = NADO_MAINNET_REST if network == "mainnet" else NADO_TESTNET_REST
                if is_gateway_blocked(gateway):
                    cached = _snapshot_cache.get(key)
                    if cached:
                        stale = deepcopy(cached)
                        stale.update({"stale": True, "reason": reason})
                        return stale
                    logger.debug(
                        "portfolio sync skipped user=%s network=%s: gateway circuit open",
                        user_id,
                        network,
                    )
                    return {
                        "user_id": int(user_id),
                        "network": network,
                        "stale": True,
                        "reason": reason,
                        "monotonic_ts": time.time(),
                    }
                if portfolio_ws_enabled() and is_healthy(int(user_id), network):
                    prior = _snapshot_cache.get(key) or {}
                    last_reconcile = float(prior.get("last_reconcile_monotonic", 0))
                    if prior and not reconcile_due(int(user_id), network, last_reconcile):
                        stale = deepcopy(prior)
                        stale.update({"stale": False, "reason": "ws_cached", "ws_healthy": True})
                        return stale
            except Exception:  # policy: degrade-ok(ws-cache fast path; falls through to full sync)
                pass

        # Gateway circuit short-circuit for ALL sync reasons (not just poll):
        # when the Cloudflare circuit is open, hitting the gateway just adds
        # load and stalls behind the same dead host. Return the cached snapshot
        # marked stale (or a synthetic stale row if we have nothing cached)
        # rather than constructing an SDK client and issuing writes. ``force``
        # does not bypass this — a forced refresh against an open circuit is
        # exactly the traffic we must shed.
        if _gateway_circuit_open(network):
            cached = _snapshot_cache.get(key)
            if cached:
                stale = deepcopy(cached)
                stale.update({"stale": True, "reason": reason})
                return stale
            logger.debug(
                "portfolio sync skipped user=%s network=%s reason=%s: gateway circuit open",
                user_id, network, reason,
            )
            return {
                "user_id": int(user_id),
                "network": network,
                "stale": True,
                "reason": reason,
                "monotonic_ts": time.time(),
            }

        started = time.perf_counter()
        try:
            from src.nadobro.core.async_utils import run_blocking_sdk

            client = await run_blocking_sdk(get_user_nado_client, int(user_id), network)
            if not client:
                raise RuntimeError("Nado client unavailable")
            client.acting_user_id = int(user_id)
            # Fill-nudge is on by default, while the broader portfolio-WS
            # rollout remains independently gated. When only fill-nudge is
            # enabled, subscribe to the fill stream without activating WS
            # portfolio invalidation/cache semantics.
            portfolio_streams_on = portfolio_ws_enabled()
            fill_stream_on = False
            if fill_nudge_enabled():
                try:
                    from src.nadobro.venue.nado_ws import has_fill_listeners

                    fill_stream_on = has_fill_listeners()
                except Exception:
                    fill_stream_on = False
            if (portfolio_streams_on or fill_stream_on) and not str(reason).startswith("ws"):
                try:
                    from src.nadobro.venue.nado_ws import PortfolioWsSubscription, portfolio_ws

                    subaccount = getattr(client, "subaccount_hex", None)
                    if subaccount:
                        portfolio_ws.subscribe(
                            PortfolioWsSubscription(
                                int(user_id), network, str(subaccount),
                                sync_portfolio=portfolio_streams_on,
                            )
                        )
                except Exception:
                    logger.debug("portfolio ws subscribe failed user=%s network=%s", user_id, network, exc_info=True)

            prior = _snapshot_cache.get(key) or {}
            heavy_interval = float(portfolio_heavy_sync_seconds())
            last_heavy = float(prior.get("last_heavy_monotonic", 0))
            need_heavy = force or str(reason) not in ("poll",) or (
                time.time() - last_heavy >= heavy_interval
            )

            # The DB write below treats missing live orders as closed, so every
            # authoritative sync must include isolated subaccounts too.
            include_isolated = True

            summary, orders, trigger_orders, balance = await asyncio.gather(
                client.calculate_account_summary(ts=int(time.time())),
                run_blocking_sdk(client.get_all_open_orders, True, include_isolated=include_isolated),
                client.get_trigger_orders(limit=200),
                run_blocking_sdk(client.get_balance),
            )

            if need_heavy:
                matches, funding = await asyncio.gather(
                    client.get_matches(limit=200),
                    client.get_interest_and_funding_payments(limit=200),
                )
                last_heavy_monotonic = time.time()
            else:
                matches = list(prior.get("matches") or [])
                funding = list(prior.get("funding_payments") or [])
                last_heavy_monotonic = last_heavy
            plain_orders = _normalize_order_rows(orders)
            trigger_rows = [_mark_trigger_order(o) for o in _normalize_order_rows(trigger_orders)]
            all_orders = _dedupe_orders_by_digest(plain_orders + trigger_rows)
            positions = [p.to_dict() for p in positions_from_account_summary(summary or {})]
            # The SDK's account summary labels rows "Product_{id}"; resolve
            # real catalog names (BTC-PERP, ...) once here so every render
            # and DB write downstream gets human symbols. Catalog lookups are
            # cached/static-fallback but may touch the network on a cold
            # cache, so keep them off the event loop.
            await run_blocking_sdk(_resolve_product_names, positions, all_orders, matches or [], network)
            stats = aggregate_trading_stats(matches or [], funding or [])
            spot_balances = ((balance or {}).get("balances") or {}) if isinstance(balance, dict) else {}
            equity = compute_total_equity(summary or {}, spot_balances)
            snapshot = {
                "user_id": int(user_id),
                "network": network,
                "summary": summary or {},
                "positions": positions,
                "open_orders": all_orders,
                "matches": matches or [],
                "funding_payments": funding or [],
                "stats": stats,
                "equity": equity,
                "spot_balances": spot_balances,
                "last_sync": _now(),
                "monotonic_ts": time.time(),
                "last_heavy_monotonic": last_heavy_monotonic,
                "last_reconcile_monotonic": time.monotonic() if str(reason).startswith("ws") or force else float(
                    (_snapshot_cache.get(key) or {}).get("last_reconcile_monotonic", time.monotonic())
                ),
                "stale": False,
                "reason": reason,
            }
            await run_blocking_db(_write_snapshot, snapshot, int((time.perf_counter() - started) * 1000))
            _snapshot_cache[key] = deepcopy(snapshot)
            return deepcopy(snapshot)
        except Exception as exc:
            logger.warning("portfolio sync failed user=%s network=%s reason=%s: %s", user_id, network, reason, exc)
            stale = deepcopy(_snapshot_cache.get(key) or {"user_id": int(user_id), "network": network})
            stale.update({"stale": True, "error": str(exc), "last_error_at": _now(), "monotonic_ts": time.time()})
            _snapshot_cache[key] = deepcopy(stale)
            await run_blocking_db(_write_sync_log_error, int(user_id), network, int((time.perf_counter() - started) * 1000), str(exc))
            return stale


def _write_snapshot(snapshot: dict[str, Any], duration_ms: int) -> None:
    user_id = int(snapshot["user_id"])
    network = _normalize_network(snapshot["network"])
    positions = list(snapshot.get("positions") or [])
    orders = list(snapshot.get("open_orders") or [])
    matches = list(snapshot.get("matches") or [])
    funding = list(snapshot.get("funding_payments") or [])

    _write_positions(user_id, network, positions)
    _write_open_orders(user_id, network, orders)
    fills_inserted = _write_matches(user_id, network, matches)
    funding_inserted = _write_funding(user_id, network, funding)

    # Realized PnL is DERIVED position-aware from the FULL trades history (this
    # venue reports none per-fill, so the snapshot's per-fill sum was always 0).
    # Recompute it here — off the event loop, AFTER _write_matches has persisted
    # the latest fills — and overwrite the always-zero pnl fields on the in-memory
    # ``stats`` so the portfolio deck's Realized line reflects real round trips.
    # Volume/fees windows stay as computed from the venue x18 columns.
    try:
        from src.nadobro.models.database import (
            get_account_realized_pnl_windows,
            get_analytics_fills,
        )
        from src.nadobro.quant.user_analytics import aggregate_user_analytics

        realized = get_account_realized_pnl_windows(user_id, network)
        stats = snapshot.get("stats")
        if isinstance(stats, dict) and realized:
            stats["pnl_windows"] = realized["pnl_windows"]
            stats["total_pnl"] = realized["total_pnl"]
            stats["wins"] = realized["wins"]
            stats["losses"] = realized["losses"]
            stats["win_rate"] = realized["win_rate"]

        # Volume / fees / funding must come from the SAME complete ledger as
        # realized PnL. They used to be aggregated from the transient
        # get_matches(limit=200) list, which produced "Volume $0.00" next to
        # "Realized +$16.73" and capped the All-time window at 200 fills.
        if isinstance(stats, dict):
            ledger = get_analytics_fills(user_id, network)
            if ledger:
                analytics = aggregate_user_analytics(ledger, funding)
                stats["analytics"] = analytics
                stats["volume_windows"] = {
                    w: v["total_usd"] for w, v in analytics["nado_volume"].items()
                }
                stats["fees_windows"] = analytics["fees"]
                stats["funding_windows"] = analytics["funding"]
                stats["total_volume"] = analytics["nado_volume"]["all"]["total_usd"]
            else:
                # No ledger rows (fresh account, or the read failed): KEEP the
                # in-memory match aggregate rather than overwriting real volume
                # with $0.00 — zeroing it is the bug this replaced.
                logger.debug(
                    "analytics ledger empty user=%s network=%s — keeping match-derived stats",
                    user_id, network,
                )
    except Exception as exc:  # display-only; never fail the snapshot write
        logger.warning("analytics recompute failed user=%s network=%s: %s", user_id, network, exc)
    execute(
        """
        INSERT INTO sync_log (
          user_id, network, positions_seen, positions_closed, orders_seen,
          orders_cleared, fills_inserted, funding_inserted, duration_ms
        )
        VALUES (%s, %s, %s, 0, %s, 0, %s, %s, %s)
        """,
        (user_id, network, len(positions), len(orders), fills_inserted, funding_inserted, duration_ms),
    )


def _write_positions(user_id: int, network: str, positions: list[dict[str, Any]]) -> None:
    live_keys = [(int(p.get("product_id") or 0), bool(p.get("isolated"))) for p in positions]
    if live_keys:
        clauses = " OR ".join(["(product_id = %s AND isolated = %s)" for _ in live_keys])
        params: list[Any] = [user_id, network]
        for product_id, isolated in live_keys:
            params.extend([product_id, isolated])
        execute(
            f"""
            UPDATE positions
            SET closed_at = COALESCE(closed_at, now()), synced_at = now(), status = 'closed'
            WHERE user_id = %s
              AND network = %s
              AND closed_at IS NULL
              AND NOT ({clauses})
            """,
            tuple(params),
        )
    else:
        execute(
            """
            UPDATE positions
            SET closed_at = COALESCE(closed_at, now()), synced_at = now(), status = 'closed'
            WHERE user_id = %s AND network = %s AND closed_at IS NULL
            """,
            (user_id, network),
        )

    for pos in positions:
        product_id = int(pos.get("product_id") or 0)
        isolated = bool(pos.get("isolated"))
        pair = str(pos.get("symbol") or pos.get("product_name") or f"ID:{product_id}")
        side = "long" if bool(pos.get("is_long", True)) else "short"
        amount = abs(Decimal(str(pos.get("amount") or 0)))
        leverage = _resolve_leverage(pos)
        execute(
            """
            UPDATE positions
            SET pair = %s, side = %s, size = %s, notional_value = %s,
                avg_entry_price = %s, est_liq_price = %s, est_pnl = %s,
                margin_used = %s, leverage = %s, synced_at = now(), status = 'open'
            WHERE user_id = %s AND network = %s AND product_id = %s
              AND isolated = %s AND closed_at IS NULL
            """,
            (
                pair,
                side,
                str(amount),
                _decimal_or_none(pos.get("notional_value")),
                _decimal_or_none(pos.get("avg_entry_price")),
                _decimal_or_none(pos.get("est_liq_price")),
                _decimal_or_none(pos.get("est_pnl")),
                _decimal_or_none(pos.get("margin_used")),
                leverage,
                user_id,
                network,
                product_id,
                isolated,
            ),
        )
        execute(
            """
            INSERT INTO positions (
              user_id, network, product_id, isolated, pair, side, size,
              notional_value, avg_entry_price, est_liq_price, est_pnl,
              margin_used, leverage, status, opened_at, synced_at
            )
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', now(), now()
            WHERE NOT EXISTS (
              SELECT 1 FROM positions
              WHERE user_id = %s AND network = %s AND product_id = %s
                AND isolated = %s AND closed_at IS NULL
            )
            """,
            (
                user_id,
                network,
                product_id,
                isolated,
                pair,
                side,
                str(amount),
                _decimal_or_none(pos.get("notional_value")),
                _decimal_or_none(pos.get("avg_entry_price")),
                _decimal_or_none(pos.get("est_liq_price")),
                _decimal_or_none(pos.get("est_pnl")),
                _decimal_or_none(pos.get("margin_used")),
                leverage,
                user_id,
                network,
                product_id,
                isolated,
            ),
        )


def _write_open_orders(user_id: int, network: str, orders: list[dict[str, Any]]) -> None:
    digests = [str(o.get("digest") or o.get("order_digest") or "") for o in orders if o.get("digest") or o.get("order_digest")]
    if digests:
        placeholders = ", ".join(["%s"] * len(digests))
        execute(
            f"""
            UPDATE open_orders
            SET status = 'cancelled_or_filled', synced_at = now()
            WHERE user_id = %s AND network = %s AND status IN ('open', 'pending', 'armed')
              AND order_digest NOT IN ({placeholders})
            """,
            (user_id, network, *digests),
        )
    elif not orders:
        execute(
            """
            UPDATE open_orders
            SET status = 'cancelled_or_filled', synced_at = now()
            WHERE user_id = %s AND network = %s AND status IN ('open', 'pending', 'armed')
            """,
            (user_id, network),
        )
    else:
        logger.warning(
            "portfolio sync saw %s orders without stable digests; skipping stale order sweep user=%s network=%s",
            len(orders),
            user_id,
            network,
        )
    for order in orders:
        digest = str(order.get("digest") or order.get("order_digest") or "")
        if not digest:
            continue
        product_id = order.get("product_id")
        pair = str(order.get("product_name") or order.get("product") or (f"ID:{product_id}" if product_id else ""))
        execute(
            """
            INSERT INTO open_orders (
              user_id, network, product_id, pair, side, order_type, size, price,
              order_digest, status, placed_at, updated_at, synced_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', now(), now(), now())
            ON CONFLICT (user_id, network, order_digest)
            DO UPDATE SET product_id = EXCLUDED.product_id, pair = EXCLUDED.pair,
              side = EXCLUDED.side, order_type = EXCLUDED.order_type, size = EXCLUDED.size,
              price = EXCLUDED.price, status = 'open', updated_at = now(), synced_at = now()
            """,
            (
                user_id,
                network,
                product_id,
                pair,
                str(order.get("side") or "").lower() or "unknown",
                str(order.get("type") or order.get("order_type") or "limit").lower(),
                str(abs(Decimal(str(order.get("amount") or order.get("size") or 0)))),
                _decimal_or_none(order.get("price") or order.get("limit_price")),
                digest,
            ),
        )


def _write_matches(user_id: int, network: str, matches: list[dict[str, Any]]) -> int:
    inserted = 0
    table = f"trades_{_network_table_suffix(network)}"
    # Sessions that received a (late-syncing) fill this pass. The stored session
    # totals (Volume/Fees/PnL on the Performance + Share-PnL cards) are written by
    # the finalize rollup at STOP — but a session's CLOSE fills sync AFTER stop, so
    # without re-rolling up here the shareable card would miss the close turnover.
    touched_sessions: set[int] = set()
    for match in matches:
        submission_idx = match.get("submission_idx")
        if submission_idx is None:
            continue
        order = match.get("order") or {}
        product_id = match.get("product_id") or order.get("product_id") or 0
        row = query_one(f"SELECT id FROM {table} WHERE submission_idx = %s LIMIT 1", (submission_idx,))
        if row:
            continue
        base_x18 = _x18_field(match, "base_filled", "base_filled_x18")
        quote_x18 = _x18_field(match, "quote_filled", "quote_filled_x18")
        fee_x18 = _x18_field(match, "fee", "fee_x18")
        # This venue's indexer match has NO per-fill realized PnL field (SDK
        # ``IndexerMatch`` carries only base/quote/fee), so this is ALWAYS "0".
        # It is persisted only for column-shape compatibility — realized PnL is
        # DERIVED downstream from signed cash flow (see get_session_live_metrics);
        # never treat ``realized_pnl_x18`` as authoritative PnL.
        # For the same reason there is no ``is_taker`` to write here: the SDK match
        # model has no maker/taker flag either, so a VENUE-ONLY fill (one with no
        # recorder row) leaves the column at its default. Engine fills get the flag
        # from the executor that placed them (Executor._fill_was_taker), which reads
        # the order type actually sent to the venue — the only place that knows.
        # Do NOT "fix" this by guessing from price-vs-mid at sync time; the book has
        # moved by then. The raw archive HTTP shape may carry is_taker (see
        # venue/nado_archive._parse_match); switching this writer onto that source
        # is the real fix if venue-only rows ever need the flag.
        pnl_x18 = _x18_field(match, "realized_pnl", "realized_pnl_x18")
        base_amount = from_x18(base_x18)
        # HUMAN columns. Rollups + PnL/History cards read fill_size * fill_price
        # (human), NOT the *_x18 columns. A venue-only fill (no recorder row) was
        # inserted with x18 amounts but NULL price, so it counted as $0 volume even
        # once attributed. Derive human size/price/fee from the authoritative venue
        # quote (the notional) so every synced fill contributes real volume.
        base_h = abs(base_amount)
        quote_h = abs(from_x18(quote_x18))
        fee_h = abs(from_x18(fee_x18))
        price_h = (quote_h / base_h) if base_h > 0 else Decimal(0)
        digest = str(
            match.get("digest")
            or match.get("order_digest")
            or order.get("digest")
            or order.get("order_digest")
            or ""
        ).strip()
        # IndexerMatch carries no product_id/name; recover BOTH from open_orders by
        # digest BEFORE the window fallback so (a) the product-scoped window resolve
        # can match the session's product (product_id=0 never matched → orphaned),
        # and (b) the inserted fill keeps a real product_id + name (not product_id=0,
        # which the per-trade card can't pair).
        recovered_pname = str(match.get("product_name") or "").strip()
        if int(product_id or 0) == 0 and digest:
            try:
                _oo = query_one(
                    "SELECT product_id, pair FROM open_orders "
                    "WHERE order_digest = %s AND COALESCE(product_id, 0) <> 0 LIMIT 1",
                    (digest,),
                )
                if _oo and _oo.get("product_id"):
                    product_id = int(_oo["product_id"])
                    if not recovered_pname and _oo.get("pair"):
                        recovered_pname = str(_oo["pair"])
            # policy: degrade-ok(open_orders product lookup best-effort; later trades/session fallbacks still run)
            except Exception:  # noqa: BLE001 - recovery is best-effort
                pass
        # A market/text-to-trade order fills instantly and leaves open_orders, so
        # its digest is often gone by sync time. Fall back to any prior trades row
        # for the same digest — execute_market_order and the engine recorder both
        # wrote one carrying the real product_id. This keeps text-to-trade fills OUT
        # of the product_id=0 bucket that History (get_paired_trades) excludes.
        inherited_session: int | None = None
        _prior_row_checked = False
        if int(product_id or 0) == 0 and digest:
            _prior_row_checked = True
            try:
                _tr = query_one(
                    f"SELECT product_id, product_name, strategy_session_id FROM {table} "
                    f"WHERE order_digest = %s AND COALESCE(product_id, 0) <> 0 "
                    f"ORDER BY id ASC LIMIT 1",
                    (digest,),
                )
                if _tr:
                    if _tr.get("product_id"):
                        product_id = int(_tr["product_id"])
                        if not recovered_pname and _tr.get("product_name"):
                            recovered_pname = str(_tr["product_name"])
                    if _tr.get("strategy_session_id"):
                        inherited_session = int(_tr["strategy_session_id"])
            # policy: degrade-ok(prior-trades product lookup best-effort; session-product fallback still runs)
            except Exception:  # noqa: BLE001 - recovery is best-effort
                pass
        session_id, source, intent_found, intent_pid, intent_pname = _back_link_intent(digest, network)
        if int(product_id or 0) == 0 and intent_pid:
            # Close tags carry the product (link_digest_intent): the only
            # product source for an instantly-filled close that never rested
            # in open_orders and has no product-carrying recorder row.
            product_id = int(intent_pid)
            if not recovered_pname and intent_pname:
                recovered_pname = intent_pname
        if session_id is None and not intent_found and digest and not _prior_row_checked:
            # Lazy variant of the prior-row lookup above for fills whose
            # product resolved earlier: only consulted when the intent row is
            # missing, so the common (tagged) path costs no extra query.
            try:
                _tr = query_one(
                    f"SELECT strategy_session_id FROM {table} "
                    f"WHERE order_digest = %s AND strategy_session_id IS NOT NULL "
                    f"ORDER BY id ASC LIMIT 1",
                    (digest,),
                )
                if _tr and _tr.get("strategy_session_id"):
                    inherited_session = int(_tr["strategy_session_id"])
            # policy: degrade-ok(session-inherit lookup best-effort; fill stays unattributed, window fallback still runs)
            except Exception:  # noqa: BLE001 - recovery is best-effort
                pass
        if session_id is None and not intent_found and inherited_session is not None:
            # No intent row at all (best-effort link lost), but the bot's own
            # recorder row for this digest knows the session. Inherit it and
            # label the fill 'strategy' so the session rollup counts it —
            # otherwise a session flatten orphans as manual/unattributed and
            # its close volume/fees/PnL silently vanish from Performance
            # (prod session #115, 2026-07-09).
            session_id = inherited_session
            source = "strategy"
            intent_found = True
        if session_id is None and not intent_found:
            # Digest back-link missed (NO order_intents row for this fill).
            # Recover attribution by product + time window so the match still
            # counts toward its session's rollup (see _resolve_session_by_window).
            # Gated on intent_found: a fill TAGGED manual (bot manual trade,
            # incl. closes) must never be swallowed into a concurrent session
            # on the same product — that hid it from History (session NOT NULL)
            # while the rollup still excluded it (source manual): invisible
            # everywhere.
            session_id = _resolve_session_by_window(
                user_id, network, product_id, _timestamp_or_now(match.get("timestamp"))
            )
            if session_id is not None:
                # A window-recovered fill is an engine fill whose placement
                # link was lost (this fallback's documented purpose). Label it
                # source='strategy' so the session rollup — which excludes
                # 'manual' — actually counts it; leaving it 'manual' made the
                # recovery self-defeating (attributed but never totted up).
                source = "strategy"

        # Last-resort product_id: a session-attributed fill whose id is still 0
        # inherits its SESSION's product. This is the stop-close case — the flatten
        # market order fills instantly (digest gone from open_orders) with no prior
        # recorder row, so neither recovery above resolves it — yet it IS the
        # session's own close and must carry the session's product to count.
        if int(product_id or 0) == 0 and session_id:
            try:
                _sp = query_one(
                    "SELECT product_id, product_name FROM strategy_sessions "
                    "WHERE id = %s AND COALESCE(product_id, 0) <> 0 LIMIT 1",
                    (int(session_id),),
                )
                if _sp and _sp.get("product_id"):
                    product_id = int(_sp["product_id"])
                    if not recovered_pname and _sp.get("product_name"):
                        recovered_pname = str(_sp["product_name"])
            # policy: degrade-ok(session-product fallback best-effort; fill may stay product_id=0, excluded from per-trade card)
            except Exception:  # noqa: BLE001 - recovery is best-effort
                pass

        if session_id is not None:
            touched_sessions.add(int(session_id))

        # Every recorder row (``source`` = strategy / manual / copy / vol_stop)
        # is written at fill time carrying human columns and — crucially — a real
        # ``product_id`` (IndexerMatch has none). Enrich the earliest such row for
        # this digest with the authoritative venue x18 PnL/fee instead of inserting
        # a duplicate, so the row stays canonical, the match is counted once, and
        # it KEEPS ITS SOURCE (the UPDATE never rewrites ``source``).
        #
        # The source restriction is deliberately absent: it used to be
        # ``source IN ('strategy','manual')``, which skipped ``copy`` (and
        # ``vol_stop``) recorder rows — so a copied fill's venue match inserted a
        # NEW ``source='manual'`` duplicate that then LEAKED into the History tab
        # (which is manual-only) and double-counted. Matching any prior recorder
        # row by digest fixes the leak AND the duplicate.
        #
        # The bot's synthetic account-wide ``MARKET_CLOSE`` rows are still EXCLUDED
        # (``NOT ILIKE '%close%'``) so an oversized close never acquires a
        # submission_idx and pollutes the ledger.
        if digest:
            recorder_row = query_one(
                f"SELECT id, fill_size FROM {table} "
                f"WHERE order_digest = %s "
                f"AND submission_idx IS NULL AND COALESCE(order_type, '') NOT ILIKE '%%close%%' "
                f"ORDER BY id ASC LIMIT 1",
                (digest,),
            )
            if recorder_row:
                # FILL-PRICE-CONSISTENCY: the venue quote is authoritative. If the
                # recorder's own size/price disagree materially with it, that row
                # was priced off a requested/partial size — log it so the source
                # is visible instead of silently shipping a phantom price.
                _rec_size = abs(float(recorder_row.get("fill_size") or 0) or 0.0)
                if _rec_size > 0 and float(base_h) > 0:
                    _skew = abs(_rec_size - float(base_h)) / float(base_h)
                    if _skew > 0.02:
                        logger.warning(
                            "FILL-PRICE-CONSISTENCY user=%s pid=%s submission_idx=%s "
                            "recorder fill_size=%.8f vs venue base=%.8f (%.1f%% skew) "
                            "— human columns resynced to the venue quote "
                            "(price %.2f)",
                            user_id, product_id, submission_idx, _rec_size,
                            float(base_h), _skew * 100.0, float(price_h),
                        )
                execute(
                    f"""
                    UPDATE {table} SET
                      submission_idx = %s,
                      realized_pnl_x18 = %s,
                      fee_x18 = %s,
                      base_filled_x18 = %s,
                      quote_filled_x18 = %s,
                      isolated = %s,
                      strategy_session_id = COALESCE(strategy_session_id, %s),
                      -- Sync the HUMAN columns to the authoritative venue quote.
                      -- These MUST track the x18 columns being overwritten just
                      -- above: the old code only backfilled them when they were
                      -- ZERO, so a recorder row that already carried its
                      -- REQUESTED/partial size kept it while base/quote_x18
                      -- became the venue's real (larger) fill. The row then
                      -- disagreed with itself and fill_price stayed at
                      -- quote / requested_size — e.g. 250.18 / 0.00265 = 94,408
                      -- while BTC was 63.6k (~1.49x). That phantom price made a
                      -- grid session book a huge fake profit and the session
                      -- take-profit rail stopped the bot at a real $0.68
                      -- (sessions 164 + 159, 2026-07-28).
                      -- fill_fee follows for the SAME reason (audit follow-up):
                      -- fee_x18 above is overwritten unconditionally, so leaving
                      -- fill_fee at a submit-time estimate for the FULL requested
                      -- size left the row contradicting itself again, and every
                      -- later match inserted its own fee row on top — an
                      -- over-counted fee. Fees are netted into the session SL/TP
                      -- rail, so an inflated fee stops a run early: the same
                      -- failure mode as the phantom price.
                      fill_size = CASE WHEN %s > 0 THEN %s ELSE fill_size END,
                      fill_price = CASE WHEN %s > 0 THEN %s ELSE fill_price END,
                      price = CASE WHEN %s > 0 THEN %s ELSE price END,
                      fill_fee = CASE WHEN %s > 0 THEN %s ELSE fill_fee END
                    WHERE id = %s
                    """,
                    (
                        submission_idx, pnl_x18, fee_x18, base_x18, quote_x18,
                        bool(match.get("isolated")), session_id,
                        float(base_h), float(base_h),
                        float(price_h), float(price_h),
                        float(price_h), float(price_h),
                        float(fee_h), float(fee_h),
                        recorder_row["id"],
                    ),
                )
                inserted += 1
                _maybe_increment_session_win_loss(session_id, pnl_x18)
                continue

        # product_id + name were already recovered from open_orders above (used for
        # the window fallback too); reuse them here so the inserted fill is
        # attributable to a product instead of the product_id=0 bucket.
        insert_pid = int(product_id or 0)
        insert_pname = recovered_pname
        # Resolve the real catalog name when we have a product id but no name —
        # otherwise the row stores "ID:5" and every downstream surface (History,
        # HOWL top pairs, the Share PnL card) shows the raw id instead of the pair.
        if (not insert_pname) and insert_pid > 0:
            try:
                from src.nadobro.config import get_product_name as _gpn

                resolved = _gpn(insert_pid, network=network)
                if resolved and not str(resolved).startswith("ID:"):
                    insert_pname = resolved
            except Exception:  # policy: degrade-ok(name resolution best-effort)
                pass
        insert_pname = insert_pname or f"ID:{insert_pid}"

        execute(
            f"""
            INSERT INTO {table} (
              user_id, product_id, product_name, order_type, side, size,
              fill_size, price, fill_price, fill_fee, status,
              submission_idx, isolated, realized_pnl_x18, fee_x18, base_filled_x18, quote_filled_x18,
              order_digest, strategy_session_id, source, via_nadobro,
              filled_at, created_at
            )
            VALUES (%s, %s, %s, 'match', %s, %s, %s, %s, %s, %s, 'filled', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                user_id,
                insert_pid,
                insert_pname,
                "long" if base_amount >= 0 else "short",
                str(abs(base_amount)),
                # Human columns so this venue-only fill contributes real volume/fees
                # (rollups read fill_size * fill_price, not the x18 columns). Use
                # fixed-point (:f) so a value like 100/1.25 is stored "80", not "8E+1".
                f"{base_h:f}",
                f"{price_h:f}",
                f"{price_h:f}",
                f"{fee_h:f}",
                submission_idx,
                bool(match.get("isolated")),
                pnl_x18,
                fee_x18,
                base_x18,
                quote_x18,
                digest or None,
                session_id,
                source,
                # Analytics attribution: this fill came from an order Nadobro
                # placed iff its digest is registered in order_intents.
                # `source` cannot carry this — unlinked venue fills default to
                # 'manual', conflating bot trades with Nado-UI trades.
                bool(intent_found),
                _timestamp_or_now(match.get("timestamp")),
            ),
        )
        inserted += 1
        _maybe_increment_session_win_loss(session_id, pnl_x18)

    # Re-roll up any session that got a fill this pass so its STORED totals
    # (Volume/Fees/PnL/counts) reflect late-syncing fills — crucially the close
    # turnover, which lands after the finalize rollup ran at stop. Off the event
    # loop (this runs in a worker thread) and idempotent (recomputed from trades).
    for _sid in touched_sessions:
        try:
            from src.nadobro.models.database import (
                rollup_engine_session_pnl_funding,
                rollup_session_from_trades,
            )

            rollup_session_from_trades(_sid, network)
            # Realized PnL / funding are derived (venue reports no per-fill PnL);
            # must run AFTER rollup_session_from_trades. GATE to engine strategies
            # (this fn is not self-gating — the caller must, or a legacy session's
            # PnL gets overwritten).
            _srow = query_one("SELECT strategy FROM strategy_sessions WHERE id = %s", (_sid,))
            _strat = str((_srow or {}).get("strategy") or "").lower()
            from src.nadobro.strategy.engine_runtime import ENGINE_MAPPED_STRATEGIES
            if _strat in ENGINE_MAPPED_STRATEGIES:
                rollup_engine_session_pnl_funding(_sid, network)
        except Exception:  # noqa: BLE001 - re-rollup is best-effort, never break sync
            logger.debug("post-sync session re-rollup failed sid=%s", _sid, exc_info=True)
    return inserted


def _maybe_increment_session_win_loss(session_id: int | None, pnl_x18: str) -> None:
    """Bump ``strategy_sessions.win_count`` / ``loss_count`` on decisive closes.

    The performance cards read these counters directly. We increment here
    when a venue-sync fill arrives with a back-linked ``strategy_session_id``
    and a non-zero realized PnL — belt-and-suspenders alongside the
    session-end rollup in ``bot_runtime._finalize_session``.
    """
    if not session_id:
        return
    try:
        pnl = float(from_x18(pnl_x18))
    except Exception:
        return
    if abs(pnl) <= 1e-9:
        return
    column = "win_count" if pnl > 0 else "loss_count"
    try:
        execute(
            f"UPDATE strategy_sessions SET {column} = {column} + 1 WHERE id = %s",
            (int(session_id),),
        )
    except Exception:
        logger.debug("session win/loss increment failed session=%s", session_id, exc_info=True)


def _back_link_intent(
    digest: str, network: str
) -> tuple[int | None, str, bool, int | None, str | None]:
    """Resolve ``(strategy_session_id, source, intent_found, product_id,
    product_name)`` from order_intents.

    Strategies, the engine adapter, and the close paths write to
    ``order_intents`` with ``value`` JSONB carrying ``strategy_session_id``
    and ``source``. Venue-sync fills only arrive with an ``order_digest``;
    this lookup re-attaches the tags so per-session rollups and History
    (source=manual) filtering work. Returns ``(None, "manual", False)`` when
    no intent is found.

    ``intent_found`` distinguishes "this digest has no tag at all" (an engine
    fill whose placement link failed, or external venue activity — the window
    fallback may recover it) from "this digest is TAGGED manual with no
    session" (a genuine bot manual trade — the window fallback must NOT
    swallow it into a concurrent session on the same product, or the fill
    vanishes from History while staying excluded from the session rollup).

    Execution-mode safety: the resolved ``strategy_session_id`` is dropped
    if it doesn't belong to the same ``network`` as the incoming fill, so a
    testnet fill never links to a mainnet session (or vice-versa).
    """
    if not digest:
        return None, "manual", False, None, None
    try:
        intent = query_one(
            "SELECT value FROM order_intents WHERE order_digest = %s ORDER BY updated_at DESC NULLS LAST LIMIT 1",
            (digest,),
        )
    except Exception:
        logger.debug("intent back-link query failed digest=%s", digest, exc_info=True)
        return None, "manual", False, None, None
    if not intent:
        return None, "manual", False, None, None
    value = intent.get("value") or {}
    if isinstance(value, str):
        try:
            import json

            value = json.loads(value)
        except Exception:
            value = {}
    if not isinstance(value, dict):
        return None, "manual", True, None, None
    raw_session = value.get("strategy_session_id")
    session_id: int | None
    try:
        session_id = int(raw_session) if raw_session is not None else None
    except (TypeError, ValueError):
        session_id = None
    source = str(value.get("source") or "manual") or "manual"
    # Product identity stored by link_digest_intent (close paths pass it): the
    # only reliable product source for an instantly-filled close whose digest
    # never rested in open_orders.
    try:
        intent_pid = int(value.get("product_id") or 0) or None
    except (TypeError, ValueError):
        intent_pid = None
    intent_pname = str(value.get("product_name") or "").strip() or None
    if session_id is not None:
        try:
            session_row = query_one(
                "SELECT network FROM strategy_sessions WHERE id = %s",
                (int(session_id),),
            )
        except Exception:
            session_row = None
        if not session_row or _normalize_network(session_row.get("network")) != _normalize_network(network):
            logger.debug(
                "back-link dropped: session=%s network=%s != fill network=%s",
                session_id,
                session_row.get("network") if session_row else None,
                network,
            )
            return None, source, True, intent_pid, intent_pname
    return session_id, source, True, intent_pid, intent_pname


def _resolve_session_by_window(
    user_id: int, network: str, product_id: Any, ts: Any
) -> int | None:
    """Fallback session attribution when the digest back-link misses: find the
    user's strategy session on this product whose window contains the fill.

    The digest path (``_back_link_intent``) depends on the engine having written
    an ``order_intents`` row for the order; engine fills that never got one land
    untagged and silently vanish from per-session rollups. Attributing by
    product + time window recovers them. Scoped to the fill's own ``network`` and
    ``product_id`` so it can't cross-link.
    """
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return None
    if ts is None:
        return None
    try:
        row = query_one(
            """
            SELECT id FROM strategy_sessions
            WHERE user_id = %s AND network = %s AND product_id = %s
              AND started_at <= %s
              AND (stopped_at IS NULL OR stopped_at >= %s)
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (int(user_id), _normalize_network(network), pid, ts, ts),
        )
    except Exception:
        logger.debug("window session resolve failed pid=%s", product_id, exc_info=True)
        return None
    if row and row.get("id") is not None:
        return int(row["id"])

    # DN sessions store the perp product id on strategy_sessions, but a missed
    # digest link can be a SPOT-leg fill. Recover those by matching the fill pid
    # against the session's resolved spot/perp pair.
    try:
        sessions = query_all(
            """
            SELECT id, product_name FROM strategy_sessions
            WHERE user_id = %s AND network = %s AND strategy = 'dn'
              AND started_at <= %s
              AND (stopped_at IS NULL OR stopped_at >= %s)
            ORDER BY started_at DESC
            LIMIT 20
            """,
            (int(user_id), _normalize_network(network), ts, ts),
        )
    except Exception:
        logger.debug("dn window session resolve failed pid=%s", product_id, exc_info=True)
        return None
    if not sessions:
        return None
    try:
        from src.nadobro.venue.product_catalog import get_dn_pair

        for sess in sessions:
            product = str(sess.get("product_name") or "").strip()
            candidates = [product]
            if "-" in product:
                candidates.append(product.split("-", 1)[0])
            for candidate in candidates:
                if not candidate:
                    continue
                dn_pair = get_dn_pair(candidate, network=network) or {}
                pair_ids = {dn_pair.get("spot_product_id"), dn_pair.get("perp_product_id")}
                if pid in {int(v) for v in pair_ids if v is not None}:
                    return int(sess["id"])
    except Exception:
        logger.debug("dn product-pair session resolve failed pid=%s", product_id, exc_info=True)
    return None


def _write_funding(user_id: int, network: str, payments: list[dict[str, Any]]) -> int:
    inserted = 0
    table = f"funding_payments_{_network_table_suffix(network)}"
    for payment in payments:
        if str(payment.get("type") or "funding") != "funding":
            continue
        product_id = int(payment.get("product_id") or 0)
        paid_at = _timestamp_or_now(payment.get("timestamp"))
        amount_x18 = _x18_field(payment, "amount", "amount_x18")
        row = query_one(
            f"SELECT id FROM {table} WHERE user_id = %s AND product_id = %s AND paid_at = %s AND amount_x18 = %s LIMIT 1",
            (user_id, product_id, paid_at, amount_x18),
        )
        if row:
            continue
        execute(
            f"""
            INSERT INTO {table} (
              user_id, product_id, amount_x18, balance_amount_x18,
              rate_x18, oracle_price_x18, paid_at, synced_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                user_id,
                product_id,
                amount_x18,
                payment.get("balance_amount") or payment.get("balance_amount_x18"),
                payment.get("rate_x18"),
                payment.get("oracle_price_x18"),
                paid_at,
            ),
        )
        inserted += 1
    return inserted


def _write_sync_log_error(user_id: int, network: str, duration_ms: int, error: str) -> None:
    try:
        execute(
            "INSERT INTO sync_log (user_id, network, duration_ms, error) VALUES (%s, %s, %s, %s)",
            (user_id, network, duration_ms, error[:1000]),
        )
    except Exception:
        logger.debug("failed to write portfolio sync error log", exc_info=True)


def _decimal_or_none(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(Decimal(str(value)))


def _resolve_leverage(pos: dict[str, Any]) -> str:
    """Return a non-NULL leverage string.

    Nado's ``calculate_account_summary`` often omits ``leverage`` on cross/
    isolated rows; the ``positions`` table has a legacy ``NOT NULL DEFAULT 1``
    column from the original DDL. Derive leverage from
    ``notional / max(margin_used, eps)`` when available, otherwise fall back
    to ``1`` so the row insert never fails.
    """
    raw = pos.get("leverage")
    if raw is not None and str(raw) != "":
        try:
            value = Decimal(str(raw))
            if value > 0:
                return str(value)
        except Exception:  # policy: degrade-ok(malformed venue leverage; falls back to derived value)
            pass
    try:
        notional = Decimal(str(pos.get("notional_value") or 0))
        margin = Decimal(str(pos.get("margin_used") or 0))
        if margin > 0 and notional > 0:
            return str(notional / margin)
    except Exception:  # policy: degrade-ok(malformed venue fields; defaults to 1x)
        pass
    return "1"


def _timestamp_or_now(value: Any) -> datetime:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value)
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _now()


def _normalize_network(network: Any) -> str:
    text = str(network or "mainnet").lower()
    return "testnet" if text == "testnet" else "mainnet"


def _network_table_suffix(network: Any) -> str:
    normalized = _normalize_network(network)
    if normalized not in {"mainnet", "testnet"}:
        raise ValueError(f"unsupported network: {network}")
    return normalized


def _dedupe_orders_by_digest(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate open orders when trigger + plain fetches overlap."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for order in orders:
        digest = str(order.get("digest") or order.get("order_digest") or "")
        if digest:
            if digest in seen:
                continue
            seen.add(digest)
        deduped.append(order)
    return deduped


_PLACEHOLDER_SYMBOL_RE = re.compile(r"^(?:Product[_\s-]?\d+|ID:\d+)?$", re.IGNORECASE)


def _resolve_product_names(
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    network: str,
) -> None:
    """Replace SDK placeholder symbols (``Product_2``) with catalog names.

    Mutates rows in place. Resolution is per-product-id and memoized for the
    snapshot, so a portfolio with N rows costs at most one catalog lookup per
    distinct product.
    """
    from src.nadobro.config import get_product_name

    resolved: dict[int, str] = {}

    def _name_for(pid: Any) -> str | None:
        try:
            product_id = int(pid)
        except (TypeError, ValueError):
            return None
        if product_id not in resolved:
            try:
                resolved[product_id] = str(get_product_name(product_id, network=network))
            except Exception:  # policy: degrade-ok(catalog miss; placeholder stays)
                resolved[product_id] = ""
        name = resolved[product_id]
        # A failed catalog lookup returns "ID:{pid}" — not an improvement.
        return name if name and not name.startswith("ID:") else None

    for row in positions:
        current = str(row.get("symbol") or row.get("product_name") or "")
        if _PLACEHOLDER_SYMBOL_RE.match(current):
            name = _name_for(row.get("product_id"))
            if name:
                row["symbol"] = name
                row["product_name"] = name
    for row in orders:
        current = str(row.get("product_name") or row.get("product") or "")
        if _PLACEHOLDER_SYMBOL_RE.match(current):
            name = _name_for(row.get("product_id"))
            if name:
                row["product_name"] = name
    for row in matches:
        current = str(row.get("product_name") or "")
        if _PLACEHOLDER_SYMBOL_RE.match(current):
            name = _name_for(row.get("product_id"))
            if name:
                row["product_name"] = name


def _normalize_order_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        rows = value.get("orders") or value.get("data") or []
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _x18_field(row: dict[str, Any], human_key: str, x18_key: str) -> str:
    if row.get(x18_key) is not None:
        return str(row.get(x18_key))
    value = row.get(human_key)
    if value is None or value == "":
        return "0"
    text = str(value)
    if any(ch in text for ch in ".eE"):
        return str(int(Decimal(text) * Decimal("1000000000000000000")))
    return text


def _mark_trigger_order(order: dict[str, Any]) -> dict[str, Any]:
    row = dict(order or {})
    row["type"] = str(row.get("type") or row.get("order_type") or "trigger").upper()
    row["is_trigger"] = True
    return row
