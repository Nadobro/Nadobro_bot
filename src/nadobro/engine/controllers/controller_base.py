"""Controller base — long-running strategy with on_start / on_tick / on_stop.

A controller owns a stable ``id`` (used to filter the orchestrator's executor
pool), splits its parameters into **configs** (strategy knobs) and **limits**
(``RiskLimits`` consumed by the Risk Engine), and spawns Executors via the
orchestrator. Lifecycle state (CREATED → ACTIVE → STOPPED/FAILED) is driven by
the orchestrator's spawn/stop/tick methods.

Implemented in Phase 4.
"""
from __future__ import annotations

import abc
import time
import uuid
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.nadobro.engine.adapter.base import NadoAdapterBase
from src.nadobro.engine.executor_base import Executor
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.risk import ExecutorRequest
from src.nadobro.engine.types import RiskLimits

if TYPE_CHECKING:  # avoid a runtime import cycle (orchestrator imports nothing here)
    from src.nadobro.engine.orchestrator import ExecutorOrchestrator


class ControllerState(Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


# ── ladder re-center geometry (shared by GridController + DynamicGridController) ──
# A ladder grid only follows price through the executor's in-place re-center,
# which re-quotes UNFILLED maker opens (no flatten, no realized loss). The
# trigger is therefore a POSITIONING question, not a risk question, and it is
# bounded by the ladder's own geometry — not by a free-form percent.
LADDER_RECENTER_FLOOR_BP = 12.0
LADDER_RECENTER_MIN_INTERVAL_S = 5.0


def ladder_recenter_threshold_bp(
    step_bp: float,
    levels: int,
    user_bp: float,
    *,
    floor_bp: float = LADDER_RECENTER_FLOOR_BP,
) -> tuple[float, bool]:
    """Effective drift (bp) that must accrue before a ladder re-centers.

    RGRID-STALE-LADDER (prod session 165, 2026-07-28): the user-facing knob is a
    percent of PRICE (``rgrid_reset_threshold_pct`` — registry default 1.0%, UI
    presets 0.8% / 1.5%) while the ladder it steers is only ``step x (levels-1)``
    wide. On BTC at 10bp x 3 levels that is a 20bp band asked to wait for 80bp of
    drift, so price left the band and the grid never re-quoted: 2063 of 2080
    cycles placed zero orders and the same maker price sat on the book for 3.5h.

    Bounds, both derived from the ladder itself:

    * lower — one level ``step``: re-centering for less than the spacing between
      two levels just churns cancel/replace without moving the ladder anywhere.
    * upper — one ``band`` width: once price has drifted the full width of the
      ladder, every level is either filled or unreachable. Waiting longer cannot
      earn anything, it only strands the quotes.

    Returns ``(threshold_bp, clamped)``; ``clamped`` is True when the caller's
    value had to be cut down to the geometry so it can be logged once.
    """
    step_bp = max(float(step_bp or 0.0), 0.0)
    band_bp = step_bp * float(max(int(levels or 0) - 1, 1))
    lo = max(float(floor_bp), step_bp)
    hi = max(lo, band_bp)
    user_bp = max(float(user_bp or 0.0), 0.0)
    if user_bp <= 0:
        # Auto-follow: one band width of drift (unchanged default behaviour).
        return hi, False
    if user_bp > hi:
        return hi, True
    return max(user_bp, lo), False


class Controller(abc.ABC):
    def __init__(
        self,
        *,
        user_id: int,
        name: str,
        orchestrator: "ExecutorOrchestrator",
        adapter: NadoAdapterBase,
        inventory: Optional[InventoryRepository] = None,
        configs: Optional[Dict[str, object]] = None,
        limits: Optional[RiskLimits] = None,
        controller_id: Optional[str] = None,
    ) -> None:
        self.id = controller_id or f"{name}-{uuid.uuid4().hex[:8]}"
        self.user_id = user_id
        self.name = name
        self.orchestrator = orchestrator
        self.adapter = adapter
        self.inventory = inventory
        self.configs: Dict[str, object] = configs or {}
        self.limits = limits or RiskLimits()
        self.state = ControllerState.CREATED
        self.started_at: Optional[float] = None
        self.stopped_at: Optional[float] = None
        # Set by the orchestrator when on_start raises, so the runtime can
        # surface why a start failed (e.g. a leg rejected by the risk gate).
        self._start_error: Optional[str] = None

    # -- state transitions (called by the orchestrator) -------------------
    @property
    def is_active(self) -> bool:
        return self.state is ControllerState.ACTIVE

    def _set_active(self) -> None:
        self.state = ControllerState.ACTIVE
        self.started_at = time.time()

    def _set_stopped(self) -> None:
        self.state = ControllerState.STOPPED
        self.stopped_at = time.time()

    def _set_failed(self) -> None:
        self.state = ControllerState.FAILED
        self.stopped_at = time.time()

    # -- helpers ----------------------------------------------------------
    async def spawn_executor(
        self, executor: Executor, request: Optional[ExecutorRequest] = None
    ) -> bool:
        return await self.orchestrator.spawn(executor, request)

    def my_executors(self, active_only: bool = True) -> List[Executor]:
        return self.orchestrator.list(self.id, active_only=active_only)

    def order_counts(self) -> Dict[str, int]:
        """Real venue-order activity for this controller, summed across all of
        its executors (active + terminated still held by the orchestrator) for
        this worker's lifetime. The engine cycle result carries no per-order
        count, so this is how /status and the per-cycle log get a true placed/
        filled/cancelled figure instead of 0."""
        placed = filled = cancelled = 0
        for ex in self.my_executors(active_only=False):
            placed += int(getattr(ex, "orders_placed", 0) or 0)
            filled += int(getattr(ex, "orders_filled", 0) or 0)
            cancelled += int(getattr(ex, "orders_cancelled", 0) or 0)
        return {"orders_placed": placed, "orders_filled": filled, "orders_cancelled": cancelled}

    def cfg(self, key: str, default: Any = None) -> Any:
        return self.configs.get(key, default)

    # -- regime gate (grid family + MM) ------------------------------------
    # Pause semantics: PAUSE blocks NEW opening quotes only. Existing
    # positions, close legs, barriers, and the inventory cap keep running —
    # pause is "stop digging", never "flatten". The gate transition is
    # surfaced via ``consume_gate_event`` so the runtime can notify the user
    # exactly once per flip.
    async def evaluate_quote_gate(
        self,
        trading_pair: str,
        *,
        pause_on_trend: bool = True,
        adverse_trend: Optional[str] = None,
        pause_on_breakout: bool = True,
    ) -> str:
        """Refresh ``self.gate_verdict`` from the regime-gate routine.

        Requires ``regime_gate_enabled`` in configs and a ``candle_provider``;
        without either, the gate stays inactive (verdict QUOTE) — a missing
        candle feed must degrade to ungated behavior, not silence.

        ``pause_on_trend=False`` (dgrid): the controller's own regime
        selector TRADES trends (ReverseGrid), so a trend verdict is treated
        as QUOTE and only breakout/expansion (no acceptance anywhere) pauses.

        ``adverse_trend`` (e.g. ``"trending_up"`` for a short reverse grid):
        pause ONLY on that one trend direction; the opposite trend — the regime
        this directional grid exists to trade — is treated as QUOTE. A short
        reverse grid must engage a downtrend, not sit it out. Breakout /
        expansion still pause regardless.

        ``pause_on_breakout=False`` (dgrid): the controller's variance-ratio
        classifier already chooses GRID vs RGRID per regime, so a breakout /
        expansion is a tradeable directional move for it, not a sit-out — treat
        those verdicts as QUOTE too. With both pause flags off, dgrid quotes in
        every regime (it is never gated out).

        Transition discipline is asymmetric: a PAUSE commits IMMEDIATELY
        (protection first), but resuming to QUOTE requires
        ``gate_resume_confirm_ticks`` consecutive QUOTE verdicts — a regime
        flickering at the threshold must not churn cancels or spam the user
        with pause/resume notifications.
        """
        if not getattr(self, "gate_verdict", None):
            self.gate_verdict: str = "QUOTE"
            self.gate_reason: str = ""
            self.gate_atr_pct: float = 0.0
            self._gate_event: Optional[Dict[str, str]] = None
            self._gate_resume_streak: int = 0
            self._gate_prev_enabled: bool = False
        if not bool(self.cfg("regime_gate_enabled", False)):
            # MID-GATE-STALE-PAUSE (audit 2026-07-31): a gate disabled MID-RUN
            # (the signal overlay arms it while suppressing, then disarms —
            # overlay_actuator sets regime_gate_enabled=True and the next
            # un-suppressed cycle reverts it) must not freeze its last verdict.
            # This early return used to hand back a stale PAUSE forever; with a
            # flat book that quotes nothing — LIVE session, zero orders,
            # indefinitely. Reset ONLY on the enabled→disabled transition: a
            # verdict the gate itself produced dies with the gate, but a
            # never-enabled gate stays a passive carrier (dgrid's own
            # classifier drives gate_verdict directly in that mode and its
            # breakout sit-out must not be clobbered).
            if getattr(self, "_gate_prev_enabled", False):
                self._gate_prev_enabled = False
                if self.gate_verdict != "QUOTE":
                    self.gate_verdict, self.gate_reason = "QUOTE", ""
                    self._gate_resume_streak = 0
                    self._gate_event = {"state": "QUOTE", "reason": ""}
            return self.gate_verdict
        self._gate_prev_enabled = True
        provider = self.cfg("candle_provider")
        if provider is None:
            return self.gate_verdict
        try:
            import inspect as _inspect

            from src.nadobro.engine.routines import regime_gate

            raw = provider(trading_pair)  # type: ignore[operator]
            if _inspect.isawaitable(raw):
                raw = await raw
            result = await regime_gate.run(trading_pair, list(raw or []))
        except Exception:  # policy: degrade-ok(gate eval is best-effort; stay on last verdict)
            return self.gate_verdict
        new_verdict = str(result.get("verdict") or "QUOTE")
        new_reason = str(result.get("reason") or "")
        self.gate_atr_pct = float(str(result.get("atr_pct") or 0.0))
        if not pause_on_trend and new_reason in ("trending_up", "trending_down"):
            new_verdict, new_reason = "QUOTE", ""
        elif adverse_trend and new_reason in ("trending_up", "trending_down") and new_reason != adverse_trend:
            # Directional grid: the favorable trend is its purpose, not a hazard.
            new_verdict, new_reason = "QUOTE", ""
        if not pause_on_breakout and new_reason in ("breakout", "expansion"):
            # dgrid trades breakouts/expansions via its own GRID<->RGRID
            # selector — do not sit it out.
            new_verdict, new_reason = "QUOTE", ""

        if new_verdict == "PAUSE":
            self._gate_resume_streak = 0
            if self.gate_verdict != "PAUSE":
                self._gate_event = {"state": "PAUSE", "reason": new_reason}
            self.gate_verdict, self.gate_reason = "PAUSE", new_reason
            return self.gate_verdict

        # new_verdict == QUOTE
        if self.gate_verdict == "PAUSE":
            confirm = int(self.cfg("gate_resume_confirm_ticks", 2) or 2)
            self._gate_resume_streak += 1
            if self._gate_resume_streak < max(1, confirm):
                return self.gate_verdict  # stay paused until the range confirms
            self._gate_event = {"state": "QUOTE", "reason": new_reason}
        self._gate_resume_streak = 0
        self.gate_verdict, self.gate_reason = "QUOTE", new_reason
        return self.gate_verdict

    @property
    def gate_paused(self) -> bool:
        return getattr(self, "gate_verdict", "QUOTE") == "PAUSE"

    def consume_gate_event(self) -> Optional[Dict[str, str]]:
        """Pop the pending QUOTE<->PAUSE transition (None if no flip)."""
        event = getattr(self, "_gate_event", None)
        self._gate_event = None
        return event

    # -- inventory cap (backstop behind the gate) ---------------------------
    # Suppress the side that WORSENS net exposure once it exceeds
    # ``max_net_exposure_pct`` of allocated margin; re-allow below
    # ``resume_frac`` of the cap (hysteresis, no flapping). Reduce-only
    # quoting always continues — this caps how lopsided the book can get
    # before the session stop would have to act.
    def exposure_allowed_sides(self, trading_pair: str, mid: object) -> Dict[str, bool]:
        from decimal import Decimal

        allowed = {"buy": True, "sell": True}
        if self.inventory is None:
            return allowed
        cap_pct = self.cfg("max_net_exposure_pct")
        margin = self.cfg("margin_quote")
        try:
            cap_frac = Decimal(str(cap_pct)) / Decimal(100)
            margin_quote = Decimal(str(margin))
            mid_d = Decimal(str(mid))
        except Exception:  # policy: degrade-ok(cap unset/malformed; cap inactive)
            return self._apply_entry_suppression(allowed, trading_pair, mid)
        if cap_frac <= 0 or margin_quote <= 0 or mid_d <= 0:
            return self._apply_entry_suppression(allowed, trading_pair, mid)
        net_quote = self.inventory.get(self.user_id, trading_pair, self.id).net_amount_base * mid_d
        cap_quote = margin_quote * cap_frac
        resume_quote = cap_quote * Decimal(str(self.cfg("exposure_resume_frac", "0.7")))
        capped = bool(getattr(self, "_exposure_capped", False))
        if abs(net_quote) >= cap_quote:
            capped = True
        elif abs(net_quote) <= resume_quote:
            capped = False
        self._exposure_capped = capped
        self.exposure_net_quote = net_quote
        if capped:
            if net_quote > 0:
                allowed["buy"] = False   # long over cap: only reduce
            else:
                allowed["sell"] = False  # short over cap: only reduce
        return self._apply_entry_suppression(allowed, trading_pair, mid)

    def _apply_entry_suppression(
        self, allowed: Dict[str, bool], trading_pair: str, mid: object
    ) -> Dict[str, bool]:
        """Honour an explicit ``suppress_new_entries`` posture: reduce-only.

        The financial overlay used to express suppression by writing
        ``max_net_exposure_pct = 0``. That is the OPPOSITE of suppression — a cap
        of 0 short-circuits both this check and
        ``_projected_order_within_exposure`` as "cap INACTIVE", so the strategy
        lost its net-exposure ceiling at exactly the moment the overlay wanted to
        choke it. A resting ladder survived that (its own budget bounds it); a
        taker strategy that adds a fresh step per break did not, and the regime
        gate only pauses on TRENDS, so in chop it pyramided with no ceiling at
        all. Suppression is now its own flag and can never disable the cap.
        """
        from decimal import Decimal

        if not self.cfg("suppress_new_entries"):
            return allowed
        net_base = Decimal(0)
        if self.inventory is not None:
            try:
                net_base = Decimal(
                    str(self.inventory.get(self.user_id, trading_pair, self.id).net_amount_base)
                )
            except Exception:  # noqa: BLE001  # policy: degrade-ok(flat book ⇒ both sides denied)
                net_base = Decimal(0)
        # Reduce-only: permit exactly the side that shrinks |net|. A flat book has
        # nothing to reduce, so both sides close.
        allowed["buy"] = allowed["buy"] and net_base < 0
        allowed["sell"] = allowed["sell"] and net_base > 0
        return allowed

    # -- lifecycle hooks --------------------------------------------------
    @abc.abstractmethod
    async def on_start(self) -> None:
        ...

    @abc.abstractmethod
    async def on_tick(self) -> None:
        ...

    async def on_stop(self, reason: str = "stopped") -> None:
        """Default: rely on the orchestrator to batch-cancel child executors."""
        return None
