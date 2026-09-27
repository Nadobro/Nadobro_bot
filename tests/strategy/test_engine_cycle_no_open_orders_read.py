"""An engine strategy cycle must not pay for an open-orders read it never uses
(audit 2026-09-27, efficiency_audit F5 / E1).

``_run_cycle`` gathered ``client.get_open_orders(product_id)`` alongside the mid
for every engine strategy, but the result only ever reaches the legacy
``_dispatch_strategy`` branch — unreachable for the engine-mapped set. It was a
refresh=False read whose only side effect was warming the 5s client cache, which
the engine adapter never reads (its polls are refresh=True). Cost: 2 gateway
weight per cycle per user — ~48% of a D-Grid session's gateway weight.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy import bot_runtime  # noqa: E402


class _CountingClient:
    def __init__(self):
        self.open_orders_calls = 0

    def get_market_price(self, _product_id):
        return {"mid": 100.0}

    def get_open_orders(self, *_a, **_k):
        self.open_orders_calls += 1
        return []


def _drive(strategy: str, *, engine_mapped: bool):
    client = _CountingClient()
    state = {
        "running": True, "strategy": strategy, "product": "BTC", "reference_price": 100.0,
        "sl_pct": 0.0, "tp_pct": 0.0, "notional_usd": 100.0, "interval_seconds": 1,
        "last_run_ts": 0.0,
    }
    fake_user = SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
    dispatched: dict = {}

    async def _run_blocking_stub(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def _engine_cycle(*_a, **_k):
        return {"success": True, "action": "engine_ticked", "strategy": strategy}

    def _dispatch(*args, **_k):
        dispatched["open_orders"] = args[-1]
        return {"success": True, "orders_placed": 0}

    patches = [
        patch.object(bot_runtime, "is_trading_paused", return_value=False),
        patch.object(bot_runtime, "run_blocking", side_effect=_run_blocking_stub),
        patch.object(bot_runtime, "get_user", return_value=fake_user),
        patch.object(bot_runtime, "get_user_readonly_client", return_value=client),
        patch.object(bot_runtime, "get_user_nado_client", return_value=client),
        patch("src.nadobro.strategy.engine_runtime.engine_v2_enabled", return_value=engine_mapped),
        patch("src.nadobro.strategy.engine_runtime.run_engine_cycle", side_effect=_engine_cycle),
        patch.object(bot_runtime, "_dispatch_strategy", side_effect=_dispatch),
        patch("src.nadobro.users.settings_service.get_strategy_settings",
              return_value=("mainnet", {})),
        patch("src.nadobro.trading.session_resolver.resolve_current_strategy_session",
              return_value=None),
        patch.object(bot_runtime, "_finalize_session"),
        patch.object(bot_runtime, "_save_state"),
        patch.object(bot_runtime, "_notify"),
    ]
    if not engine_mapped:
        patches.append(patch("src.nadobro.strategy.engine_runtime.ENGINE_MAPPED_STRATEGIES", set()))
    for p in patches:
        p.start()
    try:
        asyncio.run(bot_runtime._run_cycle(7, "mainnet", state))
    finally:
        for p in reversed(patches):
            p.stop()
    return client, dispatched


@pytest.mark.parametrize("strategy", ["grid", "rgrid", "dgrid", "mid"])
def test_engine_cycle_makes_no_runtime_open_orders_read(strategy):
    client, dispatched = _drive(strategy, engine_mapped=True)
    assert client.open_orders_calls == 0, (
        f"{strategy}: the engine cycle paid for an unused get_open_orders read"
    )
    assert "open_orders" not in dispatched       # the engine path never dispatches legacy


def test_legacy_dispatch_still_receives_the_open_orders():
    """The non-engine dispatch path still gets a fresh open-orders list."""
    client, dispatched = _drive("grid", engine_mapped=False)
    assert client.open_orders_calls == 1
    assert dispatched.get("open_orders") == []
