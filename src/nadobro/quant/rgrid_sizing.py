"""R-Grid step sizing against the stop budget. Pure math — no I/O, no config.

Reverse Grid trades ``margin x leverage / levels`` per step. The session stop is a
% of MARGIN judged NET of fees, so leverage buys size but not stop budget — and
past a point the two collide. The bound below is priced at the TAKER round trip
even though R-Grid rests makers: it is a risk bound, and the cheaper real cost
must only ever leave MORE headroom than assumed, never less:

    $100 margin, 49x, 4 levels  →  $1,225 per break, $4,900 at full pyramid
    the pyramid's taker round trip →  $4,900 x 8.6bp = $4.21
    0.8%-of-margin stop          →  $0.80

The session then stops out on the FIRST entry+exit whichever way price went. The
user never sees a losing trade, just a strategy that "keeps stopping".

So the step is capped: one round trip may consume at most ``max_fee_share`` of the
stop budget, which guarantees ``1 / max_fee_share`` round trips fit inside the
stop before costs alone close the session. The cap can only ever SHRINK the step
(it is a min), and it is skipped entirely when the user disarmed their stop —
there is no budget to size against, and inventing one would silently override
their choice.

A floor stops the cap turning into a different failure: below the venue's minimum
order notional an order simply cannot be placed. When the budget implies a step
under that floor, sizing stops at the floor and ``floored`` is set so the caller
can tell the user the configuration cannot work rather than shipping an
unplaceable size.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.nadobro.quant.vol_fee_estimator import (
    DEFAULT_BUILDER_FEE_RATE,
    DEFAULT_SPOT_TAKER_FEE_RATE,
)

# All-in TAKER rate actually charged: catalog base + the 1bp builder routing that
# policy locks on.
TAKER_ALL_IN_RATE = DEFAULT_SPOT_TAKER_FEE_RATE + DEFAULT_BUILDER_FEE_RATE   # 4.3 bp
# R-Grid rests POST-ONLY quotes on both legs, so it does not pay the taker rate.
# The cap is still computed against the taker round trip on purpose: it is a
# RISK bound, and sizing it against the cheaper maker cost would let the step grow
# until an unexpected taker fill (a venue-side conversion, an escalated exit, a
# future edit) no longer fitted inside the stop. Conservative by construction —
# the real maker cost leaves strictly more headroom than the cap assumes.
TAKER_ROUND_TRIP_RATE = TAKER_ALL_IN_RATE * Decimal(2)                        # 8.6 bp

# One round trip may eat at most this share of the stop budget ⇒ at least three
# fit before fees alone close the session.
DEFAULT_MAX_FEE_SHARE = Decimal("0.33")

# ---------------------------------------------------------------------------
# EXIT GEOMETRY — one source of truth for the controller AND for step sizing.
#
# R-Grid has three distances, and shipping them all equal to the entry band is
# what made a trend follower lose money in trends:
#
#   entry band      how far a break must travel to qualify as a signal
#   trail giveback  how far back from the favourable EXTREME the trailing exit sits
#   exit band       how far from the average entry the exposure-band exit sits
#
# The trailing exit is the only one that can book a PROFIT — it ratchets with the
# extreme. The exposure-band exit fires at ``avg_entry x (1 - band)`` and is
# therefore LOSS-ONLY by construction. So the ordering that matters is: the trail
# must get its chance BEFORE the loss-only exit can fire. Shipped, it was the exact
# opposite — the trail armed at 2x the distance the band exit fired at, so on any
# tape whose pullbacks reach one band the band exit always won and the trail was
# unreachable. Measured on the repo's cost-aware backtester, the shipped geometry
# returned -32.05 across five trending regimes; the geometry below returned +181.37
# on the same tapes and the same costs.
#
# Both distances are DERIVED, not tuned:
#   giveback = arm  -> the instant the trail arms at +arm favourable, its stop sits
#                      at peak*(1-arm) ~= the entry, i.e. breakeven, and ratchets
#                      into profit from there.
#   exit     = arm + band  -> strictly beyond the arm point, so the profit-taking
#                      exit always engages first, with one band of margin.
# At the shipped 10bp band these evaluate to 2x and 3x the band, which is exactly
# where an independent parameter sweep put the optimum.
# ---------------------------------------------------------------------------


def arm_pct(band_frac: object, reset_threshold_pct: object = 0) -> Decimal:
    """Favourable excursion at which the trailing exit engages.

    Floored at the taker round trip: a "profit" smaller than the cost of taking it
    is not profit. Deliberately conservative — R-Grid rests makers, so its real
    cost is lower and this floor only ever makes arming harder, never easier.
    """
    band = _as_frac(band_frac)
    reset = _as_frac(reset_threshold_pct)
    return max(reset, band * Decimal(2), TAKER_ROUND_TRIP_RATE)


def trail_giveback_frac(band_frac: object, reset_threshold_pct: object = 0) -> Decimal:
    """How far back from the favourable extreme the trailing exit fires."""
    return arm_pct(band_frac, reset_threshold_pct)


def exit_band_frac(band_frac: object, reset_threshold_pct: object = 0) -> Decimal:
    """How far from the average entry the (loss-only) exposure-band exit fires.

    This is also the adverse move ``max_step_for_stop_budget`` must size against:
    the step cap exists so the strategy's OWN exit can trigger before the session
    rail does, so the two must use the same number or the rail always wins.
    """
    return arm_pct(band_frac, reset_threshold_pct) + _as_frac(band_frac)


# The crossing exit is priced THROUGH the touch so it actually fills, bounded so a
# gapped book cannot fill it at an arbitrary price (``rgrid._EXIT_CROSS_BP``). That
# bound is REALISED COST on the way out, so the stop budget has to carry it: sizing
# exposure against fees alone left the exit able to print 30bp worse than the
# modelled level, i.e. up to 1.8x the stop at the shipped defaults — "the user never
# sees a losing trade, just a strategy that keeps stopping", from the slippage side.
EXIT_CROSS_RATE = Decimal("0.0030")


def exit_cost_frac(band_frac: object, reset_threshold_pct: object = 0) -> Decimal:
    """Total adverse move a full pyramid can realise reaching its OWN exit:
    the trigger distance, the round-trip fees, and the worst-case crossing print.

    This is the number the stop budget must cover — anything smaller and the
    session rail fires before the strategy can exit on its own terms.
    """
    return (exit_band_frac(band_frac, reset_threshold_pct)
            + TAKER_ROUND_TRIP_RATE + EXIT_CROSS_RATE)


def step_band_frac(band_frac: object, reset_threshold_pct: object = 0) -> Decimal:
    """The ``band_frac`` to hand :func:`resolve_step_quote`.

    ``max_step_for_stop_budget`` adds ``TAKER_ROUND_TRIP_RATE`` itself, so this is
    :func:`exit_cost_frac` minus that term — everything else the move costs.

    It exists so the engine mapping and the pre-start card cannot disagree. They
    have now drifted apart twice (once on the entry band vs the exit band, once on
    the crossing print), each time letting the card quote a size the strategy does
    not place and swallow the "stop too tight" warning exactly when it applied.
    One call, one number, both callers.
    """
    return exit_cost_frac(band_frac, reset_threshold_pct) - TAKER_ROUND_TRIP_RATE


def _as_frac(value: object) -> Decimal:
    try:
        return max(Decimal(0), Decimal(str(value or 0)))
    except Exception:  # noqa: BLE001 - an unusable distance simply does not bind
        return Decimal(0)


@dataclass(frozen=True)
class StepPlan:
    """The resolved per-break size, and why it is what it is."""
    step: Decimal                 # what to trade per break
    uncapped: Decimal             # what sizing asked for before the stop budget
    stop_budget_usd: Decimal      # SL% x margin (0 ⇒ stop disarmed, no cap applied)
    # Taker fees for one entry + exit of the PYRAMID (``levels * step``), not of a
    # single step. R-Grid adds a step per break and exits the whole position at
    # once, so a per-step figure told the user they had ``levels`` times more
    # headroom than they did — the card renders this as "about N round trips".
    round_trip_cost: Decimal
    capped: bool                  # the stop budget shrank the step
    floored: bool                 # the budget wanted LESS than min_step_usd

    @property
    def round_trips_in_budget(self) -> Decimal:
        """How many entry+exit round trips fit inside the stop before costs alone
        close the session. Infinite when the stop is disarmed."""
        if self.round_trip_cost <= 0:
            return Decimal("Infinity")
        if self.stop_budget_usd <= 0:
            return Decimal("Infinity")
        return self.stop_budget_usd / self.round_trip_cost


def taker_round_trip_cost(step_quote: object) -> Decimal:
    """Fee cost of one entry + exit at this step size."""
    try:
        step = Decimal(str(step_quote or 0))
    except Exception:  # noqa: BLE001 - a bad size costs nothing to trade
        return Decimal(0)
    return max(Decimal(0), step) * TAKER_ROUND_TRIP_RATE


def max_step_for_stop_budget(
    stop_budget_usd: object,
    *,
    max_fee_share: Decimal = DEFAULT_MAX_FEE_SHARE,
    levels: int = 1,
    band_frac: object = 0,
) -> Optional[Decimal]:
    """Largest step whose PYRAMID stays inside the stop budget. ``None`` when
    there is no budget to size against.

    Two bounds, both against ``levels * step`` — the exposure a full pyramid
    reaches — not against one step. Sizing against one step was the original
    error (RGRID-STEP-EXIT-FEE / RGRID-STEP-PRICE-BOUND, self-audit 2026-08-08):
    R-Grid ADDS a step per break, and the exit closes the whole position at once,
    so a bound priced on a single step under-states the real cost by ``levels``.

    1. FEE bound — the pyramid's round trip may consume at most ``max_fee_share``
       of the budget::

           levels * step * RATE <= budget * max_fee_share

    2. PRICE bound — a full band of adverse move against the pyramid, plus its
       fees, must still fit inside the budget::

           levels * step * (band + RATE) <= budget

       Without this the cap bounded FEES only. R-Grid's own exit needs a ``band``
       pullback to trigger, so if one band of adverse move already exceeds the
       budget the session rail ALWAYS fires first and the strategy can never exit
       on its own terms — again "the user never sees a losing trade, just a
       strategy that keeps stopping", from the price side instead of the fee side.

    ``band_frac`` of 0 disables bound 2 (callers that genuinely have no band).
    """
    try:
        budget = Decimal(str(stop_budget_usd or 0))
    except Exception:  # noqa: BLE001
        return None
    if budget <= 0 or max_fee_share <= 0 or TAKER_ROUND_TRIP_RATE <= 0:
        return None
    lv = Decimal(max(1, int(levels or 1)))
    fee_bound = (budget * max_fee_share) / (lv * TAKER_ROUND_TRIP_RATE)
    try:
        band = max(Decimal(0), Decimal(str(band_frac or 0)))
    except Exception:  # noqa: BLE001 - an unusable band simply does not bind
        band = Decimal(0)
    move_rate = band + TAKER_ROUND_TRIP_RATE
    if band <= 0 or move_rate <= 0:
        return fee_bound
    return min(fee_bound, budget / (lv * move_rate))


def resolve_step_quote(
    *,
    deployed_quote: object,
    levels: int,
    chunk_quote: object = None,
    stop_budget_usd: object = 0,
    min_step_usd: object = 0,
    max_fee_share: Decimal = DEFAULT_MAX_FEE_SHARE,
    band_frac: object = 0,
) -> StepPlan:
    """Resolve R-Grid's per-break size.

    ``deployed_quote / levels`` is the base step. A participation chunk may only
    make it SMALLER. The stop budget may only make it smaller again. ``min_step_usd``
    is the venue's minimum order notional — the cap never goes below it, because an
    order that cannot be placed is a worse outcome than one that is too big.
    """
    deployed = max(Decimal(0), Decimal(str(deployed_quote or 0)))
    lv = max(1, int(levels or 1))
    step = deployed / Decimal(lv)
    if chunk_quote is not None:
        try:
            chunk = Decimal(str(chunk_quote))
            if chunk > 0:
                step = min(step, chunk)
        except Exception:  # noqa: BLE001 - an unusable chunk simply does not apply
            pass
    uncapped = step

    floor = max(Decimal(0), Decimal(str(min_step_usd or 0)))
    capped = floored = False
    budget_cap = max_step_for_stop_budget(
        stop_budget_usd, max_fee_share=max_fee_share, levels=lv, band_frac=band_frac,
    )
    if budget_cap is not None and budget_cap < step:
        if budget_cap < floor:
            # The stop is too tight for ANY placeable size. Stop at the floor and
            # let the caller say so — shrinking further just yields rejected orders.
            step = min(step, floor) if floor > 0 else budget_cap
            floored = True
            capped = step < uncapped
        else:
            step = budget_cap
            capped = True

    try:
        budget = max(Decimal(0), Decimal(str(stop_budget_usd or 0)))
    except Exception:  # noqa: BLE001
        budget = Decimal(0)
    return StepPlan(
        step=step,
        uncapped=uncapped,
        stop_budget_usd=budget,
        round_trip_cost=taker_round_trip_cost(step * Decimal(lv)),
        capped=capped,
        floored=floored,
    )


# ---------------------------------------------------------------------------
# TRIGGER REVERSE GRID — the ONE plan the engine mapping AND the pre-start card
# both derive from. The trigger ladder arms ``levels`` venue price-trigger rungs
# on EACH side of the anchor (2 x levels pending triggers while flat), fills every
# rung as a TAKER (a fired trigger crosses the book), and manages one venue
# reduce-only stop that trails. Everything money-relevant about it is decided by
# the four numbers below, so they are derived in one place:
#
#   step        rung spacing: the user's spread floored at REVGRID_STEP_FLOOR (the
#               Aug-2026 real-tape sweep: ~15-25bp is the viable zone; 10bp bled chop)
#   levels      rungs PER SIDE, capped at REVGRID_MAX_LEVELS: Nado allows at most 25
#               PENDING trigger orders per product per subaccount (docs, trigger
#               service rate limits). A flat ladder is 2 x levels, so 12 is the most
#               that fits (24) with room for the reduce-only stop while in a position
#               (levels-1 same-side rungs + 1 stop); 20 levels asked for 40 rungs and
#               the venue rejected the 26th+ (prod session 292: exactly 25 placed).
#   stop        protective distance from the average entry: 2 x step by default
#               (floored at the taker round trip), or the user's own rgrid_stop_pct
#   trail       favourable excursion that arms the trailing stop, and the giveback it
#               trails by: 2 x step by default (the stop sits at ~breakeven the moment
#               it arms and ratchets into profit), or the user's own rgrid_trail_pct
#
# The per-rung notional is ``resolve_step_quote`` against ``levels``, with the
# stop-budget PRICE bound sized on the trigger geometry (``step_band_frac(step)``),
# so a full pyramid reaching its own stop stays inside the session stop budget.
# The card used to size this off the RAW spread and the legacy reset threshold
# while the engine floored the step and ignored the reset — the card quoted
# $186.57/rung where the engine placed $119.62 (prod 2026-08-30 config). One
# function, one number, both callers.
# ---------------------------------------------------------------------------

# Validation-derived rung spacing floor (15bp); a user's wider spread is honoured.
REVGRID_STEP_FLOOR = Decimal("0.0015")
# How far THROUGH its level a fired entry rung may fill (its IOC price bound), and
# how far through the stop level the reduce-only stop close is priced. ONE source
# for the controller (its defaults) and the stop-budget sizing below: the bound
# the budget covers must be the bound the orders actually carry.
REVGRID_ENTRY_SLIP = Decimal("0.0015")   # 15bp
REVGRID_STOP_SLIP = Decimal("0.005")     # 50bp (the rail's venue stop uses the same)
# Venue limit: 25 pending trigger orders per product per subaccount.
REVGRID_VENUE_MAX_PENDING_TRIGGERS = 25
# Rungs per side that fit the venue limit while flat (2 x 12 = 24 <= 25).
REVGRID_MAX_LEVELS = 12


@dataclass(frozen=True)
class TriggerLadderPlan:
    """The resolved trigger-ladder geometry + per-rung size."""
    step_pct: Decimal            # rung spacing, as a fraction of price
    levels: int                  # rungs per side actually armed (capped)
    levels_requested: int        # what the user asked for
    stop_pct: Decimal            # protective stop distance from avg entry
    trail_arm_pct: Decimal       # favourable excursion that arms the trail
    trail_giveback_pct: Decimal  # distance the trailing stop sits behind the extreme
    sizing: StepPlan             # per-rung notional and why
    step_floored: bool           # the user's spread was below REVGRID_STEP_FLOOR
    stop_is_auto: bool           # stop derived from the step (no user override)
    trail_is_auto: bool          # trail derived from the step (no user override)

    @property
    def rung_quote(self) -> Decimal:
        return self.sizing.step

    @property
    def levels_capped(self) -> bool:
        return self.levels < self.levels_requested

    @property
    def pending_triggers_flat(self) -> int:
        return 2 * self.levels


def revgrid_stop_pct(step_pct: object, user_stop_pct: object = None) -> tuple[Decimal, bool]:
    """Protective stop distance: the user's ``rgrid_stop_pct`` (percent of price,
    > 0) or 2 x step. Floored at the taker round trip either way so ordinary
    noise + fees cannot trip it. Returns ``(fraction, is_auto)``."""
    step = _as_frac(step_pct)
    user = _as_frac(user_stop_pct) / Decimal(100) if user_stop_pct not in (None, "") else Decimal(0)
    if user > 0:
        return max(user, TAKER_ROUND_TRIP_RATE), False
    return max(step * Decimal(2), TAKER_ROUND_TRIP_RATE), True


def revgrid_trail_pct(step_pct: object, user_trail_pct: object = None) -> tuple[Decimal, bool]:
    """Trailing-stop arm distance (== giveback): the user's ``rgrid_trail_pct``
    (percent of price, > 0) or ``arm_pct(step)`` = 2 x step floored at the taker
    round trip. Returns ``(fraction, is_auto)``."""
    step = _as_frac(step_pct)
    user = _as_frac(user_trail_pct) / Decimal(100) if user_trail_pct not in (None, "") else Decimal(0)
    if user > 0:
        return max(user, TAKER_ROUND_TRIP_RATE), False
    return arm_pct(step), True


def trigger_ladder_plan(
    *,
    deployed_quote: object,
    levels: int,
    spread_frac: object,
    stop_budget_usd: object,
    min_step_usd: object,
    chunk_quote: object = None,
    user_stop_pct: object = None,
    user_trail_pct: object = None,
    max_levels: int = REVGRID_MAX_LEVELS,
) -> TriggerLadderPlan:
    """Resolve the trigger Reverse Grid's whole plan from the user's settings.

    ``spread_frac`` is the user's spread as a fraction (10bp -> 0.001);
    ``stop_budget_usd`` = the rail margin x the user's SL%; ``levels`` is the user's
    setting (capped here); ``user_stop_pct`` / ``user_trail_pct`` are percents of
    price (0 / None = derive from the step).
    """
    requested = max(1, int(levels or 1))
    lv = max(1, min(requested, max(1, int(max_levels or REVGRID_MAX_LEVELS))))
    raw_spread = _as_frac(spread_frac)
    step = max(raw_spread, REVGRID_STEP_FLOOR)
    stop, stop_auto = revgrid_stop_pct(step, user_stop_pct)
    trail, trail_auto = revgrid_trail_pct(step, user_trail_pct)
    # The adverse move the stop budget must cover is the distance from the entry
    # to the ladder's OWN stop plus the prints on the way in and out: the entry
    # may fill up to REVGRID_ENTRY_SLIP through its level and the stop close is
    # priced REVGRID_STOP_SLIP through the stop level (worst-case bounds — a limit
    # fills at the best available prices). The step-derived maker geometry is kept
    # as a floor so the sizing never loosens below what the real-tape validation
    # ran at. A user-WIDENED stop moves the exit further away and shrinks the rung
    # (SL/TP trace 2026-09-16); a tighter one never grows it.
    band = max(step_band_frac(step, Decimal(0)), stop + REVGRID_STOP_SLIP + REVGRID_ENTRY_SLIP)
    sizing = resolve_step_quote(
        deployed_quote=deployed_quote,
        levels=lv,
        chunk_quote=chunk_quote,
        stop_budget_usd=stop_budget_usd,
        min_step_usd=min_step_usd,
        band_frac=band,
    )
    return TriggerLadderPlan(
        step_pct=step,
        levels=lv,
        levels_requested=requested,
        stop_pct=stop,
        trail_arm_pct=trail,
        trail_giveback_pct=trail,
        sizing=sizing,
        step_floored=raw_spread < REVGRID_STEP_FLOOR,
        stop_is_auto=stop_auto,
        trail_is_auto=trail_auto,
    )
