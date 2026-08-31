"""Engine adapter base — the venue-agnostic Adapter interface (ABC) plus the
value objects exchanged with executors.

Both the live ``NadoAdapter`` (``engine/adapter/nado.py``) and the test
``MockNadoAdapter`` implement :class:`NadoAdapterBase`. This module imports
NOTHING from ``connectors/`` or ``services/nado_client`` so test doubles can
depend on it without touching the venue or the 1CT signer.

Implemented in Phase 1.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import AsyncIterator, List, Optional

from src.nadobro.engine.types import OrderType, TradeType
from src.nadobro.utils.env import env_int

# Per-cycle placement cap (all strategies). Bounds how many EXPOSURE-GROWING or
# requoting venue writes one engine cycle may issue on a single strategy session,
# so a deep-ladder re-quote burst can't flood the rate-limited venue and starve
# the (shared, single-GIL) event loop — the failure that hung the whole bot when
# a 15-level Mid book churned hundreds of orders in one 180s cycle. It counts and
# gates ONLY openings/requotes (place_order with reduce_only/never_grow False,
# cancel_and_place, place_trigger_order); it NEVER counts or gates a risk-reducing
# write (a reduce-only close/flatten, or the inherently-reduce-only protective
# stop), so the book can always trim or exit even when the opening budget is spent.
# It NEVER changes per-order size — it only withholds whole placements, which the
# reconcile-style controllers re-issue on the next cycle (the #268 lesson: a cap
# must not resize orders as a side effect). <=0 disables it. Read at call time so
# a redeploy — or a test monkeypatch — picks up the new value.
_OPENING_CAP_PER_CYCLE = env_int("NADO_MAX_OPENINGS_PER_CYCLE", 20)


class OrderState(Enum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED)


class AdapterError(Exception):
    """Venue error raised by an adapter. Executors retry on these per the
    executor retry policy (3 attempts, exponential backoff)."""


@dataclass
class NadoOrder:
    id: str
    trading_pair: str
    side: TradeType
    order_type: OrderType
    amount_base: Decimal
    price: Optional[Decimal] = None
    state: OrderState = OrderState.OPEN
    filled_base: Decimal = Decimal(0)
    filled_quote: Decimal = Decimal(0)
    fee_quote: Decimal = Decimal(0)

    @property
    def avg_fill_price(self) -> Optional[Decimal]:
        if self.filled_base <= 0:
            return None
        return self.filled_quote / self.filled_base


@dataclass
class Fill:
    order_id: str
    trading_pair: str
    side: TradeType
    amount_base: Decimal
    price: Decimal
    fee_quote: Decimal
    timestamp: float

    @property
    def amount_quote(self) -> Decimal:
        return self.amount_base * self.price


@dataclass
class OrderBookLevel:
    price: Decimal
    amount: Decimal


@dataclass
class OrderBookSnapshot:
    trading_pair: str
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[Decimal]:
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / Decimal(2)


class NadoAdapterBase(abc.ABC):
    """Contract that every Nado adapter (live or simulated) must satisfy.

    All ``async`` methods may raise :class:`AdapterError` on transient venue
    failures; executors apply the retry policy around them.
    """

    connector_name: str = "nado"

    # -- per-cycle placement budget (see _OPENING_CAP_PER_CYCLE) --------------
    # State is lazy (getattr-defaulted) so every concrete adapter — live,
    # simulated, or a test double — inherits the budget without changing its
    # __init__. The live adapter increments the counter on each successful
    # opening/requote via _note_opening_placement(); the simulator never
    # increments, so the cap is inert in backtests (as intended).
    def begin_cycle(self) -> None:
        """Reset this session's per-cycle opening/requote counter. Called once
        per engine cycle (the tick path) before the controller places anything."""
        self._cycle_openings = 0

    def _note_opening_placement(self) -> None:
        """Record one successful exposure-growing / requoting placement."""
        self._cycle_openings = getattr(self, "_cycle_openings", 0) + 1

    def opening_placements_this_cycle(self) -> int:
        return int(getattr(self, "_cycle_openings", 0))

    def opening_budget_exhausted(self) -> bool:
        """True once this cycle has issued the configured number of opening/
        requote placements. Controllers consult this to DEFER further OPENING
        quotes (never a reducing/exit quote) to the next cycle. Cap <=0 disables
        the bound entirely."""
        cap = _OPENING_CAP_PER_CYCLE
        if cap <= 0:
            return False
        return int(getattr(self, "_cycle_openings", 0)) >= cap

    @abc.abstractmethod
    async def place_order(
        self,
        trading_pair: str,
        side: TradeType,
        order_type: OrderType,
        amount_base: Decimal,
        price: Optional[Decimal] = None,
        leverage: int = 1,
        reduce_only: bool = False,
    ) -> NadoOrder:
        ...

    @abc.abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. Idempotent: cancelling an unknown/terminal order
        returns ``False`` rather than raising."""
        ...

    @abc.abstractmethod
    async def order_status(self, order_id: str) -> NadoOrder:
        ...

    @abc.abstractmethod
    def fill_stream(self, trading_pair: str) -> AsyncIterator[Fill]:
        """Async generator yielding fills for ``trading_pair`` in order."""
        ...

    @abc.abstractmethod
    async def order_book(self, trading_pair: str) -> OrderBookSnapshot:
        ...

    @abc.abstractmethod
    async def mid_price(self, trading_pair: str) -> Decimal:
        ...

    @abc.abstractmethod
    def tick_size(self, trading_pair: str) -> Decimal:
        ...

    @abc.abstractmethod
    def lot_size(self, trading_pair: str) -> Decimal:
        ...

    @abc.abstractmethod
    def min_notional(self, trading_pair: str) -> Decimal:
        ...

    # Market-data reads (concrete defaults so test doubles need not implement
    # them; the live adapter overrides). Consumed via the MarketData service.
    async def candles(
        self, trading_pair: str, timeframe: str = "1h", limit: int = 200
    ) -> List[dict]:
        raise NotImplementedError

    async def funding_rate(self, trading_pair: str) -> Optional[Decimal]:
        raise NotImplementedError

    async def depth_book(
        self, trading_pair: str, depth: int = 10
    ) -> OrderBookSnapshot:
        """Sized order book — levels carry real ``amount``, unlike ``order_book``.

        ``order_book`` is the top-of-book hot path behind every ``mid_price``
        call and returns levels with zero size; loading it with the full ladder
        would put a heavier query on the most-called method in the engine. This
        is the separate, lower-cadence read for structure and microstructure
        work (imbalance, liquidity-at-price, adverse-selection defense).

        Implementations must degrade to an empty snapshot rather than raise:
        depth is an enrichment and no consumer may hard-depend on it.
        """
        raise NotImplementedError

    async def cancel_and_place(
        self,
        cancel_order_id: str,
        trading_pair: str,
        side: TradeType,
        order_type: OrderType,
        amount_base: Decimal,
        price: Decimal,
        leverage: int = 1,
        reduce_only: bool = False,
    ) -> NadoOrder:
        """Atomically cancel a resting order and place a new one; return the NEW
        order. Replaces a resting quote with NO gap between the cancel and the
        re-place, and for ONE execute round trip instead of two.

        Failure is atomic and TOTAL: on any error this raises
        :class:`AdapterError`, and because the venue processes the request as
        one unit, a failure means the OLD order is still resting and nothing new
        was placed. That invariant is what lets a caller fall back to the
        classic cancel-then-place safely — a fused replace can never leave a
        half-done state, so it is never worse than doing the two separately.

        Concrete default raises ``NotImplementedError``: only the live adapter
        (and the test doubles that exercise replace) implement it, and no
        controller may hard-depend on it — it is gated and always has a
        cancel-then-place fallback.
        """
        raise NotImplementedError

    def forget_cancelled(self, order_id: str) -> None:
        """Drop local bookkeeping for an order the VENUE already cancelled as
        part of an atomic :meth:`cancel_and_place` (so NO cancel is issued for
        it). Best-effort no-op by default."""
        return None

    # -- price-trigger orders (Reverse Grid rungs) ------------------------
    # A price trigger is a DIFFERENT venue primitive from a resting order: the
    # venue watches the mid and fires the order when it crosses a level, so a
    # trigger can sit on the crossing side of the mid (a BUY ABOVE / a SELL
    # BELOW) — which a post-only maker LIMIT cannot. Placement and cancellation
    # therefore route through their OWN methods and their OWN registry, never the
    # resting-order path (``place_order`` / ``cancel_order`` hit the wrong venue
    # service for a trigger and would leak it).
    async def place_trigger_order(
        self,
        trading_pair: str,
        side: TradeType,
        amount_base: Decimal,
        trigger_price: Decimal,
        *,
        slippage_pct: float = 0.5,
        dependency: Optional[str] = None,
    ) -> NadoOrder:
        """Place a venue PRICE-TRIGGER **entry** order — the Reverse Grid rung
        primitive. The venue fires it when the mid crosses ``trigger_price``: a
        BUY rung fires on a RISE, a SELL rung on a FALL (momentum). It is NOT
        reduce-only — it OPENS/GROWS a position — and it is priced ``slippage_pct``
        THROUGH the level so it crosses and fills on trigger.

        ``dependency`` (a prior rung's digest) chains this rung to fire only after
        that one fills, which builds a pyramid. The returned :class:`NadoOrder`
        carries the trigger digest as its ``id`` for a later
        :meth:`cancel_trigger_order`.

        Concrete default raises ``NotImplementedError``: only the live adapter and
        the trigger-aware test doubles implement it; no controller may hard-depend
        on it without a capability check.
        """
        raise NotImplementedError

    async def cancel_trigger_order(self, order_id: str) -> bool:
        """Cancel a resting price-trigger order via the venue's TRIGGER service.
        Idempotent: an unknown / already-fired / already-cancelled trigger returns
        ``False`` rather than raising.

        This is a DIFFERENT venue endpoint from :meth:`cancel_order` — a trigger
        digest cancelled through the regular order path is a no-op that LEAKS the
        trigger (it keeps watching the mid and can fire an unwanted entry). Only
        the live adapter and trigger-aware doubles implement it."""
        raise NotImplementedError

    async def place_stop_order(
        self,
        trading_pair: str,
        close_size: Decimal,
        stop_price: Decimal,
        position_is_long: bool,
        *,
        slippage_pct: float = 0.5,
    ) -> NadoOrder:
        """Place a venue REDUCE-ONLY protective / trailing stop that flattens (part
        of) an open position when the mid crosses ``stop_price`` AGAINST it: a
        long's stop fires on a FALL (``mid_price_below``, a SELL close), a short's
        on a RISE (``mid_price_above``, a BUY close). ``reduce_only`` means the
        venue guarantees it can only SHRINK the position — never grow or flip it —
        so even a wrong price/side is bounded.

        The Reverse Grid re-places this at a trailing level as the position's
        favourable extreme advances, which locks profit while letting the winner
        run; a fresh position arms it at the protective ``avg_entry`` distance. It
        is a venue trigger, so it is cancelled via :meth:`cancel_trigger_order`
        (the trigger service), and the returned :class:`NadoOrder` carries the stop
        digest as its ``id``. Concrete default raises ``NotImplementedError``."""
        raise NotImplementedError

    async def held_base(self, trading_pair: str) -> Optional[Decimal]:
        """Base units of ``trading_pair`` the account ACTUALLY holds, per the
        VENUE — the spot balance for a spot product, the signed position size for
        a perp. ``None`` means the venue could not be read.

        This exists because SPOT IS A BALANCE, NOT A POSITION. Every safety net
        built on ``get_all_positions()`` is structurally blind to a spot leg: on
        2026-07-28 a Delta Neutral run left ~$99 of kBTC unhedged and
        ``get_all_positions()`` returned an empty list while it sat there. Engine
        inventory is no substitute — it is per-controller bookkeeping and it
        disagreed with the venue in that very incident (0.00155 recorded against
        0.0031 filled).

        ``None`` (not 0) on failure is deliberate: callers must fail SAFE rather
        than conclude "flat" from a failed read.
        """
        return None

    async def funding_since(self, trading_pair: str, since_ts: float) -> Decimal:
        """Net funding the user has *received* on ``trading_pair`` since
        ``since_ts`` (epoch seconds), as a signed quote amount: positive = the
        user earned funding (the Delta Neutral short collecting it), negative =
        the user paid funding. Default 0 so test doubles need not implement it;
        the live adapter overrides via the indexer funding endpoint."""
        return Decimal(0)
