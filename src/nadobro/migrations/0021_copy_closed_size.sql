-- Whole-trade size for closed copy positions (partial-close accuracy).
--
-- A copy position the leader trimmed before fully closing had its row `size`
-- decremented on every partial close and its `pnl` OVERWRITTEN with only the
-- final slice at close — so the Type A "COPY TRADE" share card and the History
-- tab (both reconstruct exit = entry + pnl/(size*dir)) showed last-slice-only
-- Size / Realized PnL / Exit. `closed_size` accumulates the TOTAL base closed
-- across every slice (reduce + final close), giving the card/History the whole
-- trade: Size = closed_size, and the exit reconstruction now divides the
-- accumulated pnl by the accumulated size. Nullable: legacy already-closed rows
-- keep NULL and fall back to `size` (their overwritten pnl is unrecoverable).
--
-- Paired with close_copy_position now ACCUMULATING pnl (COALESCE(pnl,0)+slice)
-- instead of overwriting, guarded by status='open' so a re-close cannot double
-- count. The mirror-level total (copy_mirrors.cumulative_pnl) is a separate
-- ledger and is unaffected.
--
-- Idempotent: db.py startup DDL carries the same addition for deployments that
-- have not run the migration separately.

ALTER TABLE copy_positions
  ADD COLUMN IF NOT EXISTS closed_size DOUBLE PRECISION;
