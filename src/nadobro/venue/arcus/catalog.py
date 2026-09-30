"""Fail-closed Arcus market metadata (``GET /v1/markets``).

There is NO default / fallback market anywhere (contrast Nado's permissive
``ProductMeta(pid, 0.01, 0.001, 1)`` in ``strategy/engine_runtime.py``): a market
that is unknown, dropped for schema drift, or served from a stale snapshot
makes the caller refuse / HOLD. Nothing here ever invents a tick, step or
minimum.

Sources (docs ``api-reference__public__get-markets.md`` ``MarketInfo``):
- ``tickTiers``: "a submitted limit price must be a multiple of the ``tick``
  for the band its price falls in. Bands are ascending by ``upToPrice`` (the
  last band is unbounded — ``upToPrice`` omitted). ``tickSize`` equals the base
  tier"; ``upToPrice`` is an "Exclusive upper price bound".
- ``minOrderNotional``: "Flat minimum order notional … on position-opening
  orders. Reduce-only orders (including TPSLs) are exempt."
- ``maxOrderSize``: "reduce-only orders are NOT exempt".
- ``status``: "``OFFLINE`` markets are returned for visibility but should not
  be quoted or traded".
- ``markPrice``: ""0" means no mark price has been received yet — callers must
  not fall back to ``oraclePrice``" (numeric zero -> None; live F-USD sends
  ``"0.0000"``).
- ``openInterestCap`` (docs) / ``openInterestCapNotional`` (live,
  ``probe:markets_api.testnet.json``): either spelling; "Omitted when no cap is
  active".
- Signing units: "The divisor is **always the market's top-level
  ``tickSize``**", so every tier tick must be an integer multiple of it.

The allowlist can only SHRINK the v1 set (BTC-USD, ETH-USD, SOL-USD with their
pinned market ids); a venue renumbering is a code change (fail-closed).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal
from fractions import Fraction
from typing import Callable, Final, Iterable, Mapping, Protocol, Sequence

from src.nadobro.utils.venue_scope import parse_arcus_net
from src.nadobro.venue.arcus.errors import (
    ArcusSchemaError,
    InexactUnitError,
    Ok,
    ReadResult,
    record_schema_error,
)
from src.nadobro.venue.arcus.parse import _bool, _dec, _dec_opt, _fail, _market_id, _obj, _str
from src.nadobro.venue.arcus.types import ARCUS_V1_MARKET_IDS, TICKER_RE, Lane, Side

logger = logging.getLogger(__name__)

_QUANTIZE_PASSES: Final = 4
# Wide enough that multiplying an integer tick count back by its unit is exact.
_CTX: Final = Context(prec=60)
_DEFAULT_MAX_AGE_S: Final = 300.0


def _is_multiple(value: Decimal, unit: Decimal) -> bool:
    return (Fraction(value) / Fraction(unit)).denominator == 1


def _finite_positive(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a finite Decimal > 0")
    return value


@dataclass(frozen=True)
class TickTier:
    up_to_price: Decimal | None  # exclusive; None = the unbounded top band
    tick: Decimal


@dataclass(frozen=True)
class ArcusMarket:
    market_id: int
    ticker: str
    status: str
    type: str
    category: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    tick_tiers: tuple[TickTier, ...]
    min_order_notional: Decimal
    min_order_size: Decimal
    max_order_size: Decimal
    imf: Decimal
    mmf: Decimal
    off_hours_imf: Decimal
    is_outside_rth: bool | None
    oracle_price: Decimal | None
    mark_price: Decimal | None
    volume_24h_notional: Decimal | None
    open_interest_cap_notional: Decimal | None

    def max_leverage(self) -> int:
        """``floor(1 / imf)``: BTC 0.025 -> 40, ETH 0.04 -> 25, SOL 0.05 -> 20."""
        return math.floor(Fraction(1) / Fraction(self.imf))

    def tick_for_price(self, price: Decimal) -> Decimal:
        """The tick of the band ``price`` falls in (``upToPrice`` exclusive)."""
        for tier in self.tick_tiers:
            if tier.up_to_price is not None and price < tier.up_to_price:
                return tier.tick
        return self.tick_tiers[-1].tick

    def quantize_price(self, price: Decimal, side: Side, *, crossing: bool = False) -> Decimal:
        """A band-valid price, never more aggressive than asked for a maker
        (BUY floors, SELL ceils); ``crossing`` (a protective taker bound) rounds
        the other way (BUY ceils, SELL floors).

        Moving across a band edge can require a coarser tick ("a limit price in
        a coarser band must also be a multiple of that band's tick"), so the
        rounding is repeated (at most 4 passes; then ``InexactUnitError``).
        """
        p = _finite_positive(price, "price")
        if not isinstance(side, Side):
            raise ValueError("side must be a Side")
        if not isinstance(crossing, bool):
            raise ValueError("crossing must be a bool")
        round_up = (side is Side.SELL) != crossing
        for _ in range(_QUANTIZE_PASSES):
            tick = self.tick_for_price(p)
            ratio = Fraction(p) / Fraction(tick)
            n = math.ceil(ratio) if round_up else math.floor(ratio)
            q = _CTX.multiply(Decimal(n), tick)
            if q <= 0:
                raise ValueError("price rounds to zero")
            if _is_multiple(q, self.tick_for_price(q)):
                return q
            p = q
        raise InexactUnitError("price tier")

    def quantize_size_down(self, size: Decimal) -> Decimal:
        """Floor to a ``stepSize`` multiple; never grows. May return 0 (the
        caller refuses)."""
        if not isinstance(size, Decimal) or not size.is_finite() or size < 0:
            raise ValueError("size must be a finite Decimal >= 0")
        n = math.floor(Fraction(size) / Fraction(self.step_size))
        return _CTX.multiply(Decimal(n), self.step_size)

    def effective_min_notional(self, mark: Decimal) -> Decimal:
        """``max(minOrderNotional, minOrderSize × mark)`` for an OPENING order."""
        m = _finite_positive(mark, "mark")
        return max(self.min_order_notional, _CTX.multiply(self.min_order_size, m))


def _tick_tiers(raw: object, tick_size: Decimal) -> tuple[TickTier, ...]:
    if not isinstance(raw, list) or not raw:
        _fail("market.tickTiers")
    tiers: list[TickTier] = []
    last_index = len(raw) - 1
    prev_up: Decimal | None = None
    for i, entry in enumerate(raw):
        tier = _obj(entry, "market.tickTiers")
        tick = _dec(tier.get("tick"), "market.tickTiers.tick", gt=0)
        up_raw = tier.get("upToPrice")
        if i == last_index:
            if up_raw is not None:  # "the last band is unbounded — upToPrice omitted"
                _fail("market.tickTiers.last")
            up = None
        else:
            up = _dec(up_raw, "market.tickTiers.upToPrice", gt=0)
            if prev_up is not None and not up > prev_up:  # "ascending by upToPrice"
                _fail("market.tickTiers.order")
            prev_up = up
        if not _is_multiple(tick, tick_size):  # every accepted price stays an exact tick count
            _fail("market.tickTiers.tick")
        tiers.append(TickTier(up_to_price=up, tick=tick))
    if tiers[0].tick != tick_size:  # "tickSize equals the base tier"
        _fail("market.tickTiers.base")
    return tuple(tiers)


def parse_market(obj: Mapping[str, object]) -> ArcusMarket:
    """One ``MarketInfo`` -> :class:`ArcusMarket`; ``ArcusSchemaError`` (counted)
    on any missing/invalid required field. The three margin fractions are
    required HERE on purpose (fail-closed: leverage and margin math need them)
    although the doc's ``required`` list omits them."""
    row = _obj(obj, "market")
    tick_size = _dec(row.get("tickSize"), "market.tickSize", gt=0)
    imf = _dec(row.get("initialMarginFraction"), "market.initialMarginFraction", gt=0)
    if imf > 1:
        _fail("market.initialMarginFraction")
    mmf = _dec(row.get("maintenanceMarginFraction"), "market.maintenanceMarginFraction", gt=0)
    if mmf > imf:
        _fail("market.maintenanceMarginFraction")
    off_hours = _dec(row.get("offHoursInitialMarginFraction"), "market.offHoursInitialMarginFraction", gt=0)
    if off_hours > 1:
        _fail("market.offHoursInitialMarginFraction")
    rth_raw = row.get("isOutsideRth")
    oi_cap = _dec_opt(row.get("openInterestCap"), "market.openInterestCap", ge=0)
    oi_cap_notional = _dec_opt(row.get("openInterestCapNotional"), "market.openInterestCapNotional", ge=0)
    if oi_cap is not None and oi_cap_notional is not None and oi_cap != oi_cap_notional:
        _fail("market.openInterestCap")
    return ArcusMarket(
        market_id=_market_id(row.get("marketId"), "market.marketId"),
        ticker=_str(row.get("marketDisplayName"), "market.marketDisplayName", pattern=TICKER_RE).upper(),
        status=_str(row.get("status"), "market.status").upper(),
        type=_str(row.get("type"), "market.type").upper(),
        category=_str(row.get("category"), "market.category").upper(),
        base_asset=_str(row.get("baseAsset"), "market.baseAsset"),
        quote_asset=_str(row.get("quoteAsset"), "market.quoteAsset"),
        tick_size=tick_size,
        step_size=_dec(row.get("stepSize"), "market.stepSize", gt=0),
        tick_tiers=_tick_tiers(row.get("tickTiers"), tick_size),
        min_order_notional=_dec(row.get("minOrderNotional"), "market.minOrderNotional", ge=0),
        min_order_size=_dec(row.get("minOrderSize"), "market.minOrderSize", ge=0),
        max_order_size=_dec(row.get("maxOrderSize"), "market.maxOrderSize", gt=0),
        imf=imf,
        mmf=mmf,
        off_hours_imf=off_hours,
        is_outside_rth=None if rth_raw is None else _bool(rth_raw, "market.isOutsideRth"),
        oracle_price=_dec_opt(row.get("oraclePrice"), "market.oraclePrice", zero_is_none=True, ge=0),
        mark_price=_dec_opt(row.get("markPrice"), "market.markPrice", zero_is_none=True, ge=0),
        volume_24h_notional=_dec_opt(row.get("volume24hNotional"), "market.volume24hNotional", ge=0),
        open_interest_cap_notional=oi_cap if oi_cap is not None else oi_cap_notional,
    )


class MarketsSource(Protocol):
    """What :meth:`ArcusCatalog.refresh` needs from the REST client."""

    async def get_markets(
        self, *, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[list[Mapping[str, object]]]: ...


def _v1_allowlist() -> Iterable[str]:
    return ARCUS_V1_MARKET_IDS.keys()


def _default_max_age_s() -> float:
    return _DEFAULT_MAX_AGE_S


class ArcusCatalog:
    """Per-network market snapshot. Swapped atomically on a good load; a denied,
    empty or all-invalid load keeps the OLD snapshot (its age keeps growing, so
    ``is_fresh()`` turns False and callers refuse)."""

    def __init__(
        self,
        network: str,
        *,
        allowlist: Callable[[], Iterable[str]] | None = None,
        max_age_s: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.network = parse_arcus_net(network)
        self._allowlist = allowlist or _v1_allowlist
        self._max_age_s = max_age_s or _default_max_age_s
        self._clock = clock
        self._by_id: Mapping[int, ArcusMarket] = {}
        self._by_ticker: Mapping[str, ArcusMarket] = {}
        self._loaded_mono: float | None = None
        self._drift_warned: set[str] = set()
        self.last_error: str | None = None

    async def refresh(
        self, client: MarketsSource, *, lane: Lane = Lane.L1_ENGINE, max_wait_s: float | None = None
    ) -> bool:
        """``GET /v1/markets`` then :meth:`load_from_payload`. A denied read keeps
        the old snapshot (False, ``last_error`` = the outcome type)."""
        result = await client.get_markets(lane=lane, max_wait_s=max_wait_s)
        if not isinstance(result, Ok):
            self.last_error = type(result).__name__
            return False
        return self.load_from_payload(result.value)

    def load_from_payload(self, markets: Sequence[Mapping[str, object]]) -> bool:
        """Sync. Parse every row; drop bad rows (counted) and BOTH sides of any
        duplicate id / ticker; swap in the rest. ``[]`` or nothing valid -> False
        with the old snapshot kept (the venue never has zero markets)."""
        if isinstance(markets, (str, bytes)) or not isinstance(markets, Sequence):
            raise TypeError("markets must be a sequence of objects")
        if not markets:
            self.last_error = "empty"
            return False
        parsed: list[ArcusMarket] = []
        dropped = 0
        for row in markets:
            try:
                parsed.append(parse_market(row))
            except ArcusSchemaError:
                dropped += 1
        id_counts: dict[int, int] = {}
        ticker_counts: dict[str, int] = {}
        for market in parsed:
            id_counts[market.market_id] = id_counts.get(market.market_id, 0) + 1
            ticker_counts[market.ticker] = ticker_counts.get(market.ticker, 0) + 1
        by_id: dict[int, ArcusMarket] = {}
        by_ticker: dict[str, ArcusMarket] = {}
        for market in parsed:
            if id_counts[market.market_id] > 1 or ticker_counts[market.ticker] > 1:
                record_schema_error("markets.duplicate")
                dropped += 1
                continue
            by_id[market.market_id] = market
            by_ticker[market.ticker] = market
        if not by_id:
            self.last_error = "no_valid_market"
            return False
        self._by_id = by_id
        self._by_ticker = by_ticker
        self._loaded_mono = self._clock()
        self.last_error = None if dropped == 0 else f"dropped:{dropped}"
        return True

    def get(self, market_id: int) -> ArcusMarket | None:
        """ANY parsed market (ignores allowlist, status and freshness): brakes
        need tick/step for reduce-only closes even after an allowlist shrink."""
        if not isinstance(market_id, int) or isinstance(market_id, bool):
            return None
        return self._by_id.get(market_id)

    def by_ticker(self, ticker: str) -> ArcusMarket | None:
        """Case-insensitive ticker lookup (any parsed market)."""
        if not isinstance(ticker, str):
            return None
        return self._by_ticker.get(ticker.strip().upper())

    def allowlisted(self) -> tuple[ArcusMarket, ...]:
        """v1 set ∩ allowlist, and only ``PERPETUAL`` + ``CRYPTO`` + ``ONLINE``
        markets whose id is the pinned v1 id. Does NOT check freshness."""
        wanted = {t.strip().upper() for t in self._allowlist() if isinstance(t, str)}
        out: list[ArcusMarket] = []
        for ticker in sorted(set(ARCUS_V1_MARKET_IDS) & wanted):
            market = self._by_ticker.get(ticker)
            if market is None:
                continue
            expected_id = ARCUS_V1_MARKET_IDS[ticker]
            if market.market_id != expected_id:
                if ticker not in self._drift_warned:
                    self._drift_warned.add(ticker)
                    logger.error(
                        "arcus %s market id drift: %s is id %d, expected %d (excluded)",
                        self.network,
                        ticker,
                        market.market_id,
                        expected_id,
                    )
                continue
            if market.type != "PERPETUAL" or market.category != "CRYPTO" or market.status != "ONLINE":
                continue
            out.append(market)
        out.sort(key=lambda m: m.market_id)
        return tuple(out)

    def age_s(self) -> float | None:
        """Seconds since the last good load; None = never loaded."""
        if self._loaded_mono is None:
            return None
        return self._clock() - self._loaded_mono

    def is_fresh(self) -> bool:
        age = self.age_s()
        return age is not None and age <= self._max_age_s()


__all__ = [
    "TickTier",
    "ArcusMarket",
    "parse_market",
    "MarketsSource",
    "ArcusCatalog",
]
