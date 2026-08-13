"""Volume Bot (spot, v4 taker) fee estimation — pure math, no venue calls.

The user agrees to real charges off this output, so the contract is strict:
never under-quote. Every rate is resolved live-first with a measured fallback,
and the caller is told which basis was used so the card can say so.

Volume accounting (the reason this is a clean product, not a 2x guess):
``VolumeBotController`` books BOTH legs into ``session_volume_usd`` — the buy
notional at fill and the sell notional at close. So the user's target volume
is exactly the fee-bearing notional::

    fee = target_volume_usd x (venue_taker_rate + builder_rate)

Doubling that (once per leg) would double-count; using only the sell leg would
halve it. See docs/volume_bot_taker_v4.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Optional

# Venue BASE taker rate, i.e. EXCLUDING the builder share — the same basis the
# product catalog reports, because callers add the builder rate on top.
#
# Provenance (settled 2026-07-31 against production): the venue's charged fee
# (``order.fee_quote``) is ALL-IN — engine_persistence states it outright and
# splits the builder out purely for attribution
# (``fill_fee = venue_fee - notional*1bp``). Measured all-in taker is
# 4.30-4.44 bp, and the catalog quotes 3.50 bp for KBTC, so
# catalog_base + 1 bp builder == the charged all-in. This constant must
# therefore be a BASE rate too: 4.3 all-in - 1.0 builder = 3.3 bp. Using the
# all-in figure here counted the builder twice and over-quoted the card by
# ~20%.
DEFAULT_SPOT_TAKER_FEE_RATE = Decimal("0.00033")   # 3.3 bp base (4.3 bp all-in)

# Builder routing is locked to 1 bps by policy (config.get_nado_builder_routing_config;
# testnet routes without a builder and pays 0). Added on top of the CATALOG
# BASE rate to reach the charged all-in. (Do not cite trades_mainnet.builder_fee
# as evidence of the fee's structure: that column is COMPUTED as
# notional x 0.0001, so it reads 1.000 bp tautologically.)
DEFAULT_BUILDER_FEE_RATE = Decimal("0.0001")       # 1.0 bp
DEFAULT_MAKER_FEE_RATE = Decimal("0.00015")        # 1.5 bp (matches SimCosts.maker_fee)
# A resting-quote round trip: post-only in, post-only out, builder routing on both
# legs. This is the floor a grid level's step must clear — a level whose close leg
# sits closer than this to its open leg LOSES money every time it completes, which
# is DGRID-FEE-FLOOR (the shipped "Spread 2bp" preset was under it).
MAKER_ROUND_TRIP_RATE = (DEFAULT_MAKER_FEE_RATE + DEFAULT_BUILDER_FEE_RATE) * Decimal(2)  # 5.0 bp
# Worst REALISTIC round trip for a ladder level: rest the open (maker), but pay
# taker on the way out. Not pessimism — grid/rgrid exits escalate to a bounded
# CROSSING order on the risk path (_EXIT_CROSS_BP), and the venue can convert a
# post-only to a taker fill. This is the floor a per-level step must clear for a
# completed level to actually EARN something: flooring at MAKER_ROUND_TRIP_RATE
# only guarantees break-even, which the cost-aware backtester measures as net
# -0.0001 across trend/range/chop — a zero-edge grid that trades for nothing.
MIXED_ROUND_TRIP_RATE = (
    (DEFAULT_MAKER_FEE_RATE + DEFAULT_BUILDER_FEE_RATE)
    + (DEFAULT_SPOT_TAKER_FEE_RATE + DEFAULT_BUILDER_FEE_RATE)
)  # 6.8 bp

# Product decision (2026-07-31): the spot volume bot sizes each cycle inside
# this band. Enforced in the UI (buttons + custom input) and re-checked here so
# a stale/hand-edited config cannot start an out-of-band run.
MIN_MARGIN_USD = Decimal("100")
MAX_MARGIN_USD = Decimal("500")


def _dec(value: object, default: str = "0") -> Decimal:
    try:
        if value is None:
            return Decimal(default)
        return Decimal(str(value))
    except Exception:  # noqa: BLE001  # policy: degrade-ok(malformed rate falls back to the default)
        return Decimal(default)


def clamp_margin_usd(value: object) -> Decimal:
    """Clamp a requested per-cycle margin into the product's [100, 500] band."""
    margin = _dec(value, str(MIN_MARGIN_USD))
    if margin < MIN_MARGIN_USD:
        return MIN_MARGIN_USD
    if margin > MAX_MARGIN_USD:
        return MAX_MARGIN_USD
    return margin


@dataclass(frozen=True)
class VolFeeEstimate:
    """What the user is agreeing to when they press Start."""

    margin_usd: Decimal
    target_volume_usd: Decimal
    taker_fee_rate: Decimal          # venue, per leg, as a fraction
    builder_fee_rate: Decimal        # builder, per leg, as a fraction
    rate_source: str                 # "venue" | "measured_default"

    @property
    def total_rate(self) -> Decimal:
        """All-in cost per unit of traded notional (venue + builder)."""
        return self.taker_fee_rate + self.builder_fee_rate

    @property
    def total_rate_bp(self) -> Decimal:
        return self.total_rate * Decimal(10000)

    @property
    def estimated_fee_usd(self) -> Decimal:
        """Fees for the whole run.

        Both legs count toward ``session_volume_usd``, so the target volume IS
        the fee-bearing notional — no per-leg multiplier.
        """
        if self.target_volume_usd <= 0:
            return Decimal(0)
        return self.target_volume_usd * self.total_rate

    @property
    def estimated_cycles(self) -> int:
        """Round trips needed: each cycle trades ~margin on the buy and ~margin
        on the sell, i.e. ~2x margin of counted volume."""
        if self.margin_usd <= 0 or self.target_volume_usd <= 0:
            return 0
        cycles = self.target_volume_usd / (self.margin_usd * Decimal(2))
        # Round UP: a partial final cycle still trades and still pays fees.
        # ROUND_CEILING explicitly — Decimal's // truncates toward zero, so the
        # usual -(-x // 1) ceil idiom silently under-counts here.
        return int(cycles.to_integral_value(rounding=ROUND_CEILING))

    @property
    def fee_pct_of_margin(self) -> Decimal:
        """Total fees as a % of the margin at risk — the number that tells a
        user whether the run is affordable at their size."""
        if self.margin_usd <= 0:
            return Decimal(0)
        return self.estimated_fee_usd / self.margin_usd * Decimal(100)


def estimate_vol_fees(
    *,
    margin_usd: object,
    target_volume_usd: object,
    taker_fee_rate: Optional[object] = None,
    builder_fee_rate: Optional[object] = None,
) -> VolFeeEstimate:
    """Build the estimate the agreement card renders.

    ``taker_fee_rate`` is the venue rate when the caller resolved one from the
    product catalog; ``None`` (or a non-positive value) falls back to the
    measured production default and marks the source accordingly, so the card
    can be honest about the basis.
    """
    resolved_taker = _dec(taker_fee_rate, "0") if taker_fee_rate is not None else Decimal(0)
    if resolved_taker > 0:
        source = "venue"
    else:
        resolved_taker = DEFAULT_SPOT_TAKER_FEE_RATE
        source = "measured_default"

    resolved_builder = (
        _dec(builder_fee_rate, "0") if builder_fee_rate is not None
        else DEFAULT_BUILDER_FEE_RATE
    )
    if resolved_builder < 0:
        resolved_builder = Decimal(0)

    target = _dec(target_volume_usd, "0")
    if target < 0:
        target = Decimal(0)

    return VolFeeEstimate(
        margin_usd=clamp_margin_usd(margin_usd),
        target_volume_usd=target,
        taker_fee_rate=resolved_taker,
        builder_fee_rate=resolved_builder,
        rate_source=source,
    )


def taker_breakeven_price(
    entry_price: object,
    *,
    taker_fee_rate: object,
    builder_fee_rate: object = DEFAULT_BUILDER_FEE_RATE,
) -> Decimal:
    """Sell price at which a taker round trip nets exactly zero.

    Buying pays ``entry_price x (1 + r)`` per unit and selling receives
    ``sell x (1 - r)``, where ``r`` is the all-in per-leg rate. Setting them
    equal gives ``sell = entry x (1 + r) / (1 - r)``.

    Returns 0 for a non-positive entry (nothing bought yet) so callers can
    treat 0 as "no breakeven known" rather than a tradable price.
    """
    entry = _dec(entry_price, "0")
    if entry <= 0:
        return Decimal(0)
    rate = _dec(taker_fee_rate, "0") + _dec(builder_fee_rate, "0")
    if rate < 0:
        rate = Decimal(0)
    # A malformed rate >= 100% would invert or explode the denominator.
    if rate >= Decimal("0.99"):
        rate = Decimal("0.99")
    return entry * (Decimal(1) + rate) / (Decimal(1) - rate)


def taker_sell_target(
    entry_price: object,
    *,
    taker_fee_rate: object,
    builder_fee_rate: object = DEFAULT_BUILDER_FEE_RATE,
    min_profit_bp: object = Decimal("1"),
) -> Decimal:
    """The price the patient-taker sell leg waits for.

    ``max(entry x (1 + min_profit), breakeven)`` satisfies BOTH product
    requirements at once: strictly above the buy price ("sell higher than it
    bought") and at least covering both legs' fees, so the gain is real rather
    than fee-funded. Returns 0 when there is no entry yet.
    """
    entry = _dec(entry_price, "0")
    if entry <= 0:
        return Decimal(0)
    edge_bp = _dec(min_profit_bp, "0")
    if edge_bp < 0:
        edge_bp = Decimal(0)
    above_entry = entry * (Decimal(1) + edge_bp / Decimal(10000))
    breakeven = taker_breakeven_price(
        entry, taker_fee_rate=taker_fee_rate, builder_fee_rate=builder_fee_rate
    )
    return max(above_entry, breakeven)
