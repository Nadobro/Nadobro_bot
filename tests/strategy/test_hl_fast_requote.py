"""Waking a Mid session on a Hyperliquid book move.

Three properties keep this from becoming a stampede, and each is the kind of
bug that only shows up in production:

* ONE listener however often registration is asked for — a per-call closure
  would attach a new one every cycle, which is the leak hl_ws shipped with;
* interest EXPIRES, so a stopped session stops being nudged with nothing to
  unregister;
* only MATERIAL moves nudge — HL pushes thousands of book events a minute.
"""
import time

import pytest

from src.nadobro.market_data import hl_ws
from src.nadobro.strategy import hl_fast_requote as fr


@pytest.fixture(autouse=True)
def _clean():
    hl_ws.reset_state()
    fr.reset_state()
    yield
    hl_ws.reset_state()
    fr.reset_state()
    hl_ws._book_listeners.clear()


@pytest.fixture()
def nudges(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "src.nadobro.strategy.bot_runtime.nudge_strategy_cycle",
        lambda uid, net: seen.append((uid, net)) or True,
    )
    return seen


def _set_mid(coin, px):
    hl_ws._mids[coin] = {"mid": px, "received_at": time.time()}


# --- registration -----------------------------------------------------------

def test_the_listener_is_attached_exactly_once():
    before = len(hl_ws._book_listeners)
    for _ in range(10):
        fr.ensure_registered()
    assert len(hl_ws._book_listeners) == before + 1
    assert fr.watch_state()["registered"] is True


def test_registration_is_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(fr, "enabled", lambda: False)
    before = len(hl_ws._book_listeners)
    fr.ensure_registered()
    assert len(hl_ws._book_listeners) == before


# --- who gets woken ---------------------------------------------------------

def test_a_material_move_wakes_the_watching_session(nudges):
    fr.note_active(7, "mainnet", "BTC-PERP")
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")            # seeds, never nudges on the first frame
    assert nudges == []
    _set_mid("BTC", 100.5)          # 50bp
    fr.on_hl_book("BTC")
    assert nudges == [(7, "mainnet")]


def test_a_trivial_move_wakes_nobody(nudges):
    fr.note_active(7, "mainnet", "BTC-PERP")
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    _set_mid("BTC", 100.001)        # 0.1bp, under the 3bp floor
    fr.on_hl_book("BTC")
    assert nudges == []


def test_a_coin_nobody_watches_is_free(nudges):
    _set_mid("DOGE", 0.1)
    fr.on_hl_book("DOGE")
    _set_mid("DOGE", 0.2)
    fr.on_hl_book("DOGE")
    assert nudges == []


def test_only_sessions_on_that_product_are_woken(nudges):
    fr.note_active(1, "mainnet", "BTC-PERP")
    fr.note_active(2, "mainnet", "ETH-PERP")
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    _set_mid("BTC", 101.0)
    fr.on_hl_book("BTC")
    assert nudges == [(1, "mainnet")]


def test_several_sessions_on_one_product_are_all_woken(nudges):
    fr.note_active(1, "mainnet", "BTC-PERP")
    fr.note_active(2, "testnet", "BTC-PERP")
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    _set_mid("BTC", 101.0)
    fr.on_hl_book("BTC")
    assert sorted(nudges) == [(1, "mainnet"), (2, "testnet")]


# --- throttling -------------------------------------------------------------

def test_a_burst_of_book_events_produces_one_nudge(nudges):
    fr.note_active(7, "mainnet", "BTC-PERP")
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    for i in range(50):
        _set_mid("BTC", 100.0 + i)
        fr.on_hl_book("BTC")
    assert len(nudges) == 1          # the per-session interval floor holds


# --- expiry -----------------------------------------------------------------

def test_a_stopped_session_ages_out_with_nothing_to_unregister(nudges):
    fr.note_active(7, "mainnet", "BTC-PERP")
    fr._watchers["BTC"][(7, "mainnet")] = time.time() - fr.WATCH_TTL_S - 1
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    _set_mid("BTC", 101.0)
    fr.on_hl_book("BTC")
    assert nudges == []
    assert fr.watch_state()["coins"] == {}


def test_renewing_keeps_a_session_alive(nudges):
    fr.note_active(7, "mainnet", "BTC-PERP")
    fr._watchers["BTC"][(7, "mainnet")] = time.time() - fr.WATCH_TTL_S - 1
    fr.note_active(7, "mainnet", "BTC-PERP")      # the cycle renewed it
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    _set_mid("BTC", 101.0)
    fr.on_hl_book("BTC")
    assert nudges == [(7, "mainnet")]


# --- robustness -------------------------------------------------------------

def test_an_unlisted_product_registers_no_watch():
    fr.note_active(7, "mainnet", "")
    assert fr.watch_state()["coins"] == {}


def test_a_raising_nudge_cannot_kill_the_feed(monkeypatch):
    monkeypatch.setattr(
        "src.nadobro.strategy.bot_runtime.nudge_strategy_cycle",
        lambda uid, net: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    fr.note_active(7, "mainnet", "BTC-PERP")
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    _set_mid("BTC", 101.0)
    fr.on_hl_book("BTC")            # must not raise

def test_disabled_means_no_nudges(nudges, monkeypatch):
    fr.note_active(7, "mainnet", "BTC-PERP")
    _set_mid("BTC", 100.0)
    fr.on_hl_book("BTC")
    monkeypatch.setattr(fr, "enabled", lambda: False)
    _set_mid("BTC", 105.0)
    fr.on_hl_book("BTC")
    assert nudges == []
