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

R-GRID IS A PYRAMIDING TREND FOLLOWER. It takes the side the move is going, ADDS
to that side for as long as the move extends, and flips when the move turns. Four
things had to be true for that to work, and none of them were:

* **The reference must track the market.** While FLAT with an empty window the
  anchor is not exposure at all — it is only what the next break is measured from
  — so it rides a leash behind mid (``_track_flat_anchor``). Frozen on the first
  tick's mid, it made every break relative to whenever the user pressed start.
* **The add must march, not converge.** In a position the add leg is quoted off
  the LAST FILL on that side (``_add_reference``), so each add costs one fresh
  band of trend extension. Off the exposure VWAP — an average — the spacing decays
  toward zero and the pyramid stalls behind its own cost basis.
* **The pyramid must fit its own ceiling.** ``levels x step == deployed``, so the
  net-exposure cap has to admit 100% of the deployed notional. At the shared MM
  default of 30% exactly one step fitted and every add was refused.
* **The profit-taking exit must get its chance first.** The exposure-band exit
  fires at ``avg_entry x (1 - band)`` and is LOSS-ONLY by construction; only the
  trail can book a gain. So the trail arms strictly inside the band exit
  (``_exit_band`` > ``_arm_pct``), and its give-back equals the arm, which puts
  the stop at breakeven the moment it engages. Shipped, those were the other way
  round and the loss-only exit always won.

Measured end to end on the repo's cost-aware backtester through the real mapped
config: the shipped geometry returned **-85.27** across five trending regimes,
this one **+487.19**, and the churn that produced the loss (35-47 fills per trend)
collapsed to 5-6 — it now rides instead of round-tripping.

Safety, in layers:

* **Session SL / TP** — % of margin on live PnL including uPnL, judged net of
  fees. Owned by ``strategy/bot_runtime._evaluate_session_pnl_rail``; this
  controller never second-guesses it, and never applies the same user number as a
  price barrier too (the units invariant).
* **Soft reset** — once price has moved favourably by ``reset_threshold_pct`` AND
  the position is in profit, the exit starts FOLLOWING the trend: it trails the
  best price by one give-back (``_trail_giveback``, == the arm threshold) instead
  of sitting at the anchor, so the run keeps going and the profit is locked. It
  fires by CROSSING, like every exit here.
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

import inspect
import logging
from collections import deque
from decimal import ROUND_DOWN, Decimal
from typing import Deque, Dict, List, Optional, Tuple

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.routines import variance_regime
from src.nadobro.engine.executors.rgrid_maker_executor import (
    LEG_ENTRY,
    LEG_ENTRY_CROSS,
    LEG_EXIT,
    LEG_TRAIL_STOP,
    RGridMakerExecutor,
    build_cross_entry,
    build_maker_quote,
    build_trail_stop,
)
from src.nadobro.engine.risk import ExecutorRequest
from src.nadobro.engine.types import TradeType, _as_bool, _dec
from src.nadobro.quant.rgrid_sizing import (
    EXIT_CROSS_RATE,
    TAKER_ROUND_TRIP_RATE,
    arm_pct,
    exit_band_frac,
)

logger = logging.getLogger(__name__)

# Retained fills per leg. Large enough that a volume-fraction window has history.
_FILL_HISTORY = 200
# The soft-reset arm floor (a "profit" smaller than the round-trip cost is not
# profit) lives inside ``arm_pct`` in quant.rgrid_sizing, which floors on the
# round-trip cost itself — so the arm this controller derives can never sit under
# it, and the step cap agrees with the controller about these distances.
# How far through the touch a crossing exit prices. Wide enough to cross a normal
# book, bounded so a gapped book cannot fill it at an arbitrary price. Shared with
# the step cap (quant.rgrid_sizing.EXIT_CROSS_RATE) — it is realised cost on the way
# out, so the stop budget has to be sized against it too, not just against fees.
_EXIT_CROSS_BP = EXIT_CROSS_RATE * Decimal(10000)
# After this many consecutive refused exits, stop pretending a retry-only posture
# is safe and say so loudly (the rail still owns the hard stop).
_MAX_CONSECUTIVE_EXIT_FAILURES = 5
# Ticks a crossing exit may sit unfilled before it is cancelled and re-priced. The
# executor has no timeout of its own for a plain LIMIT, so without this a gapped
# book or a partial fill freezes the controller indefinitely (see _reap_stale_stop).
_STOP_STALE_TICKS = 3
# Ticks a crossing ADD may sit unfilled before it is cancelled and re-priced through
# the current touch (same reasoning as _STOP_STALE_TICKS, see _reap_stale_add).
_ADD_STALE_TICKS = 3
# How far through the touch a crossing ADD prices. Tighter than the emergency exit
# bound (_EXIT_CROSS_BP=30): an add is not time-critical, so a book that gaps past it
# re-fires next tick via the stale-reap rather than filling at an arbitrary price.
_ADD_CROSS_BP_DEFAULT = Decimal(10)
# Extra spacing (over the trail giveback) each ADD must clear so the MARGINAL add is
# net-of-fee positive: s_add = arm + cushion, and cushion covers the taker round trip
# plus a small edge. See _add_spacing. This lives in add-trigger pricing ONLY — the
# exit geometry (arm, exit_band) is untouched, so the exit_band>arm invariant holds.
_ADD_CUSHION_BP_DEFAULT = Decimal(13)
# How far the FLAT-book anchor may lag mid, in bands. The lag is the whole point —
# it is what makes a break a break — but it has to be bounded or the reference goes
# stale (see _track_flat_anchor). One band is degenerate: it parks the trigger
# exactly ON mid, where it is never strictly postable. Two leaves a full band of
# working room, so a resting entry sits ~one band from the touch and follows price.
_FLAT_ANCHOR_MAX_BANDS = Decimal(2)

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
        # The levels actually worked last tick, so /status reports what the engine
        # is doing rather than re-deriving it from the anchor and the entry band.
        self._last_add_ref: Optional[Decimal] = None
        self._last_exit_band: Optional[Decimal] = None
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
        self._stop_age = 0
        # Crossing-add mode. "maker" (default) = the legacy post-only rest, which
        # cannot fill in a clean trend (0 fills, measured); "cross" = a bounded
        # marketable-limit ADD fired on confirmed momentum. Only ONE crossing add
        # is in flight at a time (mirror of the stop), tracked here.
        self.add_mode = str(self.cfg("add_mode", "maker") or "maker").strip().lower()
        self._add_cross_bp = _dec(self.cfg("add_cross_bp", _ADD_CROSS_BP_DEFAULT))
        self._add_cushion_bp = _dec(self.cfg("add_cushion_bp", _ADD_CUSHION_BP_DEFAULT))
        # Trend gate: only OPEN/ADD in a confirmed trend (exits are never gated).
        # Taker adds in chop are a guaranteed per-false-break bleed. PHASE-0
        # (2026-08-24): default ON regardless of add_mode — a trend follower must
        # never add against the overlay's trend read. The FULL variance-regime
        # gate (stand down in chop) is wired in Phase 1; today this vetoes adds the
        # overlay opposes and is the safe default while that lands.
        self.trend_gate = bool(self.cfg("trend_gate", True))
        # PHASE-1 (2026-08-24): the REAL trend gate. R-Grid is a trend follower, so
        # it must STAND DOWN when there is no trend — trading chop is where it bled
        # all August (measured -2673bp chop at overlay x1.5, commit 5be3b9b). Each
        # tick it classifies the regime from candles (variance_regime, the same
        # routine D-Grid uses) and, unless a directional trend is CONFIRMED, quotes
        # no new entries or adds. Exits are never gated. This can only PREVENT
        # trades (never add risk), so it defaults ON. Owner ruling 2026-08-24:
        # "stand down in chop". Set ``rgrid_chop_stand_down=0`` to disable.
        self.chop_stand_down: bool = _as_bool(self.cfg("rgrid_chop_stand_down", True), True)
        # Regime-classifier params (same DEFAULTS as D-Grid so the two strategies
        # agree on what "a trend" is, but R-Grid-namespaced so no D-Grid phase-
        # switcher key leaks into an R-Grid config).
        self._regime_short_window = int(max(2, int(self.cfg("rgrid_regime_short_window", 4) or 4)))
        self._regime_long_window = int(max(self._regime_short_window + 1,
                                           int(self.cfg("rgrid_regime_long_window", 12) or 12)))
        self._regime_trend_on_vr = float(self.cfg("rgrid_regime_trend_on_vr", 1.25) or 1.25)
        self._regime_range_on_vr = float(self.cfg("rgrid_regime_range_on_vr", 1.15) or 1.15)
        self._regime_trend_drift_pct = float(self.cfg("rgrid_regime_trend_drift_pct", 0.30) or 0.30)
        # Hysteresis state (GRID = ranging/chop = stand down; RGRID = trending).
        self._regime_phase: str = variance_regime.GRID
        # CONFIRMATION DEBOUNCE. A single swing in high-vol chop can show one tick of
        # directional drift; a real trend sustains it. Require N consecutive
        # same-direction drift ticks before the gate admits entries, so swing-chop
        # (drift that flips direction tick-to-tick) never clears the bar. The signal
        # itself is the drift over the long candle WINDOW (~12 bars), not a per-tick
        # read, so even where the venue's ~30s candle cache serves identical candles
        # across ticks (short intervals) the bar still requires a sustained multi-bar
        # move; at normal rgrid intervals each tick also re-reads fresh candles.
        self._trend_confirm_ticks = int(max(1, int(self.cfg("rgrid_trend_confirm_ticks", 3) or 3)))
        self._trend_streak = 0
        self._trend_streak_dir = variance_regime.FLAT
        self._confirmed_trend_dir = variance_regime.FLAT
        # Consecutive ticks the gate has stood the book down purely because the
        # candle feed is empty (insufficient_history), so an operator can tell
        # "no candle data" apart from "no trend" — see _classify_regime.
        self._no_candle_streak = 0
        self._add_id: Optional[str] = None
        self._add_age = 0
        # Exposure-price window as a fraction of each leg's recent fill VOLUME.
        # 0 ⇒ VWAP over the whole retained window.
        self.vwap_volume_fraction = _dec(self.cfg("vwap_volume_fraction", "0") or "0")
        # Soft reset: arm threshold (favourable move, fraction of price).
        self.reset_threshold_pct = _dec(self.cfg("reset_threshold_pct", "0.002"))
        self.trail_enabled = bool(self.cfg("trail_enabled", True))
        self._trail_peak: Optional[Decimal] = None
        # First mid observed while holding THIS position — the arm basis when the
        # seeded window cannot be trusted (see _track_trail).
        self._trail_origin: Optional[Decimal] = None
        self._trail_armed = False
        # Give-back latched at arm so an overlay-widened band cannot move an
        # already-armed stop further away (RGRID-TRAIL-LOOSENS).
        self._latched_giveback: Optional[Decimal] = None
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
        self._latched_giveback = None

    def _has_fills(self) -> bool:
        return any(self._leg_fills[leg] for leg in (BUY, SELL))

    def _track_flat_anchor(self, mid: Decimal) -> None:
        """Keep the FLAT, fill-less reference on a leash behind mid.

        While the window is empty the anchor is not exposure at all — it is purely
        the reference the next break is measured from, and nothing else writes it.
        It was therefore pinned to the mid of the FIRST tick for the entire
        session: ``_reset_exposure_window`` is the only other writer and it is
        gated on ``_has_fills()``, which is False precisely here. So every break
        was measured against wherever price happened to sit when the user pressed
        start, and once price left that level R-Grid was inert for the rest of the
        run — it rested ONE entry at the seeded trigger, price walked away from it,
        and because the anchor moves only on a FILL there was no way back.
        Reproduced at 72 ticks / 82bp of range: one order placed, zero fills, the
        anchor still on its seed and the bid stranded 72bp under the market.

        The leash fixes that without dissolving the break. Re-seeding to mid every
        tick would be the opposite failure — the band could never be breached and
        nothing would ever rest — so the anchor keeps its lag and is only dragged
        along once mid pulls more than ``_FLAT_ANCHOR_MAX_BANDS`` away, in either
        direction. Inside the leash it does not move, so a fresh break still has to
        travel a full band. A fill hands the anchor back to the exposure VWAP and
        this stops applying; only the flat, empty-window state is affected.
        """
        if self._anchor is None or mid <= 0:
            return
        lag = self._band() * max(
            _dec(self.cfg("flat_anchor_lag_bands", _FLAT_ANCHOR_MAX_BANDS)),
            Decimal(1),
        )
        # Cross mode fires the FIRST entry when mid has extended one add-spacing
        # (s_add ≈ arm+cushion, ~33bp) past this reference. The maker leash (2 bands
        # ≈ 20bp) is TIGHTER than s_add, so the flat break trigger would be
        # structurally unreachable — the anchor would drag along with mid before the
        # break ever completes, and cross mode would place nothing. Widen the leash
        # past the trigger so a genuine breakout can accumulate before the drag.
        if self.add_mode == "cross":
            lag = max(lag, self._add_spacing() * Decimal("1.5"))
        clamped = min(max(self._anchor, mid * (Decimal(1) - lag)), mid * (Decimal(1) + lag))
        if clamped != self._anchor:
            self._anchor = clamped

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
        """ENTRY trigger offset from the reference — the user's spread, fee-floored."""
        return max(self.spread_ask_pct, self.spread_floor_half_pct)

    def _exit_geometry(self) -> Tuple[Decimal, Decimal]:
        """The ``(exit_band, arm)`` PAIR. Jointly constrained, so derived together.

        THE INVARIANT: ``exit_band > arm``, always. The exposure-band exit fires at
        ``avg_entry x (1 - exit_band)`` and is LOSS-ONLY by construction; only the
        trail can book a gain. So the arm has to be the NEARER trigger, or on any
        tape whose pullbacks reach one band the loss-only exit always wins — the
        configuration that measured -85.27.

        Kept SEPARATE from the entry band for the original reason: when the two were
        one number the stop stood exactly as far from the position as the break that
        opened it, so the same pullback that qualified as an entry also stopped the
        position out. Traced on the backtester: a +3.14% uptrend with ordinary 12bp
        pullbacks produced 29 fills and -$12.10 realised, the whole trend handed back
        one band at a time.

        Deriving the two INDEPENDENTLY is what broke it (RGRID-EXITBAND-INVERT):
        ``exit_band_cap`` is computed once in ``map_strategy_config`` from the
        UNSCALED spread, while the overlay rescales ``spread_ask_pct`` live by up to
        3x and pushes it here. The cap ceilinged the exit and NOTHING ceilinged the
        arm, so they crossed:  x1.0 -> 10/20/30 ok;  x1.5 -> 15/30/30 INVERTED;
        x3.0 -> 30/60/30 INVERTED. And the overlay only widens the spread BECAUSE
        the tape is volatile, so the inversion armed itself exactly when noise was
        largest.

        The reconciliation shrinks the ARM. It does not widen the exit past the cap
        (bar the degenerate branch below): widening is the pathology the cap exists
        to prevent — the session rail firing before the strategy's own exit, so every
        close becomes a rail flatten.
        """
        band = self._band()
        arm = arm_pct(band, self.reset_threshold_pct)
        exit_band = max(exit_band_frac(band, self.reset_threshold_pct),
                        band * _dec(self.cfg("exit_band_mult", "0") or "0"))
        # CEILING (``exit_band_cap``, from the mapper's stop budget). The derived
        # distance is the one the strategy WANTS; this is the one the stop can
        # AFFORD. Never narrower than the entry band: an exit inside the trigger
        # that opened the position would fire on the noise that entered it.
        cap = _dec(self.cfg("exit_band_cap", "0") or "0")
        if cap > 0:
            exit_band = max(min(exit_band, cap), band)
        if arm < exit_band:
            # The invariant already holds, so NOTHING is touched. This is the exact
            # geometry commit #222 measured at +487 across five trending regimes,
            # and it is the branch taken for every config where the cap does not
            # bind — including every overlay x1.0 case in that run. Proof it is an
            # identity: with A = arm_pct, D = exit_band_frac = A + band, and band > 0,
            # D > A always; so whenever cap <= 0 or cap >= D the pair is returned
            # unmodified.
            return exit_band, arm
        # Inverted: the cap truncated the exit UNDER the derived arm.
        #
        # RECONCILE BY WIDENING THE EXIT, NEVER BY SHRINKING THE ARM (product ruling,
        # 2026-08-13: "when the strategy is in profit, the wins shouldn't be capped").
        # The arm is the profit-taking trigger, so pulling it inward arms the trail
        # sooner and hands the rest of the move back — measured at -205/-328/-233bp
        # across trend/weak-trend/chop at overlay x1.5, because a trend's value is in
        # win SIZE and an early arm truncates exactly that.
        #
        # So the arm stays where the geometry derives it and the exit moves out to one
        # entry band beyond it — the same ``exit = arm + band`` relationship the
        # derived geometry already has, which is why the branch above is an identity.
        #
        # THE TRADE-OFF IS REAL AND DELIBERATE: this puts the exit past what the stop
        # budget affords, so the user's own %-of-margin session rail can now fire
        # before the strategy's own band exit. That is the backstop by design — the
        # rail is fee-aware, judged net, and is the number the user actually set —
        # but it means some closes become rail flattens rather than strategy exits.
        # Logged every time so the choice is visible in prod rather than inferred.
        exit_band = arm + band
        if cap > 0 and exit_band > cap:
            logger.warning(
                "rgrid exit widened past the stop budget user=%s pair=%s band=%s "
                "arm=%s cap=%s -> exit=%s — winners are uncapped by design; the "
                "session SL/TP rail is the backstop from here",
                self.user_id, self.trading_pair, band, arm, cap, exit_band,
            )
        return exit_band, arm

    def _arm_pct(self) -> Decimal:
        """Favourable excursion at which the trailing exit engages. See
        :meth:`_exit_geometry` — jointly derived with the exit band."""
        return self._exit_geometry()[1]

    def _exit_band(self) -> Decimal:
        """How far from the average entry the (loss-only) exposure-band exit fires.
        See :meth:`_exit_geometry`."""
        return self._exit_geometry()[0]

    def _add_spacing(self) -> Decimal:
        """Fresh extension each CROSS-MODE add requires — add-trigger pricing ONLY.

        ``s_add = arm + cushion``. The trailing exit gives back only ``arm``, so
        requiring ``arm + cushion`` of favourable extension before adding makes the
        MARGINAL add net-of-fee positive: it captures ``s_add`` of move and can only
        give back ``arm``, and the cushion covers the taker round trip plus a small
        edge. The cushion is floored at the taker round trip so a mis-set config can
        never make a marginal add a structural loss.

        This is DELIBERATELY not ``_band()`` and is NEVER fed into ``_exit_geometry``
        — the arm and exit band stay derived from the entry band, so the
        ``exit_band > arm`` invariant and the exposure-band backstop are preserved
        exactly. It only moves WHERE the add trigger sits, not the exit machinery.
        """
        cushion = max(self._add_cushion_bp / Decimal(10000), TAKER_ROUND_TRIP_RATE)
        return self._arm_pct() + cushion

    def _add_reference(self, net: Decimal, anchor: Decimal) -> Decimal:
        """The price the ENTRY/ADD leg is quoted around.

        While FLAT this is the leashed anchor — the break reference.

        While a position is OPEN it is the LAST FILL on the position's own side,
        because that is what makes the pyramid march. Quoting the add off the
        exposure VWAP stalls it: a VWAP is an average, so each successive add moves
        it less than the one before and the add level converges while price runs on.
        Traced on the backtester, one leg's adds came at 2005.20, 2007.20, 2008.21,
        2008.88, 2009.38 — steps of 10.0, 5.0, 3.3 and 2.5bp, decaying to nothing.
        Off the last fill each add instead requires one fresh band of trend
        extension, so the level follows price for as long as the move lasts.

        The exit keeps using the exposure anchor, so this cannot move any stop.
        ``add_ref_mode="anchor"`` restores the old reference if an operator needs it.
        """
        if net == 0:
            return anchor
        if str(self.cfg("add_ref_mode", "last_fill") or "last_fill").lower() == "anchor":
            return anchor
        leg = BUY if net > 0 else SELL
        # Only a LIVE fill may price the add — the same evidence rule the crossing
        # exit uses (:meth:`_band_exit_trustworthy`). ``seed_fills`` restores the
        # whole SESSION's prints on a rebuild, exits included, so the newest entry
        # in a seeded window is not necessarily this position's last add. Pricing
        # off one that sits well below mid would rest an add far under the market —
        # exactly the stale-order class this change exists to remove — and it would
        # fill by adding to a long into a collapse. Until this leg fills in THIS
        # process the anchor is the honest reference.
        if leg not in self._live_fill_legs:
            return anchor
        fills = self._leg_fills[leg]
        return fills[-1][0] if fills else anchor

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
        # Never SEND a post-only order that would cross: the venue rejects it
        # (error_code 2008) and R-Grid must not cross to force a fill.
        #
        # But do not PULL one that is already resting AT THIS TARGET. Post-only
        # binds at placement; an order on the book is unaffected by the rule. A bid
        # becomes "unpostable" exactly when mid reaches it — the moment it is about
        # to be hit — so cancelling there withdrew the entry at the one instant it
        # could have filled, and R-Grid only got in when a venue fill beat the next
        # tick (~8-10s in prod).
        #
        # The distinction is REQUIRED, not cosmetic. Holding unconditionally leaks:
        # if the position is flattened out-of-band — the session SL/TP rail, a
        # liquidation, a manual close, none of which run _fire_trail_stop and its
        # two-leg cancel — the next tick is flat, re-anchors to mid, computes an
        # unpostable target, and would leave the DEAD position's add leg resting.
        # That order can re-open exposure the rail just closed. So a resting quote
        # survives only while it is still the quote we want; once the target has
        # moved off it, it is somebody else's order and it goes.
        if not self._is_postable(side, price, mid):
            resting_px = self._resting_price.get(side)
            if ex_id is None or resting_px is None or not self._price_is_close(
                resting_px, price
            ):
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
            self._stop_age = 0
            return False
        ex = self.orchestrator.get(self._stop_id)
        if ex is None or ex.is_terminated:
            self._stop_id = None
            self._stop_age = 0
            return False
        return True

    async def _reap_stale_stop(self) -> bool:
        """Cancel a crossing exit that has stopped working, so it can be re-fired
        at the CURRENT mid. Returns True if the caller should stand down this tick.

        The exit is a bounded marketable limit (``_EXIT_CROSS_BP`` through the
        touch), and ``OrderExecutor`` neither times out nor re-prices a plain
        LIMIT — it terminates only on FILLED / CANCELLED / REJECTED. So on a gapped
        or one-sided book, or after a partial (which reports PARTIALLY_FILLED and
        is NOT terminal), the order rests unfilled and ``on_tick`` returns at the
        in-flight guard every tick, forever: no re-price, no add, no trail update,
        no second attempt. A position with a stranded exit and a frozen controller,
        in exactly the fast-moving tape the exit exists for.

        Ageing it out re-prices through the current mid on the next pass, which is
        what an order that has to act must do. Re-firing is safe by construction:
        it is reduce-only and sized off live inventory, so a partial simply leads
        to a smaller replacement.
        """
        if not self._stop_in_flight():
            return False
        self._stop_age += 1
        if self._stop_age <= _STOP_STALE_TICKS:
            return True
        stop_id, self._stop_id, self._stop_age = self._stop_id, None, 0
        logger.warning(
            "rgrid crossing exit has not filled in %s ticks — cancelling and "
            "re-pricing it through the current touch rather than leaving the "
            "position with a stranded exit (user=%s pair=%s)",
            _STOP_STALE_TICKS, self.user_id, self.trading_pair,
        )
        if stop_id is not None:
            await self.orchestrator.stop(stop_id)
        return False

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

    # -- crossing add (rgrid_add_mode="cross") -------------------------------
    def _add_in_flight(self) -> bool:
        """Is a crossing add currently working? Mirror of :meth:`_stop_in_flight`."""
        if self._add_id is None:
            self._add_age = 0
            return False
        ex = self.orchestrator.get(self._add_id)
        if ex is None or ex.is_terminated:
            self._add_id = None
            self._add_age = 0
            return False
        return True

    async def _reap_stale_add(self) -> bool:
        """Cancel a crossing add that has not filled so it can re-fire at the current
        mid. Returns True if the caller should stand down this tick (add still
        working, not yet stale). Mirror of :meth:`_reap_stale_stop`.

        A bounded marketable LIMIT that a gapped/one-sided book does not fill rests
        forever (OrderExecutor does not time out a plain LIMIT), which would freeze
        the pyramid. Ageing it out re-prices through the current touch. Re-firing is
        safe: it is one step, sized off the same budget, exposure-checked at fire.
        """
        if not self._add_in_flight():
            return False
        self._add_age += 1
        if self._add_age <= _ADD_STALE_TICKS:
            return True
        add_id, self._add_id, self._add_age = self._add_id, None, 0
        if add_id is not None:
            await self.orchestrator.stop(add_id)
        return False

    async def _candles(self) -> List[dict]:
        """Fetch classification candles from the injected provider (run_engine_cycle
        and the backtester both wire one for rgrid). Empty when none is available —
        the classifier then reports insufficient history and the gate stands down."""
        provider = self.cfg("candle_provider")
        if provider is None:
            return []
        try:
            result = provider(self.trading_pair)  # type: ignore[operator]
            if inspect.isawaitable(result):
                result = await result
            return list(result or [])
        except Exception:  # noqa: BLE001  # policy: degrade-ok(no candles -> stand down)
            return []

    async def _classify_regime(self) -> Dict[str, object]:
        """Classify the regime for the chop stand-down gate. Holds the prior phase
        on insufficient history (never flips on noise) and caches the verdict for
        the tick. Returns the ``variance_regime.run`` dict."""
        candles = await self._candles()
        info = await variance_regime.run(
            self.trading_pair, candles,
            short_window=self._regime_short_window,
            long_window=self._regime_long_window,
            trend_on=self._regime_trend_on_vr,
            range_on=self._regime_range_on_vr,
            trend_drift_pct=self._regime_trend_drift_pct,
            current_phase=self._regime_phase,
        )
        # Only advance the hysteresis state on a real verdict; insufficient history
        # holds the prior phase (and, on a fresh start with no candles yet, GRID =
        # stand down, which is the safe default for a trend follower).
        if not info.get("insufficient_history"):
            self._regime_phase = str(info.get("phase") or self._regime_phase)
            self._no_candle_streak = 0
        else:
            # No candle data (feed down / gateway-budget throttle). The gate stands
            # the book DOWN — safe (it cannot bleed), but a strategy that silently
            # never trades looks identical to "no trend". Log once per window so an
            # operator can tell the two apart and chase the candle feed.
            self._no_candle_streak += 1
            if self._no_candle_streak % 30 == 1:
                logger.warning(
                    "rgrid chop gate standing down for %s ticks — NO candle data "
                    "(insufficient_history), not 'no trend'; check the candle feed "
                    "(user=%s pair=%s)",
                    self._no_candle_streak, self.user_id, self.trading_pair,
                )
        # Confirmation debounce: count consecutive same-direction SUSTAINED-DRIFT
        # ticks (VR burstiness is deliberately excluded — see _trend_gate_sides).
        d = str(info.get("direction") or variance_regime.FLAT)
        if (not info.get("insufficient_history") and bool(info.get("trend_by_drift"))
                and d in (variance_regime.UP, variance_regime.DOWN)):
            if d == self._trend_streak_dir:
                self._trend_streak += 1
            else:
                self._trend_streak_dir = d
                self._trend_streak = 1
        else:
            self._trend_streak = 0
            self._trend_streak_dir = variance_regime.FLAT
        self._confirmed_trend_dir = (
            self._trend_streak_dir if self._trend_streak >= self._trend_confirm_ticks
            else variance_regime.FLAT
        )
        return info

    def _trend_gate_sides(self, allow_buy: bool, allow_sell: bool) -> Tuple[bool, bool]:
        """Apply the chop stand-down gate to the ENTRY/ADD side permissions.

        Reduces are triggers (the trail and the exposure band), never routed through
        ``allow_buy``/``allow_sell``, so gating these only ever suppresses NEW or
        ADD exposure — it can never block an exit. In chop (no confirmed trend) both
        entry sides are refused (stand down). In a trend only the trend-aligned side
        may open/add (a long in an uptrend, a short in a downtrend), which is also
        how a reversal flips the book: the old side stops adding and exits, then the
        new side is the only one the gate admits.

        CONFIRMATION IS BY SUSTAINED DIRECTIONAL DRIFT, NOT VARIANCE RATIO. The
        variance-regime classifier calls a high VR a "trend", but a high VR fires on
        BURSTY chop — exactly the high-vol chop R-Grid bled in. A trend follower only
        wants a sustained one-way move, so the gate admits ONLY on ``trend_by_drift``
        held for ``rgrid_trend_confirm_ticks`` consecutive same-direction ticks
        (``_confirmed_trend_dir``, computed in :meth:`_classify_regime`). The
        classifier ``phase`` and ``holding_trend`` can both be set by VR alone, so
        they are deliberately NOT consulted here.
        """
        # Confirmation is by SUSTAINED directional drift held for
        # ``rgrid_trend_confirm_ticks`` consecutive ticks (computed in
        # _classify_regime). ``holding_trend`` and the classifier phase can be set by
        # a high variance ratio (bursty chop), so they are deliberately NOT used — a
        # trend follower must not read burstiness as a trend.
        direction = self._confirmed_trend_dir
        if direction == variance_regime.UP:
            return allow_buy, False      # confirmed uptrend: longs only
        if direction == variance_regime.DOWN:
            return False, allow_sell     # confirmed downtrend: shorts only
        return False, False              # no confirmed trend -> stand down

    def _add_gate_admits(self, side: TradeType) -> bool:
        """Trend gate for cross-mode opens/adds. Exits are NEVER gated (they do not
        route through here). When the gate is off, always admit.

        The crossing add is itself a momentum filter — it only fires once mid has
        extended one full add-spacing (s_add ≈ arm+cushion, ~33bp) in one direction,
        so chop below that amplitude never triggers a false add. When the overlay
        supplies a regime read, additionally refuse an add the regime opposes
        (a long while the overlay reads trend_down, or a short while trend_up), which
        is the same signal ``_overlay_opposes`` already uses to tighten protection.
        """
        if not self.trend_gate:
            return True
        long_side = side is TradeType.BUY
        # Only veto when the overlay is confident AND reads against the add side.
        if self._overlay_opposes(long_side):
            return False
        return True

    async def _fire_cross_add(self, side: TradeType, mid: Decimal, step_base: Decimal) -> bool:
        """Cross the spread to ADD one step on ``side`` — the cross-mode entry.

        Mirror of :meth:`_fire_trail_stop` for the OPEN side: a bounded marketable
        LIMIT (``_add_cross_bp`` through the touch), NOT reduce-only. Cancels any
        resting maker leg first so cross and maker never both rest on the add side.
        """
        amount = self._quantize_quote(step_base, mid)
        if amount is None or amount <= 0:
            return False
        await self._cancel_leg(side)
        slip = self._add_cross_bp / Decimal(10000)
        px = (mid * (Decimal(1) + slip) if side is TradeType.BUY
              else mid * (Decimal(1) - slip))
        cfg = build_cross_entry(
            self.trading_pair, side, amount,
            leverage=int(self.cfg("leverage", 1) or 1), price=px,
        )
        ex = RGridMakerExecutor(
            cfg, user_id=self.user_id, controller_id=self.id,
            adapter=self.adapter, inventory=self.inventory, leg=LEG_ENTRY_CROSS,
        )
        if not await self.spawn_executor(
            ex, ExecutorRequest(
                order_amount_quote=amount * mid, reduce_only=False,
                position_action=cfg.position_action,
            )
        ):
            return False
        self._add_id = ex.id
        self._add_age = 0
        logger.info(
            "rgrid crossing add: %s one step (%s) at %s (mid %s, s_add %s) "
            "(user=%s pair=%s)",
            "BUY" if side is TradeType.BUY else "SELL", amount, px, mid,
            self._add_spacing(), self.user_id, self.trading_pair,
        )
        return True

    async def _drive_cross_add(
        self, net: Decimal, add_ref: Decimal, mid: Decimal, step_base: Decimal,
        allow_buy: bool, allow_sell: bool,
    ) -> None:
        """Cross-mode add driver. Fires ONE bounded marketable add per direction once
        mid has extended one add-spacing past the reference (confirmed momentum),
        subject to the trend gate, the exposure cap, and one-add-in-flight.

        A refused/gated add stands down for the tick — it NEVER falls through into a
        resting maker quote (the same pile-on rule the refused exit already follows).
        Both resting maker legs are dropped in cross mode so the two paths never mix.
        """
        # Re-price a stranded add; stand down while one is still working.
        if await self._reap_stale_add():
            return
        # No resting maker orders in cross mode.
        await self._cancel_leg(TradeType.BUY)
        await self._cancel_leg(TradeType.SELL)
        if self._add_in_flight() or step_base <= 0:
            return
        s_add = self._add_spacing()
        buy_trigger = add_ref * (Decimal(1) + s_add)
        sell_trigger = add_ref * (Decimal(1) - s_add)

        def _ready(side: TradeType) -> bool:
            allowed = allow_buy if side is TradeType.BUY else allow_sell
            if not allowed or not self._add_gate_admits(side):
                return False
            reached = mid >= buy_trigger if side is TradeType.BUY else mid <= sell_trigger
            if not reached:
                return False
            return self._projected_order_within_exposure(side, mid, step_base * mid)

        # Long adds buy; short adds sell; flat fires whichever trigger price reached
        # (only one can be — buy trigger is above the ref, sell below).
        if net > 0:
            if _ready(TradeType.BUY):
                await self._fire_cross_add(TradeType.BUY, mid, step_base)
        elif net < 0:
            if _ready(TradeType.SELL):
                await self._fire_cross_add(TradeType.SELL, mid, step_base)
        else:
            if _ready(TradeType.BUY):
                await self._fire_cross_add(TradeType.BUY, mid, step_base)
            elif _ready(TradeType.SELL):
                await self._fire_cross_add(TradeType.SELL, mid, step_base)

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

    async def flatten_now(self, mid: Decimal, *, reason: str = "handoff") -> bool:
        """Cancel maker quotes and cross the whole position. True iff flat.

        D-Grid calls this on a phase handoff. Stopping R-Grid's maker executors
        only cancels resting quotes — inventory would otherwise survive into
        the ranging ladder.
        """
        eps = Decimal("1e-12")
        if abs(self._net_base()) <= eps:
            await self._cancel_leg(TradeType.BUY)
            await self._cancel_leg(TradeType.SELL)
            if mid > 0:
                self._reset_exposure_window(mid)
            return True
        if mid <= 0:
            return False
        if self._stop_in_flight():
            standing = await self._reap_stale_stop()
            if standing and self._stop_id is not None:
                await self.orchestrator.tick(self._stop_id)
            self._absorb_fills()
        if abs(self._net_base()) <= eps:
            self._reset_exposure_window(mid)
            return True
        if not self._stop_in_flight():
            await self._fire_trail_stop(self._net_base(), mid, reason=reason)
            if self._stop_id is not None:
                await self.orchestrator.tick(self._stop_id)
            self._absorb_fills()
        if abs(self._net_base()) <= eps:
            self._reset_exposure_window(mid)
            return True
        return False

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

        # PHASE-1 chop stand-down: unless a directional trend is CONFIRMED, quote no
        # new entries or adds (exits below are unaffected — they are triggers, not
        # gated by these flags). This is the fix for the dominant August bleed: a
        # trend follower must not trade chop.
        if self.chop_stand_down:
            await self._classify_regime()
            allow_buy, allow_sell = self._trend_gate_sides(allow_buy, allow_sell)

        # Flat and holding stale entries: the closed position's prices still
        # anchor us, and their average sits far from the new mid. Re-anchor so a
        # genuine fresh move is required.
        if net == 0 and self._has_fills():
            self._reset_exposure_window(mid)
        elif net == 0:
            self._track_flat_anchor(mid)

        if await self._reap_stale_stop():
            return          # the crossing stop is working; do not re-quote over it

        anchor = self.exposure_anchor(mid)
        if anchor is None or anchor <= 0:
            await self._cancel_leg(TradeType.BUY)
            await self._cancel_leg(TradeType.SELL)
            return
        self._last_anchor = anchor
        band = self._band()

        # ENTRY reference and EXIT reference are deliberately separate quantities.
        # They used to be the same number (the exposure anchor) at the same width
        # (one band), which is what made the strategy unable to hold a trend: the
        # stop sat exactly as far from the position as the move that opened it, so
        # the very noise that triggers an entry also triggers the exit. See
        # _exit_band() and _add_reference().
        add_ref = self._add_reference(net, anchor)

        # The two legs. Mirror of Grid: the BUY sits ABOVE the reference and the
        # SELL BELOW it, so each becomes postable only once price has travelled
        # past it — that is the momentum. At most one is postable at a time.
        buy_price = add_ref * (Decimal(1) + band)
        sell_price = add_ref * (Decimal(1) - band)
        # Telemetry has to report the levels the engine is ACTUALLY working, not
        # the ones it used to. Both moved: the add is off the last fill while a
        # position is open, and the exit is a wider band. grid_metrics() derived
        # both from anchor x (1 -+ band), so the /status card would have quoted the
        # user two levels nothing was trading on.
        self._last_add_ref = add_ref
        self._last_exit_band = self._exit_band()

        # Soft reset. Once armed, the exit follows the trend.
        #
        # A trailing stop wants to act BELOW the peak (for a long), which is exactly
        # where a resting post-only ask cannot sit. So it CROSSES — the single
        # exemption from maker-only, taken deliberately: the alternative was leaving
        # a position with no exit in the one situation the mechanism exists for.
        # Everything else R-Grid does still rests post-only.
        self._track_trail(mid, net)
        if self._trail_breached(mid, net) and not self._stop_in_flight():
            # A REFUSED exit must not fall through into the add branch below.
            # _fire_trail_stop returns False when the risk engine or the kill
            # switch declines it (reduce-only is exempt from the SIZE caps, but not
            # from max_open_executors or the kill switch). Falling through rested a
            # fresh ADD on the losing side, on the same tick the strategy had just
            # failed to close it and logged "the session rail is the only stop
            # left" — piling on risk at the worst possible moment. Stand down for
            # this tick instead; both legs are already cancelled by _fire_trail_stop.
            await self._fire_trail_stop(net, mid, reason="trailing stop")
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
            exit_band = self._exit_band()
            exit_trigger = (anchor * (Decimal(1) - exit_band) if net > 0
                            else anchor * (Decimal(1) + exit_band))
            reached = mid <= exit_trigger if net > 0 else mid >= exit_trigger
            if reached:
                # Same standing-down rule as the trail above: a refused close is
                # never a licence to add.
                await self._fire_trail_stop(net, mid, reason="exposure band")
                return

        # Sizing: only the ADDING leg rests, one step at a time. The reducing side
        # is the trigger above, so there is no resting exit to size.
        step_base = (self.order_amount_quote / mid) if mid > 0 else Decimal(0)

        # CROSS MODE: the add is a bounded marketable LIMIT fired on confirmed
        # momentum (mid extended one add-spacing past the reference), trend-gated —
        # not a resting post-only bid, which cannot fill a rising market (0 fills,
        # measured). A refused/gated add stands down and never rests a maker quote.
        if self.add_mode == "cross":
            await self._drive_cross_add(net, add_ref, mid, step_base, allow_buy, allow_sell)
            return

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
        gated differently: see :meth:`_track_trail`, which measures its excursion
        from OBSERVED price on a rebuild rather than from the seeded window.
        """
        leg = BUY if net > 0 else SELL
        return leg in self._live_fill_legs

    def _track_trail(self, mid: Decimal, net: Decimal) -> None:
        """Update the favourable extreme and the armed flag. No orders here — the
        exit leg's PRICE is the mechanism, so arming only changes where it rests."""
        if net == 0:
            self._trail_peak = None
            self._trail_origin = None
            self._trail_armed = False
            return
        if not self.trail_enabled or self.reset_threshold_pct <= 0 or mid <= 0:
            return
        long_side = net > 0
        if self._trail_peak is None:
            self._trail_peak = mid
            self._trail_origin = mid
        elif long_side:
            self._trail_peak = max(self._trail_peak, mid)
        else:
            self._trail_peak = min(self._trail_peak, mid)
        if self._trail_armed:
            return
        # THE ARM BASIS MUST BE TRUSTWORTHY, and on a rebuild the window is not.
        #
        # ``_position_entry_price()`` is the leg window's VWAP, and ``seed_fills``
        # restores the whole SESSION — both sides, exits included. A previous SHORT
        # cycle's cover is a BUY, so it lands in the BUY deque and drags a later
        # long's "entry" down. Reproduced: true entry 96.5, seeded basis 94.25, so
        # the very first tick read a 2.4% excursion, armed instantly, and a 31bp dip
        # crossed out of a healthy position at a loss. That is the exact "~10bp
        # adverse + 8.6bp taker every cycle" pathology _position_entry_price was
        # written to remove, re-entered through the seed — and it recurs on EVERY
        # worker handoff while a position is open.
        #
        # This docstring used to claim the trail "arms off observed price extremes,
        # not the window". Make that true when the window cannot be trusted: with no
        # live fill on this leg, measure the excursion from the first mid this
        # process observed. Conservative by construction — a rebuild deep in profit
        # starts at zero excursion and must earn the arm again — and it keeps the
        # trail working instead of leaving a rebuilt position to the rail alone.
        if self._band_exit_trustworthy(net):
            entry = self._position_entry_price() or self.exposure_anchor(mid)
        else:
            entry = self._trail_origin
        if entry is None or entry <= 0:
            return
        excursion = ((mid - entry) / entry) if long_side else ((entry - mid) / entry)
        # The arm is WIDENED to clear the band and the round-trip cost, never
        # disabled: the overlay scales the spread live while the threshold is not
        # scaled, and the shipped defaults sit on the boundary, so refusing would
        # have silently removed the mechanism.
        arm = self._arm_pct()
        # An overlay read AGAINST the position arms early rather than pausing
        # R-Grid — but never underwater, or the trail becomes a stop that
        # front-runs the SL rail.
        opposed = self._overlay_opposes(long_side)
        if excursion < arm and not (opposed and excursion > 0):
            return
        self._trail_armed = True
        self._latched_giveback = self._trail_giveback()
        logger.info(
            "rgrid soft reset armed%s: %s%% favourable from %s — the exit leg now "
            "follows the trend (user=%s pair=%s)",
            " EARLY (overlay reads the market against this position)" if opposed else "",
            round(float(excursion) * 100, 3), entry, self.user_id, self.trading_pair,
        )

    def _trail_giveback(self) -> Decimal:
        """How far back from the favourable extreme the trailing exit fires.

        A third quantity that used to BE the entry band. The trail is the only exit
        that can book a PROFIT (it ratchets with the extreme, whereas the band exit
        is loss-only), so the room it gives the position is the single number that
        decides whether a trend is ridden or handed back. At one entry band it is
        tighter than an ordinary pullback and the run is cut almost immediately.

        Set to the ARM threshold, which is what makes it self-consistent: the moment
        the trail arms at +arm favourable, its stop sits at ``peak x (1 - arm)`` —
        the entry, i.e. breakeven — and ratchets into profit from there. A run can
        no longer be given back below the point at which it was recognised.
        """
        # Track the ARM, not a second independent derivation. Byte-for-byte
        # identical today (rgrid_sizing.trail_giveback_frac just returns arm_pct),
        # but load-bearing now that the arm can be clamped: otherwise the giveback
        # would keep the UNCLAMPED value (e.g. 30bp) while the arm is clamped (15bp),
        # putting the armed stop 15bp BELOW entry and inverting the "give-back == arm
        # puts the stop at breakeven" property documented just above.
        floor = self._arm_pct()
        mult = _dec(self.cfg("trail_giveback_mult", "0") or "0")
        return max(floor, self._band() * mult)

    def _trail_price(self, net: Decimal) -> Decimal:
        """Where the armed exit fires: one give-back behind the best price seen.
        It only ever ratchets forward."""
        peak = self._trail_peak or Decimal(0)
        # RGRID-TRAIL-LOOSENS: once armed, the give-back is the value at arm —
        # an ATR/overlay rise must not push the stop further from the peak.
        band = self._latched_giveback if self._latched_giveback is not None else self._trail_giveback()
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
        # The ADD triggers come off the reference the add leg is actually quoted
        # around (the last fill while in a position), not off the exposure anchor.
        add_ref = self._last_add_ref or anchor
        up_price = down_price = 0.0
        if add_ref and add_ref > 0:
            up_price = float(add_ref * (Decimal(1) + band))
            down_price = float(add_ref * (Decimal(1) - band))
        # The exposure-band EXIT is a different, wider distance off the anchor.
        exit_up = exit_down = 0.0
        if anchor and anchor > 0:
            _eb = self._last_exit_band or self._exit_band()
            exit_up = float(anchor * (Decimal(1) + _eb))
            exit_down = float(anchor * (Decimal(1) - _eb))
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
            # Where the position is actually given up, which is NOT the add
            # trigger mirrored — the exit band is the wider derived distance.
            "rgrid_exit_trigger": (
                exit_down if net_base > 0 else exit_up if net_base < 0 else 0.0
            ),
            "rgrid_exit_band_bp": float(
                (self._last_exit_band or self._exit_band()) * Decimal(10000)
            ),
            "rgrid_trail_armed": bool(self._trail_armed),
            "rgrid_trail_peak": float(self._trail_peak) if self._trail_peak else 0.0,
            "rgrid_signal_regime": self.signal_regime,
            "rgrid_signal_confidence": self.signal_confidence,
        }
