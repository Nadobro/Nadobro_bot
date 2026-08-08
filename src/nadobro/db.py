import os

from src.nadobro.utils.env import env_int
import logging
import psycopg2
import psycopg2.pool
import psycopg2.extras

logger = logging.getLogger(__name__)

_pool = None
_pool_pid = None
_DB_POOL_MIN = env_int("DB_POOL_MIN", 2)
_DB_POOL_MAX = env_int("DB_POOL_MAX", 30)

# Connection-level network hardening. Production runs against Supabase's
# direct host, which is IPv6-only on Fly — when that route flaps ("No route
# to host", 2026-06-11 incident) a timeout-less connect can hang for the OS
# TCP timeout (minutes), wedging APScheduler ticks ("maximum number of
# running instances reached"). connect_timeout bounds the hang; keepalives
# detect half-dead pooled connections instead of failing the first query
# after an outage.
_DB_CONNECT_KWARGS = {
    "connect_timeout": env_int("DB_CONNECT_TIMEOUT_SECONDS", 10),
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
}

# Errors that mean "this connection (or the route to the server) is dead" —
# safe to retry a READ on a fresh connection; never auto-retried for writes
# (the statement may have reached the server before the link died).
_DISCONNECT_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)

def _prepare_db_url(url: str) -> str:
    import re
    from urllib.parse import quote
    m = re.match(r'^(postgresql|postgres)(\+\w+)?://', url)
    if not m:
        return url
    scheme_end = m.end()
    rest = url[scheme_end:]
    at_idx = rest.rfind('@')
    if at_idx < 0:
        return url
    userinfo = rest[:at_idx]
    hostpart = rest[at_idx + 1:]
    colon_idx = userinfo.find(':')
    if colon_idx < 0:
        return url
    username = userinfo[:colon_idx]
    password = userinfo[colon_idx + 1:]
    encoded_pw = quote(password, safe='')
    return f"{url[:scheme_end]}{username}:{encoded_pw}@{hostpart}"


def _resolve_host_ipv4(url: str) -> str:
    import re
    import socket
    m = re.search(r'@([^/:]+)', url)
    if not m:
        return url
    hostname = m.group(1)
    try:
        ipv4 = socket.getaddrinfo(hostname, None, socket.AF_INET)[0][4][0]
        logger.info("Resolved database hostname to IPv4")
        return url.replace(f"@{hostname}", f"@{ipv4}") + (f"&options=-csearch_path%3Dpublic" if "?" in url else f"?options=-csearch_path%3Dpublic")
    except Exception as e:
        logger.warning("IPv4 resolution failed for database hostname: %s — using hostname as-is", e)
        return url


def get_pool():
    global _pool, _pool_pid
    current_pid = os.getpid()
    if _pool is not None and _pool_pid is not None and _pool_pid != current_pid:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None
        _pool_pid = None

    if _pool is None:
        url = os.environ.get("SUPABASE_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("Neither SUPABASE_DATABASE_URL nor DATABASE_URL environment variable is set.")
        db_label = "Supabase" if os.environ.get("SUPABASE_DATABASE_URL") else "default"
        url = _prepare_db_url(url)
        # Only force the DB host to IPv4 when IPv4-only egress is explicitly
        # requested. By default we stay dual-stack: Supabase's direct host often
        # publishes only AAAA (IPv6) records, so forcing an A lookup logged
        # "No address associated with hostname" and fell back to the hostname
        # anyway. Letting psycopg2/libpq resolve dual-stack is simpler and works.
        if db_label == "Supabase":
            try:
                from src.nadobro.core.ipv4_egress import force_ipv4_enabled
            except Exception:  # pragma: no cover - keep DB init resilient
                force_ipv4_enabled = lambda: False  # noqa: E731
            if force_ipv4_enabled():
                url = _resolve_host_ipv4(url)
        _pool = psycopg2.pool.ThreadedConnectionPool(
            _DB_POOL_MIN, _DB_POOL_MAX, url, **_DB_CONNECT_KWARGS
        )
        _pool_pid = current_pid
        logger.info("PostgreSQL connection pool initialized (%s) min=%s max=%s", db_label, _DB_POOL_MIN, _DB_POOL_MAX)
    return _pool


def get_db():
    return get_pool().getconn()


def put_db(conn, *, close: bool = False):
    try:
        get_pool().putconn(conn, close=close)
    except Exception as e:
        logger.warning("Failed to return connection to pool: %s", e)
        try:
            conn.close()
        except Exception:  # policy: degrade-ok(best-effort close of an orphaned conn)
            pass


def pool_stats() -> dict[str, int]:
    return {"min": _DB_POOL_MIN, "max": _DB_POOL_MAX}


def _run_statement(sql, params, consume, *, cursor_factory=None, retry_disconnect=False):
    """Run one statement on a pooled connection with disconnect hygiene.

    - A connection that raised a disconnect-class error is CLOSED instead of
      being returned to the pool (previously every dead connection went back
      and poisoned a later caller — after an outage, up to DB_POOL_MAX
      queries each failed once on a corpse).
    - ``retry_disconnect=True`` (reads only) retries ONCE on a fresh
      connection, so a single route flap costs milliseconds, not a failed
      scheduler tick.
    - rollback on a dead connection raises too; it must never mask the
      original error (it previously replaced it).
    """
    attempts = 2 if retry_disconnect else 1
    last_exc = None
    for attempt in range(attempts):
        conn = get_db()
        broken = False
        try:
            with conn.cursor(cursor_factory=cursor_factory) as cur:
                cur.execute(sql, params)
                result = consume(cur)
            conn.commit()
            return result
        except _DISCONNECT_ERRORS as e:
            broken = True
            last_exc = e
            if attempt + 1 < attempts:
                logger.warning(
                    "DB connection failed (%s); retrying once on a fresh connection",
                    str(e).strip().splitlines()[0][:160],
                )
                continue
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception as rb_exc:
                broken = True
                logger.warning("rollback failed on errored connection: %s", rb_exc)
            raise
        finally:
            put_db(conn, close=broken)
    raise last_exc  # pragma: no cover - loop always returns or raises


def query_one(sql, params=None):
    def _consume(cur):
        row = cur.fetchone()
        return dict(row) if row else None

    return _run_statement(
        sql, params, _consume,
        cursor_factory=psycopg2.extras.RealDictCursor, retry_disconnect=True,
    )


def query_all(sql, params=None):
    def _consume(cur):
        return [dict(r) for r in cur.fetchall()]

    return _run_statement(
        sql, params, _consume,
        cursor_factory=psycopg2.extras.RealDictCursor, retry_disconnect=True,
    )


def execute(sql, params=None):
    # No disconnect retry: the statement may have reached the server before
    # the link died, and replaying a non-idempotent write could double-apply.
    _run_statement(sql, params, lambda cur: None)


def execute_returning(sql, params=None):
    def _consume(cur):
        row = cur.fetchone()
        return dict(row) if row else None

    return _run_statement(
        sql, params, _consume, cursor_factory=psycopg2.extras.RealDictCursor,
    )


def run_transaction(work):
    """Run related read/write statements in one short database transaction.

    This is intentionally not retried: callers use it for state transitions
    where replaying after a disconnect could duplicate a write. The cursor is
    dictionary-shaped like ``query_one``/``execute_returning`` so model code
    can make locking decisions without a second connection or stale snapshot.
    """
    conn = get_db()
    broken = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            result = work(cur)
        conn.commit()
        return result
    except _DISCONNECT_ERRORS:
        broken = True
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_exc:
            broken = True
            logger.warning("rollback failed on transactional operation: %s", rb_exc)
        raise
    finally:
        put_db(conn, close=broken)


def query_count(sql, params=None):
    def _consume(cur):
        row = cur.fetchone()
        return row[0] if row else 0

    return _run_statement(sql, params, _consume, retry_disconnect=True)


def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    telegram_username TEXT,
                    main_address TEXT,
                    linked_signer_address TEXT,
                    encrypted_linked_signer_pk TEXT,
                    salt TEXT,
                    language TEXT DEFAULT 'en',
                    strategy_settings JSONB DEFAULT '{}',
                    network_mode TEXT DEFAULT 'mainnet',
                    created_at TIMESTAMPTZ DEFAULT now(),
                    last_active TIMESTAMPTZ DEFAULT now(),
                    last_trade_at TIMESTAMPTZ,
                    total_trades INT DEFAULT 0,
                    total_volume_usd DOUBLE PRECISION DEFAULT 0,
                    mainnet_volume_usd DOUBLE PRECISION DEFAULT 0,
                    testnet_volume_usd DOUBLE PRECISION DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS bot_state (
                    id SERIAL PRIMARY KEY,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    product_id INT NOT NULL,
                    product_name TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    side TEXT NOT NULL,
                    size DOUBLE PRECISION NOT NULL,
                    price DOUBLE PRECISION,
                    leverage DOUBLE PRECISION DEFAULT 1.0,
                    status TEXT DEFAULT 'pending',
                    order_digest TEXT,
                    pnl DOUBLE PRECISION,
                    fees DOUBLE PRECISION DEFAULT 0,
                    network TEXT NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    filled_at TIMESTAMPTZ,
                    close_price DOUBLE PRECISION,
                    closed_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_trades_user_product ON trades (user_id, product_id);
                CREATE INDEX IF NOT EXISTS idx_trades_created ON trades (created_at);
                CREATE TABLE IF NOT EXISTS alerts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    product_id INT NOT NULL,
                    product_name TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    target_value DOUBLE PRECISION NOT NULL,
                    is_active BOOLEAN DEFAULT true,
                    triggered_at TIMESTAMPTZ,
                    network TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (user_id, is_active);
                CREATE INDEX IF NOT EXISTS idx_users_last_active ON users (last_active);
                CREATE TABLE IF NOT EXISTS admin_logs (
                    id SERIAL PRIMARY KEY,
                    admin_id BIGINT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                -- Append-only security audit trail for sensitive user actions
                -- (wallet link/unlink/revoke, order placement, rejected agent
                -- auto-tuning). No UPDATE/DELETE paths write to this table.
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_audit_logs_user_created
                    ON audit_logs (user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_logs_action_created
                    ON audit_logs (action, created_at DESC);
                CREATE TABLE IF NOT EXISTS invite_codes (
                    id BIGSERIAL PRIMARY KEY,
                    code_hash TEXT UNIQUE NOT NULL,
                    public_code TEXT,
                    code_type TEXT NOT NULL DEFAULT 'private_access',
                    code_prefix TEXT NOT NULL,
                    created_by BIGINT NOT NULL,
                    referrer_user_id BIGINT,
                    network TEXT,
                    created_for_telegram_id BIGINT,
                    note TEXT,
                    max_redemptions INT NOT NULL DEFAULT 1,
                    redemption_count INT NOT NULL DEFAULT 0,
                    active BOOLEAN NOT NULL DEFAULT true,
                    redeemed_by BIGINT,
                    redeemed_username TEXT,
                    redeemed_at TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ,
                    revoked_at TIMESTAMPTZ,
                    revoked_by BIGINT,
                    earned_volume_threshold_usd DOUBLE PRECISION,
                    sequence_number INT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now(),
                    CHECK (code_type IN ('private_access', 'referral')),
                    CHECK (max_redemptions > 0),
                    CHECK (redemption_count >= 0)
                );
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS public_code TEXT;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS code_type TEXT NOT NULL DEFAULT 'private_access';
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS referrer_user_id BIGINT;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS network TEXT;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS earned_volume_threshold_usd DOUBLE PRECISION;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS sequence_number INT;
                CREATE INDEX IF NOT EXISTS idx_invite_codes_redeemed_by ON invite_codes (redeemed_by);
                CREATE INDEX IF NOT EXISTS idx_invite_codes_created_at ON invite_codes (created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_codes_public_code ON invite_codes (public_code) WHERE public_code IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_invite_codes_referrer ON invite_codes (referrer_user_id, code_type, active);
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'invite_codes_code_type_check'
                    ) THEN
                        ALTER TABLE invite_codes
                        ADD CONSTRAINT invite_codes_code_type_check
                        CHECK (code_type IN ('private_access', 'referral'));
                    END IF;
                END $$;
            """)
            conn.commit()

        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS private_access_granted BOOLEAN DEFAULT false;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS private_access_code_id BIGINT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS private_access_granted_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS private_access_granted_by BIGINT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS mainnet_volume_usd DOUBLE PRECISION DEFAULT 0;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS testnet_volume_usd DOUBLE PRECISION DEFAULT 0;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS public_code TEXT;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS code_type TEXT NOT NULL DEFAULT 'private_access';
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS referrer_user_id BIGINT;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS network TEXT;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS earned_volume_threshold_usd DOUBLE PRECISION;
                ALTER TABLE invite_codes ADD COLUMN IF NOT EXISTS sequence_number INT;
                CREATE INDEX IF NOT EXISTS idx_users_private_access ON users (private_access_granted);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_invite_codes_public_code ON invite_codes (public_code) WHERE public_code IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_invite_codes_referrer ON invite_codes (referrer_user_id, code_type, active);
                CREATE INDEX IF NOT EXISTS idx_invite_codes_referrer_network ON invite_codes (referrer_user_id, network, code_type, active);
            """)
            conn.commit()

        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    id BIGSERIAL PRIMARY KEY,
                    referrer_user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    referred_user_id BIGINT NOT NULL UNIQUE REFERENCES users(telegram_id) ON DELETE CASCADE,
                    invite_code_id BIGINT REFERENCES invite_codes(id),
                    referred_username TEXT,
                    referred_volume_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
                    referred_trade_count INT NOT NULL DEFAULT 0,
                    first_trade_at TIMESTAMPTZ,
                    last_trade_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now(),
                    CHECK (referrer_user_id <> referred_user_id),
                    CHECK (referred_volume_usd >= 0),
                    CHECK (referred_trade_count >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals (referrer_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_referrals_referred ON referrals (referred_user_id);

                CREATE TABLE IF NOT EXISTS referral_volume_events (
                    id BIGSERIAL PRIMARY KEY,
                    referral_id BIGINT NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
                    referrer_user_id BIGINT NOT NULL,
                    referred_user_id BIGINT NOT NULL,
                    network TEXT NOT NULL DEFAULT 'mainnet',
                    volume_usd DOUBLE PRECISION NOT NULL,
                    trade_count_delta INT NOT NULL DEFAULT 1,
                    source TEXT NOT NULL DEFAULT 'trade_stats',
                    created_at TIMESTAMPTZ DEFAULT now(),
                    CHECK (volume_usd >= 0)
                );
                ALTER TABLE referral_volume_events ADD COLUMN IF NOT EXISTS network TEXT NOT NULL DEFAULT 'mainnet';
                ALTER TABLE referral_volume_events ADD COLUMN IF NOT EXISTS trade_count_delta INT NOT NULL DEFAULT 1;
                CREATE INDEX IF NOT EXISTS idx_referral_volume_events_referrer ON referral_volume_events (referrer_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_referral_volume_events_referred ON referral_volume_events (referred_user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_referral_volume_events_referrer_network ON referral_volume_events (referrer_user_id, network, created_at DESC);
            """)
            conn.commit()

        with conn.cursor() as cur:
            for col, col_type in [("close_price", "DOUBLE PRECISION"), ("closed_at", "TIMESTAMPTZ")]:
                try:
                    cur.execute(f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {col_type}")  # Safe: col and col_type are hardcoded constants above
                    conn.commit()
                    logger.info(f"Added column trades.{col}")
                except Exception:
                    conn.rollback()

        _NETWORK_TRADES_DDL = """
            CREATE TABLE IF NOT EXISTS trades_{net} (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                product_id INT NOT NULL,
                product_name TEXT NOT NULL,
                order_type TEXT NOT NULL,
                side TEXT NOT NULL,
                size DOUBLE PRECISION NOT NULL,
                price DOUBLE PRECISION,
                leverage DOUBLE PRECISION DEFAULT 1.0,
                status TEXT DEFAULT 'pending',
                order_digest TEXT,
                pnl DOUBLE PRECISION,
                fees DOUBLE PRECISION DEFAULT 0,
                error_message TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                filled_at TIMESTAMPTZ,
                close_price DOUBLE PRECISION,
                closed_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_trades_{net}_user_product ON trades_{net} (user_id, product_id);
            CREATE INDEX IF NOT EXISTS idx_trades_{net}_created ON trades_{net} (created_at);
        """
        _NETWORK_ALERTS_DDL = """
            CREATE TABLE IF NOT EXISTS alerts_{net} (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                product_id INT NOT NULL,
                product_name TEXT NOT NULL,
                condition TEXT NOT NULL,
                target_value DOUBLE PRECISION NOT NULL,
                is_active BOOLEAN DEFAULT true,
                triggered_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_{net}_active ON alerts_{net} (user_id, is_active);
        """
        _NETWORK_VAULT_WATCH_DDL = """
            CREATE TABLE IF NOT EXISTS vault_deposit_watch_{net} (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL UNIQUE,
                enabled BOOLEAN NOT NULL DEFAULT false,
                last_seen_mintable_usdt0 DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_vault_deposit_watch_{net}_enabled
                ON vault_deposit_watch_{net} (enabled) WHERE enabled = true;
        """
        _NETWORK_VAULT_LP_EVENTS_DDL = """
            CREATE TABLE IF NOT EXISTS vault_lp_events_{net} (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                event_type TEXT NOT NULL,
                quote_usdt0 DOUBLE PRECISION,
                nlp_amount DOUBLE PRECISION,
                submission_idx TEXT,
                tx_digest TEXT,
                event_ts TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                CHECK (event_type IN ('mint', 'burn'))
            );
            CREATE INDEX IF NOT EXISTS idx_vault_lp_events_{net}_user
                ON vault_lp_events_{net} (user_id, event_ts DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_lp_events_{net}_dedupe
                ON vault_lp_events_{net} (user_id, event_type, submission_idx)
                WHERE submission_idx IS NOT NULL;
        """
        _NETWORK_DESK_PLANS_DDL = """
            CREATE TABLE IF NOT EXISTS desk_plans_{net} (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                plan_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                plan_json TEXT NOT NULL,
                state_json TEXT,
                error TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                confirmed_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                CHECK (status IN ('draft','awaiting_trigger','running',
                                  'completed','cancelled','failed'))
            );
            CREATE INDEX IF NOT EXISTS idx_desk_plans_{net}_user_status
                ON desk_plans_{net} (user_id, status);
            CREATE INDEX IF NOT EXISTS idx_desk_plans_{net}_active
                ON desk_plans_{net} (status)
                WHERE status IN ('awaiting_trigger', 'running');
        """
        with conn.cursor() as cur:
            for net in ("testnet", "mainnet"):
                cur.execute(_NETWORK_TRADES_DDL.format(net=net))
                cur.execute(_NETWORK_ALERTS_DDL.format(net=net))
                cur.execute(_NETWORK_VAULT_WATCH_DDL.format(net=net))
                cur.execute(_NETWORK_VAULT_LP_EVENTS_DDL.format(net=net))
                cur.execute(_NETWORK_DESK_PLANS_DDL.format(net=net))
            conn.commit()
            logger.info("Network-specific tables (trades_testnet/mainnet, alerts_testnet/mainnet) verified/created")

        with conn.cursor() as cur:
            for net in ("testnet", "mainnet"):
                cur.execute(f"SELECT COUNT(*) FROM trades_{net}")
                trades_count = cur.fetchone()[0]
                cur.execute(f"SELECT COUNT(*) FROM alerts_{net}")
                alerts_count = cur.fetchone()[0]
                migrated_trades = 0
                migrated_alerts = 0
                if trades_count == 0:
                    cur.execute(f"""
                        INSERT INTO trades_{net}
                            (user_id, product_id, product_name, order_type, side, size, price,
                             leverage, status, order_digest, pnl, fees, error_message,
                             created_at, filled_at, close_price, closed_at)
                        SELECT user_id, product_id, product_name, order_type, side, size, price,
                               leverage, status, order_digest, pnl, fees, error_message,
                               created_at, filled_at, close_price, closed_at
                        FROM trades WHERE network = %s
                    """, (net,))
                    migrated_trades = cur.rowcount
                if alerts_count == 0:
                    cur.execute(f"""
                        INSERT INTO alerts_{net}
                            (user_id, product_id, product_name, condition, target_value,
                             is_active, triggered_at, created_at)
                        SELECT user_id, product_id, product_name, condition, target_value,
                               is_active, triggered_at, created_at
                        FROM alerts WHERE network = %s
                    """, (net,))
                    migrated_alerts = cur.rowcount
                if migrated_trades or migrated_alerts:
                    logger.info("Migrated %d trades, %d alerts to %s tables", migrated_trades, migrated_alerts, net)
            conn.commit()

        _COPY_TRADING_DDL = """
            CREATE TABLE IF NOT EXISTS copy_traders (
                id SERIAL PRIMARY KEY,
                wallet_address TEXT UNIQUE NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                is_curated BOOLEAN DEFAULT false,
                active BOOLEAN DEFAULT true,
                total_pnl_usd NUMERIC(20, 8) DEFAULT 0,
                total_volume_usd NUMERIC(20, 8) DEFAULT 0,
                nado_points NUMERIC(20, 8) DEFAULT 0,
                win_rate NUMERIC(8, 4),
                leader_roi NUMERIC(20, 8),
                leader_active_days INTEGER,
                leader_period_days INTEGER,
                leader_last_activity_at TIMESTAMPTZ,
                leader_closed_trades INTEGER,
                leader_max_drawdown_pct NUMERIC(20, 8),
                last_updated_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_copy_traders_active ON copy_traders (active);

            -- migrations/0011_copy_trader_owner.sql: privacy scoping for custom
            -- copy-trade wallets. owner_user_id IS NULL = public/curated entry
            -- (admin-managed); otherwise the row is private to that telegram
            -- user. Old single-column UNIQUE constraint on wallet_address
            -- (which leaks visibility between users) is replaced by two
            -- partial unique indexes.
            ALTER TABLE copy_traders
                ADD COLUMN IF NOT EXISTS owner_user_id BIGINT;
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'copy_traders_wallet_address_key'
                ) THEN
                    ALTER TABLE copy_traders
                        DROP CONSTRAINT copy_traders_wallet_address_key;
                END IF;
            END $$;
            CREATE UNIQUE INDEX IF NOT EXISTS copy_traders_curated_wallet_uq
                ON copy_traders (wallet_address)
                WHERE owner_user_id IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS copy_traders_owner_wallet_uq
                ON copy_traders (owner_user_id, wallet_address)
                WHERE owner_user_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_copy_traders_owner
                ON copy_traders (owner_user_id)
                WHERE owner_user_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS copy_mirrors (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                trader_id INT NOT NULL REFERENCES copy_traders(id),
                network TEXT NOT NULL DEFAULT 'mainnet',
                margin_per_trade DOUBLE PRECISION NOT NULL DEFAULT 50.0,
                max_leverage DOUBLE PRECISION NOT NULL DEFAULT 10.0,
                cumulative_stop_loss_pct DOUBLE PRECISION NOT NULL DEFAULT 50.0,
                cumulative_take_profit_pct DOUBLE PRECISION NOT NULL DEFAULT 100.0,
                total_allocated_usd DOUBLE PRECISION NOT NULL DEFAULT 500.0,
                cumulative_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                active BOOLEAN DEFAULT true,
                paused BOOLEAN DEFAULT false,
                stop_requested BOOLEAN NOT NULL DEFAULT false,
                auto_stopped_reason TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                stopped_at TIMESTAMPTZ,
                UNIQUE(user_id, trader_id, network)
            );
            CREATE INDEX IF NOT EXISTS idx_copy_mirrors_user ON copy_mirrors (user_id, active);
            CREATE INDEX IF NOT EXISTS idx_copy_mirrors_trader ON copy_mirrors (trader_id, active);

            -- migrations/0011_copy_trader_owner.sql backfill: preserve legacy
            -- personal wallets with one clear owner and deactivate ambiguous
            -- ownerless non-curated rows so they cannot leak as public traders.
            UPDATE copy_traders ct
            SET owner_user_id = owners.user_id
            FROM (
                SELECT trader_id, MIN(user_id)::BIGINT AS user_id
                FROM copy_mirrors
                GROUP BY trader_id
                HAVING COUNT(DISTINCT user_id) = 1
            ) owners
            WHERE ct.id = owners.trader_id
              AND ct.owner_user_id IS NULL
              AND COALESCE(ct.is_curated, false) = false;
            UPDATE copy_traders
            SET active = false
            WHERE owner_user_id IS NULL
              AND COALESCE(is_curated, false) = false
              AND active = true;

            CREATE TABLE IF NOT EXISTS copy_positions (
                id SERIAL PRIMARY KEY,
                mirror_id INT NOT NULL REFERENCES copy_mirrors(id),
                user_id BIGINT NOT NULL,
                product_id INT NOT NULL,
                product_name TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price DOUBLE PRECISION,
                size DOUBLE PRECISION,
                leverage DOUBLE PRECISION DEFAULT 1.0,
                tp_price DOUBLE PRECISION,
                sl_price DOUBLE PRECISION,
                tp_order_digest TEXT,
                sl_order_digest TEXT,
                leader_entry_price DOUBLE PRECISION,
                leader_size DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'open',
                pnl DOUBLE PRECISION DEFAULT 0.0,
                opened_at TIMESTAMPTZ DEFAULT now(),
                closed_at TIMESTAMPTZ,
                close_reason TEXT
            );
            ALTER TABLE copy_positions ADD COLUMN IF NOT EXISTS tp_order_digest TEXT;
            ALTER TABLE copy_positions ADD COLUMN IF NOT EXISTS sl_order_digest TEXT;
            CREATE INDEX IF NOT EXISTS idx_copy_positions_mirror ON copy_positions (mirror_id, status);
            CREATE INDEX IF NOT EXISTS idx_copy_positions_user ON copy_positions (user_id, status);
            CREATE INDEX IF NOT EXISTS idx_copy_positions_tp_digest
                ON copy_positions (tp_order_digest) WHERE tp_order_digest IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_copy_positions_sl_digest
                ON copy_positions (sl_order_digest) WHERE sl_order_digest IS NOT NULL;

            CREATE TABLE IF NOT EXISTS copy_snapshots (
                id SERIAL PRIMARY KEY,
                trader_id INT NOT NULL REFERENCES copy_traders(id),
                network TEXT NOT NULL DEFAULT 'mainnet',
                positions_json JSONB NOT NULL DEFAULT '[]',
                captured_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_copy_snapshots_trader ON copy_snapshots (trader_id, network);

            CREATE TABLE IF NOT EXISTS copy_trades (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                mirror_id INT NOT NULL REFERENCES copy_mirrors(id),
                hl_fill_tid BIGINT,
                hl_coin TEXT NOT NULL DEFAULT '',
                nado_product_id INT NOT NULL DEFAULT 0,
                side TEXT NOT NULL,
                hl_size DOUBLE PRECISION NOT NULL DEFAULT 0,
                hl_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                nado_size DOUBLE PRECISION,
                nado_price DOUBLE PRECISION,
                nado_trade_id INT,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                filled_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS idx_copy_trades_mirror ON copy_trades (mirror_id);
            CREATE INDEX IF NOT EXISTS idx_copy_trades_user ON copy_trades (user_id, created_at);
        """
        with conn.cursor() as cur:
            cur.execute(_COPY_TRADING_DDL)
            conn.commit()
            logger.info("Copy trading tables verified/created")

        # --- New columns on trades_testnet / trades_mainnet ---
        _NEW_TRADE_COLS = [
            ("fill_price", "DOUBLE PRECISION"),
            ("fill_size", "DOUBLE PRECISION"),
            ("fill_fee", "DOUBLE PRECISION DEFAULT 0"),
            ("builder_fee", "DOUBLE PRECISION DEFAULT 0"),
            ("slippage_bps", "DOUBLE PRECISION"),
            ("source", "TEXT DEFAULT 'manual'"),
            ("strategy_session_id", "INT"),
            ("open_trade_id", "INT"),
            ("realized_pnl", "DOUBLE PRECISION"),
            ("is_taker", "BOOLEAN"),
            ("funding_paid", "DOUBLE PRECISION DEFAULT 0"),
            # Portfolio rebuild columns (migrations/0004_portfolio_rebuild.sql).
            # Added here too so the column ships with the bot — production was
            # hitting `column "submission_idx" does not exist` because the SQL
            # migration was never applied out-of-band.
            ("submission_idx", "NUMERIC(78,0)"),
            ("isolated", "BOOLEAN"),
            ("realized_pnl_x18", "NUMERIC(78,0)"),
            ("fee_x18", "NUMERIC(78,0)"),
            ("base_filled_x18", "NUMERIC(78,0)"),
            ("quote_filled_x18", "NUMERIC(78,0)"),
            ("filled_at", "TIMESTAMPTZ"),
            # Analytics attribution: TRUE when the order behind this fill
            # was placed BY Nadobro (an order_intents digest match). NULL /
            # FALSE = the user traded on the Nado UI. `source` cannot carry
            # this: unlinked venue fills default to source='manual', which
            # conflates bot manual trades with external ones.
            ("via_nadobro", "BOOLEAN"),
        ]
        with conn.cursor() as cur:
            for net in ("testnet", "mainnet"):
                table = f"trades_{net}"
                for col, col_type in _NEW_TRADE_COLS:
                    try:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
                        conn.commit()
                        logger.info("Added column %s.%s", table, col)
                    except Exception:
                        conn.rollback()
                # Unique index used by nado_sync._write_matches to dedupe fills.
                try:
                    cur.execute(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS trades_{net}_submission_idx "
                        f"ON trades_{net} (submission_idx) WHERE submission_idx IS NOT NULL"
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
            # Also add to the legacy trades table for backwards compat
            for col, col_type in _NEW_TRADE_COLS:
                try:
                    cur.execute(f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {col_type}")
                    conn.commit()
                except Exception:
                    conn.rollback()
            try:
                cur.execute("""
                    UPDATE users u
                    SET mainnet_volume_usd = COALESCE(v.total, 0)
                    FROM (
                        SELECT user_id, COALESCE(SUM(ABS(size) * COALESCE(NULLIF(price, 0), fill_price, 0)), 0) AS total
                        FROM trades_mainnet
                        WHERE status IN ('filled', 'closed')
                        GROUP BY user_id
                    ) v
                    WHERE u.telegram_id = v.user_id;
                """)
                cur.execute("""
                    UPDATE users u
                    SET testnet_volume_usd = COALESCE(v.total, 0)
                    FROM (
                        SELECT user_id, COALESCE(SUM(ABS(size) * COALESCE(NULLIF(price, 0), fill_price, 0)), 0) AS total
                        FROM trades_testnet
                        WHERE status IN ('filled', 'closed')
                        GROUP BY user_id
                    ) v
                    WHERE u.telegram_id = v.user_id;
                """)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.warning("Failed to backfill per-network user volume counters", exc_info=True)

        # migrations/0015_backfill_fill_price_from_x18.sql: repair fills whose
        # human columns were never backfilled (submit-time resolve missed, retry
        # queue expired, match sync stamped x18 only). Derive fill_size /
        # fill_price / fill_fee from the stored venue quote. Idempotent — the
        # CASE guards only fill missing values.
        _repaired_sessions: dict[str, set] = {"testnet": set(), "mainnet": set()}
        with conn.cursor() as cur:
            try:
                for net in ("testnet", "mainnet"):
                    cur.execute(f"""
                        UPDATE trades_{net} SET
                          fill_size = CASE WHEN COALESCE(fill_size, 0) = 0
                              THEN ABS(base_filled_x18::numeric) / 1e18 ELSE fill_size END,
                          fill_price = CASE WHEN COALESCE(fill_price, 0) = 0
                              THEN ABS(quote_filled_x18::numeric / NULLIF(base_filled_x18, 0)::numeric) ELSE fill_price END,
                          price = CASE WHEN COALESCE(price, 0) = 0
                              THEN ABS(quote_filled_x18::numeric / NULLIF(base_filled_x18, 0)::numeric) ELSE price END,
                          fill_fee = CASE WHEN COALESCE(fill_fee, 0) = 0
                              THEN COALESCE(ABS(fee_x18::numeric), 0) / 1e18 ELSE fill_fee END
                        WHERE submission_idx IS NOT NULL
                          AND COALESCE(base_filled_x18, 0) <> 0
                          AND COALESCE(quote_filled_x18, 0) <> 0
                          AND (COALESCE(fill_price, 0) = 0 OR COALESCE(fill_size, 0) = 0 OR COALESCE(price, 0) = 0)
                        RETURNING strategy_session_id
                    """)
                    _repaired_sessions[net] = {
                        int(r[0]) for r in (cur.fetchall() or []) if r and r[0]
                    }
                conn.commit()
                logger.info(
                    "fill price x18 backfill verified (sessions touched: testnet=%d mainnet=%d)",
                    len(_repaired_sessions["testnet"]), len(_repaired_sessions["mainnet"]),
                )
            except Exception:
                conn.rollback()
                _repaired_sessions = {"testnet": set(), "mainnet": set()}
                logger.warning("fill price x18 backfill failed", exc_info=True)

        # Re-roll the stored session totals for any session whose fills were
        # just repaired — Performance reads strategy_sessions.total_volume_usd
        # etc., which were computed while the fills still read $0.
        for _net, _sids in _repaired_sessions.items():
            for _sid in _sids:
                try:
                    from src.nadobro.models.database import (
                        rollup_engine_session_pnl_funding,
                        rollup_session_from_trades,
                    )
                    from src.nadobro.strategy.engine_runtime import ENGINE_MAPPED_STRATEGIES

                    rollup_session_from_trades(_sid, _net)
                    _srow = query_one(
                        "SELECT strategy FROM strategy_sessions WHERE id = %s", (_sid,)
                    )
                    if str((_srow or {}).get("strategy") or "").lower() in ENGINE_MAPPED_STRATEGIES:
                        rollup_engine_session_pnl_funding(_sid, _net)
                except Exception:  # noqa: BLE001 - repair rollup is best-effort
                    logger.warning("post-backfill session re-rollup failed sid=%s", _sid, exc_info=True)

        # migrations/0016_retag_leaked_copy_fills.sql: re-tag copy fills that
        # leaked into History as source='manual' (the old venue-match enrich
        # gate skipped source='copy' rows and inserted a manual dup). Match by
        # order_digest shared with a source='copy' row for the same user — a
        # legit manual trade shares its digest with nothing. Idempotent.
        with conn.cursor() as cur:
            try:
                for net in ("testnet", "mainnet"):
                    cur.execute(f"""
                        UPDATE trades_{net} m SET source = 'copy'
                        WHERE m.source = 'manual'
                          AND m.order_digest IS NOT NULL
                          AND EXISTS (
                            SELECT 1 FROM trades_{net} c
                            WHERE c.source = 'copy'
                              AND c.order_digest = m.order_digest
                              AND c.user_id = m.user_id
                              AND c.id <> m.id
                          )
                    """)
                conn.commit()
                logger.info("leaked copy-fill retag verified")
            except Exception:
                conn.rollback()
                logger.warning("leaked copy-fill retag failed", exc_info=True)

        # --- strategy_sessions table ---
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategy_sessions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    strategy TEXT NOT NULL,
                    product_id INT,
                    product_name TEXT,
                    network TEXT NOT NULL,
                    started_at TIMESTAMPTZ DEFAULT now(),
                    stopped_at TIMESTAMPTZ,
                    status TEXT DEFAULT 'running',
                    total_cycles INT DEFAULT 0,
                    total_orders_placed INT DEFAULT 0,
                    total_orders_filled INT DEFAULT 0,
                    total_orders_cancelled INT DEFAULT 0,
                    realized_pnl DOUBLE PRECISION DEFAULT 0,
                    total_fees_paid DOUBLE PRECISION DEFAULT 0,
                    total_volume_usd DOUBLE PRECISION DEFAULT 0,
                    total_funding_paid DOUBLE PRECISION DEFAULT 0,
                    config_snapshot JSONB,
                    stop_reason TEXT,
                    error_message TEXT
                );
                -- migrations/0010_portfolio_workflow.sql: persist win/loss
                -- counters for per-session performance cards.
                ALTER TABLE strategy_sessions
                    ADD COLUMN IF NOT EXISTS win_count INT NOT NULL DEFAULT 0;
                ALTER TABLE strategy_sessions
                    ADD COLUMN IF NOT EXISTS loss_count INT NOT NULL DEFAULT 0;
                CREATE INDEX IF NOT EXISTS idx_strategy_sessions_user
                    ON strategy_sessions (user_id, network, strategy);
                CREATE INDEX IF NOT EXISTS idx_strategy_sessions_status
                    ON strategy_sessions (status);
            """)
            conn.commit()
            logger.info("strategy_sessions table verified/created")

        # --- overlay_signals (migrations/0017_overlay_signals.sql) ---
        # Financial-overlay signal + action log; powers Night HOWL analysis.
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS overlay_signals (
                    id                 BIGSERIAL PRIMARY KEY,
                    user_id            BIGINT NOT NULL,
                    network            TEXT NOT NULL,
                    strategy           TEXT NOT NULL,
                    product_id         INTEGER,
                    product_name       TEXT,
                    strategy_session_id INTEGER,
                    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
                    bias               DOUBLE PRECISION,
                    regime             TEXT,
                    confidence         DOUBLE PRECISION,
                    entry_ok           BOOLEAN,
                    scale              DOUBLE PRECISION,
                    spread_mult        DOUBLE PRECISION,
                    sl_pct             DOUBLE PRECISION,
                    tp_pct             DOUBLE PRECISION,
                    action_json        JSONB,
                    reasons_json       JSONB,
                    risks_json         JSONB
                );
                CREATE INDEX IF NOT EXISTS idx_overlay_signals_user
                    ON overlay_signals (user_id, network, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_overlay_signals_session
                    ON overlay_signals (strategy_session_id) WHERE strategy_session_id IS NOT NULL;
            """)
            conn.commit()
            logger.info("overlay_signals table verified/created")

        # --- signal_outcomes (migrations/0019_signal_outcomes.sql) ---
        # Grading ledger: what actually happened after each overlay signal.
        # One row per (signal, horizon) — horizons complete at different times.
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    id                 BIGSERIAL PRIMARY KEY,
                    signal_id          BIGINT NOT NULL REFERENCES overlay_signals (id) ON DELETE CASCADE,
                    user_id            BIGINT NOT NULL,
                    network            TEXT NOT NULL,
                    strategy           TEXT,
                    product_id         INTEGER,
                    product_name       TEXT,
                    ts_signal          TIMESTAMPTZ NOT NULL,
                    mid_at_signal      DOUBLE PRECISION,
                    bias               DOUBLE PRECISION,
                    regime             TEXT,
                    confidence         DOUBLE PRECISION,
                    horizon            TEXT NOT NULL,
                    fwd_return         DOUBLE PRECISION,
                    excursion_up       DOUBLE PRECISION,
                    excursion_down     DOUBLE PRECISION,
                    directional_hit    BOOLEAN,
                    bars_used          INTEGER,
                    graded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (signal_id, horizon)
                );
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_user
                    ON signal_outcomes (user_id, network, ts_signal DESC);
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_horizon
                    ON signal_outcomes (horizon, ts_signal DESC);
                CREATE INDEX IF NOT EXISTS idx_signal_outcomes_regime
                    ON signal_outcomes (regime, horizon) WHERE regime IS NOT NULL;
            """)
            conn.commit()
            logger.info("signal_outcomes table verified/created")

        # --- Engine v2 tables (migrations/0007_engine_v2_tables.sql) ---
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS engine_executors (
                    id               UUID PRIMARY KEY,
                    user_id          BIGINT NOT NULL,
                    controller_id    TEXT NOT NULL,
                    strategy_type    TEXT NOT NULL,
                    trading_pair     TEXT NOT NULL,
                    side             TEXT NOT NULL,
                    config_json      JSONB NOT NULL,
                    state            TEXT NOT NULL,
                    close_type       TEXT,
                    net_pnl_quote    NUMERIC(38,18) DEFAULT 0,
                    fees_paid_quote  NUMERIC(38,18) DEFAULT 0,
                    volume_quote     NUMERIC(38,18) DEFAULT 0,
                    duration_seconds INTEGER,
                    keep_position    BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    terminated_at    TIMESTAMPTZ
                );
                -- Unique per-RUN tag: controller_id is stable across runs, so
                -- order/executor counts must be scoped by the run's session id
                -- or one run's stats bleed into the next.
                ALTER TABLE engine_executors ADD COLUMN IF NOT EXISTS strategy_session_id INT;
                CREATE INDEX IF NOT EXISTS ix_engine_executors_user_ctrl ON engine_executors(user_id, controller_id);
                CREATE INDEX IF NOT EXISTS ix_engine_executors_session ON engine_executors(controller_id, strategy_session_id);
                CREATE INDEX IF NOT EXISTS ix_engine_executors_pair ON engine_executors(trading_pair);
                CREATE INDEX IF NOT EXISTS ix_engine_executors_state ON engine_executors(state);

                CREATE TABLE IF NOT EXISTS engine_position_hold (
                    user_id            BIGINT NOT NULL,
                    trading_pair       TEXT NOT NULL,
                    controller_id      TEXT NOT NULL,
                    buy_amount_base    NUMERIC(38,18) NOT NULL DEFAULT 0,
                    buy_amount_quote   NUMERIC(38,18) NOT NULL DEFAULT 0,
                    sell_amount_base   NUMERIC(38,18) NOT NULL DEFAULT 0,
                    sell_amount_quote  NUMERIC(38,18) NOT NULL DEFAULT 0,
                    cum_fees_quote     NUMERIC(38,18) NOT NULL DEFAULT 0,
                    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, trading_pair, controller_id)
                );

                CREATE TABLE IF NOT EXISTS engine_portfolio_history (
                    user_id           BIGINT NOT NULL,
                    network           TEXT NOT NULL DEFAULT 'mainnet',
                    ts                TIMESTAMPTZ NOT NULL,
                    total_value_quote NUMERIC(38,18) NOT NULL,
                    by_account_json   JSONB NOT NULL,
                    by_asset_json     JSONB NOT NULL,
                    PRIMARY KEY (user_id, network, ts)
                );
                -- migrations/0014_portfolio_history_network.sql: samples are
                -- taken on the user's ACTIVE network — tag each row so a
                -- testnet<->mainnet switch never interleaves one series.
                ALTER TABLE engine_portfolio_history
                    ADD COLUMN IF NOT EXISTS network TEXT NOT NULL DEFAULT 'mainnet';
                DO $$
                DECLARE
                    pk_cols text;
                BEGIN
                    SELECT string_agg(a.attname, ',' ORDER BY k.ord) INTO pk_cols
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                    WHERE t.relname = 'engine_portfolio_history' AND c.contype = 'p';

                    IF pk_cols IS DISTINCT FROM 'user_id,network,ts' THEN
                        EXECUTE 'ALTER TABLE engine_portfolio_history DROP CONSTRAINT IF EXISTS engine_portfolio_history_pkey';
                        EXECUTE 'ALTER TABLE engine_portfolio_history ADD PRIMARY KEY (user_id, network, ts)';
                    END IF;
                END $$;

                CREATE TABLE IF NOT EXISTS engine_strategy_sessions (
                    id            UUID PRIMARY KEY,
                    user_id       BIGINT NOT NULL,
                    controller_id TEXT NOT NULL,
                    session_n     INTEGER NOT NULL,
                    started_at    TIMESTAMPTZ NOT NULL,
                    ended_at      TIMESTAMPTZ,
                    summary       TEXT,
                    journal_path  TEXT NOT NULL,
                    UNIQUE (user_id, controller_id, session_n)
                );

                CREATE TABLE IF NOT EXISTS engine_kill_switch (
                    scope      TEXT PRIMARY KEY,
                    engaged    BOOLEAN NOT NULL DEFAULT FALSE,
                    reason     TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                -- Live per-controller progress, written each tick by the worker
                -- process so the main process (/status) can surface it
                -- cross-process. Keyed by the deterministic controller id.
                CREATE TABLE IF NOT EXISTS engine_controller_state (
                    controller_id      TEXT PRIMARY KEY,
                    user_id            BIGINT NOT NULL,
                    strategy           TEXT,
                    network            TEXT,
                    cycles_completed   INTEGER NOT NULL DEFAULT 0,
                    funding_earned_usd NUMERIC(38,18) NOT NULL DEFAULT 0,
                    phase              TEXT,
                    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            conn.commit()
            logger.info("engine v2 tables verified/created")

        # --- fill_sync_queue table ---
        # --- Product database design tables: strategy configs, positions, orders, points, and analytics ---
        with conn.cursor() as cur:
            cur.execute("""
                -- NOTE: the legacy `strategies` and
                -- `strategy_performance_snapshots` tables were dropped in
                -- migration 0008 (Engine v2 Phase 2). They had no readers and
                -- are replaced by engine_executors / engine_position_hold.
                CREATE TABLE IF NOT EXISTS positions (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    strategy_id BIGINT,
                    network TEXT NOT NULL CHECK (network IN ('testnet', 'mainnet')),
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('long', 'short')),
                    size NUMERIC(30, 12) NOT NULL,
                    entry_price NUMERIC(30, 12),
                    mark_price NUMERIC(30, 12),
                    leverage NUMERIC(10, 4) NOT NULL DEFAULT 1,
                    unrealized_pnl_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
                    realized_pnl_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
                    opened_at TIMESTAMPTZ DEFAULT now(),
                    closed_at TIMESTAMPTZ,
                    synced_at TIMESTAMPTZ,
                    metadata JSONB NOT NULL DEFAULT '{}'
                );
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS isolated BOOLEAN NOT NULL DEFAULT false;
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS product_id INTEGER;
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS amount_x18 NUMERIC(78,0);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS v_quote_balance_x18 NUMERIC(78,0);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_cum_funding_x18 NUMERIC(78,0);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS est_pnl NUMERIC(38,18);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS est_liq_price NUMERIC(38,18);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS avg_entry_price NUMERIC(38,18);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS notional_value NUMERIC(38,18);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS margin_used NUMERIC(38,18);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS strategy_session_id BIGINT;
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS close_price NUMERIC(38,18);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS close_realized_pnl NUMERIC(38,18);
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS time_limit TIMESTAMPTZ NULL;
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS time_limit_source TEXT NULL;
                ALTER TABLE positions ADD COLUMN IF NOT EXISTS time_limit_fired_at TIMESTAMPTZ NULL;
                -- migrations/0010_portfolio_workflow.sql: Nado summary often
                -- omits leverage on cross/isolated rows; accept NULL to avoid
                -- portfolio sync write failures.
                ALTER TABLE positions ALTER COLUMN leverage DROP NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS positions_unique_open
                    ON positions (user_id, network, COALESCE(product_id, 0), isolated)
                    WHERE closed_at IS NULL;
                CREATE INDEX IF NOT EXISTS positions_user_network_open
                    ON positions (user_id, network)
                    WHERE closed_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_positions_user_status ON positions (user_id, status);
                CREATE INDEX IF NOT EXISTS idx_positions_pair_status ON positions (pair, status);
                CREATE INDEX IF NOT EXISTS idx_positions_time_limit_due
                    ON positions (network, time_limit)
                    WHERE time_limit IS NOT NULL AND time_limit_fired_at IS NULL AND status = 'open';

                CREATE TABLE IF NOT EXISTS open_orders (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    strategy_id BIGINT,
                    network TEXT NOT NULL CHECK (network IN ('testnet', 'mainnet')),
                    pair TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL DEFAULT 'limit',
                    size NUMERIC(30, 12) NOT NULL,
                    price NUMERIC(30, 12),
                    leverage NUMERIC(10, 4) DEFAULT 1,
                    order_digest TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'open',
                    placed_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now(),
                    synced_at TIMESTAMPTZ,
                    metadata JSONB NOT NULL DEFAULT '{}'
                );
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS isolated BOOLEAN NOT NULL DEFAULT false;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS product_id INTEGER;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS price_x18 NUMERIC(78,0);
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS amount_x18 NUMERIC(78,0);
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS expiration BIGINT;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS nonce BIGINT;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS strategy_session_id BIGINT;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS time_limit TIMESTAMPTZ NULL;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS time_limit_source TEXT NULL;
                ALTER TABLE open_orders ADD COLUMN IF NOT EXISTS time_limit_fired_at TIMESTAMPTZ NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS open_orders_unique_digest
                    ON open_orders (user_id, network, order_digest);
                CREATE INDEX IF NOT EXISTS idx_open_orders_user_status ON open_orders (user_id, status);
                CREATE INDEX IF NOT EXISTS idx_open_orders_pair_status ON open_orders (pair, status);
                CREATE INDEX IF NOT EXISTS idx_open_orders_time_limit_due
                    ON open_orders (network, time_limit)
                    WHERE time_limit IS NOT NULL AND time_limit_fired_at IS NULL AND status IN ('open', 'pending', 'armed');

                -- Strategy Studio and conditional-order tables retired (2026-05).
                -- Drop on boot so historic deploys converge.
                DROP TABLE IF EXISTS conditional_orders CASCADE;
                DROP TABLE IF EXISTS studio_sessions CASCADE;

                CREATE TABLE IF NOT EXISTS points_snapshots (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    period TEXT NOT NULL CHECK (period IN ('daily', 'weekly', 'monthly', 'all')),
                    period_start DATE NOT NULL,
                    nado_points NUMERIC(20, 8) NOT NULL DEFAULT 0,
                    volume_usd NUMERIC(20, 8) NOT NULL DEFAULT 0,
                    cost_per_point_usd NUMERIC(20, 8),
                    maker_ratio NUMERIC(8, 4),
                    taker_ratio NUMERIC(8, 4),
                    metadata JSONB NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (user_id, period, period_start)
                );
                CREATE INDEX IF NOT EXISTS idx_points_snapshots_user_period
                    ON points_snapshots (user_id, period, period_start DESC);

                CREATE TABLE IF NOT EXISTS funding_payments_mainnet (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    product_id INTEGER NOT NULL,
                    amount_x18 NUMERIC(78,0) NOT NULL,
                    balance_amount_x18 NUMERIC(78,0),
                    rate_x18 NUMERIC(78,0),
                    oracle_price_x18 NUMERIC(78,0),
                    paid_at TIMESTAMPTZ NOT NULL,
                    synced_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (user_id, product_id, paid_at, amount_x18)
                );
                CREATE TABLE IF NOT EXISTS funding_payments_testnet (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    product_id INTEGER NOT NULL,
                    amount_x18 NUMERIC(78,0) NOT NULL,
                    balance_amount_x18 NUMERIC(78,0),
                    rate_x18 NUMERIC(78,0),
                    oracle_price_x18 NUMERIC(78,0),
                    paid_at TIMESTAMPTZ NOT NULL,
                    synced_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (user_id, product_id, paid_at, amount_x18)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS funding_payments_mainnet_event_unique
                    ON funding_payments_mainnet (user_id, product_id, paid_at, amount_x18);
                CREATE UNIQUE INDEX IF NOT EXISTS funding_payments_testnet_event_unique
                    ON funding_payments_testnet (user_id, product_id, paid_at, amount_x18);
                CREATE TABLE IF NOT EXISTS sync_cursors (
                    user_id BIGINT,
                    network TEXT,
                    matches_idx NUMERIC(78,0),
                    funding_idx NUMERIC(78,0),
                    ws_last_event_at TIMESTAMPTZ,
                    PRIMARY KEY (user_id, network)
                );
                CREATE TABLE IF NOT EXISTS sync_log (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    network TEXT,
                    ran_at TIMESTAMPTZ DEFAULT now(),
                    positions_seen INTEGER DEFAULT 0,
                    positions_closed INTEGER DEFAULT 0,
                    orders_seen INTEGER DEFAULT 0,
                    orders_cleared INTEGER DEFAULT 0,
                    fills_inserted INTEGER DEFAULT 0,
                    funding_inserted INTEGER DEFAULT 0,
                    duration_ms INTEGER,
                    error TEXT
                );
            """)
            conn.commit()
            logger.info("Strategy, position, order, and points analytics tables verified/created")

        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_status TEXT DEFAULT 'not_started';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS has_strategy_bot BOOLEAN DEFAULT false;

                ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS scope TEXT DEFAULT 'global';
                ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS user_id BIGINT;
                ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}';
                CREATE INDEX IF NOT EXISTS idx_bot_state_scope_key ON bot_state (scope, key);
                CREATE INDEX IF NOT EXISTS idx_bot_state_user_scope ON bot_state (user_id, scope);

                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS total_pnl_usd NUMERIC(20, 8) DEFAULT 0;
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS total_volume_usd NUMERIC(20, 8) DEFAULT 0;
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS nado_points NUMERIC(20, 8) DEFAULT 0;
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS win_rate NUMERIC(8, 4);
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS leader_roi NUMERIC(20, 8);
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS leader_active_days INTEGER;
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS leader_period_days INTEGER;
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS leader_last_activity_at TIMESTAMPTZ;
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS leader_closed_trades INTEGER;
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS leader_max_drawdown_pct NUMERIC(20, 8);
                ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS last_updated_at TIMESTAMPTZ;
                CREATE INDEX IF NOT EXISTS idx_copy_traders_leaderboard
                    ON copy_traders (active, total_pnl_usd DESC, total_volume_usd DESC);

                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS budget_cap_usd NUMERIC(20, 8);
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS risk_multiplier NUMERIC(10, 4) DEFAULT 1;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS budget_usd DOUBLE PRECISION;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS risk_factor DOUBLE PRECISION DEFAULT 1.0;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS last_synced_fill_tid BIGINT;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS stop_requested BOOLEAN NOT NULL DEFAULT false;
                -- copy_service reads this per-mirror open gate (COPY-NO-SLIPPAGE), but the
                -- column never existed, so it silently always used the 1.5 default.
                -- Materialize it so the gate is real (and per-mirror configurable).
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS max_entry_deviation_pct DOUBLE PRECISION DEFAULT 1.5;
                -- COPY-RAIL accounting spine: realized PnL is DERIVED (price pairing,
                -- gross) because the venue reports no per-fill realized PnL; fees and
                -- volume accumulate separately; the last poll's unrealized PnL is
                -- persisted so tap-path UI never calls the venue. strategy_session_id
                -- links the mirror run to a strategy_sessions row (strategy='copy')
                -- so Performance/rollup (volume/fees/funding) work like any session.
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS cumulative_fees_usd DOUBLE PRECISION DEFAULT 0;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS cumulative_volume_usd DOUBLE PRECISION DEFAULT 0;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS last_unrealized_pnl_usd DOUBLE PRECISION DEFAULT 0;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS last_rail_checked_at TIMESTAMPTZ;
                ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS strategy_session_id BIGINT;

                ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS original_trade_digest TEXT;
                ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS copied_order_digest TEXT;
                ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS pair TEXT;
                ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS pnl_usd NUMERIC(20, 8);
                ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS fees_usd NUMERIC(20, 8) DEFAULT 0;
                CREATE INDEX IF NOT EXISTS idx_copy_trades_original_digest ON copy_trades (original_trade_digest);
            """)
            conn.commit()
            logger.info("Product database design columns verified/created")

        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS fill_sync_queue (
                    id SERIAL PRIMARY KEY,
                    trade_id INT NOT NULL,
                    network TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    subaccount_hex TEXT NOT NULL,
                    order_digest TEXT NOT NULL,
                    product_id INT NOT NULL,
                    placed_at_ts DOUBLE PRECISION,
                    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'resolved', 'expired')),
                    attempts INT DEFAULT 0,
                    claimed_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    resolved_at TIMESTAMPTZ
                );
                ALTER TABLE fill_sync_queue ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
                CREATE INDEX IF NOT EXISTS idx_fill_sync_pending
                    ON fill_sync_queue (status) WHERE status = 'pending';
                CREATE INDEX IF NOT EXISTS idx_fill_sync_processing
                    ON fill_sync_queue (claimed_at) WHERE status = 'processing';
            """)
            conn.commit()
            logger.info("fill_sync_queue table verified/created")

        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'recorded', 'submitted', 'filled', 'failed', 'cancelled', 'expired')),
                    value JSONB NOT NULL DEFAULT '{}',
                    trade_id BIGINT,
                    order_digest TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_order_intents_status_updated
                    ON order_intents (status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_order_intents_trade_id
                    ON order_intents (trade_id) WHERE trade_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_order_intents_order_digest
                    ON order_intents (order_digest) WHERE order_digest IS NOT NULL;
            """)
            conn.commit()
            logger.info("order_intents table verified/created")

        # --- Additional indexes and constraints ---
        with conn.cursor() as cur:
            # Unique constraint on fill_sync_queue to prevent duplicate entries
            try:
                cur.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_fill_sync_trade_network
                        ON fill_sync_queue (trade_id, network)
                """)
                conn.commit()
            except Exception:
                conn.rollback()
            # Index on strategy_session_id for fast strategy-trade lookups
            for net in ("testnet", "mainnet"):
                try:
                    cur.execute(f"""
                        CREATE INDEX IF NOT EXISTS idx_trades_{net}_session
                            ON trades_{net} (strategy_session_id)
                            WHERE strategy_session_id IS NOT NULL
                    """)
                    conn.commit()
                except Exception:
                    conn.rollback()

        # --- Open-access migration (May 2026): the private-alpha invite gate
        # was removed. Backfill historical users to granted and flip the
        # default so any code path that still reads the column never blocks.
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "ALTER TABLE users ALTER COLUMN private_access_granted SET DEFAULT true"
                )
                cur.execute(
                    "UPDATE users SET private_access_granted = true "
                    "WHERE private_access_granted IS DISTINCT FROM true"
                )
                conn.commit()
                logger.info("Open-access backfill applied (private_access_granted=true)")
            except Exception:
                conn.rollback()
                logger.warning(
                    "Failed to backfill private_access_granted=true; the gate is "
                    "no longer enforced in code but historical rows may still "
                    "read as false.",
                    exc_info=True,
                )

        logger.info("Database tables verified/created")
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db(conn)
