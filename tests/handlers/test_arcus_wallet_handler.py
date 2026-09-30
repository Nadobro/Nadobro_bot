"""handlers/arcus_wallet_handler.py — the Arcus wallet card, the ax:link:* flow,
unlink, the network card and the background result reporting (Arcus P3b,
03 §9, §19.8; the handler-side cases of §19.4).

Every Arcus read is a scripted P2 outcome and every DB call an off-loop fake
(tests/arcus_link_helpers.py); the handler seams (network mode, venue,
language) come from tests/arcus_handler_helpers.py.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from _stubs import install_test_stubs

install_test_stubs()

import arcus_link_helpers as H  # noqa: E402
from arcus_handler_helpers import (  # noqa: E402
    FakeMessage,
    FakeQuery,
    SentMessage,
    World,
    buttons,
    callback_data,
    ctx,
    drain,
    update_for,
)
from arcus_link_helpers import (  # noqa: E402
    ADDR,
    ADDR2,
    DAY_MS,
    NOW_MS,
    PUB_B,
    RFC_PUB,
    RFC_SEED,
    UID,
    WHITELIST,
    compliance,
    entry,
    ok,
    row,
)
from src.nadobro.core import crypto  # noqa: E402
from src.nadobro.handlers import arcus_ui  # noqa: E402
from src.nadobro.handlers import arcus_wallet_handler as awh  # noqa: E402
from src.nadobro.handlers import callbacks  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402
from src.nadobro.i18n import _ACTIVE_LANG  # noqa: E402
from src.nadobro.users import arcus_link_service as ls  # noqa: E402
from src.nadobro.users.arcus_link_service import AddressCheck, LinkOutcome, LinkPending, LinkResult  # noqa: E402

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_REUSED_LABELS = {"🏠 Home", "◀ Back", "❌ Cancel", "🔄 Refresh", vh.LABEL_VENUE}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEYS", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))
    monkeypatch.delenv("ARCUS_MAINNET_ENABLED", raising=False)
    monkeypatch.delenv("ARCUS_KEY_EXPIRY_STOP_HOURS", raising=False)
    token = _ACTIVE_LANG.set("en")
    yield
    _ACTIVE_LANG.reset(token)
    ls._reset_for_tests()
    awh._reset_for_tests()
    crypto._fernet_instance = None


def _card(query):
    text, kw = query.edits[-1]
    return text, kw.get("reply_markup")


def tap(data, context, *, card=None):
    """One tap through handle (the venue re-check already said Arcus)."""
    query = FakeQuery(data, card=card)

    async def body():
        await awh.handle(query, data, UID, context)
        await drain()

    asyncio.run(body())
    return query


def say(text, context):
    msg = FakeMessage(text, log=context.bot.log)

    async def body():
        consumed = await awh.arcus_text_router(update_for(msg), context, text)
        await drain()
        return consumed

    return msg, asyncio.run(body())


def _pending(world, step="key", *, address=ADDR, generation=None, previous=None, check=AddressCheck.ELIGIBLE):
    now = world.env.mono.now
    return LinkPending(
        network=world.network,
        step=step,
        expires_mono=now + 1800,
        generation=generation if generation is not None else ls.begin_generation(UID, world.network),
        attested_at=datetime(2026, 9, 21, tzinfo=timezone.utc) if step != "attest" else None,
        renewal=previous is not None,
        previous_address=previous,
        address=address if step in ("address_check", "key", "verifying") else None,
        key_name="nadobro-ab12" if step in ("key", "verifying") else None,
        address_check=check if step in ("key", "verifying") else None,
        address_checked_mono=now if step in ("key", "verifying") else None,
        has_activity=True if step in ("key", "verifying") else None,
    )


def _stash(world, pending, seed=RFC_SEED):
    async def body():
        return await ls.intake_key(user_id=UID, pending=pending, pasted_text=seed)

    assert asyncio.run(body()).status == "stashed"


# ---------------------------------------------------------------------------
# the wallet card
# ---------------------------------------------------------------------------

def test_wallet_db_error_never_says_not_linked(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = RuntimeError("db down")
    text, markup = _card(tap("ax:wallet", ctx()))
    assert arcus_ui.TEXT_W_UNREADABLE in text
    assert "Not linked" not in text
    assert callback_data(markup) == ["ax:wallet", "ax:home"]


def test_wallet_mode_unreadable_shows_a_dash_network(monkeypatch):
    world = World(monkeypatch)
    world.mode_error = RuntimeError("db down")
    text, _markup = _card(tap("ax:wallet", ctx()))
    assert "· —" in text and arcus_ui.TEXT_W_UNREADABLE in text


def test_wallet_not_linked_offers_link(monkeypatch):
    World(monkeypatch)
    text, markup = _card(tap("ax:wallet", ctx()))
    assert arcus_ui.TEXT_W_NOT_LINKED in text and "TESTNET" in text
    assert callback_data(markup) == ["ax:link:start", "ax:mode", "ax:home"]
    assert buttons(markup)[0][0] == arcus_ui.LABEL_LINK


def test_wallet_not_linked_flag_off_says_closed(monkeypatch):
    World(monkeypatch)
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    text, markup = _card(tap("ax:wallet", ctx()))
    assert arcus_ui.TEXT_W_LINK_CLOSED in text
    assert "ax:link:start" not in callback_data(markup)


def test_wallet_active_shows_address_key_expiry_and_soon(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS + 10 * DAY_MS + 3_600_000, name="nadobro-ab12")
    text, markup = _card(tap("ax:wallet", ctx()))
    assert f"<code>{ADDR}</code>" in text
    assert "<code>nadobro-ab12</code>" in text
    assert arcus_ui.format_utc_ms(NOW_MS + 10 * DAY_MS + 3_600_000) in text
    assert "expires in 10 days" in text and "stop 24 hours before" in text
    assert "Last checked: 2026-09-21 14:13 UTC" in text
    # R2-5: [✅ Check key] has its own callback (ax:link:check is [Continue linking]).
    assert callback_data(markup) == ["ax:link:start", "ax:link:key", "ax:unlink", "ax:mode", "ax:home"]
    assert buttons(markup)[0][0] == arcus_ui.LABEL_RENEW
    assert not _HEX64.search(text)  # neither the pubkey nor anything key-like


def test_wallet_key_soon_uses_the_single_stop_hours_reader(monkeypatch):
    world = World(monkeypatch)
    monkeypatch.setenv("ARCUS_KEY_EXPIRY_STOP_HOURS", "48")
    world.db.credential = H.row(until=NOW_MS + 5 * DAY_MS)
    text, _ = _card(tap("ax:wallet", ctx()))
    assert "stop 48 hours before" in text


def test_wallet_no_expiry_and_far_expiry_have_no_warning(monkeypatch):
    world = World(monkeypatch)
    for until in (0, NOW_MS + 30 * DAY_MS):
        world.db.credential = H.row(until=until)
        text, _ = _card(tap("ax:wallet", ctx()))
        assert "expires in" not in text
    world.db.credential = H.row(until=0)
    text, _ = _card(tap("ax:wallet", ctx()))
    assert "valid until no expiry" in text


def test_wallet_all_subaccounts_note(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(all_sub=True)
    text, _ = _card(tap("ax:wallet", ctx()))
    assert arcus_ui.TEXT_W_ALL_SUBACCOUNTS in text


@pytest.mark.parametrize("status,needle", [("expired", "The key expired on"), ("invalid", "no longer active on Arcus")])
def test_wallet_expired_and_invalid(monkeypatch, status, needle):
    world = World(monkeypatch)
    world.db.credential = H.row(status=status, until=NOW_MS - DAY_MS)
    text, markup = _card(tap("ax:wallet", ctx()))
    assert needle in text and f"<code>{ADDR}</code>" in text
    assert callback_data(markup)[:3] == ["ax:link:start", "ax:link:key", "ax:unlink"]  # R2-5


def test_wallet_active_but_past_valid_until_is_shown_expired(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS - 1)
    text, _ = _card(tap("ax:wallet", ctx()))
    assert "The key expired on" in text and "expires in" not in text


def test_wallet_with_a_pending_flow_offers_continue(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "address")
    text, markup = _card(tap("ax:wallet", context))
    assert arcus_ui.TEXT_W_LINKING in text
    assert callback_data(markup)[0] == "ax:link:check"
    assert buttons(markup)[0][0] == arcus_ui.LABEL_CONTINUE


# ---------------------------------------------------------------------------
# ax:link:start / attest
# ---------------------------------------------------------------------------

def test_link_start_flag_off(monkeypatch):
    World(monkeypatch)
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    context = ctx()
    text, _ = _card(tap("ax:link:start", context))
    assert vh.TEXT_ARCUS_NOT_ALLOWED in text
    assert awh.PENDING_KEY not in context.user_data


def test_link_start_mainnet_closed(monkeypatch):
    World(monkeypatch, network="mainnet")
    context = ctx()
    text, _ = _card(tap("ax:link:start", context))
    assert arcus_ui.TEXT_MAINNET_CLOSED in text
    assert awh.PENDING_KEY not in context.user_data


def test_link_start_blocked_egress(monkeypatch):
    World(monkeypatch)
    ls.note_egress_posture("testnet", compliance(perps=True, bypassed=False, country="US"))
    context = ctx()
    text, _ = _card(tap("ax:link:start", context))
    assert arcus_ui.TEXT_PR_GEO in text
    assert awh.PENDING_KEY not in context.user_data


def test_link_start_db_error(monkeypatch):
    world = World(monkeypatch)
    world.mode_error = RuntimeError("db down")
    context = ctx()
    text, _ = _card(tap("ax:link:start", context))
    assert arcus_ui.TEXT_DB_BUSY in text and awh.PENDING_KEY not in context.user_data


def test_link_start_renders_the_attestation(monkeypatch):
    World(monkeypatch)
    context = ctx()
    text, markup = _card(tap("ax:link:start", context))
    assert "United States, Canada or the United Kingdom" in text and "sanctioned" in text
    assert "https://arcus.xyz/legal/terms" in text
    assert "public" in text and "subaccount 0" in text and "cannot withdraw" in text
    assert callback_data(markup) == ["ax:link:attest", "ax:link:cancel"]
    pending = context.user_data[awh.PENDING_KEY]
    assert pending.step == "attest" and pending.renewal is False and pending.previous_address is None
    assert pending.generation == ls.current_generation(UID, "testnet")


def test_link_start_for_a_renewal_remembers_the_address(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(status="expired")
    context = ctx()
    tap("ax:link:start", context)
    pending = context.user_data[awh.PENDING_KEY]
    assert pending.renewal is True and pending.previous_address == ADDR


def test_attest_records_the_audit_and_asks_for_the_address(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    tap("ax:link:start", context)
    text, markup = _card(tap("ax:link:attest", context))
    pending = context.user_data[awh.PENDING_KEY]
    assert pending.step == "address" and pending.attested_at is not None
    assert [a[1] for a in world.db.audits] == ["arcus_attested"]
    assert world.db.audits[0][2] == f"testnet {ls.ARCUS_ATTESTATION_VERSION}"
    assert arcus_ui.TEXT_AD_ASK in text
    assert callback_data(markup) == ["ax:link:cancel"]


def test_attest_for_a_renewal_offers_same_address(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row()
    context = ctx()
    tap("ax:link:start", context)
    text, markup = _card(tap("ax:link:attest", context))
    assert f"<code>{ADDR}</code>" in text
    assert callback_data(markup) == ["ax:link:same", "ax:link:cancel"]


def test_attest_without_an_attest_step_shows_the_wallet(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "address")
    text, _ = _card(tap("ax:link:attest", context))
    assert "Arcus wallet" in text and world.db.audits == []


def test_attest_rechecks_the_flag(monkeypatch):
    World(monkeypatch)
    context = ctx()
    tap("ax:link:start", context)
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    text, _ = _card(tap("ax:link:attest", context))
    assert vh.TEXT_ARCUS_NOT_ALLOWED in text and awh.PENDING_KEY not in context.user_data


# ---------------------------------------------------------------------------
# the address step (text router) and the precheck
# ---------------------------------------------------------------------------

def test_router_ignores_text_without_a_pending_flow(monkeypatch):
    World(monkeypatch)
    msg, consumed = say(ADDR, ctx())
    assert consumed is False and msg.replies == []


def test_router_attest_first(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "attest")
    msg, consumed = say("hello", context)
    assert consumed and msg.replies[0][0] == arcus_ui.TEXT_K_ATTEST_FIRST


def test_router_invalid_address(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "address")
    msg, consumed = say("0x1234", context)
    assert consumed and msg.replies[0][0] == arcus_ui.TEXT_AD_INVALID
    assert msg.deleted == 0 and world.client.calls == []


def test_router_wait_paste_at_the_key_step(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "key")
    msg, consumed = say("hi there", context)
    assert consumed and msg.replies[0][0] == arcus_ui.TEXT_K_WAIT_PASTE


def _address_flow(monkeypatch, *, network="testnet", mainnet_flag=False):
    world = World(monkeypatch, network=network)
    if mainnet_flag:
        monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    context = ctx()
    tap("ax:link:start", context)
    tap("ax:link:attest", context)
    return world, context


def test_valid_address_runs_the_precheck_and_shows_the_instructions(monkeypatch):
    world, context = _address_flow(monkeypatch)
    world.client.api_keys = [ok([entry(PUB_B, name="nadobro-0000")])]
    gen_before = ls.current_generation(UID, "testnet")
    msg, consumed = say(ADDR.upper().replace("0X", "0x"), context)
    assert consumed
    [(checking, _)] = msg.replies
    assert checking == f"Checking <code>{ADDR}</code> on Arcus TESTNET…"
    pending = context.user_data[awh.PENDING_KEY]
    assert pending.step == "key" and pending.address == ADDR
    assert pending.generation == gen_before + 1
    assert re.fullmatch(r"nadobro-[0-9a-f]{4}", pending.key_name) and pending.key_name != "nadobro-0000"
    text, markup = _REPLY_EDITS[-1]
    assert f"<code>{pending.key_name}</code>" in text
    assert "Subaccount #" in text and "Days Valid to 180" in text and "Generate" in text
    assert ("🌐 Open Arcus API keys", None, "https://testnet.arcus.xyz/api-keys") in buttons(markup)
    assert [c[0] for c in world.client.calls] == ["compliance", "account", "apiKeys"]


# Edits of bot-sent messages (the "Checking…" reply the precheck task edits).
_REPLY_EDITS: list = []


@pytest.fixture(autouse=True)
def _capture_reply_edits(monkeypatch):
    _REPLY_EDITS.clear()
    real_edit = SentMessage.edit_text

    async def edit(self, text, **kw):
        _REPLY_EDITS.append((text, kw.get("reply_markup")))
        return await real_edit(self, text, **kw)

    monkeypatch.setattr(SentMessage, "edit_text", edit)


def test_mainnet_instructions_point_at_the_mainnet_app(monkeypatch):
    world, context = _address_flow(monkeypatch, network="mainnet", mainnet_flag=True)
    say(ADDR, context)
    text, markup = _REPLY_EDITS[-1]
    assert ("🌐 Open Arcus API keys", None, "https://app.arcus.xyz/api-keys") in buttons(markup)
    assert "MAINNET" in text


def test_no_activity_address_says_so(monkeypatch):
    world, context = _address_flow(monkeypatch)
    world.client.account = [H.NO_ACTIVITY]
    say(ADDR, context)
    text, _ = _REPLY_EDITS[-1]
    assert arcus_ui.TEXT_PR_NO_ACTIVITY in text


def test_busy_precheck_offers_check_again(monkeypatch):
    world, context = _address_flow(monkeypatch)
    world.client.compliance = [H.THROTTLED]
    say(ADDR, context)
    text, markup = _REPLY_EDITS[-1]
    assert text == ls.TEXT_BUSY
    assert callback_data(markup) == ["ax:link:check", "ax:link:cancel"]
    assert context.user_data[awh.PENDING_KEY].step == "address_check"


def test_db_unavailable_precheck(monkeypatch):
    world, context = _address_flow(monkeypatch)
    world.db.owner = RuntimeError("db down")
    say(ADDR, context)
    text, _ = _REPLY_EDITS[-1]
    assert text == arcus_ui.TEXT_DB_BUSY


def test_not_whitelisted_on_mainnet_ends_the_flow_with_both_lines(monkeypatch):
    world, context = _address_flow(monkeypatch, network="mainnet", mainnet_flag=True)
    world.client.account = [WHITELIST]
    say(ADDR, context)
    text, markup = _REPLY_EDITS[-1]
    assert ls.TEXT_PR_NOT_ELIGIBLE in text and arcus_ui.TEXT_PR_NOT_ELIGIBLE_MAINNET in text
    assert awh.PENDING_KEY not in context.user_data
    # R2-6: TEXT_PR_NOT_ELIGIBLE_MAINNET points at 🌐 Network, so the card carries it.
    assert callback_data(markup) == ["ax:link:start", "ax:home", "ax:mode"]
    assert buttons(markup)[2][0] == arcus_ui.LABEL_NETWORK
    assert ("arcus_link_refused" in [a[1] for a in world.db.audits])
    assert world.db.upserts == []


def test_not_whitelisted_on_testnet_has_no_mainnet_line(monkeypatch):
    world, context = _address_flow(monkeypatch)
    world.client.account = [WHITELIST]
    say(ADDR, context)
    text, _ = _REPLY_EDITS[-1]
    assert ls.TEXT_PR_NOT_ELIGIBLE in text and arcus_ui.TEXT_PR_NOT_ELIGIBLE_MAINNET not in text


@pytest.mark.parametrize("setup,needle", [
    (lambda w: setattr(w.client, "compliance", [ok(compliance("BLOCKED"))]), arcus_ui.TEXT_PR_BLOCKED),
    (lambda w: setattr(w.db, "owner", UID + 1), arcus_ui.TEXT_PR_TAKEN),
])
def test_terminal_precheck_refusals(monkeypatch, setup, needle):
    world, context = _address_flow(monkeypatch)
    setup(world)
    say(ADDR, context)
    text, _ = _REPLY_EDITS[-1]
    assert needle in text and awh.PENDING_KEY not in context.user_data


def test_same_address_uses_the_previous_address(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(address=ADDR2)
    context = ctx()
    tap("ax:link:start", context)
    tap("ax:link:attest", context)
    query = tap("ax:link:same", context)
    assert query.edits[-1][0] == f"Checking <code>{ADDR2}</code> on Arcus TESTNET…"
    pending = context.user_data[awh.PENDING_KEY]
    assert pending.address == ADDR2 and pending.step == "key"


def test_a_paste_during_the_address_check_is_verified_automatically(monkeypatch):
    world, context = _address_flow(monkeypatch)
    world.client.api_keys = [ok([]), ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    pending = replace(context.user_data[awh.PENDING_KEY], step="address_check", address=ADDR,
                      generation=ls.begin_generation(UID, "testnet"))
    context.user_data[awh.PENDING_KEY] = pending
    _stash(world, pending)  # pasted while the check was running
    # [Check again] restarts the precheck WITHOUT a new generation, so the stash survives
    # and the verification starts as soon as the address passes.
    query = tap("ax:link:check", context)
    assert len(world.db.upserts) == 1 and world.db.upserts[0]["sealed"].api_public_key == RFC_PUB
    assert awh.PENDING_KEY not in context.user_data
    assert "<b>Linked</b>" in query.message.text
    # the "can use Arcus · checking your key" line comes from the verify task itself,
    # strictly before its result (it can never overwrite the result)
    edits = [text for text, _kw in query.message.edits]
    checking = next(i for i, t in enumerate(edits) if arcus_ui.TEXT_K_CHECKING in t)
    assert f"<code>{ADDR}</code> can use Arcus TESTNET" in edits[checking]
    assert checking < len(edits) - 1 and "<b>Linked</b>" in edits[-1]


def test_a_second_check_key_during_a_diagnosis_starts_nothing_new(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS + 100 * DAY_MS)
    gate = {}
    calls = []

    async def slow(uid, network):
        calls.append(network)
        gate["started"].set()
        await gate["release"].wait()
        return ls.KeyDiagnosis(ls.KeyVerdict.KEY_OK, network, 1.0, NOW_MS + 100 * DAY_MS, True, "nadobro-ab12")

    monkeypatch.setattr(ls, "diagnose_key", slow)

    async def body():
        gate["started"], gate["release"] = asyncio.Event(), asyncio.Event()
        first = FakeQuery("ax:link:check")
        await awh.handle(first, "ax:link:check", UID, ctx())
        await asyncio.wait_for(gate["started"].wait(), 5)
        second = FakeQuery("ax:link:check")
        await awh.handle(second, "ax:link:check", UID, ctx())
        gate["release"].set()
        await drain()
        return first, second

    first, second = asyncio.run(body())
    assert calls == ["testnet"]
    assert callback_data(second.edits[0][1]["reply_markup"]) == ["ax:wallet"]
    assert "Key active on Arcus" in second.message.text  # re-pointed to the newer card
    assert first.message.text == arcus_ui.TEXT_K_CHECKING_STORED


# ---------------------------------------------------------------------------
# ax:link:check (03 §9.7)
# ---------------------------------------------------------------------------

def test_check_while_a_task_runs_starts_nothing_new(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "address_check")
    gate = {}
    starts = []

    async def slow(network, address, *, user_id):
        starts.append(address)
        gate["started"].set()
        await gate["release"].wait()
        return ls.AddressPrecheck(AddressCheck.BUSY, frozenset(), None, 0.0, None)

    monkeypatch.setattr(ls, "precheck_address", slow)

    async def body():
        gate["started"], gate["release"] = asyncio.Event(), asyncio.Event()
        first = FakeQuery("ax:link:check")
        await awh.handle(first, "ax:link:check", UID, context)
        await asyncio.wait_for(gate["started"].wait(), 5)
        second = FakeQuery("ax:link:check")
        await awh.handle(second, "ax:link:check", UID, context)
        assert second.edits[-1][0].endswith(arcus_ui.TEXT_K_STILL_CHECKING)
        again = FakeQuery("ax:link:check", card=second.message)
        await awh.handle(again, "ax:link:check", UID, context)
        assert again.edits[-1][0].count(arcus_ui.TEXT_K_STILL_CHECKING) == 1
        gate["release"].set()
        await drain()
        return first, again

    first, again = asyncio.run(body())
    assert starts == [ADDR]  # no second precheck
    # the running task was re-pointed at the tapped card: the result appears THERE
    assert again.message.text == ls.TEXT_BUSY
    assert first.message.text != ls.TEXT_BUSY


def test_check_at_address_check_restarts_the_precheck_keeping_the_generation(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    pending = _pending(world, "address_check")
    context.user_data[awh.PENDING_KEY] = pending
    query = tap("ax:link:check", context)
    assert ls.current_generation(UID, "testnet") == pending.generation
    assert context.user_data[awh.PENDING_KEY].step == "key"
    assert "Create an API key" in query.message.text


def test_check_with_a_stash_verifies(monkeypatch):
    world = World(monkeypatch)
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    context = ctx()
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending
    _stash(world, pending)
    query = tap("ax:link:check", context)
    assert query.edits[0][0] == arcus_ui.TEXT_K_CHECKING
    assert "<b>Linked</b>" in query.message.text
    assert len(world.db.upserts) == 1


def test_check_verifying_without_a_stash_says_timed_out(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "verifying")
    text, markup = _card(tap("ax:link:check", context))
    assert text == arcus_ui.TEXT_R_PENDING_EXPIRED
    assert context.user_data[awh.PENDING_KEY].step == "key"
    # R2-6: "…or tap Link to start over" — the card carries [🔗 Link Arcus account].
    assert callback_data(markup) == ["ax:link:start", "ax:link:cancel"]
    assert buttons(markup)[0][0] == arcus_ui.LABEL_LINK


def test_check_at_the_key_step_without_a_paste_shows_the_instructions_again(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "key")
    text, _ = _card(tap("ax:link:check", context))
    assert "Create an API key" in text and "<code>nadobro-ab12</code>" in text


@pytest.mark.parametrize("step,needle", [("attest", "Before you link"), ("address", "Send the wallet address")])
def test_check_at_an_early_step_re_renders_it(monkeypatch, step, needle):
    world = World(monkeypatch)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, step)
    text, _ = _card(tap("ax:link:check", context))
    assert needle in text


def test_check_key_without_a_flow_diagnoses_the_stored_key(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS + 100 * DAY_MS)
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    query = tap("ax:link:check", ctx())
    assert query.edits[0][0] == arcus_ui.TEXT_K_CHECKING_STORED
    assert "Key active on Arcus" in query.message.text and "Arcus wallet" in query.message.text
    assert world.db.touches  # KEY_OK -> touch_verified


def test_check_key_revoked(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS + 100 * DAY_MS)
    world.client.api_keys = [ok([])]
    query = tap("ax:link:check", ctx())
    assert ls.TEXT_D_REVOKED in query.message.text
    assert world.db.marks[0][2] == "invalid"


def test_check_without_a_credential_shows_the_wallet(monkeypatch):
    World(monkeypatch)
    text, _ = _card(tap("ax:link:check", ctx()))
    assert arcus_ui.TEXT_W_NOT_LINKED in text


def test_cancel_ends_everything(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending
    _stash(world, pending)
    gen = ls.current_generation(UID, "testnet")
    cancelled = []

    class Task:
        def done(self):
            return bool(cancelled)

        def cancel(self):
            cancelled.append(True)

    awh._TASKS[(UID, "testnet", "verify")] = Task()
    text, _ = _card(tap("ax:link:cancel", context))
    assert arcus_ui.TEXT_R_CANCELLED in text
    assert awh.PENDING_KEY not in context.user_data
    assert ls.current_generation(UID, "testnet") == gen + 1
    assert not ls.has_stash(UID, "testnet", pending.generation)
    assert cancelled == [True]


# ---------------------------------------------------------------------------
# results of a verification
# ---------------------------------------------------------------------------

def _verify_with(monkeypatch, outcome, *, venue="arcus", step="key"):
    world = World(monkeypatch, venue=venue)
    context = ctx()
    pending = _pending(world, step)
    context.user_data[awh.PENDING_KEY] = pending

    async def fake(*, user_id, pending, pasted_secret=None):
        return outcome(pending) if callable(outcome) else outcome

    monkeypatch.setattr(ls, "verify_and_store", fake)
    card = SentMessage([], H.UID, "ack")
    target = awh._Target(card, H.UID, None)

    async def body():
        await awh._verify_and_report(context, UID, pending, target)

    asyncio.run(body())
    return world, context, card


def _outcome(result, **kw):
    return LinkOutcome(result=result, network="testnet", address=ADDR, **kw)


@pytest.mark.parametrize("result,kind,step", [
    (LinkResult.BUSY, "retry", "verifying"),
    (LinkResult.KEY_NOT_FOUND, "retry", "verifying"),
    (LinkResult.STORE_FAILED, "retry", "verifying"),
    (LinkResult.INVALID_KEY, "paste", "key"),
    (LinkResult.KEY_INACTIVE, "paste", "key"),
    (LinkResult.KEY_EXPIRES_TOO_SOON, "paste", "key"),
    (LinkResult.KEY_WRONG_SUBACCOUNT, "paste", "key"),
    (LinkResult.KEY_HAS_WITHDRAW, "paste", "key"),
    (LinkResult.WALLET_KEY_REFUSED, "paste", "key"),
    (LinkResult.PENDING_EXPIRED, "paste_expired", "key"),
    (LinkResult.NOT_WHITELISTED, "terminal", None),
    (LinkResult.BLOCKED, "terminal", None),
    (LinkResult.GEO_RESTRICTED, "terminal", None),
    (LinkResult.ALREADY_LINKED_ELSEWHERE, "terminal", None),
    (LinkResult.NOT_ALLOWED, "terminal", None),
    (LinkResult.AUTOMATION_RUNNING, "terminal", None),
    (LinkResult.NO_PENDING, "terminal", None),
])
def test_result_moves_the_flow_and_picks_the_buttons(monkeypatch, result, kind, step):
    _world, context, card = _verify_with(monkeypatch, _outcome(result))
    text, kw = card.edits[-1]
    markup = kw["reply_markup"]
    expected = {
        "retry": ["ax:link:check", "ax:link:cancel"],
        "paste": ["ax:link:cancel"],
        "paste_expired": ["ax:link:start", "ax:link:cancel"],  # R2-6: [🔗 Link Arcus account][Cancel]
        "terminal": ["ax:link:start", "ax:home"],
    }[kind]
    assert callback_data(markup) == expected
    if step is None:
        assert awh.PENDING_KEY not in context.user_data
    else:
        assert context.user_data[awh.PENDING_KEY].step == step
    assert text and "{" not in text


def test_every_link_result_has_a_kind():
    assert set(awh._RESULT_KIND) == set(LinkResult)


def test_superseded_is_silent(monkeypatch):
    _world, context, card = _verify_with(monkeypatch, _outcome(LinkResult.SUPERSEDED))
    assert card.edits == []
    assert context.user_data[awh.PENDING_KEY].step == "key"


def test_a_generation_mismatch_is_silent(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending

    async def fake(*, user_id, pending, pasted_secret=None):
        # the user started over while the verification ran
        newer = replace(pending, generation=ls.begin_generation(UID, "testnet"))
        context.user_data[awh.PENDING_KEY] = newer
        return _outcome(LinkResult.BUSY)

    monkeypatch.setattr(ls, "verify_and_store", fake)
    card = SentMessage([], UID, "ack")
    asyncio.run(awh._verify_and_report(context, UID, pending, awh._Target(card, UID, None)))
    assert card.edits == []
    assert context.user_data[awh.PENDING_KEY].step == "key"  # the newer flow is untouched


def test_linked_result_ends_the_flow(monkeypatch):
    linked_row = H.row(until=NOW_MS + 179 * DAY_MS)
    _world, context, card = _verify_with(
        monkeypatch, _outcome(LinkResult.LINKED, row=linked_row, valid_until_ms=linked_row.valid_until_ms)
    )
    text, kw = card.edits[-1]
    assert text.startswith("✅ <b>Linked</b> · <code>" + ADDR)
    assert "subaccount 0" in text and arcus_ui.format_utc_ms(NOW_MS + 179 * DAY_MS) in text
    assert callback_data(kw["reply_markup"]) == ["ax:wallet", "ax:home"]
    assert awh.PENDING_KEY not in context.user_data


def test_linked_no_activity_and_short_validity(monkeypatch):
    linked_row = H.row(until=NOW_MS + 5 * DAY_MS + 1000)
    _world, _context, card = _verify_with(
        monkeypatch, _outcome(LinkResult.LINKED_NO_ACTIVITY, row=linked_row, has_activity=False)
    )
    text, _ = card.edits[-1]
    assert arcus_ui.TEXT_R_NO_ACTIVITY in text
    assert "This key expires in 5 days" in text


def test_a_nado_view_result_offers_only_the_venue_button(monkeypatch):
    _world, _context, card = _verify_with(monkeypatch, _outcome(LinkResult.BUSY), venue="nado")
    assert callback_data(card.edits[-1][1]["reply_markup"]) == ["venue:view"]
    _world, _context, card = _verify_with(monkeypatch, _outcome(LinkResult.BUSY), venue=RuntimeError("db"))
    assert callback_data(card.edits[-1][1]["reply_markup"]) == ["venue:view"]


def test_not_allowed_on_mainnet_says_mainnet_closed(monkeypatch):
    World(monkeypatch, network="mainnet")
    lines = awh._outcome_lines(UID, LinkOutcome(result=LinkResult.NOT_ALLOWED, network="mainnet"))
    assert lines == [arcus_ui.TEXT_MAINNET_CLOSED]
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    lines = awh._outcome_lines(UID, LinkOutcome(result=LinkResult.NOT_ALLOWED, network="mainnet"))
    assert lines == [vh.TEXT_ARCUS_NOT_ALLOWED]


# ---- renewal message (03 §9.8 / §19.4 handler part) ----

def _renewal(prev_name, new_name, *, same_pub=False):
    prev = H.row(pub=RFC_PUB, name=prev_name)
    new = H.row(pub=RFC_PUB if same_pub else PUB_B, name=new_name, until=NOW_MS + 179 * DAY_MS)
    out = LinkOutcome(result=LinkResult.LINKED, network="testnet", address=ADDR, row=new, previous=prev, renewed=True)
    return "\n".join(awh._outcome_lines(UID, out))


def test_renewal_with_a_new_name_mentions_the_old_key(monkeypatch):
    World(monkeypatch)
    text = _renewal("nadobro-aaaa", "nadobro-bbbb")
    assert "<b>Key renewed</b>" in text and "<code>nadobro-aaaa</code> still works" in text


@pytest.mark.parametrize("new_name", ["nadobro-aaaa", "NADOBRO-AAAA"])
def test_renewal_reusing_the_name_never_claims_the_old_key_works(monkeypatch, new_name):
    # docs changelog: re-creating a key with the same apiWalletName revokes the old one.
    World(monkeypatch)
    text = _renewal("nadobro-aaaa", new_name)
    assert "<b>Key renewed</b>" in text and "still works" not in text


def test_renewal_with_the_same_pubkey_has_no_old_key_line(monkeypatch):
    World(monkeypatch)
    assert "still works" not in _renewal("nadobro-aaaa", "nadobro-bbbb", same_pub=True)


# ---- background exceptions never escape and never log the text (03 §9.3, §19.4) ----

def test_a_raising_verify_ends_the_task_normally_and_logs_the_type_only(monkeypatch, caplog):
    world = World(monkeypatch)
    context = ctx()
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending

    async def boom(*, user_id, pending, pasted_secret=None):
        raise RuntimeError(f"leak {RFC_SEED}")

    monkeypatch.setattr(ls, "verify_and_store", boom)
    card = SentMessage([], UID, "ack")
    reaped = []

    async def body():
        awh._start_task(UID, "testnet", "verify", awh._verify_and_report(context, UID, pending, awh._Target(card, UID, None)))
        task = awh._TASKS[(UID, "testnet", "verify")]
        await asyncio.gather(task, return_exceptions=True)
        reaped.append(task.exception() if not task.cancelled() else "cancelled")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(body())
    assert reaped == [None]  # nothing reached fire_and_forget's reaper
    assert "RuntimeError" in caplog.text and RFC_SEED not in caplog.text
    assert card.edits[-1][0] == ls.TEXT_BUSY  # the user still gets [Check again]


def test_a_raising_precheck_is_busy(monkeypatch, caplog):
    world, context = _address_flow(monkeypatch)

    async def boom(network, address, *, user_id):
        raise RuntimeError(f"x {RFC_SEED}")

    monkeypatch.setattr(ls, "precheck_address", boom)
    msg = FakeMessage(ADDR, log=context.bot.log)

    async def body():
        assert await awh.arcus_text_router(update_for(msg), context, ADDR)
        await drain()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(body())
    assert _REPLY_EDITS[-1][0] == ls.TEXT_BUSY
    assert RFC_SEED not in caplog.text


# ---------------------------------------------------------------------------
# a background result never clobbers a newer screen (03 §9.3, V-16)
# ---------------------------------------------------------------------------

def test_a_tap_elsewhere_makes_the_result_a_new_message(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(address=ADDR2)
    context = ctx()
    tap("ax:link:start", context)
    tap("ax:link:attest", context)
    gate = {}
    real = ls.precheck_address

    async def slow(network, address, *, user_id):
        gate["started"].set()
        await gate["release"].wait()
        return await real(network, address, user_id=user_id)

    monkeypatch.setattr(ls, "precheck_address", slow)

    async def body():
        gate["started"], gate["release"] = asyncio.Event(), asyncio.Event()
        same = FakeQuery("ax:link:same")
        await awh.handle(same, "ax:link:same", UID, context)
        await asyncio.wait_for(gate["started"].wait(), 5)
        callbacks.bump_interaction_seq(H.UID)  # the user taps ax:home (render_arcus_target bumps)
        gate["release"].set()
        await drain()
        return same

    same = asyncio.run(body())
    assert same.message.text.startswith("Checking")  # the tapped card was NOT edited
    assert any("Create an API key" in t for t in context.bot.texts())  # a new message instead


def test_handle_bumps_the_sequence_exactly_once_per_edit_and_tasks_never(monkeypatch):
    world = World(monkeypatch)
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    bumps = []
    real_bump = callbacks.bump_interaction_seq
    monkeypatch.setattr(callbacks, "bump_interaction_seq", lambda chat: bumps.append(chat) or real_bump(chat))
    context = ctx()
    for data in ("ax:wallet", "ax:link:start", "ax:link:attest", "ax:mode", "ax:unlink", "ax:link:cancel"):
        bumps.clear()
        query = tap(data, context)
        assert len(query.edits) == 1 and bumps == [H.UID], data
    # a verification started by a tap: one bump for the tap, none for the background result
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending
    _stash(world, pending)
    bumps.clear()
    query = tap("ax:link:check", context)
    assert bumps == [H.UID]
    assert "<b>Linked</b>" in query.message.text


# ---------------------------------------------------------------------------
# unlink
# ---------------------------------------------------------------------------

def test_unlink_card_offers_confirm_for_the_current_network_and_the_nado_revoke(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(name="nadobro-ab12")
    text, markup = _card(tap("ax:unlink", ctx()))
    assert f"<code>{ADDR}</code>" in text and "<code>nadobro-ab12</code>" in text
    assert arcus_ui.TEXT_U_RUNNING_NOTE in text and arcus_ui.TEXT_U_NADO_NOTE in text
    assert callback_data(markup) == ["ax:unlink:confirm:testnet", "wallet:revoke_steps", "ax:wallet"]


def test_unlink_card_without_a_key(monkeypatch):
    World(monkeypatch)
    text, markup = _card(tap("ax:unlink", ctx()))
    assert "No Arcus key is linked on TESTNET." in text
    assert callback_data(markup) == ["wallet:revoke_steps", "ax:wallet"]


@pytest.mark.parametrize("fail", ["mode", "credential"])
def test_unlink_card_db_error_never_says_no_key(monkeypatch, fail):
    world = World(monkeypatch)
    if fail == "mode":
        world.mode_error = RuntimeError("db down")
    else:
        world.db.credential = RuntimeError("db down")
    text, markup = _card(tap("ax:unlink", ctx()))
    assert arcus_ui.TEXT_W_UNREADABLE in text and "No Arcus key" not in text
    assert not any(d.startswith("ax:unlink:confirm") for d in callback_data(markup))


def _unlink_spy(monkeypatch, result):
    calls = []

    async def fake(uid, network):
        calls.append((uid, network))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(ls, "unlink", fake)
    return calls


def test_unlink_confirm_unlinked(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(name="nadobro-ab12")
    calls = _unlink_spy(monkeypatch, "unlinked")
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "address")
    text, _ = _card(tap("ax:unlink:confirm:testnet", context))
    assert calls == [(UID, "testnet")]
    assert "✅ Unlinked. Revoke <code>nadobro-ab12</code>" in text
    assert awh.PENDING_KEY not in context.user_data


def test_unlink_confirm_refused_and_none_and_db_error(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row()
    _unlink_spy(monkeypatch, "refused_running")
    text, _ = _card(tap("ax:unlink:confirm:testnet", ctx()))
    assert arcus_ui.TEXT_U_REFUSED in text and "Unlink Arcus" in text
    _unlink_spy(monkeypatch, "none")
    text, _ = _card(tap("ax:unlink:confirm:testnet", ctx()))
    assert "No Arcus key is linked on TESTNET." in text and "Arcus wallet" in text
    _unlink_spy(monkeypatch, RuntimeError("db down"))
    text, _ = _card(tap("ax:unlink:confirm:testnet", ctx()))
    assert arcus_ui.TEXT_DB_BUSY in text


def test_a_stale_unlink_card_unlinks_nothing(monkeypatch):
    World(monkeypatch)  # the mode is testnet now
    calls = _unlink_spy(monkeypatch, "unlinked")
    text, markup = _card(tap("ax:unlink:confirm:mainnet", ctx()))
    assert calls == []
    assert "Unlink Arcus</b> · TESTNET" in text


@pytest.mark.parametrize("data", ["ax:unlink:confirm:bogus", "ax:unlink:confirm:Testnet", "ax:unlink:confirm:arcus_testnet"])
def test_a_malformed_unlink_confirm_does_nothing(monkeypatch, data):
    World(monkeypatch)
    calls = _unlink_spy(monkeypatch, "unlinked")
    query = tap(data, ctx())
    assert calls == [] and query.edits == []


# ---------------------------------------------------------------------------
# the network card
# ---------------------------------------------------------------------------

def test_mode_card_labels_and_statuses(monkeypatch):
    world = World(monkeypatch)
    world.credentials = {"testnet": H.row(), "mainnet": H.row(network="mainnet", status="expired")}
    text, markup = _card(tap("ax:mode", ctx()))
    assert "Viewing: <b>TESTNET</b>" in text
    assert "TESTNET: linked · <code>0xabab…abab</code>" in text and "MAINNET: key expired" in text
    assert [label for label, _d, _u in buttons(markup)] == ["🧪 Arcus testnet ✅", "🟢 Arcus mainnet", "🏠 Home"]
    assert callback_data(markup) == ["ax:mode:testnet", "ax:mode:mainnet", "ax:home"]


def test_mode_card_credentials_unreadable_says_couldnt_check(monkeypatch):
    world = World(monkeypatch)
    world.credentials = RuntimeError("db down")
    text, _ = _card(tap("ax:mode", ctx()))
    assert text.count("couldn't check") == 2


def test_mode_card_mode_unreadable_offers_no_switch(monkeypatch):
    world = World(monkeypatch)
    world.mode_error = RuntimeError("db down")
    text, markup = _card(tap("ax:mode", ctx()))
    assert arcus_ui.TEXT_DB_BUSY in text
    assert callback_data(markup) == ["ax:mode", "ax:home"]


def test_switch_to_mainnet_without_the_flag_is_refused(monkeypatch):
    world = World(monkeypatch)
    text, _ = _card(tap("ax:mode:mainnet", ctx()))
    assert arcus_ui.TEXT_MAINNET_CLOSED in text and world.set_mode_calls == []


def test_switch_with_automation_running_is_refused(monkeypatch):
    world = World(monkeypatch)
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")

    async def running(uid):
        return True

    monkeypatch.setattr(ls, "automation_active", running)
    query = FakeQuery("ax:mode:mainnet")
    asyncio.run(awh.handle(query, "ax:mode:mainnet", UID, ctx()))
    text, _ = _card(query)
    assert arcus_ui.TEXT_M_REFUSED_RUNNING in text and world.set_mode_calls == []


def test_switch_clears_a_pending_flow(monkeypatch):
    world = World(monkeypatch)
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "address")
    gen = ls.current_generation(UID, "testnet")
    text, markup = _card(tap("ax:mode:mainnet", context))
    assert world.set_mode_calls == [(UID, "mainnet")]
    assert "Switched to Arcus MAINNET." in text
    assert awh.PENDING_KEY not in context.user_data
    assert ls.current_generation(UID, "testnet") == gen + 1
    assert [label for label, _d, _u in buttons(markup)][:2] == ["🧪 Arcus testnet", "🟢 Arcus mainnet ✅"]


def test_switch_failure_and_not_allowed(monkeypatch):
    world = World(monkeypatch, network="mainnet")
    world.set_mode_result = RuntimeError("db down")
    text, _ = _card(tap("ax:mode:testnet", ctx()))  # testnet is never flag-gated
    assert vh.TEXT_SWITCH_FAILED in text
    world.network = "testnet"
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    world.set_mode_result = "not_allowed"
    text, _ = _card(tap("ax:mode:mainnet", ctx()))
    assert arcus_ui.TEXT_MAINNET_CLOSED in text


def test_switch_to_testnet_is_never_flag_gated(monkeypatch):
    world = World(monkeypatch, network="mainnet")
    monkeypatch.delenv("ARCUS_ENABLED", raising=False)
    text, _ = _card(tap("ax:mode:testnet", ctx()))
    assert world.set_mode_calls == [(UID, "testnet")] and "Switched to Arcus TESTNET." in text


@pytest.mark.parametrize("data", ["ax:mode:Mainnet", "ax:mode:arcus_mainnet", "ax:mode:"])
def test_a_malformed_mode_switch_is_ignored(monkeypatch, data):
    world = World(monkeypatch)
    query = tap(data, ctx())
    assert query.edits == [] and world.set_mode_calls == []


def test_switch_to_the_current_network_changes_nothing(monkeypatch):
    world = World(monkeypatch)
    text, _ = _card(tap("ax:mode:testnet", ctx()))
    assert world.set_mode_calls == [] and "Viewing: <b>TESTNET</b>" in text


# ---------------------------------------------------------------------------
# hygiene
# ---------------------------------------------------------------------------

def test_render_functions_never_call_the_venue(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row()
    calls = []
    monkeypatch.setattr(ls, "_services", lambda net: calls.append(net) or (_ for _ in ()).throw(AssertionError("venue")))
    from src.nadobro.handlers import arcus_portfolio_handler as apf

    monkeypatch.setattr(vh, "nado_automation_snapshot", lambda uid: ("testnet", [], False))

    async def body():
        await awh.render_wallet(UID, context=ctx())
        await awh.render_mode(UID)
        await awh.render_unlink(UID)
        await apf.render_home(UID, context=ctx())

    asyncio.run(body())
    assert calls == []


def test_every_callback_and_label_is_accounted_for(monkeypatch):
    world = World(monkeypatch)
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    markups = []
    for cred in (None, H.row(), H.row(status="expired"), H.row(status="invalid")):
        world.db.credential = cred
        markups += [_card(tap(d, ctx()))[1] for d in ("ax:wallet", "ax:unlink", "ax:mode")]
    context = ctx()
    markups.append(_card(tap("ax:link:start", context))[1])
    markups.append(_card(tap("ax:link:attest", context))[1])  # with [Same address]
    markups.append(_card(tap("ax:wallet", context))[1])  # [Continue linking]
    for network in ("testnet", "mainnet"):
        pending = replace(_pending(world, "key"), network=network)
        markups += [awh._instructions_card(pending)[1], awh._checking_address_card(pending)[1]]
        markups += [awh._attestation_card(network)[1], awh._address_card(replace(pending, previous_address=ADDR))[1]]
    for kind in ("linked", "retry", "paste", "terminal"):
        markups += [awh._outcome_markup(kind, True), awh._outcome_markup(kind, False)]
    markups += [awh._retry_markup(), awh._cancel_only(), awh._wallet_only(), awh._terminal_markup()]
    markups += [awh._terminal_markup(mainnet_refusal=True), awh._pending_expired_markup(), awh._no_pending_markup()]
    allowed_foreign = {"venue:view", "wallet:revoke_steps"}
    labels_ok = set(arcus_ui.ARCUS_P3B_LABEL_KEYS) | _REUSED_LABELS
    seen = set()
    for markup in markups:
        for label, data, url in buttons(markup):
            base = label[:-2] if label.endswith(" ✅") else label
            assert base in labels_ok, label
            seen.add(base)
            if data is not None:
                assert len(data.encode()) <= 64, data
                assert data.startswith("ax:") or data in allowed_foreign, data
            else:
                assert url and url.startswith("https://"), url
    # every new label is actually used somewhere
    assert set(arcus_ui._HANDLER_LABEL_KEYS) <= seen | {arcus_ui.LABEL_RENEW}
    assert arcus_ui.LABEL_RENEW in seen


def test_callback_constants_have_no_network_suffix_names():
    names = [n for n in dir(awh) if n.startswith("CB_")]
    assert names and not any(n.endswith(("TESTNET", "MAINNET")) for n in names)


def test_no_card_contains_a_secret_or_pubkey(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(pub=RFC_PUB)
    context = ctx()
    for data in ("ax:wallet", "ax:unlink", "ax:mode"):
        text, _ = _card(tap(data, context))
        assert RFC_PUB not in text and not _HEX64.search(text)


# ---------------------------------------------------------------------------
# R2-5: [✅ Check key] always checks the STORED key, also while a flow is pending
# ---------------------------------------------------------------------------

def test_wallet_with_a_pending_renewal_offers_continue_and_a_distinct_check_key(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS + 100 * DAY_MS)
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "key", previous=ADDR)
    text, markup = _card(tap("ax:wallet", context))
    assert arcus_ui.TEXT_W_LINKING in text
    pairs = [(label, data) for label, data, _url in buttons(markup)]
    assert (arcus_ui.LABEL_CONTINUE, "ax:link:check") in pairs
    assert (arcus_ui.LABEL_CHECK_KEY, "ax:link:key") in pairs
    assert len({data for _label, data in pairs}) == len(pairs)  # no two buttons share a callback


def test_check_key_during_a_pending_flow_diagnoses_the_stored_key(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS + 100 * DAY_MS)
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    context = ctx()
    pending = _pending(world, "key", previous=ADDR)
    context.user_data[awh.PENDING_KEY] = pending
    query = tap("ax:link:key", context)
    assert query.edits[0][0] == arcus_ui.TEXT_K_CHECKING_STORED  # not the instructions card
    assert "Key active on Arcus" in query.message.text
    assert world.db.touches  # KEY_OK -> touch_verified
    assert context.user_data[awh.PENDING_KEY] == pending  # the flow is left untouched


def test_check_key_without_a_flow_diagnoses_too(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row(until=NOW_MS + 100 * DAY_MS)
    world.client.api_keys = [ok([])]
    query = tap("ax:link:key", ctx())
    assert ls.TEXT_D_REVOKED in query.message.text


# ---------------------------------------------------------------------------
# R2-6: a text that names a button is shown with that button
# ---------------------------------------------------------------------------

def test_pending_expired_result_offers_the_link_button(monkeypatch):
    _world, context, card = _verify_with(monkeypatch, _outcome(LinkResult.PENDING_EXPIRED))
    text, kw = card.edits[-1]
    assert text == arcus_ui.TEXT_R_PENDING_EXPIRED  # "…or tap Link to start over"
    assert [(label, data) for label, data, _u in buttons(kw["reply_markup"])] == [
        (arcus_ui.LABEL_LINK, "ax:link:start"), ("❌ Cancel", "ax:link:cancel")]


def test_no_pending_result_offers_link_arcus_account(monkeypatch):
    _world, context, card = _verify_with(monkeypatch, _outcome(LinkResult.NO_PENDING))
    text, kw = card.edits[-1]
    assert text == arcus_ui.TEXT_R_NO_PENDING  # "Tap Link Arcus account to start."
    assert buttons(kw["reply_markup"])[0][:2] == (arcus_ui.LABEL_LINK, "ax:link:start")


def test_a_mainnet_not_whitelisted_result_offers_the_network_button(monkeypatch):
    world = World(monkeypatch, network="mainnet")
    monkeypatch.setenv("ARCUS_MAINNET_ENABLED", "1")
    context = ctx()
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending

    async def fake(*, user_id, pending, pasted_secret=None):
        return LinkOutcome(result=LinkResult.NOT_WHITELISTED, network="mainnet", address=ADDR)

    monkeypatch.setattr(ls, "verify_and_store", fake)
    card = SentMessage([], UID, "ack")
    asyncio.run(awh._verify_and_report(context, UID, pending, awh._Target(card, UID, None)))
    text, kw = card.edits[-1]
    assert arcus_ui.TEXT_PR_NOT_ELIGIBLE_MAINNET in text  # "…(🌐 Network)."
    assert ("🌐 Network", "ax:mode") in [(label, data) for label, data, _u in buttons(kw["reply_markup"])]
    # testnet: no Network line and no Network button
    _world, _context, card = _verify_with(monkeypatch, _outcome(LinkResult.NOT_WHITELISTED))
    assert callback_data(card.edits[-1][1]["reply_markup"]) == ["ax:link:start", "ax:home"]


# ---------------------------------------------------------------------------
# R2-2: a flow that times out while a check runs is reported, never silent
# ---------------------------------------------------------------------------

def _timed_out_verify(monkeypatch, outcome_for):
    world = World(monkeypatch)
    context = ctx()
    pending = replace(_pending(world, "verifying"), expires_mono=world.env.mono.now + 20.0)
    context.user_data[awh.PENDING_KEY] = pending

    async def slow(*, user_id, pending, pasted_secret=None):
        world.env.mono.now += 60.0  # the 60 s apiKeys poll outlives the TTL
        return outcome_for(pending)

    monkeypatch.setattr(ls, "verify_and_store", slow)
    card = SentMessage([], UID, "ack")
    asyncio.run(awh._verify_and_report(context, UID, pending, awh._Target(card, UID, None)))
    return world, context, card


def test_a_key_stored_after_the_flow_timed_out_is_still_announced(monkeypatch):
    linked_row = H.row(until=NOW_MS + 179 * DAY_MS)
    _world, context, card = _timed_out_verify(
        monkeypatch, lambda p: _outcome(LinkResult.LINKED, row=linked_row, valid_until_ms=linked_row.valid_until_ms)
    )
    text, kw = card.edits[-1]
    assert text.startswith("✅ <b>Linked</b>")
    assert callback_data(kw["reply_markup"]) == ["ax:wallet", "ax:home"]
    assert awh.PENDING_KEY not in context.user_data


@pytest.mark.parametrize("result", [LinkResult.KEY_NOT_FOUND, LinkResult.BUSY, LinkResult.SUPERSEDED,
                                    LinkResult.KEY_INACTIVE, LinkResult.PENDING_EXPIRED])
def test_any_other_result_after_a_timeout_says_it_timed_out(monkeypatch, result):
    _world, context, card = _timed_out_verify(monkeypatch, lambda p: _outcome(result))
    text, kw = card.edits[-1]
    assert text == arcus_ui.TEXT_R_FLOW_EXPIRED
    assert callback_data(kw["reply_markup"]) == ["ax:link:start", "ax:home"]  # "Tap Start over"
    assert awh.PENDING_KEY not in context.user_data


def test_a_timed_out_precheck_is_reported(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    pending = replace(_pending(world, "address_check"), expires_mono=world.env.mono.now + 5.0)
    context.user_data[awh.PENDING_KEY] = pending

    async def slow(network, address, *, user_id):
        world.env.mono.now += 30.0
        return ls.AddressPrecheck(AddressCheck.ELIGIBLE, frozenset(), True, world.env.mono.now, None)

    monkeypatch.setattr(ls, "precheck_address", slow)
    card = SentMessage([], UID, "Checking…")
    asyncio.run(awh._precheck_and_report(context, UID, pending, awh._Target(card, UID, None)))
    assert card.edits[-1][0] == arcus_ui.TEXT_R_FLOW_EXPIRED
    assert awh.PENDING_KEY not in context.user_data


def test_check_again_refreshes_the_ttl_before_a_long_check(monkeypatch, caplog):
    world = World(monkeypatch)
    world.client.api_keys = [ok([]), ok([]), ok([]), ok([]), ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    context = ctx()
    pending = replace(_pending(world, "verifying"), expires_mono=world.env.mono.now + 10.0)
    context.user_data[awh.PENDING_KEY] = pending
    _stash(world, replace(pending, expires_mono=world.env.mono.now + 1800))
    with caplog.at_level(logging.INFO):
        query = tap("ax:link:check", context)
    assert "<b>Linked</b>" in query.message.text
    assert len(world.db.upserts) == 1
    assert "timed out mid-check" not in caplog.text  # the 25 s poll did not outlive the flow


def test_check_again_at_the_address_check_refreshes_the_ttl(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    pending = replace(_pending(world, "address_check"), expires_mono=world.env.mono.now + 3.0)
    context.user_data[awh.PENDING_KEY] = pending

    async def slow(network, address, *, user_id):
        world.env.mono.now += 20.0
        return ls.AddressPrecheck(AddressCheck.BUSY, frozenset(), None, world.env.mono.now, None)

    monkeypatch.setattr(ls, "precheck_address", slow)
    query = FakeQuery("ax:link:check")

    async def body():
        await awh.handle(query, "ax:link:check", UID, context)
        await drain()

    asyncio.run(body())
    assert query.message.text == ls.TEXT_BUSY  # a result, not silence
    assert context.user_data[awh.PENDING_KEY].step == "address_check"


def test_a_wallet_key_warning_survives_a_flow_that_ended_meanwhile(monkeypatch):
    # The "treat it as exposed, move your funds" warning must never be dropped, even
    # when the flow was cancelled while the key was being checked.
    world = World(monkeypatch)
    context = ctx()
    pending = _pending(world, "key", address=H.WALLET_ADDR)
    context.user_data[awh.PENDING_KEY] = pending
    card = SentMessage([], UID, "ack")
    awh.clear_link_pending(context, UID)  # e.g. [❌ Cancel] tapped during intake
    asyncio.run(awh._after_intake(context, UID, pending, ls.KeyIntake("wallet_key"), awh._Target(card, UID, None)))
    text, kw = card.edits[-1]
    assert text.startswith(ls.TEXT_R_WALLET_KEY)
    assert callback_data(kw["reply_markup"]) == ["ax:link:start", "ax:home"]


# ---------------------------------------------------------------------------
# SEC-2: [❌ Cancel] / Unlink while a store is already saving the key
# ---------------------------------------------------------------------------

def _store_in_flight(monkeypatch, world, context):
    """Start a verification whose store is blocked INSIDE its upsert (past every check)."""
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending
    _stash(world, pending)
    world.db.upsert_gate = threading.Event()
    return pending


async def _wait_for_upsert(world):
    for _ in range(500):
        if any(a[0] == "upsert" for a in world.db.all_args):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the store never reached its upsert")


def test_cancel_during_a_store_says_too_late_not_nothing_stored(monkeypatch):
    world = World(monkeypatch)
    context = ctx()
    _store_in_flight(monkeypatch, world, context)

    async def body():
        check = FakeQuery("ax:link:check")
        await awh.handle(check, "ax:link:check", UID, context)  # starts the verification
        await _wait_for_upsert(world)
        cancel = FakeQuery("ax:link:cancel")
        tap_task = asyncio.ensure_future(awh.handle(cancel, "ax:link:cancel", UID, context))
        await asyncio.sleep(0.05)
        assert not tap_task.done()  # waits for the save that cannot be stopped any more
        world.db.upsert_gate.set()
        await tap_task
        await drain()
        return cancel

    cancel = asyncio.run(body())
    text, _ = _card(cancel)
    assert arcus_ui.TEXT_R_CANCEL_TOO_LATE in text and arcus_ui.TEXT_R_CANCELLED not in text
    assert len(world.db.upserts) == 1
    assert awh.PENDING_KEY not in context.user_data


def test_cancel_before_the_store_stores_nothing_and_says_so(monkeypatch):
    world = World(monkeypatch)
    world.client.api_keys = [ok([entry(RFC_PUB, until=NOW_MS + 100 * DAY_MS)])]
    context = ctx()
    pending = _pending(world, "key")
    context.user_data[awh.PENDING_KEY] = pending
    _stash(world, pending)
    text, _ = _card(tap("ax:link:cancel", context))
    assert arcus_ui.TEXT_R_CANCELLED in text and arcus_ui.TEXT_R_CANCEL_TOO_LATE not in text
    assert world.db.upserts == []


def test_cancel_with_no_flow_claims_nothing(monkeypatch):
    World(monkeypatch)
    text, _ = _card(tap("ax:link:cancel", ctx()))
    assert arcus_ui.TEXT_R_CANCELLED not in text and arcus_ui.TEXT_R_CANCEL_TOO_LATE not in text
    assert arcus_ui.TEXT_W_NOT_LINKED in text


def test_unlink_note_names_the_key_actually_removed(monkeypatch):
    # A renewal saved nadobro-new2 just before the unlink: the note must name THAT key.
    world = World(monkeypatch)
    world.db.credential = H.row(name="nadobro-old1")

    async def unlink(uid, network):
        world.db.credential = H.row(name="nadobro-new2", status="unlinked")  # the row the unlink wiped
        return "unlinked"

    monkeypatch.setattr(ls, "unlink", unlink)
    query = FakeQuery("ax:unlink:confirm:testnet")
    asyncio.run(awh.handle(query, "ax:unlink:confirm:testnet", UID, ctx()))
    text, _ = _card(query)
    assert "Revoke <code>nadobro-new2</code>" in text and "nadobro-old1" not in text


def test_unlink_db_error_also_ends_the_link_flow(monkeypatch):
    world = World(monkeypatch)
    world.db.credential = H.row()
    _unlink_spy(monkeypatch, RuntimeError("db down"))
    context = ctx()
    context.user_data[awh.PENDING_KEY] = _pending(world, "key")
    text, _ = _card(tap("ax:unlink:confirm:testnet", context))
    assert arcus_ui.TEXT_DB_BUSY in text
    assert awh.PENDING_KEY not in context.user_data
