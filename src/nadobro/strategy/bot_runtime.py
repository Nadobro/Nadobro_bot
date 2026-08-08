import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

# Keys that must never be overwritten from persisted strategy *settings* into live runtime state.
_STRATEGY_SETTINGS_RUNTIME_BLOCKLIST = frozenset({
    "running", "strategy", "strategy_id_v2", "product", "last_run_ts", "runs", "started_at",
    "reference_price", "last_error", "error_streak", "last_action", "last_action_detail",
    "strategy_session_id", "bro_state", "mm_paused", "mm_pause_reason", "mm_last_metrics",
    "worker_group", "worker_last_heartbeat", "last_dispatch_ts", "last_cycle_ms",
    "last_cycle_result", "worker_pid", "grid_anchor_price", "grid_buy_exposure_price",
    "grid_sell_exposure_price", "grid_drift_from_anchor_pct", "grid_reset_active",
    "grid_reset_side", "grid_last_cycle_pnl_usd",
    # fill-anchored grid soft-reset telemetry (green/red trigger levels).
    "grid_reset_up_price", "grid_reset_down_price", "grid_soft_reset_engaged",
    "grid_net_base", "grid_reset_threshold_bp", "grid_mode",
    # MM participation/duration (TWAP): preset + user duration are start-snapshot
    # (they freeze the chunk/run-duration computed at start, like vol_direction);
    # the computed run duration, chunk + one-shot notify flag are runtime-owned.
    # NOTE: twap_pause_move_bp is intentionally NOT here — it is read every cycle
    # so live UI edits to the fast-move pause threshold take effect mid-run.
    "participation_preset", "mm_duration_minutes", "mm_run_duration_minutes",
    "mm_duration_target_notified", "mm_cycle_notional_usd",
    # TWAP fast-move pause runtime telemetry (last mid baseline, paused flag).
    "twap_last_mid", "twap_paused",
    "dn_last_funding_rate", "dn_unfavorable_count",
    "dn_mode", "order_observability",
    "last_error_category", "vol_order_attempts", "vol_order_failures", "last_order_error", "last_order_ts",
    "vol_market",
    # VOL perp direction is chosen at start (Start Long / Start Short). Saved dashboard
    # `vol_direction` must not overwrite live runtime each tick — that caused opposite-side
    # entries when UI showed one direction and stale settings said another.
    "vol_direction",
})

from src.nadobro.config import (
    get_dn_pair,
    get_dn_products,
    get_product_id,
    get_product_max_leverage,
    get_spot_product_id,
    get_perp_products,
    list_volume_spot_product_names,
    normalize_volume_spot_symbol,
)
from src.nadobro.models.database import (
    get_bot_state_raw, set_bot_state,
    insert_strategy_session, update_strategy_session, increment_session_metrics,
    get_running_strategy_sessions, rollup_session_from_trades,
)
from src.nadobro.db import query_all
from src.nadobro.users.admin_service import is_trading_paused
from src.nadobro.users.settings_service import get_strategy_settings
from src.nadobro.trading.trade_service import (
    close_all_positions,
    close_delta_neutral_legs,
    get_trade_analytics,
)
from src.nadobro.users.user_service import (
    get_user_nado_client,
    get_user_readonly_client,
    get_user,
    run_strategy_start_preflight,
)
from src.nadobro.core.async_utils import run_blocking, run_blocking_db, run_blocking_sdk
from src.nadobro.core.perf import timed_metric, record_metric
from src.nadobro.utils.env import env_bool, env_float, env_tristate
from src.nadobro.core.cadence import FAST_CADENCE_STRATEGIES, effective_interval_seconds
from src.nadobro.trading.execution_queue import enqueue_strategy
from src.nadobro.core.feature_flags import legacy_bro_autoloop_enabled
from src.nadobro.strategy.strategy_registry import (
    SUPPORTED_STRATEGIES,
    migrate_state_strategy,
    normalize_strategy_id,
    runtime_strategy_default,
    strategy_display_name,
)
from src.nadobro.strategy.strategy_lifecycle import cleanup_strategy_positions

logger = logging.getLogger(__name__)

STATE_PREFIX = "strategy_bot:"
RUNTIME_TICK_SECONDS = 20
BRO_MIGRATION_NOTICE = "Legacy Alpha Agent has been retired. Pick a strategy from the Strategy hub or use the Nado Vault."

# User-facing strategy names for runtime notifications. Several strategies share
# one controller (rgrid + dgrid both run DynamicGridController), so messages must
# name the mode the USER started, not the controller behind it.
_STRATEGY_DISPLAY_NAMES = {
    "grid": "GRID",
    "rgrid": "Reverse GRID",
    "dgrid": "Dynamic GRID",
    "mid": "Mid Mode",
    "vol": "Vol Bot",
    "dn": "Delta Neutral",
}


def _strategy_cycle_timeout_seconds() -> float | None:
    """Wall-clock cap for one strategy cycle (single-process path). 0 or unset disables."""
    raw = (os.environ.get("NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS") or "180").strip()
    try:
        v = float(raw)
    except ValueError:
        return 180.0
    if v <= 0:
        return None
    return v


def _vol_use_multiprocess() -> bool:
    return env_bool("NADO_VOL_USE_MULTIPROCESS", False)


def _strategy_use_multiprocess(strategy: str) -> bool:
    strategy_key = str(strategy or "").lower().strip()
    specific = env_tristate(f"NADO_{strategy_key.upper()}_USE_MULTIPROCESS")
    if specific is not None:
        return specific
    if strategy_key == "vol":
        return _vol_use_multiprocess()
    return env_bool("NADO_USE_MULTIPROCESS_STRATEGIES", False)


def _vol_call_timeout_seconds() -> float:
    raw = (os.environ.get("NADO_VOL_CALL_TIMEOUT_SECONDS") or "12").strip()
    try:
        val = float(raw)
    except Exception:
        return 12.0
    return max(3.0, val)


MAX_OPEN_ORDERS_PER_PRODUCT = 6
STRATEGY_ERROR_ALERT_STREAK = 3

# Engine strategies that spawn their executors on the first *tick* (not in
# on_start). For these the eager kickoff must fire an immediate follow-up tick
# or the strategy posts nothing until the scheduler's next interval. rgrid now
# runs DynamicGridController (spawns in on_tick), so it joins this set.
_SPAWN_ON_TICK_STRATEGIES = {"dgrid", "rgrid", "mid"}

_bot_app = None
_runtime_loop: asyncio.AbstractEventLoop | None = None
_tasks: dict[str, asyncio.Task] = {}
_job_locks: dict[str, asyncio.Lock] = {}
_job_pending_payloads: dict[str, dict] = {}
_job_coalesce_counts: dict[str, int] = {}
_job_stats: dict[str, int] = {
    "cycles_started": 0,
    "cycles_ok": 0,
    "cycles_failed": 0,
    "coalesced_ticks": 0,
    "deferred_cycles": 0,
    "cycle_timeouts": 0,
    "vol_coalesced_ticks": 0,
    "vol_deferred_cycles": 0,
    "vol_overlap_skips": 0,
}
_process_worker_mode = False
_CYCLE_SKIP_MARKERS = frozenset({"maintenance_pause", "skipped_interval"})


def _normalize_strategy_id(strategy: str) -> str:
    return normalize_strategy_id(strategy)


def _migrate_state_strategy(state: dict) -> dict:
    return migrate_state_strategy(state)


def _safe_last_run_ts(raw) -> float:
    """Parse last_run_ts from JSON state (float, ISO string, or datetime)."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, datetime):
        try:
            return float(raw.timestamp())
        except Exception:
            return 0.0
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            pass
        try:
            iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
            return float(datetime.fromisoformat(iso).timestamp())
        except Exception:
            return 0.0
    return 0.0


def _strategy_display_name(strategy: str) -> str:
    return strategy_display_name(strategy)


def _market_label_for_strategy(strategy: str, product: str, state: dict | None = None) -> str:
    """Return a user-facing market label like 'KBTC SPOT' or 'BTC-PERP'.

    Volume is spot-only as of 2026-05 — its alerts must not show '-PERP'. All
    other strategies (GRID family, MM, MID, DN, BRO) trade perps and keep the
    legacy '-PERP' suffix.
    """
    s = str(strategy or "").strip().lower()
    p = str(product or "").upper()
    if s == "vol":
        return f"{p} SPOT"
    return f"{p}-PERP"


def _update_order_observability(state: dict, result: dict) -> None:
    obs = state.get("order_observability")
    if not isinstance(obs, dict):
        obs = {
            "cycles": 0,
            "ok_cycles": 0,
            "failed_cycles": 0,
            "cycles_with_orders": 0,
            "zero_order_cycles": 0,
            "orders_placed": 0,
            "orders_filled": 0,
            "orders_cancelled": 0,
            "last_action": "",
            "last_reason": "",
            "last_ts": 0.0,
        }

    orders_placed = int(result.get("orders_placed", 0) or 0)
    orders_filled = int(result.get("orders_filled", 0) or 0)
    orders_cancelled = int(result.get("orders_cancelled", 0) or 0)
    success = bool(result.get("success", True))

    obs["cycles"] = int(obs.get("cycles", 0) or 0) + 1
    if success:
        obs["ok_cycles"] = int(obs.get("ok_cycles", 0) or 0) + 1
    else:
        obs["failed_cycles"] = int(obs.get("failed_cycles", 0) or 0) + 1

    if orders_placed > 0:
        obs["cycles_with_orders"] = int(obs.get("cycles_with_orders", 0) or 0) + 1
    else:
        obs["zero_order_cycles"] = int(obs.get("zero_order_cycles", 0) or 0) + 1

    obs["orders_placed"] = int(obs.get("orders_placed", 0) or 0) + orders_placed
    obs["orders_filled"] = int(obs.get("orders_filled", 0) or 0) + orders_filled
    obs["orders_cancelled"] = int(obs.get("orders_cancelled", 0) or 0) + orders_cancelled
    obs["last_action"] = str(result.get("action") or "")
    obs["last_reason"] = str(
        result.get("reason")
        or result.get("detail")
        or result.get("error")
        or result.get("order_error")
        or ""
    )[:200]
    obs["last_ts"] = time.time()
    state["order_observability"] = obs


def _categorize_runtime_error(error_msg: str) -> str:
    msg = str(error_msg or "").lower()
    if not msg or msg == "none":
        return "unknown"
    if "wallet not initialized" in msg or "wallet client unavailable" in msg:
        return "wallet_not_initialized"
    if "wallet not linked" in msg:
        return "wallet_not_linked"
    if "builder routing misconfigured" in msg:
        return "builder_misconfigured"
    if "market price" in msg:
        return "market_data"
    if "timed out" in msg:
        return "timeout"
    if "network mismatch" in msg or "mode switched" in msg:
        return "network_mismatch"
    return "execution"


def _merge_vol_order_counters(state: dict, result: dict) -> None:
    for key in (
        "vol_phase",
        "volume_done_usd",
        "volume_remaining_usd",
        "session_volume_usd",
        "session_realized_pnl_usd",
        "vol_cycles_completed",
        "vol_entry_size",
        "vol_entry_quote",
        "vol_entry_price",
        "vol_entry_fill_ts",
        "vol_close_size",
        "vol_last_order_digest",
        "vol_last_order_kind",
    ):
        if key in result:
            state[key] = result[key]
    attempts = int(result.get("vol_order_attempts") or 0)
    failures = int(result.get("vol_order_failures") or 0)
    if attempts > 0:
        state["vol_order_attempts"] = int(state.get("vol_order_attempts") or 0) + attempts
        state["last_order_ts"] = time.time()
    if failures > 0:
        state["vol_order_failures"] = int(state.get("vol_order_failures") or 0) + failures
    last_order_error = str(result.get("last_order_error") or "").strip()
    if last_order_error:
        state["last_order_error"] = last_order_error[:220]
        state["last_error_category"] = _categorize_runtime_error(last_order_error)


def _format_cycle_failure_error(strategy: str, result: dict) -> str:
    raw = str(result.get("error") or result.get("order_error") or "unknown").strip()
    category = _categorize_runtime_error(raw)
    action = str(result.get("action") or "cycle")
    return f"{strategy.upper()}[{action}/{category}]: {raw}"[:300]


def _cycle_result_label(ok: bool, error_msg: str | None) -> str:
    if not ok:
        return "error"
    marker = str(error_msg or "").strip().lower()
    if marker in _CYCLE_SKIP_MARKERS:
        return "skipped"
    return "ok"


def set_bot_app(app):
    global _bot_app, _runtime_loop
    _bot_app = app
    try:
        _runtime_loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called outside of event loop; _ensure_task will latch loop later.
        pass


def _task_key(telegram_id: int, network: str) -> str:
    return f"{telegram_id}:{network}"


def _state_key(telegram_id: int, network: str) -> str:
    return f"{STATE_PREFIX}{telegram_id}:{network}"


def _default_state() -> dict:
    return {
        "running": False,
        "strategy": None,
        "product": "BTC",
        "notional_usd": 100.0,
        "spread_bp": 5.0,
        "tp_pct": 1.0,
        "sl_pct": 0.5,
        "leverage": 3.0,
        "slippage_pct": 1.0,
        "interval_seconds": 60,
        "reference_price": 0.0,
        "started_at": None,
        "last_run_ts": 0.0,
        "last_error": None,
        "error_streak": 0,
        "runs": 0,
        "worker_group": None,
        "worker_last_heartbeat": 0.0,
        "last_dispatch_ts": 0.0,
        "last_cycle_ms": 0.0,
        "last_cycle_result": "",
        "worker_pid": None,
        "last_error_category": "",
        "vol_order_attempts": 0,
        "vol_order_failures": 0,
        "last_order_error": "",
        "last_order_ts": 0.0,
    }


def _load_state(telegram_id: int, network: str) -> dict:
    raw = get_bot_state_raw(_state_key(telegram_id, network))
    if not raw:
        return _default_state()
    try:
        loaded = json.loads(raw)
        state = _default_state()
        state.update(loaded if isinstance(loaded, dict) else {})
        _migrate_state_strategy(state)
        return state
    except Exception:
        logger.warning("Invalid bot state JSON for user %s", telegram_id)
        return _default_state()


def _save_state(telegram_id: int, network: str, state: dict):
    set_bot_state(_state_key(telegram_id, network), state)


# ``bot_state`` is an uncached KV table: every _load_state/_save_state is a live
# psycopg2 round-trip to Supabase. A single strategy cycle does 4-8 of them, and
# calling the sync versions from a coroutine put all of that on the event loop —
# multiplied by every running strategy, that is a steady stream of stalls that
# shows up as skipped APScheduler ticks and laggy taps. Coroutines must use these
# awaitable variants instead.
async def _load_state_async(telegram_id: int, network: str) -> dict:
    return await run_blocking_db(_load_state, telegram_id, network)


async def _save_state_async(telegram_id: int, network: str, state: dict) -> None:
    await run_blocking_db(_save_state, telegram_id, network, state)


def _engine_stop_timeout_seconds() -> float:
    raw = (os.environ.get("NADO_ENGINE_STOP_TIMEOUT_SECONDS") or "30").strip()
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return 30.0


def _run_engine_stop_sync(coro_factory, *, user_id: int, network: str, strategy: str) -> tuple[bool, str | None]:
    """Run engine teardown from sync stop paths without losing the async stop."""
    timeout_s = _engine_stop_timeout_seconds()
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    target_loop = _runtime_loop if _runtime_loop and _runtime_loop.is_running() else None

    try:
        if target_loop is not None and target_loop is not current_loop:
            future = asyncio.run_coroutine_threadsafe(coro_factory(), target_loop)
            future.result(timeout=timeout_s)
        elif current_loop is not None and current_loop.is_running():
            # This fallback is only for unexpected direct calls from async code.
            # Known handlers dispatch stop operations through run_blocking so we
            # can wait for venue/order teardown to finish.
            current_loop.create_task(coro_factory())
            logger.warning(
                "engine stop scheduled asynchronously from running loop user=%s network=%s strategy=%s",
                user_id,
                network,
                strategy,
            )
        else:
            asyncio.run(coro_factory())
        return True, None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "engine runtime stop failed user=%s network=%s strategy=%s: %s",
            user_id,
            network,
            strategy,
            exc,
            exc_info=True,
        )
        return False, str(exc)


def _run_engine_start_sync(
    coro_factory, *, user_id: int, network: str, strategy: str
) -> tuple[bool, dict | None, str | None]:
    """NO_ORDERS_AUDIT-FIX-R3: same shape as ``_run_engine_stop_sync`` but
    returns the coroutine's result dict so the caller can decide whether to
    surface a failure to the user. Eager engine starts from the sync
    ``start_user_bot`` path go through here.
    """
    timeout_s = _engine_stop_timeout_seconds()  # reuse the same env knob
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    target_loop = _runtime_loop if _runtime_loop and _runtime_loop.is_running() else None
    try:
        if target_loop is not None and target_loop is not current_loop:
            future = asyncio.run_coroutine_threadsafe(coro_factory(), target_loop)
            result = future.result(timeout=timeout_s)
        elif current_loop is not None and current_loop.is_running():
            # We're already on the loop — schedule and don't wait. The
            # scheduler tick will pick it up on the next pass.
            current_loop.create_task(coro_factory())
            logger.info(
                "engine start scheduled asynchronously from running loop user=%s strategy=%s",
                user_id, strategy,
            )
            return True, {"success": True, "action": "engine_scheduled"}, None
        else:
            result = asyncio.run(coro_factory())
        if isinstance(result, dict):
            return bool(result.get("success", False)), result, result.get("error")
        return True, {"success": True}, None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "eager engine start failed user=%s network=%s strategy=%s: %s",
            user_id, network, strategy, exc, exc_info=True,
        )
        return False, None, str(exc)


def _stop_engine_runtime_for_state(telegram_id: int, network: str, state: dict) -> tuple[bool, str | None]:
    strategy = _normalize_strategy_id(str(state.get("strategy") or ""))
    if not strategy:
        return True, None
    try:
        from src.nadobro.strategy import engine_runtime
    except Exception as exc:  # noqa: BLE001
        logger.warning("engine runtime import failed during stop user=%s: %s", telegram_id, exc, exc_info=True)
        return False, str(exc)
    if strategy not in engine_runtime.ENGINE_MAPPED_STRATEGIES:
        return True, None

    return _run_engine_stop_sync(
        lambda: engine_runtime.RUNTIME.stop(telegram_id, network, strategy),
        user_id=telegram_id,
        network=network,
        strategy=strategy,
    )


def dgrid_flip_reason(event: dict) -> str:
    """Human reason for a Dynamic Grid phase flip.

    Must describe the DECISION, not the raw sign of drift. ``direction`` is only
    ``sign(drift)``: a -0.001% wobble reads "down" even when the classifier ruled
    the market RANGING and correctly returned to the neutral long grid. Reading it
    as a verdict is what produced the reported contradiction —

        "switched RGRID → GRID — downtrend detected (variance ratio 0.83) …
         now quoting the LONG ladder"

    — a wrong message about a right decision. Only call it a trend when the
    classifier actually declared one (``trending``).
    """
    if str(event.get("reason") or "") == "reversal":
        return "reversal — locked profit, flipping"
    direction = str(event.get("direction") or "")
    if event.get("trending"):
        if direction == "down":
            return "downtrend detected"
        if direction == "up":
            return "uptrend detected"
    if str(event.get("to") or "").lower() == "grid":
        return "trend stalled — back to range mode"
    return "regime change"


async def _notify(telegram_id: int, text: str, **fmt_kwargs):
    if not _bot_app:
        return
    try:
        from src.nadobro.i18n import language_context, get_user_language, localize_text, get_active_language
        with language_context(get_user_language(telegram_id)):
            lang = get_active_language()
            translated = localize_text(text, lang)
            if fmt_kwargs:
                translated = translated.format(**fmt_kwargs)
            await _bot_app.bot.send_message(chat_id=telegram_id, text=translated)
    except Exception as e:
        logger.warning("Notify failed for %s: %s", telegram_id, e)


def _strategy_defaults(strategy: str) -> dict:
    default_bro_products = get_perp_products()[:6] or ["BTC", "ETH", "SOL"]
    return runtime_strategy_default(strategy, default_bro_products)


def _mark_previous_sessions_superseded(telegram_id: int, network: str) -> None:
    """Stop duplicate 'running' DB rows when user starts a new strategy on the same network."""
    try:
        rows = get_running_strategy_sessions(telegram_id, network)
        for row in rows:
            sid = row.get("id")
            if not sid:
                continue
            update_strategy_session(int(sid), {
                "status": "stopped",
                "stopped_at": datetime.now(timezone.utc).isoformat(),
                "stop_reason": "superseded_by_new_strategy",
            })
    except Exception as e:
        logger.warning("Could not supersede old sessions for user %s: %s", telegram_id, e)


def _schedule_bro_migration_notice(telegram_id: int) -> None:
    if not _bot_app:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = _runtime_loop
    if loop and loop.is_running():
        loop.create_task(_notify(telegram_id, BRO_MIGRATION_NOTICE))


def _migrate_legacy_bro_state(telegram_id: int, network: str, state: dict) -> dict:
    """Disable the retired Bro autoloop and request the same cleanup as Stop."""
    now = datetime.now(timezone.utc).isoformat()
    session_id = state.get("strategy_session_id")
    state["running"] = False
    state["last_action"] = "migrated_off_bro"
    state["last_action_detail"] = "Legacy Alpha Agent autoloop retired; cleanup requested."
    state["bro_migrated_at"] = state.get("bro_migrated_at") or now
    should_notify = not bool(state.get("bro_migration_notice_sent"))
    state["bro_migration_notice_sent"] = True
    _save_state(telegram_id, network, state)
    try:
        if session_id:
            update_strategy_session(
                int(session_id),
                {
                    "status": "migrated",
                    "stopped_at": now,
                    "stop_reason": "bro_autoloop_retired",
                },
            )
        else:
            rows = get_running_strategy_sessions(telegram_id, network)
            for row in rows:
                if str(row.get("strategy") or "").lower() != "bro":
                    continue
                update_strategy_session(
                    int(row["id"]),
                    {
                        "status": "migrated",
                        "stopped_at": now,
                        "stop_reason": "bro_autoloop_retired",
                    },
                )
    except Exception as e:
        logger.warning("Could not mark Bro session migrated user=%s network=%s: %s", telegram_id, network, e)
    try:
        cleanup_res = cleanup_strategy_positions(telegram_id, network, state)
    except Exception as e:
        cleanup_res = {"success": False, "error": str(e)}
    if not cleanup_res.get("success"):
        state["last_error"] = f"Legacy Alpha Agent cleanup failed: {str(cleanup_res.get('error') or 'unknown')[:220]}"
        _save_state(telegram_id, network, state)
    if should_notify:
        _schedule_bro_migration_notice(telegram_id)
    return cleanup_res


def _is_legacy_vol_perp_state(state: dict) -> bool:
    """Return True for persisted Volume sessions from before the spot-only cutover."""
    strategy = _normalize_strategy_id(str(state.get("strategy") or ""))
    if strategy != "vol":
        return False
    return str(state.get("vol_market") or "perp").strip().lower() != "spot"


async def _retire_legacy_vol_perp_state(telegram_id: int, network: str, state: dict) -> tuple[bool, str | None]:
    """Stop legacy VOL perp state before it can be interpreted as a spot cycle."""
    product = str(state.get("product") or "?").upper()
    _finalize_session(state, stop_reason="vol_perp_retired")
    state["running"] = False
    state["last_action"] = "vol_perp_retired"
    state["last_action_detail"] = "Volume Perp was retired; legacy session stopped before spot migration."
    state["last_error"] = "Volume Perp was retired; legacy session stopped and perp cleanup requested."
    await _save_state_async(telegram_id, network, state)

    close_res = await run_blocking(close_all_positions, telegram_id, network, strategy_session_id=int(state.get("strategy_session_id") or 0) or None)
    if close_res.get("success"):
        await _notify(
            telegram_id,
            "Volume Perp on {product}-PERP ({network}) was retired and has been stopped. "
            "Perp cleanup was requested; start Volume again to use the spot loop.",
            product=product,
            network=network,
        )
        return True, None

    error = str(close_res.get("error") or "close_all_positions failed")
    latest = await _load_state_async(telegram_id, network)
    latest["running"] = False
    latest["last_error"] = f"Legacy Volume Perp cleanup failed: {error[:220]}"
    await _save_state_async(telegram_id, network, latest)
    await _notify(
        telegram_id,
        "Volume Perp on {product}-PERP ({network}) was retired and stopped, but cleanup failed. "
        "Please check Nado for any remaining perp orders or positions. Error: {error}",
        product=product,
        network=network,
        error=error[:180],
    )
    return False, latest["last_error"]


def _capture_position_baseline(telegram_id: int, network: str, product_id) -> dict:
    """Signed size + avg entry of any position that ALREADY exists on the product
    when a run starts, so the run's PnL/SL excludes it (baseline-adjusted). The
    common case is flat -> (0, 0). Best-effort; never blocks session creation."""
    if product_id is None:
        return {}
    try:
        from src.nadobro.models.database import get_open_position_rows_for_product
        rows = get_open_position_rows_for_product(int(telegram_id), network, int(product_id)) or []
    except Exception:  # noqa: BLE001
        return {}
    net_signed = 0.0
    entry = 0.0
    dom_abs = -1.0
    for r in rows:
        try:
            size = abs(float(r.get("size") or 0))
            signed = size if str(r.get("side") or "").lower() == "long" else -size
            net_signed += signed
            if size > dom_abs:
                dom_abs = size
                entry = float(r.get("avg_entry_price") or 0)
        except (TypeError, ValueError):
            continue
    if abs(net_signed) <= 1e-12:
        return {}
    return {"baseline_size": net_signed, "baseline_entry": entry}


def _create_session(telegram_id: int, strategy: str, product: str, network: str, state: dict) -> int | None:
    """Create a strategy_sessions row and return the session_id."""
    try:
        product_id = get_product_id(product, network=network) if product != "MULTI" else None
        baseline = _capture_position_baseline(telegram_id, network, product_id)
        snapshot = {
            k: v for k, v in state.items()
            if k in (
                    "notional_usd", "cycle_notional_usd", "spread_bp", "leverage",
                    "slippage_pct", "interval_seconds", "tp_pct", "sl_pct", "levels",
                    "budget_usd", "risk_level", "max_positions", "products",
                    # Grid / MM EXECUTION MODE + live knobs, so each session
                    # records the mode (anchored grid vs classic ladder) and the
                    # spread/leverage/reset it actually ran with — not just the
                    # current per-user settings (which can change between runs).
                    "fill_anchored", "mm_leverage_override",
                    "min_spread_bp", "max_spread_bp", "grid_reset_threshold_pct",
                    # Mid Mode directional bias (its key per-session knob).
                    "directional_bias",
                    "rgrid_spread_bp", "rgrid_stop_loss_pct", "rgrid_take_profit_pct",
                    "rgrid_discretion", "rgrid_reset_threshold_pct",
                    "dgrid_trend_on_variance_ratio", "dgrid_range_on_variance_ratio",
                    "dgrid_spread_bp", "dgrid_min_spread_bp", "dgrid_max_spread_bp",
                    "dgrid_short_window_points", "dgrid_long_window_points",
                    "target_volume_usd", "funding_entry_mode", "fixed_margin_usd", "vol_direction",
                    # Delta Neutral (engine v2) settings.
                    "dn_hold_seconds", "dn_cycles", "dn_cycle_gap_seconds",
                    "dn_max_drift_pct", "dn_hedge_ratio",
                )
        }
        # Baseline-adjust the run's PnL: record any pre-existing position so the
        # session SL/PnL counts only what THIS run does (see live_session).
        snapshot.update(baseline)
        session_id = insert_strategy_session({
            "user_id": telegram_id,
            "strategy": strategy,
            "product_name": product,
            "product_id": product_id,
            "network": network,
            "config_snapshot": json.dumps(snapshot),
        })
        if session_id:
            logger.info("Created strategy session #%s for user %s (%s/%s)", session_id, telegram_id, strategy, network)
        return session_id
    except Exception as e:
        logger.warning("Failed to create strategy session for user %s: %s", telegram_id, e)
        return None


def _resolve_session_network(state: dict) -> str:
    """Resolve the network for ``state`` for session rollup.

    ``state`` is keyed externally by ``(telegram_id, network)`` and does not
    currently carry the network inline. We probe a few common fields and
    fall back to ``mainnet`` so the rollup never crashes the stop path.
    """
    for key in ("network", "network_mode", "selected_network", "active_network"):
        val = state.get(key) if isinstance(state, dict) else None
        if val:
            text = str(val).lower()
            return "testnet" if text == "testnet" else "mainnet"
    return "mainnet"


def _finalize_session(state: dict, stop_reason: str = "stopped"):
    """Mark the strategy session as completed/stopped.

    Belt + suspenders: in addition to writing the status/stop_reason, re-run
    a rollup from ``trades_<network>`` so the persisted card numbers reflect
    every tagged fill — even late-arriving venue-sync rows. The status write
    happens AFTER the rollup so a rollup error doesn't leave the row open.
    """
    session_id = state.get("strategy_session_id")
    if not session_id:
        return
    network = _resolve_session_network(state)
    try:
        rollup_session_from_trades(int(session_id), network)
    except Exception as exc:
        logger.warning("Session rollup failed #%s: %s", session_id, exc)
    # Engine strategies (grid/dgrid/dn/...) record fills via DbTradeRecorder
    # with realized_pnl left NULL (grid PnL is portfolio-level) and funding in
    # the separate venue feed. Source the venue-authoritative realized PnL +
    # funding AFTER the human-column rollup so the session card matches reality.
    # Gated to engine strategies so legacy sessions keep their rollup PnL.
    try:
        from src.nadobro.strategy.engine_runtime import ENGINE_MAPPED_STRATEGIES

        strat = _normalize_strategy_id(str(state.get("strategy") or ""))
        if strat in ENGINE_MAPPED_STRATEGIES:
            from src.nadobro.models.database import rollup_engine_session_pnl_funding

            rollup_engine_session_pnl_funding(int(session_id), network)
    except Exception as exc:
        logger.warning("Engine session PnL/funding rollup failed #%s: %s", session_id, exc)
    try:
        update_strategy_session(int(session_id), {
            "status": "completed" if stop_reason in ("tp_hit", "target_reached", "target_volume_hit") else "stopped",
            "stopped_at": datetime.now(timezone.utc).isoformat(),
            "stop_reason": str(stop_reason)[:200],
        })
    except Exception as e:
        logger.warning("Failed to finalize session #%s: %s", session_id, e)


def _available_quote_balance_for_network(client) -> float | None:
    """Return the available quote balance for ``client``.

    BUG-CC-3 fix: distinguish "definitely zero" from "we couldn't read it".
    Returning 0.0 on any exception silently masks API failures and would
    trip start-guards (e.g. "insufficient margin -> deny start") that the
    operator can't easily diagnose. We now return ``None`` on read failure
    so callers can decide whether to fail loud or fall back.
    """
    try:
        bal = client.get_balance() or {}
        balances = bal.get("balances", {}) or {}
        return float(balances.get(0, balances.get("0", 0.0)) or 0.0)
    except Exception:  # noqa: BLE001 - intentional: surface read failures
        logger.warning("get_balance failed during start guard", exc_info=True)
        return None


def _active_position_size_for_product(client, product_id: int) -> float | None:
    """Return the position size for ``product_id`` or ``None`` on read
    failure (BUG-CC-3 fix). Returning 0.0 on exception lets a stale API
    error silently say "no position" and pass start-guards that should fail
    closed.
    """
    try:
        for pos in client.get_all_positions() or []:
            if int(pos.get("product_id", -1)) != int(product_id):
                continue
            signed_amount = float(pos.get("signed_amount", 0) or 0.0)
            amount = abs(float(pos.get("amount", 0) or 0.0))
            size = abs(signed_amount) if abs(signed_amount) > 0 else amount
            if size > 1e-9:
                return size
        return 0.0
    except Exception:  # noqa: BLE001 - surface read failures
        logger.warning(
            "get_all_positions failed during start guard for product %s",
            product_id, exc_info=True,
        )
        return None


def _run_mm_start_guard(telegram_id: int, network: str, product: str, leverage: float, state: dict) -> tuple[bool, str]:
    client = get_user_readonly_client(telegram_id, network=network) or get_user_nado_client(telegram_id, network=network)
    if not client:
        return False, "Could not initialize market-maker account checks. Please retry."

    product_id = get_product_id(product, network=network, client=client)
    if product_id is None:
        return False, f"Unknown product '{product}'."

    existing_position = _active_position_size_for_product(client, product_id)
    if existing_position is None:
        # BUG-CC-3 fix: fail closed when the position read failed. Previously
        # the helper returned 0.0 on exception, which silently passed this
        # guard and let MM start on top of a possibly-open position.
        return (
            False,
            "Could not read existing positions from the venue (network/API error). "
            "Please retry in a moment.",
        )
    if existing_position > 0:
        return (
            False,
            f"Close your existing {product.upper()}-PERP position before starting the MM bot. "
            f"Detected open size {existing_position:.6f}."
        )

    try:
        open_orders = client.get_open_orders(product_id) or []
    except Exception:
        open_orders = []
    if open_orders:
        return (
            False,
            f"Cancel existing {product.upper()}-PERP open orders before starting the MM bot. "
            f"Detected {len(open_orders)} open order(s)."
        )

    margin_usd = float(state.get("notional_usd") or 0.0)
    cycle_notional_cfg = float(state.get("cycle_notional_usd") or margin_usd or 0.0)
    cycle_notional = max(cycle_notional_cfg, margin_usd)
    inventory_soft_limit = float(
        state.get("inventory_soft_limit_usd")
        or (margin_usd * 0.60)
    )
    lev = max(1.0, float(leverage or 1.0))
    required_margin = margin_usd if margin_usd > 0 else (cycle_notional / lev if cycle_notional > 0 else 0.0)
    rebalance_buffer = inventory_soft_limit / lev if inventory_soft_limit > 0 else 0.0
    safety_buffer = max(5.0, required_margin * 0.20)
    recommended_available = required_margin + rebalance_buffer + safety_buffer
    available_quote = _available_quote_balance_for_network(client)
    if available_quote is None:
        # BUG-CC-3 fix: fail closed if we couldn't read the wallet balance.
        return (
            False,
            "Could not read your USDC balance from the venue (network/API error). "
            "Please retry in a moment.",
        )
    if available_quote + 1e-9 < recommended_available:
        return (
            False,
            f"Insufficient margin buffer for MM on {product.upper()}-PERP. "
            f"Available ${available_quote:,.2f}, recommended about ${recommended_available:,.2f} "
            f"(trade margin ${required_margin:,.2f} + rebalance buffer ${rebalance_buffer:,.2f} + safety ${safety_buffer:,.2f}). "
            f"Reduce margin or deposit more funds."
        )

    return True, ""


def start_user_bot(
    telegram_id: int,
    strategy: str,
    product: str,
    leverage: float = 3.0,
    slippage_pct: float = 1.0,
    **kwargs,
) -> tuple[bool, str]:
    strategy = _normalize_strategy_id(strategy)
    if strategy not in SUPPORTED_STRATEGIES:
        return False, "Unknown strategy."
    try:
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
    except Exception:
        user = None
        network = "mainnet"

    if strategy == "bro":
        return False, "Legacy Alpha Agent has been retired. Pick a strategy from the Strategy hub."

    vol_market_kw = "perp"
    if strategy == "vol":
        # Volume is spot-only as of 2026-05. Ignore stale callers that still pass
        # vol_market="perp" so new sessions cannot persist the retired mode.
        vol_market_kw = "spot"
        product = normalize_volume_spot_symbol(str(product or "").strip())
        allowed_vol_spot = list_volume_spot_product_names(network=network)
        if product not in allowed_vol_spot:
            return False, (
                f"Volume spot supports only {', '.join(allowed_vol_spot) or 'no resolved spot pairs'} on {network}."
            )
        dir_spot = str(kwargs.get("direction") or "long").strip().lower()
        if dir_spot == "short":
            return False, "Volume spot mode is buy-then-sell only (direction must be long)."

    dn_pair = get_dn_pair(product, network=network) if strategy == "dn" else {}
    if strategy == "dn":
        product_id = int(dn_pair.get("perp_product_id")) if dn_pair.get("perp_product_id") is not None else None
    elif strategy == "vol" and vol_market_kw == "spot":
        product_id = get_spot_product_id(product, network=network)
    else:
        product_id = get_product_id(product, network=network)
    if product_id is None:
        return False, f"Unknown product '{product}'."
    if strategy == "dn" and not dn_pair:
        supported_products = ", ".join(get_dn_products(network=network) or ["BTC", "ETH"])
        return False, (
            "Delta Neutral currently supports assets with both Nado spot and perp markets. "
            f"Available now: {supported_products}."
        )
    if strategy == "dn" and not bool(dn_pair.get("entry_allowed", True)):
        return False, str(dn_pair.get("entry_block_reason") or f"{product.upper()} is not currently tradable for Delta Neutral.")
    if strategy == "dn":
        # Reject an undersized leg up front instead of letting the venue silently
        # bump it to the spot/perp min-notional ("$50 set but $97 executed").
        try:
            _, _dn_cfg = get_strategy_settings(telegram_id, "dn")
            leg_usd = float(_dn_cfg.get("fixed_margin_usd") or _dn_cfg.get("notional_usd") or 100.0)
            _dn_client = get_user_readonly_client(telegram_id, network=network)
            mins: list[tuple[float, str]] = []
            if _dn_client is not None:
                for _pid, _label in (
                    (dn_pair.get("spot_product_id"), "spot"),
                    (dn_pair.get("perp_product_id"), "perp"),
                ):
                    if _pid is not None:
                        _m = _dn_client.get_product_min_notional_usd(int(_pid))
                        if _m and _m > 0:
                            mins.append((float(_m), _label))
            if mins:
                need, leg_label = max(mins, key=lambda t: t[0])
                if leg_usd < need - 0.01:
                    return False, (
                        f"Size ${leg_usd:,.0f}/leg is below {product.upper()}'s {leg_label} "
                        f"minimum of ${need:,.0f}. Raise the Size to at least ${need:,.0f} so "
                        f"both legs trade at the size you set."
                    )
        except Exception:  # noqa: BLE001 - never block start on a resolver hiccup
            logger.debug("dn min-notional preflight skipped", exc_info=True)
    if strategy == "vol" and vol_market_kw == "spot":
        max_leverage = 1
        leverage = 1.0
    else:
        max_leverage = get_product_max_leverage(product, network=network)
        if strategy == "dn":
            max_leverage = min(max_leverage, 5)
        # NOTE (CEO directive 2026-05): Volume Perp now uses per-asset MAX leverage
        # rather than being pinned to 1x. The strategy itself overwrites
        # state["leverage"] at cycle start (volume_bot._resolve_max_leverage).
    # CEO directive: MM and Volume Perp coerce to MAX leverage internally — accept
    # the user's stale UI value silently rather than rejecting; the strategies
    # overwrite state["leverage"] at cycle start (mm_bot.py:~686, volume_bot.py:~884).
    if strategy in ("mm", "vol") and not (strategy == "vol" and vol_market_kw == "spot"):
        leverage = float(max_leverage)
    elif float(leverage or 0) > max_leverage:
        return False, f"Max leverage for {product.upper()} is {max_leverage}x."
    if float(leverage or 0) < 1:
        return False, "Leverage must be at least 1x."
    preflight_ok, preflight_msg = run_strategy_start_preflight(
        telegram_id, product, network, vol_market=vol_market_kw if strategy == "vol" else "perp"
    )
    if not preflight_ok:
        return False, preflight_msg

    # Volume spot places one post-only buy for the user's session margin. If
    # that one order is below the spot pair's min_notional, the venue rejects it.
    # Surface a precise actionable error instead.
    if strategy == "vol" and vol_market_kw == "spot":
        from src.nadobro.config import get_spot_metadata
        from src.nadobro.venue.product_catalog import _x18_to_float

        _, _vol_cfg = get_strategy_settings(telegram_id, "vol")
        _notional = float(
            _vol_cfg.get("session_margin_usd")
            or _vol_cfg.get("cycle_notional_usd")
            or _vol_cfg.get("notional_usd")
            or 100.0
        )
        spot_meta = get_spot_metadata(product, network=network) or {}
        min_size_x18 = spot_meta.get("min_size_x18")
        min_notional_usd = _x18_to_float(min_size_x18) if min_size_x18 else None
        # If the catalog lookup failed we don't block — we'd rather let the
        # user try than refuse on a transient catalog miss — but we log it.
        if min_notional_usd is None:
            logger.warning(
                "vol start: spot min_notional unavailable for %s on %s; "
                "skipping pre-flight check (transient catalog miss)",
                product, network,
            )
        elif _notional < float(min_notional_usd):
            return False, (
                f"Vol margin ${_notional:,.2f} is below the spot pair's "
                f"venue minimum ${float(min_notional_usd):,.2f}. "
                f"Raise margin to at least ${float(min_notional_usd):,.0f}."
            )

    _mark_previous_sessions_superseded(telegram_id, network)
    _, strat_cfg = get_strategy_settings(telegram_id, strategy)
    state = _default_state()
    state.update(_strategy_defaults(strategy))
    state.update(strat_cfg)
    state.update(
        {
            "running": True,
            "strategy": strategy,
            "strategy_id_v2": 1,
            "product": (product if (strategy == "vol" and vol_market_kw == "spot") else str(product).upper()),
            # DN short is strictly 1x (engine forces it); vol is spot 1x. Don't
            # seed a misleading 3x that the start notification/status would echo.
            "leverage": 1.0 if strategy in ("vol", "dn") else float(leverage or 3.0),
            "slippage_pct": float(slippage_pct or 1.0),
            "reference_price": 0.0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "last_run_ts": 0.0,
            "last_error": None,
            "runs": 0,
            # Reset quote-gate telemetry: a stale PAUSED verdict from the
            # previous session must not render on the new session's /status.
            "mm_gate_verdict": "",
            "mm_gate_reason": "",
            "mm_gate_since_ts": 0.0,
        }
    )
    if strategy in ("grid", "rgrid", "dgrid", "mid"):
        mm_ok, mm_msg = _run_mm_start_guard(telegram_id, network, product.upper(), float(state.get("leverage") or leverage or 1.0), state)
        if not mm_ok:
            return False, mm_msg
        # Participation/Duration (TWAP): resolve the enforced run duration AND
        # the per-cycle order chunk once at start from the participation preset
        # (or a custom mm_duration_minutes) against the pair's 24h volume.
        # Both are opt-in — absent a preset/duration they stay 0 (unchanged).
        try:
            # Match map_strategy_config's deployed basis exactly: cycle_notional
            # fallback notional, × the EFFECTIVE leverage (mm_leverage_override,
            # venue-capped) — otherwise the duration + chunk cap would be skewed.
            from src.nadobro.strategy.engine_runtime import _effective_leverage
            _notional = float(state.get("cycle_notional_usd") or state.get("notional_usd") or 0.0)
            _deployed = _notional * _effective_leverage(state, float(state.get("leverage") or 1.0))
            _vol = _best_effort_pair_24h_volume_usd(telegram_id, network, product.upper())
            _dur = _resolve_mm_run_duration_minutes(state, _deployed, _vol)
            if _dur > 0:
                state["mm_run_duration_minutes"] = _dur
                state["mm_duration_target_notified"] = False
            from src.nadobro.venue.product_catalog import get_product_min_quote_notional_usd
            from src.nadobro.quant.mm_quote_math import DEFAULT_MIN_ORDER_NOTIONAL_USD
            _min_notional = (
                get_product_min_quote_notional_usd(product.upper(), network=network)
                or DEFAULT_MIN_ORDER_NOTIONAL_USD
            )
            _chunk = _resolve_mm_cycle_notional_usd(state, _deployed, _vol, _min_notional)
            if _chunk > 0:
                state["mm_cycle_notional_usd"] = _chunk
        except Exception:  # noqa: BLE001  # policy: degrade-ok(participation is opt-in; never block start)
            logger.debug("mm participation wiring skipped user=%s", telegram_id, exc_info=True)
    if strategy == "vol":
        state["vol_market"] = vol_market_kw
        if vol_market_kw == "spot":
            state["vol_direction"] = "long"
            state["direction"] = "long"
        else:
            direction = str(kwargs.get("direction") or strat_cfg.get("vol_direction") or "long").strip().lower()
            direction = "short" if direction == "short" else "long"
            state["vol_direction"] = direction
            state["direction"] = direction
        _vol_margin = float(
            state.get("session_margin_usd")
            or state.get("cycle_notional_usd")
            or state.get("notional_usd")
            or state.get("fixed_margin_usd")
            or 100.0
        )
        # AUDIT-VOL-2026-07-31 F5: clamp to the $100-$500 band HERE, at the one
        # place the session state is written, so every downstream reader agrees:
        # map_strategy_config (order size), map_risk_limits (single-order cap)
        # and live_session._resolve_margin (the SL/TP denominator) all read
        # these keys. Clamping only in the config mapping left a stale $1000
        # config trading $500 while the stop measured against $1000 (a 2x
        # looser stop), and a stale sub-$100 config with a risk cap below its
        # own clamped order size (every buy rejected — a silent dead session).
        from src.nadobro.quant.vol_fee_estimator import clamp_margin_usd as _clamp_vol
        _vol_margin = float(_clamp_vol(_vol_margin))
        state["session_margin_usd"] = _vol_margin
        state["fixed_margin_usd"] = _vol_margin
        state["notional_usd"] = _vol_margin
        state["vol_phase"] = "idle"
        state["session_realized_pnl_usd"] = 0.0
        state["volume_done_usd"] = 0.0
        state["volume_remaining_usd"] = float(state.get("target_volume_usd") or 0.0)
        try:
            from src.nadobro.venue.product_catalog import get_spot_maker_fee_rate

            _fee_rate = get_spot_maker_fee_rate(str(product), network=network)
            if _fee_rate is not None:
                state["vol_maker_fee_rate"] = float(_fee_rate)
                state["vol_maker_fee_bp"] = float(_fee_rate) * 10000.0
        except Exception:
            logger.debug("vol maker fee resolution skipped user=%s product=%s", telegram_id, product, exc_info=True)
        # Keep saved VOL prefs aligned with this run so dashboards / Advanced match execution.
        if vol_market_kw == "perp":
            try:
                from src.nadobro.users.settings_service import update_user_settings

                d = str(state.get("vol_direction") or "long").strip().lower()
                d = "short" if d == "short" else "long"

                def _sync_vol_direction_pref(settings: dict) -> None:
                    strategies = settings.setdefault("strategies", {})
                    vol_cfg = strategies.setdefault("vol", {})
                    vol_cfg["vol_direction"] = d

                update_user_settings(telegram_id, _sync_vol_direction_pref)
            except Exception:
                logger.warning(
                    "Could not sync vol_direction into user strategy prefs for user=%s",
                    telegram_id,
                    exc_info=True,
                )
    from src.nadobro.runtime.runtime_supervisor import strategy_worker_group

    state["worker_group"] = strategy_worker_group(strategy)
    session_id = _create_session(
        telegram_id,
        strategy,
        (product if (strategy == "vol" and vol_market_kw == "spot") else str(product).upper()),
        network,
        state,
    )
    if session_id:
        state["strategy_session_id"] = session_id
    else:
        # AUDIT-MM-2026-07-14 #3: fail CLOSED. The session SL/TP rail resolves
        # the running strategy_sessions row every cycle and silently no-ops
        # without one — a session started through a DB hiccup would trade with
        # NO loss bound (the engine itself has no per-strategy PnL floor).
        # Volume/fee reporting also keys off this row. Refuse to arm.
        state["running"] = False
        state["last_error"] = "strategy session record could not be created"
        _save_state(telegram_id, network, state)
        return False, (
            "Could not create the strategy session record (database hiccup). "
            "Not starting without the SL/TP safety rail — please try again."
        )
    # Persist network on state so _finalize_session can rollup against the
    # correct trades_<network> table after stop.
    state["network"] = "testnet" if str(network).lower() == "testnet" else "mainnet"
    _save_state(telegram_id, network, state)
    _ensure_task(telegram_id, network)

    # NO_ORDERS_AUDIT-FIX-R3: eager engine kickoff. Without this, the first
    # order doesn't post until the scheduler's next tick (up to
    # interval_seconds = 60s by default). Users hit Start, see no orders for
    # 60s, and assume the bot is broken. Kick the first run_engine_cycle
    # right now so on_start side-effects (executors spawned, orders posted)
    # happen within seconds.
    #
    # Failure handling: a transient failure here (e.g., mid feed returned
    # 0) is non-fatal — the scheduler tick will retry. A *hard* error
    # (e.g., engine_v2 disabled, controller construction failed) DOES bubble
    # up and we tear down the session so the user can retry cleanly.
    try:
        from src.nadobro.strategy import engine_runtime as _er

        if _er.engine_v2_enabled() and strategy in _er.ENGINE_MAPPED_STRATEGIES:
            from src.nadobro.runtime.runtime_supervisor import (
                is_multiprocess_enabled as _is_mp,
                strategy_worker_group as _swg,
                submit_cycle_job as _submit_job,
            )
            # MULTIPROCESS NO-ORDERS FIX: scheduled cycles run in the worker
            # pool, so the controller must be BUILT there. The old in-loop
            # kickoff built it in the MAIN process; the worker then saw
            # is_running() True (cross-process via engine_executors) and
            # RUNTIME.tick() no-op'd every cycle — the strategy never placed an
            # order. Dispatch the kickoff through the SAME pool as the scheduler
            # (a follow-up cycle for spawn-on-tick strategies so they quote
            # within seconds rather than after one interval).
            _mp_routed = bool(_is_mp() and _strategy_use_multiprocess(strategy))
            if _mp_routed:
                _wg = _swg(strategy)
                _payload = {"telegram_id": telegram_id, "network": network,
                            "strategy": strategy, "worker_group": _wg}
                _n_jobs = 2 if strategy in _SPAWN_ON_TICK_STRATEGIES else 1
                _mp_ok = True
                _mp_err = None
                for _ in range(_n_jobs):
                    _mp_ok, _mp_r, _mp_err = _run_engine_start_sync(
                        lambda: _submit_job(dict(_payload)),
                        user_id=telegram_id, network=network, strategy=strategy,
                    )
                    if not _mp_ok:
                        break
                logger.info(
                    "eager engine kickoff routed to worker pool user=%s strategy=%s "
                    "group=%s jobs=%s ok=%s err=%s",
                    telegram_id, strategy, _wg, _n_jobs, _mp_ok, _mp_err,
                )
            user_client = None if _mp_routed else get_user_nado_client(telegram_id, network)
            if _mp_routed:
                pass  # controller built + ticked in the worker pool above
            elif user_client is None:
                logger.warning(
                    "eager engine start: no Nado client for user=%s — skipping kickoff "
                    "(scheduler will retry on next tick)",
                    telegram_id,
                )
            else:
                # Resolve the mid + product_id the same way _run_cycle does.
                try:
                    mp = user_client.get_market_price(int(product_id)) or {}
                    mid_val = float(mp.get("mid", 0) or 0)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "eager engine start: get_market_price failed user=%s product_id=%s; "
                        "scheduler will retry", telegram_id, product_id, exc_info=True,
                    )
                    mid_val = 0.0
                # mid==0 is acceptable here for non-grid strategies; map_strategy_config
                # caps the grid band to a minimum, and mm/vol get a fresh mid on tick.
                ok, _result, err = _run_engine_start_sync(
                    lambda: _er.run_engine_cycle(
                        telegram_id, network, state, user_client, mid_val,
                        str(state.get("product") or product), int(product_id),
                    ),
                    user_id=telegram_id, network=network, strategy=strategy,
                )
                if ok:
                    logger.info(
                        "eager engine kickoff OK user=%s strategy=%s on %s mid=%s",
                        telegram_id, strategy, network, mid_val,
                    )
                    # NO_ORDERS-FIX: dgrid/mid spawn their executors on the
                    # first *tick*, not in on_start — so the eager kickoff above
                    # only STARTS them and the grid wouldn't post until the
                    # scheduler's next tick (up to interval_seconds later). Fire
                    # one immediate follow-up cycle (now is_running() -> tick)
                    # so these strategies place orders within seconds like the
                    # spawn-on-start ones. Best-effort: a throttle/empty-candle
                    # tick is a no-op the scheduler retries.
                    if strategy in _SPAWN_ON_TICK_STRATEGIES:
                        ok2, _r2, err2 = _run_engine_start_sync(
                            lambda: _er.run_engine_cycle(
                                telegram_id, network, state, user_client, mid_val,
                                str(state.get("product") or product), int(product_id),
                            ),
                            user_id=telegram_id, network=network, strategy=strategy,
                        )
                        logger.info(
                            "eager engine first-tick user=%s strategy=%s ok=%s err=%s",
                            telegram_id, strategy, ok2, err2,
                        )
                else:
                    logger.critical(
                        "eager engine kickoff FAILED user=%s strategy=%s on %s err=%s",
                        telegram_id, strategy, network, err,
                    )
                    # Tear down: stop any partial engine state, mark session
                    # stopped, clear running flag so the scheduler doesn't
                    # keep retrying a broken config.
                    try:
                        _stop_engine_runtime_for_state(telegram_id, network, state)
                    except Exception:
                        logger.warning("eager start rollback: engine stop failed", exc_info=True)
                    _finalize_session(state, stop_reason=f"eager_start_failed: {err}")
                    state["running"] = False
                    state["last_error"] = f"Engine start failed: {err}"
                    _save_state(telegram_id, network, state)
                    return False, f"Engine failed to start: {err}"
    except Exception:
        # The eager kickoff is best-effort; don't block Start on it.
        logger.warning("eager engine kickoff threw — scheduler will retry", exc_info=True)

    if strategy == "dn":
        leg_size = float(state.get("fixed_margin_usd") or state.get("notional_usd") or 100.0)
        hold_s = int(state.get("dn_hold_seconds") or 3600)
        hold_lbl = (
            f"{hold_s // 3600}h" if hold_s % 3600 == 0
            else (f"{hold_s // 60}m" if hold_s % 60 == 0 else f"{hold_s}s")
        )
        cycles = int(state.get("dn_cycles") or 1)
        return (
            True,
            f"DN bot started: {str(dn_pair.get('spot_symbol') or product.upper())} spot long + "
            f"{str(dn_pair.get('perp_symbol') or f'{product.upper()}-PERP')} short 1x ({network}) "
            f"| Size ${leg_size:,.0f}/leg | Hold {hold_lbl} | Cycles {cycles}",
        )
    if strategy == "vol":
        # Volume is spot-only as of 2026-05.
        session_margin = float(
            state.get("session_margin_usd") or state.get("fixed_margin_usd") or 100.0
        )
        target_volume = float(state.get("target_volume_usd") or 0.0)
        return (
            True,
            f"VOL spot bot started on {str(product).upper()} spot ({network}) "
            f"| Post-only buy/sell loop | Margin ${session_margin:,.0f} @ 1x "
            f"| Target volume ${target_volume:,.0f} | SL {state.get('sl_pct')}%",
        )
    if strategy in ("grid", "rgrid", "dgrid", "mid"):
        spread_key = {"rgrid": "rgrid_spread_bp", "dgrid": "dgrid_spread_bp"}.get(strategy, "spread_bp")
        margin_usd = float(state.get("notional_usd") or 0.0)
        cycle_notional_cfg = float(state.get("cycle_notional_usd") or margin_usd or 0.0)
        cycle_notional = max(cycle_notional_cfg, margin_usd)
        spread_bp = float(state.get(spread_key) or state.get("spread_bp") or 0.0)
        return (
            True,
            f"{_strategy_display_name(strategy)} bot started on {product.upper()}-PERP ({network}) "
            f"| Maker-only quotes | Margin ${margin_usd:,.0f} | Notional ${cycle_notional:,.0f} / cycle | Spread {spread_bp:.0f}bp",
        )
    return (
        True,
        f"{_strategy_display_name(strategy)} bot started on {product.upper()}-PERP ({network}) "
        f"| TP {state.get('tp_pct')}% / SL {state.get('sl_pct')}%",
    )


def set_process_worker_mode(enabled: bool) -> None:
    global _process_worker_mode
    _process_worker_mode = bool(enabled)


def is_process_worker_mode() -> bool:
    """True inside a strategy-cycle worker process (set by run_cycle_job_sync).
    The engine build gate uses this to let the cycle-running worker ADOPT
    (build) its own controller when it has none locally — even if another
    (dead) process left a non-terminated executor row — without the main
    fallback double-building."""
    return _process_worker_mode


def run_cycle_job_sync(payload: dict) -> dict:
    telegram_id = int(payload.get("telegram_id"))
    network = str(payload.get("network"))
    started = time.perf_counter()
    set_process_worker_mode(True)
    try:
        state = _load_state(telegram_id, network)
        if not state.get("running"):
            return {
                "ok": True,
                "skipped": "not_running",
                "worker_pid": os.getpid(),
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                "completed_at": time.time(),
            }
        ok, error_msg = asyncio.run(
            _run_cycle(telegram_id, network, state, nudge=bool(payload.get("nudge")))
        )
        return {
            "ok": bool(ok),
            "error": error_msg,
            "worker_pid": os.getpid(),
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            "completed_at": time.time(),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "worker_pid": os.getpid(),
            "elapsed_ms": (time.perf_counter() - started) * 1000.0,
            "completed_at": time.time(),
        }
    finally:
        set_process_worker_mode(False)


def stop_user_bot(telegram_id: int, cancel_orders: bool = True) -> tuple[bool, str]:
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    state = _load_state(telegram_id, network)
    if not state.get("running"):
        return False, "No running strategy bot found."

    _finalize_session(state, stop_reason="user_stop")
    state["running"] = False
    _save_state(telegram_id, network, state)

    tk = _task_key(telegram_id, network)
    task = _tasks.pop(tk, None)
    if task:
        task.cancel()
    try:
        from src.nadobro.core.feature_flags import strategy_scheduler_enabled
        from src.nadobro.strategy.strategy_scheduler import get_scheduler

        if strategy_scheduler_enabled():
            get_scheduler().unregister(telegram_id, network)
    except Exception as e:
        logger.warning(
            "scheduler unregister failed for user %s on %s — stopped session may keep ticking: %s",
            telegram_id, network, e,
        )

    engine_ok, engine_error = _stop_engine_runtime_for_state(telegram_id, network, state)

    if cancel_orders:
        close_res = cleanup_strategy_positions(telegram_id, network, state)
        if not close_res.get("success"):
            return False, f"Strategy loop stopped, but cleanup failed: {close_res.get('error', 'unknown')}"
    if not engine_ok:
        return False, f"Strategy loop stopped, but engine cleanup failed: {engine_error or 'unknown'}"

    summary = _session_fee_truth_summary(state)
    base_msg = "Strategy bot stopped. Open orders cancellation requested."
    return True, f"{base_msg}\n{summary}" if summary else base_msg


def _session_fee_truth_summary(state: dict) -> str:
    """One-line gross/fees/funding -> net decomposition for the ended session.

    The nine-run grid post-mortem lesson: judge by net-of-fees PnL, never by
    volume — volume is a cost. Surfacing the split at every stop makes fee
    drag impossible to miss.
    """
    session_id = state.get("strategy_session_id")
    if not session_id:
        return ""
    try:
        from src.nadobro.db import query_one

        row = query_one(
            "SELECT realized_pnl, total_fees_paid, total_funding_paid, total_volume_usd "
            "FROM strategy_sessions WHERE id = %s",
            (int(session_id),),
        )
    except Exception:  # policy: degrade-ok(summary is informational)
        return ""
    if not row:
        return ""
    gross = float(row.get("realized_pnl") or 0.0)
    fees = abs(float(row.get("total_fees_paid") or 0.0))
    funding = float(row.get("total_funding_paid") or 0.0)
    volume = float(row.get("total_volume_usd") or 0.0)
    net = gross - fees - funding
    funding_part = f" − funding ${funding:,.2f}" if funding > 0 else (
        f" + funding ${abs(funding):,.2f}" if funding < 0 else ""
    )
    return (
        f"Session result: gross {'+' if gross >= 0 else '-'}${abs(gross):,.2f}"
        f" − fees ${fees:,.2f}{funding_part}"
        f" = net {'+' if net >= 0 else '-'}${abs(net):,.2f}\n"
        f"Volume ${volume:,.0f} (volume is a cost — fees scale with it)"
    )


def stop_all_user_bots(telegram_id: int, cancel_orders: bool = True) -> tuple[bool, str]:
    stopped = 0
    close_errors: list[str] = []
    stop_errors: list[str] = []
    rows = query_all(
        "SELECT key, value FROM bot_state WHERE key LIKE %s",
        (f"{STATE_PREFIX}{telegram_id}:%",),
    )
    for row in rows:
        try:
            key = row.get("key", "")
            user_network = key.replace(STATE_PREFIX, "")
            user_id_str, network = user_network.split(":", 1)
            if int(user_id_str) != int(telegram_id):
                continue
            state = json.loads(row.get("value") or "{}")
            _migrate_state_strategy(state)
            if not state.get("running"):
                continue
            _finalize_session(state, stop_reason="user_stop_all")
            state["running"] = False
            set_bot_state(key, state)
            tk = _task_key(telegram_id, network)
            task = _tasks.pop(tk, None)
            if task:
                task.cancel()
            engine_ok, engine_error = _stop_engine_runtime_for_state(telegram_id, network, state)
            if not engine_ok:
                close_errors.append(f"{network}: engine cleanup failed: {engine_error or 'unknown'}")
            if cancel_orders:
                close_res = cleanup_strategy_positions(telegram_id, network, state)
                if not close_res.get("success"):
                    close_errors.append(f"{network}: {close_res.get('error', 'close_all_positions failed')}")
            stopped += 1
        except Exception as exc:
            key = str(row.get("key", ""))
            logger.warning("stop_all_user_bots failed for key=%s user=%s: %s", key, telegram_id, exc, exc_info=True)
            stop_errors.append(f"{key or 'unknown'}: {exc}")
            continue
    if stopped > 0:
        all_errors = [*close_errors, *stop_errors]
        cleanup_note = ""
        if cancel_orders and not close_errors and not stop_errors:
            cleanup_note = (
                " Open resting orders were cancelled and positions closed on Nado per stopped strategy "
                "(same behavior as single-bot stop)."
            )
        if all_errors:
            return True, (
                f"Stopped {stopped} running strategy loop(s). Some stop/cleanup actions failed: "
                f"{'; '.join(all_errors)}{cleanup_note}"
            )
        return True, f"Stopped {stopped} running strategy loop(s).{cleanup_note}"
    if stop_errors:
        return False, f"No strategy bot was fully stopped. Errors: {'; '.join(stop_errors)}"
    return False, "No running strategy bot found."


def stop_all_automation_for_user(telegram_id: int) -> tuple[bool, str]:
    """Stop strategy loops (with Nado cleanup) and halt active copy mirrors."""
    ok_bot, msg_bot = stop_all_user_bots(telegram_id, cancel_orders=True)
    from src.nadobro.trading.copy_service import stop_all_copies

    ok_copy, msg_copy = stop_all_copies(telegram_id)
    parts = [msg_bot]
    no_active_copies = not ok_copy and msg_copy.strip() == "No active copy mirrors."
    copy_stop_failed = not ok_copy and not no_active_copies
    if ok_copy or copy_stop_failed:
        parts.append(msg_copy)
    combined = " ".join(parts)
    overall_ok = bool(ok_bot or ok_copy)
    if copy_stop_failed:
        # A strategy loop may have stopped successfully, but /stop_all is not
        # complete while a copy mirror is still retrying failed closes.
        overall_ok = False
    return overall_ok, combined


def get_user_bot_state(telegram_id: int, network: str | None = None) -> dict:
    """Public accessor for the persisted strategy state dict.

    Used by the Phase 3 /mm_status and /mm_fills handlers so they can read
    long-lived counters (mm_session_notional_done_usd, grid_*_fills, etc.)
    without going through the full status enrichment pipeline.
    """
    if not network:
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
    return _load_state(telegram_id, network)


def get_user_bot_status(telegram_id: int) -> dict:
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    state = _load_state(telegram_id, network)
    global_pause_active = bool(is_trading_paused())
    last_run = _safe_last_run_ts(state.get("last_run_ts"))
    # Option 1: show the countdown against the effective cadence (fast for
    # rgrid/mid) so the status card matches when the next cycle actually runs.
    interval = effective_interval_seconds(
        str(state.get("strategy") or "").lower().strip(),
        int(state.get("interval_seconds") or 60),
    )
    next_cycle_in = max(0, int(interval - (time.time() - last_run))) if last_run > 0 else 0
    other_running_networks: list[str] = []
    try:
        rows = query_all(
            "SELECT key, value FROM bot_state WHERE key LIKE %s",
            (f"{STATE_PREFIX}{telegram_id}:%",),
        )
        for row in rows:
            key = str(row.get("key", ""))
            key_part = key.replace(STATE_PREFIX, "")
            _, row_network = key_part.split(":", 1)
            if row_network == network:
                continue
            row_state = json.loads(row.get("value") or "{}")
            if row_state.get("running"):
                other_running_networks.append(row_network)
    except Exception:  # policy: degrade-ok(status display; cross-network list may be incomplete)
        pass

    running_sessions: list[dict] = []
    try:
        for row in get_running_strategy_sessions(telegram_id, network):
            running_sessions.append({
                "id": row.get("id"),
                "strategy": row.get("strategy"),
                "product_name": row.get("product_name"),
                "network": row.get("network"),
                "started_at": row.get("started_at"),
                "total_cycles": int(row.get("total_cycles") or 0),
            })
    except Exception:  # policy: degrade-ok(status display; session list may be incomplete)
        pass

    session_analytics = {"total_trades": 0}
    strategy_session_id = state.get("strategy_session_id")
    if strategy_session_id:
        try:
            session_analytics = get_trade_analytics(telegram_id, strategy_session_id=int(strategy_session_id))
        except Exception:
            session_analytics = {"total_trades": 0}

    # Live DN progress (cycles-completed + funding-earned) is written each tick
    # by the worker process into engine_controller_state; read it here so
    # /status reflects real progress, not just the configured cycle count.
    dn_progress: dict = {}
    engine_order_counts: dict = {}
    _strat_lc = str(state.get("strategy") or "").lower()
    try:
        from src.nadobro.strategy.engine_runtime import ENGINE_MAPPED_STRATEGIES as _EMS
    except Exception:  # noqa: BLE001
        _EMS = ()
    if _strat_lc in _EMS:
        try:
            from src.nadobro.strategy.engine_runtime import deterministic_controller_id
            from src.nadobro.trading.engine_persistence import (
                get_controller_progress, count_engine_orders,
                resolve_running_session_id,
            )

            _cid = deterministic_controller_id(_strat_lc, telegram_id, network)
            # Real order counts from engine_executors — the engine cycle result
            # carries none, so the legacy order_observability stays 0 (Bug 3).
            # Scope to THIS run's session so prior runs of the same strategy
            # don't inflate the count (controller_id is stable across runs).
            _run_session_id = resolve_running_session_id(_strat_lc, telegram_id, network)
            engine_order_counts = count_engine_orders(_cid, _run_session_id)
            if _strat_lc == "dn":
                dn_progress = get_controller_progress(_cid) or {}
        except Exception:  # policy: degrade-ok(status display)
            dn_progress = {}
            engine_order_counts = {}

    try:
        from src.nadobro.strategy.strategy_fsm import infer_phase

        strategy_phase = infer_phase(state).to_dict()
    except Exception:
        strategy_phase = {"phase": "unknown", "detail": "", "recoverable": True, "allowed_actions": []}

    # Prefer the real per-order counts the worker accumulates into
    # order_observability (placed/filled/cancelled from the executors). Fall
    # back to the all-time engine_executors row count only when the worker
    # hasn't populated state yet (e.g. just after a restart).
    _state_obs = dict(state.get("order_observability") or {})
    if int(_state_obs.get("orders_placed") or 0) > 0:
        _order_obs = _state_obs
    else:
        _order_obs = {**_state_obs, **engine_order_counts}

    return {
        "network": network,
        "running": bool(state.get("running")),
        "global_pause_active": global_pause_active,
        "strategy": state.get("strategy"),
        "product": state.get("product"),
        "vol_market": state.get("vol_market") or ("spot" if str(state.get("strategy") or "").lower() == "vol" else "perp"),
        "notional_usd": state.get("notional_usd"),
        "cycle_notional_usd": state.get("cycle_notional_usd"),
        "spread_bp": state.get("spread_bp"),
        "tp_pct": state.get("tp_pct"),
        "sl_pct": state.get("sl_pct"),
        "interval_seconds": interval,
        "started_at": state.get("started_at"),
        "runs": state.get("runs", 0),
        "last_error": state.get("last_error"),
        "last_error_category": state.get("last_error_category") or "",
        "last_action": state.get("last_action"),
        "last_action_detail": state.get("last_action_detail"),
        "strategy_phase": strategy_phase,
        "last_run_ts": last_run,
        "next_cycle_in": next_cycle_in,
        "error_streak": int(state.get("error_streak") or 0),
        "pause_reason": state.get("mm_pause_reason") or "",
        "is_paused": bool(state.get("mm_paused")),
        # Quote-gate visibility (audit 2026-07-31, MID-GATE-STATUS-DEAD): the
        # worker persists these each engine cycle; without them here the
        # /status "Quoting: PAUSED (…)" line can never render.
        "mm_gate_verdict": str(state.get("mm_gate_verdict") or ""),
        "mm_gate_reason": str(state.get("mm_gate_reason") or ""),
        "mm_gate_since_ts": float(state.get("mm_gate_since_ts") or 0.0),
        "bro_state": state.get("bro_state"),
        "maker_fill_ratio": (state.get("mm_last_metrics") or {}).get("maker_fill_ratio"),
        "cancellation_ratio": (state.get("mm_last_metrics") or {}).get("cancellation_ratio"),
        "avg_quote_distance_bp": (state.get("mm_last_metrics") or {}).get("avg_quote_distance_bp"),
        "quote_refresh_rate": (state.get("mm_last_metrics") or {}).get("quote_refresh_rate"),
        "inventory_skew_usd": (state.get("mm_last_metrics") or {}).get("inventory_skew_usd"),
        "inventory_source": (state.get("mm_last_metrics") or {}).get("inventory_source") or state.get("mm_last_inventory_source"),
        "session_notional_done_usd": (state.get("mm_last_metrics") or {}).get("session_notional_done_usd"),
        "worker_group": state.get("worker_group"),
        "worker_last_heartbeat": float(state.get("worker_last_heartbeat") or 0.0),
        "last_dispatch_ts": float(state.get("last_dispatch_ts") or 0.0),
        "last_cycle_ms": float(state.get("last_cycle_ms") or 0.0),
        "last_cycle_result": state.get("last_cycle_result") or "",
        "worker_pid": state.get("worker_pid"),
        "dn_last_funding_rate": state.get("dn_last_funding_rate"),
        "dn_unfavorable_count": state.get("dn_unfavorable_count"),
        "dn_mode": state.get("dn_mode") or state.get("funding_entry_mode"),
        # Engine-v2 DN settings (what the controller actually runs) so /status
        # matches the dashboard. Size = per-leg notional from fixed_margin_usd.
        "dn_size_usd": (
            state.get("fixed_margin_usd")
            if state.get("fixed_margin_usd") is not None
            else state.get("notional_usd")
        ),
        "dn_hold_seconds": state.get("dn_hold_seconds"),
        "dn_cycles": state.get("dn_cycles"),
        "dn_cycles_completed": dn_progress.get("cycles_completed"),
        "dn_funding_earned_usd": dn_progress.get("funding_earned_usd"),
        # Reverse GRID telemetry surface for runtime status.
        "rgrid_anchor_price": state.get("grid_anchor_price"),
        "rgrid_buy_exposure_price": state.get("grid_buy_exposure_price"),
        "rgrid_sell_exposure_price": state.get("grid_sell_exposure_price"),
        "rgrid_drift_from_anchor_pct": state.get("grid_drift_from_anchor_pct"),
        "rgrid_reset_active": bool(state.get("grid_reset_active")),
        "rgrid_reset_side": state.get("grid_reset_side"),
        "rgrid_last_cycle_pnl_usd": state.get("grid_last_cycle_pnl_usd"),
        # fill-anchored grid soft-reset levels (green = over-short rise trigger,
        # red = over-long fall trigger) for the order-monitor card.
        "grid_reset_up_price": float(state.get("grid_reset_up_price") or 0.0),
        "grid_reset_down_price": float(state.get("grid_reset_down_price") or 0.0),
        "grid_soft_reset_engaged": bool(state.get("grid_soft_reset_engaged")),
        "grid_net_base": float(state.get("grid_net_base") or 0.0),
        "rgrid_stop_loss_pct": state.get("rgrid_stop_loss_pct") or state.get("grid_stop_loss_pct") or state.get("sl_pct"),
        "rgrid_take_profit_pct": state.get("rgrid_take_profit_pct") or state.get("grid_take_profit_pct") or state.get("tp_pct"),
        "rgrid_reset_threshold_pct": state.get("rgrid_reset_threshold_pct") or state.get("grid_reset_threshold_pct"),
        "rgrid_reset_timeout_seconds": state.get("rgrid_reset_timeout_seconds") or state.get("grid_reset_timeout_seconds"),
        "rgrid_discretion": state.get("rgrid_discretion") or state.get("grid_discretion"),
        "dgrid_phase": state.get("dgrid_phase") or "",
        "dgrid_variance_ratio": float(state.get("dgrid_variance_ratio") or 0.0),
        "dgrid_realized_move_bp": float(state.get("dgrid_realized_move_bp") or 0.0),
        "dgrid_dynamic_spread_bp": float(state.get("dgrid_dynamic_spread_bp") or 0.0),
        "dgrid_reset_threshold_bp": float(state.get("dgrid_reset_threshold_bp") or 0.0),
        "dgrid_phase_changed": bool(state.get("dgrid_phase_changed")),
        "other_running_networks": other_running_networks,
        "strategy_session_id": strategy_session_id,
        "running_sessions": running_sessions,
        "order_observability": _order_obs,
        "session_trade_count": int(session_analytics.get("total_trades") or 0),
        "session_filled_trades": int(session_analytics.get("filled") or 0),
        "session_closed_trades": int(session_analytics.get("closed") or 0),
        "session_failed_trades": int(session_analytics.get("failed") or 0),
        "session_volume_usd": float(session_analytics.get("total_volume") or 0.0),
        "session_fees_usd": float(session_analytics.get("total_fees") or 0.0),
        "session_funding_usd": float(session_analytics.get("total_funding") or 0.0),
        "session_analytics_pnl_usd": float(session_analytics.get("total_pnl") or 0.0),
        "vol_order_attempts": int(state.get("vol_order_attempts") or 0),
        "vol_order_failures": int(state.get("vol_order_failures") or 0),
        "last_order_error": state.get("last_order_error") or "",
        "last_order_ts": float(state.get("last_order_ts") or 0.0),
        "target_volume_usd": float(state.get("target_volume_usd") or 0.0),
        "volume_done_usd": float(state.get("volume_done_usd") or 0.0),
        "volume_remaining_usd": float(state.get("volume_remaining_usd") or 0.0),
        "session_realized_pnl_usd": float(state.get("session_realized_pnl_usd") or 0.0),
        "vol_phase": state.get("vol_phase") or "",
        "vol_last_order_digest": state.get("vol_last_order_digest") or "",
        "vol_last_order_kind": state.get("vol_last_order_kind") or "",
        "vol_entry_digest": state.get("vol_entry_digest") or "",
        "vol_close_digest": state.get("vol_close_digest") or "",
        "vol_effective_margin_usd": float(state.get("vol_effective_margin_usd") or 0.0),
        "vol_closed_cycles": int(state.get("vol_closed_cycles") or 0),
        "vol_winning_cycles": int(state.get("vol_winning_cycles") or 0),
        "vol_losing_cycles": int(state.get("vol_losing_cycles") or 0),
        "vol_win_rate": float(state.get("vol_win_rate") or 0.0),
        "vol_avg_cycle_pnl_usd": float(state.get("vol_avg_cycle_pnl_usd") or 0.0),
        "runtime_diagnostics": get_runtime_diagnostics(),
    }


def get_runtime_diagnostics() -> dict:
    from src.nadobro.trading.execution_queue import get_queue_diagnostics
    from src.nadobro.runtime.runtime_supervisor import runtime_mode

    active_loops = len([t for t in _tasks.values() if not t.done()])
    pending_keys = len(_job_pending_payloads)
    pending_coalesced_ticks = sum(int(v or 0) for v in _job_coalesce_counts.values())
    return {
        "active_strategy_loops": active_loops,
        "tracked_job_locks": len(_job_locks),
        "pending_keys": pending_keys,
        "pending_coalesced_ticks": pending_coalesced_ticks,
        "queue": get_queue_diagnostics(),
        "stats": dict(_job_stats),
        "env": {
            "NADO_RUNTIME_MODE": runtime_mode(),
            "NADO_STRATEGY_WORKERS": (os.environ.get("NADO_STRATEGY_WORKERS") or "2").strip(),
            "NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS": (os.environ.get("NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS") or "180").strip(),
        },
    }


def stop_all_strategies_for_user(telegram_id: int) -> None:
    """Stop all running strategies for a given user. Used on network switch."""
    rows = query_all(
        "SELECT key, value FROM bot_state WHERE key LIKE %s",
        (f"{STATE_PREFIX}{telegram_id}:%",),
    )
    stopped = []
    for row in rows:
        try:
            key = row.get("key", "")
            user_network = key.replace(STATE_PREFIX, "")
            user_id_str, network = user_network.split(":", 1)
            if int(user_id_str) != int(telegram_id):
                continue
            state = json.loads(row.get("value") or "{}")
            if not state.get("running"):
                continue
            strategy = state.get("strategy", "unknown")
            _finalize_session(state, stop_reason="network_switch")
            state["running"] = False
            state["last_error"] = "Stopped due to network switch"
            set_bot_state(key, state)
            tk = _task_key(telegram_id, network)
            task = _tasks.pop(tk, None)
            if task:
                task.cancel()
            engine_ok, engine_error = _stop_engine_runtime_for_state(telegram_id, network, state)
            if not engine_ok:
                logger.warning(
                    "Network switch engine cleanup failed for user=%s network=%s strategy=%s: %s",
                    telegram_id,
                    network,
                    strategy,
                    engine_error or "unknown",
                )
                state["last_error"] = (
                    "Stopped due to network switch; engine cleanup failed: "
                    f"{str(engine_error or 'unknown')[:180]}"
                )
                set_bot_state(key, state)
            close_res = cleanup_strategy_positions(telegram_id, network, state)
            if not close_res.get("success"):
                logger.warning(
                    "Network switch cleanup failed for user=%s network=%s strategy=%s: %s",
                    telegram_id,
                    network,
                    strategy,
                    close_res.get("error", "unknown"),
                )
                state["last_error"] = (
                    "Stopped due to network switch; cleanup failed: "
                    f"{str(close_res.get('error') or 'unknown')[:180]}"
                )
                set_bot_state(key, state)
            stopped.append(f"{strategy}@{network}")
        except Exception as e:
            logger.warning("Error stopping strategy for user %s: %s", telegram_id, e)
            continue
    if stopped:
        logger.info(
            "stop_all_strategies_for_user: stopped %d strategy/strategies for user %s: %s",
            len(stopped), telegram_id, ", ".join(stopped),
        )
    else:
        logger.debug("stop_all_strategies_for_user: no running strategies found for user %s", telegram_id)


def stop_runtime():
    for task_id, task in list(_tasks.items()):
        task.cancel()
        _tasks.pop(task_id, None)


def restore_running_bots(enabled: bool = False):
    if not enabled:
        logger.info("Skipping strategy auto-restore on startup (disabled).")
        return
    rows = query_all(
        "SELECT key, value FROM bot_state WHERE key LIKE %s",
        (f"{STATE_PREFIX}%",),
    )
    for row in rows:
        try:
            key = row.get("key", "")
            user_network = key.replace(STATE_PREFIX, "")
            user_id_str, network = user_network.split(":", 1)
            user_id = int(user_id_str)
            state = json.loads(row.get("value") or "{}")
            if state.get("running"):
                strategy = _normalize_strategy_id(state.get("strategy"))
                if strategy == "bro":
                    _migrate_legacy_bro_state(user_id, network, state)
                    logger.info(
                        "Retired legacy Bro autoloop session user=%s network=%s",
                        user_id,
                        network,
                    )
                    continue
                _ensure_task(user_id, network)
        except Exception as e:
            logger.warning(
                "restore_running_bots: skipping state row %r — a running strategy "
                "may not have been restored after restart: %s",
                row.get("key") if isinstance(row, dict) else row, e,
            )
            continue


def _schedule_task_on_loop(telegram_id: int, network: str):
    tk = _task_key(telegram_id, network)
    task = _tasks.get(tk)
    if task and not task.done():
        return
    _tasks[tk] = asyncio.create_task(_bot_loop(telegram_id, network))


def _ensure_task(telegram_id: int, network: str):
    from src.nadobro.core.feature_flags import strategy_scheduler_enabled

    if strategy_scheduler_enabled():
        from src.nadobro.strategy.strategy_scheduler import get_scheduler

        get_scheduler().register(telegram_id, network)
        return

    global _runtime_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        _runtime_loop = loop
        _schedule_task_on_loop(telegram_id, network)
        return

    if _runtime_loop and _runtime_loop.is_running():
        _runtime_loop.call_soon_threadsafe(_schedule_task_on_loop, telegram_id, network)
        return

    logger.error(
        "Could not start strategy loop for user %s on %s: no running event loop",
        telegram_id,
        network,
    )


async def _bot_loop(telegram_id: int, network: str):
    logger.info("Starting strategy loop for user %s on %s", telegram_id, network)
    await _notify(telegram_id, "Strategy loop started on {network}.", network=network)
    try:
        while True:
            state = await _load_state_async(telegram_id, network)
            if not state.get("running"):
                break
            # Option 1: same effective cadence as the central scheduler, so the
            # legacy per-user loop (scheduler disabled) also runs rgrid/mid fast.
            _strat = str(state.get("strategy") or "").lower().strip()
            interval = effective_interval_seconds(
                _strat, max(1, int(state.get("interval_seconds") or 60))
            )
            last_run = _safe_last_run_ts(state.get("last_run_ts"))
            now = time.time()
            # Avoid flooding the global strategy queue: only tick when the strategy interval has elapsed.
            if last_run > 0 and (now - last_run) < interval:
                wait_for = interval - (now - last_run)
                sleep_s = min(float(RUNTIME_TICK_SECONDS), max(0.5, wait_for))
                await asyncio.sleep(sleep_s)
                continue
            try:
                strategy = str(state.get("strategy") or "").lower().strip()
                lock = _job_locks.get(_task_key(telegram_id, network))
                if strategy == "vol" and lock and lock.locked():
                    _job_stats["vol_overlap_skips"] += 1
                    logger.info(
                        "Vol tick skipped while prior cycle still running user=%s network=%s",
                        telegram_id,
                        network,
                    )
                    await asyncio.sleep(min(float(RUNTIME_TICK_SECONDS), max(1.0, interval / 2.0)))
                    continue
                # Dedupe at the strategy's effective cadence. A fixed 20s
                # bucket silently discarded the supported 5s Turbo ticks when
                # the central scheduler was disabled.
                now_bucket = _legacy_strategy_dedupe_bucket(time.time(), interval)
                enqueued = await enqueue_strategy(
                    {"telegram_id": telegram_id, "network": network, "strategy": strategy},
                    dedupe_key=f"{telegram_id}:{network}:{now_bucket}",
                )
                if not enqueued:
                    logger.debug(
                        "Strategy tick deduped user=%s network=%s bucket=%s",
                        telegram_id,
                        network,
                        now_bucket,
                    )
            except Exception as cycle_error:
                logger.error("Cycle failure for user %s: %s", telegram_id, cycle_error, exc_info=True)
                state["last_error"] = str(cycle_error)
                await _save_state_async(telegram_id, network, state)
            # Poll cadence: honor a configured interval FASTER than the base
            # runtime tick for ANY strategy (Turbo Volume writes 5s grid/dgrid
            # intervals; previously only the FAST_CADENCE set got this, so a
            # fast grid interval silently polled at the 20s base tick). Slower
            # intervals keep the original base poll tick unchanged.
            await asyncio.sleep(max(0.5, min(float(RUNTIME_TICK_SECONDS), float(interval))))
    except asyncio.CancelledError:
        logger.info("Strategy loop cancelled for user %s", telegram_id)
    finally:
        _tasks.pop(_task_key(telegram_id, network), None)


def _legacy_strategy_dedupe_bucket(now: float, interval: float) -> int:
    """Stable queue-dedupe bucket for the legacy per-user scheduler path."""
    return int(float(now) / max(1.0, float(interval)))


# --- P2 fill-nudge ------------------------------------------------------------
# A venue WS ``fill`` event wakes the strategy runtime immediately, so an MM
# controller re-quotes within seconds of a fill instead of waiting out its
# tick interval. Registered as a nado_ws fill listener at boot (main.py).
# Monotonic clocks throughout (wall time jumps on NTP corrections and would
# stall or burst the debounce). Both dicts are size-bounded — user churn over
# a long-lived process must not grow them forever.
_NUDGE_MIN_SECONDS = env_float("NADO_FILL_NUDGE_MIN_SECONDS", 2.0)
# Fills also arrive for users with NO running engine strategy (manual traders,
# copy fills). Remember the negative for a window so those users don't cost a
# Postgres state read per fill-burst; a user who starts a strategy inside the
# window just misses nudges briefly (the scheduler still ticks normally).
_NUDGE_NEG_CACHE_SECONDS = 30.0
_NUDGE_DICT_MAX = 4096
_last_nudge_ts: dict[str, float] = {}
_nudge_skip_until: dict[str, float] = {}


def _nudge_prune(store: dict[str, float]) -> None:
    if len(store) > _NUDGE_DICT_MAX:
        for k in sorted(store, key=store.get)[: len(store) // 2]:
            store.pop(k, None)


def nudge_strategy_cycle(telegram_id: int, network: str) -> bool:
    """Schedule an immediate strategy cycle for this user (WS fill nudge).

    Deliberately bypasses the interval gate — a fill means the book changed
    and the controller should react NOW. Safe to fire liberally: cycles are
    serialized per user by ``_job_locks`` (overlaps coalesce, never double-run),
    and the in-memory debounce absorbs fill bursts. This function runs on
    the WS event loop, so it does no IO — all checks happen in the async
    task it schedules.
    """
    key = _task_key(telegram_id, network)
    now = time.monotonic()
    if now < _nudge_skip_until.get(key, 0.0):
        return False
    if now - _last_nudge_ts.get(key, 0.0) < _NUDGE_MIN_SECONDS:
        return False
    _last_nudge_ts[key] = now
    _nudge_prune(_last_nudge_ts)
    coro = _nudge_async(telegram_id, network, key)
    try:
        asyncio.get_running_loop().create_task(coro)
        return True
    except RuntimeError:
        if _runtime_loop and _runtime_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, _runtime_loop)
            return True
    coro.close()
    return False


async def _nudge_async(telegram_id: int, network: str, key: str) -> None:
    try:
        state = await run_blocking(_load_state, telegram_id, network)
        strategy = str(state.get("strategy") or "").lower().strip()
        from src.nadobro.strategy.engine_runtime import ENGINE_MAPPED_STRATEGIES

        if not state.get("running") or strategy not in ENGINE_MAPPED_STRATEGIES:
            _nudge_skip_until[key] = time.monotonic() + _NUDGE_NEG_CACHE_SECONDS
            _nudge_prune(_nudge_skip_until)
            return
        _nudge_skip_until.pop(key, None)
        await enqueue_strategy(
            {"telegram_id": telegram_id, "network": network, "strategy": strategy,
             "nudge": True},
            dedupe_key=f"{key}:fillnudge:{int(time.time())}",
        )
        _job_stats["fill_nudges"] = int(_job_stats.get("fill_nudges", 0)) + 1
    except Exception:  # noqa: BLE001 - a nudge is opportunistic; the scheduler is the backstop
        logger.debug("fill nudge failed user=%s network=%s", telegram_id, network, exc_info=True)


async def handle_strategy_job(payload: dict):
    kind = str((payload or {}).get("kind") or "")
    if kind.startswith("time_limit"):
        from src.nadobro.strategy.time_limit_watcher import handle_time_limit_job
        await handle_time_limit_job(payload)
        return
    if kind == "condition_order":
        # Conditional orders were owned by the retired Strategy Studio.
        # Drop the job rather than dispatching to a deleted module.
        logger.debug("Dropping legacy condition_order job: %s", payload)
        return
    telegram_id = int(payload.get("telegram_id"))
    network = str(payload.get("network"))
    try:
        from src.nadobro.core.user_circuit import is_open, last_error

        if is_open(telegram_id, network):
            logger.info(
                "Strategy cycle skipped — user circuit open user=%s network=%s err=%s",
                telegram_id,
                network,
                last_error(telegram_id, network),
            )
            return
    except Exception:  # policy: degrade-ok(circuit probe is best-effort; cycle proceeds)
        pass
    key = _task_key(telegram_id, network)
    lock = _job_locks.setdefault(key, asyncio.Lock())
    if lock.locked():
        payload_strategy = str(payload.get("strategy") or "").lower().strip()
        if payload_strategy == "vol" and key in _job_pending_payloads:
            # A fill nudge upgrades the already-pending Volume cycle even
            # though further overlap is otherwise dropped for load control.
            if payload.get("nudge"):
                _job_pending_payloads[key]["nudge"] = True
            _job_stats["vol_overlap_skips"] += 1
            logger.info(
                "Dropping extra overlapping vol tick user=%s network=%s pending_already_set",
                telegram_id,
                network,
            )
            return
        pending_payload = dict(payload)
        # Coalescing is last-payload-wins for ordinary ticks, but a fill nudge
        # must be sticky: a later scheduler tick must not erase its interval
        # bypass before the deferred cycle runs.
        if (_job_pending_payloads.get(key) or {}).get("nudge"):
            pending_payload["nudge"] = True
        _job_pending_payloads[key] = pending_payload
        _job_coalesce_counts[key] = int(_job_coalesce_counts.get(key, 0)) + 1
        _job_stats["coalesced_ticks"] += 1
        if payload_strategy == "vol":
            _job_stats["vol_coalesced_ticks"] += 1
        logger.info(
            "Coalescing overlapping strategy cycle for user %s on %s (pending=%s)",
            telegram_id,
            network,
            _job_coalesce_counts[key],
        )
        return

    async with lock:
        while True:
            cycle_started = time.perf_counter()
            _job_stats["cycles_started"] += 1
            state = await _load_state_async(telegram_id, network)
            if not state.get("running"):
                _job_pending_payloads.pop(key, None)
                _job_coalesce_counts.pop(key, None)
                return
            try:
                from src.nadobro.trading.execution_queue import get_queue_diagnostics
                from src.nadobro.runtime.runtime_supervisor import (
                    is_multiprocess_enabled,
                    strategy_worker_group,
                    submit_cycle_job,
                )

                qsz = int((get_queue_diagnostics() or {}).get("strategy_qsize") or 0)
                logger.info(
                    "Strategy cycle start user=%s network=%s strategy=%s queue_depth=%s",
                    telegram_id,
                    network,
                    state.get("strategy"),
                    qsz,
                )

                strategy = str(state.get("strategy") or "").lower().strip()
                use_multiprocess = is_multiprocess_enabled() and _strategy_use_multiprocess(strategy)
                if use_multiprocess:
                    strategy = str(state.get("strategy") or "")
                    worker_group = strategy_worker_group(strategy)
                    state["worker_group"] = worker_group
                    state["last_dispatch_ts"] = time.time()
                    await _save_state_async(telegram_id, network, state)
                    timeout_sec = _strategy_cycle_timeout_seconds() or 180.0
                    if str(strategy).lower().strip() == "vol":
                        timeout_sec = min(timeout_sec, 45.0)
                    delegated: dict = {}
                    delegated_timed_out = False
                    try:
                        delegated = await asyncio.wait_for(
                            submit_cycle_job(
                                {
                                    "telegram_id": telegram_id,
                                    "network": network,
                                    "strategy": strategy,
                                    "worker_group": worker_group,
                                    # AUDIT-MM-2026-07-14 #4: nudged cycles keep
                                    # their gate bypass in the worker too.
                                    "nudge": bool(payload.get("nudge")),
                                }
                            ),
                            timeout=timeout_sec,
                        )
                    except asyncio.TimeoutError:
                        delegated_timed_out = True
                        _job_stats["cycle_timeouts"] += 1
                        logger.error(
                            "Delegated strategy cycle timeout user=%s network=%s strategy=%s timeout=%.0fs; "
                            "not running local fallback because the worker job may still be active",
                            telegram_id,
                            network,
                            strategy,
                            timeout_sec,
                        )

                    if delegated_timed_out:
                        ok = False
                        error_msg = (
                            f"Delegated strategy cycle timed out after {timeout_sec:.0f}s; "
                            "worker may still be running"
                        )
                        refreshed = await _load_state_async(telegram_id, network)
                        refreshed["worker_group"] = worker_group
                        refreshed["worker_last_heartbeat"] = time.time()
                        refreshed["last_cycle_ms"] = (time.perf_counter() - cycle_started) * 1000.0
                        refreshed["last_cycle_result"] = _cycle_result_label(ok, error_msg)
                        await _save_state_async(telegram_id, network, refreshed)
                    else:
                        ok = bool(delegated.get("ok", True))
                        error_msg = delegated.get("error")
                        refreshed = await _load_state_async(telegram_id, network)
                        refreshed["worker_group"] = worker_group
                        refreshed["worker_last_heartbeat"] = float(delegated.get("completed_at") or time.time())
                        refreshed["last_cycle_ms"] = float(delegated.get("elapsed_ms") or 0.0)
                        refreshed["last_cycle_result"] = _cycle_result_label(ok, error_msg)
                        refreshed["worker_pid"] = delegated.get("worker_pid")
                        await _save_state_async(telegram_id, network, refreshed)
                else:
                    timeout_sec = _strategy_cycle_timeout_seconds()
                    strategy = str(state.get("strategy") or "").lower().strip()
                    if timeout_sec and strategy == "vol":
                        timeout_sec = min(timeout_sec, 45.0)
                    _nudged = bool(payload.get("nudge"))
                    try:
                        if timeout_sec:
                            ok, error_msg = await asyncio.wait_for(
                                _run_cycle(telegram_id, network, state, nudge=_nudged),
                                timeout=timeout_sec,
                            )
                        else:
                            ok, error_msg = await _run_cycle(telegram_id, network, state, nudge=_nudged)
                    except asyncio.TimeoutError:
                        _job_stats["cycle_timeouts"] += 1
                        tsec = timeout_sec or 180.0
                        ok = False
                        error_msg = (
                            f"Strategy cycle timed out after {tsec:.0f}s "
                            "(NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS)"
                        )
                        logger.error(
                            "Strategy cycle timeout user=%s network=%s strategy=%s",
                            telegram_id,
                            network,
                            state.get("strategy"),
                        )
                    hb_state = await _load_state_async(telegram_id, network)
                    hb_state["worker_last_heartbeat"] = time.time()
                    hb_state["last_cycle_ms"] = (time.perf_counter() - cycle_started) * 1000.0
                    hb_state["last_cycle_result"] = _cycle_result_label(ok, error_msg)
                    await _save_state_async(telegram_id, network, hb_state)
                if ok:
                    _job_stats["cycles_ok"] += 1
                    try:
                        from src.nadobro.core.user_circuit import record_success

                        record_success(telegram_id, network)
                    except Exception as e:
                        logger.debug("user-circuit record_success failed: %s", e)
                    if _cycle_result_label(ok, error_msg) == "ok":
                        refreshed = await _load_state_async(telegram_id, network)
                        if refreshed.get("error_streak") or refreshed.get("last_error"):
                            refreshed["error_streak"] = 0
                            refreshed["last_error"] = None
                            refreshed["last_error_category"] = ""
                            await _save_state_async(telegram_id, network, refreshed)
                else:
                    _job_stats["cycles_failed"] += 1
                    try:
                        from src.nadobro.core.user_circuit import record_failure

                        record_failure(telegram_id, network, error_msg or "unknown cycle error")
                    except Exception as e:
                        logger.warning(
                            "user-circuit record_failure failed for user %s — "
                            "breaker may not open for a failing strategy: %s",
                            telegram_id, e,
                        )
                    await _mark_cycle_error(telegram_id, network, error_msg or "unknown cycle error")
            except Exception as e:
                _job_stats["cycles_failed"] += 1
                logger.error("Strategy cycle crash for user %s on %s: %s", telegram_id, network, e, exc_info=True)
                strategy_name = str(state.get("strategy") or "strategy").upper()
                try:
                    from src.nadobro.core.user_circuit import record_failure

                    record_failure(telegram_id, network, str(e))
                except Exception as circuit_err:
                    logger.warning(
                        "user-circuit record_failure failed for user %s — "
                        "breaker may not open for a failing strategy: %s",
                        telegram_id, circuit_err,
                    )
                await _mark_cycle_error(telegram_id, network, f"{strategy_name}[cycle/crash]: {str(e)[:220]}")
            finally:
                elapsed_ms = (time.perf_counter() - cycle_started) * 1000.0
                record_metric("runtime.strategy_cycle.total", elapsed_ms)
                logger.info(
                    "Strategy cycle end user=%s network=%s elapsed_ms=%.1f",
                    telegram_id,
                    network,
                    elapsed_ms,
                )

            pending_payload = _job_pending_payloads.pop(key, None)
            coalesced_count = int(_job_coalesce_counts.pop(key, 0))
            if not pending_payload:
                break
            _job_stats["deferred_cycles"] += 1
            strategy_for_pending = str((pending_payload or {}).get("strategy") or state.get("strategy") or "").lower().strip()
            if strategy_for_pending == "vol":
                _job_stats["vol_deferred_cycles"] += 1
            # Carry the coalesced job's metadata into the next loop iteration.
            # Without this assignment the loop re-ran the original payload, so
            # a pending fill nudge silently lost nudge=True and hit the normal
            # interval gate.
            payload = pending_payload
            logger.info(
                "Running deferred coalesced strategy cycle for user %s on %s (%s coalesced tick(s))",
                telegram_id,
                network,
                coalesced_count or 1,
            )


async def _mark_cycle_error(telegram_id: int, network: str, error_msg: str):
    state = await _load_state_async(telegram_id, network)
    if not state.get("running"):
        return
    state["last_error"] = str(error_msg)[:300]
    state["last_error_category"] = _categorize_runtime_error(error_msg)
    streak = int(state.get("error_streak") or 0) + 1
    state["error_streak"] = streak
    await _save_state_async(telegram_id, network, state)
    if streak == STRATEGY_ERROR_ALERT_STREAK:
        strategy = str(state.get("strategy") or "").upper() or "STRATEGY"
        product = str(state.get("product") or "?")
        await _notify(
            telegram_id,
            "⚠️ {strategy} health alert on {product}-PERP ({network}): "
            "{streak} consecutive cycle errors.\n"
            "Latest: {error}",
            strategy=strategy, product=product, network=network,
            streak=streak, error=str(error_msg)[:220],
        )


def _dispatch_strategy(strategy: str, telegram_id: int, network: str, state: dict,
                       client, mid: float, product_id: int, product: str, open_orders: list) -> dict:
    if strategy == "bro":
        if not legacy_bro_autoloop_enabled():
            return {"success": True, "action": "skipped", "reason": "legacy_bro_autoloop_disabled"}
        # Alpha Agent autoloop backend was removed; nothing to run even if the
        # legacy flag is force-enabled.
        return {"success": True, "action": "skipped", "reason": "alpha_agent_autoloop_removed"}
    # Engine v2: legacy run_cycle dispatch retired; strategies are managed by
    # engine controllers via the orchestrator (see services/strategy_runtime).
    from src.nadobro.strategy.strategy_runtime import dispatch_cycle

    return dispatch_cycle(
        strategy, telegram_id, network, state,
        client=client, mid=mid, product_id=product_id, product=product, open_orders=open_orders,
    )


# MM run-duration (TWAP/participation): Mid + Dynamic Grid treat the duration as
# a HARD cap (flatten at expiry); Grid + Reverse Grid treat it as a SOFT target
# (keep running past it to let grid/profit conditions complete — per the docs).
_MM_DURATION_HARD_CAP_STRATEGIES = ("mid", "dgrid")


def _mm_duration_is_hard_cap(strategy: str) -> bool:
    return str(strategy or "").lower() in _MM_DURATION_HARD_CAP_STRATEGIES


# TWAP fast-move pause: when the reference price moves more than the configured
# per-cycle threshold, the passive quoting modes pause re-quoting for that cycle
# to avoid chasing a fast market (documented TWAP behavior). Opt-in — 0/unset =
# off (the right threshold is pair-dependent, so we never force a default).
# R-Grid is EXCLUDED: its taker-momentum entry is meant to fire on fast moves,
# so pausing it would suppress the strategy's whole point.
DEFAULT_TWAP_PAUSE_MOVE_BP = 0.0
_TWAP_PAUSE_STRATEGIES = ("mid", "grid", "dgrid")


def _twap_should_pause(state: dict, strategy: str, mid: float) -> bool:
    """True when the per-cycle price move exceeds ``twap_pause_move_bp`` (so the
    bot should skip re-quoting this cycle). Tracks ``twap_last_mid`` /
    ``twap_paused`` for the next cycle + /status. Returns False (and disables)
    for R-Grid, for a 0/unset threshold, or for a bad mid."""
    if str(strategy or "").lower() not in _TWAP_PAUSE_STRATEGIES:
        return False
    try:
        thr_bp = float(state.get("twap_pause_move_bp", DEFAULT_TWAP_PAUSE_MOVE_BP) or 0.0)
        mid_f = float(mid)
    except (TypeError, ValueError):
        return False
    if thr_bp <= 0 or mid_f <= 0:
        state["twap_paused"] = False
        return False
    last = state.get("twap_last_mid")
    state["twap_last_mid"] = mid_f
    paused = False
    try:
        last_f = float(last) if last is not None else 0.0
        if last_f > 0:
            paused = (abs(mid_f - last_f) / last_f) > (thr_bp / 10000.0)
    except (TypeError, ValueError, ZeroDivisionError):
        paused = False
    state["twap_paused"] = paused
    return paused


def _resolve_mm_run_duration_minutes(state: dict, deployed_usd: float, vol_24h_usd: float) -> float:
    """Enforced MM run length in minutes (0 = no cap → unchanged behavior).

    A user-set ``mm_duration_minutes`` wins, clamped to the participation bounds
    ([Aggressive … 10×Passive]) when 24h volume is known; otherwise the
    participation preset implies the duration (compute_pov_duration). Opt-in:
    with neither a custom duration nor a preset (or no volume), returns 0."""
    from src.nadobro.quant import pov_engine

    try:
        custom = float(state.get("mm_duration_minutes") or 0.0)
    except (TypeError, ValueError):
        custom = 0.0
    deployed = max(0.0, float(deployed_usd or 0.0))
    vol = max(0.0, float(vol_24h_usd or 0.0))
    preset = state.get("participation_preset")
    if custom > 0:
        if vol > 0 and deployed > 0:
            clamped, _lo, _hi = pov_engine.bound_user_duration_minutes(custom, deployed, vol)
            return float(clamped)
        return custom
    if preset and vol > 0 and deployed > 0:
        return float(pov_engine.compute_pov_duration(deployed, str(preset), vol)["duration_minutes"])
    return 0.0


def _resolve_mm_cycle_notional_usd(
    state: dict, deployed_usd: float, vol_24h_usd: float, min_notional_usd: float
) -> float:
    """Participation-derived per-cycle order chunk in USD (0 = no override →
    keep the deployed-based order sizing). Only when a participation preset is
    set AND 24h volume is known.

    Delegates to ``pov_engine.cycle_notional_usd`` — the SINGLE source of truth
    shared with the pre-trade card. The two used to compute different numbers
    (the card assumed a 1/multiplier-minute cycle the bot never ran), so the
    preview and the live bot told the user different stories. Floored at the
    throughput minimum and the venue minimum, capped at the deployed budget."""
    from src.nadobro.quant import pov_engine

    preset = state.get("participation_preset")
    deployed = max(0.0, float(deployed_usd or 0.0))
    vol = max(0.0, float(vol_24h_usd or 0.0))
    if not preset or vol <= 0 or deployed <= 0:
        return 0.0
    # POV-CADENCE (2026-08-03): size against the cadence the bot ACTUALLY runs.
    # rgrid/mid are fast-cadence (min(configured, 8s)), so sizing off the raw
    # `interval_seconds` overstated the per-cycle chunk by up to 7.5x — the
    # participation preset then meant nothing like its nominal rate.
    return pov_engine.cycle_notional_usd(
        str(preset), vol,
        effective_interval_seconds(
            str(state.get("strategy") or "").lower().strip(),
            state.get("interval_seconds") or 60,
        ),
        deployed,
        venue_min_notional_usd=float(min_notional_usd or 0.0),
    )


def _best_effort_pair_24h_volume_usd(telegram_id: int, network: str, product: str) -> float:
    """Pair 24h USD volume for POV sizing; 0 on any failure (so duration stays
    opt-out rather than crashing the start)."""
    try:
        from src.nadobro.users.user_service import get_user_readonly_client
        client = get_user_readonly_client(telegram_id, network=network)
        if client is None:
            return 0.0
        pid = get_product_id(str(product).upper(), network=network)
        if pid is None:
            return 0.0
        stats = client.get_product_market_stats(int(pid)) or {}
        return float(stats.get("volume_24h_usd") or 0.0)
    except Exception:  # noqa: BLE001  # policy: degrade-ok(volume is best-effort; duration just stays uncapped)
        return 0.0


async def _evaluate_mm_duration_rail(
    telegram_id: int, network: str, state: dict, strategy: str, product: str, *, client,
) -> tuple[bool, str | None] | None:
    """Enforce the MM run duration. Hard-cap modes (Mid/D-Grid) finalize +
    flatten at expiry; soft-target modes (Grid/R-Grid) notify once and keep
    running. ``None`` when no duration is set or it hasn't elapsed yet."""
    try:
        dur = float(state.get("mm_run_duration_minutes") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        return None
    started_raw = state.get("started_at")
    if not started_raw:
        return None
    try:
        started = datetime.fromisoformat(str(started_raw))
        elapsed_min = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
    except Exception:  # noqa: BLE001  # policy: degrade-ok(bad timestamp → skip the cap this cycle)
        return None
    if elapsed_min < dur:
        return None
    if not _mm_duration_is_hard_cap(strategy):
        # Grid / R-Grid: target only — keep running for profit conditions; tell
        # the user once that the planned duration elapsed.
        if not state.get("mm_duration_target_notified"):
            state["mm_duration_target_notified"] = True
            await _save_state_async(telegram_id, network, state)
            await _notify(
                telegram_id,
                "⏱️ {strategy} on {market} ({network}): planned duration "
                "({mins:.0f}m) reached — still running to let grid/profit conditions complete.",
                strategy=_strategy_display_name(strategy),
                market=_market_label_for_strategy(strategy, product, state),
                network=network, mins=dur,
            )
        return None
    # Mid / D-Grid: hard cap — finalize, then stop + flatten.
    _finalize_session(state, stop_reason="duration_reached")
    state["running"] = False
    state["last_action"] = "mm_duration_reached"
    await _save_state_async(telegram_id, network, state)
    # Stop the engine controller FIRST so its resting maker orders are cancelled
    # before we flatten — otherwise close_all_positions races the still-live
    # controller and leaves "1 open orders remain" (mirrors the SL/TP rail).
    try:
        from src.nadobro.strategy import engine_runtime as _er_dur
        if strategy in _er_dur.ENGINE_MAPPED_STRATEGIES:
            await _er_dur.RUNTIME.stop(telegram_id, network, strategy)
    except Exception:  # noqa: BLE001 - close_all_positions is the backstop
        logger.warning("engine stop before duration cap close failed user=%s", telegram_id, exc_info=True)
    close_res = await run_blocking(close_all_positions, telegram_id, network, strategy_session_id=int(state.get("strategy_session_id") or 0) or None)
    if isinstance(close_res, dict) and not close_res.get("success"):
        logger.warning(
            "mm duration close failed user=%s network=%s strategy=%s: %s",
            telegram_id, network, strategy, close_res.get("error", "unknown"),
        )
    await _notify(
        telegram_id,
        "⏱️ {strategy} on {market} ({network}): run duration ({mins:.0f}m) reached — "
        "orders cancelled and positions flattened.",
        strategy=_strategy_display_name(strategy),
        market=_market_label_for_strategy(strategy, product, state),
        network=network, mins=dur,
    )
    return True, None


async def _evaluate_session_pnl_rail(
    telegram_id: int,
    network: str,
    state: dict,
    strategy: str,
    product: str,
    *,
    client,
    close_coro,
    market_label: str | None = None,
) -> tuple[bool, str | None] | None:
    """Session-level SL/TP guard driven by *live* Nado PnL (realized +
    unrealized) measured as a percentage of the configured margin.

    Returns ``(True, None)`` when a stop fired (session finalized, positions
    closed, user notified) so ``_run_cycle`` can return it directly; ``None``
    when no stop is warranted and the cycle should continue.

    This replaces the dead ``result["action"] == "grid_stop_loss_hit"`` branches:
    ``run_engine_cycle`` only ever returns ``action="engine_ticked"``, so those
    branches never fired and the session SL/TP never triggered — the bug that let
    a $1 stop ride to a $32 loss. The threshold is now ``% of margin`` (the
    user's intent: SL 1% of $100 = stop at -$1), not a leverage-blind
    ``% price move``, and PnL includes the open position's unrealized PnL so an
    open drawdown actually trips the stop.
    """
    # Honor the per-strategy SL/TP (rgrid/dgrid store them under rgrid_* keys);
    # previously this read sl_pct/tp_pct only, so a custom rgrid/dgrid SL was
    # ignored and the rail used the 0.8 default.
    from src.nadobro.strategy.strategy_registry import effective_sl_tp_pct
    sl_pct, tp_pct = effective_sl_tp_pct(strategy, state)
    # Overlay-adaptive barriers: when the financial overlay steers this strategy
    # and has written regime-adjusted SL/TP (widen in a trend, tighten in chop),
    # the rail uses them instead of the user's static config. Bounded upstream
    # (the engine caps the widening at ~1.3x the user's base) and backstopped by
    # the 10% overlay drawdown cap below.
    try:
        from src.nadobro.strategy.overlay_actuator import overlay_applies
        if overlay_applies(strategy):
            ov_sl = state.get("overlay_sl_pct")
            ov_tp = state.get("overlay_tp_pct")
            # RE-CLAMP against the user's CURRENT setting, don't just adopt.
            # overlay_actuator.rail_barriers already enforces tighten-only SL /
            # widen-only TP — but only at WRITE time, and these keys survive in
            # state across cycles where the overlay bailed early (candle fetch
            # failed, product id missing, an exception). The user's settings are
            # re-merged into state every cycle, so a mid-run SL tighten
            # (2.0% -> 0.5%) would otherwise lose to a stale 1.6% overlay value.
            # The user's number is the binding contract in both directions.
            if ov_sl is not None:
                sl_pct = min(float(ov_sl), sl_pct) if sl_pct > 0 else float(ov_sl)
            if ov_tp is not None:
                tp_pct = max(float(ov_tp), tp_pct) if tp_pct > 0 else float(ov_tp)
    except Exception:  # noqa: BLE001 - fall back to the user's config barriers
        logger.debug("overlay barrier read failed", exc_info=True)
    if sl_pct <= 0 and tp_pct <= 0:
        return None

    from src.nadobro.trading.session_resolver import resolve_current_strategy_session

    sess = await run_blocking(
        resolve_current_strategy_session,
        telegram_id, network, strategy,
        state=state,
    )
    if not sess:
        return None
    if str(sess.get("status") or "").lower() != "running":
        session_id = sess.get("id") or state.get("strategy_session_id") or "unknown"
        logger.warning(
            "state still running for non-running strategy session #%s user=%s network=%s strategy=%s; stopping",
            session_id, telegram_id, network, strategy,
        )
        state["running"] = False
        state["last_error"] = (
            f"Stopped because strategy session #{session_id} is no longer running; "
            "orders were cleaned up to prevent untracked fills."
        )
        await _save_state_async(telegram_id, network, state)
        try:
            from src.nadobro.strategy import engine_runtime as _er_stop
            if strategy in _er_stop.ENGINE_MAPPED_STRATEGIES:
                await _er_stop.RUNTIME.stop(telegram_id, network, strategy)
        except Exception:  # noqa: BLE001 - close_coro still runs as the backstop
            logger.warning("engine stop for stale session failed user=%s", telegram_id, exc_info=True)
        close_res = await close_coro()
        if not close_res.get("success"):
            logger.warning(
                "stale session close failed user=%s network=%s strategy=%s: %s",
                telegram_id, network, strategy, close_res.get("error", "unknown"),
            )
        await _notify(
            telegram_id,
            "⚠️ {strategy} stopped on {network}: the tracked session was already closed, "
            "so orders were cleaned up to prevent untracked fills.",
            strategy=_strategy_display_name(strategy), network=network,
        )
        return True, None

    from src.nadobro.trading.live_session import get_live_session_snapshot

    snap = await run_blocking(
        get_live_session_snapshot, telegram_id, network, sess,
        state=state, client=client,
    )
    if float(snap.get("margin") or 0.0) <= 0:
        # No margin basis to measure a % against — skip rather than divide by zero.
        return None
    pnl = float(snap.get("session_pnl") or 0.0)
    pct = float(snap.get("session_pnl_pct") or 0.0)
    # SLTP-GROSS fix: judge the stop on NET-of-fees PnL so it doesn't fire late
    # by the accumulated fee drag. Fall back to the gross basis when the net
    # field is absent (older snapshots). The user-facing message still shows the
    # gross figure (``pnl``/``pct``) for continuity.
    pct_net = float(snap.get("session_pnl_pct_net", pct) or 0.0)

    reason = ""
    if sl_pct > 0 and pct_net <= -sl_pct:
        reason = "sl_hit"
    elif tp_pct > 0 and pct_net >= tp_pct:
        reason = "tp_hit"
    # Overlay drawdown kill-switch: a SECOND, independent stop for the financial
    # overlay (10% of margin by default), armed only when the overlay steers
    # this strategy. Either the user's session SL OR this cap trips flatten +
    # stand-down. It reuses THIS snapshot (no extra live-session read).
    if not reason:
        try:
            from src.nadobro.strategy.overlay_actuator import (
                OVERLAY_DRAWDOWN_CAP_PCT,
                overlay_applies,
                overlay_drawdown_breached,
            )
            if overlay_applies(strategy) and overlay_drawdown_breached(
                pct_net, OVERLAY_DRAWDOWN_CAP_PCT
            ):
                reason = "overlay_drawdown"
        except Exception:  # noqa: BLE001 - overlay cap is best-effort; user SL still governs
            logger.debug("overlay drawdown check failed", exc_info=True)
    if not reason:
        return None

    _finalize_session(state, stop_reason=reason)
    state["running"] = False
    if reason == "tp_hit":
        state["last_error"] = None
    elif reason == "overlay_drawdown":
        from src.nadobro.strategy.overlay_actuator import OVERLAY_DRAWDOWN_CAP_PCT as _cap
        state["last_error"] = (
            f"Stopped by overlay drawdown kill-switch (≥{_cap:.0f}% of "
            f"margin): PnL ${pnl:,.2f} ({pct:.2f}% of "
            f"${float(snap.get('margin') or 0.0):,.2f} margin)."
        )
    else:
        state["last_error"] = (
            f"Stopped by session SL: PnL ${pnl:,.2f} ({pct:.2f}% of "
            f"${float(snap.get('margin') or 0.0):,.2f} margin)."
        )
    await _save_state_async(telegram_id, network, state)

    label = market_label or f"{product}-PERP"
    strategy_label = _strategy_display_name(strategy)
    # Stop the engine controller FIRST so its resting maker orders are cancelled
    # (GridExecutor._stop_out) before we flatten — otherwise close_all_positions
    # races the still-live controller and leaves "1 open orders remain". Awaited
    # on THIS cycle's loop (where the orchestrator was just ticked) so its
    # loop-bound primitives are valid; close_coro is the backstop.
    try:
        from src.nadobro.strategy import engine_runtime as _er_stop
        if strategy in _er_stop.ENGINE_MAPPED_STRATEGIES:
            await _er_stop.RUNTIME.stop(telegram_id, network, strategy)
    except Exception:  # noqa: BLE001 - close_coro still runs as the backstop
        logger.warning("engine stop before SL/TP close failed user=%s", telegram_id, exc_info=True)
    close_res = await close_coro()
    if close_res.get("success"):
        if reason == "tp_hit":
            _msg = ("✅ {strategy} target reached on {market} ({network}) — session TP hit "
                    "(PnL ${pnl} / {pct}% of margin).")
        elif reason == "overlay_drawdown":
            _msg = ("🛑 {strategy} stopped on {market} ({network}) — overlay drawdown "
                    "kill-switch (PnL ${pnl} / {pct}% of margin).")
        else:
            _msg = ("🛑 {strategy} stopped on {market} ({network}) — session SL hit "
                    "(PnL ${pnl} / {pct}% of margin).")
        await _notify(
            telegram_id,
            _msg,
            strategy=strategy_label, market=label, network=network,
            pnl=f"{pnl:,.2f}", pct=f"{pct:.2f}",
        )
    else:
        logger.warning(
            "session %s close failed for user %s on %s: %s",
            reason, telegram_id, network, close_res.get("error", "unknown"),
        )
        await _notify(
            telegram_id,
            "⚠️ {strategy} session {kind} triggered on {market} ({network}), but cleanup "
            "failed. Please close remaining exposure on Nado. Error: {error}",
            strategy=strategy_label, kind=("TP" if reason == "tp_hit" else "SL"),
            market=label, network=network, error=close_res.get("error", "unknown"),
        )
    return True, None


async def _run_cycle(
    telegram_id: int, network: str, state: dict, *, nudge: bool = False
) -> tuple[bool, str | None]:
    try:
        from src.nadobro.strategy.strategy_fsm import PHASE_SCANNING, apply_phase

        apply_phase(state, PHASE_SCANNING, "Strategy cycle started.")
    except Exception:  # policy: degrade-ok(phase indicator is display-only)
        pass
    # Apply latest saved strategy parameters (margin, spreads, etc.) every cycle so edits
    # in the UI take effect without restarting the loop.
    try:
        strategy_key = str(state.get("strategy") or "").lower()
        if strategy_key:
            from src.nadobro.users.settings_service import get_strategy_settings

            _, cfg = get_strategy_settings(telegram_id, strategy_key)
            if isinstance(cfg, dict):
                for k, v in cfg.items():
                    if k in _STRATEGY_SETTINGS_RUNTIME_BLOCKLIST:
                        continue
                    state[k] = v
    except Exception:
        logger.warning("Failed to merge strategy settings into runtime state for user %s", telegram_id, exc_info=True)

    if is_trading_paused():
        state["last_action"] = "maintenance_pause"
        state["last_action_detail"] = "Global trading pause active. Strategy execution is suspended."
        if str(state.get("strategy", "")).lower() == "dn" and float(state.get("auto_close_on_maintenance") or 0) >= 0.5:
            _finalize_session(state, stop_reason="maintenance_pause")
            state["running"] = False
            state["last_error"] = "Auto-closed on maintenance pause."
            await _save_state_async(telegram_id, network, state)
            close_res = await run_blocking(
                close_delta_neutral_legs,
                telegram_id,
                str(state.get("product") or ""),
                network,
                float(state.get("slippage_pct") or 1.0),
                "dn",
                state.get("strategy_session_id"),
            )
            if close_res.get("success"):
                await _notify(telegram_id, "Delta Neutral stopped and auto-closed due to maintenance pause.")
            else:
                await _notify(
                    telegram_id,
                    "Delta Neutral stop triggered, but auto-close cleanup failed. "
                    "Please close manually and check open orders. Error: {error}",
                    error=close_res.get('error', 'unknown'),
                )
        else:
            await _save_state_async(telegram_id, network, state)
        return True, "maintenance_pause"
    user = await run_blocking(get_user, telegram_id)
    if not user:
        _finalize_session(state, stop_reason="user_deleted")
        state["running"] = False
        await _save_state_async(telegram_id, network, state)
        return True, None
    if user.network_mode.value != network:
        _finalize_session(state, stop_reason="mode_switch")
        state["running"] = False
        state["last_error"] = f"Stopped because active mode switched to {user.network_mode.value}"
        await _save_state_async(telegram_id, network, state)
        await _notify(
            telegram_id,
            "Stopped {strategy} loop on {network}: active mode changed to {new_mode}.",
            strategy=state.get('strategy', '').upper(), network=network, new_mode=user.network_mode.value,
        )
        return True, None

    strategy = _normalize_strategy_id(state.get("strategy"))
    state["strategy"] = strategy
    if strategy == "bro":
        cleanup_res = await run_blocking(_migrate_legacy_bro_state, telegram_id, network, state)
        if not cleanup_res.get("success"):
            return False, str(cleanup_res.get("error") or "Legacy Alpha Agent cleanup failed")[:300]
        return True, None

    last_run = _safe_last_run_ts(state.get("last_run_ts"))
    # Option 1: honour the per-strategy effective cadence (fast for rgrid/mid).
    # This in-cycle guard would otherwise skip every accelerated cycle back to
    # the raw 60s interval and silently defeat the faster scheduler cadence.
    # AUDIT-MM-2026-07-14 #4: a fill-NUDGED cycle uses a 1s minimum gap instead
    # of the full interval — reacting before the next scheduled tick is the
    # nudge's entire purpose, and this gate was silently discarding every
    # nudged cycle as "skipped_interval". Overlap safety is unchanged (the
    # per-user job lock upstream serializes cycles).
    interval = effective_interval_seconds(strategy, int(state.get("interval_seconds") or 60))
    gate_seconds = 1.0 if nudge else interval
    if last_run > 0 and time.time() - last_run < gate_seconds:
        return True, "skipped_interval"

    product = state.get("product", "BTC")
    if _is_legacy_vol_perp_state(state):
        return await _retire_legacy_vol_perp_state(telegram_id, network, state)

    if strategy == "bro":
        client = await run_blocking(get_user_nado_client, telegram_id)
        if not client:
            client = await run_blocking(get_user_readonly_client, telegram_id)
        if not client:
            raise RuntimeError("Wallet client unavailable")
        if hasattr(client, "network") and client.network != network:
            raise RuntimeError(f"Network mismatch: expected {network}, got {client.network}")
        with timed_metric("runtime.strategy.dispatch.bro"):
            result = await run_blocking(
                _dispatch_strategy,
                strategy, telegram_id, network, state,
                client, 0.0, 0, "MULTI", [],
            )
        prev_runs = int(state.get("runs") or 0)
        state["last_run_ts"] = time.time()
        bro_action = result.get("action", "")
        bro_detail = result.get("detail", "")
        bro_confidence = result.get("confidence", 0)
        state["runs"] = prev_runs + 1
        state["last_error"] = result.get("error") or result.get("order_error")
        state["last_action"] = bro_action
        state["last_action_detail"] = bro_detail[:200] if bro_detail else ""
        _update_order_observability(state, result)
        await _save_state_async(telegram_id, network, state)
        if prev_runs == 0 or bro_action in ("open_long", "open_short", "close", "emergency_flatten", "blocked"):
            if bro_action == "hold":
                await _notify(
                    telegram_id,
                    "🧠 Alpha Agent cycle #{cycle} ({network}): "
                    "HOLD — {detail}\n"
                    "Confidence: {confidence} | Next scan in {interval}s",
                    cycle=state['runs'], network=network, detail=bro_detail[:150],
                    confidence=f"{bro_confidence:.0%}", interval=int(state.get('interval_seconds', 300)),
                )
            elif bro_action in ("open_long", "open_short"):
                side_label = "LONG 📈" if bro_action == "open_long" else "SHORT 📉"
                await _notify(
                    telegram_id,
                    "🧠 Alpha Agent cycle #{cycle} ({network}): "
                    "{side} — {detail}",
                    cycle=state['runs'], network=network, side=side_label, detail=bro_detail[:150],
                )
            elif bro_action == "blocked":
                await _notify(
                    telegram_id,
                    "🧠 Alpha Agent cycle #{cycle} ({network}): "
                    "Blocked — {detail}",
                    cycle=state['runs'], network=network, detail=bro_detail[:150],
                )
            elif bro_action:
                await _notify(
                    telegram_id,
                    "🧠 Alpha Agent cycle #{cycle} ({network}): "
                    "{action} — {detail}",
                    cycle=state['runs'], network=network, action=bro_action.upper(), detail=bro_detail[:150],
                )
        elif bro_action == "hold" and state["runs"] % 6 == 0:
            await _notify(
                telegram_id,
                "🧠 Alpha Agent update ({network}): Still scanning, {cycles} cycles run. "
                "Last: HOLD — {detail}",
                network=network, cycles=state['runs'], detail=bro_detail[:100],
            )
        logger.info(
            "Alpha Agent cycle #%d for user %s: action=%s confidence=%.2f detail=%s",
            state["runs"], telegram_id, bro_action, bro_confidence, bro_detail[:100],
        )

        # Increment bro session metrics
        session_id = state.get("strategy_session_id")
        if session_id:
            try:
                orders = 1 if bro_action in ("open_long", "open_short", "close") else 0
                filled = orders if result.get("success") else 0
                await run_blocking(
                    increment_session_metrics,
                    int(session_id),
                    cycles=1,
                    orders_placed=orders,
                    orders_filled=filled,
                    pnl=float(result.get("pnl", 0) or 0),
                    volume=float(result.get("placed_notional_usd", 0) or 0),
                )
            except Exception as e:
                logger.warning(
                    "session metrics not recorded for session %s — "
                    "cycle/PnL/volume counters will undercount: %s",
                    session_id, e,
                )

        if not result.get("success", True):
            return False, str(result.get("error") or result.get("order_error") or "unknown")[:300]
        return True, None

    dn_pair = get_dn_pair(product, network=network, client=None) if strategy == "dn" else {}
    if strategy == "vol":
        prod = normalize_volume_spot_symbol(str(product or ""))
        product_id = get_spot_product_id(prod, network=network)
    else:
        product_id = int(dn_pair.get("perp_product_id")) if dn_pair.get("perp_product_id") is not None else get_product_id(product, network=network)
    if product_id is None:
        raise RuntimeError(f"Invalid product '{product}'")

    # Use signing client so strategies can cancel orders and perform writes.
    # Falls back to readonly if signing client isn't available (read-only mode).
    # LOOP-STARVATION fix: these per-cycle VENUE reads go on the SDK pool, NOT
    # the 8-worker MISC pool. The MISC pool is the interactive click path's pool
    # (get_user, render helpers); a fleet of concurrent strategy cycles holding
    # MISC workers for the ~30s SDK timeout chain was queueing button taps
    # behind them (FIFO) — the ~100s tap latency in the callback.total SLO.
    client = await run_blocking_sdk(get_user_nado_client, telegram_id)
    if not client:
        client = await run_blocking_sdk(get_user_readonly_client, telegram_id)
    if not client:
        raise RuntimeError("Wallet client unavailable")

    # Option 7: overlap the two independent per-cycle reads (mid + open orders)
    # so cycle latency is the slower of the two, not their sum. Engine strategies
    # always take both paths and never early-return between the reads, so they
    # gather; the legacy directional path stays sequential because its
    # session-PnL rail can return before open orders are ever needed.
    async def _fetch_mid() -> float:
        with timed_metric("runtime.market_price.fetch"):
            if strategy == "vol":
                try:
                    _mp = await asyncio.wait_for(
                        run_blocking_sdk(client.get_market_price, product_id),
                        timeout=_vol_call_timeout_seconds(),
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError("VOL market price call timed out")
            else:
                _mp = await run_blocking_sdk(client.get_market_price, product_id)
        return float(_mp.get("mid") or 0.0)

    async def _fetch_open_orders():
        with timed_metric("runtime.open_orders.fetch"):
            if strategy == "vol":
                try:
                    return await asyncio.wait_for(
                        run_blocking_sdk(client.get_open_orders, product_id),
                        timeout=_vol_call_timeout_seconds(),
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError("VOL open-orders call timed out")
            return await run_blocking_sdk(client.get_open_orders, product_id)

    _engine_cycle = strategy in ("grid", "rgrid", "dgrid", "mid", "dn", "vol")
    open_orders = None
    if _engine_cycle:
        mid, open_orders = await asyncio.gather(_fetch_mid(), _fetch_open_orders())
    else:
        mid = await _fetch_mid()
    if mid <= 0:
        raise RuntimeError("Could not fetch market price")

    reference_price = float(state.get("reference_price") or 0.0)
    if reference_price <= 0:
        state["reference_price"] = mid
        await _save_state_async(telegram_id, network, state)
        reference_price = mid

    # TWAP fast-move pause (Phase 3): for the passive quoting modes, skip this
    # cycle's re-quote when the price moved too fast (opt-in via twap_pause_move_bp).
    mm_twap_paused = False
    if strategy in _TWAP_PAUSE_STRATEGIES:
        _prev_twap_paused = bool(state.get("twap_paused"))
        mm_twap_paused = _twap_should_pause(state, strategy, mid)
        await _save_state_async(telegram_id, network, state)
        if mm_twap_paused and not _prev_twap_paused:
            await _notify(
                telegram_id,
                "⏸ {strategy} paused new quotes on {product} ({network}) — price moved "
                "too fast this cycle. Resting orders stay; quoting resumes when it settles.",
                strategy=strategy.upper(), product=product, network=network,
            )
        elif _prev_twap_paused and not mm_twap_paused:
            await _notify(
                telegram_id,
                "▶️ {strategy} resumed quoting on {product} ({network}) — price settled.",
                strategy=strategy.upper(), product=product, network=network,
            )

    if strategy not in ("grid", "rgrid", "dgrid", "mid", "dn", "vol"):
        # Legacy/directional strategies: same margin-based session-PnL rail as
        # the engine strategies (was a leverage-blind, direction-blind % price
        # move from a reference price — wrong units and would fire on favourable
        # moves too).
        rail = await _evaluate_session_pnl_rail(
            telegram_id, network, state, strategy, product,
            client=client,
            close_coro=lambda: run_blocking(close_all_positions, telegram_id, network, strategy_session_id=int(state.get("strategy_session_id") or 0) or None),
        )
        if rail is not None:
            return rail

    # Engine strategies already fetched this concurrently with mid above; the
    # legacy path fetches it here, only after its session-PnL rail may have
    # returned (so a rail exit never pays for an unused open-orders read).
    if open_orders is None:
        open_orders = await _fetch_open_orders()

    from src.nadobro.strategy.engine_runtime import (
        ENGINE_MAPPED_STRATEGIES, engine_v2_enabled, run_engine_cycle,
    )

    if engine_v2_enabled() and strategy in ENGINE_MAPPED_STRATEGIES:
        # Engine v2 owns execution for this strategy (feature-gated).
        with timed_metric(f"runtime.strategy.engine.{strategy}"):
            if mm_twap_paused:
                # Skip the controller tick (no re-quote/re-anchor) this cycle;
                # resting orders stay put. The SL + duration rails below still
                # run, so safety is never paused — only quoting.
                result = {
                    "success": True, "action": "twap_paused",
                    "strategy": strategy, "paused_reason": "fast_move",
                }
            else:
                result = await run_engine_cycle(
                    telegram_id, network, state, client, mid, product, product_id
                )
        # Real per-cycle order counts. run_engine_cycle returns the controller's
        # CUMULATIVE placed/filled/cancelled; convert to this-cycle deltas so the
        # log / "orders placed" notify / session metrics report actual activity
        # (the cycle result otherwise carries no count -> always 0). The
        # cumulative is then re-accumulated into order_observability for /status.
        if isinstance(result, dict) and isinstance(result.get("order_counts"), dict):
            _counts = result["order_counts"]
            _prev = state.get("_engine_order_counts_prev") or {}
            for _k in ("orders_placed", "orders_filled", "orders_cancelled"):
                _cum = int(_counts.get(_k, 0) or 0)
                _p = int(_prev.get(_k, 0) or 0)
                # Guard a worker restart resetting the cumulative below prev.
                result[_k] = (_cum - _p) if _cum >= _p else _cum
            state["_engine_order_counts_prev"] = {
                _k: int(_counts.get(_k, 0) or 0)
                for _k in ("orders_placed", "orders_filled", "orders_cancelled")
            }
        # Gate visibility (2026-07-31): persist the live quote-gate verdict so
        # /status can show "Quoting: PAUSED (…)" instead of a green "Last
        # cycle: OK" while the strategy sits dark. engine_diag carries the
        # controller's verdict every cycle; "since" only moves on a flip. The
        # keys ride the state saves already on this path (telemetry block /
        # end-of-cycle observability save).
        _gate_diag = result.get("engine_diag") if isinstance(result, dict) else None
        if isinstance(_gate_diag, dict) and _gate_diag.get("gate_verdict"):
            _gate_v = str(_gate_diag.get("gate_verdict") or "")
            if state.get("mm_gate_verdict") != _gate_v:
                state["mm_gate_since_ts"] = time.time()
            state["mm_gate_verdict"] = _gate_v
            state["mm_gate_reason"] = str(_gate_diag.get("gate_reason") or "")
        # Regime-gate flip: tell the user once per transition. Pause never
        # touches open positions — exits/close legs keep managing; only NEW
        # opening quotes wait for the range to return.
        gate_event = result.get("gate_event") if isinstance(result, dict) else None
        if gate_event:
            from src.nadobro.engine.routines.regime_gate import (
                GATE_REASON_HUMAN as _gate_reasons,
            )
            if str(gate_event.get("state")) == "PAUSE":
                await _notify(
                    telegram_id,
                    "⏸ {strategy} paused new quotes on {product} ({network}) — {why}.\n"
                    "Open positions and exits keep running; quoting resumes "
                    "automatically when the market ranges again.",
                    strategy=strategy.upper(), product=product, network=network,
                    why=_gate_reasons.get(str(gate_event.get("reason")), "unfavourable regime"),
                )
            else:
                await _notify(
                    telegram_id,
                    "▶️ {strategy} resumed quoting on {product} ({network}) — "
                    "market is ranging again.",
                    strategy=strategy.upper(), product=product, network=network,
                )
        # Dynamic Grid telemetry: persist live phase / variance / realized-move
        # so /status reflects reality (it previously always read GRID / 0.00 /
        # 0.0bp because nothing ever wrote these keys).
        # Grid / Reverse / Dynamic telemetry: persist live phase / variance /
        # anchor / side / reset so /status reflects reality (it previously read
        # GRID / 0.00 / 0.0bp / Anchor n/a / Side NONE because nothing wrote
        # these keys). dgrid_metrics also carries the shared grid_* block.
        _telemetry: dict = {}
        if isinstance(result, dict):
            _telemetry.update(result.get("dgrid_metrics") or {})
            _telemetry.update(result.get("grid_metrics") or {})
        # Self-heal: R-Grid used to run the D-Grid engine, so live R-Grid sessions
        # carry dgrid_phase / dgrid_variance_ratio in bot_state from before the
        # split. Nothing overwrites them now (the keys are only copied when
        # present), so a stale "DGRID phase: grid (var ratio 0.97)" would sit on
        # R-Grid's /status card forever. Drop them on the first cycle instead.
        _stale_dgrid = [
            _k for _k in ("dgrid_phase", "dgrid_variance_ratio", "dgrid_realized_move_bp",
                          "dgrid_reset_threshold_bp")
            if strategy != "dgrid" and _k in state and _k not in _telemetry
        ]
        if _telemetry or _stale_dgrid:
            for _k in _stale_dgrid:
                state.pop(_k, None)
            # The card/order-monitor read every one of these out of state (see
            # _bot_status_payload + formatters). Six of them were emitted by the
            # controllers and then never copied here, so the soft-reset levels, the
            # engaged flag, the net base and the two exposure legs rendered as
            # 0 / False / n/a for every grid-family session.
            for _k in ("dgrid_phase", "dgrid_variance_ratio", "dgrid_realized_move_bp",
                       "dgrid_reset_threshold_bp", "grid_anchor_price", "grid_reset_side",
                       "grid_drift_from_anchor_pct", "grid_reset_active",
                       "grid_mode", "grid_reset_threshold_bp", "grid_soft_reset_engaged",
                       "grid_reset_up_price", "grid_reset_down_price", "grid_net_base",
                       "grid_buy_exposure_price", "grid_sell_exposure_price"):
                if _k in _telemetry:
                    state[_k] = _telemetry[_k]
            await _save_state_async(telegram_id, network, state)
        # Dynamic Grid flip: GRID<->RGRID is a directional change the user must
        # see — the old position was just closed (reduce-only) and the opposite
        # side armed. One message per flip.
        dgrid_event = result.get("dgrid_event") if isinstance(result, dict) else None
        if dgrid_event:
            # Dynamic Grid only. R-Grid used to share DynamicGridController and so
            # inherited these flip messages for a mode that has no phases at all;
            # it now runs its own exposure-anchored controller (engine_runtime
            # CONTROLLER_REGISTRY) and never emits a dgrid_event.
            #
            # The reason describes the DECISION, not the raw sign of drift — see
            # dgrid_flip_reason. The side is named from the phase we switch TO.
            _to_phase = str(dgrid_event.get("to", "")).lower()
            await _notify(
                telegram_id,
                "🔄 {mode} switched {frm} → {to} on {product} ({network}) — "
                "{why} (variance ratio {vr}). Previous position closed; "
                "now quoting the {side} ladder.",
                mode=_STRATEGY_DISPLAY_NAMES.get(str(strategy).lower(), str(strategy).upper()),
                frm=str(dgrid_event.get("from", "")).upper(),
                to=str(dgrid_event.get("to", "")).upper(),
                side=("SHORT" if _to_phase == "rgrid" else "LONG"),
                product=product, network=network,
                why=dgrid_flip_reason(dgrid_event),
                vr=str(dgrid_event.get("variance_ratio", "")),
            )
        # Delta Neutral execution alerts: a hedge that is no longer a hedge
        # must NEVER be silent. One message per event, with the consequence.
        for dn_event in (result.get("dn_events") or []) if isinstance(result, dict) else []:
            kind = str(dn_event.get("kind") or "")
            detail = str(dn_event.get("detail") or "")
            dn_messages = {
                "leg_rollback": (
                    "⚠️ Delta Neutral on {product} ({network}): one leg failed to open — "
                    "rolling the other back so you are not left directional.\n{detail}"
                ),
                "leg_dead": (
                    "⚠️ Delta Neutral on {product} ({network}): one leg of your hedge died — "
                    "closing both legs now to avoid directional exposure.\n{detail}"
                ),
                "close_stuck": (
                    "🚨 Delta Neutral on {product} ({network}): the exit is taking longer than "
                    "expected — the bot keeps retrying every cycle.\n{detail}"
                ),
                "residual_exposure": (
                    "🚨 Delta Neutral on {product} ({network}): a position remainder could not be "
                    "flattened after 3 attempts — check your positions.\n{detail}"
                ),
                "funding_flip": (
                    "💹 Delta Neutral on {product} ({network}): funding flipped unfavorable after the "
                    "minimum hold — closing both legs to stop paying funding.\n{detail}"
                ),
            }
            template = dn_messages.get(kind)
            if template:
                await _notify(
                    telegram_id, template,
                    product=product, network=network, detail=detail,
                )
    elif strategy in ENGINE_MAPPED_STRATEGIES:
        # BUG-BR-1 fix: engine-mapped strategy but engine disabled. The
        # legacy run_cycle path was removed, so this would silently no-op
        # every cycle. Surface a clear error in the result + log so the
        # operator notices.
        logger.critical(
            "engine-mapped strategy '%s' invoked with engine_v2 DISABLED for user %s on %s; "
            "legacy dispatch path was removed — strategy will not place orders",
            strategy, telegram_id, network,
        )
        result = {
            "success": False,
            "error": f"engine_v2 disabled and strategy '{strategy}' has no legacy dispatch path",
            "action": "engine_disabled_noop",
            "strategy": strategy,
        }
    else:
        with timed_metric(f"runtime.strategy.dispatch.{strategy}"):
            if strategy == "vol":
                try:
                    result = await asyncio.wait_for(
                        run_blocking(
                            _dispatch_strategy,
                            strategy, telegram_id, network, state,
                            client, mid, product_id, product, open_orders,
                        ),
                        timeout=max(_vol_call_timeout_seconds(), 20.0),
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError("VOL strategy dispatch timed out")
            else:
                result = await run_blocking(
                    _dispatch_strategy,
                    strategy, telegram_id, network, state,
                    client, mid, product_id, product, open_orders,
                )

    if strategy in ("grid", "rgrid", "dgrid", "mid"):
        # MM run-duration (participation/TWAP): hard cap for Mid/D-Grid, soft
        # target for Grid/R-Grid. Checked before the SL rail so an expired run
        # finalizes cleanly.
        dur_rail = await _evaluate_mm_duration_rail(
            telegram_id, network, state, strategy, product, client=client,
        )
        if dur_rail is not None:
            return dur_rail
        # Session SL/TP on live Nado PnL (% of margin). Replaces the dead
        # ``result["action"] == grid_stop_loss_hit | grid_take_profit_hit |
        # circuit_breaker`` / "session notional cap reached" branches: the engine
        # (run_engine_cycle) only ever returns action="engine_ticked", so none of
        # those ever fired and the session SL/TP never triggered.
        rail = await _evaluate_session_pnl_rail(
            telegram_id, network, state, strategy, product,
            client=client,
            close_coro=lambda: run_blocking(close_all_positions, telegram_id, network, strategy_session_id=int(state.get("strategy_session_id") or 0) or None),
        )
        if rail is not None:
            return rail
    if strategy == "vol":
        # NO-EXPOSED-POSITION (2026-07-31): fold THIS cycle's vol counters into
        # state BEFORE the rail can fire. The rail's close path sizes its spot
        # sweep from state (vol_close_size / vol_entry_size via
        # _volume_spot_managed_size); merging afterwards meant a stop that
        # tripped on the same cycle as the first buy fill swept against a stale
        # 0, sold nothing, and still reported "positions closed" — leaving the
        # base sitting in the user's wallet. Merging is idempotent, and the
        # merge below still runs for the non-rail path.
        _merge_vol_order_counters(state, result)
        # Volume strategy session SL/TP on live Nado PnL (% of margin). Replaces
        # the dead ``result.get("done") + stop_reason`` branch — run_engine_cycle
        # never sets those, so the vol session stop never fired either.
        rail = await _evaluate_session_pnl_rail(
            telegram_id, network, state, strategy, product,
            client=client,
            close_coro=lambda: run_blocking(cleanup_strategy_positions, telegram_id, network, state),
            market_label=_market_label_for_strategy(strategy, product, state),
        )
        if rail is not None:
            return rail
    # DN deliberately has NO price-move session SL/TP rail. The shared
    # _evaluate_session_pnl_rail reads get_live_session_snapshot, which measures a
    # SINGLE product_id — but a Delta-Neutral run is a TWO-leg hedge (spot long +
    # perp short) on two products. Measuring one leg makes a normal ~sl_pct price
    # move look like a full session loss, so the rail fired within minutes of
    # opening, flattened the hedge, and finalized the run — which is exactly the
    # "doesn't hold for the configured time / cycles don't auto-restart" bug.
    # A delta-neutral position has no directional price risk to stop out of; its
    # real risks are covered by the controller itself: the drift gate (hedge
    # breakage), the dead-leg gate (one leg dies), the funding-flip exit, the
    # min/max hold, and manual close. So DN is intentionally excluded here.
    # (A correctly NET-of-both-legs session rail could be reinstated later, but it
    # needs a two-product snapshot — see get_live_session_snapshot.)

    # VOL-LOOP completion: the engine controller finished its work (e.g. Volume
    # reached its target volume / cycle cap) and signalled result["done"].
    # Finalize the session and tear the engine down — otherwise the strategy
    # would sit "running" forever (the old idle-in-'done'-phase bug).
    if isinstance(result, dict) and result.get("done"):
        reason = str(result.get("stop_reason") or "completed")
        # VOL-DONE-NO-SWEEP (self-review 2026-07-31): the controller can
        # self-complete while STILL HOLDING base — _complete("unsold_inventory")
        # means exactly that. This branch finalized the session and tore the
        # engine down without ever sweeping, stranding the position in the
        # user's wallet permanently. Flatten FIRST, then finalize. The sweep is
        # sized from vol_open_base (what the bot actually still holds), so a
        # normal completion that ended flat is a no-op and can never touch the
        # user's own spot balance.
        if strategy == "vol":
            _merge_vol_order_counters(state, result)
            try:
                _swept = await run_blocking(
                    cleanup_strategy_positions, telegram_id, network, state
                )
                if isinstance(_swept, dict) and not _swept.get("skipped"):
                    logger.warning(
                        "vol completion (%s) held inventory — swept before "
                        "finalizing user=%s result=%s",
                        reason, telegram_id, _swept.get("success"),
                    )
            except Exception:  # noqa: BLE001  # policy: degrade-ok(finalize still runs; sweep is logged)
                logger.error(
                    "vol completion sweep FAILED user=%s reason=%s — a position "
                    "may still be open", telegram_id, reason, exc_info=True,
                )
        _finalize_session(state, stop_reason=reason)
        state["running"] = False
        state["last_action"] = "engine_completed"
        await _save_state_async(telegram_id, network, state)
        try:
            from src.nadobro.strategy import engine_runtime as _er_done
            if strategy in _er_done.ENGINE_MAPPED_STRATEGIES:
                await _er_done.RUNTIME.stop(telegram_id, network, strategy)
        except Exception:  # noqa: BLE001 - finalize already persisted; stop is cleanup
            logger.warning("engine stop after completion failed user=%s", telegram_id, exc_info=True)
        market_label = _market_label_for_strategy(strategy, product, state)
        vol_usd = result.get("session_volume_usd")
        if vol_usd:
            await _notify(
                telegram_id,
                "✅ {strategy} completed on {market} ({network}) — {reason}. "
                "Volume traded: ${vol}.",
                strategy=_strategy_display_name(strategy), market=market_label,
                network=network, reason=reason.replace("_", " "),
                vol=f"{float(vol_usd):,.0f}",
            )
        else:
            await _notify(
                telegram_id,
                "✅ {strategy} completed on {market} ({network}) — {reason}.",
                strategy=_strategy_display_name(strategy), market=market_label,
                network=network, reason=reason.replace("_", " "),
            )
        return True, None

    prev_runs = int(state.get("runs") or 0)
    state["last_run_ts"] = time.time()
    state["runs"] = prev_runs + 1
    cycle_error = result.get("error") or result.get("order_error")
    state["last_error"] = cycle_error
    state["last_error_category"] = _categorize_runtime_error(cycle_error) if cycle_error else ""
    state["last_action"] = result.get("action", "cycle")
    state["last_action_detail"] = str(result.get("detail", ""))[:200]
    _merge_vol_order_counters(state, result)
    _update_order_observability(state, result)
    await _save_state_async(telegram_id, network, state)
    drift_seconds = max(0.0, state["last_run_ts"] - last_run - interval) if last_run > 0 else 0.0
    if drift_seconds > 0:
        record_metric("runtime.cycle_drift_ms", drift_seconds * 1000.0)

    # Notify user on first successful cycle with order placement confirmation
    orders_placed = int(result.get("orders_placed", 0))
    if prev_runs == 0 and result.get("success", True):
        market_label = _market_label_for_strategy(strategy, product, state)
        if orders_placed > 0:
            await _notify(
                telegram_id,
                "{strategy} is live on {market} ({network}) — {n} order(s) placed.",
                strategy=_strategy_display_name(strategy), market=market_label,
                network=network, n=orders_placed,
            )
        else:
            reason = result.get("reason") or result.get("detail") or result.get("action") or "waiting for conditions"
            reason = str(reason)
            # HUMANIZE-CYCLE1 (2026-07-31): "engine_ticked" is an internal
            # action name, not an explanation — users read it as a fault code.
            # Name the regime gate when it is the actual blocker; otherwise
            # translate the known action names into plain language.
            _diag1 = result.get("engine_diag") if isinstance(result, dict) else None
            if isinstance(_diag1, dict) and _diag1.get("gate_paused"):
                from src.nadobro.engine.routines.regime_gate import GATE_REASON_HUMAN
                _why = GATE_REASON_HUMAN.get(
                    str(_diag1.get("gate_reason") or ""), "unfavourable regime")
                reason = (
                    f"quoting is paused by the regime gate ({_why}); "
                    "it resumes when the market ranges again"
                )
            elif reason in ("engine_ticked", "engine_completed"):
                reason = "no quoting opportunity this cycle; the engine keeps watching"
            await _notify(
                telegram_id,
                "{strategy} cycle #1 on {market} ({network}): no orders placed — {reason}",
                strategy=_strategy_display_name(strategy), market=market_label,
                network=network, reason=str(reason)[:150],
            )

    # Surface persistent order-placement failures (cycles 2-5)
    if 1 <= prev_runs <= 4 and result.get("success", True) and orders_placed == 0:
        reason = result.get("reason") or result.get("detail") or result.get("action") or ""
        if reason and reason not in ("below threshold", "wait", "waiting"):
            logger.warning(
                "%s cycle #%d for user %s: 0 orders placed — %s",
                strategy, prev_runs + 1, telegram_id, reason,
            )

    # NO_ORDERS_AUDIT-FIX-DIAG: one services-stream line per grid-family cycle
    # carrying the decisive "why no orders" facts (gate verdict/reason, candle
    # feed, mid, active executors, spawn-refusal reason). The controllers log
    # these under engine.controllers; surface them here so a silent no-orders
    # run is pinpointable from the same log stream as the cycle messages.
    _diag = result.get("engine_diag") if isinstance(result, dict) else None
    if _diag:
        # Render controller-specific telemetry as "n/a" when the controller
        # doesn't expose it (Momentum/taker & Mid have no candle/mid/phase/vr),
        # and always name the controller — so a "no orders" line is read
        # against the RIGHT controller instead of misdiagnosed as a starved
        # grid from getattr defaults.
        def _dv(key):
            return _diag[key] if key in _diag else "n/a"
        logger.info(
            "engine_diag user=%s strategy=%s controller=%s active=%s gate=%s/%s "
            "candles=%s mid=%s phase=%s vr=%s spawn_refused=%s "
            "move_bp=%s reset_bp=%s anchor=%s",
            telegram_id, strategy, _diag.get("controller") or "?",
            _diag.get("active_executors"),
            _diag.get("gate_verdict"), _diag.get("gate_reason") or "-",
            _dv("candle_count"), _dv("mid"), _dv("phase"),
            _dv("variance_ratio"), _diag.get("spawn_refused") or "-",
            _dv("move_bp"), _dv("reset_bp"), _dv("anchor"),
        )

    # Increment strategy session metrics from cycle result
    session_id = state.get("strategy_session_id")
    if session_id and result.get("success", True):
        try:
            await run_blocking(
                increment_session_metrics,
                int(session_id),
                cycles=1,
                orders_placed=orders_placed,
                orders_filled=int(result.get("orders_filled", 0)),
                orders_cancelled=int(result.get("orders_cancelled", 0)),
                volume=float(
                    result.get("placed_notional_usd", 0)
                    or result.get("cycle_placed_notional_usd", 0)
                    or 0
                ),
            )
        except Exception as e:
            logger.warning(
                "session metrics not recorded — cycle/PnL/volume counters will undercount: %s",
                e,
            )
        # Engine strategies don't carry fill data on the cycle result (fills are
        # bridged to trades_<network> by DbTradeRecorder). Periodically refresh
        # the running session's volume/fees/PnL/funding from the trade rows so
        # the session row stays live mid-run instead of only catching up at
        # finalize. Throttled (every 6th cycle) and offloaded via run_blocking
        # so we don't add a per-cycle synchronous DB round-trip to the event
        # loop — /mm_status reads live numbers on-demand regardless, and
        # _finalize_session always does the authoritative pass at stop.
        try:
            from src.nadobro.strategy.engine_runtime import ENGINE_MAPPED_STRATEGIES

            if (
                prev_runs % 6 == 0
                and _normalize_strategy_id(str(state.get("strategy") or "")) in ENGINE_MAPPED_STRATEGIES
            ):
                _net = _resolve_session_network(state)
                _sid = int(session_id)

                def _live_session_refresh():
                    from src.nadobro.models.database import (
                        rollup_engine_session_pnl_funding,
                        rollup_session_from_trades,
                    )

                    rollup_session_from_trades(_sid, _net)
                    rollup_engine_session_pnl_funding(_sid, _net)

                await run_blocking(_live_session_refresh)
        except Exception as e:
            logger.debug("live engine session refresh skipped #%s: %s", session_id, e)

    if not result.get("success", True):
        error_msg = _format_cycle_failure_error(strategy, result)
        return False, error_msg
    return True, None
