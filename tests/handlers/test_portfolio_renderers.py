from pathlib import Path
from unittest.mock import patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers.history_view import render_history_view
from src.nadobro.handlers.orders_view import render_orders_view
from src.nadobro.handlers.performance_view import ensure_session_card_template, generate_session_card
from src.nadobro.handlers.portfolio_deck import render_loading, render_portfolio_deck
from src.nadobro.handlers.positions_view import render_positions_view
from src.nadobro.utils.x18 import to_x18


def _snapshot():
    return {
        "user_id": 42,
        "network": "testnet",
        "last_sync": "2026-01-01T00:00:00+00:00",
        "equity": {"spot": "1000", "cross": "500", "isolated": "0", "total": "1500"},
        "positions": [
            {
                "product_id": 1,
                "symbol": "BTC",
                "isolated": False,
                "is_long": True,
                "amount": "1",
                "notional_value": "100000",
                "avg_entry_price": "90000",
                "est_pnl": "100",
                "est_liq_price": None,
                "margin_used": "10000",
                "leverage": "10",
                "upnl_pct": "1",
            }
        ],
        "open_orders": [
            {"product_id": 1, "product_name": "BTC", "side": "LONG", "amount": "1", "price": "100000", "digest": "0xabc"}
        ],
        "matches": [
            {
                "submission_idx": "1",
                "product_id": 1,
                "product_name": "BTC",
                "base_filled": str(to_x18("1")),
                "quote_filled": str(to_x18("-100")),
                "fee": str(to_x18("1")),
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ],
        "stats": {
            "volume_windows": {"24h": "100", "7d": "200", "30d": "300", "all": "400"},
            "total_fees": "2",
            "total_funding": "-1",
            "total_pnl": "5",
            "total_trades": 2,
            "win_rate": "50",
        },
    }


def test_portfolio_deck_shows_trigger_order_type():
    snapshot = _snapshot()
    snapshot["open_orders"] = [
        {
            "product_id": 1,
            "product_name": "BTC",
            "side": "LONG",
            "price": "90000",
            "type": "STOP",
            "is_trigger": True,
            "digest": "0xtrg",
        }
    ]
    text, _ = render_portfolio_deck(snapshot)
    assert "⚡ STOP" in text


def test_portfolio_deck_and_subviews_render_without_local_sync_text():
    text, kb = render_portfolio_deck(_snapshot())
    assert "<b>Portfolio</b> · TESTNET" in text
    assert "local ledger" not in text
    assert "Total Balance" in text
    assert kb.inline_keyboard
    assert render_loading() == "⏳ Loading portfolio…"

    for renderer in (render_positions_view, render_orders_view):
        view_text, view_kb = renderer(_snapshot())
        assert "TESTNET" in view_text
        assert view_kb.inline_keyboard

    # History view pulls closed round-trips from the positions table; stub them
    # out for the smoke test so the renderer still produces a valid card.
    with patch("src.nadobro.models.database.get_manual_closed_round_trips", return_value=[]):
        view_text, view_kb = render_history_view(_snapshot())
    assert "TESTNET" in view_text
    assert view_kb.inline_keyboard


def test_history_renders_venue_windows_newest_first():
    """History lists the VENUE'S position windows (every source), newest last
    change first. Each row shows the venue entry -> exit -> realized PnL, and the
    Share PnL button carries the window id (``vp:``) so the card is minted from
    the venue figures. Open windows show as OPEN without a share button."""
    from datetime import datetime, timezone

    rows = [
        {
            "id": 200, "product_id": 1, "product_name": "NEW-PERP", "is_long": True, "isolated": False,
            "total_close_amount": "1", "max_amount": "1", "amount": "0",
            "avg_entry_price": "100", "avg_exit_price": "110", "realized_pnl": "10",
            "open_fee": "0.1", "close_fee": "0.1", "is_open": False,
            "open_ts": datetime(2026, 1, 2, tzinfo=timezone.utc),
            "update_ts": datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
            "strategy_session_id": 314, "session_strategy": "mid", "source": "strategy",
        },
        {
            "id": 100, "product_id": 2, "product_name": "OLD-PERP", "is_long": False, "isolated": True,
            "total_close_amount": "0.5", "max_amount": "0.5", "amount": "0",
            "avg_entry_price": "90", "avg_exit_price": "80", "realized_pnl": "5",
            "open_fee": "0.05", "close_fee": "0.05", "is_open": False,
            "open_ts": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "update_ts": datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            "strategy_session_id": None, "session_strategy": None, "source": "manual",
        },
        {
            "id": 300, "product_id": 3, "product_name": "LIVE-PERP", "is_long": True, "isolated": False,
            "total_close_amount": "0", "max_amount": "2", "amount": "2",
            "avg_entry_price": "50", "avg_exit_price": "0", "realized_pnl": "0",
            "open_fee": "0.2", "close_fee": "0", "is_open": True,
            "open_ts": datetime(2026, 1, 3, tzinfo=timezone.utc),
            "update_ts": datetime(2026, 1, 3, 1, tzinfo=timezone.utc),
            "strategy_session_id": 315, "session_strategy": "grid", "source": "strategy",
        },
    ]
    with patch("src.nadobro.models.database.get_venue_positions", return_value=rows):
        text, kb = render_history_view(_snapshot())

    assert text.index("NEW-PERP") < text.index("OLD-PERP")
    assert "$100.00 → $110.00" in text and "mid #314" in text
    assert "manual" in text and "iso" in text
    assert "OPEN" in text
    callback_data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "portfolio:share_pnl:vp:200" in callback_data
    assert "portfolio:share_pnl:vp:100" in callback_data
    assert not any(cb == "portfolio:share_pnl:vp:300" for cb in callback_data)   # open: no exit yet
    assert "Funding" not in text


def test_order_cancel_indices_follow_sorted_order():
    snapshot = _snapshot()
    snapshot["open_orders"] = [
        {"product_id": 1, "product_name": "OLD", "created_at": "2026-01-01T00:00:00Z", "digest": "0xold"},
        {"product_id": 1, "product_name": "NEW", "created_at": "2026-01-02T00:00:00Z", "digest": "0xnew"},
    ]

    text, kb = render_orders_view(snapshot)

    assert text.index("NEW") < text.index("OLD")
    # Digest-addressed cancel: immune to list reordering between render and tap.
    assert kb.inline_keyboard[0][0].callback_data == "portfolio:cancel_order:d:new"


def test_positions_cancel_indices_paginate_correctly():
    snapshot = _snapshot()
    snapshot["open_orders"] = [
        {
            "product_id": 1,
            "product_name": f"ORD{i}",
            "created_at": f"2026-01-{i:02d}T00:00:00Z",
            "digest": f"0x{i}",
        }
        for i in range(1, 8)
    ]

    _, kb = render_positions_view(snapshot, ord_page=1, page_size=6)

    cancel_callbacks = [
        btn.callback_data
        for row in kb.inline_keyboard
        for btn in row
        if btn.callback_data and btn.callback_data.startswith("portfolio:cancel_order:")
    ]
    # Sorted newest-first: ORD7..ORD1; ord page 1 (size 4) shows ORD3, ORD2,
    # ORD1 — addressed by digest, not list position.
    assert cancel_callbacks == [
        "portfolio:cancel_order:d:3",
        "portfolio:cancel_order:d:2",
        "portfolio:cancel_order:d:1",
    ]


def test_session_card_template_and_card_generation(tmp_path):
    template = ensure_session_card_template(tmp_path / "template.png")
    assert template.exists()
    out = generate_session_card(
        {
            "strategy_label": "Test Strategy",
            "realized_pnl": "12.5",
            "fees": "1",
            "funding": "-0.5",
            "volume": "1000",
            "win_count": 2,
            "loss_count": 1,
        },
        "testnet",
        tmp_path / "card.png",
    )
    assert Path(out).exists()
    assert Path(out).stat().st_size > 0


def test_session_card_cost_per_million_is_net_of_fees_per_million():
    """Cost/$1M = (realized PnL − fees) / volume × $1M — the net-of-fees result
    per $1M of volume, SIGNED by outcome, labelled 'Cost/$1M'."""
    from decimal import Decimal

    from src.nadobro.handlers.performance_view import _render_session_card
    from src.nadobro.utils.visual import signed_money

    # LOSING copy session (screenshot #6: gross −$3.94, fees $2.61 → net −$6.55).
    losing = {
        "id": 6, "strategy": "copy", "product_name": "Top trader 0x31b1…02e4",
        "status": "ended", "total_volume_usd": 6070.52, "total_fees_paid": 2.61,
        "total_funding_paid": 0.0, "realized_pnl": -3.94,
    }
    lines: list = []
    _render_session_card(lines, losing, 6)
    text = "\n".join(lines)
    expected = signed_money((Decimal("-3.94") - Decimal("2.61")) / Decimal("6070.52") * Decimal("1000000"))
    assert f"Cost/$1M {expected}" in text          # (pnl − fees)/vol×1M — negative on a loss
    assert "Net/$1M" not in text

    # WINNING session (screenshot #7: gross +$16.62, fees $5.10) → positive.
    winning = {**losing, "total_volume_usd": 11850.97, "total_fees_paid": 5.10,
               "realized_pnl": 16.62}
    lines2: list = []
    _render_session_card(lines2, winning, 7)
    exp2 = signed_money((Decimal("16.62") - Decimal("5.10")) / Decimal("11850.97") * Decimal("1000000"))
    assert f"Cost/$1M {exp2}" in "\n".join(lines2)
    assert exp2.startswith("+")                     # profit after fees → positive


def test_deck_upnl_dot_matches_sign():
    # Screenshot bug 2026-06-10: a green dot rendered next to uPnL -19.19.
    snapshot = _snapshot()
    snapshot["positions"][0]["est_pnl"] = "-19.19"
    text, _ = render_portfolio_deck(snapshot)
    assert "<b>Unrealized PnL</b>  🔴 -$19.19" in text
    snapshot["positions"][0]["est_pnl"] = "19.19"
    text, _ = render_portfolio_deck(snapshot)
    assert "<b>Unrealized PnL</b>  🟢 +$19.19" in text


def test_deck_has_no_funding_line():
    """Funding is not shown on the portfolio card (2026-09-16)."""
    snapshot = _snapshot()
    snapshot["stats"]["funding_windows"] = {"24h": "0.12"}
    text, _ = render_portfolio_deck(snapshot)
    assert "Funding" not in text


def test_deck_realized_line_is_venue_sourced_or_marked_syncing():
    """The Realized line shows the venue figure only once the venue position
    windows are synced; before that it says so instead of showing the old
    fill-replay guess."""
    snapshot = _snapshot()
    snapshot["stats"]["pnl_windows"] = {"24h": "-161.36"}
    snapshot["stats"].pop("realized_source", None)
    text, _ = render_portfolio_deck(snapshot)
    assert "Realized  — (syncing from Nado)" in text
    assert "-161.36" not in text
    snapshot["stats"]["realized_source"] = "venue"
    snapshot["stats"]["pnl_windows"] = {"24h": "10.50"}
    text, _ = render_portfolio_deck(snapshot)
    assert "Realized  +$10.50" in text


def test_deck_refreshing_banner():
    text, _ = render_portfolio_deck(_snapshot(), refreshing=True)
    assert "🔄 Refreshing" in text
    text, _ = render_portfolio_deck(_snapshot(), refreshing=False)
    assert "🔄 Refreshing" not in text


def test_deck_hides_pct_when_unknown():
    # upnl_pct=None must render no percentage, not a fake (+0.00%).
    snapshot = _snapshot()
    snapshot["positions"][0]["upnl_pct"] = None
    text, _ = render_portfolio_deck(snapshot)
    assert "(+0.00%)" not in text


def test_sync_resolves_placeholder_product_names():
    from unittest.mock import patch

    from src.nadobro.venue.nado_sync import _resolve_product_names

    positions = [{"product_id": 2, "symbol": "Product_2", "product_name": ""}]
    orders = [{"product_id": 4, "product_name": "Product_4"}]
    matches = [{"product_id": 2, "product_name": ""}]
    with patch(
        "src.nadobro.config.get_product_name",
        side_effect=lambda pid, network=None: {2: "BTC-PERP", 4: "ETH-PERP"}[int(pid)],
    ):
        _resolve_product_names(positions, orders, matches, "mainnet")
    assert positions[0]["symbol"] == "BTC-PERP"
    assert positions[0]["product_name"] == "BTC-PERP"
    assert orders[0]["product_name"] == "ETH-PERP"
    assert matches[0]["product_name"] == "BTC-PERP"
    # Real names are never overwritten.
    keep = [{"product_id": 2, "symbol": "KBTC", "product_name": "KBTC"}]
    _resolve_product_names(keep, [], [], "mainnet")
    assert keep[0]["symbol"] == "KBTC"


def test_unrealized_pnl_pct_cross_falls_back_to_margin_used():
    from decimal import Decimal

    from src.nadobro.quant.portfolio_calculator import unrealized_pnl_pct

    # SDK cross rows often omit leverage; margin_used must back the pct
    # instead of returning None (rendered as a fake 0.00%).
    value = unrealized_pnl_pct(
        est_pnl=Decimal("-19.19"),
        margin_used=Decimal("82"),
        notional_value=Decimal("164"),
        leverage=None,
        isolated=False,
    )
    assert value is not None and value < 0


def test_cancel_callback_falls_back_to_index_without_digest():
    from src.nadobro.handlers.orders_view import cancel_callback_for

    assert cancel_callback_for({"digest": "0xABCDEF1234567890ffff"}, 3) == (
        "portfolio:cancel_order:d:abcdef1234567890"
    )
    assert cancel_callback_for({"order_digest": "0x99"}, 3) == "portfolio:cancel_order:d:99"
    assert cancel_callback_for({}, 3) == "portfolio:cancel_order:3"
