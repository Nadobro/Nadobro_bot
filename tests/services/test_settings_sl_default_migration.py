"""One-time migration of the grid-family default session SL to 5% (owner decision
2026-09-27). Saved settings carry the merged defaults, so a stored OLD default
(grid 0.5%, rgrid/dgrid 0.8%) moves to GRID_FAMILY_DEFAULT_SL_PCT; any custom
value is kept; the marker makes it apply exactly once per settings key."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy.strategy_registry import GRID_FAMILY_DEFAULT_SL_PCT  # noqa: E402
from src.nadobro.users import settings_service as ss  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    """In-memory bot_state + a mainnet user; returns the backing dict."""
    data: dict[str, str] = {}
    monkeypatch.setattr(ss, "get_bot_state_raw", lambda key: data.get(key))
    monkeypatch.setattr(ss, "set_bot_state", lambda key, value: data.__setitem__(key, json.dumps(value)))
    monkeypatch.setattr(
        ss, "get_user", lambda tid: SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
    )
    return data


def _saved_blob(grid_sl=0.5, rgrid_sl=0.8, dgrid_sl=0.8, *, migrations=None) -> dict:
    blob = {
        "default_leverage": 3.0,
        "strategies": {
            "grid": {"sl_pct": grid_sl, "tp_pct": 0.6, "notional_usd": 75.0},
            "rgrid": {"sl_pct": rgrid_sl, "rgrid_stop_loss_pct": rgrid_sl, "rgrid_take_profit_pct": 1.2},
            "dgrid": {"sl_pct": dgrid_sl, "rgrid_stop_loss_pct": dgrid_sl, "tp_pct": 1.2},
            "mid": {"sl_pct": 0.5, "tp_pct": 0.6},
        },
    }
    if migrations is not None:
        blob["migrations"] = migrations
    return blob


def test_old_defaults_move_to_five_percent(store):
    store["user_settings:1:mainnet"] = json.dumps(_saved_blob())
    _net, settings = ss.get_user_settings(1)
    st = settings["strategies"]
    assert st["grid"]["sl_pct"] == GRID_FAMILY_DEFAULT_SL_PCT
    for sid in ("rgrid", "dgrid"):
        assert st[sid]["sl_pct"] == GRID_FAMILY_DEFAULT_SL_PCT
        assert st[sid]["rgrid_stop_loss_pct"] == GRID_FAMILY_DEFAULT_SL_PCT
    # Everything else untouched, incl. Mid's own 0.5% and the TPs.
    assert st["mid"]["sl_pct"] == 0.5
    assert st["grid"]["tp_pct"] == 0.6 and st["rgrid"]["rgrid_take_profit_pct"] == 1.2
    assert ss.GRID_FAMILY_SL_MIGRATION in settings["migrations"]


def test_custom_values_are_kept_and_keys_are_independent(store):
    blob = _saved_blob(grid_sl=2.0, rgrid_sl=10.0, dgrid_sl=0.8)
    blob["strategies"]["dgrid"]["sl_pct"] = 3.0  # only rgrid_stop_loss_pct is the old default
    store["user_settings:1:mainnet"] = json.dumps(blob)
    _net, settings = ss.get_user_settings(1)
    st = settings["strategies"]
    assert st["grid"]["sl_pct"] == 2.0
    assert st["rgrid"]["sl_pct"] == 10.0 and st["rgrid"]["rgrid_stop_loss_pct"] == 10.0
    assert st["dgrid"]["sl_pct"] == 3.0
    assert st["dgrid"]["rgrid_stop_loss_pct"] == GRID_FAMILY_DEFAULT_SL_PCT


def test_string_and_malformed_values_are_safe(store):
    blob = _saved_blob()
    blob["strategies"]["grid"]["sl_pct"] = "0.5"
    blob["strategies"]["rgrid"]["sl_pct"] = "not-a-number"
    blob["strategies"]["rgrid"]["rgrid_stop_loss_pct"] = None
    store["user_settings:1:mainnet"] = json.dumps(blob)
    _net, settings = ss.get_user_settings(1)
    st = settings["strategies"]
    assert st["grid"]["sl_pct"] == GRID_FAMILY_DEFAULT_SL_PCT
    assert st["rgrid"]["sl_pct"] == "not-a-number"
    assert st["rgrid"]["rgrid_stop_loss_pct"] is None


def test_applies_once_then_an_explicit_old_value_is_kept(store):
    store["user_settings:1:mainnet"] = json.dumps(_saved_blob())
    # Re-loading before any save re-applies the same upgrade (idempotent).
    assert ss.get_user_settings(1)[1]["strategies"]["grid"]["sl_pct"] == GRID_FAMILY_DEFAULT_SL_PCT
    assert ss.get_user_settings(1)[1]["strategies"]["grid"]["sl_pct"] == GRID_FAMILY_DEFAULT_SL_PCT

    # The user's next save persists the marker with the upgraded values ...
    def _pick_old_value(s):
        s["strategies"]["grid"]["sl_pct"] = 0.5  # a deliberate post-migration choice

    ss.update_user_settings(1, _pick_old_value)
    saved = json.loads(store["user_settings:1:mainnet"])
    assert ss.GRID_FAMILY_SL_MIGRATION in saved["migrations"]
    # ... so the deliberate 0.5% survives every later load.
    assert ss.get_user_settings(1)[1]["strategies"]["grid"]["sl_pct"] == 0.5


def test_marked_blob_is_never_migrated(store):
    store["user_settings:1:mainnet"] = json.dumps(
        _saved_blob(migrations=[ss.GRID_FAMILY_SL_MIGRATION])
    )
    st = ss.get_user_settings(1)[1]["strategies"]
    assert st["grid"]["sl_pct"] == 0.5
    assert st["rgrid"]["rgrid_stop_loss_pct"] == 0.8


def test_new_user_is_born_migrated(store):
    # No saved blob: defaults are already 5% and carry the marker, so a later
    # explicit 0.5% saved by this user is never "migrated" away.
    _net, settings = ss.get_user_settings(1)
    assert settings["strategies"]["grid"]["sl_pct"] == GRID_FAMILY_DEFAULT_SL_PCT
    assert ss.GRID_FAMILY_SL_MIGRATION in settings["migrations"]

    ss.update_user_settings(1, lambda s: s["strategies"]["grid"].__setitem__("sl_pct", 0.5))
    assert ss.get_user_settings(1)[1]["strategies"]["grid"]["sl_pct"] == 0.5
