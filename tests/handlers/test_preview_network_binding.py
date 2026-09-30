"""PREVIEW-NETWORK-BIND guardrails: a preview built on one Nado network must
never confirm on the other.

The testnet<->mainnet switch (Execution Mode -> ``callbacks._handle_mode`` ->
``strategy/network_switch.switch_network``) flips ``users.network_mode``. Every
confirm path below resolves the network it executes on from
``users.network_mode`` AT CONFIRM TIME. So a preview the user built and reviewed
on TESTNET (product list, size, leverage, fees and settings all read from
testnet) confirms as a REAL MAINNET action. The headline case is the guided
trade card: ``handle_trade_card_callback`` re-stamps ``session["network"]`` with
the current network on every tap, and the switch does not clear
``context.user_data["trade_card_session"]``.

Each scenario drives the REAL handlers end to end: the callback router
(``callbacks.handle_callback``), the free-text router
(``messages.handle_message``) and the domain handlers they dispatch to. Only
process boundaries and inputs are faked: the user row, readiness gates,
settings, the product catalog, portfolio and vault snapshots, the copy trader
list, the strategy preview body, the Volume fee quote, the venue teardown of a
switch, and the Desk / AI-chat tail that unclaimed free text falls through to.
Every function that moves money is a spy, so nothing reaches Nado.

Most scenarios run twice:

* ``same_network`` is a control: the preview is built and confirmed on
  TESTNET, and the action runs exactly once. This keeps the spies honest, so a
  guardrail can never pass vacuously.
* ``cross_network``: the preview is built on TESTNET, the user's network
  becomes MAINNET, and then the preview is confirmed. Nothing may run, and the
  user must be told the preview expired ("Nothing was sent").

The network changes in one of two ways:

* ``Harness.switch`` goes through the REAL Execution Mode handler
  (``mode:mainnet``), with only the venue teardown (``switch_network``) faked
  as a success. It is used for every CONFIRMED path: the switch's own clean-up
  does not stop them today.
* ``Harness.flip_elsewhere`` changes ``users.network_mode`` but leaves this
  process's ``context.user_data`` untouched, as when another machine served the
  switch during a rolling deploy. It is used for LATENT paths, which are safe
  in-process today only because the switch pops their ``user_data`` key. The
  fix binds them to their network, so correctness no longer depends on that
  clean-up.

PREVIEW-NETWORK-BIND — fixed: every cross-network scenario here was a strict
xfail until previews were bound to the network they were built on
(handlers/network_guard.py). They are plain regression tests now.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback
from decimal import Decimal
from types import SimpleNamespace

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import (  # noqa: E402
    callbacks,
    copy_handler,  # noqa: F401 - imported so _swap() rebinds its imports too
    desk_handler,
    home_card,  # noqa: F401
    intent_handlers,
    messages,
    portfolio_deck,
    portfolio_handler,
    strategy_handler,
    trade_card,
    vault_handler,  # noqa: F401
    wallet_handler,  # noqa: F401
    wallet_view,
)
from src.nadobro.handlers.keyboards import SIZE_PRESETS, trade_card_cb  # noqa: E402
from src.nadobro.models.database import UserRow  # noqa: E402
from src.nadobro.quant.vol_fee_estimator import estimate_vol_fees  # noqa: E402
from src.nadobro.strategy import bot_runtime, network_switch, strategy_pending_input  # noqa: E402
from src.nadobro.trading import copy_service, text_trade_pending, trade_service  # noqa: E402
from src.nadobro.core import user_rate_limit  # noqa: E402
from src.nadobro.users import (  # noqa: E402
    admin_service,
    onboarding_service,
    settings_service,
    user_service,
    wallet_pending_flow,
)
from src.nadobro.vault import nlp_vault_service  # noqa: E402
from src.nadobro.venue import product_catalog  # noqa: E402

UID = 7_300_001
CARD_MSG = 501  # the card / confirm message that every tap in a scenario edits
MODE_MSG = 777  # the Execution Mode message the switch is tapped on
TESTNET, MAINNET = "testnet", "mainnet"

_STATIC_CATALOG = product_catalog._build_static_catalog()
_STATIC_SPOT_CATALOG = product_catalog._build_static_spot_catalog()


class HarnessError(RuntimeError):
    """The scenario itself broke: a handler raised or swallowed an error, or a
    button the scenario needs was never rendered.

    This is deliberately NOT an AssertionError: a broken harness must fail
    loudly as a harness failure, never pass itself off as a refused confirm
    (or, while these were strict xfails, as the expected bug)."""


# --------------------------------------------------------------------------- #
# Telegram fakes: every text/keyboard the bot shows lands on one Screen.       #
# --------------------------------------------------------------------------- #
class Screen:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self._next_message_id = 1000

    def show(self, text, markup=None) -> None:
        self.events.append((str(text or ""), markup))

    def mark(self) -> int:
        return len(self.events)

    def texts(self, since: int = 0) -> list[str]:
        return [text for text, _ in self.events[since:]]

    def next_message_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    def button(self, prefix: str) -> str:
        """callback_data of the newest rendered inline button starting with ``prefix``."""
        for _text, markup in reversed(self.events):
            for row in getattr(markup, "inline_keyboard", None) or ():
                for btn in row:
                    data = getattr(btn, "callback_data", None)
                    if isinstance(data, str) and data.startswith(prefix):
                        return data
        raise HarnessError(f"no rendered button starts with {prefix!r}; screen: {self.texts()[-4:]!r}")


class _Chat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id

    async def send_action(self, *_a, **_k) -> bool:
        return True


class _Message:
    def __init__(self, screen: Screen, *, message_id: int = CARD_MSG, text: str | None = None) -> None:
        self._screen = screen
        self.chat_id = UID
        self.message_id = message_id
        self.text = text
        self.chat = _Chat(UID)

    async def reply_text(self, text, **kwargs):
        self._screen.show(text, kwargs.get("reply_markup"))
        return _Message(self._screen, message_id=self._screen.next_message_id())


class _Query:
    def __init__(self, screen: Screen, data: str, *, message_id: int) -> None:
        self._screen = screen
        self.data = data
        self.from_user = SimpleNamespace(id=UID, username="guardrail")
        self.message = _Message(screen, message_id=message_id)

    async def edit_message_text(self, text, **kwargs):
        self._screen.show(text, kwargs.get("reply_markup"))
        return True

    async def edit_message_reply_markup(self, reply_markup=None, **_k):
        self._screen.show("", reply_markup)
        return True

    async def answer(self, text=None, **_k):
        if text:
            self._screen.show(text)
        return True


class _Bot:
    def __init__(self, screen: Screen) -> None:
        self._screen = screen

    async def edit_message_text(self, text=None, **kwargs):
        self._screen.show(text, kwargs.get("reply_markup"))
        return True

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self._screen.show(text, kwargs.get("reply_markup"))
        return _Message(self._screen, message_id=self._screen.next_message_id())


class _Context:
    def __init__(self, screen: Screen) -> None:
        self.user_data: dict = {}
        self.bot = _Bot(screen)
        self.application = SimpleNamespace(bot_data={})


# --------------------------------------------------------------------------- #
# The venue: every money-moving entry point records instead of trading.       #
# --------------------------------------------------------------------------- #
class Venue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def spy(self, name: str, result):
        def _spy(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return result()

        _spy.__name__ = f"spy_{name}"
        return _spy


class _SigningClient:
    """What ``get_user_nado_client`` hands out. Every venue call is recorded
    along with the network the client was built for."""

    def __init__(self, venue: Venue, network: str) -> None:
        self._venue = venue
        self.network = network

    async def cancel_orders(self, product_id=None, digests=None, **_k):
        self._venue.calls.append(
            ("cancel_orders", (), {"network": self.network, "product_id": product_id, "digests": list(digests or [])})
        )
        return {"success": True}

    async def cancel_trigger_orders(self, product_id=None, digests=None, **_k):
        self._venue.calls.append(
            ("cancel_trigger_orders", (), {"network": self.network, "product_id": product_id, "digests": list(digests or [])})
        )
        return {"success": True}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self._venue.calls.append((f"client.{name}", args, dict(kwargs, network=self.network)))
            return {"success": True}

        return _call


class _ReadonlyClient:
    def __init__(self, network: str) -> None:
        self.network = network

    def get_market_price(self, *_a, **_k):
        return {"mid": 60_000.0, "bid": 59_990.0, "ask": 60_010.0}

    def get_balance(self, *_a, **_k):
        return {"exists": True, "balances": {0: 1_000.0}}

    def get_all_positions(self, *_a, **_k):
        return [{
            "product_id": 2, "product_name": "BTC-PERP", "side": "LONG",
            "amount": 0.01, "price": 60_000.0, "unrealized_pnl": 0.0,
        }]

    def get_all_market_prices(self, *_a, **_k):
        return {}


class _SwallowedErrors(logging.Handler):
    """Collects the exceptions the routers catch and log (``Callback error
    for ...``, ``Button dispatch error ...``, any ``logger.exception``), so a
    handler crash can never pass for a refused confirm."""

    _ROUTER_PREFIXES = ("Callback error", "Callback BadRequest", "Button dispatch error", "Dynamic button dispatch error")

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info or str(record.msg).startswith(self._ROUTER_PREFIXES):
            self.records.append(record)


def _swap(monkeypatch, owner, name: str, replacement) -> None:
    """Replace ``owner.name`` and every ``from owner import name`` binding
    across ``src.nadobro``, so a fix that adds a new import site is still
    covered."""
    original = getattr(owner, name)
    monkeypatch.setattr(owner, name, replacement)
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod is owner or not mod_name.startswith("src.nadobro"):
            continue
        if mod.__dict__.get(name) is original:
            monkeypatch.setattr(mod, name, replacement)


# --------------------------------------------------------------------------- #
# Harness                                                                      #
# --------------------------------------------------------------------------- #
class Harness:
    def __init__(self, monkeypatch) -> None:
        self.network = TESTNET
        self.screen = Screen()
        self.venue = Venue()
        self.ctx = _Context(self.screen)
        self.bot_state: dict[str, dict] = {}
        # Fresh open orders per network, and whether the CACHED render lacks
        # digests (see the legacy positional-cancel scenario).
        self.orders: dict[str, list[dict]] = {TESTNET: [], MAINNET: []}
        self.cached_without_digests = False
        self._error_log = _SwallowedErrors()
        logging.getLogger("src.nadobro.handlers").addHandler(self._error_log)
        self._install(monkeypatch)

    def close(self) -> None:
        logging.getLogger("src.nadobro.handlers").removeHandler(self._error_log)

    # -- process boundaries -------------------------------------------------
    def user(self) -> UserRow:
        return UserRow({
            "telegram_id": UID,
            "network_mode": self.network,
            "main_address": "0x" + "ab" * 20,
            "linked_signer_address": "0x" + "cd" * 20,
            "encrypted_linked_signer_pk": "enc",
            "language": "en",
        })

    def settings(self, *_a, **_k):
        return self.network, {
            "default_leverage": 3,
            "slippage": 1,
            "strategies": {
                "grid": {"notional_usd": 200.0},
                "vol": {"session_margin_usd": 100.0, "target_volume_usd": 10_000.0, "sl_pct": 0.0},
            },
        }

    def snapshot(self, network: str, *, cached: bool = False) -> dict:
        snap = portfolio_deck.empty_portfolio_snapshot(UID, network)
        orders = [dict(o) for o in self.orders.get(network, [])]
        if cached and self.cached_without_digests:
            for order in orders:
                order.pop("digest", None)
        snap.update(stale=False, monotonic_ts=time.time(), open_orders=orders)
        return snap

    def vault_snapshot(self, *_a, **_k) -> dict:
        return {
            "usdt0_balance": 1_000.0,
            "deposit_room_usdt0": 10_000.0,
            "deposit_max_usdt0": 700.0,
            "max_mintable_usdt0": 700.0,
            "mintable_known": True,
            "nlp_nav_usdt0": 1.04,
            "lp_balance": 100.0,
            "lp_value_usdt0": 104.0,
            "unlocked_known": True,
            "lp_unlocked": 100.0,
            "lp_locked": 0.0,
            "lockup_seconds_remaining": 0,
            "pool": {"tvl_usdt0": 1_000_000.0, "apr_pct": 12.0},
        }

    def _install(self, mp) -> None:
        h = self
        v = self.venue

        # The portfolio domain renders with ParseMode.HTML. The test-stub
        # ParseMode (used when the real PTB was not imported first) lacks it.
        from telegram.constants import ParseMode

        if not hasattr(ParseMode, "HTML"):
            mp.setattr(ParseMode, "HTML", "HTML", raising=False)

        # The user row: every get_user() resolves through the per-process cache.
        mp.setattr(user_service, "_get_cached_user", lambda _tid: h.user())
        _swap(mp, user_service, "get_or_create_user", lambda *_a, **_k: (h.user(), False, None))
        _swap(mp, user_service, "get_user_readonly_client", lambda _tid, network=None, **_k: _ReadonlyClient(network or h.network))
        _swap(mp, user_service, "get_user_nado_client", lambda _tid, network=None, **_k: _SigningClient(v, network or h.network))

        # Readiness gates: onboarded, wallet linked, trading not paused.
        _swap(mp, user_service, "ensure_active_wallet_ready", lambda *_a, **_k: (True, ""))
        _swap(mp, onboarding_service, "is_new_onboarding_complete", lambda *_a, **_k: True)
        _swap(mp, onboarding_service, "get_resume_step", lambda *_a, **_k: "complete")
        _swap(mp, admin_service, "is_trading_paused", lambda *_a, **_k: False)
        _swap(mp, settings_service, "get_user_settings", h.settings)
        _swap(mp, bot_runtime, "get_user_bot_status", lambda *_a, **_k: {})

        # Hermetic product catalog, identical on both networks, so availability
        # never differs and only the network binding is under test.
        mp.setattr(product_catalog, "get_catalog", lambda network="mainnet", client=None, refresh=False: _STATIC_CATALOG)
        mp.setattr(product_catalog, "get_spot_catalog", lambda network="mainnet", refresh=False: _STATIC_SPOT_CATALOG)
        mp.setattr(product_catalog, "list_volume_spot_bases", lambda network="mainnet", refresh=False: ["KBTC"])

        # bot_state-backed pending state lives in memory.
        def _persist_trade(uid, payload):
            h.bot_state[f"text_trade_pending:{int(uid)}"] = dict(payload)

        def _load_trade(uid):
            row = h.bot_state.get(f"text_trade_pending:{int(uid)}")
            return dict(row) if row else None

        def _clear(prefix):
            return lambda uid: h.bot_state.pop(f"{prefix}:{int(uid)}", None)

        _swap(mp, text_trade_pending, "persist_text_trade_pending", _persist_trade)
        _swap(mp, text_trade_pending, "load_text_trade_pending", _load_trade)
        _swap(mp, text_trade_pending, "clear_text_trade_pending", _clear("text_trade_pending"))
        _swap(mp, text_trade_pending, "persist_text_close_all_pending",
              lambda uid: h.bot_state.__setitem__(f"text_close_all_pending:{int(uid)}", {"_ts": time.time()}))
        _swap(mp, text_trade_pending, "clear_text_close_all_pending", _clear("text_close_all_pending"))
        _swap(mp, strategy_pending_input, "load_strategy_pending_input", lambda *_a, **_k: None)
        _swap(mp, strategy_pending_input, "clear_strategy_pending_input", lambda *_a, **_k: None)
        _swap(mp, wallet_pending_flow, "clear_wallet_pending_flow", lambda *_a, **_k: None)
        _swap(mp, wallet_view, "hydrate_wallet_flow_context", lambda *_a, **_k: False)

        # Money movers: spies. Nothing reaches the venue.
        trade_fail = lambda: {"success": False, "error": "guardrail spy (no venue)"}  # noqa: E731
        _swap(mp, trade_service, "execute_market_order", v.spy("execute_market_order", trade_fail))
        _swap(mp, trade_service, "execute_limit_order", v.spy("execute_limit_order", trade_fail))
        _swap(mp, trade_service, "close_all_positions",
              v.spy("close_all_positions", lambda: {"success": True, "products": ["BTC"], "cancelled": 0.01}))
        _swap(mp, trade_service, "close_position",
              v.spy("close_position", lambda: {"success": True, "product": "BTC", "cancelled": 0.01}))
        _swap(mp, bot_runtime, "start_user_bot", v.spy("start_user_bot", lambda: (True, "started")))
        _swap(mp, copy_service, "start_copy", v.spy("start_copy", lambda: (True, "copying")))
        _swap(mp, nlp_vault_service, "deposit_to_vault", v.spy("deposit_to_vault", lambda: {"success": True}))
        _swap(mp, nlp_vault_service, "withdraw_from_vault", v.spy("withdraw_from_vault", lambda: {"success": True}))

        # Domain reads the scenarios need.
        _swap(mp, nlp_vault_service, "get_user_vault_snapshot", h.vault_snapshot)
        _swap(mp, copy_service, "get_available_traders", lambda *_a, **_k: [
            {"id": 7, "label": "Guardrail Trader", "wallet": "0x" + "c" * 40, "is_curated": False},
        ])
        mp.setattr(portfolio_handler, "_cached_snapshot", lambda _tid, network: h.snapshot(network or h.network, cached=True))
        mp.setattr(portfolio_handler, "_spawn_background_refresh", lambda *_a, **_k: None)

        async def _snapshot_for_user(_uid, **_k):
            return h.snapshot(h.network)

        mp.setattr(portfolio_deck, "snapshot_for_user", _snapshot_for_user)
        mp.setattr(strategy_handler, "_render_strategy_preview_card",
                   lambda _tid, sid, product, *_a, **_k: (f"🧪 *{sid.upper()} {product} preview*", None))
        mp.setattr(strategy_handler, "_vol_fee_estimate", lambda *_a, **_k: estimate_vol_fees(
            margin_usd=100, target_volume_usd=10_000,
            taker_fee_rate=Decimal("0.0004"), builder_fee_rate=Decimal("0.0001"),
        ))
        mp.setattr(strategy_handler, "_vol_record_fee_ack", lambda *_a, **_k: None)

        # Free text nobody claims ends here rather than in Desk / AI chat.
        async def _desk_declines(*_a, **_k):
            return False

        async def _ai_chat(*_a, **_k):
            return True

        mp.setattr(desk_handler, "handle_desk_text", _desk_declines)
        mp.setattr(messages, "_handle_managed_agent_message", _ai_chat)
        mp.setattr(user_rate_limit, "check_rate_limit", lambda *_a, **_k: (True, 0.0))

        # The venue teardown of a switch always succeeds here. The switch
        # HANDLER (and its clean-up of pending state) is the real one.
        def _switch_network(_tid, target):
            previous, h.network = h.network, target
            return network_switch.NetworkSwitchResult(switched=True, from_network=previous, to_network=target)

        _swap(mp, network_switch, "switch_network", _switch_network)

    # -- driving the bot ------------------------------------------------------
    def _check(self, mark: int, what: str) -> None:
        if self._error_log.records:
            record = self._error_log.records[0]
            self._error_log.records.clear()
            detail = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            raise HarnessError(f"{what}: a handler logged an error: {record.getMessage()}\n{detail}")
        for text in self.screen.texts(mark):
            if "An error occurred" in text or "Something went wrong" in text:
                raise HarnessError(f"{what}: the handler showed a generic error: {text!r}")

    async def tap(self, data: str, *, message_id: int = CARD_MSG) -> None:
        query = _Query(self.screen, data, message_id=message_id)
        update = SimpleNamespace(
            callback_query=query, effective_user=query.from_user,
            effective_chat=_Chat(UID), effective_message=query.message, message=None,
        )
        mark = self.screen.mark()
        await callbacks.handle_callback(update, self.ctx)
        self._check(mark, f"tap {data!r}")

    async def say(self, text: str) -> None:
        message = _Message(self.screen, message_id=self.screen.next_message_id(), text=text)
        update = SimpleNamespace(
            message=message, effective_user=SimpleNamespace(id=UID, username="guardrail"),
            effective_chat=_Chat(UID), effective_message=message, callback_query=None,
        )
        mark = self.screen.mark()
        await messages.handle_message(update, self.ctx)
        self._check(mark, f"message {text!r}")

    async def switch(self, target: str) -> None:
        """Execution Mode -> ``target`` through the real mode handler."""
        await self.tap(f"mode:{target}", message_id=MODE_MSG)
        if self.network != target:
            raise HarnessError(f"the Execution Mode switch to {target} did not happen: {self.screen.texts()[-1:]!r}")

    def flip_elsewhere(self, target: str) -> None:
        """``users.network_mode`` changes where this process's ``user_data``
        is unreachable (another machine served the switch)."""
        self.network = target

    def button(self, prefix: str) -> str:
        return self.screen.button(prefix)

    def card_session(self) -> dict:
        session = self.ctx.user_data.get(trade_card.TRADE_CARD_SESSION_KEY)
        if not session:
            raise HarnessError("no trade card session was opened")
        return session


@pytest.fixture
def h(monkeypatch):
    harness = Harness(monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


# --------------------------------------------------------------------------- #
# Verdicts                                                                     #
# --------------------------------------------------------------------------- #
def _same_and_cross(path: str):
    """``path``: the confirm under test (documentation only)."""
    del path
    return [
        pytest.param(False, id="same_network"),
        pytest.param(True, id="cross_network"),
    ]


def _assert_nothing_executed(h: Harness) -> None:
    assert h.venue.calls == [], (
        f"a preview built on {TESTNET} executed on {h.network}: {h.venue.calls!r}"
    )


def _assert_expired_notice(h: Harness, mark: int, *, names: bool = True) -> None:
    shown = h.screen.texts(mark)
    notices = [t for t in shown if "nothing was sent" in t.lower()]
    assert notices, f"the user was not told the preview expired; the bot showed: {shown!r}"
    if names:
        assert any(TESTNET in t.lower() and MAINNET in t.lower() for t in notices), (
            f"the expiry notice must name both networks: {notices!r}"
        )


def _assert_executed_on_testnet(h: Harness, name: str, *, times: int = 1) -> None:
    calls = [c for c in h.venue.calls if c[0] == name]
    assert len(calls) == times, f"expected {times} x {name}, venue saw {h.venue.calls!r}"
    for _name, _args, kwargs in calls:
        assert kwargs.get("network") in (None, TESTNET), f"{name} ran on {kwargs.get('network')}: {kwargs!r}"


def _drive(h: Harness, build, *, cross_network: bool, change: str = "switch") -> int:
    """Build a preview on TESTNET, optionally change network, then confirm.
    Returns the screen mark taken just before the confirm."""

    async def scenario() -> int:
        confirm = await build(h)
        if h.venue.calls:
            raise HarnessError(f"building the preview already executed: {h.venue.calls!r}")
        if cross_network:
            if change == "switch":
                await h.switch(MAINNET)
            else:
                h.flip_elsewhere(MAINNET)
        mark = h.screen.mark()
        await confirm()
        return mark

    return asyncio.run(scenario())


def _verdict(h: Harness, mark: int, cross_network: bool, executed: str, *, times: int = 1) -> None:
    if cross_network:
        _assert_nothing_executed(h)
        _assert_expired_notice(h, mark)
    else:
        _assert_executed_on_testnet(h, executed, times=times)


def _size_label(product: str) -> str:
    s = SIZE_PRESETS[product][2]
    return str(int(s)) if s == int(s) else str(s)


# --------------------------------------------------------------------------- #
# S1 - the guided trade card                                                   #
# --------------------------------------------------------------------------- #
async def _card_to(h: Harness, *, order_type: str, stop_after: str = "confirm") -> str:
    """Open a guided trade card on the current network and tap through it.
    Returns the session id."""
    await h.tap("card:trade:start")
    sid = h.card_session()["session_id"]
    await h.tap(h.button(trade_card_cb(sid, "direction", "long")))
    await h.tap(h.button(trade_card_cb(sid, "order", order_type)))
    await h.tap(h.button(trade_card_cb(sid, "product", "BTC")))
    if stop_after == "product":
        return sid
    await h.tap(h.button(trade_card_cb(sid, "lev", "5")))
    await h.tap(h.button(trade_card_cb(sid, "size", _size_label("BTC"))))
    if order_type == "limit":
        if stop_after == "size":
            return sid
        await h.say("95000")
    await h.tap(h.button(trade_card_cb(sid, "tpsl", "skip")))
    return sid


def _build_trade_card(order_type: str):
    async def build(h: Harness):
        sid = await _card_to(h, order_type=order_type)
        confirm = h.button(trade_card_cb(sid, "confirm"))
        return lambda: h.tap(confirm)

    return build


@pytest.mark.parametrize("cross_network", _same_and_cross("trade card confirm (card:trade:<sid>:confirm)"))
@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_trade_card_confirm_is_bound_to_the_network_it_was_built_on(h, order_type, cross_network):
    """A card built and priced on TESTNET is confirmed after the user's network
    became MAINNET: the order must not be placed (today: a real mainnet order)."""
    executor = "execute_market_order" if order_type == "market" else "execute_limit_order"
    mark = _drive(h, _build_trade_card(order_type), cross_network=cross_network, change="elsewhere")
    _verdict(h, mark, cross_network, executor)
    if cross_network:
        assert trade_card.TRADE_CARD_SESSION_KEY not in h.ctx.user_data, "the stale card must be cleared"


def test_trade_card_built_on_testnet_cannot_confirm_after_execution_mode_switch(h):
    """The reported bug, end to end: card on TESTNET -> Execution Mode ->
    MAINNET -> Confirm on the old card places a REAL mainnet order."""

    async def scenario() -> None:
        sid = await _card_to(h, order_type="market")
        confirm = h.button(trade_card_cb(sid, "confirm"))
        await h.switch(MAINNET)
        assert trade_card.TRADE_CARD_SESSION_KEY not in h.ctx.user_data, (
            "a successful network switch must clear the in-flight trade card"
        )
        await h.tap(confirm)

    asyncio.run(scenario())
    _assert_nothing_executed(h)


@pytest.mark.parametrize("step", ["tap", "text"])
def test_trade_card_half_built_on_testnet_cannot_continue_on_mainnet(h, step):
    """The card is half built on TESTNET and the network changes to MAINNET. The
    next step (a button tap, or a typed limit price) must expire the card. It
    must NOT silently re-stamp the card to mainnet (the
    ``session["network"] = <current>`` overwrite), and the rest of the stale
    card can never place an order."""

    async def scenario() -> None:
        if step == "tap":
            sid = await _card_to(h, order_type="market", stop_after="product")
            next_step = h.button(trade_card_cb(sid, "lev", "5"))
        else:
            sid = await _card_to(h, order_type="limit", stop_after="size")
        h.flip_elsewhere(MAINNET)
        mark = h.screen.mark()
        if step == "tap":
            await h.tap(next_step)
        else:
            await h.say("95000")
        stamped = (h.ctx.user_data.get(trade_card.TRADE_CARD_SESSION_KEY) or {}).get("network")
        assert stamped != MAINNET, "the stale TESTNET card was silently re-stamped as a MAINNET card"
        _assert_expired_notice(h, mark)
        # Whatever is still on screen (stale buttons, a double tap) must be dead.
        for action, value in (("lev", "5"), ("size", _size_label("BTC")), ("tpsl", "skip"), ("confirm", "")):
            await h.tap(trade_card_cb(sid, action, value))

    asyncio.run(scenario())
    _assert_nothing_executed(h)


# --------------------------------------------------------------------------- #
# S7 + H1 - strategy start (the start button and the Volume fee consent)      #
# --------------------------------------------------------------------------- #
async def _build_strategy_start(h: Harness):
    await h.tap("strategy:preview:grid")
    start = h.button("strategy:start:grid:")
    return lambda: h.tap(start)


async def _build_vol_fee_consent(h: Harness):
    await h.tap("strategy:preview:vol")
    await h.tap(h.button("strategy:start:vol:"))
    agree = h.button("strategy:startok:vol:")
    return lambda: h.tap(agree)


@pytest.mark.parametrize("cross_network", _same_and_cross("strategy:start:<sid>:<product> (strategy card Start)"))
def test_strategy_start_button_is_bound_to_the_network_it_was_rendered_on(h, cross_network):
    """A GRID card rendered on TESTNET (showing testnet settings) is tapped
    after the switch. Today this starts a real MAINNET run with mainnet
    settings the card never showed."""
    mark = _drive(h, _build_strategy_start, cross_network=cross_network)
    _verdict(h, mark, cross_network, "start_user_bot")


@pytest.mark.parametrize("cross_network", _same_and_cross("strategy:startok:vol:<product> (Volume fee consent)"))
def test_vol_fee_consent_is_bound_to_the_network_it_was_quoted_on(h, cross_network):
    """The Volume Bot's charges were agreed on TESTNET. The consent key has no
    network in it, so when mainnet's quote matches, "Agree & Start" starts a
    real MAINNET taker run on consent given for testnet."""
    mark = _drive(h, _build_vol_fee_consent, cross_network=cross_network)
    _verdict(h, mark, cross_network, "start_user_bot")


# --------------------------------------------------------------------------- #
# H2 / H3 / H4 / H5 / H7 - close and cancel confirmations                     #
# --------------------------------------------------------------------------- #
def _build_close_all_via(entry: str):
    async def build(h: Harness):
        if entry.startswith("say:"):
            await h.say(entry[4:])
        else:
            await h.tap(entry)
        yes = h.button("pos:confirm_close_all")
        return lambda: h.tap(yes)

    return build


@pytest.mark.parametrize("cross_network", _same_and_cross("pos:confirm_close_all"))
@pytest.mark.parametrize(
    "entry",
    ["pos:close_all", "trade:close_all", "say:close all positions"],
    ids=["positions_card", "trade_menu", "typed_close_all"],
)
def test_close_all_confirm_button_is_bound_to_the_network_it_was_rendered_on(h, entry, cross_network):
    mark = _drive(h, _build_close_all_via(entry), cross_network=cross_network)
    _verdict(h, mark, cross_network, "close_all_positions")


async def _build_portfolio_close_all(h: Harness):
    await h.tap("portfolio:close_all_confirm")
    yes = h.button("portfolio:close_all_yes")
    return lambda: h.tap(yes)


@pytest.mark.parametrize("cross_network", _same_and_cross("portfolio:close_all_yes"))
def test_portfolio_close_all_is_bound_to_the_network_it_was_rendered_on(h, cross_network):
    mark = _drive(h, _build_portfolio_close_all, cross_network=cross_network)
    _verdict(h, mark, cross_network, "close_all_positions")


def _orders_on_both_networks(h: Harness) -> None:
    h.orders[TESTNET] = [
        {"product_id": 2, "product_name": "BTC-PERP", "side": "BUY", "amount": 0.01, "price": 59_000.0,
         "created_at": "2026-09-30T10:00:00Z", "digest": "0x" + "1a" * 32},
    ]
    # A running mainnet strategy's resting quote and its R-Grid trigger rung.
    h.orders[MAINNET] = [
        {"product_id": 2, "product_name": "BTC-PERP", "side": "BUY", "amount": 0.02, "price": 58_000.0,
         "created_at": "2026-09-30T11:00:00Z", "digest": "0x" + "2b" * 32},
        {"product_id": 2, "product_name": "BTC-PERP", "side": "SELL", "amount": 0.02, "price": 62_000.0,
         "created_at": "2026-09-30T11:00:01Z", "digest": "0x" + "3c" * 32, "is_trigger": True},
    ]


async def _build_cancel_all(h: Harness):
    _orders_on_both_networks(h)
    await h.tap("portfolio:positions")
    await h.tap(h.button("portfolio:cancel_all_confirm"))
    yes = h.button("portfolio:cancel_all_yes")
    return lambda: h.tap(yes)


@pytest.mark.parametrize("cross_network", _same_and_cross("portfolio:cancel_all_yes"))
def test_portfolio_cancel_all_is_bound_to_the_network_it_was_rendered_on(h, cross_network):
    """Today a TESTNET "Yes, cancel all" wipes MAINNET's book, including a
    running strategy's quotes and trigger rungs."""
    mark = _drive(h, _build_cancel_all, cross_network=cross_network)
    _verdict(h, mark, cross_network, "cancel_orders")


def _build_close_one_via(entry: str):
    async def build(h: Harness):
        await h.tap(entry)
        close = h.button("pos:close:BTC")
        return lambda: h.tap(close)

    return build


@pytest.mark.parametrize("cross_network", _same_and_cross("pos:close:<product>"))
@pytest.mark.parametrize("entry", ["trade:close", "pos:view"], ids=["close_menu", "positions_card"])
def test_close_position_button_is_bound_to_the_network_it_was_rendered_on(h, entry, cross_network):
    mark = _drive(h, _build_close_one_via(entry), cross_network=cross_network)
    _verdict(h, mark, cross_network, "close_position")


async def _build_positional_cancel(h: Harness):
    # The cached render lacked the digest, so the row got the legacy positional
    # callback (``portfolio:cancel_order:<idx>``). The fresh snapshot the tap
    # resolves against has it.
    _orders_on_both_networks(h)
    h.cached_without_digests = True
    await h.tap("portfolio:positions")
    cancel = h.button("portfolio:cancel_order:")
    if cancel.split(":")[2] == "d":
        raise HarnessError(f"expected the positional cancel form, got {cancel!r}")
    return lambda: h.tap(cancel)


@pytest.mark.parametrize("cross_network", _same_and_cross("portfolio:cancel_order:<idx> (positional)"))
def test_positional_order_cancel_is_bound_to_the_network_it_was_rendered_on(h, cross_network):
    mark = _drive(h, _build_positional_cancel, cross_network=cross_network)
    _verdict(h, mark, cross_network, "cancel_orders")


def _count_book_reads(h: Harness, monkeypatch, *, move_to: str | None = None) -> list:
    """Wrap the fresh-snapshot read. With ``move_to``, the user's network
    changes DURING the read (a switch served by another process)."""
    reads: list = []

    async def _snapshot_for_user(_uid, **_k):
        reads.append(h.network)
        if move_to is not None:
            h.network = move_to
        return h.snapshot(h.network)

    monkeypatch.setattr(portfolio_deck, "snapshot_for_user", _snapshot_for_user)
    return reads


@pytest.mark.parametrize("build", [_build_cancel_all, _build_positional_cancel], ids=["cancel_all", "cancel_one"])
def test_stale_cancel_is_refused_before_the_book_is_even_read(h, monkeypatch, build):
    reads = _count_book_reads(h, monkeypatch)
    mark = _drive(h, build, cross_network=True, change="elsewhere")
    assert reads == [], "a stale cancel read the other network's book"
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark)


@pytest.mark.parametrize("build", [_build_cancel_all, _build_positional_cancel], ids=["cancel_all", "cancel_one"])
def test_cancel_refuses_when_the_network_moves_during_the_book_read(h, monkeypatch, build):
    """Second layer: the confirm matched when tapped, but the snapshot came
    back for the other network. Nothing may be cancelled on it."""

    async def scenario() -> int:
        confirm = await build(h)
        _count_book_reads(h, monkeypatch, move_to=MAINNET)
        mark = h.screen.mark()
        await confirm()
        return mark

    mark = asyncio.run(scenario())
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark)


# --------------------------------------------------------------------------- #
# H6 - vault deposit / withdraw confirms                                       #
# --------------------------------------------------------------------------- #
async def _build_vault_deposit_preset(h: Harness):
    await h.tap("vault:deposit")
    await h.tap(h.button("vault:deposit:preset:"))
    confirm = h.button("vault:deposit:confirm:")
    return lambda: h.tap(confirm)


async def _build_vault_deposit_custom(h: Harness):
    await h.tap("vault:deposit:custom")
    await h.say("250")
    confirm = h.button("vault:deposit:confirm:")
    return lambda: h.tap(confirm)


async def _build_vault_withdraw(h: Harness):
    await h.tap("vault:withdraw")
    await h.tap(h.button("vault:withdraw:pct:"))
    confirm = h.button("vault:withdraw:confirm:")
    return lambda: h.tap(confirm)


@pytest.mark.parametrize("cross_network", _same_and_cross("vault:deposit|withdraw:confirm:<amount>"))
@pytest.mark.parametrize(
    "build, executed",
    [
        (_build_vault_deposit_preset, "deposit_to_vault"),
        (_build_vault_deposit_custom, "deposit_to_vault"),
        (_build_vault_withdraw, "withdraw_from_vault"),
    ],
    ids=["deposit_preset", "deposit_custom_amount", "withdraw_pct"],
)
def test_vault_confirm_is_bound_to_the_network_it_was_rendered_on(h, build, executed, cross_network):
    """A TESTNET vault confirm tapped after the switch mints or burns NLP
    with REAL mainnet USDT0 today."""
    mark = _drive(h, build, cross_network=cross_network)
    _verdict(h, mark, cross_network, executed)
    if cross_network:
        assert "vault_op_inflight" not in h.ctx.user_data, "a refused confirm must not hold the in-flight lock"


# --------------------------------------------------------------------------- #
# LATENT stateful previews: safe in-process only because the switch pops     #
# their user_data key. The network changes where that clear cannot reach.    #
# --------------------------------------------------------------------------- #
async def _build_text_close_all(h: Harness):
    await h.say("close all positions")
    if not h.ctx.user_data.get(messages.PENDING_TEXT_CLOSE_ALL_KEY):
        raise HarnessError("typing 'close all positions' did not arm the typed confirm")
    return lambda: h.say("confirm")


async def _build_legacy_inline_trade(h: Harness):
    await h.tap("trade:long")
    await h.tap(h.button("product:long:BTC"))
    await h.tap(h.button("size:long:BTC:"))
    await h.tap(h.button("leverage:long:BTC:"))
    confirm = h.button("exec_trade:")
    return lambda: h.tap(confirm)


async def _build_reply_keyboard_trade_flow(h: Harness):
    # Only reachable with DUAL_MODE_CARD_FLOW=false (the default is the card).
    for label in ("Trade", "🟢 Long", "📈 Market", "BTC", "5x", _size_label("BTC"), "⏭ Skip"):
        await h.say(label)
    if (h.ctx.user_data.get("trade_flow") or {}).get("state") != "confirm":
        raise HarnessError(f"trade_flow did not reach confirm: {h.ctx.user_data.get('trade_flow')!r}")
    return lambda: h.say("✅ Confirm Trade")


async def _build_copy_wizard(h: Harness):
    await h.tap("copy:start:7")
    for prefix in ("copy:budget:250", "copy:risk:1", "copy:lev:5", "copy:csl:0", "copy:ctp:0"):
        await h.tap(h.button(prefix))
    confirm = h.button("copy:confirm")
    return lambda: h.tap(confirm)


@pytest.mark.parametrize(
    "cross_network",
    _same_and_cross("an unbound in-memory preview (the switch's user_data clear is its only guard)"),
)
@pytest.mark.parametrize(
    "build, executed",
    [
        (_build_text_close_all, "close_all_positions"),
        (_build_legacy_inline_trade, "execute_market_order"),
        (_build_reply_keyboard_trade_flow, "execute_market_order"),
        (_build_copy_wizard, "start_copy"),
    ],
    ids=["typed_close_all_confirm", "legacy_exec_trade", "reply_keyboard_trade_flow", "copy_confirm"],
)
def test_in_memory_preview_is_bound_to_its_network_not_to_the_switch_clear(h, monkeypatch, build, executed, cross_network):
    if build is _build_reply_keyboard_trade_flow:
        monkeypatch.setattr(trade_card, "DUAL_MODE_CARD_FLOW", False)
    mark = _drive(h, build, cross_network=cross_network, change="elsewhere")
    _verdict(h, mark, cross_network, executed)


def test_pending_text_trade_without_a_network_stamp_is_refused(h):
    """``handle_pending_text_trade_confirmation`` checks the network only
    ``if pending_network:``. A payload hydrated from bot_state without a
    stamp therefore executes on whatever network the user is on now."""
    h.bot_state[f"text_trade_pending:{UID}"] = {
        "direction": "long", "order_type": "market", "product": "BTC", "size": 0.01,
        "leverage": 5, "slippage_pct": 1.0, "price": 60_000.0,
    }

    async def scenario() -> int:
        h.flip_elsewhere(MAINNET)
        mark = h.screen.mark()
        await h.say("confirm")
        return mark

    mark = asyncio.run(scenario())
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark, names=False)
    assert f"text_trade_pending:{UID}" not in h.bot_state, "the refused preview must be discarded"


# --------------------------------------------------------------------------- #
# Already network-bound (regression locks, pass today)                         #
# --------------------------------------------------------------------------- #
async def _build_text_trade(h: Harness):
    message = _Message(h.screen, message_id=h.screen.next_message_id(), text="long 0.01 BTC 5x market")
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=UID), effective_chat=_Chat(UID))
    handled = await intent_handlers.handle_trade_intent_message(update, h.ctx, UID, message.text)
    if not handled or not h.ctx.user_data.get(intent_handlers.PENDING_TEXT_TRADE_KEY):
        raise HarnessError(f"the text trade preview was not built: {h.screen.texts()[-2:]!r}")
    return lambda: h.say("confirm")


@pytest.mark.parametrize(
    "change",
    [None, "switch", "elsewhere"],
    ids=["same_network", "cross_network_via_switch", "cross_network_elsewhere"],
)
def test_text_trade_preview_is_bound_to_its_network(h, change):
    """The typed-trade preview is already stamped with its network (keep it so)."""
    cross_network = change is not None
    _drive(h, _build_text_trade, cross_network=cross_network, change=change or "switch")
    if cross_network:
        _assert_nothing_executed(h)
        assert intent_handlers.PENDING_TEXT_TRADE_KEY not in h.ctx.user_data
        assert f"text_trade_pending:{UID}" not in h.bot_state
    else:
        _assert_executed_on_testnet(h, "execute_market_order")


# --------------------------------------------------------------------------- #
# The switch's own clean-up of in-memory previews                              #
# --------------------------------------------------------------------------- #
_SWITCH_CLEARS = [
    "pending_text_trade",
    "pending_text_close_all",
    "pending_trade",
    "trade_flow",
    "copy_setup",
    "vault_pending_amount",
    trade_card.TRADE_CARD_SESSION_KEY,
    "vol_fee_quote",
]


@pytest.mark.parametrize("key", _SWITCH_CLEARS)
def test_successful_network_switch_clears_in_memory_previews(h, key):
    h.ctx.user_data[key] = {"network": TESTNET, "sentinel": True}
    asyncio.run(h.switch(MAINNET))
    assert key not in h.ctx.user_data, f"{key} survived a successful network switch"


def test_successful_network_switch_clears_persisted_previews(h):
    """The bot_state copies (what a multi-worker hop hydrates from) go too."""
    h.bot_state[f"text_trade_pending:{UID}"] = {"network": TESTNET, "product": "BTC"}
    h.bot_state[f"text_close_all_pending:{UID}"] = {"_ts": time.time()}
    asyncio.run(h.switch(MAINNET))
    assert h.bot_state == {}, f"persisted previews survived the switch: {h.bot_state!r}"


def test_network_switch_keeps_the_vault_in_flight_guard(h):
    """``vault_op_inflight`` guards a mint/burn that is still round-tripping.
    Dropping it mid-operation would re-open the double-execute window."""
    h.ctx.user_data["vault_op_inflight"] = "deposit"
    asyncio.run(h.switch(MAINNET))
    assert h.ctx.user_data.get("vault_op_inflight") == "deposit"


def _refuse_switches(h: Harness, monkeypatch, *, error: str) -> None:
    """The fail-closed switch refuses: the user stays on the old network."""

    def _refused(_tid, target):
        items = ()
        if error == network_switch.ERR_NOT_CONFIRMED:
            items = (network_switch.SwitchItem(
                kind=network_switch.STRATEGY, outcome=network_switch.FAILED,
                label="grid", product="BTC", error="rate limited",
            ),)
        return network_switch.NetworkSwitchResult(
            switched=False, from_network=h.network, to_network=target, items=items, error=error,
        )

    _swap(monkeypatch, network_switch, "switch_network", _refused)


@pytest.mark.parametrize("error", [network_switch.ERR_NOT_CONFIRMED, network_switch.ERR_FLIP_FAILED])
@pytest.mark.parametrize("key", _SWITCH_CLEARS)
def test_refused_network_switch_keeps_every_preview(h, monkeypatch, key, error):
    """A refused switch leaves the user on the network their previews were
    built on, so they are still valid: nothing may be cleared, in memory or
    in bot_state."""
    _refuse_switches(h, monkeypatch, error=error)
    h.ctx.user_data[key] = {"network": TESTNET, "sentinel": True}
    h.bot_state[f"text_trade_pending:{UID}"] = {"network": TESTNET, "product": "BTC"}
    asyncio.run(h.tap(f"mode:{MAINNET}", message_id=MODE_MSG))
    assert h.network == TESTNET, "the refused switch must not change the network"
    assert h.ctx.user_data.get(key) == {"network": TESTNET, "sentinel": True}, f"{key} was cleared by a refused switch"
    assert f"text_trade_pending:{UID}" in h.bot_state, "a refused switch cleared a persisted preview"


def test_switch_to_the_already_active_network_keeps_previews(h):
    h.ctx.user_data[trade_card.TRADE_CARD_SESSION_KEY] = {"network": TESTNET, "sentinel": True}
    h.bot_state[f"text_trade_pending:{UID}"] = {"network": TESTNET, "product": "BTC"}
    asyncio.run(h.tap(f"mode:{TESTNET}", message_id=MODE_MSG))
    assert h.ctx.user_data.get(trade_card.TRADE_CARD_SESSION_KEY) == {"network": TESTNET, "sentinel": True}
    assert f"text_trade_pending:{UID}" in h.bot_state


def test_trade_card_still_confirms_on_its_network_after_a_refused_switch(h, monkeypatch):
    """No over-blocking: the switch was refused, so the card's network is still
    the user's network and Confirm places the order there, exactly once."""
    _refuse_switches(h, monkeypatch, error=network_switch.ERR_NOT_CONFIRMED)

    async def scenario() -> None:
        sid = await _card_to(h, order_type="market")
        confirm = h.button(trade_card_cb(sid, "confirm"))
        await h.tap(f"mode:{MAINNET}", message_id=MODE_MSG)
        await h.tap(confirm)

    asyncio.run(scenario())
    _assert_executed_on_testnet(h, "execute_market_order")


@pytest.mark.parametrize("switched", [True, False], ids=["switched", "refused"])
def test_wallet_network_switch_clears_previews_only_when_it_switches(h, monkeypatch, switched):
    """The stale ``wallet:network:*`` path runs the same switch and the same
    clean-up as Execution Mode."""
    if not switched:
        _refuse_switches(h, monkeypatch, error=network_switch.ERR_NOT_CONFIRMED)
    for key in (trade_card.TRADE_CARD_SESSION_KEY, "vol_fee_quote", "pending_text_trade"):
        h.ctx.user_data[key] = {"network": TESTNET}
    h.bot_state[f"text_trade_pending:{UID}"] = {"network": TESTNET, "product": "BTC"}
    asyncio.run(h.tap(f"wallet:network:{MAINNET}", message_id=MODE_MSG))
    assert h.network == (MAINNET if switched else TESTNET)
    for key in (trade_card.TRADE_CARD_SESSION_KEY, "vol_fee_quote", "pending_text_trade"):
        assert (key in h.ctx.user_data) is (not switched), key
    assert (f"text_trade_pending:{UID}" in h.bot_state) is (not switched)


# --------------------------------------------------------------------------- #
# Same network: exactly the old behaviour, with the network now explicit       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("order_type", ["market", "limit"])
def test_same_network_trade_card_places_exactly_what_the_card_showed(h, order_type):
    """The binding adds a check, nothing else: the order carries the card's
    own fields, and the network it was built on is now passed explicitly."""

    async def scenario() -> None:
        sid = await _card_to(h, order_type=order_type)
        await h.tap(h.button(trade_card_cb(sid, "confirm")))

    asyncio.run(scenario())
    size = float(SIZE_PRESETS["BTC"][2])
    if order_type == "market":
        assert h.venue.calls == [(
            "execute_market_order",
            (UID, "BTC", size),
            {"is_long": True, "leverage": 5, "slippage_pct": 1, "tp_price": None, "sl_price": None,
             "network": TESTNET},
        )]
    else:
        assert h.venue.calls == [(
            "execute_limit_order",
            (UID, "BTC", size, 95_000.0),
            {"is_long": True, "leverage": 5, "tp_price": None, "sl_price": None, "network": TESTNET},
        )]


def test_a_card_built_after_the_switch_trades_on_the_new_network(h):
    """No over-blocking: a card opened AFTER the switch belongs to the new
    network and confirms there."""

    async def scenario() -> None:
        await h.switch(MAINNET)
        sid = await _card_to(h, order_type="market")
        await h.tap(h.button(trade_card_cb(sid, "confirm")))

    asyncio.run(scenario())
    assert [(c[0], c[2].get("network")) for c in h.venue.calls] == [("execute_market_order", MAINNET)]


# --------------------------------------------------------------------------- #
# Buttons rendered before the binding existed carry no network: fail closed    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "data",
    [
        "pos:confirm_close_all",
        "portfolio:close_all_yes",
        "portfolio:cancel_all_yes",
        "pos:close:BTC",
        "portfolio:cancel_order:0",
        "strategy:start:grid:BTC",
        "strategy:startok:vol:KBTC",
        "vault:deposit:confirm:100.0",
        "vault:withdraw:confirm:10.0",
    ],
)
def test_untagged_legacy_confirm_button_is_refused_even_on_the_same_network(h, data):
    """An untagged button cannot say which network it was rendered on, so it
    is refused (fail closed) rather than executed on whatever network the user
    is on now. One stale message costs the user a re-tap; the other way round
    costs a real order."""
    _orders_on_both_networks(h)
    mark = h.screen.mark()
    asyncio.run(h.tap(data))
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark, names=False)


def test_untagged_digest_cancel_is_still_honoured_on_its_own_network(h):
    """The digest form is chain-specific (safe by construction), so a legacy
    untagged digest button keeps working on the network it was read from..."""
    _orders_on_both_networks(h)
    asyncio.run(h.tap("portfolio:cancel_order:d:" + "1a" * 8))
    _assert_executed_on_testnet(h, "cancel_orders")


def test_untagged_digest_cancel_never_matches_on_the_other_network(h):
    """...and can never match an order on the other network."""
    _orders_on_both_networks(h)
    h.flip_elsewhere(MAINNET)
    asyncio.run(h.tap("portfolio:cancel_order:d:" + "1a" * 8))
    _assert_nothing_executed(h)


# --------------------------------------------------------------------------- #
# Adjacent money-path bug in the same confirm family                           #
# --------------------------------------------------------------------------- #
def test_close_all_button_disarms_the_typed_close_all_confirm(h):
    """The user types "close all positions", then taps the prompt's
    "Yes, Close All" button: everything closes. Their next "yes" (to anything
    at all) must not close the account a second time."""

    async def scenario() -> None:
        await h.say("close all positions")
        await h.tap(h.button("pos:confirm_close_all"))
        await h.say("yes")

    asyncio.run(scenario())
    _assert_executed_on_testnet(h, "close_all_positions", times=1)
