-- Post-fill mark-out ledger: for every strategy fill, where the reference
-- price actually was 1s / 5s / 30s / 60s / 300s later.
--
-- This is the missing half of the market maker's feedback loop. Spread capture
-- is the GROSS edge; adverse selection is what you keep. Mid mode has been
-- quoting since it shipped without ever measuring whether its fills were
-- toxic, so "widen the spread" has only ever been a guess.
--
-- One row per (trade, horizon) rather than wide per-horizon columns: horizons
-- complete at different times (1s grades immediately, 300s five minutes later)
-- and the long shape makes "GROUP BY horizon" metrics trivial. Same shape and
-- reasoning as signal_outcomes (0019).
--
-- The fill lives in trades_<network>, which is not a single table, so this
-- carries the network as a column and references the trade by id rather than
-- by foreign key.

CREATE TABLE IF NOT EXISTS fill_markouts (
  id                  BIGSERIAL PRIMARY KEY,
  trade_id            BIGINT NOT NULL,
  network             TEXT NOT NULL,
  user_id             BIGINT,
  strategy            TEXT,
  product_name        TEXT,
  strategy_session_id BIGINT,
  ts_fill             TIMESTAMPTZ NOT NULL,

  -- The fill being graded, denormalized so metrics never need the join back.
  side                TEXT,                    -- 'BUY' | 'SELL'
  fill_price          DOUBLE PRECISION,
  fill_size           DOUBLE PRECISION,
  fee_bp              DOUBLE PRECISION,
  is_taker            BOOLEAN,

  horizon_nominal_s   DOUBLE PRECISION NOT NULL,
  -- ACTUAL elapsed time of the reference sample. A "5s" mark-out read at 9s is
  -- not a 5s mark-out; samples past the jitter bound are dropped entirely, and
  -- storing the real elapsed time keeps the residual drift auditable rather
  -- than assumed away.
  horizon_actual_s    DOUBLE PRECISION,

  ref_price           DOUBLE PRECISION,
  -- 'tick' = Hyperliquid pushed mid (volatile, in-process ring)
  -- 'candle_1m' = HL 1m closes (exact grid, survives restarts) -> the primary
  ref_source          TEXT,

  -- Signed so POSITIVE always means the market moved OUR way, for buys and
  -- sells alike. net_ subtracts the round-trip fee.
  markout_bp          DOUBLE PRECISION,
  net_markout_bp      DOUBLE PRECISION,

  -- Nado-vs-Hyperliquid basis at fill time. Mark-out is measured on the HL
  -- reference while the fill happened on Nado, so without this a persistent
  -- price offset would masquerade as adverse selection.
  basis_bp            DOUBLE PRECISION,

  graded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (trade_id, network, horizon_nominal_s)
);

CREATE INDEX IF NOT EXISTS idx_fill_markouts_user
  ON fill_markouts (user_id, network, ts_fill DESC);
CREATE INDEX IF NOT EXISTS idx_fill_markouts_horizon
  ON fill_markouts (horizon_nominal_s, ts_fill DESC);
CREATE INDEX IF NOT EXISTS idx_fill_markouts_session
  ON fill_markouts (strategy_session_id) WHERE strategy_session_id IS NOT NULL;
