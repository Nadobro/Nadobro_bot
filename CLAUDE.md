# Nadobro — Telegram trading bot for Nado DEX (Ink L2 CLOB)

Python 3.11 bot (python-telegram-bot 22.x; long-polling in dev, webhook on Fly.io in prod) for
trading perps on Nado: natural-language trades, 1CT linked-signer wallets, live PnL, alerts, and
automated strategies. PostgreSQL via psycopg2 raw SQL — no ORM. Entry point: `main.py`.

`replit.md` is the legacy product doc — useful history, but partially stale (it predates both the
Engine-v2 cutover and the 2026-07 services/ decomposition). Trust code over it.
`docs/ARCHITECTURE.md` is the authoritative package map and layering contract.

## Commands

- Full test suite (must stay green; CI runs all of it): `.venv/bin/python -m pytest -q`
- DB-backed tests need a local postgres: `docker compose -f compose.postgres-test.yaml up -d`, then
  run pytest with `NADO_TEST_DATABASE_URL=postgresql://nadobro:nadobro@127.0.0.1:5433/nadobro_test`.
  conftest scrubs any non-local `DATABASE_URL` — without that var, DB tests skip (they never touch prod).
- Strategy gate — required before merging any strategy / SL-TP / config change:
  `PYTHON=.venv/bin/python bash scripts/self_review.sh` (`--full` adds the whole engine suite).
  Loop, audit agents, and triage protocol: `.claude/skills/self-review/SKILL.md` and
  `docs/self_review/SELF_REVIEW_WORKFLOW.md`.
- Type-check (BLOCKING in CI — ci.yml runs it as a required job): `.venv/bin/python -m mypy src/nadobro/engine`.
  `scripts/self_review.sh` fails on it too, so a green local gate matches CI.

## Layout (`src/nadobro/`) — see docs/ARCHITECTURE.md for the full map

Layering (enforced by `tests/lint/test_architecture_layers.py` — domain packages never import
`handlers/`; the package import-edge set can only shrink):

- Leaves: `utils/` (env parsing — ALL env reads go through `utils/env.py`: `env_bool/env_int/
  env_float/env_str/clean_env_value`; values may carry inline `#` comments), `core/` (thread
  pools `async_utils`, caches, rate limits, HTTP session, `log_redaction`, perf/SLI, flags),
  `quant/` (pure math: `margin`, `portfolio_calculator` fill pairing, `mm_quote_math`, `pov_engine`).
- `config.py` — env vars + product/market constants. `db.py` + `models/database.py` — psycopg2
  pool, raw-SQL CRUD. Tables are split per network (`trades_testnet`/`trades_mainnet`, `alerts_*`);
  `bot_state` is a KV store for settings/strategy state.
- Integration: `venue/` (Nado REST/WS clients, fill `nado_sync`, archive indexer, product catalog,
  `gateway_budget`), `market_data/` (CMC/HL/X/news/scanners), `llm/` (`llm_gateway.py` — ALL LLM
  reasoning routes through here; NanoGPT, per-task model env vars — plus AI chat, knowledge/vector,
  HOWL/edge/signals/briefs, managed agent), `connectors/` (+ `provider_config.py`, source registry).
- Domain: `engine/` — Engine v2: `controllers/` (grid_trading, dynamic_grid, market_making,
  volume_bot, delta_neutral, desk, fill_anchored), `executors/`, `risk.py`, `backtester/` — a
  cost-aware (fees + funding + slippage) sim harness driving the same controllers as live.
  `trading/` — `trade_service.py`, `order_intents.py` (digest tagging), `live_session.py` (live
  session PnL snapshot, net-of-fees variants), `engine_persistence.py`, desk suite,
  `copy_service.py` (the LIVE copy-trading path; the old engine CopyController was unreachable
  and is removed). `strategy/` — `bot_runtime.py` (strategy lifecycle + the session SL/TP rail
  `_evaluate_session_pnl_rail`), `engine_runtime.py` (`map_strategy_config`, `map_risk_limits`,
  `ENGINE_MAPPED_STRATEGIES`), `strategy_registry.py` (identity/defaults, `effective_sl_tp_pct`).
  `users/`, `portfolio/`, `vault/`, `notify/` (rate-limited `telegram_sender`, alerts),
  `runtime/` (`scheduler.py`, supervisor).
- `handlers/` — Telegram UI: `commands.py`, `callbacks.py` (button router), `messages.py` (free
  text + passphrase flow), `keyboards.py`, `home_card.py`/`trade_card.py` (edit-in-place cards).
  Domain-owned keyboards a service must send itself live in the domain (`users/points_ui.py`,
  `llm/howl_ui.py`).
- `relay/` (repo root) — separate FastAPI/Telethon microservice for @lowiqpts points lookups,
  deployed independently.

## Architecture facts that bite

- Engine-mapped strategies: `("grid", "rgrid", "dgrid", "mid", "vol", "dn")`. Engine fills are
  bridged into the legacy `trades_<network>` / `strategy_sessions` tables so /status, fills, and
  portfolio views show real numbers — keep that bridge in sync when touching fill handling.
- The venue reports NO per-fill realized PnL (`realized_pnl_x18` is always 0). All PnL comes from
  our own fill attribution; attribution bugs corrupt History and volume. Close orders must be
  digest-tagged (`trading/order_intents.py::link_digest_intent`) so closes reconcile instead of
  leaking into History as phantom trades.
- Funding: `funding_rate_x18` is a signed DAILY rate settled hourly (indexer
  `get_perp_funding_rate(s)`); `cum_funding_x18` is NOT a rate — don't treat it as one.
- Session SL/TP fires off live PnL as % of margin including uPnL, judged NET of fees
  (`live_session.py` net snapshot → `bot_runtime._evaluate_session_pnl_rail`). Units invariant: a
  user value is either a price-move barrier OR a %-of-margin rail — never applied as both.
- asyncio discipline: never call sync/blocking IO (Redis, token buckets, `requests`) inside
  coroutine bodies — it starves the event loop and APScheduler starts skipping jobs. Tap-driven
  paths (home card) must serve cached data and refresh in the background, never block on the venue.
- i18n: user-facing strings go through `i18n.py` (`localize_text`/`localize_markup`); translated
  reply-keyboard labels round-trip back to English for routing via `resolve_reply_button_text`.
- Nado rate limits are per-IP WEIGHT (400/10s, 2400/min for live queries) and the venue charges
  exactly what the docs say — measured 2026-09-16: a multi-product `orders` read costs
  2 × product_ids (~192 for the whole catalog), `market_prices` costs N. Never flat-charge a
  batched read; keep N small instead (scoped parent read, one product per isolated child). Every
  SDK call goes through `_gateway_allowed` + `_record_gateway_error` (a venue 429 is an HTTP 200
  JSON body, `error_code=1000`). Bulk price polling uses the edge lane (`/edge/query`
  `cached_prices`, weight 1, separate 12000/min bucket) — never a per-product fan-out. When the
  book cannot be read, cancel-only cleanup uses `cancel_product_orders` (read-free execute).

## Hard rules

- Redeploys NEVER auto-resume or re-arm any trade, plan, or strategy. Boot = stand-down; resuming
  is strictly user-initiated. This is a deliberate product rule — do not "fix" it.
- Strategy/SL-TP/config changes go through the self-review loop. New `[VERIFIED]` bug → add a
  strict-xfail guardrail in `tests/engine/test_sltp_invariants.py` referencing its audit ID; the
  fix makes it XPASS → delete the marker in the same PR. Never fix silently, never let red tests
  accumulate.
- Secrets: log output passes through redaction (`core/log_redaction.py`); never log tokens or
  keys; `attached_assets/` stays gitignored (git history was purged of a leaked token once —
  don't make it twice).
- All LLM calls go through `llm/llm_gateway.py` — don't call providers directly. Exception:
  Grok X-search stays on the native xAI path.

## Git / PRs

- Branch from `main` (`codex/*` or `claude/*` convention), PR back to `main`. CI must be green:
  full pytest (with postgres service), SL/TP invariants, security audit.
