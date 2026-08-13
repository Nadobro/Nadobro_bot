"""CLICK-PATH: opening a strategy card must not wait on the venue.

The preview used to call get_balance, get_market_price, get_funding_rate
(all products), verify_linked_signer, a live session snapshot, and an
archive volume POST — sequentially — which is why Strategy Lab felt as
stuck as Portfolio after a restart.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import strategy_handler
from src.nadobro.models.database import UserRow
from src.nadobro.users import user_service


def test_cached_user_balance_never_hits_the_gateway(monkeypatch):
    strategy_handler._balance_cache.clear()
    client = MagicMock()
    client.get_balance.return_value = {"exists": True, "balances": {0: 10.0}}
    monkeypatch.setattr(strategy_handler, "get_user_readonly_client", lambda tid: client)
    assert strategy_handler._cached_user_balance(1)["exists"] is True
    client.get_balance.assert_called_once_with(cache_only=True)


def test_cached_user_balance_warms_off_thread_on_pending(monkeypatch):
    strategy_handler._balance_cache.clear()
    client = MagicMock()
    client.get_balance.return_value = {"exists": False, "balances": {}, "pending": True}
    warm = MagicMock()
    monkeypatch.setattr(strategy_handler, "get_user_readonly_client", lambda tid: client)
    monkeypatch.setattr("src.nadobro.handlers.home_card._warm_balance_async", warm)
    assert strategy_handler._cached_user_balance(1) == {}
    warm.assert_called_once_with(client)


def test_preview_skips_on_chain_signer_check(monkeypatch):
    user = UserRow({
        "telegram_id": 1,
        "main_address": "0x" + "1" * 40,
        "linked_signer_address": "0x" + "2" * 40,
        "encrypted_linked_signer_pk": "pk",
        "network_mode": "mainnet",
    })
    ro = MagicMock()
    monkeypatch.setattr(user_service, "get_user", lambda tid: user)
    monkeypatch.setattr(user_service, "get_user_readonly_client", lambda *a, **k: ro)
    ready, _msg = user_service.ensure_active_wallet_ready(1, verify_on_chain=False)
    assert ready is True
    ro.verify_linked_signer.assert_not_called()


def test_strategy_preview_price_and_funding_are_cache_only(monkeypatch):
    strategy_handler._balance_cache.clear()
    client = MagicMock()
    client.get_balance.return_value = {"exists": True, "balances": {0: 50.0}}
    client.get_market_price.return_value = {"bid": 1, "ask": 1, "mid": 1}
    client.get_funding_rate.return_value = {"funding_rate": 0.0}

    monkeypatch.setattr(strategy_handler, "get_user_readonly_client", lambda *a, **k: client)
    monkeypatch.setattr(
        strategy_handler,
        "get_user",
        lambda tid: SimpleNamespace(network_mode=SimpleNamespace(value="mainnet")),
    )
    monkeypatch.setattr(
        strategy_handler,
        "get_user_settings",
        lambda tid: ("mainnet", {"strategies": {"grid": {}}, "default_leverage": 3}),
    )
    monkeypatch.setattr(strategy_handler, "get_user_bot_status", lambda tid: {})
    monkeypatch.setattr(
        strategy_handler,
        "ensure_active_wallet_ready",
        lambda *a, **k: (True, ""),
    )
    monkeypatch.setattr(
        strategy_handler,
        "get_user_wallet_info",
        lambda *a, **k: {"active_address": "0xabc"},
    )
    monkeypatch.setattr(strategy_handler, "get_product_id", lambda *a, **k: 2)
    monkeypatch.setattr(
        "src.nadobro.models.database.get_strategy_sessions_by_user",
        lambda *a, **k: [],
    )

    text = strategy_handler._build_strategy_preview_text(1, "grid", "BTC")
    assert "GRID" in text
    client.get_balance.assert_called_with(cache_only=True)
    client.get_market_price.assert_called_with(2, cache_only=True)
    client.get_funding_rate.assert_called_with(2, cache_only=True)


def test_mm_breakdown_volume_is_cache_only(monkeypatch):
    volume = MagicMock(return_value=None)
    monkeypatch.setattr(
        strategy_handler,
        "get_user_settings",
        lambda tid: ("mainnet", {
            "strategies": {"grid": {"participation_preset": "medium"}},
            "default_leverage": 3,
        }),
    )
    monkeypatch.setattr(strategy_handler, "_cached_user_balance", lambda tid: {})
    monkeypatch.setattr(strategy_handler, "get_product_id", lambda *a, **k: 2)
    monkeypatch.setattr(
        "src.nadobro.venue.nado_archive.get_pair_24h_volume_usd",
        volume,
    )
    monkeypatch.setattr(
        "src.nadobro.strategy.mm_dashboard.build_pretrade_breakdown",
        lambda **k: {},
    )
    monkeypatch.setattr(
        "src.nadobro.strategy.mm_dashboard.render_pretrade_card_lines",
        lambda breakdown: ["ok"],
    )
    strategy_handler._append_mm_pretrade_breakdown(1, "grid", "BTC", "BASE")
    volume.assert_called_once()
    assert volume.call_args.kwargs.get("cache_only") is True
