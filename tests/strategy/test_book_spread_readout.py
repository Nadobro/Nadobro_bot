"""The pre-trade card surfaces the LIVE orderbook spread vs the configured spread.

The core MM job is to rest near the touch and capture the spread; a quote parked
far beyond the book (e.g. 10 bp on a ~1 bp BTC book) rarely fills — the low-volume
failure. The card must show the live book spread (cache-only, non-blocking) so the
user can set a spread that actually rests near the touch, and warn when it doesn't.
"""
from __future__ import annotations

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.strategy import mm_dashboard as md  # noqa: E402
from src.nadobro.venue import market_feed as mf  # noqa: E402


def _seed_book(network="mainnet", product="BTC", bid=99.995, ask=100.005):
    mf._cache[network] = {product: {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}}


def test_cached_spread_bps_is_computed_from_the_book():
    _seed_book(bid=99.99, ask=100.01)  # 2 bp book
    assert abs(mf.cached_spread_bps("mainnet", "BTC") - 2.0) < 1e-6
    assert mf.cached_spread_bps("mainnet", "NOTCACHED") is None


def test_cached_spread_reads_are_cache_only_and_never_raise(monkeypatch):
    mf._cache.clear()
    assert mf.cached_spread_bps("mainnet", "BTC") is None  # empty cache -> None, no raise


def test_configured_spread_reads_per_strategy_key():
    assert md._configured_spread_bp("rgrid", {"rgrid_spread_bp": 7.0, "spread_bp": 5.0}) == 7.0
    assert md._configured_spread_bp("dgrid", {"dgrid_spread_bp": 6.0}) == 6.0
    assert md._configured_spread_bp("mid", {"spread_bp": 5.0}) == 5.0
    # rgrid falls back to the generic key the mapper also falls back to
    assert md._configured_spread_bp("rgrid", {"spread_bp": 4.0}) == 4.0


def test_wide_spread_on_a_tight_book_is_flagged_as_far_from_touch():
    rec = md._spread_recommendation(1.0, 10.0, maker_bp=1.0)  # 1bp book, 10bp setting
    assert rec["verdict"] == "far"
    assert rec["behind_touch_bp"] > 0
    assert rec["recommended_half_bp"] <= 2.0  # steer toward the touch


def test_touch_tight_spread_is_approved():
    rec = md._spread_recommendation(1.0, 0.5, maker_bp=1.0)
    assert rec["verdict"] in ("at_touch", "near_touch")


def test_recommendation_clears_the_maker_fee_floor():
    # Even on a ~0 bp book, do not recommend resting below the maker fee.
    rec = md._spread_recommendation(0.2, 5.0, maker_bp=1.0)
    assert rec["recommended_half_bp"] >= 1.0


def test_card_line_shows_book_vs_configured_spread():
    _seed_book(bid=99.995, ask=100.005)  # 1 bp book
    bd = md.build_pretrade_breakdown(
        strategy_id="rgrid",
        conf={"notional_usd": 100, "rgrid_spread_bp": 10.0, "rgrid_stop_loss_pct": 0.8},
        network="mainnet", product="BTC", leverage=1.0,
    )
    assert bd["book_spread_bp"] is not None
    assert bd["configured_spread_bp"] == 10.0
    lines = md.render_pretrade_card_lines(bd)
    book_line = next((ln for ln in lines if "Book spread now" in ln), None)
    assert book_line is not None
    assert "your spread: 10.0 bp" in book_line
    assert "BEHIND the touch" in book_line  # the low-volume warning fires
