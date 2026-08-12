"""Environment-backed feature flags for additive Nadobro features."""

from __future__ import annotations

import os

from src.nadobro.utils.env import env_bool


def env_flag(name: str, default: bool = False) -> bool:
    """Return a boolean env flag using the project's existing truthy style.

    Thin alias kept for backwards compatibility; delegates to the shared
    :func:`src.nadobro.utils.env.env_bool` so truthy semantics live in one
    place.
    """
    return env_bool(name, default)


def legacy_bro_autoloop_enabled() -> bool:
    return env_flag("NADO_LEGACY_BRO_AUTOLOOP", False)


def portfolio_sync_enabled() -> bool:
    # Default ON per the workflow plan so the combined Positions screen
    # tracks fills in near real time without requiring an environment
    # override. Operators can disable by setting the env flag to 0.
    return env_flag("NADO_PORTFOLIO_SYNC", True)


def portfolio_ws_enabled() -> bool:
    return env_flag("NADO_PORTFOLIO_WS", False)


def fill_nudge_enabled() -> bool:
    """Whether venue fills should wake engine strategy cycles immediately.

    This is intentionally independent from the broader portfolio-WS rollout:
    the runtime can subscribe to the fill stream only while portfolio cache
    invalidation remains disabled.
    """
    return env_flag("NADO_FILL_NUDGE", True)


def strategy_scheduler_enabled() -> bool:
    return env_flag("NADO_STRATEGY_SCHEDULER", True)


def portfolio_reconcile_seconds() -> int:
    raw = (os.environ.get("NADO_WS_RECONCILE_SECONDS") or "300").strip()
    try:
        return max(60, int(float(raw)))
    except ValueError:
        return 300


def vault_deposit_watch_enabled() -> bool:
    return env_flag("NADO_VAULT_DEPOSIT_WATCH", True)


def vault_deposit_watch_interval_seconds() -> int:
    raw = (os.environ.get("NADO_VAULT_DEPOSIT_WATCH_SECONDS") or "60").strip()
    try:
        return max(30, int(float(raw)))
    except ValueError:
        return 60


def portfolio_sync_interval_seconds() -> int:
    # Default 30s: keeps Positions reasonably fresh without flooding gateway/
    # archive when many users are active. Strategies/copy/vault use separate loops.
    raw = (os.environ.get("NADO_PORTFOLIO_SYNC_SECONDS") or "30").strip()
    try:
        return max(15, int(float(raw)))
    except ValueError:
        return 30


def portfolio_sync_users_per_tick() -> int:
    """How many active users to sync per scheduler tick (cursor page size)."""
    raw = (os.environ.get("NADO_PORTFOLIO_SYNC_USERS_PER_TICK") or "8").strip()
    try:
        return max(1, min(50, int(float(raw))))
    except ValueError:
        return 8


def portfolio_poll_cache_seconds() -> int:
    """Skip re-fetching a user during background poll if synced within this window."""
    raw = (os.environ.get("NADO_PORTFOLIO_POLL_CACHE_SECONDS") or "45").strip()
    try:
        return max(15, int(float(raw)))
    except ValueError:
        return 45


def portfolio_heavy_sync_seconds() -> int:
    """Matches/funding archive calls run at most this often per user during poll."""
    raw = (os.environ.get("NADO_PORTFOLIO_HEAVY_SYNC_SECONDS") or "300").strip()
    try:
        return max(60, int(float(raw)))
    except ValueError:
        return 300


def dgrid_intelligence_enabled() -> bool:
    """Master switch for the D-Grid intelligence upgrade.

    When True, mm_bot.run_cycle activates the regime classifier
    (_regime.py), the adaptive layer-sizing engine (_layer_sizing.py), and
    the active position manager (_position_manager.py) for any session whose
    ``strategy == "dgrid"``. Individual sessions can override by setting
    ``state["dgrid_intelligence_enabled"]`` (True/False).
    """
    return env_flag("NADO_DGRID_INTELLIGENCE", False)
