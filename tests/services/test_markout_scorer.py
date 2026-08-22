"""Mark-out grading job — the pure parts.

No DB and no network: ``grade_fill`` takes a row and a close series, and the
helpers are pure. The grading query itself is exercised by the DB suite.

The properties worth pinning are the ones that keep the ledger honest: a fill
is graded only when the reference genuinely reached its horizon, the fee used
is the real one when the fill records it, and an unmappable product yields no
grade rather than a wrong one.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.nadobro.quant import markout as mk
from src.nadobro.trading import markout_scorer as sc


def _series(start_ts, closes, step=60.0):
    return [(start_ts + i * step, c) for i, c in enumerate(closes)]


def _row(**kw):
    base = {
        "id": 1,
        "user_id": 42,
        "product_name": "BTC-PERP",
        "side": "BUY",
        "fill_price": 100.0,
        "price": 100.0,
        "fill_size": 1.0,
        "size": 1.0,
        "fill_fee": 0.0,
        "builder_fee": 0.0,
        "is_taker": False,
        "strategy_session_id": 7,
        "ts_fill": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    base.update(kw)
    return base


# --- product mapping --------------------------------------------------------

@pytest.mark.parametrize("product,coin", [
    ("BTC-PERP", "BTC"), ("eth-perp", "ETH"), ("SOL", "SOL"),
    ("BTC-USD", "BTC"), ("", ""),
])
def test_product_maps_to_the_hl_coin(product, coin):
    assert sc._coin_for(product) == coin


def test_a_wrapped_equity_does_not_masquerade_as_a_crypto_coin():
    # HL does not list Nado's RWA markets. The mapping must not "helpfully"
    # strip a wrapper into something HL happens to list.
    assert sc._coin_for("wGOOGLx") == "WGOOGLX"


# --- side ------------------------------------------------------------------

def test_side_sign():
    assert sc._side_sign("BUY") == mk.BUY
    assert sc._side_sign("sell") == mk.SELL
    assert sc._side_sign("SHORT") == mk.SELL
    assert sc._side_sign(None) == mk.BUY


# --- fees -------------------------------------------------------------------

def test_uses_the_recorded_fee_as_a_round_trip():
    # 0.05 quote on 100 notional = 5bp one leg => 10bp round trip.
    row = _row(fill_fee=0.05, fill_price=100.0, fill_size=1.0)
    assert sc._fee_bp(row) == pytest.approx(10.0)


def test_builder_fee_is_included():
    row = _row(fill_fee=0.05, builder_fee=0.01, fill_price=100.0, fill_size=1.0)
    assert sc._fee_bp(row) == pytest.approx(12.0)


def test_falls_back_to_the_maker_round_trip_when_no_fee_recorded():
    assert sc._fee_bp(_row(fill_fee=0.0)) == pytest.approx(sc._DEFAULT_FEE_BP)
    # Over-charging the fee can only make mark-out look worse, never better,
    # so the fallback can never manufacture a false all-clear.
    assert sc._DEFAULT_FEE_BP > 0


# --- grading ----------------------------------------------------------------

def test_grades_both_candle_horizons_when_the_series_reaches_them():
    t0 = _row()["ts_fill"].timestamp()
    # closes at t0, +60s, +120s ... +300s
    series = _series(t0, [100.0, 101.0, 101.0, 101.0, 101.0, 102.0])
    samples = sc.grade_fill(_row(), series)
    assert {s.horizon_nominal_s for s in samples} == {60.0, 300.0}
    by_h = {s.horizon_nominal_s: s for s in samples}
    assert by_h[60.0].markout_bp == pytest.approx(100.0)    # 100 -> 101 on a buy
    assert by_h[300.0].markout_bp == pytest.approx(200.0)   # 100 -> 102
    assert all(s.ref_source == mk.REF_CANDLE_1M for s in samples)


def test_a_sell_fill_grades_with_the_opposite_sign():
    t0 = _row()["ts_fill"].timestamp()
    series = _series(t0, [100.0, 99.0, 99.0, 99.0, 99.0, 99.0])
    samples = sc.grade_fill(_row(side="SELL"), series)
    assert samples and all(s.markout_bp > 0 for s in samples)  # price fell: good


def test_no_grade_when_the_horizon_has_not_elapsed():
    t0 = _row()["ts_fill"].timestamp()
    series = _series(t0, [100.0, 101.0])          # only reaches +60s
    samples = sc.grade_fill(_row(), series)
    assert {s.horizon_nominal_s for s in samples} == {60.0}   # 300s not graded


def test_no_grade_at_all_without_a_series():
    assert sc.grade_fill(_row(), []) == []


def test_a_degenerate_fill_price_is_never_graded():
    t0 = _row()["ts_fill"].timestamp()
    series = _series(t0, [100.0] * 6)
    assert sc.grade_fill(_row(fill_price=0.0, price=0.0), series) == []


def test_net_markout_reflects_the_fill_s_own_fee():
    t0 = _row()["ts_fill"].timestamp()
    series = _series(t0, [100.0, 101.0, 101.0, 101.0, 101.0, 101.0])
    row = _row(fill_fee=0.05, fill_price=100.0, fill_size=1.0)   # 10bp round trip
    sample = next(s for s in sc.grade_fill(row, series) if s.horizon_nominal_s == 60.0)
    assert sample.markout_bp == pytest.approx(100.0)
    assert sample.net_markout_bp == pytest.approx(90.0)


# --- query bounds -----------------------------------------------------------

def test_lookback_is_derived_from_the_candle_reach_not_asserted():
    # Selecting fills older than the fetch can cover would re-scan them every
    # pass, fail to anchor, and grade nothing — a permanent no-op loop.
    reach = sc._TF_SECONDS * sc._CANDLE_LIMIT
    assert 0 < sc.lookback_seconds() < reach


def test_invalid_network_is_rejected_rather_than_interpolated():
    with pytest.raises(ValueError):
        sc._trades_table("'; DROP TABLE trades_mainnet; --")
