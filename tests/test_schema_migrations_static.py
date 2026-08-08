from pathlib import Path


def test_concurrency_schema_migration_covers_startup_ddl_additions():
    sql = Path("src/nadobro/migrations/0005_concurrency_and_copy_constraints.sql").read_text()

    assert "ALTER TABLE fill_sync_queue" in sql
    assert "claimed_at" in sql
    assert "CREATE TABLE IF NOT EXISTS order_intents" in sql
    assert "order_intents_status_check" in sql
    assert "ALTER TABLE copy_positions" in sql
    assert "tp_order_digest" in sql
    assert "sl_order_digest" in sql


def test_migration_sequence_has_no_gap():
    migration_dir = Path("src/nadobro/migrations")
    numbers = sorted(int(path.name.split("_", 1)[0]) for path in migration_dir.glob("*.sql"))

    assert numbers == list(range(1, max(numbers) + 1))


def test_desk_plans_migration_covers_startup_ddl():
    sql = Path("src/nadobro/migrations/0012_desk_plans.sql").read_text()
    for net in ("testnet", "mainnet"):
        assert f"CREATE TABLE IF NOT EXISTS desk_plans_{net}" in sql
        assert f"idx_desk_plans_{net}_user_status" in sql
        assert f"idx_desk_plans_{net}_active" in sql
    # the guarded-transition contract relies on these statuses exactly
    for status in ("draft", "awaiting_trigger", "running",
                   "completed", "cancelled", "failed"):
        assert status in sql


def test_engine_v2_migration_covers_new_tables():
    sql = Path("src/nadobro/migrations/0007_engine_v2_tables.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS engine_executors" in sql
    assert "CREATE TABLE IF NOT EXISTS engine_position_hold" in sql
    assert "CREATE TABLE IF NOT EXISTS engine_portfolio_history" in sql
    assert "CREATE TABLE IF NOT EXISTS engine_strategy_sessions" in sql
    assert "ix_engine_executors_user_ctrl" in sql


def test_backfill_fill_price_migration_present():
    sql = Path("src/nadobro/migrations/0015_backfill_fill_price_from_x18.sql").read_text()
    assert "UPDATE trades_testnet" in sql and "UPDATE trades_mainnet" in sql
    assert "base_filled_x18" in sql and "quote_filled_x18" in sql


def test_overlay_signals_migration_and_startup_ddl():
    sql = Path("src/nadobro/migrations/0017_overlay_signals.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS overlay_signals" in sql
    assert "idx_overlay_signals_user" in sql
    ddl = Path("src/nadobro/db.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS overlay_signals" in ddl


def test_copy_quality_and_safe_stop_migration_matches_startup_ddl():
    sql = Path("src/nadobro/migrations/0018_copy_discovery_quality_and_safe_stops.sql").read_text()
    for column in (
        "leader_roi",
        "leader_active_days",
        "leader_period_days",
        "leader_last_activity_at",
        "leader_closed_trades",
        "leader_max_drawdown_pct",
        "stop_requested",
    ):
        assert column in sql
    ddl = Path("src/nadobro/db.py").read_text()
    assert "ALTER TABLE copy_traders ADD COLUMN IF NOT EXISTS leader_roi" in ddl
    assert "ALTER TABLE copy_mirrors ADD COLUMN IF NOT EXISTS stop_requested" in ddl


def test_retag_leaked_copy_fills_migration_and_startup_ddl():
    sql = Path("src/nadobro/migrations/0016_retag_leaked_copy_fills.sql").read_text()
    for net in ("testnet", "mainnet"):
        assert f"UPDATE trades_{net} m SET source = 'copy'" in sql
    assert "c.source = 'copy'" in sql and "m.source = 'manual'" in sql
    # db.py boot DDL must mirror the retag.
    ddl = Path("src/nadobro/db.py").read_text()
    assert "SET source = 'copy'" in ddl


def test_portfolio_history_network_migration_covers_startup_ddl():
    sql = Path("src/nadobro/migrations/0014_portfolio_history_network.sql").read_text()
    assert "ALTER TABLE engine_portfolio_history" in sql
    assert "ADD COLUMN IF NOT EXISTS network TEXT NOT NULL DEFAULT 'mainnet'" in sql
    assert "PRIMARY KEY (user_id, network, ts)" in sql
    # The startup DDL in db.py must mirror the migration.
    ddl = Path("src/nadobro/db.py").read_text()
    assert "ADD COLUMN IF NOT EXISTS network TEXT NOT NULL DEFAULT 'mainnet'" in ddl
    assert "PRIMARY KEY (user_id, network, ts)" in ddl


def test_startup_trade_column_migrations_are_idempotent():
    ddl = Path("src/nadobro/db.py").read_text()

    assert "ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}" in ddl
    assert "ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {col_type}" in ddl
    assert "ALTER TABLE trades ADD COLUMN {col} {col_type}" not in ddl
    assert "ALTER TABLE {table} ADD COLUMN {col} {col_type}" not in ddl


def test_signal_outcomes_migration_covers_startup_ddl_and_allowlist():
    """The grading ledger has to be declared in three places or it half-exists.

    ``insert_signal_outcome`` filters writes against a hardcoded column
    allowlist, so a column present in the migration but missing from that list
    is silently dropped on every insert — the row lands with a NULL and nothing
    errors. Pin all three together.
    """
    sql = Path("src/nadobro/migrations/0019_signal_outcomes.sql").read_text()
    ddl = Path("src/nadobro/db.py").read_text()
    accessors = Path("src/nadobro/models/database.py").read_text()

    assert "CREATE TABLE IF NOT EXISTS signal_outcomes" in sql
    assert "CREATE TABLE IF NOT EXISTS signal_outcomes" in ddl
    # Idempotent re-grading depends on this constraint existing in both.
    assert "UNIQUE (signal_id, horizon)" in sql
    assert "UNIQUE (signal_id, horizon)" in ddl
    assert "REFERENCES overlay_signals (id) ON DELETE CASCADE" in sql
    assert "REFERENCES overlay_signals (id) ON DELETE CASCADE" in ddl

    graded_columns = (
        "signal_id", "user_id", "network", "strategy", "product_id",
        "product_name", "ts_signal", "mid_at_signal", "bias", "regime",
        "confidence", "horizon", "fwd_return", "excursion_up",
        "excursion_down", "directional_hit", "bars_used",
    )
    for col in graded_columns:
        assert col in sql, f"{col} missing from migration"
        assert col in ddl, f"{col} missing from db.py startup DDL"
        assert f'"{col}"' in accessors, f"{col} missing from insert allowlist"

    for index in (
        "idx_signal_outcomes_user",
        "idx_signal_outcomes_horizon",
        "idx_signal_outcomes_regime",
    ):
        assert index in sql, index
        assert index in ddl, index


def test_signal_outcomes_ddl_agrees_across_all_THREE_places():
    """migrations/0019, db.py's startup DDL, and the insert allowlist must carry
    the same columns.

    ``insert_signal_outcome``'s own docstring says "a new column means editing the
    migration, db.py's startup DDL, AND this list" — three hand-maintained copies
    with nothing enforcing agreement. A column added to the DDL but missed in the
    allowlist is silently dropped on write (the filter ignores unknown keys), so
    the grading ledger would just be missing data with no error anywhere.
    """
    import re

    sql = Path("src/nadobro/migrations/0019_signal_outcomes.sql").read_text()
    startup = Path("src/nadobro/db.py").read_text()
    dbmod = Path("src/nadobro/models/database.py").read_text()

    # Columns declared in the migration's CREATE TABLE body.
    body = sql.split("CREATE TABLE IF NOT EXISTS signal_outcomes", 1)[1]
    # Split on a paren at the START of a line: a column comment in this migration
    # contains "down <= 0); MFE/MAE", and splitting on a bare ");" truncated the
    # body there — silently dropping half the columns from the comparison.
    body = body.split("\n);", 1)[0]
    declared = {
        m.group(1) for m in re.finditer(r"^ {2}([a-z_]+)[ \t]+[A-Z]", body, re.M)
    }
    assert "signal_id" in declared and "horizon" in declared, (
        f"parser found no columns — retune this test: {sorted(declared)}"
    )

    # The startup DDL must create the same table with the same columns.
    assert "CREATE TABLE IF NOT EXISTS signal_outcomes" in startup, (
        "db.py's init_db does not create signal_outcomes — the migration file "
        "alone does not run at boot in this codebase"
    )
    start_body = startup.split("CREATE TABLE IF NOT EXISTS signal_outcomes", 1)[1]
    start_body = start_body.split(");", 1)[0]      # no such comment in db.py's DDL
    for col in sorted(declared):
        assert col in start_body, (
            f"column {col!r} is in migrations/0019 but not in db.py's startup DDL"
        )

    # Every writable column must be in insert_signal_outcome's allowlist.
    allow = dbmod.split("def insert_signal_outcome", 1)[1].split("payload =", 1)[0]
    generated = {"id", "graded_at"}          # BIGSERIAL / DEFAULT now()
    for col in sorted(declared - generated):
        assert f'"{col}"' in allow, (
            f"column {col!r} exists in the schema but is missing from "
            f"insert_signal_outcome's allowlist — writes to it are silently dropped"
        )

    # And the reverse: nothing in the allowlist that the table cannot store.
    for col in re.findall(r'"([a-z_]+)"', allow):
        assert col in declared, (
            f"insert_signal_outcome allows {col!r}, which is not a "
            f"signal_outcomes column — the INSERT would raise at runtime"
        )
