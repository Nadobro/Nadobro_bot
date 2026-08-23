"""Which objective a market is actually good for, and the fee floor that keeps
a spread-capture quote from paying to trade.

Pure math — no I/O, no config, stdlib only.

Why a profile at all
--------------------
Mid mode has had ONE behaviour for every market, and that is why it loses on
BTC. Two different jobs hide under "market making":

* **VOLUME** — on a one-tick book the spread does not cover a round trip, so
  there is no edge to capture and the only thing worth optimising is fill rate
  at bounded cost. Queue position is the whole game: never requote a still-good
  quote, ladder to cover levels, keep cancels rare.
* **SPREAD** — on a wide book the quoted edge genuinely exceeds the fee, so the
  quote should be priced (fee floor, inventory reservation price) and inventory
  held tighter.

Running the SPREAD playbook on BTC is how a maker bleeds: it quotes for an edge
that is not there and pays the fee on every fill. Running the VOLUME playbook
on a wide market leaves real money on the table. The selector below picks per
market instead of pretending one setting fits both.

THE FEE FLOOR
-------------
A resting quote at half-spread ``δ`` from fair value earns ``δ`` on the fill and
``δ`` again when the inventory is closed at fair value, so a completed round
trip captures ``2δ`` against a round-trip fee of ``f_rt``. Break-even is
therefore ``δ = f_rt / 2``, and the per-leg fee share ``f = f_rt / 2`` is the
quantity a half-spread must clear:

    δ* = f + max(1/k, min_edge)      with the hard invariant  δ* > f

``1/k`` is the GLFT order-arrival term and needs fill data to estimate; until a
calibration pass supplies it, ``min_edge`` carries the floor alone. What must
never happen is ``δ <= f``: that is a quote which loses money every time it
completes, and the shipped ``spread_floor_half_pct`` default (1.5bp) sits below
the 2.5bp per-leg fee, so it was reachable.

The floor is deliberately NOT applied to the VOLUME profile. Quoting inside the
fee is that profile's whole point — it buys fill rate with per-fill edge, and it
is bounded by the session SL rail rather than by a per-quote floor.
"""
from __future__ import annotations

from typing import Any, Optional

AUTO = "auto"
VOLUME = "volume"
SPREAD = "spread"

_PROFILES = (VOLUME, SPREAD)

# How far above the round trip a spread must sit before SPREAD is selected.
# A book quoting exactly the fee offers zero net edge, and one quoting a hair
# more offers an edge that a single tick of adverse selection erases — so the
# band defaults to VOLUME and only concedes SPREAD with real room to spare.
DEFAULT_SPREAD_MARGIN = 1.25

# Fallback per-leg maker+builder fee in bp when the caller has no per-product
# number. Mirrors vol_fee_estimator.MAKER_ROUND_TRIP_RATE (5.0bp) halved; kept
# as a literal here because ``quant`` is a stdlib-only leaf.
DEFAULT_FEE_ROUND_TRIP_BP = 5.0


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def normalize_objective(value: Any) -> str:
    """User setting -> ``auto`` | ``volume`` | ``spread``. Anything unknown is
    ``auto``: a typo must not silently pick a playbook."""
    token = str(value or "").strip().lower()
    return token if token in (AUTO, VOLUME, SPREAD) else AUTO


def per_leg_fee_bp(fee_round_trip_bp: Any = None) -> float:
    """Half of the round trip — the edge one leg of a quote has to clear."""
    rt = _num(fee_round_trip_bp)
    if rt is None or rt <= 0:
        rt = DEFAULT_FEE_ROUND_TRIP_BP
    return rt / 2.0


def half_spread_floor_bp(
    *,
    fee_round_trip_bp: Any = None,
    min_edge_bp: float = 1.0,
    inv_k_bp: Optional[float] = None,
) -> float:
    """``δ* = f + max(1/k, min_edge)``, strictly greater than the per-leg fee.

    ``inv_k_bp`` is the GLFT ``1/k`` term in bp once a calibration pass can
    estimate it from fills; ``None`` means it is unknown and ``min_edge_bp``
    carries the floor. A non-positive ``min_edge_bp`` would collapse δ* onto the
    fee itself, which is the break-even quote — so the edge term has a hard
    minimum that keeps the invariant ``δ* > f`` true by construction.
    """
    fee = per_leg_fee_bp(fee_round_trip_bp)
    edge = _num(min_edge_bp) or 0.0
    k_term = _num(inv_k_bp)
    if k_term is not None and k_term > edge:
        edge = k_term
    # Never zero: a floor equal to the fee is a quote that trades for nothing.
    return fee + max(edge, 0.1)


def resolve_profile(
    objective: Any,
    *,
    spread_bp: Optional[float],
    fee_round_trip_bp: Any = None,
    margin: float = DEFAULT_SPREAD_MARGIN,
) -> str:
    """Pick the playbook for this market.

    An explicit ``volume``/``spread`` setting always wins — the user overriding
    the measurement is a legitimate choice, not an error to correct.

    Under ``auto`` the rule is the one the arithmetic forces: SPREAD only when
    the observed spread clears the round-trip fee with the margin to spare.
    A missing or unusable spread reading resolves to VOLUME, because an unknown
    market is exactly the one not to run a pricing playbook on.
    """
    choice = normalize_objective(objective)
    if choice in _PROFILES:
        return choice
    width = _num(spread_bp)
    if width is None or width <= 0:
        return VOLUME
    rt = _num(fee_round_trip_bp)
    if rt is None or rt <= 0:
        rt = DEFAULT_FEE_ROUND_TRIP_BP
    threshold = rt * max(1.0, _num(margin) or DEFAULT_SPREAD_MARGIN)
    return SPREAD if width >= threshold else VOLUME


def reservation_offset_bp(
    inventory_ratio: Any,
    *,
    sigma_bp: Any,
    half_spread_bp: Any,
    gamma: float = 0.1,
    max_frac_of_half_spread: float = 0.5,
) -> float:
    """Shift of the quoting anchor away from mid to work inventory off.

    GLFT's reservation price is ``r = θ - q·γ·σ²``: holding inventory makes you
    want to quote where it gets sold. Long inventory (``inventory_ratio > 0``)
    therefore returns a NEGATIVE offset — both quotes move down, so the ask is
    more likely to fill and the bid less. Short inventory mirrors it.

    Two properties make this safe to apply to a live quote:

    * it is bounded by a fraction of the half-spread, so the shift can never
      cross the two sides over each other or push a quote through the fee
      floor — the sides keep their ordering whatever the inventory does;
    * it is separate from ``directional_bias``, which the USER owns. Overloading
      one field with two writers is how dead-bands stop working, and it would
      also let inventory silently overrule an explicit user lean.

    Returns 0.0 rather than guessing whenever an input is missing.
    """
    ratio = _num(inventory_ratio)
    sigma = _num(sigma_bp)
    half = _num(half_spread_bp)
    if ratio is None or half is None or half <= 0:
        return 0.0
    ratio = max(-1.0, min(1.0, ratio))
    if ratio == 0.0:
        return 0.0
    # No volatility estimate -> scale off the half-spread itself, which is the
    # only other quantity in the right units.
    scale = sigma if (sigma is not None and sigma > 0) else half
    magnitude = (_num(gamma) or 0.0) * scale
    cap = half * max(0.0, _num(max_frac_of_half_spread) or 0.0)
    return -ratio * min(magnitude, cap)
