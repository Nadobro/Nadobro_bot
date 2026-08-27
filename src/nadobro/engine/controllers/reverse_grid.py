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
``place_stop_order``), NOT through the OrderExecutor: an executor cancels via the
resting-order path, which is the wrong venue service for a trigger and would leak
it. Fills are detected by POLLING the net position (``adapter.held_base``) and
attributing the change to the ladder's own rung geometry — this needs no
assumption about whether a fired trigger's fill echoes the trigger's digest.

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
from src.nadobro.engine.types import TradeType, _as_bool, _dec
from src.nadobro.quant.rgrid_sizing import TAKER_ROUND_TRIP_RATE, arm_pct

logger = logging.getLogger(__name__)


@dataclass
class _Rung:
    """One armed entry trigger on the ladder."""
    digest: str
    side: TradeType
    level: Decimal
    size_base: Decimal
    fired: bool = False


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
        self._pos_base: Decimal = Decimal(0)      # last observed signed net
        self._avg_entry: Optional[Decimal] = None
        self._peak: Optional[Decimal] = None      # favourable extreme mid since open
        self._trail_armed = False
        self._stop_digest: Optional[str] = None
        self._stop_level: Optional[Decimal] = None
        self._stop_size: Optional[Decimal] = None
        self._last_mid: Optional[Decimal] = None
        # Order telemetry for /status. The base order_counts() sums executors, which
        # this controller has none of — it manages venue triggers directly — so it
        # tracks its own placed / filled / cancelled counts and overrides that method.
        self._n_placed = 0
        self._n_filled = 0
        self._n_cancelled = 0

    # -- config ---------------------------------------------------------------
    def _load_config(self) -> None:
        """(Re)read the config-derived geometry + gate params from ``self.configs``.
        Called at construction and by :meth:`reload_config` on a live settings edit.
        Sets ONLY config attrs — never the runtime position/regime state."""
        self.trading_pair = str(self.cfg("trading_pair") or "")
        self.levels = max(1, int(self.cfg("levels", 4) or 4))
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
        # How far THROUGH its level a fired entry crosses (a few bp — enough to fill,
        # not to overpay); the stop close is priced further through so it always fills.
        self.entry_slippage_pct = float(self.cfg("entry_slippage_pct", 0.05) or 0.05)
        self.stop_slippage_pct = float(self.cfg("stop_slippage_pct", 0.5) or 0.5)
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
        # No auto-resume: a redeploy/start never re-arms a prior ladder. Start from
        # a clean slate; the first on_tick anchors and arms from the live mid. If the
        # account happens to hold a position (a user restart mid-trade), on_tick
        # detects it and arms a protective stop rather than adding new exposure.
        self._anchor = None
        self._rungs = []
        self._reset_position_state()

    async def on_tick(self) -> None:
        mid = await self._mid()
        if mid is None or mid <= 0:
            return
        net = await self._read_net()
        if net is None:
            # Unreadable venue — hold. Never act (open, close, reset) on a bad read.
            return
        self._last_mid = mid
        # Refresh the regime read every tick so the flat gate and the debounce stay
        # current (cheap; exits never consult it).
        await self._classify_regime()

        if net == 0:
            if self._pos_base != 0:
                await self._reset_after_close()
            self._pos_base = Decimal(0)
            await self._maintain_flat(mid)
            return

        # In a position.
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
        """Cancel every venue trigger this controller owns — the ladder and the
        stop — so a stopped session leaves nothing watching the mid."""
        await self._reset_after_close()

    # -- venue reads ---------------------------------------------------------
    async def _mid(self) -> Optional[Decimal]:
        try:
            return _dec(await self.adapter.mid_price(self.trading_pair))
        except Exception:  # noqa: BLE001 - a bad mid read holds the tick
            logger.debug("revgrid mid read failed pair=%s", self.trading_pair, exc_info=True)
            return None

    async def _read_net(self) -> Optional[Decimal]:
        """Signed net position base from the VENUE (``held_base``). None = the venue
        could not be read — the caller then holds and acts on nothing (fail safe)."""
        try:
            held = await self.adapter.held_base(self.trading_pair)
        except Exception:  # noqa: BLE001
            logger.debug("revgrid net read failed pair=%s", self.trading_pair, exc_info=True)
            return None
        return None if held is None else _dec(held)

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
        if self.chop_stand_down and not self._trend_confirmed():
            # No confirmed trend: stand down. Never ENTER in chop — drop any resting
            # ladder and re-anchor fresh when a trend resumes. (An OPEN position is not
            # here — it is managed by its stop, which the gate never touches.)
            if self._rungs:
                await self._cancel_all_rungs()
            self._anchor = None
            return
        if self._anchor is None:
            self._anchor = mid
        drift = abs(mid - self._anchor) / self._anchor if self._anchor > 0 else Decimal(0)
        if drift > self.reanchor_bands * self.step_pct:
            await self._cancel_all_rungs()
            self._anchor = mid
        if not self._rungs_resting():
            await self._place_ladder(self._anchor)

    def _rungs_resting(self) -> bool:
        return any(not r.fired for r in self._rungs)

    async def _place_ladder(self, anchor: Decimal) -> None:
        """Arm both sides: BUY rungs above the anchor, SELL rungs below it."""
        if anchor <= 0 or self.order_amount_quote <= 0:
            return
        placed = 0
        for k in range(1, self.levels + 1):
            offset = self.step_pct * Decimal(k)
            for side, level in (
                (TradeType.BUY, anchor * (Decimal(1) + offset)),
                (TradeType.SELL, anchor * (Decimal(1) - offset)),
            ):
                if level <= 0:
                    continue
                size = self._rung_base(level)
                if size is None or size <= 0:
                    continue
                try:
                    order = await self.adapter.place_trigger_order(
                        self.trading_pair, side, size, level,
                        slippage_pct=self.entry_slippage_pct,
                    )
                except AdapterError:
                    logger.warning(
                        "revgrid rung place failed side=%s level=%s (user=%s pair=%s)",
                        side.name, level, self.user_id, self.trading_pair, exc_info=True,
                    )
                    continue
                self._rungs.append(_Rung(order.id, side, level, size))
                placed += 1
        self._n_placed += placed
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
            if not r.fired:
                self._n_cancelled += 1
            try:
                await self.adapter.cancel_trigger_order(r.digest)
            except AdapterError:
                logger.debug("revgrid rung cancel failed digest=%s", r.digest, exc_info=True)
        self._rungs = []

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
        # The losing side is cancelled — the break went the other way.
        await self._cancel_side_rungs(TradeType.SELL if long else TradeType.BUY)
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

        The observed net grew on one side, so the fill was some of that side's
        rungs. They fire NEAREST the anchor first (k=1 before k=2), so attribute the
        filled base to un-fired same-side rungs in that order and take the
        size-weighted average of their LEVELS — a trigger fills at ~its level. This
        needs no fill feed and no digest match; the ladder geometry is the basis.
        """
        side = TradeType.BUY if net > 0 else TradeType.SELL
        target = abs(net)
        same = sorted(
            [r for r in self._rungs if r.side is side],
            key=lambda r: abs(r.level - (self._anchor or r.level)),
        )
        acc = Decimal(0)
        num = Decimal(0)
        for r in same:
            if acc >= target:
                break
            take = min(r.size_base, target - acc)
            if take <= 0:
                continue
            if not r.fired:
                self._n_filled += 1     # a rung just fired (entry / add)
            r.fired = True
            num += r.level * take
            acc += take
        if acc > 0:
            self._avg_entry = num / acc
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
        await self._cancel_all_rungs()
        self._anchor = None       # re-anchor to the current mid on the next flat tick
        self._reset_position_state()

    def _reset_position_state(self) -> None:
        self._avg_entry = None
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
            # --- reverse-grid extras (ignored by the card; used by tests/logs) ---
            "avg_entry": self._avg_entry,
            "stop_level": self._stop_level,
            "trail_armed": self._trail_armed,
            "peak": self._peak,
            "resting_rungs": sum(1 for r in self._rungs if not r.fired),
            "step_pct": self.step_pct,
            "last_mid": self._last_mid,
        }
