"""The Arcus-scoped secret interceptor at the venue gate (Arcus P3b, 03 §9.8,
§11.1, §19.7; build_decisions D-11).

Harness ``_dispatch``: the gate (group -1) runs first; ``ApplicationHandlerStop``
means no group-0 handler runs. Otherwise ``messages.handle_message`` runs with
the LOWIQPTS relay and the LLM fall-through replaced by recorders — so "the
secret never reaches the relay / LLM" is checked on the real Nado router.

Covered: new AND edited messages, text AND captions, the Arcus view, the Nado
view with and without a pending link, an unreadable venue (fail-closed for
secrets only), delete failures, crashes (still stopped), an expired pending
entry, the off-lock background verification, supersession by a newer paste,
and a full paste -> LINKED flow after which the seed (and the pubkey) appears
in no log, reply, user_data, audit record or bot_state write.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from _stubs import install_test_stubs

install_test_stubs()

from telegram.error import BadRequest  # noqa: E402
from telegram.ext import ApplicationHandlerStop  # noqa: E402

import arcus_handler_helpers as AH  # noqa: E402
import arcus_link_helpers as H  # noqa: E402
from arcus_handler_helpers import FakeBot, FakeMessage, World, ctx, drain, update_for  # noqa: E402
from arcus_link_helpers import ADDR, DAY_MS, NOW_MS, PUB_B, RFC_PUB, RFC_SEED, SEED_B, UID, entry, ok  # noqa: E402
from src.nadobro.core import crypto  # noqa: E402
from src.nadobro.handlers import arcus_ui  # noqa: E402
from src.nadobro.handlers import arcus_wallet_handler as awh  # noqa: E402
from src.nadobro.handlers import messages  # noqa: E402
from src.nadobro.handlers import update_serialization as us  # noqa: E402
from src.nadobro.handlers import venue_gate as vg  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402
from src.nadobro.i18n import _ACTIVE_LANG  # noqa: E402
from src.nadobro.users import arcus_link_service as ls  # noqa: E402
from src.nadobro.users.arcus_link_service import AddressCheck, LinkPending, LinkResult  # noqa: E402

GENERIC = arcus_ui.TEXT_K_GENERIC_DELETED + "\n" + arcus_ui.TEXT_K_GENERIC_EXPOSED
# The same warning when the message could NOT be deleted: it never claims a deletion (SEC-1).
GENERIC_KEPT = (
    arcus_ui.TEXT_K_GENERIC_KEPT + "\n" + arcus_ui.TEXT_K_GENERIC_EXPOSED + "\n\n" + arcus_ui.TEXT_K_NOT_DELETED
)
PEM = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIJ1hsZ3v/VpguoRK9JLsLMREScVpezJpGXA7rAMcrn9g\n-----END PRIVATE KEY-----"

# Every group-0 step of messages._handle_message_inner that runs before the relay.
_NADO_STEPS = (
    "_handle_wallet_flow", "_handle_pending_strategy_input", "_handle_pending_bro_input",
    "_handle_pending_question", "handle_trade_card_text_input", "handle_pending_text_trade_confirmation",
    "_handle_pending_text_close_all_confirmation", "_handle_trade_flow_free_text", "_handle_pending_trade",
    "_handle_pending_alert", "_handle_pending_copy_wallet", "_handle_pending_admin_copy_wallet",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEYS", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))
    monkeypatch.delenv("ARCUS_MAINNET_ENABLED", raising=False)
    token = _ACTIVE_LANG.set("en")
    us._user_locks.clear()
    yield
    _ACTIVE_LANG.reset(token)
    ls._reset_for_tests()
    awh._reset_for_tests()
    crypto._fernet_instance = None


class Nado:
    """The group-0 side: handle_message with recorders at the relay and the LLM."""

    def __init__(self, monkeypatch):
        self.relayed: list[str] = []
        self.llm: list[str] = []
        self.reached = 0

        async def relay(context, chat_id, text):
            self.relayed.append(text)
            return {"handled": True, "ok": True}

        async def llm(update, context, question):
            self.llm.append(question)

        async def no(*_a, **_k):
            return False

        monkeypatch.setattr(messages, "relay_user_reply_to_lowiqpts", relay)
        monkeypatch.setattr(messages, "_handle_nado_question", llm)
        monkeypatch.setattr(messages, "get_or_create_user", lambda *a, **k: None)
        monkeypatch.setattr(messages, "get_user_language", lambda uid: "en")
        for name in _NADO_STEPS:
            monkeypatch.setattr(messages, name, no)
        monkeypatch.setattr(vg, "get_or_create_user", lambda *a, **k: None)

    async def handle(self, update, context):
        self.reached += 1
        await messages.handle_message(update, context)


def _world(monkeypatch, venue="arcus"):
    world = World(monkeypatch, venue=venue)

    async def read(uid):
        if isinstance(world.venue, BaseException):
            raise world.venue
        return world.venue

    monkeypatch.setattr(vg, "read_active_venue", read)
    return world


async def _dispatch(nado, update, context):
    """PTB's groups in miniature: the gate (-1), then group 0 unless stopped.
    Returns True when group 0 ran."""
    try:
        await vg.venue_gate(update, context)
    except ApplicationHandlerStop:
        return False
    await nado.handle(update, context)
    return True


def _pending(step="key", *, generation=None, expires_in=1800.0, address=ADDR, mono=None):
    now = mono.now if mono is not None else ls._mono()
    return LinkPending(
        network="testnet",
        step=step,
        expires_mono=now + expires_in,
        generation=generation if generation is not None else ls.begin_generation(UID, "testnet"),
        attested_at=datetime(2026, 9, 21, tzinfo=timezone.utc) if step != "attest" else None,
        address=address if step in ("address_check", "key", "verifying") else None,
        key_name="nadobro-ab12" if step in ("key", "verifying") else None,
        address_check=AddressCheck.ELIGIBLE if step in ("key", "verifying") else None,
        address_checked_mono=now if step in ("key", "verifying") else None,
        has_activity=True if step in ("key", "verifying") else None,
    )


def _with_pending(world, step="key", **kw):
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(step, mono=world.env.mono, **kw)
    return context


def _record_process_paste(monkeypatch):
    started = []

    async def fake(context, uid, pending, text, target):
        started.append((uid, pending.step))

    monkeypatch.setattr(awh, "_process_paste", fake)
    return started


# ---------------------------------------------------------------------------
# 1-2. Arcus view, no pending: deleted FIRST, generic warning, never group 0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["new", "edited"])
@pytest.mark.parametrize("secret", [RFC_SEED, "0x" + RFC_SEED, RFC_SEED.upper(), f"my key is {RFC_SEED} ok", PEM])
def test_arcus_view_secret_is_deleted_first_and_warned(monkeypatch, kind, secret):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = ctx()
    msg = FakeMessage(secret, log=context.bot.log)
    update = update_for(msg) if kind == "new" else update_for(None, edited=msg)

    async def body():
        reached = await _dispatch(nado, update, context)
        await drain()
        return reached

    assert asyncio.run(body()) is False
    assert msg.deleted == 1
    assert context.bot.log[0] == ("delete", None)  # delete awaited before anything else
    assert context.bot.texts() == [GENERIC]
    assert nado.reached == 0 and nado.relayed == [] and nado.llm == []
    assert world.db.upserts == []


# ---------------------------------------------------------------------------
# 3-4. Nado view with a pending key step: deleted + verification started
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["new", "edited"])
def test_nado_view_pending_key_is_intercepted_and_verified(monkeypatch, kind):
    world = _world(monkeypatch, "nado")
    nado = Nado(monkeypatch)
    started = _record_process_paste(monkeypatch)
    context = _with_pending(world, "key")
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    update = update_for(msg) if kind == "new" else update_for(None, edited=msg)

    async def body():
        reached = await _dispatch(nado, update, context)
        await drain()
        return reached

    assert asyncio.run(body()) is False
    assert msg.deleted == 1
    assert started == [(UID, "key")]
    assert context.bot.texts() == [arcus_ui.TEXT_K_ACK]
    assert nado.relayed == [] and nado.llm == []


# ---------------------------------------------------------------------------
# 5-6. Nado view, nothing pending: new messages route as today; edits stop
# ---------------------------------------------------------------------------

def test_nado_view_without_pending_routes_a_hex_message_as_today(monkeypatch):
    # D-11: the interceptor is Arcus-scoped. A Nado user pasting a 64-hex (e.g. a tx
    # hash) keeps today's routing: not deleted, it reaches handle_message.
    _world(monkeypatch, "nado")
    nado = Nado(monkeypatch)
    context = ctx()
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is True
    assert msg.deleted == 0
    assert nado.relayed == [RFC_SEED]
    assert context.bot.sent == []


def test_nado_view_edited_message_stops_silently(monkeypatch):
    _world(monkeypatch, "nado")
    nado = Nado(monkeypatch)
    context = ctx()
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(None, edited=msg), context)) is False
    assert msg.deleted == 0 and context.bot.sent == []
    assert nado.reached == 0


# ---------------------------------------------------------------------------
# 7. unreadable venue: fail-closed for secrets only
# ---------------------------------------------------------------------------

def test_unreadable_venue_intercepts_a_secret(monkeypatch, caplog):
    _world(monkeypatch, RuntimeError("db down"))
    nado = Nado(monkeypatch)
    context = ctx()
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    assert msg.deleted == 1 and context.bot.texts() == [GENERIC]
    assert nado.reached == 0
    assert RFC_SEED not in caplog.text


def test_unreadable_venue_keeps_p1_policy_for_other_text(monkeypatch):
    _world(monkeypatch, RuntimeError("db down"))
    nado = Nado(monkeypatch)
    context = ctx()
    msg = FakeMessage("long BTC 10x", log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is True  # never seen on Arcus -> Nado
    assert msg.deleted == 0 and nado.relayed == ["long BTC 10x"]


# ---------------------------------------------------------------------------
# 8. pending key step, non-key shapes; pending address step
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,reply", [
    (f"key: {RFC_SEED}", arcus_ui.TEXT_R_INVALID),
    ("ab" * 64, arcus_ui.TEXT_R_INVALID),
    (PEM, arcus_ui.TEXT_R_PEM),
])
def test_pending_key_step_other_shapes_are_deleted_and_explained(monkeypatch, text, reply):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    started = _record_process_paste(monkeypatch)
    context = _with_pending(world, "key")
    msg = FakeMessage(text, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    assert msg.deleted == 1 and started == []
    assert context.bot.texts() == [reply]


@pytest.mark.parametrize("step", ["attest", "address"])
def test_pending_before_the_address_asks_for_the_address_first(monkeypatch, step):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    started = _record_process_paste(monkeypatch)
    context = _with_pending(world, step)
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    assert msg.deleted == 1 and started == []
    assert context.bot.texts() == [arcus_ui.TEXT_K_ADDRESS_FIRST]


# ---------------------------------------------------------------------------
# 9-10. delete failures; captions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error", [BadRequest("Message can't be deleted"), AttributeError("x"), RuntimeError("y")])
def test_a_failed_delete_still_warns_and_asks_the_user_to_delete(monkeypatch, error):
    _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = ctx()
    msg = FakeMessage(RFC_SEED, log=context.bot.log, delete_error=error)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    [text] = context.bot.texts()
    # Updated for SEC-1: the not-deleted variant never says "I deleted".
    assert text == GENERIC_KEPT
    assert "I deleted" not in text
    assert nado.reached == 0


def test_a_message_without_delete_is_handled_by_the_catch_all(monkeypatch):
    _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = ctx()
    msg = FakeMessage(RFC_SEED, log=context.bot.log, no_delete=True)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    assert context.bot.texts() == [GENERIC_KEPT]  # SEC-1: no deletion claimed


@pytest.mark.parametrize("venue", ["arcus", "nado"])
def test_a_caption_with_a_key_is_deleted(monkeypatch, venue):
    world = _world(monkeypatch, venue)
    nado = Nado(monkeypatch)
    started = _record_process_paste(monkeypatch)
    context = _with_pending(world, "key") if venue == "nado" else ctx()
    msg = FakeMessage(None, caption=RFC_SEED, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    assert msg.deleted == 1
    assert nado.reached == 0
    if venue == "nado":
        assert started == [(UID, "key")]


# ---------------------------------------------------------------------------
# 11, 18. crashes never let a secret through
# ---------------------------------------------------------------------------

def test_a_failing_reply_still_stops_the_update(monkeypatch, caplog):
    _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = ctx()
    context.bot.fail_send = RuntimeError(f"boom {RFC_SEED}")
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    assert msg.deleted == 1 and nado.reached == 0
    assert "RuntimeError" in caplog.text and RFC_SEED not in caplog.text


@pytest.mark.parametrize("venue", ["arcus", "nado"])
def test_an_interceptor_crash_is_fail_closed_via_probe_secret(monkeypatch, venue):
    world = _world(monkeypatch, venue)
    nado = Nado(monkeypatch)

    async def crash(update, context, *, shape, venue):
        raise RuntimeError("interceptor bug")

    monkeypatch.setattr(awh, "arcus_secret_interceptor", crash)
    context = _with_pending(world, "key") if venue == "nado" else ctx()
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False  # never reaches group 0
    assert nado.reached == 0


def test_the_recheck_is_fail_closed_for_secrets_too(monkeypatch):
    # venue_recheck (inside the per-user lock) never runs the Nado handler when the
    # interceptor crashes on a secret-shaped message.
    world = _world(monkeypatch, "nado")

    async def crash(update, context, *, shape, venue):
        raise RuntimeError("interceptor bug")

    monkeypatch.setattr(awh, "arcus_secret_interceptor", crash)
    context = _with_pending(world, "key")
    ran = []

    async def nado_handler(update, context):
        ran.append(update)

    asyncio.run(vg.venue_recheck(nado_handler)(update_for(FakeMessage(RFC_SEED)), context))
    assert ran == []


def test_an_error_before_link_pending_returns_still_logs_and_stops(monkeypatch, caplog):
    _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)

    def broken(context, uid):
        raise RuntimeError("state bug")

    monkeypatch.setattr(awh, "link_pending", broken)
    context = ctx()
    msg = FakeMessage(RFC_SEED, log=context.bot.log)
    with caplog.at_level(logging.INFO):
        assert asyncio.run(_dispatch(nado, update_for(msg), context)) is False
    assert msg.deleted == 1
    assert "arcus secret intercepted" in caplog.text and "step=-" in caplog.text
    assert "NameError" not in caplog.text and RFC_SEED not in caplog.text


# ---------------------------------------------------------------------------
# 15-17. non-secret text; an expired pending entry; the address step
# ---------------------------------------------------------------------------

def test_non_secret_text_with_a_pending_link_on_the_nado_view_passes(monkeypatch):
    world = _world(monkeypatch, "nado")
    nado = Nado(monkeypatch)
    context = _with_pending(world, "key")
    msg = FakeMessage("long BTC 10x", log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(msg), context)) is True
    assert msg.deleted == 0 and nado.relayed == ["long BTC 10x"]


def test_an_expired_pending_entry_still_intercepts_once(monkeypatch):
    world = _world(monkeypatch, "nado")
    nado = Nado(monkeypatch)
    started = _record_process_paste(monkeypatch)
    context = _with_pending(world, "key", expires_in=-1.0)  # already expired
    first = FakeMessage(RFC_SEED, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(first), context)) is False  # the gate ignores the TTL
    assert first.deleted == 1 and started == []
    assert context.bot.texts() == [GENERIC]
    assert awh.PENDING_KEY not in context.user_data  # the interceptor popped it
    second = FakeMessage(RFC_SEED, log=context.bot.log)
    assert asyncio.run(_dispatch(nado, update_for(second), context)) is True  # plain Nado routing again
    assert second.deleted == 0 and nado.relayed == [RFC_SEED]


def test_an_address_on_the_arcus_view_goes_to_the_link_flow(monkeypatch):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    world.client.api_keys = [ok([])]
    context = _with_pending(world, "address")
    msg = FakeMessage(ADDR, log=context.bot.log)

    async def body():
        reached = await _dispatch(nado, update_for(msg), context)
        await drain()
        return reached

    assert asyncio.run(body()) is False
    assert msg.deleted == 0 and nado.reached == 0
    [(reply, _kw)] = msg.replies
    assert reply.startswith("Checking <code>" + ADDR)
    assert context.user_data[awh.PENDING_KEY].step == "key"  # the precheck passed


# ---------------------------------------------------------------------------
# 13. verification runs OFF the per-user lock
# ---------------------------------------------------------------------------

def test_verification_runs_off_the_per_user_lock(monkeypatch):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = _with_pending(world, "key")
    gate = {}

    async def slow_verify(*, user_id, pending, pasted_secret=None):
        gate["started"].set()
        await gate["release"].wait()
        return ls.LinkOutcome(result=LinkResult.BUSY, network="testnet", address=ADDR)

    monkeypatch.setattr(ls, "verify_and_store", slow_verify)
    other_ran = []

    async def other(update, context):
        other_ran.append(True)

    async def body():
        gate["started"], gate["release"] = asyncio.Event(), asyncio.Event()
        msg = FakeMessage(RFC_SEED, log=context.bot.log)
        assert await _dispatch(nado, update_for(msg), context) is False
        await asyncio.wait_for(gate["started"].wait(), 5)
        lock = us._user_locks.get(UID)
        assert lock is None or not lock.locked()
        await us.with_user_serialized(other)(update_for(FakeMessage("hi")), ctx())
        assert other_ran == [True]  # completed while verification is still blocked
        assert not gate["release"].is_set()
        gate["release"].set()
        await drain()

    asyncio.run(body())
    assert context.user_data[awh.PENDING_KEY].step == "verifying"  # BUSY keeps the stash


# ---------------------------------------------------------------------------
# 14. a newer paste supersedes the older one
# ---------------------------------------------------------------------------

def test_a_newer_paste_supersedes_the_older_one(monkeypatch):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = _with_pending(world, "key")
    client = world.client
    world.client.api_keys = [ok([entry(PUB_B, until=NOW_MS + 100 * DAY_MS)])]
    gate = {}
    real_get_api_keys = client.get_api_keys

    async def gated(address, **kw):
        if not gate["release"].is_set():
            gate["blocked"].set()
            await gate["release"].wait()
        return await real_get_api_keys(address, **kw)

    monkeypatch.setattr(client, "get_api_keys", gated)

    async def body():
        gate["blocked"], gate["release"] = asyncio.Event(), asyncio.Event()
        await _dispatch(nado, update_for(FakeMessage(RFC_SEED, log=context.bot.log)), context)
        await asyncio.wait_for(gate["blocked"].wait(), 5)  # paste A is polling apiKeys
        await _dispatch(nado, update_for(FakeMessage(SEED_B, log=context.bot.log)), context)
        await asyncio.sleep(0)
        gate["release"].set()
        await drain()

    asyncio.run(body())
    assert [u["sealed"].api_public_key for u in world.db.upserts] == [PUB_B]
    assert awh.PENDING_KEY not in context.user_data  # linked: the flow ended


# ---------------------------------------------------------------------------
# 12. full flow: the secret appears nowhere
# ---------------------------------------------------------------------------

def test_a_full_link_flow_never_leaks_the_secret(monkeypatch, caplog):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    state_writes = []
    from src.nadobro.models import database

    monkeypatch.setattr(database, "set_bot_state", lambda *a, **k: state_writes.append((a, k)))
    world.client.api_keys = [ok([]), ok([entry(RFC_PUB, until=NOW_MS + 179 * DAY_MS)])]
    context = _with_pending(world, "key")
    replies = []

    async def body():
        for kind in ("new", "edited"):
            msg = FakeMessage("  0x" + RFC_SEED.upper() + "\n", log=context.bot.log)
            update = update_for(msg) if kind == "new" else update_for(None, edited=msg)
            assert await _dispatch(nado, update, context) is False
            replies.extend(msg.replies)
            await drain()
            if kind == "new":
                # linked after the first paste; re-open a flow for the edited paste
                context.user_data[awh.PENDING_KEY] = _pending("key", mono=world.env.mono)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(body())
    assert len(world.db.upserts) >= 1
    assert world.db.upserts[0]["sealed"].api_public_key == RFC_PUB
    shown = [t for _c, t, _k in context.bot.sent] + [e[1] for e in context.bot.log if e[0] == "edit"]
    assert any("<b>Linked</b>" in t for t in shown)
    secrets = (RFC_SEED,)
    assert RFC_SEED not in caplog.text and RFC_SEED.upper() not in caplog.text
    assert not H.contains_secret(shown, secrets)
    assert not H.contains_secret(replies, secrets)
    assert not H.contains_secret(context.user_data, secrets)
    assert not H.contains_secret(world.db.audits, secrets)
    assert not H.contains_secret(state_writes, secrets)
    # the pubkey is not echoed either
    assert RFC_PUB not in caplog.text
    assert not H.contains_secret(shown, (RFC_PUB,)) and not H.contains_secret(context.user_data, (RFC_PUB,))


def test_the_gate_pending_key_constant_matches_the_handler():
    assert vg._ARCUS_LINK_PENDING_KEY == awh.PENDING_KEY == "arcus_link_pending"


def test_gate_imports_no_arcus_module_at_import_time():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(vg.__file__)).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {getattr(n, "module", None) or n.names[0].name for n in top}
    assert not any("arcus" in (name or "") for name in names), names
    assert "src.nadobro.utils.secret_text" in names


# ---------------------------------------------------------------------------
# SEC-1: a paste that could NOT be deleted (Telegram refuses messages older than
# 48 h — e.g. an edited old message — and API errors happen). Before the fix the
# "delete it yourself" warning lived only in the ack, and the first result EDITED
# it away; for a wallet key the result even said "I deleted it". Now every text
# shown for that paste ends with the warning and none claims a deletion.
# ---------------------------------------------------------------------------

_DELETE_FAILED = BadRequest("Message can't be deleted")
_DELETION_CLAIMS = ("I deleted", "removed from the chat")


def _shown(log):
    return [text for kind, text in log if kind in ("send", "edit")]


def _assert_kept_and_warned(log):
    """The last visible text (the ack's final edit) ends with the warning, and no
    text ever shown claims the message was deleted."""
    shown = _shown(log)
    assert shown, log
    assert shown[-1].endswith("\n\n" + arcus_ui.TEXT_K_NOT_DELETED), shown[-1]
    for text in shown:
        assert not any(claim in text for claim in _DELETION_CLAIMS), text
    return shown[-1]


def _paste_undeletable(monkeypatch, world, context, secret, *, kind="new"):
    nado = Nado(monkeypatch)
    msg = FakeMessage(secret, log=context.bot.log, delete_error=_DELETE_FAILED)
    update = update_for(msg) if kind == "new" else update_for(None, edited=msg)

    async def body():
        reached = await _dispatch(nado, update, context)
        await drain()
        return reached

    assert asyncio.run(body()) is False
    assert msg.deleted == 0 and nado.reached == 0
    return msg


@pytest.mark.parametrize("kind", ["new", "edited"])
def test_undeleted_wallet_key_result_never_says_i_deleted_it(monkeypatch, kind):
    world = _world(monkeypatch, "arcus")
    context = _with_pending(world, "key", address=H.WALLET_ADDR)
    _paste_undeletable(monkeypatch, world, context, H.WALLET_SEED, kind=kind)
    final = _assert_kept_and_warned(context.bot.log)
    assert final.startswith(ls.TEXT_R_WALLET_KEY + "\n" + arcus_ui.TEXT_R_WALLET_KEY_2_KEPT)
    assert _shown(context.bot.log)[0] == arcus_ui.TEXT_K_ACK_KEPT + "\n\n" + arcus_ui.TEXT_K_NOT_DELETED
    assert world.db.upserts == []


def test_undeleted_key_that_links_keeps_the_warning(monkeypatch):
    world = _world(monkeypatch, "arcus")
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 179 * DAY_MS)])]
    context = _with_pending(world, "key")
    _paste_undeletable(monkeypatch, world, context, RFC_SEED)
    final = _assert_kept_and_warned(context.bot.log)
    assert final.startswith("✅ <b>Linked</b>")
    assert len(world.db.upserts) == 1


def test_undeleted_key_busy_result_keeps_the_warning(monkeypatch):
    world = _world(monkeypatch, "arcus")
    world.client.api_keys = [H.UNAVAILABLE]
    context = _with_pending(world, "key")
    _paste_undeletable(monkeypatch, world, context, RFC_SEED)
    final = _assert_kept_and_warned(context.bot.log)
    assert final == ls.TEXT_BUSY + "\n\n" + arcus_ui.TEXT_K_NOT_DELETED
    assert context.user_data[awh.PENDING_KEY].step == "verifying"  # [Check again] still works


def test_undeleted_key_waiting_for_the_address_check_keeps_the_warning(monkeypatch):
    world = _world(monkeypatch, "arcus")
    context = _with_pending(world, "address_check")
    _paste_undeletable(monkeypatch, world, context, RFC_SEED)
    final = _assert_kept_and_warned(context.bot.log)
    assert final == arcus_ui.TEXT_K_WAIT_ADDRESS_KEPT + "\n\n" + arcus_ui.TEXT_K_NOT_DELETED
    # ...and when the precheck then passes, its card shows the result; the ack (the
    # message next to the key still in the chat) keeps the warning.
    pending = context.user_data[awh.PENDING_KEY]
    card = AH.SentMessage([], UID, "Checking…")
    world.client.api_keys = [ok([]), ok([entry(RFC_PUB, until=NOW_MS + 179 * DAY_MS)])]
    async def precheck():
        await awh._precheck_and_report(context, UID, pending, awh._Target(card, UID, None))
        await drain()  # the verification it starts runs in this loop

    asyncio.run(precheck())
    assert card.text.startswith("✅ <b>Linked</b>")
    assert _shown(context.bot.log)[-1] == final


@pytest.mark.parametrize("step", ["attest", "address"])
def test_undeleted_key_before_the_address_never_says_i_deleted_your_key(monkeypatch, step):
    world = _world(monkeypatch, "arcus")
    context = _with_pending(world, step)
    _paste_undeletable(monkeypatch, world, context, RFC_SEED)
    assert context.bot.texts() == [arcus_ui.TEXT_K_ADDRESS_FIRST_KEPT + "\n\n" + arcus_ui.TEXT_K_NOT_DELETED]


def test_undeleted_key_warning_follows_a_check_again_on_another_card(monkeypatch):
    # [🔄 Check again] while the verification runs re-points the result to the tapped
    # card: the warning goes with it.
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = _with_pending(world, "key")
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 179 * DAY_MS)])]
    gate = {}
    real = world.client.get_api_keys

    async def gated(address, **kw):
        if not gate["release"].is_set():
            gate["blocked"].set()
            await gate["release"].wait()
        return await real(address, **kw)

    monkeypatch.setattr(world.client, "get_api_keys", gated)

    async def body():
        gate["blocked"], gate["release"] = asyncio.Event(), asyncio.Event()
        msg = FakeMessage(RFC_SEED, log=context.bot.log, delete_error=_DELETE_FAILED)
        assert await _dispatch(nado, update_for(msg), context) is False
        await asyncio.wait_for(gate["blocked"].wait(), 5)
        tapped = AH.FakeQuery("ax:link:check")
        await awh.handle(tapped, "ax:link:check", UID, context)
        gate["release"].set()
        await drain()
        return tapped

    tapped = asyncio.run(body())
    assert tapped.message.text.startswith("✅ <b>Linked</b>")
    assert tapped.message.text.endswith("\n\n" + arcus_ui.TEXT_K_NOT_DELETED)


# ---------------------------------------------------------------------------
# R2-2: a paste is activity. Before the fix a key pasted with little TTL left was
# STORED while the flow expired mid-poll, and the result was dropped silently.
# ---------------------------------------------------------------------------

def test_a_paste_near_the_end_of_the_ttl_still_gets_its_result(monkeypatch):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    context = _with_pending(world, "key", expires_in=20.0)  # 20 s of TTL left at paste time
    # the new key becomes visible on the 5th read (offset 25 s): docs "takes a moment"
    world.client.api_keys = [ok([]), ok([]), ok([]), ok([]), ok([entry(RFC_PUB, until=NOW_MS + 179 * DAY_MS)])]

    async def body():
        await _dispatch(nado, update_for(FakeMessage(RFC_SEED, log=context.bot.log)), context)
        await drain()

    asyncio.run(body())
    assert len(world.db.upserts) == 1
    edits = [text for kind, text in context.bot.log if kind == "edit"]
    assert edits and edits[-1].startswith("✅ <b>Linked</b>")
    assert awh.PENDING_KEY not in context.user_data  # linked: the flow ended normally


def test_a_paste_refreshes_the_flow_ttl_and_the_stash_expiry(monkeypatch, caplog):
    world = _world(monkeypatch, "arcus")
    nado = Nado(monkeypatch)
    world.client.api_keys = [ok([])]  # never listed: KEY_NOT_FOUND after the full poll, stash kept
    context = _with_pending(world, "key", expires_in=5.0)
    pasted_at = world.env.mono.now

    async def body():
        await _dispatch(nado, update_for(FakeMessage(RFC_SEED, log=context.bot.log)), context)
        await drain()

    with caplog.at_level(logging.INFO):
        asyncio.run(body())
    ttl = awh.arcus_link_pending_ttl_s()
    assert world.env.mono.now >= pasted_at + 60.0  # the whole poll ran
    current = context.user_data[awh.PENDING_KEY]
    assert current.step == "verifying" and current.expires_mono == pasted_at + ttl
    assert ls._STASH[(UID, "testnet")].expires_mono == pasted_at + ttl  # the stash inherits it
    assert "timed out mid-check" not in caplog.text
    edits = [text for kind, text in context.bot.log if kind == "edit"]
    assert edits and "couldn't find that key" in edits[-1]
