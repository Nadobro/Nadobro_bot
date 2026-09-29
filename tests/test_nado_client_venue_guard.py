"""Nado chokepoints refuse a non-Nado (Arcus) scope — and nothing else changes.

Arcus P1 / AD-11 layer 6. Every place a network value becomes a Nado client,
a Nado catalog read or a Nado product lookup raises VenueScopeError for an
``arcus_*`` scope, BEFORE any try/except that would have swallowed it into
"no client" or static Nado data. For every other value the chokepoint is
untouched (same object, same cache key, same fallback).
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from src.nadobro.utils.venue_scope import VenueScopeError

ARCUS_VALUES = ["arcus_mainnet", "arcus_testnet", "ARCUS_MAINNET", " arcus_mainnet"]
NADO_VALUES = ["testnet", "mainnet", "MAINNET", "Testnet", "garbage", ""]
_PK = "0x" + "11" * 32
_ADDR = "0x" + "ab" * 20


# --- NadoClient constructors ----------------------------------------------

@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_nado_client_init_refuses_arcus(net):
    from src.nadobro.venue.nado_client import NadoClient

    with pytest.raises(VenueScopeError):
        NadoClient(_PK, net)


@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_nado_client_from_address_refuses_arcus(net):
    # from_address uses cls.__new__ and never runs __init__ — it needs its own guard.
    from src.nadobro.venue.nado_client import NadoClient

    with pytest.raises(VenueScopeError):
        NadoClient.from_address(_ADDR, net)


@pytest.mark.parametrize("net", NADO_VALUES)
def test_nado_client_constructors_keep_network_verbatim(net):
    from src.nadobro.venue.nado_client import NadoClient

    assert NadoClient(_PK, net).network is net
    assert NadoClient.from_address(_ADDR, net).network is net


def test_nado_client_default_network_is_unchanged():
    from src.nadobro.venue.nado_client import NadoClient

    assert NadoClient(_PK).network == "testnet"
    assert NadoClient.from_address(_ADDR).network == "testnet"


# --- client factories -----------------------------------------------------

@pytest.fixture
def _fresh_client_cache(monkeypatch):
    from src.nadobro.venue import nado_client

    cache: dict = {}
    monkeypatch.setattr(nado_client, "_NADO_CLIENT_CACHE", cache)
    monkeypatch.setattr(nado_client, "_NADO_CLIENT_CACHE_USER_INDEX", {})
    monkeypatch.setattr(nado_client, "_client_cache", {})
    # Never touch the SDK / network from a unit test.
    monkeypatch.setattr(nado_client.NadoClient, "initialize", lambda self: True)
    return cache


@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_signing_factory_refuses_arcus_and_caches_nothing(_fresh_client_cache, net):
    from src.nadobro.venue import nado_client

    with pytest.raises(VenueScopeError):
        nado_client.get_or_create_signing_client(_PK, net, user_id=1)
    with pytest.raises(VenueScopeError):
        nado_client.get_nado_client(_PK, net)
    assert _fresh_client_cache == {}


@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_readonly_factory_refuses_arcus_and_caches_nothing(_fresh_client_cache, net):
    from src.nadobro.venue import nado_client

    with pytest.raises(VenueScopeError):
        nado_client.get_or_create_readonly_client(_ADDR, net, user_id=1)
    assert _fresh_client_cache == {}


@pytest.mark.parametrize("net", NADO_VALUES)
def test_factories_keep_their_cache_keys(_fresh_client_cache, net):
    from src.nadobro.venue import nado_client

    signer = nado_client.get_or_create_signing_client(_PK, net, user_id=1)
    reader = nado_client.get_or_create_readonly_client(_ADDR, net, user_id=1)
    assert signer.network is net and reader.network is net
    keys = set(_fresh_client_cache)
    assert ("readonly", _ADDR.lower(), str(net)) in keys
    assert any(k[0] == "signer" and k[2] == str(net) for k in keys)


@pytest.mark.parametrize("net", ARCUS_VALUES + [None])
def test_cache_cleanup_paths_never_raise(net):
    # Cleanup must always work — never guard it.
    from src.nadobro.venue import nado_client

    nado_client.clear_client_cache(_ADDR, net)
    nado_client.clear_linked_signer_cache(_ADDR, net)


# --- users/user_service getters -------------------------------------------

@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_get_user_nado_client_raises_instead_of_returning_none(monkeypatch, net):
    # The body's try/except turns every error into None; the guard sits before it.
    from src.nadobro.users import user_service

    calls: list = []
    monkeypatch.setattr(user_service, "get_user", lambda uid: calls.append(uid))
    with pytest.raises(VenueScopeError):
        user_service.get_user_nado_client(1, network=net)
    with pytest.raises(VenueScopeError):
        user_service.get_user_readonly_client(1, network=net)
    assert calls == []  # refused before any user lookup


def _linked_user(network_mode="mainnet"):
    from src.nadobro.models.database import UserRow

    return UserRow({
        "telegram_id": 1, "main_address": _ADDR, "linked_signer_address": _ADDR,
        "encrypted_linked_signer_pk": "AAAA", "network_mode": network_mode,
    })


@pytest.mark.parametrize("net", [None] + NADO_VALUES)
def test_get_user_nado_client_forwards_nado_networks_unchanged(monkeypatch, net):
    from src.nadobro.core import crypto
    from src.nadobro.users import user_service

    seen: list = []
    monkeypatch.setattr(user_service, "get_user", lambda uid: _linked_user())
    monkeypatch.setattr(crypto, "decrypt_with_server_key", lambda ct: _PK.encode())
    monkeypatch.setattr(
        user_service, "get_nado_client",
        lambda pk, network, main_address=None: seen.append(network) or SimpleNamespace(),
    )
    user_service.get_user_nado_client(1, network=net)
    assert seen == [str(net or "mainnet")]  # legacy: str(network or user.network_mode.value)


@pytest.mark.parametrize("net", [None] + NADO_VALUES)
def test_get_user_readonly_client_forwards_nado_networks_unchanged(monkeypatch, net):
    from src.nadobro.users import user_service
    from src.nadobro.venue import nado_client

    seen: list = []
    monkeypatch.setattr(user_service, "get_user", lambda uid: _linked_user())
    monkeypatch.setattr(user_service, "_readonly_cache", {})
    monkeypatch.setattr(
        nado_client, "get_or_create_readonly_client",
        lambda addr, network, user_id=None: seen.append(network) or SimpleNamespace(),
    )
    user_service.get_user_readonly_client(1, network=net)
    assert seen == [str(net or "mainnet")]


@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_set_network_mode_refuses_arcus_before_writing(monkeypatch, net):
    # users.network_mode has no CHECK: an Arcus value there would make every
    # get_user for the user raise.
    from src.nadobro.users import user_service

    writes: list = []
    monkeypatch.setattr(user_service, "execute", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(user_service, "get_user", lambda uid: None)
    with pytest.raises(VenueScopeError):
        user_service.set_network_mode(1, net)
    assert writes == []


@pytest.mark.parametrize("net", ["testnet", "mainnet"])
def test_set_network_mode_still_writes_nado_networks(monkeypatch, net):
    from src.nadobro.users import user_service

    writes: list = []
    monkeypatch.setattr(user_service, "execute", lambda sql, params: writes.append(params))
    monkeypatch.setattr(user_service, "get_user", lambda uid: None)
    monkeypatch.setattr(user_service, "_invalidate_user_caches", lambda *a, **k: None)
    user_service.set_network_mode(1, net)
    assert writes == [(net, 1)]


# --- product catalog roots ------------------------------------------------

@pytest.fixture
def _catalog_spy(monkeypatch):
    from src.nadobro.venue import product_catalog as pc

    built: list = []
    for cache in ("_catalog_cache", "_spot_catalog_cache", "_dn_pair_cache"):
        monkeypatch.setattr(pc, cache, {})
    empty = {"perps": {}, "aliases": {}, "by_id": {}, "spots": {}, "pairs": {}}
    monkeypatch.setattr(pc, "_build_dynamic_catalog", lambda net, client=None: built.append(net) or dict(empty))
    monkeypatch.setattr(pc, "_build_dynamic_spot_catalog", lambda net: built.append(net) or dict(empty))
    monkeypatch.setattr(pc, "_build_dn_pair_catalog", lambda net, client=None: built.append(net) or dict(empty))
    return pc, built


_ROOTS = ("get_catalog", "get_spot_catalog", "get_dn_pair_catalog")


@pytest.mark.parametrize("root", _ROOTS)
@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_catalog_roots_refuse_arcus_and_never_fetch(_catalog_spy, root, net):
    pc, built = _catalog_spy
    with pytest.raises(VenueScopeError):
        getattr(pc, root)(net)
    assert built == []
    with pytest.raises(VenueScopeError):
        pc.spot_min_notional_cached("KBTC", net)


@pytest.mark.parametrize("root", _ROOTS)
@pytest.mark.parametrize("net", [None] + NADO_VALUES)
def test_catalog_roots_keep_legacy_keys(_catalog_spy, root, net):
    pc, built = _catalog_spy
    getattr(pc, root)(net)
    assert built == [str(net or "mainnet").lower()]


@pytest.mark.parametrize(
    "call",
    [
        lambda pc, n: pc.get_product_id("BTC", network=n),
        lambda pc, n: pc.get_product_name(2, network=n),
        lambda pc, n: pc.list_perp_names(network=n),
        lambda pc, n: pc.list_dn_product_names(network=n),
        lambda pc, n: pc.get_dn_pair("BTC", network=n),
        lambda pc, n: pc.get_spot_product_id("KBTC", network=n),
        lambda pc, n: pc.get_spot_metadata("KBTC", network=n),
        lambda pc, n: pc.get_product_max_leverage("BTC", network=n),
        lambda pc, n: pc.is_product_isolated_only("BTC", network=n),
        lambda pc, n: pc.list_volume_spot_bases(network=n),
    ],
)
def test_public_catalog_lookups_refuse_arcus(_catalog_spy, call):
    pc, built = _catalog_spy
    with pytest.raises(VenueScopeError):
        call(pc, "arcus_mainnet")
    assert built == []


# --- config wrappers (guard BEFORE the swallowing try) ----------------------

_CONFIG_WRAPPERS = {
    # wrapper: (call, product_catalog function it delegates to)
    "get_product_id": (lambda c, n: c.get_product_id("BTC", network=n), "get_product_id"),
    "get_product_name": (lambda c, n: c.get_product_name(2, network=n), "get_product_name"),
    "get_spot_product_id": (lambda c, n: c.get_spot_product_id("KBTC", network=n), "get_spot_product_id"),
    "get_spot_metadata": (lambda c, n: c.get_spot_metadata("KBTC", network=n), "get_spot_metadata"),
    "get_product_max_leverage": (lambda c, n: c.get_product_max_leverage("BTC", network=n), "get_product_max_leverage"),
    "get_product_initial_margin_fraction": (
        lambda c, n: c.get_product_initial_margin_fraction("BTC", network=n), "get_product_initial_margin_fraction"),
    "get_product_maintenance_margin_fraction": (
        lambda c, n: c.get_product_maintenance_margin_fraction("BTC", network=n),
        "get_product_maintenance_margin_fraction"),
    "get_perp_products": (lambda c, n: c.get_perp_products(network=n), "list_perp_names"),
    "get_dn_pair": (lambda c, n: c.get_dn_pair("BTC", network=n), "get_dn_pair"),
    "list_volume_spot_product_names": (lambda c, n: c.list_volume_spot_product_names(network=n), "list_volume_spot_bases"),
    "get_dn_products": (lambda c, n: c.get_dn_products(network=n), "list_dn_product_names"),
    "is_product_isolated_only": (lambda c, n: c.is_product_isolated_only("BTC", network=n), "is_product_isolated_only"),
}


def test_every_config_catalog_wrapper_is_covered():
    import ast
    import pathlib

    from src.nadobro import config

    src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
    guarded = {
        fn.name for fn in ast.walk(ast.parse(src))
        if isinstance(fn, ast.FunctionDef) and "_default_catalog_network()" in ast.get_source_segment(src, fn)
        and fn.name != "_default_catalog_network"
    }
    assert guarded == set(_CONFIG_WRAPPERS)


@pytest.fixture
def _catalog_delegates(monkeypatch):
    """Record the network each config wrapper hands to product_catalog."""
    from src.nadobro.venue import product_catalog as pc

    seen: list = []
    names = {d for _, d in _CONFIG_WRAPPERS.values()}
    for name in names:
        monkeypatch.setattr(pc, name, lambda *a, network=None, _n=name, **k: seen.append(network) or None)
    return seen


@pytest.mark.parametrize("wrapper", sorted(_CONFIG_WRAPPERS))
@pytest.mark.parametrize("net", ARCUS_VALUES)
def test_config_wrappers_refuse_arcus_instead_of_static_nado_data(_catalog_delegates, wrapper, net):
    from src.nadobro import config

    with pytest.raises(VenueScopeError):
        _CONFIG_WRAPPERS[wrapper][0](config, net)
    assert _catalog_delegates == []


@pytest.mark.parametrize("wrapper", sorted(_CONFIG_WRAPPERS))
@pytest.mark.parametrize("net", [None] + NADO_VALUES)
def test_config_wrappers_forward_legacy_network(monkeypatch, _catalog_delegates, wrapper, net):
    from src.nadobro import config

    monkeypatch.delenv("NADO_PRODUCT_CATALOG_DEFAULT_NETWORK", raising=False)
    _CONFIG_WRAPPERS[wrapper][0](config, net)
    assert _catalog_delegates and set(_catalog_delegates) == {str(net or "mainnet")}


def test_config_default_catalog_network_env_cannot_smuggle_arcus(monkeypatch, _catalog_delegates):
    from src.nadobro import config

    monkeypatch.setenv("NADO_PRODUCT_CATALOG_DEFAULT_NETWORK", "arcus_mainnet")
    with pytest.raises(VenueScopeError):
        config.get_product_id("BTC")
    monkeypatch.setenv("NADO_PRODUCT_CATALOG_DEFAULT_NETWORK", "testnet")
    config.get_product_id("BTC")
    assert _catalog_delegates == ["testnet"]


def test_config_get_product_id_static_fallback_unchanged_for_nado(monkeypatch):
    # The swallow-and-fallback still works for real Nado failures.
    from src.nadobro import config
    from src.nadobro.venue import product_catalog as pc

    def boom(*a, **k):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(pc, "get_product_id", boom)
    assert config.get_product_id("BTC", network="mainnet") == config.PRODUCT_ALIASES.get("btc")


# --- nado_sync: sweeps log and skip; back-link drops Arcus sessions ---------

def test_sync_active_users_skips_a_scope_error_and_advances(monkeypatch, caplog):
    from src.nadobro.venue import nado_sync

    rows = [{"telegram_id": 5, "network": "arcus_mainnet"}, {"telegram_id": 6, "network": "mainnet"}]
    synced: list = []

    async def fake_sync_user(uid, *, network, reason, max_age_ms):
        nado_sync._normalize_network(network)  # raises for the Arcus row
        synced.append((uid, network))

    async def fake_blocking_db(fn, *a, **k):
        return rows

    monkeypatch.setattr(nado_sync, "run_blocking_db", fake_blocking_db)
    monkeypatch.setattr(nado_sync, "sync_user", fake_sync_user)
    monkeypatch.setattr(nado_sync, "_active_users_cursor", 0)
    caplog.set_level(logging.WARNING, logger=nado_sync.__name__)
    asyncio.run(nado_sync.sync_active_users(reason="manual"))
    assert synced == [(6, "mainnet")]
    assert nado_sync._active_users_cursor == 6
    assert any("user=5 skipped" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("session_net", ["arcus_mainnet", "arcus_testnet"])
def test_back_link_drops_a_non_nado_session_instead_of_raising(monkeypatch, session_net):
    from src.nadobro.venue import nado_sync

    def fake_query_one(sql, params=None):
        if "order_intents" in sql:
            return {"value": {"strategy_session_id": 9, "source": "strategy"}}
        return {"network": session_net}

    monkeypatch.setattr(nado_sync, "query_one", fake_query_one)
    sid, source, found, _pid, _pname = nado_sync._back_link_intent("0xabc", "mainnet")
    assert (sid, source, found) == (None, "strategy", True)


@pytest.mark.parametrize(
    "session_net, fill_net, linked",
    [("mainnet", "mainnet", 9), ("testnet", "mainnet", None), ("MAINNET", "mainnet", 9), (None, "mainnet", 9)],
)
def test_back_link_nado_behaviour_unchanged(monkeypatch, session_net, fill_net, linked):
    from src.nadobro.venue import nado_sync

    def fake_query_one(sql, params=None):
        if "order_intents" in sql:
            return {"value": {"strategy_session_id": 9, "source": "strategy"}}
        return {"network": session_net}

    monkeypatch.setattr(nado_sync, "query_one", fake_query_one)
    assert nado_sync._back_link_intent("0xabc", fill_net)[0] == linked
