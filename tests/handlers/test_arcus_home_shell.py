"""The Arcus home shell (Arcus P3b, 03 §10, §19.9): link status from Postgres
only, P1's Nado banner intact, no venue call, DENIED != EMPTY."""
from __future__ import annotations

import asyncio

import pytest

from _stubs import install_test_stubs

install_test_stubs()

import arcus_link_helpers as H  # noqa: E402
from arcus_handler_helpers import FakeQuery, World, callback_data, ctx  # noqa: E402
from arcus_link_helpers import ADDR, DAY_MS, NOW_MS, UID  # noqa: E402
from src.nadobro.handlers import arcus_portfolio_handler as apf  # noqa: E402
from src.nadobro.handlers import arcus_ui  # noqa: E402
from src.nadobro.handlers import arcus_wallet_handler as awh  # noqa: E402
from src.nadobro.handlers import venue_handler as vh  # noqa: E402
from src.nadobro.i18n import _ACTIVE_LANG  # noqa: E402
from src.nadobro.users import arcus_link_service as ls  # noqa: E402
from src.nadobro.users.arcus_link_service import LinkPending  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ARCUS_ENABLED", "1")
    monkeypatch.setenv("ARCUS_ALLOWED_USER_IDS", str(UID))
    monkeypatch.delenv("ARCUS_KEY_EXPIRY_STOP_HOURS", raising=False)
    token = _ACTIVE_LANG.set("en")
    yield
    _ACTIVE_LANG.reset(token)
    ls._reset_for_tests()
    awh._reset_for_tests()


def _home(monkeypatch, *, credential=None, snapshot=("testnet", [], False), context=None):
    world = World(monkeypatch)
    world.db.credential = credential
    calls = []
    monkeypatch.setattr(ls, "_services", lambda net: calls.append(net) or (_ for _ in ()).throw(AssertionError("venue")))
    if isinstance(snapshot, BaseException):
        def boom(uid):
            raise snapshot
        monkeypatch.setattr(vh, "nado_automation_snapshot", boom)
    else:
        monkeypatch.setattr(vh, "nado_automation_snapshot", lambda uid: snapshot)
    text, markup = asyncio.run(apf.render_home(UID, context=context))
    assert calls == []  # never a venue call
    return text, markup


def test_not_linked(monkeypatch):
    text, markup = _home(monkeypatch)
    assert "ARCUS · beta" in text and "TESTNET" in text
    assert arcus_ui.TEXT_H_NOT_LINKED in text and arcus_ui.TEXT_H_STRATEGIES_SOON in text
    assert vh.TEXT_HOME_LINK_SOON not in text
    assert callback_data(markup) == ["ax:wallet", "ax:mode", "venue:view", "ax:help"]


def test_linked_shows_the_short_address_and_expiry(monkeypatch):
    until = NOW_MS + 100 * DAY_MS
    text, _ = _home(monkeypatch, credential=H.row(until=until))
    assert f"👛 <code>{arcus_ui.addr_short(ADDR)}</code> · subaccount 0 · key valid until {arcus_ui.format_utc_ms(until)}" in text
    assert "expires in" not in text


def test_linked_key_soon_renders_stop_hours(monkeypatch):
    text, _ = _home(monkeypatch, credential=H.row(until=NOW_MS + 3 * DAY_MS + 5))
    assert "The key expires in 3 days" in text and "stop 24 hours before" in text


def test_linked_without_expiry(monkeypatch):
    text, _ = _home(monkeypatch, credential=H.row(until=0))
    assert "key valid until no expiry" in text


@pytest.mark.parametrize("status,needle", [("expired", arcus_ui.TEXT_H_EXPIRED), ("invalid", arcus_ui.TEXT_H_INVALID)])
def test_expired_and_invalid(monkeypatch, status, needle):
    text, _ = _home(monkeypatch, credential=H.row(status=status))
    assert needle in text and "<code>" not in text


def test_active_past_valid_until_is_shown_expired(monkeypatch):
    text, _ = _home(monkeypatch, credential=H.row(until=NOW_MS - 1))
    assert arcus_ui.TEXT_H_EXPIRED in text


def test_unlinked_row_is_not_linked(monkeypatch):
    text, _ = _home(monkeypatch, credential=H.row(status="unlinked"))
    assert arcus_ui.TEXT_H_NOT_LINKED in text


def test_unreadable_never_says_not_linked(monkeypatch):
    text, _ = _home(monkeypatch, credential=RuntimeError("db down"))
    assert arcus_ui.TEXT_H_UNREADABLE in text and "Not linked" not in text


def test_pending_link_line(monkeypatch):
    context = ctx()
    context.user_data[awh.PENDING_KEY] = LinkPending(
        network="testnet", step="address", expires_mono=ls._mono() + 1800, generation=1,
    )
    text, _ = _home(monkeypatch, context=context)
    assert arcus_ui.TEXT_H_LINKING in text


def test_nado_banner_lines_and_stop_entries_still_appear(monkeypatch):
    items = [(vh.TEXT_ITEM_STRATEGY, {"strategy": "GRID BTC", "network": "MAINNET"}), (vh.TEXT_ITEM_DESK, {"n": "2"})]
    text, markup = _home(monkeypatch, snapshot=("testnet", items, False))
    assert "Nado is still running:</b> GRID BTC on MAINNET, Desk plans: 2" in text
    assert callback_data(markup) == [
        "ax:wallet", "ax:mode", "venue:view", "ax:help",
        "portfolio:close_all_confirm", "portfolio:cancel_all_confirm", "desk:view",
    ]


def test_banner_failure_degrades_to_could_not_check(monkeypatch):
    text, markup = _home(monkeypatch, snapshot=RuntimeError("pool exhausted"))
    assert "Couldn't check your Nado automation" in text
    assert "desk:view" in callback_data(markup)


def test_mainnet_home_reads_the_mainnet_credential(monkeypatch):
    world = World(monkeypatch)
    seen = []

    def cred(uid, network):
        seen.append(network)
        return None

    monkeypatch.setattr(world.env.db, "get_credential", cred)
    monkeypatch.setattr(ls._creds, "get_credential", cred)
    monkeypatch.setattr(vh, "nado_automation_snapshot", lambda uid: ("mainnet", [], False))
    text, _ = asyncio.run(apf.render_home(UID))
    assert seen == ["mainnet"] and "MAINNET" in text


def test_p1_home_text_is_byte_identical_without_link_lines():
    for args in (("testnet", [], False), ("mainnet", [(vh.TEXT_ITEM_COPY, {"n": "2"})], True)):
        old = "\n".join(
            [vh.tr(vh.TEXT_HOME_TITLE, network=args[0].upper()), "", vh.tr(vh.TEXT_HOME_LINK_SOON)]
            + (["", vh.tr(vh.TEXT_HOME_NADO_RUNNING, items="Copy trades: 2"), vh.tr(vh.TEXT_HOME_NADO_KEEPS_RUNNING)]
               if args[1] else [])
            + (["", vh.tr(vh.TEXT_HOME_NADO_UNREADABLE)] if args[2] else [])
        )
        assert vh.arcus_home_text(*args) == old
        assert vh.arcus_home_text(*args, link_lines=None) == old


def test_refresh_re_renders_in_place_and_bumps_once(monkeypatch):
    World(monkeypatch)
    monkeypatch.setattr(vh, "nado_automation_snapshot", lambda uid: ("testnet", [], False))
    from src.nadobro.handlers import callbacks

    bumps = []
    monkeypatch.setattr(callbacks, "bump_interaction_seq", lambda chat: bumps.append(chat))
    query = FakeQuery("ax:refresh")
    asyncio.run(apf.handle(query, "ax:refresh", UID, ctx()))
    assert len(query.edits) == 1 and "ARCUS · beta" in query.edits[0][0]
    assert bumps == [UID]
    other = FakeQuery("ax:other")
    asyncio.run(apf.handle(other, "ax:other", UID, ctx()))
    assert other.edits == []


def test_ax_home_through_the_venue_handler_renders_the_shell(monkeypatch):
    World(monkeypatch)
    monkeypatch.setattr(vh, "nado_automation_snapshot", lambda uid: ("testnet", [], False))
    text, markup = asyncio.run(vh.build_arcus_screen(vh.AX_HOME, UID, ctx()))
    assert arcus_ui.TEXT_H_NOT_LINKED in text
    assert callback_data(markup)[:2] == ["ax:wallet", "ax:mode"]
