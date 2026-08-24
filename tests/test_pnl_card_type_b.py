"""Type B PnL card (strategy sessions: Grid/RGrid/DGrid/Mid/Volume/DN) —
renderer, data builder, and per-user scoping.

Pins: the card renders both variants; each card maps ONLY that session's stats;
every DB query is scoped to the requesting user (never leaks another user's
session); DN folds net funding into a fee-net PnL; fees render as a plain
magnitude; product/strategy map to the friendly badge + short Mode word.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.portfolio import pnl_card_builder as bld
from src.nadobro.portfolio.pnl_card_type_b import (
    _BADGE_LABEL,
    _MODE_LABEL,
    _strategy_key,
    generate_type_b_card,
)


def _png_ok(b: bytes) -> bool:
    return isinstance(b, bytes) and len(b) > 1000 and b[:8] == b"\x89PNG\r\n\x1a\n"


# ── fonts ───────────────────────────────────────────────────────

def test_space_grotesk_weights_are_bundled():
    """The card's display face is Space Grotesk — the static weights must be
    committed so CI/prod render the master's numerals (not the Poppins
    fallback)."""
    from pathlib import Path

    fonts = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    for weight in ("Regular", "Medium", "SemiBold", "Bold"):
        f = fonts / f"SpaceGrotesk-{weight}.ttf"
        assert f.exists() and f.stat().st_size > 10_000, f"missing font: {f}"


def test_official_wordmark_asset_is_bundled():
    """The header uses the real NADOBRO wordmark (dark-bg variant); it must be
    committed so the card renders the brand logotype, not the typeset
    fallback."""
    from pathlib import Path

    wm = Path(__file__).resolve().parents[1] / "assets" / "logos" / "nadobro_wordmark_white.png"
    assert wm.exists() and wm.stat().st_size > 2_000, f"missing wordmark: {wm}"


# The Type A/B renderers paint every dynamic stat at hard-coded ABSOLUTE pixel
# coordinates (referral line at y=888, Mode at y=855, ...) calibrated to the
# master's 1672×941 canvas. There is no size/aspect guard in the renderer, so a
# wrong-size background silently mispositions text and clips the bottom
# referral/mode lines off a shorter canvas — it renders a broken card with no
# error. This bit prod once: commit 5a058d0 swapped all four live backgrounds
# for 1600×900 placeholders. Pin the canvas size so an asset refresh that
# changes it fails CI loudly instead of shipping a broken card.
_CARD_CANVAS = (1672, 941)
# Load-bearing: the four backgrounds the renderers actually open + draw onto.
_BACKGROUND_ASSETS = (
    "Background positive pnl.png",
    "Background negative pnl.png",
    "Background Positive PnL Type B.png",
    "Background Negative PnL Type B.png",
)
# Design references the backgrounds are derived from; size drift here flags a
# future refresh mistake before it reaches the live backgrounds.
_MASTER_ASSETS = (
    "Master positive pnl.png",
    "Master negative pnl.png",
    "Master Positive PnL Type B.png",
    "Master Negative PnL Type B.png",
)


def test_card_backgrounds_are_master_canvas_size():
    """Every live PnL-card background (Type A + Type B) must be exactly
    1672×941 so the renderers' hard-coded text coordinates land correctly."""
    from pathlib import Path

    from PIL import Image

    cards = Path(__file__).resolve().parents[1] / "assets" / "cards"
    for name in _BACKGROUND_ASSETS + _MASTER_ASSETS:
        path = cards / name
        assert path.exists(), f"missing card asset: {path}"
        with Image.open(path) as img:
            assert img.size == _CARD_CANVAS, (
                f"{name} is {img.size}, expected {_CARD_CANVAS} — the renderer "
                f"draws stats at fixed coords for the master canvas; a resized "
                f"background silently breaks the card."
            )


# ── renderer ────────────────────────────────────────────────────

def test_renderer_produces_both_variants():
    base = {
        "strategy": "dgrid", "product": "BTC:PERP-USDC", "base_symbol": "BTC",
        "volume": 35076.68, "net_fees": 3.51, "pnl": 52.07, "referral_code": "3UOEDJUW",
    }
    assert _png_ok(generate_type_b_card(base))                       # positive/trophy
    assert _png_ok(generate_type_b_card({**base, "pnl": -52.07}))    # negative/miner


def test_renderer_all_strategies_and_labels():
    for key in ("grid", "rgrid", "dgrid", "mid", "vol", "dn"):
        data = {
            "strategy": key, "product": "ETH:PERP-USDC", "base_symbol": "ETH",
            "volume": 1000.0, "net_fees": 1.0, "pnl": 5.0, "referral_code": "X",
        }
        assert _png_ok(generate_type_b_card(data))
    # Friendly badge + short Mode word are distinct per strategy.
    assert _BADGE_LABEL["dgrid"] == "DGrid Strategy" and _MODE_LABEL["dgrid"] == "DGrid"
    assert _BADGE_LABEL["mid"] == "Mid Mode" and _MODE_LABEL["mid"] == "Mid"
    assert _BADGE_LABEL["vol"] == "Vol Bot" and _MODE_LABEL["vol"] == "Volume"
    assert _BADGE_LABEL["dn"] == "Delta Neutral"


def test_renderer_tolerates_multi_unknown_icon_and_no_referral():
    # MULTI (multi-product volume run) → no token icon, still renders.
    assert _png_ok(generate_type_b_card({
        "strategy": "vol", "product": "MULTI", "base_symbol": "",
        "volume": 50000.0, "net_fees": 10.0, "pnl": -5.0, "referral_code": "",
    }))
    # Unknown icon symbol + zeros + no referral → still renders.
    assert _png_ok(generate_type_b_card({
        "strategy": "grid", "product": "WIF:PERP-USDC", "base_symbol": "WIF",
        "volume": 0, "net_fees": 0, "pnl": 0, "referral_code": "",
    }))


def test_renderer_big_volume_uses_million_shorthand():
    # Large volumes must not overflow into the mascot — M-shorthand keeps width.
    assert _png_ok(generate_type_b_card({
        "strategy": "vol", "product": "ETH:PERP-USDC", "base_symbol": "ETH",
        "volume": 1234567.89, "net_fees": 92.4, "pnl": 12482.66, "referral_code": "NBVOL03",
    }))


def test_strategy_key_normalizes_aliases():
    assert _strategy_key("mm") == "mid"
    assert _strategy_key("market maker") == "mid"
    assert _strategy_key("volume_bot") == "vol"
    assert _strategy_key("delta_neutral") == "dn"
    assert _strategy_key("d-grid") == "dgrid"
    assert _strategy_key("reverse grid") == "rgrid"
    assert _strategy_key("totally-unknown") == "grid"   # safe default


# ── product mapping ─────────────────────────────────────────────

def test_type_b_product_perp_spot_multi_and_aliases():
    assert bld._type_b_product("BTC-PERP") == ("BTC", "BTC:PERP-USDC")
    assert bld._type_b_product("ETH:PERP-USDC") == ("ETH", "ETH:PERP-USDC")
    assert bld._type_b_product("MULTI") == ("", "MULTI")
    assert bld._type_b_product(None) == ("BTC", "BTC:PERP-USDC")
    # Bare tickers mean the perp (grid/mid/etc. trade perps).
    assert bld._type_b_product("ETH") == ("ETH", "ETH:PERP-USDC")
    # Spot symbols/pairs display as-is but map to the perp icon.
    assert bld._type_b_product("KBTC") == ("BTC", "KBTC")
    assert bld._type_b_product("WETH") == ("ETH", "WETH")
    assert bld._type_b_product("ETH-USDC") == ("ETH", "ETH-USDC")


# ── builder: per-user scoping (security) ────────────────────────

def test_latest_session_query_scoped_to_user_and_network():
    captured: list[tuple] = []

    def fake_query_one(sql: str, params=None):
        captured.append((sql, params))
        return None

    with patch.object(bld, "query_one", side_effect=fake_query_one):
        bld.build_type_b_card_data(telegram_id=42, network="mainnet")

    # No session found → two queries: latest session + referral code.
    assert len(captured) == 2
    session_sql, session_params = captured[0]
    assert "strategy_sessions" in session_sql
    assert "user_id = %s" in session_sql and "network = %s" in session_sql
    assert session_params[0] == 42 and session_params[1] == "mainnet"
    referral_sql, referral_params = captured[1]
    assert "invite_codes" in referral_sql
    assert referral_params[0] == 42


def test_specific_session_query_requires_user_id():
    captured: list[tuple] = []

    def fake_query_one(sql: str, params=None):
        captured.append((sql, params))
        return None

    with patch.object(bld, "query_one", side_effect=fake_query_one):
        bld.build_type_b_card_data(telegram_id=42, network="mainnet", session_id=999)

    session_sql, params = captured[0]
    # A forged session_id must still be gated by user_id + network.
    assert "id = %s" in session_sql and "user_id = %s" in session_sql
    assert 999 in params and 42 in params and "mainnet" in params


# ── builder: mapping + numbers ──────────────────────────────────

def test_builder_maps_session_to_raw_floats():
    session = {
        "id": 1, "user_id": 42, "network": "mainnet", "strategy": "dgrid",
        "product_name": "BTC-PERP", "total_volume_usd": Decimal("35076.68"),
        "realized_pnl": Decimal("52.07"), "total_fees_paid": Decimal("3.51"),
    }
    calls = {"n": 0}

    def stub(sql, params=None):
        calls["n"] += 1
        return session if calls["n"] == 1 else {"public_code": "3UOEDJUW"}

    with patch.object(bld, "query_one", side_effect=stub):
        d = bld.build_type_b_card_data(42, "mainnet", session_id=1)

    assert d["strategy"] == "dgrid"
    assert d["product"] == "BTC:PERP-USDC" and d["base_symbol"] == "BTC"
    assert d["volume"] == 35076.68
    assert d["net_fees"] == 3.51        # plain magnitude, no sign
    assert d["pnl"] == 52.07
    assert d["referral_code"] == "3UOEDJUW"


def test_builder_fees_magnitude_even_if_stored_negative():
    session = {
        "user_id": 42, "network": "mainnet", "strategy": "grid",
        "product_name": "ETH", "total_volume_usd": Decimal("500"),
        "realized_pnl": Decimal("-99.99"), "total_fees_paid": Decimal("-3.21"),
    }
    with patch.object(bld, "query_one", side_effect=[session, None]):
        d = bld.build_type_b_card_data(42, "mainnet")
    assert d["net_fees"] == 3.21        # abs() of a negative stored fee
    assert d["pnl"] == -99.99
    assert d["product"] == "ETH:PERP-USDC"


def test_builder_dn_folds_funding_net_of_fees():
    """DN PnL = realized + funding_received - fees (DN-PNL-FEES). Funding comes
    from the synced funding feed, signed received-positive."""
    session = {
        "id": 5, "user_id": 42, "network": "testnet", "strategy": "delta_neutral",
        "product_name": "QQQ", "product_id": 7, "started_at": "2026-06-01T00:00:00Z",
        "stopped_at": None, "total_volume_usd": Decimal("1000"),
        "realized_pnl": Decimal("0.50"), "total_fees_paid": Decimal("0.20"),
    }
    funding_row = {"paid_x18": Decimal("-3000000000000000000")}  # -3e18 => received 3.0
    calls = {"n": 0}

    def stub(sql, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return session
        if calls["n"] == 2:
            assert "funding_payments_testnet" in sql
            return funding_row
        return None  # referral

    with patch.object(bld, "query_one", side_effect=stub):
        d = bld.build_type_b_card_data(42, "testnet", session_id=5)

    # 0.50 realized + 3.00 funding - 0.20 fees = 3.30
    assert abs(d["pnl"] - 3.30) < 1e-6
    assert d["net_fees"] == 0.20
    assert calls["n"] == 3               # session + funding + referral


def test_builder_no_session_returns_zero_state():
    with patch.object(bld, "query_one", side_effect=[None, None]):
        d = bld.build_type_b_card_data(42, "mainnet")
    assert d["strategy"] == "grid"
    assert d["product"] == "BTC:PERP-USDC" and d["base_symbol"] == "BTC"
    assert d["volume"] == 0.0 and d["pnl"] == 0.0 and d["net_fees"] == 0.0
    assert d["referral_code"] == ""
    # The zero-state dict still renders a valid card.
    assert _png_ok(generate_type_b_card(d))
