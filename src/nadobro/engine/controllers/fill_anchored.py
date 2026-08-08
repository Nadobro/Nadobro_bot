"""Fill-anchored quoting — Grid's single-pair maker mode (opt-in).

Where the classic ladder anchors a fixed band at spawn time, this controller
prices off a reference that moves with fills:

- reference = the LAST EXECUTED FILL, with the no-cross invariant — never place a
  buy above the last sell, never a sell below the last buy. Captures spread in
  chop; each round trip re-anchors the pair.
- **Soft reset**: when mid drifts further than ``reset_threshold_pct`` from the
  reference, quotes temporarily re-anchor to mid (the ref-anchored pair is either
  stranded or un-postable post-only) — the next fill snaps the reference back and
  grid pricing resumes. No flatten, no restart.
- **Stall escalation**: if the soft-reset's conceding maker leg cannot rebalance
  one-sided exposure within N ticks, a BOUNDED reduce-only taker concession
  flattens part of it before the SL rail.

Selected by grid's ``fill_anchored`` setting; the classic ladder is grid's
default. Reverse Grid does NOT live here — it is its own strategy with its own
controller and taker executor (engine/controllers/rgrid.py). It used to share
this class, which is how a grid-shaped concept (an "anchor_mode") ended up
governing a trend strategy.

Inherits MarketMakingController's executor plumbing, regime gate
(pause = reduce-only, never flatten), inventory cap, and ATR auto-spread.
"""
from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal
from typing import Deque, Optional, Tuple

from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.executors.order_executor import OrderExecutor, OrderExecutorConfig
from src.nadobro.engine.risk import ExecutorRequest
from src.nadobro.engine.types import ExecutionStrategy, PositionAction, TradeType, _dec

logger = logging.getLogger(__name__)

# How many recent fills to retain behind the last-fill reference.
_FILL_HISTORY = 200


class FillAnchoredQuotingController(MarketMakingController):
    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("name", "fill_anchored")
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # Kept for the config contract and telemetry; only "grid" reaches here now.
        self.mode = str(self.cfg("anchor_mode", "grid")).lower()
        self.reset_threshold_pct = _dec(self.cfg("reset_threshold_pct", "0.0025"))
        self._reference: Optional[Decimal] = None
        self._last_buy_px: Optional[Decimal] = None
        self._last_sell_px: Optional[Decimal] = None
        self._fills: Deque[Tuple[Decimal, Decimal]] = deque(maxlen=_FILL_HISTORY)
        self._seen_filled: set[str] = set()
        self._pending_taker_id: Optional[str] = None
        self._soft_reset_active: bool = False
        self._last_ref: Optional[Decimal] = None
        # Two-step stall escalation (grid): if the soft-reset's conceding maker
        # leg can't rebalance one-sided exposure within N consecutive ticks (price
        # keeps trending away), escalate to a BOUNDED reduce-only TAKER concession
        # to flatten part of the exposure before the session SL rail fires — the
        # documented price concession, done safely (bounded fraction, fee-aware
        # via a material-exposure gate) instead of a floored negative spread.
        self.concession_enabled = bool(self.cfg("concession_enabled", False))
        self.concession_escalation_ticks = max(1, int(self.cfg("concession_escalation_ticks", 5) or 5))
        _frac = _dec(self.cfg("concession_fraction", "0.5") or "0.5")
        self.concession_fraction = max(Decimal("0.05"), min(_frac, Decimal(1)))
        self._stall_ticks: int = 0
        self._last_net_base: Decimal = Decimal(0)
        # SESSION ISOLATION: the exposure VWAP must reflect THIS run's fills only.
        # In-memory absorption already guarantees that (my_executors is scoped to
        # this controller_id = strategy:user:network and a per-run orchestrator),
        # but a rebuild (worker handoff / restart) would otherwise start blank and
        # lose the session's prior fills. The runtime injects ``seed_fills`` —
        # this session's OWN recorded trades (get_session_recent_fills, scoped by
        # strategy_session_id + user_id) — so the anchor is provably per-session
        # and survives rebuilds. Never seeded from other users/sessions/products.
        self._seed_from_history(self.cfg("seed_fills", None))

    def _seed_from_history(self, rows: object) -> None:
        """Seed the exposure window from this session's recorded fills (newest
        first), so the VWAP/anchor reflect the run's real history on (re)build."""
        if not rows or not isinstance(rows, (list, tuple)):
            return
        parsed: list[Tuple[Decimal, Decimal, str]] = []
        for r in rows:
            if isinstance(r, dict):
                px_raw, base_raw, side = r.get("price"), r.get("size"), r.get("side")
            elif isinstance(r, (list, tuple)) and len(r) >= 2:
                px_raw, base_raw = r[0], r[1]
                side = r[2] if len(r) >= 3 else None
            else:
                continue
            try:
                px = _dec(px_raw)
                base = abs(_dec(base_raw))
            except Exception:  # policy: degrade-ok(skip a malformed seeded fill; live fills re-anchor)
                continue
            if px <= 0 or base <= 0:
                continue
            parsed.append((px, base, str(side or "").lower()))
        # rows arrive newest-first; append oldest-first so the deque order and the
        # final _reference (newest fill) match live absorption.
        for px, base, side in reversed(parsed):
            self._fills.append((px, base))
            self._reference = px
            if side in ("long", "buy"):
                self._last_buy_px = px
            elif side in ("short", "sell"):
                self._last_sell_px = px

    # -- fill anchoring -----------------------------------------------------
    def _absorb_fills(self) -> None:
        """Re-anchor on every child quote that terminated FILLED.

        EXIT fills are excluded (AUDIT-F9). ``_fills`` is the *exposure* window —
        the price at which the book took ON risk — and the reduce-only actions
        (the grid stall concession) are risk REMOVAL at
        whatever the market offered. Feeding them back in dragged the VWAP
        toward the exit price, so the reference drifted to reflect its own
        closes instead of where the book actually took risk on.

        Ordinary maker round trips are NOT exits in this sense: a grid sell that
        happens to close a long is the strategy working, and must re-anchor.
        Only orders explicitly tagged ``PositionAction.CLOSE`` are skipped.
        """
        from src.nadobro.engine.adapter.base import OrderState

        for ex in self.my_executors(active_only=False):
            if ex.id in self._seen_filled:
                continue
            order = getattr(ex, "order", None)
            if order is None or order.state is not OrderState.FILLED:
                continue
            self._seen_filled.add(ex.id)
            if getattr(getattr(ex, "config", None), "position_action", None) is PositionAction.CLOSE:
                continue    # exit fill: inventory absorbs it, the anchor must not
            base = abs(_dec(order.filled_base))
            quote = abs(_dec(order.filled_quote))
            if base <= 0:
                continue
            px = quote / base
            self._reference = px
            self._fills.append((px, base))
            side = getattr(getattr(ex, "config", None), "side", None)
            if side is TradeType.BUY:
                self._last_buy_px = px
            elif side is TradeType.SELL:
                self._last_sell_px = px
        # Bounded: forget terminated executors the orchestrator has dropped.
        if len(self._seen_filled) > 200:
            live = {e.id for e in self.my_executors(active_only=False)}
            self._seen_filled &= live









    def _position_entry_price(self) -> Optional[Decimal]:
        """The open position's own cost basis from inventory — avg BUY price for
        a long, avg SELL price for a short. Exits never enter this number, which
        is what makes it a trustworthy reference for a profit target."""
        if self.inventory is None:
            return None
        try:
            hold = self.inventory.get(self.user_id, self.trading_pair, self.id)
            be = hold.breakeven
        except Exception:  # noqa: BLE001  # policy: degrade-ok(fall back to the exposure window)
            return None
        return be if (be is not None and be > 0) else None

    def _current_reference(self, mid: Decimal) -> Decimal:
        return self._reference or mid

    # -- stall escalation (grid) --------------------------------------------
    def _concession_in_flight(self) -> bool:
        """A reduce-only concession is already working — never stack another."""
        if self._pending_taker_id is None:
            return False
        ex = self.orchestrator.get(self._pending_taker_id)
        if ex is None or ex.is_terminated:
            self._pending_taker_id = None
            return False
        return True

    def _net_base(self) -> Decimal:
        if self.inventory is None:
            return Decimal(0)
        return self.inventory.get(self.user_id, self.trading_pair, self.id).net_amount_base

    async def _fire_concession(self, net_base: Decimal, mid: Decimal) -> None:
        """Flatten a BOUNDED fraction of the one-sided exposure with a reduce-only
        TAKER market order (never adds/flips). Net long ⇒ sell to reduce; net
        short ⇒ buy to reduce."""
        if mid <= 0 or net_base == 0:
            return
        side = TradeType.SELL if net_base > 0 else TradeType.BUY
        amount_base = abs(net_base) * self.concession_fraction
        if amount_base <= 0:
            return
        oc = OrderExecutorConfig(
            self.trading_pair, side, amount_base, ExecutionStrategy.MARKET,
            leverage=int(self.cfg("leverage", 1) or 1),
            position_action=PositionAction.CLOSE,   # reduce-only: never add/flip
        )
        ex = OrderExecutor(
            oc, user_id=self.user_id, controller_id=self.id,
            adapter=self.adapter, inventory=self.inventory,
        )
        ok = await self.spawn_executor(ex, ExecutorRequest(order_amount_quote=amount_base * mid))
        if ok:
            self._pending_taker_id = ex.id
            logger.warning(
                "grid concession: soft-reset stalled %s ticks — flattening %s %s of %s @ market "
                "(user=%s pair=%s)",
                self._stall_ticks, side.name, amount_base, abs(net_base),
                self.user_id, self.trading_pair,
            )

    async def _maybe_escalate_concession(self, mid: Decimal) -> None:
        """Track whether the soft-reset is rebalancing; if one-sided exposure
        stays stuck for ``concession_escalation_ticks``, fire a bounded reduce-only
        taker concession (fee-aware: only when at least one quote's worth of
        exposure is stuck)."""
        net_base = self._net_base()
        # Not in a soft reset, or flat → nothing stuck; reset the counter.
        if not self._soft_reset_active or net_base == 0:
            self._stall_ticks = 0
            self._last_net_base = net_base
            return
        # LADDER: these thresholds mean "one QUOTE's worth", not one side's
        # worth. order_amount_quote became the per-side deployment when the
        # ladder landed, so both must divide by the level count — otherwise the
        # concession safety valve silently needs `levels`x more stuck exposure
        # before it fires.
        level_quote = self.order_amount_quote / Decimal(max(1, self.ladder_levels))
        dust = abs(level_quote / mid) * Decimal("0.01") if mid > 0 else Decimal(0)
        if abs(net_base) < abs(self._last_net_base) - dust:
            self._stall_ticks = 0           # exposure shrinking — the maker leg is filling
        else:
            self._stall_ticks += 1
        self._last_net_base = net_base
        if not self.concession_enabled or self._concession_in_flight():
            return
        if self._stall_ticks < self.concession_escalation_ticks:
            return
        # Fee-aware gate: only pay a taker fee to unstick MATERIAL exposure
        # (≥ one quote's notional); let the maker leg handle smaller residue.
        if abs(net_base) * mid < level_quote:
            return
        await self._fire_concession(net_base, mid)
        self._stall_ticks = 0               # re-arm; fires again next window if still stuck

    # -- quoting --------------------------------------------------------------
    async def on_tick(self) -> None:
        # Absorb fills BEFORE pricing (the whole point of fill anchoring).
        for ex in self.my_executors(active_only=True):
            await self.orchestrator.tick(ex.id)
        self._absorb_fills()

        mid = await self.adapter.mid_price(self.trading_pair)
        if mid <= 0:
            return

        base_value = self._base_value(mid)

        exposure = self.exposure_allowed_sides(self.trading_pair, mid)
        allow_buy = exposure["buy"]
        allow_sell = exposure["sell"]

        await self.evaluate_quote_gate(self.trading_pair)
        if self.gate_paused:
            allow_buy = allow_buy and base_value < 0
            allow_sell = allow_sell and base_value > 0

        if self.auto_spread and self.gate_atr_pct > 0:
            half = _dec(str(self.gate_atr_pct)) * self.auto_spread_k / Decimal(2)
            half = max(self.spread_floor_half_pct, min(half, self.spread_cap_half_pct))
            self.spread_bid_pct = half
            self.spread_ask_pct = half

        ref = self._current_reference(mid)
        self._last_ref = ref
        drift = abs(mid - ref) / ref if ref > 0 else Decimal(0)
        soft_reset = drift > self.reset_threshold_pct
        self._soft_reset_active = bool(soft_reset)

        # Directional bias, applied through the SAME skew Mid uses. Grid inherits
        # Mid's quoting machinery, so the signal overlay's bias (and the user's
        # own setting) must steer this book too — previously both were computed
        # and then silently dropped here. bias=0 is symmetric, i.e. unchanged.
        spread_bid_pct, spread_ask_pct = self.effective_spreads()

        if soft_reset:
            # SOFT RESET (drift escaped the band): re-anchor BOTH legs to mid so
            # neither is stranded across it, then DROP the no-cross clamp on the
            # BEHIND (under-filled) leg only — so it can concede and rebalance even
            # below cost (the documented "switch the behind leg to mid-market"
            # circuit-breaker). The profit/flat leg keeps no-cross, so it never
            # adds at a loss. net long ⇒ SELL behind; net short ⇒ BUY behind.
            target_bid = mid * (Decimal(1) - spread_bid_pct)
            target_ask = mid * (Decimal(1) + spread_ask_pct)
            sell_behind = base_value > 0
            buy_behind = base_value < 0
            if not buy_behind and self._last_sell_px is not None:
                target_bid = min(target_bid, self._last_sell_px * (Decimal(1) - spread_bid_pct))
            if not sell_behind and self._last_buy_px is not None:
                target_ask = max(target_ask, self._last_buy_px * (Decimal(1) + spread_ask_pct))
        else:
            # Normal grid: both legs at the last-fill reference with the full
            # no-cross invariant — never buy above the last sell, never sell below
            # the last buy (every round trip captures spread).
            target_bid = ref * (Decimal(1) - spread_bid_pct)
            target_ask = ref * (Decimal(1) + spread_ask_pct)
            if self._last_sell_px is not None:
                target_bid = min(target_bid, self._last_sell_px * (Decimal(1) - spread_bid_pct))
            if self._last_buy_px is not None:
                target_ask = max(target_ask, self._last_buy_px * (Decimal(1) + spread_ask_pct))

        # Forward the same mark used for exposure decisions. The inherited
        # reconciler projects each quote against the exposure cap. With
        # ladder_levels > 1 this rests N levels per side stepping away from the
        # anchor (scaling in/out) instead of the single order that used to sit
        # there until it filled; the per-side total is unchanged either way.
        await self._quote_side(TradeType.BUY, target_bid, allow_buy, mid)
        await self._quote_side(TradeType.SELL, target_ask, allow_sell, mid)

        # Step 2 of the stall escalation: if the soft-reset's maker concession
        # can't rebalance, escalate to a bounded reduce-only taker before SL.
        await self._maybe_escalate_concession(mid)

    # Introspection for dashboards/tests.
    def anchor_state(self) -> dict:
        return {
            "mode": self.mode,
            "reference": self._reference,
            "last_buy_px": self._last_buy_px,
            "last_sell_px": self._last_sell_px,
        }

    def grid_metrics(self) -> dict:
        """Soft-reset telemetry for the /status + order-monitor card, incl. the
        green/red price LEVELS where the reset fires (docs' green/red numbers):
        when over-short, price must RISE to the green level; when over-long, it
        must FALL to the red level."""
        ref = self._last_ref or self._reference
        up_price = down_price = 0.0
        if ref and ref > 0 and self.reset_threshold_pct > 0:
            up_price = float(ref * (Decimal(1) + self.reset_threshold_pct))
            down_price = float(ref * (Decimal(1) - self.reset_threshold_pct))
        net_base = 0.0
        if self.inventory is not None:
            net_base = float(self.inventory.get(self.user_id, self.trading_pair, self.id).net_amount_base)
        return {
            "grid_mode": self.mode,
            "grid_anchor_price": float(ref) if ref else 0.0,
            "grid_reset_threshold_bp": float(self.reset_threshold_pct * Decimal(10000)),
            "grid_reset_active": bool(self.reset_threshold_pct > 0),
            "grid_soft_reset_engaged": bool(self._soft_reset_active),
            "grid_reset_up_price": up_price,      # green: over-short → rise to trigger
            "grid_reset_down_price": down_price,   # red: over-long → fall to trigger
            "grid_net_base": net_base,             # >0 long (watch red), <0 short (watch green)
        }
