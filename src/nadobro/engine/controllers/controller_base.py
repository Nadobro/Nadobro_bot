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
import inspect
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.nadobro.quant import vol_model
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

# Pause reasons that are a VENUE hold set by a controller, never a gate verdict
# (see evaluate_quote_gate). Mirrors routines/regime_gate.VENUE_GATE_REASONS;
# duplicated as a literal so this module keeps no import edge to the routine.
_VENUE_GATE_REASONS = frozenset({
    "venue_unreadable", "venue_residual", "venue_foreign_position", "venue_min_notional",
    "stop_budget_too_tight",
})

# Pause reasons asserted by the OPT-IN realized-volatility model (quant/vol_model,
# docs/grid_vol_model.md), never by the regime gate. Mirrors
# routines/regime_gate.VOL_GATE_REASONS; literal here for the same reason as above.
_VOL_GATE_REASONS = frozenset({"vol_hot", "vol_unknown", "vol_warming", "rgrid_vol_wait"})

# Retry a vol refresh that got no NEW closed bar (denied / failed / stale cache
# read) after this long instead of waiting for the next wall-clock minute.
_GVOL_RETRY_S = 20.0


def _cfg_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    try:
        return bool(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return bool(value)


def _cfg_float(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if out == out else default


@dataclass(frozen=True)
class VolModelConfig:
    """The grid family's OPT-IN vol-model switches (every one default OFF).

    Read from the mapped ``gvol_*`` config keys (``strategy/engine_runtime.
    _gvol_config``). Every key is absent from a config that predates the model,
    so ``from_configs({})`` is all-OFF and the controllers behave exactly as
    before — pinned by tests/engine/test_gvol_controllers.py."""

    gate_enabled: bool = False
    gate_mult: float = 0.82
    spacing_enabled: bool = False
    spacing_k: float = 2.6
    spacing_floor_bp: float = 6.8
    spacing_min_bp: float = 0.0
    spacing_max_bp: float = 0.0
    skew_enabled: bool = False
    cap_hard: bool = False
    cap_pct: float = 30.0
    arm_enabled: bool = False
    arm_compress_mult: float = 0.91
    arm_expand_mult: float = 1.42

    @classmethod
    def from_configs(cls, configs: Optional[Dict[str, object]]) -> "VolModelConfig":
        c = configs or {}
        d = cls()
        return cls(
            gate_enabled=_cfg_bool(c.get("gvol_gate_enabled", False)),
            gate_mult=_cfg_float(c.get("gvol_gate_mult"), d.gate_mult),
            spacing_enabled=_cfg_bool(c.get("gvol_spacing_enabled", False)),
            spacing_k=_cfg_float(c.get("gvol_spacing_k"), d.spacing_k),
            spacing_floor_bp=_cfg_float(c.get("gvol_spacing_floor_bp"), d.spacing_floor_bp),
            spacing_min_bp=_cfg_float(c.get("gvol_spacing_min_bp"), d.spacing_min_bp),
            spacing_max_bp=_cfg_float(c.get("gvol_spacing_max_bp"), d.spacing_max_bp),
            skew_enabled=_cfg_bool(c.get("gvol_skew_enabled", False)),
            cap_hard=_cfg_bool(c.get("gvol_cap_hard", False)),
            cap_pct=_cfg_float(c.get("gvol_cap_pct"), d.cap_pct),
            arm_enabled=_cfg_bool(c.get("gvol_arm_enabled", False)),
            arm_compress_mult=_cfg_float(c.get("gvol_arm_compress_mult"), d.arm_compress_mult),
            arm_expand_mult=_cfg_float(c.get("gvol_arm_expand_mult"), d.arm_expand_mult),
        )

    @property
    def needs_data(self) -> bool:
        """A feature that reads the volatility estimate is on."""
        return self.gate_enabled or self.spacing_enabled or self.skew_enabled or self.arm_enabled

    @property
    def any_enabled(self) -> bool:
        return self.needs_data or self.cap_hard

    def features(self) -> str:
        names = []
        if self.gate_enabled:
            names.append("gate")
        if self.spacing_enabled:
            names.append("spacing")
        if self.skew_enabled:
            names.append("skew")
        if self.cap_hard:
            names.append("cap")
        if self.arm_enabled:
            names.append("arm")
        return ",".join(names)


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
        # Order counts of TERMINATED executors the orchestrator has since pruned
        # (bounded retention, 2026-09-03): banked here so order_counts() stays a
        # whole-run figure while the orchestrator no longer holds every executor
        # the session ever spawned (1,500+ for a re-quoting ladder in a day).
        self._banked_counts: Dict[str, int] = {
            "orders_placed": 0, "orders_filled": 0, "orders_cancelled": 0,
        }

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
        banked = getattr(self, "_banked_counts", None) or {}
        placed = int(banked.get("orders_placed", 0))
        filled = int(banked.get("orders_filled", 0))
        cancelled = int(banked.get("orders_cancelled", 0))
        for ex in self.my_executors(active_only=False):
            placed += int(getattr(ex, "orders_placed", 0) or 0)
            filled += int(getattr(ex, "orders_filled", 0) or 0)
            cancelled += int(getattr(ex, "orders_cancelled", 0) or 0)
        return {"orders_placed": placed, "orders_filled": filled, "orders_cancelled": cancelled}

    def bank_executor_counts(self, executor: Executor) -> None:
        """Called by the orchestrator right before it prunes a TERMINATED
        executor, so its venue-order activity survives in ``order_counts``."""
        banked = getattr(self, "_banked_counts", None)
        if banked is None:
            self._banked_counts = banked = {"orders_placed": 0, "orders_filled": 0, "orders_cancelled": 0}
        banked["orders_placed"] += int(getattr(executor, "orders_placed", 0) or 0)
        banked["orders_filled"] += int(getattr(executor, "orders_filled", 0) or 0)
        banked["orders_cancelled"] += int(getattr(executor, "orders_cancelled", 0) or 0)

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
        # VENUE HOLDS ARE NOT GATE VERDICTS (audit 2026-09-16): a controller may
        # park itself on a venue reason (position unreadable / residual / foreign
        # position — see regime_gate.VENUE_GATE_REASONS) without a gate event. If
        # the gate then read that PAUSE as its own, every QUOTE verdict would walk
        # the resume streak and emit a "resumed quoting" event — a notification
        # storm during a hold that never paused quoting for a regime reason.
        # Treat the prior verdict as QUOTE; the controller re-asserts the hold
        # after this call while it still applies.
        # A vol-model stand-down (quant/vol_model) is the same kind of hold: it is
        # re-asserted by _assert_vol_gate after this call, and must never walk the
        # regime resume streak or emit a spurious "resumed" event.
        if self.gate_verdict == "PAUSE" and (
            self.gate_reason in _VENUE_GATE_REASONS or self.gate_reason in _VOL_GATE_REASONS
        ):
            self.gate_verdict, self.gate_reason = "QUOTE", ""
            self._gate_resume_streak = 0
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

    # -- realized-volatility model (OPT-IN; quant/vol_model) ------------------
    # Every method below is a no-op unless a ``gvol_*`` feature is switched on,
    # so a config without those keys trades exactly as before.
    @property
    def vol_cfg(self) -> VolModelConfig:
        cfg = getattr(self, "_vol_cfg", None)
        if cfg is None:
            cfg = VolModelConfig.from_configs(self.configs)
            self._vol_cfg = cfg
        self._gvol_state_init()
        return cfg

    @vol_cfg.setter
    def vol_cfg(self, value: VolModelConfig) -> None:
        self._vol_cfg = value

    def reload_vol_cfg(self) -> VolModelConfig:
        """Re-read the ``gvol_*`` keys after a live settings edit."""
        self._vol_cfg = VolModelConfig.from_configs(self.configs)
        return self._vol_cfg

    def _gvol_now(self) -> float:
        return time.time()

    def _gvol_state_init(self) -> None:
        if getattr(self, "_gvol_init", False):
            return
        self._gvol_init = True
        self.gvol_series: Optional[vol_model.MinuteSeries] = None
        self.gvol_baseline: Optional[vol_model.VolBaseline] = None
        self.gvol_gate: Optional[vol_model.GateVerdict] = None
        self.gvol_arm: Optional[vol_model.ArmVerdict] = None
        self.gvol_rv60: Optional[float] = None
        self._gvol_minute: int = -1
        self._gvol_last_try: float = 0.0
        # Last COMBINED (verdict, reason) after _assert_vol_gate — drives events.
        self._gvol_prev: tuple[str, str] = ("QUOTE", "")
        self._gvol_resumed: bool = False
        self.gvol_withdrawn: int = 0
        self.gvol_spacing_bp_live: float = 0.0
        self.gvol_skew_bp: float = 0.0
        self.gvol_cap_usd: float = 0.0
        self.gvol_cap_used_usd: float = 0.0

    async def _gvol_refresh(self, pair: str) -> None:
        """Refresh the volatility reading at most once per CLOSED wall-clock minute.

        DENIED != EMPTY: the candle provider returns ``[]`` both on a budget
        denial and on an SDK failure. An empty read, an exception, or a series
        whose newest closed bar is no newer than the last good one is "no new
        information": the last good series is KEPT and the verdict is re-evaluated
        against the wall clock, so it turns UNKNOWN by itself once the newest bar
        is older than ``vol_model.MAX_DATA_AGE_S`` — an empty read never moves a
        verdict to CALM and never flaps it on one throttled read."""
        self._gvol_state_init()
        cfg = self.vol_cfg
        if not cfg.needs_data:
            self.gvol_gate = None
            self.gvol_arm = None
            return
        now = self._gvol_now()
        minute = int(now // 60)
        want_newest = (minute - 1) * 60          # open time of the last closed bar
        stale = self.gvol_series is None or self.gvol_series.newest_ts < want_newest
        due = minute != self._gvol_minute or (stale and now - self._gvol_last_try >= _GVOL_RETRY_S)
        if due:
            self._gvol_last_try = now
            candles: list = []
            provider = self.cfg("candle_provider")
            if provider is not None:
                try:
                    raw = provider(pair)  # type: ignore[operator]
                    if inspect.isawaitable(raw):
                        raw = await raw
                    candles = list(raw or [])
                except Exception:  # noqa: BLE001  # policy: degrade-ok(no new info; keep last good series)
                    candles = []
            series = vol_model.closed_minute_series(candles, now_s=now) if candles else None
            fresh = series is not None and (
                self.gvol_series is None or series.newest_ts > self.gvol_series.newest_ts
            )
            if fresh:
                self.gvol_series = series
                if series is not None and series.newest_ts >= want_newest:
                    self._gvol_minute = minute
                bp = self.cfg("gvol_baseline_provider")
                if bp is not None:
                    try:
                        res = bp(pair, candles)  # type: ignore[operator]
                        if inspect.isawaitable(res):
                            res = await res
                        if isinstance(res, vol_model.VolBaseline):
                            self.gvol_baseline = res
                    except Exception:  # noqa: BLE001  # policy: degrade-ok(keep last baseline; WARMING if none)
                        logging.getLogger(__name__).debug(
                            "gvol baseline provider failed pair=%s", pair, exc_info=True)
        self._gvol_evaluate(now)

    def _gvol_evaluate(self, now: float) -> None:
        cfg = self.vol_cfg
        self.gvol_gate = vol_model.gate_verdict(
            self.gvol_series, self.gvol_baseline, mult=cfg.gate_mult, now_s=now,
        )
        self.gvol_rv60 = self.gvol_gate.rv60_bp if self.gvol_gate.state in (
            vol_model.CALM, vol_model.HOT, vol_model.WARMING) else None
        if cfg.arm_enabled:
            self.gvol_arm = vol_model.arm_verdict(
                self.gvol_series, self.gvol_baseline,
                compress_mult=cfg.arm_compress_mult, expand_mult=cfg.arm_expand_mult,
                now_s=now,
            )
        else:
            self.gvol_arm = None

    @property
    def gvol_paused(self) -> bool:
        """The combined gate is paused BECAUSE of the vol model."""
        return self.gate_paused and getattr(self, "gate_reason", "") in _VOL_GATE_REASONS

    @property
    def gvol_stand_down(self) -> bool:
        """The vol gate is ON and not CALM (HOT / UNKNOWN / WARMING): withdraw
        resting entries and hold, whatever reason the combined gate displays."""
        if not self.vol_cfg.gate_enabled:
            return False
        v = getattr(self, "gvol_gate", None)
        return v is None or v.state != vol_model.CALM

    async def gvol_tick(self, pair: str) -> None:
        """Per-tick vol step: refresh + fold into the gate. A strict no-op (no
        state touched) while every vol feature is off and the gate was never
        vol-paused — so a config without ``gvol_*`` keys trades exactly as
        before. One extra pass after a toggle-OFF lets a vol pause resume."""
        self._gvol_state_init()
        self._gvol_resumed = False
        if not self.vol_cfg.any_enabled and self._gvol_prev[1] not in _VOL_GATE_REASONS:
            return
        await self._gvol_refresh(pair)
        self._assert_vol_gate()

    def gvol_tick_bp(self, mid: object) -> float:
        """One venue tick in bp of ``mid`` (0 when unknown)."""
        try:
            tick = Decimal(str(self.adapter.tick_size(str(self.cfg("trading_pair")))))
            m = Decimal(str(mid))
        except Exception:  # noqa: BLE001  # policy: degrade-ok(no tick meta -> no tick floor)
            return 0.0
        if tick <= 0 or m <= 0:
            return 0.0
        return float(tick / m * Decimal(10000))

    def gvol_spacing_target(self, mid: object) -> Optional[float]:
        """Vol-scaled level spacing (bp) for this tick, or None (feature off / rv
        unknown — the caller keeps its current spacing)."""
        cfg = self.vol_cfg
        if not cfg.spacing_enabled:
            return None
        return vol_model.spacing_bp(
            getattr(self, "gvol_rv60", None), k=cfg.spacing_k, floor_bp=cfg.spacing_floor_bp,
            min_bp=cfg.spacing_min_bp, max_bp=cfg.spacing_max_bp, tick_bp=self.gvol_tick_bp(mid),
        )

    def _assert_vol_gate(self) -> None:
        """Fold the vol gate into ``gate_verdict`` (call right after
        ``evaluate_quote_gate``). Precedence: a venue hold or a regime-gate PAUSE
        already asserted keeps its reason (either way we are paused); otherwise
        a non-CALM vol verdict pauses with its own reason. Events fire only when
        the COMBINED verdict flips because of the vol gate, so the single-slot
        ``_gate_event`` never double-fires."""
        self._gvol_state_init()
        if not getattr(self, "gate_verdict", None):
            self.gate_verdict = "QUOTE"
            self.gate_reason = ""
            self._gate_event = None
        cfg = self.vol_cfg
        v = self.gvol_gate
        if cfg.gate_enabled and (v is None or v.state != vol_model.CALM):
            if not (self.gate_verdict == "PAUSE" and self.gate_reason not in _VOL_GATE_REASONS):
                reason = v.reason if v is not None else vol_model.REASON_UNKNOWN
                self.gate_verdict, self.gate_reason = "PAUSE", reason or vol_model.REASON_UNKNOWN
        elif self.gate_reason in _VOL_GATE_REASONS:
            self.gate_verdict, self.gate_reason = "QUOTE", ""
        prev_verdict, prev_reason = self._gvol_prev
        now_pair = (str(self.gate_verdict), str(self.gate_reason))
        self._gvol_resumed = False
        if getattr(self, "_gate_event", None) is None:
            if prev_verdict != "PAUSE" and now_pair[0] == "PAUSE" and now_pair[1] in _VOL_GATE_REASONS:
                self._gate_event = {"state": "PAUSE", "reason": now_pair[1]}
            elif prev_verdict == "PAUSE" and prev_reason in _VOL_GATE_REASONS and now_pair[0] == "QUOTE":
                self._gate_event = {"state": "QUOTE", "reason": "", "prev_reason": prev_reason}
        if prev_verdict == "PAUSE" and prev_reason in _VOL_GATE_REASONS and now_pair[0] == "QUOTE":
            self._gvol_resumed = True
        self._gvol_prev = now_pair

    def gvol_cap_quote(self) -> Optional[Decimal]:
        """Hard-cap budget (held + resting growth side), or None when the hard cap
        is off / the deployment is unknown."""
        cfg = self.vol_cfg
        if not cfg.cap_hard:
            return None
        try:
            margin = Decimal(str(self.cfg("margin_quote") or 0))
        except Exception:  # noqa: BLE001  # policy: degrade-ok(no deployment -> cap inactive)
            return None
        if margin <= 0 or cfg.cap_pct <= 0:
            return None
        return margin * Decimal(str(cfg.cap_pct)) / Decimal(100)

    def _gvol_held_quote(self, mid: Decimal, active: list) -> Decimal:
        total = Decimal(0)
        for ex in active:
            hq = getattr(ex, "held_quote", None)
            if callable(hq):
                total += Decimal(str(hq(mid)))
        return total

    def _gvol_ladder_skew(self, mid: Decimal, active: list, step_bp: float,
                          *, sell_side: bool = False) -> float:
        """A-S skew (bp, <= 0 for a long ladder holding inventory) for a
        GridExecutor ladder; 0 while the skew feature is off."""
        cfg = self.vol_cfg
        if not cfg.skew_enabled:
            self.gvol_skew_bp = 0.0
            return 0.0
        margin = Decimal(str(self.cfg("margin_quote") or 0))
        cap = margin * Decimal(str(cfg.cap_pct)) / Decimal(100)
        if cap <= 0:
            self.gvol_skew_bp = 0.0
            return 0.0
        q = max(0.0, min(1.0, float(self._gvol_held_quote(mid, active) / cap)))
        if sell_side:
            q = -q
        off = vol_model.skew_offset_bp(q, getattr(self, "gvol_rv60", None), step_bp)
        self.gvol_skew_bp = off
        return off

    async def _gvol_apply_cap(self, mid: Optional[Decimal], active: list) -> None:
        """Hard cap (opt-in) for GridExecutor ladders: held + resting entries <=
        cap + one level, trimmed deepest-first. Off -> every executor's cap is
        None (the classic ladder, untouched)."""
        self._gvol_state_init()
        cap = self.gvol_cap_quote()
        if cap is None:
            if self.gvol_cap_usd:
                self.gvol_cap_usd = 0.0
                self.gvol_cap_used_usd = 0.0
            for ex in active:
                if getattr(ex, "cap_quote", None) is not None:
                    ex.cap_quote = None
            return
        used = Decimal(0)
        for ex in active:
            if not hasattr(ex, "cap_quote"):
                continue
            ex.cap_quote = cap
            if mid is not None and mid > 0:
                await ex.trim_to_cap(mid)
                used += ex.held_quote(mid) + ex.resting_open_quote()
        self.gvol_cap_usd = float(cap)
        self.gvol_cap_used_usd = float(used)

    def gvol_metrics(self) -> Dict[str, object]:
        """Vol-model telemetry for /status (``gvol_*``; the prefix keeps it clear
        of the Volume Bot's ``vol_*`` keys). ``{"gvol_state": ""}`` when the model
        is off, which clears a stale card line."""
        self._gvol_state_init()
        cfg = self.vol_cfg
        if not cfg.any_enabled:
            return {"gvol_state": ""}
        out: Dict[str, object] = {
            "gvol_features": cfg.features(),
            "gvol_withdrawn": int(self.gvol_withdrawn),
            "gvol_spacing_bp": float(self.gvol_spacing_bp_live or 0.0),
            "gvol_skew_bp": float(self.gvol_skew_bp or 0.0),
            "gvol_cap_usd": float(self.gvol_cap_usd or 0.0),
            "gvol_cap_used_usd": float(self.gvol_cap_used_usd or 0.0),
        }
        g = self.gvol_gate
        a = self.gvol_arm
        if cfg.arm_enabled and a is not None:
            out.update({
                "gvol_state": a.state, "gvol_reason": a.reason, "gvol_detail": a.detail,
                "gvol_rv60_bp": float(a.rv60_bp or 0.0), "gvol_rv15_bp": float(a.rv15_bp or 0.0),
                "gvol_compress_bp": float(a.compress_bp or 0.0),
                "gvol_expand_bp": float(a.expand_bp or 0.0),
                "gvol_compressed_ago_min": int(a.compressed_ago_min) if a.compressed_ago_min is not None else -1,
                "gvol_data_age_s": float(a.data_age_s or 0.0),
                "gvol_base_hours": float(a.base_hours or 0.0),
            })
        elif g is not None:
            out.update({
                "gvol_state": g.state,
                "gvol_reason": g.reason, "gvol_detail": g.detail,
                "gvol_rv60_bp": float(g.rv60_bp or 0.0),
                "gvol_gate_bp": float(g.gate_bp or 0.0),
                "gvol_base_bp": float(g.base_bp or 0.0),
                "gvol_base_hours": float(g.base_hours or 0.0),
                "gvol_calm_min": int(g.calm_streak_min),
                "gvol_resume_in_min": int(g.resume_in_min),
                "gvol_data_age_s": float(g.data_age_s or 0.0),
            })
        else:
            # Hard cap alone reads no data.
            out["gvol_state"] = "CAP"
        return out

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
