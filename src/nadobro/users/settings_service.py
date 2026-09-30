import json
from datetime import datetime

from src.nadobro.models.database import get_bot_state_raw, set_bot_state
from src.nadobro.strategy.strategy_registry import (
    GRID_FAMILY_DEFAULT_SL_PCT,
    MARKET_MAKING_STRATEGIES,
    normalize_strategy_id,
    settings_strategy_defaults,
)
from src.nadobro.users.user_service import get_user

SETTINGS_PREFIX = "user_settings:"

# One-time, owner-approved (2026-09-27) migration of the grid-family default
# session stop-loss to GRID_FAMILY_DEFAULT_SL_PCT. Saved settings are persisted
# WITH the merged defaults, so a user who ever edited any setting carries the OLD
# default (grid 0.5%, rgrid/dgrid 0.8%) and it is indistinguishable from a
# choice; the owner decided those values move to the new default. Any other
# value (a custom SL) is kept. The marker makes it apply exactly once per
# settings key: a user who picks 0.5% again after the migration keeps it.
GRID_FAMILY_SL_MIGRATION = "grid_family_sl_5pct_2026_09"
_OLD_GRID_FAMILY_SL_DEFAULTS: dict[str, dict[str, float]] = {
    "grid": {"sl_pct": 0.5},
    "rgrid": {"sl_pct": 0.8, "rgrid_stop_loss_pct": 0.8},
    "dgrid": {"sl_pct": 0.8, "rgrid_stop_loss_pct": 0.8},
}


def _migrate_grid_family_sl_default(strategies: dict) -> bool:
    """Replace a stored grid-family SL that exactly equals its OLD default with
    GRID_FAMILY_DEFAULT_SL_PCT, key by key. Returns True if anything changed.
    Pure (in-memory); the caller records the marker and the next save persists."""
    changed = False
    if not isinstance(strategies, dict):
        return False
    for sid, old_keys in _OLD_GRID_FAMILY_SL_DEFAULTS.items():
        cfg = strategies.get(sid)
        if not isinstance(cfg, dict):
            continue
        for key, old_val in old_keys.items():
            try:
                current = float(cfg.get(key))
            except (TypeError, ValueError):
                continue
            if abs(current - old_val) < 1e-9:
                cfg[key] = GRID_FAMILY_DEFAULT_SL_PCT
                changed = True
    return changed


def _settings_key(telegram_id: int, network: str) -> str:
    return f"{SETTINGS_PREFIX}{telegram_id}:{network}"


def _default_strategy_settings() -> dict:
    return settings_strategy_defaults()


def _default_settings() -> dict:
    return {
        "default_leverage": 3.0,
        "slippage": 1.0,
        "risk_profile": "balanced",
        "strategies": _default_strategy_settings(),
        # A brand-new blob already carries the new defaults, so it is born
        # migrated — otherwise a later explicit 0.5% would be "migrated" away.
        "migrations": [GRID_FAMILY_SL_MIGRATION],
    }


def _looks_like_rgrid_config(cfg: dict) -> bool:
    if not isinstance(cfg, dict):
        return False
    if any(k.startswith("rgrid_") for k in cfg.keys()):
        return True
    legacy_keys = {"grid_spread_bp", "grid_stop_loss_pct", "grid_take_profit_pct", "grid_reset_threshold_pct", "grid_discretion"}
    return any(k in cfg for k in legacy_keys)


def _looks_like_grid_config(cfg: dict) -> bool:
    if not isinstance(cfg, dict):
        return False
    marker_keys = {"threshold_bp", "close_offset_bp", "reference_mode", "directional_bias", "min_spread_bp", "max_spread_bp"}
    return any(k in cfg for k in marker_keys)


def _normalize_strategy_id(strategy: str) -> str:
    return normalize_strategy_id(strategy)


def _migrate_loaded_strategies(loaded_strats: dict) -> dict:
    migrated = dict(loaded_strats or {})
    mm_cfg = migrated.get("mm") if isinstance(migrated.get("mm"), dict) else None
    grid_cfg = migrated.get("grid") if isinstance(migrated.get("grid"), dict) else None
    rgrid_cfg = migrated.get("rgrid") if isinstance(migrated.get("rgrid"), dict) else None

    # Requested migration policy:
    # - mm -> grid
    # - legacy grid (reverse-grid payload) -> rgrid
    if mm_cfg is not None:
        migrated["grid"] = dict(mm_cfg)
    if rgrid_cfg is None and grid_cfg is not None:
        # Legacy "grid" strategy payloads were reverse-grid style.
        if _looks_like_rgrid_config(grid_cfg):
            migrated["rgrid"] = dict(grid_cfg)
            if mm_cfg is None and "grid" in migrated:
                migrated.pop("grid", None)

    migrated.pop("mm", None)
    return migrated


def get_user_settings(telegram_id: int) -> tuple[str, dict]:
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    key = _settings_key(telegram_id, network)
    settings = _default_settings()
    raw = get_bot_state_raw(key)
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                settings.update(loaded)
                default_strats = _default_strategy_settings()
                loaded_strats = _migrate_loaded_strategies(loaded.get("strategies", {}))
                if isinstance(loaded_strats, dict):
                    for sid, base in default_strats.items():
                        if sid in loaded_strats and isinstance(loaded_strats[sid], dict):
                            base.update(loaded_strats[sid])
                    settings["strategies"] = default_strats
                # Judge the marker from the LOADED blob (the defaults dict above
                # carries it for new users and would otherwise mask an old blob).
                # Until the user's next save persists the marker, every load
                # re-applies the same in-memory upgrade — idempotent, and no
                # post-migration explicit choice can exist without that save.
                loaded_migrations = loaded.get("migrations")
                loaded_migrations = list(loaded_migrations) if isinstance(loaded_migrations, list) else []
                if GRID_FAMILY_SL_MIGRATION not in loaded_migrations:
                    _migrate_grid_family_sl_default(settings.get("strategies"))
                    loaded_migrations.append(GRID_FAMILY_SL_MIGRATION)
                settings["migrations"] = loaded_migrations
        except Exception:
            pass
    return network, settings


def save_user_settings(telegram_id: int, network: str, settings: dict):
    key = _settings_key(telegram_id, network)
    set_bot_state(key, settings)


def update_user_settings(telegram_id: int, mutator):
    network, settings = get_user_settings(telegram_id)
    mutator(settings)
    save_user_settings(telegram_id, network, settings)
    return network, settings


def sync_cycle_notional_with_margin(strategies: dict, strategy_id: str) -> None:
    """When margin (notional_usd) changes, keep per-cycle budget aligned for MM/Grid."""
    strategy_id = _normalize_strategy_id(strategy_id)
    if strategy_id not in MARKET_MAKING_STRATEGIES:
        return
    cfg = strategies.get(strategy_id)
    if not isinstance(cfg, dict):
        return
    if "notional_usd" not in cfg:
        return
    try:
        n = float(cfg["notional_usd"])
    except (TypeError, ValueError):
        return
    cfg["notional_usd"] = n
    cfg["cycle_notional_usd"] = n


def get_strategy_settings(telegram_id: int, strategy: str) -> tuple[str, dict]:
    network, settings = get_user_settings(telegram_id)
    strategies = settings.get("strategies", {})
    strategy = _normalize_strategy_id(strategy)
    strat = strategies.get(strategy, _default_strategy_settings().get(strategy, {}))
    return network, strat
