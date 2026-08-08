"""Reverse Grid (R-Grid) — its own market-making strategy.

R-Grid is NOT a grid variant and NOT a phase switcher (that is D-Grid). Grid says
"don't buy above where I sold, don't sell below where I bought", which works in a
range and gets stuck when price trends out of it. R-Grid inverts that so a trend
pays:

    anchor = (buy exposure price + sell exposure price) / 2

    buy  trigger = anchor x (1 + spread)   → acts as price RISES above it
    sell trigger = anchor x (1 - spread)   → acts as price FALLS below it

Both legs reference the MIDPOINT between where the book has bought and where it
has sold — unlike grid, which mirrors the opposite leg's price directly. Each
leg's *exposure price* is a rolling VWAP over the most recent portion of that
leg's filled volume (``vwap_volume_fraction``, driven by the user's discretion
knob), which is a steadier reference than a single last fill.

ENTRIES REST, EXITS CROSS (2026-08-08, product ruling).

The ADDING leg is a POST-ONLY limit order (:class:`RGridMakerExecutor`), and the
geometry is maker by construction: a bid parked ABOVE the anchor becomes fillable
exactly once price has risen past it, because only then is it a resting bid BELOW
market that a seller can hit; symmetrically for the ask. So the fill IS the
momentum signal without paying the spread. While flat, both sides rest and
whichever fills sets the direction.

Every EXIT crosses — the exposure-band exit, the armed trailing stop, and the
session rail's close. All three are MARKET and reduce-only. The exposure-band exit
used to rest post-only, and that was the source of a whole class of defects: its
level is anchor*(1-band) for a long, i.e. BELOW mid, which is the shape of a stop
rather than a quote, so the order declined below the venue minimum (leaving a
residual with no exit while the entry leg kept adding), was cancelled outright
whenever the trail armed above it, and left a stub on a partial fill. An exit that
may not fill is not an exit.

Safety, in layers:

* **Session SL / TP** — % of margin on live PnL including uPnL, judged net of
  fees. Owned by ``strategy/bot_runtime._evaluate_session_pnl_rail``; this
  controller never second-guesses it, and never applies the same user number as a
  price barrier too (the units invariant).
* **Soft reset** — once price has moved favourably by ``reset_threshold_pct`` AND
  the position is in profit, the exit level starts FOLLOWING the trend: it trails
  the best price by one spread instead of sitting at the anchor, so the run keeps
  going and the profit is locked. It fires by CROSSING, like every exit here.
* **Reversal recalibration** — an exit always closes the WHOLE position in one
  order, so a turn books all of it at once rather than a step per tick while the
  move runs. Once flat the window clears and the next move picks the side.
* **Net-exposure cap** — inherited from MarketMakingController, and never
  disabled. The regime gate ships OFF (it pauses on TRENDS, which is when R-Grid
  must act); the cap, the soft reset and the session rails are the backstops.

R-Grid is never STOOD DOWN by the financial overlay
(``overlay_actuator.NEVER_SUPPRESSED``). Pausing a trend follower in a trend is
backwards, and pausing it mid-position is worse — nobody is left managing the
exit. The overlay protects it two other ways instead: it shades size and trigger
width through the mapped config, and a regime read that CONTRADICTS the open
position arms the trailing exit as soon as the position is in profit, rather than
waiting for the full threshold. Both reduce risk without abandoning the book.
"""
from __future__ import annotations

import logging
from collections import deque
from decimal import ROUND_DOWN, Decimal
from typing import Deque, Dict, Optional, Tuple

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.executors.rgrid_maker_executor import (
    LEG_ENTRY,
    LEG_EXIT,
    LEG_TRAIL_STOP,
    RGridMakerExecutor,
    build_maker_quote,
    build_trail_stop,
)
from src.nadobro.engine.risk import ExecutorRequest
from src.nadobro.engine.types import PositionAction, TradeType, _dec

logger = logging.getLogger(__name__)

# Retained fills per leg. Large enough that a volume-fraction window has history.
_FILL_HISTORY = 200
# Absolute floor on the soft-reset arm: a "profit" smaller than the round-trip
# round-trip cost is not profit. Kept at the TAKER round trip (8.6bp) as a
# deliberately conservative floor: R-Grid rests maker quotes, so its real cost is
# lower and this only ever makes the arm harder to reach, never easier.
_MIN_ARM_PCT = Decimal("0.00086")
# How far through the touch a crossing exit prices. Wide enough to cross a normal
# book, bounded so a gapped book cannot fill it at an arbitrary price.
_EXIT_CROSS_BP = Decimal("30")
# After this many consecutive refused exits, stop pretending a retry-only posture
# is safe and say so loudly (the rail still owns the hard stop).
_MAX_CONSECUTIVE_EXIT_FAILURES = 5

BUY, SELL = "buy", "sell"


class RGridController(MarketMakingController):
    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("name", "rgrid")
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # Per-leg exposure windows. The anchor is the average of the two legs, so
        # they must be tracked separately: one blended VWAP over both sides weights
        # whichever leg traded more volume, and a book that bought 3 units at 100
        # and sold 1 at 110 would anchor at 102.5 instead of the 105 midpoint.
        self._leg_fills: Dict[str, Deque[Tuple[Decimal, Decimal]]] = {
            BUY: deque(maxlen=_FILL_HISTORY),
            SELL: deque(maxlen=_FILL_HISTORY),
        }
        self._seen_filled: set[str] = set()
        # Seeded to mid on the first tick so the FIRST trigger is a real ±band move
        # rather than an instant one-sided entry.
        self._anchor: Optional[Decimal] = None
        self._last_anchor: Optional[Decimal] = None
        # Last mid seen, so grid_metrics() can report drift from the anchor without
        # an extra venue read (the /status card renders it every refresh).
        self._last_mid: Optional[Decimal] = None
        # One resting post-only quote per side, tracked by side so each leg can be
        # sized and reduce-only independently (the shared MM ladder cannot express
        # "one step on the adding side, the whole position on the reducing side").
        self._resting: Dict[TradeType, str] = {}
        self._resting_price: Dict[TradeType, Decimal] = {}
        # The armed trailing stop is the one order that crosses; never stack two.
        self._stop_id: Optional[str] = None
        # Exposure-price window as a fraction of each leg's recent fill VOLUME.
        # 0 ⇒ VWAP over the whole retained window.
        self.vwap_volume_fraction = _dec(self.cfg("vwap_volume_fraction", "0") or "0")
        # Soft reset: arm threshold (favourable move, fraction of price).
        self.reset_threshold_pct = _dec(self.cfg("reset_threshold_pct", "0.002"))
        self.trail_enabled = bool(self.cfg("trail_enabled", True))
        self._trail_peak: Optional[Decimal] = None
        self._trail_armed = False
        # Overlay telemetry only — the actuator has already applied its effect to
        # size/spread/exposure in the mapped config before this controller sees it.
        self.signal_regime = str(self.cfg("signal_regime", "") or "")
        self.signal_confidence = float(self.cfg("signal_confidence", 0.0) or 0.0)
        # Below this the overlay's read is not confident enough to change anything.
        self.signal_min_confidence = float(self.cfg("rgrid_signal_min_confidence", 0.45) or 0.45)
        # SESSION ISOLATION: the anchor must reflect THIS run's fills. In-memory
        # absorption already guarantees that (my_executors is scoped to this
        # controller_id), but a rebuild (worker handoff / restart) would start
        # blank. The runtime injects ``seed_fills`` — this session's OWN recorded
        # trades — so the anchor survives a rebuild and never sees another
        # user/session/product.
        # Consecutive refused exits, and which legs have a LIVE (this-process) fill.
        # Both exist because a REBUILD is not a fresh start: the controller can come
        # back holding a position, with an exposure window seeded from the whole
        # session's fills.
        self._exit_failures = 0
        self._live_fill_legs: set[str] = set()
        self._seed_from_history(self.cfg("seed_fills", None))

    # -- exposure window -----------------------------------------------------
    def _seed_from_history(self, rows: object) -> None:
        if not rows or not isinstance(rows, (list, tuple)):
            return
        parsed: list[Tuple[Decimal, Decimal, str]] = []
        for row in rows:
            if isinstance(row, dict):
                px_raw, base_raw, side = row.get("price"), row.get("size"), row.get("side")
            elif isinstance(row, (list, tuple)) and len(row) >= 3:
                px_raw, base_raw, side = row[0], row[1], row[2]
            else:
                continue
            try:
                px = _dec(px_raw)
                base = abs(_dec(base_raw))
            except Exception:  # policy: degrade-ok(skip a malformed seed; live fills re-anchor)
                continue
            if px <= 0 or base <= 0:
                continue
            leg = BUY if str(side or "").lower() in ("long", "buy") else SELL
            parsed.append((px, base, leg))
        # rows arrive newest-first; append oldest-first so window order matches live.
        for px, base, leg in reversed(parsed):
            self._leg_fills[leg].append((px, base))

    def _absorb_fills(self) -> None:
        """Fold newly FILLED quotes into their leg's exposure window.

        BOTH legs are absorbed, and that is essential: the anchor is defined as the
        average of the buy AND sell exposure prices, so excluding the reducing leg
        would leave the sell exposure price permanently undefined and the "average"
        would only ever be the buy VWAP.

        This is safe precisely BECAUSE the quotes are makers. The exclusion existed
        when exits were market orders: a close printed at whatever the market
        offered, dragging the anchor to the exit price and re-triggering an entry a
        tick later. A resting post-only fill happens at a price the strategy CHOSE
        (anchor x (1 -+ spread), or the trailing price once armed), which is real
        exposure information — exactly as in Grid, where "a sell that happens to
        close a long is the strategy working, and must re-anchor".
        """
        for ex in self.my_executors(active_only=False):
            if ex.id in self._seen_filled:
                continue
            order = getattr(ex, "order", None)
            # NOT gated on state is FILLED. The adapter reports a partially filled
            # order that is no longer resting as CANCELLED with the fill amounts
            # preserved (adapter/nado.py). Inventory and the reporting bridge both
            # record that fill, so gating on FILLED left a REAL position the
            # exposure window had never seen: the anchor stayed at the stale
            # reference and the same-direction break could re-fire immediately.
            # Terminal + any fill is the correct condition. (Audit 2026-08-06.)
            if order is None or not ex.is_terminated:
                continue
            if abs(_dec(order.filled_base)) <= 0:
                continue
            self._seen_filled.add(ex.id)
            base = abs(_dec(order.filled_base))
            quote = abs(_dec(order.filled_quote))
            if base <= 0:
                continue
            px = quote / base
            side = getattr(getattr(ex, "config", None), "side", None)
            if side is TradeType.BUY:
                self._leg_fills[BUY].append((px, base))
                self._live_fill_legs.add(BUY)
            elif side is TradeType.SELL:
                self._leg_fills[SELL].append((px, base))
                self._live_fill_legs.add(SELL)
        if len(self._seen_filled) > 200:
            live = {e.id for e in self.my_executors(active_only=False)}
            self._seen_filled &= live

    def _windowed_vwap(self, fills: "Deque[Tuple[Decimal, Decimal]]") -> Optional[Decimal]:
        if not fills:
            return None
        total_base = sum((b for _, b in fills), Decimal(0))
        if total_base <= 0:
            return None
        if self.vwap_volume_fraction > 0:
            want = total_base * self.vwap_volume_fraction
            num = den = Decimal(0)
            for px, base in reversed(fills):  # most recent first
                num += px * base
                den += base
                if den >= want:
                    break
            return num / den if den > 0 else None
        return sum((px * base for px, base in fills), Decimal(0)) / total_base

    def leg_exposure_price(self, side: str) -> Optional[Decimal]:
        """One leg's exposure price: the windowed VWAP of its own fills."""
        return self._windowed_vwap(self._leg_fills.get(str(side).lower()) or deque())

    def exposure_anchor(self, mid: Optional[Decimal] = None) -> Optional[Decimal]:
        """The R-Grid anchor — the average of the two legs' exposure prices.

        Degenerate cases, in order: one leg only ⇒ that leg IS the average of what
        exists (a one-sided book has no midpoint to split); no fills at all ⇒ the
        seeded anchor (mid at session start), so the first trigger is a real break.
        """
        buy_px = self.leg_exposure_price(BUY)
        sell_px = self.leg_exposure_price(SELL)
        if buy_px is not None and sell_px is not None:
            return (buy_px + sell_px) / Decimal(2)
        if buy_px is not None:
            return buy_px
        if sell_px is not None:
            return sell_px
        return self._anchor or mid

    def _reset_exposure_window(self, mid: Decimal) -> None:
        """Flat book: forget the closed position's entries and re-anchor to mid, so
        a genuine fresh break is required instead of re-entering against a stale
        anchor. Also disarms the trail — it belonged to that position."""
        for leg in self._leg_fills.values():
            leg.clear()
        # The window is gone, so the evidence that it was grounded in a live
        # position goes with it: the next position must earn the crossing exit
        # again with its own fill.
        self._live_fill_legs.clear()
        self._anchor = mid
        self._trail_peak = None
        self._trail_armed = False

    def _has_fills(self) -> bool:
        return any(self._leg_fills[leg] for leg in (BUY, SELL))

    # -- resting-quote plumbing ----------------------------------------------

    def _net_base(self) -> Decimal:
        if self.inventory is None:
            return Decimal(0)
        return self.inventory.get(self.user_id, self.trading_pair, self.id).net_amount_base

    def _position_entry_price(self) -> Optional[Decimal]:
        """Cost basis of the CURRENTLY OPEN position — the full VWAP of the leg
        that opened it.

        NOT ``inventory.breakeven``: that is the session-LIFETIME avg buy / avg
        sell, and every leg accumulates into it, including the trail exits. A long
        closed by a SELL leaves that exit inside ``avg_sell_price``, so the next
        short read a large favourable excursion the moment it opened and armed the
        trail instantly — then exited on the first band-width move against it,
        paying ~10bp adverse + 8.6bp taker every cycle. (Audit 2026-08-06.)

        The per-leg windows are the right basis by construction: exits are excluded
        from them (:meth:`_absorb_fills`) and they are cleared whenever the book
        goes flat (:meth:`_reset_exposure_window`), so the leg matching the position
        side holds exactly this position's entries. Unwindowed on purpose — the
        discretion slice is for the *trigger* anchor, whereas a profit measurement
        must span everything actually paid for the position.
        """
        net = self._net_base()
        if net == 0:
            return None
        fills = self._leg_fills[BUY if net > 0 else SELL]
        total_base = sum((b for _, b in fills), Decimal(0))
        if total_base > 0:
            return sum((px * b for px, b in fills), Decimal(0)) / total_base
        # No window (a rebuild that seeded nothing): inventory is the last resort.
        if self.inventory is None:
            return None
        try:
            breakeven = self.inventory.get(self.user_id, self.trading_pair, self.id).breakeven
        except Exception:  # noqa: BLE001  # policy: degrade-ok(fall back to the anchor)
            return None
        return breakeven if (breakeven is not None and breakeven > 0) else None

    def _band(self) -> Decimal:
        """Trigger offset from the anchor — the user's spread, fee-floored."""
        return max(self.spread_ask_pct, self.spread_floor_half_pct)

    def _quantize_quote(self, amount_base: Decimal, price: Decimal) -> Optional[Decimal]:
        """Round a quote DOWN to the venue lot, and decline it below the venue
        minimum notional.

        ``NadoClient.place_order`` GROWS a non-reducing order that lands under the
        minimum, so shipping a sub-minimum quote means resting more than the risk
        engine, the step cap and the stop budget were sized against. Rounding down
        and declining keeps the venue from ever having to bump us.
        """
        try:
            lot = Decimal(str(self.adapter.lot_size(self.trading_pair) or 0))
            floor_quote = Decimal(str(self.adapter.min_notional(self.trading_pair) or 0))
        except Exception:  # noqa: BLE001  # policy: degrade-ok(no metadata ⇒ send as-is)
            return amount_base
        if lot > 0:
            amount_base = (amount_base / lot).to_integral_value(rounding=ROUND_DOWN) * lot
        if amount_base <= 0:
            return None
        if floor_quote > 0 and amount_base * price < floor_quote:
            logger.warning(
                "rgrid: quote notional %.2f is below the venue minimum %.2f — not "
                "resting it rather than letting the venue grow it past the "
                "risk-approved size (user=%s pair=%s)",
                float(amount_base * price), float(floor_quote),
                self.user_id, self.trading_pair,
            )
            return None
        return amount_base

    def _leg_slot(self, side: TradeType) -> Optional[str]:
        return self._resting.get(side)

    async def _cancel_leg(self, side: TradeType) -> None:
        """Drop a resting quote (it moved, or its side is no longer postable)."""
        ex_id = self._resting.pop(side, None)
        if ex_id is None:
            return
        ex = self.orchestrator.get(ex_id)
        if ex is not None and not ex.is_terminated:
            await self.orchestrator.stop(ex_id)

    async def _quote_leg(
        self, side: TradeType, price: Decimal, amount_base: Decimal, *,
        leg: str, allowed: bool, mid: Decimal,
    ) -> None:
        """Reconcile ONE resting post-only leg against its target price.

        Mirrors MarketMakingController._reconcile (forget a terminated quote, drop
        the leg when it is not allowed, hold a resting quote that is still close
        enough, else cancel and re-place) but sized and reduce-only per leg, which
        the shared ladder path cannot express.
        """
        ex_id = self._resting.get(side)
        if ex_id is not None:
            ex = self.orchestrator.get(ex_id)
            if ex is None or ex.is_terminated:
                self._resting.pop(side, None)
                ex_id = None
        if not allowed or price <= 0 or amount_base <= 0:
            await self._cancel_leg(side)
            return
        # Never send a post-only order that would cross: the venue rejects it
        # (error_code 2008) and R-Grid must not cross to force a fill.
        if not self._is_postable(side, price, mid):
            await self._cancel_leg(side)
            return
        # Exposure: include the order we are about to rest, not just filled
        # inventory. Reducing quotes are always admitted by this check.
        if not self._projected_order_within_exposure(side, mid, amount_base * price):
            await self._cancel_leg(side)
            return
        if ex_id is not None:
            resting_px = self._resting_price.get(side)
            if resting_px is not None and self._price_is_close(resting_px, price):
                return          # keep queue position — the target barely moved
            await self._cancel_leg(side)

        quantized = self._quantize_quote(amount_base, price)
        if quantized is None:
            return
        amount_base = quantized
        reduce_only = leg == LEG_EXIT
        cfg = build_maker_quote(
            self.trading_pair, side, amount_base, price,
            leverage=int(self.cfg("leverage", 1) or 1), reduce_only=reduce_only,
        )
        ex = RGridMakerExecutor(
            cfg, user_id=self.user_id, controller_id=self.id,
            adapter=self.adapter, inventory=self.inventory, leg=leg,
        )
        if not await self.spawn_executor(
            ex, ExecutorRequest(
                order_amount_quote=amount_base * price, reduce_only=reduce_only,
                position_action=cfg.position_action,
            )
        ):
            return
        self._resting[side] = ex.id
        self._resting_price[side] = price

    def _trail_breached(self, mid: Decimal, net: Decimal) -> bool:
        """Has price come back through the armed trailing level?

        Long: mid at/below ``peak x (1 - band)``. Short: mid at/above
        ``trough x (1 + band)``. This is exactly the condition a resting post-only
        order cannot express, which is why the stop crosses.
        """
        if not self._trail_armed or net == 0 or self._trail_peak is None or mid <= 0:
            return False
        trigger = self._trail_price(net)
        return mid <= trigger if net > 0 else mid >= trigger

    def _stop_in_flight(self) -> bool:
        if self._stop_id is None:
            return False
        ex = self.orchestrator.get(self._stop_id)
        if ex is None or ex.is_terminated:
            self._stop_id = None
            return False
        return True

    async def _fire_trail_stop(
        self, net: Decimal, mid: Decimal, *, reason: str = "trailing stop",
    ) -> bool:
        """Cross the spread to close the WHOLE position — the single exemption from
        maker-only, and the only order R-Grid pays the spread on.

        Both resting legs are cancelled first: leaving the maker exit up alongside
        this would let the same position be sold twice (the stop is reduce-only so
        the venue could not over-close, but the second order would re-open the other
        way once the first flattened us).
        """
        await self._cancel_leg(TradeType.BUY)
        await self._cancel_leg(TradeType.SELL)
        side = TradeType.SELL if net > 0 else TradeType.BUY
        # Priced THROUGH the touch so it crosses, but bounded — limit orders only.
        _slip = _EXIT_CROSS_BP / Decimal(10000)
        _px = (mid * (Decimal(1) - _slip) if side is TradeType.SELL
               else mid * (Decimal(1) + _slip))
        cfg = build_trail_stop(
            self.trading_pair, side, abs(net),
            leverage=int(self.cfg("leverage", 1) or 1), price=_px,
        )
        ex = RGridMakerExecutor(
            cfg, user_id=self.user_id, controller_id=self.id,
            adapter=self.adapter, inventory=self.inventory, leg=LEG_TRAIL_STOP,
        )
        if not await self.spawn_executor(
            ex, ExecutorRequest(
                order_amount_quote=abs(net) * mid, reduce_only=True,
                position_action=cfg.position_action,
            )
        ):
            self._exit_failures += 1
            log = (logger.error if self._exit_failures >= _MAX_CONSECUTIVE_EXIT_FAILURES
                   else logger.warning)
            log(
                "rgrid exit REFUSED (%s consecutive): %s of %s still open on %s and "
                "no exit order is working — the risk engine or the kill switch "
                "declined it (reduce-only is exempt from the SIZE caps, but not "
                "from max_open_executors or the kill switch). The session rail is "
                "the only stop left. (user=%s controller=%s)",
                self._exit_failures, "long" if net > 0 else "short", abs(net),
                self.trading_pair, self.user_id, self.id,
            )
            return False
        self._exit_failures = 0
        self._stop_id = ex.id
        logger.info(
            "rgrid crossing exit (%s): mid %s reached the level %s "
            "(peak %s) — crossing to close the %s of %s (user=%s pair=%s)",
            reason, mid, self._trail_price(net), self._trail_peak,
            "long" if net > 0 else "short", abs(net), self.user_id, self.trading_pair,
        )
        return True

    def _is_postable(self, side: TradeType, price: Decimal, mid: Decimal) -> bool:
        """Can this price rest without crossing? A bid must sit below the market
        and an ask above it.

        This is what makes the geometry momentum: the buy leg lives at
        anchor x (1+spread), so it is only postable once price has risen ABOVE it,
        and the sell leg at anchor x (1-spread) only once price has fallen BELOW
        it. The two conditions are mutually exclusive, so at most one leg rests at
        a time and inside the band R-Grid simply waits.
        """
        if mid <= 0:
            return False
        return price < mid if side is TradeType.BUY else price > mid

    def _price_is_close(self, resting: Decimal, target: Decimal) -> bool:
        """Within the configured requote tolerance — leave the order alone.
        Cancel/replace churn destroys queue position, which is the whole edge of a
        maker quote."""
        tol = _dec(self.cfg("price_distance_tolerance", "0.0005") or "0.0005")
        if target <= 0 or tol <= 0:
            return False
        return abs(resting - target) / target <= tol


    def _overlay_opposes(self, long_side: bool) -> bool:
        """Does the financial overlay read the market against this position?

        Used to TIGHTEN protection, never to pause: R-Grid keeps trading, but a
        position the overlay disagrees with gets its exit leg armed as soon as it is
        in profit instead of waiting for the full arm threshold.
        """
        if self.signal_confidence < self.signal_min_confidence:
            return False
        regime = str(self.signal_regime or "").lower()
        return (regime == "trend_down" and long_side) or (regime == "trend_up" and not long_side)

    # -- soft reset ----------------------------------------------------------

    # -- tick ----------------------------------------------------------------
    # -- tick ----------------------------------------------------------------
    async def on_tick(self) -> None:
        # Absorb fills BEFORE pricing: the anchor is defined by them.
        for ex in self.my_executors(active_only=True):
            await self.orchestrator.tick(ex.id)
        self._absorb_fills()

        mid = await self.adapter.mid_price(self.trading_pair)
        if mid <= 0:
            return
        self._last_mid = mid
        if self._anchor is None:
            self._anchor = mid

        exposure = self.exposure_allowed_sides(self.trading_pair, mid)
        allow_buy, allow_sell = exposure["buy"], exposure["sell"]
        # The gate ships OFF for R-Grid; when an operator arms it, honour it as
        # reduce-only rather than a full stop.
        await self.evaluate_quote_gate(self.trading_pair)
        net = self._net_base()
        if self.gate_paused:
            allow_buy = allow_buy and net < 0     # only reduce a short
            allow_sell = allow_sell and net > 0   # only reduce a long

        # Flat and holding stale entries: the closed position's prices still
        # anchor us, and their average sits far from the new mid. Re-anchor so a
        # genuine fresh move is required.
        if net == 0 and self._has_fills():
            self._reset_exposure_window(mid)

        if self._stop_in_flight():
            return          # the crossing stop is working; do not re-quote over it

        anchor = self.exposure_anchor(mid)
        if anchor is None or anchor <= 0:
            await self._cancel_leg(TradeType.BUY)
            await self._cancel_leg(TradeType.SELL)
            return
        self._last_anchor = anchor
        band = self._band()

        # The two legs. Mirror of Grid: the BUY sits ABOVE the anchor and the SELL
        # BELOW it, so each becomes postable only once price has travelled past it
        # — that is the momentum. At most one is postable at a time.
        buy_price = anchor * (Decimal(1) + band)
        sell_price = anchor * (Decimal(1) - band)

        # Soft reset. Once armed, the exit follows the trend.
        #
        # A trailing stop wants to act BELOW the peak (for a long), which is exactly
        # where a resting post-only ask cannot sit. So it CROSSES — the single
        # exemption from maker-only, taken deliberately: the alternative was leaving
        # a position with no exit in the one situation the mechanism exists for.
        # Everything else R-Grid does still rests post-only.
        self._track_trail(mid, net)
        if self._trail_breached(mid, net) and not self._stop_in_flight():
            if await self._fire_trail_stop(net, mid, reason="trailing stop"):
                return

        # THE REDUCING SIDE IS A TRIGGER, NOT A RESTING ORDER.
        #
        # R-Grid's exit level is anchor*(1-band) for a long — BELOW mid, i.e. price
        # has to come DOWN to it, which is precisely the shape of a stop. Resting it
        # post-only made it an order that might never fill: it declined below the
        # venue minimum (leaving a residual with no exit while the entry leg kept
        # adding), it was cancelled outright whenever the trail armed above it, and
        # a partial fill left a stub. So the reducing side now WATCHES mid and
        # crosses the whole position when the level is reached, like the trail stop
        # it sits alongside. Only the ADDING leg still rests post-only — that one
        # has no deadline and earns the spread, which is the whole point of the
        # geometry (a bid parked above the anchor only becomes fillable once price
        # has risen past it, so the fill IS the momentum signal).
        if net != 0 and not self._stop_in_flight() and self._band_exit_trustworthy(net):
            exit_trigger = sell_price if net > 0 else buy_price
            reached = mid <= exit_trigger if net > 0 else mid >= exit_trigger
            if reached:
                if await self._fire_trail_stop(net, mid, reason="exposure band"):
                    return

        # Sizing: only the ADDING leg rests, one step at a time. The reducing side
        # is the trigger above, so there is no resting exit to size.
        step_base = (self.order_amount_quote / mid) if mid > 0 else Decimal(0)
        if net > 0:
            # Long: the sell side is the exit trigger; only the buy adds.
            await self._quote_leg(
                TradeType.BUY, buy_price, step_base,
                leg=LEG_ENTRY, allowed=allow_buy, mid=mid,
            )
            await self._cancel_leg(TradeType.SELL)
        elif net < 0:
            await self._quote_leg(
                TradeType.SELL, sell_price, step_base,
                leg=LEG_ENTRY, allowed=allow_sell, mid=mid,
            )
            await self._cancel_leg(TradeType.BUY)
        else:
            # Flat: both sides are entries, and whichever fills sets the direction.
            await self._quote_leg(
                TradeType.BUY, buy_price, step_base,
                leg=LEG_ENTRY, allowed=allow_buy, mid=mid,
            )
            await self._quote_leg(
                TradeType.SELL, sell_price, step_base,
                leg=LEG_ENTRY, allowed=allow_sell, mid=mid,
            )

    def _band_exit_trustworthy(self, net: Decimal) -> bool:
        """Whether the anchor is grounded in THIS position, not a seeded history.

        ``seed_fills`` restores the exposure window from the whole SESSION's fills
        (``get_session_recent_fills`` is scoped to the session, not the open
        position), and the window is only ever scrubbed on a flat book
        (``net == 0``). So a controller rebuilt while a position is OPEN — a worker
        handoff, a reconfigure — carries prior cycles' prints, including their EXIT
        prices, into the anchor.

        That was survivable when the reducing side merely RESTED: a mispriced quote
        sits there until price reaches it. Now that the band exit CROSSES, a stale
        anchor flattens a healthy position at market on the first tick. Concretely:
        cycle 1 shorts 100 and covers 95; cycle 2 goes long at 96; a rebuild puts
        the anchor at 97.75, whose sell trigger is 97.67 — already above mid, so the
        whole long is dumped at 96 instead of running.

        So the crossing exit waits for one LIVE fill on the position's own side.
        Until then the anchor is only a hint, and the session rail — which reads
        venue PnL, not this window — remains the hard stop. The trailing stop is
        deliberately NOT gated: it arms off observed price extremes, not the window.
        """
        leg = BUY if net > 0 else SELL
        return leg in self._live_fill_legs

    def _track_trail(self, mid: Decimal, net: Decimal) -> None:
        """Update the favourable extreme and the armed flag. No orders here — the
        exit leg's PRICE is the mechanism, so arming only changes where it rests."""
        if net == 0:
            self._trail_peak = None
            self._trail_armed = False
            return
        if not self.trail_enabled or self.reset_threshold_pct <= 0 or mid <= 0:
            return
        long_side = net > 0
        if self._trail_peak is None:
            self._trail_peak = mid
        elif long_side:
            self._trail_peak = max(self._trail_peak, mid)
        else:
            self._trail_peak = min(self._trail_peak, mid)
        if self._trail_armed:
            return
        entry = self._position_entry_price() or self.exposure_anchor(mid)
        if entry is None or entry <= 0:
            return
        excursion = ((mid - entry) / entry) if long_side else ((entry - mid) / entry)
        band = self._band()
        # The arm is WIDENED to clear the band and the round-trip cost, never
        # disabled: the overlay scales the spread live while the threshold is not
        # scaled, and the shipped defaults sit on the boundary, so refusing would
        # have silently removed the mechanism.
        arm_pct = max(self.reset_threshold_pct, band * Decimal(2), _MIN_ARM_PCT)
        # An overlay read AGAINST the position arms early rather than pausing
        # R-Grid — but never underwater, or the trail becomes a stop that
        # front-runs the SL rail.
        opposed = self._overlay_opposes(long_side)
        if excursion < arm_pct and not (opposed and excursion > 0):
            return
        self._trail_armed = True
        logger.info(
            "rgrid soft reset armed%s: %s%% favourable from %s — the exit leg now "
            "follows the trend (user=%s pair=%s)",
            " EARLY (overlay reads the market against this position)" if opposed else "",
            round(float(excursion) * 100, 3), entry, self.user_id, self.trading_pair,
        )

    def _trail_price(self, net: Decimal) -> Decimal:
        """Where the armed exit leg rests: one band back from the best price seen.
        Give-back is capped at one spread and it only ever ratchets forward."""
        peak = self._trail_peak or Decimal(0)
        band = self._band()
        return (
            peak * (Decimal(1) - band) if net > 0
            else peak * (Decimal(1) + band)
        )


    # -- introspection -------------------------------------------------------
    def anchor_state(self) -> dict:
        return {
            "mode": "rgrid",
            "anchor": self._last_anchor or self._anchor,
            "buy_exposure_px": self.leg_exposure_price(BUY),
            "sell_exposure_px": self.leg_exposure_price(SELL),
            "trail_armed": self._trail_armed,
            "trail_peak": self._trail_peak,
        }

    def grid_metrics(self) -> dict:
        """Telemetry for /status and the order-monitor card. Deliberately reuses the
        shared ``grid_*`` keys the runtime already persists, plus rgrid-only ones."""
        anchor = self._last_anchor or self._anchor
        band = self._band()
        up_price = down_price = 0.0
        if anchor and anchor > 0:
            up_price = float(anchor * (Decimal(1) + band))
            down_price = float(anchor * (Decimal(1) - band))
        net_base = 0.0
        if self.inventory is not None:
            net_base = float(self.inventory.get(self.user_id, self.trading_pair, self.id).net_amount_base)
        # SHARED grid telemetry block. These exact key names are what the runtime
        # persists into bot_state and the /status card renders as Anchor / Drift /
        # Side (bot_runtime maps grid_* -> rgrid_* for the card). Emitting fewer of
        # them than the previous controller did leaves the card showing 0.000% /
        # NONE — or worse, the STALE value from the old controller, because the
        # runtime only overwrites a key it is actually given.
        drift_pct = 0.0
        if anchor and anchor > 0 and self._last_mid:
            drift_pct = float((self._last_mid - anchor) / anchor * Decimal(100))
        # Which leg the soft reset is protecting: the EXIT of the open position.
        if not self._trail_armed or net_base == 0:
            reset_side = "NONE"
        else:
            reset_side = "SELL" if net_base > 0 else "BUY"
        return {
            "grid_mode": "rgrid",
            "grid_anchor_price": float(anchor) if anchor else 0.0,
            "grid_drift_from_anchor_pct": drift_pct,
            "grid_reset_side": reset_side,
            "grid_reset_threshold_bp": float(self.reset_threshold_pct * Decimal(10000)),
            "grid_reset_active": bool(self.trail_enabled and self.reset_threshold_pct > 0),
            "grid_soft_reset_engaged": bool(self._trail_armed),
            "grid_net_base": net_base,
            # The two legs the anchor averages — the R-Grid card has always had a
            # "Buy VWAP / Sell VWAP" row and nothing ever populated it.
            "grid_buy_exposure_price": float(self.leg_exposure_price(BUY) or 0),
            "grid_sell_exposure_price": float(self.leg_exposure_price(SELL) or 0),
            # Where the next break fires: buy above the green level, sell below red.
            "grid_reset_up_price": up_price,
            "grid_reset_down_price": down_price,
            "rgrid_buy_trigger": up_price,
            "rgrid_sell_trigger": down_price,
            "rgrid_trail_armed": bool(self._trail_armed),
            "rgrid_trail_peak": float(self._trail_peak) if self._trail_peak else 0.0,
            "rgrid_signal_regime": self.signal_regime,
            "rgrid_signal_confidence": self.signal_confidence,
        }
