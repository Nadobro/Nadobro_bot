"""Type A PnL card (normal trades: desk/agent + copy) — renderer, data
builders, and History integration.

Pins: the card renders both variants; each card maps ONLY that trade's stats;
copy exit price is recovered exactly from the gross PnL; spot is gated out
(perps only); closed copy positions surface in History (display-only) without
double-counting the manual round-trip stream.
"""
from __future__ import annotations

from unittest.mock import patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.portfolio import pnl_card_builder as bld
from src.nadobro.portfolio.pnl_card_type_a import (
    _GREEN,
    _RED,
    _fmt_leverage,
    _side_color,
    generate_type_a_card,
)


def _png_ok(b: bytes) -> bool:
    return isinstance(b, bytes) and len(b) > 1000 and b[:8] == b"\x89PNG\r\n\x1a\n"


# ── redesign invariants (pin the mockup-matching behaviour) ─────

def test_side_pill_colour_keys_on_side_not_pnl():
    # LONG is always green, SHORT always red — a losing long still shows green.
    assert _side_color("LONG") == _GREEN
    assert _side_color("long") == _GREEN
    assert _side_color("SHORT") == _RED
    assert _side_color("short") == _RED


def test_leverage_formats_lowercase_x_and_omits_when_unset():
    assert _fmt_leverage(10) == "10x"
    assert _fmt_leverage(1) == "1x"
    assert _fmt_leverage(2.5) == "2.5x"
    assert _fmt_leverage(0) == ""          # no leverage → no pill suffix
    assert _fmt_leverage(None) == ""
    assert _fmt_leverage("bad") == ""


# ── renderer ────────────────────────────────────────────────────

def test_renderer_produces_both_variants():
    base = {
        "badge": "COPY TRADE", "product": "ETH:PERP-USDC", "base_symbol": "ETH",
        "side": "LONG", "leverage": 10, "pnl": 428.32,
        "entry_price": 2412.35, "exit_price": 2456.78, "size": 1.25,
        "referral_code": "NADO8RO",
    }
    assert _png_ok(generate_type_a_card(base))                       # positive/trophy robot
    assert _png_ok(generate_type_a_card({**base, "badge": "DESK TRADE", "pnl": -428.32}))  # negative/sad robot


def test_renderer_tolerates_unknown_icon_and_zero_leverage():
    data = {
        "badge": "DESK TRADE", "product": "WIF:PERP-USDC", "base_symbol": "WIF",
        "side": "SHORT", "leverage": 0, "pnl": -12.0,
        "entry_price": 1.23, "exit_price": 1.30, "size": 100, "referral_code": "",
    }
    assert _png_ok(generate_type_a_card(data))  # no WIF icon, no leverage, no referral → still renders


# ── copy builder ────────────────────────────────────────────────

def test_copy_builder_maps_and_recovers_exact_exit():
    pos = {"id": 7, "user_id": 42, "product_name": "ETH-PERP", "side": "long",
           "entry_price": 2412.35, "size": 1.25, "leverage": 10, "pnl": 55.54}
    with patch("src.nadobro.models.database.get_closed_copy_position", return_value=pos), \
         patch.object(bld, "_fetch_active_referral_code", return_value="NADO8RO"):
        d = bld.build_copy_trade_card_data(42, "mainnet", 7)
    assert d["badge"] == "COPY TRADE"
    assert d["product"] == "ETH:PERP-USDC" and d["base_symbol"] == "ETH"
    assert d["side"] == "LONG" and d["leverage"] == 10.0
    # exit = entry + pnl/(size*dir) recovers the effective exit exactly.
    assert abs(d["exit_price"] - (2412.35 + 55.54 / 1.25)) < 1e-6
    assert d["referral_code"] == "NADO8RO"


def test_copy_builder_short_exit_direction():
    pos = {"id": 8, "user_id": 42, "product_name": "BTC-PERP", "side": "short",
           "entry_price": 100.0, "size": 2.0, "leverage": 5, "pnl": 20.0}
    with patch("src.nadobro.models.database.get_closed_copy_position", return_value=pos), \
         patch.object(bld, "_fetch_active_referral_code", return_value="X"):
        d = bld.build_copy_trade_card_data(42, "mainnet", 8)
    # short profit => exit below entry: 100 - 20/2 = 90
    assert abs(d["exit_price"] - 90.0) < 1e-6 and d["side"] == "SHORT"


def test_copy_builder_uses_whole_trade_closed_size_after_partial_closes():
    # A copy the leader trimmed before fully closing: the row's `size` is the
    # last remaining slice (0.25) but `closed_size` is the WHOLE trade (1.0) and
    # `pnl` is the accumulated total (15 + 10 = 25). The card must show the whole
    # trade: Size 1.0, PnL 25, and a size-weighted exit — NOT the last slice.
    #   slice1: close 0.75 @ 120 → +15 ; slice2: close 0.25 @ 140 → +10
    #   weighted exit = (120*0.75 + 140*0.25)/1.0 = 125
    pos = {"id": 9, "user_id": 42, "product_name": "ETH-PERP", "side": "long",
           "entry_price": 100.0, "size": 0.25, "closed_size": 1.0,
           "leverage": 3, "pnl": 25.0}
    with patch("src.nadobro.models.database.get_closed_copy_position", return_value=pos), \
         patch.object(bld, "_fetch_active_referral_code", return_value="X"):
        d = bld.build_copy_trade_card_data(42, "mainnet", 9)
    assert d["size"] == 1.0                       # whole-trade base, not 0.25
    assert d["pnl"] == 25.0                       # accumulated, not last slice
    assert abs(d["exit_price"] - 125.0) < 1e-9    # size-weighted exit over the whole trade
    assert d["entry_price"] == 100.0


def test_copy_builder_legacy_row_without_closed_size_falls_back_to_size():
    # Legacy row (closed before closed_size existed): closed_size absent/None →
    # fall back to the row `size` and its (last-slice) pnl. Stays internally
    # consistent; new closes are the ones that get whole-trade numbers.
    legacy = {"id": 10, "user_id": 42, "product_name": "ETH-PERP", "side": "long",
              "entry_price": 100.0, "size": 0.25, "closed_size": None,
              "leverage": 3, "pnl": 10.0}
    with patch("src.nadobro.models.database.get_closed_copy_position", return_value=legacy), \
         patch.object(bld, "_fetch_active_referral_code", return_value="X"):
        d = bld.build_copy_trade_card_data(42, "mainnet", 10)
    assert d["size"] == 0.25
    assert abs(d["exit_price"] - 140.0) < 1e-9    # 100 + 10/(0.25*1)


def test_copy_builder_guards_missing_and_foreign():
    with patch("src.nadobro.models.database.get_closed_copy_position", return_value=None):
        assert bld.build_copy_trade_card_data(42, "mainnet", 7).get("unsupported") == "not_found"
    foreign = {"id": 7, "user_id": 999, "product_name": "ETH-PERP", "side": "long",
               "entry_price": 1.0, "size": 1.0, "pnl": 0.0}
    with patch("src.nadobro.models.database.get_closed_copy_position", return_value=foreign):
        assert bld.build_copy_trade_card_data(42, "mainnet", 7).get("unsupported") == "not_found"


# ── round-trip (desk/manual) builder ────────────────────────────

def test_round_trip_builder_desk_badge_and_perp_mapping():
    rt = {"product_name": "ETH-PERP", "side": "long", "leverage": 10, "realized_pnl": 55.5,
          "avg_open_price": 2412.35, "avg_close_price": 2456.78, "size": 1.25}
    with patch("src.nadobro.trading.trade_service.find_round_trip", return_value=rt), \
         patch.object(bld, "_fetch_active_referral_code", return_value="NADO8RO"):
        d = bld.build_round_trip_card_data(42, "mainnet", "99")
    assert d["badge"] == "DESK TRADE"
    assert d["product"] == "ETH:PERP-USDC" and d["side"] == "LONG" and d["leverage"] == 10.0
    assert d["entry_price"] == 2412.35 and d["exit_price"] == 2456.78 and d["size"] == 1.25


def test_round_trip_builder_gates_spot_and_missing():
    with patch("src.nadobro.trading.trade_service.find_round_trip", return_value=None):
        assert bld.build_round_trip_card_data(42, "mainnet", "1").get("unsupported") == "not_found"
    spot = {"product_name": "KBTC", "side": "long", "realized_pnl": 1.0,
            "avg_open_price": 1.0, "avg_close_price": 1.1, "size": 1.0}
    with patch("src.nadobro.trading.trade_service.find_round_trip", return_value=spot):
        assert bld.build_round_trip_card_data(42, "mainnet", "1").get("unsupported") == "spot"


# ── History integration (display-only, no double-count) ─────────

def test_history_merges_copy_positions_without_touching_round_trips():
    from src.nadobro.handlers import history_view

    manual_rt = [{
        "trip_key": "rt1", "product_id": 1, "product_name": "ETH-PERP", "pair": "ETH-PERP",
        "side": "long", "size": 1.0, "leverage": 10, "avg_open_price": 2400.0,
        "avg_close_price": 2450.0, "realized_pnl": 50.0, "fees": 1.0, "funding_paid": 0.0,
        "volume_usd": 4850.0, "open_ts": "2026-07-19T10:00:00Z", "close_ts": "2026-07-19T11:00:00Z",
        "isolated": False,
    }]
    closed_copy = [{
        "id": 55, "product_id": 2, "product_name": "BTC-PERP", "side": "short",
        "entry_price": 100.0, "size": 2.0, "leverage": 5, "pnl": 20.0,
        "closed_at": "2026-07-19T12:00:00Z",
    }]
    with patch("src.nadobro.trading.trade_service.compute_round_trips", return_value=manual_rt), \
         patch("src.nadobro.models.database.get_closed_copy_positions", return_value=closed_copy):
        text, kb = history_view.render_history_view({"network": "mainnet", "user_id": 42})

    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    # Both a copy card and a desk round-trip card are offered, from their OWN sources.
    assert "portfolio:share_pnl:copy:55" in cbs
    assert "portfolio:share_pnl:rt:rt1" in cbs
    # Copy renders after the manual trip (later close_ts sorts first) — display only.
    assert "copy" in text and "ETH-PERP" in text


def test_history_shows_copies_even_with_no_manual_trades():
    from src.nadobro.handlers import history_view

    closed_copy = [{
        "id": 60, "product_id": 2, "product_name": "SOL-PERP", "side": "long",
        "entry_price": 100.0, "size": 3.0, "leverage": 3, "pnl": -9.0,
        "closed_at": "2026-07-19T12:00:00Z",
    }]
    with patch("src.nadobro.trading.trade_service.compute_round_trips", return_value=[]), \
         patch("src.nadobro.models.database.get_closed_copy_positions", return_value=closed_copy):
        text, kb = history_view.render_history_view({"network": "mainnet", "user_id": 42})
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "portfolio:share_pnl:copy:60" in cbs
    assert "No trades yet" not in text


def test_history_shows_whole_trade_size_and_exit_for_partial_closed_copy():
    from src.nadobro.handlers import history_view

    # Leader trimmed before closing: row size = last slice (0.25), closed_size =
    # whole trade (1.0), pnl = accumulated (25). History must show the whole
    # trade — size 1 and exit $125.00 (100 + 25/1.0) — not the broken last-slice
    # reconstruction (0.25 → exit $200.00 = 100 + 25/0.25).
    closed_copy = [{
        "id": 70, "product_id": 2, "product_name": "ETH-PERP", "side": "long",
        "entry_price": 100.0, "size": 0.25, "closed_size": 1.0, "leverage": 3,
        "pnl": 25.0, "closed_at": "2026-07-19T12:00:00Z",
    }]
    with patch("src.nadobro.trading.trade_service.compute_round_trips", return_value=[]), \
         patch("src.nadobro.models.database.get_closed_copy_positions", return_value=closed_copy):
        text, _kb = history_view.render_history_view({"network": "mainnet", "user_id": 42})
    assert "$125.00" in text        # size-weighted whole-trade exit
    assert "$200.00" not in text    # the broken last-slice-only exit must be gone
    assert "1 @ $100.00" in text    # whole-trade size, not 0.25
