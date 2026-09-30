# Arcus G1: owner testnet probe checklist

**Who runs it:** the owner, on the owner's own machine. An agent never runs
`scripts/arcus_testnet_probe.py` and never handles an Arcus key.
**When:** after the Arcus P2 library merges, and before P6a (the adapter) starts.
The `sign-check` result gates P6a. The other results set constants that later
phases need (see §7).
**Where the rules come from:** `02_client_library.md` §11.2 and §13.2 of the
Arcus build specs, plus `06_adapter_runtime.md` §15.1 for the three reduce-only
and ALO-cross steps.

The probe is **testnet only**. It refuses mainnet, refuses a testnet URL
override that points anywhere other than `api.testnet.arcus.xyz`, and refuses to
run on a Fly machine. It reads the signing key only from the environment and
never prints, logs or writes it.

---

## 0. Before you start

- [ ] The repo is at the merged commit and the worktree is clean. Each report
      records the git sha and a `dirty` flag.
- [ ] `.venv` is installed (`.venv/bin/python -m pytest -q tests/test_arcus_scripts.py` passes).
- [ ] You know your Arcus **testnet** address (`0x…`), and Arcus lets it trade on
      testnet.

## 1. Create the probe key (Arcus testnet web app)

1. Open the **API Keys** page and create a key:
   - **Name:** `nadobro-probe-YYYYMMDD`. Use a new, unique name. Creating a
     key with an existing name revokes the old key, so never reuse a name the
     bot or you still use, and never pick a `nadobro-xxxx` name.
   - **Subaccount:** 0
   - **Days Valid:** 30
   - Web-app keys are trade-only. The probe refuses any key that has the
     `withdraw` permission.
2. Copy the **API Signing Key** (64 hex characters). Do not paste it into chat,
   a file, or a command line.
3. Click **Testnet Deposit** to fund the account with about $1,000.

## 2. Optional: keyless shape capture (no key needed)

```
.venv/bin/python scripts/capture_arcus_shapes.py --network testnet --address 0x<YOUR_TESTNET_ADDRESS> --out /tmp/arcus_capture_testnet
```

This runs public GETs only. The saved files replace your address with
`0x…dead`, and replace the compliance country and region with `XX`. Copy files
into `tests/fixtures/arcus/captured/` only if you want to keep them.

## 3. Load the key into this shell only

```
cd <REPO>
git status --porcelain                        # must print nothing
export ARCUS_PROBE_NETWORK=testnet            # anything else is refused
export ARCUS_PROBE_ADDRESS=0x<YOUR_TESTNET_ADDRESS>
read -rs ARCUS_PROBE_SIGNING_KEY && export ARCUS_PROBE_SIGNING_KEY   # paste, press Enter: nothing echoes, nothing goes into shell history
OUT=tests/fixtures/arcus/g1
```

Each run first prints `probe key fingerprint xxxxxxxx`: the first 8 hex
characters of sha256(public key). It never prints the key itself. Before
placing anything, the probe checks that the key is listed `ACTIVE` for your
address, stays valid for more than 24 hours, covers subaccount 0, and has no
`withdraw` permission. It also checks that the account has been funded.

## 4. Non-trading runs

These runs place only resting post-only (ALO) orders. Each order is priced from
the **oracle** on the side of the book it cannot cross (5 % away by default),
and each one is cancelled.

```
.venv/bin/python scripts/arcus_testnet_probe.py all --market BTC-USD --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py oracle-band --market ETH-USD --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py oracle-band --market SOL-USD --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py open-order-cap --max 120 --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py ack-latency --n 50 --out $OUT
```

`all` runs these steps in order: `sign-check`, `ct-order`,
`ack-404-window --n 5`, `cancel-race --n 5`, `oracle-band`, the non-trading
half of `min-size`, `charged-400`, `default-leverage` and `ack-latency --n 20`.
It never trades, even if you pass `--i-understand-this-trades`.

**Gate:** in the `all_*.json` report, `results.sign-check.all_accepted` must be
`true` before P6a starts.

## 5. Trading runs (optional)

Each of these runs opens and closes its **own** tiny BTC-USD position, about
$8 (0.0001 BTC). `ioc-reduce-only` opens one twice.

Before trading, the probe refuses with exit 2 and trades nothing unless all of
these hold:
- your BTC-USD position is **flat**;
- both sides of the book are present;
- both the best bid and the best ask are within 500 bp of mark AND oracle.

Every close is a reduce-only order. Its protective price is checked to be
within 10 % of **mark** before it is sent.

```
.venv/bin/python scripts/arcus_testnet_probe.py tradeid-parity  --i-understand-this-trades --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py fee-sign        --i-understand-this-trades --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py entry-units     --i-understand-this-trades --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py ws-fresh        --i-understand-this-trades --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py min-size        --i-understand-this-trades --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py ioc-reduce-only --i-understand-this-trades --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py alo-reduce-only --i-understand-this-trades --out $OUT
.venv/bin/python scripts/arcus_testnet_probe.py alo-cross       --i-understand-this-trades --out $OUT
```

If the testnet book is far from the oracle (it has been 7–12 % away before),
these refuse with `taker preflight: …`. Retry later.

## 6. Remove the key, then start the 72-hour keyless pool watch

```
unset ARCUS_PROBE_SIGNING_KEY                 # BEFORE the long-running job
nohup .venv/bin/python scripts/arcus_testnet_probe.py pool-watch --interval 600 --hours 72 --out $OUT > /tmp/arcus_pool_watch.log 2>&1 &
```

`pool-watch` never reads the key. It also removes the key from its own process
environment if it finds one there. It reads `GET /v1/rateLimit` every 10 minutes
and writes `pool-watch_<utc>.jsonl` plus a summary report. Stop it early with
`kill <pid>` (SIGTERM is handled like Ctrl-C). It still writes its summary and
exits 0. Each sample is appended to the JSONL as it is taken, so nothing is
lost.

After the keyed runs in §4 and §5 finish, **delete the `nadobro-probe-YYYYMMDD`
key** in the web app. `pool-watch` does not need it.

## 7. Reading the reports

Each report is `$OUT/<subcommand>_<YYYYmmddTHHMMSSZ>.json`. Values sit under
`results`; for `all` they sit under `results.<step>`.

| Report | Key(s) | Sets / decides |
|---|---|---|
| `sign-check` | `all_accepted`, `verdicts`, `failed`, `inconclusive` | **G1 gate.** `true` means both signing schemes and the batch cancel are venue-verified. `failed` (a 401) means signing is wrong: stop and report it. `inconclusive` (a 400/403, e.g. `OracleDeviation` at 5 %) is not a verdict: re-run with `--far-bp 300`. |
| `ack-latency` | `recommend.ARCUS_ACK_GRACE_S`, `recommend.ARCUS_CANCEL_CONFIRM_S`, `recommend.ARCUS_CANCEL_WAIT_S` | ACK grace, cancel confirm and per-call cancel wait (3 × p99, 3 × p99, 2 × p99; floors 2 s / 2 s / 0.5 s). If `incomplete` is true, some WS events timed out. |
| `ack-404-window` | `first_200_ms`, `n_404_before_visible`, `open_orders_visible_ms` | How long an ACKed order can be absent (404) before it is visible |
| `cancel-race` | `summary` per delay | A6: `CANCELED` = a cancel that beat its placement was buffered; `OPEN_AFTER_NOT_FOUND` = it was not buffered. Ignore when `confounded` is true. |
| `ct-order` | `older_ct_after_newer`, `replayed_ct` | A16: does P6a need a per-key send lock? A rejected older `ct` means yes. |
| `oracle-band` (BTC, ETH, SOL) | `threshold_bp.buy` / `.sell`, `sides.*.result` | `ARCUS_ORACLE_DEVIATION_BP` per ticker (A4). Use the smaller side. `bracketed` = measured; `above_upper` = more than 2000 bp; `below_lower` / `inconclusive` / `n/a` = not measured, so re-run later. |
| `open-order-cap` | `cap`, `baseline_open`, `stop` | `ARCUS_OPEN_ORDER_CAP` (A4). `cap` counts every open order on the account, including your own. `>N` = the cap was not reached. |
| `min-size` | `below_min_size`, `below_min_notional`, `reduce_only_dust` | Reduce-only dust policy (A5). Each case runs on the market where it is unconfounded: BTC-USD for size, the first market whose minimum size is under $5 for notional. |
| `charged-400` | `order_used_delta` (after 3 rejected placements) | Are gateway 400s charged to the order pool? (A1 / D-17) |
| `default-leverage` | per ticker `leverage`, `margin_mode` | Default leverage and margin mode |
| `pool-watch` | `reseed_events` + the JSONL series | A1: does the pool reseed? |
| `ws-fresh` | `account_resnapshot_s.p50`, `positions_lead_ms` | A14: is a `positions` subscription needed for rail freshness? |
| `tradeid-parity` | `equal`, `ws_only`, `rest_only` | A19: tradeId de-dup policy |
| `fee-sign` | `fee_sign.TAKER` / `.MAKER` | A9: rebate sign. `MAKER` is only known if the account has a maker fill. |
| `entry-units` | `ratio`, `scale_suspect` | A8: `averageEntryPrice` scale |
| `ioc-reduce-only` | `market_reduce_only`, `oversize_reduce_only`, `zero_fill_ioc.ws` | A-6: `ARCUS_CROSSING_WIRE` (market vs limit_ioc); D6-4: oversize reduce-only gives a 400, a reject, or a clip; the exact zero-fill IOC frame |
| `alo-reduce-only` | `reduce_only_alo.accepted_and_open` | Resting reduce-only ALO close legs. `false` blocks P7b. |
| `alo-cross` | `ws`, `order_units_charged` | The ALO-cross reject shape, and the pool units it costs |

## 8. Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | ok | read the report |
| 2 | refused in preflight; nothing was placed | read the `REFUSED:` line (wrong network, key not ACTIVE, not funded, book too far for trading, …) |
| 3 | cleanup incomplete | the `PROBE CLEANUP INCOMPLETE` line lists the `nb0_…` clientIds and any position delta: cancel or close them in the Arcus app, then re-run |
| 1 | unexpected error, or interrupted | the redacted traceback is printed. Press Ctrl-C **once** (or `kill <pid>`) and let cleanup finish; a second Ctrl-C aborts cleanup |

## 9. Commit

```
git add tests/fixtures/arcus/g1/
```

Put a short decision log in the PR: one line per row of §7 with the chosen
value. The reports contain no address (it becomes `0x…dead`), no key, no
public key, no signature and no country. The script refuses to write a report
that still contains any of them. G1 covers testnet only; mainnet bands and caps
stay unverified until the canary.

## 10. What the script guarantees (for review)

- Testnet only: it checks the network token and the REST/WS hosts. It refuses
  on Fly unless `--allow-fly`.
- The key comes only from `ARCUS_PROBE_SIGNING_KEY`. It is popped from the
  process environment at start. A wallet private key is refused with *"That is
  a WALLET private key. Treat it as exposed and move your funds."*
- Every order is `nb0_<run>-<n>` (user tag 0 = probe), goodTilTime now +
  `ARCUS_GTT_DAYS`, ALO + LIMIT unless the step says otherwise. At most 2
  placements per second, `--max` per subcommand (default 60), 300 per process.
  A reduce-only safety close is never blocked by these caps.
- Cleanup always runs, including on Ctrl-C. It batch-cancels probe clientIds
  by id (never cancel-all, never modify), then re-reads open orders by the run
  prefix, up to 3 rounds. After a trading run it flattens only the
  probe-created position delta with reduce-only orders.
- DENIED ≠ EMPTY: a denied read is never treated as "no orders" or "flat". The
  run then reports `unknown` and exits 3.

## 11. Already answered keylessly (capture of 2026-09-30)

- **Default leverage and margin mode:** `GET /v1/leverages` for an address with
  no testnet activity lists BTC-USD 40, ETH-USD 25 and SOL-USD 20, all `CROSS`.
  These are the markets' maximum leverage (`floor(1 / initialMarginFraction)`).
  Source: `tests/fixtures/arcus/captured/testnet_20260930T030516Z/leverages.json`.
  `default-leverage` on your funded address confirms it for an active account.
- Live candles come newest first (the docs say oldest first; the parser sorts
  either way). Live markets carry the undocumented `lastTradePrice` and
  `openInterestCapNotional` fields. The testnet gateway reported `testnet-v1.11.6`.
