"""Grid Executor — multi-level grid with a per-level state machine
(NOT_ACTIVE -> OPEN_ORDER_PLACED -> OPEN_ORDER_FILLED -> CLOSE_ORDER_PLACED ->
COMPLETE) and an aggregate Triple Barrier (limit_price hard stop / stop_loss).

Direction-generic: ``side=BUY`` is a long grid (buy a level, sell one step
up); ``side=SELL`` is a short grid (sell a level, buy one step down) — the
ReverseGridExecutor is the short configuration.

``activation_bounds`` keeps resting open orders within a fraction of mid. On a
limit_price / stop_loss breach the executor cancels every resting order and,
unless ``keep_position`` is set, flattens the net held inventory, terminating
with ``close_type=STOP_LOSS``.

Implemented in Phase 3.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)

from src.nadobro.engine.adapter.base import Fill, NadoAdapterBase, NadoOrder, OrderState
from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import (
    CloseType,
    OrderType,
    TradeType,
    TripleBarrierConfig,
    _dec,
)


# LIMIT ORDERS ONLY (2026-08-08, product ruling). This executor never sends a naked
# MARKET order. Entries and the ladder's paired per-level close legs REST post-only.
# Every risk exit — the stop-out flatten, the take-profit flatten, and D-Grid's
# profit-tier scale-out — is a MARKETABLE LIMIT: a bounded limit priced through the
# touch, so it crosses and fills like a taker but can never execute worse than its
# limit. An exit that may not fill is not an exit; an exit with no price bound is
# not one either.
#
# How far through the touch an exit prices. Wide enough to cross a normal book,
# bounded so a thin or gapped book cannot fill it at an arbitrary price.
_EXIT_CROSS_BP = Decimal("30")


class GridLevelState(Enum):
    NOT_ACTIVE = "NOT_ACTIVE"
    OPEN_ORDER_PLACED = "OPEN_ORDER_PLACED"
    OPEN_ORDER_FILLED = "OPEN_ORDER_FILLED"
    CLOSE_ORDER_PLACED = "CLOSE_ORDER_PLACED"
    COMPLETE = "COMPLETE"


@dataclass
class GridLevel:
    index: int
    open_price: Decimal
    close_price: Decimal
    amount_base: Decimal
    state: GridLevelState = GridLevelState.NOT_ACTIVE
    open_order_id: Optional[str] = None
    close_order_id: Optional[str] = None
    # Cumulative venue-side fills already recorded for this level. We track
    # base/quote/fee separately for both open and close legs so multi-price
    # fills (variable VWAP) can be ingested as exact deltas — BUG-GR-4 fix.
    _open_recorded: Decimal = Decimal(0)
    _open_quote_recorded: Decimal = Decimal(0)
    _open_fee_recorded: Decimal = Decimal(0)
    _close_recorded: Decimal = Decimal(0)
    _close_quote_recorded: Decimal = Decimal(0)
    _close_fee_recorded: Decimal = Decimal(0)
    # Base of this level's inventory closed by an order that is NOT its own close
    # leg — a profit-tier booking or the stop-out flatten. GRID-CLOSE-WATERMARK:
    # this used to be added into ``_close_recorded``, which ``_ingest`` assigns
    # ABSOLUTELY as the close ORDER's cumulative-fill watermark. Mixing a counter
    # into a watermark made ``_ingest`` subtract externally-closed base from the
    # close order's real fills: the fill was under-counted or dropped, its price
    # was divided by a shrunken delta, and the executor held phantom inventory
    # that permanently blocked D-Grid's next phase spawn.
    _close_booked_base: Decimal = Decimal(0)
    filled_base: Decimal = Decimal(0)
    filled_quote: Decimal = Decimal(0)


@dataclass
class GridExecutorConfig:
    trading_pair: str
    side: TradeType
    start_price: Decimal
    end_price: Decimal
    limit_price: Decimal
    total_amount_quote: Decimal
    min_spread_between_orders: Decimal
    min_order_amount_quote: Decimal = Decimal(0)
    max_open_orders: int = 10
    max_orders_per_batch: int = 10
    order_frequency: float = 0.0
    activation_bounds: Optional[Decimal] = None
    safe_extra_spread: Decimal = Decimal(0)
    triple_barrier_config: Optional[TripleBarrierConfig] = None
    leverage: int = 1
    keep_position: bool = False
    # Continuous market-making grid: when True, a fully round-tripped level
    # (COMPLETE) is re-armed to a fresh NOT_ACTIVE slot each tick so the grid
    # keeps working its band (buy the dip, sell the pop, re-arm, repeat) instead
    # of draining to all-COMPLETE and terminating. Default False preserves the
    # classic one-shot ladder (grid/rgrid fill_anchored=0). D-Grid sets it True.
    recycle_levels: bool = False
    connector_name: str = "nado"

    def __post_init__(self) -> None:
        for f in ("start_price", "end_price", "limit_price", "total_amount_quote",
                  "min_spread_between_orders", "min_order_amount_quote", "safe_extra_spread"):
            setattr(self, f, _dec(getattr(self, f)))
        if self.activation_bounds is not None:
            self.activation_bounds = _dec(self.activation_bounds)


def generate_grid_levels(cfg: GridExecutorConfig) -> List[GridLevel]:
    lo, hi = sorted([cfg.start_price, cfg.end_price])
    spread = cfg.min_spread_between_orders
    if spread > 0 and hi > 0:
        max_by_spread = int((hi - lo) / (hi * spread))
    else:
        max_by_spread = cfg.max_open_orders
    n = max(1, min(cfg.max_open_orders, max_by_spread + 1))
    if n == 1:
        prices = [lo]
    else:
        step = (hi - lo) / (n - 1)
        prices = [lo + step * i for i in range(n)]
    cap = cfg.total_amount_quote / n
    step_frac = cfg.min_spread_between_orders + cfg.safe_extra_spread
    return _ladder_levels(prices, cap, cfg.side, step_frac, start_index=0)


def _ladder_levels(
    prices: List[Decimal], cap_quote: Decimal, side: TradeType, step_frac: Decimal,
    *, start_index: int,
) -> List[GridLevel]:
    """Build fresh NOT_ACTIVE grid levels from a list of open prices. The close
    leg sits one ``step_frac`` away on the profit side (above for a long grid,
    below for a short). Shared by initial generation and in-place re-centering.
    """
    levels: List[GridLevel] = []
    for i, p in enumerate(prices):
        if p <= 0:
            continue
        if side is TradeType.BUY:
            close_price = p * (Decimal(1) + step_frac)
        else:
            close_price = p * (Decimal(1) - step_frac)
        levels.append(GridLevel(index=start_index + i, open_price=p,
                                close_price=close_price, amount_base=cap_quote / p))
    return levels


def _ladder_prices(start: Decimal, end: Decimal, count: int) -> List[Decimal]:
    lo, hi = sorted([start, end])
    if count <= 1:
        return [lo]
    step = (hi - lo) / (count - 1)
    return [lo + step * i for i in range(count)]


class GridExecutor(Executor):
    def __init__(
        self,
        config: GridExecutorConfig,
        *,
        user_id: int,
        controller_id: str,
        adapter: NadoAdapterBase,
        inventory: Optional[InventoryRepository] = None,
        executor_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            user_id=user_id,
            controller_id=controller_id,
            trading_pair=config.trading_pair,
            adapter=adapter,
            inventory=inventory,
            keep_position=config.keep_position,
            executor_id=executor_id,
        )
        self.config = config
        self.levels: List[GridLevel] = generate_grid_levels(config)
        self._last_place_ts = 0.0
        # Real venue-order activity counters (cumulative for this executor's
        # life) so /status and the per-cycle log can report actual placements
        # instead of the always-0 engine cycle result. Summed per controller.
        self.orders_placed = 0
        self.orders_filled = 0
        self.orders_cancelled = 0
        # Accumulators for an external reduce-only close (a profit tier), mirroring
        # the per-level ones so a multi-price fill is ingested as exact deltas.
        self._book_recorded_base = Decimal(0)
        self._book_recorded_quote = Decimal(0)
        self._book_recorded_fee = Decimal(0)

    @property
    def open_side(self) -> TradeType:
        return self.config.side

    @property
    def close_side(self) -> TradeType:
        return self.config.side.opposite

    # -- placement --------------------------------------------------------
    def _within_bounds(self, price: Decimal, mid: Decimal) -> bool:
        ab = self.config.activation_bounds
        if ab is None or mid <= 0:
            return True
        return abs(price - mid) / mid <= ab

    async def _place_open(self, level: GridLevel) -> None:
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair, self.open_side, OrderType.LIMIT_MAKER,
                level.amount_base, level.open_price, self.config.leverage, False,
            ),
            label="grid_open",
        )
        level.open_order_id = order.id
        level.state = GridLevelState.OPEN_ORDER_PLACED
        self.orders_placed += 1

    async def _place_close(self, level: GridLevel) -> None:
        await self._place_close_remaining(
            level,
            level.filled_base - level._close_recorded - level._close_booked_base,
        )

    async def _place_close_remaining(self, level: GridLevel, base_amount: Decimal) -> None:
        if base_amount <= 0:
            level.state = GridLevelState.COMPLETE
            return
        order = await self._guard(
            lambda: self.adapter.place_order(
                self.trading_pair, self.close_side, OrderType.LIMIT_MAKER,
                base_amount, level.close_price, self.config.leverage, True,
            ),
            label="grid_close",
        )
        level.close_order_id = order.id
        level.state = GridLevelState.CLOSE_ORDER_PLACED
        self.orders_placed += 1

    def _ingest(self, level: GridLevel, order: NadoOrder, side: TradeType, *, opening: bool) -> None:
        """Record the *delta* between the venue's current cumulative fill
        totals and what this level has already booked. BUG-GR-4 fix: track
        recorded base/quote/fee explicitly so multi-price (VWAP) fills give
        the right marginal price for each new chunk.
        BUG-GR-3 fix: pass the fee delta to the inventory book.
        """
        if opening:
            recorded_b = level._open_recorded
            recorded_q = level._open_quote_recorded
            recorded_f = level._open_fee_recorded
        else:
            recorded_b = level._close_recorded
            recorded_q = level._close_quote_recorded
            recorded_f = level._close_fee_recorded
        delta_base = order.filled_base - recorded_b
        if delta_base <= 0:
            return
        delta_quote = order.filled_quote - recorded_q
        delta_fee = order.fee_quote - recorded_f
        # Use the marginal price of this *chunk*, not the running VWAP.
        price = (delta_quote / delta_base) if delta_base > 0 else Decimal(0)
        self._record_fill(
            Fill(order.id, self.trading_pair, side, delta_base, price, delta_fee, time.time()),
            order,
        )
        if opening:
            level._open_recorded = order.filled_base
            level._open_quote_recorded = order.filled_quote
            level._open_fee_recorded = order.fee_quote
            level.filled_base = order.filled_base
            level.filled_quote = order.filled_quote
        else:
            level._close_recorded = order.filled_base
            level._close_quote_recorded = order.filled_quote
            level._close_fee_recorded = order.fee_quote

    # -- lifecycle --------------------------------------------------------
    async def on_create(self) -> None:
        self._activate()
        await self._maybe_place_opens()

    async def _maybe_place_opens(self) -> None:
        # Regime gate / inventory cap: the owning controller sets this flag
        # to stop NEW entry orders while letting existing close legs, stops,
        # and fills keep managing. Pause is "stop digging", never "flatten".
        if self.suppress_new_entries:
            return
        if self.config.order_frequency > 0:
            if time.time() - self._last_place_ts < self.config.order_frequency:
                return
        mid = await self._guard(lambda: self.adapter.mid_price(self.trading_pair), label="mid")
        placed = 0
        for level in self.levels:
            if placed >= self.config.max_orders_per_batch:
                break
            if level.state is GridLevelState.NOT_ACTIVE and self._within_bounds(level.open_price, mid):
                await self._place_open(level)
                placed += 1
        if placed:
            self._last_place_ts = time.time()

    async def recenter(self, start_price: object, end_price: object) -> None:
        """Re-quote the resting (unfilled) open ladder around a new ``[start,
        end]`` band WITHOUT realizing the held position. Levels that hold
        inventory (``OPEN_ORDER_FILLED`` / ``CLOSE_ORDER_PLACED``) keep their
        close legs working at the original target — only free / completed slots
        are cancelled and re-priced. This is a true grid re-center, NOT a
        flatten: no market order, no reduce-only close.
        """
        if self.is_terminated:
            return
        start = _dec(start_price)
        end = _dec(end_price)
        if start <= 0 or end <= 0:
            return

        kept: List[GridLevel] = []
        for lv in self.levels:
            if lv.state in (GridLevelState.OPEN_ORDER_FILLED, GridLevelState.CLOSE_ORDER_PLACED):
                kept.append(lv)  # holding inventory — leave the close leg working
                continue
            if lv.state is GridLevelState.OPEN_ORDER_PLACED and lv.open_order_id is not None:
                # Cancel the stale resting open, then re-poll to capture any
                # partial fill that landed before the cancel (BUG-GR-1 pattern).
                oid = lv.open_order_id
                cancelled = False
                try:
                    # Deliberately NOT via self._guard: _guard counts failures
                    # against the executor's budget and TERMINATES it FAILED after
                    # a few, and _terminate() does not cancel resting orders — so a
                    # spell of cancel errors killed the ladder outright and it never
                    # re-quoted again, reproducing the very stale-quote symptom this
                    # branch exists to fix (probed: 4 failed re-centers -> FAILED,
                    # then zero orders on every later tick). A re-center is
                    # OPPORTUNISTIC and runs again next cycle, so a transient cancel
                    # failure must cost nothing but this round. Audit round 3.
                    await self.adapter.cancel_order(oid)
                    self.orders_cancelled += 1
                    cancelled = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "grid %s: recenter cancel failed for %s (retrying next "
                        "cycle, level kept): %s", self.id, oid, exc,
                    )
                if not cancelled:
                    # RECENTER-ORPHAN (fail closed). ``cancel_order`` only returns
                    # after verifying the order really is off the book, so an
                    # exception here means it is STILL LIVE on the venue. Freeing
                    # the slot would re-quote on top of it AND drop its id, so
                    # nothing could ever cancel it again — the ladder would run at
                    # double the intended exposure. Keep the level bound to its
                    # live order and let the next re-center retry.
                    # Latent while the re-center almost never fired; routine once
                    # the threshold was capped to the band (6946ee5).
                    kept.append(lv)
                    continue
                try:
                    refreshed = await self.adapter.order_status(oid)
                    self._ingest(lv, refreshed, self.open_side, opening=True)
                except Exception as exc:  # noqa: BLE001
                    # FAIL CLOSED, like the cancel above (audit round 4). The
                    # cancel SUCCEEDED, so this order is off the book — but we do
                    # not know how much of it FILLED before it went. Freeing the
                    # slot here discards that fill: the level is re-quoted as if
                    # empty, the held base is never given a close leg, and the
                    # position silently drifts from what the ladder thinks it
                    # holds. Keep the level and re-probe next cycle; the id is
                    # retained so the fill can still be recovered.
                    logger.warning(
                        "grid %s: recenter status probe failed for %s (%s) — keeping "
                        "the level so an unknown partial fill is not discarded",
                        self.id, oid, exc,
                    )
                    kept.append(lv)
                    continue
                lv.open_order_id = None
                if lv.filled_base > 0:
                    # Partial inventory now held — book its close leg and keep it.
                    lv.state = GridLevelState.OPEN_ORDER_FILLED
                    await self._place_close(lv)
                    kept.append(lv)
                    continue
            # NOT_ACTIVE, COMPLETE, or an emptied open: this slot is free to recycle.

        # Re-quote the free capacity across the new band. Per-level size stays
        # total / max_open_orders so the grid's notional-per-level is unchanged.
        fresh_count = max(0, self.config.max_open_orders - len(kept))
        if fresh_count > 0:
            spread = self.config.min_spread_between_orders
            lo, hi = sorted([start, end])
            if spread > 0 and hi > 0:
                max_by_spread = int((hi - lo) / (hi * spread)) + 1
                fresh_count = max(1, min(fresh_count, max_by_spread))
            cap = self.config.total_amount_quote / Decimal(max(1, self.config.max_open_orders))
            step_frac = self.config.min_spread_between_orders + self.config.safe_extra_spread
            prices = _ladder_prices(start, end, fresh_count)
            fresh = _ladder_levels(prices, cap, self.open_side, step_frac, start_index=len(kept))
        else:
            fresh = []
        self.levels = kept + fresh
        logger.info(
            "grid %s recentered: kept=%s holding inventory, requoted=%s opens in [%s, %s]",
            self.id, len(kept), len(fresh), start, end,
        )
        # Place the re-priced opens now (subject to the usual suppress/bounds).
        await self._maybe_place_opens()

    def _exit_cross_price(self, mid: Decimal) -> Optional[Decimal]:
        """A limit price far enough THROUGH the touch to cross, and no further.

        LIMIT ORDERS ONLY: an exit must act, so it is priced aggressively; but it is
        still a limit, so a gapped or thin book cannot fill it at an arbitrary
        price the way a naked MARKET order can. ``None`` when there is no usable
        mid — the caller then has nothing safe to price against.
        """
        if mid is None or mid <= 0:
            return None
        slip = _EXIT_CROSS_BP / Decimal(10000)
        if self.close_side is TradeType.SELL:
            return mid * (Decimal(1) - slip)     # sell down through the bid
        return mid * (Decimal(1) + slip)         # buy up through the ask

    def _unclosed_base(self) -> Decimal:
        held = sum(
            (lv.filled_base - lv._close_recorded - lv._close_booked_base
             for lv in self.levels),
            Decimal(0),
        )
        return max(Decimal(0), held)

    async def _ingest_book(self, order: NadoOrder, *, crossed: bool = False) -> Decimal:
        """Book the fill of an external reduce-only close (a profit tier).

        Mirrors :meth:`_ingest` — marginal price for the chunk, fee delta passed
        through — but against this executor's booking accumulators rather than a
        level's, and it advances per-level close accounting by the SAME delta so
        ``_net_base`` and the resting close legs stay consistent with the venue.
        Returns the base newly closed.
        """
        delta_base = _dec(getattr(order, "filled_base", 0) or 0) - self._book_recorded_base
        if delta_base <= 0:
            return Decimal(0)
        delta_quote = _dec(getattr(order, "filled_quote", 0) or 0) - self._book_recorded_quote
        delta_fee = _dec(getattr(order, "fee_quote", 0) or 0) - self._book_recorded_fee
        self._book_recorded_base += delta_base
        self._book_recorded_quote += delta_quote
        self._book_recorded_fee += delta_fee
        price = (delta_quote / delta_base) if delta_base > 0 else Decimal(0)
        self._record_fill(
            Fill(order.id, self.trading_pair, self.close_side, delta_base, price,
                 delta_fee, time.time()),
            order,
            crossed=crossed or None,
        )
        await self._advance_close_accounting(delta_base)
        return delta_base

    async def reduce_position(
        self, amount_base: object, mid: Optional[Decimal] = None,
    ) -> Decimal:
        """Close up to ``amount_base`` of held inventory by CROSSING, and return
        the base actually reduced.

        TAKER RISK EXITS (2026-08-08, product ruling). This is D-Grid's profit-tier
        scale-out — a risk exit, and every risk exit crosses now. A brief maker
        experiment rested it post-only and chased the touch; that produced the
        whole class of defects this replaced: a tier chasing a reversing mid until
        it booked a LOSS, a slice stranded below one lot, an order orphaned on the
        flat branch with nobody left to drive it, and a partial fill colliding with
        the level close watermark. An exit that may not fill is not an exit.
        Entries and the ladder's paired per-level close legs stay post-only —
        those have no deadline and earn the spread.

        Synchronous: a reduce-only MARKET either fills now or comes back unfilled,
        and only what actually FILLED is booked. Booking the request regardless
        would inject a phantom close at price 0 into inventory and wrongly advance
        per-level accounting.

        DGRID-BOOK-RACE fix (kept): the reduction is routed THROUGH this executor
        rather than fired at the adapter behind its back, so the fill lands in the
        shared inventory AND advances per-level close accounting — its resting
        close legs and the controller's net view cannot drift apart.
        """
        if self.is_terminated:
            return Decimal(0)
        amount = _dec(amount_base)
        if amount <= 0:
            return Decimal(0)
        reduce = min(amount, self._unclosed_base())
        if reduce <= 0:
            return Decimal(0)
        # No min-notional guard: a crossing order is not subject to the venue's
        # RESTING minimum, which is exactly why crossing is the right tool for an
        # exit that would otherwise be too small to place at all.
        if mid is None:
            mid = await self._guard(
                lambda: self.adapter.mid_price(self.trading_pair), label="grid_book_mid",
            )
        # NOT via _guard: it terminates the executor FAILED after its retries, and
        # _terminate cancels nothing — so a transient venue error on a DISCRETIONARY
        # profit tier used to kill the ladder and strand every resting order on the
        # venue with no executor driving them. A tier is best-effort; the stop-out
        # flatten is the path that may terminate.
        try:
            order = await self.adapter.place_order(
                self.trading_pair, self.close_side, OrderType.LIMIT,
                reduce, self._exit_cross_price(_dec(mid or 0)),
                self.config.leverage, True,
            )
        except Exception as exc:  # noqa: BLE001 - booking is best-effort, retry next tick
            logger.warning("grid %s: profit-tier book failed (retry next tick): %s",
                           self.id, exc)
            return Decimal(0)
        if order is None:
            return Decimal(0)
        self._book_recorded_base = Decimal(0)
        self._book_recorded_quote = Decimal(0)
        self._book_recorded_fee = Decimal(0)
        self.orders_placed += 1
        booked = await self._ingest_book(order, crossed=True)
        if booked > 0:
            self.orders_filled += 1
        return booked

    async def _maybe_complete_level(self, lv: GridLevel) -> bool:
        """Complete a level whose inventory is fully closed, cancelling any close
        leg still resting for base that is already gone.

        Needed because a level can be closed by TWO orders — its own close leg and
        an external booking (profit tier / stop-out flatten) — and neither alone
        reports "FILLED" for the whole level. Without this, a level closed half by
        a tier and half by a partial close-leg fill stays CLOSE_ORDER_PLACED with
        an oversized order still on the book: reduce-only stops it over-closing,
        but it is a stale order against inventory the executor no longer holds.
        """
        if lv._close_recorded + lv._close_booked_base < lv.filled_base:
            return False
        # BOTH legs. A level can be completed while its OPEN order is still resting
        # and only partially filled — an external close books the filled part, which
        # is all this level "holds". Clearing only the close leg left the entry live
        # on the venue, and with recycle_levels (D-Grid's default) the recycled slot
        # DISCARDS the id and places a second entry at the same price: two live
        # orders on one rung, the orphan invisible to _net_base, to
        # _cancel_all_resting and to the flat check that gates the next phase.
        for attr, label in (("open_order_id", "grid_open_complete_cancel"),
                            ("close_order_id", "grid_close_complete_cancel")):
            oid = getattr(lv, attr)
            if oid is None:
                continue
            cid = oid
            try:
                await self._guard(lambda: self.adapter.cancel_order(cid), label=label)
                self.orders_cancelled += 1
            except Exception as exc:  # noqa: BLE001 - cancel is best-effort
                logger.warning("grid %s: completing level cancel failed for %s: %s",
                               self.id, cid, exc)
            setattr(lv, attr, None)
        lv.state = GridLevelState.COMPLETE
        return True

    async def _advance_close_accounting(self, filled: Decimal) -> None:
        """Attribute ``filled`` base of closing volume across the levels holding
        inventory, so resting close legs don't try to re-close it."""
        remaining = filled
        for lv in self.levels:
            if remaining <= 0:
                break
            lv_held = lv.filled_base - lv._close_recorded - lv._close_booked_base
            if lv_held <= 0:
                continue
            take = min(lv_held, remaining)
            lv._close_booked_base += take
            remaining -= take
            if await self._maybe_complete_level(lv):
                continue
            # Booked only PART of this level: its resting close leg is now sized for
            # base the level no longer holds. reduce_only is an account-level flag,
            # not per-level, so with other rungs still holding inventory the
            # oversized leg fills in full and closes ANOTHER rung's base at this
            # rung's price. Re-place it at what is actually left.
            if lv.state is GridLevelState.CLOSE_ORDER_PLACED and lv.close_order_id:
                cid = lv.close_order_id
                try:
                    await self._guard(lambda: self.adapter.cancel_order(cid),
                                      label="grid_close_resize_cancel")
                    self.orders_cancelled += 1
                except Exception as exc:  # noqa: BLE001 - cancel is best-effort
                    logger.warning("grid %s: close-leg resize cancel failed for %s: %s",
                                   self.id, cid, exc)
                    continue          # leave it be rather than double-place
                lv.close_order_id = None
                lv.state = GridLevelState.OPEN_ORDER_FILLED
                await self._place_close_remaining(
                    lv, lv.filled_base - lv._close_recorded - lv._close_booked_base,
                )

    async def on_tick(self) -> None:
        if self.is_terminated:
            return
        mid = await self._guard(lambda: self.adapter.mid_price(self.trading_pair), label="mid")
        if self._stop_breached(mid):
            await self._stop_out(CloseType.STOP_LOSS)
            return
        if self._take_profit_breached(mid):
            # GRID-TP-DEAD fix: the configured take_profit was passed into the
            # barrier but never read, so an executor-level TP never fired (only
            # the per-level close legs + the session rail booked profit). Honor
            # it now — a favorable move of ``take_profit`` from the average entry
            # flattens the position to lock the gain.
            await self._stop_out(CloseType.TAKE_PROFIT)
            return
        for level in self.levels:
            await self._process_level(level, mid)
        if self.config.recycle_levels:
            # Continuous grid: re-arm round-tripped levels so the band keeps
            # working. New entries still pass through _maybe_place_opens, which
            # honors suppress_new_entries (the controller's net-exposure cap),
            # so this can't accumulate past the inventory limit.
            self._recycle_completed_levels()
        await self._maybe_place_opens()
        if not self.config.recycle_levels and all(
            lv.state is GridLevelState.COMPLETE for lv in self.levels
        ):
            self._terminate(CloseType.COMPLETED)

    def _recycle_completed_levels(self) -> None:
        """Reset fully round-tripped levels (open filled + close filled) back to
        a fresh NOT_ACTIVE slot at the same band price so ``_maybe_place_opens``
        re-issues the entry. The completed round trip is already booked into the
        shared inventory, so zeroing the level's local accounting is safe — the
        new open is a new position with its own close leg. Price-following is
        handled separately by the controller's re-center (which re-prices free
        slots toward the current mid)."""
        for i, lv in enumerate(self.levels):
            if lv.state is GridLevelState.COMPLETE:
                self.levels[i] = GridLevel(
                    index=lv.index, open_price=lv.open_price,
                    close_price=lv.close_price, amount_base=lv.amount_base,
                )

    def _take_profit_breached(self, mid: Decimal) -> bool:
        """True when price has moved FAVORABLY by ``take_profit`` from the
        average entry (BUY: up; SELL: down). Mirrors ``_stop_breached``'s
        avg-entry reference so SL and TP use one consistent basis. Inert until
        there is a real average entry (no position ⇒ nothing to take)."""
        tb = self.config.triple_barrier_config
        if tb is None or tb.take_profit is None:
            return False
        avg = self._avg_entry()
        if avg is None:
            return False
        if self.open_side is TradeType.BUY and mid >= avg * (Decimal(1) + tb.take_profit):
            return True
        if self.open_side is TradeType.SELL and mid <= avg * (Decimal(1) - tb.take_profit):
            return True
        return False

    def _stop_breached(self, mid: Decimal) -> bool:
        lp = self.config.limit_price
        if self.open_side is TradeType.BUY:
            if lp > 0 and mid <= lp:
                return True
        else:
            if lp > 0 and mid >= lp:
                return True
        tb = self.config.triple_barrier_config
        if tb is not None and tb.stop_loss is not None:
            avg = self._avg_entry()
            if avg is not None:
                if self.open_side is TradeType.BUY and mid <= avg * (Decimal(1) - tb.stop_loss):
                    return True
                if self.open_side is TradeType.SELL and mid >= avg * (Decimal(1) + tb.stop_loss):
                    return True
        return False

    def _avg_entry(self) -> Optional[Decimal]:
        base = sum((lv.filled_base for lv in self.levels), Decimal(0))
        quote = sum((lv.filled_quote for lv in self.levels), Decimal(0))
        return (quote / base) if base > 0 else None

    async def _process_level(self, level: GridLevel, mid: Decimal) -> None:
        if level.state is GridLevelState.OPEN_ORDER_PLACED:
            assert level.open_order_id is not None
            oid = level.open_order_id
            order = await self._guard(lambda: self.adapter.order_status(oid), label="grid_open_status")
            # Always ingest any partial fills first so we don't lose inventory
            # records when the order is about to be cancelled (BUG-GR-1) or
            # was externally cancelled/rejected (BUG-GR-2).
            self._ingest(level, order, self.open_side, opening=True)
            if order.state is OrderState.FILLED:
                level.state = GridLevelState.OPEN_ORDER_FILLED
                self.orders_filled += 1
                await self._place_close(level)
                return
            if order.state in (OrderState.CANCELLED, OrderState.REJECTED):
                # BUG-GR-2 fix: external cancel/reject. If we got any fills,
                # close them via the close leg; otherwise return to NOT_ACTIVE
                # so the next placement cycle re-issues the open.
                level.open_order_id = None
                if level.filled_base > 0:
                    level.state = GridLevelState.OPEN_ORDER_FILLED
                    await self._place_close(level)
                else:
                    level.state = GridLevelState.NOT_ACTIVE
                return
            if self.config.activation_bounds is not None and not self._within_bounds(level.open_price, mid):
                # BUG-GR-1 fix: cancel, then RE-POLL to capture any fills that
                # arrived between status and cancel. If any base was filled,
                # treat the level as opened and book the close leg.
                try:
                    await self._guard(lambda: self.adapter.cancel_order(oid), label="grid_cancel")
                    self.orders_cancelled += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "grid %s: out-of-bounds cancel failed for %s — order may still be resting: %s",
                        self.id, oid, exc,
                    )
                try:
                    post_cancel = await self._guard(
                        lambda: self.adapter.order_status(oid), label="grid_open_post_cancel",
                    )
                    self._ingest(level, post_cancel, self.open_side, opening=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "grid %s: post-cancel status probe failed for %s — last fills may be unbooked: %s",
                        self.id, oid, exc,
                    )
                level.open_order_id = None
                if level.filled_base > 0:
                    level.state = GridLevelState.OPEN_ORDER_FILLED
                    await self._place_close(level)
                else:
                    level.state = GridLevelState.NOT_ACTIVE
        elif level.state is GridLevelState.CLOSE_ORDER_PLACED:
            assert level.close_order_id is not None
            cid = level.close_order_id
            order = await self._guard(lambda: self.adapter.order_status(cid), label="grid_close_status")
            self._ingest(level, order, self.close_side, opening=False)
            if order.state is OrderState.FILLED:
                level.state = GridLevelState.COMPLETE
                self.orders_filled += 1
                return
            # A tier may already have closed the rest of this level, so the level
            # can be whole even when THIS order is only partially filled.
            if await self._maybe_complete_level(level):
                return
            if order.state in (OrderState.CANCELLED, OrderState.REJECTED):
                # BUG-GR-2 fix: re-place the close leg for the remaining base
                # if any. If the level is fully closed via partials, complete it.
                level.close_order_id = None
                remaining = (level.filled_base - level._close_recorded
                             - level._close_booked_base)
                if remaining <= 0:
                    level.state = GridLevelState.COMPLETE
                else:
                    # Drop back to OPEN_ORDER_FILLED so _place_close re-issues
                    # against the remaining inventory.
                    level.state = GridLevelState.OPEN_ORDER_FILLED
                    await self._place_close_remaining(level, remaining)

    # -- stop / teardown --------------------------------------------------
    async def _cancel_all_resting(self) -> None:
        for level in self.levels:
            for oid in (level.open_order_id, level.close_order_id):
                if oid is not None:
                    cid = oid
                    try:
                        await self._guard(lambda: self.adapter.cancel_order(cid), label="grid_cancel_all")
                        self.orders_cancelled += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "grid %s: cancel-all failed for %s — order may still be resting: %s",
                            self.id, cid, exc,
                        )

    def _net_base(self) -> Decimal:
        opened = sum((lv.filled_base for lv in self.levels), Decimal(0))
        closed = sum(
            (lv._close_recorded + lv._close_booked_base for lv in self.levels),
            Decimal(0),
        )
        return opened - closed

    async def _stop_out(self, close_type: CloseType) -> None:
        await self._cancel_all_resting()
        # After cancelling open orders, re-poll levels to absorb any last
        # partial fills (BUG-GR-1 again at stop-out time).
        for level in self.levels:
            for label, oid, opening in (
                ("grid_open_status_stop", level.open_order_id, True),
                ("grid_close_status_stop", level.close_order_id, False),
            ):
                if oid is None:
                    continue
                try:
                    captured = oid
                    refreshed = await self._guard(
                        lambda: self.adapter.order_status(captured), label=label,
                    )
                    self._ingest(level, refreshed, self.open_side if opening else self.close_side, opening=opening)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "grid %s: stop-out status refresh (%s) failed for %s — "
                        "flatten size may miss last fills: %s",
                        self.id, label, oid, exc,
                    )
        net = self._net_base()
        if not self.config.keep_position and net > 0:
            stop_mid = await self._guard(
                lambda: self.adapter.mid_price(self.trading_pair), label="grid_stop_mid",
            )
            exit_px = self._exit_cross_price(_dec(stop_mid or 0))
            flat = await self._guard(
                lambda: self.adapter.place_order(
                    self.trading_pair, self.close_side, OrderType.LIMIT, net,
                    exit_px, self.config.leverage, True,
                ),
                label="grid_flatten",
            )
            if flat.filled_base > 0:
                # BUG-GR-5 fix: use the venue's authoritative quote total
                # rather than re-deriving price * base (which silently loses
                # precision for multi-price market fills) and pass the real
                # venue fee through to inventory rather than zero.
                price = flat.filled_quote / flat.filled_base
                self._record_fill(
                    Fill(
                        flat.id, self.trading_pair, self.close_side,
                        flat.filled_base, price, flat.fee_quote, time.time(),
                    ),
                    flat,
                    crossed=True,          # a marketable limit IS a taker fill
                )
                # The flatten is an EXTERNAL close like a profit tier, so it goes
                # through the same attribution and completion rule. This used to
                # inline a copy of both, which worked only because
                # _cancel_all_resting() runs first — a latent dependency on call
                # order, in duplicated arithmetic, on the path that closes a user's
                # position. The redundant cancel _maybe_complete_level may issue for
                # an already-cancelled leg is idempotent at the adapter.
                await self._advance_close_accounting(flat.filled_base)
            remaining = self._net_base()
            if remaining > Decimal("1e-12"):
                logger.warning(
                    "grid %s: %s flatten left open base=%s; executor remains active for retry",
                    self.id, close_type.name, remaining,
                )
                return
        self._terminate(close_type)

    async def on_stop(self, close_type: CloseType = CloseType.EARLY_STOP) -> None:
        if self.is_terminated:
            return
        await self._stop_out(close_type)
