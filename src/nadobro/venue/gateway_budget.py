"""Unified Nado gateway budget — the lasting rate-limit solution.

Every outbound call to Nado (REST via ``http_session`` or SDK via
``nado_client``) must pass through :func:`try_acquire` **before** hitting
the network and call :func:`release` in a ``finally`` block.

Nado is **weight-based** with three independent budgets
(https://docs.nado.xyz/developer-resources/api/rate-limits):

  * Core queries  — per **IP**     : 2400/min, 400/10s  (= 40 weight/s, burst 400)
  * Archive/index — per **IP**     : 2400/min, 400/10s  (separate host)
  * Executes      — per **wallet** : 600/min, 100/10s   (= 10 weight/s, burst 100)

Callers pass ``weight`` (documented per-call cost — see ``nado_weights``) and
``kind`` (``"query"`` or ``"execute"``). Layers, checked in order:

**Query / archive (kind="query", limited per IP):**

1. **Host circuit** — Cloudflare + Nado ``error_code=1000`` breakers.
2. **Per-user in-flight cap** — max ``NADO_USER_MAX_INFLIGHT`` (default 4).
3. **Host weight bucket** — the per-IP query budget (40 weight/s, set in
   ``http_session``; keyed by netloc so gateway and archive are independent).
4. **Per-user weight bucket** — fair share so one user can't starve the IP
   budget (default **8 weight/s / burst 24**).

**Execute (kind="execute", limited per wallet):**

1. **Host circuit** (executes still ride the gateway host).
2. **Per-wallet weight bucket** — the 600/min wallet budget (default **9
   weight/s / burst 90**; doc 10/100 with a safety margin). Charging the
   documented weights makes order sub-limits fall out for free — e.g. a
   place *without* spot leverage costs 20, so 20×30 = 600/min caps it at the
   documented 30/min automatically.

**Edge (kind="edge", limited per IP, its own bucket):**

1. **Cloudflare circuit** only — the ``error_code=1000`` live breaker does not
   apply (a throttled live lane is exactly when the edge lane must carry the
   price snapshot).
2. **Per-host edge bucket** — 12000/min, 2000/10s, weight 1 per request.

When :func:`try_acquire` returns ``False``, callers must **skip** the call
and serve cached data — never fan out to per-product fallbacks.
"""
from __future__ import annotations

import logging

from src.nadobro.utils.env import env_float, env_int
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Per-user fair share of the per-IP query budget (now in *weight*/s, not
# requests/s). Defaults bumped to track Nado's 40 weight/s IP ceiling.
_USER_RPS = env_float("NADO_USER_GATEWAY_RPS", 8.0)
_USER_BURST = env_float("NADO_USER_GATEWAY_BURST", 24.0)
_USER_MAX_WAIT = env_float("NADO_USER_GATEWAY_MAX_WAIT_SECONDS", 2.0)
_USER_MAX_INFLIGHT = env_int("NADO_USER_MAX_INFLIGHT", 4)
_USER_BUCKET_MAX = env_int("NADO_USER_GATEWAY_BUCKETS_MAX", 8192)

# Per-wallet execute budget (weight/s). Nado allows 600/min == 100/10s == 10
# weight/s per wallet; default carries a ~10% safety margin (9/90).
_WALLET_RPS = env_float("NADO_WALLET_EXECUTE_RPS", 9.0)
_WALLET_BURST = env_float("NADO_WALLET_EXECUTE_BURST", 90.0)
_WALLET_BUCKET_MAX = env_int("NADO_WALLET_EXECUTE_BUCKETS_MAX", 8192)

_RL_THRESHOLD = env_int("NADO_GATEWAY_RL_THRESHOLD", 4)
_RL_WINDOW = env_float("NADO_GATEWAY_RL_WINDOW_SECONDS", 15.0)
_RL_COOLDOWN = env_float("NADO_GATEWAY_RL_COOLDOWN_SECONDS", 60.0)

# ip_query_only WRITE circuit. Nado downgrades a saturated IP to query-only:
# reads still work, but every execute (place/cancel) is rejected with
# {"reason":"ip_query_only","blocked":true}. This is INVISIBLE to the
# Cloudflare / error_code=1000 breakers above, so without this the bot keeps
# firing doomed orders into the ban — adding load that keeps the IP banned and
# churning executors into FAILED. When we observe an ip_query_only rejection we
# open a short per-host write circuit so executes short-circuit (cheap, no
# round-trip) until the ban is likely lifted; queries are unaffected.
_WRITE_BAN_COOLDOWN = env_float("NADO_WRITE_BAN_COOLDOWN_SECONDS", 45.0)

# EDGE lane (``/edge/query``, kind="edge"): in-gateway cached market/reference
# reads on a SEPARATE, larger per-IP bucket — 12000/min, 2000/10s, weight 1 per
# request regardless of product count (docs: developer-resources/api/gateway/edge).
# Bulk price polling belongs here: it must never compete with order/margin reads
# for the 400/10s live budget (2026-09-16: the alert scan's 82-weight live
# ``market_prices`` read every 5s alone burned ~41% of the live IP budget).
_EDGE_RPS = env_float("NADO_EDGE_RPS", 150.0)        # doc 200/s; safety margin
_EDGE_BURST = env_float("NADO_EDGE_BURST", 1500.0)   # doc 2000/10s; safety margin


@dataclass
class _TokenBucket:
    rps: float
    burst: float
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.tokens <= 0:
            self.tokens = self.burst

    def try_acquire(self, *, max_wait: float, cost: float = 1.0) -> bool:
        # A single call can't cost more than the burst ceiling or the bucket
        # could never fill enough; clamp to avoid a deadlock until the deadline.
        need = max(1.0, min(float(cost), self.burst))
        deadline = time.monotonic() + max(0.0, max_wait)
        while True:
            now = time.monotonic()
            elapsed = max(0.0, now - self.last_refill)
            self.tokens = min(self.burst, self.tokens + elapsed * self.rps)
            self.last_refill = now
            if self.tokens >= need:
                self.tokens -= need
                return True
            if now >= deadline:
                return False
            needed = (need - self.tokens) / self.rps if self.rps > 0 else 0.25
            time.sleep(min(needed, 0.25, max(0.0, deadline - now)))


@dataclass
class _BreakerLite:
    failures: list[float]
    open_until: float


_lock = threading.RLock()
_user_buckets: dict[int, _TokenBucket] = {}
_user_inflight: dict[int, int] = {}
_wallet_buckets: dict[str, _TokenBucket] = {}
_edge_buckets: dict[str, _TokenBucket] = {}
_gateway_rl: dict[str, _BreakerLite] = {}
_write_ban: dict[str, float] = {}  # host -> open_until (monotonic-free wall clock)


def _host(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).netloc.lower()
    except Exception:
        return ""


def is_rate_limit_error(exc: BaseException | str) -> bool:
    msg = str(exc).lower()
    return (
        "too many requests" in msg
        or '"error_code":1000' in msg
        or "'error_code': 1000" in msg
        or "error_code=1000" in msg
    )


def record_gateway_failure(url: str, exc: BaseException | str | None = None) -> None:
    if exc is not None and not is_rate_limit_error(exc):
        return
    host = _host(url)
    if not host:
        return
    now = time.time()
    with _lock:
        state = _gateway_rl.get(host)
        if state is None:
            state = _BreakerLite(failures=[], open_until=0.0)
            _gateway_rl[host] = state
        cutoff = now - _RL_WINDOW
        state.failures = [t for t in state.failures if t >= cutoff]
        state.failures.append(now)
        if len(state.failures) >= _RL_THRESHOLD and now >= state.open_until:
            state.open_until = now + _RL_COOLDOWN
            logger.warning(
                "Nado gateway rate-limit circuit OPEN host=%s cooldown=%.0fs failures=%d",
                host,
                _RL_COOLDOWN,
                len(state.failures),
            )


def is_gateway_rate_limited(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    with _lock:
        state = _gateway_rl.get(host)
        if not state:
            return False
        return time.time() < state.open_until


def is_gateway_blocked(url: str) -> bool:
    try:
        from src.nadobro.core.http_session import is_circuit_open
        return is_circuit_open(url) or is_gateway_rate_limited(url)
    except Exception:
        return is_gateway_rate_limited(url)


def record_ip_query_only(url: str) -> None:
    """Open the per-host WRITE circuit after an ip_query_only rejection.

    Reads are intentionally NOT affected — only executes short-circuit while the
    circuit is open. Idempotent: a fresh rejection re-arms the cooldown so a
    sustained ban keeps writes parked instead of probing every tick.
    """
    host = _host(url)
    if not host:
        return
    now = time.time()
    with _lock:
        prev = _write_ban.get(host, 0.0)
        _write_ban[host] = now + _WRITE_BAN_COOLDOWN
        if now >= prev:  # transition closed -> open; log once per window
            logger.warning(
                "Nado WRITE circuit OPEN host=%s cooldown=%.0fs (ip_query_only) — "
                "executes parked, reads unaffected",
                host, _WRITE_BAN_COOLDOWN,
            )


def is_write_blocked(url: str) -> bool:
    """True while the per-host write circuit is open (ip_query_only cooldown)."""
    host = _host(url)
    if not host:
        return False
    with _lock:
        return time.time() < _write_ban.get(host, 0.0)


def clear_write_ban(url: str) -> None:
    """Close the write circuit early (e.g. after a confirmed successful execute)."""
    host = _host(url)
    if not host:
        return
    with _lock:
        _write_ban.pop(host, None)


def _user_bucket(user_id: int) -> _TokenBucket:
    with _lock:
        bucket = _user_buckets.get(user_id)
        if bucket is None:
            if len(_user_buckets) >= _USER_BUCKET_MAX:
                oldest = min(_user_buckets, key=lambda k: _user_buckets[k].last_refill)
                _user_buckets.pop(oldest, None)
            bucket = _TokenBucket(rps=_USER_RPS, burst=_USER_BURST)
            _user_buckets[user_id] = bucket
        return bucket


def _wallet_bucket(wallet: str) -> _TokenBucket:
    with _lock:
        bucket = _wallet_buckets.get(wallet)
        if bucket is None:
            if len(_wallet_buckets) >= _WALLET_BUCKET_MAX:
                oldest = min(_wallet_buckets, key=lambda k: _wallet_buckets[k].last_refill)
                _wallet_buckets.pop(oldest, None)
            bucket = _TokenBucket(rps=_WALLET_RPS, burst=_WALLET_BURST)
            _wallet_buckets[wallet] = bucket
        return bucket


def _edge_bucket(host: str) -> _TokenBucket:
    with _lock:
        bucket = _edge_buckets.get(host)
        if bucket is None:
            bucket = _TokenBucket(rps=_EDGE_RPS, burst=_EDGE_BURST)
            _edge_buckets[host] = bucket
        return bucket


def try_acquire(
    url: str,
    *,
    user_id: Optional[int] = None,
    weight: float = 1.0,
    kind: str = "query",
    wallet: Optional[str] = None,
    max_wait: Optional[float] = None,
) -> bool:
    """Reserve gateway budget for one call of ``weight`` (documented Nado
    weight). ``kind`` selects the budget:

      * ``"query"`` — per-IP host bucket + per-user fair share + in-flight cap.
        Pair with :func:`release` when ``user_id`` is supplied.
      * ``"execute"`` — per-wallet bucket only (no in-flight slot, so **no**
        :func:`release` needed). Executes are limited per wallet, not per IP.

      * ``"edge"`` — the edge-query bucket only (12000/min per IP, weight 1 per
        request). Independent of the live query budget AND of the
        ``error_code=1000`` breaker; only a Cloudflare challenge on the host
        parks it. No :func:`release` needed.

    Returns False when budget is unavailable — callers must skip and serve
    cached data.
    """
    wait = _USER_MAX_WAIT if max_wait is None else float(max_wait)
    w = max(1.0, float(weight))

    if kind == "edge":
        try:
            from src.nadobro.core.http_session import is_circuit_open
            if is_circuit_open(url):
                return False
        except Exception:
            pass
        if not _edge_bucket(_host(url)).try_acquire(max_wait=wait, cost=w):
            logger.debug("gateway budget: edge bucket starved host=%s (w=%s)", _host(url), w)
            return False
        return True

    if is_gateway_blocked(url):
        return False

    if kind == "execute":
        # ip_query_only write circuit: while open, every execute is rejected by
        # the venue anyway — short-circuit so we don't add load that prolongs
        # the ban or churn executors into FAILED on a doomed round-trip.
        if is_write_blocked(url):
            logger.debug("gateway budget: execute parked — write circuit open host=%s", _host(url))
            return False
        key = (wallet or "").strip().lower()
        if not key:
            return True  # no wallet to scope by; host circuit already checked
        if not _wallet_bucket(key).try_acquire(max_wait=wait, cost=w):
            logger.debug("gateway budget: wallet %s execute bucket starved (w=%s)", key[:10], w)
            return False
        return True

    uid: Optional[int] = int(user_id) if user_id is not None else None
    if uid is not None:
        with _lock:
            if _user_inflight.get(uid, 0) >= _USER_MAX_INFLIGHT:
                logger.debug("gateway budget: user %s in-flight cap", uid)
                return False
            _user_inflight[uid] = _user_inflight.get(uid, 0) + 1
    allowed = True
    try:
        from src.nadobro.core.http_session import throttle_host
        if not throttle_host(url, cost=w, max_wait=wait):
            allowed = False
        elif uid is not None and not _user_bucket(uid).try_acquire(max_wait=wait, cost=w):
            logger.debug("gateway budget: user %s token bucket starved (w=%s)", uid, w)
            allowed = False
    except Exception:
        pass
    if not allowed:
        release(uid)
        return False
    return True


def release(user_id: Optional[int] = None) -> None:
    if user_id is None:
        return
    uid = int(user_id)
    with _lock:
        current = _user_inflight.get(uid, 0)
        if current <= 1:
            _user_inflight.pop(uid, None)
        else:
            _user_inflight[uid] = current - 1


def snapshot() -> dict:
    with _lock:
        inflight = dict(_user_inflight)
        rl_open = {h: s.open_until for h, s in _gateway_rl.items() if time.time() < s.open_until}
    try:
        from src.nadobro.core.http_session import bucket_snapshot, breaker_snapshot
        host_buckets = bucket_snapshot()
        breakers = breaker_snapshot()
    except Exception:
        host_buckets = {}
        breakers = {}
    return {
        "user_buckets": len(_user_buckets),
        "wallet_buckets": len(_wallet_buckets),
        "edge_buckets": len(_edge_buckets),
        "user_inflight": inflight,
        "gateway_rl_open": rl_open,
        "host_buckets": host_buckets,
        "breakers": breakers,
        "limits": {
            "user_rps": _USER_RPS,
            "user_burst": _USER_BURST,
            "user_max_inflight": _USER_MAX_INFLIGHT,
            "wallet_rps": _WALLET_RPS,
            "wallet_burst": _WALLET_BURST,
            "edge_rps": _EDGE_RPS,
            "edge_burst": _EDGE_BURST,
        },
    }
