"""DB-backed persistence for the Engine v2 runtime (production wiring).

Real Postgres implementations (via ``db.py``) of the engine's persistence
seams, so position holds, executor lifecycle, and the risk kill switch survive
restarts and are visible cross-process:

- ``DbInventoryRepository`` — atomic ``apply_fill`` upsert into
  ``engine_position_hold``; satisfies the same interface as the in-memory
  ``InventoryRepository`` (and the Portfolio ``HoldsSource``).
- ``DbExecutorStore`` — upserts executor lifecycle rows into
  ``engine_executors``.
- ``DbKillSwitchStore`` — persists the risk kill switch in
  ``engine_kill_switch`` (migration 0009).

The engine library stays DB-agnostic; these live in the services layer and are
injected by the runtime owner. In-memory variants remain for unit tests.
"""
from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from typing import List, Optional

from src.nadobro.engine.inventory import PositionHold
from src.nadobro.engine.risk import KillSwitchStore
from src.nadobro.engine.types import TradeType, _dec

_STRATEGY_TYPES = {
    "OrderExecutor": "order",
    "PositionExecutor": "position",
    "GridExecutor": "grid",
    "ReverseGridExecutor": "reverse_grid",
    "DCAExecutor": "dca",
    "TWAPExecutor": "twap",
}


# --------------------------------------------------------------------------
# Inventory (engine_position_hold)
# --------------------------------------------------------------------------
def _row_to_hold(row: dict) -> PositionHold:
    return PositionHold(
        user_id=int(row["user_id"]),
        trading_pair=row["trading_pair"],
        controller_id=row["controller_id"],
        buy_amount_base=_dec(row["buy_amount_base"]),
        buy_amount_quote=_dec(row["buy_amount_quote"]),
        sell_amount_base=_dec(row["sell_amount_base"]),
        sell_amount_quote=_dec(row["sell_amount_quote"]),
        cum_fees_quote=_dec(row["cum_fees_quote"]),
    )


class DbInventoryRepository:
    """engine_position_hold-backed inventory. Same surface as the in-memory
    ``InventoryRepository`` so it is a drop-in for executors and Portfolio."""

    def apply_fill(
        self,
        user_id: int,
        trading_pair: str,
        controller_id: str,
        side: TradeType,
        base_qty: object,
        quote_qty: object,
        fee_quote: object = Decimal(0),
        timestamp: Optional[float] = None,
    ) -> PositionHold:
        from src.nadobro.db import execute_returning

        base, quote, fee = _dec(base_qty), _dec(quote_qty), _dec(fee_quote)
        if side is TradeType.BUY:
            bb, bq, sb, sq = base, quote, Decimal(0), Decimal(0)
        else:
            bb, bq, sb, sq = Decimal(0), Decimal(0), base, quote
        row = execute_returning(
            """
            INSERT INTO engine_position_hold
              (user_id, trading_pair, controller_id, buy_amount_base, buy_amount_quote,
               sell_amount_base, sell_amount_quote, cum_fees_quote, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (user_id, trading_pair, controller_id) DO UPDATE SET
              buy_amount_base   = engine_position_hold.buy_amount_base   + EXCLUDED.buy_amount_base,
              buy_amount_quote  = engine_position_hold.buy_amount_quote  + EXCLUDED.buy_amount_quote,
              sell_amount_base  = engine_position_hold.sell_amount_base  + EXCLUDED.sell_amount_base,
              sell_amount_quote = engine_position_hold.sell_amount_quote + EXCLUDED.sell_amount_quote,
              cum_fees_quote    = engine_position_hold.cum_fees_quote    + EXCLUDED.cum_fees_quote,
              updated_at = NOW()
            RETURNING *
            """,
            (user_id, trading_pair, controller_id, bb, bq, sb, sq, fee),
        )
        assert row is not None
        return _row_to_hold(row)

    def get(self, user_id: int, trading_pair: str, controller_id: str) -> PositionHold:
        from src.nadobro.db import query_one

        row = query_one(
            "SELECT * FROM engine_position_hold "
            "WHERE user_id=%s AND trading_pair=%s AND controller_id=%s",
            (user_id, trading_pair, controller_id),
        )
        if row is None:
            return PositionHold(user_id=user_id, trading_pair=trading_pair, controller_id=controller_id)
        return _row_to_hold(row)

    def list_for_user(self, user_id: int) -> List[PositionHold]:
        from src.nadobro.db import query_all

        return [_row_to_hold(r) for r in query_all(
            "SELECT * FROM engine_position_hold WHERE user_id=%s", (user_id,))]

    def list_for_controller(self, user_id: int, controller_id: str) -> List[PositionHold]:
        from src.nadobro.db import query_all

        return [_row_to_hold(r) for r in query_all(
            "SELECT * FROM engine_position_hold WHERE user_id=%s AND controller_id=%s",
            (user_id, controller_id))]

    def clear_for_controller(self, controller_id: str) -> None:
        """Drop the controller's inventory rows. ``controller_id`` is stable
        across runs, so a NEW run must start from a clean hold rather than
        inheriting the prior run's net position (which would skew the engine's
        exposure cap and sizing). Called by EngineRuntime.start."""
        from src.nadobro.db import execute

        try:
            execute("DELETE FROM engine_position_hold WHERE controller_id=%s", (controller_id,))
        except Exception:  # noqa: BLE001 - best-effort; a stale hold must not block start
            pass


# --------------------------------------------------------------------------
# Trade recorder (trades_<network>) — bridges Engine v2 fills into the legacy
# reporting tables so /status, /mm_status, /mm_fills, portfolio cards, the
# per-session rollup, and DB-wide volume all light up for engine strategies.
#
# Why this exists: engine executors persist fills to ``engine_executors`` /
# ``engine_position_hold`` only. Every user-facing reporting surface reads the
# legacy ``trades_<network>`` table (and the ``strategy_sessions`` counters it
# rolls up into). Without this bridge, engine strategies (grid/dgrid/dn/...)
# show 0 fills / 0 volume / 0 PnL even after profitable runs.
# --------------------------------------------------------------------------
import logging as _logging

_recorder_logger = _logging.getLogger(__name__)

# Builder fee is locked at 1.0 bps for Nadobro routing (config:
# NADO_BUILDER_FEE_RATE_1_BPS = 10 in 0.1-bps units → 0.0001 of notional).
# Every engine order is routed with this builder code by NadoClient.place_order.
# The venue match ``fee`` ALREADY INCLUDES this builder portion (confirmed), so
# we split it out for attribution: builder_fee = notional × rate, and
# fill_fee = venue_fee − builder_fee. Readers sum ``fill_fee + builder_fee``,
# which then equals the true total the trader paid (no double-count).
_BUILDER_FEE_RATE = Decimal("0.0001")


def _parse_controller_id(controller_id: str) -> Optional[tuple[str, int, str]]:
    """``{strategy}:{user_id}:{network}`` → (strategy, user_id, network)."""
    parts = str(controller_id or "").split(":")
    if len(parts) != 3:
        return None
    strategy, user_raw, network = parts
    try:
        return strategy, int(user_raw), network
    except (TypeError, ValueError):
        return None


def resolve_running_session_id(strategy: str, user_id: int, network: str) -> Optional[int]:
    """The active run's unique id (``strategy_sessions.id``) for this
    strategy/user/network. This is the per-RUN tag used to scope fills and
    executors so one run's stats never bleed into the next."""
    from src.nadobro.db import query_one

    row = query_one(
        "SELECT id FROM strategy_sessions "
        "WHERE user_id = %s AND network = %s AND strategy = %s AND status = 'running' "
        "ORDER BY started_at DESC LIMIT 1",
        (int(user_id), str(network), str(strategy)),
    )
    return int(row["id"]) if row and row.get("id") is not None else None


def resolve_session_id_for_controller(controller_id: str) -> Optional[int]:
    parsed = _parse_controller_id(controller_id)
    if parsed is None:
        return None
    strategy, user_id, network = parsed
    return resolve_running_session_id(strategy, user_id, network)


def _resolve_engine_fill_product(strategy: str, trading_pair: str, network: str) -> tuple[int, str]:
    """Best-effort product id/name for an engine fill row.

    Perp-only lookup is wrong for strategies that trade spot through the engine
    (DN long leg, Volume spot, Desk spot). Store the real spot product id so the
    spot side remains visible in downstream product/volume views; session totals
    still do not depend on this resolving successfully.
    """
    from src.nadobro.config import get_product_name
    from src.nadobro.venue.product_catalog import (
        get_product_id,
        get_spot_product_id,
    )

    pair = str(trading_pair or "").strip()
    strategy_id = str(strategy or "").lower()
    pair_upper = pair.upper()
    product_id: Optional[int] = None

    if strategy_id in {"vol", "volume", "volume_bot"}:
        try:
            from src.nadobro.config import normalize_volume_spot_symbol

            product_id = get_spot_product_id(normalize_volume_spot_symbol(pair), network=network)
        except Exception:  # noqa: BLE001
            product_id = None
    elif strategy_id in {"dn", "delta_neutral", "delta-neutral"} and not pair_upper.endswith("-PERP"):
        base = pair.split("-", 1)[0]
        product_id = get_spot_product_id(base, network=network)
    elif pair_upper.endswith("-SPOT"):
        product_id = get_spot_product_id(pair.split("-", 1)[0], network=network)

    if product_id is None:
        try:
            product_id = get_product_id(pair, network=network)
        except Exception:  # noqa: BLE001
            product_id = None
    if product_id is None:
        return 0, pair
    try:
        product_name = get_product_name(int(product_id), network=network)
    except Exception:  # noqa: BLE001
        product_name = pair
    if not product_name or str(product_name).startswith("ID:"):
        product_name = pair
    return int(product_id), str(product_name)


class DbTradeRecorder:
    """Writes each engine fill into ``trades_<network>`` tagged with the
    active ``strategy_session_id``. Injected into executors by the runtime
    (mirrors ``DbInventoryRepository``); a no-op when no recorder is wired so
    unit tests and read-only modes are unaffected.

    Resolution is keyed entirely off ``controller_id`` (``{strategy}:{user}:
    {network}``) — executors carry it, so no extra session plumbing is needed.
    """

    def _resolve_session_id(self, strategy: str, user_id: int, network: str) -> Optional[int]:
        return resolve_running_session_id(strategy, user_id, network)

    def record(
        self,
        controller_id: str,
        trading_pair: str,
        side: TradeType,
        amount_base: object,
        price: object,
        fee_quote: object,
        order_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        *,
        realized_pnl: object = None,
        is_taker: bool = False,
        leverage: Optional[float] = None,
    ) -> None:
        """Best-effort: persist one engine fill. Never raises — fill recording
        must not break execution (same policy as the inventory/registry writes).
        """
        try:
            self._record(
                controller_id, trading_pair, side, amount_base, price, fee_quote,
                order_id, timestamp, realized_pnl=realized_pnl, is_taker=is_taker,
                leverage=leverage,
            )
        except Exception:  # noqa: BLE001 - persistence must never break a fill
            _recorder_logger.warning(
                "engine fill not recorded to trades for controller=%s — "
                "session volume/PnL counters will undercount",
                controller_id, exc_info=True,
            )

    def _record(
        self,
        controller_id: str,
        trading_pair: str,
        side: TradeType,
        amount_base: object,
        price: object,
        fee_quote: object,
        order_id: Optional[str],
        timestamp: Optional[float],
        *,
        realized_pnl: object,
        is_taker: bool,
        leverage: Optional[float] = None,
    ) -> None:
        from datetime import datetime, timezone

        from src.nadobro.models.database import insert_trade

        parsed = _parse_controller_id(controller_id)
        if parsed is None:
            return
        strategy, user_id, network = parsed

        session_id = self._resolve_session_id(strategy, user_id, network)
        source = "strategy"
        if session_id is None:
            # Resolved per fill (indexed lookup) rather than cached, so a new run
            # reusing the same controller_id never misattributes to a stale,
            # already-finalized session.
            if strategy != "desk":
                # Engine running outside a tracked session: nothing to
                # attribute the fill to; skip rather than orphan a row.
                return
            # Desk plans drive the engine WITHOUT a strategy session — they are
            # user-initiated trades and belong on the History surface. Record
            # them as MANUAL so the venue match enriches this row (digest +
            # product already known here) and round-trip pairing picks it up;
            # skipping them left desk fills to sync in as unattributable
            # product_id=0 rows invisible to History (prod, 2026-07-09).
            source = "manual"

        base = abs(_dec(amount_base))
        px = _dec(price)
        if base <= 0 or px <= 0:
            return
        notional = base * px
        venue_fee = abs(_dec(fee_quote))
        # The venue fee already includes the 1bp builder portion — split it out
        # so readers' ``fill_fee + builder_fee`` reconstructs the true total.
        builder_fee = (notional * _BUILDER_FEE_RATE).quantize(Decimal("0.00000001"))
        if builder_fee > venue_fee:
            # Defensive: never let the split go negative (e.g. a fee-free /
            # rebated maker fill). Attribute the whole fee to builder.
            builder_fee = venue_fee
        trading_fee = venue_fee - builder_fee

        ts = float(timestamp) if timestamp else None
        when = (
            datetime.fromtimestamp(ts, tz=timezone.utc) if ts
            else datetime.now(timezone.utc)
        ).isoformat()

        # Resolve product metadata best-effort; the volume/PnL math does not
        # depend on product_id, so an unresolved symbol must not drop the fill.
        product_id, product_name = _resolve_engine_fill_product(strategy, str(trading_pair), network)

        data = {
            "user_id": int(user_id),
            "product_id": int(product_id),
            "product_name": product_name,
            "order_type": "match",
            "side": "long" if side is TradeType.BUY else "short",
            "size": str(base),
            "price": str(px),
            "fill_size": str(base),
            "fill_price": str(px),
            "fill_fee": str(trading_fee),
            "builder_fee": str(builder_fee),
            "status": "filled",
            "source": source,
            # Every engine/desk fill is routed through Nadobro by construction, so
            # it counts toward "Nadobro Vol". Stamp it at record time (the venue
            # match's enrichment UPDATE also COALESCEs this to TRUE) — desk fills
            # record as source='manual', so `source` can't carry this.
            "via_nadobro": True,
            "is_taker": bool(is_taker),
            "created_at": when,
            "filled_at": when,
        }
        # Real leverage of the position this fill belongs to (perps). Recorded so
        # the History round-trip + Type A PnL card show the true "Nx" instead of
        # the trades table's misleading ``leverage DEFAULT 1.0`` — the desk-fill
        # bug that rendered a 49x trade as "LONG 1x". None (spot, or an executor
        # without config leverage) leaves the column unset.
        try:
            if leverage is not None and float(leverage) > 0:
                data["leverage"] = float(leverage)
        except (TypeError, ValueError):
            pass
        if session_id is not None:
            data["strategy_session_id"] = int(session_id)
        if order_id:
            data["order_digest"] = str(order_id)
            # Idempotency: if venue-sync already recorded this fill authoritatively
            # (it carries a submission_idx), do NOT insert a duplicate strategy row.
            # The executor recorder and nado_sync._write_matches are two writers for
            # the same fill; whichever runs SECOND must defer. When the recorder
            # runs first its submission_idx-less row is ENRICHED by _write_matches
            # (not duplicated); when venue-sync runs first (it can beat the
            # executor's fill detection by tens of seconds), this guard stops the
            # recorder from double-counting the fill.
            from src.nadobro.db import query_one as _qone
            _tbl = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
            try:
                if _qone(
                    f"SELECT 1 FROM {_tbl} WHERE order_digest = %s "
                    f"AND submission_idx IS NOT NULL LIMIT 1",
                    (str(order_id),),
                ):
                    return
            except Exception:  # noqa: BLE001 - dedupe probe is best-effort
                pass
        if realized_pnl is not None:
            data["realized_pnl"] = str(_dec(realized_pnl))
        insert_trade(data, network=network)

        if session_id is None:
            # Desk volume counts toward the user's volume counters + referral
            # stats (update_trade_stats feeds record_referred_volume). Platform
            # -placed trades never reach this path, so they stay excluded —
            # that split is deliberate product policy.
            try:
                from src.nadobro.users.user_service import update_trade_stats

                update_trade_stats(
                    int(user_id), float(notional),
                    increment_trade_count=True, network=network,
                )
            except Exception:  # noqa: BLE001 - counters must never break a fill
                _recorder_logger.warning(
                    "desk fill stats not updated user=%s — volume/referral "
                    "counters will undercount", user_id, exc_info=True,
                )

        # Link the order digest to this session in ``order_intents`` so the
        # venue match-sync (nado_sync._write_matches) back-links the
        # authoritative per-match ``realized_pnl_x18`` to the session and
        # enriches this row instead of writing an untagged duplicate.
        if order_id:
            if session_id is not None:
                self._link_intent(str(order_id), int(session_id), network)
            else:
                # Sessionless desk fill: tag the digest manual (+ product) so
                # the venue match stays attributable even if the row above is
                # ever lost — mirrors the manual close paths.
                try:
                    from src.nadobro.trading.order_intents import link_digest_intent

                    link_digest_intent(
                        str(order_id), network, source="manual",
                        product_id=int(data.get("product_id") or 0) or None,
                        product_name=str(data.get("product_name") or "") or None,
                    )
                except Exception:  # noqa: BLE001 - tag is best-effort; the row is primary
                    pass

    def link_placement(self, controller_id: str, order_id: Optional[str]) -> None:
        """Link a freshly-PLACED order digest to its live session BEFORE any fill.

        This is the deterministic fix for session-volume undercount. Engine fill
        detection (order_status polling) is unreliable, but nado_sync reliably
        syncs every venue fill. Writing the digest→session intent at placement
        (``source='strategy'``) means ``_write_matches._back_link_intent`` then
        attributes EVERY venue fill for this order to the session as
        ``source='strategy'`` — and the session rollup counts only
        ``source='strategy'`` — so volume no longer depends on the executor
        catching the fill. Best-effort; never raises (a fill is never lost to it).
        """
        try:
            if not order_id:
                return
            parsed = _parse_controller_id(controller_id)
            if parsed is None:
                return
            strategy, user_id, network = parsed
            session_id = self._resolve_session_id(strategy, user_id, network)
            if session_id is None:
                return
            self._link_intent(str(order_id), int(session_id), network)
        except Exception:  # noqa: BLE001 - placement linking is best-effort
            _recorder_logger.debug(
                "placement intent link failed controller=%s", controller_id, exc_info=True
            )

    @staticmethod
    def _link_intent(digest: str, session_id: int, network: str) -> None:
        import json

        from src.nadobro.db import execute

        value = json.dumps({"strategy_session_id": int(session_id), "source": "strategy"})
        intent_id = f"engine:{network}:{digest}"
        try:
            execute(
                """
                INSERT INTO order_intents (intent_id, status, value, order_digest, updated_at)
                VALUES (%s, 'filled', %s::jsonb, %s, now())
                ON CONFLICT (intent_id) DO UPDATE SET
                  order_digest = EXCLUDED.order_digest,
                  value = EXCLUDED.value,
                  updated_at = now()
                """,
                (intent_id, value, digest),
            )
        except Exception:  # noqa: BLE001 - best-effort back-link tag
            pass


# --------------------------------------------------------------------------
# Executors (engine_executors)
# --------------------------------------------------------------------------
def _strategy_type(executor: object) -> str:
    name = type(executor).__name__
    return _STRATEGY_TYPES.get(name, name.lower())


def _side(executor: object) -> str:
    cfg = getattr(executor, "config", None)
    side = getattr(cfg, "side", None)
    if side is None:
        side = getattr(getattr(cfg, "order_config", None), "side", None)
    return str(getattr(side, "value", "BUY"))


def _config_json(executor: object) -> str:
    cfg = getattr(executor, "config", None)
    try:
        if cfg is not None and dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
            return json.dumps(dataclasses.asdict(cfg), default=str)
    except Exception:  # noqa: BLE001
        pass
    return "{}"


_EXECUTOR_UPSERT_SQL = """
    INSERT INTO engine_executors
      (id, user_id, controller_id, strategy_type, trading_pair, side, config_json,
       state, close_type, net_pnl_quote, fees_paid_quote, volume_quote,
       duration_seconds, keep_position, created_at, terminated_at, strategy_session_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s,
            to_timestamp(%s), to_timestamp(%s), %s)
    ON CONFLICT (id) DO UPDATE SET
      state = EXCLUDED.state,
      close_type = EXCLUDED.close_type,
      net_pnl_quote = EXCLUDED.net_pnl_quote,
      fees_paid_quote = EXCLUDED.fees_paid_quote,
      volume_quote = EXCLUDED.volume_quote,
      duration_seconds = EXCLUDED.duration_seconds,
      terminated_at = EXCLUDED.terminated_at,
      strategy_session_id = COALESCE(engine_executors.strategy_session_id, EXCLUDED.strategy_session_id)
"""


def executor_persist_signature(executor: object) -> tuple:
    """The stored fields that can CHANGE after the first insert — the dirty
    key for per-tick persistence (2026-09-03). Everything else (config, side,
    pair, created_at) is immutable, so an executor whose signature is unchanged
    since its last successful write has nothing to persist. A TERMINATED
    executor's signature is frozen, so it is written exactly once after it
    terminates instead of on every tick for the rest of the session."""
    m = executor.metrics()  # type: ignore[attr-defined]
    close_type = getattr(executor, "close_type", None)
    # duration_seconds is deliberately NOT part of the signature: it advances
    # every second for a live executor, which would make every idle tick a
    # write. A row is written whenever anything else changes and the terminal
    # write carries the final duration.
    return (
        executor.state.value,  # type: ignore[attr-defined]
        getattr(close_type, "value", None),
        str(m["net_pnl_quote"]), str(m["fees_paid_quote"]), str(m["volume_quote"]),
        bool(executor.keep_position),  # type: ignore[attr-defined]
        executor.terminated_at,  # type: ignore[attr-defined]
    )


def executor_row(executor: object, session_id: Optional[int]) -> tuple:
    """The upsert parameter tuple for ``_EXECUTOR_UPSERT_SQL``. Pure — safe to
    build on the event loop thread; only the write goes to a DB worker."""
    m = executor.metrics()  # type: ignore[attr-defined]
    close_type = getattr(executor, "close_type", None)
    return (
        executor.id, executor.user_id, executor.controller_id,  # type: ignore[attr-defined]
        _strategy_type(executor), executor.trading_pair, _side(executor),  # type: ignore[attr-defined]
        _config_json(executor), executor.state.value,  # type: ignore[attr-defined]
        getattr(close_type, "value", None),
        m["net_pnl_quote"], m["fees_paid_quote"], m["volume_quote"],
        int(m["duration_seconds"]), executor.keep_position,  # type: ignore[attr-defined]
        executor.created_at, executor.terminated_at,  # type: ignore[attr-defined]
        session_id,
    )


class DbExecutorStore:
    def save(self, executor: object) -> None:
        """One executor, one statement (tests / ad-hoc). The engine tick uses
        ``save_rows`` — one batched round trip for every dirty executor."""
        from src.nadobro.db import execute
        # Tag the executor with the RUN's unique session id so per-run order
        # counts don't bleed across runs (controller_id is stable across runs).
        # Resolved once at first insert; ON CONFLICT never overwrites it.
        session_id = resolve_session_id_for_controller(executor.controller_id)  # type: ignore[attr-defined]
        execute(_EXECUTOR_UPSERT_SQL, executor_row(executor, session_id))

    def save_rows(self, rows: list, controller_ids: list) -> int:
        """Batched upsert of prebuilt ``executor_row`` tuples — ONE session-id
        lookup per controller (not per executor) and one round trip per page.
        Blocking: call it from the DB worker pool, never on the event loop."""
        from src.nadobro.db import execute_batch
        if not rows:
            return 0
        session_ids: dict = {}
        resolved = []
        for row, cid in zip(rows, controller_ids):
            if cid not in session_ids:
                session_ids[cid] = resolve_session_id_for_controller(cid)
            resolved.append((*row[:-1], session_ids[cid]))
        return execute_batch(_EXECUTOR_UPSERT_SQL, resolved)

    def get(self, executor_id: str) -> Optional[dict]:
        from src.nadobro.db import query_one

        return query_one("SELECT * FROM engine_executors WHERE id=%s", (executor_id,))


# --------------------------------------------------------------------------
# Kill switch (engine_kill_switch)
# --------------------------------------------------------------------------
class DbKillSwitchStore(KillSwitchStore):
    def __init__(self, scope: str = "global") -> None:
        self.scope = scope

    def is_engaged(self) -> bool:
        from src.nadobro.db import query_one

        row = query_one("SELECT engaged FROM engine_kill_switch WHERE scope=%s", (self.scope,))
        return bool(row and row["engaged"])

    def engage(self, reason: str) -> None:
        from src.nadobro.db import execute

        execute(
            "INSERT INTO engine_kill_switch (scope, engaged, reason, updated_at) "
            "VALUES (%s, TRUE, %s, NOW()) "
            "ON CONFLICT (scope) DO UPDATE SET engaged=TRUE, reason=EXCLUDED.reason, updated_at=NOW()",
            (self.scope, reason),
        )

    def disengage(self) -> None:
        from src.nadobro.db import execute

        execute(
            "INSERT INTO engine_kill_switch (scope, engaged, reason, updated_at) "
            "VALUES (%s, FALSE, NULL, NOW()) "
            "ON CONFLICT (scope) DO UPDATE SET engaged=FALSE, reason=NULL, updated_at=NOW()",
            (self.scope,),
        )

    def reason(self) -> Optional[str]:
        from src.nadobro.db import query_one

        row = query_one(
            "SELECT reason FROM engine_kill_switch WHERE scope=%s AND engaged=TRUE", (self.scope,)
        )
        return row["reason"] if row else None


# --------------------------------------------------------------------------
# Live controller progress (engine_controller_state)
# --------------------------------------------------------------------------
def upsert_controller_progress(
    controller_id: str,
    user_id: int,
    *,
    strategy: Optional[str] = None,
    network: Optional[str] = None,
    cycles_completed: int = 0,
    funding_earned_usd: object = Decimal(0),
    phase: Optional[str] = None,
) -> None:
    """Persist a controller's live progress so the main process (/status) can
    read it cross-process. Written each tick by the worker; best-effort —
    persistence must never break a cycle."""
    from src.nadobro.db import execute

    execute(
        """
        INSERT INTO engine_controller_state
          (controller_id, user_id, strategy, network, cycles_completed,
           funding_earned_usd, phase, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (controller_id) DO UPDATE SET
          cycles_completed   = EXCLUDED.cycles_completed,
          funding_earned_usd = EXCLUDED.funding_earned_usd,
          phase              = EXCLUDED.phase,
          updated_at         = NOW()
        """,
        (
            controller_id, int(user_id), strategy, network,
            int(cycles_completed), _dec(funding_earned_usd), phase,
        ),
    )


def get_controller_progress(controller_id: str) -> Optional[dict]:
    """Most recent persisted progress for ``controller_id``, or None."""
    from src.nadobro.db import query_one

    return query_one(
        "SELECT cycles_completed, funding_earned_usd, phase, updated_at "
        "FROM engine_controller_state WHERE controller_id=%s",
        (controller_id,),
    )


def clear_controller_progress(controller_id: str) -> None:
    """Remove a controller's progress row (called on stop). Best-effort."""
    from src.nadobro.db import execute

    execute("DELETE FROM engine_controller_state WHERE controller_id=%s", (controller_id,))


def terminate_engine_executors(controller_id: str) -> int:
    """Mark all non-terminated engine_executors rows for ``controller_id`` as
    TERMINATED. Used on a cross-process stop (the owning process is gone) so
    stale ACTIVE rows don't make ``_remote_active`` report the strategy as still
    running and block the next run. Returns rows affected (best-effort)."""
    from src.nadobro.db import execute
    try:
        execute(
            "UPDATE engine_executors SET state = 'TERMINATED', "
            "terminated_at = COALESCE(terminated_at, now()) "
            "WHERE controller_id = %s AND state <> 'TERMINATED'",
            (controller_id,),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


def count_engine_orders(controller_id: str, session_id: Optional[int] = None) -> dict:
    """Per-RUN order/position counts from engine_executors, so /status reflects
    THIS run's activity. ``controller_id`` is stable across runs, so a
    ``session_id`` (the run's unique tag) scopes the count to the current run —
    without it, every past run of the same strategy would be summed in.
    Each executor opens an entry order (placed) and, if it closed, a close order.
    """
    from src.nadobro.db import query_one

    where = "controller_id = %s"
    params: list = [controller_id]
    if session_id is not None:
        where += " AND strategy_session_id = %s"
        params.append(int(session_id))
    row = query_one(
        f"""
        SELECT
          COUNT(*)                                              AS executors,
          COUNT(*) FILTER (WHERE terminated_at IS NOT NULL)     AS closed,
          COUNT(*) FILTER (WHERE volume_quote > 0)              AS filled,
          COUNT(*) FILTER (WHERE close_type = 'FAILED')         AS failed
        FROM engine_executors WHERE {where}
        """,
        tuple(params),
    )
    if not row:
        return {"orders_placed": 0, "orders_filled": 0, "orders_cancelled": 0}
    executors = int(row.get("executors") or 0)
    closed = int(row.get("closed") or 0)
    # placed = one entry order per executor + one close order per closed executor.
    return {
        "orders_placed": executors + closed,
        "orders_filled": int(row.get("filled") or 0),
        "orders_cancelled": int(row.get("failed") or 0),
    }
