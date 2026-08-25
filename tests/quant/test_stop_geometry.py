"""Pure invariants for the venue-side stop price geometry.

No DB or network — exercises ``src.nadobro.quant.stop_geometry`` directly. These
pin the mark price a venue reduce-only trigger fires at, derived from the user's
%-of-margin SL and leverage. Audit: VENUE-STOP.
"""
from __future__ import annotations

import pytest

from src.nadobro.quant.stop_geometry import (
    stop_close_is_buy,
    stop_loss_price,
    stop_trigger_is_below,
)


def test_long_stop_is_below_entry_by_the_leverage_scaled_move():
    # 10% of margin at 10x = a 1% adverse move. Long entry 100 -> stop 99.
    assert stop_loss_price(True, 100.0, 10.0, 10.0) == pytest.approx(99.0)


def test_short_stop_is_above_entry_by_the_leverage_scaled_move():
    # Short entry 100, 10% / 10x = 1% up -> stop 101.
    assert stop_loss_price(False, 100.0, 10.0, 10.0) == pytest.approx(101.0)


def test_higher_leverage_puts_the_stop_closer_to_entry():
    near = stop_loss_price(True, 100.0, 50.0, 10.0)   # 0.2% move -> 99.8
    far = stop_loss_price(True, 100.0, 10.0, 10.0)    # 1% move -> 99.0
    assert near == pytest.approx(99.8)
    assert far == pytest.approx(99.0)
    assert near > far                                 # tighter at higher leverage


@pytest.mark.parametrize("is_long,entry,lev,sl", [
    (True, 0.0, 10.0, 10.0),      # no entry
    (True, 100.0, 0.5, 10.0),     # leverage < 1
    (True, 100.0, 10.0, 0.0),     # disarmed sl
    (True, 100.0, 0.5, 10.0),
])
def test_bad_inputs_return_none(is_long, entry, lev, sl):
    assert stop_loss_price(is_long, entry, lev, sl) is None


def test_move_at_or_beyond_100pct_returns_none():
    # sl 200% at 1x = a 200% move -> degenerate, skip rather than a negative price.
    assert stop_loss_price(True, 100.0, 1.0, 200.0) is None


def test_trigger_side_and_close_direction_by_position_side():
    # Long: fires when mark falls below; closes with a SELL.
    assert stop_trigger_is_below(True) is True
    assert stop_close_is_buy(True) is False
    # Short: fires when mark rises above; closes with a BUY.
    assert stop_trigger_is_below(False) is False
    assert stop_close_is_buy(False) is True
