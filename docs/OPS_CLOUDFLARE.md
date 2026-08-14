# Cloudflare 403 challenge — operations runbook

This bot talks to three Nado endpoints over HTTPS:

| What                | Host                                | Used by                                            |
| ------------------- | ----------------------------------- | -------------------------------------------------- |
| Gateway REST        | `gateway.{mainnet,testnet}.nado.xyz` | `venue.nado_client`, `venue.product_catalog` |
| Archive REST        | `archive.{mainnet,testnet}.nado.xyz` | `venue.nado_archive`                            |
| Archive v2 symbols  | `archive.{...}.nado.xyz/v2/symbols`  | `venue.product_catalog`                         |

When Cloudflare (Nado's edge) decides our traffic looks botty, it answers any
plain JSON request with a 403 Forbidden whose body is an HTML "Just a moment…"
interstitial. Symptoms:

```
WARNING] src.nadobro.venue.nado_client: REST returned non-JSON status=403
content_type=text/html; charset=UTF-8 body='<!DOCTYPE html>...Just a moment...'
```

Until the challenge clears, the bot has:

- Empty product catalogs (mainnet menu collapses to "Switch to mainnet"-style
  errors even when the user is already on mainnet).
- Stale portfolio snapshots.
- Strategy cycles that report success but fail to fetch market price.

## How the code defends itself

The defenses live in `core/http_session.py`:

1. **Hardened headers**: every outbound call goes through a shared
   `requests.Session` configured with browser-like `User-Agent`,
   `Accept-Language`, `Sec-Fetch-*`, `Origin: https://app.nado.xyz`, and a
   `Referer`. This is enough for Cloudflare's lightweight bot check on most
   days.
2. **CF-aware retries**: `cf_request` recognises the
   `403 + content-type:text/html` Cloudflare interstitial and retries with
   exponential backoff plus jitter (default `0.5s, 1.0s, 2.0s` ±`0.4s`).
3. **Per-host circuit breaker**: after 8 challenges within a 10s window the
   circuit opens for 30s. Subsequent calls short-circuit to `None`; callers
   serve from `_spot_catalog_cache` / `_catalog_cache` via the stale-TTL
   path (`NADO_PRODUCT_CATALOG_STALE_TTL_SECONDS`, default 86400s / 24h).
4. **Throttled logs**: only one Cloudflare-warning line per host per minute
   (was: hundreds per second).
5. **Unified gateway budget** (`venue/gateway_budget.py`): every Nado REST/SDK
   call passes through host + per-user token buckets and an in-flight cap.
   Nado `error_code=1000` opens a host rate-limit circuit (default 60s cooldown).
   Callers **must skip** and serve cache when `try_acquire` returns false — never
   fan out to per-product fallbacks.
6. **WS-first portfolio** (when `NADO_PORTFOLIO_WS=true`): healthy WebSocket
   connections skip REST poll ticks; full reconcile runs every
   `NADO_WS_RECONCILE_SECONDS` (default 300s).

## Tunables (env vars)

| Env var                                  | Default | Purpose                                              |
| ---------------------------------------- | ------- | ---------------------------------------------------- |
| `NADO_HTTP_USER_AGENT`                   | Chrome  | Override the UA string if Cloudflare blocks ours.    |
| `NADO_CF_RETRY_MAX`                      | 2       | Retries on Cloudflare 403 before giving up.          |
| `NADO_CF_RETRY_BASE_SECONDS`             | 0.5     | Base backoff before the 2× ramp.                     |
| `NADO_CF_RETRY_JITTER_SECONDS`           | 0.4     | Random jitter added to each retry.                   |
| `NADO_CF_BREAKER_THRESHOLD`              | 8       | Challenges in window before opening the breaker.     |
| `NADO_CF_BREAKER_WINDOW_SECONDS`         | 10      | Sliding window for the threshold above.              |
| `NADO_CF_BREAKER_COOLDOWN_SECONDS`       | 30      | How long the breaker stays open.                     |
| `NADO_CF_LOG_THROTTLE_SECONDS`           | 60      | Cooldown between CF-warning log emissions per host.  |
| `NADO_PRODUCT_CATALOG_TTL_SECONDS`       | 3600    | Live catalog refresh interval (default: 1 hour).     |
| `NADO_PRODUCT_CATALOG_STALE_TTL_SECONDS` | 86400   | Stale catalog served after fetch failure (24h).      |
| `NADO_PORTFOLIO_SYNC_SECONDS`            | 30      | Background portfolio poll interval.                  |
| `NADO_PORTFOLIO_SYNC_USERS_PER_TICK`     | 8       | Active users synced per poll tick.                   |
| `NADO_PORTFOLIO_POLL_CACHE_SECONDS`      | 45      | Skip re-sync if user polled within this window.      |
| `NADO_PORTFOLIO_HEAVY_SYNC_SECONDS`      | 300     | Matches/funding archive refresh cadence per user.    |
| `NADO_ALL_PRODUCTS_CACHE_TTL_SECONDS`    | 3600    | SDK all-products cache TTL.                          |
| `NADO_FANOUT_WORKERS`                    | 2       | Cap on parallel SDK fan-out worker threads.          |
| `NADO_HTTP_RPS_PER_HOST`                 | 16      | Sustained outbound RPS cap per Nado host (2× after Nado limit increase). |
| `NADO_HTTP_BURST_PER_HOST`               | 32      | Token-bucket burst capacity per Nado host.           |
| `NADO_HTTP_BUCKET_MAX_WAIT_SECONDS`      | 2.5     | Max time a thread waits for a token before skipping. |
| `NADO_USER_GATEWAY_RPS`                  | 4       | Per-user fair-share RPS cap (all gateway/SDK calls). |
| `NADO_USER_GATEWAY_BURST`                | 8       | Per-user token-bucket burst.                         |
| `NADO_USER_MAX_INFLIGHT`                 | 4       | Max concurrent gateway calls per user.               |
| `NADO_GATEWAY_RL_THRESHOLD`              | 4       | Nado `error_code=1000` hits before host RL circuit.  |
| `NADO_GATEWAY_RL_COOLDOWN_SECONDS`       | 60      | Host rate-limit circuit cooldown.                    |
| `NADO_STRATEGY_SCHEDULER`                | true    | Central scheduler (one loop) vs N per-user tasks.    |
| `NADO_PORTFOLIO_WS`                      | false   | WS-driven portfolio invalidation (rollout flag).     |
| `NADO_WS_DEBOUNCE_SECONDS`               | 2       | Coalesce WS bursts into one sync.                    |
| `NADO_WS_RECONCILE_SECONDS`              | 300     | Full REST reconcile interval when WS is healthy.     |
| `NADO_WS_HEALTH_SECONDS`                 | 45      | WS considered stale after this silence.              |
| `NADO_USER_CIRCUIT_THRESHOLD`            | 5       | Consecutive cycle errors before user circuit opens.  |
| `NADO_USER_CIRCUIT_COOLDOWN_SECONDS`     | 120     | User circuit cooldown after threshold.               |

## What to do when you see the storm in logs

1. Check `core.http_session.breaker_snapshot()` (or grep
   `Cloudflare circuit OPEN`). If the breaker is open, the bot is correctly
   self-throttling; users keep seeing the *previous* catalog instead of an
   empty list. No action required on the bot side.
2. If the breaker is stuck open for >5 minutes, escalate to the Nado team
   using the templated message below.

## `ip_read_only` / `ip_query_only` is a GEO-BLOCK — egress must be in an allowed country

> ⚠️ **Do not commit real IP addresses to this repo.** Keep actual egress IPs in
> the Fly dashboard / secrets store / private ops channel. The commands below
> regenerate them on demand.

**Root cause of strategy orders failing with `{"reason":"ip_read_only","blocked":true}`:**
Nado rejects *writes* (place/cancel order — reads are unaffected) from any IP
that **geolocates to a restricted territory** (US, CA, and others). See the list:
<https://docs.nado.xyz/legal/restricted-territories>. This is a legal/compliance
block — it is **not** rate limiting (raising the per-IP limit does nothing) and
**not** a 1CT signer problem (the linked signer reads `verified=True` throughout).

**The trap — Fly region ≠ egress IP geolocation.** A machine in `ams`
(Amsterdam) can still be assigned a **US-registered** egress IP from Fly's pool
(AS40509 skews US). Example seen in prod: an `ams` machine egressed via a Fly IP
that geolocated to **Colorado, US** → every order blocked. So you must verify the
**geolocation of the actual IP**, not the region.

### Procedure: obtain an allowed-country egress IP

```bash
# 1. Allocate a dedicated static egress IP (per machine/region):
fly ips allocate-egress -a <app-name> -r <region>

# 2. List allocations (look for Type: egress):
fly ips list -a <app-name>

# 3. VERIFY THE COUNTRY Nado sees — this is the check that matters, not the IP:
fly ssh console -a <app-name> -C "curl -4 -s https://ipinfo.io/country"          # from inside the machine
curl -s https://ipinfo.io/<egress-ip>/country                                     # for a specific allocated IP
# cross-check org/region too:
curl -s https://ipinfo.io/<egress-ip>/json
```

- If the country is **NOT** on the restricted list (e.g. `NL`, `JP`, `SG`) →
  done. Confirm with Nado, then writes should succeed.
- If it geolocates to **US/CA/restricted** → release it
  (`fly ips release <egress-ip> -a <app-name>`) and try another region, or
  **open a Fly support ticket asking for an egress IP that geolocates to a
  specific allowed country** (Fly's pool is US-heavy, so a clean NL/JP IP may
  need their help, or a different provider/proxy — see fallback below).

**Re-verify the country after every redeploy / egress change** (Machine
recreation can drop or change the allocation). This is now automated:

- **In-bot guard** (`core/egress_geo.py`): on boot and every
  `NADO_EGRESS_GEO_CHECK_HOURS` (default 6h) the bot probes its own IPv4 **and**
  IPv6 egress country and logs `EGRESS GEO-BLOCK RISK: ...` at ERROR if either
  geolocates to a restricted territory. It is **advisory** (never blocks
  trading — a flaky geo lookup must not halt the desk); the authoritative
  reactive defense stays the `ip_query_only` write circuit. The current status
  also shows in `/ops` under Diagnostics (`egress geo: ok|RESTRICTED|unverified`).
- **On-demand / release check** — run from inside the machine; exits non-zero on
  a restricted egress so a deploy pipeline can gate on it:

  ```bash
  fly ssh console -a nadobro-bot -C "python scripts/check_egress_geo.py"
  ```

Restricted set defaults to `US,CA,PA,BY,CU,IR,KP,RU,SY` — the whole-country
entries from Nado's Terms of Use (US, Canada, Panama, Belarus, Cuba, Iran, North
Korea, Russia, plus Syria via comprehensive US/UK sanctions). Ukraine is excluded
because only the Crimea/Donetsk/Luhansk **regions** are restricted, not the
country. Tune with `NADO_RESTRICTED_COUNTRIES` if Nado revises the list.

`NADO_FORCE_IPV4` defaults to `0` (dual-stack) in `fly.toml` — forcing IPv4-only
also forced the DB resolver to IPv4 and broke Supabase's IPv6-only direct host.
Because egress is dual-stack, Nado may see **either** the IPv4 or the IPv6 egress
on a connect, so both families must geolocate to an allowed country — which is
why the guard above checks both. Set `NADO_FORCE_IPV4=1` only if you have pinned
a single clean IPv4 egress and need Nado to see exactly that address.

### Fallback if Fly can't provide an allowed-geo egress

Route Nado traffic through a proxy / small VPS in an allowed country (NL/JP)
whose IP verifiably geolocates correctly. This requires adding egress-proxy
support to the bot (SDK engine/indexer/trigger sessions + the pooled
`http_session.SESSION` + the WS connect) — not yet implemented. Track as a
follow-up if the Fly-native path doesn't yield a clean IP.

> Compliance: this is only legitimate when correcting a **misclassification**
> (you operate from a non-restricted country and the IP was mislabeled) — not to
> serve users actually located in restricted territories. Get sign-off from
> whoever owns compliance.

## Templated message to send to Nado

> ⚠️ Fill `<service-id>` and `<egress-ipv4>` in at send time from the ops
> secret store — do **not** commit the real values here.
>
> Hi Nado team — our Telegram bot is consistently being challenged by
> Cloudflare on `gateway.{mainnet,testnet}.nado.xyz/query` and
> `archive.{mainnet,testnet}.nado.xyz/v2/symbols`. We see HTTP 403 with the
> "Just a moment…" interstitial across reasonable request rates
> (≈1–2 rps per worker, normal browser-like headers).
>
> Could you either (a) whitelist our egress IPs (we will share static ones),
> (b) issue us an API key / bearer token that bypasses the bot-management
> rule for these paths, or (c) raise the bot score threshold for the
> `/query` and `/v2/symbols` endpoints?
>
> Our service ID is **`<service-id>`**, our static egress IPv4 is
> **`<egress-ipv4>`** (see ops secret store), and we are happy to add any
> custom header or auth scheme you prefer. Even a 30 rpm shared bucket would
> unblock the per-user portfolio sync that 1000s of our users rely on.
>
> Thanks!

## Why the previous reproducer showed "Switch to mainnet"

The old menu literal was hardcoded. When Cloudflare blocked the v2 symbols
fetch, `list_volume_spot_product_names("mainnet")` collapsed to an empty list
and the menu rendered `"Switch to mainnet"` to a user who was already on
mainnet. This module now serves the stale catalog so the menu keeps working,
and the menu copy now distinguishes "no pairs listed on $network" vs
"catalog temporarily unavailable on $network" so users get accurate guidance.
