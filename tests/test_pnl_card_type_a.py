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
    # Sourced from the positions table (correct LEVERAGE — the fills record 0/1).
    rt = {"pair": "ETH-PERP", "side": "long", "leverage": 10, "close_realized_pnl": 55.5,
          "avg_entry_price": 2412.35, "close_price": 2456.78, "size": 1.25}
    with patch("src.nadobro.models.database.get_manual_closed_round_trip", return_value=rt), \
         patch.object(bld, "_fetch_active_referral_code", return_value="NADO8RO"):
        d = bld.build_round_trip_card_data(42, "mainnet", "99")
    assert d["badge"] == "DESK TRADE"
    assert d["product"] == "ETH:PERP-USDC" and d["side"] == "LONG" and d["leverage"] == 10.0
    assert d["entry_price"] == 2412.35 and d["exit_price"] == 2456.78 and d["size"] == 1.25


def test_round_trip_builder_gates_spot_and_missing():
    with patch("src.nadobro.models.database.get_manual_closed_round_trip", return_value=None):
        assert bld.build_round_trip_card_data(42, "mainnet", "1").get("unsupported") == "not_found"
    spot = {"pair": "KBTC", "side": "long", "close_realized_pnl": 1.0,
            "avg_entry_price": 1.0, "close_price": 1.1, "size": 1.0, "leverage": 1}
    with patch("src.nadobro.models.database.get_manual_closed_round_trip", return_value=spot):
        assert bld.build_round_trip_card_data(42, "mainnet", "1").get("unsupported") == "spot"


# ── History integration (display-only, no double-count) ─────────

def test_history_shows_copy_and_desk_trades_from_the_venue_ledger():
    """Copy trades and desk trades are both venue position windows now; each
    is labelled by its source and gets a venue-window share card."""
    from src.nadobro.handlers import history_view

    from datetime import datetime, timezone
    rows = [
        {
            "id": 55, "product_id": 2, "product_name": "BTC-PERP", "is_long": False, "isolated": True,
            "total_close_amount": "2", "max_amount": "2", "amount": "0",
            "avg_entry_price": "100", "avg_exit_price": "90", "realized_pnl": "20",
            "open_fee": "0.5", "close_fee": "0.5", "is_open": False,
            "open_ts": datetime(2026, 7, 19, 11, 30, tzinfo=timezone.utc),
            "update_ts": datetime(2026, 7, 19, 12, tzinfo=timezone.utc),
            "strategy_session_id": 311, "session_strategy": "copy", "source": "copy",
        },
        {
            "id": 77, "product_id": 1, "product_name": "ETH-PERP", "is_long": True, "isolated": False,
            "total_close_amount": "1", "max_amount": "1", "amount": "0",
            "avg_entry_price": "2400", "avg_exit_price": "2450", "realized_pnl": "50",
            "open_fee": "0.5", "close_fee": "0.5", "is_open": False,
            "open_ts": datetime(2026, 7, 19, 10, tzinfo=timezone.utc),
            "update_ts": datetime(2026, 7, 19, 11, tzinfo=timezone.utc),
            "strategy_session_id": None, "session_strategy": None, "source": "manual",
        },
    ]
    with patch("src.nadobro.models.database.get_venue_positions", return_value=rows):
        text, kb = history_view.render_history_view({"network": "mainnet", "user_id": 42})

    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "portfolio:share_pnl:vp:55" in cbs
    assert "portfolio:share_pnl:vp:77" in cbs
    assert "copy #311" in text and "ETH-PERP" in text and "manual" in text
    # The venue's own exit is rendered — no reconstruction from an accumulated pnl.
    assert "$100.00 → $90.00" in text
    assert "No trades yet" not in text


def test_venue_position_card_data_uses_the_venue_figures():
    from src.nadobro.portfolio.pnl_card_builder import build_venue_position_card_data

    row = {
        "id": 70, "product_id": 1, "product_name": "ETH-PERP", "is_long": True, "isolated": False,
        "total_close_amount": "1.0", "max_amount": "1.0", "avg_entry_price": "100",
        "avg_exit_price": "125", "realized_pnl": "25", "is_open": False, "session_strategy": "copy",
    }
    with patch("src.nadobro.models.database.get_venue_position", return_value=row), \
         patch("src.nadobro.portfolio.pnl_card_builder._fetch_active_referral_code", return_value="REF"):
        data = build_venue_position_card_data(42, "mainnet", 70)
    assert data["badge"] == "COPY SESSION"
    assert data["entry_price"] == 100.0 and data["exit_price"] == 125.0 and data["size"] == 1.0
    assert data["pnl"] == 25.0 and data["side"] == "LONG"
    with patch("src.nadobro.models.database.get_venue_position", return_value={**row, "is_open": True}):
        assert build_venue_position_card_data(42, "mainnet", 70) == {"unsupported": "not_found"}
    with patch("src.nadobro.models.database.get_venue_position", return_value=None):
        assert build_venue_position_card_data(42, "mainnet", 70) == {"unsupported": "not_found"}
