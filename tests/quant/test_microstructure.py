"""Order-book microstructure math.

These pin the properties the Mid Mode v3 signal layer is built on. The
cross-weighting in the microprice is the one most worth guarding: getting it
backwards silently inverts every downstream signal while still producing a
plausible-looking number between the bid and the ask.

Pure: plain dicts in, floats out. The same book shape is emitted by both
``nado_client.get_market_liquidity`` and ``market_data.hl_ws.book``.
"""
import pytest

from src.nadobro.quant import microstructure as ms


def _book(bids, asks):
    return {"bids": [list(b) for b in bids], "asks": [list(a) for a in asks]}


BALANCED = _book([(100.0, 10.0)], [(101.0, 10.0)])


# --- l1 / mid / spread ------------------------------------------------------

def test_l1_and_mid():
    assert ms.l1(BALANCED) == (100.0, 10.0, 101.0, 10.0)
    assert ms.mid(BALANCED) == pytest.approx(100.5)


def test_spread_bp_is_relative_to_mid():
    assert ms.spread_bp(BALANCED) == pytest.approx((1.0 / 100.5) * 10_000)


@pytest.mark.parametrize("book", [
    None, {}, {"bids": [], "asks": []},
    {"bids": [(100.0, 1.0)], "asks": []},          # one-sided
    {"bids": [], "asks": [(101.0, 1.0)]},
    {"bids": [("x", "y")], "asks": [(101.0, 1.0)]},  # malformed
    {"bids": [(0.0, 1.0)], "asks": [(101.0, 1.0)]},  # zero price
])
def test_every_accessor_fails_open_on_a_degraded_book(book):
    # Everything that needs BOTH sides degrades to None rather than raising.
    assert ms.l1(book) is None
    assert ms.mid(book) is None
    assert ms.microprice(book) is None
    assert ms.spread_bp(book) is None
    assert ms.obi(book) is None
    assert ms.slippage_bp(book, ms.BUY, 100.0) is None


def test_depth_notional_is_defined_per_side_even_on_a_one_sided_book():
    # Unlike the two-sided accessors, depth on a side is meaningful whenever
    # that side has levels — it is anchored on its own touch, not the mid.
    one_sided = {"bids": [[100.0, 1.0]], "asks": []}
    assert ms.depth_notional(one_sided, ms.BUY, bp=10) == pytest.approx(100.0)
    assert ms.depth_notional(one_sided, ms.SELL, bp=10) == 0.0
    assert ms.depth_notional(None, ms.BUY, bp=10) == 0.0


# --- microprice: the sign convention ---------------------------------------

def test_microprice_equals_mid_on_a_balanced_book():
    assert ms.microprice(BALANCED) == pytest.approx(ms.mid(BALANCED))


def test_heavy_bid_size_pulls_fair_value_UP_toward_the_ask():
    # Buyers queued => the next trade more likely lifts the ask. This is the
    # cross-weighting; inverting it would push fair value DOWN here.
    book = _book([(100.0, 90.0)], [(101.0, 10.0)])
    mp = ms.microprice(book)
    assert mp > ms.mid(book)
    assert mp == pytest.approx((100.0 * 10.0 + 101.0 * 90.0) / 100.0)


def test_heavy_ask_size_pulls_fair_value_DOWN_toward_the_bid():
    book = _book([(100.0, 10.0)], [(101.0, 90.0)])
    assert ms.microprice(book) < ms.mid(book)


def test_microprice_limits_to_the_bid_when_there_is_no_bid_size():
    # With nothing bid, the book is all offer and fair value is the bid.
    book = {"bids": [[100.0, 1e-9]], "asks": [[101.0, 10.0]]}
    assert ms.microprice(book) == pytest.approx(100.0, abs=1e-6)


def test_microprice_stays_within_the_touch():
    for bs, asz in ((1.0, 99.0), (99.0, 1.0), (50.0, 50.0)):
        book = _book([(100.0, bs)], [(101.0, asz)])
        assert 100.0 <= ms.microprice(book) <= 101.0


def test_microprice_can_aggregate_several_levels():
    book = _book([(100.0, 1.0), (99.0, 50.0)], [(101.0, 1.0), (102.0, 1.0)])
    shallow = ms.microprice(book, levels=1)
    deep = ms.microprice(book, levels=2)
    assert shallow == pytest.approx(ms.mid(book))   # 1 vs 1 at the touch
    assert deep > shallow                            # depth is bid-heavy


# --- imbalance --------------------------------------------------------------

def test_obi_is_already_bounded_and_signed():
    assert ms.obi(_book([(100.0, 30.0)], [(101.0, 10.0)])) == pytest.approx(0.5)
    assert ms.obi(_book([(100.0, 10.0)], [(101.0, 30.0)])) == pytest.approx(-0.5)
    assert ms.obi(BALANCED) == pytest.approx(0.0)


def test_obi_bands_can_disagree_across_depth():
    # Thin at the touch, heavy underneath — the disagreement is the signal.
    book = _book([(100.0, 1.0), (99.0, 99.0)], [(101.0, 1.0), (102.0, 1.0)])
    bands = ms.obi_bands(book, bands=(1, 2))
    assert bands[1] == pytest.approx(0.0)
    assert bands[2] > 0.9


# --- depth / slippage -------------------------------------------------------

def test_depth_notional_counts_only_levels_within_the_band():
    book = _book([(100.0, 1.0), (99.9, 1.0), (90.0, 100.0)], [(101.0, 1.0)])
    # 90.0 is ~10% below the touch, far outside 20bp, so it must not count.
    within = ms.depth_notional(book, ms.BUY, bp=20)
    assert within == pytest.approx(100.0 * 1.0 + 99.9 * 1.0)


def test_depth_notional_is_measured_from_the_touch_not_the_mid():
    # Regression: anchoring the band on the mid returned 0 whenever the spread
    # exceeded the band — i.e. on every wide market, which is exactly where
    # sizing against visible depth matters most.
    wide = _book([(100.0, 1.0), (99.99, 1.0)], [(110.0, 1.0)])   # ~950bp spread
    assert ms.depth_notional(wide, ms.BUY, bp=20) > 0
    assert ms.depth_notional(wide, ms.SELL, bp=20) == pytest.approx(110.0)


def test_slippage_grows_with_size_and_is_none_when_the_book_cannot_absorb():
    book = _book([(100.0, 10.0)], [(101.0, 1.0), (102.0, 1.0), (103.0, 1.0)])
    small = ms.slippage_bp(book, ms.BUY, notional=101.0)     # fills at the touch
    large = ms.slippage_bp(book, ms.BUY, notional=300.0)     # walks three levels
    assert large > small > 0
    assert ms.slippage_bp(book, ms.BUY, notional=10_000.0) is None


def test_buy_walks_the_asks_and_sell_walks_the_bids():
    book = _book([(100.0, 1.0), (99.0, 1.0)], [(101.0, 1.0), (102.0, 1.0)])
    assert ms.slippage_bp(book, ms.BUY, 200.0) is not None
    assert ms.slippage_bp(book, ms.SELL, 190.0) is not None


# --- stale-feed detection ---------------------------------------------------

def test_book_hash_is_stable_and_changes_with_the_book():
    a = ms.book_hash(BALANCED)
    assert a == ms.book_hash(_book([(100.0, 10.0)], [(101.0, 10.0)]))
    assert a != ms.book_hash(_book([(100.0, 11.0)], [(101.0, 10.0)]))
