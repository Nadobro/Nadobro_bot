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


def test_tight_book_is_flagged_as_spread_capture_NOT_viable():
    # 1 bp book << ~5 bp maker round trip: capturing the spread is a structural loss.
    rec = md._spread_recommendation(1.0, 10.0)
    assert rec["spread_capture_viable"] is False
    # Do NOT steer to the touch; the nearest fee-positive half is the RT breakeven.
    assert rec["recommended_half_bp"] >= rec["breakeven_half_bp"] - 1e-9
    assert rec["breakeven_half_bp"] >= 2.0  # ~2.5 bp/side to clear the ~5 bp round trip


def test_wide_book_alt_is_spread_capture_viable():
    # A thin alt whose book spread exceeds the maker round trip: capture clears fees.
    rec = md._spread_recommendation(8.0, 8.0)  # 8 bp book > ~5 bp RT
    assert rec["spread_capture_viable"] is True
    assert abs(rec["recommended_half_bp"] - 4.0) < 0.5  # join near the 4 bp half-touch


def test_recommendation_never_below_the_round_trip_breakeven_on_a_tight_book():
    rec = md._spread_recommendation(0.2, 5.0)
    assert rec["recommended_half_bp"] >= rec["breakeven_half_bp"] - 1e-9


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
    # On a ~1 bp book the card must warn that spread capture LOSES (5 bp round trip),
    # not tell the user to quote at the touch.
    warn = next((ln for ln in lines if "Spread capture LOSES" in ln), None)
    assert warn is not None
    assert "DIRECTIONAL/volume play" in warn
