"""CLICK-PATH: portfolio taps must never await a live venue snapshot.

Cold cache must render a real deck immediately. A "Loading…" card with no
escape was leaving users stuck for minutes while the indexer sync ran.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import portfolio_handler


def _parse_mode():
    return SimpleNamespace(HTML="HTML")


def test_overview_cold_start_renders_deck_not_loading():
    async def body():
        query = MagicMock()
        user = SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
        with patch.object(portfolio_handler, "get_user", return_value=user), \
             patch.object(portfolio_handler, "_cached_snapshot", return_value=None), \
             patch.object(portfolio_handler, "_edit_loc", new_callable=AsyncMock) as edit, \
             patch.object(portfolio_handler, "_spawn_background_refresh") as spawn, \
             patch.object(portfolio_handler, "ParseMode", _parse_mode()), \
             patch(
                 "src.nadobro.handlers.portfolio_deck.render_portfolio_deck",
                 return_value=("DECK", "KB"),
             ) as render:
            await portfolio_handler._handle_portfolio(query, "portfolio:view", 42)
        spawn.assert_called_once()
        assert spawn.call_args.kwargs.get("force") in (False, None)
        assert render.call_args.kwargs.get("refreshing") is True
        assert edit.await_args.args[1] == "DECK"
        assert "Loading" not in edit.await_args.args[1]

    asyncio.run(body())


def test_positions_cold_start_does_not_await_snapshot():
    async def body():
        query = MagicMock()
        user = SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
        with patch.object(portfolio_handler, "get_user", return_value=user), \
             patch.object(portfolio_handler, "_cached_snapshot", return_value=None), \
             patch.object(portfolio_handler, "_edit_loc", new_callable=AsyncMock), \
             patch.object(portfolio_handler, "_spawn_background_refresh") as spawn, \
             patch.object(portfolio_handler, "ParseMode", _parse_mode()), \
             patch(
                 "src.nadobro.handlers.positions_view.render_positions_view",
                 return_value=("POS", "KB"),
             ):
            await portfolio_handler._handle_portfolio(query, "portfolio:positions", 42)
        spawn.assert_called_once()
        assert spawn.call_args.kwargs.get("force") in (False, None)

    asyncio.run(body())


def test_cached_overview_renders_without_spawning_when_fresh():
    async def body():
        query = MagicMock()
        user = SimpleNamespace(network_mode=SimpleNamespace(value="mainnet"))
        cached = {"monotonic_ts": 9e18, "positions": [], "open_orders": [], "network": "mainnet"}
        with patch.object(portfolio_handler, "get_user", return_value=user), \
             patch.object(portfolio_handler, "_cached_snapshot", return_value=cached), \
             patch.object(portfolio_handler, "_edit_loc", new_callable=AsyncMock) as edit, \
             patch.object(portfolio_handler, "_spawn_background_refresh") as spawn, \
             patch.object(portfolio_handler, "ParseMode", _parse_mode()), \
             patch(
                 "src.nadobro.handlers.portfolio_deck.render_portfolio_deck",
                 return_value=("DECK", "KB"),
             ):
            await portfolio_handler._handle_portfolio(query, "portfolio:view", 42)
        spawn.assert_not_called()
        edit.assert_awaited()
        assert edit.await_args.args[1] == "DECK"

    asyncio.run(body())
