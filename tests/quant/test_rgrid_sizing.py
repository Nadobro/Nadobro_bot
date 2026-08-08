"""R-Grid step sizing against the stop budget — pure math.

R-Grid crosses the spread on both legs and trades ``margin x leverage / levels``
per break, while the session stop is a % of MARGIN judged net of fees. Leverage
therefore buys size but not stop budget, and past a point one round trip costs
more than the whole stop: the session closes on the first entry+exit whichever way
price went. These pin the cap that prevents it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.nadobro.quant.rgrid_sizing import (
    DEFAULT_MAX_FEE_SHARE,
    TAKER_ROUND_TRIP_RATE,
    max_step_for_stop_budget,
    resolve_step_quote,
    taker_round_trip_cost,
)


def test_the_round_trip_rate_is_the_all_in_taker_charged_twice():
    # Catalog base 3.3bp + 1bp builder = 4.3bp all-in, crossed on both legs.
    assert TAKER_ROUND_TRIP_RATE == Decimal("0.00086")
    assert taker_round_trip_cost(1225) == Decimal("1225") * TAKER_ROUND_TRIP_RATE


def test_the_cap_leaves_room_for_several_round_trips():
    cap = max_step_for_stop_budget(Decimal("0.80"))
    assert cap is not None
    fee = taker_round_trip_cost(cap)
    assert fee <= Decimal("0.80") * DEFAULT_MAX_FEE_SHARE + Decimal("0.0001")
    assert Decimal("0.80") / fee >= 3


def test_no_budget_means_no_cap():
    assert max_step_for_stop_budget(0) is None
    assert max_step_for_stop_budget(None) is None
    assert max_step_for_stop_budget(-5) is None


def test_the_reported_session_is_capped():
    """$100 margin at 49x over 4 levels wants a $1,225 step — a $4,900 PYRAMID
    whose round trip is $4.21, more than five times the whole $0.80 stop.

    The bound is on the pyramid, so this configuration cannot be traded at any
    placeable size: the cap lands on the venue floor and FLAGS it (``floored``),
    which the card renders as "🚨 Stop too tight to trade". That is the honest
    answer — the previous per-step bound certified ~3 round trips of headroom
    that did not exist."""
    plan = resolve_step_quote(
        deployed_quote=Decimal(4900), levels=4,
        stop_budget_usd=Decimal("0.80"), min_step_usd=100,
    )
    assert plan.uncapped == Decimal(1225)
    assert plan.capped is True and plan.floored is True
    assert plan.step == Decimal(100)
    # The reported cost is the PYRAMID's, so the card cannot overstate the room:
    # even at the floored $100 step the 4-level pyramid's round trip is $0.344,
    # 43% of the $0.80 stop — past the 33% share, which is WHY it floored.
    assert plan.round_trip_cost == Decimal("0.34400")
    assert plan.round_trips_in_budget < 3, (
        "floored means the cap could not reach its round-trip target"
    )


def test_the_users_example_is_capped():
    """50x on $100 over 10 steps with a 1% stop: $500 → smaller."""
    plan = resolve_step_quote(
        deployed_quote=Decimal(5000), levels=10,
        stop_budget_usd=Decimal("1.00"), min_step_usd=100,
    )
    assert plan.uncapped == Decimal(500)
    assert plan.capped is True and plan.step < Decimal(500)
    # The bound is on the PYRAMID (levels x step), so at 10 levels a $1.00 stop
    # cannot fund a tradeable size at all: the cap lands on the venue floor and
    # says so, rather than certifying headroom that does not exist.
    assert plan.floored is True and plan.step == Decimal(100)


def test_a_step_that_already_fits_is_untouched():
    """A step fits when the PYRAMID's round trip fits. deployed 500 / 4 levels is
    a $125 step, i.e. a $500 pyramid whose round trip is $0.43 — 43% of a $1.00
    stop, past the 33% share — so it is capped. Give it a stop that genuinely
    affords the pyramid and nothing moves."""
    plan = resolve_step_quote(
        deployed_quote=Decimal(500), levels=4,
        stop_budget_usd=Decimal("5.00"), min_step_usd=100,
    )
    assert plan.capped is False and plan.step == plan.uncapped == Decimal(125)


def test_the_cap_can_only_shrink_never_grow():
    """Whatever the budget, the step must never exceed deployed/levels."""
    for budget in ("0.01", "1", "10", "1000", "1000000"):
        plan = resolve_step_quote(
            deployed_quote=Decimal(4900), levels=4,
            stop_budget_usd=Decimal(budget), min_step_usd=100,
        )
        assert plan.step <= plan.uncapped, budget


def test_a_participation_chunk_may_also_only_shrink():
    small = resolve_step_quote(
        deployed_quote=Decimal(4900), levels=4, chunk_quote=Decimal(200),
        stop_budget_usd=Decimal(1000), min_step_usd=100,
    )
    assert small.step == Decimal(200)
    big = resolve_step_quote(
        deployed_quote=Decimal(4900), levels=4, chunk_quote=Decimal(99999),
        stop_budget_usd=Decimal(1000), min_step_usd=100,
    )
    assert big.step == Decimal(1225), "a chunk must never enlarge the step"


def test_a_disarmed_stop_is_not_given_an_invented_budget():
    plan = resolve_step_quote(
        deployed_quote=Decimal(4900), levels=4, stop_budget_usd=0, min_step_usd=100,
    )
    assert plan.capped is False and plan.step == Decimal(1225)
    assert plan.round_trips_in_budget == Decimal("Infinity")


def test_a_budget_below_the_venue_minimum_floors_and_flags():
    """Shipping a size the venue will reject is worse than saying the stop is too
    tight — the caller needs to be able to tell the user."""
    plan = resolve_step_quote(
        deployed_quote=Decimal(4900), levels=4,
        stop_budget_usd=Decimal("0.05"), min_step_usd=100,
    )
    assert plan.floored is True
    assert plan.step == Decimal(100), "never size below what the venue accepts"
    assert plan.capped is True, "it still shrank from 1225"


def test_degenerate_inputs_do_not_raise():
    for kwargs in (
        {"deployed_quote": 0, "levels": 4},
        {"deployed_quote": Decimal(100), "levels": 0},
        {"deployed_quote": None, "levels": 1},
        {"deployed_quote": Decimal(100), "levels": 1, "chunk_quote": "not a number"},
        {"deployed_quote": Decimal(100), "levels": 1, "stop_budget_usd": "nonsense"},
    ):
        plan = resolve_step_quote(min_step_usd=100, **kwargs)  # type: ignore[arg-type]
        assert plan.step >= 0


@pytest.mark.parametrize("leverage,levels,sl_pct", [
    (49, 4, 0.8), (50, 10, 1.0), (25, 5, 0.5), (10, 3, 2.0), (1, 1, 1.0),
])
def test_after_capping_the_stop_always_covers_at_least_one_round_trip(leverage, levels, sl_pct):
    """The invariant that matters: never ship a configuration whose FIRST round
    trip closes the session, unless the venue minimum makes that impossible (and
    then ``floored`` says so)."""
    margin = Decimal(100)
    plan = resolve_step_quote(
        deployed_quote=margin * Decimal(leverage), levels=levels,
        stop_budget_usd=margin * Decimal(str(sl_pct)) / Decimal(100),
        min_step_usd=100,
    )
    assert plan.round_trip_cost < plan.stop_budget_usd or plan.floored
