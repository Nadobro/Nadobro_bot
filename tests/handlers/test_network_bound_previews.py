"""PREVIEW-NETWORK-BIND unit tests: the binding primitives, the i18n of the
refusal, the switch-time clean-up helper, and the already-safe Desk path.

The end-to-end scenarios (every preview built on TESTNET and confirmed after
the user's network became MAINNET) live in ``test_preview_network_binding.py``.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro import i18n  # noqa: E402
from src.nadobro.handlers import (  # noqa: E402
    desk_handler,
    home_card,
    keyboards,
    orders_view,
    portfolio_deck,
    state_reset,
    strategy_handler,
    vault_handler,
)
from src.nadobro.handlers.network_guard import (  # noqa: E402
    STALE_ACTION_TEXT,
    STALE_TRADE_TEXT,
    STALE_UNKNOWN_TEXT,
    bind_cb,
    same_network,
    stale_preview_text,
    unbind_cb,
)
from src.nadobro.quant.vol_fee_estimator import estimate_vol_fees  # noqa: E402
from src.nadobro.trading.desk_plans import ST_DRAFT, ExecutionPlan  # noqa: E402

_NON_EN = ("zh", "fr", "ar", "ru", "ko")
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


# --------------------------------------------------------------------------- #
# The comparison fails closed                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "built, current, expected",
    [
        ("testnet", "testnet", True),
        ("mainnet", "mainnet", True),
        ("MAINNET", " mainnet ", True),
        ("testnet", "mainnet", False),
        ("mainnet", "testnet", False),
        (None, "testnet", False),        # unstamped preview
        ("", "mainnet", False),
        ("testnet", None, False),        # current network unknown
        (None, None, False),
        ("devnet", "devnet", False),     # junk on both sides is still not a match
        (True, "mainnet", False),        # the legacy ``pending_text_close_all = True`` flag
    ],
)
def test_same_network_is_true_only_for_two_equal_valid_networks(built, current, expected):
    assert same_network(built, current) is expected


# --------------------------------------------------------------------------- #
# callback_data tagging                                                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "data",
    [
        "pos:confirm_close_all",
        "pos:close:BTC",
        "portfolio:close_all_yes",
        "portfolio:cancel_all_yes",
        "portfolio:cancel_order:3",
        "portfolio:cancel_order:d:abcdef1234567890",
        "strategy:start:grid:BTC",
        "strategy:startok:vol:KBTC",
        "vault:deposit:confirm:100.0",
        "vault:withdraw:confirm:1.2345678901234567e-05",
    ],
)
@pytest.mark.parametrize("network", ["testnet", "mainnet"])
def test_bind_and_unbind_round_trip(data, network):
    bound = bind_cb(data, network)
    assert bound == f"{data}:{network}"
    assert unbind_cb(bound) == (data, network)


@pytest.mark.parametrize(
    "data",
    ["pos:confirm_close_all", "strategy:start:grid:BTC", "pos:close:TESTNETX", "mode:testnet_", "", "testnet"],
)
def test_unbind_leaves_an_untagged_callback_alone(data):
    assert unbind_cb(data) == (data, None)


@pytest.mark.parametrize("network", [None, "", "devnet", "Main net"])
def test_bind_refuses_anything_but_a_real_network(network):
    with pytest.raises(ValueError):
        bind_cb("pos:confirm_close_all", network)


def test_bind_refuses_callback_data_over_telegrams_64_byte_limit():
    assert len(bind_cb("x" * 56, "mainnet")) == 64  # exactly at the limit is fine
    with pytest.raises(ValueError):
        bind_cb("x" * 57, "testnet")  # one byte over is not


def test_the_longest_real_confirms_fit_in_64_bytes():
    """Worst cases that the renderers actually produce."""
    worst = [
        # float repr at its longest (the vault amounts are raw floats)
        vault_handler._deposit_confirm_card(-1.2345678901234567e-308, None, network="mainnet"),
        vault_handler._withdraw_confirm_card(1.2345678901234567e-308, 0.0, 0.0, network="testnet"),
    ]
    callbacks = [btn.callback_data for _text, kb in worst for row in kb.inline_keyboard for btn in row]
    callbacks += [
        orders_view.cancel_callback_for({"digest": "0x" + "f" * 64}, 999, network="testnet"),
        orders_view.cancel_callback_for({}, 99_999, network="mainnet"),
    ]
    for data in callbacks:
        assert len(data.encode("utf-8")) <= 64, data


def _est():
    return estimate_vol_fees(
        margin_usd=100, target_volume_usd=10_000,
        taker_fee_rate=Decimal("0.0004"), builder_fee_rate=Decimal("0.0001"),
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: keyboards.positions_kb([]),
        lambda: keyboards.close_product_kb(),
        lambda: keyboards.confirm_close_all_kb(),
        lambda: keyboards.strategy_action_kb("grid"),
        lambda: orders_view.cancel_callback_for({}, 0),
        lambda: orders_view.render_cancel_all_confirm(),
        lambda: portfolio_deck.render_close_all_confirm(),
        lambda: vault_handler._deposit_confirm_card(100.0),
        lambda: vault_handler._withdraw_confirm_card(1.0, 1.0, 0.1),
        lambda: strategy_handler._vol_fee_quote_key(_est(), 0.0),
    ],
    ids=[
        "positions_kb", "close_product_kb", "confirm_close_all_kb", "strategy_action_kb",
        "cancel_callback_for", "render_cancel_all_confirm", "render_close_all_confirm",
        "deposit_confirm_card", "withdraw_confirm_card", "vol_fee_quote_key",
    ],
)
def test_every_binding_builder_requires_the_network(build):
    """No default: a ``"mainnet"`` default is exactly how a card rendered on
    testnet ended up acting on mainnet."""
    with pytest.raises(TypeError):
        build()


def test_executing_buttons_are_tagged_with_the_network_they_were_rendered_on():
    kb = keyboards.positions_kb([{"product_name": "BTC-PERP"}], network="testnet")
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "pos:close:BTC:testnet" in callbacks
    assert "pos:close_all" in callbacks, "the opener renders a fresh, bound confirm; it stays untagged"
    assert keyboards.confirm_close_all_kb(network="mainnet").inline_keyboard[0][0].callback_data == (
        "pos:confirm_close_all:mainnet"
    )
    start = keyboards.strategy_action_kb("grid", "BTC", ["BTC"], network="testnet").inline_keyboard[0][0]
    assert start.callback_data == "strategy:start:grid:BTC:testnet"
    _text, deposit = vault_handler._deposit_confirm_card(250.0, None, network="testnet")
    assert deposit.inline_keyboard[0][0].callback_data == "vault:deposit:confirm:250.0:testnet"


# --------------------------------------------------------------------------- #
# i18n of the refusal                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", [STALE_TRADE_TEXT, STALE_ACTION_TEXT, STALE_UNKNOWN_TEXT])
def test_refusal_strings_are_translated_in_every_language(key):
    entry = i18n._TEXTS.get(key)
    assert entry is not None, f"missing from i18n._TEXTS: {key!r}"
    assert set(entry) == set(_NON_EN)
    for lang, text in entry.items():
        assert text and text != key, lang
        assert set(_PLACEHOLDER.findall(text)) == set(_PLACEHOLDER.findall(key)), lang
        assert "⚠️" in text, lang


@pytest.mark.parametrize("lang", sorted(i18n.SUPPORTED_LANGS))
@pytest.mark.parametrize("notice", ["trade", "action"])
def test_refusal_names_both_networks_in_every_language(lang, notice):
    with i18n.language_context(lang):
        text = stale_preview_text("testnet", "mainnet", notice=notice)
    assert "TESTNET" in text and "MAINNET" in text, text
    assert "{" not in text and "}" not in text, text
    if lang == "en":
        assert "Nothing was sent" in text
    else:
        assert text != (STALE_TRADE_TEXT if notice == "trade" else STALE_ACTION_TEXT).format(
            built="TESTNET", current="MAINNET"
        ), f"{lang} fell back to English"


@pytest.mark.parametrize("lang", sorted(i18n.SUPPORTED_LANGS))
@pytest.mark.parametrize("built, current", [(None, "mainnet"), ("testnet", None), ("junk", "testnet")])
def test_refusal_without_a_known_network_says_out_of_date(lang, built, current):
    with i18n.language_context(lang):
        text = stale_preview_text(built, current, notice="trade")
    assert text == i18n.localize_text(STALE_UNKNOWN_TEXT, lang)


def test_refusal_text_needs_no_parse_mode():
    """Sent without a parse mode: it must carry no Markdown/HTML markup."""
    for key in (STALE_TRADE_TEXT, STALE_ACTION_TEXT, STALE_UNKNOWN_TEXT):
        for text in [key, *i18n._TEXTS[key].values()]:
            assert not any(ch in text for ch in "*_`\\<>"), text


# --------------------------------------------------------------------------- #
# The switch-time clean-up                                                     #
# --------------------------------------------------------------------------- #
class _Ctx:
    def __init__(self) -> None:
        self.user_data: dict = {}


def _patch_persisted(monkeypatch) -> tuple[list, list]:
    """Record the bot_state clearers and the pool they run on."""
    cleared: list = []
    pooled: list = []
    for name in (
        "clear_strategy_pending_input",
        "clear_text_trade_pending",
        "clear_text_close_all_pending",
        "clear_wallet_pending_flow",
    ):
        monkeypatch.setattr(state_reset, name, lambda uid, _n=name: cleared.append((_n, uid)))

    async def _db_pool(fn, *args, **kwargs):
        pooled.append(fn.__name__)
        return fn(*args, **kwargs)

    monkeypatch.setattr(state_reset, "run_blocking_db", _db_pool)
    return cleared, pooled


def test_switch_clean_up_drops_every_preview_and_keeps_live_guards(monkeypatch):
    cleared, pooled = _patch_persisted(monkeypatch)
    ctx = _Ctx()
    for key in state_reset._TRANSIENT_USER_DATA_KEYS + state_reset._NETWORK_SWITCH_EXTRA_KEYS:
        ctx.user_data[key] = {"network": "testnet"}
    ctx.user_data["vault_op_inflight"] = "deposit"   # a mint still in flight
    ctx.user_data[home_card.HOME_CARD_KEY] = {"message_id": 7}  # view state, not a preview

    asyncio.run(state_reset.clear_state_after_network_switch(ctx, 42))

    assert ctx.user_data == {"vault_op_inflight": "deposit", home_card.HOME_CARD_KEY: {"message_id": 7}}
    assert "trade_card_session" in state_reset._NETWORK_SWITCH_EXTRA_KEYS
    assert "vol_fee_quote" in state_reset._NETWORK_SWITCH_EXTRA_KEYS
    assert sorted(name for name, _uid in cleared) == [
        "clear_strategy_pending_input",
        "clear_text_close_all_pending",
        "clear_text_trade_pending",
        "clear_wallet_pending_flow",
    ]
    assert {uid for _name, uid in cleared} == {42}
    assert pooled == ["_clear_persisted_pending"], "the bot_state deletes must run on the DB pool, not the loop"


def test_switch_clean_up_without_a_context_still_clears_bot_state(monkeypatch):
    cleared, _pooled = _patch_persisted(monkeypatch)
    asyncio.run(state_reset.clear_state_after_network_switch(None, 42))
    assert len(cleared) == 4


def test_one_failing_clearer_does_not_stop_the_others(monkeypatch):
    cleared, _pooled = _patch_persisted(monkeypatch)

    def _boom(_uid):
        raise RuntimeError("db down")

    monkeypatch.setattr(state_reset, "clear_text_trade_pending", _boom)
    asyncio.run(state_reset.clear_state_after_network_switch(_Ctx(), 42))
    assert sorted(name for name, _uid in cleared) == [
        "clear_strategy_pending_input",
        "clear_text_close_all_pending",
        "clear_wallet_pending_flow",
    ]


def test_navigation_clean_up_is_unchanged(monkeypatch):
    """``clear_pending_user_state`` (every nav tap) keeps its old contract: the
    trade card and the Volume consent survive navigation."""
    cleared, _pooled = _patch_persisted(monkeypatch)
    ctx = _Ctx()
    ctx.user_data.update({"trade_card_session": {"x": 1}, "vol_fee_quote": ("q",), "pending_trade": {"x": 1}})
    state_reset.clear_pending_user_state(ctx, 42)
    assert ctx.user_data == {"trade_card_session": {"x": 1}, "vol_fee_quote": ("q",)}
    assert len(cleared) == 4
    cleared.clear()
    state_reset.clear_pending_user_state(None, 42)
    assert cleared == []


# --------------------------------------------------------------------------- #
# Desk: already bound by construction (per-network plan tables)                #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("current", ["testnet", "mainnet"], ids=["same_network", "cross_network"])
def test_desk_draft_is_armed_only_on_its_own_network(monkeypatch, current):
    """Desk drafts live in ``desk_plans_<network>`` and the confirm looks the
    plan up in the CURRENT network's table, so a testnet draft confirmed after
    the switch is simply not found. Lock that in (with a same-network control
    that must arm, so the check can never pass vacuously)."""
    plan = ExecutionPlan(algo="twap", market="spot", product="ETH", side="buy",
                         size_quote=500.0, duration_minutes=60, interval_seconds=30)
    tables = {
        "testnet": {plan.plan_id: {"row_id": 1, "user_id": 42, "plan_id": plan.plan_id,
                                    "status": ST_DRAFT, "plan": plan, "state": {}}},
        "mainnet": {},
    }
    confirmed: list = []
    edits: list = []

    async def _edit(_query, text, **_kw):
        edits.append(str(text))

    monkeypatch.setattr(desk_handler, "_edit_loc", _edit)
    monkeypatch.setattr(desk_handler, "_network_of", lambda _tid: current)
    monkeypatch.setattr(desk_handler.desk_store, "get_plan", lambda pid, net: tables[net].get(pid))
    monkeypatch.setattr(desk_handler.desk_store, "count_confirmed_today", lambda *_a: 0)
    monkeypatch.setattr(desk_handler.desk_store, "confirm_plan", lambda *a: confirmed.append(a) or True)

    asyncio.run(desk_handler.handle_desk_callback(object(), f"desk:confirm:{plan.plan_id}", 42, None))

    if current == "testnet":
        assert [(a[0], a[2]) for a in confirmed] == [(plan.plan_id, "testnet")]
    else:
        assert confirmed == [], "a testnet draft was armed on mainnet"
        assert edits and "gone" in edits[-1]
