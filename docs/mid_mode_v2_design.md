# Mid Mode v2 + position scaling — design plan

Plan only. Nothing built. Venue capabilities verified against Nado SDK v2 or
probed live on mainnet (2026-08-01); market numbers measured, not assumed.

Sources: Tread Fi docs (`docs.tread.fi/bots/market-maker-bot/…`) for the
product spec; Quant Guild *How To Actually Market Make* for the model.

**Decisions taken:** Mid Mode is a **perp volume product**, not a spread-PnL
product. Buy-low/sell-high stays with Grid. Mid does **not** become
entry-aware. Both Mid and Grid gain **position scaling**.

---

## 0. Correction: measured against the right objective, Mid belongs on BTC

My previous verdict — "Mid cannot profit on BTC, it must move to wide markets"
— was measured against a **spread-PnL** objective. Given Mid is a **volume**
product, that verdict is wrong and I am withdrawing it.

For a two-sided maker, one round trip buys at `mid − δ` and sells at
`mid + δ`:

```
net per round trip = 2·(δ − f)          volume per round trip = 2 × notional
⇒  cost per $1 of VOLUME = (f − δ)
```

| Market | Spread | δ at touch | Cost per $1M of volume |
| --- | --- | --- | --- |
| **BTC-PERP** | 0.16 bp | 0.08 bp | **$352** |
| ETH-PERP | 0.54 bp | 0.27 bp | $333 |
| SOL-PERP | 1.37 bp | 0.69 bp | $292 |
| Spot Vol bot (taker) | — | — | **$715** |

**A tight spread is an advantage for a volume product**, and Mid on BTC-PERP
is the cheapest volume engine we have — roughly **2× cheaper per $1M than the
spot Vol bot**. It is already pointed at the right market. The eligibility
gate I proposed is withdrawn; for volume we want *tight spread + deep book +
heavy flow*, which is exactly BTC/ETH.

### 0.1 The real problem is FILL RATE, not cost

```
BTC-PERP spread = 1 tick ($1 on $63,271)
best bid queue  = $217,171      next level down = $4,998
```

The spread is a single tick, so **there is no room to improve the quote** —
you cannot post inside it. You can only *join the queue*, behind $217k.

Fills therefore come from **queue priority**, and priority is destroyed by
every cancel. Today Mid re-places both sides on nearly every cycle
(`place_order BUY … place_order SELL` every ~30 s in the logs), so it
perpetually returns to the back of a very long line. That is why 10 minutes of
running produced **one $6 fill**.

> For Mid-as-volume the ranking of fixes is: **(1) stop cancelling,
> (2) ladder the quotes, (3) fix the loop latency.** Pricing cleverness is
> nearly irrelevant on a one-tick book.

---

## 1. The missing feature: position scaling (applies to Mid AND Grid)

Your observation is correct and I verified it in the code.

| Mode | Orders per side | Scaling |
| --- | --- | --- |
| **Mid** (`market_making.py`) | **1** | none — the word `levels` **does not appear in the controller**. The mapping states: *"Mid is a single bid + single ask (NOT a ladder) … `levels` does not subdivide the quote here."* **`levels` is a dead input.** |
| **Grid, fill-anchored** (`fill_anchored.py`) | **1** | none — *"quotes ONE bid + ONE ask around a reference price."* |
| Grid, classic ladder (`grid_executor.py`) | N | yes (`generate_grid_levels`) — but the band is **static at spawn**, so it does not follow price |

So **no mode combines a floating/fill-anchored reference with a scaling
ladder.** One order goes out, and if it does not fill the strategy simply
waits — exactly as you described.

### 1.1 Ladder design

Per side, N levels stepping away from the reference:

```
δ_i    = δ_0 + i · step_bp                     i = 0 … N−1
size_i = base · w_i        with Σ size_i = deployed notional
```

Weight curves `w_i` (user-selectable, defaults differ by mode):

- **flat** — equal size per level. Simplest; maximum queue coverage.
- **linear** — size grows with depth. Scales *into* an adverse move.
- **geometric** — size grows faster with depth. Aggressive averaging; needs a
  hard inventory cap or it becomes a martingale.

On BTC the two effects compose usefully:

- **Level 0** sits at the touch behind the $217k queue → fills from ordinary
  flow, slowly.
- **Levels 1…N** sit at successively worse prices with far shorter queues
  ($4,998 at the next level) → fill when price ticks toward them.

So as price falls, deeper bids fill and the position **tops up**; as it
recovers, the ask ladder **reduces** it. That is the scale-in/scale-out
behaviour that is missing today, and it is the same mechanism for both modes —
only the *reference* differs (Mid: book mid; Grid: last fill).

### 1.2 The hard constraint on level count

Each level must clear the venue minimum notional:

```
max_levels = floor(deployed_notional / min_notional)
```

BTC-PERP min notional is **$100**. A $100 margin at 10× deploys $1,000 per
side → **at most 10 levels**, realistically 3–5 so each level is meaningful.
At 1× ($100 deployed) **a ladder is impossible** — one level only. The UI must
compute and show this, and clamp, rather than silently shrinking sizes below
the venue floor (the failure mode that made `levels` dead in the first place).

### 1.3 Inventory bound

A scaling ladder is an averaging-down machine. It **requires**:

- a hard cap on total base inventory (not just notional per order),
- reduce-only quoting once the cap is hit,
- and the existing session SL as the outer bound.

Without these, `geometric` weighting on a trending market is a martingale.
This is non-negotiable and must land in the same change as the ladder.

---

## 2. The model

### 2.1 What the notebook actually teaches

The notebook builds up from a dice market, and its transferable core is:

**(a) The mid is an estimate, not an observation.** The fair value θ is the
MSE-minimising estimator of the payoff:

```
MSE(θ) = E[(θ − x)²]     →     θ* = argmin MSE(θ) = E[x]
```

For the dice, θ* = 3.5. The lesson for us: **the book midpoint is not
automatically fair value** — it is one estimator, and on a thin or lopsided
book it is a poor one.

**(b) Edge is the difference between your fill price and fair value.** Buying
at θ − δ or selling at θ + δ accrues δ per fill in expectation.

**(c) The central trade-off — wider is not better.** The notebook's key figure
contrasts the naive and realistic models:

```python
# naive:      profit per trade grows forever with spread
expected_profit_per_trade = spread_range / 2

# realistic:  volume DECAYS exponentially as you widen
def simple_liquidity_curve(spread, k=0.8):
    return np.exp(-k * spread)
expected_profit_realistic = (spread_range / 2) * liquidity
```

So expected profit rate = `(δ) × A·exp(−k·δ)`, which has an **interior
maximum**. Quote too tight and you earn nothing per fill; too wide and you
never fill.

**(d) Hedging is how the spread is actually collected.** In the options
section: *"the efficacy of our hedge dictates how effectively we collect our
spread"*. For a perp maker the analogue is inventory management — an unhedged
maker is just a directional trader who paid fees for the privilege.

### 2.2 The fee-aware optimum (the notebook's result, corrected for our reality)

The notebook is written from a bank-desk perspective where fees are ignored.
We cannot ignore a fee that is 12–22× the spread. Adding it:

```
profit rate(δ) = (δ − f) · A · e^(−k·δ)          f = all-in maker fee

d/dδ = 0   →   A·e^(−k·δ)·[1 − k(δ − f)] = 0

                    δ* = f + 1/k
```

**Quote at the fee plus one over the liquidity-decay constant.** This is the
single formula the whole strategy hangs on, and both parameters are
measurable:

- `f` — known exactly (published rate + builder, reconciled against our fills).
- `k` — **calibrated from our own fill data**: regress log(fill rate) on
  quote distance δ. We already persist every fill with its price, so the
  distance-from-mid at placement is recoverable.

Note the immediate consequence: **δ\* > f always**. A quote closer to fair
value than the fee can never be profitable, which is a hard floor the current
implementation does not have.

### 2.3 Inventory skew (what the notebook implies, made explicit)

Quote center is not θ but a **reservation price** shifted against inventory:

```
r = θ − q · γ · σ²          q = signed inventory (base units)
                            γ = risk aversion
                            σ² = variance of θ over the holding horizon
bid = r − δ*,   ask = r + δ*
```

Long inventory (q > 0) lowers both quotes: the ask becomes easier to hit
(shedding risk) and the bid harder (stop accumulating). This is the standard
Avellaneda–Stoikov reservation price and it is the mechanism the current
`directional_bias` is a static, non-adaptive imitation of.

`σ` is directly available from the 1m candles already used elsewhere.

### 2.4 Where the notebook does NOT transfer — adverse selection

The dice market has **no information**: the buyer and seller are equally
likely and equally uninformed, so `λ(δ) = A·e^{−kδ}` is exogenous. A perp DEX
is not that market. The fill you receive at distance δ is disproportionately
from someone who knows the price is about to move — you are filled precisely
when you did not want to be.

This is the difference between a model that backtests beautifully and a bot
that bleeds. v2 must therefore measure **mark-out**:

```
markout(h) = side · (θ(t+h) − fill_price) / θ(t)      h ∈ {1s, 5s, 30s}
```

If average mark-out at 5 s is more negative than the captured edge, the quotes
are being picked off and δ must widen (or that market must be dropped). **No
market-making system should run without this measurement**, and it is exactly
what the current Mid Mode cannot tell you.

---

## 3. What Nado actually supports (verified)

| Need | Mechanism | Status |
| --- | --- | --- |
| Post-only quoting | `OrderType.POST_ONLY` | ✅ verified in SDK |
| **Atomic requote** | **`cancel_and_place`** | ✅ in SDK, **not yet wrapped** in `nado_client` |
| Batch cancel / cancel-all | `cancel_orders`, `cancel_product_orders` | ✅ |
| Full book depth | `get_market_liquidity(product_id, depth)` | ✅ probed (15 levels) |
| Public trade tape | `GET /v2/trades?ticker_id=` | ✅ probed (dedupe by `trade_id` — each match appears twice) |
| Spread / 24h volume per market | `GET /v2/tickers` | ✅ probed |
| Fair-value anchor | `get_oracle_prices`, `get_perp_prices` | ✅ present |
| Inventory, fast | `fill` WS stream (`venue/nado_ws.py`) | ✅ account-scoped |
| Inventory, reconcile | `get_all_positions` / account summary | ⚠️ currently failing in prod |
| Funding (carry on held inventory) | `get_perp_funding_rate(s)` | ✅ (signed DAILY rate) |
| Quote auto-expiry | `get_expiration_timestamp(seconds)` | ✅ |

**Not available — do not design around it:**
- **No public book/trade websocket.** Book and tape are **poll-only**, so quote
  reaction is floored by poll cadence and gateway budget.
- No native iceberg/hidden orders; the order-type enum is exactly
  `{DEFAULT, IOC, FOK, POST_ONLY}`.
- No venue-side MM/algo primitives.

---

## 4. Architecture

```
                    MidModeController  (per user, per product)
                              │
     ┌────────────────────────┼─────────────────────────┐
     ▼                        ▼                         ▼
┌──────────────┐   ┌────────────────────┐   ┌─────────────────────┐
│ FAIR VALUE   │   │  QUOTE ENGINE      │   │  RISK / ELIGIBILITY │
│ θ estimator  │──▶│  δ* = f + 1/k      │◀──│  spread > 2f gate   │
│ microprice + │   │  r = θ − qγσ²      │   │  inventory cap      │
│ oracle blend │   │  skew + widen      │   │  session PnL stop   │
└──────────────┘   └─────────┬──────────┘   └─────────────────────┘
       ▲                     │
       │                     ▼
┌──────────────┐   ┌────────────────────┐
│ MARKET DATA  │   │  QUOTE MANAGER     │
│ book (poll)  │   │  queue-aware:      │
│ tape (poll)  │   │  amend ONLY when   │
│ candles → σ  │   │  materially stale  │
└──────────────┘   │  cancel_and_place  │
                   └─────────┬──────────┘
                             │
                   ┌─────────▼──────────┐      ┌──────────────────┐
                   │  FILL HANDLER      │─────▶│  CALIBRATION     │
                   │  WS-driven, owns   │      │  k, A from fills │
                   │  inventory truth   │      │  MARK-OUT monitor│
                   └────────────────────┘      └──────────────────┘
```

### 4.1 Fair value — `θ`

Not the raw book mid. Blend:

1. **Microprice** — depth-weighted: `(bid·askSize + ask·bidSize)/(bidSize+askSize)`.
   On a lopsided book this is materially better than the midpoint and is the
   single cheapest upgrade available (we already fetch full depth).
2. **Oracle price** as an anchor, to catch a locally distorted book.
3. Reject the tick entirely if book and oracle disagree beyond a threshold —
   a stale or crossed book must not produce quotes.

### 4.2 Quote engine

```
δ*        = f + 1/k                       (fee-aware optimum, §2.2)
δ_floor   = max(δ*, f + min_edge_bp)      hard floor: never quote inside the fee
r         = θ − q·γ·σ²                    inventory skew, §2.3
bid       = min(r − δ, best_bid + tick)   never cross; may improve the touch
ask       = max(r + δ, best_ask − tick)
```

Widen δ (or go one-sided) when: mark-out is deteriorating, |q| is near the
cap, realized σ spikes, or funding makes holding the inventory expensive.

### 4.3 Quote manager — queue position is the product

The single biggest behavioural change from today:

- **Do not re-place a quote that is still good.** Amend only when the target
  moved by more than a hysteresis band (e.g. > ½ tick, or δ drifted > X bp).
  The current code re-places nearly every cycle and forfeits its queue slot.
- Use **`cancel_and_place`** so there is no window with no live order (needs a
  thin wrapper in `nado_client` — it is in the SDK but unwrapped today).
- Attach an **expiration** so an orphaned quote dies on its own.

### 4.4 Calibration + mark-out

- **k, A**: regress fill counts against quote distance from our own fill
  history, per product, rolling. Until enough data exists, use a conservative
  seed δ and *log* the implied k so the first sessions are the calibration.
- **Mark-out**: for every fill, record θ at +1 s/+5 s/+30 s. Report average
  mark-out vs captured edge. This is the number that says whether the strategy
  works — and it belongs on `/status`, not in a notebook.

### 4.5 Risk

- **Eligibility gate (new, and the highest-value control):** at start, measure
  the live spread; if `spread < 2 × f`, **refuse to start** and tell the user
  why, naming markets that do qualify. This alone would have prevented every
  losing BTC-PERP session.
- Inventory cap in base units, coherent with quote size (fixes the
  `$330 cap < $1,000 order` incoherence).
- Session PnL stop, net of fees, on the controller's own fill book — the
  live-session rail is unreliable here (it is perp-scoped, but the sync is
  failing in prod).
- Funding-aware: a long inventory paying funding has a carry cost that must be
  added to the effective fee when deciding δ.

---

## 5. The latency problem — prerequisite, not an afterthought

Nothing in §4 works on a 6–13 s loop. Before any pricing work:

1. Find why a mid cycle takes seconds (the logs show `place_order` calls
   ~1.5–2 s apart inside one cycle — likely sequential blocking SDK calls).
2. Quote refresh must be driven by **market data change and own fills**, not by
   the generic strategy scheduler.
3. Target: sub-second quote reaction. If that is not achievable within the
   current runtime, Mid Mode should quote **wider and less often** by design —
   an honest slow maker on a wide market beats a fast maker that is actually
   slow.

**This is the highest-risk unknown in the plan.** I would not commit to the
full v2 until we know what the 6–13 s is made of.

---

## 6. Phasing

| Phase | Deliverable | Why this order |
| --- | --- | --- |
| **0** ✅ | **Stop destroying queue position** — amend only when the target moved materially; never re-place a still-good quote | On a one-tick book this is the entire fill rate. Biggest win, smallest diff |
| **1** | **Latency diagnosis** — what is the 6–13 s made of? | A maker at the touch that reprices in 10 s is adversely selected by construction |
| **2** ✅ | **Position scaling ladder** (§1) for Mid **and** fill-anchored Grid, with level-count clamp + inventory cap | The feature you asked for; fixes the dead `levels` input |
| **3** | Cost transparency: cost per $1M of volume on the start card, mirroring the Vol bot fee card | It is a volume product with a known price — say the price |
| **4** | Microprice fair value + mark-out logging (§2, §4.4) | Makes quality measurable before tuning |
| **5** | Inventory skew (reservation price) | Turns the ladder from static to adaptive |

Phases 0 and 2 are the ones that address what you actually reported.

### 6.1 What shipped (2026-08-02)

**Phase 0 — queue preservation.** A resting quote is now kept when both hold:
it is **not behind the touch** (the venue BBO includes our own order, so being
at the best price on our side means we *are* the touch), and the new target is
**not a better price for us**. The second condition is what stops the rule from
becoming "never re-quote": backing a bid off to buy cheaper is still worth the
queue slot, and so is chasing once we have been outbid. What it refuses is the
opposite trade — paying more while also going to the back of the queue — which
is exactly what the improve-by-a-tick rule computes once our own quote becomes
the BBO. A `min_quote_lifetime_s` (≈2 cycles, derived from the strategy's real
cadence, capped at 30 s) bounds churn on top of that. Safety cancels —
inventory ceiling, exposure cap, regime gate — are evaluated *before* the hold
rules and are never delayed.

**Venue-contention hold (2026-09-02).** After the queue rules, an opening-side
re-quote is also held while the venue is denying our status reads this cycle
(`adapter.reads_throttled_this_cycle() > 0`): a re-quote is cancel+place
against the same budget, and churning it only deepens the contention. The hold
never outlives `max_quote_lifetime_s` (a profile without one is bounded by
`NADO_THROTTLE_HOLD_MAX_SECONDS`), never applies to the reducing side or to an
empty level, and never touches the stop path. A stop that ends FAILED with its
order still resting keeps the level bound across cycles — each cycle retries
the stop — until the order is confirmed terminal (AUDIT-DENY-2026-09-02-F2b).

**Phase 2 — the ladder.** `quant/ladder.py` is a pure planner: deployment in,
`(offset_bp, size_quote)` levels out, sizes summing exactly to the deployment
with the rounding remainder on the *deepest* level so the near-touch level is
never inflated above plan. Both `MarketMakingController` (Mid) and
`FillAnchoredQuotingController` (Grid) now track N quote slots per side instead
of one, keyed `(side, level)`; level 0 keeps the original attribute names so the
live-resize path, dashboards and existing tests are unaffected.

The property that makes this safe to enable by default: **the ladder
redistributes a side's deployment, it never adds to it.** `order_amount_quote`
became the per-*side* total, so `levels=1` is the shipped single-quote
behaviour bit for bit, and `levels=N` places the same total notional in N
pieces. That is also the answer to the martingale worry about the geometric
curve — the curve changes where size sits, not how much there is.

Three things this changed that the tests did not initially catch, all fixed:

- **rgrid is excluded.** It rests ONE post-only quote per break rather than a
  ladder, and it sizes that quote from `order_amount_quote` — the new per-side
  meaning would have multiplied its step size by `levels`. It keeps
  `deployed / levels` and `ladder_levels = 1`. (This said "fires one taker per
  break" until 2026-08-10; R-Grid has been maker-first since `a9a4d2a` — only its
  risk exits cross, as bounded marketable limits.)
- **Grid's stall-escalation concession** compares stuck exposure against "one
  quote's notional". That now divides by the level count, or the safety valve
  would have needed `levels`× more stuck exposure before firing.
- **`max_levels(deployed, 0)`** returned 1, which would have collapsed every
  ladder on any adapter reporting no minimum — the same silent-failure shape
  that killed the `levels` input the first time. No floor now means no clamp.

`ProductMeta.min_notional` is populated from `min_size_x18`; despite the name
that field carries a **quote notional** ($5 on a perp, $100 on KBTC spot per
the catalog tests), so the level-count clamp reads it correctly.

## 7. Honest assessment

- **As a volume product, Mid on BTC-PERP costs ~$352 per $1M** and is our
  cheapest turnover engine. The economics are sound; the execution is not.
- **Spread-capture profit remains impossible on the majors** (BTC/ETH/SOL fail
  by 25–45×). If anyone ever asks Mid to be profitable rather than
  volume-generating, that is a different product on different markets
  (PUMP/SPCX) — do not conflate the two objectives again.
- The binding constraint on BTC is **queue priority**, not fees. Every cancel
  costs a place in a $217k line.
- **Withdrawn:** my earlier "move Mid to wide markets" recommendation. That was
  measured against a profit objective. As a volume product Mid belongs exactly
  where it is — tight, deep, heavily traded books.
- **If the requirement is "buy low, sell high on BTC", that is not Mid Mode at
  all** — it is the fill-anchored grid we already ship (§0.4). Building it
  again inside Mid would produce two names for one product. Point the user
  there, harden it, and keep Mid as the wide-market spread capturer.
- The parameters that decide success — `k` and mark-out — **are not known
  today**. Phase 2 exists to learn them before we tune anything.
