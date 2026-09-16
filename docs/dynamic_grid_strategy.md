# Dynamic GRID (DGRID)

Dynamic GRID is Nadobro's regime-switching grid for Nado perps. Every cycle it
classifies the market and runs the matching engine:

- **GRID phase (ranging)** — a maker ladder below the mid (`GridExecutor`): buy a
  level, sell it one step up, recycle the level. Follows price through an
  in-place re-center; scales out in tiers up to the user's take-profit.
- **RGRID phase (trending)** — the Reverse GRID trigger ladder
  (`ReverseGridController`, see `docs/reverse_grid_strategy.md`): buys above /
  sells below the mid, pyramids with the move, one trailing venue stop. Its own
  chop guard is OFF inside D-Grid (the D-Grid classifier is the gate).

Controller: `engine/controllers/dynamic_grid.py::DynamicGridController`.
Auto-switch is ON by default (`dgrid_trend_follow=1`); `0` = Grid only.

## Regime logic (`engine/routines/variance_regime.py`)

Over the user's short/long windows of 1m candles:

- `VR = M(long) / M(short)` (non-central variance ratio). `VR ≥ trend_on`
  (default 1.25) → RGRID; `VR ≤ range_on` (default 1.15) → GRID; in between
  the current phase is held (hysteresis).
- **Sustained drift**: `|drift over the long window| ≥ dgrid_trend_drift_pct`
  (default 0.30%) also declares a trend — the slow one-way grind the VR misses.
  Leaving RGRID needs the drift to fall below half that threshold (the move
  must stall, not merely ease).
- A flip is debounced by `dgrid_flip_confirm_ticks` (default 2); the financial
  overlay can only add a confirming tick, never trigger a flip.
- The engine clamps `range_on` to `trend_on` if a user inverts the band.

Phase handoffs are flat-to-flat: RGRID→GRID flattens the delegate
(`flatten_now`, cancelling its entry rungs first) before the ladder arms;
GRID→RGRID stops the ladder executor (reduce-only flatten) first.

## Foreign positions and the run baseline (2026-09-16)

A position on the product that this run did not open — a manual trade, a
leftover a previous stop could not close — is never market-closed at spawn (the
previous code did exactly that) and is never traded on top of: Nado's
reduce-only exits act on the whole account position, so a close against an
opposite-signed position could never fill and a same-signed one could be
trimmed by the run's exits. The run HOLDS, visibly, until the position is gone
(`Quoting: PAUSED (an open position on this market was not opened by this run)`
— "Close that position (or Stop and pick another market) to arm").

Once the venue reads flat the run's baseline is zero. Only exposure the run
itself created — a partial the previous phase left behind — is a residual and
is closed reduce-only before the next phase arms. No phase arms before the
baseline is known; an unreadable venue defers and the card shows why. A rebuild
of the controller mid-session (worker handoff / recovery) restores the persisted
baseline so the run's own position is still read as the run's.

Do not trade manually on a market where D-Grid is running: the venue cannot
tell the run's fills from yours, so a later manual add is treated as the run's
exposure (closed as a residual at the next phase change, flattened on Stop).

## Settings (Core / Regime / Risk tabs, all wired end-to-end)

| Setting | Key |
|---|---|
| Margin, leverage, levels (1–20 for the ladder; the trend phase caps at 12/side) | `notional_usd`, `mm_leverage_override`, `levels` |
| Starting spread, spread band | `dgrid_spread_bp`, `dgrid_min_spread_bp`, `dgrid_max_spread_bp` |
| Auto-switch / Grid only | `dgrid_trend_follow` |
| Variance thresholds | `dgrid_trend_on_variance_ratio`, `dgrid_range_on_variance_ratio` |
| Windows | `dgrid_short_window_points`, `dgrid_long_window_points` |
| Trend drift %, flip confirm ticks | `dgrid_trend_drift_pct`, `dgrid_flip_confirm_ticks` |
| PnL SL / TP (% of margin, both phases) | `rgrid_stop_loss_pct`, `rgrid_take_profit_pct` |
| Trend-phase exits (auto = 2 × step) | `rgrid_stop_pct`, `rgrid_trail_pct` |

`/status` shows the phase, variance ratio, realized move, auto-reset, the
trigger ladder while the trend phase runs (rungs armed / step / stop / trail)
and any pre-existing position the run left alone.

## Validation

`tests/engine/backtester/test_dgrid_autoswitch_net_floor.py` on the real
Aug-2026 tapes: ≥ +100bp on the ETH trend fixture, ≥ −200bp on the BTC chop
fixture, `fee_leak == 0` (measured +207bp / −107bp).
