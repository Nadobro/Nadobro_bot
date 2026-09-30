"""/venue + venue:* switching and the Arcus placeholder screens (Arcus P1).

Venues run in PARALLEL: a switch only changes which venue's screens the user
sees. These tests pin that it never stops, cancels, starts or resumes anything
(every such function is patched to raise), that a refused / failed switch
changes nothing, that switching back renders exactly today's Nado home, and that
the pending Nado flows (in-memory AND persisted) are dropped on a switch.

The real ``users/venue_service`` CAS logic runs against an in-memory row; the
DB-backed round trip lives in test_venue_switch_db.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import callbacks  # noqa: E402
from src.nadobro.handlers import update_serialization as us  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402
from src.nadobro.i18n import _ACTIVE_LANG  # noqa: E402
from src.nadobro.models.database import NetworkMode  # noqa: E402
from src.nadobro.users import venue_service  # noqa: E402

UID = 990_023_301
CHAT = 990_023_302


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, dict]] = []
        self.message = SimpleNamespace(chat_id=CHAT)

    async def answer(self, text=None, show_alert=False, **_kw):
        self.answers.append((text, bool(show_alert)))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


class FakeMessage:
    def __init__(self):
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


def cb_update(data):
    return SimpleNamespace(callback_query=FakeQuery(data), effective_user=SimpleNamespace(id=UID),
                           effective_message=None)


def cmd_update():
    msg = FakeMessage()
    return SimpleNamespace(callback_query=None, effective_user=SimpleNamespace(id=UID), effective_message=msg,
                           message=msg)


def ctx(**user_data):
    return SimpleNamespace(user_data=dict(user_data))


class Row:
    """The user's row + everything the switch path may touch, in memory."""

    def __init__(self, monkeypatch, *, venue="nado", onboarded=True, exists=True):
        self.venue = venue
        self.network = "mainnet"
        self.onboarded = onboarded
        self.exists = exists
        self.cas_calls: list[tuple] = []
        self.cleared: list[str] = []
        self.fail_cas = False
        self.fail_read = False
        # A cached row that predates a switch (served until invalidated).
        self.stale: str | None = None
        us._user_locks.clear()

        def user(_uid):
            if self.fail_read:
                raise RuntimeError("db down")
            if not self.exists:
                return None
            venue_ = self.stale if self.stale is not None else self.venue
            return SimpleNamespace(active_venue=venue_, network_mode=NetworkMode(self.network),
                                   arcus_network_mode="testnet", language="en")

        def invalidate(_uid=None):
            self.stale = None

        def cas(sql, params):
            self.cas_calls.append(params)
            if self.fail_cas:
                raise RuntimeError("db down")
            new, uid, previous = params
            if self.exists and uid == UID and self.venue == previous:
                self.venue = new
                return {"active_venue": new}
            return None

        async def rb(fn, *a, **k):
            return fn(*a, **k)

        # Real venue_service logic over the in-memory row.
        monkeypatch.setattr(venue_service, "get_user", user)
        monkeypatch.setattr(venue_service, "_get_cached_user", lambda _uid: None)
        monkeypatch.setattr(venue_service, "execute_returning", cas)
        monkeypatch.setattr(venue_service, "invalidate_user_cache", invalidate)
        monkeypatch.setattr(venue_service, "record_audit_event", lambda *a, **k: None)
        monkeypatch.setattr(vh, "get_user", user)
        monkeypatch.setattr(vh, "run_blocking_db", rb)
        monkeypatch.setattr(vh, "is_new_onboarding_complete", lambda _uid: self.onboarded)
        monkeypatch.setattr(vh, "nado_automation_snapshot", lambda _uid: ("testnet", [], False))
        for name in ("clear_strategy_pending_input", "clear_text_trade_pending",
                     "clear_text_close_all_pending", "clear_wallet_pending_flow"):
            monkeypatch.setattr(vh, name, (lambda n: (lambda uid: self.cleared.append(n)))(name))


def _allow(monkeypatch, uid=UID):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(uid))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_ALLOWED_USER_IDS", raising=False)
    token = _ACTIVE_LANG.set("en")
    yield
    _ACTIVE_LANG.reset(token)


@pytest.fixture(autouse=True)
def _nothing_live_is_touched(monkeypatch):
    """The parallel-venues invariant: a switch (or rendering the Arcus home)
    never stops, cancels, starts, resumes or re-arms anything on Nado."""
    from src.nadobro.llm import managed_agent_state
    from src.nadobro.strategy import bot_runtime, network_switch, pending_cleanup
    from src.nadobro.trading import copy_service, desk_store, stop_loss_service
    from src.nadobro.users import user_service

    def trap(name):
        def _raise(*_a, **_k):
            raise AssertionError(f"venue switch must never call {name}")
        return _raise

    targets = {
        bot_runtime: ("stop_user_bot", "stop_all_user_bots", "start_user_bot", "stop_all_automation_for_user",
                      "stop_strategy_for_network_switch", "cancel_orders_on_products", "retry_pending_cleanups",
                      "restore_running_bots"),
        copy_service: ("stop_copy", "pause_copy", "resume_copy", "stop_all_copies", "start_copy"),
        desk_store: ("cancel_plan", "discard_draft", "confirm_plan", "finish_plan", "fail_plan"),
        network_switch: ("switch_network",),
        pending_cleanup: ("begin", "end", "delete", "record_failed"),
        stop_loss_service: ("register_stop_loss_rule",),
        managed_agent_state: ("set_managed_agent_enabled",),
        user_service: ("set_network_mode", "get_user_nado_client", "get_user_readonly_client"),
    }
    for module, names in targets.items():
        for name in names:
            monkeypatch.setattr(module, name, trap(f"{module.__name__}.{name}"))
    monkeypatch.setattr(callbacks, "switch_network", trap("callbacks.switch_network"))
    monkeypatch.setattr(callbacks, "stop_user_bot", trap("callbacks.stop_user_bot"))


@pytest.fixture
def nado_home(monkeypatch):
    async def home_text(_uid):
        return "🏠 *Home* \\- card"

    monkeypatch.setattr(callbacks, "build_home_card_text_async", home_text)


def run(coro):
    async def body():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    return asyncio.run(body())


def _norm(edits):
    """Edits as plain data (stub keyboards have no __eq__)."""
    return [
        (text, str(kw.get("parse_mode")),
         [[(b.text, b.callback_data) for b in r] for r in kw["reply_markup"].inline_keyboard])
        for text, kw in edits
    ]


def tap(data, context=None):
    update = cb_update(data)
    wrapped = vh.venue_callback_ack(us.with_user_serialized(vh.handle_venue_callback))
    run(wrapped(update, context if context is not None else ctx()))
    return update.callback_query


# ---------------------------------------------------------------------------
# Nado -> Arcus: refusals change nothing
# ---------------------------------------------------------------------------

def test_flag_off_refuses_and_changes_nothing(monkeypatch):
    row = Row(monkeypatch)
    context = ctx(pending_trade={"x": 1})
    q = tap("venue:set:arcus", context)
    assert q.answers == [(vh.TEXT_ARCUS_NOT_ALLOWED, True)]
    assert row.venue == "nado" and row.cas_calls == [] and row.cleared == []
    assert q.edits == []
    assert context.user_data == {"pending_trade": {"x": 1}}


def test_not_allowlisted_refuses(monkeypatch):
    row = Row(monkeypatch)
    _allow(monkeypatch, uid=UID + 1)
    q = tap("venue:set:arcus")
    assert q.answers == [(vh.TEXT_ARCUS_NOT_ALLOWED, True)]
    assert row.venue == "nado" and row.cas_calls == [] and q.edits == []


def test_onboarding_incomplete_refuses(monkeypatch):
    row = Row(monkeypatch, onboarded=False)
    _allow(monkeypatch)
    q = tap("venue:set:arcus")
    assert q.answers == [(vh.TEXT_FINISH_SETUP_FIRST, True)]
    assert row.venue == "nado" and row.cas_calls == [] and q.edits == []


def test_db_error_on_the_switch_refuses_and_never_renders_arcus(monkeypatch):
    row = Row(monkeypatch)
    row.fail_cas = True
    _allow(monkeypatch)
    q = tap("venue:set:arcus")
    assert q.answers == [(vh.TEXT_SWITCH_FAILED, True)]
    assert row.venue == "nado" and q.edits == [] and row.cleared == []


def test_unreadable_venue_refuses(monkeypatch):
    row = Row(monkeypatch)
    row.fail_read = True
    _allow(monkeypatch)
    q = tap("venue:set:arcus")
    assert q.answers == [(vh.TEXT_SWITCH_FAILED, True)]
    assert row.cas_calls == [] and q.edits == []


def test_missing_row_refuses(monkeypatch):
    row = Row(monkeypatch, exists=False)
    _allow(monkeypatch)
    q = tap("venue:set:arcus")
    assert q.answers == [(vh.TEXT_SWITCH_FAILED, True)]
    assert q.edits == [] and row.cleared == []


# ---------------------------------------------------------------------------
# the switch itself, both ways
# ---------------------------------------------------------------------------

def test_switch_to_arcus_compare_and_sets_and_renders_the_arcus_home(monkeypatch):
    row = Row(monkeypatch)
    _allow(monkeypatch)
    q = tap("venue:set:arcus")
    assert row.venue == "arcus"
    assert row.cas_calls == [("arcus", UID, "nado")]
    assert q.answers == [(vh.TEXT_SWITCHED_TO_ARCUS, False)]  # a toast, answered exactly once
    [(text, kw)] = q.edits
    assert "ARCUS · beta" in text and "TESTNET" in text and "coming soon" in text
    assert kw["parse_mode"] == "HTML" or str(kw["parse_mode"]).endswith("HTML")
    buttons = [b.callback_data for row_ in kw["reply_markup"].inline_keyboard for b in row_]
    assert buttons == ["venue:view", "ax:help"]


def test_switch_back_to_nado_renders_exactly_todays_nado_home(monkeypatch, nado_home):
    row = Row(monkeypatch, venue="arcus")
    q = tap("venue:set:nado")  # flag OFF: switching back is never gated
    assert row.venue == "nado"
    assert row.cas_calls == [("nado", UID, "arcus")]
    assert q.answers == [(vh.TEXT_SWITCHED_TO_NADO, False)]

    direct = FakeQuery("nav:main")
    run(callbacks._show_dashboard(direct, UID))
    assert len(q.edits) == 1 and _norm(q.edits) == _norm(direct.edits)  # identical Nado home


def test_nado_to_nado_just_renders_the_nado_home(monkeypatch, nado_home):
    row = Row(monkeypatch, venue="nado")
    q = tap("venue:set:nado")
    assert row.cas_calls == [("nado", UID, "arcus")]  # always attempted; a no-op here
    assert row.venue == "nado" and row.cleared == []
    assert q.answers == [(None, False)]
    direct = FakeQuery("nav:main")
    run(callbacks._show_dashboard(direct, UID))
    assert _norm(q.edits) == _norm(direct.edits)


def test_arcus_to_arcus_renders_the_arcus_home_without_writing(monkeypatch):
    row = Row(monkeypatch, venue="arcus")
    q = tap("venue:set:arcus")  # even with the flag off: nothing changes
    assert row.cas_calls == [] and row.cleared == []
    assert q.answers == [(None, False)]
    assert "ARCUS · beta" in q.edits[0][0]


def test_round_trip(monkeypatch, nado_home):
    row = Row(monkeypatch)
    _allow(monkeypatch)
    tap("venue:set:arcus")
    assert row.venue == "arcus"
    tap("venue:set:nado")
    assert row.venue == "nado"
    assert row.cas_calls == [("arcus", UID, "nado"), ("nado", UID, "arcus")]


def test_failed_switch_back_refuses_and_keeps_the_arcus_view(monkeypatch, nado_home):
    row = Row(monkeypatch, venue="arcus")
    row.fail_cas = True
    q = tap("venue:set:nado")
    assert q.answers == [(vh.TEXT_SWITCH_FAILED, True)]
    assert row.venue == "arcus" and q.edits == []


def test_switch_back_with_an_unreadable_venue_after_a_cas_miss_refuses(monkeypatch, nado_home):
    row = Row(monkeypatch, venue="nado")
    row.fail_read = True
    q = tap("venue:set:nado")
    assert q.answers == [(vh.TEXT_SWITCH_FAILED, True)] and q.edits == []


# ---------------------------------------------------------------------------
# a cached row that predates a switch never decides one (BC1-STALE-CACHE / BC2-1)
# ---------------------------------------------------------------------------

def test_switch_to_nado_writes_even_when_the_cached_venue_says_nado(monkeypatch, nado_home):
    row = Row(monkeypatch, venue="arcus")
    row.stale = "nado"  # the row says arcus; a pre-switch read re-cached 'nado'
    context = ctx(pending_trade={"x": 1})
    q = tap("venue:set:nado", context)
    assert row.venue == "nado"
    assert row.cas_calls == [("nado", UID, "arcus")]
    assert q.answers == [(vh.TEXT_SWITCHED_TO_NADO, False)]
    assert context.user_data == {} and row.cleared  # a real switch: pending flows dropped


def test_switch_to_arcus_reads_fresh_when_the_cached_venue_says_arcus(monkeypatch):
    row = Row(monkeypatch, venue="nado")
    row.stale = "arcus"
    _allow(monkeypatch)
    q = tap("venue:set:arcus")
    assert row.venue == "arcus"
    assert row.cas_calls == [("arcus", UID, "nado")]
    assert q.answers == [(vh.TEXT_SWITCHED_TO_ARCUS, False)]


def test_switch_back_when_the_cache_says_arcus_but_the_row_is_nado(monkeypatch, nado_home):
    row = Row(monkeypatch, venue="nado")
    row.stale = "arcus"
    q = tap("venue:set:nado")
    assert row.venue == "nado" and row.cleared == []
    assert q.answers == [(None, False)]  # nothing flipped: no toast, still the Nado home
    assert len(q.edits) == 1


# ---------------------------------------------------------------------------
# pending Nado flows are dropped on a switch (in-memory and persisted)
# ---------------------------------------------------------------------------

_PENDING = {
    "pending_trade": {"product": "BTC"},
    "pending_text_trade": {"side": "long"},
    "pending_text_close_all": True,
    "pending_question": True,
    "trade_flow": {"state": "size"},
    "wallet_flow": "awaiting_key",
    "copy_setup": {"trader": 1},
    "trade_card_session": {"id": "ab12cd34"},
}
_KEPT = {
    "vault_op_inflight": True,          # in-flight guard: clearing it risks a double submit
    "strategy_pair:grid": "BTC",        # UI selections survive a round trip
    "home_card_message": 123,
    "settings": {"lev": 5},
}


@pytest.mark.parametrize("start,data", [("nado", "venue:set:arcus"), ("arcus", "venue:set:nado")])
def test_switch_drops_pending_flows_and_keeps_ui_state(monkeypatch, nado_home, start, data):
    row = Row(monkeypatch, venue=start)
    _allow(monkeypatch)
    context = ctx(**_PENDING, **_KEPT)
    tap(data, context)
    assert context.user_data == _KEPT
    # The persisted twins too: a text-trade preview reloads from bot_state, so an
    # in-memory-only clear would let a "yes" after switching back execute it.
    assert sorted(row.cleared) == sorted([
        "clear_strategy_pending_input", "clear_text_trade_pending",
        "clear_text_close_all_pending", "clear_wallet_pending_flow",
    ])


def test_persisted_clear_failure_does_not_block_the_switch(monkeypatch):
    row = Row(monkeypatch)
    _allow(monkeypatch)

    def boom(_uid):
        raise RuntimeError("db blip")

    monkeypatch.setattr(vh, "clear_text_trade_pending", boom)
    q = tap("venue:set:arcus")
    assert row.venue == "arcus" and q.answers == [(vh.TEXT_SWITCHED_TO_ARCUS, False)]


def test_trade_card_key_matches_the_trade_card_module():
    from src.nadobro.handlers import trade_card

    assert vh.TRADE_CARD_SESSION_KEY == trade_card.TRADE_CARD_SESSION_KEY == "trade_card_session"


# ---------------------------------------------------------------------------
# /venue and venue:view
# ---------------------------------------------------------------------------

def test_venue_command_is_silent_for_a_nado_user_outside_the_cohort(monkeypatch):
    Row(monkeypatch)
    update = cmd_update()
    run(vh.cmd_venue(update, ctx()))
    assert update.message.replies == []


def test_venue_command_shows_the_card_to_the_cohort(monkeypatch):
    Row(monkeypatch)
    _allow(monkeypatch)
    update = cmd_update()
    run(vh.cmd_venue(update, ctx()))
    [(text, kw)] = update.message.replies
    assert "Trading venue" in text and "Nado · MAINNET" in text
    assert "never stops, closes or starts anything" in text
    assert "/stop_all stops them" in text
    labels = [(b.text, b.callback_data) for r in kw["reply_markup"].inline_keyboard for b in r]
    assert labels == [("Nado ✅", "venue:set:nado"), ("Arcus (beta)", "venue:set:arcus"), ("🏠 Home", "nav:main")]


def test_venue_command_always_reaches_an_arcus_user(monkeypatch):
    Row(monkeypatch, venue="arcus")  # flag OFF, not allowlisted: never stranded
    update = cmd_update()
    run(vh.cmd_venue(update, ctx()))
    [(text, kw)] = update.message.replies
    assert "Arcus (beta) · TESTNET" in text
    labels = [b.text for r in kw["reply_markup"].inline_keyboard for b in r]
    assert labels[:2] == ["Nado", "Arcus (beta) ✅"]


def test_venue_view_edits_only_for_the_cohort_or_arcus(monkeypatch):
    Row(monkeypatch)
    q = tap("venue:view")
    assert q.edits == [] and q.answers == [(None, False)]  # pre-acked, nothing rendered
    _allow(monkeypatch)
    q = tap("venue:view")
    assert "Trading venue" in q.edits[0][0]


# ---------------------------------------------------------------------------
# ax:* screens
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("data,needle,buttons", [
    ("ax:home", "ARCUS · beta", ["venue:view", "ax:help"]),
    ("ax:help", "closed beta", ["ax:home", "venue:view"]),
    ("ax:settings", "Settings · Arcus", ["settings:language_menu", "venue:view", "ax:home"]),
])
def test_ax_screens_render_for_arcus_users(monkeypatch, data, needle, buttons):
    Row(monkeypatch, venue="arcus")
    q = tap(data)
    assert q.answers == [(None, False)]
    [(text, kw)] = q.edits
    assert needle in text
    assert [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r] == buttons


@pytest.mark.parametrize("data", ["ax:home", "ax:help", "ax:settings", "ax:unavailable", "ax:bogus", "venue:bogus"])
def test_ax_screens_render_nothing_for_nado_users_and_unknown_values(monkeypatch, data):
    Row(monkeypatch, venue="nado")
    q = tap(data)
    assert q.edits == [] and q.answers == [(None, False)]


def test_unknown_ax_value_renders_nothing_for_arcus(monkeypatch):
    Row(monkeypatch, venue="arcus")
    q = tap("ax:unavailable")  # render-only key, not a route
    assert q.edits == []


def test_render_bumps_the_interaction_sequence(monkeypatch):
    Row(monkeypatch, venue="arcus")
    before = callbacks.interaction_seq(CHAT)
    tap("ax:home")
    assert callbacks.interaction_seq(CHAT) == before + 1


# ---------------------------------------------------------------------------
# callback acks
# ---------------------------------------------------------------------------

async def _never_answers(update, context):
    return None  # e.g. dropped behind a busy per-user lock


async def _raises(update, context):
    raise RuntimeError("boom")


def test_self_answering_switch_gets_a_bare_ack_if_the_handler_never_answers(monkeypatch):
    update = cb_update("venue:set:arcus")
    run(vh.venue_callback_ack(_never_answers)(update, ctx()))
    assert update.callback_query.answers == [(None, False)]


def test_self_answering_switch_is_answered_exactly_once(monkeypatch):
    Row(monkeypatch)
    q = tap("venue:set:arcus")  # refused: one alert, no extra bare ack
    assert q.answers == [(vh.TEXT_ARCUS_NOT_ALLOWED, True)]


def test_a_raising_switch_still_clears_the_spinner(monkeypatch):
    update = cb_update("venue:set:nado")
    with pytest.raises(RuntimeError):
        run(vh.venue_callback_ack(_raises)(update, ctx()))
    assert update.callback_query.answers == [(None, False)]


# ---------------------------------------------------------------------------
# the Arcus home banner: Postgres only, DENIED != EMPTY
# ---------------------------------------------------------------------------

@pytest.fixture
def banner_sources(monkeypatch):
    from src.nadobro.llm import managed_agent_state
    from src.nadobro.models import database
    from src.nadobro.strategy import bot_runtime, pending_cleanup
    from src.nadobro.trading import desk_store, stop_loss_service
    from src.nadobro.venue import nado_client

    def no_venue(*_a, **_k):
        raise AssertionError("the Arcus home must never build a Nado client")

    monkeypatch.setattr(nado_client, "get_nado_client", no_venue)
    monkeypatch.setattr(nado_client.NadoClient, "__init__", no_venue)
    monkeypatch.setattr(vh, "get_user", lambda _uid: SimpleNamespace(arcus_network_mode="testnet"))

    src = SimpleNamespace(
        state={"testnet": {"running": False}, "mainnet": {"running": True, "strategy": "grid", "product": "btc"}},
        pending={"testnet": [("k", {})], "mainnet": []},
        desk={"testnet": [], "mainnet": [{"plan_id": "a"}, {"plan_id": "b"}]},
        rules={"testnet": [{"product": "ETH"}], "mainnet": []},
        mirrors=[{"id": 1}, {"id": 2}, {"id": 3}],
        agent=True,
        fail=set(),
    )

    def guarded(name, fn):
        def _call(*a, **k):
            if name in src.fail:
                raise RuntimeError(f"{name} unreadable")
            return fn(*a, **k)
        return _call

    monkeypatch.setattr(bot_runtime, "get_user_bot_state", guarded("state", lambda uid, net: src.state[net]))
    monkeypatch.setattr(pending_cleanup, "list_entries", guarded("pending", lambda uid, net: src.pending[net]))
    monkeypatch.setattr(desk_store, "list_active_plans", guarded("desk", lambda uid, net: src.desk[net]))
    monkeypatch.setattr(stop_loss_service, "list_active_stop_loss_rules",
                        guarded("rules", lambda uid, net: src.rules[net]))
    monkeypatch.setattr(database, "get_user_active_mirrors_v2", guarded("mirrors", lambda uid, network=None: src.mirrors))
    monkeypatch.setattr(managed_agent_state, "is_managed_agent_globally_enabled", lambda: True)
    monkeypatch.setattr(managed_agent_state, "get_managed_agent_state",
                        guarded("agent", lambda uid: {"enabled": src.agent}))
    return src


def test_banner_lists_what_is_still_live_on_nado(banner_sources):
    net, items, failed = vh.nado_automation_snapshot(UID)
    assert (net, failed) == ("testnet", False)
    assert items == [
        (vh.TEXT_ITEM_CLEANUP, {"network": "TESTNET"}),
        (vh.TEXT_ITEM_STRATEGY, {"strategy": "GRID BTC", "network": "MAINNET"}),
        (vh.TEXT_ITEM_COPY, {"n": "3"}),
        (vh.TEXT_ITEM_DESK, {"n": "2"}),
        (vh.TEXT_ITEM_STOP_LOSS, {"n": "1"}),
        (vh.TEXT_ITEM_MANAGED_AI, {}),
    ]
    text = vh.arcus_home_text(net, items, failed)
    assert "Nado is still running:</b> Order cleanup pending on TESTNET, GRID BTC on MAINNET, Copy trades: 3, " \
           "Desk plans: 2, Stop-loss rules: 1, Managed AI on" in text
    assert "/stop_all stops Nado strategies and copy trades" in text
    assert "Couldn't check" not in text


def test_banner_quiet_when_nothing_runs(banner_sources):
    banner_sources.state = {"testnet": {}, "mainnet": {}}
    banner_sources.pending = {"testnet": [], "mainnet": []}
    banner_sources.desk = {"testnet": [], "mainnet": []}
    banner_sources.rules = {"testnet": [], "mainnet": []}
    banner_sources.mirrors = []
    banner_sources.agent = False
    net, items, failed = vh.nado_automation_snapshot(UID)
    assert (items, failed) == ([], False)
    text = vh.arcus_home_text(net, items, failed)
    assert "Nado is still running" not in text and "Couldn't check" not in text


@pytest.mark.parametrize("source", ["state", "pending", "desk", "rules", "mirrors", "agent"])
def test_an_unreadable_source_is_never_reported_as_nothing_running(banner_sources, source):
    banner_sources.fail = {source}
    net, items, failed = vh.nado_automation_snapshot(UID)
    assert failed is True
    assert "Couldn't check your Nado automation" in vh.arcus_home_text(net, items, failed)


def test_banner_escapes_dynamic_values():
    text = vh.arcus_home_text("testnet", [(vh.TEXT_ITEM_STRATEGY, {"strategy": "<b>X&Y</b>", "network": "M"})], False)
    assert "&lt;b&gt;X&amp;Y&lt;/b&gt;" in text and "<b>X&Y</b>" not in text


def _home_buttons(monkeypatch, snapshot):
    Row(monkeypatch, venue="arcus")
    monkeypatch.setattr(vh, "nado_automation_snapshot", lambda _uid: snapshot)
    q = tap("ax:home")
    [(_text, kw)] = q.edits
    return [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]


_STOP_ENTRIES = ["portfolio:close_all_confirm", "portfolio:cancel_all_confirm"]


@pytest.mark.parametrize("snapshot,expected", [
    (("testnet", [], False), []),
    (("testnet", [(vh.TEXT_ITEM_COPY, {"n": "1"})], False), _STOP_ENTRIES),
    (("testnet", [(vh.TEXT_ITEM_DESK, {"n": "2"})], False), _STOP_ENTRIES + ["desk:view"]),
    (("testnet", [], True), _STOP_ENTRIES + ["desk:view"]),  # could not check: offer them all
])
def test_arcus_home_offers_the_nado_stop_entries_while_the_banner_shows(monkeypatch, snapshot, expected):
    # BC1-STOP-ENTRY-UNREACHABLE: /stop_all does not stop desk plans, close
    # positions or cancel orders — the home carries those entries, all NEVER_GATE.
    from src.nadobro.utils.venue_capabilities import NEVER_GATE, classify_callback

    buttons = _home_buttons(monkeypatch, snapshot)
    assert buttons == ["venue:view", "ax:help"] + expected
    for data in expected:
        assert classify_callback(data)[0] == NEVER_GATE, data


def test_home_render_failure_degrades_to_could_not_check(monkeypatch):
    Row(monkeypatch, venue="arcus")

    def boom(_uid):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(vh, "nado_automation_snapshot", boom)
    q = tap("ax:home")
    assert "Couldn't check your Nado automation" in q.edits[0][0]


async def _media_card_refuses_edit(text, **kw):
    from telegram.error import BadRequest

    raise BadRequest("Bad Request: there is no text in the message to edit")


def test_edit_falls_back_to_a_new_message_on_a_media_card(monkeypatch):
    Row(monkeypatch, venue="arcus")
    q = FakeQuery("ax:help")
    sent = FakeMessage()
    q.message = SimpleNamespace(chat_id=CHAT, reply_text=sent.reply_text)
    q.edit_message_text = _media_card_refuses_edit
    run(vh.edit_html(q, "hello", vh.arcus_help_kb()))
    assert sent.replies and sent.replies[0][0] == "hello"
