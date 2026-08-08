import enum
import json
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, TypeAlias

from psycopg2 import sql as pgsql
from src.nadobro.db import query_one, query_all, execute, execute_returning, query_count, run_transaction

logger = logging.getLogger(__name__)

# JSON-compatible values for bot_state (serialized with json.dumps unless value is str).
JsonSerializable: TypeAlias = (
    str | int | float | bool | None | list["JsonSerializable"] | dict[str, "JsonSerializable"]
)


class NetworkMode(enum.Enum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


class OrderSide(enum.Enum):
    LONG = "long"
    SHORT = "short"


class OrderTypeEnum(enum.Enum):
    MARKET = "market"
    LIMIT = "limit"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"


class TradeStatus(enum.Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"


class AlertCondition(enum.Enum):
    ABOVE = "above"
    BELOW = "below"
    FUNDING_ABOVE = "funding_above"
    FUNDING_BELOW = "funding_below"
    PNL_ABOVE = "pnl_above"
    PNL_BELOW = "pnl_below"


_VALID_NETWORKS = frozenset({"testnet", "mainnet"})


def _trades_table(network: str) -> str:
    if network not in _VALID_NETWORKS:
        raise ValueError(f"Invalid network: {network}")
    return f"trades_{network}"


def _alerts_table(network: str) -> str:
    if network not in _VALID_NETWORKS:
        raise ValueError(f"Invalid network: {network}")
    return f"alerts_{network}"


def init_db():
    from src.nadobro.db import init_db as _init
    _init()


def get_bot_state(key: str) -> Optional[dict]:
    row = query_one("SELECT value FROM bot_state WHERE key = %s", (key,))
    if not row:
        return None
    raw = row.get("value")
    if not raw:
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def set_bot_state(key: str, value: JsonSerializable):
    payload = json.dumps(value) if not isinstance(value, str) else value
    now = datetime.now(timezone.utc).isoformat()
    execute(
        """INSERT INTO bot_state (key, value, updated_at) VALUES (%s, %s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
        (key, payload, now),
    )


def get_bot_state_raw(key: str) -> Optional[str]:
    row = query_one("SELECT value FROM bot_state WHERE key = %s", (key,))
    if not row:
        return None
    return row.get("value")


_TRADE_INSERT_ALLOWED_COLS = frozenset({
    "user_id", "product_id", "product_name", "order_type", "side",
    "size", "price", "leverage", "status", "order_digest", "pnl",
    "fees", "error_message", "created_at", "filled_at",
    "close_price", "closed_at",
    "fill_price", "fill_size", "fill_fee", "builder_fee", "slippage_bps",
    "source", "strategy_session_id", "open_trade_id", "realized_pnl", "is_taker",
    "funding_paid", "via_nadobro",
})


def insert_trade(data: dict, network: str = "mainnet") -> Optional[int]:
    filtered = {k: v for k, v in data.items() if k != "network"}
    disallowed = set(filtered.keys()) - _TRADE_INSERT_ALLOWED_COLS
    if disallowed:
        raise ValueError(f"insert_trade: disallowed column(s): {disallowed}")
    table = _trades_table(network)
    cols = list(filtered.keys())
    vals = [filtered[c] for c in cols]
    query = pgsql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
        pgsql.Identifier(table),
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in cols),
        pgsql.SQL(", ").join(pgsql.Placeholder() * len(cols)),
    )
    row = execute_returning(query, vals)
    return row["id"] if row else None


_TRADE_UPDATE_ALLOWED_COLS = frozenset({
    "status", "order_digest", "price", "filled_at", "error_message",
    "pnl", "fees", "close_price", "closed_at",
    "fill_price", "fill_size", "fill_fee", "builder_fee", "slippage_bps",
    "source", "strategy_session_id", "open_trade_id", "realized_pnl", "is_taker",
    "funding_paid", "via_nadobro",
})


def update_trade(trade_id: int, data: dict, network: str = "mainnet"):
    disallowed = set(data.keys()) - _TRADE_UPDATE_ALLOWED_COLS
    if disallowed:
        raise ValueError(f"update_trade: disallowed column(s): {disallowed}")
    table = _trades_table(network)
    set_clause = pgsql.SQL(", ").join(
        pgsql.SQL("{} = %s").format(pgsql.Identifier(k)) for k in data.keys()
    )
    query = pgsql.SQL("UPDATE {} SET {} WHERE id = %s").format(
        pgsql.Identifier(table), set_clause
    )
    vals = list(data.values()) + [trade_id]
    execute(query, vals)


def get_last_trade_for_rate_limit(telegram_id: int, network: str = "mainnet") -> Optional[dict]:
    table = _trades_table(network)
    return query_one(
        f"SELECT created_at FROM {table} WHERE user_id = %s AND status = 'filled' ORDER BY created_at DESC LIMIT 1",
        (telegram_id,),
    )


def find_open_trade(
    telegram_id: int,
    product_id: int,
    network: str = "mainnet",
    side: str | None = None,
    ) -> Optional[dict]:
    """Most recent open (not-yet-closed) row a MANUAL close should pair with.

    Session-tagged fills are excluded: this matcher used to take the most
    recent row regardless of source, so a manual close could grab a live
    strategy session's fill, mark it closed, and stamp the venue's close
    price/PnL onto it — corrupting that session's realized PnL and win/loss
    counts (observed on prod 2026-07-09: four manual closes each stamped a
    different session's fill). ``side`` narrows to the position side being
    closed (a close ORDER's side is the opposite of the position's).
    """
    table = _trades_table(network)
    conds = [
        "user_id = %s",
        "product_id = %s",
        "status IN ('filled', 'partially_filled')",
        "COALESCE(open_trade_id, 0) = 0",
        "order_type NOT ILIKE '%%close%%'",
        "strategy_session_id IS NULL",
    ]
    params: list = [telegram_id, product_id]
    if side:
        conds.append("side = %s")
        params.append(str(side))
    where_sql = "\n          AND ".join(conds)
    return query_one(
        f"""
        SELECT *
        FROM {table}
        WHERE {where_sql}
        ORDER BY COALESCE(filled_at, created_at) DESC
        LIMIT 1
        """,
        tuple(params),
    )


def get_trades_by_user(
    telegram_id: int,
    limit: int | None = 50,
    network: str = "mainnet",
    strategy_session_id: int | None = None,
    since_created_at=None,
    since_filled_at=None,
) -> list:
    table = _trades_table(network)
    where = ["user_id = %s"]
    params: list = [telegram_id]
    if strategy_session_id is not None:
        where.append("strategy_session_id = %s")
        params.append(int(strategy_session_id))
    if since_created_at is not None:
        where.append("created_at >= %s")
        params.append(since_created_at)
    # Window on the actual FILL time (not sync/record time) so surfaces that pair
    # this with the fill-time-bucketed realized PnL replay describe the SAME 24h
    # set (Night HOWL). ``created_at`` for venue-only fills is the sync time.
    if since_filled_at is not None:
        where.append("COALESCE(filled_at, created_at) >= %s")
        params.append(since_filled_at)
    where_sql = " AND ".join(where)
    if limit is None or int(limit) <= 0:
        return query_all(
            f"SELECT * FROM {table} WHERE {where_sql} ORDER BY created_at DESC",
            tuple(params),
        )
    params.append(int(limit))
    return query_all(
        f"SELECT * FROM {table} WHERE {where_sql} ORDER BY created_at DESC LIMIT %s",
        tuple(params),
    )


def get_trade_by_id(trade_id: int, network: str = "mainnet") -> Optional[dict]:
    table = _trades_table(network)
    return query_one(f"SELECT * FROM {table} WHERE id = %s LIMIT 1", (trade_id,))


_ALERT_INSERT_ALLOWED_COLS = frozenset({
    "user_id", "product_id", "product_name", "condition",
    "target_value", "is_active", "created_at",
})


def insert_alert(data: dict, network: str = "mainnet") -> Optional[int]:
    filtered = {k: v for k, v in data.items() if k != "network"}
    disallowed = set(filtered.keys()) - _ALERT_INSERT_ALLOWED_COLS
    if disallowed:
        raise ValueError(f"insert_alert: disallowed column(s): {disallowed}")
    table = _alerts_table(network)
    cols = list(filtered.keys())
    vals = [filtered[c] for c in cols]
    query = pgsql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
        pgsql.Identifier(table),
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in cols),
        pgsql.SQL(", ").join(pgsql.Placeholder() * len(cols)),
    )
    row = execute_returning(query, vals)
    return row["id"] if row else None


def get_alerts_by_user(telegram_id: int, active_only: bool = True, network: str = "mainnet") -> list:
    table = _alerts_table(network)
    if active_only:
        return query_all(
            f"SELECT * FROM {table} WHERE user_id = %s AND is_active = true ORDER BY created_at DESC",
            (telegram_id,),
        )
    return query_all(
        f"SELECT * FROM {table} WHERE user_id = %s ORDER BY created_at DESC",
        (telegram_id,),
    )


def update_alert(alert_id: int, is_active: bool, network: str = "mainnet"):
    table = _alerts_table(network)
    execute(
        f"UPDATE {table} SET is_active = %s WHERE id = %s",
        (is_active, alert_id),
    )


def get_alert_by_id_and_user(alert_id: int, telegram_id: int, network: str = "mainnet") -> Optional[dict]:
    table = _alerts_table(network)
    return query_one(
        f"SELECT * FROM {table} WHERE id = %s AND user_id = %s",
        (alert_id, telegram_id),
    )


def get_all_active_alerts(network: str = None) -> list:
    if network:
        table = _alerts_table(network)
        return query_all(f"SELECT * FROM {table} WHERE is_active = true")
    testnet_alerts = query_all("SELECT *, 'testnet' AS network FROM alerts_testnet WHERE is_active = true")
    mainnet_alerts = query_all("SELECT *, 'mainnet' AS network FROM alerts_mainnet WHERE is_active = true")
    return testnet_alerts + mainnet_alerts


def update_alert_triggered(alert_id: int, network: str = "mainnet"):
    table = _alerts_table(network)
    execute(
        f"UPDATE {table} SET is_active = false, triggered_at = %s WHERE id = %s",
        (datetime.now(timezone.utc).isoformat(), alert_id),
    )


def insert_admin_log(data: dict):
    execute(
        "INSERT INTO admin_logs (admin_id, action, details) VALUES (%s, %s, %s)",
        (data.get("admin_id"), data.get("action"), data.get("details")),
    )


def get_trades_count(network: str = None) -> int:
    if network:
        return query_count(f"SELECT COUNT(*) FROM {_trades_table(network)}")
    t = query_count("SELECT COUNT(*) FROM trades_testnet")
    m = query_count("SELECT COUNT(*) FROM trades_mainnet")
    return t + m


def get_trades_count_filled(network: str = None) -> int:
    if network:
        return query_count(f"SELECT COUNT(*) FROM {_trades_table(network)} WHERE status = 'filled'")
    t = query_count("SELECT COUNT(*) FROM trades_testnet WHERE status = 'filled'")
    m = query_count("SELECT COUNT(*) FROM trades_mainnet WHERE status = 'filled'")
    return t + m


def get_trades_count_failed(network: str = None) -> int:
    if network:
        return query_count(f"SELECT COUNT(*) FROM {_trades_table(network)} WHERE status = 'failed'")
    t = query_count("SELECT COUNT(*) FROM trades_testnet WHERE status = 'failed'")
    m = query_count("SELECT COUNT(*) FROM trades_mainnet WHERE status = 'failed'")
    return t + m


def get_total_volume_filled(network: str = None) -> float:
    if network:
        table = _trades_table(network)
        row = query_one(f"SELECT COALESCE(SUM(size * price), 0) AS total FROM {table} WHERE status = 'filled'")
        return float(row["total"]) if row else 0.0
    t = query_one("SELECT COALESCE(SUM(size * price), 0) AS total FROM trades_testnet WHERE status = 'filled'")
    m = query_one("SELECT COALESCE(SUM(size * price), 0) AS total FROM trades_mainnet WHERE status = 'filled'")
    return float(t["total"] if t else 0) + float(m["total"] if m else 0)


def get_recent_trades(limit: int = 20, network: str = None) -> list:
    if network:
        table = _trades_table(network)
        return query_all(
            f"SELECT *, '{network}' AS network FROM {table} ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
    t = query_all(
        "SELECT *, 'testnet' AS network FROM trades_testnet ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    m = query_all(
        "SELECT *, 'mainnet' AS network FROM trades_mainnet ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    combined = t + m
    combined.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return combined[:limit]


def get_recent_admin_logs(limit: int = 20) -> list:
    return query_all(
        "SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )


def upsert_copy_trader(
    wallet_address: str,
    label: str = "",
    is_curated: bool = False,
    owner_user_id: Optional[int] = None,
) -> Optional[int]:
    """Insert/update a copy trader entry.

    Privacy contract:
      * Curated entries (is_curated=True) MUST have owner_user_id=None and
        are visible to everyone.
      * Personal entries MUST have owner_user_id set; they are visible only
        to that telegram user. Two different users may add the same wallet
        and each gets their own private row.
    """
    wallet = wallet_address.strip()
    if is_curated:
        owner_user_id = None
        row = execute_returning(
            """INSERT INTO copy_traders
                   (wallet_address, label, is_curated, active, owner_user_id)
               VALUES (%s, %s, true, true, NULL)
               ON CONFLICT (wallet_address) WHERE owner_user_id IS NULL
               DO UPDATE
                   SET label = EXCLUDED.label,
                       is_curated = true,
                       active = true
               RETURNING id""",
            (wallet, label),
        )
    else:
        if owner_user_id is None:
            return None
        row = execute_returning(
            """INSERT INTO copy_traders
                   (wallet_address, label, is_curated, active, owner_user_id)
               VALUES (%s, %s, false, true, %s)
               ON CONFLICT (owner_user_id, wallet_address) WHERE owner_user_id IS NOT NULL
               DO UPDATE
                   SET label = EXCLUDED.label,
                       is_curated = false,
                       active = true
               RETURNING id""",
            (wallet, label, int(owner_user_id)),
        )
    return row["id"] if row else None


def get_copy_trader(trader_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM copy_traders WHERE id = %s", (trader_id,))


def get_copy_trader_by_wallet(
    wallet: str,
    owner_user_id: Optional[int] = None,
) -> Optional[dict]:
    if owner_user_id is None:
        return query_one(
            "SELECT * FROM copy_traders WHERE wallet_address = %s AND owner_user_id IS NULL",
            (wallet.strip(),),
        )
    return query_one(
        """SELECT * FROM copy_traders
           WHERE wallet_address = %s
             AND (owner_user_id IS NULL OR owner_user_id = %s)""",
        (wallet.strip(), int(owner_user_id)),
    )


def get_active_copy_traders(user_id: Optional[int] = None) -> list:
    """Return active copy traders visible to `user_id`.

    A NULL `user_id` returns only curated (public) entries (used by
    background pollers that don't have a specific viewer context but still
    must respect privacy). A bot user always sees curated + their own
    personal entries — never any other user's custom wallets.
    """
    if user_id is None:
        return query_all(
            """SELECT * FROM copy_traders
               WHERE active = true AND owner_user_id IS NULL
               ORDER BY id"""
        )
    return query_all(
        """SELECT * FROM copy_traders
           WHERE active = true
             AND (owner_user_id IS NULL OR owner_user_id = %s)
           ORDER BY (owner_user_id IS NULL) DESC, id""",
        (int(user_id),),
    )


def get_curated_copy_traders() -> list:
    return query_all(
        """SELECT * FROM copy_traders
           WHERE active = true AND is_curated = true AND owner_user_id IS NULL
           ORDER BY id"""
    )


def update_copy_trader_stats(
    trader_id: int,
    *,
    total_pnl_usd: float = 0.0,
    total_volume_usd: float = 0.0,
    nado_points: float = 0.0,
    win_rate: Optional[float] = None,
    leader_roi: Optional[float] = None,
    leader_active_days: Optional[int] = None,
    leader_period_days: Optional[int] = None,
    leader_last_activity_at: Optional[object] = None,
    leader_closed_trades: Optional[int] = None,
    leader_max_drawdown_pct: Optional[float] = None,
) -> None:
    """Stamp leaderboard performance stats onto a copy_traders row.

    Fills the stat columns (added with idx_copy_traders_leaderboard) that had
    no writer until the NadoExplorer discovery flow. Display-only data —
    sizing/mirroring never read these.
    """
    execute(
        """UPDATE copy_traders
           SET total_pnl_usd = %s, total_volume_usd = %s, nado_points = %s,
               win_rate = %s, leader_roi = %s, leader_active_days = %s,
               leader_period_days = %s, leader_last_activity_at = %s,
               leader_closed_trades = %s, leader_max_drawdown_pct = %s,
               last_updated_at = NOW()
           WHERE id = %s""",
        (
            float(total_pnl_usd),
            float(total_volume_usd),
            float(nado_points),
            None if win_rate is None else float(win_rate),
            None if leader_roi is None else float(leader_roi),
            None if leader_active_days is None else int(leader_active_days),
            None if leader_period_days is None else int(leader_period_days),
            leader_last_activity_at,
            None if leader_closed_trades is None else int(leader_closed_trades),
            None if leader_max_drawdown_pct is None else float(leader_max_drawdown_pct),
            int(trader_id),
        ),
    )


def deactivate_copy_trader(trader_id: int):
    execute("UPDATE copy_traders SET active = false WHERE id = %s", (trader_id,))


def clear_saved_copy_trader_selections(user_id: int) -> tuple[int, int]:
    """Hide only safe-to-clear personal trader selections for one user.

    A selection is intentionally distinct from an active copy mirror. The
    statement is ownership-scoped and protects every network plus any tracked
    open position, so a stale/crafted callback cannot deactivate a row whose
    exposure is still being monitored. Rows are soft-deactivated rather than
    deleted because mirrors, snapshots, and trade history reference them.
    """
    def _clear(cur):
        # Lock selection rows before checking mirrors. ``create_copy_mirror_v2``
        # takes the same lock, so Clear cannot deactivate a trader between a
        # start's validation and its mirror insert.
        cur.execute(
            """SELECT t.id FROM copy_traders t
               WHERE t.owner_user_id = %s
                 AND t.is_curated = false
                 AND t.active = true
               FOR UPDATE""",
            (int(user_id),),
        )
        owned = cur.fetchall() or []
        cleared = 0
        protected = 0
        for selection in owned:
            trader_id = int(selection["id"])
            cur.execute(
                """SELECT (
                       EXISTS (
                         SELECT 1 FROM copy_mirrors m
                         WHERE m.trader_id = %s AND m.active = true
                       )
                       OR EXISTS (
                         SELECT 1
                         FROM copy_positions p
                         JOIN copy_mirrors m ON m.id = p.mirror_id
                         WHERE m.trader_id = %s AND p.status = 'open'
                       )
                     ) AS protected""",
                (trader_id, trader_id),
            )
            state = cur.fetchone() or {}
            if state.get("protected"):
                protected += 1
                continue
            cur.execute(
                "UPDATE copy_traders SET active = false WHERE id = %s AND active = true",
                (trader_id,),
            )
            cleared += max(0, int(cur.rowcount or 0))
        return cleared, protected

    return run_transaction(_clear)


def create_copy_mirror(user_id: int, trader_id: int, budget_usd: float = 100.0,
                        risk_factor: float = 1.0, max_leverage: float = 10.0) -> Optional[int]:
    row = execute_returning(
        """INSERT INTO copy_mirrors (user_id, trader_id, budget_usd, risk_factor, max_leverage, active)
           VALUES (%s, %s, %s, %s, %s, true)
           ON CONFLICT (user_id, trader_id) DO UPDATE
               SET budget_usd = EXCLUDED.budget_usd,
                   risk_factor = EXCLUDED.risk_factor,
                   max_leverage = EXCLUDED.max_leverage,
                   active = true,
                   stopped_at = NULL
           RETURNING id""",
        (user_id, trader_id, budget_usd, risk_factor, max_leverage),
    )
    return row["id"] if row else None


def get_copy_mirror(mirror_id: int) -> Optional[dict]:
    return query_one("SELECT * FROM copy_mirrors WHERE id = %s", (mirror_id,))


def get_user_active_mirrors(user_id: int) -> list:
    return query_all(
        """SELECT m.*, t.wallet_address, t.label
           FROM copy_mirrors m JOIN copy_traders t ON m.trader_id = t.id
           WHERE m.user_id = %s AND m.active = true
           ORDER BY m.created_at""",
        (user_id,),
    )


def get_mirrors_for_trader(trader_id: int) -> list:
    return query_all(
        """SELECT m.* FROM copy_mirrors m
           WHERE m.trader_id = %s
             AND (
               m.active = true
               OR EXISTS (
                 SELECT 1 FROM copy_positions p
                 WHERE p.mirror_id = m.id AND p.status = 'open'
               )
             )""",
        (trader_id,),
    )


def get_all_active_mirrors() -> list:
    return query_all(
        """SELECT m.*, t.wallet_address, t.label
           FROM copy_mirrors m JOIN copy_traders t ON m.trader_id = t.id
           WHERE m.active = true AND t.active = true"""
    )


def stop_copy_mirror(mirror_id: int):
    execute(
        "UPDATE copy_mirrors SET active = false, stop_requested = false, stopped_at = %s WHERE id = %s",
        (datetime.now(timezone.utc).isoformat(), mirror_id),
    )


def request_copy_mirror_stop(mirror_id: int):
    """Keep a mirror monitored while its requested flatten is retried.

    A paused mirror is normally excluded from the poller. Clearing that flag,
    and reactivating a legacy inactive mirror with tracked exposure, is safe
    because ``stop_requested`` makes the sync path flatten-only; it cannot
    resume copying new leader positions.
    """
    execute(
        """UPDATE copy_mirrors
           SET active = true, stop_requested = true, paused = false, stopped_at = NULL
           WHERE id = %s""",
        (int(mirror_id),),
    )


def pause_copy_mirror(mirror_id: int):
    execute(
        "UPDATE copy_mirrors SET paused = true WHERE id = %s AND active = true AND NOT stop_requested",
        (mirror_id,),
    )


def resume_copy_mirror(mirror_id: int):
    execute(
        "UPDATE copy_mirrors SET paused = false WHERE id = %s AND active = true",
        (mirror_id,),
    )


def update_mirror_last_synced(mirror_id: int, fill_tid: int):
    execute(
        "UPDATE copy_mirrors SET last_synced_fill_tid = %s WHERE id = %s",
        (fill_tid, mirror_id),
    )


def count_user_active_mirrors(user_id: int) -> int:
    return query_count(
        "SELECT COUNT(*) FROM copy_mirrors WHERE user_id = %s AND active = true",
        (user_id,),
    )


def insert_copy_trade(data: dict) -> Optional[int]:
    cols = [
        "user_id", "mirror_id", "hl_fill_tid", "hl_coin",
        "nado_product_id", "side", "hl_size", "hl_price",
        "nado_size", "nado_price", "nado_trade_id", "status",
        "error_message", "created_at", "filled_at",
    ]
    filtered = {k: v for k, v in data.items() if k in cols and v is not None}
    if "created_at" not in filtered:
        filtered["created_at"] = datetime.now(timezone.utc).isoformat()
    col_names = list(filtered.keys())
    vals = [filtered[c] for c in col_names]
    placeholders = ", ".join(["%s"] * len(col_names))
    col_str = ", ".join(col_names)
    row = execute_returning(
        f"INSERT INTO copy_trades ({col_str}) VALUES ({placeholders}) RETURNING id",
        vals,
    )
    return row["id"] if row else None


def get_copy_trades_by_user(user_id: int, limit: int = 50) -> list:
    return query_all(
        "SELECT * FROM copy_trades WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
        (user_id, limit),
    )


def get_copy_trades_by_mirror(mirror_id: int, limit: int = 50) -> list:
    return query_all(
        "SELECT * FROM copy_trades WHERE mirror_id = %s ORDER BY created_at DESC LIMIT %s",
        (mirror_id, limit),
    )


def copy_trade_exists(hl_fill_tid: int, user_id: int, mirror_id: int) -> bool:
    return query_count(
        "SELECT COUNT(*) FROM copy_trades WHERE hl_fill_tid = %s AND user_id = %s AND mirror_id = %s",
        (hl_fill_tid, user_id, mirror_id),
    ) > 0


def get_active_trader_wallets() -> list[str]:
    rows = query_all(
        """SELECT DISTINCT t.wallet_address
           FROM copy_traders t JOIN copy_mirrors m ON t.id = m.trader_id
           WHERE t.active = true AND m.active = true"""
    )
    return [r["wallet_address"] for r in rows]


class UserRow:
    def __init__(self, data: dict):
        self._data = data or {}
        tid = self._data.get("telegram_id")
        self.telegram_id = int(tid) if tid is not None else None
        self.telegram_username = self._data.get("telegram_username")
        self.main_address = self._data.get("main_address")
        self.linked_signer_address = self._data.get("linked_signer_address")
        self.encrypted_linked_signer_pk = self._data.get("encrypted_linked_signer_pk")
        self.salt = self._data.get("salt")
        self.language = self._data.get("language") or "en"
        self.strategy_settings = self._data.get("strategy_settings") or {}
        nm = self._data.get("network_mode") or "mainnet"
        self.network_mode = NetworkMode.MAINNET if nm == "mainnet" else NetworkMode.TESTNET
        self.created_at = self._data.get("created_at")
        self.last_active = self._data.get("last_active")
        self.last_trade_at = self._data.get("last_trade_at")
        self.total_trades = int(self._data.get("total_trades") or 0)
        self.total_volume_usd = float(self._data.get("total_volume_usd") or 0)
        self.mainnet_volume_usd = float(self._data.get("mainnet_volume_usd") or 0)
        self.testnet_volume_usd = float(self._data.get("testnet_volume_usd") or 0)
        self.private_access_granted = bool(self._data.get("private_access_granted"))
        self.private_access_code_id = self._data.get("private_access_code_id")
        self.private_access_granted_at = self._data.get("private_access_granted_at")
        self.private_access_granted_by = self._data.get("private_access_granted_by")


# --- New copy trading ORM functions (Nado-native v2) ---

def create_copy_mirror_v2(user_id: int, trader_id: int, network: str,
                          margin_per_trade: float = 50.0, max_leverage: float = 10.0,
                          cumulative_stop_loss_pct: float = 50.0,
                          cumulative_take_profit_pct: float = 100.0,
                          total_allocated_usd: float = 500.0) -> Optional[int]:
    # UNIQUE(user_id, trader_id, network) routes every restart through this
    # upsert, so the DO UPDATE must reset the WHOLE accounting spine — a
    # restart that kept the previous run's cumulative_fees_usd started the
    # new run at net = -fees and could trip the stop-loss rail with zero
    # trades. strategy_session_id is cleared too: start_copy assigns a fresh
    # session right after, and a failure there must degrade to "no session",
    # never to tagging new fills onto the old stopped session.
    def _create(cur):
        # Serialize against Clear on this selection. The active predicate is
        # deliberately read under the lock rather than trusting start_copy's
        # earlier display/permission read.
        cur.execute(
            "SELECT id FROM copy_traders WHERE id = %s AND active = true FOR UPDATE",
            (int(trader_id),),
        )
        if not cur.fetchone():
            return None

        # A failed stop — including the pre-upgrade inactive orphan shape —
        # owns this mirror until it has flattened. A restart must never reset
        # accounting and resume new opens over a tracked copy position.
        cur.execute(
            """SELECT id FROM copy_mirrors
               WHERE user_id = %s AND trader_id = %s AND network = %s
                 AND (
                   stop_requested = true
                   OR EXISTS (
                     SELECT 1 FROM copy_positions p
                     WHERE p.mirror_id = copy_mirrors.id AND p.status = 'open'
                   )
                 )
               FOR UPDATE""",
            (int(user_id), int(trader_id), network),
        )
        pending = cur.fetchone()
        if pending:
            # Rehydrate a legacy inactive orphan into the same flatten-only
            # retry path as a current failed stop. This query is under the
            # trader lock, so Clear cannot hide the selection meanwhile.
            cur.execute(
                """UPDATE copy_mirrors
                   SET active = true, stop_requested = true, paused = false, stopped_at = NULL
                   WHERE id = %s""",
                (int(pending["id"]),),
            )
            return None

        cur.execute(
            """INSERT INTO copy_mirrors
               (user_id, trader_id, network, margin_per_trade, max_leverage,
                cumulative_stop_loss_pct, cumulative_take_profit_pct, total_allocated_usd, active)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)
               ON CONFLICT (user_id, trader_id, network) DO UPDATE
                   SET margin_per_trade = EXCLUDED.margin_per_trade,
                       max_leverage = EXCLUDED.max_leverage,
                       cumulative_stop_loss_pct = EXCLUDED.cumulative_stop_loss_pct,
                       cumulative_take_profit_pct = EXCLUDED.cumulative_take_profit_pct,
                       total_allocated_usd = EXCLUDED.total_allocated_usd,
                       active = true,
                       paused = false,
                       auto_stopped_reason = NULL,
                       cumulative_pnl = 0.0,
                       cumulative_fees_usd = 0.0,
                       cumulative_volume_usd = 0.0,
                       last_unrealized_pnl_usd = 0.0,
                       last_rail_checked_at = NULL,
                       strategy_session_id = NULL,
                       stop_requested = false,
                       created_at = now(),
                       stopped_at = NULL
               RETURNING id""",
            (user_id, trader_id, network, margin_per_trade, max_leverage,
             cumulative_stop_loss_pct, cumulative_take_profit_pct, total_allocated_usd),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    return run_transaction(_create)


def get_user_active_mirrors_v2(user_id: int, network: str = None) -> list:
    if network:
        return query_all(
            """SELECT m.*, t.wallet_address, t.label
               FROM copy_mirrors m JOIN copy_traders t ON m.trader_id = t.id
               WHERE m.user_id = %s AND m.active = true AND m.network = %s
               ORDER BY m.created_at""",
            (user_id, network),
        )
    return query_all(
        """SELECT m.*, t.wallet_address, t.label
           FROM copy_mirrors m JOIN copy_traders t ON m.trader_id = t.id
           WHERE m.user_id = %s AND m.active = true
           ORDER BY m.created_at""",
        (user_id,),
    )


def get_all_active_mirrors_v2(network: str = None) -> list:
    # A stop_requested mirror is flatten-only and MUST stay pollable even when
    # its trader was deactivated (remove_trader): a failed stop-race unwind
    # re-arms stop_requested on a mirror whose trader is now inactive, and the
    # old `t.active = true` filter hid it forever with the position still open.
    # Copying mirrors still require an active trader (no new opens off a
    # removed trader).
    if network:
        return query_all(
            """SELECT m.*, t.wallet_address, t.label
               FROM copy_mirrors m JOIN copy_traders t ON m.trader_id = t.id
               WHERE m.active = true AND (NOT m.paused OR m.stop_requested = true)
                 AND (t.active = true OR m.stop_requested = true) AND m.network = %s""",
            (network,),
        )
    return query_all(
        """SELECT m.*, t.wallet_address, t.label
           FROM copy_mirrors m JOIN copy_traders t ON m.trader_id = t.id
           WHERE m.active = true AND (NOT m.paused OR m.stop_requested = true)
             AND (t.active = true OR m.stop_requested = true)"""
    )


def update_mirror_cumulative_pnl(mirror_id: int, pnl_delta: float):
    execute(
        "UPDATE copy_mirrors SET cumulative_pnl = cumulative_pnl + %s WHERE id = %s",
        (pnl_delta, mirror_id),
    )


def auto_stop_mirror(mirror_id: int, reason: str):
    execute(
        "UPDATE copy_mirrors SET active = false, paused = false, stop_requested = false, auto_stopped_reason = %s, stopped_at = %s WHERE id = %s",
        (reason, datetime.now(timezone.utc).isoformat(), mirror_id),
    )


def insert_copy_position(data: dict) -> Optional[int]:
    cols = [
        "mirror_id", "user_id", "product_id", "product_name", "side",
        "entry_price", "size", "leverage", "tp_price", "sl_price",
        "tp_order_digest", "sl_order_digest", "leader_entry_price",
        "leader_size", "status", "opened_at",
    ]
    filtered = {k: v for k, v in data.items() if k in cols and v is not None}
    if "opened_at" not in filtered:
        filtered["opened_at"] = datetime.now(timezone.utc).isoformat()
    if "status" not in filtered:
        filtered["status"] = "open"
    col_names = list(filtered.keys())
    vals = [filtered[c] for c in col_names]
    placeholders = ", ".join(["%s"] * len(col_names))
    col_str = ", ".join(col_names)
    row = execute_returning(
        f"INSERT INTO copy_positions ({col_str}) VALUES ({placeholders}) RETURNING id",
        vals,
    )
    return row["id"] if row else None


def get_open_copy_positions(mirror_id: int) -> list:
    return query_all(
        "SELECT * FROM copy_positions WHERE mirror_id = %s AND status = 'open' ORDER BY opened_at",
        (mirror_id,),
    )


def get_closed_copy_position(position_id: int) -> Optional[dict]:
    """A single CLOSED copy position by id (Type A PnL card source)."""
    return query_one(
        "SELECT * FROM copy_positions WHERE id = %s AND status = 'closed'",
        (int(position_id),),
    )


def get_closed_copy_positions(user_id: int, network: str, limit: int = 100) -> list:
    """Closed copy positions for a user on a network, newest close first.

    Display-only source for the History tab (Type A copy cards). Joins
    copy_mirrors for the network scope; these rows are NEVER fed into the
    manual round-trip PnL/volume aggregation — copy stays tracked solely in
    copy accounting, so History shows them without double-counting.
    """
    return query_all(
        """SELECT p.* FROM copy_positions p
           JOIN copy_mirrors m ON p.mirror_id = m.id
           WHERE p.user_id = %s AND p.status = 'closed' AND m.network = %s
           ORDER BY p.closed_at DESC NULLS LAST, p.id DESC
           LIMIT %s""",
        (int(user_id), network, int(limit)),
    )


def get_open_copy_position_for_product(mirror_id: int, product_id: int) -> Optional[dict]:
    return query_one(
        "SELECT * FROM copy_positions WHERE mirror_id = %s AND product_id = %s AND status = 'open' ORDER BY opened_at DESC LIMIT 1",
        (mirror_id, product_id),
    )


def update_mirror_accounting(
    mirror_id: int,
    *,
    pnl_delta: float = 0.0,
    fees_delta: float = 0.0,
    volume_delta: float = 0.0,
) -> None:
    """Additive accounting update for a copy mirror.

    ``pnl_delta`` is DERIVED gross realized PnL (price pairing — the venue's
    per-fill realized_pnl is always 0 and must never be trusted); fees and
    volume accumulate separately so the rail can judge NET of fees.
    """
    execute(
        """UPDATE copy_mirrors
           SET cumulative_pnl = cumulative_pnl + %s,
               cumulative_fees_usd = COALESCE(cumulative_fees_usd, 0) + %s,
               cumulative_volume_usd = COALESCE(cumulative_volume_usd, 0) + %s
           WHERE id = %s""",
        (float(pnl_delta), float(fees_delta), float(volume_delta), int(mirror_id)),
    )


def set_mirror_unrealized(mirror_id: int, upnl_usd: float) -> None:
    execute(
        """UPDATE copy_mirrors
           SET last_unrealized_pnl_usd = %s, last_rail_checked_at = NOW()
           WHERE id = %s""",
        (float(upnl_usd), int(mirror_id)),
    )


def set_mirror_session(mirror_id: int, session_id: int) -> None:
    execute(
        "UPDATE copy_mirrors SET strategy_session_id = %s WHERE id = %s",
        (int(session_id), int(mirror_id)),
    )


def reduce_copy_position(
    position_id: int, new_size: float, new_leader_size: float, pnl_delta: float = 0.0
) -> None:
    """Shrink an open copy position after mirroring a leader's partial close.

    ``size`` is the follower's remaining base size, ``leader_size`` becomes the
    new baseline the next partial-close comparison measures against, and the
    realized PnL of the closed slice accumulates on the row (the final
    close_copy_position pnl then only carries the LAST slice, so dashboards
    sum row pnl rather than overwrite it).
    """
    execute(
        """UPDATE copy_positions
           SET size = %s, leader_size = %s, pnl = COALESCE(pnl, 0) + %s
           WHERE id = %s AND status = 'open'""",
        (float(new_size), float(new_leader_size), float(pnl_delta), int(position_id)),
    )


def grow_copy_position(
    position_id: int, new_size: float, new_entry_price: float, new_leader_size: float
) -> None:
    """Scale an open copy position UP after mirroring a leader's add.

    Counterpart to :func:`reduce_copy_position`. ``size`` becomes the
    follower's new total base size, ``entry_price`` the size-weighted average
    across the original entry and the add (so PnL stays correct), and
    ``leader_size`` the new baseline the next partial-close comparison measures
    against — without moving that baseline, a later trim would be computed off
    the pre-add size and close far too little.
    """
    execute(
        """UPDATE copy_positions
           SET size = %s, entry_price = %s, leader_size = %s
           WHERE id = %s AND status = 'open'""",
        (float(new_size), float(new_entry_price), float(new_leader_size), int(position_id)),
    )


def close_copy_position(position_id: int, pnl: float = 0.0, reason: str = "leader_closed"):
    execute(
        "UPDATE copy_positions SET status = 'closed', pnl = %s, closed_at = %s, close_reason = %s WHERE id = %s",
        (pnl, datetime.now(timezone.utc).isoformat(), reason, position_id),
    )


def save_copy_snapshot(trader_id: int, network: str, positions_json: str):
    execute(
        """INSERT INTO copy_snapshots (trader_id, network, positions_json)
           VALUES (%s, %s, %s)""",
        (trader_id, network, positions_json),
    )


def get_latest_copy_snapshot(trader_id: int, network: str) -> Optional[dict]:
    return query_one(
        "SELECT * FROM copy_snapshots WHERE trader_id = %s AND network = %s ORDER BY captured_at DESC LIMIT 1",
        (trader_id, network),
    )


# ---------------------------------------------------------------------------
# Strategy Sessions ORM
# ---------------------------------------------------------------------------

_STRATEGY_SESSION_INSERT_COLS = frozenset({
    "user_id", "strategy", "product_id", "product_name", "network",
    "started_at", "status", "config_snapshot",
})


def insert_strategy_session(data: dict) -> Optional[int]:
    filtered = {k: v for k, v in data.items() if k in _STRATEGY_SESSION_INSERT_COLS and v is not None}
    if "started_at" not in filtered:
        filtered["started_at"] = datetime.now(timezone.utc).isoformat()
    if "status" not in filtered:
        filtered["status"] = "running"
    cols = list(filtered.keys())
    vals = [filtered[c] for c in cols]
    query = pgsql.SQL("INSERT INTO strategy_sessions ({}) VALUES ({}) RETURNING id").format(
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in cols),
        pgsql.SQL(", ").join(pgsql.Placeholder() * len(cols)),
    )
    row = execute_returning(
        query,
        vals,
    )
    return row["id"] if row else None


_STRATEGY_SESSION_UPDATE_COLS = frozenset({
    "stopped_at", "status", "total_cycles", "total_orders_placed",
    "total_orders_filled", "total_orders_cancelled", "realized_pnl",
    "total_fees_paid", "total_volume_usd", "total_funding_paid",
    "stop_reason", "error_message",
    # Portfolio workflow (migration 0010): persisted win/loss counters so
    # per-session performance cards can render trustworthy stats.
    "win_count", "loss_count",
})


def update_strategy_session(session_id: int, data: dict):
    filtered = {k: v for k, v in data.items() if k in _STRATEGY_SESSION_UPDATE_COLS}
    if not filtered:
        return
    set_clause = pgsql.SQL(", ").join(
        pgsql.SQL("{} = %s").format(pgsql.Identifier(k)) for k in filtered.keys()
    )
    query = pgsql.SQL("UPDATE strategy_sessions SET {} WHERE id = %s").format(set_clause)
    vals = list(filtered.values()) + [session_id]
    execute(query, vals)


def increment_session_metrics(
    session_id: int,
    cycles: int = 0,
    orders_placed: int = 0,
    orders_filled: int = 0,
    orders_cancelled: int = 0,
    pnl: float = 0.0,
    fees: float = 0.0,
    volume: float = 0.0,
    funding: float = 0.0,
):
    execute(
        """UPDATE strategy_sessions SET
            total_cycles = total_cycles + %s,
            total_orders_placed = total_orders_placed + %s,
            total_orders_filled = total_orders_filled + %s,
            total_orders_cancelled = total_orders_cancelled + %s,
            realized_pnl = realized_pnl + %s,
            total_fees_paid = total_fees_paid + %s,
            total_volume_usd = total_volume_usd + %s,
            total_funding_paid = total_funding_paid + %s
        WHERE id = %s""",
        (cycles, orders_placed, orders_filled, orders_cancelled,
         pnl, fees, volume, funding, session_id),
    )


def get_strategy_session_by_id(
    session_id: int,
    *,
    user_id: int | None = None,
    network: str | None = None,
    strategy: str | None = None,
) -> Optional[dict]:
    """Load one strategy session, optionally pinned to its owner/network/strategy."""
    conditions = ["id = %s"]
    params: list = [int(session_id)]
    if user_id is not None:
        conditions.append("user_id = %s")
        params.append(int(user_id))
    if network:
        conditions.append("network = %s")
        params.append(str(network))
    if strategy:
        conditions.append("strategy = %s")
        params.append(str(strategy))
    return query_one(
        f"SELECT * FROM strategy_sessions WHERE {' AND '.join(conditions)} LIMIT 1",
        tuple(params),
    )


def get_active_strategy_session_for_strategy(
    user_id: int,
    network: str,
    strategy: str,
) -> Optional[dict]:
    return query_one(
        "SELECT * FROM strategy_sessions "
        "WHERE user_id = %s AND network = %s AND strategy = %s AND status = 'running' "
        "ORDER BY started_at DESC LIMIT 1",
        (int(user_id), str(network), str(strategy)),
    )


def rollup_session_from_trades(session_id: int, network: str) -> dict:
    """Recompute session totals from ``trades_<network>``.

    Belt-and-suspenders rollup for the workflow plan: at session end we
    re-aggregate every trade tagged with ``strategy_session_id`` so the
    persisted card numbers match the actual fills (and don't drift when
    venue-sync fills arrive late or when cycle increments missed a fee).

    Returns the resolved totals dict (always returns; on DB error returns
    an empty dict so callers can decide what to do).
    """
    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    try:
        row = query_one(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE status IN ('filled', 'closed', 'partially_filled')) AS filled,
              COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
              COALESCE(SUM(COALESCE(realized_pnl, pnl, 0)), 0) AS realized_pnl,
              -- Volume/fees prefer the venue-authoritative x18 columns (set by
              -- nado_sync per match), falling back to human columns — IDENTICAL to
              -- get_session_live_metrics so the finalize card and the live /mm_status
              -- can never disagree, and a fill counts even if one column set is
              -- missing (the human/x18 split was a source of $0-volume undercounts).
              COALESCE(SUM(COALESCE(
                NULLIF(fee_x18, 0) / 1e18,
                COALESCE(fill_fee, fees, 0) + COALESCE(builder_fee, 0)
              )), 0) AS fees,
              COALESCE(SUM(COALESCE(
                -- quote_filled_x18 is SIGNED (negative for buys); ABS it so the
                -- volume is gross turnover (opens + closes), not net cash flow —
                -- summing the signed value made a flattened run collapse to ~$0.
                ABS(NULLIF(quote_filled_x18, 0)) / 1e18,
                ABS(COALESCE(fill_size, size, 0)) * COALESCE(NULLIF(fill_price, 0), price, 0)
              )), 0) AS volume,
              COALESCE(SUM(COALESCE(funding_paid, 0)), 0) AS funding,
              COUNT(*) FILTER (
                WHERE status IN ('filled', 'closed') AND COALESCE(realized_pnl, pnl, 0) > 1e-9
              ) AS wins,
              COUNT(*) FILTER (
                WHERE status IN ('filled', 'closed') AND COALESCE(realized_pnl, pnl, 0) < -1e-9
              ) AS losses
            FROM {table}
            WHERE strategy_session_id = %s
              AND COALESCE(source, '') <> 'manual'
            """,
            (int(session_id),),
        )
    except Exception:
        return {}
    if not row:
        return {}
    totals = {
        "total_orders_filled": int(row.get("filled") or 0),
        "total_orders_cancelled": int(row.get("cancelled") or 0),
        "realized_pnl": float(row.get("realized_pnl") or 0),
        "total_fees_paid": float(row.get("fees") or 0),
        "total_volume_usd": float(row.get("volume") or 0),
        "total_funding_paid": float(row.get("funding") or 0),
        "win_count": int(row.get("wins") or 0),
        "loss_count": int(row.get("losses") or 0),
    }
    try:
        update_strategy_session(int(session_id), totals)
    except Exception:
        pass
    return totals


def _resolve_session_user_id(session_id: int) -> Optional[int]:
    """Owner of a session (``strategy_sessions.user_id``). Used to force every
    session query to be scoped to the owning user, even when a caller forgot to
    pass ``user_id`` — a session belongs to exactly one user, so this is the
    authoritative cross-user guard."""
    try:
        row = query_one(
            "SELECT user_id FROM strategy_sessions WHERE id = %s", (int(session_id),)
        )
    except Exception:
        return None
    return int(row["user_id"]) if row and row.get("user_id") is not None else None


def _session_match_where(session_id: int, user_id: Optional[int] = None) -> tuple[str, list]:
    """WHERE clause + params selecting a session's fills in ``trades_<network>``.

    Scoped by ``strategy_session_id`` AND ``user_id`` (per-user, per-run). The
    session id is a global ``strategy_sessions.id`` (unique across users), so it
    alone identifies one run — but we ALSO pin ``user_id`` as defense in depth so
    a mis-tagged or future venue-synced row can never leak another user's fills
    into this user's PnL/SL. (The previous product+time-window fallback that
    pulled untagged fills on the product into the session is gone — that was the
    false-SL contamination.)

    ALSO excludes synthetic ``source='manual'`` rows: the account-wide stop
    flatten (``close_all_positions`` -> ``_record_close_in_db``) writes ONE
    close row sized to the whole venue position (not the session's own size) and
    inherits a session id from a matched open trade. Counting it corrupted the
    run — e.g. a session that opened 0.0032 BTC booked a 0.02785 "close",
    inflating signed cash flow far past the run's real volume. Engine fills are
    ``source='strategy'``; DN/vol closes use their own sources, so only the
    mis-sized flatten placeholder is dropped here.
    """
    if user_id is None:
        return "strategy_session_id = %s AND COALESCE(source, '') <> 'manual'", [int(session_id)]
    return (
        "strategy_session_id = %s AND user_id = %s AND COALESCE(source, '') <> 'manual'",
        [int(session_id), int(user_id)],
    )


def _session_realized_pnl(
    session_id: int, table: str, user_id: Optional[int] = None
) -> float:
    """Session realized PnL — position-aware and DERIVED from session fills.

    This venue reports NO per-fill realized PnL, so realized is replayed from the
    run's own fills. Delegates to ``get_session_live_metrics`` so the finalize
    rollup and the live dashboard compute realized PnL identically and can never
    disagree. ``table`` selects the network; ``user_id`` auto-resolves from the
    session when not supplied."""
    network = "testnet" if str(table).lower().endswith("testnet") else "mainnet"
    metrics = get_session_live_metrics(int(session_id), network, user_id=user_id)
    return float(metrics.get("realized_pnl") or 0.0)


def _derive_session_realized_pnl(
    table: str,
    where: str,
    params: list,
    session_id: int,
) -> float | None:
    """Replay this session's fills to realize closed legs while residual inventory remains.

    SESSION-PNL-CONTAMINATION (2026-07-27): a run reported realized +$205.72 when
    its own fills were worth ~$1.56. The replay had closed 0.03256 BTC against a
    blended entry of $58,912.60 — 1.7x the session's own closed size (0.01910),
    at a price from long before the run started. Fills that PREDATE the session
    had been tagged into it, so the replay built its entry basis from another
    era's inventory and the inflated PnL tripped the take-profit rail, stopping
    a bot that was actually ~$1 up.

    A session's own fills can never predate ``started_at``, so the window is
    pinned here. This is the same class of leak the product+time-window fallback
    caused (see ``_session_match_where``), from the opposite direction.
    """
    from src.nadobro.quant.portfolio_calculator import realized_pnl_windows_from_rows

    rows = query_all(
        f"""
        SELECT
          COALESCE(NULLIF(product_id, 0), (
            SELECT product_id FROM strategy_sessions WHERE id = %s
          )) AS product_id,
          side, fill_size, size, fill_price, price, isolated, source,
          submission_idx, base_filled_x18, quote_filled_x18,
          COALESCE(filled_at, created_at) AS filled_at
        FROM {table}
        WHERE {where}
          AND status IN ('filled', 'closed', 'partially_filled')
          -- A session's fills cannot predate the run. Without this, fills
          -- tagged in from an earlier era set the entry basis and inflated
          -- realized PnL ~130x, firing the TP rail on a ~$1 run.
          AND COALESCE(filled_at, created_at) >= COALESCE(
                (SELECT started_at FROM strategy_sessions WHERE id = %s),
                '-infinity'::timestamptz)
        ORDER BY COALESCE(filled_at, created_at), id
        """,
        (int(session_id), *tuple(params), int(session_id)),
    )
    realized = float(realized_pnl_windows_from_rows(rows).get("total_pnl") or 0)
    _log_session_replay_diagnostics(session_id, rows, realized)
    return realized


# Realized PnL beyond this multiple of the session's own price range x traded
# base is mathematically impossible from its own fills — it means foreign rows
# are in the replay. Logged (never silently swallowed) so the contaminating row
# is identifiable in production.
_SESSION_PNL_SANITY_SLACK = 1.5


def _log_session_replay_diagnostics(session_id: int, rows: list, realized: float) -> None:
    """Dump the exact rows a session replay consumed when its realized PnL is
    not achievable from those rows' own price range.

    Upper bound: |realized| <= (max_price - min_price) * total_base_traded.
    Exceeding it proves the entry basis came from outside this row set.
    """
    try:
        if not rows:
            return
        prices, base = [], 0.0
        for r in rows:
            px = float(r.get("fill_price") or r.get("price") or 0)
            sz = abs(float(r.get("fill_size") or r.get("size") or 0))
            if px > 0:
                prices.append(px)
            base += sz
        if len(prices) < 2 or base <= 0:
            return
        bound = (max(prices) - min(prices)) * base * _SESSION_PNL_SANITY_SLACK
        if abs(realized) <= bound + 1e-9:
            return
        logger.error(
            "SESSION-PNL-CONTAMINATION session=%s realized=%.4f exceeds bound=%.4f "
            "(price range %.2f-%.2f, base %.6f over %d fills) — dumping rows",
            session_id, realized, bound, min(prices), max(prices), base, len(rows),
        )
        for r in rows:
            logger.error(
                "  session=%s fill side=%s size=%s price=%s source=%s isolated=%s "
                "submission_idx=%s filled_at=%s",
                session_id, r.get("side"),
                r.get("fill_size") or r.get("size"),
                r.get("fill_price") or r.get("price"),
                r.get("source"), r.get("isolated"),
                r.get("submission_idx"), r.get("filled_at"),
            )
    except Exception:  # noqa: BLE001 - diagnostics must never break the read path
        logger.debug("session replay diagnostics failed session=%s", session_id, exc_info=True)


def get_session_live_metrics(
    session_id: int, network: str, user_id: Optional[int] = None
) -> dict:
    """Live session totals computed straight from ``trades_<network>`` for the
    active session — restart-safe and cross-process, used by /mm_status so the
    dashboard reflects engine fills the moment DbTradeRecorder writes them
    (rather than the in-memory ``state`` the engine never populates).

    Scoped per ``user_id`` + ``strategy_session_id`` so the numbers are unique
    per user AND per run — one user's fills can never leak into another's PnL/SL
    (see _session_match_where). ``user_id`` is auto-resolved from the session
    when not supplied; an unresolvable owner yields empty rather than an
    account-wide query."""
    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    if user_id is None:
        user_id = _resolve_session_user_id(session_id)
        if user_id is None:
            return {}
    where, params = _session_match_where(session_id, user_id)
    try:
        row = query_one(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE status IN ('filled', 'closed', 'partially_filled')) AS fills,
              -- Per-fill volume/fees prefer the venue-authoritative x18 columns
              -- (set by nado_sync on each match) so the run's totals grow as
              -- orders fill/close, even before the recorder columns are present.
              COALESCE(SUM(COALESCE(
                -- quote_filled_x18 is SIGNED (negative for buys); ABS it so the
                -- volume is gross turnover (opens + closes), not net cash flow —
                -- summing the signed value made a flattened run collapse to ~$0.
                ABS(NULLIF(quote_filled_x18, 0)) / 1e18,
                ABS(COALESCE(fill_size, size, 0)) * COALESCE(NULLIF(fill_price, 0), price, 0)
              )), 0) AS volume,
              COALESCE(SUM(COALESCE(
                NULLIF(fee_x18, 0) / 1e18,
                COALESCE(fill_fee, fees, 0) + COALESCE(builder_fee, 0)
              )), 0) AS fees,
              -- The session's OWN net open base and signed cash flow, so PnL can
              -- be marked to the live mid against THIS run's position only (no
              -- account-aggregate contamination). base/quote prefer venue x18.
              COALESCE(SUM(
                CASE WHEN side = 'long'
                       THEN  ABS(COALESCE(NULLIF(base_filled_x18, 0) / 1e18, fill_size, size, 0))
                     WHEN side = 'short'
                       THEN -ABS(COALESCE(NULLIF(base_filled_x18, 0) / 1e18, fill_size, size, 0))
                     ELSE 0 END
              ), 0) AS net_base,
              COALESCE(SUM(
                CASE WHEN side = 'short'
                       THEN  ABS(COALESCE(NULLIF(quote_filled_x18, 0) / 1e18,
                                          ABS(COALESCE(fill_size, size, 0)) * COALESCE(NULLIF(fill_price, 0), price, 0)))
                     WHEN side = 'long'
                       THEN -ABS(COALESCE(NULLIF(quote_filled_x18, 0) / 1e18,
                                          ABS(COALESCE(fill_size, size, 0)) * COALESCE(NULLIF(fill_price, 0), price, 0)))
                     ELSE 0 END
              ), 0) AS signed_cash,
              -- Realized PnL building blocks (flat-aware decision below). The
              -- venue per-match realized_pnl_x18 is authoritative; the recorder
              -- buy/sell cash-flow is ONLY equal to realized PnL when the run is
              -- flat — for an OPEN position it is just net cash spent and must
              -- NEVER be shown as "realized" (that produced the bogus -$506).
              COALESCE(SUM(realized_pnl_x18) FILTER (WHERE realized_pnl_x18 IS NOT NULL), 0) / 1e18
                AS venue_pnl,
              COUNT(*) FILTER (WHERE realized_pnl_x18 IS NOT NULL) AS venue_rows,
              COUNT(*) FILTER (
                WHERE source = 'strategy' AND fill_price IS NOT NULL AND submission_idx IS NULL
              ) AS pending_sync,
              COALESCE(SUM(
                CASE WHEN side = 'short'
                       THEN  ABS(COALESCE(fill_size, size, 0)) * COALESCE(NULLIF(fill_price, 0), price, 0)
                     WHEN side = 'long'
                       THEN -ABS(COALESCE(fill_size, size, 0)) * COALESCE(NULLIF(fill_price, 0), price, 0)
                     ELSE 0 END
              ) FILTER (WHERE fill_price IS NOT NULL), 0) AS recorder_gross
            FROM {table}
            WHERE {where}
            """,
            tuple(params),
        )
    except Exception:
        return {}
    if not row:
        return {}
    net_base = float(row.get("net_base") or 0)
    is_flat = abs(net_base) <= 1e-9
    # Realized PnL is DERIVED from session fills, NOT read from a venue field.
    # Replay fills position-aware so partial closes count even while the run has
    # residual inventory; open-only buys/sells still realize 0. If the replay read
    # fails, keep the previous safe flat-only fallback rather than surfacing raw
    # cash spent as realized PnL.
    try:
        replayed = _derive_session_realized_pnl(table, where, params, int(session_id))
        realized = float(replayed or 0)
    except Exception:
        realized = float(row.get("signed_cash") or 0) if is_flat else 0.0
    return {
        "fills": int(row.get("fills") or 0),
        "volume": float(row.get("volume") or 0),
        "fees": float(row.get("fees") or 0),
        "net_base": net_base,
        "signed_cash": float(row.get("signed_cash") or 0),
        "realized_pnl": realized,
    }


def get_session_turnover(
    user_id: int, network: str, product_id: int, started_at, stopped_at=None
) -> dict:
    """Real traded turnover for THIS user on THIS product since the run started —
    the "accumulated position value" the user expects as Session Volume. Unlike
    the strict session-tagged volume (which undercounts because position closes
    flow through a separate archive path and not every fill is tagged), this
    reflects what Nado shows: every fill on the product in the run's window.

    Scoped to user_id + product_id + the session time window, so it never mixes
    in another user or another product. Returns {volume, fills}."""
    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    if product_id is None or started_at is None:
        return {"volume": 0.0, "fills": 0}
    try:
        row = query_one(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE status IN ('filled', 'closed', 'partially_filled')) AS fills,
              COALESCE(SUM(COALESCE(
                -- signed x18 → ABS for gross turnover (see rollup note above).
                ABS(NULLIF(quote_filled_x18, 0)) / 1e18,
                ABS(COALESCE(fill_size, size, 0)) * COALESCE(NULLIF(fill_price, 0), price, 0)
              )), 0) AS volume
            FROM {table}
            WHERE user_id = %s AND product_id = %s
              AND COALESCE(filled_at, created_at) >= %s
              AND (%s IS NULL OR COALESCE(filled_at, created_at) <= %s)
            """,
            (int(user_id), int(product_id), started_at, stopped_at, stopped_at),
        )
    except Exception:
        return {"volume": 0.0, "fills": 0}
    if not row:
        return {"volume": 0.0, "fills": 0}
    return {"volume": float(row.get("volume") or 0), "fills": int(row.get("fills") or 0)}


def get_account_realized_pnl_windows(user_id: int, network: str, now=None) -> dict:
    """Account-level realized PnL (24h/7d/30d/all), DERIVED position-aware from the
    user's COMPLETE venue-confirmed fill history in ``trades_<network>``.

    This venue reports no per-fill realized PnL, so the per-fill sum that powered
    the portfolio deck was always 0. Here we replay every real fill per product in
    time order and realize PnL on position reductions (see
    ``portfolio_calculator.realized_pnl_windows_from_rows``). Synthetic flatten
    rows are excluded by ``submission_idx IS NOT NULL`` — they can never acquire
    one (nado_sync's enrich skips ``order_type ILIKE '%close%'`` recorder rows).
    The old ``source <> 'manual'`` filter targeted those same synthetic rows but
    ALSO dropped every REAL manual fill (unlinked venue fills default to
    source='manual'), so a manual trader's account PnL read ~0 — removed.
    Returns an empty dict on any error so the display path never raises."""
    from src.nadobro.quant.portfolio_calculator import realized_pnl_windows_from_rows

    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    try:
        rows = query_all(
            f"""
            SELECT DISTINCT ON (submission_idx)
                   product_id, side, fill_size, size, fill_price, price, isolated,
                   base_filled_x18, quote_filled_x18,
                   COALESCE(filled_at, created_at) AS filled_at
            FROM {table}
            WHERE user_id = %s
              AND submission_idx IS NOT NULL
              AND COALESCE(product_id, 0) <> 0
              AND status IN ('filled', 'closed', 'partially_filled')
            ORDER BY submission_idx, COALESCE(filled_at, created_at), id
            """,
            (int(user_id),),
        )
    except Exception:
        return {}
    return realized_pnl_windows_from_rows(rows, now=now)


def get_analytics_fills(user_id: int, network: str) -> list[dict]:
    """Every venue-confirmed fill for a user — the COMPLETE ledger behind the
    portfolio analytics (volume / fees / realized PnL).

    The card previously aggregated volume from a transient
    ``client.get_matches(limit=200)`` list while PnL came from this table, so
    the two disagreed (Volume $0.00 next to Realized +$16.73) and "All" volume
    was capped at 200 fills. One source now feeds both.

    Deduped per ``submission_idx`` (unique per match — verified on mainnet), so
    the bot's synthetic close rows and duplicate recorder rows never
    double-count. ``[]`` on any error so the deck never raises.
    """
    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    try:
        return query_all(
            f"""
            SELECT DISTINCT ON (submission_idx)
                   product_id, product_name, side,
                   fill_size, size, fill_price, price,
                   base_filled_x18, quote_filled_x18, fee_x18,
                   fill_fee, fees, builder_fee, via_nadobro,
                   COALESCE(filled_at, created_at) AS filled_at
            FROM {table}
            WHERE user_id = %s
              AND submission_idx IS NOT NULL
              AND COALESCE(product_id, 0) <> 0
              AND status IN ('filled', 'closed', 'partially_filled')
            ORDER BY submission_idx, COALESCE(filled_at, created_at), id
            """,
            (int(user_id),),
        )
    except Exception:
        return []


def backfill_via_nadobro(network: str, limit: int = 5000) -> int:
    """Stamp ``via_nadobro`` on historical fills by matching their
    ``order_digest`` against ``order_intents`` (the registry every bot-placed
    order writes). Rows with no intent are stamped FALSE = traded on the Nado
    UI. Idempotent; returns the number of rows updated."""
    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    try:
        row = execute_returning(
            f"""
            WITH target AS (
                SELECT id FROM {table}
                WHERE via_nadobro IS NULL
                  AND submission_idx IS NOT NULL
                LIMIT %s
            )
            UPDATE {table} t
               SET via_nadobro = (
                     t.order_digest IS NOT NULL
                     AND EXISTS (SELECT 1 FROM order_intents oi
                                  WHERE oi.order_digest = t.order_digest)
                   )
              FROM target
             WHERE t.id = target.id
            RETURNING (SELECT COUNT(*) FROM target) AS n
            """,
            (int(limit),),
        )
        return int((row or {}).get("n") or 0)
    except Exception:
        return 0


def get_paired_trades(
    user_id: int,
    network: str,
    *,
    product_id: int | None = None,
    closed_only: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Per-position round-trips for a user's NORMAL + engine trades, paired from
    the authoritative venue fill ledger so each open is matched to its close and a
    per-trade PnL card (entry/exit/PnL/fees) can be built.

    Source = venue-confirmed fills only (``submission_idx IS NOT NULL``), deduped
    per ``submission_idx`` so the bot's synthetic ``MARKET_CLOSE`` rows and any
    duplicate recorder/match pair can never double-count. Pairing is position-
    aware (see ``portfolio_calculator.pair_fills_into_trades``). Returns newest
    closed trades first, with any still-open position last. ``[]`` on any error."""
    from src.nadobro.quant.portfolio_calculator import pair_fills_into_trades

    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    # product_id 0 = product-less venue fills (this indexer's match feed carries no
    # product_id); excluded so per-product pairing never mixes BTC with ETH.
    where = ["user_id = %s", "submission_idx IS NOT NULL", "COALESCE(product_id, 0) <> 0"]
    params: list = [int(user_id)]
    if product_id is not None:
        where.append("product_id = %s")
        params.append(int(product_id))
    try:
        rows = query_all(
            f"""
            SELECT DISTINCT ON (submission_idx)
                   product_id, side, fill_size, size, fill_price, price,
                   base_filled_x18, quote_filled_x18, fee_x18, fill_fee, fees, builder_fee,
                   submission_idx, COALESCE(filled_at, created_at) AS filled_at
            FROM {table}
            WHERE {" AND ".join(where)}
            ORDER BY submission_idx, COALESCE(filled_at, created_at)
            """,
            tuple(params),
        )
    except Exception:
        return []
    trades = [t.to_dict() for t in pair_fills_into_trades(rows)]
    if closed_only:
        trades = [t for t in trades if t.get("closed")]
    # Newest first (open trade, with no closed_at, sorts to the top).
    trades.sort(key=lambda t: (t.get("closed_at") is not None, t.get("closed_at") or ""), reverse=True)
    if limit is not None and int(limit) > 0:
        trades = trades[: int(limit)]
    return trades


def get_session_recent_fills(
    session_id: int, network: str, limit: int = 10, user_id: Optional[int] = None
) -> list:
    """Most recent recorded fills for a session, newest first — powers
    /mm_fills for engine strategies. Scoped per ``user_id`` + ``strategy_session_id``
    (see _session_match_where); ``user_id`` auto-resolves from the session and an
    unresolvable owner yields [] rather than an account-wide query."""
    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    if user_id is None:
        user_id = _resolve_session_user_id(session_id)
        if user_id is None:
            return []
    where, params = _session_match_where(session_id, user_id)
    try:
        return query_all(
            f"""
            SELECT side, COALESCE(fill_size, size) AS size,
                   COALESCE(NULLIF(fill_price, 0), price) AS price,
                   EXTRACT(EPOCH FROM COALESCE(filled_at, created_at)) AS ts
            FROM {table}
            WHERE ({where})
              AND status IN ('filled', 'closed', 'partially_filled')
            ORDER BY COALESCE(filled_at, created_at) DESC
            LIMIT %s
            """,
            (*params, int(max(1, limit))),
        )
    except Exception:
        return []


def get_open_position_rows_for_product(user_id: int, network: str, product_id: int) -> list:
    """Open ``positions`` rows for one product (cross + each isolated child),
    kept fresh by ``nado_sync``. Carries the venue-authoritative unrealized PnL
    (``est_pnl``), entry, liq price, margin and leverage that Nado shows.
    Returns [] on any error (read-only display path must never raise)."""
    try:
        return query_all(
            """
            SELECT side, size, avg_entry_price, est_liq_price, est_pnl,
                   margin_used, leverage, isolated,
                   EXTRACT(EPOCH FROM synced_at) AS synced_ts
            FROM positions
            WHERE user_id = %s AND network = %s AND product_id = %s
              AND status = 'open' AND closed_at IS NULL
            """,
            (int(user_id), str(network), int(product_id)),
        )
    except Exception:
        return []


def count_open_orders_for_product(user_id: int, network: str, product_id: int) -> int:
    """Resting order count for one product from the nado_sync-maintained
    ``open_orders`` table. Returns 0 on any error."""
    try:
        row = query_one(
            """
            SELECT COUNT(*) AS n FROM open_orders
            WHERE user_id = %s AND network = %s AND product_id = %s
              AND status IN ('open', 'pending', 'armed')
            """,
            (int(user_id), str(network), int(product_id)),
        )
        return int(row.get("n") or 0) if row else 0
    except Exception:
        return 0


def rollup_engine_session_pnl_funding(session_id: int, network: str) -> dict:
    """Engine-strategy finalize: source realized PnL + funding that the legacy
    human-column rollup can't see, and write them onto the session.

    Engine fills are bridged into ``trades_<network>`` by ``DbTradeRecorder``
    (human volume/fees, ``realized_pnl`` left NULL — grid PnL is not per-fill).
    The authoritative realized PnL comes from the venue's per-match
    ``realized_pnl_x18`` (back-linked to the session once the engine writes
    ``order_intents``); when the venue sync hasn't caught up we fall back to the
    recorder rows' buy/sell/fee decomposition (the same math the engine's
    ``engine_position_hold`` would give). Funding is summed from the venue
    ``funding_payments_<network>`` feed for the session's product/window.

    Must run AFTER ``rollup_session_from_trades`` (which zeroes ``realized_pnl``
    for engine sessions); this overwrites with the correct value. Gated by the
    caller to engine strategies so legacy sessions are never touched.

    Returns the resolved ``{realized_pnl, total_funding_paid}`` (empty on error).
    """
    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    funding_table = "funding_payments_testnet" if str(network).lower() == "testnet" else "funding_payments_mainnet"
    try:
        sess = query_one(
            "SELECT user_id, product_id, started_at, stopped_at FROM strategy_sessions WHERE id = %s",
            (int(session_id),),
        )
    except Exception:
        return {}
    if not sess:
        return {}

    # 1) Venue-authoritative realized PnL (per-match), with recorder-row
    #    buy/sell/fee fallback when the venue sync hasn't tagged matches yet.
    #    Shared with /mm_status's live read so both surfaces agree. Scoped to
    #    the session owner so finalize never mixes in another user's fills.
    product_id = sess.get("product_id")
    started_at = sess.get("started_at")
    stopped_at = sess.get("stopped_at")
    realized_pnl = _session_realized_pnl(
        int(session_id), table, int(sess["user_id"]) if sess.get("user_id") is not None else None
    )

    # 2) Funding: realized funding payments on the session's product within the
    #    session window (paid-positive, matching total_funding_paid convention).
    funding = 0.0
    if product_id is not None and started_at is not None:
        try:
            frow = query_one(
                f"""
                SELECT COALESCE(SUM(amount_x18), 0) / 1e18 AS funding
                FROM {funding_table}
                WHERE user_id = %s AND product_id = %s
                  AND paid_at >= %s
                  AND (%s IS NULL OR paid_at <= %s)
                """,
                (int(sess["user_id"]), int(product_id), started_at, stopped_at, stopped_at),
            )
            funding = float(frow.get("funding") or 0) if frow else 0.0
        except Exception:
            funding = 0.0

    totals = {"realized_pnl": realized_pnl, "total_funding_paid": funding}
    try:
        update_strategy_session(int(session_id), totals)
    except Exception:
        pass
    return totals


def get_strategy_sessions_by_user(
    user_id: int,
    strategy: str = None,
    network: str = None,
    limit: int = 50,
) -> list:
    conditions = ["user_id = %s"]
    params: list = [user_id]
    if strategy:
        conditions.append("strategy = %s")
        params.append(strategy)
    if network:
        conditions.append("network = %s")
        params.append(network)
    params.append(limit)
    where = " AND ".join(conditions)
    return query_all(
        f"SELECT * FROM strategy_sessions WHERE {where} ORDER BY started_at DESC LIMIT %s",
        params,
    )


def get_active_strategy_session(user_id: int, network: str) -> Optional[dict]:
    return query_one(
        "SELECT * FROM strategy_sessions WHERE user_id = %s AND network = %s AND status = 'running' ORDER BY started_at DESC LIMIT 1",
        (user_id, network),
    )


def get_running_strategy_sessions(user_id: int, network: str | None = None) -> list:
    """All sessions still marked running (used for status display and supersede on new start)."""
    conditions = ["user_id = %s", "status = 'running'"]
    params: list = [user_id]
    if network:
        conditions.append("network = %s")
        params.append(network)
    where = " AND ".join(conditions)
    return query_all(
        f"SELECT * FROM strategy_sessions WHERE {where} ORDER BY started_at DESC",
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Time limits
# ---------------------------------------------------------------------------

_VALID_TIME_LIMIT_SOURCES = frozenset({"manual", "bro", "time_limit"})


def _json_payload(value, default):
    if value is None:
        return json.dumps(default)
    if isinstance(value, str):
        return value
    return json.dumps(value)


def set_position_time_limit(position_id: int, ts, source: str = "manual"):
    source = source if source in _VALID_TIME_LIMIT_SOURCES else "manual"
    execute(
        """UPDATE positions
           SET time_limit = %s, time_limit_source = %s, time_limit_fired_at = NULL
           WHERE id = %s""",
        (ts, source, position_id),
    )


def set_order_time_limit(order_id: int, ts, source: str = "manual"):
    source = source if source in _VALID_TIME_LIMIT_SOURCES else "manual"
    execute(
        """UPDATE open_orders
           SET time_limit = %s, time_limit_source = %s, time_limit_fired_at = NULL, updated_at = now()
           WHERE id = %s""",
        (ts, source, order_id),
    )


def clear_position_time_limit(position_id: int):
    execute(
        "UPDATE positions SET time_limit = NULL, time_limit_source = NULL, time_limit_fired_at = NULL WHERE id = %s",
        (position_id,),
    )


def clear_order_time_limit(order_id: int):
    execute(
        """UPDATE open_orders
           SET time_limit = NULL, time_limit_source = NULL, time_limit_fired_at = NULL, updated_at = now()
           WHERE id = %s""",
        (order_id,),
    )


def fetch_due_time_limits(now_utc, network: str, limit: int = 50) -> dict:
    """Atomically claim due position/order time limits for one network."""
    if network not in _VALID_NETWORKS:
        raise ValueError(f"Invalid network: {network}")
    rows_positions = query_all(
        """
        WITH due AS (
            SELECT id
            FROM positions
            WHERE network = %s
              AND status = 'open'
              AND time_limit IS NOT NULL
              AND time_limit <= %s
              AND time_limit_fired_at IS NULL
            ORDER BY time_limit ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE positions p
        SET time_limit_fired_at = %s
        FROM due
        WHERE p.id = due.id
        RETURNING p.*
        """,
        (network, now_utc, limit, now_utc),
    )
    rows_orders = query_all(
        """
        WITH due AS (
            SELECT id
            FROM open_orders
            WHERE network = %s
              AND status IN ('open', 'pending', 'armed')
              AND time_limit IS NOT NULL
              AND time_limit <= %s
              AND time_limit_fired_at IS NULL
            ORDER BY time_limit ASC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE open_orders o
        SET time_limit_fired_at = %s, updated_at = now()
        FROM due
        WHERE o.id = due.id
        RETURNING o.*
        """,
        (network, now_utc, limit, now_utc),
    )
    return {"positions": rows_positions, "orders": rows_orders}


# ---------------------------------------------------------------------------
# Fill Sync Queue ORM
# ---------------------------------------------------------------------------

def insert_overlay_signal(data: dict) -> Optional[int]:
    """Persist one financial-overlay signal + the bounded action it applied.
    Best-effort — the caller wraps this so a log failure never breaks a tick."""
    cols = [
        "user_id", "network", "strategy", "product_id", "product_name",
        "strategy_session_id", "bias", "regime", "confidence", "entry_ok",
        "scale", "spread_mult", "sl_pct", "tp_pct",
    ]
    json_cols = {"action_json", "reasons_json", "risks_json"}
    filtered = {k: v for k, v in data.items() if k in cols and v is not None}
    payload = dict(filtered)
    for jc in json_cols:
        if data.get(jc) is not None:
            payload[jc] = json.dumps(data[jc])
    col_names = list(payload.keys())
    if not col_names:
        return None
    vals = [payload[c] for c in col_names]
    query = pgsql.SQL("INSERT INTO overlay_signals ({}) VALUES ({}) RETURNING id").format(
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in col_names),
        pgsql.SQL(", ").join(pgsql.Placeholder() * len(col_names)),
    )
    row = execute_returning(query, vals)
    return row["id"] if row else None


def get_overlay_signals(user_id: int, network: str, since=None, limit: int = 500) -> list:
    """Recent financial-overlay signals for a user/network (newest first). Used
    by Night HOWL to explain what the overlay did. ``[]`` on any error."""
    try:
        clauses = ["user_id = %s", "network = %s"]
        params: list = [int(user_id), str(network)]
        if since is not None:
            clauses.append("ts >= %s")
            params.append(since)
        params.append(int(limit))
        return query_all(
            f"SELECT strategy, product_name, bias, regime, confidence, entry_ok, "
            f"scale, spread_mult, action_json, reasons_json, risks_json, ts "
            f"FROM overlay_signals WHERE {' AND '.join(clauses)} "
            f"ORDER BY ts DESC LIMIT %s",
            tuple(params),
        ) or []
    except Exception:
        return []


def insert_signal_outcome(data: dict) -> Optional[int]:
    """Persist one graded outcome for an overlay signal at one horizon.

    Idempotent by ``(signal_id, horizon)`` — a re-run of the scorer over the same
    window is a no-op and returns ``None`` rather than duplicating the row. Like
    ``insert_overlay_signal`` this filters against an explicit column allowlist,
    so a new column means editing the migration, ``db.py``'s startup DDL, AND
    this list.
    """
    cols = [
        "signal_id", "user_id", "network", "strategy", "product_id",
        "product_name", "ts_signal", "mid_at_signal", "bias", "regime",
        "confidence", "horizon", "fwd_return", "excursion_up",
        "excursion_down", "directional_hit", "bars_used",
    ]
    # ``directional_hit`` is deliberately nullable (a neutral call cannot be
    # right or wrong), so filter on presence, not truthiness.
    payload = {k: v for k, v in data.items() if k in cols and v is not None}
    col_names = list(payload.keys())
    if not col_names:
        return None
    vals = [payload[c] for c in col_names]
    query = pgsql.SQL(
        "INSERT INTO signal_outcomes ({}) VALUES ({}) "
        "ON CONFLICT (signal_id, horizon) DO NOTHING RETURNING id"
    ).format(
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in col_names),
        pgsql.SQL(", ").join(pgsql.Placeholder() * len(col_names)),
    )
    row = execute_returning(query, vals)
    return row["id"] if row else None


def get_ungraded_signals(
    horizon: str,
    *,
    ready_before,
    not_older_than,
    limit: int = 500,
) -> list:
    """Overlay signals old enough for ``horizon`` to be knowable but not yet graded.

    ``ready_before`` is ``now - horizon`` (the outcome exists) and
    ``not_older_than`` bounds the backlog so a long outage doesn't make the
    scorer chew through months of history on one pass. Oldest first, so repeated
    runs drain the queue in order. ``[]`` on any error — grading is best-effort
    and must never break the scheduler.
    """
    try:
        return query_all(
            "SELECT s.id, s.user_id, s.network, s.strategy, s.product_id, "
            "s.product_name, s.ts, s.bias, s.regime, s.confidence "
            "FROM overlay_signals s "
            "LEFT JOIN signal_outcomes o "
            "  ON o.signal_id = s.id AND o.horizon = %s "
            "WHERE o.id IS NULL AND s.ts <= %s AND s.ts >= %s "
            "ORDER BY s.ts ASC LIMIT %s",
            (str(horizon), ready_before, not_older_than, int(limit)),
        ) or []
    except Exception:
        return []


def get_signal_outcomes(
    *,
    user_id: Optional[int] = None,
    network: Optional[str] = None,
    horizon: Optional[str] = None,
    since=None,
    limit: int = 5000,
) -> list:
    """Graded signal outcomes for metrics (newest first).

    Every filter is optional: the ``/structure`` card reads one user's slice,
    while the nightly weight fit reads across users to get a sample large enough
    to be worth anything. ``[]`` on any error.
    """
    try:
        clauses: list = []
        params: list = []
        if user_id is not None:
            clauses.append("user_id = %s")
            params.append(int(user_id))
        if network is not None:
            clauses.append("network = %s")
            params.append(str(network))
        if horizon is not None:
            clauses.append("horizon = %s")
            params.append(str(horizon))
        if since is not None:
            clauses.append("ts_signal >= %s")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(int(limit))
        return query_all(
            "SELECT signal_id, user_id, network, strategy, product_id, "
            "product_name, ts_signal, mid_at_signal, bias, regime, confidence, "
            "horizon, fwd_return, excursion_up, excursion_down, "
            "directional_hit, bars_used "
            f"FROM signal_outcomes {where}"
            "ORDER BY ts_signal DESC LIMIT %s",
            tuple(params),
        ) or []
    except Exception:
        return []


def insert_fill_sync(data: dict) -> Optional[int]:
    cols = ["trade_id", "network", "user_id", "subaccount_hex", "order_digest", "product_id", "placed_at_ts"]
    filtered = {k: v for k, v in data.items() if k in cols and v is not None}
    col_names = list(filtered.keys())
    vals = [filtered[c] for c in col_names]
    query = pgsql.SQL("INSERT INTO fill_sync_queue ({}) VALUES ({}) RETURNING id").format(
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in col_names),
        pgsql.SQL(", ").join(pgsql.Placeholder() * len(col_names)),
    )
    row = execute_returning(
        query,
        vals,
    )
    return row["id"] if row else None


def get_pending_fill_syncs(limit: int = 100) -> list:
    return query_all(
        "SELECT * FROM fill_sync_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT %s",
        (limit,),
    )


def claim_pending_fill_syncs(limit: int = 100, stale_after_minutes: int = 10) -> list:
    """Atomically claim pending fill-sync rows for one worker/process."""
    return query_all(
        """
        WITH claim AS (
            SELECT id
            FROM fill_sync_queue
            WHERE status = 'pending'
               OR (status = 'processing' AND claimed_at < now() - (%s * interval '1 minute'))
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT %s
        )
        UPDATE fill_sync_queue q
        SET status = 'processing',
            claimed_at = now(),
            attempts = attempts + 1
        FROM claim
        WHERE q.id = claim.id
        RETURNING q.*
        """,
        (int(stale_after_minutes), int(limit)),
    )


def release_fill_sync(sync_id: int):
    execute(
        "UPDATE fill_sync_queue SET status = 'pending', claimed_at = NULL WHERE id = %s AND status = 'processing'",
        (sync_id,),
    )


def resolve_fill_sync(sync_id: int):
    execute(
        "UPDATE fill_sync_queue SET status = 'resolved', claimed_at = NULL, resolved_at = %s WHERE id = %s",
        (datetime.now(timezone.utc).isoformat(), sync_id),
    )


def expire_fill_sync(sync_id: int):
    execute(
        "UPDATE fill_sync_queue SET status = 'expired', claimed_at = NULL, resolved_at = %s WHERE id = %s",
        (datetime.now(timezone.utc).isoformat(), sync_id),
    )


def increment_fill_sync_attempts(sync_id: int):
    execute(
        "UPDATE fill_sync_queue SET attempts = attempts + 1 WHERE id = %s",
        (sync_id,),
    )


# ---------------------------------------------------------------------------
# Order Intent ORM
# ---------------------------------------------------------------------------

def get_order_intent_row(intent_id: str) -> Optional[dict]:
    row = query_one(
        "SELECT intent_id, status, value, trade_id, order_digest, created_at, updated_at FROM order_intents WHERE intent_id = %s",
        (str(intent_id),),
    )
    if not row:
        return None
    value = row.get("value") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    payload = dict(value)
    payload.setdefault("intent_id", row.get("intent_id"))
    payload["status"] = row.get("status") or payload.get("status")
    if row.get("trade_id") is not None:
        payload["trade_id"] = row.get("trade_id")
    if row.get("order_digest"):
        payload["order_digest"] = row.get("order_digest")
    if row.get("created_at") is not None:
        payload.setdefault("created_at", row.get("created_at"))
    if row.get("updated_at") is not None:
        payload.setdefault("updated_at", row.get("updated_at"))
    return payload


def reserve_order_intent_row(intent_id: str, payload: dict, stale_after_seconds: int = 120) -> Optional[dict]:
    stored = json.dumps(payload)
    return execute_returning(
        """
        INSERT INTO order_intents (intent_id, status, value, trade_id, order_digest)
        VALUES (%s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (intent_id) DO UPDATE
        SET status = EXCLUDED.status,
            value = EXCLUDED.value,
            trade_id = EXCLUDED.trade_id,
            order_digest = EXCLUDED.order_digest,
            updated_at = now()
        WHERE order_intents.status NOT IN ('pending', 'recorded', 'submitted', 'filled')
           OR order_intents.updated_at < now() - (%s * interval '1 second')
        RETURNING intent_id, status, value, trade_id, order_digest, created_at, updated_at
        """,
        (
            str(intent_id),
            str(payload.get("status") or "pending"),
            stored,
            payload.get("trade_id"),
            payload.get("order_digest"),
            int(stale_after_seconds),
        ),
    )


def update_order_intent_row(intent_id: str, payload: dict) -> Optional[dict]:
    return execute_returning(
        """
        UPDATE order_intents
        SET status = %s,
            value = %s::jsonb,
            trade_id = %s,
            order_digest = %s,
            updated_at = now()
        WHERE intent_id = %s
        RETURNING intent_id, status, value, trade_id, order_digest, created_at, updated_at
        """,
        (
            str(payload.get("status") or "pending"),
            json.dumps(payload),
            payload.get("trade_id"),
            payload.get("order_digest"),
            str(intent_id),
        ),
    )


def _vault_watch_table(network: str) -> str:
    if network not in _VALID_NETWORKS:
        raise ValueError(f"Invalid network: {network}")
    return f"vault_deposit_watch_{network}"


def _vault_lp_events_table(network: str) -> str:
    if network not in _VALID_NETWORKS:
        raise ValueError(f"Invalid network: {network}")
    return f"vault_lp_events_{network}"


def get_vault_deposit_watch(telegram_id: int, network: str = "mainnet") -> dict | None:
    table = _vault_watch_table(network)
    return query_one(f"SELECT * FROM {table} WHERE user_id = %s", (telegram_id,))


def set_vault_deposit_watch(
    telegram_id: int,
    *,
    enabled: bool,
    network: str = "mainnet",
    last_seen_mintable_usdt0: float | None = None,
) -> None:
    table = _vault_watch_table(network)
    existing = get_vault_deposit_watch(telegram_id, network)
    if existing:
        if last_seen_mintable_usdt0 is not None:
            execute(
                f"""UPDATE {table}
                    SET enabled = %s, last_seen_mintable_usdt0 = %s, updated_at = now()
                    WHERE user_id = %s""",
                (enabled, last_seen_mintable_usdt0, telegram_id),
            )
        else:
            execute(
                f"UPDATE {table} SET enabled = %s, updated_at = now() WHERE user_id = %s",
                (enabled, telegram_id),
            )
        return
    execute(
        f"""INSERT INTO {table} (user_id, enabled, last_seen_mintable_usdt0)
            VALUES (%s, %s, %s)""",
        (telegram_id, enabled, float(last_seen_mintable_usdt0 or 0.0)),
    )


def update_vault_watch_last_mintable(
    telegram_id: int,
    mintable_usdt0: float,
    network: str = "mainnet",
) -> None:
    table = _vault_watch_table(network)
    execute(
        f"""UPDATE {table}
            SET last_seen_mintable_usdt0 = %s, updated_at = now()
            WHERE user_id = %s""",
        (mintable_usdt0, telegram_id),
    )


def get_enabled_vault_deposit_watches(network: str = "mainnet") -> list:
    table = _vault_watch_table(network)
    return query_all(
        f"SELECT * FROM {table} WHERE enabled = true ORDER BY user_id",
    )


def insert_vault_lp_event(
    telegram_id: int,
    *,
    event_type: str,
    quote_usdt0: float | None = None,
    nlp_amount: float | None = None,
    submission_idx: str | None = None,
    tx_digest: str | None = None,
    event_ts=None,
    network: str = "mainnet",
) -> None:
    table = _vault_lp_events_table(network)
    # Archive rows (submission_idx NOT NULL) UPSERT so a corrected re-sync can
    # self-heal a previously mis-sized nlp_amount (e.g. rows written while the NLP
    # product id resolved to the wrong spot). Bot-audit rows (submission_idx NULL)
    # never match the partial unique index, so they always insert as before.
    execute(
        f"""INSERT INTO {table}
            (user_id, event_type, quote_usdt0, nlp_amount, submission_idx, tx_digest, event_ts)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, event_type, submission_idx) WHERE submission_idx IS NOT NULL
            DO UPDATE SET
                quote_usdt0 = COALESCE(EXCLUDED.quote_usdt0, {table}.quote_usdt0),
                nlp_amount  = COALESCE(EXCLUDED.nlp_amount, {table}.nlp_amount),
                tx_digest   = COALESCE(EXCLUDED.tx_digest, {table}.tx_digest),
                event_ts    = COALESCE(EXCLUDED.event_ts, {table}.event_ts)""",
        (
            telegram_id,
            event_type,
            quote_usdt0,
            nlp_amount,
            submission_idx,
            tx_digest,
            event_ts,
        ),
    )


def get_vault_lp_events(telegram_id: int, network: str = "mainnet", limit: int = 500) -> list:
    table = _vault_lp_events_table(network)
    return query_all(
        f"""SELECT event_type, quote_usdt0, nlp_amount, submission_idx, tx_digest, event_ts
            FROM {table}
            WHERE user_id = %s
            ORDER BY event_ts ASC NULLS LAST, id ASC
            LIMIT %s""",
        (telegram_id, limit),
    )


def get_session_net_base_by_product(session_id: int, network: str) -> dict[int, Decimal]:
    """Net BASE this strategy session holds per product_id, from PERSISTED fills.

    DN-ATTRIBUTION. The engine's in-memory ``engine_position_hold`` cannot answer
    "what did THIS session buy?" reliably: ``EngineRuntime.start`` clears it for the
    controller id on every build, and prod session 167 showed it disagreeing with
    the venue outright (it recorded BTC-USDT0 as 0.00155 bought / 0.00155 sold —
    net flat — while the venue had filled 0.0031 and the user was left holding ~$99
    of unhedged spot).

    ``trades_<network>`` is the durable record of what actually filled, stamped with
    ``strategy_session_id``. For session 167 it correctly shows spot buys 0.0031 vs
    sells 0.00155, i.e. net +0.00155 — so a residual sweep sourced from here can
    close a genuine orphan while still never reaching a balance the session did not
    buy. It also survives a process restart, a worker handoff and a controller
    rebuild, which the in-memory hold does not.

    Keyed by product_id on purpose: the DN controller's pair strings are
    ``BASE-USDT0`` / ``BASE-PERP`` while the fill rows carry venue product names
    (``KBTC``), so names do not join.
    """
    table = f"trades_{'testnet' if str(network).lower() == 'testnet' else 'mainnet'}"
    rows = query_all(
        f"""
        SELECT product_id,
               COALESCE(SUM(CASE WHEN LOWER(side) = 'long'
                                 THEN COALESCE(fill_size, 0) ELSE 0 END), 0) AS bought,
               COALESCE(SUM(CASE WHEN LOWER(side) = 'short'
                                 THEN COALESCE(fill_size, 0) ELSE 0 END), 0) AS sold
          FROM {table}
         WHERE strategy_session_id = %s
           AND COALESCE(fill_size, 0) > 0
         GROUP BY product_id
        """,
        (int(session_id),),
    )
    out: dict[int, Decimal] = {}
    for r in (rows or []):
        try:
            pid = int(r["product_id"])
        except (TypeError, ValueError, KeyError):
            continue
        out[pid] = Decimal(str(r["bought"] or 0)) - Decimal(str(r["sold"] or 0))
    return out
