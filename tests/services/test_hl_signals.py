"""Assembling the Hyperliquid feed into Mid's directional components.

The properties that matter most here are the two absence states and the
side-convention self-check:

* a market Hyperliquid does not list (every Nado equity/RWA) must report
  ``supported: False`` — permanent and expected, NOT degradation, or those
  products would quote wide forever waiting for a feed that never comes;
* a market HL does list but has gone quiet must report ``None`` — that IS
  degradation and the caller widens;
* the trade ``side`` convention is unverified, so a mapping that scores below
  chance disables the component instead of trading backwards.
"""
import time

import pytest

from src.nadobro.market_data import hl_ws
from src.nadobro.trading import hl_signals as hs


@pytest.fixture(autouse=True)
def _clean():
    hl_ws.reset_state()
    hs.reset_state()
    yield
    hl_ws.reset_state()
    hs.reset_state()
    hl_ws._book_listeners.clear()


def _book(coin="BTC", bids=((100.0, 5.0),), asks=((100.2, 5.0),), ts_ms=1_700_000_000_000):
    return {
        "channel": "l2Book",
        "data": {
            "coin": coin,
            "levels": [
                [{"px": str(p), "sz": str(s)} for p, s in bids],
                [{"px": str(p), "sz": str(s)} for p, s in asks],
            ],
            "time": ts_ms,
        },
    }


def _trades(coin="BTC", rows=((100.1, 2.0, "B", "0xa"),)):
    return {
        "channel": "trades",
        "data": [
            {"coin": coin, "px": str(px), "sz": str(sz), "side": side,
             "hash": h, "time": 1_700_000_000_000}
            for px, sz, side, h in rows
        ],
    }


# --- product mapping --------------------------------------------------------

@pytest.mark.parametrize("product,coin", [
    ("BTC-PERP", "BTC"), ("eth-perp", "ETH"), ("SOL", "SOL"),
    ("BTC-USDT0", "BTC"), ("", ""),
])
def test_product_maps_to_the_hl_coin(product, coin):
    assert hs.coin_for(product) == coin


def test_a_wrapped_equity_does_not_masquerade_as_a_crypto_coin():
    assert hs.coin_for("wGOOGLx-PERP") == "WGOOGLX"


# --- the two absence states -------------------------------------------------

def test_a_market_hyperliquid_never_lists_is_unsupported_not_degraded():
    # QQQ has no HL equivalent. Reporting degradation would widen this product
    # forever for a feed that is never coming.
    out = hs.build_components("QQQ-PERP", 500.0)
    assert out == {"supported": False}


def test_a_listed_market_that_went_quiet_is_degraded():
    hl_ws._dispatch(_book())
    assert hs.build_components("BTC-PERP", 100.1) is not None
    hl_ws._books["BTC"]["received_at"] = time.time() - 3600
    assert hs.build_components("BTC-PERP", 100.1) is None


def test_an_empty_product_name_is_unsupported():
    assert hs.build_components("", 100.0) == {"supported": False}


# --- components -------------------------------------------------------------

def test_a_bid_heavy_book_produces_a_positive_lean():
    hl_ws._dispatch(_book(bids=((100.0, 90.0),), asks=((100.2, 10.0),)))
    out = hs.build_components("BTC-PERP", 100.1)
    comps = out["components"]
    assert comps["obi"] > 0
    assert comps["micro_displacement"] > 0        # microprice sits above mid


def test_every_component_key_is_on_the_alpha_allowlist():
    # A defensive name leaking in here would be silently dropped by the blend;
    # catching it at the source is better than discovering it in `dropped`.
    from src.nadobro.quant import alpha as al

    hl_ws._dispatch(_book())
    out = hs.build_components("BTC-PERP", 100.1)
    assert set(out["components"]) <= al.DIRECTIONAL_SIGNALS


def test_components_are_all_bounded():
    hl_ws._dispatch(_book(bids=((100.0, 900.0),), asks=((100.2, 1.0),)))
    hl_ws._dispatch(_trades())
    out = hs.build_components("BTC-PERP", 100.1)
    for name, value in out["components"].items():
        assert value is None or -1.0 <= value <= 1.0, name


def test_a_nado_premium_reads_as_a_short_lean():
    # Nado richer than HL => expected to fall back toward it => negative alpha.
    hl_ws._dispatch(_book(bids=((100.0, 5.0),), asks=((100.2, 5.0),)))
    rich = hs.build_components("BTC-PERP", 110.0)["components"]["basis"]
    cheap = hs.build_components("BTC-PERP", 90.0)["components"]["basis"]
    assert rich < 0 < cheap


def test_the_basis_term_can_be_switched_off(monkeypatch):
    # It rests on HL LEADING Nado, which is a hypothesis until measured.
    monkeypatch.setattr(hs, "basis_alpha_enabled", lambda: False)
    hl_ws._dispatch(_book())
    assert hs.build_components("BTC-PERP", 110.0)["components"]["basis"] is None


def test_ofi_needs_two_consecutive_books():
    hl_ws._dispatch(_book())
    assert hs.build_components("BTC-PERP", 100.1)["components"]["ofi"] is None
    hl_ws._dispatch(_book(bids=((100.0, 50.0),), asks=((100.2, 5.0),)))
    assert hs.build_components("BTC-PERP", 100.1)["components"]["ofi"] is not None


# --- the mid ring -----------------------------------------------------------

def test_the_mid_ring_is_fed_by_the_pushed_book_not_the_tick():
    # Momentum and realized vol need the market's clock, not the strategy's.
    hs.ensure_registered()
    for i in range(5):
        hl_ws._dispatch(_book(bids=((100.0 + i, 5.0),), asks=((100.2 + i, 5.0),)))
    assert len(hs._mids["BTC"]) == 5


def test_the_listener_is_registered_once_however_often_it_is_asked():
    # A closure would defeat the equality dedupe — the exact leak hl_ws had.
    before = len(hl_ws._book_listeners)
    for _ in range(5):
        hs.ensure_registered()
    assert len(hl_ws._book_listeners) == before + 1


# --- the side-convention self-check ----------------------------------------

def test_an_inverted_side_convention_disables_the_component(monkeypatch):
    monkeypatch.setattr(hs, "_SIDE_CHECK_MIN_SAMPLES", 4)
    hs.ensure_registered()
    # Net buying every round while the price falls: the mapping is backwards.
    price = 100.0
    for i in range(10):
        price -= 0.5
        hl_ws._dispatch(_book(bids=((price, 5.0),), asks=((price + 0.2, 5.0),)))
        hl_ws._dispatch(_trades(rows=((price, 5.0, "B", f"0x{i}"),)))
        hs.build_components("BTC-PERP", price)
    out = hs.build_components("BTC-PERP", price)
    assert out["side_convention_disabled"] is True
    assert out["components"]["trade_imbalance"] is None


def test_a_consistent_convention_is_left_enabled(monkeypatch):
    monkeypatch.setattr(hs, "_SIDE_CHECK_MIN_SAMPLES", 4)
    hs.ensure_registered()
    price = 100.0
    for i in range(10):
        price += 0.5
        hl_ws._dispatch(_book(bids=((price, 5.0),), asks=((price + 0.2, 5.0),)))
        hl_ws._dispatch(_trades(rows=((price, 5.0, "B", f"0x{i}"),)))
        hs.build_components("BTC-PERP", price)
    assert hs.build_components("BTC-PERP", price)["side_convention_disabled"] is False


def test_a_disabled_feed_reports_degraded(monkeypatch):
    hl_ws._dispatch(_book())
    monkeypatch.setattr(hl_ws, "enabled", lambda: False)
    assert hs.build_components("BTC-PERP", 100.1) is None
