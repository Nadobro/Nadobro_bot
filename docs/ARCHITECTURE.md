# Nadobro architecture

Updated 2026-07 after the services/ decomposition. This is the authoritative map of
what lives where, which direction imports may flow, and the guards that keep it true.

## Why the 2026-07 restructure

Before the restructure, `src/nadobro/services/` held 108 modules (~30k lines) spanning
venue IO, LLM plumbing, strategy runtime, user accounts, Telegram delivery, and pure
math — one flat namespace with no dependency direction. Measured consequences:

- Upward imports: `engine/adapter` imported `services.nado_client`; `models/database`
  imported a service for PnL pairing; `points_service` and the scheduler imported
  Telegram keyboards from the UI layer; `connectors` imported services.
- ~100 function-local "lazy" imports existed mainly to dodge import cycles
  (`bot_runtime` 25, `scheduler` 22, `engine_runtime` 17).
- New code had no obvious home, so everything landed in `services/`.

The restructure was purely mechanical (`git mv` + repo-wide reference rewrite, no
renames, no behavior change) executed in five batches with the full test suite green
after each batch.

## Package map and layering

Lower layers never import higher ones. Within a level, packages may reference each
other where noted.

```
Level 5  main.py, handlers/          composition + Telegram UI (may import anything)
Level 4  runtime/                    APScheduler job wiring, runtime supervisor
Level 3  strategy/  trading/  portfolio/  vault/  notify/  users/   ← domain
         engine/ (Engine v2: orchestrator/controllers/executors/risk/backtester)
Level 2  venue/  market_data/  llm/  connectors/                    ← integration
Level 1  db.py  models/  migrations/ config.py  i18n.py             ← persistence/config
Level 0  core/  quant/  utils/                                      ← leaves (no upward imports)
```

Package responsibilities:

| Package | Owns | May import (notable) |
|---|---|---|
| `utils/` | env parsing (inline-`#` tolerant), x18 conversions | stdlib only |
| `core/` | thread pools (`async_utils`), caches, rate limits/circuits, HTTP session, log redaction, perf/SLI, feature flags | utils |
| `quant/` | pure math: `margin`, `portfolio_calculator` (fill pairing/PnL windows), `mm_quote_math`, `pov_engine` | utils |
| `db.py`/`models/` | psycopg2 pool + raw-SQL CRUD; per-network tables (`trades_testnet`/`trades_mainnet`), `bot_state` KV | core, quant, utils |
| `connectors/` | news/data connectors, provider catalog, LLM-provider env resolution (`provider_config`), source freshness registry | core, utils |
| `venue/` | Nado access: `nado_client` (REST/SDK), `nado_ws*`, fill `nado_sync`, `nado_archive` indexer, `product_catalog`, `market_feed`, `gateway_budget`, `ws_health` | db/models, quant, core, trading (queue diagnostics) |
| `market_data/` | CMC/HL/X clients, news aggregator, scanners, price tracker, `nadoexplorer_client` (public leaderboard/trader-stats API, 120 rpm/IP budget-aware) | connectors, core |
| `llm/` | `llm_gateway` (ALL LLM calls route here; Grok X-search stays native xAI), NanoGPT client, AI chat (`bro_llm`), knowledge + vector store, HOWL/night-HOWL, edge scanner, signals, briefs, managed agent, `howl_ui` | venue, market_data, users, trading, strategy (managed agent) |
| `engine/` | Engine v2: orchestrator, controllers (grid/rgrid/dgrid/mid/vol/dn/desk), executors, risk, cost-aware backtester | venue (adapter), quant, utils |
| `trading/` | order/trade domain: `trade_service`, `order_intents` (digest tagging), `live_session` (session PnL snapshot), `engine_persistence`, desk suite, `copy_service` (LIVE copy mirroring plane: venue read-only polling, sizing, TP/SL brackets, full+partial close mirroring, bracket-fill sweep, derived-PnL accounting — each mirror run is a `strategy_sessions` row with strategy='copy'), `copy_discovery` (NadoExplorer leaderboard/preview plane), stop-loss, readiness, risk/budget | engine, venue, users, llm (desk parser), market_data (copy discovery) |
| `strategy/` | strategy lifecycle: `bot_runtime` (session SL/TP rail), `engine_runtime` (`map_strategy_config`, `CONTROLLER_REGISTRY`, `ENGINE_MAPPED_STRATEGIES`), registry, FSM, schedulers, MM overlay + dashboard, `network_switch` (fail-closed testnet<->mainnet switch) | trading, engine, llm, users, venue |
| `users/` | user accounts, settings, onboarding, invites/referrals/points (`points_ui`), admin, audit log, wallet flows, `venue_service` (/venue view selection — `users.active_venue`; never stops/starts anything) | strategy (registry defaults, stop-on-unlink), venue |
| `portfolio/` | portfolio views, history worker, PnL cards | trading, engine, users, venue |
| `vault/` | NLP vault metrics, deposit watcher | venue, users |
| `notify/` | rate-limited `telegram_sender`, alert evaluation/dispatch | users, models |
| `runtime/` | `scheduler` (APScheduler jobs), `runtime_supervisor` | everything except handlers |
| `handlers/` | Telegram UI: commands, single callback router (`callbacks.handle_callback`), free-text flow (`messages`), keyboards, cards/views | anything |

Domain-owned UI fragments: keyboards a domain service must send itself live in that
domain (`users/points_ui.py`, `llm/howl_ui.py`), not in `handlers/keyboards.py` —
that is what keeps domain→handlers imports at zero.

## Guards that enforce this

- `tests/lint/test_architecture_layers.py` — pins the package→package module-level
  import edge set (TYPE_CHECKING-exempt). New edges fail; shrinking is welcome. Also
  hard-forbids importing `handlers` from any other package.
- `tests/lint/test_no_legacy_strategy_imports.py` — the pre-Engine-v2
  `src/nadobro/strategies/` (plural) package is deleted; nothing may import it.
  (The current `strategy/` package is singular deliberately.)
- `tests/test_env_read_hygiene_static.py` — env values must be read via
  `utils/env.py` helpers (inline-`#`-comment tolerant), never raw numeric casts or
  hand-rolled truthy parses.
- `tests/test_i18n_reply_routing.py` — every reply-keyboard label must round-trip
  localize→resolve→same action in every supported language.
- `tests/lint/test_parse_mode_consistency.py` — Markdown-built text is never sent
  as HTML and vice versa (scans the whole package tree).
- `tests/engine/test_sltp_invariants.py` — strategy SL/TP invariants
  (see `docs/self_review/SELF_REVIEW_WORKFLOW.md` for the audit loop).

## Facts that bite (unchanged by the restructure)

- Engine-mapped strategies: `("grid", "rgrid", "dgrid", "mid", "vol", "dn")`. Desk
  sessions are driven by `trading/desk_runtime`, not bot_runtime cycles. Live copy
  trading is `trading/copy_service.py` — the old engine `CopyController` was
  unreachable and has been removed.
- The venue reports NO per-fill realized PnL (`realized_pnl_x18` is always 0). All
  PnL comes from local fill attribution (`quant/portfolio_calculator` + trades
  tables). Close orders must be digest-tagged (`trading/order_intents.py::
  link_digest_intent`) so closes reconcile instead of leaking into History.
- `funding_rate_x18` is a signed DAILY rate settled hourly; `cum_funding_x18` is NOT
  a rate.
- Session SL/TP fires off live PnL as % of margin including uPnL, judged NET of fees
  (`trading/live_session.py` → `strategy/bot_runtime._evaluate_session_pnl_rail`).
- asyncio discipline: no sync IO in coroutine bodies — dispatch through
  `core/async_utils` (`run_blocking`, `run_blocking_db`, `run_blocking_sdk`).
- Redeploys NEVER auto-resume any trade/plan/strategy. Boot = stand-down.
- The runtime is single-active-network (Stop, /status, cards and /desk read the
  user's ACTIVE network). The testnet<->mainnet switch is fail-closed
  (`strategy/network_switch.py`): it stops the old network's strategy run (Stop
  semantics), retries earlier unconfirmed stops, cancels its desk plans (sweeping a
  running plan's product), and flips `users.network_mode` only once every item is
  confirmed. Copy mirrors, stop-loss rules and the managed agent are network-scoped
  and keep running. Never call `user_service.set_network_mode` from a UI path.
- Every stop path (Stop, /stop_all, rails, duration cap, boot stand-down, the
  switch) records a pending cleanup (`strategy/pending_cleanup.py`, its own
  `bot_state` key) BEFORE its venue half and deletes it only once the cancel /
  flatten / trigger sweep is confirmed. Stop, /stop_all and the switch retry what
  is left — cancel-only, never a flatten.
- Env values may carry inline `# comments` — read through `utils/env.py`
  (`env_bool/env_int/env_float/env_str`, `clean_env_value`), never raw `os.environ`
  parsing.

## Known residual debt (intentional, next candidates)

- God modules remain by line count (`strategy/bot_runtime.py` ~3.3k,
  `trading/trade_service.py` ~3.3k, `i18n.py` ~3.8k, `handlers/strategy_handler.py`
  ~2.5k). Splitting them is semantic surgery — out of scope for the mechanical
  restructure, worth doing per-module with the self-review gate.
- Some package-level edge pairs are bidirectional at package granularity
  (venue↔trading, llm↔strategy, users↔strategy) while remaining acyclic at module
  level. Tightening these means moving individual functions, not directories.
- ~100 function-local imports predate the restructure; many are now legal at module
  level. They are harmless (and some are deliberate: startup cost, optional deps),
  so they are converted opportunistically, not wholesale.
