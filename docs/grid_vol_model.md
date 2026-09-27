# Grid-family volatility model (Grid, D-Grid, R-Grid)

**Status: OPT-IN. Every behaviour is OFF by default.** The evidence is in-sample
only (BTC, TUNE window 2026-08-28..09-11), and the owner rule forbids flipping a
default on an unvalidated backtest. With every toggle off, the controllers trade
byte-for-byte as they did before this model existed. That is pinned by golden
venue-call logs recorded on the base branch
(`tests/engine/fixtures/gvol_off_golden.json`, the `GVOL-OFF-IDENTITY`
invariant in `tests/engine/test_sltp_invariants.py`).

## The statistic

`rv60` is the RMS of the last 60 **closed** 1-minute log returns, in bp per
minute, not de-meaned. It is exactly the harness formula
`sqrt(mean(diff(log(close))**2)) * 1e4`, recomputed once per closed minute
(`quant/vol_model.realized_vol_bp`).

Why this statistic, from the grid-family audits:
- It predicts the next hour's high-low range with Spearman **0.60**; the variance
  ratio manages **0.12** (8,597 points, 31 days).
- The next-60-minute range runs from about 28 bp in the quietest fifth to about
  81 bp in the busiest.
- Variance-ratio trend switches showed no directional edge (+0.3 / +0.9 / −3.4 bp
  after 5 / 15 / 60 minutes).

## Per-product self-calibration

Every threshold is a multiple of the product's own **trailing 7-day median
rv60** (`strategy/vol_baseline.py`), which needs at least **72 h** of coverage
before any verdict other than `WARMING`. In that form:
- the BTC gate of 3.05 bp/min becomes **0.82×** the median (3.05 / 3.70);
- the BTC quintile edges become 0.65×, 0.91×, 1.10× and 1.42×.

Multiplying every log return by a constant scales rv60, the baseline and every
threshold together, so the verdicts are scale-invariant (a unit test pins this).
The spacing floors are absolute on purpose, because fees are absolute.

A short baseline destroys the signal. On BTC, Spearman to the next hour's range
falls to 0.29 with a 6 h baseline and 0.42 with 24 h; a 7-day baseline keeps
0.63 and agrees with the absolute 3.05 gate 95% of the time. So 7 days is a
design constraint, not a tuning choice.

Minutes inside a run of more than 30 forward-filled (no-bar) minutes are
excluded from the baseline. Those are closed markets (equity/RWA perps) or dead
tape, and their zero returns would make the product look permanently hot. The
live verdict is `UNKNOWN` when fewer than half of the last 60 minutes have real
bars.

### Baseline provider and IO

- `strategy/vol_baseline.get_baseline` runs every venue page and all of its work
  on the SDK thread pool (`run_blocking_sdk`). The only on-loop work is a memo
  lookup.
- In steady state it merges the 200 candles the controller already fetched, so
  it costs **zero extra weight**.
- While the series covers less than 7 days, it pages older 1m candles, at most
  3 pages per call (51 weight each) and one call per closed minute.
- The memo is per process and per (network, product); there is no
  cross-process cache in this repo any more. A redeploy therefore re-warms: 72 h
  in about two minutes, 7 days in about four. The card shows `LEARNING` in the
  meantime.
- **DENIED ≠ EMPTY.** An empty page means "retry later" (10-minute backoff). It
  never means "history ends here"; only a short, successful page proves the
  start of history.

## Verdicts (stateless, from candle history)

| Verdict | Meaning | Reason code |
|---|---|---|
| `CALM` | rv60 at **each** of the last 15 closed minutes ≤ `mult × median` | — |
| `HOT` | any of those 15 minutes above the gate | `vol_hot` |
| `UNKNOWN` | no candles, newest closed bar > 180 s old, < 50% real bars, or too little history | `vol_unknown` |
| `WARMING` | baseline covers < 72 h | `vol_warming` |

A pause is immediate; a resume needs 15 calm minutes. This reproduces the V2
harness's `gated_until = t + 900` rule exactly (property test). Because the
verdict is computed from candle history, not a tick counter, it survives
restarts and rebuilds and cannot be advanced by fast ticks.

**DENIED ≠ EMPTY on the live read.** A budget-denied or failed candle read
(`[]`), an exception, or a series with no newer bar is "no new information". The
last good series is kept, and the verdict turns `UNKNOWN` by itself once the
newest bar is older than 180 s. An empty read never produces `CALM`.

## What each strategy does with it

| Strategy | Settings | Behaviour when ON |
|---|---|---|
| Grid (classic + fill-anchored), **🌡 Vol** tab | `grid_vol_gate`, `grid_vol_gate_mult`, `grid_vol_spacing`, `grid_vol_spacing_k`, `grid_inv_skew`, `grid_inv_cap_hard`, `grid_inv_cap_pct` | Gate: not CALM → withdraw resting **entries** (nearest-to-mid first), keep close legs, hold inventory, skip the FA taker concession, place no entry at start until calm; on resume, re-lay the ladder once around the current mid. |
| D-Grid, **⚡ Regime** tab | `dgrid_regime_model` (`vr` / `vol`), `dgrid_vol_gate_mult`, `dgrid_vol_spacing`, `dgrid_vol_spacing_k`, `dgrid_inv_skew`, `dgrid_inv_cap_hard`, `dgrid_inv_cap_pct` | Under `vol`, the same gate as Grid. D-Grid **never** switches to the trend phase or fires the reversal flip, so there is no taker dump of the ladder. The user's `dgrid_trend_follow` is not rewritten: the card shows it as "ignored under Vol model", and it is honoured again on `vr`. On a live switch `vr → vol` while the trend delegate holds a position, the delegate keeps running until its own stop / trail / the rail closes it; a settings change never flattens by taker. |
| R-Grid, **⚙️ Core** tab | `rgrid_vol_arm`, `rgrid_vol_compress_mult` (0.91), `rgrid_vol_expand_mult` (1.42) | Arm only when rv15 ≥ expand × median within 5 minutes of a moment when rv60 ≤ compress × median in the prior 120 minutes. This includes the first arm. It composes with the chop guard by **AND**. It works on both R-Grid engines: the trigger ladder (`NADO_REVGRID_TRIGGER_ENABLED`) and the legacy exposure-anchored controller. |

The R-Grid vol-arm is **UNVALIDATED**. No harness or replica run has tested this
rule; it can only remove arms. No lever has made R-Grid profitable at taker fees
(replica −350 to −490 per $1M), and the card says so.

### Spacing (`*_vol_spacing`)

Level spacing = `k × rv60` (k defaults to 2.6, since 2.6 × 3.05 ≈ the V2 lattice's
8 bp), clamped as follows:
- never below the fee floor: 6.8 bp for the classic ladder / D-Grid (the mixed
  round trip) and 6.0 bp for fill-anchored (4.0 bp maker round trip + 2 bp);
- never below 2 ticks;
- within the card's existing spread band (`min_spread_bp` / `max_spread_bp`,
  `dgrid_min_spread_bp` / `dgrid_max_spread_bp`).

A change is applied only when it moves at least max(1 bp, 15%), and only through
a recenter; resting rungs never move tick by tick. Held levels keep the close leg
they were opened for. On fill-anchored, the overlay's spread factor is not
applied on top (the card discloses this).

### Inventory control

- **Skew (`*_inv_skew`).** An Avellaneda–Stoikov reservation offset driven by rv
  (γ = 0.5, horizon 15 minutes). It reuses `quant/mm_profile.reservation_offset_bp`,
  so it is bounded at half the spacing: it can never cross sides or breach the
  fee floor. Long inventory shifts **new** entries lower. Classic close legs are
  unchanged, and on fill-anchored the no-cross clamps still apply after the
  shift. It is 0 while rv is unknown.
- **Hard cap (`*_inv_cap_hard`, `*_inv_cap_pct`, default 30%).** It counts
  **held + resting** growth-side notional: an entry may be placed only while
  `held + resting ≤ cap` (so the total stays ≤ cap + one level, and a flat book
  can always place its first rung). Entries are placed nearest-to-mid first and
  trimmed deepest-first. The default soft cap only watches filled inventory and
  never withdraws resting orders, which is why prod inventory reached 42–79% of
  deployed against a "30%" cap. The hard cap is a drawdown lever (≈ neutral
  Cost/$1M, lower volume).

## Fail-safe direction (all three)

When volatility cannot be measured (`UNKNOWN`, `WARMING`, thin or closed
market), the strategy **stops adding risk**:
- no new entries, and resting entries are withdrawn;
- every exit, stop, close leg and reduce-only order keeps working;
- inventory is held, and nothing is sold at market.

This is the `GVOL-EXITS-NEVER-GATED` invariant.

## Telemetry

The controllers emit `gvol_*` keys, prefixed so they never collide with the
Volume Bot's `vol_*` keys. `bot_runtime` blocklists them against settings,
copies them into state, and pops them when a controller stops emitting them.
`/status` shows:
- `🌡 Vol: CALM 2.4bp/min · gate 3.1 (0.82× 7d median 3.8) · spacing 8bp · skew −1.2bp`;
- `HOT … standing down: N entries withdrawn · exits working`;
- `LEARNING this market (41h / 72h)`;
- `UNREADABLE (candles 240s old)`;
- the R-Grid arm state;
- the hard-cap usage.

The gate line gives a resume condition per reason. Stand-down / re-entry
notifications are limited to one PAUSE and one RESUME per session per hour.
WARMING and the R-Grid arm wait are card-only. The services log line carries
`gvol=… rv=… gate_bp=…`.

## Deferred: fair-value (lead-venue) quote protection

The Hyperliquid websocket mid (`market_data/hl_ws.py`) is in-process and
readable at decision time, but this change does not build on it:
1. The measured lead is **Binance**'s (about one 10 s bucket). The bot has no
   Binance feed, and HL→Nado lead-lag has never been measured.
2. Grids decide every 17–49 s and requote serially (about 1.47 s per placement),
   so a 1–10 s lead is spent before a grid can act.
3. The harness shows faster reaction buys grids no Cost/$1M improvement, while
   the current slot requote multiplies cancels (10.2 → 36.6 per fill).

The follow-up, in order, each step gated on the previous one:
1. price-keyed requote;
2. shadow-log the HL-vs-Nado gap at our fills and cancels;
3. measure HL→Nado lead;
4. harness test (audit H6);
5. only then an opt-in `grid_fv_protect`.

A Nado public-websocket book feed for a Stoikov microprice is a separate
infrastructure change.

## Validation required before any default flip

This is not part of this change; see the spec's §12.7. Use the audited harness
with QUEUE fills, TUNE → frozen TEST, and a paired day-bootstrap.
- **Grid gate:** ΔCost/$1M 80% CI > 0 on TEST, with volume ≥ 40% of ungated and
  maxDD ≤ ungated. Re-run V2 unchanged on TEST as well.
- **D-Grid vol model:** TEST F0 ≥ the VR baseline + 100.
- **Hard cap:** maxDD −30% with Cost/$1M not worse by more than 20.
- **Spacing:** paired ΔCost/$1M > 0 for k ∈ {1.5, 2, 2.6, 3}.
- **R-Grid vol-arm:** paired ΔF0 > 0 at 80% vs the first-arm-gate baseline. The
  honest target is "less negative", never positive.
- **Per product:** repeat the 7-day-baseline Spearman check on ETH and SOL.

Open question for the owner: should the Vol tab offer a combined "experiment
preset" (gate + hard cap + hold, the shape that measured +169/$1M in-sample)? It
is not included, to keep one toggle per behaviour.
