"""Venue capability table after Arcus P3b (03 §11.3, §19.11)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from _stubs import install_test_stubs

install_test_stubs()

from telegram.ext import ApplicationHandlerStop  # noqa: E402

from src.nadobro.handlers import venue_gate as vg  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402
from src.nadobro.utils import venue_capabilities as vc  # noqa: E402
from src.nadobro.utils.venue_capabilities import (  # noqa: E402
    ARCUS_ONLY,
    DISPATCH,
    NEVER_GATE,
    classify_callback,
    classify_command,
)

UID = 990_034_101


def test_dispatch_targets():
    assert classify_callback("wallet:view") == (DISPATCH, "ax:wallet")
    assert classify_callback("home:mode") == (DISPATCH, "ax:mode")
    assert classify_callback("nav:mode") == (DISPATCH, "ax:mode")
    assert classify_command("revoke") == (DISPATCH, "ax:unlink")
    assert (vc.AX_WALLET, vc.AX_MODE, vc.AX_UNLINK) == ("ax:wallet", "ax:mode", "ax:unlink")


def test_the_nado_revoke_path_stays_never_gate():
    assert classify_callback("wallet:revoke_steps") == (NEVER_GATE, None)
    assert classify_callback("wallet:revoke_confirm") == (NEVER_GATE, None)


@pytest.mark.parametrize("data", [
    "ax:wallet", "ax:link:start", "ax:link:attest", "ax:link:same", "ax:link:check", "ax:link:cancel",
    "ax:unlink", "ax:unlink:confirm:testnet", "ax:unlink:confirm:mainnet", "ax:mode", "ax:mode:testnet",
    "ax:mode:mainnet", "ax:refresh",
])
def test_every_p3b_callback_is_arcus_only(data):
    assert classify_callback(data) == (ARCUS_ONLY, None)


def test_arcus_features_are_wallet_only():
    assert vc.VENUE_CAPABILITIES["arcus"]["features"] == frozenset({"wallet"})
    assert vc.VENUE_CAPABILITIES["arcus"]["strategies"] == frozenset()


def test_the_venue_handler_routes_every_p3b_callback(monkeypatch):
    """Every ax: value above reaches the wallet / home handler for an Arcus-view user."""
    routed = []

    async def arcus_view(uid):
        return "arcus"

    from src.nadobro.handlers import arcus_portfolio_handler as apf
    from src.nadobro.handlers import arcus_wallet_handler as awh

    async def wallet(query, data, uid, context):
        routed.append(("wallet", data))

    async def home(query, data, uid, context):
        routed.append(("home", data))

    monkeypatch.setattr(vh, "read_active_venue", arcus_view)
    monkeypatch.setattr(awh, "handle", wallet)
    monkeypatch.setattr(apf, "handle", home)
    for data in ("ax:wallet", "ax:link:start", "ax:unlink", "ax:unlink:confirm:testnet", "ax:mode",
                 "ax:mode:mainnet", "ax:refresh", "ax:bogus"):
        update = SimpleNamespace(callback_query=SimpleNamespace(data=data, message=None),
                                 effective_user=SimpleNamespace(id=UID))
        asyncio.run(vh.handle_venue_callback(update, SimpleNamespace(user_data={})))
    assert routed == [
        ("wallet", "ax:wallet"), ("wallet", "ax:link:start"), ("wallet", "ax:unlink"),
        ("wallet", "ax:unlink:confirm:testnet"), ("wallet", "ax:mode"), ("wallet", "ax:mode:mainnet"),
        ("home", "ax:refresh"),
    ]


def test_a_nado_view_tap_on_a_wallet_route_renders_nothing(monkeypatch):
    from src.nadobro.handlers import arcus_wallet_handler as awh

    async def nado_view(uid):
        return "nado"

    called = []

    async def wallet(query, data, uid, context):
        called.append(data)

    monkeypatch.setattr(vh, "read_active_venue", nado_view)
    monkeypatch.setattr(awh, "handle", wallet)
    update = SimpleNamespace(callback_query=SimpleNamespace(data="ax:link:start", message=None),
                             effective_user=SimpleNamespace(id=UID))
    asyncio.run(vh.handle_venue_callback(update, SimpleNamespace(user_data={})))
    assert called == []


class _Msg:
    def __init__(self, text):
        self.text = text
        self.caption = None
        self.entities = (SimpleNamespace(type="bot_command", offset=0, length=len(text.split()[0])),)
        self.chat_id = UID
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def test_nado_view_revoke_passes_to_cmd_revoke_unchanged(monkeypatch):
    """A Nado-view /revoke still reaches the Nado command (DISPATCH passes for Nado
    users); only the venue is now read first."""
    reads = []

    async def nado_view(uid):
        reads.append(uid)
        return "nado"

    monkeypatch.setattr(vg, "read_active_venue", nado_view)
    ran = []

    async def cmd_revoke(update, context):
        ran.append(update.message.text)

    msg = _Msg("/revoke")
    update = SimpleNamespace(callback_query=None, message=msg, edited_message=None,
                             effective_user=SimpleNamespace(id=UID, username="u"), effective_message=msg)

    async def body():
        try:
            await vg.venue_gate(update, SimpleNamespace(user_data={}))
        except ApplicationHandlerStop:
            return False
        await cmd_revoke(update, SimpleNamespace(user_data={}))
        return True

    assert asyncio.run(body()) is True
    assert ran == ["/revoke"] and msg.replies == [] and reads == [UID]


def test_arcus_view_revoke_renders_the_unlink_card(monkeypatch):
    async def arcus_view(uid):
        return "arcus"

    rendered = []

    async def render(target, uid, *, query=None, message=None, context=None):
        rendered.append(target)

    async def rb(fn, *a, **k):
        return fn(*a, **k)

    monkeypatch.setattr(vg, "read_active_venue", arcus_view)
    monkeypatch.setattr(vg, "render_arcus_target", render)
    monkeypatch.setattr(vg, "get_or_create_user", lambda *a, **k: None)
    monkeypatch.setattr(vg, "run_blocking_db", rb)
    msg = _Msg("/revoke")
    update = SimpleNamespace(callback_query=None, message=msg, edited_message=None,
                             effective_user=SimpleNamespace(id=UID, username="u"), effective_message=msg)

    async def body():
        try:
            await vg.venue_gate(update, SimpleNamespace(user_data={}))
        except ApplicationHandlerStop:
            return False
        return True

    assert asyncio.run(body()) is False
    assert rendered == ["ax:unlink"]
