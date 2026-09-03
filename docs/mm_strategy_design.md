# Nadobro Market Making Strategy — Research & Design Doc

Author: Nadobro dev  ·  Target venue: **Nado DEX perps (on-chain)**  ·  Status: Proposal v1

## 1. Objective

Build a profitable, inventory-aware perpetuals market maker on top of the existing Nadobro bot framework. The deliverable is a new `AS_MM` (Avellaneda–Stoikov Market Maker) strategy that coexists with the current `GRID` and `RGRID` strategies, shares their runtime / rate-limit / risk plumbing, but quotes continuously using an economically derived reservation price and spread rather than a fixed grid.

Profitability for a passive maker comes from three sources, in order of importance on Nado:

1. **Maker rebates.** Nado pays a maker rebate of up to **−0.8 bp** and charges takers **1.5 bp**, so every round-turn a true maker captures 2.3 bp of edge before slippage vs a taker. That is the economic reason this strategy exists, and it is what the quoting logic must protect.
2. **Spread capture (half-spread × fill rate × 2).** The expected dollar PnL per filled round-turn is `2 × δ × S × q` where `δ` is the half-spread in price units, `S` the mid, and `q` the filled size. This only realizes if we quote tight enough to fill but wide enough to not get adversely selected.
3. **Inventory-aware risk premium.** Holding an unhedged inventory `q` on a perp has drift exposure `γ · σ² · q` over the horizon `T`. A principled MM must price this into the quotes, which is exactly what Avellaneda–Stoikov formalizes.

Funding PnL is a fourth, opportunistic source — we *do not* build the strategy around it, but we make the bot aware of the funding sign so it can skew quotes when funding is paying our current inventory direction.

## 2. Literature Review and Candidate Selection

Four families of MM models dominate the literature. Below is a concise critique from the perspective of an on-chain perp venue with second-grade (not microsecond-grade) latency, a central limit order book, explicit maker rebates, and funding accrual.

### 2.1 Avellaneda–Stoikov (2008)

The canonical model. Solves a stochastic-control HJB where the MM chooses bid/ask quotes to maximize CARA-utility of terminal wealth against Poisson fills and Brownian mid-price. The closed-form is

$$
r(t) = S(t) - q \cdot \gamma \cdot \sigma^2 \cdot (T - t)
$$

$$
\delta^{a} + \delta^{b} = \gamma \cdot \sigma^2 \cdot (T - t) + \frac{2}{\gamma} \ln\left(1 + \frac{\gamma}{k}\right)
$$

Here `r` is the reservation price (the MM's indifference price given current inventory `q`), `S` is mid, `γ` is the risk aversion, `σ` is the mid-price volatility, `k` is the order-flow intensity decay constant, and `T` is the end-of-horizon time. The ask quote is `r + δᵃ` and the bid is `r − δᵇ` where the split between `δᵃ` and `δᵇ` comes from the reservation skew. This is the right starting point for Nado: the math is clean, it produces inventory-aware quoting, and it degrades gracefully under bad parameter estimates.

### 2.2 GLFT (Guéant–Lehalle–Fernandez-Tapia, 2013)

A refinement that handles finite inventory bounds `Q` and asymmetric fill intensities. Produces a quasi-closed-form skew-and-spread that stays stable when inventory hits the bounds (AS diverges at high `|q|` in long horizons). The quoting is more robust for venues where inventory limits bind often — which is exactly what happens with a small account on a perp. We adopt its **stationary / infinite-horizon** quoting form as the default, falling back to AS-with-horizon only when the user opts into a session-bound mode.

Stationary-regime closed form (Guéant 2017):

$$
\delta^{b,a}(q) = \frac{1}{k} + \frac{2q \pm 1}{2} \cdot \sqrt{\frac{\sigma^2 \gamma}{2 k A} \cdot \left(1 + \frac{\gamma}{2k}\right)^{2 + \frac{k}{\gamma}}}
$$

where `A` is the fill-intensity scale (fills/sec at zero spread) and `k` is its decay with distance from mid. This is what we actually implement; AS is presented for intuition.

### 2.3 Inventory-Skew Heuristics (Stoikov–Sağlam, industry variants)

A simpler class of models: quote a constant base spread, skew the mid by a linear function of inventory, widen when volatility spikes. Pragmatic, fast to tune, and already partially embodied in the current `mm_bot.py` via the `directional_bias` and volatility-widening logic. We don't want to throw this away — it's the fallback the strategy reverts to when vol estimation fails.

### 2.4 Cartea–Jaimungal (Algorithmic & High-Frequency Trading, 2015)

Extends AS/GLFT with adverse-selection penalties and market-order impact. Very elegant, but requires a reliable estimate of fill-informedness (the probability that a fill is followed by an adverse mid move). We currently can't estimate that well on Nado without a historical trade archive, so we park this as a phase-3 upgrade.

### 2.5 Recommendation

Implement **GLFT-stationary** as the quoting core, with an Avellaneda-style finite-horizon mode as a session-bound variant, and keep the inventory-skew heuristic as a hard fallback. Adverse selection and impact modeling are deferred to phase 3.

## 3. Nado-Specific Design Constraints

Several Nado platform characteristics shape the implementation and must be accounted for in the spec rather than treated as afterthoughts.

**Order types.** Nado supports default (GTC), IOC, FOK, post-only, reduce-only, and trigger orders via the appendix bit-layout (order_type bits 9–10, post_only=3, reduce_only=bit 11, isolated=bit 8). All MM quotes must be posted with `post_only=true` — a quote that crosses the book turns the strategy from maker (−0.8 bp rebate) to taker (+1.5 bp fee), a 2.3 bp per-fill swing that destroys edge. The existing client's `place_limit_order` with `post_only=True` sets order_type_int=3 and is the correct primitive.

**Book-crossing retries.** On Nado, a post-only that would cross is rejected (not silently converted to a limit). The existing `_reprice_post_only_quote` helper in `mm_bot.py` already handles this with a 3-attempt widening ladder (0.5 / 1.0 / 2.0 bp). We reuse it.

**Rate limits.** 600 places/minute with spot leverage, 30/min without, 500 open orders per subaccount per market. At our expected cadence (re-quote every 1–3 seconds, 2-4 quote levels per side) we are well under the place ceiling, but we must cancel-before-replace rather than stacking new quotes on top of stale ones. This is implemented as "quote → wait for fill or stale → cancel → re-quote."

**Funding.** Hourly TWAP(mark) − TWAP(spot_index) / 24, capped at 2% daily. The MM should consume funding rate as an exogenous signal: when funding is paying our current inventory direction (long with negative funding, short with positive) we widen the opposite-side quote (we're OK holding more inventory) and tighten the same-side quote (we want to lean into the paying side). This is a small skew, not a directional bet.

**Fee structure.** Taker 1.5 bp, maker rebate up to −0.8 bp. The minimum profitable half-spread at zero inventory risk is therefore `(1.5 − (−0.8)) / 2 = 1.15 bp` per side round-trip, plus a buffer for slippage and adverse selection. We hard-floor the minimum quoted half-spread at 1.5 bp per side (3 bp round-trip) in config.

**Tick and size increments.** Per the Nado client's `_price_increment_cache` and `_size_increment_cache`. All quote prices must be rounded to `price_increment`, all sizes to `size_increment`, and sizes must meet `min_size_x18_cache`. We reuse those helpers.

**Isolated margin.** Phase 2 deliverable: allow the MM to run on an isolated child subaccount so a blow-up on BTC-PERP doesn't eat ETH-PERP collateral. Phase 1 runs on the default (parent) subaccount with cross margin.

## 4. Math Specification (GLFT-stationary)

Let `S_t` be the Nado mid price, `q_t` the signed perp position in base units, `γ` the risk-aversion parameter, `σ` the mid-price volatility estimate (annualized), `A` the fill-intensity at zero spread (fills/sec/side), `k` the fill-intensity decay per unit price-distance from mid, `Δ_tick` the Nado price increment, `F_t` the next-hour predicted funding rate (signed, annualized), and `b_fund` a funding-skew coefficient.

**Reservation price** (inventory-skewed mid):

$$
r_t = S_t - q_t \cdot \gamma \cdot \sigma^2 \cdot \tau
$$

For stationary-regime use `τ = 1 / γ` (Guéant's equivalence); for session-bound use `τ = T − t` with `T` the session end.

**Half-spreads**:

$$
\delta^{b}_t = \frac{1}{k} - \left(q_t - \tfrac{1}{2}\right) \cdot \psi
$$

$$
\delta^{a}_t = \frac{1}{k} + \left(q_t + \tfrac{1}{2}\right) \cdot \psi
$$

with

$$
\psi = \sqrt{\frac{\sigma^2 \gamma}{2 k A}} \cdot \left(1 + \frac{\gamma}{2 k}\right)^{1 + \frac{k}{2 \gamma}}
$$

**Quote prices**:

$$
P^{b}_t = \mathrm{round\_to\_tick}\left(\min(r_t - \delta^{b}_t,\ \mathrm{best\_bid}_t)\right)
$$

$$
P^{a}_t = \mathrm{round\_to\_tick}\left(\max(r_t + \delta^{a}_t,\ \mathrm{best\_ask}_t)\right)
$$

The `min(·, best_bid)` / `max(·, best_ask)` clamp enforces post-only; combined with the `_reprice_post_only_quote` widening ladder, it guarantees we never submit a crossing quote.

**Funding skew** (additive to reservation, bounded):

$$
r_t \leftarrow r_t - \mathrm{clip}(b_{\text{fund}} \cdot F_t,\ -\delta^{b}_t / 2,\ +\delta^{a}_t / 2)
$$

Bounded so funding can tilt quotes within the spread but never flip the sign of inventory skew.

**Volatility estimator**. EWMA of squared log-returns at 5-second cadence, half-life 5 min:

$$
\hat{\sigma}^2_t = \lambda \hat{\sigma}^2_{t-1} + (1-\lambda) \cdot r_t^2,\quad \lambda = 0.99
$$

with `r_t = ln(S_t / S_{t-1})`. Annualize by `σ̂ · √(secs_per_year / 5)`.

**Fill-intensity `k` and `A` estimator**. Fit online from fill distances: for every fill observed at half-distance `δ` from mid at time of submission, bin the inverse-time-to-fill, and fit `λ(δ) = A · exp(−k · δ)` with a rolling 500-fill window. Phase 1 uses sane defaults: `A = 1.0 /s`, `k = 1.5 / bp`. Phase 2 replaces with online fit.

**Risk-aversion `γ`**. A user-facing parameter in the bot config, defaulted to `γ = 0.1`. Higher `γ` narrows inventory bounds and widens spread; lower `γ` lets inventory run further.

## 5. Risk Controls

The quoting math is half the strategy; the other half is the hard bounds that keep a bad parameter estimate from becoming a liquidation. These sit *outside* the quoting math as short-circuits.

**Inventory cap `Q_max`**. When `|q_t| ≥ Q_max`, suppress the same-side quote entirely (don't add to the position) and hold only the mean-reverting side. Default `Q_max = cycle_notional / S_t · leverage_cap`.

**Drawdown circuit breaker**. Reuse `mm_bot.py`'s cycle-pnl circuit breaker, unified by the recent fix. If session realized PnL falls below `−sl_pct × margin`, stop the strategy and cancel all open MM quotes. This already exists for GRID/RGRID and we hook into the same code path.

**Volatility kill-switch**. If `σ̂` exceeds `σ_max` (default 10× the rolling 24h mean), cancel all quotes and pause for a cool-down interval. Avellaneda-style quotes widen under high vol by construction, but in a vol *explosion* the model's assumption of a diffusion mid breaks down — better to step out.

**Funding cap**. If `|F_t| > 1.5% /day` (within 25% of the 2% daily cap), halve `Q_max` on the paying side. Protects against funding being a leading indicator of a manipulated mark price.

**Max placement rate**. Self-throttle to 5 re-quotes per second per side per market, leaving 100× headroom under the 600/min Nado limit. Implemented with a token-bucket on the `execute_limit_order` call.

**Venue-contention hold (2026-09-02)**. When a status poll in the current cycle was budget-denied by our own gateway (the venue's per-user query budget is contended), an *opening-side* re-quote is held instead of cancel+replaced — a re-quote spends the same execute budget that is already starved, and deepens the contention that produced the hold. Reducing-side re-quotes, fresh quotes on empty levels, safety cancels, and the stop path are never held. The hold is time-bounded: a profile's `max_quote_lifetime_s` still wins, and a profile without one is bounded by `NADO_THROTTLE_HOLD_MAX_SECONDS` (120s). Ladder re-centers (grid / dgrid) apply the same rule using the *previous* cycle's denials, because they are decided ahead of the executor ticks.

**Stale-quote watchdog**. If a posted quote has rested > `stale_seconds` (default 8s) AND mid has moved > `max_stale_drift_bp` (default `0.5 × spread`), cancel and re-quote. Prevents the bot from being picked off by latency arbs after a price move. The existing `_update_reference_price` / `STALE_DRIFT_MULTIPLIER` plumbing in `mm_bot.py` already does this; we reuse it.

**Reduce-only exit**. When `running=False` or circuit breaker fires, all inventory is flattened via reduce-only IOC. Same primitive as the volume_bot escalation path.

## 6. Integration Plan with Nadobro

The strategy plugs in exactly where `GRID` and `RGRID` do today — as a new strategy module under `src/nadobro/strategies/`. Everything below reuses existing infra rather than rebuilding it.

### 6.1 New file: `src/nadobro/strategies/as_mm_bot.py`

Single-entry `run_cycle(telegram_id, network, state, **kwargs) -> dict` matching the GRID/RGRID contract. State machine roughly:

- `idle` → compute `r_t, δᵇ, δᵃ`, cancel any live quotes, post new bid and ask → `quoting`
- `quoting` → on every tick, refresh `r_t` and deltas; if drift > threshold OR fill observed, cancel-and-repost; if inventory cap breached, suppress a side; update EWMA vol & fill intensity.

Each tick returns the same shape as `volume_bot.run_cycle` so the runtime treats it uniformly. This makes orchestration trivial and keeps the Telegram UI unchanged.

### 6.2 Config additions (`src/nadobro/config.py`)

New block `MM_AS_DEFAULTS` with `gamma`, `sigma_halflife_sec`, `fill_intensity_A`, `fill_intensity_k`, `inv_cap_notional_mult`, `min_half_spread_bp = 1.5`, `stale_seconds = 8`, `stale_drift_bp = 2.0`, `funding_skew_coef`, `sigma_kill_mult = 10.0`, `requote_throttle_hz = 5`.

### 6.3 Reused infrastructure

`venue/nado_client.py` — `place_limit_order(post_only=True, reduce_only=...)`, `cancel_order`, `get_open_orders`, `get_market_price`, `_price_increment_cache`, `_size_increment_cache`. No changes needed.

`trading/trade_service.py` — `execute_limit_order(post_only=True)` for the happy path, `execute_market_order(reduce_only=True)` for emergency flatten. No changes needed.

`venue/nado_archive.py` — `query_order_by_digest` to confirm fills and realized PnL, `query_matches_by_subaccount` for the online fill-intensity estimator. No changes needed.

`strategies/mm_bot.py` helpers — `_update_both_emas`, `_reprice_post_only_quote` (renamed to `reprice_post_only_quote` and promoted to a shared module), tick-aware dedupe via `_price_increment_cache`. Moving these into a shared `strategies/_quoting_utils.py` is a mechanical refactor in phase 1.

### 6.4 UI wiring (`handlers/strategy_handler.py` or equivalent)

Add an `AS_MM` entry in the strategy picker with sliders/text-inputs for the user-facing knobs: `risk_aversion (γ)`, `inventory_cap_usd`, `min_half_spread_bp`, `session_or_stationary`. Everything else is internal.

### 6.5 Telemetry

Log per-cycle: `r_t`, `δᵇ`, `δᵃ`, inventory `q`, `σ̂`, estimated `A` and `k`, quotes_placed, quotes_cancelled, fills_received, cycle_pnl, cumulative_rebate_earned. Emit a compact single-line INFO log (same style as `mm_bot.py`) per cycle for grep-ability, plus structured metrics into `metrics/` if the repo already has a metrics sink.

## 7. Rollout Phases

**Phase 1 — MVP on testnet (1 week).** GLFT-stationary with hard-coded `A = 1.0, k = 1.5/bp`, single market (BTC-PERP), cross margin, single quote level per side, hard inventory cap. Goal: verify fill mechanics, verify no post-only crosses, verify the PnL attribution (rebate vs spread vs inventory P&L vs funding) reconciles against the archive indexer.

**Phase 2 — Mainnet small-size (2 weeks).** Same parameters as phase 1, capped at `$500` notional per side, run on an isolated child subaccount so failure is contained. Add online fill-intensity fit. Add funding-skew term. Roll out to ETH-PERP and SOL-PERP. Goal: show positive session PnL attributable to rebate + spread capture after fees across ≥ 5 uninterrupted 24h sessions.

**Phase 3 — Multi-level quotes + adverse-selection pricing (2 weeks).** Ladder quoting (2–4 levels per side with a geometric spacing `β = 1.4`), Cartea–Jaimungal adverse-selection penalty, and opportunistic inventory unwind via reduce-only IOC when an edge widens past a threshold. Goal: 2× gross volume for same realized PnL per unit-size, proving that depth is earning rebate.

**Phase 4 — Cross-market hedging (stretch).** Hedge BTC-PERP inventory on spot KBTC (or vice-versa) when the basis is wide. Requires the existing spot client plumbing to be generalized. Explicitly out of scope for V1.

## 8. Parameter Choices and Their Sensitivity

A short note on what will and won't matter in practice:

`γ` (risk aversion) is the single most important knob. Tripling `γ` roughly triples the inventory-skew term and cuts max carried inventory by ~3x while narrowing the spread slightly. Default 0.1 is conservative; production tuning will likely land in [0.05, 0.3] depending on market.

`k` (fill-intensity decay) matters mostly through `1/k` — the no-inventory half-spread. Setting `k` too low overquotes tight (adverse selection), too high overquotes wide (no fills). The online fit in phase 2 is what makes this robust.

`σ` is self-correcting: the formula widens spreads linearly with `σ` and the inventory penalty grows with `σ²`. A volatility estimate that's 2× too high simply costs us fill rate but not safety; 2× too low costs us adverse selection. Both are tolerable at phase-1 size.

`A` (base fill intensity) is just a scale factor in `ψ`. Getting it wrong shifts the spread by a constant multiplier; the online fit calibrates it.

## 9. What This Does *Not* Do

Out of scope for V1, and deliberate:

No order-book shape modeling beyond the top of book. Quoting is a single level per side in phase 1, with depth added in phase 3.

No latency racing. We are not a HFT; our edge is the maker rebate plus mean reversion, not queue position vs sub-millisecond competitors. On a CEX this model would get run over; on Nado's on-chain engine the quoting cadence we need (~seconds) is inside the venue's own execution envelope.

No alpha. We take no directional view. Any short-term signal that could improve the reservation price — e.g., funding-rate momentum, spot-perp basis, Nado's WS trade tape — is additive and lives in phase 3+ as a *skew* on the reservation price, not a replacement for it.

No cross-market arb. We do not try to be a basis arb bot. If it wants to hedge BTC-PERP on spot KBTC that's phase 4; until then inventory risk is held outright.

---

## Appendix A — Pseudocode for the core quote step

```python
def run_cycle(telegram_id, network, state, client, product, **kw):
    product_id = get_product_id(product, network=network)
    mp = client.get_market_price(product_id)
    mid = float(mp["mid"])
    best_bid = float(mp.get("bid") or mid)
    best_ask = float(mp.get("ask") or mid)

    # Inventory
    pos = _load_position(client, product_id)
    q = signed_base_size(pos)  # >0 long, <0 short

    # Vol (EWMA)
    sigma2 = update_ewma_sigma2(state, mid)

    # Params
    gamma = state["as_gamma"]
    A = state["as_fill_A"]
    k = state["as_fill_k"]
    tau = 1.0 / gamma  # stationary

    # GLFT
    psi = math.sqrt(sigma2 * gamma / (2 * k * A)) * \
          (1 + gamma / (2 * k)) ** (1 + k / (2 * gamma))
    delta_b = 1/k - (q - 0.5) * psi
    delta_a = 1/k + (q + 0.5) * psi
    # enforce fee-floor
    delta_b = max(delta_b, MIN_HALF_SPREAD_BP * mid / 1e4)
    delta_a = max(delta_a, MIN_HALF_SPREAD_BP * mid / 1e4)

    # Reservation price w/ inventory skew + funding skew
    r = mid - q * gamma * sigma2 * tau
    r += funding_skew(state, delta_b, delta_a)

    # Quote prices (post-only safe)
    pb = round_to_tick(min(r - delta_b, best_bid), product_id, client)
    pa = round_to_tick(max(r + delta_a, best_ask), product_id, client)

    # Size per side
    size = state["quote_size_base"]

    # Inventory cap
    if abs(q) >= state["inv_cap"]:
        # Suppress add-side, keep reduce-side
        if q > 0:
            pb = None
        else:
            pa = None

    # Cancel-before-replace
    cancel_stale_quotes(client, product_id, state)

    # Place
    if pb is not None:
        resp = execute_limit_order(
            telegram_id, product, size, pb,
            is_long=True, leverage=state["leverage"],
            post_only=True, reduce_only=False,
            enforce_rate_limit=False, source="as_mm",
            strategy_session_id=state["strategy_session_id"],
        )
        if resp["success"]:
            state["bid_digest"] = resp["digest"]
            state["bid_posted_ts"] = time.time()

    if pa is not None:
        resp = execute_limit_order(
            telegram_id, product, size, pa,
            is_long=False, leverage=state["leverage"],
            post_only=True, reduce_only=False,
            enforce_rate_limit=False, source="as_mm",
            strategy_session_id=state["strategy_session_id"],
        )
        if resp["success"]:
            state["ask_digest"] = resp["digest"]
            state["ask_posted_ts"] = time.time()

    return {"success": True, "done": False, "action": "as_mm_requoted",
            "orders_placed": int(pb is not None) + int(pa is not None),
            "mid": mid, "r": r, "delta_b": delta_b, "delta_a": delta_a,
            "inventory_base": q, "sigma2": sigma2}
```

## Appendix B — Expected PnL Decomposition

Round-turn expected PnL per fill (base units `q`, round-turn `Δq = q`):

    E[PnL] = (δᵇ + δᵃ) · q                     # spread capture
           + rebate_bp × 2 × q × S / 1e4        # maker rebate, both legs
           − inventory_variance_cost            # γ · σ² · q² · Δt
           + funding · q · Δt_hours / 24        # funding, signed

With conservative defaults on BTC-PERP (`S ≈ 70k, σ ≈ 0.5/year, γ = 0.1, k = 1.5/bp, A = 1.0`) the first two terms sum to `~2 bp × notional` per round-turn, the third eats `~0.3 bp × notional × sqrt(dt_min)`, and funding is `~±0.01 bp/min`. At 100 round-turns per hour per $500 notional, gross edge ≈ `2 bp × 500 × 100 = $10/hr` before inventory cost; after `~2.5 bp / hour inventory variance cost × 500 = $1.25/hr`, net ~`$8/hr`. This is the number the phase-2 testnet results need to clear for V1 to be a ship.
