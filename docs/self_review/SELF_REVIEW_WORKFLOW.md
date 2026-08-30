# Nadobro Strategy Self-Review Workflow

A repeatable loop that keeps every strategy (grid, rgrid, dgrid, mid, vol, copy,
dn) correct, honors the user's SL/TP, and stops it bleeding money. Built from the
2026-06-20 audit (`docs/audit/STRATEGY_SLTP_AUDIT_2026-06-20.md`).

The principle: **the audit becomes executable.** Every finding either becomes a
guardrail test (`tests/engine/test_sltp_invariants.py`) or a checklist item below.
A fix is only "done" when its guardrail flips from xfail to pass.

---

## The loop

```
        ┌──────────────────────────────────────────────────────────┐
        │ 1. CHANGE      edit a strategy / config / SL-TP path      │
        │ 2. AUDIT       fan out read-only agents (1 per strategy   │
        │                + sltp-tracer) — file:line evidence only   │
        │ 3. GUARD       run scripts/self_review.sh (mypy + the     │
        │                invariant tests + targeted strategy tests) │
        │ 4. TRIAGE      new [VERIFIED] finding? -> add an xfail     │
        │                guardrail referencing the audit ID         │
        │ 5. FIX         make the xfail XPASS, then delete the      │
        │                marker (strict=True enforces this)         │
        │ 6. BACKTEST    once the harness exists, assert net-of-fee │
        │                PnL >= threshold per strategy              │
        └──────────────────────────────────────────────────────────┘
```

### Run the agents (step 2)
The two reusable agent definitions live in `docs/self_review/agents/`. Copy them
to `.claude/agents/` (a protected dir this tooling can't write to) once:

```bash
mkdir -p .claude/agents && cp docs/self_review/agents/*.md .claude/agents/
```

Then, before merging any strategy change, launch them in parallel — one
`strategy-auditor` per touched strategy plus one `sltp-tracer` if the change
touches SL/TP, fees, session PnL, or `map_strategy_config`. They are READ-ONLY
and must cite `file:line` for every claim; they are instructed never to invent
bugs.

### Run the guardrails (step 3)
```bash
bash scripts/self_review.sh
```

---

## Strategy correctness checklist

Each item is an invariant the bot must satisfy. Status reflects the 2026-06-20
audit. `[test]` = covered by `tests/engine/test_sltp_invariants.py`. Fix the
Critical/High items first.

Every open item has a ready-to-paste `/goal` prompt with a deterministic exit
criterion in [`GOAL_LOOPS.md`](GOAL_LOOPS.md) — run them as goal loops instead
of babysitting fix sessions. Recurring drift detection (nightly attribution
reconciliation via `scripts/reconcile_attribution.py`, PR babysitting, the
alpha-brief prototype) lives in [`SCHEDULED_LOOPS.md`](SCHEDULED_LOOPS.md).

### SL/TP (priority)
- [x] **DN-RAIL** (Critical) — *FIXED 2026-06-20:* DN now gets a post-dispatch session SL/TP rail (`bot_runtime.py`, dn block) that flattens both legs via `close_delta_neutral_legs`. Tested in `tests/services/test_session_safety_rails.py`.
- [x] **SLTP-GROSS** (High) — *FIXED 2026-06-20:* the snapshot exposes `session_pnl_net`/`session_pnl_pct_net` (gross minus fees) and the rail judges the stop on the net basis; displayed gross PnL unchanged. `live_session.py`, `bot_runtime.py`. Tested.
- [x] **GRID-DUAL-UNIT** (High) — *FIXED 2026-06-20:* re-examined with the actual rail basis (margin = **notional**, not notional/leverage), so the price barrier and the rail are the same magnitude — there was no leverage-scaled double-stop. The real defect was the fill-blind, mid-referenced `limit_price` stop firing on a wick before the grid filled. Disabled it (`engine_runtime.py`, `grid_trading.py`, `dynamic_grid.py` set `limit_price=0`); SL is now the avg-entry barrier + the fee-aware rail. `[test]` `test_grid_does_not_set_fill_blind_limit_price_stop`.
- [x] **GRID-TP-DEAD** (High) — *FIXED 2026-06-20:* the executor now enforces `take_profit` (avg-entry referenced, mirrors the stop). `grid_executor._take_profit_breached`. `[test]` `test_take_profit_breach_triggers_take_profit`.
- [ ] **DGRID-SHADOW-KEYS** (Med) — dgrid defaults don't carry dead `sl_pct`/`tp_pct` copies that shadow the live `rgrid_*` values. `strategy_registry.py:148,263`.
- [ ] **SLTP-MARGIN-BASIS** (Med) — "% of margin" is measured against true posted margin (notional/leverage), or the UI says "% of notional". `live_session.py:56`. 2026-07-19 investigation: the session rail's `_resolve_margin` uses `notional_usd` (the user's configured collateral), but the grid/MM family SIZES at `deployed = notional × eff_lev`; whenever the venue's real posted margin exceeds `notional_usd` (account leverage < the eff_lev used for sizing) the rail OVER-states return-on-margin and TP fires early by ~`eff_lev/account_leverage`. Needs live-data confirmation of the account-vs-eff_lev leverage before changing the denominator (a blind change risks flipping SL/TP timing the other way).
- [ ] **OVERLAY-TP-NO-FLOOR** (Med, 2026-07-19) — `overlay_actuator.rail_barriers` bounds SL tighten-only (`min(signal, base)`) but passes TP through with NO floor, so the chop-regime `tp_pct = base_tp × 0.8` (`signal_engine.py:248`) silently lowers the user's TP ~20% and the session rail fires early. Affects exactly OVERLAY_STRATEGIES = grid/rgrid/mid/dgrid. Proposed fix: `tp = max(signal.tp_pct, base_tp_pct)` so the user's TP is a floor (overlay may only WIDEN it — let winners run in a trend — never take profit before the user's setting). SL/TP change → guardrail + self-review.
- [ ] **GRID-EXEC-TP-UNITS** (Low, 2026-07-19) — `map_strategy_config` packs `tp = tp_pct/100` into `TripleBarrierConfig.take_profit`, and `GridExecutor._take_profit_breached` treats it as a favorable PRICE MOVE (`mid >= avg × (1 + tp)`), so a 50% TP needs a 50% price move — fires LATE/never, not early (opposite of the user complaint). Units-invariant violation but not the early-fire cause; decide whether the executor TP should be disabled for the grid family (session rail already enforces the %-of-margin TP) or re-united.
- [x] **SLTP-KEYS** — rgrid/dgrid resolve SL/TP from the keys the UI writes. *Fixed.* `[test]` green `test_user_sltp_is_resolved_...`.

### Volume bot
- [x] **VOL-MARGIN** (High) — *FIXED 2026-06-20:* the vol branch of `map_strategy_config` now prefers `session_margin_usd` (then `cycle_notional_usd`/`notional_usd`). `[test]` green `test_vol_uses_user_session_margin`.
- [x] **VOL-LOOP** (High) — *FIXED 2026-06-20:* the controller now loops buy→sell until the user's `target_volume_usd` is met (single round-trip when unset), then signals completion; `run_engine_cycle` surfaces `result["done"]` and bot_runtime finalizes the session (no more idling "running"). `volume_bot.py`, `engine_runtime.py`, `bot_runtime.py`. `[test]` `tests/engine/controllers/test_volume_bot.py`.
- [x] **VOL-DEAD-SL** (Med) — *NOT DEAD (reframed) 2026-06-20:* the vol SL is enforced by the session SL/TP rail (`effective_sl_tp_pct('vol', state)`, now fee-aware), not the controller config. `[test]` `test_vol_stop_loss_is_enforced_by_the_session_rail`.
- [x] **VOL-NO-CAP** (Med) — *FIXED 2026-06-20:* a hard `max_cycles` ceiling bounds fee burn if the target is mis-set; docstring corrected (the claimed Risk-Engine cap never existed). `[test]` `test_max_cycles_caps_runaway_loop`.

### Copy trading
- [x] **COPY-SIZE** (High) — *FIXED 2026-06-20:* mirror size scales with the leader's conviction (position notional as a fraction of the leader's largest position), capped by the user's per-trade budget — a probe is copied small, max-conviction copied full. `copy_service._compute_copy_sizing`. `[test]` `tests/services/test_copy_sizing.py`.
- [x] **COPY-LEVERAGE** (Med-High) — *FIXED 2026-06-20:* leverage mirrors the leader's, capped by the user's max + product max; falls back to `min(max, product_max)` when the venue doesn't report it. Same helper/tests.
- [x] **COPY-NO-SLIPPAGE** (Med) — *FIXED 2026-06-20:* a max-deviation gate (`_entry_deviation_too_far`, default 1.5%) skips a late entry that's drifted too far from `leader_entry` (retried next poll). `copy_service.py`. `[test]` `test_entry_deviation_gate_*`.
- [x] **COPY-VENUE-RECONCILE** (Med) — *FIXED 2026-06-20:* before opening, the follower's REAL on-venue positions are read once and a product already held untracked is skipped (no duplicate/orphan stacking). Best-effort; degrades to DB-only if the client is unavailable. `copy_service._sync_mirror_positions`.
- [ ] **COPY-DEDUP** (Low-Med) — a DB unique constraint / lock prevents double-open per (mirror, product). 
- [ ] **COPY-PAUSED-NO-RAIL** (Med, pre-existing, 2026-07-18 audit F1) — a user-paused mirror is excluded from polling entirely (`database.py get_all_active_mirrors_v2`), so open copied positions have NO SL/TP rail and no leader-close mirroring until resume. The pause reply now discloses it (copy_service.pause_copy); running the rail for paused mirrors (mirroring-off, rail-on mode) is the real fix but changes deliberate pause semantics — needs a product decision.
- [ ] **COPY-LEADER-READ-RAIL-SKIP** (Low, pre-existing, 2026-07-18 audit F2) — an exception in `_load_leader_position_map` (client construction/executor) skips `_sync_mirror_positions` — and hence the rail — for that trader group's copying mirrors for one poll (30s, self-heals). A rail-only fallback can't just pass an empty leader map (that means "leader flat" and would close everything); needs a dedicated rail-only mode. `copy_service._poll_all_mirrors`.
- [ ] **COPY-PARTIAL-CLOSE-ORPHAN** (Med, pre-existing, 2026-07-18 copy audit F-5) — a reduce-only IOC close that only partially fills (thin book inside the 1.5% band) marks the copy row fully closed, orphaning the venue remainder; a stop could then complete with residual exposure. NOT fixed here on purpose: naive fill-size detection false-positives on archive indexing lag and would deadlock the stop (worse than the rare orphan). Needs archive-lag-aware fill resolution: book actual filled size, reduce (not close) on a confirmed short fill, keep the stop armed until truly flat. `copy_service._settle_copy_close` / `_flatten_mirror_positions`.
- [ ] **COPY-POLL-HEAD-OF-LINE** (Low, 2026-07-18 copy audit F-9) — the synchronous maker fill-wait (up to `MAKER_FILL_WAIT_SECONDS`≈20s) runs inside the single poll task, so one mirror's opens delay every later mirror's rail evaluation (SL is meant to be immediate). Fix: make the maker open non-blocking — place post-only + register pending immediately, let subsequent polls resolve the fill/cancel (the pending machinery already supports "book later"). `copy_service._execute_maker_open`.
- [ ] **COPY-MAKER-RETRY-LATENCY** (Low, 2026-07-18 copy audit F-8) — a maker open retried at an unchanged touch within the 120s intent window gets a duplicate-suppressed success carrying the old cancelled digest, wasting a full fill-wait re-watching a dead order. Latency only (no wrong booking — `_close_result_ok`/fill resolution guard correctness). Resolved incidentally by non-blocking maker opens (COPY-POLL-HEAD-OF-LINE) or a per-attempt order nonce on copy opens.

### MM / grid family
- [x] **DGRID-BOOK-RACE** (High) — *FIXED 2026-06-20:* profit-booking is routed through the executor's new `reduce_position` (records the fill in shared inventory + advances per-level close accounting + cancels fully-booked close legs), with a direct reduce-only MARKET fallback only when no executor reduce-path exists. `grid_executor.reduce_position`, `dynamic_grid._maybe_book_profit`. `[test]` `test_reduce_position_books_through_executor_and_advances_accounting`.
- [x] **DGRID-RECENTER** (High) — *NOT A BUG (false positive), verified 2026-06-20:* `recenter` already sizes fresh levels as `fresh_count = max_open − len(kept)` at `total/max_open`, so total committed notional stays bounded by `total_amount_quote`. Confirmed empirically (held+resting held at the 1000 budget across repeated re-centers). No change made.
- [x] **GRID-MIN-NOTIONAL-INFLATE** (Med) — *FIXED 2026-06-20:* `run_engine_cycle` caps the grid-family level count so `total/levels >= venue min-notional` (only ever reduces levels), preventing the silent exposure inflation from venue min-notional bumps. `engine_runtime.py`.
- [ ] **DGRID-TREND-BLEED** (Med, tuning — not changed) — lowering the 0.30% drift default risks whipsaw; the exposure cap + fee-aware SL rail already bound a slow-decline bleed. Left to deliberate tuning rather than a blind default change. `variance_regime.py`.
- [x] **DGRID-NO-GATE** (Med) — *BY DESIGN (not changed), verified 2026-06-20:* dgrid's variance-ratio selector chooses GRID/RGRID for every regime incl. breakout, so it deliberately doesn't sit out (documented in `dynamic_grid.on_tick`). Changing it would break the intended flip behavior.
- [x] **MM-SPREAD-FLOOR** (Med) — *FIXED 2026-06-20:* the manual per-side spread is floored at `spread_floor_half_pct` (same as the auto path) so a sub-fee book can't be quoted. `market_making.py`. `[test]` `test_manual_spread_is_floored_at_fee_clearing_minimum`.
- [ ] **RGRID-GATE** (verify) — confirm whether the production `regime_gate_enabled=0` override for rgrid exists; if not, rgrid is gated out of its own downtrends. `reverse_grid.py:16` vs `engine_runtime.py:419`.

### Delta Neutral (economic)
- [x] **DN-CYCLES** (High) — *FIXED 2026-06-20:* DN cycle count + funding are restored from persisted progress on rebuild (`engine_runtime.py` injects `restore_cycles_completed`/`restore_funding_usd`, gated on `runs>0`; `delta_neutral.py` resumes the count and won't open a cycle past `total_cycles`). So a restart/worker-handoff no longer ignores the configured cycle count. Tested in `tests/engine/controllers/test_delta_neutral.py`.
- [x] **DN-CUSTOM-ASSETS** (High) — *FIXED 2026-06-20:* wrapped RWA spots (wQQQX/wSPYX) now pair with their perps so DN offers more than BTC/ETH (`product_catalog._dn_pair_candidates` + candidate fallback in `_build_dn_pair_catalog`). Tested in `tests/services/test_dn_pairing.py`.
- [ ] **DN-HOLD-CLOCK-ON-REBUILD** (Med, remaining) — the hold timer (`opened_at`) is still memory-only; on a rebuild mid-hold the controller re-opens a fresh cycle and restarts the clock rather than ADOPTING the open legs. A full fix needs `opened_at` persisted (schema field) + venue-position adoption on rebuild + integration testing.
- [x] **DN-PNL-FEES** (High) — *FIXED 2026-06-20:* DN headline PnL is now `realized + funding − fees` (was gross, overstating DN profit / hiding net losses). `pnl_card_builder.py`. `[test]` updated `test_delta_neutral_folds_funding_into_pnl` (+$3.30 net).
- [x] **DN-FUNDING-WINDOW** (Low) — *FIXED 2026-06-20:* funding rows with an unparseable timestamp are now excluded from the run total. `nado.py funding_since`. `[test]` `test_funding_since_excludes_undated_rows`.

### Engine / risk
- [ ] **FUNDING-SIGN** (Med, needs live-data verification — not changed) — the live-session path (`total_funding_paid`, paid-positive: `- funding_paid`) and the share card (`_net_funding_usd`, received-positive: `+ funding`) express funding differently but appear to net consistently (both add received funding). Confirming requires the live sign of the DB column vs the funding feed; flipping a sign blind would risk a real PnL-display bug, so left for data-verified change. `live_session.py` vs `pnl_card_builder.py`.
- [ ] **NO-LIQ-CHECK** (Low) — consider an engine-side liquidation-distance gate as defense-in-depth. `engine/risk.py`.
- [ ] **LOOP-STARVE-CYCLE-CAP** (Med, 2026-07-19 — F4b) — the non-vol engine strategy per-cycle reads (`get_market_price`/`get_open_orders`, `bot_runtime.py:2973-2997`) now run on the SDK pool (F4a shipped) but are still UNCAPPED, so under venue slowness a cycle can hold an SDK worker ~30s (the vol branch already wraps them in `asyncio.wait_for`). Wrapping the non-vol reads in `asyncio.wait_for` bounds cycle time and prevents SDK-pool saturation — but it can ABORT a cycle on a slow venue (skip re-quote/re-anchor), so it's engine-adjacent and must go through `scripts/self_review.sh`. Not needed to bound the tap tail (the capped click-path handlers already do that); it's a defense-in-depth follow-up.
- [ ] **LINT-VENUE-CALLS** (Med, 2026-07-19 — F5) — `tests/lint/test_no_blocking_calls_in_coroutines.py` only flags `requests/time/socket/subprocess` and bare `nadobro.db` helpers, so it MISSED the bare `client.get_balance()`/`get_user_wallet_info()` venue calls on the loop (the CLICK-PATH-BLOCKING incident). Extend it to flag venue method calls (`get_balance`/`get_all_positions`/`get_all_market_prices`/`get_subaccount_info`/`verify_linked_signer`) and the `get_user_wallet_info` service call **directly** in coroutine bodies (not in sync helpers). Test-only, but each newly-flagged site must be audited first (most current hits are inside sync render helpers that are correctly offloaded) — do it as its own PR so a missed site can't break CI here.
- [ ] **SLTP-UNITS-DUAL-CONSUMPTION** (Med→High, 2026-07-19 — product decision) — CONFIRMED: the same `effective_sl_tp_pct` value feeds BOTH the session rail as %-of-margin (`live_session.py` `session_pnl_pct_net = uPnL/notional_usd = eff_lev × price_move`, fires at `price_move = tp_pct/eff_lev`) AND the executor as a raw price-move barrier (`grid_executor.py:466-481`, fires at `price_move = tp_pct`). The session rail preempts by a factor of `eff_lev` (3× default, up to 10×) → TP fires early vs a price-move mental model / the executor. **NOT ship-blind**: the rail's %-of-margin basis is intentional and CORRECT for SL (stop at X% of posted capital) — flipping the rail to price-move basis (the naive "fix") would make the STOP-LOSS fire LATE by `eff_lev` (a 10% SL at 10× would only trip at ~100% loss, past liquidation). The real defect is the field overload; the fix is a product decision (separate, explicitly-labelled session-rail vs executor-barrier fields, or relabel the UI) and must go through self-review with a strict-xfail guardrail. Optional live measure: on one open cross-margin position compare `snap['margin_used']` (venue-posted initial margin) vs `snap['margin']` (=notional_usd) to quantify the rail-vs-Portfolio-ROI gap (note `margin_used` is 0 in the direct-client fallback — needs a robust fallback before it could ever replace the denominator).

### Backtester (money-bleed proof harness)
- [x] **BT-EMPTY** (High capability gap) — *BUILT 2026-06-20:* the `backtester/` package is implemented — `candle_ingest` (OHLC / price-path / CSV resample), cost-aware `executor_sim` (taker/maker fees + funding accrual on perps only + slippage), `engine` time loop (no look-ahead), net-of-fees `report` (equity curve + max drawdown). `run_backtest(strategy, configs, candles, costs=...)` drives the SAME controllers the live engine builds. Tests in `tests/engine/backtester/` prove the harness is honest (fees flip a winner to a loser) and that grid/rgrid/vol/dn run end-to-end — incl. the DN thesis check (net positive only when funding > fees).

  Run a quick money-bleed check::

      from src.nadobro.engine.backtester import run_backtest, resample_trades_csv, SimCosts
      candles = resample_trades_csv("f14288_*_trades_*.csv", interval_s=3600, market="WTI")
      print(run_backtest("grid", grid_cfg, candles, costs=SimCosts()).summary())

---

## Open findings — self-review audit 2026-08-12

Fan-out: `strategy-auditor` on grid / rgrid / mid / copy + `sltp-tracer`, run against
the signal-advisor conviction clamp and the copy-pause branch. All `[VERIFIED]` with
`file:line` evidence; each re-checked before landing here.

**dgrid coverage:** the first dgrid auditor died on an API session limit; the re-run
completed and its findings are folded in below. All five overlay strategies plus copy
are now covered.

### D-Grid composition — is it "grid + rgrid"?

Answered, and the answer is a naming collision rather than a defect:

* **Range phase IS genuine grid reuse.** `DynamicGridController` is a sibling of
  `GridController`, not a subclass, but it imports `build_grid_config` and spawns the
  same `GridExecutor`, so the execution core (partial-fill ingestion, close-leg
  resize, double-cancel, watermark separation, recycle) is single-implementation. Only
  `_rebuild_bounds_for_side` and the recenter throttle are duplicated.
* **Trend phase is NOT the R-Grid strategy, and must not become it.** dgrid's
  `RGRID` phase is `ReverseGridExecutor` — `GridExecutor` with `side=SELL` — with zero
  references to `rgrid.py`, `rgrid_maker_executor.py` or `quant/rgrid_sizing.py`. That
  is a mirrored mean-reversion ladder on the short side, exactly as
  `variance_regime.py:67` documents (`RGRID = "rgrid"  # short reverse grid
  (downtrend)`). It is deliberately NOT the pyramiding trend follower: R-Grid is its
  own strategy and never the D-Grid phase switcher. **Do not "wire R-Grid into
  dgrid"** — that would contradict a recorded product decision.
* Consequence: none of R-Grid's four shipped defects apply. The frozen FLAT anchor
  has no analogue (dgrid re-seeds `_grid_anchor_mid` from live mid on every spawn and
  recenter, bounded by `ladder_recenter_threshold_bp`); the VWAP spacing decay has no
  code path (levels are fixed geometric steps); the loss-only band exit has no code
  path (each level's close is on the profit side by construction). Entry
  cancel-on-touch exists but is the documented, bounded "follow price" recenter.

### Guardrailed (strict xfail in `tests/engine/test_sltp_invariants.py`)

| ID | Sev | Where |
|---|---|---|
| `ADVISOR-SIZE-SIGN` | Low | `overlay_actuator.py:98` — `size_factor = 1 + 0.25*scale*confidence`; `scale` is signed, so LOWERING confidence shallows a trim (more notional). Pre-existing, on the disagree path. |
| `GRID-TOTALQUOTE-UNCAPPED` | **Critical** | `engine_runtime.py:2180` clamps `order_amount_quote` only; classic grid ships `total_amount_quote` and hands the whole ladder to the risk gate as one order → spawn refused → session reports LIVE with 0 orders, retrying every tick. Audit measured 528.70 vs a 500.0 cap. |
| `OVERLAY-DISARMED-BARRIER-ARMS` | Medium | `bot_runtime.py:2691-2694` — `... if tp_pct > 0 else float(ov_tp)` ADOPTS a stale overlay barrier when the user deliberately disarmed it (0). Reachable via the Turbo Volume preset on mid (`tp_pct: 0.0`); reproduced closing a session at +0.96%. Fix: gate on `sltp_is_explicit`. |

### Recorded, not yet guardrailed

| ID | Sev | Where / what |
|---|---|---|
| `DGRID-REVERSAL-FLIPFLOP` | **High** | `dynamic_grid.py:411-415`, `:385` — `_maybe_reversal_flip` runs BEFORE the classifier and reads neither `last_direction` nor `last_is_trend`, so a 0.4% retrace arms a SHORT ladder inside a declared uptrend (sell entries rest above mid, fill into the rally) and the classifier reverses it ~60s later. Reproduced on a real controller at shipped defaults. Two contradictory user notifications a minute apart. Guardrailed. |
| ~~`DGRID-FEE-FLOOR`~~ **FIXED** | **Med/High** | `engine_runtime.py:1532/1585` + `grid_executor.py:155` — the per-level step is floored only when ZERO, so the shipped "Spread 2bp" preset button and the 3bp Turbo preset are net-negative against a ~3bp maker+builder round trip on EVERY completed level. `dgrid_min_spread_bp` (default 2.0, own button, rendered on the card) only reaches `spread_floor_half_pct`, which the manual-step path never reads. Guardrailed. |
| `DGRID-ORPHAN-ON-FLIP` | Medium | `grid_executor.py:785-789` + `:864` — `_cancel_all_resting` swallows cancel failures, then `_stop_out` terminates anyway; an unfilled order stays on the book with no executor to cancel it, and the opposite-side grid arms because the book reads flat. dgrid is worst-exposed since it flips sides routinely. |
| `DGRID-ABANDONED-SIDE-REQUOTES` | Medium | `grid_executor.py:857-863` + `:710-719` — a partly-failed flatten leaves the executor active, so cancelled entries become `NOT_ACTIVE` and `_maybe_place_opens` re-places them on the side the controller just abandoned. |
| `DGRID-MINNOTIONAL-CAP-LOST` | Medium | `engine_runtime.py:2463/2476-2492` vs `:583/602` — the min-notional level cap lives inside `if _should_build:` and is undone by the live-reconfig path, after which the venue bumps each sub-minimum level UP. On a $100-min product at dgrid defaults that is $400 placed against a $100 approved budget, with the risk engine outside the placement path. |
| `DGRID-ADVISOR-DEBOUNCE-LOSS` | Low | `dynamic_grid.py:469/496-498` — down-shading confidence below 0.45 removes dgrid's only overlay conservatism (the extra confirming tick when the overlay contradicts the classifier). Same non-monotonicity as `ADVISOR-SIZE-SIGN`. |
| `DGRID-TIERS-GROSS` | Low | `inventory.py:93` + `dynamic_grid.py:602-607` — profit tiers compare gross uPnL against a ladder whose top rung is the user's TP, while the rail judges the same % net of fees, so the scale-out completes before the TP is actually earned (docstring claims it "lands exactly at the user's TP"). |
| `DGRID-DEAD-AUTOSPREAD-AND-DOCS` | Low | `engine_runtime.py:1594` computes `auto_spread = spread_frac <= 0` after `spread_frac` was already floored to 0.0005 at `:1532`, so the ATR branch at `dynamic_grid.py:215-222` is unreachable and `dgrid_min/max_spread_bp` stay dead. `docs/dynamic_grid_strategy.md:26-32` is stale on three counts; `_recenter` reports success when zero levels moved (`:560-567`); the breakout "do NOT arm" branches at `:460-462`/`:514-521` are dead because every gate PAUSE reason is neutralised. |
| `RGRID-EXITBAND-INVERT` | **High** | `rgrid.py:448-451` + `engine_runtime.py:1345-1369` — `exit_band_cap` is derived from the UNSCALED band but `spread_ask_pct` is overlay-scaled up to 3x, inverting the `_exit_band > _arm_pct` ordering the module docstring calls essential. At >=1.5x the loss-only band exit always wins — the pathology that measured -85.27. Arms itself precisely in high vol. |
| `MID-AUTOSPREAD-DEAD` | **High** | `market_making.py:313` gates on `gate_atr_pct > 0`, only ever set inside the regime gate, which mid ships OFF (`engine_runtime.py:1060`) — so ATR auto-spread can never fire. Compounded: mid's `min_spread_bp` default is `-10.0` → `max(0.0, -10) = 0`, so `spread_bp=0` quotes BOTH sides at mid with no fee floor, against UI copy promising "tightest fee-floored quote". |
| ~~`COPY-LEADER-READ-FLAP`~~ **FIXED** | **High** | `copy_service.py:1329-1337` treats an absent leader position as "leader closed". `nado_client.get_all_positions` returns `[]` WITHOUT raising on total read failure (`:2121`) and on isolated-margin discovery failure (`:2133`). One flap market-closes the follower's entire copy book at 1.5% slippage and books phantom PnL. The follower side has three guards for this; the leader side has none. |
| `GRID-LIVE-RESIZE-UNCHECKED` | High | `engine_runtime.py:576` writes the overlay-scaled `total_amount_quote` onto a running executor with no `pre_executor_check` → up to 1.25x the risk-approved notional. Same root cause as `GRID-TOTALQUOTE-UNCAPPED`. |
| `MID-MINNOTIONAL-GROW` | Medium | `ladder.py:85` returns `max(1, ...)`, and at one level the min-notional clamp is a no-op; `nado_client.py:2901` then GROWS the sub-minimum order before signing. A 25% overlay "risk reduction" becomes a 33% over-deployment, invisible to the risk engine. |
| `MID-SYNC-DB-IN-TICK` | Medium | `market_making.py:287,298,482` → `engine_persistence.py:100`: ~6 blocking psycopg2 reads per 8s tick inside `on_tick`. The documented APScheduler-starvation class; `test_no_blocking_calls_in_coroutines.py` misses it because the call is one indirection deep. |
| `RGRID-STALE-INVENTORY-READD` | Medium | `rgrid.py:372` sizes off engine in-memory inventory with no venue reconcile. A portfolio-level Close All (`portfolio_handler.py:149`) does no strategy teardown, so `net` stays non-zero and the NON-reduce-only add leg keeps re-resting — re-opening exposure the user just closed. |
| `RGRID-TRAIL-LOOSENS` | Medium | `rgrid.py:998-1010` re-derives the giveback from the LIVE band each evaluation, so an ATR rise moves an already-armed stop 40bp further away — the docstring promises it "only ever ratchets forward". |
| `COPY-AGGREGATE-MARGIN` | Medium | `copy_service.py:1141` uses `total_allocated_usd` for the rail only, never for budget admission; the open loop has no product-count cap and the wizard sets `margin_per_trade == whole allocation` at risk >=1x. A $500 allocation against a 4-product leader commits ~$2,000. |
| `GRID-MINNOTIONAL-CAP-LOST` | Medium | `engine_runtime.py:2483` writes the level cap into the per-cycle dict only; `configs` is re-mapped uncapped every cycle and pushed live, so venue min-notional bumps inflate deployed size. |
| `GRID-BIAS-CHURN` | Medium | `overlay_actuator.py:254` writes `directional_bias` for grid; classic grid never reads it, but it IS in the live-config signature → full ladder cancel/re-place per bias move, bypassing both recenter throttles. Permanent queue-position loss. |
| `GRID-MID-FAILS-OPEN` | Medium | `grid_trading.py:200` — on `mid is None` exposure defaults to `{"buy": True, "sell": True}`, skipping `_apply_entry_suppression`, so a venue hiccup removes both the exposure cap and overlay suppression. |
| `RGRID-MINCONF-DEAD` | Low | `rgrid.py:194` reads `rgrid_signal_min_confidence`, never written by `map_strategy_config` — permanently 0.45, untunable. |
| `ADVISOR-CONF-GATE-WITHHELD` | Low | Capping conviction keeps shaded confidence under the 0.45 gate for deterministic reads in `[0.30, 0.45)`, so rgrid's early-arm (`rgrid.py:973`) and dgrid's extra-confirm tick no longer fire — protective behaviours, withheld. Fix: gate them on the pre-advisor confidence. |
| `RAIL-GROSS-VS-NET` | Low | `bot_runtime.py:2795` shows GROSS PnL in a stop message whose trigger is NET, so users read a stop as having fired early. |
| `GRID-NO-FEE-FLOOR` | Low | `engine_runtime.py:1532` floors only when `spread_frac <= 0`; the UI allows `spread_bp` down to 0.1, where a round trip is a guaranteed net loss. |

## Open findings — self-review audit 2026-08-25 (SL/TP overshoot strengthening: buffer + fast poll + venue stop)

### Fixed in the same PR
| ID | Sev | What |
|---|---|---|
| `VENUE-STOP-DEAD-METHOD` | **Critical** | `nado_client.py:1699` called a non-existent `_build_place_order_params` → `AttributeError` swallowed by two `except` wrappers → the venue stop was 100% dead while enabled in prod. Renamed to `_prepare_place_order_params`; guarded by `tests/test_place_reduce_only_stop.py` (drives the real method — a wrong name AttributeErrors there). |
| `VOL-OPEN-BASE-MERGE` | **High** | `bot_runtime.py::_merge_vol_order_counters` omitted `vol_open_base`, so the spot-sweep sizer's "sell exact held / 0 when flat" guard was dead — a stop firing while vol was flat in `cycle_gap` could market-sell the user's OWN spot. Added to the whitelist; guarded in `test_sltp_invariants.py`. Pre-existing on all vol close paths; the fast poll amplified it. |
| `SLTP-FAST-POLL-VOL-SCOPE` | Med | The fast poll enqueued vol (spot, no leverage → no overshoot benefit) every 15s, multiplying the `VOL-OPEN-BASE-MERGE` exposure. vol added to the scheduler `_skip` set; its per-cycle rail remains the backstop. |
| `SLTP-BUFFER-LEV-ZERO` | Med | The buffer read `snap["leverage"]`, which the stale-DB→fresh-venue position fallback hard-codes to 0 (no-oping the buffer when the read is freshest) and which can diverge from the config-margin basis. Now derives effective leverage = `position_value / margin` (the true %-of-margin driver); guarded in `test_session_safety_rails.py`. |
| `VENUE-STOP-REENTRY` | Med | The venue stop priced off the venue-reported position leverage, so a fire realized `sl_pct` of a DIFFERENT margin than the rail measures → on recovery from a bot outage the rail might not re-fire and the strategy re-opened. Now `venue_stop._effective_leverage` prices off `position_value / margin` (the SAME basis the rail uses), so a venue-stop fire lands where the rail also stands the session down. |
| `VENUE-STOP-ORPHAN` | **High** | The venue stop was cancelled only on the SL/TP-fired path; duration-cap / manual-stop / stale-session flattens left it resting. Now `cancel_session_venue_stop` is called on the fired + duration-cap + stale-session paths, and `sync_session_venue_stop` runs a one-time per-session `reconcile_venue_stops` (list + cancel our resting reduce-only stops on the product) on first manage — sweeping any left by a prior run (start wipes the in-state tracker). Guarded in `test_venue_stop.py` + `test_sltp_invariants.py`. |
| `VENUE-STOP-CHURN` | Low | Cancel-then-place fired on every >1% size change. Now a `NADO_VENUE_STOP_MIN_REPRICE_SECONDS` (20s) interval defers non-urgent refreshes — EXCEPT a side flip or position *increase*, which always re-cover immediately (never under-cover a growing position). Guarded in `test_venue_stop.py`. |

### Remaining before enabling (`NADO_VENUE_STOP_ENABLED=1`) — testnet validation only
The lifecycle code is fixed + unit-tested against a mock client. The live trigger-service round-trip **cannot be validated off-venue** and is the sole remaining gate:
- [ ] `place_price_trigger_order` actually rests a reduce-only trigger (confirm via `get_trigger_orders` at the expected `mid_price_{below,above}` + size).
- [ ] `_extract_digest` matches the real place-response shape (else replace/cancel-by-digest is a no-op — refine the key list).
- [ ] `_row_is_reduce_only_stop` matches the real `get_trigger_orders` row shape (else the orphan sweep can't identify our stops).
- [ ] A crossed trigger flattens reduce-only (never grows/flips) and the software rail reconciles.

### Recorded (intended / low)
| ID | Sev | Where / what |
|---|---|---|
| `SLTP-BUFFER-CALM-TIGHTEN` | Low (intended) | The leverage buffer tightens the SL up to `cap_frac` (0.5) even in a calm market at high leverage (leverage term is a floor, not gated on live volatility). Only ever TIGHTENS (honors the user's cap); a deliberate safety tradeoff worth a conscious product sign-off. To make it volatility-adaptive, feed `recent_move_bp` and let the leverage term cap rather than floor. |

---

## Open findings — self-review audit 2026-08-28 (D-Grid auto-switch default-on, PR #263)

Enabling `dgrid_trend_follow` by default (under the trigger flag) put the trigger
`ReverseGridController` in the RGRID phase for every D-Grid session by default.
Two auditors (dgrid strategy-auditor + sltp-tracer) independently flagged the same
CRITICAL. Validated net floor: +207bp trend / −107bp chop / fee_leak 0
(`test_dgrid_autoswitch_net_floor.py`).

### Fixed in the same PR
| ID | Sev | What |
|---|---|---|
| `DGRID-ONSTOP-ORPHAN` | **Critical** | `DynamicGridController` had no `on_stop`, so a session-rail stop / user stop / redeploy stand-down during the RGRID phase left the trigger delegate's entry rungs (NOT reduce-only) ARMED on the venue — a later cross re-opened an unmonitored position with no rail. Added `DynamicGridController.on_stop` that awaits `self._trend.on_stop(reason)` (cancels rungs + stop); the stop path runs this BEFORE `close_all_positions`, so it also prevents a rung firing mid-flatten. Guarded by `test_dgrid_stop_in_trend_phase_cancels_the_delegates_venue_triggers`. |
| `DGRID-CANDLE-OUTAGE-RGRID` | Med | `_classify` HELD the current phase on a candle outage; parked in RGRID (delegate chop gate disabled) it kept pyramiding blind for the whole outage. Now degrades to the SAFE mean-reversion GRID (debounced flip flattens the delegate); a GRID session stays GRID; candles returning can flip back. Guarded by `test_dgrid_candle_outage_degrades_to_grid_not_trapped_in_rgrid`. |
| `DGRID-GRIDRGRID-RESIDUAL` | Med | GRID→RGRID handoff gated only on `_inventory_net_base()` (inventory), no venue re-read (RGRID→GRID has one via `flatten_now`). If the grid's accounting drifts from the venue, inventory reads flat while a residual remains; the delegate baselines it out and it survives the cycle, invisible to the exposure cap / tier booking / status. Added `_ensure_venue_flat_for_trend`: before arming the delegate, read the venue and close any residual reduce-only, arming only once the venue confirms flat (symmetric with the RGRID→GRID check). Guarded by `test_dgrid_trend_spawn_closes_a_venue_residual_before_the_delegate_baselines_it`. |
| `DGRID-ORDERCOUNTS-RGRID` | Low (telemetry) | The RGRID trend delegate is a trigger controller with NO executor, so the base `order_counts` (sums executors) read 0 for the whole RGRID phase — undercounting `strategy_sessions.total_orders_*` and the /status figure (the FILLS still reached `trades_<network>` via nado_sync, so PnL/volume/fees/entry-exit were always correct). Added a `DynamicGridController.order_counts` override that includes the live delegate AND banks a completed phase's counts before the delegate is dropped, keeping the cumulative monotonic for the per-cycle delta. Guarded by `test_dgrid_order_counts_include_the_trend_delegate_across_a_flip`. (The `count_engine_orders`/`engine_executors` fallback still can't see triggers, but it is used only post-restart before `order_observability` is populated — moot under the redeploy stand-down rule.) |

### DB-tracking + settings audit (2026-08-28) — VERIFIED CORRECT (no change needed)
- **SL/TP adherence:** a user's SL 20% ($20 of $100) / TP 100% ($100) maps to `sl_pct=20`/`tp_pct=100`, `tp_margin_basis=$100`; the session rail fires on `session_pnl_pct_net` (net of fees) at `-sl_trigger`/`+tp_pct` and the tier ladder tops EXACTLY at the user's TP. The delegate's own stop is a spread-derived price barrier (no explicit `sl_pct`), so the units invariant holds.
- **Config inputs:** levels → `levels_count` + delegate; regime → `trend_on_vr`/`range_on_vr`/`short/long_window`; spread `min/max_bp` → `spread_floor/cap_half_pct`; interval → `effective_interval_seconds`; POV/participation → run-duration + per-cycle chunk (`mm_cycle_notional_usd` → `_chunk_dec`). All wired and read, none dead.
- **Money tracking:** nado_sync fetches `get_trigger_orders` + merges them, and `get_matches` captures ALL venue fills (including fired-trigger fills) → `trades_<network>`, attributed via the wired `_on_place` digest-link + session-window fallback. So fills, fees, PnL, volume, and entry/exit prices are tracked for BOTH the GRID and RGRID phases.
| `DGRID-LEGACY-OPTIN-FLAGOFF` | Low [VERIFIED] | With `NADO_REVGRID_TRIGGER_ENABLED` OFF, an explicit `dgrid_trend_follow=1` (the "🔀 Auto-switch" button always sends `:1`) spawns the legacy pyramiding `RGridController` — the measured August bleed — with no PHASE-0 backstop. Moot in prod (flag ON). Fix idea: force trend-follow off when the trigger delegate isn't available, or retire the legacy delegate. |
| `DGRID-DELEGATE-RISK-BYPASS` | Low [VERIFIED] | The trend delegate places rungs via `place_trigger_order` directly, not `spawn_executor`, so `max_single_order_quote` / kill-switch aren't enforced per-order. Self-bounded (`deployed/levels`, finite ladder, sub-min declined) and the parent stays `pre_tick_check`-gated. Pre-existing, not introduced here. |
| `DGRID-SIM-HANDOFF-COVERAGE` | Low [SUSPECTED] | The net-floor backtest books the delegate's fills into inventory (`book_market_fills`), so the sim's inventory is non-vacuous while LIVE's is — the PnL floors are trustworthy but the sim does not exercise `DGRID-GRIDRGRID-RESIDUAL`'s live inventory-flat-but-venue-not gap. |
| `RGRID-QUOTE-ZERO-ORDERS` | Low [VERIFIED] | The gate telemetry renders "QUOTE" with 0 orders in the first-tick-before-read and degenerate-sizing (sub-min-notional / `order_amount_quote<=0`) cases — safe direction (never a false PAUSE), pre-existing; the telemetry only distinguishes the chop stand-down. |

---

## Open findings — self-review audit 2026-08-30 (Grid + Reverse Grid presence-first entry)

User directive: "Grid and Reverse Grid are not placing orders. They need to enter
the market first before figuring out whether to pause or keep quoting." Root cause
(evidence in prod telemetry): both modes gated the ENTRY. Trigger `ReverseGridController`
(prod `NADO_REVGRID_TRIGGER_ENABLED=1`) with `chop_stand_down=True` armed zero triggers
until a trend confirmed (rgrid user 1124285818: 19/20 zero-order cycles). Classic grid
rests maker buys that don't fill on the thin venue; fill-anchored grid (user 8542863313,
`fill_anchored=1`) showed 25 placed / 21 cancelled / 0 filled with gate=QUOTE.

Fix = **presence-first (maker)**, user-chosen: always place the initial ladder / arm the
triggers on entry; demote the regime/chop gate from an ENTRY BLOCK to an ADD/RE-arm
governor. Four read-only auditors (grid + rgrid + dgrid strategy-auditors + sltp-tracer)
returned **zero `[VERIFIED]` bugs**. Full suite green (2858 passed), mypy clean, SL/TP
invariants 60 passed, DGrid net-floor unchanged (+207bp/−107bp).

### Changed
| File | Change |
|---|---|
| `reverse_grid.py` | New `_has_opened` (reset in `on_start`, set in `_open_position`). `_maintain_flat` gate now `chop_stand_down and _has_opened and not _trend_confirmed()` — the FIRST arming is never gated; chop only gates RE-arm after a close. |
| `grid_trading.py` | `on_start` always `_spawn()`s the initial ladder (was: return early when paused). Deferred re-spawn in `on_tick` no longer gated (retry a failed initial spawn even while paused). |
| `fill_anchored.py` | Paused-gate reduce-only clamp now `gate_paused and base_value != 0` — a FLAT book still places entry quotes; reduce-only applies only once a position is held. |
| `dynamic_grid.py` | `_trend_mapped_config` forces `revgrid_chop_stand_down=False` (mirrors the mapper's live default at `engine_runtime.py:2064`; makes it authoritative for the fallback branch too). |

### Recorded (intended / by-design / pre-existing — no fix, no guardrail)
| ID | Sev | What |
|---|---|---|
| `RGRID-PRESENCE-ONE-CHOP-ENTRY` | Info [VERIFIED] | Presence-first allows exactly ONE ungated entry per run, including into chop. Worst case ≈ one round-trip: `stop_pct` (floored above the taker round trip) + taker fees, then gated. Bounded by the venue reduce-only stop + session %-margin rail. Matches the directive. |
| `GRID-ARMEDGATE-QUOTEFIRST` | Low [VERIFIED] | For an EXPLICITLY-armed classic-grid gate (`regime_gate_enabled=1`, non-default), a later PAUSE suppresses NEW levels but does not withdraw the resting ladder (gate contract = "stop digging, never flatten"), so resting opens keep filling into a trend. Bounded by `total_amount_quote` + inventory cap (new levels) + session SL rail. Documented in `grid_trading.on_start`. Fill-anchored differs: its reconciler cancels the disallowed side's resting quote. |
| `GRID-NO-VENUE-STOP-OFFLINE` | Low [VERIFIED, pre-existing] | Grid (and dgrid GRID phase) has no controller-side venue stop; presence-first raises trend-entry frequency, so an offline polling loop leaves such a position protected only by the default-off `sync_session_venue_stop` (`NADO_VENUE_STOP_ENABLED`). Enable once testnet-validated — already tracked in the 2026-08-25 SL/TP-overshoot work. Not introduced here. |

### Guardrails / tests updated (contract change, not a bug fix)
The 4 rgrid chop-gate tests + 1 grid regime-gate test encoded the OLD "stand down before
entry" contract; rewritten to the presence-first contract (initial entry arms; chop gates
the re-arm). New coverage: `test_initial_entry_arms_with_no_candle_feed`,
`test_initial_entry_arms_in_chop_then_gates_the_rearm`,
`test_rearm_after_close_arms_once_a_trend_confirms` (rgrid);
`test_grid_enters_first_then_suppresses_new_entries_while_trending` (regime_gate);
`test_presence_first_flat_book_quotes_while_paused_then_reduce_only_in_position` (fill_anchored).

---

## Open findings — self-review audit 2026-08-30 (Mid level recycling "fill the gaps")

User request: in Mid, after a level round-trips (buy fills → its sell closes), re-arm
the entry at ~the same price so a reversal re-fills it. Root cause it addresses: Mid
quotes around `_reservation_price(mid)`, which FOLLOWS the mid, so a filled deep level
is re-quoted near the new mid, never recycled. Chosen design (Path B, drift anchor,
2% floor): pin the quoting anchor to a slowly-drifting reference so Mid's existing
reconciler (which already re-places a terminated level) recycles fixed levels.

**Opt-in, default OFF.** Two auditors (mid strategy-auditor + sltp-tracer) returned
zero Critical/High/Med findings. Full suite green (2868 passed), mypy clean, SL/TP
invariants 60 passed. Backtest (cost-aware harness): range **+35% net** vs plain Mid;
steep downtrend **identical** (floor + cap bind at the same exposure — never worse);
gentle downtrend bounded. The session %-margin SL rail is controller-external and
covers a recycled position byte-identically to a normal one.

### Changed
| File | Change |
|---|---|
| `market_making.py` | `_recycle_theta(mid)` (drift/static anchor + 2% floor); `on_tick` pins `theta` when `recycle_enabled`; `_reconcile` floor guard suppresses new LONG-side buys below the band; `ladder_metrics` telemetry. All opt-in, default OFF — inheritors (FillAnchored/RGrid) never set the keys and fully override `on_tick`, so it is inert for them (3 independent layers). |
| `engine_runtime.py` | Mid-branch config keys: `mid_recycle_enabled` / `mid_recycle_anchor_mode` (drift) / `mid_recycle_drift_alpha` / `mid_recycle_floor_pct` (percent→fraction). |
| `tests/engine/controllers/test_mm_recycle.py` (new) | 10 tests: off-by-default, drift lag, static freeze, recycle-near-anchor, floor suppression, inheritor inertness, + the 3 fixes below. |

### Fixed in the same change (auditor findings — config-robustness, no money impact)
| ID | Sev | What |
|---|---|---|
| `MID-RECYCLE-FLOOR-REDUCEONLY` | Low [VERIFIED] (sltp-tracer) | The floor suppressed ANY bid below the band, including a bid that REDUCES a net short (a profit-taking cover). Now exempts reducing orders (`_base_value(mid) >= 0` guard) — mirrors the exposure cap's reduce-only exemption; the floor bounds LONG accumulation only, never an exit. A cover-bid larger than a small short may flip to a bounded new long below the band (net-exposure-cap-bounded, re-floored next tick) — documented inline as intentional. Guarded by `test_floor_exempts_a_reducing_cover_bid_when_short`. |
| `MID-RECYCLE-ALPHA-ZERO` | Low [VERIFIED] (strategy-auditor) | `mid_recycle_drift_alpha=0` did not freeze the anchor — a bare `or "0.02"` treated the falsy `Decimal(0)` as "unset" and restored drift. Now 0 is honored (static via alpha); only None/"" default. Guarded by `test_drift_alpha_zero_freezes_the_anchor`. |
| `MID-RECYCLE-FLOOR-DEGENERATE` | Info [VERIFIED] (strategy-auditor) | `floor_pct>=1` silently disabled the floor, and `floor_pct<=0` would suppress EVERY buy. Now the floor arms only for a sane band `0 < floor_pct < 1`; outside that it is disabled (None), with the inventory cap + SL as the hard bounds. Guarded by `test_floor_pct_zero_disables...` / `test_floor_pct_ge_one_disables...`. |

### Recorded (by-design, no fix)
- Bypassing the reservation inventory-skew removes a SOFT accumulation brake, so a
  recycled position can reach the (unchanged) net-exposure cap faster — higher
  effective velocity within the same margin. The %-margin rail measures this
  correctly and the velocity-aware overshoot buffer sizes to it. Bounded by the cap
  + SL. **Risk note for high leverage:** at 40× the SL rail is the primary backstop;
  advised to the user, leverage left to their discretion.
- Backtest is controller-only (no session rail); the live SL rail caps trend loss
  earlier than the raw backtest figure, more so at high leverage.

---

## Open findings — self-review audit 2026-08-30 (Mid quoting — 298-placed/0-filled + gate flap)

User report (live logs + screenshot): Mid makes NO volume (entry far from price) and
the regime gate flaps pause/resume every ~15 min. A 4-agent investigation workflow +
verification pinned FOUR compounding defects on a thin, rate-limited venue (user
5776741680, BTC, levels=20, aggressive):

1. **Infeasible ladder** — `levels=20` → 40 order-ops/tick at ~1 execute/sec; the
   deep ladder is re-laid one-per-second and never rests (298 placed / 0 filled).
2. **TTL < cadence** — aggressive `max_quote_lifetime_s=6s` sat UNDER the 8s enforced
   cadence, so `_should_hold` force-refreshed every quote every tick; the 16s
   min-lifetime queue-hold was unreachable dead code above it.
3. **Gate flap** — the overlay toggled `regime_gate_enabled` True↔0.0 each cycle
   (arm-on-suppress, mapper-default-off), flipping the live-config signature
   (→ stop_all_quotes teardown) AND bypassing the gate's resume hysteresis.
4. **Blind pricing** — `quote_mode="mid"` never reads the touch, so quotes rest
   behind the book and can't fill on a thin venue.

Fix (user-chosen "all four, touch opt-in"). Two auditors (mid strategy-auditor +
sltp-tracer) returned zero Critical/High/Med; full suite green (2877), mypy clean,
SL/TP invariants pass.

### Changed (bug fixes 1–3 default ON, mid-only; touch opt-in default OFF)
| File | Change |
|---|---|
| `engine_runtime.py` | `_mid_max_ladder_levels` caps mid `ladder_levels` to `min(levels, place_rate×cadence/2, NADO_MM_MAX_LADDER_LEVELS=5)` (20→4). `max_quote_lifetime_s = max(profile_ttl, min_quote_lifetime_s)` (6→16 > cadence). `_maybe_apply_overlay` sticky gate arm with a decaying dwell (`NADO_MID_GATE_ARM_DWELL_CYCLES=24`), re-asserted BEFORE the candle fetch so a transient overlay skip can't disarm it. `quote_mode` resolves to "touch" on `mid_objective=volume`/`mm_quote_mode=touch`, else "mid". |
| `controller_base.py` | `evaluate_quote_gate`: on an overlay-driven gate DISARM, keep the internal stale-PAUSE reset (MID-GATE-STALE-PAUSE) but emit no `_gate_event` (no spurious "resumed" card). |

### Auditor findings (both Low, no money-bleed)
| ID | Sev | Disposition |
|---|---|---|
| `MID-GATE-DWELL-TRANSIENT` | Low [SUSPECTED] | An overlay early-return DURING the dwell would disarm the gate for one tick (2 teardowns). **FIXED**: the arm is re-asserted at the top of `_maybe_apply_overlay` before the candle fetch; the dwell decays only on a clean cycle. Guarded by `test_mid_gate_arm_survives_a_transient_overlay_skip`. |
| `MID-GATE-DWELL-STALE-CARD` | Low [VERIFIED] | On dwell-expiry the gate disarms silently (no resume card) while the last card may still say "paused" — the intended 3b tradeoff (an overlay disarm is not a market resume). Book resumes correctly; no money impact. Recorded, not fixed. |

### Recorded (product notes)
- Touch mode rests INSIDE the fee — the session %-margin SL rail becomes the
  adverse-selection bound. Hence opt-in + testnet-gated before any default flip.
- The aggressive profile's mapped `interval_seconds=4` is DEAD (the scheduler uses
  the user's interval via `effective_interval_seconds`, capped at 8s); the TTL fix
  derives from the real 8s cadence. Left as-is (a faster cadence would worsen the
  rate-limit problem); flagged for a later cleanup.
- Longer-term: batch placement via a real `place_orders` (weight=count) would let an
  N-level side lay in one round-trip — a build, not part of this minimal fix.

---

## Product decision — D-Grid trend phase should pyramid (2026-08-12)

The owner has decided that **dgrid's trend phase should behave like R-Grid** (add
while the move extends, flip when it turns) rather than the current mirrored
mean-reversion short ladder. This **supersedes** the earlier "R-Grid is its own
strategy and never the D-Grid phase switcher" framing for the phase-switcher's
trend leg. R-Grid remains a separately selectable strategy.

This is an architecture change, not a wiring tweak, because dgrid's trend phase is
currently `ReverseGridExecutor` (= `GridExecutor` with `side=SELL`) and shares
nothing with `rgrid.py`. Scoped work:

1. **Controller composition.** Decide between (a) dgrid delegating its trend phase
   to an embedded `RGridController` instance, or (b) extracting R-Grid's pyramiding
   core (anchor leash, add-off-last-fill spacing, derived arm/giveback/exit
   geometry, step-vs-stop-budget cap) into a mixin both consume. (b) avoids two
   controllers owning one session's inventory; (a) is faster but needs a clear
   owner for `spawn_executor`/inventory.
2. **Config mapping.** `map_strategy_config`'s dgrid branch must emit the R-Grid
   keys the pyramiding core reads (`step_capped_quote`, `exit_band_cap`,
   `reset_threshold_pct`, `exit_band_mult`) — today it emits none of them, and
   `rgrid_signal_min_confidence` is separately dead for rgrid itself.
3. **Blockers that must be fixed FIRST**, or the pyramiding leg inherits them:
   `RGRID-EXITBAND-INVERT` (overlay-scaled band inverts the exit ordering — the
   configuration that measured -85.27) and `RGRID-TRAIL-LOOSENS` (armed stop moves
   away when the overlay widens). Both are already guardrailed.
4. **Phase transition.** Flipping between a mean-reversion ladder and a pyramiding
   trend follower must flatten and cancel both legs before re-arming;
   `DGRID-REVERSAL-FLIPFLOP` (guardrailed) must be fixed first or the new trend leg
   will be armed and unwound on every 0.4% retrace.
5. **Reporting bridge.** Engine fills must still bridge into
   `trades_<network>`/`strategy_sessions` for the pyramiding leg.
6. **Proof.** Cost-aware backtest across trending AND ranging regimes before merge.
   The checked-in reference CSV resamples to 11 bars and cannot serve; real trending
   data is required (R-Grid's own +487 figure came from five trending regimes).

Deliberately NOT started in the security-followups branch: it needs its own branch,
its own backtest evidence, and items 3-4 landed first.

---

## Anti-hallucination contract

This workflow exists because a *wrong* bug report is worse than a missed one — it
burns a fix cycle and erodes trust in the whole process. Therefore:

1. Auditors are read-only and must quote `file:line` for every claim.
2. Every finding is tagged `[VERIFIED]` or `[SUSPECTED]`; only `[VERIFIED]` items get a fix.
3. Top findings are re-checked by a second pass (the orchestrator) before they enter the report.
4. Each fixed bug leaves behind a guardrail test, so it can never silently regress.
