"""Volume Bot controller — fast spot maker ping-pong (v3).

Objective: maximum executed spot volume per unit time at minimum cost, using
limit orders on BOTH legs. Each cycle is one buy → sell round trip of
``total_amount_quote``; cycles repeat until ``target_volume_usd`` (or the
``max_cycles`` safety cap) is reached.

The v2 controller produced almost no volume in production (max one fill per
session): the buy rested 5bp below mid with a ~25bp requote dead band (56 min
to fill $101 on KBTC), the sell was priced max(breakeven+edge, ask) with NO
requote path at all (8.5 h stall on WNVDAX), and the whole machine was gated
on per-cycle PROFIT — impossible in a flat/falling market. v3 replaces the
pricing and recovery mechanics while keeping the proven cycle accounting:

* Quotes are glued to the TOUCH: the buy joins the best bid (improving it by
  one tick when the spread leaves room), the sell joins the best ask the same
  way. Post-only, so every resting fill pays the maker fee (~1.8bp).
* BOTH legs requote on a timer (``vol_requote_seconds``): unfilled after N
  seconds → cancel and re-place at the fresh touch. No drift dead band.
* Per-cycle PROFIT is no longer required. The sell floor is the cycle's
  breakeven MINUS ``vol_max_cycle_loss_bp`` — a volume bot buys turnover with
  a bounded, configurable cost per cycle. The session SL rail remains the
  hard backstop.
* Maker-first, cross-on-deadline: a leg still unfilled after
  ``vol_cross_after_seconds`` (0 disables) is finished with a marketable
  LIMIT priced ``vol_cross_slippage_bp`` through the touch — still a limit
  order (bounded price), fills as taker. Restores the 8bf08d0 feature lost in
  the d10e6f1 merge.
* Market-hours aware: RWA spots (WNVDAX, WQQQX, …) have no live book when the
  underlying market is closed. A missing best bid/ask puts the controller in
  a ``market_closed`` wait state instead of quoting into a dead book (or
  failing the spawn) — it resumes automatically when the book comes back.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Optional

from src.nadobro.engine.controllers.controller_base import Controller
from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.executors.order_executor import OrderExecutor, OrderExecutorConfig
from src.nadobro.engine.risk import ExecutorRequest
from src.nadobro.engine.types import ExecutionStrategy, PositionAction, TradeType, _dec
from src.nadobro.quant.vol_fee_estimator import (
    DEFAULT_BUILDER_FEE_RATE,
    DEFAULT_SPOT_TAKER_FEE_RATE,
)

# Quote-like symbols that must never be selected as a base for Volume.
# Kept in sync with ``product_catalog._QUOTE_LIKE_SYMBOLS``.
_QUOTE_LIKE_BASES = frozenset({"USDC", "USDC0", "USDT", "USDT0", "USD"})

logger = logging.getLogger(__name__)


def _non_negative_decimal(value: object, default: str = "0") -> Decimal:
    try:
        parsed = _dec(value)
    except Exception:  # noqa: BLE001
        parsed = _dec(default)
    return parsed if parsed > 0 else Decimal(0)


class VolumeBotController(Controller):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(name="volume_bot", **kwargs)  # type: ignore[arg-type]
        raw_pair = str(self.cfg("trading_pair") or "").strip().upper()
        market = str(self.cfg("market", "spot")).lower()
        leverage = int(self.cfg("leverage", 1))
        if market != "spot":
            raise ValueError("VolumeBotController is spot-only (market must be 'spot')")
        if leverage != 1:
            raise ValueError("VolumeBotController is spot-only (leverage must be 1)")
        if not raw_pair:
            raise ValueError("VolumeBotController requires a non-empty trading_pair")
        if raw_pair.endswith("-PERP") or raw_pair.endswith("PERP"):
            raise ValueError(f"{raw_pair} is a perp; VolumeBotController is spot-only")
        base = raw_pair.split("-", 1)[0]
        if base in _QUOTE_LIKE_BASES:
            raise ValueError(
                f"{raw_pair} is a quote-like asset and not a valid Volume spot base"
            )

        self.trading_pair = raw_pair
        self.total_amount_quote = _dec(self.cfg("total_amount_quote", "100"))
        self.target_volume_usd = _dec(self.cfg("target_volume_usd", "0"))
        self.max_cycles = max(1, int(self.cfg("max_cycles", 100) or 100))
        # Passive distance BELOW the touch for the buy (0 = join/improve the
        # best bid). The v2 default of 5bp-under-mid is retired: on a tight
        # book that rests below the best bid and needs a down-move to fill.
        self.buy_offset_bp = _non_negative_decimal(self.cfg("vol_buy_offset_bp", 0))
        # Bounded cost of one round trip, in bp of the cycle notional,
        # measured against the fee-inclusive breakeven. Replaces the v2
        # forced-profit floor (breakeven + edge) that made cycles impossible
        # to complete unless price rose past entry + 2×fees + edge.
        self.max_cycle_loss_bp = _non_negative_decimal(
            self.cfg("vol_max_cycle_loss_bp", 20.0)
        )
        # Requote cadence: a resting leg older than this is cancelled and
        # re-placed at the fresh touch. Applies to BOTH legs (v2 chased only
        # the buy, and only after a ~25bp adverse run).
        self.requote_seconds = float(self.cfg("vol_requote_seconds", 20.0) or 0.0)
        # Maker-first deadline: a leg unfilled this long is finished with a
        # marketable LIMIT priced ``cross_slippage_bp`` through the touch.
        # 0 disables crossing (pure maker mode).
        self.cross_after_seconds = float(self.cfg("vol_cross_after_seconds", 25.0) or 0.0)
        self.cross_slippage_bp = _non_negative_decimal(
            self.cfg("vol_cross_slippage_bp", 15.0)
        )
        self.maker_fee_rate = self._maker_fee_rate()
        # v4 TAKER MODE (2026-07-31, docs/volume_bot_taker_v4.md). Both legs
        # are MARKET orders so every cycle fills on demand and the volume
        # target actually completes (v3's resting quotes stalled: one sell
        # rested 8.5h; a session reached $101 of a $10,000 target). 0 restores
        # the v3 maker path — a reversible kill-switch, not dead config.
        # EXECUTION ALGO (2026-07-31). One selector replaces the old
        # taker-on/off boolean:
        #   "twap"  — maker TWAP (DEFAULT): rest post-only at the touch, pace
        #             against a linear schedule, chase patiently, and cross
        #             only when the leg falls materially behind.
        #   "chase" — same ladder, impatient: short chase interval, early
        #             escalation. The fill-certainty end of the dial.
        #   "taker" — cross immediately (the v4.1 behaviour, kept as an opt-in
        #             and as the kill-switch).
        # Measured on KBTC spot: a taker round trip costs 14.3bp (5.7bp spread
        # + 2x4.3bp) versus 3.6bp resting — ~4x the volume per unit of the
        # session loss budget. Price impact at this size is 0.00bp, so resting
        # is the entire prize; nothing here is about impact.
        _algo = str(self.cfg("vol_execution_algo", "twap") or "twap").strip().lower()
        if _algo not in ("twap", "chase", "taker"):
            _algo = "twap"
        # Back-compat: an explicit vol_taker_mode=1 still forces the taker path
        # (it is the documented kill-switch), and =0 forces a maker path.
        _legacy = self.cfg("vol_taker_mode", None)
        if _legacy is not None:
            _algo = "taker" if _dec(_legacy) != 0 else (
                _algo if _algo != "taker" else "twap"
            )
        self.execution_algo = _algo
        self.taker_mode = _algo == "taker"
        self.maker_mode = not self.taker_mode
        # Pacing horizon for the maker schedule. The leg should be done inside
        # this window; debt against it is what escalates the ladder.
        self.twap_horizon_seconds = float(
            self.cfg("vol_twap_horizon_seconds", 120.0 if _algo == "twap" else 30.0) or 0.0
        )
        self.twap_slices = int(_dec(self.cfg("vol_twap_slices", 4)) or 4)
        # How far behind schedule (as a fraction of the leg) before crossing,
        # and how much of the leg may be crossed in total.
        self.cross_tolerance_frac = _dec(self.cfg("vol_cross_tolerance_frac", "0.5"))
        self.max_taker_frac = _dec(self.cfg("vol_max_taker_frac", "0.25"))
        # Hard deadline as a multiple of the horizon. Past it a maker leg
        # crosses regardless of schedule debt or taker budget — the valve that
        # makes "patient" bounded rather than indefinite. 0 disables (unwise).
        self.leg_hard_deadline_mult = float(
            self.cfg("vol_leg_hard_deadline_mult", 3.0) or 0.0
        )
        # Chase cadence: how long a resting child may sit before it is
        # cancelled and re-posted at the fresh touch.
        self.chase_interval_seconds = float(
            self.cfg("vol_chase_interval_seconds", 20.0 if _algo == "twap" else 5.0) or 0.0
        )
        # Execution telemetry (per session) — proves the maker prize is real.
        self.maker_fills = 0
        self.taker_fills = 0
        self.chases = 0
        self.crossed_quote = Decimal(0)
        self.leg_started_quote_ts = 0.0
        self.taker_fee_rate = self._taker_fee_rate()
        self.builder_fee_rate = _non_negative_decimal(
            self.cfg("vol_builder_fee_rate", DEFAULT_BUILDER_FEE_RATE)
        )
        # v4.1 (2026-07-31, user directive): PnL is explicitly not the goal —
        # both legs fire as taker, back to back, with NO wait for a profitable
        # exit. Every round trip therefore costs spread + both legs' fees by
        # construction; the session loss limit below is what bounds the run.
        # (The v4 patient-hold gate and its hold-stop knobs are gone: with no
        # hold there is no unwatched inventory for them to protect.)
        #
        # Session loss limit: STOP and flatten once cumulative realized PnL
        # NET OF FEES reaches -limit. Default 5% of margin => -$5 on $100.
        self.session_loss_limit_usd = _non_negative_decimal(
            self.cfg("vol_session_loss_limit_usd", 0)
        )
        # TAKER-REST-STALL (2026-07-31): a marketable LIMIT is not an IOC. If
        # the book has less size than our order within the slippage bound, the
        # remainder RESTS instead of cancelling — and taker mode disables the
        # maker requote/cross rescues, so the leg would sit forever holding a
        # partial position. Any taker leg still live after this long is
        # cancelled and re-placed at the fresh touch (a partially filled buy
        # flips straight to selling what it actually got). 0 disables.
        self.taker_fill_timeout_seconds = float(
            self.cfg("vol_taker_fill_timeout_seconds", 10.0) or 0.0
        )
        # AUDIT-VOL-2026-07-31 F1: "taker" is executed as a MARKETABLE LIMIT,
        # not a naked MARKET. NadoClient.place_market_order is an IOC priced
        # +/-slippage_pct through the touch (default 1%) and the adapter never
        # passes that argument — so a bare MARKET could fill up to 99bp through
        # the loss floor and the "never below the floor" guarantee was false.
        # A marketable limit still fills immediately as taker (it crosses the
        # book) but carries a hard price bound.
        self.taker_slippage_bp = _non_negative_decimal(
            self.cfg("vol_taker_slippage_bp", 15.0)
        )
        # Set when a stop fires while inventory is still held: the controller
        # flattens FIRST and only then completes, so a stop can never leave the
        # user holding base the bot opened.
        self._emergency_exit_reason = ""

        # Cumulative session progress. RESTORED on a rebuild (recovery / worker
        # handoff) from the state the worker persists each cycle — otherwise a
        # rebuild would zero the loss counter and restart the user's 5% stop
        # budget (and the max_cycles cap) mid-run. See VOL-STOP-RESET in
        # engine_runtime.run_engine_cycle; same pattern as DN's restore_*.
        self.session_volume_usd: Decimal = _non_negative_decimal(
            self.cfg("restore_session_volume_usd", 0)
        )
        self.session_realized_pnl_usd: Decimal = _dec(
            self.cfg("restore_session_realized_pnl_usd", 0) or 0
        )
        self.cycles_completed = max(0, int(_dec(self.cfg("restore_cycles_completed", 0) or 0)))
        self.completed = False
        self.stop_reason = ""
        self.phase = "idle"

        self.buy_id: Optional[str] = None
        self.sell_id: Optional[str] = None
        self.entry_base = Decimal(0)
        self.entry_quote = Decimal(0)
        self.entry_fee_quote = Decimal(0)
        self.entry_price = Decimal(0)
        self.entry_fill_ts = 0.0
        self.close_base_remaining = Decimal(0)
        # Per-cycle accumulators across (possibly several) sell orders. Each
        # executor is booked exactly once (a tick can re-enter the terminated
        # branch if a follow-up spawn raised mid-transition).
        self.sold_base = Decimal(0)
        self.sold_quote = Decimal(0)
        self.sold_fee_quote = Decimal(0)
        self._accounted_sells: set = set()
        # Leg timers. ``leg_started_ts`` is set once per leg per cycle (the
        # cross deadline measures total leg age across requotes);
        # ``leg_quoted_ts`` resets on every placement (the requote timer).
        self.leg_started_ts = 0.0
        self.leg_quoted_ts = 0.0
        self.leg_crossed = False
        # Bounded recovery counters (reset each completed cycle).
        self.buy_retries = 0
        self.sell_attempts = 0
        self.requotes = 0
        self.crosses = 0
        self.market_closed = False
        self._market_closed_logged = False
        self.last_order_digest = ""
        self.last_order_kind = ""

    _MAX_BUY_RETRIES = 3
    _MAX_SELL_ATTEMPTS = 5
    _MAX_REQUOTES_PER_CYCLE = 120

    def _maker_fee_rate(self) -> Decimal:
        """Return positive maker fee cost as a fraction; rebates count as 0 cost."""
        raw = self.cfg("spot_maker_fee_rate", self.cfg("vol_maker_fee_rate"))
        if raw is None and self.cfg("vol_maker_fee_bp") is not None:
            raw = _dec(self.cfg("vol_maker_fee_bp")) / Decimal(10000)
        rate = _non_negative_decimal(raw, "0")
        # A malformed rate >= 100% would make the breakeven denominator invalid.
        return min(rate, Decimal("0.99"))

    def _taker_fee_rate(self) -> Decimal:
        """Venue taker fee as a positive fraction.

        Falls back to the production-measured default (4.3 bp) when the
        catalog carries no rate — never to 0, which would make the breakeven
        gate think a round trip is free and sell at a real loss.
        """
        raw = self.cfg("spot_taker_fee_rate", self.cfg("vol_taker_fee_rate"))
        rate = _non_negative_decimal(raw, "0")
        if rate <= 0:
            rate = DEFAULT_SPOT_TAKER_FEE_RATE
        return min(rate, Decimal("0.99"))

    def _taker_all_in_rate(self) -> Decimal:
        return self.taker_fee_rate + self.builder_fee_rate

    def _leg_should_cross(self, total_quote: Decimal, filled_quote: Decimal) -> bool:
        """Has this leg fallen far enough behind its TWAP schedule to justify
        paying the taker fee?

        Replaces the v3 fixed ``vol_cross_after_seconds`` deadline. A wall-clock
        deadline crosses even when the leg is nearly done; schedule debt only
        crosses when we are actually behind, which is what keeps the maker
        prize (~4x volume per unit of loss budget) intact. The taker budget
        (``vol_max_taker_frac``) is the second brake: past it we deliberately
        run late rather than silently revert to the taker cost curve.
        """
        from src.nadobro.quant.twap_schedule import should_cross

        if self.taker_mode or total_quote <= 0:
            return False
        elapsed = (time.time() - self.leg_started_ts) if self.leg_started_ts else 0.0
        # SAFETY VALVE (self-review 2026-07-31): patience must have a hard end.
        # Schedule debt alone cannot rescue a leg that is PARTIALLY filled —
        # e.g. 60% done leaves 40% of debt, under the 50% tolerance, so it
        # never escalates; once the chase budget is also spent the leg rests
        # forever and the cycle never completes. That is precisely the v3
        # stall this work exists to remove, and it is reachable with
        # vol_chase_interval_seconds=0 even without exhausting chases.
        #
        # This valve deliberately BYPASSES the taker budget: a leg that can
        # never complete is worse than paying the fee, the same reasoning that
        # lets the v4.1 emergency exit price through the loss floor.
        if self._leg_patience_exhausted(elapsed):
            logger.info(
                "volume_bot: patience exhausted on the %s leg (elapsed %.0fs, "
                "chases %s) — crossing to finish it pair=%s controller=%s",
                "sell" if self.phase == "pending_close_fill" else "buy",
                elapsed, self.requotes, self.trading_pair, self.id,
            )
            return True
        return should_cross(
            total_quote,
            filled_quote=filled_quote,
            elapsed_seconds=elapsed,
            horizon_seconds=self.twap_horizon_seconds,
            tolerance_frac=self.cross_tolerance_frac,
            crossed_quote=self.crossed_quote,
            max_taker_frac=self.max_taker_frac,
        )

    def _leg_patience_exhausted(self, elapsed: float) -> bool:
        """Has the maker leg run out of passive options?

        Either we have spent the per-cycle chase budget, or the leg has been
        working for a hard multiple of its horizon. Both mean "resting is not
        going to finish this", independently of how far behind schedule the
        arithmetic says we are.
        """
        if self.requotes >= self._MAX_REQUOTES_PER_CYCLE:
            return True
        hard_deadline = self.twap_horizon_seconds * self.leg_hard_deadline_mult
        return hard_deadline > 0 and elapsed >= hard_deadline

    def _sell_leg_notional(self) -> Decimal:
        """Quote notional the sell leg is unwinding (what the buy actually
        cost), so the sell paces against real size rather than the configured
        clip — a partial buy must not be measured against a full-size schedule.
        """
        if self.entry_base > 0 and self.entry_price > 0:
            return self.entry_base * self.entry_price
        return self.total_amount_quote

    def _session_loss_breached(self) -> bool:
        """Has cumulative realized PnL, NET OF FEES, hit the session limit?

        ``session_realized_pnl_usd`` is booked per completed round trip as
        ``sold_quote - entry_quote - entry_fee - sold_fee`` (_finish_cycle), so
        it is already net of both legs' venue fees. Default limit is 5% of
        margin: -$5 on a $100 cycle. 0 disables the check.

        This lives in the CONTROLLER, not the live-session rail, because that
        rail is structurally blind to spot (strategy_sessions.product_id is
        resolved perp-only, so it is NULL for a spot session and unrealized
        PnL short-circuits to 0). The controller knows exactly what it bought
        and sold, so its own book is the authoritative stop for this strategy.
        """
        if self.session_loss_limit_usd <= 0:
            return False
        return self.session_realized_pnl_usd <= -self.session_loss_limit_usd

    def _target_reached(self) -> bool:
        return self.target_volume_usd > 0 and self.session_volume_usd >= self.target_volume_usd

    def _complete(self, reason: str) -> None:
        self.phase = "done"
        self.completed = True
        self.stop_reason = reason
        self._set_stopped()

    # -- book helpers -------------------------------------------------------

    async def _touch(self) -> Optional[tuple[Decimal, Decimal]]:
        """(best_bid, best_ask) when the book is LIVE, else None.

        A missing side is the market-closed signal for RWA spots (the venue
        keeps the product listed but the book empties outside market hours).
        v2 fell back to mid_price here and quoted into the dead book all
        night; v3 waits instead.
        """
        try:
            book = await self.adapter.order_book(self.trading_pair)
            bid, ask = book.best_bid, book.best_ask
        except Exception:  # noqa: BLE001 - a dead feed is handled as closed
            return None
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        return bid, ask

    def _tick(self) -> Decimal:
        try:
            tick = self.adapter.tick_size(self.trading_pair)
        except Exception:  # noqa: BLE001
            tick = Decimal(0)
        return tick if tick > 0 else Decimal(0)

    @staticmethod
    def _snap(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
        """Quantize to the venue tick, away from the aggressive side."""
        if tick <= 0 or price <= 0:
            return price
        steps = price / tick
        snapped = steps.to_integral_value(rounding="ROUND_CEILING" if up else "ROUND_FLOOR")
        return snapped * tick

    def _buy_price(self, bid: Decimal, ask: Decimal) -> Decimal:
        """Join the best bid; improve it by one tick when the spread leaves
        room (price-time priority: an improving quote is first in line)."""
        tick = self._tick()
        px = bid
        if tick > 0 and (ask - bid) >= tick * 2:
            px = bid + tick
        offset = self.buy_offset_bp / Decimal(10000)
        if offset > 0:
            px = px * (Decimal(1) - offset)
        return self._snap(px, tick, up=False)

    def _cycle_breakeven(self) -> Decimal:
        """Sell price at which the round trip nets exactly zero after both
        maker fees. Estimated from the entry when the buy fee is unknown."""
        if self.entry_base <= 0 or self.entry_quote <= 0:
            return Decimal(0)
        buy_fee = self.entry_fee_quote if self.entry_fee_quote != 0 else (
            self.entry_quote * self.maker_fee_rate
        )
        gross_cost = self.entry_quote + buy_fee
        denominator = Decimal(1) - self.maker_fee_rate
        return gross_cost / (self.entry_base * denominator)

    def _sell_floor(self) -> Decimal:
        """Lowest acceptable sell price: breakeven minus the loss budget."""
        breakeven = self._cycle_breakeven()
        if breakeven <= 0:
            return Decimal(0)
        return breakeven * (Decimal(1) - self.max_cycle_loss_bp / Decimal(10000))

    def _sell_price(self, bid: Decimal, ask: Decimal) -> Decimal:
        """Join the best ask (improving by one tick when the spread leaves
        room), clamped to the loss floor. The v2 rule px >= max(breakeven +
        edge, ask) forced per-cycle profit and never quoted inside the
        spread; v3 sells AT the market and bounds the downside instead."""
        tick = self._tick()
        px = ask
        if tick > 0 and (ask - bid) >= tick * 2:
            px = ask - tick
        floor = self._sell_floor()
        if floor > 0 and px < floor:
            px = floor
        return self._snap(px, tick, up=True)

    # -- order plumbing ------------------------------------------------------

    async def _spawn_order(
        self,
        side: TradeType,
        amount_base: Decimal,
        price: Optional[Decimal],
        *,
        kind: str,
        execution: ExecutionStrategy = ExecutionStrategy.LIMIT_MAKER,
        position_action: PositionAction = PositionAction.OPEN,
        ref_price: Optional[Decimal] = None,
        crosses_book: bool = False,
    ) -> tuple[bool, Optional[OrderExecutor]]:
        # MARKET carries no price (the venue fills at the touch); ``ref_price``
        # still sizes the risk request so a taker order is never submitted to
        # the risk engine as a $0 notional.
        sizing_price = price if price is not None and price > 0 else (ref_price or Decimal(0))
        cfg = OrderExecutorConfig(
            self.trading_pair,
            side,
            amount_base,
            execution,
            price=price if execution is not ExecutionStrategy.MARKET else None,
            leverage=1,
            position_action=position_action,
            # The vol bot's crossing legs stay LIMIT orders priced through the
            # book, so nothing about the order says "taker" — the caller has to.
            # Reporting only (RG-TAKERFLAG-1); it changes no order parameter.
            crosses_book=crosses_book,
        )
        ex = OrderExecutor(
            cfg,
            user_id=self.user_id,
            controller_id=self.id,
            adapter=self.adapter,
            inventory=self.inventory,
        )
        # A risk LIMIT must never block risk REDUCTION. Without this, prod session
        # 104 stranded spot: the venue lot-rounds a buy fill UP, so a $100 session
        # held $101.01, and the sell's $100 cap rejected it 1.4s after the fill
        # (see the comment at strategy/engine_runtime.py's vol risk-limits block).
        # That was patched by padding the CAP with headroom; the exemption is the
        # actual fix, and it is the same one R-Grid's reducing leg already uses.
        # Safe on spot even though the adapter STRIPS reduce_only there (the venue
        # rejects it, error_code 5000): the exit is still bounded, by never_grow
        # plus the adapter's clamp down to the wallet balance.
        _closing = position_action is PositionAction.CLOSE
        ok = await self.spawn_executor(
            ex, ExecutorRequest(
                order_amount_quote=amount_base * sizing_price,
                reduce_only=_closing, position_action=position_action,
            )
        )
        if ok and ex.order is not None:
            self.last_order_digest = ex.order.id
            self.last_order_kind = kind
        return ok, ex if ok else None

    def _mark_quoted(self, *, new_leg: bool) -> None:
        now = time.time()
        if new_leg:
            self.leg_started_ts = now
            self.leg_crossed = False
        self.leg_quoted_ts = now

    async def _start_buy_cycle(self) -> bool:
        touch = await self._touch()
        if touch is None:
            self._enter_market_closed()
            return False
        self._exit_market_closed()
        bid, ask = touch
        if self.taker_mode:
            # v4: take the ask. Executed as a MARKETABLE LIMIT priced through
            # the ask — it crosses the book and fills immediately as taker, but
            # a bare MARKET is an IOC at 1% slippage (F1) that could overspend
            # the risk-approved cycle size. Size off the ask so the notional
            # lands on the user's margin.
            if ask <= 0:
                self._complete("invalid_buy_price")
                return False
            slip = self.taker_slippage_bp / Decimal(10000)
            buy_cap = self._snap(ask * (Decimal(1) + slip), self._tick(), up=True)
            amount_base = self.total_amount_quote / ask
            ok, ex = await self._spawn_order(
                TradeType.BUY, amount_base, buy_cap, kind="buy_taker",
                execution=ExecutionStrategy.LIMIT, ref_price=ask,
                crosses_book=True,
            )
        else:
            buy_price = self._buy_price(bid, ask)
            if buy_price <= 0:
                self._complete("invalid_buy_price")
                return False
            amount_base = self.total_amount_quote / buy_price
            ok, ex = await self._spawn_order(
                TradeType.BUY, amount_base, buy_price, kind="buy"
            )
        if ok and ex is not None:
            self.buy_id = ex.id
            self.sell_id = None
            self.phase = "pending_fill"
            self._mark_quoted(new_leg=True)
            self.entry_base = Decimal(0)
            self.entry_quote = Decimal(0)
            self.entry_fee_quote = Decimal(0)
            self.entry_price = Decimal(0)
            self.entry_fill_ts = 0.0
            self.close_base_remaining = Decimal(0)
            self.sold_base = Decimal(0)
            self.sold_quote = Decimal(0)
            self.sold_fee_quote = Decimal(0)
            self._accounted_sells.clear()
            return True
        self._complete("buy_spawn_failed")
        return False

    async def _requote_buy(self, buy_ex: Executor) -> bool:
        """Cancel the resting buy and re-place at the fresh touch. A partial
        fill flips the cycle to the sell leg for what we actually hold."""
        self.requotes += 1
        await self.orchestrator.stop(buy_ex.id)  # type: ignore[attr-defined]
        self._sync_buy_progress(buy_ex)
        if self.entry_base > 0:
            self.session_volume_usd += self.entry_quote
            self.phase = "filled_wait_close"
            await self._start_sell_cycle()
            return True
        if self.taker_mode:
            # Re-place with TAKER geometry (marketable through the ask), not
            # the maker join below — otherwise a taker requote would silently
            # post a passive maker bid. Nothing filled, so the per-cycle reset
            # inside _start_buy_cycle is a no-op.
            return await self._start_buy_cycle()
        touch = await self._touch()
        if touch is None:
            self._enter_market_closed()
            return False
        buy_price = self._buy_price(*touch)
        if buy_price <= 0:
            self._complete("invalid_buy_price")
            return False
        amount_base = self.total_amount_quote / buy_price
        ok, ex = await self._spawn_order(TradeType.BUY, amount_base, buy_price, kind="buy")
        if ok and ex is not None:
            self.buy_id = ex.id
            self._mark_quoted(new_leg=False)
            return True
        self._complete("buy_spawn_failed")
        return False

    async def _start_sell_cycle(self, amount_base: Optional[Decimal] = None) -> bool:
        amount = amount_base if amount_base is not None else (self.entry_base - self.sold_base)
        if amount <= 0:
            self._complete("sell_nothing_to_close")
            return False
        touch = await self._touch()
        if touch is None:
            # Holding inventory with a dead book: wait for the market to
            # reopen rather than failing the leg (the RWA overnight case).
            self._enter_market_closed()
            self.close_base_remaining = amount
            return False
        self._exit_market_closed()
        if self.taker_mode:
            # Every sell path (first close, requote remainder, cross remainder,
            # market-closed resume) funnels through the patient gate here, so
            # no caller has to know about it.
            return await self._taker_sell_now(amount, touch)
        sell_price = self._sell_price(*touch)
        if sell_price <= 0:
            self._complete("invalid_sell_price")
            return False
        ok, ex = await self._spawn_order(
            TradeType.SELL,
            amount,
            sell_price,
            kind="sell",
            position_action=PositionAction.CLOSE,
        )
        if ok and ex is not None:
            self.sell_id = ex.id
            self.close_base_remaining = amount
            new_leg = self.phase != "pending_close_fill"
            self.phase = "pending_close_fill"
            self._mark_quoted(new_leg=new_leg)
            return True
        self._complete("sell_spawn_failed")
        return False

    async def _taker_sell_now(
        self, amount: Decimal, touch: tuple[Decimal, Decimal]
    ) -> bool:
        """Sell the whole position immediately as taker. No profit gate.

        v4.1 (user directive 2026-07-31): PnL is not the objective — volume
        is. The v4 patient gate (wait for a price above entry that also clears
        both legs' fees) is gone, so a cycle is buy-then-sell back to back and
        the bot is FLAT between cycles. That is what makes the session loss
        limit a sufficient bound: there is no held bag for it to miss.

        Execution is still a MARKETABLE LIMIT rather than a naked MARKET —
        that is an execution-safety bound, not a profit gate.
        ``place_market_order`` is an IOC priced 1% through the touch by default
        and the adapter never overrides it, so a bare MARKET could fill ~99bp
        away from the book (AUDIT-VOL-2026-07-31 F1). A limit priced
        ``vol_taker_slippage_bp`` through the bid crosses and fills at once,
        with a hard worst-case price.
        """
        bid, _ask = touch
        if bid <= 0:
            self._enter_market_closed()
            self.close_base_remaining = amount
            return False
        slip = self.taker_slippage_bp / Decimal(10000)
        sell_cap = self._snap(bid * (Decimal(1) - slip), self._tick(), up=False)
        if sell_cap <= 0:
            self._complete("invalid_sell_price")
            return False
        ok, ex = await self._spawn_order(
            TradeType.SELL, amount, sell_cap, kind="sell_taker",
            execution=ExecutionStrategy.LIMIT,
            position_action=PositionAction.CLOSE, ref_price=bid,
            crosses_book=True,
        )
        if ok and ex is not None:
            self.sell_id = ex.id
            self.close_base_remaining = amount
            new_leg = self.phase != "pending_close_fill"
            self.phase = "pending_close_fill"
            self._mark_quoted(new_leg=new_leg)
            return True
        self._complete("sell_spawn_failed")
        return False

    async def _requote_sell(self, sell_ex: Executor) -> bool:
        """Cancel the resting sell and re-place the remainder at the fresh
        touch. v2 had no sell requote at all — one resting sell above a
        falling market stalled the session forever (8.5 h in prod)."""
        self.requotes += 1
        await self.orchestrator.stop(sell_ex.id)  # type: ignore[attr-defined]
        self._book_sell_fill(sell_ex)
        remaining = self.entry_base - self.sold_base
        if remaining <= 0:
            self._finish_cycle_and_continue_marker = True
            return True
        return await self._start_sell_cycle(remaining)

    async def _cross_leg(self, ex: Executor, side: TradeType) -> bool:
        """Finish a stalled leg with a marketable LIMIT through the touch —
        still a limit order (price-bounded), fills immediately as taker."""
        touch = await self._touch()
        if touch is None:
            self._enter_market_closed()
            return False
        bid, ask = touch
        self.crosses += 1
        self.leg_crossed = True
        # Track quote notional crossed so the per-leg taker budget
        # (vol_max_taker_frac) can actually bind — without this the "never
        # cross more than N% of the leg" brake has nothing to measure.
        try:
            _rem = self.entry_base - self.sold_base
            _px = self.entry_price if self.entry_price > 0 else Decimal(0)
            self.crossed_quote += (
                (_rem * _px) if (side is TradeType.SELL and _rem > 0 and _px > 0)
                else max(Decimal(0), self.total_amount_quote - self.entry_quote)
            )
        except Exception:  # noqa: BLE001  # policy: degrade-ok(telemetry only)
            pass
        await self.orchestrator.stop(ex.id)  # type: ignore[attr-defined]
        slip = self.cross_slippage_bp / Decimal(10000)
        tick = self._tick()
        if side is TradeType.BUY:
            self._sync_buy_progress(ex)
            remaining_quote = self.total_amount_quote - self.entry_quote
            if remaining_quote <= 0:
                return True
            px = self._snap(ask * (Decimal(1) + slip), tick, up=True)
            amount = remaining_quote / px
            ok, new_ex = await self._spawn_order(
                TradeType.BUY, amount, px, kind="buy_cross",
                execution=ExecutionStrategy.LIMIT, crosses_book=True,
            )
            if ok and new_ex is not None:
                self.buy_id = new_ex.id
                self.leg_quoted_ts = time.time()
                return True
            self._complete("buy_spawn_failed")
            return False
        self._book_sell_fill(ex)
        remaining = self.entry_base - self.sold_base
        if remaining <= 0:
            self._finish_cycle_and_continue_marker = True
            return True
        px = bid * (Decimal(1) - slip)
        floor = self._sell_floor()
        if floor > 0 and px < floor:
            # Crossing would exceed the per-cycle loss budget: keep resting at
            # the floor instead (bounded loss beats unbounded, but never blow
            # through the user's cost cap silently).
            logger.warning(
                "volume_bot: cross skipped — bid %s below loss floor %s; "
                "sell keeps resting pair=%s controller=%s",
                px, floor, self.trading_pair, self.id,
            )
            return await self._start_sell_cycle(remaining)
        px = self._snap(px, tick, up=False)
        ok, new_ex = await self._spawn_order(
            TradeType.SELL, remaining, px, kind="sell_cross",
            execution=ExecutionStrategy.LIMIT, crosses_book=True,
            position_action=PositionAction.CLOSE,
        )
        if ok and new_ex is not None:
            self.sell_id = new_ex.id
            self.close_base_remaining = remaining
            self.leg_quoted_ts = time.time()
            return True
        self._complete("sell_spawn_failed")
        return False

    # -- market-hours wait ----------------------------------------------------

    def _enter_market_closed(self) -> None:
        self.market_closed = True
        if self.phase not in ("done",):
            self.phase = "market_closed"
        if not self._market_closed_logged:
            self._market_closed_logged = True
            logger.warning(
                "volume_bot: no live book for %s (market closed?) — waiting, "
                "no orders placed controller=%s",
                self.trading_pair, self.id,
            )

    def _exit_market_closed(self) -> None:
        if self.market_closed:
            logger.info(
                "volume_bot: book is live again for %s — resuming controller=%s",
                self.trading_pair, self.id,
            )
        self.market_closed = False
        self._market_closed_logged = False

    # -- fill accounting -------------------------------------------------------

    def _sell_remainder_placeable(self, remaining: Decimal) -> bool:
        """A residue below the venue lot / min-notional cannot be re-sold."""
        try:
            lot = self.adapter.lot_size(self.trading_pair)
            min_notional = self.adapter.min_notional(self.trading_pair)
        except Exception:  # policy: degrade-ok(assume placeable; the spawn itself is the arbiter)
            return True
        if lot > 0 and remaining < lot:
            return False
        if min_notional > 0 and self.entry_price > 0 and remaining * self.entry_price < min_notional:
            return False
        return True

    def _sync_buy_progress(self, buy_ex: object) -> None:
        order = getattr(buy_ex, "order", None)
        if order is None:
            return
        filled_base = _dec(getattr(order, "filled_base", 0) or 0)
        if filled_base <= 0:
            return
        self.entry_base = filled_base
        self.entry_quote = _dec(getattr(order, "filled_quote", 0) or 0)
        self.entry_fee_quote = _dec(getattr(order, "fee_quote", 0) or 0)
        self.entry_price = self.entry_quote / self.entry_base if self.entry_base > 0 else Decimal(0)
        if self.entry_fill_ts <= 0:
            self.entry_fill_ts = time.time()

    def _merge_buy_fill(self, buy_ex: object) -> None:
        """Accumulate a cross-order buy fill ON TOP of the maker portion (the
        maker executor was already folded into entry_* before the cross)."""
        order = getattr(buy_ex, "order", None)
        if order is None:
            return
        filled_base = _dec(getattr(order, "filled_base", 0) or 0)
        if filled_base <= 0:
            return
        self.entry_base += filled_base
        self.entry_quote += _dec(getattr(order, "filled_quote", 0) or 0)
        self.entry_fee_quote += _dec(getattr(order, "fee_quote", 0) or 0)
        self.entry_price = self.entry_quote / self.entry_base if self.entry_base > 0 else Decimal(0)
        if self.entry_fill_ts <= 0:
            self.entry_fill_ts = time.time()

    def _book_sell_fill(self, sell_ex: object) -> None:
        """Book a sell executor's fill exactly once."""
        order = getattr(sell_ex, "order", None)
        sid = str(getattr(sell_ex, "id", "") or "")
        if order is not None and sid and sid not in self._accounted_sells:
            self._accounted_sells.add(sid)
            self.sold_base += _dec(getattr(order, "filled_base", 0) or 0)
            self.sold_quote += _dec(getattr(order, "filled_quote", 0) or 0)
            self.sold_fee_quote += _dec(getattr(order, "fee_quote", 0) or 0)
        self.close_base_remaining = max(Decimal(0), self.entry_base - self.sold_base)

    def _attribute_fill(self, *, crossed: bool) -> None:
        """Count a booked leg as maker or taker for the execution report."""
        if crossed:
            self.taker_fills += 1
        else:
            self.maker_fills += 1

    def _finish_cycle(self) -> None:
        """Book the completed round trip and reset per-cycle state."""
        self.session_volume_usd += self.sold_quote
        self.session_realized_pnl_usd += (
            self.sold_quote - self.entry_quote - self.entry_fee_quote - self.sold_fee_quote
        )
        self.cycles_completed += 1
        self.close_base_remaining = Decimal(0)
        self.buy_retries = 0
        self.sell_attempts = 0
        # _MAX_REQUOTES_PER_CYCLE says PER CYCLE, but this counter was never
        # reset — making it a per-SESSION budget. The taker rest-stall rescue
        # is bounded by it, so after 120 cumulative requotes the rescue would
        # stop firing and the stall would come back (self-review 2026-07-31).
        self.requotes = 0

    # Set by requote/cross paths when the remainder went to zero mid-transition
    # (the fill landed between our cancel and the re-place).
    _finish_cycle_and_continue_marker = False

    async def _after_cycle(self) -> None:
        self._finish_cycle()
        self.sell_id = None
        self.phase = "cycle_gap"
        if self._emergency_exit_reason:
            # The bag was flattened by the controller's own stop — do NOT open
            # a fresh cycle on top of a stop that just fired.
            self._complete(self._emergency_exit_reason)
        elif self._session_loss_breached():
            # Cumulative realized PnL net of fees hit the user's limit
            # (default 5% of margin). Stop here: the cycle just closed, so the
            # book is flat and nothing is left exposed.
            logger.warning(
                "volume_bot: session loss limit hit — net PnL %s <= -%s after "
                "%s cycles; stopping pair=%s controller=%s",
                self.session_realized_pnl_usd, self.session_loss_limit_usd,
                self.cycles_completed, self.trading_pair, self.id,
            )
            self._complete("session_loss_limit")
        elif self.target_volume_usd <= 0:
            self._complete("round_trip_complete")
        elif self._target_reached():
            self._complete("target_volume_hit")
        elif self.cycles_completed >= self.max_cycles:
            self._complete("max_cycles")
        else:
            await self._start_buy_cycle()

    # -- lifecycle --------------------------------------------------------------

    async def on_start(self) -> None:
        await self._start_buy_cycle()

    async def on_tick(self) -> None:
        for ex in self.my_executors(active_only=True):
            await self.orchestrator.tick(ex.id)

        if self._finish_cycle_and_continue_marker:
            self._finish_cycle_and_continue_marker = False
            await self._after_cycle()
            return

        if self.phase == "market_closed":
            # No inventory → try to start a buy; inventory held → resume the
            # sell. Both paths re-check the book and fall back to waiting.
            if self.entry_base - self.sold_base > 0:
                await self._start_sell_cycle(self.entry_base - self.sold_base)
            else:
                await self._start_buy_cycle()
            return

        if self.phase == "hold_for_profit":
            # v4.1 removed the patient hold, but a session that was RESTARTED
            # while parked in this phase (or an in-flight worker mid-deploy)
            # can still land here holding base. Sell it immediately rather
            # than leaving the user exposed in a phase nothing else services.
            remaining = self.entry_base - self.sold_base
            if remaining <= 0:
                await self._after_cycle()
                return
            await self._start_sell_cycle(remaining)
            return

        now = time.time()

        if self.phase == "pending_fill" and self.buy_id is not None:
            buy_ex = self.orchestrator.get(self.buy_id)
            if buy_ex is None:
                return
            was_cross = self.last_order_kind == "buy_cross" and buy_ex.id == self.buy_id
            if not buy_ex.is_terminated:
                if not was_cross:
                    # Live partial visibility (metrics + market-closed sell
                    # sizing). Cross orders merge at termination instead —
                    # syncing them here would overwrite the maker portion.
                    self._sync_buy_progress(buy_ex)
                leg_age = now - self.leg_started_ts if self.leg_started_ts else 0.0
                quote_age = now - self.leg_quoted_ts if self.leg_quoted_ts else 0.0
                # Cross-on-deadline is a MAKER mechanic (we are already
                # crossing in taker mode), so it stays off here.
                if (
                    not self.taker_mode
                    and not self.leg_crossed
                    and self._leg_should_cross(self.total_amount_quote, self.entry_quote)
                ):
                    await self._cross_leg(buy_ex, TradeType.BUY)
                    return
                # TAKER-REST-STALL: a marketable limit that could not fully
                # sweep RESTS. Without this the leg would sit forever holding
                # a partial fill, with both maker rescues disabled.
                if (
                    self.taker_mode
                    and self.taker_fill_timeout_seconds > 0
                    and quote_age >= self.taker_fill_timeout_seconds
                    and self.requotes < self._MAX_REQUOTES_PER_CYCLE
                ):
                    await self._requote_buy(buy_ex)
                    return
                if (
                    not self.taker_mode
                    and self.chase_interval_seconds > 0
                    and not was_cross
                    and quote_age >= self.chase_interval_seconds
                    and self.requotes < self._MAX_REQUOTES_PER_CYCLE
                ):
                    # CHASE: re-post at the fresh touch so the resting quote
                    # keeps price-time priority as the book moves.
                    self.chases += 1
                    await self._requote_buy(buy_ex)
                return
            if was_cross:
                self._merge_buy_fill(buy_ex)
            else:
                self._sync_buy_progress(buy_ex)
            if self.entry_base > 0:
                # Fully or PARTIALLY filled then terminated: round-trip what we
                # actually hold. Completing here without selling would strand
                # the bought base in the user's wallet.
                self._attribute_fill(crossed=self.taker_mode or self.leg_crossed)
                self.session_volume_usd += self.entry_quote
                self.phase = "filled_wait_close"
                await self._start_sell_cycle()
                return
            # Terminated with zero fill (post-only reject / venue cancel):
            # requote a bounded number of times before giving up.
            if self.buy_retries < self._MAX_BUY_RETRIES:
                self.buy_retries += 1
                logger.warning(
                    "volume_bot: buy terminated unfilled; requoting (%s/%s) "
                    "pair=%s controller=%s",
                    self.buy_retries, self._MAX_BUY_RETRIES, self.trading_pair, self.id,
                )
                await self._start_buy_cycle()
                return
            self._complete("no_fill")
            return

        if self.phase == "filled_wait_close":
            # A sell spawn raised mid-transition on a previous tick. Retry
            # rather than strand the held base in a phase no branch serviced.
            if self.sell_attempts < self._MAX_SELL_ATTEMPTS:
                self.sell_attempts += 1
                await self._start_sell_cycle()
            else:
                self._complete("sell_spawn_failed")
            return

        if self.phase == "cycle_gap":
            # The next cycle's buy spawn raised mid-transition. Retry bounded.
            if self.buy_retries < self._MAX_BUY_RETRIES:
                self.buy_retries += 1
                await self._start_buy_cycle()
            else:
                self._complete("buy_respawn_failed")
            return

        if self.phase == "pending_close_fill" and self.sell_id is not None:
            sell_ex = self.orchestrator.get(self.sell_id)
            if sell_ex is None:
                return
            if not sell_ex.is_terminated:
                order = getattr(sell_ex, "order", None)
                if order is not None:
                    live_filled = _dec(getattr(order, "filled_base", 0) or 0)
                    self.close_base_remaining = max(
                        Decimal(0), self.entry_base - self.sold_base - live_filled
                    )
                leg_age = now - self.leg_started_ts if self.leg_started_ts else 0.0
                quote_age = now - self.leg_quoted_ts if self.leg_quoted_ts else 0.0
                was_cross = self.last_order_kind == "sell_cross"
                # Maker-only rescue mechanic (see the buy leg above).
                if (
                    not self.taker_mode
                    and not self.leg_crossed
                    and self._leg_should_cross(self._sell_leg_notional(), self.sold_quote)
                ):
                    await self._cross_leg(sell_ex, TradeType.SELL)
                    return
                # TAKER-REST-STALL: a sell that could not fully sweep RESTS,
                # leaving the user holding the unsold remainder. Re-place it at
                # the fresh bid — an exit must never be left to linger.
                if (
                    self.taker_mode
                    and self.taker_fill_timeout_seconds > 0
                    and quote_age >= self.taker_fill_timeout_seconds
                    and self.requotes < self._MAX_REQUOTES_PER_CYCLE
                ):
                    await self._requote_sell(sell_ex)
                    return
                if (
                    not self.taker_mode
                    and self.chase_interval_seconds > 0
                    and not was_cross
                    and quote_age >= self.chase_interval_seconds
                    and self.requotes < self._MAX_REQUOTES_PER_CYCLE
                ):
                    self.chases += 1
                    await self._requote_sell(sell_ex)
                    return
                return
            self._book_sell_fill(sell_ex)
            self._attribute_fill(crossed=self.taker_mode or self.leg_crossed)
            remaining = self.entry_base - self.sold_base
            if remaining > 0:
                # Partial close: re-place the remainder unless it is venue dust
                # or we are out of attempts — never quietly strand inventory.
                if (
                    self.sell_attempts < self._MAX_SELL_ATTEMPTS
                    and self._sell_remainder_placeable(remaining)
                ):
                    self.sell_attempts += 1
                    await self._start_sell_cycle(remaining)
                    return
                if self._sell_remainder_placeable(remaining):
                    # AUDIT-VOL-2026-07-31 F4: attempts are exhausted but this
                    # is a REAL, sellable position — not dust. Falling through
                    # to _after_cycle() would zero entry_base in the next
                    # _start_buy_cycle and buy AGAIN on top of it, accumulating
                    # orphaned spot the controller can no longer see (and
                    # booking a phantom full-notional realized loss). End the
                    # session instead and leave the bag visible to the user.
                    logger.error(
                        "volume_bot: %s base still unsold after %s attempts — "
                        "STOPPING the session rather than buying on top of it "
                        "pair=%s controller=%s",
                        remaining, self.sell_attempts, self.trading_pair, self.id,
                    )
                    # Book what this cycle DID trade before stopping —
                    # returning straight to _complete skipped _finish_cycle,
                    # losing the sold volume and the realized loss from the
                    # session totals (and so from the loss limit).
                    self._finish_cycle()
                    self.close_base_remaining = remaining
                    self._complete("unsold_inventory")
                    return
                logger.warning(
                    "volume_bot: %s base unsold after %s sell attempts "
                    "(venue dust) — finishing cycle with the residue held "
                    "pair=%s controller=%s",
                    remaining, self.sell_attempts, self.trading_pair, self.id,
                )
            await self._after_cycle()

    def volume_metrics(self) -> dict:
        volume_done = self.session_volume_usd
        remaining = max(Decimal(0), self.target_volume_usd - volume_done)
        return {
            "vol_phase": self.phase,
            "volume_done_usd": float(volume_done),
            "volume_remaining_usd": float(remaining),
            "session_volume_usd": float(volume_done),
            "session_realized_pnl_usd": float(self.session_realized_pnl_usd),
            "vol_cycles_completed": int(self.cycles_completed),
            "vol_entry_size": float(self.entry_base),
            "vol_entry_quote": float(self.entry_quote),
            "vol_entry_price": float(self.entry_price),
            "vol_entry_fill_ts": float(self.entry_fill_ts or 0.0),
            "vol_close_size": float(self.close_base_remaining),
            # AUTHORITATIVE "how much base does the BOT still hold right now".
            # The stop sweep sizes from this. vol_entry_size is the last cycle's
            # BUY size and is never reset by _finish_cycle, so using it as a
            # fallback let a FLAT bot authorise a sweep capped at the last
            # cycle's size — which min()s against the wallet and sells the
            # USER'S OWN pre-existing spot (self-review 2026-07-31).
            # AUTHORITATIVE "how much base does the BOT still hold right now".
            # KNOWN WINDOW: this is refreshed when the controller ticks, so a
            # manual stop landing between a venue fill and the next tick (~5s)
            # sizes 0 and leaves that base in the wallet. Reading the executor's
            # cached order does NOT close it (the snapshot is equally stale),
            # and capping by the ORDER SIZE instead would let an UNFILLED buy
            # authorise selling the user's own pre-existing spot — strictly
            # worse, since that is irreversible. The rail path is already
            # covered: bot_runtime merges these counters before it can fire.
            "vol_open_base": float(max(Decimal(0), self.entry_base - self.sold_base)),
            # Expected all-in cost of one taker round trip (both legs' fees;
            # the spread is on top and varies with the book). Surfaced so the
            # user can see WHY the session loss limit approaches — with no
            # profit gate, every cycle costs this by construction.
            "vol_execution_algo": self.execution_algo,
            "vol_maker_fills": int(self.maker_fills),
            "vol_taker_fills": int(self.taker_fills),
            "vol_chases": int(self.chases),
            "vol_crossed_quote": float(self.crossed_quote),
            # Share of legs that rested (paid maker) rather than crossed. This
            # is the number that proves the ~4x volume-per-loss-budget claim is
            # being realised in production rather than just in the design doc.
            "vol_maker_fill_ratio": (
                float(self.maker_fills) / float(self.maker_fills + self.taker_fills)
                if (self.maker_fills + self.taker_fills) > 0 else 0.0
            ),
            "vol_round_trip_fee_bp": float(self._taker_all_in_rate() * Decimal(20000)),
            "vol_session_loss_limit_usd": float(self.session_loss_limit_usd),
            "vol_requotes": int(self.requotes),
            "vol_crosses": int(self.crosses),
            "vol_market_closed": bool(self.market_closed),
            # Legacy metric names kept for the strategy card / status readers.
            "vol_buy_reprices": int(self.requotes),
            "vol_last_order_digest": self.last_order_digest,
            "vol_last_order_kind": self.last_order_kind,
        }
