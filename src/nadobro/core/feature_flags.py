"""Environment-backed feature flags for additive Nadobro features."""

from __future__ import annotations

import logging
import math
import os

from src.nadobro.utils.env import env_bool, env_float, env_int, env_int_set, env_str

logger = logging.getLogger(__name__)


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


def match_ledger_backfill_enabled() -> bool:
    """Whether nado_sync backfills OLDER fills so the account realized-PnL replay
    has complete per-product history.

    ``get_matches`` only returns the newest 200 fills; a truncated ledger makes
    the avg-cost replay fabricate PnL from a phantom entry basis. This pages
    backward a few pages per heavy sync (bounded, one-time per account). Default
    ON; operators can disable with the env flag if it strains venue rate limits.
    """
    return env_flag("NADO_MATCH_LEDGER_BACKFILL", True)


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


def lowiqpts_relay_poll_enabled() -> bool:
    """Background 2s @lowiqpts event poll. Default OFF.

    A hung poll occupied APScheduler ``max_instances=1`` and skip-warned every
    2s, drowning Fly logs and starving other ticks. Points refresh on tap still
    talks to the relay; only the always-on poller is gated. Set
    ``LOWIQPTS_RELAY_POLL_ENABLED=1`` to turn it back on.
    """
    return env_flag("LOWIQPTS_RELAY_POLL_ENABLED", False)


def arcus_enabled() -> bool:
    """Arcus multi-venue master switch. Default OFF.

    Phase 1 is plumbing only (no Arcus client, no Arcus trading). While this is
    off and no user is already on Arcus, neither the venue gate nor /venue is
    registered, so production is byte-identical. Venues run in parallel: this
    flag never stops, cancels or resumes anything on Nado.
    """
    return env_flag("ARCUS_ENABLED", False)


def arcus_allowed_user_ids() -> frozenset[int]:
    """Telegram ids that may switch to Arcus (comma list). Empty, the default, = nobody."""
    return env_int_set("ARCUS_ALLOWED_USER_IDS")


def arcus_enabled_for(telegram_id: int | None) -> bool:
    """True only when the flag is on AND the user is on the allowlist. Fail-closed:
    anything but an int (or a decimal-int string) is refused — never ``int()``-folded,
    so ``True`` or ``123.9`` can never borrow an allowlisted id."""
    if isinstance(telegram_id, bool) or not isinstance(telegram_id, (int, str)):
        return False
    if not arcus_enabled():
        return False
    try:
        uid = int(telegram_id)
    except (TypeError, ValueError):
        return False
    return uid in arcus_allowed_user_ids()


# --- Arcus venue library readers (P2) ---------------------------------------
# Every reader re-reads the env on each call (no caching). An out-of-range or
# non-finite numeric value is clamped (or defaulted) with ONE WARNING per
# reader per process — visible, never a silent override.

ARCUS_V1_MARKET_ALLOWLIST_DEFAULT = "BTC-USD,ETH-USD,SOL-USD"

_ARCUS_CLAMP_WARNED: set[str] = set()


def _arcus_warn_once(name: str, raw: object, used: object) -> None:
    if name in _ARCUS_CLAMP_WARNED:
        return
    _ARCUS_CLAMP_WARNED.add(name)
    logger.warning("env %s=%r is out of range; using %r", name, raw, used)


def _arcus_int_in_range(name: str, default: int, lo: int, hi: int) -> int:
    value = env_int(name, default)
    clamped = max(lo, min(hi, value))
    if clamped != value:
        _arcus_warn_once(name, value, clamped)
    return clamped


def _arcus_float_in_range(name: str, default: float, lo: float, hi: float) -> float:
    value = env_float(name, default)
    if not math.isfinite(value):
        _arcus_warn_once(name, value, default)
        return default
    clamped = max(lo, min(hi, value))
    if clamped != value:
        _arcus_warn_once(name, value, clamped)
    return clamped


def _reset_arcus_flag_warnings_for_tests() -> None:
    _ARCUS_CLAMP_WARNED.clear()


def arcus_mainnet_enabled() -> bool:
    """Arcus MAINNET switch. Default OFF.

    Requires the master switch too: ``ARCUS_MAINNET_ENABLED`` alone (with
    ``ARCUS_ENABLED`` off) is False, so the mainnet flag can never open Arcus
    mainnet traffic, linking or starts by itself. Arcus network mode defaults
    to testnet; brakes never read this flag.
    """
    return arcus_enabled() and env_flag("ARCUS_MAINNET_ENABLED", False)


def arcus_force_ipv4() -> bool:
    """Pin Arcus REST/WS connections to IPv4 (a whitelisted static egress). Default OFF."""
    return env_flag("ARCUS_FORCE_IPV4", False)


def arcus_market_allowlist() -> frozenset[str]:
    """Upper-cased tickers from ``ARCUS_MARKET_ALLOWLIST`` (comma list; blanks
    dropped). Default BTC-USD, ETH-USD, SOL-USD. The catalog intersects this
    with the v1 market set, so the env can only SHRINK what is tradable."""
    raw = env_str("ARCUS_MARKET_ALLOWLIST", ARCUS_V1_MARKET_ALLOWLIST_DEFAULT)
    return frozenset(t.strip().upper() for t in raw.split(",") if t.strip())


def arcus_ip_l0_reserve() -> int:
    """IP-weight units reserved for the L0 brake lane. Default 300, range [0, 1000]."""
    return _arcus_int_in_range("ARCUS_IP_L0_RESERVE", 300, 0, 1000)


def arcus_catalog_refresh_s() -> float:
    """Seconds between Arcus market-catalog refreshes. Default 60, range [15, 3600]."""
    return _arcus_float_in_range("ARCUS_CATALOG_REFRESH_S", 60.0, 15.0, 3600.0)


def arcus_catalog_max_age_s() -> float:
    """Catalog snapshot age after which callers refuse (stale). Default 300, range [60, 3600]."""
    return _arcus_float_in_range("ARCUS_CATALOG_MAX_AGE_S", 300.0, 60.0, 3600.0)


def arcus_gtt_days() -> int:
    """goodTilTime horizon in days for every Arcus order. Default 40, range [32, 180]
    (the venue rejects a GTT less than one month ahead)."""
    return _arcus_int_in_range("ARCUS_GTT_DAYS", 40, 32, 180)


def arcus_clock_max_age_s() -> float:
    """Max age of the /v1/time offset before an OPENING placement re-syncs.
    Default 900, range [120, 3600]."""
    return _arcus_float_in_range("ARCUS_CLOCK_MAX_AGE_S", 900.0, 120.0, 3600.0)
