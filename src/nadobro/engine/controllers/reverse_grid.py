"""Reverse Grid — a venue-trigger momentum grid (the mirror of the classic Grid).

Grid (mean-reversion) rests LIMIT_MAKER buys BELOW mid and sells ABOVE mid. A
Reverse Grid wants the OPPOSITE — buys ABOVE mid, sells BELOW mid — so a trend
pays: it takes the side a breakout is going, pyramids into it, and rides the move
with a trailing stop. Those rungs CANNOT rest as maker limits (they cross the
book), which is exactly why the maker-only ``RGridController`` placed zero orders
in a quiet market. This controller uses the venue's native PRICE-TRIGGER orders
instead: the venue watches the mid and fires each rung when its level is crossed,
so the ladder is placed ONCE and reconciled — never re-quoted per cycle (the
gateway-budget amplifier the maker design suffered).

Execution model — the controller manages its trigger ladder DIRECTLY through the
adapter's trigger surface (``place_trigger_order`` / ``cancel_trigger_order`` /
``place_stop_order`` / ``list_trigger_orders``), NOT through the OrderExecutor: an
executor cancels via the resting-order path, which is the wrong venue service for
a trigger and would leak it. Fills are detected by POLLING the net position
(``adapter.held_base``) and attributing the change to the ladder's own rung
geometry — this needs no assumption about whether a fired trigger's fill echoes
the trigger's digest.

Venue facts this controller is built around (Nado trigger service, 2026-09):

* at most **25 PENDING trigger orders per product per subaccount** — a flat ladder
  is ``2 x levels`` triggers, so ``levels`` is capped at ``REVGRID_MAX_LEVELS`` (12);
  asking for 20 levels used to request 40 rungs and the venue rejected the 26th on
  (prod session 292: exactly 25 placed, the rest silently failed every re-arm);
* a fired trigger becomes an ORDER with the execution type encoded at placement.
  Entry rungs are placed **IOC**: a momentum entry that lags a fast move by more
  than its price bound must never REST as an untracked maker limit (which the
  controller could neither see nor cancel and which fills later as a phantom
  entry). An unfilled IOC rung is simply gone — so the ladder is RECONCILED
  against ``list_trigger_orders`` (a rung the venue no longer holds is re-armed);
* the venue may also drop pending triggers on its own (expiry, a linked-signer
  change, an account-health event) — the same reconciliation covers those.

Lifecycle, per tick:

* **Flat** — anchor at the mid, then arm a symmetric ladder: BUY triggers at
  ``anchor*(1+k*step)`` and SELL triggers at ``anchor*(1-k*step)`` for k=1..levels.
  Whichever side price breaks first sets the direction. While flat, if the mid
  drifts more than ``reanchor_bands`` steps from the anchor, the ladder is
  cancelled and re-anchored so a fresh break is always a real move from the
  current price (the flat "leash").
* **First fill** — the position turns non-flat. Cancel the OPPOSITE side's
  still-resting rungs (that side lost); attribute the filled base to the fired
  rungs to get the average entry; arm a venue REDUCE-ONLY stop at the protective
  distance. The same-side rungs above (buys) / below (sells) stay armed, so the
  pyramid extends naturally as the trend runs.
* **In a position** — track the favourable extreme. The single protective/
  trailing stop is a venue reduce-only stop: it starts at ``avg_entry`` distance
  and, once the position is in profit by ``trail_arm_pct``, RATCHETS to trail the
  extreme by ``trail_giveback_pct`` (never loosening). One venue-enforced order is
  therefore both the stop-loss and the take-profit — it survives a disconnect, and
  it needs only the existing reduce-only-stop primitive (no separate TP order).
* **Closed** — the stop fired, or the session rail flattened the book: the net
  poll reads flat. Cancel any residual triggers, reset, re-anchor to the current
  mid, and re-arm the ladder. A reversal is just this close-then-re-arm: the new
  break picks the new side.

Safety, in layers (honest risk — a reverse grid LOSES in chop and WINS in trends):

* the ladder is FINITE (``levels`` rungs per side), so the pyramid is bounded by
  ``levels * step`` — a hard exposure ceiling by construction;
* the per-position stop is VENUE-ENFORCED and reduce-only (can only shrink);
* the session %-of-margin SL/TP rail (``strategy/bot_runtime``) is the backstop
  behind everything here and is never second-guessed;
* every venue write is wrapped so a failure degrades rather than raising into the
  tick, and a step is floored above the taker round trip so a rung can never be a
  structural loss.

Geometry is fully deterministic — the engine + the venue book decide everything.
The LLM overlay and the HL feeds never gate an entry or an exit here.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import List, Optional

from src.nadobro.engine.adapter.base import AdapterError
from src.nadobro.engine.controllers.controller_base import Controller
from src.nadobro.engine.routines import variance_regime
from src.nadobro.engine.types import OrderType, TradeType, _as_bool, _dec
from src.nadobro.quant.rgrid_sizing import (
    REVGRID_ENTRY_SLIP,
    REVGRID_MAX_LEVELS,
    REVGRID_STOP_SLIP,
    REVGRID_VENUE_MAX_PENDING_TRIGGERS,
    TAKER_ROUND_TRIP_RATE,
    arm_pct,
)


def _norm_digest(digest: object) -> str:
    """Canonical digest for comparisons (lower-case, 0x-prefixed) — the placement
    response and the venue's pending list must compare equal for the same order."""
    text = str(digest or "").strip().lower()
    if text and not text.startswith("0x"):
        text = "0x" + text
    return text

logger = logging.getLogger(__name__)


@dataclass
class _Rung:
    """One armed entry trigger on the ladder."""
    digest: str
    side: TradeType
    level: Decimal
    size_base: Decimal
    fired: bool = False
    # Base attributed to this rung when it fired. A fired IOC is CONSUMED whether it
    # filled in full or in part, so ``filled_base`` may be below ``size_base``.
    filled_base: Decimal = Decimal(0)


def _sign(value: Decimal) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


class ReverseGridController(Controller):
    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("name", "revgrid")
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # Config-derived geometry + gate params (re-readable live via reload_config).
        self._load_config()

        # -- regime hysteresis state (NOT config — survives a live reconfig) --
        self._regime_phase: str = variance_regime.GRID
        self._trend_streak = 0
        self._trend_streak_dir = variance_regime.FLAT
        self._confirmed_trend_dir = variance_regime.FLAT

        # -- live state --
        self._anchor: Optional[Decimal] = None
        self._rungs: List[_Rung] = []
        # True when the entry ladder was truncated by the per-cycle opening cap and
        # still has rungs left to place. _maintain_flat keeps re-arming (placing only
        # the missing rungs) until the ladder is complete, so a capped arm finishes
        # over the next few ticks instead of resting a permanently-partial ladder.
        self._ladder_incomplete: bool = False
        self._pos_base: Decimal = Decimal(0)      # last observed signed net (run-only)
        # The run's venue BASELINE: the position on the product when the run began,
        # subtracted from held_base so the controller reads ONLY its own exposure.
        # None = not yet captured. It is captured on the first successful read and
        # is only ever ZERO: a position this run did not open blocks arming (see
        # _read_net — Nado's reduce-only exits act on the whole account position,
        # so a run cannot safely manage its exposure on top of one). A REBUILD of
        # the controller mid-session (worker handoff / FAILED-controller recovery)
        # restores the persisted baseline via ``venue_baseline`` in the config so
        # the run's own open position is still read as the run's, never as foreign.
        self._baseline_net: Optional[Decimal] = None
        _seed = self.cfg("venue_baseline")
        if _seed is not None:
            try:
                self._baseline_net = _dec(_seed)
            except Exception:  # noqa: BLE001 - an unusable seed is no seed
                self._baseline_net = None
        self._avg_entry: Optional[Decimal] = None
        self._peak: Optional[Decimal] = None      # favourable extreme mid since open
        # PRESENCE-FIRST: the ladder's FIRST arming (this run) is never gated — the
        # reverse grid enters the market immediately so it is on the book from the
        # first flat tick. The chop stand-down only governs RE-arming AFTER a close
        # (once this has flipped True), so a whipsaw stop-out doesn't instantly
        # re-enter the same chop. Reset per run in on_start; set in _open_position.
        self._has_opened = False
        self._trail_armed = False
        self._stop_digest: Optional[str] = None
        self._stop_level: Optional[Decimal] = None
        self._stop_size: Optional[Decimal] = None
        # Superseded stops whose cancel failed: retried on later ticks and at close
        # so a replaced stop is never silently orphaned (reduce-only, so harmless
        # to the position, but it holds a slot under the venue's 25-pending cap).
        self._stale_stops: List[str] = []
        # Cost-basis accumulators for the open position (see _recompute_avg_entry).
        self._attr_num = Decimal(0)
        self._attr_base = Decimal(0)
        self._last_mid: Optional[Decimal] = None
        # Order telemetry for /status. The base order_counts() sums executors, which
        # this controller has none of — it manages venue triggers directly — so it
        # tracks its own placed / filled / cancelled counts and overrides that method.
        self._n_placed = 0
        self._n_filled = 0
        self._n_cancelled = 0
        # Quote-gate telemetry ONLY. This controller never BRANCHES on the gate
        # (its chop stand-down is driven by chop_stand_down / _trend_confirmed,
        # not gate_verdict) — these fields exist so a chop stand-down surfaces on
        # the /status card as "Quoting: PAUSED (choppy — waiting for a trend)"
        # instead of a silent "LIVE, 0 orders" (the "R-Grid places no orders"
        # reports). engine_diag reads gate_verdict/gate_reason each cycle.
        self.gate_verdict: str = "QUOTE"
        self.gate_reason: str = ""
        # Tick counter (reconciliation cadence) and how many consecutive ticks the
        # venue position has been unreadable (rate-limits the hold log).
        self._tick_n = 0
        self._unreadable_ticks = 0
        self._foreign_ticks = 0
        self._last_reconcile_tick = -1

    # -- config ---------------------------------------------------------------
    def _load_config(self) -> None:
        """(Re)read the config-derived geometry + gate params from ``self.configs``.
        Called at construction and by :meth:`reload_config` on a live settings edit.
        Sets ONLY config attrs — never the runtime position/regime state."""
        self.trading_pair = str(self.cfg("trading_pair") or "")
        # Rungs PER SIDE. Capped at REVGRID_MAX_LEVELS: the venue holds at most 25
        # pending triggers per product per subaccount and a flat ladder is 2 x
        # levels (the mapper caps too — this guards a directly-built controller).
        _levels_raw = max(1, int(self.cfg("levels", 4) or 4))
        self.levels = min(_levels_raw, REVGRID_MAX_LEVELS)
        if _levels_raw > self.levels:
            logger.info(
                "revgrid %s: levels %s capped to %s per side (venue limit: %s pending "
                "triggers per product; a flat ladder is 2 x levels)",
                self.trading_pair, _levels_raw, self.levels, REVGRID_VENUE_MAX_PENDING_TRIGGERS,
            )
        # A rung must clear the taker round trip or it is a structural loss; floor
        # the step there regardless of the configured value (conservative — the
        # real maker/marketable cost is lower, so this only ever leaves headroom).
        raw_step = _dec(self.cfg("step_pct", "0.001") or "0.001")
        self.step_pct = max(raw_step, TAKER_ROUND_TRIP_RATE)
        # Per-rung notional (already stop-budget-capped by resolve_step_quote in the
        # mapper). The base size of each rung is derived from this and the rung's
        # own price so a full pyramid stays inside the intended deployment.
        self.order_amount_quote = _dec(self.cfg("order_amount_quote", "0") or "0")
        self.leverage = int(self.cfg("leverage", 1) or 1)
        # Protective stop distance from the average entry (a tight momentum stop —
        # small stops, let winners run). Floored above the round trip so ordinary
        # noise + fees cannot trip it.
        self.stop_pct = max(_dec(self.cfg("stop_pct", self.step_pct * 2) or (self.step_pct * 2)),
                            TAKER_ROUND_TRIP_RATE)
        # Trailing profit lock: arm once the position is this far in profit, then
        # trail the favourable extreme by the giveback. giveback == arm ⇒ the trail
        # sits at ~breakeven the instant it arms and ratchets into profit from there.
        self.trail_arm_pct = _dec(self.cfg("trail_arm_pct", arm_pct(self.step_pct)) or arm_pct(self.step_pct))
        self.trail_giveback_pct = _dec(self.cfg("trail_giveback_pct", self.trail_arm_pct) or self.trail_arm_pct)
        # While flat, re-anchor the ladder once the mid drifts this many steps from
        # the anchor, so a fresh break is always a real move from the CURRENT price.
        self.reanchor_bands = max(Decimal(1), _dec(self.cfg("reanchor_bands", 2) or 2))
        # How far THROUGH its level a fired entry may fill (its IOC price bound). A
        # LIMIT fills at the best available prices — the bound only caps the worst
        # print — so it is set wide enough to cross a normal book plus a modest gap
        # (15bp; the grid executor bounds its crossing exits at 30bp) without
        # letting a gapped book fill a rung arbitrarily far from its level. Below
        # the bound the fire lags a fast move, the IOC cancels, and the rung is
        # re-armed by the reconciliation. The stop close is priced further through
        # so it always fills.
        # Defaults are the SHARED constants the stop-budget sizing covers
        # (quant/rgrid_sizing): the bound the budget assumes must be the bound the
        # orders carry.
        _entry_slip = float(REVGRID_ENTRY_SLIP * Decimal(100))
        _stop_slip = float(REVGRID_STOP_SLIP * Decimal(100))
        self.entry_slippage_pct = float(self.cfg("entry_slippage_pct", _entry_slip) or _entry_slip)
        self.stop_slippage_pct = float(self.cfg("stop_slippage_pct", _stop_slip) or _stop_slip)
        # Reconcile the ladder against the venue's pending-trigger list this often
        # (ticks) — and immediately whenever a rung SHOULD have fired (the mid is
        # past its level) but the position did not move.
        self.reconcile_every_ticks = max(1, int(self.cfg("revgrid_reconcile_every_ticks", 6) or 6))
        # Bound stop re-arm churn: only re-place the trailing stop when its level
        # moves at least this fraction of a step (or the position size changes).
        self.stop_min_reprice_frac = max(Decimal(0), _dec(self.cfg("stop_min_reprice_frac", "0.25") or "0.25"))

        # -- chop stand-down gate --
        # A reverse grid LOSES in chop and WINS in trends; the whole grid family bled
        # all of Aug-2026 by trading chop. So by DEFAULT the ladder is only armed when
        # a directional trend is CONFIRMED — otherwise the controller stands down (arms
        # nothing, cancels any resting ladder). Exits are NEVER gated: a stop already
        # armed keeps managing an open position regardless of the regime. Confirmation
        # is by SUSTAINED directional drift (``variance_regime.trend_by_drift`` held for
        # ``trend_confirm_ticks`` consecutive same-direction ticks), NOT a bare variance
        # ratio — a high VR fires on bursty chop, which is exactly what a trend follower
        # must not read as a trend. Set ``revgrid_chop_stand_down=0`` to disable.
        self.chop_stand_down = _as_bool(self.cfg("revgrid_chop_stand_down", True), True)
        self._regime_short_window = int(max(2, int(self.cfg("revgrid_regime_short_window", 4) or 4)))
        self._regime_long_window = int(max(self._regime_short_window + 1,
                                           int(self.cfg("revgrid_regime_long_window", 12) or 12)))
        self._regime_trend_on_vr = float(self.cfg("revgrid_regime_trend_on_vr", 1.25) or 1.25)
        self._regime_range_on_vr = float(self.cfg("revgrid_regime_range_on_vr", 1.15) or 1.15)
        self._regime_trend_drift_pct = float(self.cfg("revgrid_regime_trend_drift_pct", 0.30) or 0.30)
        self._trend_confirm_ticks = int(max(1, int(self.cfg("revgrid_trend_confirm_ticks", 3) or 3)))

    def reload_config(self) -> None:
        """Apply a live settings edit: re-read the geometry + gate params from the
        (already-refreshed) ``self.configs``. RUNTIME state — the anchor, the open
        position's avg entry / peak / trailing-stop bookkeeping, and the regime
        hysteresis — is deliberately untouched, so a mid-run edit never disturbs an
        open position or re-arms an exit the trail had already locked in."""
        self._load_config()

    # -- lifecycle -----------------------------------------------------------
    async def on_start(self) -> None:
        # No auto-resume: a redeploy/start never re-arms a prior ladder OR re-engages
        # a position it did not open. Start from a clean slate; the first on_tick
        # captures whatever is held now as the BASELINE (see _read_net) so a
        # pre-existing / leftover position is left alone (the session rail still
        # bounds it), and the ladder is armed fresh from the live mid.
        self._anchor = None
        self._rungs = []
        self._ladder_incomplete = False
        self._pos_base = Decimal(0)
        # Keep a restored baseline (rebuild); a fresh run captures it on the first read.
        if self.cfg("venue_baseline") is None:
            self._baseline_net = None
        # A fresh run always does the presence-first initial entry (see _maintain_flat).
        self._has_opened = False
        self._reset_position_state()

    async def on_tick(self) -> None:
        self._tick_n += 1
        mid = await self._mid()
        if mid is None or mid <= 0:
            self._note_venue_hold("mid")
            return
        # Pending-trigger snapshot FIRST, position second: a rung that fires between
        # the two reads then shows as "still pending + net grew" (the normal fill
        # path), never as "gone + net unchanged" (a hole). See _reconcile.
        pending = await self._pending_snapshot(mid)
        net = await self._read_net()
        if net is None:
            if self.gate_reason == "venue_foreign_position":
                return                    # holding on a foreign position (surfaced by _read_net)
            # Unreadable venue — hold. Never act (open, close, reset) on a bad read.
            # Say so: a silent hold reads as "LIVE, 0 orders" on the card.
            self._note_venue_hold("position")
            return
        if self._unreadable_ticks:
            logger.info(
                "revgrid %s: venue readable again after %s held tick(s) (user=%s)",
                self.trading_pair, self._unreadable_ticks, self.user_id,
            )
            self._unreadable_ticks = 0
        self._last_mid = mid
        # Refresh the regime read every tick so the flat gate and the debounce stay
        # current (cheap; exits never consult it).
        await self._classify_regime()
        # Ladder vs venue: drop rungs/stops the venue no longer holds so the
        # bookkeeping below never trusts a trigger that is gone (see _reconcile).
        if pending is not None:
            await self._reconcile(pending, mid, net)

        if net == 0:
            if self._pos_base != 0:
                await self._reset_after_close()
            self._pos_base = Decimal(0)
            await self._maintain_flat(mid)
            return

        # In a position — actively managing (never "paused" for the card).
        self.gate_verdict, self.gate_reason = "QUOTE", ""
        if self._pos_base == 0 or _sign(net) != _sign(self._pos_base):
            if self._pos_base != 0 and _sign(net) != _sign(self._pos_base):
                # Sign flip in one tick (e.g. a rail overshoot): tidy the old side.
                await self._reset_after_close()
            await self._open_position(net, mid)
        elif abs(net) > abs(self._pos_base):
            # An add fired — the pyramid grew. Re-attribute to the newly-fired rungs.
            self._recompute_avg_entry(net)
        # A shrink (partial reduce-only fill) keeps the prior avg entry — the closed
        # part does not change the remaining position's cost basis; _maintain_position
        # re-sizes the stop to the smaller net on its own.
        self._pos_base = net
        await self._maintain_position(net, mid)

    async def on_stop(self, reason: str = "stopped") -> None:
        """Tear the ENTRY ladder down (a rung firing after the stop would open an
        unmanaged position) but LEAVE the protective reduce-only stop armed: a
        stop runs before the session's flatten, and if that flatten fails the
        open position keeps its venue stop instead of sitting naked (audit
        2026-09-16). Once the session-end path has flattened, its trigger sweep
        (``strategy/venue_triggers``) cancels the now-stale stop by digest."""
        await self._cancel_all_rungs()
        await self._retry_stale_stops()
        if self._stop_digest is not None:
            logger.info(
                "revgrid %s: stopped (%s) — protective stop %s left armed for the "
                "position; the session-end sweep clears it once flat (user=%s)",
                self.trading_pair, reason, self._stop_digest, self.user_id,
            )
        self._anchor = None

    async def flatten_now(self, mid: Decimal, *, reason: str = "handoff") -> bool:
        """Close the whole position, cancel every trigger, and report whether the
        book is now FLAT. D-Grid calls this on a phase handoff (RGRID→GRID): its
        ranging ladder must not inherit a naked position, so the trend delegate has
        to be flat before it is dropped.

        Crosses to close (reduce-only MARKET) when a position is open, then re-reads
        the venue net. Returns ``True`` only once the venue confirms flat — otherwise
        ``False`` so D-Grid retries next tick rather than dropping an open position.
        """
        net = await self._read_net()
        if net is None:
            return False              # unreadable venue — never claim flat
        # Cancel the ENTRY rungs FIRST — they are still armed (pyramiding) and, unlike
        # the reduce-only stop (which can only SHRINK the position), an entry rung
        # firing between the close and the re-read would RE-OPEN the position, so
        # D-Grid could never hand off flat and would churn taker round-trips. The
        # protective stop is deliberately LEFT until the book is confirmed flat, so
        # the position is never naked while the close is in flight.
        await self._cancel_all_rungs()
        if net != 0:
            close_side = TradeType.SELL if net > 0 else TradeType.BUY
            try:
                await self.adapter.place_order(
                    self.trading_pair, close_side, OrderType.MARKET, abs(net),
                    reduce_only=True,
                )
                self._n_filled += 1
            except AdapterError:
                logger.warning(
                    "revgrid flatten close FAILED (%s) net=%s (user=%s pair=%s)",
                    reason, net, self.user_id, self.trading_pair, exc_info=True,
                )
                return False
            net = await self._read_net()
            if net is None or net != 0:
                return False          # still open — D-Grid retries next tick
        # Flat: now drop the protective stop too, and reset. Nothing is left armed.
        if self._stop_digest is not None:
            try:
                await self.adapter.cancel_trigger_order(self._stop_digest)
            except AdapterError:
                logger.debug("revgrid flatten stop-cancel failed", exc_info=True)
        self._anchor = None
        self._pos_base = Decimal(0)
        self._reset_position_state()
        return True

    # -- venue reads ---------------------------------------------------------
    async def _mid(self) -> Optional[Decimal]:
        try:
            return _dec(await self.adapter.mid_price(self.trading_pair))
        except Exception:  # noqa: BLE001 - a bad mid read holds the tick
            logger.debug("revgrid mid read failed pair=%s", self.trading_pair, exc_info=True)
            return None

    async def _read_net(self) -> Optional[Decimal]:
        """Signed net position base for THIS RUN: the venue position (``held_base``)
        minus the baseline captured at the run's start. The baseline excludes any
        position that already existed on the product when the run began, so the
        controller reads, sizes stops against, and flattens ONLY its own exposure.
        None = the venue could not be read — the caller then holds and acts on
        nothing (fail safe), and the baseline is NOT captured off a bad read."""
        try:
            held = await self.adapter.held_base(self.trading_pair)
        except Exception:  # noqa: BLE001
            logger.debug("revgrid net read failed pair=%s", self.trading_pair, exc_info=True)
            return None
        if held is None:
            return None
        held = _dec(held)
        if self._baseline_net is None:
            # First good read of the run — the ladder is not armed yet, so whatever
            # is held now was NOT opened by this run. The run cannot trade on top of
            # it (reduce-only exits act on the whole account position), so it holds,
            # visibly, until the position is gone; then the baseline is ZERO.
            if held != 0:
                self._note_foreign_position(held)
                return None
            self._baseline_net = Decimal(0)
            if self._foreign_ticks:
                logger.info("revgrid %s: foreign position cleared after %s tick(s) — arming (user=%s)",
                            self.trading_pair, self._foreign_ticks, self.user_id)
                self._foreign_ticks = 0
        return held - self._baseline_net

    def _note_foreign_position(self, held: Decimal) -> None:
        """Hold (visibly) while the product carries a position this run did not
        open: card line "Quoting: PAUSED (an open position on this market was not
        opened by this run)"; rate-limited warning."""
        self._foreign_ticks += 1
        self.gate_verdict, self.gate_reason = "PAUSE", "venue_foreign_position"
        if self._foreign_ticks == 1 or self._foreign_ticks % 12 == 0:
            logger.warning(
                "revgrid %s: %s base already held on the venue was not opened by this run "
                "— holding (close it, or stop and use another market) (%s tick(s), user=%s)",
                self.trading_pair, held, self._foreign_ticks, self.user_id,
            )

    def _note_venue_hold(self, what: str) -> None:
        """The venue could not be read this tick; hold and make it VISIBLE. The
        gate telemetry turns the /status line into "Quoting: PAUSED (venue position
        read unavailable — holding)"; the log is rate-limited to the first hold and
        every 12th thereafter so a throttle storm does not flood it."""
        self._unreadable_ticks += 1
        self.gate_verdict, self.gate_reason = "PAUSE", "venue_unreadable"
        if self._unreadable_ticks == 1 or self._unreadable_ticks % 12 == 0:
            logger.warning(
                "revgrid %s: venue %s read unavailable — holding (no arm / no exit "
                "decisions) for %s consecutive tick(s); armed triggers and the venue "
                "stop keep working (user=%s)",
                self.trading_pair, what, self._unreadable_ticks, self.user_id,
            )

    # -- ladder vs venue reconciliation ---------------------------------------
    def _fire_expected(self, mid: Decimal) -> bool:
        """Is the mid past the level of a rung we still believe is armed? Then the
        venue should have fired it — if the position did not move, the rung was an
        IOC that cancelled unfilled (or the venue dropped it) and it is gone."""
        for r in self._rungs:
            if r.fired:
                continue
            if r.side is TradeType.BUY and mid >= r.level:
                return True
            if r.side is TradeType.SELL and mid <= r.level:
                return True
        return False

    async def _pending_snapshot(self, mid: Decimal) -> Optional[List[str]]:
        """The venue's pending trigger digests for this product when a reconcile
        is due — every ``reconcile_every_ticks`` ticks, and at once when a rung
        SHOULD have fired (the mid is past its level; ``_fire_expected``). ``None``
        when not due, when the adapter cannot list, or when the list is unreadable
        (unknown is never "gone"). Read BEFORE the position (see on_tick)."""
        since = self._tick_n - self._last_reconcile_tick
        due = since >= self.reconcile_every_ticks
        # A rung the mid is past should have fired: check sooner — but no more
        # than every other tick, so a venue that fires late (or a mid that differs
        # from ours) cannot force a 5-weight list read every tick.
        forced = self._pos_base == 0 and since >= 2 and self._fire_expected(mid)
        if not (due or forced):
            return None
        if not self._rungs and self._stop_digest is None:
            self._last_reconcile_tick = self._tick_n
            return None
        lister = getattr(self.adapter, "list_trigger_orders", None)
        if not callable(lister):
            return None
        try:
            pending = await lister(self.trading_pair)
        except Exception:  # noqa: BLE001 - unreadable list = unknown, never "gone"
            logger.debug("revgrid trigger list failed pair=%s", self.trading_pair, exc_info=True)
            return None
        if pending is None:
            return None
        self._last_reconcile_tick = self._tick_n
        return [str(d).lower() for d in pending]

    async def _reconcile(self, pending: List[str], mid: Decimal, net: Decimal) -> None:
        """Compare the ladder + stop we THINK are armed with what the venue still
        holds pending (``pending``, read before the position), and drop what is gone.

        * a missing ENTRY rung while FLAT: the ladder has a hole exactly where price
          went — tear the ladder down and re-anchor at the current mid (the flat
          path re-arms a complete ladder this tick);
        * a missing entry rung while IN A POSITION whose level the mid has NOT
          crossed: the venue dropped it — forget it (the pyramid simply has one
          fewer add). A missing rung the mid HAS crossed is a fill that is landing
          (the position read follows the list read) — leave it for the net read;
        * a missing STOP while in a position: the stop is gone (fired — the close
          will show up on the next net read — or dropped by the venue) — forget its
          digest so ``_ensure_stop`` re-arms protection at once (reduce-only, so a
          re-arm over a close-in-flight is harmless).
        """
        held = {_norm_digest(d) for d in pending}
        missing = [r for r in self._rungs if not r.fired and _norm_digest(r.digest) not in held]
        stop_missing = (
            self._stop_digest is not None and _norm_digest(self._stop_digest) not in held
        )
        if not missing and not stop_missing:
            return
        # DEFENCE IN DEPTH: a rung the venue reports gone is still CANCELLED by
        # digest (idempotent — an unknown digest costs one execute weight and a
        # venue "not found"). Should the list ever be wrong (a digest shape the
        # venue changed under us), a still-live rung is cancelled rather than
        # duplicated by the fresh ladder below.
        if missing and net == 0 and self._pos_base == 0:
            logger.info(
                "revgrid %s: %s armed rung(s) no longer pending on the venue while flat "
                "(fired unfilled / rejected / dropped) — re-anchoring the ladder at %s "
                "(user=%s)",
                self.trading_pair, len(missing), mid, self.user_id,
            )
            await self._cancel_all_rungs()      # cancels every unfired rung, missing ones included
            self._anchor = None
        elif missing:
            def _crossed(r: _Rung) -> bool:
                return (mid >= r.level) if r.side is TradeType.BUY else (mid <= r.level)
            dropped = [r for r in missing if not _crossed(r)]
            if dropped:
                logger.info(
                    "revgrid %s: %s armed add rung(s) no longer pending on the venue — "
                    "dropped (position keeps its stop; the pyramid has fewer adds) (user=%s)",
                    self.trading_pair, len(dropped), self.user_id,
                )
                for r in dropped:
                    try:
                        await self.adapter.cancel_trigger_order(r.digest)
                    except AdapterError:
                        logger.debug("revgrid dropped-rung cancel failed digest=%s", r.digest, exc_info=True)
                self._rungs = [r for r in self._rungs if r not in dropped]
        if stop_missing:
            logger.warning(
                "revgrid %s: protective stop %s is no longer pending on the venue — "
                "re-arming protection now (user=%s)",
                self.trading_pair, self._stop_digest, self.user_id,
            )
            old = self._stop_digest
            self._stop_digest = None
            self._stop_level = None
            self._stop_size = None
            if old is not None:
                # Idempotent: if the list was wrong and the stop is live, this
                # cancels it before the re-arm so two stops never stack.
                try:
                    await self.adapter.cancel_trigger_order(old)
                except AdapterError:
                    logger.debug("revgrid missing-stop cancel failed digest=%s", old, exc_info=True)

    # -- chop stand-down gate ------------------------------------------------
    async def _candles(self) -> List[dict]:
        """Classification candles from the injected provider (the live cycle and the
        backtester both wire one). Empty when none — the classifier then reports
        insufficient history and the gate holds its safe (stand-down) default."""
        provider = self.cfg("candle_provider")
        if provider is None:
            return []
        try:
            result = provider(self.trading_pair)  # type: ignore[operator]
            if inspect.isawaitable(result):
                result = await result
            return list(result or [])
        except Exception:  # noqa: BLE001 - no candles ⇒ stand down (safe)
            return []

    async def _classify_regime(self) -> None:
        """Update ``_confirmed_trend_dir`` from the variance-regime routine. A trend is
        confirmed only after ``trend_confirm_ticks`` consecutive same-direction
        SUSTAINED-DRIFT ticks — a single bursty swing (high VR) never clears the bar."""
        if not self.chop_stand_down:
            return
        info = await variance_regime.run(
            self.trading_pair, await self._candles(),
            short_window=self._regime_short_window,
            long_window=self._regime_long_window,
            trend_on=self._regime_trend_on_vr,
            range_on=self._regime_range_on_vr,
            trend_drift_pct=self._regime_trend_drift_pct,
            current_phase=self._regime_phase,
        )
        if not info.get("insufficient_history"):
            self._regime_phase = str(info.get("phase") or self._regime_phase)
        d = str(info.get("direction") or variance_regime.FLAT)
        if (not info.get("insufficient_history") and bool(info.get("trend_by_drift"))
                and d in (variance_regime.UP, variance_regime.DOWN)):
            if d == self._trend_streak_dir:
                self._trend_streak += 1
            else:
                self._trend_streak_dir, self._trend_streak = d, 1
        else:
            self._trend_streak, self._trend_streak_dir = 0, variance_regime.FLAT
        self._confirmed_trend_dir = (
            self._trend_streak_dir if self._trend_streak >= self._trend_confirm_ticks
            else variance_regime.FLAT
        )

    def _trend_confirmed(self) -> bool:
        return self._confirmed_trend_dir in (variance_regime.UP, variance_regime.DOWN)

    # -- flat: arm / re-anchor the ladder ------------------------------------
    async def _maintain_flat(self, mid: Decimal) -> None:
        # PRESENCE-FIRST (user directive): the FIRST arming of the run is never gated
        # — the reverse grid enters the market immediately so a break in either
        # direction fills from the first tick. The chop stand-down only governs
        # RE-arming after a close (``_has_opened``): a whipsaw stop-out then waits
        # for a confirmed trend instead of instantly re-entering the same chop,
        # which bounds the chop bleed to ~one round trip per episode. (An OPEN
        # position is never here — its stop manages it and the gate never touches
        # an exit.) The finite ladder + venue reduce-only stop + session %-margin
        # rail remain the risk bounds on the presence-first entry.
        if self.chop_stand_down and self._has_opened and not self._trend_confirmed():
            # Re-arm after a close, no confirmed trend: stand down. Drop any resting
            # ladder and re-anchor fresh when a trend resumes.
            self.gate_verdict, self.gate_reason = "PAUSE", "revgrid_chop"
            if self._rungs:
                await self._cancel_all_rungs()
            self._anchor = None
            return
        # Armed / re-anchoring: quoting is live again.
        self.gate_verdict, self.gate_reason = "QUOTE", ""
        if self._anchor is None:
            self._anchor = mid
        drift = abs(mid - self._anchor) / self._anchor if self._anchor > 0 else Decimal(0)
        if drift > self.reanchor_bands * self.step_pct:
            await self._cancel_all_rungs()
            self._anchor = mid
        # Re-arm when the ladder is empty OR was left partial by the per-cycle
        # opening cap. _place_ladder only places rungs that are not already resting,
        # so this completes a capped ladder over successive ticks without
        # double-placing the rungs that are already on the book.
        if not self._rungs_resting() or self._ladder_incomplete:
            await self._place_ladder(self._anchor)

    def _rungs_resting(self) -> bool:
        return any(not r.fired for r in self._rungs)

    async def _place_ladder(self, anchor: Decimal) -> None:
        """Arm both sides: BUY rungs above the anchor, SELL rungs below it.

        Resumable + cap-aware: skips any (side, level) already resting and stops
        once the per-cycle opening budget is spent, flagging the ladder incomplete
        so ``_maintain_flat`` places the remaining rungs on the next tick. Each rung
        is sized independently (``_rung_base``), so a truncated ladder never resizes
        the rungs that do get placed."""
        if anchor <= 0 or self.order_amount_quote <= 0:
            return
        resting = {(r.side, r.level) for r in self._rungs if not r.fired}
        placed = 0
        failed = False
        for k in range(1, self.levels + 1):
            offset = self.step_pct * Decimal(k)
            for side, level in (
                (TradeType.BUY, anchor * (Decimal(1) + offset)),
                (TradeType.SELL, anchor * (Decimal(1) - offset)),
            ):
                if level <= 0:
                    continue
                if (side, level) in resting:
                    continue          # already on the book — don't double-place
                # Per-cycle opening cap: stop laying rungs once the budget is spent.
                # The remaining rungs are placed on the next tick (see _maintain_flat).
                # EXECUTE-BUDGET-BACKOFF: also stop once a placement was throttled by the
                # execute budget this cycle (bucket drained — the rest would 429 anyway).
                if self.adapter.opening_budget_exhausted() or self.adapter.execute_budget_exhausted():
                    self._n_placed += placed
                    self._ladder_incomplete = True
                    logger.info(
                        "revgrid ladder capped: armed %s rungs this cycle, "
                        "deferring the rest (anchor=%s user=%s pair=%s)",
                        placed, anchor, self.user_id, self.trading_pair,
                    )
                    return
                size = self._rung_base(level)
                if size is None or size <= 0:
                    continue
                try:
                    order = await self.adapter.place_trigger_order(
                        self.trading_pair, side, size, level,
                        slippage_pct=self.entry_slippage_pct,
                        # IOC: fill on fire or vanish — never rest as an untracked limit.
                        order_type="ioc",
                    )
                except AdapterError:
                    logger.warning(
                        "revgrid rung place failed side=%s level=%s (user=%s pair=%s)",
                        side.name, level, self.user_id, self.trading_pair, exc_info=True,
                    )
                    failed = True
                    continue
                self._rungs.append(_Rung(order.id, side, level, size))
                placed += 1
        self._n_placed += placed
        # A transient placement failure leaves a hole: keep re-arming (only the
        # missing rungs) on the next ticks instead of resting an asymmetric ladder.
        self._ladder_incomplete = bool(failed)
        logger.info(
            "revgrid armed %s trigger rungs around anchor %s (levels=%s step=%s "
            "user=%s pair=%s)",
            placed, anchor, self.levels, self.step_pct, self.user_id, self.trading_pair,
        )

    def _rung_base(self, level: Decimal) -> Optional[Decimal]:
        """Base size for a rung at ``level``: the per-rung notional / level, floored
        to the venue lot and declined below the venue minimum notional (a
        sub-minimum rung would be GROWN by the venue past the risk-approved size)."""
        if level <= 0:
            return None
        base = self.order_amount_quote / level
        try:
            lot = _dec(self.adapter.lot_size(self.trading_pair) or 0)
            floor_quote = _dec(self.adapter.min_notional(self.trading_pair) or 0)
        except Exception:  # noqa: BLE001 - no metadata ⇒ send as-is
            return base
        if lot > 0:
            base = (base / lot).to_integral_value(rounding=ROUND_DOWN) * lot
        if base <= 0:
            return None
        if floor_quote > 0 and base * level < floor_quote:
            return None
        return base

    async def _cancel_all_rungs(self) -> None:
        for r in self._rungs:
            if r.fired:
                continue              # consumed on the venue — nothing to cancel
            self._n_cancelled += 1
            try:
                await self.adapter.cancel_trigger_order(r.digest)
            except AdapterError:
                logger.debug("revgrid rung cancel failed digest=%s", r.digest, exc_info=True)
        self._rungs = []
        # No ladder on the book — the next flat arm starts a fresh, complete lay.
        self._ladder_incomplete = False

    async def _cancel_side_rungs(self, side: TradeType) -> None:
        keep: List[_Rung] = []
        for r in self._rungs:
            if r.side is side and not r.fired:
                self._n_cancelled += 1
                try:
                    await self.adapter.cancel_trigger_order(r.digest)
                except AdapterError:
                    logger.debug("revgrid opp cancel failed digest=%s", r.digest, exc_info=True)
            else:
                keep.append(r)
        self._rungs = keep

    # -- position: open / grow / trail ---------------------------------------
    async def _open_position(self, net: Decimal, mid: Decimal) -> None:
        long = net > 0
        # A position has now opened this run: subsequent flats are RE-arms, which
        # the chop stand-down governs (see _maintain_flat) — the presence-first
        # first entry is done.
        self._has_opened = True
        # The losing side is cancelled — the break went the other way.
        await self._cancel_side_rungs(TradeType.SELL if long else TradeType.BUY)
        self._attr_num, self._attr_base = Decimal(0), Decimal(0)
        self._avg_entry = None
        self._recompute_avg_entry(net)
        self._peak = mid
        self._trail_armed = False
        self._stop_digest = None
        self._stop_level = None
        self._stop_size = None
        logger.info(
            "revgrid OPEN %s net=%s avg_entry=%s (mid=%s user=%s pair=%s)",
            "long" if long else "short", net, self._avg_entry, mid,
            self.user_id, self.trading_pair,
        )

    def _recompute_avg_entry(self, net: Decimal) -> None:
        """Average entry of the open position, attributed to the ladder's own rungs.

        Only the GROWTH since the last read is attributed (a shrink keeps the cost
        basis — closing part of a position does not change what the rest cost).
        Rungs fire NEAREST the anchor first (k=1 before k=2), so the growth is
        laid onto un-fired same-side rungs in that order, at most one rung's size
        each, and each rung it touches is marked FIRED with its actual attributed
        base: a fired IOC is consumed whether it filled in full or in part, so a
        40% partial must not be credited at full size (it understated the entry
        and loosened the stop; audit 2026-09-16). Growth beyond what the ladder
        explains (a manual add) is booked at the last mid. The basis is the
        running base-weighted average of everything attributed — a trigger fills
        at ~its level, so the level stands in for the print. This needs no fill
        feed and no digest match; the ladder geometry is the basis.
        """
        side = TradeType.BUY if net > 0 else TradeType.SELL
        prev = abs(self._pos_base) if _sign(self._pos_base) == _sign(net) else Decimal(0)
        remaining = abs(net) - prev
        if remaining <= 0:
            if self._avg_entry is None:
                self._avg_entry = self._anchor or self._last_mid
            return
        same = sorted(
            [r for r in self._rungs if r.side is side and not r.fired],
            key=lambda r: abs(r.level - (self._anchor or r.level)),
        )
        for r in same:
            if remaining <= 0:
                break
            take = min(r.size_base, remaining)
            if take <= 0:
                continue
            r.fired = True
            r.filled_base = take
            self._n_filled += 1         # a rung just fired (entry / add)
            self._attr_num += r.level * take
            self._attr_base += take
            remaining -= take
        if remaining > 0:
            px = self._last_mid or self._anchor
            if px:
                self._attr_num += px * remaining
                self._attr_base += remaining
        if self._attr_base > 0:
            self._avg_entry = self._attr_num / self._attr_base
        elif self._avg_entry is None:
            # No ladder to attribute against (a user restart mid-position): fall back
            # to the anchor / last mid so a protective stop can still be sized.
            self._avg_entry = self._anchor or self._last_mid

    async def _maintain_position(self, net: Decimal, mid: Decimal) -> None:
        long = net > 0
        entry = self._avg_entry or self._anchor or mid
        if entry is None or entry <= 0:
            return
        # Track the favourable extreme.
        self._peak = mid if self._peak is None else (max(self._peak, mid) if long else min(self._peak, mid))
        fav = ((self._peak - entry) / entry) if long else ((entry - self._peak) / entry)
        if fav >= self.trail_arm_pct:
            self._trail_armed = True

        protective = entry * (Decimal(1) - self.stop_pct) if long else entry * (Decimal(1) + self.stop_pct)
        if self._trail_armed and self._peak is not None:
            trailing = (self._peak * (Decimal(1) - self.trail_giveback_pct) if long
                        else self._peak * (Decimal(1) + self.trail_giveback_pct))
            desired = max(protective, trailing) if long else min(protective, trailing)
        else:
            desired = protective
        # Ratchet: a long's stop never loosens (only rises); a short's never rises.
        if self._stop_level is not None:
            desired = max(desired, self._stop_level) if long else min(desired, self._stop_level)
        await self._ensure_stop(net, desired)
        await self._retry_stale_stops()

    async def _ensure_stop(self, net: Decimal, level: Decimal) -> None:
        """Arm / re-arm the single venue reduce-only stop. Place-then-cancel so the
        position is NEVER momentarily unprotected (two reduce-only stops briefly is
        safe — the venue can't over-close). Re-arms only when the size changed or
        the level moved at least ``stop_min_reprice_frac`` of a step, to bound churn."""
        if level <= 0 or net == 0:
            return
        size = abs(net)
        long = net > 0
        moved_enough = (
            self._stop_level is None
            or abs(level - self._stop_level) >= level * self.step_pct * self.stop_min_reprice_frac
        )
        size_changed = self._stop_size is None or self._stop_size != size
        if self._stop_digest is not None and not moved_enough and not size_changed:
            return
        try:
            order = await self.adapter.place_stop_order(
                self.trading_pair, size, level, long, slippage_pct=self.stop_slippage_pct,
            )
        except AdapterError:
            logger.warning(
                "revgrid stop arm FAILED size=%s level=%s long=%s — the session rail "
                "is the backstop (user=%s pair=%s)",
                size, level, long, self.user_id, self.trading_pair, exc_info=True,
            )
            return
        self._n_placed += 1
        old = self._stop_digest
        self._stop_digest = order.id
        self._stop_level = level
        self._stop_size = size
        if old is not None:
            self._n_cancelled += 1
            try:
                await self.adapter.cancel_trigger_order(old)
            except AdapterError:
                logger.debug("revgrid stale stop cancel failed digest=%s", old, exc_info=True)
                self._stale_stops.append(old)     # retried on later ticks / at close

    async def _retry_stale_stops(self) -> None:
        """Retry the cancel of superseded stops whose cancel failed earlier."""
        if not self._stale_stops:
            return
        still: List[str] = []
        for digest in self._stale_stops:
            try:
                await self.adapter.cancel_trigger_order(digest)
            except AdapterError:
                still.append(digest)
        self._stale_stops = still

    # -- close / reset -------------------------------------------------------
    async def _reset_after_close(self) -> None:
        """The position is flat (stop fired / rail flattened). Cancel any residual
        triggers — the stop and any still-resting rungs — and clear all position
        and ladder state so the next flat tick re-anchors to the current mid."""
        self._n_filled += 1       # the exit that flattened the position (stop / rail)
        if self._stop_digest is not None:
            try:
                await self.adapter.cancel_trigger_order(self._stop_digest)
            except AdapterError:
                logger.debug("revgrid stop cancel-on-close failed", exc_info=True)
                self._stale_stops.append(self._stop_digest)
        await self._retry_stale_stops()
        await self._cancel_all_rungs()
        self._anchor = None       # re-anchor to the current mid on the next flat tick
        self._reset_position_state()

    def _reset_position_state(self) -> None:
        self._avg_entry = None
        self._attr_num, self._attr_base = Decimal(0), Decimal(0)
        self._peak = None
        self._trail_armed = False
        self._stop_digest = None
        self._stop_level = None
        self._stop_size = None

    # -- introspection (for /status and tests) -------------------------------
    def order_counts(self) -> dict:
        """Trigger activity for /status. Overrides the base (which sums executors —
        this controller has none). placed = rungs + stops armed; filled = rungs that
        fired + exits; cancelled = triggers/stops cancelled. Approximate but real,
        so /status shows activity instead of a flat zero."""
        return {
            "orders_placed": self._n_placed,
            "orders_filled": self._n_filled,
            "orders_cancelled": self._n_cancelled,
        }

    def grid_metrics(self) -> dict:
        """Telemetry for the /status card. Emits the SAME ``grid_*`` keys the legacy
        rgrid card pipeline reads (bot_runtime maps them to ``rgrid_*``; formatters
        renders them), so the trigger reverse grid renders in the existing card
        instead of showing blanks — plus a few reverse-grid extras the card ignores
        but tests / logs use."""
        anchor = self._anchor
        long = self._pos_base > 0
        short = self._pos_base < 0
        drift_pct = 0.0
        if anchor and anchor > 0 and self._last_mid is not None:
            drift_pct = float((self._last_mid - anchor) / anchor * Decimal(100))
        entry = float(self._avg_entry) if self._avg_entry else 0.0
        return {
            # --- keys the rgrid /status card consumes ---
            "grid_anchor_price": float(anchor) if anchor else 0.0,
            "grid_net_base": float(self._pos_base),
            # The reverse grid has one cost basis, not two exposure legs; surface it
            # on the side actually held so the card shows a real entry.
            "grid_buy_exposure_price": entry if long else 0.0,
            "grid_sell_exposure_price": entry if short else 0.0,
            "grid_drift_from_anchor_pct": drift_pct,
            # The trailing stop IS the reverse grid's "soft reset": armed once in
            # profit, it ratchets the exit with the move.
            "grid_reset_active": bool(self._trail_armed),
            "grid_reset_side": "long" if long else ("short" if short else "none"),
            # --- reverse-grid ladder telemetry (rendered by the /status card) ---
            "grid_rungs_armed": sum(1 for r in self._rungs if not r.fired),
            "grid_rungs_per_side": int(self.levels),
            "grid_step_bp": float(self.step_pct * Decimal(10000)),
            "grid_stop_level": float(self._stop_level) if self._stop_level else 0.0,
            "grid_trail_armed": bool(self._trail_armed),
            # Persisted by the runtime and restored on a rebuild (see __init__).
            "grid_venue_baseline": (
                float(self._baseline_net) if self._baseline_net is not None else None
            ),
            # Persisted every cycle so the stop / restart sweeps can vouch for this
            # run's triggers even if the placement→session DB link was missed, and
            # keep the protective stop when the position is left open (boot).
            "grid_stop_digest": self._stop_digest,
            "grid_trigger_digests": (
                [r.digest for r in self._rungs if not r.fired]
                + ([self._stop_digest] if self._stop_digest else [])
                + list(self._stale_stops)
            ),
            # --- reverse-grid extras (ignored by the card; used by tests/logs) ---
            "avg_entry": self._avg_entry,
            "stop_level": self._stop_level,
            "trail_armed": self._trail_armed,
            "peak": self._peak,
            "resting_rungs": sum(1 for r in self._rungs if not r.fired),
            "step_pct": self.step_pct,
            "last_mid": self._last_mid,
        }
