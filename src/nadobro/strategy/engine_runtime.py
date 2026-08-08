"""Engine v2 runtime connector (production hardening A).

Wires the engine into a runnable whole: constructs the ExecutorOrchestrator
(with a real RiskEngine + DB-backed kill switch), the NadoAdapter, the
DB-backed inventory, and the strategy Controllers — then drives them and
persists executor lifecycle rows.

This is the integration point bot_runtime's async loop calls (via
``services/strategy_runtime``): start a controller for a running strategy,
tick it each cycle, persist state. The engine library stays DB/venue-agnostic;
all the real wiring lives here.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

from src.nadobro.engine.adapter.base import NadoAdapterBase
from src.nadobro.engine.controllers.controller_base import Controller, ControllerState
from src.nadobro.engine.controllers.delta_neutral import DeltaNeutralController
from src.nadobro.engine.controllers.desk import DeskController
from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.controllers.grid_trading import GridController
from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController
from src.nadobro.engine.controllers.rgrid import RGridController
from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.controllers.volume_bot import VolumeBotController
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.risk import RiskEngine
# Re-exported for the handlers layer (handlers may import strategy but not
# engine — see tests/lint/test_architecture_layers.py): the human-readable
# quote-gate pause reasons rendered on /status and in gate notifications.
from src.nadobro.engine.routines.regime_gate import GATE_REASON_HUMAN as GATE_REASON_HUMAN  # noqa: F401
from src.nadobro.engine.types import RiskLimits, RiskState, TradeType, TripleBarrierConfig, _dec
from src.nadobro.utils.env import env_float

logger = logging.getLogger(__name__)

# Strategy id (bot_runtime's keys) -> engine controller class.
CONTROLLER_REGISTRY: Dict[str, type] = {
    "grid": GridController,
    # rgrid = Reverse Grid: its OWN controller and its own maker executor
    # (engine/controllers/rgrid.py + executors/rgrid_maker_executor.py). Both legs
    # REST post-only off the average of the buy and sell exposure prices — the bid
    # above it, the ask below — so a break fills them without paying the spread.
    # The one order that crosses is the armed trailing stop (MARKET, reduce-only),
    # which has to act where a post-only order cannot rest. It does NOT switch
    # between grid and reverse-grid phases;
    # that switcher is D-Grid, and running rgrid on it (the pre-2026-08-06 wiring)
    # is what produced "Reverse GRID switched RGRID → GRID … now quoting the LONG
    # ladder" on users' R-Grid sessions.
    "rgrid": RGridController,
    "dgrid": DynamicGridController,
    "mid": MarketMakingController,
    "dn": DeltaNeutralController,
    "vol": VolumeBotController,
    # NOTE: live copy trading is trading/copy_service.py (poll-based), not an
    # engine controller. The old CopyController was unreachable (never in
    # ENGINE_MAPPED_STRATEGIES) and was removed with its tests.
    # Desk text-to-trade plans. NOT in ENGINE_MAPPED_STRATEGIES — desk
    # sessions are driven by services/desk_runtime's scheduler job, not by
    # bot_runtime strategy cycles.
    "desk": DeskController,
}


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------
def build_adapter(
    client: object, products: Dict[str, object], on_place: Optional[Any] = None
) -> NadoAdapterBase:
    """Construct the live Nado adapter from a NadoClient + product-metadata map.
    ``products`` maps trading_pair -> ProductMeta (see adapter/nado.py).
    ``on_place`` (optional) is called with each placed digest for session linking."""
    from src.nadobro.engine.adapter.nado import NadoAdapter

    return NadoAdapter(client, products, on_place=on_place)  # type: ignore[arg-type]


def build_risk_engine(limits: Optional[RiskLimits] = None) -> RiskEngine:
    from src.nadobro.trading.engine_persistence import DbKillSwitchStore

    return RiskEngine(limits or RiskLimits(), kill_switch=DbKillSwitchStore())


def build_orchestrator(
    *,
    limits: Optional[RiskLimits] = None,
    risk_state_provider: Optional[Any] = None,
    trade_recorder: Optional[object] = None,
) -> ExecutorOrchestrator:
    return ExecutorOrchestrator(
        risk_engine=build_risk_engine(limits),
        risk_state_provider=risk_state_provider or (lambda _cid: RiskState()),
        trade_recorder=trade_recorder,
    )


def deterministic_controller_id(strategy: str, user_id: int, network: str) -> str:
    """Stable, cross-process controller id. BUG-ER-2 fix: with this, a second
    worker that tries to start the same strategy hits the engine_executors
    table's existing rows and we can detect the duplicate via
    ``_remote_active(...)``.
    """
    return f"{strategy}:{int(user_id)}:{str(network)}"


def build_controller(
    strategy: str,
    *,
    user_id: int,
    configs: Dict[str, object],
    orchestrator: ExecutorOrchestrator,
    adapter: NadoAdapterBase,
    inventory: object,
    limits: Optional[RiskLimits] = None,
    controller_id: Optional[str] = None,
) -> Controller:
    cls = CONTROLLER_REGISTRY.get(strategy)
    # Grid opt-in: fill-anchored maker quoting replaces the classic ladder when the
    # user enables ``fill_anchored``. rgrid is NEVER overridden — Reverse Grid has
    # exactly one engine, and the registry already points at it.
    if strategy != "rgrid" and configs.get("controller_override") == "fill_anchored":
        cls = FillAnchoredQuotingController
    if cls is None:
        raise ValueError(f"no engine controller for strategy '{strategy}'")
    return cls(
        user_id=user_id, orchestrator=orchestrator, adapter=adapter, inventory=inventory,
        configs=configs, limits=limits, controller_id=controller_id,
    )


# --------------------------------------------------------------------------
# runtime manager
# --------------------------------------------------------------------------
class EngineRuntime:
    """Owns live controllers keyed by (user_id, network, strategy). Driven from
    bot_runtime's async loop: ``start`` once, ``tick`` each cycle, ``stop`` on
    teardown. Persists executor lifecycle rows after each tick."""

    def __init__(
        self,
        *,
        executor_store: Optional[object] = None,
        trade_recorder: Optional[object] = None,
    ) -> None:
        self._controllers: Dict[tuple, Controller] = {}
        self._orchestrators: Dict[tuple, ExecutorOrchestrator] = {}
        self._executor_store = executor_store
        self._trade_recorder = trade_recorder

    def _key(self, user_id: int, network: str, strategy: str) -> tuple:
        return (user_id, network, strategy)

    def is_running(self, user_id: int, network: str, strategy: str) -> bool:
        c = self._controllers.get(self._key(user_id, network, strategy))
        if c is not None and c.is_active:
            return True
        # BUG-ER-2 fix: cross-process visibility. Another worker process may
        # have started this strategy; check engine_executors under the
        # deterministic controller id. SCOPED to the CURRENT run's session id
        # (NO-ORDERS fix): controller_id is stable across runs, so a prior run's
        # non-terminated row would otherwise make this return True forever and
        # the build/tick gate would skip building — strategy never places an
        # order. Scoping to the active session means only THIS run's executors
        # count, and stale rows from dead runs are ignored.
        from src.nadobro.trading.engine_persistence import resolve_running_session_id
        session_id = resolve_running_session_id(strategy, user_id, network)
        return _remote_active(strategy, user_id, network, session_id)

    def has_local_active(self, user_id: int, network: str, strategy: str) -> bool:
        """True only when THIS process holds a live (active) controller for the
        strategy — the precondition for ``tick`` to do anything. Distinct from
        ``is_running`` (which also trusts cross-process engine_executors rows)."""
        c = self._controllers.get(self._key(user_id, network, strategy))
        return c is not None and c.is_active

    def needs_recovery(self, user_id: int, network: str, strategy: str) -> bool:
        """BUG-TICK-1 recovery: True when a locally-registered controller has
        entered the terminal FAILED state. Such a controller will never tick
        again (``is_active`` is permanently False), so the engine cycle must
        tear it down and rebuild a fresh one instead of silently no-op'ing
        forever — the exact silent-stall this audit chased."""
        c = self._controllers.get(self._key(user_id, network, strategy))
        return c is not None and c.state is ControllerState.FAILED

    async def start(
        self,
        user_id: int,
        network: str,
        strategy: str,
        configs: Dict[str, object],
        adapter: NadoAdapterBase,
        inventory: object,
        *,
        limits: Optional[RiskLimits] = None,
        risk_state_provider: Optional[Any] = None,
    ) -> Controller:
        # BUG-ER-1 fix: if a previous instance is still registered (e.g. the
        # controller failed mid-tick or the user restarted the strategy
        # without an explicit stop), tear it down BEFORE replacing it with a
        # fresh orchestrator/controller. Otherwise the old orchestrator's
        # active executors keep ticking against the venue with no owner.
        key = self._key(user_id, network, strategy)
        if key in self._controllers or key in self._orchestrators:
            try:
                await self.stop(user_id, network, strategy)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "stale engine runtime cleanup failed for %s; replacing anyway",
                    key, exc_info=True,
                )

        # Fresh run = clean inventory. controller_id is stable across runs, so a
        # new session must NOT inherit the prior run's engine_position_hold (it
        # would skew exposure caps / sizing). PnL is already session-scoped via
        # trades_<network>; this keeps the engine's own position view per-run.
        cid = deterministic_controller_id(strategy, user_id, network)
        clear_inv = getattr(inventory, "clear_for_controller", None)
        if callable(clear_inv):
            try:
                clear_inv(cid)
            except Exception:  # noqa: BLE001 - best-effort; never block start
                logger.debug("inventory clear_for_controller failed for %s", cid, exc_info=True)

        orch = build_orchestrator(
            limits=limits,
            risk_state_provider=risk_state_provider,
            trade_recorder=self._trade_recorder,
        )
        controller = build_controller(
            strategy, user_id=user_id, configs=configs, orchestrator=orch,
            adapter=adapter, inventory=inventory, limits=limits,
            controller_id=deterministic_controller_id(strategy, user_id, network),
        )
        self._orchestrators[key] = orch
        self._controllers[key] = controller
        try:
            spawned = await orch.spawn_controller(controller)
        except Exception:
            # If spawn_controller raised, roll back our bookkeeping so a
            # retry can start clean rather than crashing on the duplicate
            # registration check above.
            self._orchestrators.pop(key, None)
            self._controllers.pop(key, None)
            raise
        if not spawned:
            # Controller refused to start (kill switch / risk gate). Roll
            # back so the dispatch result clearly says "not running".
            self._orchestrators.pop(key, None)
            self._controllers.pop(key, None)
        return controller

    async def tick(self, user_id: int, network: str, strategy: str) -> None:
        key = self._key(user_id, network, strategy)
        controller = self._controllers.get(key)
        orch = self._orchestrators.get(key)
        if controller is None or orch is None:
            return
        await orch.tick_controller(controller.id)
        self._persist_executors(orch)

    async def stop(self, user_id: int, network: str, strategy: str) -> None:
        key = self._key(user_id, network, strategy)
        orch = self._orchestrators.get(key)
        controller = self._controllers.get(key)
        cid = deterministic_controller_id(strategy, user_id, network)
        if orch is not None and controller is not None:
            await orch.stop_controller(controller.id)
            self._persist_executors(orch)
        else:
            # Cross-process stop: this process doesn't own the orchestrator, so
            # mark the controller's non-terminated engine_executors rows
            # TERMINATED in the DB. Without this a stop handled outside the owner
            # process leaves stale ACTIVE rows that _remote_active would treat as
            # "still running" — blocking the next run from ever building.
            try:
                from src.nadobro.trading.engine_persistence import terminate_engine_executors
                terminate_engine_executors(cid)
            except Exception:  # noqa: BLE001
                logger.debug("cross-process executor terminate sweep failed for %s", cid, exc_info=True)
        self._controllers.pop(key, None)
        self._orchestrators.pop(key, None)
        # Clear the live-progress row so a stopped strategy doesn't leave stale
        # cycles/funding behind for the next start. Best-effort.
        try:
            from src.nadobro.trading.engine_persistence import clear_controller_progress

            clear_controller_progress(deterministic_controller_id(strategy, user_id, network))
        except Exception:  # noqa: BLE001
            logger.debug("clear dn progress failed", exc_info=True)

    def _persist_executors(self, orch: ExecutorOrchestrator) -> None:
        if self._executor_store is None:
            return
        for ex in orch.list():
            try:
                self._executor_store.save(ex)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - persistence must not break a tick
                logger.warning("executor persistence failed for %s", ex.id, exc_info=True)


def _should_build_controller(
    *, needs_recovery: bool, has_local_active: bool, worker_mode: bool, is_running: bool
) -> bool:
    """Decide whether THIS process must (re)build the controller before ticking.

    - A locally-FAILED controller always rebuilds (recovery).
    - With a live LOCAL controller, never rebuild (just tick).
    - With NO local controller: the cycle-running worker ADOPTS (builds) so a
      crashed/recycled worker doesn't no-op forever against a stale remote row;
      a non-worker (e.g. the scheduler's local fallback) only builds when no
      live owner exists (``not is_running``), so it never double-builds.
    """
    if needs_recovery:
        return True
    if has_local_active:
        return False
    return worker_mode or not is_running


def _fingerprint_value(value: object) -> object:
    """Comparable representation for config/risk objects.

    Decimal/dataclass/Enum instances do not always compare predictably once
    nested inside generic dicts, and callables such as candle providers must not
    participate in live-config equality.
    """
    if callable(value):
        return "<callable>"
    if isinstance(value, Decimal):
        return ("Decimal", str(value))
    if isinstance(value, Enum):
        return ("Enum", value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value).__name__,
            tuple(
                (field.name, _fingerprint_value(getattr(value, field.name)))
                for field in fields(value)
            ),
        )
    if isinstance(value, dict):
        return tuple(
            (str(k), _fingerprint_value(v))
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
            if not callable(v)
        )
    if isinstance(value, (list, tuple)):
        return tuple(_fingerprint_value(v) for v in value)
    return value


def _live_config_signature(
    configs: Dict[str, object], limits: Optional[RiskLimits] = None
) -> tuple:
    """Stable signature for parameters that should change a live controller.

    Excludes mid-derived price anchors so ordinary market movement does not
    churn a controller. Includes risk limits because leverage/notional changes
    must also update the runtime risk engine before the next order is spawned.
    """
    cfg_sig = tuple(
        (str(k), _fingerprint_value(v))
        for k, v in sorted(configs.items(), key=lambda item: str(item[0]))
        if k not in _LIVE_CONFIG_SIGNATURE_EXCLUDE and not callable(v)
    )
    return (cfg_sig, _fingerprint_value(limits) if limits is not None else None)


def _grid_bounds_for_side(configs: Dict[str, object], side: TradeType, mid: object) -> Dict[str, object]:
    """Rebuild side-correct grid bounds from stable step/level knobs.

    This mirrors the mapping/recenter math while keeping the helper local to the
    runtime reconfig path, where the live executor side is known.
    """
    mid_dec = _dec(mid)
    if mid_dec <= 0:
        return {}
    step = _dec(configs.get("step_pct") or configs.get("min_spread_between_orders") or 0)
    levels = int(configs.get("levels_count") or configs.get("max_open_orders") or 0)
    if step <= 0 or levels < 1:
        return {}
    span = step * Decimal(max(levels - 1, 1))
    maker_offset = max(step / Decimal(2), Decimal("0.00015"))
    if side is TradeType.SELL:
        return {
            "start_price": mid_dec * (Decimal(1) + maker_offset),
            "end_price": mid_dec * (Decimal(1) + maker_offset + span),
            "limit_price": Decimal(0),
        }
    return {
        "start_price": mid_dec * (Decimal(1) - maker_offset - span),
        "end_price": mid_dec * (Decimal(1) - maker_offset),
        "limit_price": Decimal(0),
    }


def _apply_mid_controller_config(controller: Controller, configs: Dict[str, object]) -> None:
    """Refresh MarketMakingController attrs read only at __init__."""
    controller.trading_pair = str(configs["trading_pair"])  # type: ignore[attr-defined]
    controller.spread_bid_pct = max(  # type: ignore[attr-defined]
        _dec(configs.get("spread_bid_pct", "0.001")),
        _dec(configs.get("spread_floor_half_pct", "0.00015")),
    )
    controller.spread_ask_pct = max(  # type: ignore[attr-defined]
        _dec(configs.get("spread_ask_pct", "0.001")),
        _dec(configs.get("spread_floor_half_pct", "0.00015")),
    )
    controller.order_amount_quote = _dec(configs.get("order_amount_quote", "10"))  # type: ignore[attr-defined]
    controller.price_distance_tolerance = _dec(  # type: ignore[attr-defined]
        configs.get("price_distance_tolerance", "0.0005")
    )
    max_base = configs.get("max_base_quote")
    min_base = configs.get("min_base_quote")
    controller.max_base_quote = _dec(max_base) if max_base is not None else None  # type: ignore[attr-defined]
    controller.min_base_quote = _dec(min_base) if min_base is not None else None  # type: ignore[attr-defined]
    controller.profit_protection = bool(configs.get("profit_protection", False))  # type: ignore[attr-defined]
    controller.auto_spread = bool(configs.get("auto_spread", False))  # type: ignore[attr-defined]
    controller.auto_spread_k = _dec(configs.get("auto_spread_k", "1.5"))  # type: ignore[attr-defined]
    controller.spread_floor_half_pct = _dec(configs.get("spread_floor_half_pct", "0.00015"))  # type: ignore[attr-defined]
    controller.spread_cap_half_pct = _dec(configs.get("spread_cap_half_pct", "0.005"))  # type: ignore[attr-defined]
    # Live edits to Mid Mode directional bias take effect on the next tick.
    from src.nadobro.engine.controllers.market_making import _safe_bias, _safe_levels
    controller.directional_bias = _safe_bias(configs.get("directional_bias", "0"))  # type: ignore[attr-defined]
    # AUDIT-MM-2026-07-14 #5: Turbo tapped on a RUNNING mid must apply touch
    # quoting too — quote_mode was cached at __init__ only, so the live-apply
    # path delivered a half-applied preset (10x sizing at mid±spread pricing)
    # until a full stop/start.
    controller.quote_mode = str(configs.get("quote_mode", "mid") or "mid").lower()  # type: ignore[attr-defined]
    # Ladder + queue-preservation knobs are read at __init__ too; refresh them so
    # a live edit of levels/curve/spacing actually reaches the next tick instead
    # of silently no-opping until a full stop/start.
    controller.ladder_levels = _safe_levels(configs.get("ladder_levels", 1))  # type: ignore[attr-defined]
    controller.ladder_step_bp = _dec(configs.get("ladder_step_bp", "0") or "0")  # type: ignore[attr-defined]
    controller.ladder_curve = str(configs.get("ladder_curve", "flat") or "flat").lower()  # type: ignore[attr-defined]
    controller.min_quote_lifetime_s = float(_dec(configs.get("min_quote_lifetime_s", "0") or "0"))  # type: ignore[attr-defined]


def _apply_orchestrator_risk_limits(orch: ExecutorOrchestrator, limits: RiskLimits) -> None:
    """Refresh the live RiskEngine limits on the orchestrator.

    ExecutorOrchestrator exposes the engine as ``risk`` in production. Keep the
    legacy/test ``risk_engine`` spelling as a fallback so lightweight fakes still
    exercise the same path.
    """
    for attr in ("risk", "risk_engine"):
        risk_engine = getattr(orch, attr, None)
        if risk_engine is not None:
            try:
                risk_engine.limits = limits
            except Exception:  # noqa: BLE001 - bad fakes must not break a tick
                logger.debug("risk limit refresh failed via orchestrator.%s", attr, exc_info=True)


async def _reset_mm_quotes(controller: Controller, orch: ExecutorOrchestrator) -> None:
    """Forget/stop current MM-style quotes so the next tick uses new sizing."""
    # Ladder-aware path: walking only the level-0 attributes below would strand
    # levels 1..N as orphan resting orders on every live re-size.
    stop_all = getattr(controller, "stop_all_quotes", None)
    if callable(stop_all):
        await stop_all()
        return
    for attr_id, attr_price in (("_bid_id", "_bid_price"), ("_ask_id", "_ask_price")):
        ex_id = getattr(controller, attr_id, None)
        if ex_id is not None:
            await orch.stop(ex_id)
            setattr(controller, attr_id, None)
            setattr(controller, attr_price, None)


def _apply_dgrid_controller_config(controller: Controller, configs: Dict[str, object]) -> None:
    """Refresh DynamicGridController attrs that were read only at __init__.

    Every one of these is a user-facing button (the Regime card: variance-ratio
    thresholds, windows, drift, flip debounce, TP scale-out). They were captured
    once at build time, so editing any of them mid-session changed the card and
    nothing else until the user stopped and restarted the strategy — a silent
    no-op on controls that look live.

    The phase / confirm-streak RUNTIME state is deliberately NOT reset: clearing
    the streak on every settings write would let a user poking at buttons defer a
    pending flip indefinitely.
    """
    def _num(key: str, current: object, cast=float):
        raw = configs.get(key)
        if raw is None:
            return current
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return current

    controller.short_window = _num("dgrid_short_window", controller.short_window, int)  # type: ignore[attr-defined]
    controller.long_window = _num("dgrid_long_window", controller.long_window, int)  # type: ignore[attr-defined]
    controller.trend_on_vr = _num("dgrid_trend_on_vr", controller.trend_on_vr)  # type: ignore[attr-defined]
    controller.range_on_vr = _num("dgrid_range_on_vr", controller.range_on_vr)  # type: ignore[attr-defined]
    controller.trend_drift_pct = _num("dgrid_trend_drift_pct", controller.trend_drift_pct)  # type: ignore[attr-defined]
    controller.flip_confirm_ticks = _num("dgrid_flip_confirm_ticks", controller.flip_confirm_ticks, int)  # type: ignore[attr-defined]
    controller.trail_arm_pct = _num("dgrid_trail_arm_pct", controller.trail_arm_pct)  # type: ignore[attr-defined]
    controller.trail_giveback_pct = _num("dgrid_trail_giveback_pct", controller.trail_giveback_pct)  # type: ignore[attr-defined]
    controller.reversal_flip_pct = _num("dgrid_reversal_flip_pct", controller.reversal_flip_pct)  # type: ignore[attr-defined]
    controller.tp_fraction = min(1.0, max(0.0, _num("dgrid_tp_fraction", controller.tp_fraction)))  # type: ignore[attr-defined]
    controller.signal_min_confidence = _num(  # type: ignore[attr-defined]
        "dgrid_signal_min_confidence", controller.signal_min_confidence)
    # Financial overlay read — refreshed every cycle so the flip debounce weighs
    # THIS cycle's signal, not the one the controller was built with.
    controller.signal_regime = str(configs.get("signal_regime", "") or "")  # type: ignore[attr-defined]
    controller.signal_confidence = _num("signal_confidence", controller.signal_confidence)  # type: ignore[attr-defined]


def _apply_rgrid_controller_config(controller: Controller, configs: Dict[str, object]) -> None:
    """Refresh RGridController attrs read only at __init__, so a live settings edit
    (spread, discretion, soft-reset threshold) takes effect on the next cycle.

    The trail's RUNTIME state (armed flag, peak) is deliberately NOT touched: it
    belongs to the open position, and re-arming the exit from scratch mid-run would
    hand back profit the trail had already locked in.
    """
    _apply_mid_controller_config(controller, configs)
    controller.reset_threshold_pct = _dec(configs.get("reset_threshold_pct", "0.002"))  # type: ignore[attr-defined]
    controller.trail_enabled = bool(configs.get("trail_enabled", True))  # type: ignore[attr-defined]
    controller.vwap_volume_fraction = _dec(configs.get("vwap_volume_fraction", "0") or "0")  # type: ignore[attr-defined]
    # (signal_regime / signal_confidence are pushed every cycle before RUNTIME.tick,
    # not here — they must never take part in the live-config signature.)


def _apply_fill_anchored_controller_config(controller: Controller, configs: Dict[str, object]) -> None:
    """Refresh FillAnchoredQuotingController (grid maker) attrs read only at __init__."""
    _apply_mid_controller_config(controller, configs)
    controller.mode = str(configs.get("anchor_mode", getattr(controller, "mode", "grid"))).lower()  # type: ignore[attr-defined]
    controller.reset_threshold_pct = _dec(configs.get("reset_threshold_pct", "0.0025"))  # type: ignore[attr-defined]
    controller.concession_enabled = bool(configs.get("concession_enabled", False))  # type: ignore[attr-defined]
    controller.concession_escalation_ticks = max(1, int(configs.get("concession_escalation_ticks", 5) or 5))  # type: ignore[attr-defined]
    _cfrac = _dec(configs.get("concession_fraction", "0.5") or "0.5")  # type: ignore[attr-defined]
    controller.concession_fraction = max(Decimal("0.05"), min(_cfrac, Decimal(1)))  # type: ignore[attr-defined]


async def _apply_grid_live_config(
    strategy: str,
    controller: Controller,
    orch: ExecutorOrchestrator,
    configs: Dict[str, object],
    mid: object,
) -> None:
    """Apply sizing/spread/risk edits to active grid executors in place.

    Recenter cancels only free/open entry orders and preserves held inventory +
    reduce-only close legs, avoiding the flattening side effect of a full
    controller stop.
    """
    from src.nadobro.engine.controllers.grid_trading import build_grid_config

    controller.trading_pair = str(configs.get("trading_pair") or getattr(controller, "trading_pair", ""))  # type: ignore[attr-defined]
    for ex in orch.list(controller.id, active_only=True):
        recenter = getattr(ex, "recenter", None)
        ex_config = getattr(ex, "config", None)
        side = getattr(ex, "open_side", None)
        if not callable(recenter) or ex_config is None or not isinstance(side, TradeType):
            continue
        if str(getattr(ex_config, "trading_pair", "")) != str(configs.get("trading_pair")):
            logger.warning(
                "live %s reconfig skipped executor %s: trading_pair changed %s -> %s",
                strategy, getattr(ex, "id", "<unknown>"),
                getattr(ex_config, "trading_pair", None), configs.get("trading_pair"),
            )
            continue
        overlay = _grid_bounds_for_side(configs, side, mid)
        next_cfg = build_grid_config({**configs, **overlay}, side)
        for attr in (
            "start_price",
            "end_price",
            "limit_price",
            "total_amount_quote",
            "min_spread_between_orders",
            "max_open_orders",
            "max_orders_per_batch",
            "activation_bounds",
            "triple_barrier_config",
            "leverage",
            "keep_position",
        ):
            setattr(ex_config, attr, getattr(next_cfg, attr))
        await recenter(next_cfg.start_price, next_cfg.end_price)


async def _apply_live_controller_update(
    strategy: str,
    controller: Controller,
    orch: ExecutorOrchestrator,
    configs: Dict[str, object],
    limits: RiskLimits,
    mid: object,
) -> None:
    """Apply live UI settings to a locally owned controller before ticking."""
    controller.configs = dict(configs)
    controller.limits = limits
    _apply_orchestrator_risk_limits(orch, limits)

    if isinstance(controller, RGridController):
        # R-Grid rests nothing, so there are no quotes to reset — cancelling here
        # would be a no-op that still churns the venue.
        _apply_rgrid_controller_config(controller, configs)
        return

    is_fill_anchored = (
        configs.get("controller_override") == "fill_anchored"
        or isinstance(controller, FillAnchoredQuotingController)
    )
    if is_fill_anchored:
        _apply_fill_anchored_controller_config(controller, configs)
        await _reset_mm_quotes(controller, orch)
        return

    if strategy == "mid":
        _apply_mid_controller_config(controller, configs)
        await _reset_mm_quotes(controller, orch)
        return

    if strategy in ("grid", "dgrid"):
        if strategy == "dgrid":
            _apply_dgrid_controller_config(controller, configs)
        await _apply_grid_live_config(strategy, controller, orch, configs, mid)


def _remote_active(
    strategy: str, user_id: int, network: str, session_id: Optional[int] = None
) -> bool:
    """Check engine_executors for non-terminated rows under the deterministic
    controller id, optionally SCOPED to a run's ``session_id``. Used by
    ``EngineRuntime.is_running`` to detect strategies started by *another worker
    process* (BUG-ER-2) for the CURRENT run only — a stale row from a prior run
    (same stable controller id) must not be treated as "already running".
    Defensive: returns False on any DB failure so a transient error does not
    block strategy startup entirely.
    """
    try:
        from src.nadobro.db import query_count
    except Exception:  # noqa: BLE001
        return False
    cid = deterministic_controller_id(strategy, user_id, network)
    try:
        if session_id is not None:
            return bool(query_count(
                "SELECT 1 FROM engine_executors "
                "WHERE controller_id = %s AND state <> 'TERMINATED' "
                "AND strategy_session_id = %s",
                (cid, int(session_id)),
            ))
        return bool(query_count(
            "SELECT 1 FROM engine_executors "
            "WHERE controller_id = %s AND state <> 'TERMINATED'",
            (cid,),
        ))
    except Exception:  # noqa: BLE001
        return False


def _default_runtime() -> EngineRuntime:
    from src.nadobro.trading.engine_persistence import (
        DbExecutorStore,
        DbTradeRecorder,
    )

    return EngineRuntime(
        executor_store=DbExecutorStore(),
        trade_recorder=DbTradeRecorder(),
    )


# Process-wide runtime. MUST be driven from bot_runtime's single async event
# loop (its async ``_run_cycle``), NOT the sync ``_dispatch_strategy``: the
# orchestrator's asyncio primitives are loop-bound.
#
# Integration contract (the remaining bot_runtime hookup — validated on
# testnet):
#   on strategy start -> await RUNTIME.start(user_id, network, strategy,
#                            configs, build_adapter(client, product_meta),
#                            DbInventoryRepository(), limits=...)
#   each async cycle  -> await RUNTIME.tick(user_id, network, strategy)
#   on strategy stop  -> await RUNTIME.stop(user_id, network, strategy)
# where ``configs`` maps the user's saved strategy settings to the controller's
# config keys and ``product_meta`` comes from the live product catalog.
RUNTIME = _default_runtime()


# --------------------------------------------------------------------------
# bot_runtime hookup (feature-gated): settings -> controller config mapping,
# product metadata, and the per-cycle driver.
# --------------------------------------------------------------------------
# Strategies the engine can drive today. NO_ORDERS_AUDIT-FIX-R1: ``dn`` is now
# included. Previously it was excluded "for a follow-up" but ``dn`` was ALSO
# in ``strategy_runtime.LEGACY_STRATEGY_KEYS`` which silently no-op'd every
# DN cycle. The DeltaNeutralController has been live since Phase 4, so wire
# it up here and emit its config keys in ``map_strategy_config``.
ENGINE_MAPPED_STRATEGIES = ("grid", "rgrid", "dgrid", "mid", "vol", "dn")

# How many of THIS session's most-recent recorded fills to seed the fill-anchored
# exposure VWAP from on (re)build (matches FillAnchoredQuotingController history).
_FILL_HISTORY_SEED = 200

# These controllers derive live order sizing from config values that can be
# edited from the UI while the bot remains LIVE. A price move alone changes
# start/end anchors every cycle, so those mid-derived keys are deliberately
# excluded from the signature below.
_LIVE_RECONFIGURABLE_STRATEGIES = ("grid", "rgrid", "dgrid", "mid")
_LIVE_CONFIG_SIGNATURE_EXCLUDE = frozenset({
    "start_price",
    "end_price",
    "limit_price",
    "candle_provider",
    # DN-only restore fields; excluded defensively if a future strategy shares
    # this helper.
    "restore_cycles_completed",
    "restore_funding_usd",
    # FINANCIAL OVERLAY READ. ``signal_bias``/``signal_confidence`` are continuous
    # floats recomputed from fresh candles every cycle (the in-progress 15m candle
    # alone moves them), so including them flipped the signature on EVERY cycle —
    # which routes into _apply_grid_live_config -> GridExecutor.recenter, i.e. a
    # full cancel + re-place of the resting ladder every tick. That destroys queue
    # position permanently (the ladder then only fills by adverse selection) and
    # bypasses both throttles the controller owns: reset_threshold_bp and
    # _DGRID_RECENTER_MIN_INTERVAL_S. It is the exact churn ``stabilize_overrides``
    # exists to prevent. These are pushed onto the controller directly instead
    # (see the unconditional refresh before RUNTIME.tick).
    "signal_regime",
    "signal_bias",
    "signal_confidence",
})


def engine_v2_enabled() -> bool:
    """Master switch for routing live strategy execution through the engine.

    BUG-SR-1 / BR-1 fix: default is now ON. The legacy ``run_cycle`` strategy
    dispatch was removed during the engine-v2 cutover, so leaving this OFF
    causes the bot to *silently no-op* every cycle (cycles report "success"
    yet place no orders). Operators that need to roll back to the legacy
    path no longer have one — set ``NADO_ENGINE_V2_RUNTIME=false`` only as
    an emergency kill switch (the bot will accept it but log a critical
    warning so it's never a silent footgun).
    """
    raw = os.environ.get("NADO_ENGINE_V2_RUNTIME", "").strip().lower()
    if raw == "":
        return True
    if raw in ("0", "false", "no", "off"):
        logger.critical(
            "engine v2 runtime explicitly DISABLED via NADO_ENGINE_V2_RUNTIME=%s "
            "— engine-mapped strategies will silently no-op until this is unset",
            raw,
        )
        return False
    return raw in ("1", "true", "yes", "on")


def _f(settings: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def resolve_vol_fee_rates(product: str, network: str = "mainnet") -> tuple[float, float]:
    """Return ``(venue_taker_rate, builder_rate)`` for the spot Volume Bot.

    THE single source of truth for both the fee-agreement card and the engine
    config. They previously resolved rates through two independent call sites
    with different symbol normalization, so a card could quote one rate while
    the run traded another — the user would be agreeing to a price that was
    not the price. Both now call this.

    Never returns a 0 taker rate: a 0 would make the breakeven gate think a
    round trip is free and sell into a real fee loss. See
    src/nadobro/quant/vol_fee_estimator.py for the measured fallback.
    """
    from src.nadobro.quant.vol_fee_estimator import (
        DEFAULT_BUILDER_FEE_RATE,
        DEFAULT_SPOT_TAKER_FEE_RATE,
    )

    try:
        from src.nadobro.config import normalize_volume_spot_symbol

        pair = normalize_volume_spot_symbol(str(product or "")) or str(product or "")
    except Exception:  # noqa: BLE001  # policy: degrade-ok(raw symbol still resolves for most pairs)
        pair = str(product or "")
    try:
        from src.nadobro.venue.product_catalog import get_spot_taker_fee_rate

        taker = get_spot_taker_fee_rate(pair, network=network)
    except Exception:  # noqa: BLE001  # policy: degrade-ok(measured default applies)
        taker = None
    if not taker or float(taker) <= 0:
        taker = float(DEFAULT_SPOT_TAKER_FEE_RATE)
    try:
        from src.nadobro.config import get_nado_builder_routing_config

        # Units are 0.1 bps: 10 units = 1 bps = 0.0001. Testnet returns 0.
        builder = float(get_nado_builder_routing_config(network)[1]) / 100000.0
    except Exception:  # noqa: BLE001  # policy: degrade-ok(assume the locked 1bps policy rate)
        builder = float(DEFAULT_BUILDER_FEE_RATE)
    return float(taker), builder


# Cap on the auto-derived minimum quote lifetime. Grid's 60s cadence would
# otherwise derive a 120s hold, which is long enough to be its own stale-quote
# problem; the churn this guards against only exists on fast cadences.
_MAX_AUTO_QUOTE_LIFETIME_S = 30.0


def _min_quote_lifetime_s(strategy: str, settings) -> Decimal:
    """Minimum time a maker quote rests before a price change may cancel it.

    Derived from the strategy's ACTUAL cadence (~2 cycles) rather than a
    hand-picked constant, so it stays correct if the cadence changes. An
    explicit ``min_quote_lifetime_s`` in settings wins.
    """
    explicit = settings.get("min_quote_lifetime_s")
    if explicit is not None:
        try:
            return max(Decimal(0), Decimal(str(explicit)))
        except Exception:  # noqa: BLE001  # policy: degrade-ok(malformed -> derive it)
            pass
    from src.nadobro.core.cadence import effective_interval_seconds

    cadence = effective_interval_seconds(strategy, settings.get("interval_seconds", 60))
    return Decimal(str(min(_MAX_AUTO_QUOTE_LIFETIME_S, 2.0 * cadence)))


def _quote_defense_defaults(settings, notional, *, auto_spread: bool) -> dict:
    """Regime gate / inventory cap / ATR-spread knobs for grid family + MM.

    Defaults per the 2026-06 grid post-mortem plan: gate ON (pause new quotes
    in trends/breakouts, notify once per flip), net-exposure cap at 30% of
    allocated margin with resume at 70% of the cap, ATR(14, 1m) x 1.5 spread
    with a fee floor (1.5 bp/side) when the user didn't pin a spread.
    """
    # Per-side spread bounds from the user's min/max spread (bps → fraction).
    # Previously hardcoded (1.5 bp floor / 50 bp cap) so min_spread_bp /
    # max_spread_bp were dead inputs; now they drive the manual floor AND the
    # ATR auto-spread clamp. Defaults match the old constants, so an unset config
    # behaves exactly as before. floor clamped ≥0: a negative value is mid's
    # SHIPPED registry default (-10 — see strategy_registry "user-tunable in
    # [-10, +100]") and means "no floor" (the Tread-parity volume product may
    # concede spread to 0); do NOT reinterpret it as garbage (audit 2026-07-31).
    # The cap is held ≥ floor so a stray min>max can't invert the band.
    floor_half = Decimal(str(max(0.0, _f(settings, "min_spread_bp", 1.5)))) / Decimal(10000)
    cap_half = Decimal(str(_f(settings, "max_spread_bp", 50.0))) / Decimal(10000)
    if cap_half < floor_half:
        cap_half = floor_half
    return {
        "regime_gate_enabled": bool(_f(settings, "regime_gate_enabled", 1.0)),
        "max_net_exposure_pct": _f(settings, "max_net_exposure_pct", 30.0),
        "exposure_resume_frac": 0.7,
        "margin_quote": Decimal(str(notional)),
        "auto_spread": auto_spread,
        "auto_spread_k": Decimal(str(_f(settings, "auto_spread_k", 1.5))),
        "spread_floor_half_pct": floor_half,
        "spread_cap_half_pct": cap_half,
        "candle_provider": None,  # injected in run_engine_cycle (client there)
    }


def _effective_leverage(settings: Dict[str, Any], fallback: float = 1.0) -> float:
    """Resolve the effective leverage that turns the user's *margin* (collateral)
    into deployed position notional for the grid/MM family.

    Precedence: an explicit ``mm_leverage_override`` (set by the Tiny Budget
    preset / leverage selector) wins; otherwise the session ``leverage``; else
    ``fallback``. Floored at 1x. The venue cap is NOT applied here — the start
    guard (``_run_mm_start_guard``) already rejects leverage above the pair max
    and the adapter re-validates at placement, so this stays a pure function of
    settings (no catalog/network dependency in the hot mapping path).
    """
    raw = _f(settings, "mm_leverage_override", 0.0)
    if raw <= 0:
        raw = _f(settings, "leverage", 0.0)
    if raw <= 0:
        raw = float(fallback or 1.0)
    return max(1.0, raw)


def map_strategy_config(
    strategy: str, settings: Dict[str, Any], mid: Decimal, *, product: str,
    leverage: int = 1, network: str = "mainnet",
) -> Dict[str, object]:
    """Derive an engine controller config from a user's saved strategy settings
    + current mid. Documented, testnet-tunable mappings (not 1:1 with legacy).
    """
    mid = _dec(mid)
    # ``notional`` here is the user's allocated MARGIN (collateral). The grid/MM
    # family deploys ``margin x effective_leverage`` of position notional, so a
    # $100 margin at 5x quotes ~$500 across the ladder (and unlocks more levels).
    # DN/Volume size their own legs and do not use ``deployed`` below.
    notional = _f(settings, "cycle_notional_usd", _f(settings, "notional_usd", 100.0))
    eff_lev = _effective_leverage(settings, float(leverage or 1.0))
    deployed = notional * eff_lev
    # Participation chunk (Phase 1b): when a participation preset is active,
    # bot_runtime resolves the per-cycle order notional once at start (chunk =
    # preset multiplier × 24h volume, floored at min-notional, capped at
    # deployed). Use it as the per-order size in place of the deployed-budget
    # sizing. 0/unset → keep the deployed-based size (opt-out, unchanged).
    _mm_chunk = _f(settings, "mm_cycle_notional_usd", 0.0)
    _chunk_dec = Decimal(str(_mm_chunk)) if _mm_chunk > 0 else None
    # The quoting spread is USER-SET, not one hardcoded value for everyone. Read
    # the strategy's own spread field (the frontend writes rgrid_spread_bp /
    # dgrid_spread_bp), falling back to the generic spread_bp. Previously this
    # only read spread_bp, so rgrid/dgrid users' spread settings were ignored
    # and the grid quoted at the default step regardless.
    if strategy == "rgrid":
        _spread_bp = _f(settings, "rgrid_spread_bp", _f(settings, "spread_bp", 10.0))
    elif strategy == "dgrid":
        _spread_bp = _f(settings, "dgrid_spread_bp", _f(settings, "spread_bp", 8.0))
    else:
        _spread_bp = _f(settings, "spread_bp", 5.0)
    spread_frac = Decimal(str(_spread_bp)) / Decimal(10000)
    levels = max(1, int(_f(settings, "levels", 2)))
    # SL/TP honor the per-strategy fields (rgrid/dgrid store them under
    # rgrid_stop_loss_pct / rgrid_take_profit_pct), so a user's custom value
    # drives the barrier instead of the sl_pct/tp_pct default.
    from src.nadobro.strategy.strategy_registry import (
        effective_sl_tp_pct, sltp_is_explicit,
    )
    _sl_pct, _tp_pct = effective_sl_tp_pct(strategy, settings)
    # Only substitute a default when the user configured NOTHING. A key that is
    # PRESENT and 0 is a deliberate disarm, and overriding it re-arms what they
    # switched off — DGRID-TP-DISARM-PHANTOM: an explicit TP of 0 became 0.6, so
    # D-Grid scaled out a third of the position at 0.2% of margin (a ~1bp move)
    # for a user who had disarmed TP precisely to let a trend run. It also made
    # dynamic_grid's documented "no TP set keeps the legacy tiers" branch
    # unreachable from the mapper.
    _sl_set, _tp_set = sltp_is_explicit(strategy, settings)
    if _tp_pct <= 0 and not _tp_set:
        _tp_pct = _f(settings, "tp_pct", 0.6)
    if _sl_pct <= 0 and not _sl_set:
        _sl_pct = _f(settings, "sl_pct", 0.5)
    # UNITS INVARIANT (CLAUDE.md): a user value is either a price-move barrier OR a
    # %-of-margin rail — never both. ``_tp_pct`` / ``_sl_pct`` are the user's
    # numbers and they are **% of MARGIN**: that is what the UI says, and it is what
    # ``_evaluate_session_pnl_rail`` measures (live PnL incl. uPnL, net of fees).
    #
    # They therefore do NOT become an executor ``triple_barrier_config``. That
    # barrier is a PRICE-return fraction from a single executor's avg entry
    # (engine/types.py), which is a different quantity in two compounding ways:
    #   * per LEVEL, not per session — with 4 levels and one filled, an 0.8% price
    #     move is $0.20 on a $25 level while the user asked for an $0.80 stop, so
    #     it fired ~``levels`` times early and paid taker fees to do it;
    #   * leverage-blind — position notional is margin x leverage, so the same 0.8%
    #     means 0.8% of margin at 1x and 39% at 49x.
    # (Guardrail: DGRID-DUAL-UNIT-SLTP in tests/engine/test_sltp_invariants.py.)
    #
    # The session rail is the single enforcement point, exactly as it already is for
    # rgrid and mid. The ladder's own per-level take-profit is unaffected: a filled
    # BUY level is still closed by its paired SELL one step up.
    #
    # ``cfg["tp_pct"]`` / ``cfg["sl_pct"]`` stay PERCENTS below, because
    # DynamicGridController._tp_tier_ladder reads tp_pct as a percent and compares
    # the ladder against ``upnl_pct`` — also a percent. Handing it a /100 fraction
    # made every tier 100x too small: with the shipped 1.2% TP the tiers landed at
    # 0.004 / 0.008 / 0.012 % of margin, so D-Grid scaled out a third of the
    # position on the first favourable tick and never let a winner run. Its
    # controller tests pass a percent directly, so they never saw it.

    if strategy == "mid":
        # BIAS-TEXT-COERCION (2026-07-30): settings may carry the legacy TEXT
        # values ("neutral"/"long_bias"/"short_bias" — the set_text UI path and
        # old configs store them). _f() coerced any text to 0.0, silently
        # neutralizing an explicit user lean, while the overlay's user-bias
        # filter resolved the same text to ±1 — the two paths disagreed. Use
        # the shared resolver so text and float biases mean the same thing
        # everywhere.
        from src.nadobro.quant.mm_quote_math import _resolve_directional_bias_value

        _mid_bias = _resolve_directional_bias_value(settings.get("directional_bias"))
        # Directional bias intentionally builds one-sided inventory, which would
        # otherwise be choked by the symmetric net-exposure cap. Per the docs,
        # extreme bias (±1) is allowed up to 20% more directional exposure (and
        # therefore needs ~20% more margin headroom); scale the cap linearly with
        # |bias| so the lean is effective but still bounded.
        _bias_exposure_mult = 1.0 + 0.20 * abs(_mid_bias)
        # Inventory ceiling: an explicit positive value is honored as-is; <= 0
        # means AUTO = the deployed size, so the cap stays coherent with the
        # order size at any leverage (a $60 cap under a $1,000 quote suppressed
        # the bid after every single fill). The registry default is unchanged —
        # the Turbo Volume preset writes 0 to opt in.
        _inv_cap = _f(settings, "inventory_soft_limit_usd", deployed)
        if _inv_cap <= 0:
            _inv_cap = deployed
        return {
            "trading_pair": product,
            "spread_bid_pct": spread_frac,
            "spread_ask_pct": spread_frac,
            # Mid Mode directional bias in [-1, +1]: the controller skews the
            # per-side spreads (documented ±0.2 alpha-tilt) to lean the book
            # long/short. _f coerces the legacy text default ("neutral") to 0.0.
            "directional_bias": _mid_bias,
            # Notional deployed per SIDE. ``ladder_levels`` below subdivides it
            # across levels — the total is identical either way, so resurrecting
            # the (previously dead) ``levels`` input changes the SHAPE of the
            # book, never its size. With a participation preset, the per-cycle
            # chunk replaces the full size.
            "order_amount_quote": _chunk_dec or Decimal(str(deployed)),
            # LADDER (Phase 2, 2026-08-02): Mid used to place ONE bid + ONE ask
            # and then wait in position — ``levels`` was read here and thrown
            # away. Now the side's deployment is split across ``levels`` levels
            # stepping away from the target, so the book scales into an adverse
            # move and back out of it. Each level is clamped to the venue
            # min-notional by the planner, and levels=1 reproduces the old
            # single-quote behaviour exactly.
            "ladder_levels": levels,
            # Step per level. Touch mode glues to a one-tick book where a
            # spread-sized step would be nonsense, so it takes the planner's
            # auto spacing (one tick, floored at 1bp); mid mode steps by the
            # quoted spread, matching how the classic grid ladder spaces levels.
            "ladder_step_bp": (
                Decimal(0)
                if str(settings.get("mm_quote_mode") or "mid").lower() == "touch"
                else spread_frac * Decimal(10000)
            ),
            "ladder_curve": str(settings.get("size_curve") or "flat"),
            # PHASE 0 (queue preservation): Mid runs on an 8s fast cadence, so a
            # tolerance-driven cancel could bin a quote after 8s of queue time.
            # On BTC-PERP the spread is a single tick — a quote cannot be
            # improved, only re-queued at the back — so queue position IS the
            # fill rate. Hold each quote for ~2 cycles before a mere price
            # change may cancel it. Safety cancels are never delayed.
            "min_quote_lifetime_s": _min_quote_lifetime_s(strategy, settings),
            "max_base_quote": Decimal(str(_inv_cap)),
            # Quote mode: "mid" (default, mid ± spread) or "touch" (join the
            # best bid/ask — Turbo Volume). Unknown values fall back to "mid".
            # POLICY (2026-07-15): mid is MAKER-ONLY — the cross-on-deadline
            # taker flatten was removed (fees + Nado wash-trading policy).
            "quote_mode": str(settings.get("mm_quote_mode") or "mid"),
            "price_distance_tolerance": (spread_frac / Decimal(2)) or Decimal("0.0005"),
            "leverage": int(eff_lev),
            # Regime gate + inventory cap + ATR auto-spread (2026-06 upgrade).
            # auto_spread engages when the user left spread unset/zero. margin_quote
            # tracks the DEPLOYED size so the net-exposure cap scales with leverage.
            **_quote_defense_defaults(settings, deployed, auto_spread=spread_frac <= 0),
            # Bias-scaled net-exposure cap (overrides the _quote_defense_defaults
            # value): +20% at |bias|=1 so a directional lean isn't suppressed.
            "max_net_exposure_pct": _f(settings, "max_net_exposure_pct", 30.0) * _bias_exposure_mult,
            # MID-GATE-DEFAULT (2026-07-31): regime gate OFF by default for Mid —
            # the same decision GRID-IN-TRENDS made for grid/rgrid, for the same
            # symptom ("always paused, silently never quotes"). Mid is the
            # volume product: on Nado's thin books the gate's volume-profile
            # verdicts (breakout/expansion) read PAUSE 46-61% of minutes and a
            # paused FLAT book quotes nothing at all, so sessions sat dark.
            # Backstops that stay armed: session SL/TP rail, net-exposure cap,
            # inventory ceiling (the TWAP fast-move pause exists too but is
            # opt-in — twap_pause_move_bp defaults to 0). An explicit
            # regime_gate_enabled=1 in settings re-arms the gate, and the
            # signal overlay still arms it while suppressing entries.
            **(
                {"regime_gate_enabled": 0.0}
                if "regime_gate_enabled" not in settings
                else {}
            ),
        }
    if strategy == "dn":
        # NO_ORDERS_AUDIT-FIX-R1: DN config keys for DeltaNeutralController.
        # The controller expects trading_pair_long/short, hedge_ratio,
        # leg_amount_quote, max_drift_pct, and barriers. ``product`` here is
        # the base symbol (e.g. "QQQ"); the long leg is the SPOT pair and the
        # short leg is the PERP. run_engine_cycle resolves the real per-leg
        # product_ids + isolated flag via get_dn_pair (see _materialize_dn_leg_meta).
        from src.nadobro.engine.types import TripleBarrierConfig as _TBC

        base = str(product or "").upper().split("-", 1)[0]
        # Nado spot is quoted/collateralized in USDT0 (see config SPOT ids).
        # The real spot product_id is resolved from the catalog in
        # _materialize_dn_leg_meta — this string is the display/inventory key.
        long_pair = f"{base}-USDT0"        # spot leg
        short_pair = f"{base}-PERP"         # perp leg
        hedge_ratio = Decimal(str(_f(settings, "dn_hedge_ratio", 1.0)))
        leg_quote = Decimal(str(_f(settings, "fixed_margin_usd", notional)))
        max_drift = Decimal(str(_f(settings, "dn_max_drift_pct", 5.0))) / Decimal(100)
        # Hold duration is now the MINIMUM hold: default 1h, clamp [60s, 24h].
        # After it elapses the controller keeps the hedge open while funding
        # stays favorable and closes BOTH legs on a funding flip (or at the
        # max-hold safety cap). funding_exit_enabled=False restores a fixed hold.
        hold_seconds = int(max(60.0, min(_f(settings, "dn_hold_seconds", 3600.0), 86400.0)))
        # Safety cap on the total hold; 0 disables it (hold while favorable).
        # Clamp to <= 7d so a stray setting can't strand a hedge indefinitely.
        max_hold_seconds = int(max(0.0, min(_f(settings, "dn_max_hold_seconds", 86400.0), 604800.0)))
        funding_exit_enabled = _f(settings, "dn_funding_exit", 1.0) >= 0.5
        funding_flip_confirmations = int(max(1.0, _f(settings, "dn_funding_flip_confirmations", 2.0)))
        funding_poll_seconds = int(max(0.0, _f(settings, "dn_funding_poll_seconds", 60.0)))
        # Volume-farming: repeat open->hold->close N times with a gap between.
        cycles = int(max(1.0, _f(settings, "dn_cycles", 1.0)))
        cycle_gap_seconds = int(max(0.0, _f(settings, "dn_cycle_gap_seconds", 30.0)))
        # Per-leg TP/SL are OFF by default for DN: a one-sided TP would close a
        # single leg early and break the hedge. Only honor them if the operator
        # explicitly opts in via dn_leg_tp_pct / dn_leg_sl_pct.
        leg_tp = Decimal(str(_f(settings, "dn_leg_tp_pct", 0.0))) / Decimal(100)
        leg_sl = Decimal(str(_f(settings, "dn_leg_sl_pct", 0.0))) / Decimal(100)
        return {
            "trading_pair": long_pair,  # for parent base-class .trading_pair if read
            "trading_pair_long": long_pair,
            "trading_pair_short": short_pair,
            "hedge_ratio": hedge_ratio,
            "leg_amount_quote": leg_quote,
            "max_drift_pct": max_drift,
            "hold_seconds": hold_seconds,            # minimum hold
            "max_hold_seconds": max_hold_seconds,
            "funding_exit_enabled": funding_exit_enabled,
            "funding_flip_confirmations": funding_flip_confirmations,
            "funding_poll_seconds": funding_poll_seconds,
            "cycles": cycles,
            "cycle_gap_seconds": cycle_gap_seconds,
            "barriers": _TBC(take_profit=leg_tp or None, stop_loss=leg_sl or None),
            # Strictly 1x short by design (margin = full notional). Surfaced so
            # the adapter sizes isolated margin correctly for the perp leg.
            "leverage": 1,
        }
    if strategy == "vol":
        from src.nadobro.quant.vol_fee_estimator import (
            DEFAULT_BUILDER_FEE_RATE as _VOL_DEFAULT_BUILDER_RATE,
            DEFAULT_SPOT_TAKER_FEE_RATE as _VOL_DEFAULT_TAKER_RATE,
            clamp_margin_usd as _vol_clamp_margin,
        )

        interval = max(1.0, _f(settings, "interval_seconds", 60))
        # VOL-MARGIN fix: the vol card collects the run size under
        # ``session_margin_usd`` (strategy_handler), but the generic ``notional``
        # above only reads cycle_notional_usd/notional_usd — so a user who set
        # "$500" still traded the $100 default. Prefer the user's session margin,
        # then fall back to the legacy keys.
        vol_notional = _f(
            settings, "session_margin_usd",
            _f(settings, "cycle_notional_usd", _f(settings, "notional_usd", 100.0)),
        )
        # Product band $100-$500 (2026-07-31). The UI enforces it on entry;
        # re-clamp here so a stale or hand-edited config can never start an
        # out-of-band run — the fee estimate the user agreed to was quoted
        # against a banded margin.
        vol_notional = float(_vol_clamp_margin(vol_notional))
        # Normalize the trading pair so the VolumeBotController validation
        # sees a canonical base (e.g. ``KBTC``) regardless of whether
        # ``state.product`` was stored as ``KBTC`` (current UI) or as a
        # dashed pair like ``KBTC-USDC0`` (legacy/tests).
        try:
            from src.nadobro.config import normalize_volume_spot_symbol

            vol_pair = normalize_volume_spot_symbol(str(product or "")) or str(product or "")
        except Exception:
            vol_pair = str(product or "")
        try:
            from src.nadobro.venue.product_catalog import get_spot_maker_fee_rate

            spot_maker_fee_rate = get_spot_maker_fee_rate(vol_pair, network=network)
        except Exception:
            spot_maker_fee_rate = None
        # v4 taker mode: the venue taker rate drives BOTH the breakeven gate
        # and the fee estimate the user agreed to, so both MUST come from the
        # same resolver — see resolve_vol_fee_rates. The builder fee is charged
        # ON TOP of the venue fee (trades_mainnet keeps builder_fee in its own
        # column), so the breakeven carries it explicitly.
        spot_taker_fee_rate, builder_fee_rate = resolve_vol_fee_rates(vol_pair, network)
        if spot_maker_fee_rate is None:
            if settings.get("vol_maker_fee_rate") is not None:
                spot_maker_fee_rate = _f(settings, "vol_maker_fee_rate", 0.0)
            else:
                spot_maker_fee_rate = _f(settings, "vol_maker_fee_bp", 0.0) / 10000.0
        return {
            "trading_pair": vol_pair,
            "total_amount_quote": Decimal(str(vol_notional)),
            # Compatibility only; the Volume controller now places one order per
            # leg and waits for full fill instead of TWAP slicing over time.
            "total_duration": interval * 4,
            "order_interval": interval,
            "market": "spot",
            "leverage": 1,
            "spot_maker_fee_rate": Decimal(str(spot_maker_fee_rate or 0.0)),
            # VOL-LOOP / VOL-NO-CAP: cumulative volume target (0 = single
            # round-trip) and a hard cycle ceiling so the loop can't run away.
            "target_volume_usd": Decimal(str(_f(settings, "target_volume_usd", 0.0))),
            "max_cycles": int(max(1, _f(settings, "vol_max_cycles", 100))),
            # v3 quoting: passive distance below the touch for the buy
            # (0 = join/improve the best bid — the v2 5bp-under-mid default
            # rested BELOW the best bid on tight books and stalled for hours).
            "vol_buy_offset_bp": _f(settings, "vol_buy_offset_bp", 0.0),
            # Bounded round-trip cost in bp of notional (replaces the v2
            # forced-profit sell floor that made cycles impossible in a flat
            # or falling market). The session SL rail stays the hard backstop.
            "vol_max_cycle_loss_bp": _f(settings, "vol_max_cycle_loss_bp", 20.0),
            # Requote cadence for BOTH legs: unfilled after this many seconds
            # → cancel and re-place at the fresh touch (0 disables).
            "vol_requote_seconds": _f(settings, "vol_requote_seconds", 20.0),
            # Maker-first deadline: a leg unfilled this long is finished with
            # a marketable LIMIT priced vol_cross_slippage_bp through the
            # touch (still price-bounded; fills as taker). 0 = pure maker.
            "vol_cross_after_seconds": _f(settings, "vol_cross_after_seconds", 25.0),
            "vol_cross_slippage_bp": _f(settings, "vol_cross_slippage_bp", 15.0),
            # v4 TAKER MODE (docs/volume_bot_taker_v4.md): market orders on
            # both legs, with a patient sell that waits for a price strictly
            # above the entry AND at/above fee-inclusive breakeven. 0 restores
            # the v3 maker path.
            # Only forward the legacy kill-switch when the user actually set
            # it — mapping a default here would pin every run to taker and the
            # execution-algo default could never apply.
            **(
                {"vol_taker_mode": 1.0 if _f(settings, "vol_taker_mode", 1.0) != 0 else 0.0}
                if "vol_taker_mode" in settings else {}
            ),
            "spot_taker_fee_rate": Decimal(str(spot_taker_fee_rate)),
            "vol_builder_fee_rate": Decimal(str(builder_fee_rate)),
            # Taker execution bound: both legs are marketable LIMITs priced
            # this far through the touch (AUDIT-VOL-2026-07-31 F1 — a naked
            # MARKET is an IOC at 1% that the adapter never overrides).
            "vol_taker_slippage_bp": _f(settings, "vol_taker_slippage_bp", 15.0),
            # EXECUTION ALGO (2026-07-31): maker TWAP is the DEFAULT. Measured
            # on KBTC spot, resting instead of crossing costs 3.6bp vs 14.3bp
            # per round trip — ~4x the volume per unit of the session loss
            # budget. Price impact at this size is 0.00bp, so this is a fee
            # decision, not an impact one. "chase" = same ladder, impatient;
            # "taker" = cross immediately (v4.1 behaviour / kill-switch).
            "vol_execution_algo": str(
                settings.get("vol_execution_algo") or "twap"
            ).strip().lower(),
            "vol_twap_horizon_seconds": _f(settings, "vol_twap_horizon_seconds", 120.0),
            "vol_chase_interval_seconds": _f(settings, "vol_chase_interval_seconds", 20.0),
            "vol_cross_tolerance_frac": _f(settings, "vol_cross_tolerance_frac", 0.5),
            "vol_max_taker_frac": _f(settings, "vol_max_taker_frac", 0.25),
            # Hard deadline (x horizon) after which a maker leg crosses no
            # matter what. Without it a PARTIALLY filled leg can sit under the
            # debt tolerance forever once chases are spent — the v3 stall.
            "vol_leg_hard_deadline_mult": _f(settings, "vol_leg_hard_deadline_mult", 3.0),
            # A marketable limit RESTS if it cannot fully sweep, and taker mode
            # disables the maker rescues — so a taker leg still live after this
            # long is cancelled and re-placed (TAKER-REST-STALL).
            "vol_taker_fill_timeout_seconds": _f(
                settings, "vol_taker_fill_timeout_seconds", 10.0
            ),
            # SESSION LOSS LIMIT (user directive 2026-07-31): stop and flatten
            # once cumulative realized PnL NET OF FEES reaches -sl_pct% of
            # margin — 5% of a $100 margin is -$5. Enforced in the CONTROLLER
            # off its own fill accounting, because the live-session rail is
            # structurally blind to spot (session product_id is perp-only, so
            # it is NULL for a spot session). Absolute USD so the controller
            # needs no margin context.
            "vol_session_loss_limit_usd": Decimal(str(
                vol_notional * max(0.0, _f(settings, "sl_pct", 5.0)) / 100.0
            )),
        }
    # grid / rgrid / dgrid family.
    #
    # REVERSE GRID — its own controller + taker executor (RGridController). Both
    # legs price off the average of the buy/sell exposure prices and CROSS the
    # spread on a break; it never switches phase (that is D-Grid), rests no
    # ladder, and has no maker mode to opt out of.
    if strategy == "rgrid":
        # The threshold ARMS the trailing soft reset, and the controller refuses one
        # that sits inside the entry band (an exit narrower than the trigger that
        # opened the trade would arm before the break is even established). The old
        # 0.125% default was BELOW 2 x the shipped 10bp spread, so a user who never
        # touched the button had the soft reset silently disabled. Default to the
        # registry's 0.2%, never below 2 x spread, so it is always armable.
        _spread_pct = float(spread_frac * Decimal(100)) if spread_frac > 0 else 0.1
        _reset_pct = _f(
            settings, "rgrid_reset_threshold_pct",
            _f(settings, "grid_reset_threshold_pct",
               _f(settings, "reset_threshold_pct", max(0.2, 2.0 * _spread_pct))),
        )
        _band = spread_frac if spread_frac > 0 else Decimal("0.001")
        # STEP vs STOP BUDGET. R-Grid crosses the spread on both legs, and the
        # session stop is a % of MARGIN judged net of fees — so leverage buys size
        # but not stop budget. At $100/49x/4 levels the $1,225 step costs $1.05 per
        # round trip against a $0.80 stop: the session stopped out on the FIRST
        # entry+exit whichever way price went. Cap the step so at least ~3 round
        # trips fit inside the stop. Only ever shrinks it, and is skipped when the
        # user disarmed their stop (no budget to size against).
        from src.nadobro.quant.mm_quote_math import DEFAULT_MIN_ORDER_NOTIONAL_USD
        from src.nadobro.quant.rgrid_sizing import (
            TAKER_ROUND_TRIP_RATE, resolve_step_quote,
        )
        from src.nadobro.strategy.strategy_registry import (
            effective_sl_tp_pct,
            session_margin_usd,
        )

        _rg_sl_pct = max(0.0, effective_sl_tp_pct("rgrid", settings)[0])
        # The budget must use the margin the RAIL measures against, NOT the
        # deployment basis ``notional`` above. They resolve in opposite order
        # (rail: notional_usd first; deployment: cycle_notional_usd first), so a
        # user with a larger cycle size was sizing the cap against a budget 2.5x
        # what the rail would enforce.
        _rg_margin = session_margin_usd(settings) or notional
        _step_plan = resolve_step_quote(
            deployed_quote=deployed,
            levels=levels,
            chunk_quote=_chunk_dec,
            stop_budget_usd=_rg_margin * _rg_sl_pct / 100.0,
            min_step_usd=DEFAULT_MIN_ORDER_NOTIONAL_USD,
            # R-Grid's own exit triggers a band away, so the cap must bound a full
            # band of adverse move on the PYRAMID, not just its fees — otherwise
            # the session rail always fires before the exit can trigger.
            band_frac=_band,
        )
        # Derive the exposure ceiling from the same budget the step cap uses.
        _rg_budget = _dec(str(_rg_margin * _rg_sl_pct / 100.0))
        _rg_exposure_pct = _f(settings, "max_net_exposure_pct", 30.0)
        if _rg_budget > 0 and deployed > 0:
            _rg_move = _band + _dec(str(TAKER_ROUND_TRIP_RATE))
            if _rg_move > 0:
                _afford = _rg_budget / _rg_move          # USD of exposure
                _rg_exposure_pct = min(
                    _rg_exposure_pct,
                    float(_afford / _dec(str(deployed)) * Decimal(100)),
                )
        if _step_plan.capped:
            logger.info(
                "rgrid step capped by the stop budget: %s -> %s (stop $%.2f, round "
                "trip $%.2f, ~%.1f trips fit)%s",
                _step_plan.uncapped, _step_plan.step, float(_step_plan.stop_budget_usd),
                float(_step_plan.round_trip_cost), float(_step_plan.round_trips_in_budget),
                " [FLOORED at the venue minimum — the stop is too tight for any "
                "placeable size]" if _step_plan.floored else "",
            )
        return {
            "trading_pair": product,
            # Kept so /status, the live-reconfig router and the overlay keep
            # recognising this as the exposure-anchored family.
            "controller_override": "fill_anchored",
            "anchor_mode": "rgrid",
            # Maker on both legs, post-only. Kept as an explicit key so a config
            # dump shows the execution model rather than implying it.
            "passive_only": True,
            "trail_enabled": True,     # the documented soft reset
            "reset_threshold_pct": Decimal(str(_reset_pct)) / Decimal(100),
            # The trigger offset from the exposure average, per leg.
            "spread_bid_pct": _band,
            "spread_ask_pct": _band,
            # ONE resting quote of this size per break: deployed/levels, shrunk by
            # a POV chunk and by the stop budget (see resolve_step_quote above).
            # Never grown by either. The step is priced against the TAKER round
            # trip even though both legs are makers — a deliberately conservative
            # bound, and the one order that does cross (the trailing stop) is
            # therefore already inside the budget.
            "order_amount_quote": _step_plan.step,
            # The risk-bound the overlay may not exceed (see _maybe_apply_overlay).
            "step_capped_quote": _step_plan.step,
            # Surfaced so /status and the config card can explain a step that is
            # smaller than margin x leverage / levels.
            "step_uncapped_quote": _step_plan.uncapped,
            "step_capped_by_stop": bool(_step_plan.capped),
            "step_below_venue_minimum": bool(_step_plan.floored),
            "ladder_levels": 1,        # no resting ladder
            "min_quote_lifetime_s": _min_quote_lifetime_s(strategy, settings),
            "price_distance_tolerance": (spread_frac / Decimal(2)) or Decimal("0.0005"),
            "leverage": int(eff_lev),
            # Exposure-price window. Spec: "a rolling VWAP over the most recent
            # portion of filled volume (e.g. last 12% of quantity when discretion is
            # 0.06)" — the window is discretion on EACH side, so the volume fraction
            # is 2x the knob. Clamped to the whole retained window.
            "vwap_volume_fraction": min(
                1.0, max(0.0, _f(settings, "rgrid_discretion", 0.0)) * 2.0
            ),
            **_quote_defense_defaults(settings, deployed, auto_spread=spread_frac <= 0),
            # THE CEILING THE STEP CAP CANNOT REACH — after the defaults on
            # purpose, so this wins the dict literal.
            # R-Grid has no break counter: it adds one step per break until the
            # NET-EXPOSURE CAP stops it, and that cap is step-INDEPENDENT. A bound
            # written as ``levels * step`` therefore models a pyramid the strategy
            # never stops at: at $100/50x the modelled $400 is really $1,500 (15
            # steps), where ONE crossing exit costs 81% of a 0.8% stop and a single
            # band of adverse move costs 3.5x it. Shrinking the step cannot fix
            # that — it just takes more breaks to reach the same ceiling. So bound
            # the CAP: hold only the exposure that a band of adverse move plus the
            # round trip can afford out of the stop budget. min() with the user's
            # setting, so this only ever tightens.
            "max_net_exposure_pct": _rg_exposure_pct,
            # The regime gate would pause momentum exactly when it must act. OFF
            # unless the user (or the overlay's suppress posture) re-arms it; the
            # net-exposure cap, the trailing soft reset and the session SL/TP rails
            # are the backstops.
            **({"regime_gate_enabled": 0.0} if "regime_gate_enabled" not in settings else {}),
        }
    #
    # GRID's fill-anchored maker mode (FillAnchoredQuotingController) — opt-in via
    # fill_anchored=1. Default for grid is the classic multi-level recycling ladder.
    _fa_default = 0.0
    if strategy == "grid" and bool(_f(settings, "fill_anchored", _fa_default)):
        from src.nadobro.quant.mm_quote_math import _resolve_directional_bias_value

        default_reset = 0.25
        # The UI writes the reset threshold under ``grid_reset_threshold_pct`` but
        # the controller reads ``reset_threshold_pct``; translate here so the
        # "Reset Threshold" button actually drives the soft reset. Percent →
        # fraction. (Also fixes live edits: _apply_fill_anchored_controller_config
        # refreshes from this same mapped value each cycle.)
        _reset_pct = _f(settings, "grid_reset_threshold_pct",
                        _f(settings, "reset_threshold_pct", default_reset))
        return {
            "trading_pair": product,
            "controller_override": "fill_anchored",
            "anchor_mode": strategy,
            "reset_threshold_pct": Decimal(str(_reset_pct)) / Decimal(100),
            "spread_bid_pct": spread_frac if spread_frac > 0 else Decimal("0.001"),
            "spread_ask_pct": spread_frac if spread_frac > 0 else Decimal("0.001"),
            # LADDER (Phase 2, 2026-08-02): fill-anchored used to rest ONE bid +
            # ONE ask of deployed/levels and then wait in position — it deployed
            # a FRACTION of the budget the classic grid ladder deploys with the
            # same settings. Now the full budget is spread across ``levels``
            # levels stepping away from the anchor, restoring parity with the
            # classic ladder and giving the strategy something to scale in and
            # out of. A participation chunk still sizes the side directly.
            # rgrid is EXCLUDED from the ladder: it runs taker-momentum, firing
            # ONE market order of order_amount_quote per break. Handing it the
            # full side deployment would multiply its step size by ``levels``
            # while never resting a ladder at all, so it keeps the per-step
            # sizing it has always had.
            # rgrid fires ONE taker of this size per break, so its step stays
            # deployed/levels. A POV chunk may only make that step SMALLER:
            # the $1000 throughput floor is capped at the deployed budget.
            "order_amount_quote": _chunk_dec or Decimal(str(deployed)),
            "ladder_levels": levels,
            # Step by the quoted spread, so level i sits at the anchor ± (i+1)
            # spreads — the same geometry as the classic grid ladder.
            "ladder_step_bp": (spread_frac if spread_frac > 0 else Decimal("0.001")) * Decimal(10000),
            "ladder_curve": str(settings.get("size_curve") or "flat"),
            "min_quote_lifetime_s": _min_quote_lifetime_s(strategy, settings),
            "price_distance_tolerance": (spread_frac / Decimal(2)) or Decimal("0.0005"),
            "leverage": int(eff_lev),
            "momentum": False,          # grid is a maker book (no-cross capture)
            # Directional bias. Grid's registry carries a directional_bias setting
            # that nothing read — the fill-anchored controller applies the same skew
            # Mid does, so honour it (and the overlay's signal, which lands on this
            # same key). NOTE: unlike Mid, grid's net-exposure cap is NOT scaled up
            # by |bias| — the lean works inside the cap the user already has, so
            # enabling this cannot raise anyone's exposure.
            "directional_bias": _resolve_directional_bias_value(settings.get("directional_bias")),
            # Two-step stall escalation: after the soft-reset maker leg stalls for N
            # ticks, a bounded reduce-only taker concession flattens part of the
            # one-sided exposure before the SL rail.
            "concession_enabled": True,
            "concession_escalation_ticks": int(max(1.0, _f(settings, "grid_concession_ticks", 5.0))),
            "concession_fraction": _f(settings, "grid_concession_fraction", 0.5),
            **_quote_defense_defaults(settings, deployed, auto_spread=spread_frac <= 0),
            # GRID-IN-TRENDS default: quote in every regime (inventory cap +
            # soft-reset + session SL/TP rail are the backstops). An explicit
            # regime_gate_enabled=1 re-arms the gate.
            **({"regime_gate_enabled": 0.0} if "regime_gate_enabled" not in settings else {}),
        }
    #
    # NO_ORDERS_AUDIT-FIX-R4: spread_bp is now interpreted as the per-level
    # STEP (distance between adjacent grid levels), not the total band. With
    # `levels` levels stepping by `spread_frac` each:
    #
    #   * grid  (BUY, long):  N levels stepping DOWN from mid.
    #                         span = (levels - 1) * spread_frac
    #                         start = mid * (1 - span)  (lo)
    #                         end   = mid               (hi)  — all buys ≤ mid
    #
    #   * dgrid: side is chosen dynamically per tick. We DON'T fix the band
    #     here; instead we pass ``step_pct`` and ``levels_count`` so the
    #     DynamicGridController can rebuild the side-correct band against
    #     a fresh mid before spawning the executor.
    #
    # Why the old code was wrong: it used a symmetric band (mid ± band) where
    # band = step × levels. A BUY grid then had levels ABOVE mid, which a
    # post-only LIMIT_MAKER buy gets rejected for (would cross the book).
    # Those levels never placed — silent partial failure.

    # Floor the per-level step so a near-zero spread_bp doesn't collapse the
    # grid to one price level (which then divides by zero in
    # generate_grid_levels).
    if spread_frac <= 0:
        spread_frac = Decimal("0.0005")  # 5 bp fallback
    span = spread_frac * Decimal(max(levels - 1, 1))

    # GRID-DUAL-UNIT fix: do NOT derive a hard ``limit_price`` stop from the
    # user's sl_pct. That stop is referenced to the run/rebuild mid and ignores
    # how much of the grid has actually filled, so it fires on a brief wick to
    # mid*(1-sl) even when little is at risk — a premature stop-out on top of the
    # session margin-% rail. SL is now governed by the fee-aware %-of-margin
    # session rail ALONE — the executor avg-entry barrier this comment used to
    # name second is no longer constructed for the ladder either (DGRID-DUAL-UNIT,
    # 2026-08-06: one user number must be a price barrier OR a margin rail, never
    # both). limit_price stays available as an explicit catastrophic stop but is
    # no longer auto-set from sl.
    # POST-ONLY-CROSS fix: the boundary level nearest mid must be a strict maker,
    # or a post-only LIMIT_MAKER placed AT mid crosses the book and the venue
    # rejects it (error_code 2008 — seen on every rgrid SELL and, once grids
    # ladder past one level, on the top BUY too). Offset the near boundary by at
    # least half a grid step (floored at 1.5 bp) onto the maker side: buys
    # strictly below mid, sells strictly above.
    maker_offset = max(spread_frac / Decimal(2), Decimal("0.00015"))
    # Only grid and dgrid reach here (rgrid returned above with its
    # exposure-anchored config): a long band stepping DOWN from mid. dgrid
    # recomputes the side-correct band against a fresh mid at on_tick.
    start_price = mid * (Decimal(1) - maker_offset - span)
    end_price = mid * (Decimal(1) - maker_offset)
    limit_price = Decimal(0)

    cfg: Dict[str, object] = {
        "trading_pair": product,
        "start_price": start_price,
        "end_price": end_price,
        "limit_price": limit_price,
        # Deployed position notional = margin x effective leverage (see top).
        # A participation chunk caps the per-cycle ladder notional when active.
        "total_amount_quote": _chunk_dec or Decimal(str(deployed)),
        "min_spread_between_orders": spread_frac,
        "max_open_orders": levels,
        "leverage": int(eff_lev),
        # Continuous laddering for the whole GridExecutor family (classic grid
        # and D-Grid): re-arm round-tripped levels so
        # the ladder keeps working its band instead of draining to COMPLETE and
        # terminating. Bounded by the net-exposure cap + session rails. (Default
        # grid/rgrid run the fill-anchored controller, which already re-quotes
        # every tick; Mid is the market-making controller, likewise continuous —
        # this flag only affects the multi-level ladder executor.)
        "recycle_levels": True,
        # No ``triple_barrier_config``: the user's SL/TP are %-of-margin session
        # rails, never also a per-level price barrier (see the UNITS INVARIANT note
        # above). GridExecutorConfig defaults it to None and every read is guarded.
        # NO_ORDERS_AUDIT-FIX-R4: extra knobs consumed by DynamicGridController
        # so it can rebuild the side-correct band on the fly. Ignored by
        # GridController / ShortLadderController.
        "step_pct": spread_frac,
        "levels_count": levels,
        # PERCENT of margin (not the price fraction above) — this is what
        # DynamicGridController's tiered scale-out measures uPnL against.
        "tp_pct": float(_tp_pct),
        "sl_pct": float(_sl_pct),
        # Regime gate + inventory cap + ATR auto-step (2026-06 upgrade).
        # margin_quote = DEPLOYED notional so the net-exposure cap scales with
        # leverage instead of choking a leveraged ladder at 30% of collateral.
        **_quote_defense_defaults(settings, deployed, auto_spread=spread_frac <= 0),
    }
    # NO_ORDERS_AUDIT-FIX-R2: DynamicGridController requires a candle_provider
    # callable to classify the volatility regime. Without one, _candles()
    # returns [] and on_tick exits early — no executor ever spawned, no orders
    # placed. We bind the provider in ``run_engine_cycle`` because it needs
    # access to the live ``client`` and ``product_id`` to call
    # ``client.get_candlesticks(...)``; setting ``"candle_provider": None``
    # here makes the contract explicit and lets the cycle driver inject the
    # real provider on first start.
    if strategy == "dgrid":
        # D-Grid is the phase SWITCHER: a volatility-balanced classifier that
        # mean-reverts with a long ladder in ranges and flips to a short ladder on
        # a clear directional signal. R-Grid used to share this engine, which is
        # why R-Grid users were told "switched RGRID → GRID"; it now runs its own
        # exposure-anchored controller and never reaches this branch.
        cfg["candle_provider"] = None
        # (recycle_levels is set for the whole GridExecutor family above.)
        cfg["dgrid_short_window"] = int(max(2, _f(settings, "dgrid_short_window_points", 4)))
        cfg["dgrid_long_window"] = int(max(4, _f(settings, "dgrid_long_window_points", 12)))
        cfg["dgrid_trend_on_vr"] = _f(settings, "dgrid_trend_on_variance_ratio", 1.25)
        cfg["dgrid_range_on_vr"] = _f(settings, "dgrid_range_on_variance_ratio", 1.15)
        # Sustained-drift trend filter: flip the grid direction on a slow one-way
        # grind the variance ratio misses (a steady decline keeps VR<1 yet bleeds
        # a long grid). Percent over the long window; 0 disables.
        cfg["dgrid_trend_drift_pct"] = _f(settings, "dgrid_trend_drift_pct", 0.30)
        # Tiered profit-booking: scale out reduce-only as the run's uPnL climbs
        # past these tiers, closing dgrid_tp_fraction each time. The tiers are
        # ANCHORED to the user's TP in the controller (top tier == tp_pct), so
        # the ladder completes exactly AT the user's setting and never before —
        # the shape below only sets the RELATIVE spacing of the scale-out.
        cfg["dgrid_tp_tiers_pct"] = settings.get("dgrid_tp_tiers_pct") or [2.0, 4.0, 6.0]
        cfg["dgrid_tp_fraction"] = _f(settings, "dgrid_tp_fraction", 0.33)
        # The user's allocated MARGIN (= notional), so the controller measures
        # the tier % against the SAME basis as the session TP rail — NOT the
        # deployed notional (margin x leverage) that margin_quote carries.
        # The tier ladder is anchored to the user's TP, so its denominator MUST be
        # the margin the session rail measures against — not the deployment basis
        # ``notional``, which resolves cycle-first while the rail resolves
        # notional-first. With cycle_notional_usd=250 and notional_usd=100 the
        # tiers demanded $1/$2/$3 of uPnL while the rail took profit at $1.20 (the
        # ladder dead above rung one); with cycle 50 they fired at half the user's
        # TP. Same hazard the R-Grid step cap already fixed above.
        from src.nadobro.strategy.strategy_registry import (
            session_margin_usd as _rail_margin_usd,
        )

        cfg["tp_margin_basis"] = Decimal(str(_rail_margin_usd(settings) or notional))
        # Confirm-ticks debounce a flip: wait an extra tick to avoid whipsaw.
        cfg["dgrid_flip_confirm_ticks"] = int(max(1, _f(settings, "dgrid_flip_confirm_ticks", 2)))
        # Trend-capture redesign (2026-06): as a run goes in profit, ratchet a
        # trailing take-profit so a reversal still closes green, and flip
        # long<->short on a confirmed price reversal from the run's extreme
        # (faster than waiting for the variance classifier to cross). Percents.
        cfg["dgrid_trail_arm_pct"] = _f(settings, "dgrid_trail_arm_pct", 1.0)
        cfg["dgrid_trail_giveback_pct"] = _f(settings, "dgrid_trail_giveback_pct", 0.5)
        cfg["dgrid_reversal_flip_pct"] = _f(settings, "dgrid_reversal_flip_pct", 0.4)
        # Reset re-center drives an IN-PLACE re-quote of the resting ladder
        # (GridExecutor.recenter) — no flatten, no realized loss — so it can
        # follow price closely without bleeding fees. Pass the user's explicit
        # threshold through when set; otherwise pass 0 so the controller picks
        # its geometry default (~one band width of drift) and the ladder FOLLOWS
        # price as it trends instead of going stale ("placed a few orders and
        # stopped"). 50bp was too coarse — BTC rarely drifts that far inside one
        # 30-60s tick, so it almost never re-centered.
        cfg["dgrid_reset_threshold_bp"] = _f(
            settings, "dgrid_reset_threshold_bp",
            _f(settings, "grid_reset_threshold_pct", 0.0) * 100.0,
        )
        # Financial overlay: below this confidence its read is not worth an extra
        # confirm tick on a flip (see DynamicGridController._required_confirm_ticks).
        cfg["dgrid_signal_min_confidence"] = _f(settings, "dgrid_signal_min_confidence", 0.45)
        # Dynamic Grid's own min/max per-side spread bounds drive the auto-spread
        # clamp (previously dead — _quote_defense_defaults hardcoded the band).
        _dg_floor = Decimal(str(max(0.0, _f(settings, "dgrid_min_spread_bp", 2.0)))) / Decimal(10000)
        _dg_cap = Decimal(str(_f(settings, "dgrid_max_spread_bp", 50.0))) / Decimal(10000)
        cfg["spread_floor_half_pct"] = _dg_floor
        cfg["spread_cap_half_pct"] = max(_dg_cap, _dg_floor)

    # GRID in-place re-center: honor the user's reset threshold so the classic
    # long ladder follows price ("reset and continue") instead of going stale.
    # Drives GridExecutor.recenter (no flatten); the controller floors it above
    # the band. (rgrid never reaches here — it runs the exposure-anchored
    # controller, where the same knob arms its trailing soft reset.)
    if strategy == "grid":
        # Re-center ON by default: pass the user's explicit reset through, else 0
        # so the controller uses its band-width auto-follow (50bp was too coarse —
        # the ladder rarely re-centered and went stale below mid).
        cfg["reset_threshold_bp"] = _f(settings, "grid_reset_threshold_pct", 0.0) * 100.0
        # GRID-IN-TRENDS (user directive 2026-06-21): plain Grid was gated OUT of
        # trends/expansions by default and silently never quoted (the "always
        # market paused" report). Default the regime gate OFF for grid so it
        # quotes in every regime; the inventory net-exposure cap, the in-place
        # recenter, and the live-session SL/TP rails remain the backstops. Still
        # user-overridable: an explicit regime_gate_enabled=1 re-arms the gate.
        if "regime_gate_enabled" not in settings:
            cfg["regime_gate_enabled"] = 0.0
    return cfg


def map_risk_limits(
    settings: Dict[str, Any], strategy: Optional[str] = None, *, leverage: float = 1.0,
) -> RiskLimits:
    # Delta Neutral sizes each leg from ``fixed_margin_usd`` (NOT ``notional_usd``),
    # so its risk caps must follow the leg size or the Risk Engine rejects every
    # leg with ``max_single_order_quote`` and the controller never places an order
    # (the "LIVE but 0 orders" bug). Cap = per-leg notional × headroom; the
    # base-matched short can be slightly larger than the long (mid gap / hedge
    # ratio) and the venue may bump a leg up to its min-notional, hence the 2×.
    if str(strategy or "").lower() == "dn":
        leg = _f(settings, "fixed_margin_usd", _f(settings, "notional_usd", 100.0))
        hedge = max(1.0, _f(settings, "dn_hedge_ratio", 1.0))
        per_order_cap = max(1.0, leg * hedge) * 2.0
        return RiskLimits(
            # 2 legs + headroom for the close/trim and next-cycle overlap.
            max_open_executors=6,
            max_single_order_quote=Decimal(str(per_order_cap)),
            max_position_size_quote=Decimal(str(per_order_cap)),
        )
    # Volume bot sizes off session_margin_usd (same key order as its config
    # branch). The generic grid fallback below reads cycle_notional_usd /
    # notional_usd — keys vol never sets — so EVERY vol session got $100 caps:
    # a configured $500 margin failed its first buy spawn outright, and even
    # the default $100 died on the close because the venue lot-rounds the buy
    # fill UP (prod session 104: held $101.01, sell request > $100 cap →
    # rejected 1.4s after the fill, spot stranded). Headroom covers lot
    # rounding plus the touch moving between the two legs.
    if str(strategy or "").lower() == "vol":
        vol_margin = _f(
            settings, "session_margin_usd",
            _f(settings, "cycle_notional_usd", _f(settings, "notional_usd", 100.0)),
        )
        vol_cap = max(10.0, vol_margin) * 1.25 + 25.0
        return RiskLimits(
            # Resting leg + its replacement mid-requote + one recovery spawn.
            max_open_executors=3,
            max_single_order_quote=Decimal(str(vol_cap)),
            max_position_size_quote=Decimal(str(vol_cap)),
        )
    # Grid/MM family: caps must follow the DEPLOYED notional (margin x leverage),
    # or the Risk Engine rejects every leveraged order — the same "LIVE but 0
    # orders" failure the DN branch above guards against. With leverage=1 this is
    # identical to the legacy margin-sized behavior (tests rely on that).
    margin = _f(settings, "cycle_notional_usd", _f(settings, "notional_usd", 100.0))
    eff_lev = _effective_leverage(settings, float(leverage or 1.0))
    deployed = margin * eff_lev
    levels = max(1, int(_f(settings, "levels", 2)))
    cap = _f(settings, "session_notional_cap_usd", 0.0) or (deployed * levels)
    # LADDER-EXECUTOR-STARVATION (2026-08-03): ``levels + 2`` was sized for the
    # CLASSIC grid, where one GridExecutor holds every level. The laddered
    # quoters spawn one OrderExecutor per level PER SIDE, so they need 2 x
    # levels. Under the old cap, `_quote_side(BUY)` runs first and eats the
    # budget, and from levels=3 up the deep ask rungs were rejected every tick
    # with `risk:max_open_executors` — a permanent 2:1 long-skewed book that
    # accumulates inventory in a downtrend with half the depth to unwind it.
    # Mid ladders unconditionally; GRID only on its fill-anchored path. rgrid is
    # excluded on purpose: it rests exactly one quote per side (ladder_levels=1)
    # and its own toggle was removed, so an fill_anchored=1 left behind in a
    # user's bot_state from that removed button would otherwise still double
    # R-Grid's executor budget for a book that never uses it.
    _laddered = strategy == "mid" or (
        strategy == "grid" and bool(_f(settings, "fill_anchored", 0.0))
    )
    _executor_budget = (2 * levels + 2) if _laddered else (levels + 2)
    return RiskLimits(
        max_open_executors=_executor_budget,
        # A single bumped level can be up to the whole deployed size (when the
        # min-notional cap collapses the ladder to one level).
        max_single_order_quote=Decimal(str(deployed)),
        max_position_size_quote=Decimal(str(max(cap, deployed * 1.2))),
    )


def _persist_dn_progress(telegram_id: int, network: str, strategy: str) -> None:
    """Write the live DN controller progress (cycles-completed + funding-earned
    + phase) to engine_controller_state so the main process can surface it in
    /status. Cross-process via the deterministic controller id. Best-effort —
    never breaks a cycle."""
    if strategy != "dn":
        return
    try:
        controller = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
        if controller is None:
            return
        from src.nadobro.trading.engine_persistence import upsert_controller_progress

        upsert_controller_progress(
            controller.id,
            telegram_id,
            strategy=strategy,
            network=network,
            cycles_completed=int(getattr(controller, "cycles_completed", 0) or 0),
            funding_earned_usd=getattr(controller, "cumulative_funding", 0) or 0,
            phase=str(getattr(getattr(controller, "phase", None), "value", "") or ""),
        )
    except Exception:  # noqa: BLE001 - persistence must not break a cycle
        logger.debug("dn progress persist failed", exc_info=True)


def _friendly_start_error(reason: str) -> str:
    """Map an internal spawn-rejection reason to a user-facing message."""
    low = str(reason or "").lower()
    if "max_single_order_quote" in low or "max_position_size_quote" in low:
        return ("Order size exceeds this strategy's risk limit. Lower the Size or "
                "raise the cap, then start again.")
    if "max_open_executors" in low:
        return "Too many open legs for this strategy's risk limit."
    if "kill_switch" in low:
        return "Trading is paused (kill switch active). Try again shortly."
    if "insufficient" in low or "margin" in low or "health" in low:
        return "Not enough margin to open both legs. Add margin or lower the Size."
    return f"Strategy failed to start: {reason}" if reason else "Strategy failed to start."


def _opt_int(value: object) -> Optional[int]:
    """``int(value)`` or ``None`` if missing/unparseable (typed so mypy is happy
    with ``Any | None`` catalog fields)."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _x18_dec(value: object, default: str) -> Decimal:
    """Convert an x18-scaled catalog field (stored as an int-ish string, value
    × 1e18) to a real Decimal. Falls back to ``default`` when missing/unparseable
    — but a legitimate 0 is preserved."""
    if value is None:
        return _dec(default)
    try:
        return _dec(str(int(str(value)))) / _dec(10 ** 18)
    except (TypeError, ValueError):
        try:
            return _dec(value)
        except Exception:  # noqa: BLE001
            return _dec(default)


def build_product_meta_from_catalog(client: object) -> Dict[str, object]:
    """{trading_pair -> ProductMeta} from the live product catalog, with each
    product's real tick_size / lot_size / min_notional plus is_perp /
    isolated_only.

    The previous implementation expected ``client.get_all_products_info()`` to
    return a ``"products"`` list of richly-described dicts, but that method
    actually returns ``{"perp": [{"id": ...}], "spot": [{"id": ...}]}`` — so it
    produced an EMPTY dict and every engine strategy silently fell back to the
    permissive ``ProductMeta(pid, 0.01, 0.001, 1)`` with wrong increments. We now
    read the resolved perp + spot catalogs (which carry the x18-scaled
    increments and the isolated_only flag) and key each product under its base,
    canonical symbol, and — for perps — the ``BASE-PERP`` alias, so whichever
    string a strategy uses for ``trading_pair`` resolves.
    """
    from src.nadobro.engine.adapter.nado import ProductMeta
    from src.nadobro.venue import product_catalog as pc

    out: Dict[str, object] = {}
    network = str(getattr(client, "network", None) or "mainnet")

    def _register(meta: object, keys: Iterable[str], *, overwrite: bool) -> None:
        for key in keys:
            k = str(key or "").strip()
            if k and (overwrite or k not in out):
                out[k] = meta

    # Perps: id + price/size increments + min notional + isolated flag.
    try:
        perps = (pc.get_catalog(network=network, client=client) or {}).get("perps") or {}
    except Exception:  # noqa: BLE001
        logger.warning("perp catalog unavailable", exc_info=True)
        perps = {}
    for base, row in (perps.items() if isinstance(perps, dict) else []):
        if not isinstance(row, dict):
            continue
        pid = _opt_int(row.get("id"))
        if pid is None:
            continue
        meta = ProductMeta(
            product_id=pid,
            tick_size=_x18_dec(row.get("price_increment_x18"), "0.01"),
            lot_size=_x18_dec(row.get("size_increment_x18"), "0.001"),
            min_notional=_x18_dec(row.get("min_size_x18"), "1"),
            is_perp=True,
            isolated_only=bool(row.get("isolated_only")),
        )
        symbol = str(row.get("symbol") or f"{base}-PERP")
        # Market-qualified aliases (``BASE-PERP``) let a caller that handles
        # BOTH markets for the same base — the Desk controller — address the
        # perp unambiguously even when a spot listing shares the base.
        _register(meta, (base, symbol, f"{base}-PERP", f"{symbol}-PERP"), overwrite=True)

    # Spots: id + (best-effort) increments; never isolated. Don't clobber a perp
    # alias on a base collision (perps registered first).
    try:
        spots = (pc.get_spot_catalog(network=network) or {}).get("spots") or {}
    except Exception:  # noqa: BLE001
        logger.warning("spot catalog unavailable", exc_info=True)
        spots = {}
    for base, row in (spots.items() if isinstance(spots, dict) else []):
        if not isinstance(row, dict):
            continue
        pid = _opt_int(row.get("id"))
        if pid is None:
            continue
        meta = ProductMeta(
            product_id=pid,
            tick_size=_x18_dec(row.get("price_increment_x18"), "0.01"),
            lot_size=_x18_dec(row.get("size_increment_x18"), "0.001"),
            min_notional=_x18_dec(row.get("min_size_x18"), "1"),
            is_perp=False,
            isolated_only=False,
        )
        symbol = str(row.get("symbol") or base)
        # Base/symbol keys must NOT clobber a perp on a dual-listed base (a
        # grid/MM strategy keyed by bare base expects its perp). But the
        # ``BASE-SPOT`` alias is spot-only and collision-free — it's how the
        # Desk controller routes a spot plan on a dual-listed asset (e.g.
        # "buy 2 ETH" -> ETH-SPOT) to the SPOT product, not the perp.
        _register(meta, (base, symbol), overwrite=False)
        _register(meta, (f"{base}-SPOT", f"{symbol}-SPOT"), overwrite=True)

    return out


def _materialize_dn_leg_meta(
    meta: Dict[str, object],
    configs: Dict[str, Any],
    client: object,
    network: str,
    product: str,
) -> None:
    """Register ProductMeta for the DN spot (long) and perp (short) legs, keyed
    by the exact ``trading_pair_long`` / ``trading_pair_short`` strings the
    controller uses, with each leg's REAL product_id and the perp's
    isolated-only flag resolved from the DN pair catalog.

    Safety: we never fall back to a shared product_id for the two legs (that
    would trade one product twice). If a leg's id can't be resolved we leave it
    unregistered so the adapter raises a clear "Unknown trading pair" and the
    controller fails to start cleanly instead of mis-trading.
    """
    from src.nadobro.engine.adapter.nado import ProductMeta
    from src.nadobro.venue.product_catalog import (
        get_dn_pair,
        is_product_isolated_only,
    )

    base = str(product or "").upper().split("-", 1)[0]
    long_sym = str(configs.get("trading_pair_long") or "")
    short_sym = str(configs.get("trading_pair_short") or "")

    dn: Dict[str, Any] = {}
    try:
        dn = dict(get_dn_pair(base, network=network, client=client) or {})
    except Exception:  # noqa: BLE001 - catalog is best-effort
        logger.warning("dn: get_dn_pair failed for %s", base, exc_info=True)
    try:
        iso = bool(is_product_isolated_only(base, network=network, client=client))
    except Exception:  # noqa: BLE001
        iso = False

    spot_pid = dn.get("spot_product_id")
    perp_pid = dn.get("perp_product_id")

    # AUDIT round 3: these legs used to be built with LITERAL
    # tick/lot/min_notional (0.01 / 0.001 / 1). On kBTC the real values are
    # 1.0 / 0.00005 / 100, so the spot-exit clamp floored a 0.00155 balance to
    # 0.001 and STRANDED ~35% of the leg, and the sub-min MARKET fallback could
    # never fire ($99 > a fake $1 minimum). Reuse the catalog's real meta for the
    # leg's product_id and keep the literals only as a last resort.
    _by_pid = {}
    for _m in meta.values():
        try:
            _by_pid[int(getattr(_m, "product_id"))] = _m
        except (TypeError, ValueError, AttributeError):
            continue

    def _leg_meta(pid: int, *, is_perp: bool, iso: bool) -> object:
        src = _by_pid.get(int(pid))
        if src is not None:
            return ProductMeta(
                int(pid), _dec(getattr(src, "tick_size", "0.01")),
                _dec(getattr(src, "lot_size", "0.001")),
                _dec(getattr(src, "min_notional", "1")),
                is_perp=is_perp, isolated_only=iso,
            )
        logger.warning(
            "dn: no catalog meta for product_id=%s — falling back to placeholder "
            "tick/lot/min_notional, which can strand part of a leg on exit", pid,
        )
        return ProductMeta(int(pid), _dec("0.01"), _dec("0.001"), _dec("1"),
                           is_perp=is_perp, isolated_only=iso)

    if long_sym:
        if spot_pid is not None:
            meta[long_sym] = _leg_meta(int(spot_pid), is_perp=False, iso=False)
        else:
            logger.error(
                "dn: could not resolve SPOT product_id for %s (long leg %s); "
                "leaving unregistered so the controller fails cleanly",
                base, long_sym,
            )

    if short_sym:
        if perp_pid is not None:
            meta[short_sym] = _leg_meta(int(perp_pid), is_perp=True, iso=iso)
        else:
            logger.error(
                "dn: could not resolve PERP product_id for %s (short leg %s); "
                "leaving unregistered so the controller fails cleanly",
                base, short_sym,
            )


# Funding barely moves tick-to-tick (settled hourly), and every read charges
# the archive host's shared per-IP token bucket — cache per (network, pid).
_OVERLAY_FUNDING_TTL = 300.0
_OVERLAY_FUNDING_CACHE: Dict[tuple, tuple[float, Optional[float]]] = {}
# Persist the overlay signal only when the APPLIED action changed, plus a
# heartbeat so Night HOWL still sees a quiet-but-alive overlay.
#
# The heartbeat doubles as the sampling floor for signal grading
# (``llm/signal_scorer.py``): every row written here becomes one labeled example
# per horizon, so a quiet regime still produces evidence instead of a gap.
# 10 minutes is denser than the shortest graded horizon (15m), which is
# deliberate — but it also means consecutive rows share most of their forward
# window, so grades are strongly autocorrelated and the effective sample size is
# well below the row count. ``quant/scoring.MIN_SAMPLES_FOR_TRUST`` is the floor
# for acting on a metric; treat it as a lower bound, not a guarantee.
_OVERLAY_PERSIST_HEARTBEAT = env_float("NADO_OVERLAY_PERSIST_HEARTBEAT_SECONDS", 600.0)


async def _maybe_apply_overlay(
    telegram_id: int,
    network: str,
    strategy: str,
    product: str,
    product_id: object,
    configs: Dict[str, object],
    state: Dict[str, Any],
    *,
    client: object,
    mid: float,
    limits: Optional[RiskLimits] = None,
) -> None:
    """Compute the multi-timeframe signal and apply BOUNDED overrides to the
    mapped ``configs`` in place. Best-effort: any failure leaves the base config
    untouched. Candle fetch + persistence run off the event loop.

    ``limits`` are the risk caps mapped from the user's settings BEFORE the
    overlay ran; the per-order size is clamped back to them (see below)."""
    try:
        from src.nadobro.strategy.overlay_actuator import (
            APPLIED_OVERRIDE_KEYS,
            apply_overrides_to_configs,
            compute_overrides,
            overlay_applies,
            rail_barriers,
            stabilize_overrides,
        )

        if not overlay_applies(strategy):
            return
        pid = _opt_int(product_id)
        if pid is None or client is None or not hasattr(client, "get_candlesticks"):
            return

        from src.nadobro.core.async_utils import run_blocking, run_blocking_sdk
        from src.nadobro.strategy import market_features as _mf
        from src.nadobro.llm.signal_engine import build_signal
        from src.nadobro.strategy.strategy_registry import effective_sl_tp_pct

        def _gather() -> Dict[str, Dict[str, object]]:
            def _fetch(p, tf, lim):
                return client.get_candlesticks(int(p), tf, int(lim))  # type: ignore[attr-defined]

            return _mf.multi_tf_features(_fetch, network, int(pid))

        features = await run_blocking_sdk(_gather)
        if not features:
            return

        # Live funding + current position side so the signal can flag when the
        # carry is hostile to what the strategy is actually holding. Funding is
        # one batched indexer read (off-loop, best-effort); position side is read
        # from the controller's in-memory inventory — no extra SDK call.
        funding_rate: Optional[float] = None
        if hasattr(client, "get_perp_funding_rates"):
            _fkey = (str(network), int(pid))
            _fhit = _OVERLAY_FUNDING_CACHE.get(_fkey)
            if _fhit is not None and (time.time() - _fhit[0]) < _OVERLAY_FUNDING_TTL:
                funding_rate = _fhit[1]
            else:
                try:
                    def _funding():
                        return client.get_perp_funding_rates([int(pid)])  # type: ignore[attr-defined]

                    rates = await run_blocking_sdk(_funding) or {}
                    entry = rates.get(int(pid)) or rates.get(str(pid)) or {}
                    raw = entry.get("funding_rate") if isinstance(entry, dict) else None
                    funding_rate = float(raw) if raw is not None else None
                    _OVERLAY_FUNDING_CACHE[_fkey] = (time.time(), funding_rate)
                except Exception:  # noqa: BLE001 - funding context is optional
                    funding_rate = None

        position_side: Optional[str] = None
        try:
            controller = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
            inv = getattr(controller, "inventory", None)
            if controller is not None and inv is not None:
                hold = inv.get(telegram_id, controller.trading_pair, controller.id)
                net_base = float(getattr(hold, "net_amount_base", 0) or 0)
                if net_base > 0:
                    position_side = "long"
                elif net_base < 0:
                    position_side = "short"
        except Exception:  # noqa: BLE001 - position side is an optional enrichment
            position_side = None

        base_sl, base_tp = effective_sl_tp_pct(strategy, state)
        signal = build_signal(
            features,
            funding_rate=funding_rate,
            position_side=position_side,
            base_sl_pct=(base_sl if base_sl > 0 else 0.5),
            base_tp_pct=(base_tp if base_tp > 0 else 1.0),
        )
        # ADVISORY TIER (NanoGPT / DMind). Read-through cache: a HIT shades the
        # deterministic signal in the risk-REDUCING direction only; a MISS returns
        # it untouched and refreshes in the background, so a cycle never waits on
        # inference. Disabled or unconfigured ⇒ no change at all.
        from src.nadobro.llm.signal_advisor import advise as _advise

        signal, advisor_verdict = await _advise(
            signal, features, network=network, product=product
        )
        overrides = compute_overrides(strategy, signal)
        # USER-BIAS-WINS (2026-07-30): Mid's directional_bias is a user-facing
        # Long/Short/Neutral control, but the overlay stamped the AI signal's
        # bias over it every cycle (even at confidence 0.01), so the setting
        # never reached live quoting. The overlay may steer the lean only while
        # the user is neutral; an explicit user lean is a binding contract.
        # USER-BIAS-WINS: grid joined mid as a bias consumer, so the guard must
        # cover it too or the overlay silently overrides the user's own lean —
        # the exact defect AUDIT-MID-2026-07-30 fixed for mid.
        if strategy in ("mid", "grid") and "directional_bias" in overrides:
            from src.nadobro.quant.mm_quote_math import _resolve_directional_bias_value

            if abs(_resolve_directional_bias_value(state.get("directional_bias"))) > 1e-9:
                overrides.pop("directional_bias")
        # Dead-band against the previously APPLIED values so a 4th-decimal
        # signal wobble doesn't flip the live-config signature every candle
        # refresh (each flip recenters the grid ladder / resets Mid quotes).
        prev_applied = state.get("overlay_applied")
        overrides = stabilize_overrides(
            prev_applied if isinstance(prev_applied, dict) else None, overrides
        )
        applied = {k: overrides.get(k) for k in APPLIED_OVERRIDE_KEYS if k in overrides}
        applied_changed = applied != prev_applied
        state["overlay_applied"] = applied
        changed = apply_overrides_to_configs(strategy, configs, overrides)
        # Hand the signal's READ (not just its effects) to the controllers that can
        # use it: D-Grid weighs it when deciding whether to flip phase, R-Grid and
        # the cards surface it so a decision can be explained after the fact.
        configs["signal_regime"] = str(signal.regime)
        configs["signal_bias"] = float(signal.bias)
        configs["signal_confidence"] = float(signal.confidence)
        if advisor_verdict is not None:
            state["overlay_advisor"] = {
                "provider": advisor_verdict.get("provider"),
                "agree": bool(advisor_verdict.get("agree", True)),
                "confidence_delta": float(advisor_verdict.get("confidence_delta") or 0.0),
            }
        else:
            state.pop("overlay_advisor", None)

        # OVERLAY-SIZE-VS-RISK-CAP (2026-07-30): the overlay's size_factor can
        # scale order_amount_quote up to 1.25x, but ``limits`` were mapped from
        # the user's settings before the overlay — for Mid the base order
        # already sits exactly AT max_single_order_quote (one full deployed
        # quote per side), so any up-scale made the risk engine refuse every
        # spawn ("LIVE but 0 orders", the DN/vol cap-bug class). The user's
        # deployed margin is the binding budget: clamp the per-order size back
        # to the cap rather than raising the cap.
        _cap = getattr(limits, "max_single_order_quote", None)
        _oaq = configs.get("order_amount_quote")
        if _cap is not None and _oaq is not None and Decimal(str(_oaq)) > _cap:
            configs["order_amount_quote"] = _cap
            changed["order_amount_quote"] = str(_cap)
        # R-GRID STEP CAP is a RISK bound, not a preference: the step was sized so
        # one taker round trip stays inside the session stop budget. The overlay's
        # size_factor (up to 1.25x) is applied AFTER that and is otherwise only
        # clamped back to max_single_order_quote, which sits far above the capped
        # step — so a confident signal quietly turned a "33% of the stop per round
        # trip" budget into ~41%. The overlay may shrink the step, never grow it
        # past what the stop can absorb. (Audit 2026-08-06.)
        if strategy == "rgrid":
            _step_cap = configs.get("step_capped_quote")
            if _step_cap is not None and _oaq is not None:
                _now = Decimal(str(configs.get("order_amount_quote")))
                if _now > Decimal(str(_step_cap)):
                    configs["order_amount_quote"] = Decimal(str(_step_cap))
                    changed["order_amount_quote"] = str(_step_cap)

        # Surface the regime-adjusted barriers to the session SL/TP rail (which
        # reads state, not configs). Persisted with state at end of cycle, so
        # the rail uses them from the next cycle. Bounded by the user's own
        # config: SL is tighten-only (the user's session SL stays the
        # kill-switch contract) and a barrier the user disarmed stays disarmed.
        rail_sl, rail_tp = rail_barriers(base_sl, base_tp, signal)
        if rail_sl is not None:
            state["overlay_sl_pct"] = float(rail_sl)
        else:
            state.pop("overlay_sl_pct", None)
        if rail_tp is not None:
            state["overlay_tp_pct"] = float(rail_tp)
        else:
            state.pop("overlay_tp_pct", None)

        # Persist the signal + applied action for Night HOWL / audit (off loop).
        # Throttled: only when the APPLIED action changed, plus a heartbeat —
        # an unconditional insert every tick bloats overlay_signals by ~2k
        # rows/day/session without adding information.
        _last_persist = float(state.get("overlay_persist_ts") or 0.0)
        if not applied_changed and (time.time() - _last_persist) < _OVERLAY_PERSIST_HEARTBEAT:
            if changed:
                logger.debug(
                    "overlay held steady user=%s strategy=%s regime=%s",
                    telegram_id, strategy, signal.regime,
                )
            return
        try:
            from src.nadobro.models.database import insert_overlay_signal

            row = {
                "user_id": int(telegram_id),
                "network": network,
                "strategy": strategy,
                "product_id": int(pid),
                "product_name": str(product or ""),
                "strategy_session_id": _opt_int(state.get("strategy_session_id")),
                "bias": signal.bias,
                "regime": signal.regime,
                "confidence": signal.confidence,
                "entry_ok": bool(signal.entry_ok),
                "scale": signal.scale,
                "spread_mult": signal.spread_mult,
                "sl_pct": signal.sl_pct,
                "tp_pct": signal.tp_pct,
                "action_json": changed,
                "reasons_json": list(signal.reasons),
                "risks_json": list(signal.risks),
            }
            await run_blocking(insert_overlay_signal, row)
            state["overlay_persist_ts"] = time.time()
        except Exception:  # noqa: BLE001 - persistence must never break a tick
            logger.debug("overlay signal persist failed", exc_info=True)

        if changed:
            logger.info(
                "overlay applied user=%s network=%s strategy=%s regime=%s bias=%.2f conf=%.2f changed=%s",
                telegram_id, network, strategy, signal.regime, signal.bias,
                signal.confidence, list(changed.keys()),
            )
    except Exception:  # noqa: BLE001 - overlay is advisory; never break the cycle
        logger.warning("overlay apply failed user=%s strategy=%s", telegram_id, strategy, exc_info=True)


# (loop, lock) per (user, network, strategy). An asyncio.Lock is bound to the loop
# that first awaits it, so the loop is part of the identity — see _cycle_lock.
_CYCLE_LOCKS: Dict[str, tuple] = {}
_warned_mp_cycle_lock = False


def _cycle_lock(telegram_id: int, network: str, strategy: str) -> "asyncio.Lock":
    """One lock per (user, network, strategy) so build+start is atomic.

    AUDIT round 3 — two hazards this deliberately handles:

    1. LOOP BINDING. An asyncio.Lock belongs to the loop that first awaits it.
       bot_runtime normally runs every cycle on the latched ``_runtime_loop``, but
       ``_run_engine_start_sync`` can fall through to ``asyncio.run()`` when no
       loop is latched — which would cache a lock bound to a loop that is closed
       moments later, and every later cycle for that key would raise
       "got Future attached to a different loop". The loop is therefore part of the
       cache identity: a different (or dead) loop gets a fresh lock. Exclusion
       within a loop is all an asyncio primitive can offer, and that is the case
       the DN double-open race actually lives in.

    2. CROSS-PROCESS. If NADO_RUNTIME_MODE is ever switched to multiprocess, cycles
       run in worker PROCESSES and a module-level dict in the parent excludes
       nothing — the double-open would come back SILENTLY. Production is
       RUNTIME_MODE=single / NADO_USE_MULTIPROCESS_STRATEGIES=false today (verified
       on Fly), so this warns loudly rather than pretending to protect.
    """
    import asyncio as _aio

    global _warned_mp_cycle_lock
    if not _warned_mp_cycle_lock:
        try:
            from src.nadobro.runtime.runtime_supervisor import is_multiprocess_enabled

            if is_multiprocess_enabled():
                _warned_mp_cycle_lock = True
                logger.error(
                    "CYCLE-LOCK is in-process only, but the runtime is in "
                    "MULTIPROCESS mode — build+start is NOT serialized across "
                    "workers and a duplicate open (e.g. a Delta Neutral double "
                    "hedge) can recur. Use single mode or add a cross-process lock."
                )
        except Exception:  # noqa: BLE001  # policy: degrade-ok(multiprocess probe is a diagnostic; a failed import loses the warning, never the lock)
            pass

    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        loop = None

    key = f"{telegram_id}:{network}:{strategy}"
    cached = _CYCLE_LOCKS.get(key)
    if cached is not None:
        cached_loop, cached_lock = cached
        if cached_loop is loop and (loop is None or not loop.is_closed()):
            return cached_lock
        logger.warning(
            "CYCLE-LOCK rebound for %s: the cached lock belongs to a different or "
            "closed event loop. Serialization holds WITHIN a loop only.", key,
        )
    lock = _aio.Lock()
    _CYCLE_LOCKS[key] = (loop, lock)
    return lock


def release_cycle_lock(telegram_id: int, network: str, strategy: str) -> None:
    """Drop a cached lock when a strategy stops, so _CYCLE_LOCKS cannot grow
    without bound across long-lived processes."""
    _CYCLE_LOCKS.pop(f"{telegram_id}:{network}:{strategy}", None)


async def run_engine_cycle(
    telegram_id: int,
    network: str,
    state: Dict[str, Any],
    client: object,
    mid: float,
    product: str,
    product_id: int,
) -> dict:
    """Serialized entry point for one engine cycle.

    DN-DOUBLE-OPEN (prod session 167, 2026-07-28). ``_should_build_controller``
    is a read-then-act check with NO mutual exclusion, and bot_runtime's eager
    kickoff calls this function DIRECTLY — outside the ``_job_locks`` that
    serialize scheduled cycles. Those two raced 0.4s apart on a Delta Neutral
    start: both saw ``is_running`` False, both built a controller, both ran
    ``on_start``, and the hedge opened TWICE (``engine_started ...
    active_executors=2`` logged twice, 1.65s apart). One orphaned spot leg was
    left behind and the user ended up holding ~$99 of UNHEDGED spot BTC.

    A per-user lock makes build+start atomic, so the second caller sees the
    first's controller and ticks it instead of opening a second position. This
    guards every engine-mapped strategy, not just DN — any of them can be
    double-started by the same race.
    """
    strategy = str(state.get("strategy") or "")
    async with _cycle_lock(telegram_id, network, strategy):
        return await _run_engine_cycle_locked(
            telegram_id, network, state, client, mid, product, product_id,
        )


async def _run_engine_cycle_locked(
    telegram_id: int,
    network: str,
    state: Dict[str, Any],
    client: object,
    mid: float,
    product: str,
    product_id: int,
) -> dict:
    """Gated per-cycle driver called from bot_runtime's async loop. Starts the
    controller on first cycle, ticks it thereafter. Returns a dispatch-style
    result dict. Live execution validated on testnet.

    Always call via ``run_engine_cycle`` — this body is NOT re-entrant."""
    from src.nadobro.trading.engine_persistence import DbInventoryRepository

    strategy = str(state.get("strategy") or "")
    if strategy not in ENGINE_MAPPED_STRATEGIES:
        return {"success": False, "error": f"strategy '{strategy}' not engine-mapped"}

    settings = {k: v for k, v in state.items() if not isinstance(v, (dict, list))}
    _start_lev = _f(settings, "leverage", 1)
    configs = map_strategy_config(strategy, settings, _dec(mid), product=product,
                                  leverage=int(_start_lev), network=network)
    limits = map_risk_limits(settings, strategy, leverage=_start_lev)

    # Financial overlay (background, no user config): compute a multi-timeframe
    # signal and apply BOUNDED overrides to the mapped configs before the
    # live-reconfigure path pushes them to the controller. Best-effort — any
    # failure leaves the base config untouched so a tick never breaks. The
    # overlay's own drawdown kill-switch lives in bot_runtime's session rail.
    await _maybe_apply_overlay(
        telegram_id, network, strategy, product, product_id, configs, state,
        client=client, mid=mid, limits=limits,
    )

    # NO_ORDERS_AUDIT-FIX-R2: inject the dgrid candle_provider HERE, because
    # this is where ``client`` and ``product_id`` are available. The provider
    # closure is recreated on every cycle, which is fine — the controller only
    # caches a reference at on_start.
    if strategy in ("grid", "rgrid", "dgrid", "mid") and configs.get("candle_provider") is None:
        _cli = client
        _pid = int(product_id)

        async def _candle_provider(_pair: str) -> list:
            # ASYNC: get_candlesticks is a sync SDK/REST call; with the gate
            # this provider now runs on EVERY tick of four strategies, so it
            # must go through the SDK thread pool instead of blocking the
            # event loop (the exact starvation class the blocking lint guards
            # — invisible here because the call is one level indirect).
            try:
                from src.nadobro.core.async_utils import run_blocking_sdk

                # 1m candles, last 200 — enough for ema_slow_period=50 + atr_window=14.
                return await run_blocking_sdk(
                    _cli.get_candlesticks, _pid, timeframe="1m", limit=200  # type: ignore[attr-defined]
                ) or []
            except Exception:  # noqa: BLE001 - never let candle fetch break a tick
                logger.warning(
                    "candle_provider failed for pair=%s product_id=%s",
                    _pair, _pid, exc_info=True,
                )
                return []

        configs["candle_provider"] = _candle_provider

    # BUG-TICK-1 recovery: if the local controller went terminal-FAILED (a
    # genuinely fatal error, or an exhausted transient-error streak), force a
    # rebuild even though a stale executor row may still make is_running()
    # report True via _remote_active. start() tears the FAILED controller down
    # before replacing it, so this cleanly resurrects a stalled strategy.
    needs_recovery = RUNTIME.needs_recovery(telegram_id, network, strategy)
    if needs_recovery:
        logger.warning(
            "engine controller FAILED for user=%s network=%s strategy=%s — "
            "rebuilding (BUG-TICK-1 recovery)",
            telegram_id, network, strategy,
        )
    # Build decision. RUNTIME.tick only works on a LOCAL controller, so the
    # process running this cycle must own one. A worker that runs cycles ADOPTS
    # (builds) when it has no local active controller — even if a dead process
    # left a non-terminated executor row making is_running() True — otherwise a
    # crashed/recycled worker would no-op forever. The main/non-worker fallback
    # still defers to a live owner (is_running True) so it never double-builds.
    from src.nadobro.strategy.bot_runtime import is_process_worker_mode
    _has_local = RUNTIME.has_local_active(telegram_id, network, strategy)
    _worker_mode = is_process_worker_mode()
    _should_build = _should_build_controller(
        needs_recovery=needs_recovery,
        has_local_active=_has_local,
        worker_mode=_worker_mode,
        # is_running is only needed for the non-worker defer case; skip the DB
        # round-trip when the worker will adopt anyway.
        is_running=(False if (_worker_mode and not _has_local)
                    else RUNTIME.is_running(telegram_id, network, strategy)),
    )
    if _should_build:
        meta = build_product_meta_from_catalog(client)
        # ensure the traded pair has metadata (fallback to a permissive default)
        if product not in meta:
            from src.nadobro.engine.adapter.nado import ProductMeta

            meta[product] = ProductMeta(int(product_id), _dec("0.01"), _dec("0.001"), _dec("1"))
        # GRID-MIN-NOTIONAL-INFLATE fix: each grid level is sized
        # total_amount_quote / levels, but the venue bumps any sub-min-notional
        # order UP to its minimum — so a small/many-level grid silently deploys
        # MORE than the configured (and risk-approved) size. Cap the level count
        # so each level is at least the venue minimum. Conservative: only ever
        # REDUCES the level count, never raises it.
        if strategy in ("grid", "rgrid", "dgrid"):
            try:
                _mn = float(getattr(meta.get(product), "min_notional", 0) or 0)
                _tot = float(configs.get("total_amount_quote") or 0)
                if _mn > 0 and _tot > 0:
                    _max_levels = max(1, int(_tot // _mn))
                    _cur = int(configs.get("max_open_orders") or 1)
                    if _cur > _max_levels:
                        configs["max_open_orders"] = _max_levels
                        configs["levels_count"] = _max_levels
                        logger.info(
                            "grid level cap (min-notional): %s -> %s levels "
                            "(total=%.2f min_notional=%.2f) user=%s strategy=%s",
                            _cur, _max_levels, _tot, _mn, telegram_id, strategy,
                        )
            except Exception:  # noqa: BLE001 - cap is best-effort, never block start
                logger.debug("grid min-notional level cap skipped", exc_info=True)
        # NO_ORDERS_AUDIT-FIX-R1: DN needs metadata for BOTH legs (spot long +
        # perp short), each with its OWN product_id. The old fallback keyed both
        # legs to the SAME ``product_id`` — which would have traded the perp
        # twice instead of spot+perp. Resolve real per-leg ids (and the perp's
        # isolated-only flag) from the DN pair catalog.
        if strategy == "dn":
            _materialize_dn_leg_meta(meta, configs, client, network, product)
            # DN-ATTRIBUTION: give the controller a way to ask "what did THIS
            # session actually buy?" from the PERSISTED fills. Injected here (the
            # candle_provider pattern) because this is where the resolved per-leg
            # product_ids exist — the controller only knows pair STRINGS, and the
            # fill rows carry venue product names (KBTC), so names do not join.
            # engine/ stays DB-agnostic; only this closure touches the DB, and it
            # does so off the event loop.
            _sid = state.get("strategy_session_id")
            if _sid:
                _pid_by_pair = {}
                for _pair in (configs.get("trading_pair_long"), configs.get("trading_pair_short")):
                    _m = meta.get(str(_pair or ""))
                    if _m is not None:
                        try:
                            _pid_by_pair[str(_pair)] = int(getattr(_m, "product_id"))
                        except (TypeError, ValueError, AttributeError):
                            continue

                async def _session_fill_provider(
                    pair: str, _sid=int(_sid), _net=network, _map=_pid_by_pair
                ):
                    """Net base this session holds on ``pair``, or None if unknown."""
                    pid = _map.get(str(pair))
                    if pid is None:
                        return None
                    try:
                        from src.nadobro.core.async_utils import run_blocking_db
                        from src.nadobro.models.database import (
                            get_session_net_base_by_product,
                        )

                        by_pid = await run_blocking_db(
                            get_session_net_base_by_product, _sid, _net
                        )
                    except Exception:  # noqa: BLE001  # policy: degrade-ok(unknown -> caller falls back to the in-memory book)
                        logger.warning(
                            "dn: session-fill attribution read failed for %s", pair,
                            exc_info=True,
                        )
                        return None
                    return (by_pid or {}).get(int(pid))

                configs["session_fill_provider"] = _session_fill_provider
        # SPOT-EXIT-GUARANTEE (moved AFTER _materialize_dn_leg_meta, audit round
        # 3: it used to run BEFORE the DN legs were registered, so
        # meta.get("BASE-USDT0") was always None and the floor was DEAD for DN).
        # A leg sized AT the venue minimum cannot be closed
        # by a resting limit — fees plus any adverse tick put the exit notional
        # under the floor, the venue rejects it, and the leg strands naked (kBTC
        # minimum $100 vs a ~$99 DN leg, 2026-07-28). Floor the ENTRY so the EXIT
        # still clears. Only ever RAISES the size, and only for the spot-holding
        # strategies; the perp side has no balance to strand.
        if strategy in ("dn", "vol"):
            try:
                from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional

                _key = "leg_amount_quote" if strategy == "dn" else "total_amount_quote"
                _cur = float(configs.get(_key) or 0)
                _spot_pair = str(configs.get("trading_pair_long") or configs.get("trading_pair") or product)
                _mn = float(getattr(meta.get(_spot_pair), "min_notional", 0) or 0)
                _floor = min_closeable_entry_notional(_mn)
                if _floor > 0 and 0 < _cur < _floor:
                    configs[_key] = _dec(str(_floor))
                    # RISK-LIMITS MUST FOLLOW THE RAISE (audit round 4).
                    # `limits` was computed by map_risk_limits from the PRE-floor
                    # settings — for dn, per_order_cap = fixed_margin_usd * hedge
                    # * 2. A $50 leg therefore gets a $100 cap, and the floored
                    # $115 leg is REJECTED by the risk engine: the session goes
                    # LIVE and places nothing, which is the exact "LIVE but 0
                    # orders" failure that comment block warns about. Recompute the
                    # caps from the size we are actually going to trade. Safe here:
                    # `limits` is not consumed until build_orchestrator below.
                    _size_key = ("fixed_margin_usd" if strategy == "dn"
                                 else "session_margin_usd")
                    limits = map_risk_limits(
                        dict(settings, **{_size_key: _floor}),
                        strategy, leverage=_start_lev,
                    )
                    logger.warning(
                        "%s leg raised %.2f -> %.2f so the EXIT clears the venue "
                        "min notional %.2f on %s; risk caps recomputed to "
                        "single=%s position=%s (user=%s)",
                        strategy, _cur, _floor, _mn, _spot_pair,
                        limits.max_single_order_quote, limits.max_position_size_quote,
                        telegram_id,
                    )
            except Exception:  # noqa: BLE001 - best-effort, never block start
                logger.debug("closeable-entry floor skipped", exc_info=True)
            # DN-CYCLES fix: a rebuild (restart / worker handoff / recovery) used
            # to reset the controller's cycle counter to 0 and re-run the whole
            # configured cycle count. Restore the persisted progress so the
            # rebuilt controller resumes the count instead of restarting it.
            # GUARD: only restore for a run that has already ticked at least once
            # (state["runs"] > 0). A fresh user start has runs == 0, so a stale
            # progress row left by a prior crashed run (the controller_id is
            # stable across runs) can never bleed into a new run.
            if int(_f(state, "runs", 0)) > 0:
                try:
                    from src.nadobro.trading.engine_persistence import (
                        get_controller_progress,
                    )

                    _cid = deterministic_controller_id(strategy, telegram_id, network)
                    _prog = get_controller_progress(_cid) or {}
                    if _prog:
                        configs["restore_cycles_completed"] = int(
                            _prog.get("cycles_completed") or 0
                        )
                        configs["restore_funding_usd"] = _prog.get("funding_earned_usd") or 0
                        # OPTION-1 anti-doubling reconcile: a rebuild (worker
                        # handoff / process restart) loses the in-memory
                        # controller, and RUNTIME.start clears the engine's
                        # inventory view — so on_start would open a SECOND hedge
                        # on top of the prior instance's still-open venue legs,
                        # orphaning the originals (no executor handle manages
                        # them). True "resume" needs executor rehydration; until
                        # then, if the persisted phase shows a cycle was in
                        # progress, FLATTEN any existing DN legs on the venue
                        # before re-opening so the new cycle starts from flat —
                        # never 2x exposure, never an unmanaged orphan. Closing
                        # nothing (already flat) is a safe no-op.
                        _phase = str(_prog.get("phase") or "").upper()
                        if _phase in {"OPENING", "HOLDING", "CLOSING"}:
                            try:
                                from src.nadobro.core.async_utils import run_blocking_sdk
                                from src.nadobro.trading.trade_service import (
                                    close_delta_neutral_legs,
                                )
                                logger.warning(
                                    "dn rebuild mid-cycle (phase=%s) — flattening any "
                                    "existing legs before re-open user=%s product=%s",
                                    _phase, telegram_id, product,
                                )
                                await run_blocking_sdk(
                                    close_delta_neutral_legs,
                                    telegram_id, product, network,
                                    source="dn_rebuild_reconcile",
                                )
                            except Exception:  # noqa: BLE001 - best-effort safety net
                                logger.warning(
                                    "dn rebuild reconcile flatten failed user=%s product=%s",
                                    telegram_id, product, exc_info=True,
                                )
                except Exception:  # noqa: BLE001 - restore is best-effort, never block start
                    logger.debug("dn progress restore skipped", exc_info=True)
        # VOL-STOP-RESET (self-review 2026-07-31): the 5%-of-margin session loss
        # limit is enforced against the CONTROLLER's own cumulative PnL, which
        # lives in memory. A rebuild (recovery / worker handoff) reset it to 0
        # and restarted the whole loss budget — and the max_cycles cap with it,
        # so a run could bleed several multiples of the user's stop. Restore
        # both from the state the worker already persists each cycle. Same
        # runs>0 guard as DN: a fresh start must never inherit a prior run.
        if strategy == "vol" and int(_f(state, "runs", 0)) > 0:
            configs["restore_session_realized_pnl_usd"] = _f(
                state, "session_realized_pnl_usd", 0.0
            )
            configs["restore_cycles_completed"] = int(_f(state, "vol_cycles_completed", 0.0))
            configs["restore_session_volume_usd"] = _f(state, "volume_done_usd", 0.0)
        # Fill-anchored (grid/rgrid) exposure VWAP must be peculiar to THIS
        # session + user. Seed it from the run's OWN recorded fills
        # (get_session_recent_fills is scoped by strategy_session_id + user_id) so
        # the anchor is provably session-scoped and survives a rebuild — never the
        # whole platform or another run/strategy. Empty for a fresh session.
        if configs.get("controller_override") == "fill_anchored":
            try:
                from src.nadobro.core.async_utils import run_blocking_db
                from src.nadobro.trading.engine_persistence import resolve_running_session_id
                from src.nadobro.models.database import get_session_recent_fills

                _sid = await run_blocking_db(
                    resolve_running_session_id, strategy, telegram_id, network
                )
                if _sid:
                    configs["seed_fills"] = await run_blocking_db(
                        get_session_recent_fills,
                        int(_sid), network, limit=_FILL_HISTORY_SEED, user_id=telegram_id,
                    )
            except Exception:  # noqa: BLE001 - seeding is best-effort, never block start
                logger.debug("fill-anchored seed_fills skipped", exc_info=True)
        # Wire placement-time digest→session linking so every venue fill is
        # attributed to this run (source='strategy') even when the executor's own
        # fill detection misses it — the root fix for session-volume undercount.
        _placement_recorder = getattr(RUNTIME, "_trade_recorder", None)
        _placement_cid = deterministic_controller_id(strategy, telegram_id, network)

        def _link_placed_digest(digest: str) -> None:
            if _placement_recorder is not None and hasattr(_placement_recorder, "link_placement"):
                _placement_recorder.link_placement(_placement_cid, digest)

        adapter = build_adapter(client, meta, on_place=_link_placed_digest)
        started_controller = await RUNTIME.start(
            telegram_id, network, strategy, configs, adapter, DbInventoryRepository(),
            limits=limits,
        )
        # Fail loudly when the controller could not start. If on_start raised
        # (e.g. a leg rejected by the risk gate or the venue), spawn_controller
        # marks the controller FAILED and EngineRuntime.start unregisters it.
        # Returning success here is what produced the silent "LIVE but 0 orders"
        # state — instead, surface the reason and let the caller tear down.
        registered = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
        orch = RUNTIME._orchestrators.get((telegram_id, network, strategy))      # noqa: SLF001
        start_failed = registered is None or (
            started_controller is not None
            and started_controller.state is ControllerState.FAILED
        )
        if start_failed:
            reason = (
                getattr(started_controller, "_start_error", None)
                or "controller failed to start"
            )
            logger.error(
                "engine start FAILED user=%s network=%s strategy=%s reason=%s",
                telegram_id, network, strategy, reason,
            )
            try:
                await RUNTIME.stop(telegram_id, network, strategy)
            except Exception:  # noqa: BLE001
                logger.warning("engine start cleanup failed after spawn failure", exc_info=True)
            return {
                "success": False,
                "error": _friendly_start_error(str(reason)),
                "action": "engine_start_failed",
                "strategy": strategy,
            }
        active_n = len(orch.list(registered.id, active_only=True)) if (registered and orch) else 0
        logger.info(
            "engine_started user=%s network=%s strategy=%s active_executors=%s",
            telegram_id, network, strategy, active_n,
        )
        # Seed progress immediately so /status shows it right after Start
        # (DN spawns on_start, so there's no follow-up tick to wait for).
        _persist_dn_progress(telegram_id, network, strategy)
        action = "engine_recovered" if needs_recovery else "engine_started"
        if needs_recovery:
            state["last_recovery_ts"] = time.time()
        start_result: Dict[str, Any] = {"success": True, "action": action, "strategy": strategy}
        try:
            counts_fn = getattr(registered, "order_counts", None)
            if callable(counts_fn):
                _counts = counts_fn() or {}
                if _counts:
                    start_result["order_counts"] = _counts
            if strategy == "vol":
                vol_metrics_fn = getattr(registered, "volume_metrics", None)
                if callable(vol_metrics_fn):
                    _vol_metrics = vol_metrics_fn() or {}
                    start_result.update(_vol_metrics)
                    for key, value in _vol_metrics.items():
                        state[key] = value
        except Exception:  # noqa: BLE001 - start telemetry is best-effort
            logger.debug("engine start telemetry skipped", exc_info=True)
        return start_result

    if strategy in _LIVE_RECONFIGURABLE_STRATEGIES and _has_local:
        controller = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
        orch = RUNTIME._orchestrators.get((telegram_id, network, strategy))      # noqa: SLF001
        if controller is not None and orch is not None:
            old_sig = _live_config_signature(controller.configs, controller.limits)
            new_sig = _live_config_signature(configs, limits)
            if old_sig != new_sig:
                await _apply_live_controller_update(strategy, controller, orch, configs, limits, mid)
                logger.info(
                    "engine live config updated user=%s network=%s strategy=%s",
                    telegram_id, network, strategy,
                )

    # Overlay read -> controller, every cycle and OUTSIDE the live-config path.
    # D-Grid weighs it when deciding a flip and R-Grid surfaces it on /status, but
    # neither may cost a venue round trip: this is a pure attribute write, whereas
    # letting it flip the config signature re-quotes the whole ladder (see
    # _LIVE_CONFIG_SIGNATURE_EXCLUDE).
    try:
        _sig_ctrl = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
        if _sig_ctrl is not None and hasattr(_sig_ctrl, "signal_regime"):
            _sig_ctrl.signal_regime = str(configs.get("signal_regime", "") or "")
            try:
                _sig_ctrl.signal_confidence = float(configs.get("signal_confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                _sig_ctrl.signal_confidence = 0.0
    except Exception:  # noqa: BLE001  # policy: degrade-ok(advisory read; the tick must run)
        logger.debug("overlay signal push failed", exc_info=True)

    await RUNTIME.tick(telegram_id, network, strategy)
    # Regime-gate transition: surfaced exactly once per QUOTE<->PAUSE flip so
    # bot_runtime can notify the user ("paused — trending; resumes on range").
    gate_event = None
    dn_events: list = []
    dgrid_event = None
    dgrid_metrics: Dict[str, Any] = {}
    grid_metrics: Dict[str, Any] = {}
    order_counts: Dict[str, Any] = {}
    vol_metrics: Dict[str, Any] = {}
    try:
        controller = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
        if controller is not None:
            gate_event = controller.consume_gate_event()
            consume_dn = getattr(controller, "consume_dn_events", None)
            if callable(consume_dn):
                dn_events = consume_dn() or []
            # Dynamic Grid: surface the GRID<->RGRID flip (notify once) and the
            # live phase/variance telemetry (powers the /status dashboard,
            # which previously always read GRID / 0.00 / 0.0bp).
            consume_dgrid = getattr(controller, "consume_dgrid_event", None)
            if callable(consume_dgrid):
                dgrid_event = consume_dgrid()
            metrics_fn = getattr(controller, "dgrid_metrics", None)
            if callable(metrics_fn):
                dgrid_metrics = metrics_fn() or {}
            # Grid / Reverse Grid anchor/side/reset telemetry for /status.
            grid_metrics_fn = getattr(controller, "grid_metrics", None)
            if callable(grid_metrics_fn):
                grid_metrics = grid_metrics_fn() or {}
            # Real venue-order activity (placed/filled/cancelled) — the cycle
            # result otherwise carries no count, leaving the per-cycle log and
            # /status stuck at 0 for engine strategies.
            counts_fn = getattr(controller, "order_counts", None)
            if callable(counts_fn):
                order_counts = counts_fn() or {}
            if strategy == "vol":
                vol_metrics_fn = getattr(controller, "volume_metrics", None)
                if callable(vol_metrics_fn):
                    vol_metrics = vol_metrics_fn() or {}
                    for key, value in vol_metrics.items():
                        state[key] = value
            # DN: surface the live funding rate + unfavorable-debounce to
            # /status. These were referenced by the status card but NEVER
            # written anywhere, so the dashboard showed "Rate 0.000000"
            # permanently. The controller refreshes last_funding_rate each
            # (throttled) HOLDING poll; persist it into runtime state here.
            if strategy == "dn":
                _fr = getattr(controller, "last_funding_rate", None)
                if _fr is not None:
                    try:
                        state["dn_last_funding_rate"] = float(_fr)
                    except (TypeError, ValueError):
                        pass
                state["dn_unfavorable_count"] = int(
                    getattr(controller, "funding_unfavorable_count", 0) or 0
                )
    except Exception:  # policy: degrade-ok(gate notify is best-effort)
        gate_event = None
        vol_metrics = {}
    # Persist DN live progress so /status (main process) can read it from the
    # worker that runs the cycle.
    _persist_dn_progress(telegram_id, network, strategy)
    # NO_ORDERS_AUDIT-FIX-DIAG: capture the decisive "why no orders" facts for
    # the grid family (gate verdict/reason, candle count, mid, active executors,
    # last spawn-refusal reason) so bot_runtime can log them in the SERVICES
    # stream — the controllers log these but under engine.controllers, which is
    # easy to miss. Surfaced as result["engine_diag"]; cheap, best-effort.
    engine_diag: Dict[str, Any] = {}
    try:
        orch = RUNTIME._orchestrators.get((telegram_id, network, strategy))  # noqa: SLF001
        controller = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
        if controller is not None:
            active_n = len(orch.list(controller.id, active_only=True)) if orch is not None else None
            spawn_reason = orch.last_spawn_reason(controller.id) if orch is not None else None
            engine_diag = {
                # Controller identity FIRST: the candle/mid/phase/vr telemetry
                # below exists ONLY on the DynamicGrid family. For FillAnchored
                # (Momentum/taker), MarketMaking (mid) and the classic Grid
                # ladder those attributes are absent, and emitting their
                # getattr defaults (candles=0 mid=None phase=None) falsely reads
                # as a starved grid — the exact misdiagnosis this identity + the
                # conditional fields below prevent.
                "controller": type(controller).__name__,
                "active_executors": active_n,
                "gate_verdict": getattr(controller, "gate_verdict", None),
                "gate_reason": getattr(controller, "gate_reason", None) or "",
                "gate_paused": bool(getattr(controller, "gate_paused", False)),
                "spawn_refused": spawn_reason,
            }
            # Only report regime/candle telemetry the controller actually has,
            # so an absent field prints as "n/a" (not a misleading 0/None).
            if hasattr(controller, "_last_candle_count"):
                engine_diag["candle_count"] = int(getattr(controller, "_last_candle_count", 0) or 0)
            if hasattr(controller, "_last_mid"):
                engine_diag["mid"] = str(getattr(controller, "_last_mid", None))
            if hasattr(controller, "current_phase"):
                engine_diag["phase"] = getattr(controller, "current_phase", None)
            if hasattr(controller, "variance_ratio"):
                engine_diag["variance_ratio"] = float(getattr(controller, "variance_ratio", 0.0) or 0.0)
            # RE-QUOTE DIAGNOSTIC: why the ladder did (not) follow price this
            # cycle. A grid that stops moving while the market trends loses its
            # edge, and the gate is invisible without these three numbers:
            # drift from the anchor, the threshold it must clear, and the anchor
            # itself. (Fly retains only ~100 log lines, so this must be on the
            # per-cycle line to be catchable in a real trending window.)
            if hasattr(controller, "realized_move_bp"):
                engine_diag["move_bp"] = round(
                    float(getattr(controller, "realized_move_bp", 0.0) or 0.0), 1)
            if hasattr(controller, "reset_threshold_bp"):
                engine_diag["reset_bp"] = round(
                    float(getattr(controller, "reset_threshold_bp", 0.0) or 0.0), 1)
            _anchor = getattr(controller, "_grid_anchor_mid", None)
            if _anchor is not None:
                engine_diag["anchor"] = str(_anchor)
            logger.debug(
                "engine_ticked user=%s strategy=%s active_executors=%s",
                telegram_id, strategy, active_n,
            )
    except Exception:  # noqa: BLE001  # policy: degrade-ok(diagnostics-only block)
        engine_diag = {}
    result: Dict[str, Any] = {"success": True, "action": "engine_ticked", "strategy": strategy}
    if vol_metrics:
        result.update(vol_metrics)
    # VOL-LOOP completion: a controller that has finished its work (e.g. Volume
    # reached its target volume / cap) signals it via ``completed`` so bot_runtime
    # can finalize the session instead of leaving the strategy idling "running".
    try:
        _ctrl_done = RUNTIME._controllers.get((telegram_id, network, strategy))  # noqa: SLF001
        if _ctrl_done is not None and getattr(_ctrl_done, "completed", False):
            result["done"] = True
            result["action"] = "engine_completed"
            result["stop_reason"] = str(getattr(_ctrl_done, "stop_reason", "") or "completed")
            _vol_done = getattr(_ctrl_done, "session_volume_usd", None)
            if _vol_done is not None:
                result["session_volume_usd"] = float(_vol_done)
    except Exception:  # noqa: BLE001 - completion surfacing is best-effort
        logger.debug("completion surface failed", exc_info=True)
    if engine_diag:
        result["engine_diag"] = engine_diag
    if gate_event:
        result["gate_event"] = gate_event
    if dn_events:
        result["dn_events"] = dn_events
    if dgrid_metrics:
        result["dgrid_metrics"] = dgrid_metrics
    if grid_metrics:
        result["grid_metrics"] = grid_metrics
    if order_counts:
        result["order_counts"] = order_counts
    if dgrid_event:
        result["dgrid_event"] = dgrid_event
    return result
