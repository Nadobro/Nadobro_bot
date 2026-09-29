"""Behaviour of the per-venue gate (Arcus P1, handlers/venue_gate.py).

Fake updates in the style of test_update_serialization_latency.py. The gate is
driven through ``_through_groups``, which mirrors PTB's group loop: the gate runs
at -1, and ApplicationHandlerStop means NO group-0 handler (handle_callback /
handle_message / the Nado commands) ever sees the update. The same semantics are
checked against the real PTB Application in test_venue_gate_ptb.py.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from telegram.ext import ApplicationHandlerStop  # noqa: E402

from src.nadobro.handlers import update_serialization as us  # noqa: E402
from src.nadobro.handlers import venue_gate as vg  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402
from src.nadobro.i18n import SUPPORTED_LANGS, _ACTIVE_LANG, localize_label, localize_text  # noqa: E402
from src.nadobro.utils import venue_capabilities as vc  # noqa: E402

UID = 990_023_101
ARCUS, NADO = "arcus", "nado"


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers: list[tuple[str | None, bool]] = []
        self.edits: list[tuple[str, dict]] = []
        self.message = SimpleNamespace(chat_id=UID)

    async def answer(self, text=None, show_alert=False, **_kw):
        self.answers.append((text, bool(show_alert)))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


class FakeMessage:
    def __init__(self, text=None, *, command_len=None, photo=False):
        self.text = text
        self.photo = [object()] if photo else None
        self.entities = (
            (SimpleNamespace(type="bot_command", offset=0, length=command_len),) if command_len else ()
        )
        self.chat_id = UID
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


def _user():
    return SimpleNamespace(id=UID, username="pytest_venue_gate")


def cb(data):
    return SimpleNamespace(callback_query=FakeQuery(data), message=None, edited_message=None,
                           effective_user=_user(), effective_message=None)


def cmd(text):
    name = text.split()[0]
    msg = FakeMessage(text, command_len=len(name))
    return SimpleNamespace(callback_query=None, message=msg, edited_message=None,
                           effective_user=_user(), effective_message=msg)


def txt(text):
    msg = FakeMessage(text)
    return SimpleNamespace(callback_query=None, message=msg, edited_message=None,
                           effective_user=_user(), effective_message=msg)


def photo():
    msg = FakeMessage(None, photo=True)
    return SimpleNamespace(callback_query=None, message=msg, edited_message=None,
                           effective_user=_user(), effective_message=msg)


def edited(text):
    msg = FakeMessage(text)
    return SimpleNamespace(callback_query=None, message=None, edited_message=msg,
                           effective_user=_user(), effective_message=msg)


class World:
    """Patched gate collaborators: the venue, the renderer, last_active."""

    def __init__(self, monkeypatch, venue):
        self.venue = venue
        self.venue_reads = 0
        self.rendered: list[tuple[str, str]] = []  # (target, "query"|"message")
        self.touched = 0
        us._user_locks.clear()

        async def read(uid):
            self.venue_reads += 1
            if isinstance(self.venue, Exception):
                raise self.venue
            if callable(self.venue):
                return self.venue()
            return self.venue

        async def render(target, uid, *, query=None, message=None):
            assert uid == UID
            self.rendered.append((target, "query" if query is not None else "message"))

        def touch(uid, username=None, language_code=None):
            self.touched += 1

        async def rb(fn, *a, **k):
            return fn(*a, **k)

        monkeypatch.setattr(vg, "read_active_venue", read)
        monkeypatch.setattr(vg, "render_arcus_target", render)
        monkeypatch.setattr(vg, "get_or_create_user", touch)
        monkeypatch.setattr(vg, "run_blocking_db", rb)


async def _through_groups(update):
    """PTB's loop in miniature: group -1 (the gate) then group 0. Returns True
    when the update reached group 0 (i.e. handle_callback / handle_message / a
    Nado command would have run)."""
    try:
        await vg.venue_gate(update, SimpleNamespace(user_data={}))
    except ApplicationHandlerStop:
        return False
    return True


def run(update):
    async def body():
        reached = await _through_groups(update)
        await asyncio.sleep(0)  # let fire-and-forget acks land
        await asyncio.sleep(0)
        return reached

    return asyncio.run(body())


@pytest.fixture(autouse=True)
def _english():
    token = _ACTIVE_LANG.set("en")
    yield
    _ACTIVE_LANG.reset(token)


NEVER_GATE_SAMPLES = sorted(vc.NEVER_GATE_EXACT) + [
    "copy:stop:3", "copy:pause:3", "desk:stop:0123456789abcdef", "desk:discard:0123456789abcdef",
    "pos:close:BTC", "portfolio:cancel_order:d:0a1b2c3d", "portfolio:cancel_order:2", "alert:del:9",
    "howl:reject:1", "nav:strategy:stop", "nav:copy:stop:3",
]
NEUTRAL_SAMPLES = ["venue:view", "venue:set:nado", "venue:set:arcus", "settings:language_menu",
                   "settings:language:ko", "onb:lang:fr", "onb:accept_tos", "resources:home", "nav:help"]
NADO_ONLY_SAMPLES = ["strategy:start:grid:BTC", "strategy:startok:vol:BTC", "exec_trade:abc", "trade:long",
                     "card:trade:ab12cd34:confirm", "copy:resume:3", "howl:approve:0", "howl:approve_all",
                     "vault:deposit", "mode:mainnet", "wallet:network:mainnet", "desk:confirm:abc",
                     "nav:trade", "nav:ask_nado", "points:view", "market:view", "strategy:preview:grid",
                     "portfolio:share_pnl:3", "alert:set", "refer:claim", "settings:leverage:5"]
UNKNOWN_SAMPLES = ["garbage", "nav:status:stop", "nav:pos:close:BTC", "home:foo", ""]
DISPATCH_SAMPLES = {
    "nav:main": vc.AX_HOME, "nav:refresh": vc.AX_HOME, "onboarding:resume": vc.AX_HOME,
    "status:refresh": vc.AX_HOME, "strategy:status": vc.AX_HOME, "nav:quick_start": vc.AX_HOME,
    "settings:view": vc.AX_SETTINGS, "nav:settings:view": vc.AX_SETTINGS,
    "portfolio:view": vc.AX_UNAVAILABLE, "pos:view": vc.AX_UNAVAILABLE, "wallet:view": vc.AX_UNAVAILABLE,
    "home:mode": vc.AX_UNAVAILABLE, "nav:mode": vc.AX_UNAVAILABLE, "nav:strategy_hub": vc.AX_UNAVAILABLE,
    "portfolio:history:2": vc.AX_UNAVAILABLE, "mm:status:refresh": vc.AX_UNAVAILABLE,
}


# ---------------------------------------------------------------------------
# Nado-view users: everything passes exactly as today, except ax:*
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "data",
    NEVER_GATE_SAMPLES + NEUTRAL_SAMPLES + NADO_ONLY_SAMPLES + UNKNOWN_SAMPLES + sorted(DISPATCH_SAMPLES),
)
def test_nado_user_passes_every_non_arcus_callback_untouched(monkeypatch, data):
    world = World(monkeypatch, NADO)
    update = cb(data)
    assert run(update) is True
    assert update.callback_query.answers == []  # the gate never answers a tap it passes
    assert world.rendered == []


@pytest.mark.parametrize("data", ["ax:home", "ax:help", "ax:settings", "ax:bogus"])
def test_nado_user_is_denied_ax_buttons(monkeypatch, data):
    world = World(monkeypatch, NADO)
    update = cb(data)
    assert run(update) is False
    assert update.callback_query.answers == [(vh.TEXT_ARCUS_BUTTON_ON_NADO, True)]
    assert world.rendered == []


@pytest.mark.parametrize("text", ["/start", "/desk", "/stop_all", "/venue", "/agent_on", "/foo"])
def test_nado_user_commands_pass(monkeypatch, text):
    World(monkeypatch, NADO)
    update = cmd(text)
    assert run(update) is True
    assert update.message.replies == []


@pytest.mark.parametrize("factory", [lambda: txt("long BTC 10x"), photo, lambda: edited("hi")])
def test_nado_user_messages_pass(monkeypatch, factory):
    world = World(monkeypatch, NADO)
    update = factory()
    assert run(update) is True
    assert world.touched == 0


# ---------------------------------------------------------------------------
# no venue read at all for NEVER_GATE / NEUTRAL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", [lambda d=d: cb(d) for d in NEVER_GATE_SAMPLES + NEUTRAL_SAMPLES]
                         + [lambda: cmd("/stop_all"), lambda: cmd("/agent_off"), lambda: cmd("/revoke"),
                            lambda: cmd("/venue"), lambda: cmd("/help"), lambda: cmd("/ops")])
def test_never_gate_and_neutral_pass_without_reading_the_venue(monkeypatch, factory):
    world = World(monkeypatch, AssertionError("venue must not be read"))
    update = factory()
    assert run(update) is True
    assert world.venue_reads == 0
    if update.callback_query is not None:
        assert update.callback_query.answers == []


def test_points_cancel_still_answers_itself(monkeypatch):
    # The gate passes it WITHOUT answering, so _handle_points' own show_alert
    # (and with_callback_ack's exclusion of it) are unchanged.
    for venue in (NADO, ARCUS):
        World(monkeypatch, venue)
        update = cb("points:cancel")
        assert run(update) is True
        assert update.callback_query.answers == []


# ---------------------------------------------------------------------------
# Arcus-view users
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("data", NEVER_GATE_SAMPLES)
def test_arcus_user_reaches_every_nado_stop_path(monkeypatch, data):
    world = World(monkeypatch, ARCUS)
    update = cb(data)
    assert run(update) is True
    assert update.callback_query.answers == []
    assert world.rendered == []


@pytest.mark.parametrize("data", NADO_ONLY_SAMPLES + UNKNOWN_SAMPLES)
def test_arcus_user_is_denied_nado_only_and_unknown(monkeypatch, data):
    world = World(monkeypatch, ARCUS)
    update = cb(data)
    assert run(update) is False  # never reaches handle_callback
    assert update.callback_query.answers == [(vh.TEXT_DENIED_ON_ARCUS, True)]
    assert update.callback_query.edits == []
    assert world.rendered == []


@pytest.mark.parametrize("data,target", sorted(DISPATCH_SAMPLES.items()))
def test_arcus_user_dispatch_lands_on_the_arcus_screen(monkeypatch, data, target):
    world = World(monkeypatch, ARCUS)
    update = cb(data)
    assert run(update) is False  # the Nado view never renders
    assert world.rendered == [(target, "query")]
    # A bare ack — never a Nado toast like "Loading portfolio…", never a denial.
    assert update.callback_query.answers == [(None, False)]


@pytest.mark.parametrize("data", ["ax:home", "ax:help", "ax:settings"])
def test_arcus_user_ax_buttons_pass_to_the_venue_handler(monkeypatch, data):
    world = World(monkeypatch, ARCUS)
    update = cb(data)
    assert run(update) is True
    assert update.callback_query.answers == []
    assert world.rendered == []


def test_dispatch_rechecks_the_venue_under_the_lock(monkeypatch):
    # Classified as Arcus, but a switch back to Nado landed while it queued.
    reads = iter([ARCUS, NADO])
    world = World(monkeypatch, lambda: next(reads))
    update = cb("nav:main")
    assert run(update) is False
    assert world.rendered == []


@pytest.mark.parametrize("text,target", [
    ("/start", vc.AX_HOME), ("/start ref_ABC123", vc.AX_HOME), ("/status", vc.AX_HOME),
    ("/Status@nadobro_bot", vc.AX_HOME), ("/mm_status", vc.AX_UNAVAILABLE), ("/mm_fills", vc.AX_UNAVAILABLE),
])
def test_arcus_user_dispatch_commands_reply_with_the_arcus_screen(monkeypatch, text, target):
    world = World(monkeypatch, ARCUS)
    update = cmd(text)
    assert run(update) is False
    assert world.rendered == [(target, "message")]
    assert world.touched == 1  # last_active still refreshed, as /start would


@pytest.mark.parametrize("text", ["/desk", "/brief", "/howl", "/news", "/airdrop", "/agent_on",
                                  "/agent_status", "/nonexistent"])
def test_arcus_user_nado_only_commands_are_denied(monkeypatch, text):
    world = World(monkeypatch, ARCUS)
    update = cmd(text)
    assert run(update) is False
    assert update.message.replies == [(vh.TEXT_DENIED_ON_ARCUS, {})]
    assert world.rendered == []


@pytest.mark.parametrize("text", ["/stop_all", "/agent_off", "/revoke", "/venue", "/help", "/ops"])
def test_arcus_user_stop_and_neutral_commands_pass(monkeypatch, text):
    world = World(monkeypatch, ARCUS)
    update = cmd(text)
    assert run(update) is True
    assert update.message.replies == []
    assert world.rendered == []


# ---------------------------------------------------------------------------
# free text on the Arcus view never reaches Nado trade parsing / relay / LLM
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "long BTC 10x", "BTC", "yes", "confirm", "0", "1,2", "what is unified margin?",
    "close all", "ref_ABC123", "0x" + "ab" * 32,
])
def test_arcus_free_text_gets_the_hint_and_goes_nowhere(monkeypatch, text):
    world = World(monkeypatch, ARCUS)
    update = txt(text)
    assert run(update) is False  # handle_message (trade parser, LOWIQPTS relay, LLM) never runs
    assert world.rendered == []
    [(reply, kwargs)] = update.message.replies
    assert reply == vh.TEXT_ARCUS_FREE_TEXT_HINT
    markup = kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["venue:view"]
    assert world.touched == 1


@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGS))
@pytest.mark.parametrize("label,target", [("🏠 Home", vc.AX_HOME), ("⚙️ Settings", vc.AX_SETTINGS),
                                          ("📁 Portfolio", vc.AX_UNAVAILABLE)])
def test_arcus_reply_keyboard_views_render_their_arcus_screen_in_every_language(monkeypatch, lang, label, target):
    world = World(monkeypatch, ARCUS)
    update = txt(localize_label(label, lang))
    assert run(update) is False
    assert world.rendered == [(target, "message")]
    assert update.message.replies == []


@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGS))
def test_arcus_hint_is_localized(monkeypatch, lang):
    World(monkeypatch, ARCUS)
    monkeypatch.setattr(vg, "localize_markup", lambda kb, _lang: kb)  # stub-safe keyboard
    token = _ACTIVE_LANG.set(lang)
    try:
        update = txt("BTC")
        assert run(update) is False
    finally:
        _ACTIVE_LANG.reset(token)
    assert update.message.replies[0][0] == localize_text(vh.TEXT_ARCUS_FREE_TEXT_HINT, lang)


def test_arcus_free_text_is_never_logged(monkeypatch, caplog):
    secret = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
    World(monkeypatch, ARCUS)
    with caplog.at_level(logging.DEBUG):
        run(txt(secret))
        run(txt(f"my key is {secret}"))
        run(edited(secret))
        run(cmd("/" + secret[2:34]))  # an unregistered "/command" is not logged by name either
    assert secret not in caplog.text
    assert secret[2:34] not in caplog.text


def test_arcus_non_text_and_edited_messages_stop_silently(monkeypatch):
    for factory in (photo, lambda: edited("long BTC 10x"), lambda: edited("/stop_all")):
        world = World(monkeypatch, ARCUS)
        update = factory()
        assert run(update) is False
        assert (update.message or update.edited_message).replies == []
        assert world.rendered == []


# ---------------------------------------------------------------------------
# failure policy
# ---------------------------------------------------------------------------

def test_unreadable_venue_is_treated_as_nado(monkeypatch, caplog):
    World(monkeypatch, RuntimeError("db down"))
    with caplog.at_level(logging.WARNING):
        assert run(cb("strategy:start:grid:BTC")) is True
        assert run(txt("hello")) is True
        denied = cb("ax:home")
        assert run(denied) is False  # ax:* stays denied
    assert denied.callback_query.answers == [(vh.TEXT_ARCUS_BUTTON_ON_NADO, True)]
    assert "treating as nado" in caplog.text


async def _render_fails(*_a, **_k):
    raise RuntimeError("render failed")


def test_a_crash_after_reading_arcus_denies(monkeypatch):
    world = World(monkeypatch, ARCUS)
    monkeypatch.setattr(vg, "render_arcus_target", _render_fails)
    assert run(cb("nav:main")) is False
    assert run(cmd("/start")) is False
    assert world.venue_reads >= 2


def test_a_crash_for_a_nado_user_passes(monkeypatch):
    World(monkeypatch, NADO)

    def boom(_data):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(vg, "classify_callback", boom)
    assert run(cb("strategy:start:grid:BTC")) is True


def test_a_crash_before_the_venue_is_known_passes(monkeypatch):
    # Nothing read yet -> not known to be Arcus -> the update goes on (Nado view).
    World(monkeypatch, ARCUS)
    monkeypatch.setattr(vg, "classify_command", lambda _n: (_ for _ in ()).throw(RuntimeError("bug")))
    assert run(cmd("/desk")) is True


def test_no_effective_user_passes(monkeypatch):
    World(monkeypatch, ARCUS)
    update = SimpleNamespace(callback_query=FakeQuery("strategy:start:grid:BTC"), message=None,
                             edited_message=None, effective_user=None, effective_message=None)
    assert run(update) is True


# ---------------------------------------------------------------------------
# command parsing mirrors PTB
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,length,expected", [
    ("/start", 6, "start"),
    ("/start payload", 6, "start"),
    ("/Start@Nadobro_Bot x", 18, "start"),
    ("/stop_all", 9, "stop_all"),
])
def test_command_name(text, length, expected):
    msg = SimpleNamespace(text=text, entities=(SimpleNamespace(type="bot_command", offset=0, length=length),))
    assert vg.command_name(msg) == expected


def test_command_name_rejects_non_commands():
    assert vg.command_name(SimpleNamespace(text="hi /start", entities=(
        SimpleNamespace(type="bot_command", offset=3, length=6),))) is None
    assert vg.command_name(SimpleNamespace(text="/start", entities=())) is None
    assert vg.command_name(SimpleNamespace(text=None, entities=None)) is None
    assert vg.command_name(SimpleNamespace(text="#tag", entities=(
        SimpleNamespace(type="hashtag", offset=0, length=4),))) is None


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_should_register_with_the_flag_on_touches_no_db(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setattr(vg, "count_users_on_venue", lambda _v: (_ for _ in ()).throw(AssertionError("no DB")))
    assert vg.should_register_venue_gate() is True


@pytest.mark.parametrize("count,expected", [(0, False), (1, True), (7, True)])
def test_should_register_with_the_flag_off_follows_arcus_users(monkeypatch, count, expected):
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    seen = []
    monkeypatch.setattr(vg, "count_users_on_venue", lambda v: seen.append(v) or count)
    assert vg.should_register_venue_gate() is expected
    assert seen == ["arcus"]


def test_should_register_fails_to_not_registered(monkeypatch, caplog):
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)

    def boom(_v):
        raise RuntimeError("column active_venue does not exist")

    monkeypatch.setattr(vg, "count_users_on_venue", boom)
    with caplog.at_level(logging.WARNING):
        assert vg.should_register_venue_gate() is False
    assert "NOT registered" in caplog.text


class _RecordingApp:
    def __init__(self):
        self.added: list[tuple[object, int]] = []

    def add_handler(self, handler, group=0):
        self.added.append((handler, group))


def _fake_handler_classes(monkeypatch):
    import sys

    ext = sys.modules["telegram.ext"]

    class TypeHandler:
        def __init__(self, type_, callback):
            self.kind, self.type_, self.callback = "type", type_, callback

    class CommandHandler:
        def __init__(self, command, callback):
            self.kind, self.command, self.callback = "command", command, callback

    class CallbackQueryHandler:
        def __init__(self, callback, pattern=None):
            self.kind, self.callback, self.pattern = "callback", callback, pattern

    for cls in (TypeHandler, CommandHandler, CallbackQueryHandler):
        monkeypatch.setattr(ext, cls.__name__, cls, raising=False)


def test_register_adds_nothing_when_disabled(monkeypatch):
    _fake_handler_classes(monkeypatch)
    app = _RecordingApp()
    assert vg.register_venue_handlers(app, enabled=False) is False
    assert app.added == []


def test_register_adds_gate_venue_command_and_callbacks(monkeypatch):
    _fake_handler_classes(monkeypatch)
    app = _RecordingApp()
    assert vg.register_venue_handlers(app, enabled=True) is True
    kinds = [(h.kind, g) for h, g in app.added]
    assert kinds == [("type", -1), ("command", 0), ("callback", 0)]
    gate, command, callback = (h for h, _ in app.added)
    assert gate.callback is vg.venue_gate
    assert command.command == "venue"
    assert callback.pattern == r"^(?:venue|ax):"
    assert vg.VENUE_GATE_GROUP == -1
