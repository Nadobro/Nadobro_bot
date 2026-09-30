"""PREVIEW-NETWORK-BIND guardrails: a preview built on one Nado network must
never confirm on the other.

The testnet<->mainnet switch (Execution Mode -> ``callbacks._handle_mode`` ->
``strategy/network_switch.switch_network``) flips ``users.network_mode``. The
bug these lock out: every confirm path resolved the network it executed on from
``users.network_mode`` AT CONFIRM TIME, so a preview the user built and reviewed
on TESTNET (product list, size, leverage, fees and settings all read from
testnet) confirmed as a REAL MAINNET action. The headline case was the guided
trade card, which re-stamped ``session["network"]`` with the current network on
every tap and survived the switch. Each preview is now bound to the network it
was built on (handlers/network_guard.py) and is refused anywhere else.

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
  as a success. It is used for confirms the switch's own clean-up cannot reach
  (stateless buttons, which carry their network in ``callback_data``).
* ``Harness.flip_elsewhere`` changes ``users.network_mode`` but leaves this
  process's ``context.user_data`` untouched, as when another machine served the
  switch during a rolling deploy. It is used for stateful previews, so that the
  preview's own network stamp is what refuses it, never the switch's clean-up
  of ``user_data``.

Every cross-network scenario here was a strict xfail until previews were bound
to the network they were built on; they are plain regression tests now.
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
    network_guard,
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
from src.nadobro.trading import copy_discovery, copy_service, text_trade_pending, trade_service  # noqa: E402
from src.nadobro.core import user_rate_limit  # noqa: E402
from src.nadobro.users import (  # noqa: E402
    admin_service,
    onboarding_service,
    settings_service,
    user_service,
    wallet_pending_flow,
)
from src.nadobro.vault import nlp_vault_service, vault_deposit_watch_service  # noqa: E402
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
        # Open positions per network (what the Portfolio views render).
        self.positions: dict[str, list[dict]] = {TESTNET: [], MAINNET: []}
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
        positions = [dict(p) for p in self.positions.get(network, [])]
        snap.update(stale=False, monotonic_ts=time.time(), open_orders=orders, positions=positions)
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
        # Leaderboard "Copy This Trader": the private trader row is trader 7.
        mp.setattr(copy_discovery, "follow_from_leaderboard", lambda *_a, **_k: (True, "following", 7))
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


# Executors with no ``network`` parameter: they resolve the network themselves,
# so for them the confirm-time check is the whole guard. Every other executor is
# handed the preview's stamped network explicitly (the cross-process defence),
# and a dropped pass-through must fail here, not pass as ``network=None``.
_RESOLVES_ITS_OWN_NETWORK = frozenset({"start_user_bot", "deposit_to_vault", "withdraw_from_vault"})


def _assert_executed_on(h: Harness, name: str, network: str, *, times: int = 1) -> None:
    calls = [c for c in h.venue.calls if c[0] == name]
    assert len(calls) == times, f"expected {times} x {name}, venue saw {h.venue.calls!r}"
    for _name, _args, kwargs in calls:
        if name in _RESOLVES_ITS_OWN_NETWORK:
            assert "network" not in kwargs, f"{name} now takes a network; drop it from the exemption: {kwargs!r}"
        else:
            assert kwargs.get("network") == network, (
                f"{name} must be handed the preview's network ({network}) explicitly, got {kwargs.get('network')!r}"
            )


def _assert_executed_on_testnet(h: Harness, name: str, *, times: int = 1) -> None:
    _assert_executed_on(h, name, TESTNET, times=times)


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
    became MAINNET: the order is not placed, and the stale card is cleared."""
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
    after the switch: no run starts, least of all a MAINNET run with mainnet
    settings the card never showed."""
    mark = _drive(h, _build_strategy_start, cross_network=cross_network)
    _verdict(h, mark, cross_network, "start_user_bot")


@pytest.mark.parametrize("cross_network", _same_and_cross("strategy:startok:vol:<product> (Volume fee consent)"))
def test_vol_fee_consent_is_bound_to_the_network_it_was_quoted_on(h, cross_network):
    """The Volume Bot's charges were agreed on TESTNET. Consent given for
    testnet charges is not consent for a mainnet run, even when mainnet's quote
    matches number for number: "Agree & Start" starts nothing there."""
    mark = _drive(h, _build_vol_fee_consent, cross_network=cross_network)
    _verdict(h, mark, cross_network, "start_user_bot")


# --------------------------------------------------------------------------- #
# H2 / H3 / H4 / H5 / H7 - close and cancel confirmations                     #
# --------------------------------------------------------------------------- #
def _build_close_all_via(entry: str):
    async def build(h: Harness):
        if entry.startswith("say:"):
            await h.say(entry[4:])
        elif entry.startswith("view:"):
            # Render the view, then tap the "Close All" opener it rendered.
            view, opener = entry[len("view:"):].split("|")
            await h.tap(view)
            await h.tap(h.button(opener))
        else:
            await h.tap(entry)
        yes = h.button("pos:confirm_close_all")
        return lambda: h.tap(yes)

    return build


@pytest.mark.parametrize("cross_network", _same_and_cross("pos:confirm_close_all"))
@pytest.mark.parametrize(
    "entry",
    [
        "view:pos:view|pos:close_all",
        # Nothing renders the trade-menu opener any more (it only survives on
        # old messages); this is the network-tagged form it has to carry.
        f"trade:close_all:{TESTNET}",
        "say:close all positions",
    ],
    ids=["positions_card", "trade_menu", "typed_close_all"],
)
def test_close_all_confirm_button_is_bound_to_the_network_it_was_rendered_on(h, entry, cross_network):
    mark = _drive(h, _build_close_all_via(entry), cross_network=cross_network)
    _verdict(h, mark, cross_network, "close_all_positions")


def _positions_on_both_networks(h: Harness) -> None:
    h.positions[TESTNET] = [
        {"product_id": 2, "product_name": "BTC-PERP", "symbol": "BTC-PERP", "is_long": True,
         "amount": "0.01", "avg_entry_price": "60000", "notional_value": "600", "est_pnl": "1"},
    ]
    h.positions[MAINNET] = [
        {"product_id": 4, "product_name": "ETH-PERP", "symbol": "ETH-PERP", "is_long": False,
         "amount": "-0.5", "avg_entry_price": "2500", "notional_value": "1250", "est_pnl": "-3"},
    ]


def _build_portfolio_close_all_from(view: str):
    async def build(h: Harness):
        _positions_on_both_networks(h)
        await h.tap(view)
        await h.tap(h.button("portfolio:close_all_confirm"))
        yes = h.button("portfolio:close_all_yes")
        return lambda: h.tap(yes)

    return build


@pytest.mark.parametrize("cross_network", _same_and_cross("portfolio:close_all_yes"))
@pytest.mark.parametrize("view", ["portfolio:view", "portfolio:positions"], ids=["deck", "positions_view"])
def test_portfolio_close_all_is_bound_to_the_network_it_was_rendered_on(h, view, cross_network):
    mark = _drive(h, _build_portfolio_close_all_from(view), cross_network=cross_network)
    _verdict(h, mark, cross_network, "close_all_positions")


def _rendered_confirms(h: Harness, since: int) -> list[str]:
    """Every executing confirm button rendered since ``since``."""
    found = []
    for _text, markup in h.screen.events[since:]:
        for row in getattr(markup, "inline_keyboard", None) or ():
            for btn in row:
                data = str(getattr(btn, "callback_data", "") or "")
                if data.startswith(("portfolio:close_all_yes", "portfolio:cancel_all_yes", "pos:confirm_close_all")):
                    found.append(data)
    return found


@pytest.mark.parametrize("change", ["switch", "elsewhere"])
@pytest.mark.parametrize(
    "view, opener",
    [
        ("portfolio:view", "portfolio:close_all_confirm"),
        ("portfolio:positions", "portfolio:close_all_confirm"),
        ("portfolio:positions", "portfolio:cancel_all_confirm"),
        ("pos:view", "pos:close_all"),
    ],
    ids=["deck_close_all", "positions_close_all", "positions_cancel_all", "positions_card_close_all"],
)
def test_stale_view_bulk_opener_cannot_open_a_confirm_on_the_new_network(h, view, opener, change):
    """R1-BULK-OPENER-UNBOUND: a Portfolio / Positions view rendered on
    TESTNET (its header says so) stays on screen across the switch. Its bulk
    "Close All" / "Cancel All" opener must not open a confirm for the network
    current at the second tap: that confirm names no network, so two taps on a
    TESTNET view would close every MAINNET position or cancel every MAINNET
    order (a running strategy's quotes, trigger rungs and stop backstops
    included). The opener is refused, and no confirm is rendered."""
    _positions_on_both_networks(h)
    _orders_on_both_networks(h)

    async def scenario() -> int:
        await h.tap(view)
        stale_opener = h.button(opener)
        if not stale_opener.endswith(f":{TESTNET}"):
            raise HarnessError(f"the view rendered an opener not bound to {TESTNET}: {stale_opener!r}")
        if change == "switch":
            await h.switch(MAINNET)
        else:
            h.flip_elsewhere(MAINNET)
        mark = h.screen.mark()
        await h.tap(stale_opener)
        return mark

    mark = asyncio.run(scenario())
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark)
    assert _rendered_confirms(h, mark) == [], "a stale view opened a confirm on the new network"


@pytest.mark.parametrize(
    "view, opener, confirm",
    [
        ("portfolio:positions", "portfolio:cancel_all_confirm", "portfolio:cancel_all_yes"),
        ("portfolio:view", "portfolio:close_all_confirm", "portfolio:close_all_yes"),
        ("pos:view", "pos:close_all", "pos:confirm_close_all"),
    ],
    ids=["cancel_all", "deck_close_all", "positions_card_close_all"],
)
def test_bulk_opener_binds_its_confirm_to_the_views_network(h, view, opener, confirm):
    """Same network: the opener opens the usual confirm, bound to the view's
    network (the one the opener carries), exactly as before."""
    _positions_on_both_networks(h)
    _orders_on_both_networks(h)

    async def scenario() -> None:
        await h.tap(view)
        await h.tap(h.button(opener))

    asyncio.run(scenario())
    assert h.button(confirm) == f"{confirm}:{TESTNET}"
    _assert_nothing_executed(h)


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
    """A TESTNET "Yes, cancel all" cancels nothing on MAINNET's book (whose
    orders include a running strategy's quotes and trigger rungs)."""
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
    """A TESTNET vault confirm tapped after the switch mints or burns
    nothing: never NLP against real mainnet USDT0."""
    mark = _drive(h, build, cross_network=cross_network)
    _verdict(h, mark, cross_network, executed)
    if cross_network:
        assert "vault_op_inflight" not in h.ctx.user_data, "a refused confirm must not hold the in-flight lock"


# --------------------------------------------------------------------------- #
# In-memory previews: each carries its own network stamp, checked when it is  #
# confirmed. The network changes where the switch's user_data clear cannot    #
# reach, so the stamp alone must refuse it.                                   #
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


async def _copy_wizard_steps(h: Harness):
    for prefix in ("copy:budget:250", "copy:risk:1", "copy:lev:5", "copy:csl:0", "copy:ctp:0"):
        await h.tap(h.button(prefix))
    confirm = h.button("copy:confirm")
    return lambda: h.tap(confirm)


async def _build_copy_wizard(h: Harness):
    await h.tap("copy:start:7")
    return await _copy_wizard_steps(h)


async def _build_copy_leaderboard_follow(h: Harness):
    # The leaderboard's "Copy This Trader" seeds the same wizard slot.
    await h.tap("copy:lb:follow:0x" + "c" * 40)
    return await _copy_wizard_steps(h)


async def _open_legacy_custom_size(h: Harness) -> str:
    await h.tap("trade:long")
    await h.tap(h.button("product:long:BTC"))
    await h.tap(h.button("size:long:BTC:custom"))
    return "0.01"


async def _open_legacy_limit(h: Harness) -> str:
    await h.tap("trade:limit_long")
    await h.tap(h.button("product:limit_long:BTC"))
    return "0.01 95000"


def _build_legacy_typed(open_flow):
    async def build(h: Harness):
        typed = await open_flow(h)
        await h.say(typed)
        confirm = h.button("exec_trade:")
        return lambda: h.tap(confirm)

    return build


@pytest.mark.parametrize(
    "cross_network",
    _same_and_cross("an in-memory preview (its own stamp, not the switch's user_data clear)"),
)
@pytest.mark.parametrize(
    "build, executed",
    [
        (_build_text_close_all, "close_all_positions"),
        (_build_legacy_inline_trade, "execute_market_order"),
        (_build_legacy_typed(_open_legacy_custom_size), "execute_market_order"),
        (_build_legacy_typed(_open_legacy_limit), "execute_limit_order"),
        (_build_reply_keyboard_trade_flow, "execute_market_order"),
        (_build_copy_wizard, "start_copy"),
        (_build_copy_leaderboard_follow, "start_copy"),
    ],
    ids=[
        "typed_close_all_confirm", "legacy_exec_trade", "legacy_custom_size", "legacy_limit",
        "reply_keyboard_trade_flow", "copy_confirm", "copy_leaderboard_follow",
    ],
)
def test_in_memory_preview_is_bound_to_its_network_not_to_the_switch_clear(h, monkeypatch, build, executed, cross_network):
    if build is _build_reply_keyboard_trade_flow:
        monkeypatch.setattr(trade_card, "DUAL_MODE_CARD_FLOW", False)
    mark = _drive(h, build, cross_network=cross_network, change="elsewhere")
    _verdict(h, mark, cross_network, executed)


def _record_pricing(h: Harness, monkeypatch) -> list:
    """Record every read-only client handed out for pricing (by network)."""
    priced: list = []
    serve = user_service.get_user_readonly_client

    def _recording(tid, network=None, **kwargs):
        priced.append(network)
        return serve(tid, network=network, **kwargs)

    _swap(monkeypatch, user_service, "get_user_readonly_client", _recording)
    return priced


def _rendered_buttons(h: Harness, since: int, prefix: str) -> list[str]:
    return [
        str(btn.callback_data)
        for _text, markup in h.screen.events[since:]
        for row in (getattr(markup, "inline_keyboard", None) or ())
        for btn in row
        if str(getattr(btn, "callback_data", "") or "").startswith(prefix)
    ]


@pytest.mark.parametrize("open_flow", [_open_legacy_custom_size, _open_legacy_limit], ids=["custom_size", "limit"])
def test_legacy_typed_size_is_neither_priced_nor_previewed_after_the_network_changed(h, monkeypatch, open_flow):
    """The product was picked on TESTNET; the network changes before the size
    (or size and price) is typed. The typed value must not be priced or
    previewed at all: the pending trade is expired on the spot."""
    priced = _record_pricing(h, monkeypatch)

    async def scenario() -> int:
        typed = await open_flow(h)
        h.flip_elsewhere(MAINNET)
        priced.clear()
        mark = h.screen.mark()
        await h.say(typed)
        return mark

    mark = asyncio.run(scenario())
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark)
    assert priced == [], f"the stale pending trade was priced: {priced!r}"
    assert _rendered_buttons(h, mark, "exec_trade:") == [], "a stale pending trade was previewed"
    assert "pending_trade" not in h.ctx.user_data


def test_reply_keyboard_trade_flow_is_neither_priced_nor_previewed_after_the_network_changed(h, monkeypatch):
    """The reply-keyboard flow started on TESTNET; the network changes before
    its last step. Reaching the confirm step must not price or preview it."""
    monkeypatch.setattr(trade_card, "DUAL_MODE_CARD_FLOW", False)
    priced = _record_pricing(h, monkeypatch)

    async def scenario() -> int:
        for label in ("Trade", "🟢 Long", "📈 Market", "BTC", "5x", _size_label("BTC")):
            await h.say(label)
        if not h.ctx.user_data.get("trade_flow"):
            raise HarnessError("the reply-keyboard trade flow did not start")
        h.flip_elsewhere(MAINNET)
        priced.clear()
        mark = h.screen.mark()
        await h.say("⏭ Skip")
        return mark

    mark = asyncio.run(scenario())
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark)
    assert priced == [], f"the stale flow was priced: {priced!r}"
    assert "trade_flow" not in h.ctx.user_data, "the stale flow reached its confirm step"


@pytest.mark.parametrize(
    "build, executed, slot",
    [
        (_build_copy_wizard, "start_copy", "copy_setup"),
        (_build_legacy_inline_trade, "execute_market_order", "pending_trade"),
        (_build_legacy_typed(_open_legacy_custom_size), "execute_market_order", "pending_trade"),
    ],
    ids=["copy_confirm", "legacy_exec_trade", "legacy_custom_size"],
)
def test_stale_confirm_cannot_confirm_a_newer_preview_built_on_the_other_network(h, build, executed, slot):
    """R1-COPY-EXECTRADE-SINGLE-SLOT: these previews live in ONE user_data
    slot. Card A is built on TESTNET, the user switches to MAINNET and builds
    card B there (same slot). Tapping A's Confirm must not start B: A showed
    testnet values and was confirmed as such. B survives and its own Confirm
    places it, once, on mainnet."""

    async def scenario():
        stale_confirm = await build(h)
        await h.switch(MAINNET)
        fresh_confirm = await build(h)
        mark = h.screen.mark()
        await stale_confirm()
        after_stale = list(h.venue.calls)
        slot_after_stale = dict(h.ctx.user_data.get(slot) or {})
        await fresh_confirm()
        return mark, after_stale, slot_after_stale

    mark, after_stale, slot_after_stale = asyncio.run(scenario())
    assert after_stale == [], f"a TESTNET card confirmed the newer MAINNET preview: {after_stale!r}"
    _assert_expired_notice(h, mark)
    assert slot_after_stale.get("network") == MAINNET, "refusing the stale card discarded the valid preview"
    _assert_executed_on(h, executed, MAINNET)


@pytest.mark.parametrize("network", [None, "", "devnet"])
def test_copy_start_payload_without_a_network_is_refused(h, network):
    """R2-4: the executor never resolves a copy's network itself (that fell
    back to the CURRENT network, i.e. failed open). A payload that does not
    carry a valid one starts nothing."""
    payload = {"type": "start_copy", "trader_id": 7, "budget_usd": 250.0, "risk_factor": 1.0, "max_leverage": 5.0}
    if network is not None:
        payload["network"] = network
    query = _Query(h.screen, "copy:confirm", message_id=CARD_MSG)
    mark = h.screen.mark()
    asyncio.run(messages.execute_action_directly(query, h.ctx, UID, payload))
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark, names=False)


def test_pending_text_trade_without_a_network_stamp_is_refused(h):
    """A text-trade payload with no network stamp (hydrated from bot_state,
    written before stamps existed) is refused and discarded: it cannot say
    which network it was priced on, so it never executes on the current one."""
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
# The typed-trade preview (stamped since before this fix): regression lock     #
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
        # Bulk openers: an untagged one cannot say which view it was on.
        "portfolio:close_all_confirm",
        "portfolio:cancel_all_confirm",
        "pos:close_all",
        "trade:close_all",
    ],
)
def test_untagged_legacy_confirm_button_is_refused_even_on_the_same_network(h, data):
    """An untagged button cannot say which network it was rendered on, so it
    is refused (fail closed) rather than executed on whatever network the user
    is on now. One stale message costs the user a re-tap; the other way round
    costs a real order."""
    _positions_on_both_networks(h)
    _orders_on_both_networks(h)
    mark = h.screen.mark()
    asyncio.run(h.tap(data))
    _assert_nothing_executed(h)
    _assert_expired_notice(h, mark, names=False)
    assert _rendered_confirms(h, mark) == [], "an untagged opener still opened a confirm"


@pytest.mark.parametrize(
    "build, untagged, slot",
    [
        (_build_copy_wizard, "copy:confirm", "copy_setup"),
        (_build_legacy_inline_trade, "exec_trade:pending", "pending_trade"),
    ],
    ids=["copy_confirm", "legacy_exec_trade"],
)
def test_untagged_single_slot_confirm_is_refused_and_keeps_the_valid_preview(h, build, untagged, slot):
    """A legacy untagged confirm over a valid, same-network slot is refused
    (it cannot say which preview it showed), and the valid preview survives:
    its own, tagged Confirm still works."""

    async def scenario() -> int:
        confirm = await build(h)
        mark = h.screen.mark()
        await h.tap(untagged)
        if h.venue.calls:
            return mark
        if slot not in h.ctx.user_data:
            raise AssertionError(f"refusing an untagged button discarded the valid {slot}")
        await confirm()
        return mark

    mark = asyncio.run(scenario())
    _assert_expired_notice(h, mark, names=False)
    executed = "start_copy" if slot == "copy_setup" else "execute_market_order"
    _assert_executed_on_testnet(h, executed)


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


# --------------------------------------------------------------------------- #
# R1-FABRICATED-MAINNET-BINDING: the network cannot be read right now         #
# --------------------------------------------------------------------------- #
def _network_unreadable(monkeypatch) -> None:
    """``active_network`` cannot read the user row (a DB error), so it returns
    None. Everything else keeps reading the user normally, as when only this
    read failed."""

    def _db_down(_tid):
        raise RuntimeError("guardrail: users read failed")

    monkeypatch.setattr(network_guard, "get_user", _db_down)


def _assert_unknown_network_notice(h: Harness, mark: int) -> None:
    shown = h.screen.texts(mark)
    assert any("couldn't check which network" in t.lower() and "nothing was sent" in t.lower() for t in shown), (
        f"the user was not told their network could not be read: {shown!r}"
    )


@pytest.mark.parametrize("entry", ["tap", "reply_button"])
def test_trade_card_is_not_opened_on_a_guessed_network(h, monkeypatch, entry):
    """The card used to be stamped ``"mainnet"`` when the network could not be
    read. A TESTNET user who then switched to MAINNET would confirm it there;
    one who did not got a false "prepared on MAINNET" refusal. No card is
    built at all: the user is told to try again."""
    _network_unreadable(monkeypatch)
    mark = h.screen.mark()
    if entry == "tap":
        asyncio.run(h.tap("card:trade:start"))
    else:
        asyncio.run(h.say("Trade"))
    assert trade_card.TRADE_CARD_SESSION_KEY not in h.ctx.user_data, "a card was bound to a guessed network"
    # (The home keyboard under the notice has the "card:trade:start" entry.)
    card_buttons = [d for d in _rendered_buttons(h, mark, "card:trade:") if d != "card:trade:start"]
    assert card_buttons == [], f"a trade card was rendered: {card_buttons!r}"
    _assert_unknown_network_notice(h, mark)
    _assert_nothing_executed(h)


@pytest.mark.parametrize(
    "steps, confirm_prefix",
    [
        (("tap:vault:deposit", "button:vault:deposit:preset:"), "vault:deposit:confirm:"),
        (("tap:vault:withdraw", "button:vault:withdraw:pct:"), "vault:withdraw:confirm:"),
        (("tap:vault:deposit:custom", "say:250"), "vault:deposit:confirm:"),
        (("tap:vault:withdraw:custom", "say:10"), "vault:withdraw:confirm:"),
    ],
    ids=["deposit_preset", "withdraw_pct", "deposit_custom", "withdraw_custom"],
)
def test_vault_confirm_card_is_not_bound_to_a_guessed_network(h, monkeypatch, steps, confirm_prefix):
    _network_unreadable(monkeypatch)

    async def scenario() -> int:
        mark = h.screen.mark()
        for step in steps:
            kind, _, arg = step.partition(":")
            if kind == "tap":
                await h.tap(arg)
            elif kind == "button":
                await h.tap(h.button(arg))
            else:
                await h.say(arg)
        return mark

    mark = asyncio.run(scenario())
    assert _rendered_buttons(h, mark, confirm_prefix) == [], "a vault confirm was bound to a guessed network"
    _assert_unknown_network_notice(h, mark)
    _assert_nothing_executed(h)


def test_vault_deposit_watch_is_not_armed_on_a_guessed_network(h, monkeypatch):
    armed: list = []
    _swap(monkeypatch, vault_deposit_watch_service, "enable_deposit_watch",
          lambda tid, network: armed.append(network) or (True, "on"))
    _network_unreadable(monkeypatch)
    mark = h.screen.mark()
    asyncio.run(h.tap("vault:watch:on"))
    assert armed == [], f"the deposit watch was armed on a guessed network: {armed!r}"
    _assert_unknown_network_notice(h, mark)


@pytest.mark.parametrize("opener", ["portfolio:close_all_confirm", "portfolio:cancel_all_confirm", "pos:close_all"])
def test_bulk_opener_opens_no_confirm_when_the_network_is_unreadable(h, monkeypatch, opener):
    """The confirm used to be bound to ``"mainnet"`` when the current network
    could not be read. The opener's own tag cannot be verified, so nothing
    opens."""
    _network_unreadable(monkeypatch)
    mark = h.screen.mark()
    asyncio.run(h.tap(f"{opener}:{TESTNET}"))
    assert _rendered_confirms(h, mark) == []
    _assert_expired_notice(h, mark, names=False)
    _assert_nothing_executed(h)


def test_copy_wizard_started_on_an_unreadable_network_never_renders_a_confirm(h, monkeypatch):
    """A wizard that could not be stamped cannot confirm anywhere; it is
    expired at the step that would render its confirm (never bound to a
    guess), and the slot is dropped."""
    _network_unreadable(monkeypatch)

    async def scenario() -> int:
        await h.tap("copy:start:7")
        for prefix in ("copy:budget:250", "copy:risk:1", "copy:lev:5", "copy:csl:0"):
            await h.tap(h.button(prefix))
        mark = h.screen.mark()
        await h.tap(h.button("copy:ctp:0"))
        return mark

    mark = asyncio.run(scenario())
    assert _rendered_buttons(h, mark, "copy:confirm") == []
    assert "copy_setup" not in h.ctx.user_data
    _assert_expired_notice(h, mark, names=False)
    _assert_nothing_executed(h)


# --------------------------------------------------------------------------- #
# R2-3: the retired Alpha Agent's Start keeps its old, non-looping answer      #
# --------------------------------------------------------------------------- #
def test_retired_alpha_agent_start_keeps_its_old_answer_on_the_same_network(h):
    """The Alpha Agent dashboard (bro_action_kb) still renders an untagged
    ``strategy:start:bro:MULTI``. It never started anything (MULTI is no
    product, and start_user_bot refuses "bro"), and it keeps its old answer
    rather than an "out of date" refusal that re-tapping the same dashboard
    could never clear."""
    mark = h.screen.mark()
    asyncio.run(h.tap("strategy:start:bro:MULTI"))
    shown = h.screen.texts(mark)
    assert any("MULTI" in t and "not currently available" in t and TESTNET in t for t in shown), shown
    assert not any("nothing was sent" in t.lower() for t in shown), shown
    _assert_nothing_executed(h)


def test_the_retired_alpha_agent_can_never_start(monkeypatch):
    """What exempts "bro" from the network gate: the runtime refuses it on any
    network. If this ever starts a run, the exemption must go."""
    monkeypatch.setattr(bot_runtime, "get_user", lambda _tid: UserRow({"telegram_id": UID, "network_mode": MAINNET}))
    ok, msg = bot_runtime.start_user_bot(UID, "bro", "BTC")
    assert ok is False, msg
