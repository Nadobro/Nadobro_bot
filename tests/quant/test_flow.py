"""Order-flow signals: book-change imbalance and the trade tape.

Two things carry real risk and are pinned hardest:

* OFI's sign — buying pressure must read positive, and the touch-move
  indicators must stay separable from the size term;
* the trade ``side`` convention, which is UNVERIFIED against a live HL socket.
  Nothing may hardcode a guess: unclassifiable trades abstain, and
  ``sign_agreement`` is the production check that catches an inverted mapping
  before it trades backwards.
"""
import pytest

from src.nadobro.quant import flow


def _book(bid, bid_sz, ask, ask_sz):
    return {"bids": [[bid, bid_sz]], "asks": [[ask, ask_sz]]}


def _t(px=100.0, sz=1.0, side="B", ts=0.0, h="0x1"):
    return {"px": px, "sz": sz, "side": side, "ts": ts, "hash": h}


# --- OFI --------------------------------------------------------------------

def test_size_added_to_the_bid_is_positive_flow():
    prev = _book(100.0, 1.0, 101.0, 1.0)
    curr = _book(100.0, 5.0, 101.0, 1.0)
    d = flow.ofi(prev, curr)
    assert d.ofi > 0 and d.touch_moved == 0


def test_size_added_to_the_ask_is_negative_flow():
    prev = _book(100.0, 1.0, 101.0, 1.0)
    curr = _book(100.0, 1.0, 101.0, 5.0)
    assert flow.ofi(prev, curr).ofi < 0


def test_the_bid_lifting_is_positive_flow():
    prev = _book(100.0, 1.0, 101.0, 1.0)
    curr = _book(100.5, 1.0, 101.0, 1.0)
    d = flow.ofi(prev, curr)
    assert d.ofi > 0
    assert d.bid_price_moved == 1


def test_the_bid_pulling_is_negative_flow():
    prev = _book(100.0, 1.0, 101.0, 1.0)
    curr = _book(99.5, 1.0, 101.0, 1.0)
    d = flow.ofi(prev, curr)
    assert d.ofi < 0
    assert d.bid_price_moved == -1


def test_touch_move_is_reported_separately_from_size():
    # A whole-book shift up: both sides moved, so touch_moved is +1 and the
    # caller can distinguish "price moved" from "size arrived".
    prev = _book(100.0, 1.0, 101.0, 1.0)
    curr = _book(100.5, 1.0, 101.5, 1.0)
    d = flow.ofi(prev, curr)
    assert d.touch_moved == 1
    assert d.bid_price_moved == 1 and d.ask_price_moved == 1


def test_an_unchanged_book_produces_no_net_flow():
    b = _book(100.0, 1.0, 101.0, 1.0)
    assert flow.ofi(b, b).ofi == pytest.approx(0.0)


@pytest.mark.parametrize("bad", [None, {}, {"bids": [], "asks": [[101.0, 1.0]]}])
def test_ofi_needs_two_sided_books(bad):
    assert flow.ofi(bad, _book(100.0, 1.0, 101.0, 1.0)) is None
    assert flow.ofi(_book(100.0, 1.0, 101.0, 1.0), bad) is None


def test_normalisation_is_bounded_and_self_scaling():
    prev = _book(100.0, 1.0, 101.0, 1.0)
    curr = _book(100.0, 100.0, 101.0, 1.0)
    d = flow.ofi(prev, curr)
    assert 0 < flow.ofi_normalized(d, ref_size=10.0) <= 1.0
    assert flow.ofi_normalized(d, ref_size=0.0) is None
    assert flow.ofi_normalized(None, ref_size=10.0) is None


# --- dedupe -----------------------------------------------------------------

def test_replayed_trades_are_dropped_in_order():
    a, b = _t(h="0xa", ts=1.0), _t(h="0xb", ts=2.0)
    assert flow.dedupe_trades([a, b, a, b]) == [a, b]


def test_dedupe_keeps_genuinely_distinct_trades():
    rows = [_t(h="0xa", ts=1.0), _t(h="0xa", ts=1.0, sz=2.0)]
    assert len(flow.dedupe_trades(rows)) == 2


# --- the side convention ----------------------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("B", True), ("BUY", True), ("b", True),
    ("A", False), ("S", False), ("SELL", False),
    ("", None), ("weird", None), (None, None),
])
def test_side_mapping_abstains_rather_than_guessing(token, expected):
    assert flow.hl_side_is_buy({"side": token}) is expected


def test_unclassifiable_trades_are_excluded_not_assumed():
    trades = [_t(side="?"), _t(side="?")]
    assert flow.signed_volume(trades) is None       # nothing classified => abstain
    mixed = [_t(side="B", sz=3.0), _t(side="?", sz=99.0)]
    assert flow.signed_volume(mixed) == pytest.approx(3.0)   # the junk is ignored


def test_signed_volume_nets_buys_against_sells():
    trades = [_t(side="B", sz=3.0), _t(side="A", sz=1.0)]
    assert flow.signed_volume(trades) == pytest.approx(2.0)


def test_a_custom_predicate_can_override_the_convention():
    # If production measurement shows the mapping is inverted, the caller
    # supplies its own predicate rather than editing this module.
    inverted = lambda t: not flow.hl_side_is_buy(t)      # noqa: E731
    trades = [_t(side="B", sz=3.0)]
    assert flow.signed_volume(trades, side_is_buy=inverted) == pytest.approx(-3.0)


# --- windows ----------------------------------------------------------------

def test_trade_imbalance_is_bounded_and_windowed():
    trades = [_t(side="B", sz=3.0, ts=99.0), _t(side="A", sz=1.0, ts=99.5),
              _t(side="A", sz=50.0, ts=10.0)]          # far outside the window
    out = flow.trade_imbalance(trades, now=100.0, windows=(5.0,))
    assert out[5.0] == pytest.approx(0.5)              # (3-1)/4, old trade ignored


def test_short_windows_are_available_because_the_tape_is_pushed():
    trades = [_t(side="B", sz=1.0, ts=99.8)]
    out = flow.trade_imbalance(trades, now=100.0, windows=(1.0, 5.0))
    assert out[1.0] == pytest.approx(1.0)
    assert out[5.0] == pytest.approx(1.0)


def test_an_empty_window_is_none_not_zero():
    # Zero would read as "balanced flow"; None reads as "no information".
    out = flow.trade_imbalance([], now=100.0, windows=(5.0,))
    assert out[5.0] is None


def test_vwap_and_intensity():
    trades = [_t(px=100.0, sz=1.0, ts=99.0), _t(px=102.0, sz=3.0, ts=99.5)]
    assert flow.vwap(trades, now=100.0, window_s=5.0) == pytest.approx(101.5)
    assert flow.trade_intensity(trades, now=100.0, window_s=5.0) == pytest.approx(0.4)
    assert flow.vwap([], now=100.0, window_s=5.0) is None


# --- the production self-check ---------------------------------------------

def test_sign_agreement_detects_a_correct_convention():
    # Net buying accompanied rising prices every period.
    assert flow.sign_agreement([1, 2, 3], [0.1, 0.2, 0.05]) == pytest.approx(1.0)


def test_sign_agreement_detects_an_INVERTED_convention():
    # This is the failure the check exists to catch: the mapping is backwards,
    # and the caller must disable the signal rather than trade on it.
    score = flow.sign_agreement([1, 2, 3], [-0.1, -0.2, -0.05])
    assert score == pytest.approx(0.0)
    assert score < 0.5


def test_sign_agreement_abstains_without_decisive_data():
    assert flow.sign_agreement([], []) is None
    assert flow.sign_agreement([0, 0], [0.1, 0.2]) is None
