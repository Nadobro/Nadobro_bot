# Reverse GRID (RGRID)

Reverse GRID is Nadobro's trend-following ladder for Nado perps — the mirror of
GRID. GRID rests maker buys BELOW the mid and sells ABOVE it (mean reversion);
Reverse GRID enters WITH a breakout: buys ABOVE the mid, sells BELOW it, adds as
the move extends, and exits the whole position with one trailing stop.

Those rungs cannot rest as maker limits (they would cross the book), so the
strategy runs on Nado's **price-trigger** service: the venue watches the mid and
fires each rung when its level is crossed. The ladder is placed once and
reconciled — never re-quoted every cycle.

Live controller: `engine/controllers/reverse_grid.py::ReverseGridController`
(routed for `rgrid` by `NADO_REVGRID_TRIGGER_ENABLED=1`; also D-Grid's trend
phase). Sizing + geometry: `quant/rgrid_sizing.py::trigger_ladder_plan`, shared
by the engine mapping (`engine_runtime._map_revgrid_config`) and the strategy
card (`strategy_handler.rgrid_trigger_plan`) so they can never disagree.

## What one run does

1. **Flat** — anchor at the mid, arm `levels` BUY triggers at `anchor·(1+k·step)`
   and `levels` SELL triggers at `anchor·(1−k·step)`, k = 1..levels. Whichever
   side price breaks first sets the direction. While flat, if the mid drifts more
   than 2 steps from the anchor the ladder is cancelled and re-anchored.
2. **First fill** — the opposite side is cancelled; the average entry is
   attributed from the fired rungs; a venue **reduce-only stop** is armed at the
   stop distance from the average entry. Same-side rungs stay armed (pyramid).
3. **In a position** — the favourable extreme is tracked. Once the move has gone
   the trail distance in profit, the stop moves to ~breakeven and then ratchets
   behind the extreme (never loosening). One venue order is therefore both the
   stop-loss and the take-profit and survives a disconnect.
4. **Closed** — residual triggers are cancelled, the anchor resets to the current
   mid and the ladder re-arms. With the chop guard ON the re-arm waits for a
   confirmed trend (sustained drift for `revgrid_trend_confirm_ticks`); the
   FIRST arm of a run is never gated (presence first).

## Venue facts the design is built around (Nado trigger service, 2026-09)

- **25 pending trigger orders per product per subaccount.** A flat ladder is
  `2 × levels`, so `levels` is capped at **12** (mapper, controller and UI).
- A fired trigger becomes an order with the execution type chosen at placement.
  Entry rungs are **IOC** (fill now or vanish): a fire that lagged a fast move by
  more than its 15bp price bound must never rest as an untracked maker limit.
- The ladder is **reconciled** against `list_trigger_orders`: a rung the venue
  no longer holds (unfilled IOC, rejected, expired, dropped on a signer/health
  event) is re-armed while flat or dropped in a position; a vanished stop is
  re-armed at once. An unreadable list changes nothing.
- An unreadable venue position HOLDS the controller and is shown on `/status`
  as `Quoting: PAUSED (venue position read unavailable — holding)`.
- A position on the product that the run did not open (manual, or a leftover a
  previous stop could not close) HOLDS the controller too:
  `Quoting: PAUSED (an open position on this market was not opened by this run)`.
  Reduce-only exits act on the whole account position, so the run cannot manage
  its own exposure on top of one; it arms with a zero baseline once the position
  is gone. A mid-session rebuild restores the persisted baseline so the run's own
  position is re-protected, never treated as foreign. Do not trade manually on a
  market where R-Grid runs — a later manual fill is read as the run's.

## Settings (all wired end-to-end; the card shows the effective values)

| Setting | Key | Engine use |
|---|---|---|
| Margin, leverage | `notional_usd`, `mm_leverage_override` | deployed = margin × leverage |
| Rungs per side | `levels` (1–12) | rungs on each side of the anchor |
| Spread | `rgrid_spread_bp` | rung spacing, floored at 15bp (`REVGRID_STEP_FLOOR`) |
| Stop | `rgrid_stop_pct` (% of price, 0 = auto) | protective stop distance; auto = 2 × step |
| Trail | `rgrid_trail_pct` (% of price, 0 = auto) | arm distance and giveback; auto = 2 × step |
| Chop guard | `rgrid_chop_stand_down` | gates the RE-arm after a close |
| PnL SL / TP | `rgrid_stop_loss_pct`, `rgrid_take_profit_pct` | the session rail (% of margin, net of fees); the SL also sizes the rung |

Per-rung size = `deployed / levels`, shrunk so a full pyramid reaching its own
exit — trigger distance, taker round trip and the crossing print — stays inside
the stop budget (`resolve_step_quote`), never below the venue minimum notional.

Retired: the maker-only `RGridController` keys `rgrid_discretion`,
`rgrid_reset_threshold_pct`, `rgrid_reset_timeout_seconds` are not read by the
trigger engine and are no longer offered on the card.

## Cleanup guarantees

Trigger orders live outside the resting order book. The controller's `on_stop`
cancels its ENTRY rungs by digest but leaves the protective reduce-only stop
armed (a stop runs before the session's flatten; if the flatten fails the
position keeps its stop). `strategy/venue_triggers.py` then sweeps the run's
triggers on every session-end path — user Stop (after the flatten), the SL/TP
rail, the duration cap, the stale-session stop, the cross-process stop and the
retriable leftover sweep — and at the boot stand-down. Once the position is
FLAT the stale stops go too; when the position is LEFT open (boot, a failed
flatten) the protective stop is kept and the notice says so. Ownership = digests
the `order_intents` registry vouches for, unioned with the run's own persisted
digests (`grid_trigger_digests`); a user's manual TP/SL triggers on the same
product are never touched. A budget-denied or unparseable trigger list is
"not confirmed clear", never "clear".

## Validation

Real Aug-2026 Binance-futures 1m tapes (`tests/engine/backtester/fixtures/`):
`test_revgrid_net_floor.py` pins net ≥ +400bp on the ETH trend fixture and a
bounded loss (≥ −300bp) on the BTC chop fixture, `fee_leak == 0` on both. A
reverse grid wins in trends and gives back a little in chop by design; the chop
guard and the session rail bound the chop cost.
