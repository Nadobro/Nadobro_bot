"""Market Making controller — symmetric maker quoting around mid with
inventory skew and profit protection. Replaces the legacy MM / Mid Mode.

Each tick: read mid, compute target bid/ask. If a resting quote is within
``price_distance_tolerance`` of the new target it is left alone; otherwise it
is cancelled and a fresh ``OrderExecutor(LIMIT_MAKER)`` is placed. Inventory
gating: above ``max_base_quote`` stop buying; with no base stop selling.
``profit_protection``: at max inventory with negative unrealized PnL, suspend.

Per-product daily-loss / drawdown / cost gating is enforced upstream by the
Risk Engine via the orchestrator's pre-tick check.

Implemented in Phase 4.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from src.nadobro.engine.controllers.controller_base import Controller
from src.nadobro.engine.executors.order_executor import OrderExecutor, OrderExecutorConfig
from src.nadobro.engine.risk import ExecutorRequest
from src.nadobro.engine.types import ExecutionStrategy, TradeType, _dec
from src.nadobro.quant import alpha as _alpha
from src.nadobro.quant import microstructure as _ms
from src.nadobro.quant import mm_profile as _mp
from src.nadobro.quant.ladder import (
    FLAT,
    LadderLevel,
    describe as _describe_ladder,
    plan_ladder,
    proximity_weights,
)

# Auto ladder spacing when ``ladder_step_bp`` is unset: one venue tick, floored
# so a very tight book (BTC-PERP's tick is ~0.16bp) doesn't stack every level
# on effectively the same price.
_AUTO_LADDER_STEP_FLOOR_BP = Decimal("1")

# Directional bias maps linearly to the documented alpha-tilt: ±1 bias → ±0.2.
# As a per-side spread skew this means a full long bias quotes the bid at 0.8×
# the spread (closer to mid → front-loads buys) and the ask at 1.2× (further →
# back-loads sells); short bias is the mirror. Bounded so neither factor goes
# non-positive for bias in [-1, 1].
_BIAS_SKEW_STRENGTH = Decimal("0.2")

logger = logging.getLogger(__name__)


def _safe_levels(value: object) -> int:
    """Parse a ladder level count. Any unusable value means one level, i.e. the
    single-quote behaviour that shipped before the ladder existed."""
    try:
        return max(1, int(_dec(value)))
    except Exception:  # noqa: BLE001  # policy: degrade-ok(garbage -> single quote)
        return 1


def _safe_bias(value: object) -> Decimal:
    """Parse directional_bias, clamped to [-1, 1]. Tolerates the legacy text
    default ("neutral") and any unparseable value by treating it as 0 (neutral)."""
    try:
        b = _dec(value)
    except Exception:  # noqa: BLE001 - "neutral"/None/garbage → neutral
        return Decimal(0)
    return max(Decimal(-1), min(Decimal(1), b))


@dataclass
class _QuoteSlot:
    """One resting quote at one ladder level.

    ``ex_id is None`` means the slot is empty; slots are kept (not deleted) so
    the level-0 compatibility properties always have somewhere to write.
    """
    ex_id: Optional[str] = None
    price: Optional[Decimal] = None
    size_quote: Decimal = Decimal(0)
    placed_at: float = 0.0


class MarketMakingController(Controller):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(name=kwargs.pop("name", "market_making"), **kwargs)  # type: ignore[arg-type]
        self.trading_pair = str(self.cfg("trading_pair"))
        self.spread_bid_pct = _dec(self.cfg("spread_bid_pct", "0.001"))
        self.spread_ask_pct = _dec(self.cfg("spread_ask_pct", "0.001"))
        self.order_amount_quote = _dec(self.cfg("order_amount_quote", "10"))
        self.price_distance_tolerance = _dec(self.cfg("price_distance_tolerance", "0.0005"))
        _mb = self.cfg("max_base_quote")
        self.max_base_quote = _dec(_mb) if _mb is not None else None
        _nb = self.cfg("min_base_quote")
        self.min_base_quote = _dec(_nb) if _nb is not None else None
        self.profit_protection = bool(self.cfg("profit_protection", False))
        # ATR auto-spread (Phase 3): when enabled, the per-side spread tracks
        # k x ATR / 2, clamped to [floor, cap]. The floor must clear fees +
        # adverse selection — quoting below it pays to trade.
        self.auto_spread = bool(self.cfg("auto_spread", False))
        self.auto_spread_k = _dec(self.cfg("auto_spread_k", "1.5"))
        self.spread_floor_half_pct = _dec(self.cfg("spread_floor_half_pct", "0.00015"))
        self.spread_cap_half_pct = _dec(self.cfg("spread_cap_half_pct", "0.005"))
        # MM-SPREAD-FLOOR fix: the auto-spread path clamps each side to
        # spread_floor_half_pct (the fee-clearing minimum), but a MANUAL spread
        # was applied verbatim — a user could quote a sub-fee book that loses on
        # every fill. Floor the manual per-side spread at the same minimum.
        self.spread_bid_pct = max(self.spread_bid_pct, self.spread_floor_half_pct)
        self.spread_ask_pct = max(self.spread_ask_pct, self.spread_floor_half_pct)
        # Directional bias (Mid Mode): lean the book long (>0) / short (<0) by
        # skewing the per-side spreads. 0 = symmetric (default). Previously the
        # user's directional_bias setting only changed the preview math and was
        # never applied to live quoting — this wires it into the controller.
        self.directional_bias = _safe_bias(self.cfg("directional_bias", "0"))
        # Quote mode (Turbo Volume): "mid" (default) prices mid ± spread as
        # always; "touch" joins the best bid/ask (improving by one tick when
        # the spread leaves room) — the same maker geometry as volume_bot v3.
        # POLICY (2026-07-15): this controller is MAKER-ONLY — every order is
        # post-only; no taker leg exists (fees + Nado wash-trading policy; the
        # full rationale lives in docs/mm_volume_tuning.md).
        self.quote_mode = str(self.cfg("quote_mode", "mid") or "mid").lower()
        # --- Phase 2: position scaling ladder -----------------------------
        # ``order_amount_quote`` is the notional deployed on ONE SIDE. With
        # ladder_levels=1 that is a single order (the shipped behaviour, bit for
        # bit); with N it is split across N levels stepping AWAY from the target
        # so the book scales into an adverse move and back out of it. The total
        # per side is unchanged either way, which is what bounds the geometric
        # curve — laddering redistributes deployment, it never adds to it.
        self.ladder_levels = _safe_levels(self.cfg("ladder_levels", 1))
        self.ladder_step_bp = _dec(self.cfg("ladder_step_bp", "0") or "0")
        self.ladder_curve = str(self.cfg("ladder_curve", FLAT) or FLAT).lower()
        # --- Phase 0: queue preservation ----------------------------------
        # On a one-tick book a quote cannot be improved, only queued — so queue
        # position IS the fill rate and every cancel forfeits it. Minimum time a
        # quote must rest before a mere price change may cancel it (0 = off).
        # Safety cancels (inventory/exposure/gate) are never delayed by this.
        self.min_quote_lifetime_s = float(_dec(self.cfg("min_quote_lifetime_s", "0") or "0"))
        self._touch_bid: Optional[Decimal] = None
        self._touch_ask: Optional[Decimal] = None
        self._slots: Dict[Tuple[bool, int], _QuoteSlot] = {}
        self._cap_floor_warned = False
        self._last_plan_desc: str = ""
        # --- Mid Mode v3 Phase 2: microstructure telemetry ------------------
        # OBSERVATION ONLY. Reads the SIZED book (``depth_book``) once per tick
        # and records microprice / imbalance / spread. It does NOT price a
        # quote, move a target, or change a requote decision — that lands in a
        # later, separately reviewed phase.
        #
        # Default OFF so the two subclasses that inherit this controller
        # (FillAnchoredQuotingController, RGridController) are bit-for-bit
        # unchanged; only the ``mid`` mapping switches it on.
        #
        # Why it needs its own read: ``order_book`` fabricates levels with
        # amount=0, so the controller has never seen size. ``depth_book`` is the
        # sized ladder at the same weight-1 query cost, TTL-cached at the
        # cadence floor so concurrent users on a product share one fetch.
        self.microstructure_log = bool(_dec(self.cfg("microstructure_log", "0") or "0"))
        self.micro: Dict[str, object] = {}
        self._micro_last_hash: str = ""
        # --- Mid Mode v3 Phase 5: objective profile, fee floor, reservation ---
        # THE FIRST PHASE THAT ACTUATES. Everything here is off unless the
        # ``mid`` mapping switches it on, because FillAnchoredQuotingController
        # and RGridController inherit this class and must stay bit-for-bit
        # identical — a default-on flag here silently re-prices Grid and R-Grid.
        self.profile_enabled = bool(_dec(self.cfg("profile_enabled", "0") or "0"))
        self.mid_objective = _mp.normalize_objective(self.cfg("mid_objective", ""))
        # Round-trip maker+builder fee in bp. 0 => the pure module's default.
        self.fee_round_trip_bp = _dec(self.cfg("fee_round_trip_bp", "0") or "0")
        self.min_edge_bp = _dec(self.cfg("min_edge_bp", "1") or "1")
        self.inventory_skew_enabled = bool(
            _dec(self.cfg("inventory_skew_enabled", "0") or "0")
        )
        self.inventory_skew_gamma = _dec(self.cfg("inventory_skew_gamma", "0.1") or "0.1")
        # Resolved ONCE per session (see _resolve_profile_once): the two
        # profiles imply different requote and inventory behaviour, so flipping
        # per tick would churn the book instead of running either playbook.
        self.profile: str = ""
        self._profile_resolved = False
        self.reservation_offset_bp: Decimal = Decimal(0)
        # --- Mid Mode v3 Phase 6: alpha, mark-out defence, STP, failover -----
        # Same gating rule as Phase 5: off unless the ``mid`` mapping says so.
        self.alpha_enabled = bool(_dec(self.cfg("alpha_enabled", "0") or "0"))
        self.alpha_max = _dec(self.cfg("alpha_max", "0.35") or "0.35")
        self.alpha_strength = _dec(self.cfg("alpha_strength", "0.5") or "0.5")
        # Injected async callable ``(pair) -> {"components": {...},
        # "trusted": bool} | None``. None (or a None return) means the signal
        # feed is unavailable, which is a DEGRADED MODE, not an outage: the
        # controller keeps quoting off Nado's own anchor, just wider and
        # shallower. Injected in engine_runtime beside candle_provider, because
        # engine/ has no module-level edge to market_data/.
        self.signal_provider = self.cfg("signal_provider")
        # Injected sync callable ``() -> float`` in [1, 3]: how much measured
        # adverse selection says to widen. 1.0 = no evidence of harm.
        self.markout_provider = self.cfg("markout_provider")
        self.degraded_spread_mult = _dec(self.cfg("degraded_spread_mult", "1.25") or "1.25")
        self.self_trade_prevention = bool(
            _dec(self.cfg("self_trade_prevention", "0") or "0")
        )
        self.alpha: float = 0.0
        self.alpha_confidence: float = 0.0
        self.alpha_detail: Dict[str, object] = {}
        self.alpha_offset_bp: Decimal = Decimal(0)
        self.signal_degraded = False
        self.markout_widen: Decimal = Decimal(1)
        self._stp_blocks = 0
        # --- Mid Mode v3 Phase 7: support/resistance ladder shaping ----------
        # Injected async callable ``() -> {"support": [...], "resistance": [...]}``,
        # TTL-cached on the other side. Off unless the mid mapping enables it.
        self.levels_provider = self.cfg("levels_provider")
        self.level_weights_enabled = bool(
            _dec(self.cfg("level_weights_enabled", "0") or "0")
        )
        self.level_tolerance_bp = _dec(self.cfg("level_tolerance_bp", "25") or "25")
        self.level_boost = _dec(self.cfg("level_boost", "1.5") or "1.5")
        self._sr_levels: Dict[str, object] = {}

    # -- level-0 compatibility -------------------------------------------------
    # The ladder generalises what used to be two scalar pairs. Level 0 keeps the
    # original attribute names so live-config resets, dashboards and tests that
    # read/assign them keep working unchanged.
    def _slot(self, is_bid: bool, level: int) -> _QuoteSlot:
        key = (bool(is_bid), int(level))
        slot = self._slots.get(key)
        if slot is None:
            slot = _QuoteSlot()
            self._slots[key] = slot
        return slot

    @property
    def _bid_id(self) -> Optional[str]:
        return self._slot(True, 0).ex_id

    @_bid_id.setter
    def _bid_id(self, value: Optional[str]) -> None:
        self._slot(True, 0).ex_id = value

    @property
    def _ask_id(self) -> Optional[str]:
        return self._slot(False, 0).ex_id

    @_ask_id.setter
    def _ask_id(self, value: Optional[str]) -> None:
        self._slot(False, 0).ex_id = value

    @property
    def _bid_price(self) -> Optional[Decimal]:
        return self._slot(True, 0).price

    @_bid_price.setter
    def _bid_price(self, value: Optional[Decimal]) -> None:
        self._slot(True, 0).price = value

    @property
    def _ask_price(self) -> Optional[Decimal]:
        return self._slot(False, 0).price

    @_ask_price.setter
    def _ask_price(self, value: Optional[Decimal]) -> None:
        self._slot(False, 0).price = value

    def _now(self) -> float:
        """Monotonic clock, isolated so tests can drive quote ages."""
        return time.monotonic()

    def live_quote_ids(self) -> List[str]:
        """Every executor id this controller currently believes is resting."""
        return [s.ex_id for s in self._slots.values() if s.ex_id is not None]

    async def stop_all_quotes(self) -> None:
        """Cancel and forget every resting quote across ALL ladder levels.

        Used by the live-config path when sizing changes: with a ladder, walking
        only the level-0 attributes would strand levels 1..N as orphan orders.
        """
        for slot in list(self._slots.values()):
            if slot.ex_id is not None:
                await self.orchestrator.stop(slot.ex_id)
            slot.ex_id = None
            slot.price = None

    async def on_start(self) -> None:
        return None

    def _base_value(self, mid: Decimal) -> Decimal:
        if self.inventory is None:
            return Decimal(0)
        hold = self.inventory.get(self.user_id, self.trading_pair, self.id)
        return hold.net_amount_base * mid

    async def _touch_targets(self) -> Optional[tuple[Decimal, Decimal, Decimal]]:
        """(target_bid, target_ask, book_mid) glued to the touch, or None when
        the book has no live two-sided touch (dead/one-sided book -> caller
        falls back to mid ± spread pricing). Same join/improve geometry as
        volume_bot v3: join the best bid/ask, improve by one tick when the
        spread leaves at least two ticks of room (price-time priority puts the
        improver first). ``book_mid`` rides along so touch mode needs ONE
        market-data call per tick — mid_price() is itself an order_book fetch,
        and fetching both doubled the per-tick hit on the shared IP budget."""
        self._touch_bid = self._touch_ask = None
        try:
            book = await self.adapter.order_book(self.trading_pair)
            bid, ask = book.best_bid, book.best_ask
        except Exception:  # noqa: BLE001 - a dead feed falls back to mid pricing
            return None
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        # AUDIT-MM-2026-07-14 #7: a degraded feed substitutes bid = ask = mid
        # (and a crossed venue book inverts them) — neither is a real touch to
        # join. Fall back to mid ± spread pricing instead of quoting AT mid.
        if bid >= ask:
            return None
        try:
            tick = self.adapter.tick_size(self.trading_pair)
        except Exception:  # noqa: BLE001
            tick = Decimal(0)
        target_bid, target_ask = bid, ask
        if tick and tick > 0 and (ask - bid) >= tick * 2:
            target_bid = bid + tick
            target_ask = ask - tick
        if target_bid >= target_ask:  # one-tick book after improve — join only
            target_bid, target_ask = bid, ask
        # RAW touch (pre-improve) drives the Phase-0 queue-priority hold: a
        # resting quote at or better than this is already at the front of the
        # best price on its side, so re-placing it can only cost queue position.
        self._touch_bid, self._touch_ask = bid, ask
        return target_bid, target_ask, (bid + ask) / Decimal(2)

    def effective_spreads(self) -> Tuple[Decimal, Decimal]:
        """Per-side spreads after the directional-bias skew.

        The favoured side quotes closer to the reference (fills more) and the
        other side further (fills less), accumulating the desired inventory
        lean. Floored so neither side quotes through the fee-clearing minimum;
        bias=0 leaves the quotes symmetric.

        Shared with the fill-anchored subclass: Grid inherits this controller's
        quoting machinery, so the signal overlay's bias must steer it the same
        way it steers Mid rather than being silently dropped.
        """
        if self.directional_bias == 0:
            return self.spread_bid_pct, self.spread_ask_pct
        skew = self.directional_bias * _BIAS_SKEW_STRENGTH
        return (
            max(self.spread_floor_half_pct, self.spread_bid_pct * (Decimal(1) - skew)),
            max(self.spread_floor_half_pct, self.spread_ask_pct * (Decimal(1) + skew)),
        )

    # -- Phase 5: objective profile ------------------------------------------
    async def _observed_spread_bp(self) -> Optional[float]:
        """Live spread in bp, cheapest source first.

        Touch mode already fetched the book this tick, so reuse it. Otherwise
        this costs ONE weight-1 depth read — and only ever once per session,
        because the profile is resolved once.
        """
        if self._touch_bid and self._touch_ask and self._touch_bid > 0:
            mid = (self._touch_bid + self._touch_ask) / Decimal(2)
            if mid > 0:
                return float((self._touch_ask - self._touch_bid) / mid) * 10_000.0
        try:
            snap = await self.adapter.depth_book(self.trading_pair)
            book = {
                "bids": [[float(l.price), float(l.amount)] for l in snap.bids],
                "asks": [[float(l.price), float(l.amount)] for l in snap.asks],
            }
        except Exception:  # noqa: BLE001 - an unreadable book resolves to VOLUME
            return None
        return _ms.spread_bp(book)

    async def _resolve_profile_once(self) -> None:
        """Pick VOLUME or SPREAD for this session and apply the fee floor.

        Resolved once and then left alone. The SPREAD profile raises the
        half-spread floor to ``δ* = f + edge`` so a quote can never rest inside
        the fee; VOLUME deliberately keeps the shipped floor, because quoting
        inside the fee to buy fill rate is that profile's entire purpose and it
        is bounded by the session SL rail instead.
        """
        if not self.profile_enabled or self._profile_resolved:
            return
        spread_bp = await self._observed_spread_bp()
        if spread_bp is None and self.mid_objective == _mp.AUTO:
            # No reading and no explicit choice: try again next tick rather than
            # committing the session to a playbook picked from nothing.
            return
        self._profile_resolved = True
        self.profile = _mp.resolve_profile(
            self.mid_objective,
            spread_bp=spread_bp,
            fee_round_trip_bp=float(self.fee_round_trip_bp) or None,
        )
        if self.profile == _mp.SPREAD:
            floor_bp = _mp.half_spread_floor_bp(
                fee_round_trip_bp=float(self.fee_round_trip_bp) or None,
                min_edge_bp=float(self.min_edge_bp),
            )
            floor = _dec(str(floor_bp)) / Decimal(10000)
            if floor > self.spread_floor_half_pct:
                self.spread_floor_half_pct = floor
                # Re-apply to the MANUAL spreads too: __init__ floored them at
                # the old value, and a user spread under the fee is exactly what
                # this profile exists to refuse.
                self.spread_bid_pct = max(self.spread_bid_pct, floor)
                self.spread_ask_pct = max(self.spread_ask_pct, floor)
        logger.info(
            "MM %s profile=%s (spread=%s bp, objective=%s, half-spread floor=%s bp)",
            self.trading_pair, self.profile,
            None if spread_bp is None else round(spread_bp, 2),
            self.mid_objective, float(self.spread_floor_half_pct) * 10_000.0,
        )

    def _inventory_ratio(self, mid: Decimal) -> Decimal:
        """Signed inventory as a fraction of the ceiling. +1 = fully long."""
        cap = self.max_base_quote if (self.max_base_quote or 0) > 0 else self.order_amount_quote
        if not cap or cap <= 0:
            return Decimal(0)
        return self._base_value(mid) / cap

    def _reservation_price(self, mid: Decimal) -> Decimal:
        """Quoting anchor after the inventory and alpha shifts.

        Both are displacements of the SAME quantity — the fair value the quotes
        are built around — so they compose additively and share one bound. That
        is also why neither touches ``directional_bias``: that field is a spread
        skew the user owns, and a second writer would break its dead-band.

        Returns ``mid`` unchanged when both are off, so the disabled path is
        bit-for-bit the shipped one.
        """
        self.reservation_offset_bp = Decimal(0)
        self.alpha_offset_bp = Decimal(0)
        if mid <= 0:
            return mid
        half_bp = float((self.spread_bid_pct + self.spread_ask_pct) / Decimal(2)) * 10_000.0
        total = 0.0
        if self.inventory_skew_enabled:
            inv = _mp.reservation_offset_bp(
                float(self._inventory_ratio(mid)),
                sigma_bp=self.gate_atr_pct * 10_000.0,
                half_spread_bp=half_bp,
                gamma=float(self.inventory_skew_gamma),
            )
            self.reservation_offset_bp = _dec(str(inv))
            total += inv
        if self.alpha_enabled and self.alpha:
            a_off = _alpha.anchor_offset_bp(
                self.alpha, half_spread_bp=half_bp,
                strength=float(self.alpha_strength),
            )
            self.alpha_offset_bp = _dec(str(a_off))
            total += a_off
        if not total:
            return mid
        # ONE joint bound: each part is individually capped at half the
        # half-spread, so their sum could still reach a full half-spread and
        # push a quote onto the wrong side of the anchor. Cap the total too.
        cap = half_bp * 0.5
        total = max(-cap, min(cap, total))
        return mid * (Decimal(1) + _dec(str(total)) / Decimal(10000))

    # -- Phase 6: signal refresh, degraded mode, mark-out defence -------------
    async def _refresh_alpha(self, mid: Decimal) -> None:
        """Pull the forecast components and blend them. Never raises.

        Three outcomes, and the middle one matters:

        * no payload — the feed should have this market and is not answering.
          DEGRADED: lean on nothing, quote wider and shallower. Still quoting,
          off Nado's own book, which is strictly better than the blind
          mid-quoting Mid shipped with.
        * ``supported: False`` — Hyperliquid does not list this market at all
          (every equity/RWA). Permanent and expected, so alpha is simply 0 and
          nothing is widened; treating it as degradation would quote those
          markets wide forever waiting for a feed that is never coming.
        * components — blend them.
        """
        if not self.alpha_enabled:
            return
        payload = None
        provider = self.signal_provider
        if callable(provider):
            try:
                payload = await provider(self.trading_pair, mid)
            except Exception:  # noqa: BLE001 - a dead feed must not stop quoting
                logger.debug("signal provider failed %s", self.trading_pair, exc_info=True)
                payload = None
        if isinstance(payload, dict) and payload.get("supported") is False:
            self.signal_degraded = False
            self.alpha = 0.0
            self.alpha_confidence = 0.0
            self.alpha_detail = {"unsupported": True}
            return
        if not isinstance(payload, dict) or not payload.get("components"):
            self.signal_degraded = True
            self.alpha = 0.0
            self.alpha_confidence = 0.0
            self.alpha_detail = {}
            return
        self.signal_degraded = False
        out = _alpha.blend(
            payload.get("components") or {},
            weights=payload.get("weights"),
            trusted=bool(payload.get("trusted")),
            max_alpha=float(self.alpha_max),
        )
        self.alpha = float(out.get("alpha") or 0.0)
        self.alpha_confidence = float(out.get("confidence") or 0.0)
        self.alpha_detail = out

    async def _refresh_markout_widen(self) -> None:
        """How much measured adverse selection says to widen. 1.0 = no harm.

        The provider is async and TTL-cached on the other side; it must never
        block the tick on a query.
        """
        self.markout_widen = Decimal(1)
        if not self.alpha_enabled or not callable(self.markout_provider):
            return
        half_bp = float((self.spread_bid_pct + self.spread_ask_pct) / Decimal(2)) * 10_000.0
        try:
            factor = float(await self.markout_provider(half_bp))
        except Exception:  # noqa: BLE001 - a grading outage must not move quotes
            logger.debug("markout provider failed %s", self.trading_pair, exc_info=True)
            return
        # Never below 1: a mark-out series is evidence of harm, and never
        # evidence that quoting TIGHTER is safe.
        self.markout_widen = _dec(str(max(1.0, min(3.0, factor))))

    async def _refresh_levels(self) -> None:
        """Pull cached support/resistance. Never blocks, never raises."""
        if not self.level_weights_enabled or not callable(self.levels_provider):
            return
        try:
            levels = await self.levels_provider()
        except Exception:  # noqa: BLE001 - no levels just leaves the shape alone
            logger.debug("levels provider failed %s", self.trading_pair, exc_info=True)
            return
        if isinstance(levels, dict):
            self._sr_levels = levels

    def _defensive_spread_mult(self) -> Decimal:
        """Combined widening from the mark-out ledger and feed degradation."""
        mult = self.markout_widen
        if self.alpha_enabled and self.signal_degraded:
            mult *= self.degraded_spread_mult
        return mult

    def _unrealized(self, mid: Decimal) -> Decimal:
        if self.inventory is None:
            return Decimal(0)
        return self.inventory.get(self.user_id, self.trading_pair, self.id).unrealized_pnl(mid)

    async def on_tick(self) -> None:
        # BUG-MM-1 fix: tick all child OrderExecutors FIRST so fills are
        # absorbed into inventory before we read base_value for the
        # inventory-skew decision. Without this, the MM controller is blind
        # to its own fills and keeps stale quotes indefinitely.
        for ex in self.my_executors(active_only=True):
            await self.orchestrator.tick(ex.id)

        # ONE market-data call per tick: in touch mode the order-book snapshot
        # provides both the touch targets and the mid (mid_price() is itself an
        # order_book fetch — calling both doubled the per-tick hit on the
        # shared per-IP query budget). Dead/degraded book -> classic mid fetch.
        #
        # ONE EXCEPTION, and it is opt-in: with ``microstructure_log`` on (mid
        # only, off by default) ``_record_microstructure`` adds a second
        # weight-1 read for the SIZED book, which ``order_book`` cannot give —
        # it fabricates levels with amount=0. It is TTL-cached at the cadence
        # floor so concurrent users on a product share one fetch. Folding the
        # two into a single depth snapshot is the pricing phase's job; until
        # then this comment states what the code actually does.
        self._touch_bid = self._touch_ask = None
        touch = await self._touch_targets() if self.quote_mode == "touch" else None
        mid = touch[2] if touch is not None else await self.adapter.mid_price(self.trading_pair)
        # Phase 5: pick the playbook for this market. Once per session, and it
        # can raise the half-spread floor before any spread is computed below.
        await self._resolve_profile_once()
        # Phase 6: refresh the forecast and the defensive widening. Both fail
        # OPEN — no signal means alpha 0 and a wider quote, never a skipped tick.
        await self._refresh_alpha(mid)
        await self._refresh_markout_widen()
        await self._refresh_levels()
        base_value = self._base_value(mid)
        at_max = self.max_base_quote is not None and base_value >= self.max_base_quote
        at_min = self.min_base_quote is not None and base_value <= self.min_base_quote
        allow_buy = not at_max          # stop buying above the inventory ceiling
        allow_sell = not at_min         # stop selling below the inventory floor
        if self.profit_protection and at_max and self._unrealized(mid) < 0:
            allow_buy = allow_sell = False

        # Inventory cap (Phase 1): margin-relative net-exposure backstop with
        # hysteresis — suppress the side that worsens exposure, keep the
        # reducing side quoting so the book can trim back toward neutral.
        exposure = self.exposure_allowed_sides(self.trading_pair, mid)
        allow_buy = allow_buy and exposure["buy"]
        allow_sell = allow_sell and exposure["sell"]

        # Regime gate (Phase 2): in PAUSE, place no NEW exposure — quote only
        # the side that reduces the current net position (the exit path); a
        # flat book quotes nothing until the regime reads ranging again.
        await self.evaluate_quote_gate(self.trading_pair)
        if self.gate_paused:
            net = base_value
            allow_buy = allow_buy and net < 0    # buying only reduces a short
            allow_sell = allow_sell and net > 0  # selling only reduces a long

        # ATR auto-spread (Phase 3): scale the quoted spread with realized
        # volatility so captured edge stays ahead of fees as conditions move.
        if self.auto_spread and self.gate_atr_pct > 0:
            half = _dec(str(self.gate_atr_pct)) * self.auto_spread_k / Decimal(2)
            half = max(self.spread_floor_half_pct, min(half, self.spread_cap_half_pct))
            self.spread_bid_pct = half
            self.spread_ask_pct = half

        eff_bid_pct, eff_ask_pct = self.effective_spreads()
        # Phase 6: widen for measured adverse selection and for a degraded
        # signal feed. Widening only — the mark-out ledger can prove harm, it
        # can never prove that quoting tighter is safe.
        defensive = self._defensive_spread_mult()
        if defensive != 1:
            eff_bid_pct *= defensive
            eff_ask_pct *= defensive

        # Phase 5: quote around the RESERVATION price, not the raw mid. Long
        # inventory shifts the anchor down so the ask works the position off;
        # short mirrors it. Bounded to a fraction of the half-spread, so the
        # two sides can never cross and neither can breach the fee floor.
        # Kept out of ``directional_bias`` on purpose — that field is the
        # user's, and two writers to one field is how dead-bands stop working.
        theta = self._reservation_price(mid)
        target_bid = theta * (Decimal(1) - eff_bid_pct)
        target_ask = theta * (Decimal(1) + eff_ask_pct)
        # Touch mode (Turbo Volume): glue quotes to the live touch instead of
        # mid ± spread (targets computed once at the top of the tick).
        # Bias/auto-spread math above still ran — it provides the fallback
        # targets when the book has no two-sided touch. The fee-floor spread
        # does NOT apply here by design: volume mode deliberately trades
        # per-fill edge for fill rate, bounded by the session SL rail.
        if touch is not None:
            target_bid, target_ask = touch[0], touch[1]
        # Observation only — must run AFTER targets are fixed so it cannot
        # influence them, and never raise into the quoting path.
        await self._record_microstructure(mid, target_bid, target_ask)
        await self._quote_side(TradeType.BUY, target_bid, allow_buy, mid)
        await self._quote_side(TradeType.SELL, target_ask, allow_sell, mid)

    async def _record_microstructure(
        self, mid: Decimal, target_bid: Decimal, target_ask: Decimal
    ) -> None:
        """Read the sized book and record the microstructure view. Pure
        telemetry: it changes nothing about this tick's quotes.

        The number to watch is ``micro_vs_mid_bp`` — how far the size-weighted
        fair value sits from the arithmetic mid we currently quote around. If
        that is persistently non-zero and signed with our fills, the mid is the
        wrong reference and the later pricing phase has its evidence.
        """
        if not self.microstructure_log:
            return
        try:
            snap = await self.adapter.depth_book(self.trading_pair)
            book = {
                "bids": [[float(l.price), float(l.amount)] for l in snap.bids],
                "asks": [[float(l.price), float(l.amount)] for l in snap.asks],
            }
            micro = _ms.microprice(book)
            mid_f = float(mid) if mid else 0.0
            self.micro = {
                "microprice": micro,
                "book_mid": _ms.mid(book),
                "spread_bp": _ms.spread_bp(book),
                "obi": _ms.obi_bands(book),
                "bid_depth_20bp": _ms.depth_notional(book, _ms.BUY, bp=20),
                "ask_depth_20bp": _ms.depth_notional(book, _ms.SELL, bp=20),
                "levels": (len(snap.bids), len(snap.asks)),
                # Displacement of size-weighted fair value from the quoted mid.
                "micro_vs_mid_bp": (
                    (micro - mid_f) / mid_f * 10_000.0
                    if micro is not None and mid_f > 0 else None
                ),
                "target_bid": float(target_bid),
                "target_ask": float(target_ask),
            }
            # Log on change only: a repeated hash means a frozen feed, and a
            # per-tick line on a 3s cadence would drown the log.
            digest = _ms.book_hash(book)
            if digest != self._micro_last_hash:
                self._micro_last_hash = digest
                logger.info(
                    "microstructure %s micro_vs_mid_bp=%s spread_bp=%s obi=%s depth20=(%.0f/%.0f)",
                    self.trading_pair,
                    self.micro.get("micro_vs_mid_bp"),
                    self.micro.get("spread_bp"),
                    self.micro.get("obi"),
                    self.micro.get("bid_depth_20bp") or 0.0,
                    self.micro.get("ask_depth_20bp") or 0.0,
                )
        except Exception:  # noqa: BLE001 - telemetry must never break quoting
            logger.debug("microstructure read failed %s", self.trading_pair, exc_info=True)

    # -- ladder (Phase 2) ------------------------------------------------------
    def _effective_step_bp(self, mid: Decimal) -> Decimal:
        """Spacing between adjacent ladder levels. Unset ⇒ one venue tick,
        floored so a sub-bp tick doesn't collapse the ladder onto one price."""
        if self.ladder_step_bp > 0:
            return self.ladder_step_bp
        try:
            tick = self.adapter.tick_size(self.trading_pair)
        except Exception:  # noqa: BLE001  # policy: degrade-ok(no tick meta -> bp floor)
            tick = Decimal(0)
        tick_bp = (tick / mid) * Decimal(10000) if (tick > 0 and mid > 0) else Decimal(0)
        return max(tick_bp, _AUTO_LADDER_STEP_FLOOR_BP)

    def _effective_levels(self) -> int:
        """Ladder depth for this tick.

        A degraded signal feed shortens the ladder by one level: the deep
        levels are the ones that fill when the market runs, and running blind
        is exactly when being deep is expensive. Never below one, and the
        deployment per side is unchanged either way — ``plan_ladder``
        redistributes, it never adds.
        """
        if self.alpha_enabled and self.signal_degraded and self.ladder_levels > 1:
            return self.ladder_levels - 1
        return self.ladder_levels

    def _side_level_weights(
        self, mid: Decimal, base_target: Decimal, is_bid: bool
    ) -> Optional[List[Decimal]]:
        """Phase 7: bias rung SIZE toward support (bids) / resistance (asks).

        Reshaping only — ``plan_ladder`` divides by the weight total, so the
        per-side deployment is identical. The asymmetry is the point: weighting
        a bid toward a resistance level would put size exactly where sellers
        are waiting.
        """
        if not self.level_weights_enabled or base_target <= 0 or mid <= 0:
            return None
        raw = (self._sr_levels or {}).get("support" if is_bid else "resistance")
        if not isinstance(raw, (list, tuple)):
            return None
        # The payload is injected, so validate it here rather than trusting it:
        # a NaN or a negative level would silently reshape the whole ladder.
        refs: List[Decimal] = []
        for value in raw:
            try:
                level = _dec(value)
            except Exception:  # policy: degrade-ok(junk level -> rung keeps its curve weight)
                continue
            if level > 0:
                refs.append(level)
        if not refs:
            return None
        step = self._effective_step_bp(mid)
        prices = [
            self._level_price(base_target, step * Decimal(i), is_bid)
            for i in range(self._effective_levels())
        ]
        return proximity_weights(
            prices, refs,
            tolerance_bp=self.level_tolerance_bp, boost=self.level_boost,
        )

    def _plan_side(
        self, mid: Decimal, *, base_target: Decimal = Decimal(0), is_bid: bool = True
    ) -> List[LadderLevel]:
        """Split this side's deployed notional into levels, clamped so every
        level clears the venue minimum (a sub-minimum level is simply rejected,
        which is how the ``levels`` input silently died the first time)."""
        try:
            min_notional = self.adapter.min_notional(self.trading_pair)
        except Exception:  # noqa: BLE001  # policy: degrade-ok(no product meta -> no clamp)
            min_notional = Decimal(0)
        return plan_ladder(
            self.order_amount_quote,
            levels=self._effective_levels(),
            step_bp=self._effective_step_bp(mid),
            first_offset_bp=0,
            curve=self.ladder_curve,
            min_notional=min_notional,
            level_weights=self._side_level_weights(mid, base_target, is_bid),
        )

    @staticmethod
    def _level_price(base_target: Decimal, offset_bp: Decimal, is_bid: bool) -> Decimal:
        """Step AWAY from the reference: bids deeper (lower), asks higher."""
        factor = offset_bp / Decimal(10000)
        return base_target * ((Decimal(1) - factor) if is_bid else (Decimal(1) + factor))

    async def _retire_levels_beyond(self, is_bid: bool, count: int) -> None:
        """Cancel slots the current plan no longer contains (the level count
        shrank — smaller deployment, or the min-notional clamp tightened)."""
        for (slot_is_bid, level), slot in list(self._slots.items()):
            if slot_is_bid is not is_bid or level < count or slot.ex_id is None:
                continue
            await self.orchestrator.stop(slot.ex_id)
            slot.ex_id = None
            slot.price = None

    async def _quote_side(
        self, side: TradeType, base_target: Decimal, allowed: bool, mid: Decimal
    ) -> None:
        """Reconcile the whole ladder on one side against ``base_target``."""
        is_bid = side is TradeType.BUY
        plan = (
            self._plan_side(mid, base_target=base_target, is_bid=is_bid)
            if base_target > 0 else []
        )
        if not plan:
            # Degenerate deployment/target: fall through to the single-quote path
            # so behaviour is exactly what it was before the ladder existed.
            await self._reconcile(side, base_target, allowed, mid)
            await self._retire_levels_beyond(is_bid, 1)
            return
        if len(plan) > 1:
            desc = f"{side.name} {_describe_ladder(plan)}"
            if desc != self._last_plan_desc:
                self._last_plan_desc = desc
                logger.debug("MM %s ladder: %s", self.trading_pair, desc)
        for lvl in plan:
            await self._reconcile(
                side,
                self._level_price(base_target, lvl.offset_bp, is_bid),
                allowed,
                mid,
                level=lvl.index,
                size_quote=lvl.size_quote,
            )
        await self._retire_levels_beyond(is_bid, len(plan))

    def _resting_side_notional(self, is_bid: bool, exclude_level: int) -> Decimal:
        """Notional already resting on this side, EXCLUDING the level being
        reconciled (that one is about to be replaced, so counting it would
        double-book it and deadlock the side into never re-quoting).

        A partially filled level is counted at full size while its executor is
        still live, so the filled part is briefly counted twice — deliberately
        conservative, it can only under-quote, never over-expose.
        """
        total = Decimal(0)
        for (slot_is_bid, level), slot in self._slots.items():
            if slot_is_bid is not is_bid or level == exclude_level or slot.ex_id is None:
                continue
            ex = self.orchestrator.get(slot.ex_id)
            if ex is None or ex.is_terminated:
                continue
            total += slot.size_quote
        return total

    def _projected_order_within_exposure(
        self, side: TradeType, mid: Decimal,
        order_quote: Optional[Decimal] = None, level: int = 0,
    ) -> bool:
        """Whether another full quote fits the margin-relative exposure cap.

        The existing gate observes filled inventory only. Turbo Mid quotes one
        full deployed notional per side, so inventory just below the cap after
        an adverse mark could otherwise admit a second full-size order and jump
        to almost 2x the promised limit. Reducing orders are always allowed,
        even when they cannot bring an already-oversized position below the cap
        in one fill.

        With a ladder the projection is per LEVEL plus whatever is already
        resting on that side, so N levels cannot each be waved through on the
        strength of the same headroom.
        """
        if self.inventory is None or mid <= 0:
            return True
        cap_pct = self.cfg("max_net_exposure_pct")
        margin = self.cfg("margin_quote")
        if cap_pct is None or margin is None:
            return True
        try:
            cap_quote = _dec(margin) * _dec(cap_pct) / Decimal(100)
        except Exception:  # noqa: BLE001 - malformed/unset cap keeps legacy behavior
            return True
        if cap_quote <= 0:
            return True
        # MID-FLAT-DEADLOCK (2026-07-30): plain Mid maps order_amount_quote ==
        # margin_quote (one full-size quote per side) while the default
        # net-exposure cap is 30% of margin — so a FLAT book projected one
        # order at ~3.3x the cap, both sides were refused on every tick, and
        # the strategy never placed a single order. A cap below one order size
        # can never admit the strategy's own first quote; floor it there. The
        # filled-inventory gate (exposure_allowed_sides) still enforces the
        # configured cap with reduce-only quoting after fills, and stacking a
        # SECOND worsening full-size order beyond the floored cap stays
        # blocked — the original purpose of this check.
        if cap_quote < self.order_amount_quote and not self._cap_floor_warned:
            # AUDIT-MID-2026-07-30 #2: the floor overrides an explicitly small
            # user cap pre-fill (enforcement becomes post-fill reduce-only via
            # exposure_allowed_sides). Surface that once so it is never silent.
            self._cap_floor_warned = True
            logger.warning(
                "MM %s: net-exposure cap $%s < one side deployment ($%s) — floored "
                "to the deployed size for quoting; the configured cap applies to "
                "FILLED inventory (reduce-only) instead",
                self.trading_pair, cap_quote, self.order_amount_quote,
            )
        # The floor stays ONE FULL SIDE DEPLOYMENT (== order_amount_quote, which
        # the ladder sums to exactly), not one level. Flooring at a single level
        # would admit L0 and refuse the rest, silently quoting a fraction of the
        # size the user deployed.
        cap_quote = max(cap_quote, self.order_amount_quote)
        current_quote = self._base_value(mid)
        pending = (
            self.order_amount_quote if order_quote is None else order_quote
        ) + self._resting_side_notional(side is TradeType.BUY, level)
        delta_quote = pending if side is TradeType.BUY else -pending
        projected_quote = current_quote + delta_quote
        # Never block an order that reduces absolute exposure. For a worsening
        # order, equality is allowed so a flat Turbo session can place its first
        # full-size quote exactly at the configured cap.
        worsens = abs(projected_quote) > abs(current_quote)
        return not (worsens and abs(projected_quote) > cap_quote)

    async def _reconcile(
        self, side: TradeType, target: Decimal, allowed: bool, mid: Decimal,
        *, level: int = 0, size_quote: Optional[Decimal] = None,
    ) -> None:
        is_bid = side is TradeType.BUY
        order_quote = self.order_amount_quote if size_quote is None else size_quote
        slot = self._slot(is_bid, level)
        cur_id, cur_price = slot.ex_id, slot.price

        # Include the next order in the exposure decision, not only inventory
        # that has already filled. This also cancels a partially filled resting
        # quote once its remaining direction would breach the cap.
        allowed = allowed and self._projected_order_within_exposure(
            side, mid, order_quote, level
        )

        # BUG-MM-2 fix: if the recorded quote already terminated (filled or
        # cancelled by the venue), forget it so we don't skip re-spawning a
        # fresh one just because the stale price was "close enough".
        if cur_id is not None:
            ex_existing = self.orchestrator.get(cur_id)
            if ex_existing is None or ex_existing.is_terminated:
                self._set_quote(is_bid, None, None, level=level)
                cur_id, cur_price = None, None

        if not allowed:
            if cur_id is not None:
                await self.orchestrator.stop(cur_id)
                self._set_quote(is_bid, None, None, level=level)
            return

        if cur_id is not None and cur_price is not None:
            ex = self.orchestrator.get(cur_id)
            if ex is not None and not ex.is_terminated:
                if self._should_hold(is_bid, target, slot):
                    return  # leave the resting quote — see _should_hold
                await self.orchestrator.stop(cur_id)
                self._set_quote(is_bid, None, None, level=level)

        # BUG-MM-3 fix: guard against ZeroDivisionError when target collapses
        # to 0 (e.g. mid feed returned 0 and spread_*_pct is 1).
        if target <= 0 or order_quote <= 0:
            return
        if self._would_self_trade(is_bid, target):
            return
        amount_base = order_quote / target
        cfg = OrderExecutorConfig(
            self.trading_pair, side, amount_base, ExecutionStrategy.LIMIT_MAKER, price=target
        )
        ex = OrderExecutor(
            cfg, user_id=self.user_id, controller_id=self.id, adapter=self.adapter,
            inventory=self.inventory,
        )
        ok = await self.spawn_executor(
            ex, ExecutorRequest(order_amount_quote=order_quote)
        )
        if ok:
            self._set_quote(is_bid, ex.id, target, level=level, size_quote=order_quote)

    # -- Phase 6: self-trade prevention ----------------------------------------
    def _would_self_trade(self, is_bid: bool, target: Decimal) -> bool:
        """Would this quote cross one of OUR OWN resting quotes?

        The venue has no self-trade prevention, and the controller only had a
        crossed-book bail plus an inclusive touch tolerance — neither of which
        looks at our own ladder. A bid placed at or above one of our live asks
        matches against it: we pay both sides of the fee to trade with
        ourselves, the fill lands in History as a real trade, and on Nado it
        also reads as wash trading. Cheap to check, so it is checked locally
        against the slots we know are live.

        Only the OPPOSITE side matters — two bids at the same price are just
        two bids. Equality counts as a cross, because a post-only order that
        would take is REJECTED by the venue, not converted.

        Interaction with the queue hold: after a sharp drop the new ask can sit
        below a bid that ``min_quote_lifetime_s`` is still holding, so the ask
        is skipped for a few seconds. That is the right trade — the held bid is
        about to fill anyway, and the alternative is trading with ourselves —
        and it clears on its own, because BUY reconciles before SELL and the
        bid moves down as soon as the hold expires.
        """
        if not self.self_trade_prevention or target <= 0:
            return False
        for (slot_is_bid, _lvl), slot in self._slots.items():
            if slot_is_bid is is_bid or slot.ex_id is None or slot.price is None:
                continue
            ex = self.orchestrator.get(slot.ex_id)
            if ex is None or ex.is_terminated:
                continue
            crosses = target >= slot.price if is_bid else target <= slot.price
            if crosses:
                self._stp_blocks += 1
                logger.warning(
                    "MM %s self-trade blocked: %s at %s would cross our own %s at %s",
                    self.trading_pair, "BUY" if is_bid else "SELL", target,
                    "ASK" if is_bid else "BID", slot.price,
                )
                return True
        return False

    # -- Phase 0: queue preservation -------------------------------------------
    def _holds_queue_position(self, is_bid: bool, target: Decimal, price: Decimal) -> bool:
        """Is this resting quote worth keeping purely on queue grounds?

        Two conditions, both required:

        * **Not behind the touch.** The venue BBO includes our own resting
          order, so being at the best price on our side means we are the touch —
          first in line. Behind it we simply will not fill, so we must re-quote.
        * **The new target is not a better price for us.** Backing a bid off to
          buy cheaper (or an ask up to sell higher) is worth the queue slot; it
          is also the correct response to the market moving away from us. What
          this refuses is the opposite trade — paying MORE (or selling for less)
          while also going to the back of the queue, which is what the
          improve-by-a-tick rule computes once our own quote becomes the BBO.

        Only answerable with a live book snapshot; mid-mode ticks never fetch
        one, so this simply does not fire there.
        """
        if self._touch_bid is None or self._touch_ask is None:
            return False
        if is_bid:
            behind = price < self._touch_bid
            target_is_better = target < price      # buy cheaper
        else:
            behind = price > self._touch_ask
            target_is_better = target > price      # sell higher
        return not behind and not target_is_better

    def _should_hold(self, is_bid: bool, target: Decimal, slot: _QuoteSlot) -> bool:
        """Leave the resting quote alone?

        Cancel/replace costs the whole queue position, and on a one-tick book
        (BTC-PERP's spread is a single tick) a quote cannot be improved — only
        re-queued at the back. So a cancel that lands on the same or a
        one-tick-different price is a pure loss of fill rate.

        Holding is never a risk increase: a stale quote on the wrong side of the
        market simply doesn't fill, and one on the right side fills — which is
        the job. Safety cancels (inventory ceiling, exposure cap, regime gate)
        run BEFORE this and are never delayed by it.
        """
        cur_price = slot.price
        if cur_price is None or cur_price <= 0:
            return False
        if self._within_tolerance(target, cur_price):
            return True
        # Front of the queue at a price no worse than the new target.
        if self._holds_queue_position(is_bid, target, cur_price):
            return True
        # Minimum lifetime: bound the churn rate so a fast cadence can't cancel a
        # quote before it has had any queue time at all.
        if self.min_quote_lifetime_s > 0 and slot.placed_at > 0:
            if (self._now() - slot.placed_at) < self.min_quote_lifetime_s:
                return True
        return False

    def _within_tolerance(self, target: Decimal, cur_price: Decimal) -> bool:
        """Is the resting quote close enough to the new target to leave alone?

        Mid mode keeps the relative ``price_distance_tolerance`` (half-spread).
        Touch mode must TRACK the touch: half a spread behind the best bid is
        no longer at the touch at all, so staleness there is ONE venue tick —
        inclusive. AUDIT-MM-2026-07-14 #6: the venue BBO includes our own
        resting quote; once we ARE the touch, the improve rule computes
        target = our_price + tick every tick. A strict (< tick) tolerance
        cancelled and re-improved on our own reflection endlessly, walking
        both quotes inward until they sat 1-2 ticks apart. Inclusive (<= tick)
        parks the quote once it is at-or-one-tick-inside the touch."""
        if cur_price <= 0:
            return False
        if self.quote_mode == "touch":
            try:
                tick = self.adapter.tick_size(self.trading_pair)
            except Exception:  # noqa: BLE001
                tick = Decimal(0)
            if tick and tick > 0:
                return abs(target - cur_price) <= tick
        return abs(target - cur_price) / cur_price <= self.price_distance_tolerance

    def _set_quote(
        self, is_bid: bool, ex_id: Optional[str], price: Optional[Decimal],
        *, level: int = 0, size_quote: Optional[Decimal] = None,
    ) -> None:
        slot = self._slot(is_bid, level)
        slot.ex_id, slot.price = ex_id, price
        if ex_id is None:
            slot.placed_at = 0.0
            slot.size_quote = Decimal(0)
        else:
            slot.placed_at = self._now()
            if size_quote is not None:
                slot.size_quote = size_quote

    def ladder_metrics(self) -> dict:
        """Ladder shape + live level occupancy, for /status and tests."""
        live = {True: 0, False: 0}
        for (is_bid, _lvl), slot in self._slots.items():
            if slot.ex_id is not None:
                live[bool(is_bid)] += 1
        return {
            "ladder_levels": self.ladder_levels,
            "ladder_curve": self.ladder_curve,
            "ladder_step_bp": float(self.ladder_step_bp),
            "ladder_live_bids": live[True],
            "ladder_live_asks": live[False],
            "min_quote_lifetime_s": self.min_quote_lifetime_s,
            # Phase 5 actuation state, for /mm_status. Empty profile == the
            # selector is off (every strategy other than mid).
            "profile": self.profile,
            "half_spread_floor_bp": float(self.spread_floor_half_pct) * 10_000.0,
            "reservation_offset_bp": float(self.reservation_offset_bp),
            # Phase 6 state.
            "alpha": self.alpha,
            "alpha_confidence": self.alpha_confidence,
            "alpha_offset_bp": float(self.alpha_offset_bp),
            "signal_degraded": self.signal_degraded,
            "markout_widen": float(self.markout_widen),
            "self_trade_blocks": self._stp_blocks,
        }
