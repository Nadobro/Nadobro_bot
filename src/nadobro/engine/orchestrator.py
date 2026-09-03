"""Executor Orchestrator — single supervisor that owns executor lifecycles.

Supports spawn / stop / list (filtered by ``controller_id``), an event bus
(queue + inspectable log), batched cancel via ``asyncio.gather``, and consults
the Risk Engine before each spawn. Enforces a process-level kill switch.

Implemented in Phase 1.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator, Callable, Deque, Dict, List, Optional

from src.nadobro.engine.executor_base import Executor, ExecutorFailed, TradeRecorder
from src.nadobro.engine.risk import ExecutorRequest, RiskEngine
from src.nadobro.engine.types import CloseType, RiskState
from src.nadobro.utils.env import env_int

if TYPE_CHECKING:
    from src.nadobro.engine.controllers.controller_base import Controller

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    return max(1, env_int(name, default))


# BUG-TICK-1 fix: a controller used to be marked FAILED (terminal, no recovery)
# on ANY exception from on_tick — including transient venue hiccups such as a
# short-lived ip_query_only downgrade or a "Too Many Requests" rate-limit. That
# turned a recoverable blip into a permanent silent stall (ticks no-op forever
# while is_running() stays True off a stale executor row). These markers let us
# keep the controller ACTIVE across transient errors and only fail it on a
# genuinely fatal error or after the transient streak is exhausted.
_TRANSIENT_ERROR_MARKERS = (
    "ipqueryonly",
    "toomanyrequests",
    "too many requests",
    "ratelimit",
    "rate limit",
    "rate limited",
    "timeout",
    "timed out",
    "temporarily",
    "serviceunavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connectionerror",
    "429",
    "502",
    "503",
    "504",
)

# After this many consecutive transient failures we give up and mark the
# controller FAILED (the venue problem is no longer "transient").
DEFAULT_CONTROLLER_MAX_TRANSIENT_FAILS = _env_int(
    "NADO_CONTROLLER_MAX_TRANSIENT_FAILS", 8
)
# Cap on the exponential backoff window between transient retries (seconds).
DEFAULT_CONTROLLER_BACKOFF_CAP_S = float(
    _env_int("NADO_CONTROLLER_BACKOFF_CAP_S", 90)
)


def _is_transient_error(exc: BaseException) -> bool:
    """True when an on_tick exception looks like a recoverable venue/network
    hiccup rather than a fatal logic error. Conservative: unknown errors are
    treated as fatal so real bugs still surface as FAILED."""
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError)):
        return True
    text = str(exc).lower()
    compact = text.replace("_", "").replace("-", "")
    return any(m in text or m in compact for m in _TRANSIENT_ERROR_MARKERS)


# BUG-ORC-1 fix: cap the orchestrator's in-memory event_log and event queue.
# Previously both were unbounded — long-running deployments leaked memory.
DEFAULT_EVENT_LOG_LIMIT = _env_int("NADO_ORCH_EVENT_LOG_LIMIT", 10000)
DEFAULT_EVENT_QUEUE_LIMIT = _env_int("NADO_ORCH_EVENT_QUEUE_LIMIT", 5000)


@dataclass
class ExecutorEvent:
    kind: str  # spawned | spawn_rejected | stopped | tick | failed | kill_switch
    executor_id: Optional[str] = None
    controller_id: Optional[str] = None
    close_type: Optional[CloseType] = None
    reason: Optional[str] = None
    ts: float = field(default_factory=time.time)


# TERMINATED executors kept per controller after their tick (2026-09-03). The
# orchestrator used to hold every executor a session ever spawned — 1,500+ for a
# re-quoting 20-level ladder within a day — and the runtime persisted ALL of
# them every tick. Controllers read recently terminated executors (fill
# absorption / anchors, order counts), so a bounded window is kept and counts
# are banked on the controller before anything is dropped.
DEFAULT_TERMINATED_RETENTION = _env_int("NADO_TERMINATED_EXECUTOR_RETENTION", 50)


def _order_may_still_rest(ex: Executor) -> bool:
    """True when the executor's venue order might still rest. Such an executor
    is never pruned: the Mid slot logic retries its stop through it (F2b).

    Any known non-terminal ``order`` / ``close_order`` keeps it. A stop that
    ended FAILED keeps it only while an order is not confirmed terminal — once
    the F2b retry confirms the cancel, or when nothing was ever placed, the
    executor becomes prunable (its close_type stays FAILED forever, so that
    alone must not pin it). A FAILED executor with no order handles at all (a
    grid) is kept: nothing proves its levels are clear."""
    handles = ("order", "close_order", "entry_order")
    has_handle = False
    for attr in handles:
        if not hasattr(ex, attr):
            continue
        has_handle = True
        order = getattr(ex, attr, None)
        state = getattr(order, "state", None)
        if order is not None and state is not None and not getattr(state, "is_terminal", True):
            return True
    if getattr(ex, "close_type", None) is CloseType.FAILED:
        # An executor that exposes order handles and has none resting (never
        # placed, or confirmed terminal) holds nothing on the venue. Only an
        # executor without handles (a grid) is kept on FAILED alone.
        return not has_handle
    return False


class ExecutorOrchestrator:
    def __init__(
        self,
        risk_engine: Optional[RiskEngine] = None,
        risk_state_provider: Optional[object] = None,
        *,
        event_log_limit: int = DEFAULT_EVENT_LOG_LIMIT,
        event_queue_limit: int = DEFAULT_EVENT_QUEUE_LIMIT,
        trade_recorder: Optional[TradeRecorder] = None,
    ) -> None:
        self.risk = risk_engine
        # callable(controller_id) -> RiskState; defaults to an empty snapshot
        self.risk_state_provider = risk_state_provider
        # Optional bridge that mirrors engine fills into ``trades_<network>``.
        # Stamped onto every executor at spawn time (the single funnel), so no
        # executor/controller constructor needs to thread it. ``None`` in tests.
        self._trade_recorder = trade_recorder
        self._executors: Dict[str, Executor] = {}
        self._controllers: Dict[str, "Controller"] = {}
        # Bounded queue: when full, oldest events are dropped (with a log).
        self._queue: "asyncio.Queue[ExecutorEvent]" = asyncio.Queue(
            maxsize=max(1, event_queue_limit)
        )
        self._event_queue_limit = max(1, event_queue_limit)
        # event_log is a bounded ring buffer.
        self._event_log_limit = max(1, event_log_limit)
        self._event_log_deque: Deque[ExecutorEvent] = deque(maxlen=self._event_log_limit)
        self._queue_overflows = 0
        # Set by the runtime: an executor this returns True for is NOT pruned
        # yet (its terminal row has not been persisted — a failed batch is
        # retried next tick and must still find the executor).
        self.prune_keep_predicate: Optional[Callable[[Executor], bool]] = None
        self._killed = False
        self._kill_reason: Optional[str] = None
        # BUG-TICK-1: per-controller transient-failure tracking. We keep a
        # controller ACTIVE across transient venue errors (counting them) and
        # apply a short exponential backoff window before the next tick retries,
        # only failing the controller once the streak is exhausted.
        self._controller_fail_counts: Dict[str, int] = {}
        self._controller_backoff_until: Dict[str, float] = {}
        self._controller_max_transient_fails = DEFAULT_CONTROLLER_MAX_TRANSIENT_FAILS
        self._controller_backoff_cap_s = DEFAULT_CONTROLLER_BACKOFF_CAP_S
        # Last spawn-rejection/failure reason per controller, so a controller
        # can report WHY spawn returned False instead of guessing. Cleared on a
        # successful spawn.
        self._last_spawn_reason: Dict[str, str] = {}

    @property
    def event_log(self) -> List[ExecutorEvent]:
        """Snapshot of the bounded event log. Mutating the returned list does
        not affect the orchestrator's internal ring buffer."""
        return list(self._event_log_deque)

    # -- kill switch ------------------------------------------------------
    def kill_switch_on(self, reason: str) -> None:
        self._killed = True
        self._kill_reason = reason
        if self.risk is not None:
            self.risk.kill_switch_on(reason)
        self._emit(ExecutorEvent(kind="kill_switch", reason=reason))

    def kill_switch_off(self) -> None:
        self._killed = False
        self._kill_reason = None
        if self.risk is not None:
            self.risk.kill_switch_off()

    @property
    def is_killed(self) -> bool:
        return self._killed or (self.risk is not None and self.risk.is_killed())

    # -- registry ---------------------------------------------------------
    def list(
        self, controller_id: Optional[str] = None, active_only: bool = False
    ) -> List[Executor]:
        vals = list(self._executors.values())
        if controller_id is not None:
            vals = [e for e in vals if e.controller_id == controller_id]
        if active_only:
            vals = [e for e in vals if not e.is_terminated]
        return vals

    def get(self, executor_id: str) -> Optional[Executor]:
        return self._executors.get(executor_id)

    def _state_for(self, controller_id: str) -> RiskState:
        if callable(self.risk_state_provider):
            state = self.risk_state_provider(controller_id)
        else:
            state = RiskState()
        # BUG-RISK-1 fix: roll over daily counters on a UTC date boundary so
        # yesterday's daily-pnl floor / cost-cap doesn't keep the gate armed
        # into a new trading day.
        today = time.strftime("%Y-%m-%d", time.gmtime())
        state = state.rolled_over(today)
        state.executor_count = len(self.list(controller_id, active_only=True))
        return state

    # -- lifecycle --------------------------------------------------------
    async def spawn(
        self, executor: Executor, request: Optional[ExecutorRequest] = None
    ) -> bool:
        if self.is_killed:
            self._last_spawn_reason[executor.controller_id] = "kill_switch"
            self._emit(
                ExecutorEvent(
                    kind="spawn_rejected",
                    executor_id=executor.id,
                    controller_id=executor.controller_id,
                    reason="kill_switch",
                )
            )
            return False
        if self.risk is not None and request is not None:
            state = self._state_for(executor.controller_id)
            ok, reason = self.risk.pre_executor_check(
                executor.controller_id, request, state
            )
            if not ok:
                self._last_spawn_reason[executor.controller_id] = f"risk:{reason}"
                self._emit(
                    ExecutorEvent(
                        kind="spawn_rejected",
                        executor_id=executor.id,
                        controller_id=executor.controller_id,
                        reason=reason,
                    )
                )
                return False
        self._executors[executor.id] = executor
        # Stamp the trade recorder before on_create() — the entry order can
        # fill synchronously inside on_create(), and that first fill must be
        # recorded too.
        if self._trade_recorder is not None and getattr(executor, "trade_recorder", None) is None:
            executor.trade_recorder = self._trade_recorder
        try:
            await executor.on_create()
        except ExecutorFailed as exc:
            self._last_spawn_reason[executor.controller_id] = f"executor_failed:{exc}"
            self._emit(
                ExecutorEvent(
                    kind="failed",
                    executor_id=executor.id,
                    controller_id=executor.controller_id,
                    close_type=CloseType.FAILED,
                    reason=str(exc),
                )
            )
            return False
        self._last_spawn_reason.pop(executor.controller_id, None)
        self._emit(
            ExecutorEvent(
                kind="spawned",
                executor_id=executor.id,
                controller_id=executor.controller_id,
            )
        )
        return True

    def last_spawn_reason(self, controller_id: str) -> Optional[str]:
        """Why the most recent :meth:`spawn` for this controller returned False
        (``kill_switch`` / ``risk:<reason>`` / ``executor_failed:<detail>``), or
        ``None`` if the last spawn succeeded."""
        return self._last_spawn_reason.get(controller_id)

    async def tick(self, executor_id: str) -> None:
        ex = self._executors.get(executor_id)
        if ex is None or ex.is_terminated:
            return
        try:
            await ex.on_tick()
        except ExecutorFailed as exc:
            self._emit(
                ExecutorEvent(
                    kind="failed",
                    executor_id=ex.id,
                    controller_id=ex.controller_id,
                    close_type=CloseType.FAILED,
                    reason=str(exc),
                )
            )
            return
        self._emit(
            ExecutorEvent(kind="tick", executor_id=ex.id, controller_id=ex.controller_id)
        )

    async def stop(
        self, executor_id: str, close_type: CloseType = CloseType.EARLY_STOP
    ) -> bool:
        ex = self._executors.get(executor_id)
        if ex is None:
            return False
        await ex.on_stop(close_type)
        self._emit(
            ExecutorEvent(
                kind="stopped",
                executor_id=ex.id,
                controller_id=ex.controller_id,
                close_type=ex.close_type,
            )
        )
        return True

    async def adopt(self, executor: Executor, order: object) -> bool:
        """Register an executor around an order ALREADY placed atomically by a
        ``cancel_and_place`` (see the MM controller's fused requote).

        No risk pre-check and no venue call: the placement is a fait accompli
        and size-neutral — it replaced an equal-notional resting quote — so this
        only wires the bookkeeping (and NEVER opens a second order, which is the
        whole point of the fused path). Falls back to a plain register if the
        executor type has no ``adopt_order``.

        It does NOT honour the kill switch: the order is ALREADY resting on the
        venue, so refusing to register it could only orphan it (an untracked
        resting order that teardown cannot find). Tracking it means a killed
        controller's stop sweep will cancel it like any other executor.
        """
        self._executors[executor.id] = executor
        if self._trade_recorder is not None and getattr(executor, "trade_recorder", None) is None:
            executor.trade_recorder = self._trade_recorder
        adopt_order = getattr(executor, "adopt_order", None)
        if callable(adopt_order):
            adopt_order(order)
        self._last_spawn_reason.pop(executor.controller_id, None)
        self._emit(
            ExecutorEvent(
                kind="spawned",
                executor_id=executor.id,
                controller_id=executor.controller_id,
            )
        )
        return True

    async def settle_replaced(self, executor_id: str) -> bool:
        """Terminate an executor whose resting order was cancelled EXTERNALLY by
        an atomic ``cancel_and_place``. Captures any racing fill but issues NO
        venue cancel — the cancel already happened as part of the fused request,
        so a second one would waste execute budget and could race a fresh order.
        """
        ex = self._executors.get(executor_id)
        if ex is None:
            return False
        settle = getattr(ex, "settle_after_external_cancel", None)
        if callable(settle):
            await settle()
        else:
            ex._terminate(CloseType.EARLY_STOP)
        self._emit(
            ExecutorEvent(
                kind="stopped",
                executor_id=ex.id,
                controller_id=ex.controller_id,
                close_type=ex.close_type,
            )
        )
        return True

    # -- controller management -------------------------------------------
    async def spawn_controller(self, controller: "Controller") -> bool:
        if self.is_killed:
            self._emit(ExecutorEvent(kind="controller_rejected", controller_id=controller.id, reason="kill_switch"))
            return False
        # BUG-CC-2 fix: register first so the controller can spawn child
        # executors during on_start (their controller_id resolves via the
        # registry), but if on_start raises, tear down any child executors
        # the controller managed to spawn before bailing — otherwise the
        # failed controller leaves live orders on the venue with no owner.
        self._controllers[controller.id] = controller
        try:
            await controller.on_start()
        except Exception as exc:  # noqa: BLE001
            controller._set_failed()
            # Record why so callers (EngineRuntime.start → run_engine_cycle) can
            # surface a clear reason instead of a silent "LIVE but 0 orders".
            controller._start_error = str(exc)  # type: ignore[attr-defined]
            # Stop any child executors the controller created mid-start.
            child_ids = [
                ex.id for ex in self.list(controller.id, active_only=True)
            ]
            if child_ids:
                try:
                    await asyncio.gather(
                        *(self.stop(eid, CloseType.FAILED) for eid in child_ids),
                        return_exceptions=True,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "controller %s on_start rollback: child stop failed",
                        controller.id, exc_info=True,
                    )
            self._emit(ExecutorEvent(kind="controller_failed", controller_id=controller.id, reason=str(exc)))
            return False
        controller._set_active()
        self._emit(ExecutorEvent(kind="controller_spawned", controller_id=controller.id))
        return True

    async def tick_controller(self, controller_id: str) -> None:
        controller = self._controllers.get(controller_id)
        if controller is None or not controller.is_active:
            return
        # BUG-TICK-1: honor an active transient-failure backoff window so we
        # don't hammer a saturated/rate-limited venue on every scheduled tick.
        backoff_until = self._controller_backoff_until.get(controller_id, 0.0)
        if backoff_until and time.time() < backoff_until:
            self._emit(ExecutorEvent(
                kind="controller_skipped", controller_id=controller_id,
                reason="transient_backoff",
            ))
            return
        if self.risk is not None:
            ok, reason = self.risk.pre_tick_check(controller_id, self._state_for(controller_id))
            if not ok:
                self._emit(ExecutorEvent(kind="controller_skipped", controller_id=controller_id, reason=reason))
                return
        # Executors already terminated BEFORE this tick: the controller gets this
        # tick to read them (fills, anchors, counts); after it they are prunable.
        terminated_before = {e.id for e in self.list(controller_id) if e.is_terminated}
        try:
            await controller.on_tick()
        except Exception as exc:  # noqa: BLE001
            self._handle_controller_tick_error(controller, controller_id, exc)
            return
        # Success: clear any transient-failure state and resume normally.
        self._controller_fail_counts.pop(controller_id, None)
        self._controller_backoff_until.pop(controller_id, None)
        self._emit(ExecutorEvent(kind="controller_tick", controller_id=controller_id))
        self.prune_terminated(controller_id, eligible=terminated_before)

    def prune_terminated(
        self,
        controller_id: str,
        *,
        eligible: Optional[set] = None,
        keep: Optional[int] = None,
    ) -> int:
        """Drop TERMINATED executors of ``controller_id`` beyond the newest
        ``keep`` (default ``NADO_TERMINATED_EXECUTOR_RETENTION``), banking their
        order counts on the controller first. Only executors in ``eligible``
        (terminated before the tick that just ran) are dropped, never one whose
        order may still rest. Returns the number pruned."""
        keep = DEFAULT_TERMINATED_RETENTION if keep is None else max(0, int(keep))
        terminated = [e for e in self.list(controller_id) if e.is_terminated]
        if len(terminated) <= keep:
            return 0
        terminated.sort(key=lambda e: float(getattr(e, "terminated_at", 0.0) or 0.0), reverse=True)
        controller = self._controllers.get(controller_id)
        bank = getattr(controller, "bank_executor_counts", None)
        pruned = 0
        for ex in terminated[keep:]:
            if eligible is not None and ex.id not in eligible:
                continue
            if _order_may_still_rest(ex):
                continue
            keep_pred = self.prune_keep_predicate
            if keep_pred is not None:
                try:
                    if keep_pred(ex):
                        continue
                except Exception:  # noqa: BLE001 - a predicate error must not break the tick
                    continue
            if callable(bank):
                try:
                    bank(ex)
                except Exception:  # noqa: BLE001 - telemetry only; never lose the prune
                    logger.warning("bank_executor_counts failed for %s", ex.id, exc_info=True)
            self._executors.pop(ex.id, None)
            pruned += 1
        if pruned:
            logger.debug("pruned %d terminated executor(s) for %s (kept %d)", pruned, controller_id, keep)
        return pruned

    def _handle_controller_tick_error(
        self, controller: "Controller", controller_id: str, exc: Exception
    ) -> None:
        """BUG-TICK-1: classify an on_tick error. Transient venue/network
        errors keep the controller ACTIVE (with bounded exponential backoff)
        so a recoverable blip no longer permanently stalls the strategy; only
        a fatal error, or an exhausted transient streak, marks it FAILED."""
        if _is_transient_error(exc):
            count = self._controller_fail_counts.get(controller_id, 0) + 1
            self._controller_fail_counts[controller_id] = count
            if count >= self._controller_max_transient_fails:
                self._controller_backoff_until.pop(controller_id, None)
                self._controller_fail_counts.pop(controller_id, None)
                controller._set_failed()
                logger.error(
                    "controller %s FAILED after %d consecutive transient errors; "
                    "last: %s", controller_id, count, exc,
                )
                self._emit(ExecutorEvent(
                    kind="controller_failed", controller_id=controller_id,
                    reason=f"transient_exhausted({count}): {exc}",
                ))
                return
            # Exponential backoff (1.5s, 3s, 6s, ... capped), with jitter.
            backoff = min(
                self._controller_backoff_cap_s,
                1.5 * (2 ** (count - 1)),
            )
            self._controller_backoff_until[controller_id] = time.time() + backoff
            logger.warning(
                "controller %s transient error (%d/%d), backing off %.1fs: %s",
                controller_id, count, self._controller_max_transient_fails,
                backoff, exc,
            )
            self._emit(ExecutorEvent(
                kind="controller_degraded", controller_id=controller_id,
                reason=f"transient({count}/{self._controller_max_transient_fails}): {exc}",
            ))
            return
        # Fatal / unrecognized error: fail fast so real bugs surface.
        self._controller_backoff_until.pop(controller_id, None)
        self._controller_fail_counts.pop(controller_id, None)
        controller._set_failed()
        logger.error("controller %s FAILED (fatal): %s", controller_id, exc)
        self._emit(ExecutorEvent(
            kind="controller_failed", controller_id=controller_id, reason=str(exc),
        ))

    def list_controllers(self, user_id: Optional[int] = None) -> List["Controller"]:
        vals = list(self._controllers.values())
        if user_id is not None:
            vals = [c for c in vals if c.user_id == user_id]
        return vals

    def get_controller_status(self, controller_id: str) -> Optional[Dict[str, object]]:
        controller = self._controllers.get(controller_id)
        if controller is None:
            return None
        return {
            "id": controller.id,
            "name": controller.name,
            "user_id": controller.user_id,
            "state": controller.state.value,
            "open_executors": len(self.list(controller_id, active_only=True)),
        }

    async def stop_controller(
        self,
        controller_id: str,
        close_type: CloseType = CloseType.EARLY_STOP,
        reason: str = "stopped",
    ) -> int:
        """Stop a controller (if registered) and batch-cancel all of its
        active executors concurrently. Returns the executors stopped."""
        controller = self._controllers.get(controller_id)
        if controller is not None:
            try:
                await controller.on_stop(reason)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "controller %s on_stop hook failed (continuing with executor cancel): %s",
                    controller_id, exc,
                )
            controller._set_stopped()
        targets = self.list(controller_id, active_only=True)
        await asyncio.gather(*(self.stop(e.id, close_type) for e in targets))
        self._emit(ExecutorEvent(kind="controller_stopped", controller_id=controller_id, reason=reason))
        return len(targets)

    # -- events -----------------------------------------------------------
    def _emit(self, event: ExecutorEvent) -> None:
        # Ring buffer (deque maxlen) drops oldest automatically.
        self._event_log_deque.append(event)
        # Queue is bounded; on overflow, drop the oldest queued event so the
        # newest one can fit. This preserves a bounded *recent* window for
        # consumers without blocking emitters.
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                # Queue still full (raced with another producer). Drop the
                # new event and continue; the event_log retains a copy.
                pass
            self._queue_overflows += 1
            if self._queue_overflows % 100 == 1:
                logger.warning(
                    "orchestrator event queue overflow (count=%d, cap=%d) — "
                    "downstream consumer is too slow",
                    self._queue_overflows, self._event_queue_limit,
                )

    async def events(self) -> AsyncIterator[ExecutorEvent]:
        while True:
            yield await self._queue.get()

    def drain_events(self) -> List[ExecutorEvent]:
        """Non-blocking snapshot-and-clear of the queued events (test helper)."""
        out: List[ExecutorEvent] = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out

    @property
    def queue_overflow_count(self) -> int:
        return self._queue_overflows
