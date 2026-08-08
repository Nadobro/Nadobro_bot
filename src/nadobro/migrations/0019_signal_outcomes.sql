-- Signal grading ledger: for every overlay_signals row, what actually happened
-- next. This is the missing half of the feedback loop — until now the bot logged
-- what it decided and never whether it was right, so feature weights could only
-- drift, never improve.
--
-- One row per (signal, horizon) rather than wide per-horizon columns: horizons
-- complete at different times (15m grades long before 4h does), and the long
-- shape makes "GROUP BY horizon" metrics trivial. Idempotent, network-scoped.

CREATE TABLE IF NOT EXISTS signal_outcomes (
  id                 BIGSERIAL PRIMARY KEY,
  signal_id          BIGINT NOT NULL REFERENCES overlay_signals (id) ON DELETE CASCADE,
  user_id            BIGINT NOT NULL,
  network            TEXT NOT NULL,
  strategy           TEXT,
  product_id         INTEGER,
  product_name       TEXT,
  ts_signal          TIMESTAMPTZ NOT NULL,

  -- Snapshot of the call being graded, denormalized so metrics never need the
  -- join back to overlay_signals.
  mid_at_signal      DOUBLE PRECISION,
  bias               DOUBLE PRECISION,
  regime             TEXT,
  confidence         DOUBLE PRECISION,

  horizon            TEXT NOT NULL,           -- '15m' | '1h' | '4h'

  -- Outcome. All returns are signed FRACTIONS of mid_at_signal, not percents.
  -- excursion_up/down are direction-neutral (up >= 0, down <= 0); MFE/MAE are
  -- derived per-signal in quant/scoring.py from the sign of bias, so the stored
  -- numbers stay unambiguous regardless of which way the call went.
  fwd_return         DOUBLE PRECISION,
  excursion_up       DOUBLE PRECISION,
  excursion_down     DOUBLE PRECISION,

  -- NULL when bias == 0: a neutral call cannot be right or wrong.
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
