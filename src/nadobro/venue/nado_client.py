import logging
import threading
import time
import os
import random
import re
import asyncio
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional
from src.nadobro.utils.env import env_float, env_int
from src.nadobro.config import (
    NADO_TESTNET_REST, NADO_MAINNET_REST,
    NADO_TESTNET_ARCHIVE, NADO_MAINNET_ARCHIVE,
    get_product_name, get_perp_products, get_product_id,
    get_nado_builder_routing_config,
)

logger = logging.getLogger(__name__)

_price_cache = {}
_PRICE_CACHE_TTL = env_int("NADO_PRICE_CACHE_TTL_SECONDS", 10)
# Sized book depth. Shorter TTL than the price cache because depth is the input
# to imbalance/adverse-selection reads, where a 10s-old ladder is worse than no
# ladder — but long enough to collapse a burst of per-user structure reads on
# the same product into one query. Process-local by design: a Redis round-trip
# costs more than it saves at this TTL.
_liquidity_cache: dict = {}
_LIQUIDITY_CACHE_TTL = env_float("NADO_LIQUIDITY_CACHE_TTL_SECONDS", 2.0)
_ALL_PRODUCTS_CACHE = {}
# Product list is static metadata — align with catalog hourly refresh.
_ALL_PRODUCTS_TTL = env_int("NADO_ALL_PRODUCTS_CACHE_TTL_SECONDS", 3600)
# Shared, network-scoped Redis TTL for the *latest* candlestick query. Short by
# design (1m candles go stale fast) but long enough to collapse the per-tick
# 200-bar fetches that the dgrid candle_provider issues every cycle for every
# user into a single indexer query per window. Set to 0 to disable.
_CANDLES_CACHE_TTL = env_int("NADO_CANDLES_CACHE_TTL_SECONDS", 30)
# Product size/price increments are effectively static venue metadata. Cache
# the parsed map per network so each worker boot reuses one all_products query
# instead of re-fetching it. Aligns with the catalog hourly refresh.
_INCREMENTS_CACHE_TTL = env_int("NADO_INCREMENTS_CACHE_TTL_SECONDS", 3600)
# Last good full price snapshot per network. Served when the gateway budget
# is throttling/blocked so callers back off instead of fanning out to one
# REST call per product (which is what the gateway is meant to prevent).
_ALL_PRICES_CACHE = {}


def _candles_sorted_asc(candles: list) -> list:
    """Normalize a candle list to OLDEST-FIRST (chronological) order.

    The Nado indexer returns candles NEWEST-first while every consumer of
    ``get_candlesticks`` (EMA/ATR/RSI, regime gate, variance ratio, the
    backtester) assumes chronological input — reversed input silently flips
    trend classification and makes ``candles[-1]`` the stale oldest bar.
    Timestamps arrive as digit strings from the indexer; coerce to int so the
    sort is numeric, and fail open (input order) on malformed rows.
    """
    def _ts(c: object) -> int:
        try:
            get = c.get if isinstance(c, dict) else lambda k, d=None: getattr(c, k, d)  # type: ignore[union-attr]
            return int(get("time") or 0)
        except (TypeError, ValueError):
            return 0

    try:
        return sorted(candles, key=_ts)
    except Exception:  # noqa: BLE001 - never let ordering break a market read
        return list(candles)


def _format_sdk_error(exc: Exception, *, max_len: int = 200) -> str:
    """Compact SDK error text; never dump Cloudflare HTML into logs."""
    msg = str(exc)
    lower = msg.lower()
    if "just a moment" in lower or "<!doctype html" in lower:
        return "Cloudflare challenge (403 HTML response)"
    if len(msg) > max_len:
        return msg[:max_len] + "…"
    return msg
_FUNDING_CACHE = {}
_FUNDING_TTL = 10
# Shared lock for the price / open-orders / funding caches. Methods on
# NadoClient are routinely called via `run_blocking` from a 16-worker
# thread pool (see services/async_utils.py); without a lock, two workers
# can clobber each other's writes or read torn state. RLock so a method
# that holds the lock can still call into another locked helper. (Audit
# 2026-05.)
_caches_lock = threading.RLock()

# In-process TTL cache shared across calls within this worker process. Replaces
# the former Upstash (cross-process) cache layer for balance / catalog /
# increments / candlesticks. On a single-machine deployment a local dict is
# faster and has no network failure mode; on multi-process worker pools each
# process keeps its own copy (the previous cross-process sharing was the only
# thing Upstash bought us, and it is not worth a network round-trip here).
# Values are returned by reference, matching the existing ``_price_cache``
# convention — treat reads as read-only.
_shared_cache: "dict[str, tuple[float, object]]" = {}  # key -> (expires_at, value)

# Long-lived "last known balance" copy for the click/render path. The live
# balance still uses NADO_BALANCE_CACHE_TTL_SECONDS (30s); this only backs the
# non-blocking display read so a tap never falls through to the gateway.
_BALANCE_DISPLAY_TTL = env_int("NADO_BALANCE_DISPLAY_TTL_SECONDS", 900)


def _shared_cache_get(key: str):
    now = time.time()
    with _caches_lock:
        item = _shared_cache.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at < now:
            _shared_cache.pop(key, None)
            return None
        return value


def _shared_cache_set(key: str, value, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    with _caches_lock:
        _shared_cache[key] = (time.time() + float(ttl_seconds), value)
        if len(_shared_cache) > 4096:  # opportunistic eviction to bound memory
            now = time.time()
            for k in [k for k, (exp, _v) in _shared_cache.items() if exp < now]:
                _shared_cache.pop(k, None)


_size_increment_cache = {}
_price_increment_cache = {}
_size_increment_x18_cache = {}
_price_increment_x18_cache = {}
_min_size_x18_cache = {}
_REQUEST_TIMEOUT_SECONDS = env_float("NADO_HTTP_TIMEOUT_SECONDS", 6.0)
# The nado_protocol SDK builds plain ``requests.Session()`` objects and calls
# ``session.post/get`` with NO ``timeout=`` — so a stalled gateway connection
# (the exact failure mode of an ip_query_only / saturated host that accepts the
# socket then never replies) blocks the worker thread FOREVER. That is what
# wedges ``sync_active_users`` (holding the portfolio_sync max_instances slot)
# and can hang an order placement. We force a (connect, read) timeout onto every
# SDK session so no call can block indefinitely. Override via env.
_SDK_CONNECT_TIMEOUT_SECONDS = env_float("NADO_SDK_CONNECT_TIMEOUT_SECONDS", 5.0)
_SDK_READ_TIMEOUT_SECONDS = env_float("NADO_SDK_READ_TIMEOUT_SECONDS", 12.0)
# Capacity for legacy ThreadPool fan-out paths (e.g. per-product positions). Set
# conservatively to 2 so we never burst N concurrent requests into Cloudflare;
# the batched open-orders path (``get_subaccount_multi_products_open_orders``)
# avoids this fan-out entirely.
_FANOUT_WORKERS = env_int("NADO_FANOUT_WORKERS", 2)
_REST_MAX_RETRIES = env_int("NADO_REST_MAX_RETRIES", 2)
_REST_RETRY_BASE_SECONDS = env_float("NADO_REST_RETRY_BASE_SECONDS", 0.25)
_REST_RETRY_JITTER_SECONDS = env_float("NADO_REST_RETRY_JITTER_SECONDS", 0.2)
_REST_POOL_CONNECTIONS = env_int("NADO_HTTP_POOL_CONNECTIONS", 64)
_REST_POOL_MAXSIZE = env_int("NADO_HTTP_POOL_MAXSIZE", 64)
_OPEN_ORDERS_CACHE_TTL = env_float("NADO_OPEN_ORDERS_CACHE_TTL_SECONDS", 5.0)
_POSITIONS_FALLBACK_TTL = env_float("NADO_POSITIONS_FALLBACK_TTL_SECONDS", 6.0)
_POSITIONS_FALLBACK_MAX_PRODUCTS = env_int("NADO_POSITIONS_FALLBACK_MAX_PRODUCTS", 16)

# Nado requires a finite order expiration; limit/post-only orders used 3600s (1h) before.
# Default 7 days. Override with NADO_LIMIT_ORDER_EXPIRATION_SECONDS (seconds, min 60). IOC stays 10s.
_SEVEN_DAYS_SEC = 7 * 24 * 3600


def _limit_order_expiration_seconds() -> int:
    default = _SEVEN_DAYS_SEC
    raw = (os.environ.get("NADO_LIMIT_ORDER_EXPIRATION_SECONDS") or "").strip()
    if not raw:
        return default
    try:
        v = int(float(raw))
    except ValueError:
        logger.warning("Invalid NADO_LIMIT_ORDER_EXPIRATION_SECONDS=%r; using %s", raw, default)
        return default
    return max(60, v)
# Share the hardened session (browser-like UA + Accept headers) with the rest
# of the bot so Cloudflare's lightweight bot check lets our REST traffic
# through. The shared SESSION already mounts pool-sized HTTPS/HTTP adapters.
from src.nadobro.core.http_session import SESSION as _rest_session  # noqa: E402

# Per-user portfolio-sync reads run their blocking SDK work in the dedicated
# SDK thread pool, not the shared default executor that asyncio.to_thread uses.
# This isolates wedge-prone background polling from latency-critical paths
# (order execution, WS auth, Telegram-reply DB reads) that also fan work out to
# the default executor — a poll storm can no longer starve them.
from src.nadobro.core.async_utils import run_blocking_exec, run_blocking_sdk  # noqa: E402


def _install_session_timeout(session, timeout) -> bool:
    """Force a default ``timeout`` onto a ``requests.Session`` instance.

    All ``Session.get/post/put/...`` verbs funnel through ``Session.request``,
    so wrapping that one method injects the timeout for every call the SDK
    makes — without patching SDK source. Idempotent: marks the instance so a
    re-init (``_ensure_sdk_client``) does not double-wrap. ``timeout`` may be a
    float or a ``(connect, read)`` tuple. A caller-supplied ``timeout`` is never
    overridden.
    """
    if session is None or getattr(session, "_nado_timeout_installed", False):
        return False
    try:
        orig_request = session.request

        def _request(method, url, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = timeout
            return orig_request(method, url, **kwargs)

        session.request = _request  # type: ignore[assignment]
        session._nado_timeout_installed = True
        return True
    except Exception:  # noqa: BLE001
        return False


_open_orders_cache: dict[tuple[str, str, int], dict] = {}
_positions_fallback_cache: dict[tuple[str, str], dict] = {}

# verify_linked_signer is called on every order placement via
# ensure_active_wallet_ready. The result is stable across a session and the
# REST round-trip costs ~500-700ms — repeated checks before each order add up
# to multiple seconds of avoidable latency per DGRID cycle. Cache successful
# verifications for a short window; failures and errors bypass the cache.
_linked_signer_cache: dict[tuple[str, str, str], dict] = {}
_LINKED_SIGNER_CACHE_TTL_SECONDS = env_float("NADO_LINKED_SIGNER_CACHE_SECONDS", 60.0)

# Window applied to ``recvTime`` on signed query/execute payloads. The value
# is a *deadline* — Nado rejects the request with error_code 2011 ("Request
# received after 'recv_time'") if the server processes it after this stamp.
# Setting recv_time = now() leaves zero room for network latency, signing, or
# host clock drift; production logs showed 2011 on get_trigger_orders.
_RECV_TIME_WINDOW_SECONDS = max(
    5.0,
    env_float("NADO_RECV_TIME_WINDOW_SECONDS", 60.0),
)


def _recv_time_ms() -> int:
    """``recvTime`` deadline (ms epoch) padded by ``_RECV_TIME_WINDOW_SECONDS``."""
    return int((time.time() + _RECV_TIME_WINDOW_SECONDS) * 1000)


def _mask_address(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("0x") and len(text) >= 12:
        return f"{text[:6]}...{text[-4:]}"
    return text


_HEX_RUN_RE = re.compile(r"0x[0-9a-fA-F]{12,}")


def _mask_payload(value: object, *, limit: int = 600) -> str:
    """Render an arbitrary venue payload/exception for diagnostic logging:
    redact long 0x-hex runs (addresses/digests/subaccounts) and cap length so
    the FULL server reason is captured without leaking identifiers or spamming
    logs. Used to surface the complete ``ip_query_only`` body, which the bot
    previously discarded (only ``product_id`` was logged)."""
    text = str(value or "")
    text = _HEX_RUN_RE.sub("0x<REDACTED>", text)
    if len(text) > limit:
        text = text[:limit] + "…(truncated)"
    return text


# ---------------------------------------------------------------------------
# NO_ORDERS_AUDIT-FIX-R6b: process-wide NadoClient cache.
#
# Background: the logs showed "Initializing default mainnet context" firing
# every 1-30 seconds. Each one is a separate ~400 ms NadoClient construction
# (signer + query client + chain RPC handshakes). The sync_active_users job
# overran its 30 s interval as a direct consequence and APScheduler started
# skipping ticks.
#
# Cache strategy:
#  * Key for signing clients: ("signer", sha256(private_key)[:32], network).
#    The raw key never sits in the dict — only its truncated digest.
#  * Key for read-only clients: ("readonly", address.lower(), network).
#  * Returns an *already-initialized* client (we call .initialize() inside
#    the factory) so callers never re-init.
#  * Signer rotation detection: when a caller asks for a signing client and
#    the cached key for the user differs from the new private_key hash, the
#    old entry is evicted before constructing the new one. Callers route
#    rotations through ``invalidate_client_cache_for_user(user_id)`` for an
#    explicit clear.
#
# This is a "broad" cache as requested. The risk we explicitly accept is that
# a private key rotation that bypasses both the user-id eviction AND the
# digest check (e.g., the same address but a different key) cannot be
# detected — but since signing clients are derived from private keys, the
# digest WILL change whenever the underlying key changes. Verified by test.
# ---------------------------------------------------------------------------
import hashlib  # noqa: E402

_NADO_CLIENT_CACHE: "dict[tuple, NadoClient]" = {}
_NADO_CLIENT_CACHE_USER_INDEX: "dict[int, set[tuple]]" = {}
_NADO_CLIENT_CACHE_LOCK = threading.RLock()
_NADO_CLIENT_CACHE_STATS = {
    "signer_hit": 0, "signer_miss": 0, "signer_rotate": 0,
    "readonly_hit": 0, "readonly_miss": 0,
    "invalidate_user": 0, "clear_all": 0,
}


def _pk_digest(private_key: str) -> str:
    """Stable, non-reversible hash for cache keys. We truncate to 32 hex
    chars (128 bits) — collision-resistant for any realistic process and
    avoids leaking the full key shape into logs / dumps."""
    pk = (private_key or "").strip()
    if pk.startswith("0x"):
        pk = pk[2:]
    return hashlib.sha256(pk.encode("utf-8")).hexdigest()[:32]


def _track_user_key(user_id: Optional[int], cache_key: tuple) -> None:
    if user_id is None:
        return
    bucket = _NADO_CLIENT_CACHE_USER_INDEX.setdefault(int(user_id), set())
    bucket.add(cache_key)


def get_or_create_signing_client(
    private_key: str,
    network: str = "testnet",
    *,
    main_address: Optional[str] = None,
    user_id: Optional[int] = None,
) -> "NadoClient":
    """Return a cached, already-initialized signing NadoClient. Rotation-safe:
    a different ``private_key`` for the same ``user_id``+``network`` evicts
    the previous entry before constructing the new one."""
    digest = _pk_digest(private_key)
    cache_key = ("signer", digest, str(network))
    with _NADO_CLIENT_CACHE_LOCK:
        # Rotation detection: if the user already has signing keys cached,
        # they must all match the new digest; otherwise drop the stale ones.
        if user_id is not None:
            existing = list(_NADO_CLIENT_CACHE_USER_INDEX.get(int(user_id), set()))
            stale = [k for k in existing if k[0] == "signer" and k != cache_key]
            if stale:
                _NADO_CLIENT_CACHE_STATS["signer_rotate"] += 1
                for k in stale:
                    _NADO_CLIENT_CACHE.pop(k, None)
                    _NADO_CLIENT_CACHE_USER_INDEX[int(user_id)].discard(k)
        cached = _NADO_CLIENT_CACHE.get(cache_key)
        if cached is not None:
            _NADO_CLIENT_CACHE_STATS["signer_hit"] += 1
            return cached
        _NADO_CLIENT_CACHE_STATS["signer_miss"] += 1
        client = NadoClient(private_key, network, main_address=main_address)
        # Initialize INSIDE the cache build so callers don't re-pay the
        # ~400 ms cost on every retrieval.
        try:
            client.initialize()
        except Exception:  # noqa: BLE001
            logger.warning(
                "get_or_create_signing_client: initialize() raised; caching uninitialized client for %s",
                _mask_address(getattr(client, "address", "")), exc_info=True,
            )
        _NADO_CLIENT_CACHE[cache_key] = client
        _track_user_key(user_id, cache_key)
        return client


def get_or_create_readonly_client(
    address: str,
    network: str = "testnet",
    *,
    user_id: Optional[int] = None,
) -> "NadoClient":
    """Return a cached read-only NadoClient (``NadoClient.from_address``).
    Read-only clients don't need ``.initialize()`` — the SDK contexts they
    use are pulled lazily on the first query."""
    addr = str(address or "").strip().lower()
    cache_key = ("readonly", addr, str(network))
    with _NADO_CLIENT_CACHE_LOCK:
        cached = _NADO_CLIENT_CACHE.get(cache_key)
        if cached is not None:
            _NADO_CLIENT_CACHE_STATS["readonly_hit"] += 1
            return cached
        _NADO_CLIENT_CACHE_STATS["readonly_miss"] += 1
        client = NadoClient.from_address(address, network)
        _NADO_CLIENT_CACHE[cache_key] = client
        _track_user_key(user_id, cache_key)
        return client


def invalidate_client_cache_for_user(user_id: int) -> int:
    """Drop every cached client (signing + readonly) associated with the
    given user_id. Call this from the wallet rotation / unlink paths."""
    with _NADO_CLIENT_CACHE_LOCK:
        keys = _NADO_CLIENT_CACHE_USER_INDEX.pop(int(user_id), set())
        for k in list(keys):
            _NADO_CLIENT_CACHE.pop(k, None)
        _NADO_CLIENT_CACHE_STATS["invalidate_user"] += 1
        return len(keys)


def clear_nado_client_cache() -> None:
    """Wipe the entire cache. Mainly for tests and emergency operator use."""
    with _NADO_CLIENT_CACHE_LOCK:
        _NADO_CLIENT_CACHE.clear()
        _NADO_CLIENT_CACHE_USER_INDEX.clear()
        _NADO_CLIENT_CACHE_STATS["clear_all"] += 1


def get_nado_client_cache_stats() -> dict:
    """Diagnostic snapshot for observability endpoints."""
    with _NADO_CLIENT_CACHE_LOCK:
        return {
            **_NADO_CLIENT_CACHE_STATS,
            "size": len(_NADO_CLIENT_CACHE),
            "users_tracked": len(_NADO_CLIENT_CACHE_USER_INDEX),
        }


class NadoClient:
    # Class-level default so every construction path — including the
    # __init__-bypassing ``from_address`` (uses ``cls.__new__``) — exposes
    # this attribute. The gateway-budget gate reads it on every REST call;
    # a missing attribute crashes read-only clients before any network I/O.
    acting_user_id: Optional[int] = None

    def __init__(self, private_key: str, network: str = "testnet", main_address: str = None):
        self.private_key = private_key
        self.network = network
        self.client = None
        self.subaccount_hex = None
        self.address = None
        self.main_address = main_address
        self.acting_user_id: Optional[int] = None
        self._initialized = False
        self._derive_address()

    @classmethod
    def from_address(cls, address: str, network: str = "testnet") -> "NadoClient":
        instance = cls.__new__(cls)
        instance.private_key = None
        instance.network = network
        instance.client = None
        instance.address = address
        instance.main_address = address
        instance.acting_user_id = None
        instance._initialized = False
        try:
            from nado_protocol.utils.bytes32 import subaccount_to_hex
            instance.subaccount_hex = subaccount_to_hex(address, "default")
        except ImportError:
            default_bytes = "default".encode().hex()
            instance.subaccount_hex = address.lower() + default_bytes + "0" * (24 - len(default_bytes))
        return instance

    def _compute_subaccount_hex(self, address: str) -> str:
        try:
            from nado_protocol.utils.bytes32 import subaccount_to_hex
            return subaccount_to_hex(address, "default")
        except ImportError:
            default_bytes = "default".encode().hex()
            return address.lower() + default_bytes + "0" * (24 - len(default_bytes))

    def _derive_address(self):
        try:
            from eth_account import Account
            acct = Account.from_key(self.private_key)
            self.address = acct.address
            query_addr = self.main_address or self.address
            self.subaccount_hex = self._compute_subaccount_hex(query_addr)
        except Exception as e:
            logger.error(f"Failed to derive address from private key: {e}")

    def initialize(self):
        if self._initialized:
            return True
        try:
            from nado_protocol.client import create_nado_client, NadoClientMode

            mode = NadoClientMode.TESTNET if self.network == "testnet" else NadoClientMode.MAINNET
            self.client = create_nado_client(mode, self.private_key)
            self._install_sdk_timeouts()
            signer = getattr(getattr(self.client, "context", None), "signer", None)
            signer_address = getattr(signer, "address", None)
            if signer_address:
                self.address = signer_address
            query_addr = self.main_address or self.address
            if not query_addr:
                raise RuntimeError("Nado client requires a signer or read-only address")
            self.subaccount_hex = self._compute_subaccount_hex(query_addr)
            self._initialized = True
            self.private_key = None  # Clear raw key after SDK init
            logger.info(
                "Nado client initialized: signer=%s, query=%s, network=%s",
                _mask_address(self.address), _mask_address(query_addr), self.network,
            )
            return True
        except ImportError:
            logger.warning("nado_protocol not installed, using REST API fallback")
            self._initialized = False
            return False
        except Exception as e:
            logger.error(f"Failed to initialize Nado client: {e}")
            self._initialized = False
            return False

    def _install_sdk_timeouts(self) -> None:
        """Attach a (connect, read) timeout to every SDK ``requests.Session``.

        ``EngineClient`` (query+execute), ``IndexerClient`` and the optional
        ``TriggerClient`` each own one ``self.session``; the SDK never passes
        ``timeout=`` on its calls, so without this a hung connection wedges the
        worker thread permanently. Best-effort: any structural drift in the SDK
        is swallowed so client init never fails on account of this hardening.
        """
        timeout = (_SDK_CONNECT_TIMEOUT_SECONDS, _SDK_READ_TIMEOUT_SECONDS)
        ctx = getattr(self.client, "context", None)
        if ctx is None:
            return
        installed = 0
        for attr in ("engine_client", "indexer_client", "trigger_client"):
            sub = getattr(ctx, attr, None)
            if sub is None:
                continue
            if _install_session_timeout(getattr(sub, "session", None), timeout):
                installed += 1
        if installed:
            logger.info(
                "Nado SDK session timeouts installed connect=%ss read=%ss sessions=%s network=%s",
                _SDK_CONNECT_TIMEOUT_SECONDS, _SDK_READ_TIMEOUT_SECONDS, installed, self.network,
            )

    def _rest_url(self):
        return NADO_MAINNET_REST if self.network == "mainnet" else NADO_TESTNET_REST

    def _archive_url(self):
        return NADO_MAINNET_ARCHIVE if self.network == "mainnet" else NADO_TESTNET_ARCHIVE

    def _gateway_allowed(
        self,
        *,
        weight: float = 1.0,
        kind: str = "query",
        url: Optional[str] = None,
        wallet: Optional[str] = None,
        user_scoped: bool = True,
    ) -> bool:
        """Reserve gateway budget for one call. ``weight`` is the documented
        Nado weight (see ``nado_weights``). ``kind="query"`` charges the per-IP
        host budget (default ``_rest_url`` host; pass ``url=self._archive_url()``
        for indexer reads). ``kind="execute"`` charges the per-wallet budget.

        ``user_scoped`` paths take an in-flight slot and MUST call
        :meth:`_gateway_release`; execute/archive paths set it False (token
        bucket only, nothing to release).
        """
        from src.nadobro.venue.gateway_budget import try_acquire
        target = url or self._rest_url()
        uid = getattr(self, "acting_user_id", None) if user_scoped else None
        return try_acquire(target, user_id=uid, weight=weight, kind=kind, wallet=wallet)

    def _gateway_release(self) -> None:
        from src.nadobro.venue.gateway_budget import release
        release(getattr(self, "acting_user_id", None))

    def _record_gateway_error(self, exc: Exception) -> None:
        from src.nadobro.venue.gateway_budget import is_rate_limit_error, record_gateway_failure
        if is_rate_limit_error(exc):
            record_gateway_failure(self._rest_url(), exc)

    @staticmethod
    def _parse_json_response(resp) -> Optional[dict]:
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            snippet = ""
            try:
                snippet = (resp.text or "").strip().replace("\n", " ")[:180]
            except Exception:
                snippet = ""
            status_code = getattr(resp, "status_code", "?")
            content_type = (getattr(resp, "headers", {}) or {}).get("content-type") or ""
            # Dedup the Cloudflare-challenge flood: this branch was emitting
            # tens of identical WARNINGs per second when the venue was
            # challenging us. Route those through the throttled CF logger.
            if status_code == 403 and "text/html" in content_type.lower():
                try:
                    from src.nadobro.core.http_session import _log_cf_warning  # noqa: WPS437

                    _log_cf_warning(getattr(resp, "url", ""), status_code, snippet)
                except Exception:  # policy: degrade-ok(log-dedup helper is best-effort)
                    pass
            else:
                logger.warning(
                    "REST returned non-JSON status=%s content_type=%s body=%r",
                    status_code, content_type, snippet,
                )
            return None

    def _query_rest(self, query_type: str, extra_params: Optional[dict] = None) -> Optional[dict]:
        from src.nadobro.venue.nado_weights import query_weight
        if not self._gateway_allowed(weight=query_weight(query_type, extra_params)):
            return None
        params = {"type": query_type}
        if extra_params:
            params.update(extra_params)
        url = f"{self._rest_url()}/query"
        headers = {"Accept-Encoding": "gzip"}
        use_post = query_type in {"market_prices", "orders"} or isinstance((extra_params or {}).get("product_ids"), list)
        max_attempts = max(1, _REST_MAX_RETRIES + 1)
        try:
            for attempt in range(max_attempts):
                try:
                    if use_post:
                        resp = _rest_session.post(url, json=params, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
                    else:
                        resp = _rest_session.get(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
                    data = self._parse_json_response(resp)
                    if data is not None:
                        return data
                    if attempt < (max_attempts - 1):
                        sleep_s = (_REST_RETRY_BASE_SECONDS * (2 ** attempt)) + random.uniform(0.0, _REST_RETRY_JITTER_SECONDS)
                        time.sleep(sleep_s)
                        continue
                    return None
                except requests.RequestException as e:
                    self._record_gateway_error(e)
                    if attempt >= (max_attempts - 1):
                        logger.error("REST query failed type=%s attempts=%s: %s", query_type, max_attempts, e)
                        return None
                    sleep_s = (_REST_RETRY_BASE_SECONDS * (2 ** attempt)) + random.uniform(0.0, _REST_RETRY_JITTER_SECONDS)
                    time.sleep(sleep_s)
                except Exception as e:
                    logger.error("REST query failed type=%s unexpected: %s", query_type, e)
                    return None
            return None
        finally:
            self._gateway_release()

    @staticmethod
    def _to_plain(value):
        """Convert SDK/Pydantic objects into plain Python containers."""
        if value is None or isinstance(value, (str, int, float, bool, Decimal)):
            return value
        if isinstance(value, list):
            return [NadoClient._to_plain(v) for v in value]
        if isinstance(value, tuple):
            return [NadoClient._to_plain(v) for v in value]
        if isinstance(value, dict):
            return {str(k): NadoClient._to_plain(v) for k, v in value.items()}
        if hasattr(value, "dict"):
            try:
                return NadoClient._to_plain(value.dict())
            except Exception:  # policy: degrade-ok(serializer falls through to next strategy)
                pass
        if hasattr(value, "__dict__"):
            return NadoClient._to_plain(
                {k: v for k, v in vars(value).items() if not k.startswith("_")}
            )
        return value

    def _ensure_sdk_client(self) -> bool:
        if self._initialized and self.client:
            return True
        if self.private_key is not None or self.address or self.main_address:
            return self.initialize()
        return False

    def get_market_price(self, product_id: int) -> dict:
        cache_key = f"{self.network}:{product_id}"
        with _caches_lock:
            cached = _price_cache.get(cache_key)
        if cached and (time.time() - cached["ts"] < _PRICE_CACHE_TTL):
            return cached["data"]

        if self._initialized and self.client:
            try:
                from nado_protocol.utils.math import from_x18
                mp = self.client.context.engine_client.get_market_price(product_id)
                bid = from_x18(int(mp.bid_x18))
                ask = from_x18(int(mp.ask_x18)) if hasattr(mp, 'ask_x18') else bid
                result = {"bid": float(bid), "ask": float(ask), "mid": float((bid + ask) / 2)}
                with _caches_lock:
                    _price_cache[cache_key] = {"data": result, "ts": time.time()}
                return result
            except Exception as e:
                logger.error("SDK get_market_price failed: %s", _format_sdk_error(e))

        try:
            data = self._query_rest("market_price", {"product_id": product_id}) or {}
            if data.get("status") == "success":
                bid = int(data["data"]["bid_x18"]) / 1e18
                ask = int(data["data"].get("ask_x18", data["data"]["bid_x18"])) / 1e18
                result = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
                with _caches_lock:
                    _price_cache[cache_key] = {"data": result, "ts": time.time()}
                return result
        except Exception as e:
            logger.error(f"REST get_market_price failed: {e}")

        return {"bid": 0, "ask": 0, "mid": 0}

    def get_market_liquidity(self, product_id: int, depth: int = 10) -> dict:
        """Sized order-book depth for a product.

        Returns ``{"bids": [[price, size], ...], "asks": [...], "timestamp": t}``
        with prices/sizes decoded from x18 and levels ordered best-first (bids
        descending, asks ascending). Returns empty sides on any failure — depth
        is an enrichment, and every consumer must stay fail-open without it.

        ``get_market_price`` (top-of-book only) remains the hot path for mids
        and is deliberately untouched: this query carries the full ladder and is
        meant for the structure/microstructure read at its own cadence, not for
        every tick of every strategy.
        """
        depth = max(1, int(depth))
        cache_key = f"{self.network}:{product_id}:{depth}"
        with _caches_lock:
            cached = _liquidity_cache.get(cache_key)
        if cached and (time.time() - cached["ts"] < _LIQUIDITY_CACHE_TTL):
            return cached["data"]

        if not self._initialized or not self.client:
            if self.private_key is not None or self.address or self.main_address:
                self.initialize()
            if not self._initialized or not self.client:
                return {"bids": [], "asks": [], "timestamp": 0.0}

        from src.nadobro.venue.nado_weights import query_weight
        if not self._gateway_allowed(weight=query_weight("market_liquidity")):
            logger.debug(
                "get_market_liquidity throttled by gateway budget product_id=%s",
                product_id,
            )
            return {"bids": [], "asks": [], "timestamp": 0.0}

        try:
            from nado_protocol.utils.math import from_x18

            data = self.client.market.get_market_liquidity(int(product_id), depth)

            def _levels(raw) -> list:
                # The SDK models a level as a 2-element list of x18 strings:
                # ``[price_x18, size_x18]``. Tolerate objects too so a future
                # SDK shape change degrades to empty rather than raising.
                out = []
                for lvl in raw or []:
                    try:
                        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                            price = float(from_x18(int(lvl[0])))
                            size = float(from_x18(int(lvl[1])))
                        else:
                            price = float(from_x18(int(getattr(lvl, "price_x18", 0) or 0)))
                            size = float(from_x18(int(getattr(lvl, "amount_x18", 0) or 0)))
                    except (TypeError, ValueError):
                        continue
                    if price > 0 and size > 0:
                        out.append([price, size])
                return out

            bids = sorted(_levels(getattr(data, "bids", None)), key=lambda r: -r[0])
            asks = sorted(_levels(getattr(data, "asks", None)), key=lambda r: r[0])
            result = {"bids": bids, "asks": asks, "timestamp": time.time()}
            if bids or asks:
                with _caches_lock:
                    _liquidity_cache[cache_key] = {"data": result, "ts": time.time()}
            return result
        except Exception as e:
            logger.warning(
                "SDK get_market_liquidity failed product_id=%s depth=%s: %s",
                product_id, depth, _format_sdk_error(e),
            )
            return {"bids": [], "asks": [], "timestamp": 0.0}

    def get_candlesticks(self, product_id: int, timeframe: str = "1h", limit: int = 200, max_time: int | None = None) -> list[dict]:
        """Fetch OHLCV candles from the Nado indexer through the official SDK.

        Returns candles OLDEST-FIRST. CANDLE-ORDER fix (2026-07-31): the
        indexer serves candles NEWEST-first, and every downstream consumer
        (regime gate, EMA/ATR/RSI, dgrid variance ratio, vol signal filter,
        market_features, HOWL backtester) assumes chronological order — the
        regime gate was reading a 3.3h-old close as "last" and classifying the
        trend backwards. Normalize once here so no consumer can regress.
        """
        if not self._initialized or not self.client:
            if self.private_key is not None:
                self.initialize()
            if not self._initialized or not self.client:
                return []
        # Shared Redis cache for the live "latest" window (max_time is None).
        # Historical/paginated queries (max_time set) are not cached — they are
        # rare and would pollute the shared key. A hit here avoids both the
        # gateway-budget spend and the indexer query entirely.
        candles_redis_key = None
        if max_time is None and _CANDLES_CACHE_TTL > 0:
            candles_redis_key = (
                f"nado:candles:{self.network}:{int(product_id)}:{timeframe}:{int(limit)}"
            )
            cached = self._read_shared_cache(candles_redis_key)
            if isinstance(cached, list):
                # Sort cache hits too: entries written before this fix (or by an
                # older worker during a rolling deploy) are newest-first.
                return _candles_sorted_asc(cached)
        from src.nadobro.venue.nado_weights import query_weight
        if not self._gateway_allowed(
            weight=query_weight("candlesticks", {"limit": limit}),
            url=self._archive_url(),
            user_scoped=False,
        ):
            # Previously a silent []. This denial is the upstream cause of
            # dgrid's "no spawn" — surface it so an empty candle result is
            # always traceable to a gateway-budget throttle.
            logger.warning(
                "get_candlesticks throttled by gateway budget product_id=%s "
                "timeframe=%s limit=%s — returning empty (cache miss)",
                product_id, timeframe, limit,
            )
            return []
        try:
            from nado_protocol.indexer_client.types.query import IndexerCandlesticksParams
            from nado_protocol.indexer_client.types.models import IndexerCandlesticksGranularity
            from nado_protocol.utils.math import from_x18

            granularity_map = {
                "1m": IndexerCandlesticksGranularity.ONE_MINUTE,
                "5m": getattr(IndexerCandlesticksGranularity, "FIVE_MINUTES", 300),
                "15m": getattr(IndexerCandlesticksGranularity, "FIFTEEN_MINUTES", 900),
                "1h": getattr(IndexerCandlesticksGranularity, "ONE_HOUR", 3600),
                "4h": getattr(IndexerCandlesticksGranularity, "FOUR_HOURS", 14400),
                "1d": getattr(IndexerCandlesticksGranularity, "ONE_DAY", 86400),
            }
            params = IndexerCandlesticksParams(
                product_id=int(product_id),
                granularity=granularity_map.get(str(timeframe), granularity_map["1h"]),
                max_time=max_time,
                limit=int(limit),
            )
            data = self.client.market.get_candlesticks(params)
            rows = getattr(data, "candlesticks", None) or getattr(data, "data", None) or data
            candles = []
            for row in rows or []:
                get = row.get if isinstance(row, dict) else lambda k, default=None: getattr(row, k, default)
                candles.append(
                    {
                        "time": get("timestamp") or get("time") or get("start_time") or get("update_time"),
                        "open": float(from_x18(int(get("open_x18", 0) or 0))),
                        "high": float(from_x18(int(get("high_x18", 0) or 0))),
                        "low": float(from_x18(int(get("low_x18", 0) or 0))),
                        "close": float(from_x18(int(get("close_x18", 0) or 0))),
                        "volume": float(get("volume", 0) or 0),
                    }
                )
            candles = _candles_sorted_asc(candles)
            if candles_redis_key is not None and candles:
                self._write_shared_cache(candles_redis_key, candles, _CANDLES_CACHE_TTL)
            return candles
        except Exception as e:
            logger.warning("SDK get_candlesticks failed product_id=%s timeframe=%s: %s", product_id, timeframe, e)
            return []

    def get_all_market_prices(self) -> dict:
        prices = {}
        try:
            product_ids = []
            for name in get_perp_products(network=self.network, client=self):
                pid = get_product_id(name, network=self.network, client=self)
                if pid is not None:
                    product_ids.append(int(pid))
            if product_ids:
                data = self._query_rest("market_prices", {"product_ids": product_ids}) or {}
            else:
                data = {}
            if data.get("status") == "success":
                payload = data.get("data", {}) or {}
                rows = payload.get("market_prices")
                if rows is None and isinstance(payload, list):
                    rows = payload
                if rows is None and isinstance(payload, dict):
                    rows = payload.get("prices") or payload.get("markets")
                if isinstance(rows, dict):
                    rows = list(rows.values())
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    try:
                        pid = int(row.get("product_id"))
                    except Exception:  # policy: degrade-ok(malformed price row; skipped)
                        continue
                    name = str(get_product_name(pid, network=self.network, client=self)).replace("-PERP", "")
                    bid = self._from_x18_dynamic(row.get("bid_x18") or row.get("bid") or row.get("price_x18") or row.get("price"))
                    ask = self._from_x18_dynamic(row.get("ask_x18") or row.get("ask") or row.get("price_x18") or row.get("price"))
                    if bid <= 0 and ask > 0:
                        bid = ask
                    if ask <= 0 and bid > 0:
                        ask = bid
                    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
                    if mid > 0:
                        prices[name] = {"bid": float(bid), "ask": float(ask), "mid": float(mid)}
                if prices:
                    with _caches_lock:
                        _ALL_PRICES_CACHE[self.network] = {"data": prices, "ts": time.time()}
                    return prices
        except Exception as e:
            logger.debug("market_prices bulk query unavailable, falling back to fanout: %s", e)

        # The batched single-request path came back empty. If the gateway is
        # throttling/blocking this host, the budget contract requires us to
        # back off and serve cached data — NOT fan out to one REST call per
        # product, which would amplify load exactly when we must reduce it.
        from src.nadobro.venue.gateway_budget import is_gateway_blocked
        if is_gateway_blocked(self._rest_url()):
            with _caches_lock:
                cached = _ALL_PRICES_CACHE.get(self.network)
            return dict(cached["data"]) if cached else {}

        perp_products = []
        for name in get_perp_products(network=self.network, client=self):
            pid = get_product_id(name, network=self.network, client=self)
            if pid is not None:
                perp_products.append((name, pid))
        with ThreadPoolExecutor(max_workers=max(1, _FANOUT_WORKERS)) as pool:
            futures = {pool.submit(self.get_market_price, pid): name for name, pid in perp_products}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    prices[name] = fut.result()
                except Exception:
                    prices[name] = {"bid": 0, "ask": 0, "mid": 0}
        return prices

    def get_perp_contracts(self) -> dict:
        """Best-effort contracts/tickers payload used by miniapp quotes.

        Returns a map keyed by ticker id (e.g. BTC-PERP_USDT). Never raises.
        """
        try:
            # Future-compatible: if gateway adds a contracts query, use it.
            data = self._query_rest("contracts") or {}
            if data.get("status") == "success":
                payload = data.get("data") or {}
                if isinstance(payload, dict):
                    rows = payload.get("contracts") or payload.get("tickers") or payload
                    if isinstance(rows, dict):
                        return rows
                    if isinstance(rows, list):
                        out: dict[str, dict] = {}
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            key = str(
                                row.get("ticker_id")
                                or row.get("symbol")
                                or row.get("ticker")
                                or row.get("name")
                                or ""
                            )
                            if key:
                                out[key] = row
                        return out
        except Exception:  # policy: degrade-ok(payload shape unrecognized; falls through to fallback source)
            pass
        return {}

    def get_balance(self, force: bool = False, cache_only: bool = False) -> dict:
        # Redis cache layer. Three-fold purpose:
        #   1. Smooth over transient "Too Many Requests" on query_subaccount_info
        #      so callers that conflate exists=False with "not linked" don't
        #      flip the user's UI state on a rate-limit blip.
        #   2. Reduce IP weight burned by the portfolio_sync tick on
        #      subaccount_info reads (weight=2 per call).
        #   3. READ-THROUGH: serve a fresh cached value (within TTL) WITHOUT
        #      touching the gateway at all. Previously the happy path always
        #      issued the weight-2 ``get_subaccount_info`` call and only fell
        #      back to Redis on throttle/error — so a tight portfolio_sync poll
        #      burned one gateway read per cycle even though the cached value
        #      was still fresh. Under gateway saturation that steady stream of
        #      reads is exactly what trips Nado's per-IP ``ip_query_only``
        #      downgrade (which then blocks ALL writes, incl. strategy orders).
        #      The Redis TTL already bounds staleness to ``balance_ttl_seconds``
        #      (an expired key returns None), so honoring it here is safe and
        #      matches the documented freshness tolerance. ``force=True`` lets
        #      callers that need a real-time balance bypass the cache.
        # TTL kept short (30s default) so wallet deposit / withdraw / fill
        # visibility doesn't lag noticeably.
        redis_key = f"balance:{self.network}:{self.subaccount_hex}"
        balance_ttl_seconds = env_int("NADO_BALANCE_CACHE_TTL_SECONDS", 30)

        if not force and balance_ttl_seconds > 0:
            cached = self._read_balance_cache(redis_key)
            if cached is not None:
                return cached

        # PERF (click path): never block a button tap on the gateway. Return the
        # freshest cached value, else the long-lived display copy, else a
        # pending sentinel the UI can render as "updating…". The caller is
        # expected to warm the cache out-of-band (background SDK pool submit or
        # the portfolio sync tick) so the next render is fresh.
        if cache_only:
            disp = self._read_balance_cache(redis_key) or self._read_balance_cache(f"{redis_key}:display")
            if disp is not None:
                return disp
            return {"exists": False, "balances": {}, "pending": True}

        if self._initialized and self.client:
            if not self._gateway_allowed(weight=2):  # subaccount_info: IP weight 2
                # Throttled by our own gateway budget — try Redis before
                # surrendering a misleading exists=False.
                cached = self._read_balance_cache(redis_key)
                if cached is not None:
                    return cached
                return {"exists": False, "balances": {}}
            try:
                from nado_protocol.utils.math import from_x18
                info = self.client.context.engine_client.get_subaccount_info(self.subaccount_hex)
                balances = {}
                if info.exists and info.spot_balances:
                    for sb in info.spot_balances:
                        bal = from_x18(int(sb.balance.amount))
                        balances[sb.product_id] = float(bal)
                result = {"exists": bool(info.exists), "balances": balances}
                self._write_balance_cache(redis_key, result, balance_ttl_seconds)
                return result
            except Exception as e:
                self._record_gateway_error(e)
                logger.error("SDK get_balance failed: %s", _format_sdk_error(e))
                # On *any* SDK failure (rate-limit, Cloudflare, transient
                # network), prefer the last known-good snapshot to a hard
                # exists=False that downstream UI may render as "not linked".
                cached = self._read_balance_cache(redis_key)
                if cached is not None:
                    return cached
            finally:
                self._gateway_release()

        try:
            data = self._query_rest("subaccount_info", {"subaccount": self.subaccount_hex}) or {}
            if data.get("status") == "success":
                data_payload = data.get("data", {}) or {}
                exists_field = data_payload.get("exists")
                balances = {}
                for sb in data_payload.get("spot_balances", []):
                    bal = int(sb["balance"]["amount"]) / 1e18
                    balances[sb["product_id"]] = bal
                exists = bool(exists_field) if exists_field is not None else bool(balances)
                result = {"exists": exists, "balances": balances}
                self._write_balance_cache(redis_key, result, balance_ttl_seconds)
                return result
        except Exception as e:
            logger.error(f"REST get_balance failed: {e}")

        # REST path also failed — last resort cache before exists=False.
        cached = self._read_balance_cache(redis_key)
        if cached is not None:
            return cached
        return {"exists": False, "balances": {}}

    def _read_balance_cache(self, cache_key: str) -> Optional[dict]:
        cached = _shared_cache_get(cache_key)
        if isinstance(cached, dict) and "balances" in cached:
            return cached
        return None

    def _write_balance_cache(self, cache_key: str, value: dict, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        # Store a shallow copy so a later mutation of the returned result can't
        # corrupt the cached snapshot. Balance dicts are small.
        snapshot = {
            "exists": value.get("exists"),
            "balances": dict(value.get("balances") or {}),
        }
        _shared_cache_set(cache_key, snapshot, ttl_seconds)
        # PERF: also keep a long-lived "display" copy. The click path
        # (``cache_only=True``) renders the home/dashboard card from this
        # without ever touching the gateway, so a tap is a microsecond memory
        # read instead of a 5–30s blocking ``get_subaccount_info`` on a
        # throttled / ip_query_only host. Staleness is bounded by how often a
        # real fetch refreshes it; freshness for the live number still comes
        # from the short-TTL key above.
        _shared_cache_set(f"{cache_key}:display", snapshot, _BALANCE_DISPLAY_TTL)

    # -- generic in-process cache helpers ---------------------------------
    # Network-scoped (NOT per-user) cache used to absorb rate-limit pressure
    # from Nado's per-IP gateway for data that is effectively static across
    # users — the product catalog, increments, and (briefly) candlesticks.
    # Backed by the in-process ``_shared_cache`` (Upstash removed). Best-effort:
    # a miss falls through to the live SDK path.
    @staticmethod
    def _read_shared_cache(cache_key: str):
        return _shared_cache_get(cache_key)

    @staticmethod
    def _write_shared_cache(cache_key: str, value, ttl_seconds: int) -> None:
        _shared_cache_set(cache_key, value, ttl_seconds)

    def get_open_orders(self, product_id: int, refresh: bool = False, sender: Optional[str] = None) -> list:
        eff_sender = (sender or "").strip() or self.subaccount_hex
        cache_key = (self.network, str(eff_sender or ""), int(product_id))
        if not refresh:
            with _caches_lock:
                cached = _open_orders_cache.get(cache_key)
            if cached and (time.time() - float(cached.get("ts", 0))) < _OPEN_ORDERS_CACHE_TTL:
                return list(cached.get("data") or [])
        if self._initialized and self.client:
            if not self._gateway_allowed(weight=2):  # subaccount_orders (1 product): IP weight 2
                with _caches_lock:
                    cached = _open_orders_cache.get(cache_key)
                if cached:
                    return list(cached.get("data") or [])
                return []
            try:
                from nado_protocol.utils.math import from_x18
                orders_data = self.client.context.engine_client.get_subaccount_open_orders(product_id, eff_sender)
                orders = []
                for o in orders_data.orders:
                    amount = from_x18(int(o.amount))
                    price = from_x18(int(o.price_x18))
                    orders.append({
                        "digest": o.digest,
                        "amount": float(amount),
                        "price": float(price),
                        "side": "LONG" if float(amount) > 0 else "SHORT",
                        "product_id": product_id,
                        "product_name": get_product_name(product_id),
                    })
                with _caches_lock:
                    _open_orders_cache[cache_key] = {"data": orders, "ts": time.time()}
                return orders
            except Exception as e:
                self._record_gateway_error(e)
                logger.error("SDK get_open_orders failed: %s", _format_sdk_error(e))
            finally:
                self._gateway_release()
        try:
            # Read-only/runtime clients rely on REST, so keep parity with SDK path.
            data = self._query_rest(
                "subaccount_orders",
                {"sender": eff_sender, "product_id": product_id},
            ) or {}
            if data.get("status") == "success":
                payload = data.get("data", {}) or {}
                rows = payload.get("orders")
                if rows is None and isinstance(payload, list):
                    rows = payload
                if rows is None:
                    rows = data.get("orders")
                orders = []
                for o in rows or []:
                    if not isinstance(o, dict):
                        continue
                    digest = o.get("digest") or o.get("order_digest") or o.get("id")
                    amount_raw = (
                        o.get("amount")
                        or o.get("size")
                        or o.get("amount_x18")
                        or o.get("size_x18")
                        or 0
                    )
                    price_raw = o.get("price") or o.get("price_x18") or 0
                    amount = self._from_x18_dynamic(amount_raw)
                    price = self._from_x18_dynamic(price_raw)
                    if not digest:
                        continue
                    orders.append(
                        {
                            "digest": str(digest),
                            "amount": abs(float(amount)),
                            "price": float(price),
                            "side": self._normalize_side(
                                raw_side=o.get("side"),
                                raw_is_long=o.get("is_long"),
                                raw_direction=o.get("direction"),
                                signed_amount=float(amount),
                            ),
                            "product_id": product_id,
                            "product_name": get_product_name(product_id),
                        }
                    )
                with _caches_lock:
                    _open_orders_cache[cache_key] = {"data": orders, "ts": time.time()}
                return orders
        except Exception as e:
            logger.error("REST get_open_orders failed: %s", e)
        with _caches_lock:
            _open_orders_cache[cache_key] = {"data": [], "ts": time.time()}
        return []

    def _open_orders_for_sender_batched(
        self,
        sender: str,
        product_ids: list[int],
        *,
        refresh: bool = False,
        strict: bool = False,
    ) -> list[dict]:
        """One gateway call that returns open orders for ``sender`` across
        every product in ``product_ids``. Replaces the per-product fan-out
        that previously issued N requests per sender."""
        if not sender or not product_ids:
            return []
        if not self._ensure_sdk_client():
            if strict:
                raise RuntimeError("SDK client unavailable for open-order sync")
            return []

        # "Orders" query: IP weight = 2 * product_ids.length
        if not self._gateway_allowed(weight=2 * max(1, len(product_ids))):
            return []

        try:
            from nado_protocol.utils.math import from_x18

            data = self.client.context.engine_client.get_subaccount_multi_products_open_orders(
                product_ids, sender
            )
        except Exception as e:
            self._record_gateway_error(e)
            logger.error(
                "SDK get_subaccount_multi_products_open_orders failed: %s",
                _format_sdk_error(e),
            )
            if strict:
                raise RuntimeError(
                    f"SDK get_subaccount_multi_products_open_orders failed: {_format_sdk_error(e)}"
                ) from e
            return []
        finally:
            self._gateway_release()

        rows: list[dict] = []
        now = time.time()
        for product_block in getattr(data, "product_orders", None) or []:
            pid = int(getattr(product_block, "product_id", 0) or 0)
            block_orders: list[dict] = []
            for o in getattr(product_block, "orders", None) or []:
                try:
                    amount = from_x18(int(o.amount))
                    price = from_x18(int(o.price_x18))
                except Exception as e:
                    logger.debug("open-order row unparsable (pid=%s) — order hidden from this read: %s", pid, e)
                    continue
                block_orders.append(
                    {
                        "digest": o.digest,
                        "amount": float(amount),
                        "price": float(price),
                        "side": "LONG" if float(amount) > 0 else "SHORT",
                        "product_id": pid,
                        "product_name": get_product_name(pid, network=self.network, client=self),
                    }
                )
            cache_key = (self.network, str(sender), pid)
            with _caches_lock:
                _open_orders_cache[cache_key] = {"data": list(block_orders), "ts": now}
            rows.extend(block_orders)
        return rows

    def _open_order_product_ids(self, *, include_spot: bool = True, refresh: bool = False) -> list[int]:
        """Product ids the portfolio open-order sweep should query.

        Portfolio needs one consolidated "open limit orders" view. Historically
        this only included perps, which made Volume-bot spot orders invisible
        even though single-product spot order reads already worked.
        """
        product_ids: list[int] = []
        seen: set[int] = set()

        def _add(pid) -> None:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                return
            if pid_int in seen:
                return
            seen.add(pid_int)
            product_ids.append(pid_int)

        for name in get_perp_products(network=self.network, client=self):
            _add(get_product_id(name, network=self.network, client=self))

        if include_spot:
            try:
                from src.nadobro.venue.product_catalog import get_spot_catalog

                quote_like = {"USDC", "USDC0", "USDT", "USDT0", "USD"}
                catalog = get_spot_catalog(network=self.network, refresh=refresh)
                spots = catalog.get("spots") or {}
                sorted_spots = sorted(
                    (row for row in spots.values() if isinstance(row, dict)),
                    key=lambda row: int(row.get("id") or 1_000_000),
                )
                for row in sorted_spots:
                    base = str(row.get("base") or row.get("symbol") or "").upper().strip()
                    if base and base not in quote_like:
                        _add(row.get("id"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("spot product ids unavailable for open-order sweep: %s", exc, exc_info=True)

        return product_ids

    def get_all_open_orders(
        self,
        refresh: bool = False,
        *,
        include_isolated: bool = True,
        include_spot: bool = True,
        strict: bool = False,
    ) -> list[dict]:
        """Fetch open orders for every tracked product on a single sender in **one**
        gateway call (per sender), instead of the previous ``products × senders``
        ThreadPool fan-out.

        ``include_isolated=False`` (used by the background portfolio poller
        when the user has no known isolated positions) skips the archive query
        AND the extra batched call per child subaccount, dropping the
        per-user cost to a single round-trip.
        """
        product_ids = self._open_order_product_ids(include_spot=include_spot, refresh=refresh)
        if not product_ids:
            if strict:
                raise RuntimeError("product catalog unavailable for open-order sync")
            return []

        rows: list[dict] = []
        parent = self.subaccount_hex or ""
        if parent:
            rows.extend(self._open_orders_for_sender_batched(parent, product_ids, refresh=refresh, strict=strict))
        elif strict:
            raise RuntimeError("subaccount unavailable for open-order sync")

        if not include_isolated:
            return rows

        isolated: list[str] = []
        try:
            isolated = self._isolated_subaccount_hexes() or []
        except Exception as exc:
            logger.debug("isolated open-order discovery skipped: %s", exc, exc_info=True)
            if strict:
                raise RuntimeError(f"isolated open-order discovery failed: {exc}") from exc
        for iso in isolated:
            if not iso or iso.lower() == parent.lower():
                continue
            for order in self._open_orders_for_sender_batched(iso, product_ids, refresh=refresh, strict=strict):
                order.setdefault("isolated", True)
                order.setdefault("subaccount", iso)
                rows.append(order)
        return rows

    async def get_matches(
        self,
        *,
        product_ids: list[int] | None = None,
        limit: int = 200,
        idx: str | None = None,
        max_time: int | None = None,
    ) -> list[dict]:
        """
        Fetch indexer match/fill events for this subaccount.

        SDK 0.3.3 aliases ``idx`` as ``submission_idx`` on IndexerBaseParams.
        """
        if not self._ensure_sdk_client():
            return []
        from src.nadobro.venue.nado_weights import query_weight
        # _gateway_allowed -> try_acquire can time.sleep() on a starved token
        # bucket. This runs in the coroutine body (not inside _call), so doing it
        # inline would block the event loop. Run it in the SDK pool.
        if not await run_blocking_sdk(
            self._gateway_allowed,
            weight=query_weight("matches", {"limit": limit, "subaccounts": [self.subaccount_hex]}),
            url=self._archive_url(),
            user_scoped=False,
        ):
            return []
        try:
            from nado_protocol.indexer_client.types.query import IndexerMatchesParams

            def _call():
                params = IndexerMatchesParams(
                    subaccounts=[self.subaccount_hex],
                    product_ids=product_ids,
                    isolated=None,
                    idx=int(idx) if idx is not None else None,
                    max_time=max_time,
                    limit=int(limit),
                )
                return self.client.context.indexer_client.get_matches(params)

            data = await run_blocking_sdk(_call)
            rows = getattr(data, "matches", None)
            if rows is None and isinstance(data, dict):
                rows = data.get("matches")
            return self._to_plain(rows or [])
        except Exception as e:
            logger.error("SDK get_matches failed: %s", _format_sdk_error(e))
            return []

    async def get_interest_and_funding_payments(
        self,
        *,
        product_ids: list[int] | None = None,
        limit: int = 200,
        idx: str | None = None,
    ) -> list[dict]:
        """
        Fetch user-scoped indexer interest and funding payments.

        Nado SDK 0.3.3 names the cursor ``max_idx`` on this endpoint.
        """
        if not self._ensure_sdk_client():
            return []
        # Run the (potentially sleeping) token-bucket gate in the SDK pool.
        if not await run_blocking_sdk(
            self._gateway_allowed, weight=5, url=self._archive_url(), user_scoped=False
        ):
            return []  # interest & funding payments: IP weight 5
        try:
            from nado_protocol.indexer_client.types.query import IndexerInterestAndFundingParams

            if product_ids is None:
                product_ids = [
                    int(pid)
                    for name in get_perp_products(network=self.network, client=self)
                    if (pid := get_product_id(name, network=self.network, client=self)) is not None
                ]
            def _call():
                params = IndexerInterestAndFundingParams(
                    subaccount=self.subaccount_hex,
                    product_ids=list(product_ids or []),
                    max_idx=idx,
                    limit=int(limit),
                )
                return self.client.context.indexer_client.get_interest_and_funding_payments(params)

            data = await run_blocking_sdk(_call)
            plain = self._to_plain(data)
            if isinstance(plain, dict):
                payments = []
                for key in ("funding_payments", "interest_payments"):
                    for row in plain.get(key) or []:
                        item = dict(row)
                        item.setdefault("type", "funding" if key == "funding_payments" else "interest")
                        payments.append(item)
                return payments
            return []
        except Exception as e:
            logger.error("SDK get_interest_and_funding_payments failed: %s", _format_sdk_error(e))
            return []

    async def calculate_account_summary(self, *, ts: int | None = None) -> dict:
        """
        Fetch a MarginManager-backed account summary for the active subaccount.

        In SDK 0.3.3 MarginManager.calculate_account_summary takes no args; the
        subaccount and optional snapshot timestamp are supplied through from_client.
        """
        if not self._ensure_sdk_client():
            raise RuntimeError("SDK client unavailable for account summary")
        try:
            from nado_protocol.utils.margin_manager import MarginManager

            def _call():
                manager = MarginManager.from_client(
                    self.client,
                    subaccount=self.subaccount_hex,
                    include_indexer_events=True,
                    snapshot_timestamp=ts,
                )
                return manager.calculate_account_summary()

            return self._to_plain(await run_blocking_sdk(_call)) or {}
        except Exception as e:
            logger.error("SDK calculate_account_summary failed: %s", _format_sdk_error(e))
            raise RuntimeError(f"SDK calculate_account_summary failed: {_format_sdk_error(e)}") from e

    async def cancel_orders(
        self, *, product_id: int, digests: list[str], _retry_count: int = 0
    ) -> dict:
        """
        Cancel multiple plain engine orders for one product.

        CancelOrdersParams accepts hex digest strings and converts them to bytes32.
        """
        clean_digests = [str(d).strip() for d in (digests or []) if str(d).strip()]
        if not clean_digests:
            return {"success": True, "cancelled": 0, "digests": []}
        if not self._ensure_sdk_client():
            return {"success": False, "error": "Client not initialized", "digests": clean_digests}
        # Cancel with digests = wallet weight equal to the number of digests.
        if not self._gateway_allowed(
            weight=len(clean_digests), kind="execute", wallet=self.subaccount_hex, user_scoped=False
        ):
            return {"success": False, "error": "Rate limited — please retry in a moment.", "rate_limited": True, "digests": clean_digests}
        try:
            from nado_protocol.engine_client.types.execute import CancelOrdersParams

            cancel_params = CancelOrdersParams(
                sender=self.subaccount_hex,
                productIds=[int(product_id)],
                digests=clean_digests,
            )
            response = await run_blocking_exec(
                self._dispatch_execute, cancel_params, "cancel_orders"
            )
            # BUG-CANCEL-1: detect a transient ip_query_only downgrade in the
            # response and retry once (verified signer), instead of falsely
            # reporting a successful batch cancel and leaving orders live.
            if self._result_is_ip_query_only(response) and _retry_count < 1:
                try:
                    check = await run_blocking_exec(self.verify_linked_signer, self.address)
                except Exception as ve:  # pragma: no cover
                    logger.warning("verify_linked_signer during cancel_orders retry check failed: %s", ve)
                    check = {"verified": False}
                if check.get("verified"):
                    backoff = 1.5 + random.uniform(0.0, 0.75)
                    logger.warning(
                        "cancel_orders ip_query_only with verified signer; "
                        "retrying once after %.2fs (product_id=%s, n=%d)",
                        backoff, product_id, len(clean_digests),
                    )
                    await asyncio.sleep(backoff)
                    return await self.cancel_orders(
                        product_id=product_id, digests=clean_digests,
                        _retry_count=_retry_count + 1,
                    )
            if self._result_is_ip_query_only(response):
                logger.warning(
                    "cancel_orders rejected ip_query_only product_id=%s n=%d host=%s raw=%s",
                    product_id, len(clean_digests), self._rest_url(), _mask_payload(response),
                )
                return {
                    "success": False,
                    "error": self._friendly_error(str(response)),
                    "digests": clean_digests,
                    "response": self._to_plain(response),
                }
            return {
                "success": True,
                "cancelled": len(clean_digests),
                "digests": clean_digests,
                "response": self._to_plain(response),
            }
        except Exception as e:
            logger.error("cancel_orders failed: %s", e)
            return {"success": False, "error": str(e), "digests": clean_digests}

    async def get_trigger_orders(
        self,
        *,
        product_ids: list[int] | None = None,
        limit: int = 100,
        digests: list[str] | None = None,
        strict: bool = False,
    ) -> list[dict]:
        """List trigger / TP / SL / TWAP orders from the trigger service."""
        if not self._ensure_sdk_client():
            if strict:
                raise RuntimeError("SDK client unavailable for trigger-order sync")
            return []
        trigger_client = getattr(getattr(self.client, "context", None), "trigger_client", None)
        if not trigger_client:
            if strict:
                raise RuntimeError("Trigger client unavailable for trigger-order sync")
            return []
        # Run the (potentially sleeping) token-bucket gate in the SDK pool.
        if not await run_blocking_sdk(self._gateway_allowed, weight=5, user_scoped=False):
            return []  # trigger-service list query; conservative weight
        try:
            from nado_protocol.trigger_client.types.query import (
                ListTriggerOrdersParams,
                ListTriggerOrdersTx,
                TriggerOrderStatusType,
            )

            params = ListTriggerOrdersParams(
                tx=ListTriggerOrdersTx(sender=self.subaccount_hex, recvTime=_recv_time_ms()),
                product_ids=product_ids,
                status_types=[
                    TriggerOrderStatusType.WAITING_PRICE,
                    TriggerOrderStatusType.WAITING_DEPENDENCY,
                    TriggerOrderStatusType.TWAP_EXECUTING,
                ],
                digests=digests,
                limit=int(limit),
            )
            response = await run_blocking_sdk(trigger_client.list_trigger_orders, params)
            data = getattr(response, "data", None)
            rows = getattr(data, "orders", None)
            if rows is None and isinstance(data, dict):
                rows = data.get("orders")
            return self._to_plain(rows or [])
        except Exception as e:
            logger.error("get_trigger_orders failed: %s", _format_sdk_error(e))
            if strict:
                raise RuntimeError(f"get_trigger_orders failed: {_format_sdk_error(e)}") from e
            return []

    async def cancel_trigger_orders(self, *, product_id: int, digests: list[str]) -> dict:
        """Cancel trigger / TP / SL / TWAP orders for one product."""
        clean_digests = [str(d).strip() for d in (digests or []) if str(d).strip()]
        if not clean_digests:
            return {"success": True, "cancelled": 0, "digests": []}
        if not self._ensure_sdk_client():
            return {"success": False, "error": "Client not initialized", "digests": clean_digests}
        trigger_client = getattr(getattr(self.client, "context", None), "trigger_client", None)
        if not trigger_client:
            return {"success": False, "error": "Trigger client not initialized", "digests": clean_digests}
        if not self._gateway_allowed(
            weight=len(clean_digests), kind="execute", wallet=self.subaccount_hex, user_scoped=False
        ):
            return {"success": False, "error": "Rate limited — please retry in a moment.", "rate_limited": True, "digests": clean_digests}
        try:
            from nado_protocol.trigger_client.types.execute import CancelTriggerOrdersParams

            params = CancelTriggerOrdersParams(
                sender=self.subaccount_hex,
                productIds=[int(product_id)],
                digests=clean_digests,
            )
            response = await run_blocking_exec(trigger_client.cancel_trigger_orders, params)
            return {
                "success": True,
                "cancelled": len(clean_digests),
                "digests": clean_digests,
                "response": self._to_plain(response),
            }
        except Exception as e:
            logger.error("cancel_trigger_orders failed: %s", e)
            return {"success": False, "error": str(e), "digests": clean_digests}

    @staticmethod
    def _from_x18_dynamic(value) -> float:
        if value is None:
            return 0.0
        try:
            if isinstance(value, str):
                raw = value.strip()
                if not raw:
                    return 0.0
                fv = float(raw)
            else:
                fv = float(value)
            # Nado payloads can mix x18 integers and plain floats.
            # Treat very large magnitudes as x18; otherwise keep human units.
            if abs(fv) >= 1e9:
                return fv / 1e18
            return fv
        except (ValueError, TypeError) as e:
            logging.getLogger(__name__).warning("_from_x18_dynamic: invalid value %r: %s", value, e)
            return 0.0

    @staticmethod
    def _normalize_side(raw_side=None, raw_is_long=None, raw_direction=None, signed_amount: float = 0.0) -> str:
        if raw_is_long is not None:
            return "LONG" if bool(raw_is_long) else "SHORT"

        def _from_text(value):
            if value is None:
                return None
            text = str(value).strip().upper()
            if not text:
                return None
            if text in ("LONG", "BUY", "BID", "BULL", "L"):
                return "LONG"
            if text in ("SHORT", "SELL", "ASK", "BEAR", "S"):
                return "SHORT"
            return None

        side = _from_text(raw_side)
        if side:
            return side
        side = _from_text(raw_direction)
        if side:
            return side
        return "LONG" if float(signed_amount or 0.0) >= 0 else "SHORT"

    @staticmethod
    def _nested_position_container(obj):
        if obj is None:
            return None
        for key in ("subaccount_info", "subaccountInfo", "account", "result", "data"):
            if isinstance(obj, dict):
                nested = obj.get(key)
            else:
                nested = getattr(obj, key, None)
            if nested:
                return nested
        return None

    def _extract_unrealized_pnl_sdk(self, p, balance_obj=None) -> float | None:
        """Best-effort uPnL from SDK objects (matches Nado when the API exposes it)."""
        for obj in (p, balance_obj):
            if obj is None:
                continue
            for attr in (
                "unrealized_pnl",
                "unrealizedPnl",
                "unsettled_pnl",
                "unsettledPnl",
                "u_pnl",
                "perp_pnl",
                "perpPnl",
            ):
                raw = getattr(obj, attr, None)
                if raw is not None:
                    try:
                        return float(self._from_x18_dynamic(raw))
                    except Exception:  # policy: degrade-ok(alias probe; tries next field)
                        continue
        return None

    def _extract_unrealized_pnl_rest(self, p: dict, balance_dict: dict | None) -> float | None:
        """Best-effort uPnL from REST dicts (matches Nado when the API exposes it)."""
        for obj in (p, balance_dict or {}):
            if not isinstance(obj, dict):
                continue
            for key in (
                "unrealized_pnl",
                "unrealizedPnl",
                "unsettled_pnl",
                "unsettledPnl",
                "u_pnl",
                "perp_pnl",
                "perpPnl",
                "unrealized_pnl_x18",
                "unrealizedPnlX18",
            ):
                if obj.get(key) is not None:
                    try:
                        return float(self._from_x18_dynamic(obj[key]))
                    except Exception:  # policy: degrade-ok(alias probe; tries next field)
                        continue
        return None

    def _extract_liquidation_price_sdk(self, p, balance_obj) -> float | None:
        """Best-effort liquidation / est. liq price from SDK position objects."""
        for obj in (p, balance_obj):
            if obj is None:
                continue
            for attr in (
                "liquidation_price_x18",
                "liquidationPriceX18",
                "liquidation_price",
                "liquidationPrice",
                "est_liquidation_price_x18",
                "estLiquidationPriceX18",
                "est_liquidation_price",
                "estLiquidationPrice",
                "liq_price_x18",
                "liqPriceX18",
                "liq_price",
                "liqPrice",
            ):
                raw = getattr(obj, attr, None)
                if raw is not None:
                    try:
                        v = float(self._from_x18_dynamic(raw))
                        if v > 0:
                            return v
                    except Exception:  # policy: degrade-ok(alias probe; tries next field)
                        continue
        return None

    def _extract_liquidation_price_rest(self, p: dict, balance_dict: dict | None) -> float | None:
        for obj in (p, balance_dict or {}):
            if not isinstance(obj, dict):
                continue
            for key in (
                "liquidation_price_x18",
                "liquidationPriceX18",
                "liquidation_price",
                "liquidationPrice",
                "est_liquidation_price_x18",
                "estLiquidationPriceX18",
                "est_liquidation_price",
                "estLiquidationPrice",
                "liq_price_x18",
                "liqPriceX18",
                "liq_price",
                "liqPrice",
            ):
                if obj.get(key) is not None:
                    try:
                        v = float(self._from_x18_dynamic(obj[key]))
                        if v > 0:
                            return v
                    except Exception:  # policy: degrade-ok(alias probe; tries next field)
                        continue
        return None

    def _extract_positions_from_sdk_info(self, info) -> list:
        positions = []
        if not info:
            return positions
        candidate_lists = []
        for attr in (
            "perp_positions",
            "positions",
            "perp_balances",
            "perpPositions",
            "perpBalances",
            "balances",
            "margin_balances",
            "marginBalances",
            "cross_positions",
            "crossPositions",
        ):
            val = getattr(info, attr, None)
            if val:
                candidate_lists.append(val)
        if not candidate_lists:
            nested = self._nested_position_container(info)
            if nested is not None and nested is not info:
                return self._extract_positions_from_sdk_info(nested)
            return positions

        for plist in candidate_lists:
            for p in plist:
                raw_pid = (
                    getattr(p, "product_id", None)
                    or getattr(p, "productId", None)
                    or getattr(p, "pid", None)
                )
                if raw_pid is None:
                    continue
                try:
                    product_id = int(int(float(str(raw_pid).strip())))
                except Exception as e:
                    logger.debug("position row pid unparsable (%r) — position hidden from this read: %s", raw_pid, e)
                    continue
                balance_obj = getattr(p, "balance", None)
                amount_raw = None
                v_quote_raw = None
                if balance_obj is not None:
                    amount_raw = (
                        getattr(balance_obj, "amount", None)
                        or getattr(balance_obj, "amount_x18", None)
                        or getattr(balance_obj, "amountX18", None)
                        or getattr(balance_obj, "balance_amount", None)
                        or getattr(balance_obj, "balanceAmount", None)
                        or getattr(balance_obj, "balance_amount_x18", None)
                        or getattr(balance_obj, "balanceAmountX18", None)
                        or getattr(balance_obj, "size", None)
                        or getattr(balance_obj, "size_x18", None)
                        or getattr(balance_obj, "sizeX18", None)
                    )
                    v_quote_raw = (
                        getattr(balance_obj, "v_quote_balance", None)
                        or getattr(balance_obj, "vQuoteBalance", None)
                        or getattr(balance_obj, "v_quote_balance_x18", None)
                        or getattr(balance_obj, "vQuoteBalanceX18", None)
                    )
                if amount_raw is None:
                    amount_raw = (
                        getattr(p, "amount", None)
                        or getattr(p, "amount_x18", None)
                        or getattr(p, "amountX18", None)
                        or getattr(p, "balance_amount", None)
                        or getattr(p, "balanceAmount", None)
                        or getattr(p, "balance_amount_x18", None)
                        or getattr(p, "balanceAmountX18", None)
                        or getattr(p, "size", None)
                        or getattr(p, "size_x18", None)
                        or getattr(p, "sizeX18", None)
                        or getattr(p, "base_amount", None)
                        or getattr(p, "base_amount_x18", None)
                        or getattr(p, "baseAmount", None)
                        or getattr(p, "baseAmountX18", None)
                    )
                amount = self._from_x18_dynamic(amount_raw)
                if abs(amount) <= 0:
                    continue

                price_raw = (
                    getattr(p, "entry_price_x18", None)
                    or getattr(p, "entryPriceX18", None)
                    or getattr(p, "avg_entry_price_x18", None)
                    or getattr(p, "avgEntryPriceX18", None)
                    or getattr(p, "price_x18", None)
                    or getattr(p, "priceX18", None)
                    or getattr(p, "entry_price", None)
                    or getattr(p, "entryPrice", None)
                    or 0
                )
                price = self._from_x18_dynamic(price_raw)
                v_quote_val = self._from_x18_dynamic(v_quote_raw) if v_quote_raw is not None else None
                if (not price or price <= 0) and v_quote_val is not None:
                    if abs(amount) > 0:
                        price = abs(v_quote_val / amount)

                side_hint = self._normalize_side(
                    raw_side=getattr(p, "side", None) or (getattr(balance_obj, "side", None) if balance_obj is not None else None),
                    raw_is_long=getattr(p, "is_long", None) if getattr(p, "is_long", None) is not None else getattr(p, "isLong", None),
                    raw_direction=getattr(p, "direction", None),
                    signed_amount=amount,
                )
                abs_amount = abs(float(amount))
                signed_amount = abs_amount if side_hint == "LONG" else -abs_amount
                if amount != 0 and ((amount > 0 and side_hint == "SHORT") or (amount < 0 and side_hint == "LONG")):
                    logger.info(
                        "Position side override for product %s: amount_sign=%s side_hint=%s",
                        product_id,
                        "LONG" if amount > 0 else "SHORT",
                        side_hint,
                    )

                pos = {
                    "product_id": int(product_id),
                    "product_name": get_product_name(int(product_id), network=self.network, client=self),
                    "amount": abs_amount,
                    "signed_amount": signed_amount,
                    "price": float(price),
                    # Alias: "price" here IS the average entry (sourced from
                    # entry_price_x18/avg_entry_price_x18). Consumers like
                    # copy_service read "entry_price" — publish both keys.
                    "entry_price": float(price),
                    "side": side_hint,
                }
                if v_quote_val is not None:
                    pos["v_quote_balance"] = float(v_quote_val)
                upnl = self._extract_unrealized_pnl_sdk(p, balance_obj)
                if upnl is not None:
                    pos["unrealized_pnl"] = upnl
                liq = self._extract_liquidation_price_sdk(p, balance_obj)
                if liq is not None:
                    pos["liquidation_price"] = liq
                positions.append(pos)
        return positions

    def _extract_positions_from_rest_payload(self, payload: dict) -> list:
        positions = []
        if not payload:
            return positions
        lists = []
        for key in (
            "perp_positions",
            "positions",
            "perp_balances",
            "perpPositions",
            "perpBalances",
            "balances",
            "margin_balances",
            "marginBalances",
            "cross_positions",
            "crossPositions",
        ):
            val = payload.get(key)
            if isinstance(val, list) and val:
                lists.append(val)
        if not lists:
            nested = self._nested_position_container(payload)
            if isinstance(nested, dict) and nested is not payload:
                return self._extract_positions_from_rest_payload(nested)
            return positions

        for plist in lists:
            for p in plist:
                try:
                    raw_pid = p.get("product_id") or p.get("productId") or p.get("pid")
                    if raw_pid is None:
                        continue
                    product_id = int(int(float(str(raw_pid).strip())))
                except Exception as e:
                    logger.debug("REST position row pid unparsable — position hidden from this read: %s", e)
                    continue
                balance_dict = p.get("balance") if isinstance(p.get("balance"), dict) else None
                amount_raw = None
                v_quote_raw = None
                if balance_dict:
                    amount_raw = (
                        balance_dict.get("amount")
                        or balance_dict.get("amount_x18")
                        or balance_dict.get("amountX18")
                        or balance_dict.get("balance_amount")
                        or balance_dict.get("balanceAmount")
                        or balance_dict.get("balance_amount_x18")
                        or balance_dict.get("balanceAmountX18")
                        or balance_dict.get("size")
                        or balance_dict.get("size_x18")
                        or balance_dict.get("sizeX18")
                    )
                    v_quote_raw = (
                        balance_dict.get("v_quote_balance")
                        or balance_dict.get("vQuoteBalance")
                        or balance_dict.get("v_quote_balance_x18")
                        or balance_dict.get("vQuoteBalanceX18")
                    )
                if amount_raw is None:
                    amount_raw = (
                        p.get("amount")
                        or p.get("amount_x18")
                        or p.get("amountX18")
                        or p.get("balance_amount")
                        or p.get("balanceAmount")
                        or p.get("balance_amount_x18")
                        or p.get("balanceAmountX18")
                        or p.get("size")
                        or p.get("size_x18")
                        or p.get("sizeX18")
                        or p.get("base_amount")
                        or p.get("base_amount_x18")
                        or p.get("baseAmount")
                        or p.get("baseAmountX18")
                        or 0
                    )
                amount = self._from_x18_dynamic(amount_raw)
                if abs(amount) <= 0:
                    continue
                price_raw = (
                    p.get("entry_price_x18")
                    or p.get("entryPriceX18")
                    or p.get("avg_entry_price_x18")
                    or p.get("avgEntryPriceX18")
                    or p.get("price_x18")
                    or p.get("priceX18")
                    or p.get("entry_price")
                    or p.get("entryPrice")
                    or 0
                )
                price = self._from_x18_dynamic(price_raw)
                v_quote_val = self._from_x18_dynamic(v_quote_raw) if v_quote_raw is not None else None
                if (not price or price <= 0) and v_quote_val is not None:
                    if abs(amount) > 0:
                        price = abs(v_quote_val / amount)

                side_hint = self._normalize_side(
                    raw_side=p.get("side") or (balance_dict.get("side") if balance_dict else None),
                    raw_is_long=p.get("is_long") if p.get("is_long") is not None else p.get("isLong"),
                    raw_direction=p.get("direction"),
                    signed_amount=amount,
                )
                abs_amount = abs(float(amount))
                signed_amount = abs_amount if side_hint == "LONG" else -abs_amount
                if amount != 0 and ((amount > 0 and side_hint == "SHORT") or (amount < 0 and side_hint == "LONG")):
                    logger.info(
                        "REST position side override for product %s: amount_sign=%s side_hint=%s",
                        product_id,
                        "LONG" if amount > 0 else "SHORT",
                        side_hint,
                    )
                pos = {
                    "product_id": product_id,
                    "product_name": get_product_name(product_id, network=self.network, client=self),
                    "amount": abs_amount,
                    "signed_amount": signed_amount,
                    "price": float(price),
                    # Alias: "price" here IS the average entry (sourced from
                    # entry_price_x18/avg_entry_price_x18). Consumers like
                    # copy_service read "entry_price" — publish both keys.
                    "entry_price": float(price),
                    "side": side_hint,
                }
                if v_quote_val is not None:
                    pos["v_quote_balance"] = float(v_quote_val)
                upnl = self._extract_unrealized_pnl_rest(p, balance_dict)
                if upnl is not None:
                    pos["unrealized_pnl"] = upnl
                liq = self._extract_liquidation_price_rest(p, balance_dict)
                if liq is not None:
                    pos["liquidation_price"] = liq
                positions.append(pos)
        return positions

    def _positions_for_subaccount_hex(self, subaccount_hex: str, *, allow_empty_cache_fallback: bool) -> list:
        """
        Perp positions for a single subaccount (default or isolated margin child).
        """
        subaccount_hex = (subaccount_hex or "").strip()

        subaccount_info_succeeded = False
        if self._initialized and self.client:
            try:
                info = self.client.context.engine_client.get_subaccount_info(subaccount_hex)
                subaccount_info_succeeded = True
                sdk_positions = self._extract_positions_from_sdk_info(info)
                if sdk_positions:
                    return sdk_positions
                # Drop to REST silently — this fires every cycle for users on
                # isolated-only products (no positions in the parent
                # subaccount) and was flooding the logs.
                logger.debug(
                    "SDK subaccount_info: no positions in parent subaccount; trying REST (subaccount=%s)",
                    subaccount_hex[:22],
                )
            except Exception as e:
                logger.warning("SDK positions for subaccount failed: %s", e)

        try:
            data = self._query_rest("subaccount_info", {"subaccount": subaccount_hex}) or {}
            if data.get("status") == "success":
                subaccount_info_succeeded = True
                payload = data.get("data", {}) or {}
                if isinstance(payload, dict):
                    for nested_key in ("subaccount_info", "subaccountInfo", "account", "result"):
                        nested = payload.get(nested_key)
                        if isinstance(nested, dict) and nested:
                            payload = nested
                            break
                rest_positions = self._extract_positions_from_rest_payload(payload)
                if rest_positions:
                    return rest_positions
        except Exception as e:
            logger.warning("REST positions for subaccount failed: %s", e)

        if subaccount_info_succeeded:
            # Expected for users with no positions in this subaccount. Demoted
            # from INFO so the per-cycle DGRID/strategy logs are readable.
            logger.debug(
                "Subaccount info empty (no positions for %s)",
                subaccount_hex[:22],
            )

        if not allow_empty_cache_fallback:
            return []

        fallback_key = (self.network, str(subaccount_hex))
        cached = _positions_fallback_cache.get(fallback_key)
        if cached and (time.time() - float(cached.get("ts", 0))) < _POSITIONS_FALLBACK_TTL:
            return list(cached.get("data") or [])

        positions = []
        _positions_fallback_cache[fallback_key] = {"data": positions, "ts": time.time()}
        return positions

    def _isolated_subaccount_hexes(self) -> list[str]:
        try:
            from src.nadobro.venue.nado_archive import (
                isolated_subaccount_from_row,
                query_isolated_subaccounts_for_parent,
            )

            rows = query_isolated_subaccounts_for_parent(self.network, self.subaccount_hex or "") or []
        except Exception as e:
            logger.warning("isolated subaccount discovery failed: %s", e)
            return []

        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            iso = isolated_subaccount_from_row(row, self.subaccount_hex or "")
            if not iso or iso in seen:
                continue
            seen.add(iso)
            out.append(iso)
        return out

    def get_all_positions(self) -> list:
        # Default (cross / main) subaccount.
        main = self._positions_for_subaccount_hex(
            self.subaccount_hex or "",
            allow_empty_cache_fallback=True,
        )
        merged = list(main)

        # Isolated margin: each market uses a dedicated child subaccount; balances do not
        # appear on the parent subaccount_info.perp_balances list.
        try:
            for iso in self._isolated_subaccount_hexes():
                iso_positions = self._positions_for_subaccount_hex(iso, allow_empty_cache_fallback=False)
                for p in iso_positions:
                    p["subaccount"] = iso
                merged.extend(iso_positions)
        except Exception as e:
            logger.warning("get_all_positions isolated merge failed: %s", e)

        return merged

    def sign_stream_authentication(
        self, *, expiration_ms: Optional[int] = None, sender: Optional[str] = None,
        auth_id: int = 0,
    ) -> dict:
        """Build a signed ``authenticate`` message for the subscriptions (streams) websocket.

        The subscriptions gateway (``/v1/subscribe``) requires a one-time
        EIP-712 ``StreamAuthentication`` before subscribing to the
        authenticated streams (``order_update`` / ``fill``). The signed struct
        is ``StreamAuthentication{ sender: bytes32, expiration: uint64 }`` over
        the standard Nado domain, with the *endpoint* contract as the verifying
        contract (NOT the per-product order book contract — that one is only
        for ``place_order``). Authentication is per-connection and, once set,
        applies to the whole wallet for the life of the socket.

        Returns the exact JSON dict to send over the websocket:
        ``{"method":"authenticate","id":<n>,"tx":{"sender":<hex>,"expiration":"<ms>"},"signature":<0x..>}``.
        Raises ``RuntimeError`` if the client has no signer (read-only client).
        """
        if not self._initialized or not self.client:
            raise RuntimeError("Nado client not initialized; cannot sign stream auth")
        from nado_protocol.contracts.types import NadoTxType
        from nado_protocol.contracts.eip712.sign import (
            build_eip712_typed_data,
            sign_eip712_typed_data,
        )

        engine_client = self.client.context.engine_client
        signer = engine_client.linked_signer or engine_client.signer
        if signer is None:
            raise RuntimeError("Nado client has no signer; cannot authenticate stream")

        sender_hex = (sender or self.subaccount_hex or "").strip()
        if not sender_hex:
            raise RuntimeError("Missing subaccount for stream authentication")
        # Default: 24h out, in milliseconds (docs: expiration is ms since epoch).
        if expiration_ms is None:
            expiration_ms = int(time.time() * 1000) + 24 * 60 * 60 * 1000

        clean = sender_hex[2:] if sender_hex.startswith("0x") else sender_hex
        msg = {"sender": bytes.fromhex(clean), "expiration": int(expiration_ms)}
        typed = build_eip712_typed_data(
            NadoTxType.AUTHENTICATE_STREAM,
            msg,
            engine_client.endpoint_addr,
            int(engine_client.chain_id),
        )
        sig = sign_eip712_typed_data(typed, signer)
        sig = sig if str(sig).startswith("0x") else "0x" + str(sig)
        return {
            "method": "authenticate",
            "id": int(auth_id),
            "tx": {
                "sender": sender_hex if sender_hex.startswith("0x") else "0x" + sender_hex,
                "expiration": str(int(expiration_ms)),
            },
            "signature": sig,
        }

    def verify_linked_signer(self, expected_signer_address: str = None, *, use_cache: bool = True) -> dict:
        expected = (expected_signer_address or self.address or "").lower()
        cache_key = (self.network, str(self.subaccount_hex or ""), expected)
        if use_cache and _LINKED_SIGNER_CACHE_TTL_SECONDS > 0:
            cached = _linked_signer_cache.get(cache_key)
            if cached and (time.time() - float(cached.get("ts", 0))) < _LINKED_SIGNER_CACHE_TTL_SECONDS:
                # Only serve verified results from cache. Failures bypass so we
                # immediately re-check if the user re-links / fixes the wallet.
                payload = cached.get("payload") or {}
                if payload.get("verified") and not payload.get("error"):
                    return dict(payload)

        result = {
            "verified": False,
            "current_signer": None,
            "expected_signer": expected,
            "error": None,
        }

        if self._initialized and self.client:
            try:
                ls_data = self.client.context.engine_client.get_linked_signer(self.subaccount_hex)
                current = getattr(ls_data, "signer", None) or getattr(ls_data, "linked_signer", None)
                if current and hasattr(current, "address"):
                    current = current.address
                current = self._normalize_signer_address(current)
                result["current_signer"] = current
                if current and expected:
                    result["verified"] = current.lower() == expected[:42].lower()
                logger.info("Linked signer check via SDK: current=%s expected=%s verified=%s",
                            current, expected, result["verified"])
                if result["verified"]:
                    _linked_signer_cache[cache_key] = {"ts": time.time(), "payload": dict(result)}
                return result
            except Exception as e:
                logger.warning("SDK get_linked_signer failed: %s", e)

        try:
            data = self._query_rest("linked_signer", {"subaccount": self.subaccount_hex}) or {}
            if data.get("status") == "success":
                signer_raw = data.get("data", {})
                current = None
                if isinstance(signer_raw, dict):
                    current = signer_raw.get("linked_signer") or signer_raw.get("signer")
                elif isinstance(signer_raw, str):
                    current = signer_raw
                current = self._normalize_signer_address(current)
                result["current_signer"] = current
                if current and expected:
                    result["verified"] = current.lower() == expected[:42].lower()
                logger.info("Linked signer check via REST: current=%s expected=%s verified=%s",
                            current, expected, result["verified"])
            else:
                result["error"] = data.get("error", "Unknown error from exchange")
                logger.warning("REST linked_signer query returned non-success: %s", data)
        except Exception as e:
            result["error"] = str(e)
            logger.warning("REST get_linked_signer failed: %s", e)

        if result["verified"] and not result["error"]:
            _linked_signer_cache[cache_key] = {"ts": time.time(), "payload": dict(result)}
        return result

    @staticmethod
    def _normalize_signer_address(raw) -> Optional[str]:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            hex_str = raw.hex()
            if len(hex_str) >= 40:
                addr_hex = hex_str[-40:]
            else:
                return None
            addr = "0x" + addr_hex
        elif isinstance(raw, str):
            addr = raw.strip().lower()
        else:
            return None
        if not addr.startswith("0x"):
            addr = "0x" + addr
        if len(addr) > 42:
            addr = "0x" + addr[-40:]
        if len(addr) != 42:
            return None
        if addr == "0x" + "0" * 40:
            return None
        return addr.lower()

    def _friendly_error(self, error_str: str) -> str:
        err_lower = error_str.lower()
        compact = err_lower.replace("_", "").replace("-", "")
        if "ipqueryonly" in compact:
            # Branch the user-facing message on the *actual* signer state so we
            # don't tell users to re-link a key that's already correctly linked.
            # Previous copy led with "your 1CT signer key is not linked" even
            # when verify_linked_signer returned verified=True — confusing in
            # the transient IP-throttle / Cloudflare-WAF case which the user
            # has no way to fix from the Nado UI. (Audit 2026-05-27.)
            check_status: Optional[str] = None
            check_current: Optional[str] = None
            check_error: Optional[str] = None
            signer_addr = ""
            try:
                signer_addr = self.address or ""
                if signer_addr and self.subaccount_hex:
                    check = self.verify_linked_signer(signer_addr)
                    check_error = check.get("error")
                    check_current = check.get("current_signer")
                    if check_error:
                        check_status = "error"
                    elif not check_current:
                        check_status = "missing"
                    elif check.get("verified"):
                        check_status = "verified"
                    else:
                        check_status = "mismatch"
            except Exception as de:
                logger.warning("Linked signer diagnostic failed: %s", de)
                check_status = "error"
                check_error = str(de)

            if check_status == "verified":
                # Most common cause now we run on a stable allowlisted IP:
                # transient per-IP throttle from Nado's gateway / WAF when
                # query weight spikes. Don't blame the user.
                return (
                    "⚠️ The exchange temporarily restricted this request "
                    "(ip_query_only).\n\n"
                    "Your 1CT signer IS linked correctly — this is an "
                    "exchange-side IP throttle, not a setup problem. The bot "
                    "will retry automatically; if it persists, wait a few "
                    "seconds and try again."
                )
            if check_status == "mismatch":
                short_current = (check_current or "")[:10]
                short_signer = signer_addr[:10] if signer_addr else "<unavailable>"
                return (
                    "⚠️ Signer mismatch — the exchange has a different 1CT key "
                    "linked.\n\n"
                    f"• Exchange has: {short_current}... linked\n"
                    f"• Bot's signer: {short_signer}...\n\n"
                    "Fix: Nado web app → Settings → 1-Click Trading → disable → "
                    "Advanced 1CT → paste the bot's key → enable and save."
                )
            if check_status == "missing":
                return (
                    "⚠️ Your 1CT signer key is not linked on Nado.\n\n"
                    "Fix: Nado web app → Settings → 1-Click Trading → paste "
                    "your 1CT private key → enable the toggle → save.\n"
                    f"Bot's 1CT signer address: {signer_addr or '<unavailable>'}"
                )
            # check_status is "error" (or None): we couldn't verify because the
            # query itself was rate-limited or Cloudflare-challenged. Fall back
            # to the historical 3-step list but lead with the most likely
            # transient cause now that allowlisted IPs are the norm.
            suffix = ""
            if check_error:
                suffix = f"\n\n🔍 Could not verify signer status: {check_error}"
            return (
                "⚠️ The exchange restricted this trade (ip_query_only). "
                "Possible causes:\n"
                "1. The bot's server IP is temporarily throttled by the "
                "exchange — wait a moment and retry.\n"
                "2. Your 1CT signer key may not be linked on Nado — Settings → "
                "1-Click Trading → paste your 1CT private key → enable.\n"
                "3. Your subaccount may not be initialized — deposit at least "
                "$5 USDT0."
                + suffix
            )
        compact_json = error_str.replace(" ", "")
        if (
            "account health" in err_lower
            or '"error_code":2006' in compact_json.lower()
            or "'error_code':2006" in compact_json.lower()
        ):
            return (
                "The exchange rejected this order because projected account health would fall "
                "below its required threshold. That depends on isolated margin, leverage, open "
                "positions and fees — not only raw wallet balance. Try reducing order size or "
                "leverage, add collateral on Nado, or close risk elsewhere."
            )
        if "insufficient" in err_lower or "margin" in err_lower:
            return "Insufficient margin. Please deposit more funds."
        if "product" in err_lower and "not found" in err_lower:
            return "This product is not currently available on the exchange."
        if "blocked" in err_lower:
            return "Order was blocked by the exchange. Your wallet may need funds deposited on-chain first."
        if "nonce" in err_lower:
            return "Order timing issue. Please try again."
        if "rate" in err_lower and "limit" in err_lower:
            return "Too many requests. Please wait a moment and try again."
        if "invalid order price" in err_lower and "price_increment_x18" in err_lower:
            return "Order price did not match exchange tick size. Price was auto-adjusted if possible; please retry."
        if "2122" in err_lower or "isolated-only" in err_lower or "marketisolatedonlymode" in compact:
            return (
                "This market is isolated-only. The bot now sends isolated orders, "
                "but this trade may still fail if isolated margin is too low."
            )
        return error_str

    @staticmethod
    def _extract_price_increment_from_error(error_str: str, product_id: int) -> Optional[float]:
        import re
        pattern = rf"price_increment_x18 for product {product_id}:\s*(\d+)"
        m = re.search(pattern, error_str)
        if not m:
            return None
        try:
            return int(m.group(1)) / 1e18
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_size_increment_from_error(error_str: str, product_id: int) -> Optional[float]:
        import re
        patterns = [
            rf"size_increment_x18 for product {product_id}:\s*(\d+)",
            r"size_increment_x18[^0-9]*(\d+)",
            r"size_increment[^0-9]*([0-9]*\.?[0-9]+(?:e-?\d+)?)",
        ]
        for pattern in patterns:
            m = re.search(pattern, error_str, flags=re.IGNORECASE)
            if not m:
                continue
            raw = m.group(1)
            try:
                if "x18" in pattern:
                    return int(raw) / 1e18
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_min_size_x18_from_error(error_str: str, product_id: int) -> Optional[int]:
        import re
        patterns = [
            rf"min_size for product {product_id}:\s*(\d+)",
            r"min_size[^0-9]*(\d+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, error_str, flags=re.IGNORECASE)
            if not m:
                continue
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _is_size_increment_error(error_str: str) -> bool:
        lowered = (error_str or "").lower()
        return "invalid order amount" in lowered and "size_increment" in lowered

    @staticmethod
    def _is_min_notional_error(error_str: str) -> bool:
        lowered = (error_str or "").lower()
        return "invalid order size" in lowered and "min_size" in lowered

    @staticmethod
    def _align_price_to_increment(price: float, increment: float, is_buy: bool, order_type: str) -> float:
        if increment <= 0:
            return price
        d_price = Decimal(str(price))
        d_inc = Decimal(str(increment))
        ratio = d_price / d_inc
        if order_type == "ioc":
            rounding = ROUND_CEILING if is_buy else ROUND_FLOOR
        elif order_type == "post_only":
            # Never round INTO the book: a post-only buy rounds DOWN and a
            # post-only sell rounds UP, so tick alignment can't turn a valid
            # maker price into a crossing one the venue rejects.
            rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
        else:
            rounding = ROUND_HALF_UP
        ticks = ratio.to_integral_value(rounding=rounding)
        aligned = ticks * d_inc
        return float(aligned)

    @staticmethod
    def _align_size_to_increment(size: float, increment: float) -> float:
        if increment <= 0:
            return size
        d_size = Decimal(str(size))
        d_inc = Decimal(str(increment))
        ticks = (d_size / d_inc).to_integral_value(rounding=ROUND_FLOOR)
        aligned = ticks * d_inc
        return float(aligned)

    @staticmethod
    def _align_size_up_to_increment(size: float, increment: float) -> float:
        if increment <= 0:
            return size
        d_size = Decimal(str(size))
        d_inc = Decimal(str(increment))
        ticks = (d_size / d_inc).to_integral_value(rounding=ROUND_CEILING)
        aligned = ticks * d_inc
        return float(aligned)

    @staticmethod
    def _to_x18_int(value: float) -> int:
        return int((Decimal(str(value)) * Decimal("1000000000000000000")).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _to_x6_int(value: float) -> int:
        return int((Decimal(str(value)) * Decimal("1000000")).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _build_order_appendix(
        order_type_int: int,
        isolated: bool = False,
        reduce_only: bool = False,
        margin_x6: int = 0,
        builder_id: int = 0,
        builder_fee_rate: int = 0,
    ) -> int:
        # Bit layout:
        # value[64..127], builder[48..63], builder_fee_rate[38..47], reserved[14..37],
        # trigger[12..13], reduce_only[11], order_type[9..10], isolated[8], version[0..7]
        version = 1
        appendix = int(version & 0xFF)
        if isolated:
            appendix |= (1 << 8)
        appendix |= ((int(order_type_int) & 0x03) << 9)
        if reduce_only:
            appendix |= (1 << 11)
        appendix |= ((int(builder_fee_rate) & 0x3FF) << 38)
        appendix |= ((int(builder_id) & 0xFFFF) << 48)
        if isolated and margin_x6 > 0:
            appendix |= (int(margin_x6) << 64)
        return appendix

    @staticmethod
    def _align_x18_to_increment(value_x18: int, increment_x18: int) -> int:
        if increment_x18 <= 0:
            return value_x18
        sign = -1 if value_x18 < 0 else 1
        abs_v = abs(int(value_x18))
        aligned = (abs_v // int(increment_x18)) * int(increment_x18)
        return sign * aligned

    @staticmethod
    def _align_x18_up_to_increment(value_x18: int, increment_x18: int) -> int:
        if increment_x18 <= 0:
            return value_x18
        sign = -1 if value_x18 < 0 else 1
        abs_v = abs(int(value_x18))
        inc = int(increment_x18)
        aligned = ((abs_v + inc - 1) // inc) * inc
        return sign * aligned

    def _fallback_size_candidates(self, size: float) -> list[float]:
        # Conservative ladder from fine to coarse increments.
        # Includes 1/2/5 * 10^n steps to match common exchange lot sizes like 5e-5.
        increments = []
        for exp in range(-8, 1):
            base = 10 ** exp
            increments.extend([1 * base, 2 * base, 5 * base])
        candidates = []
        for inc in increments:
            aligned = self._align_size_to_increment(size, inc)
            if aligned > 0 and aligned not in candidates:
                candidates.append(aligned)
        return candidates

    def _store_increment(self, pid: int, size_x18, price_x18, min_x18) -> dict:
        """Populate the in-process increment caches for one product and return
        a JSON-serializable blob entry for the shared Redis cache."""
        entry: dict = {}
        if size_x18 is not None:
            try:
                si = int(size_x18)
                _size_increment_x18_cache[(self.network, pid)] = si
                _size_increment_cache[(self.network, pid)] = si / 1e18
                entry["size_x18"] = si
            except (TypeError, ValueError):
                pass
        if price_x18 is not None:
            try:
                pi = int(price_x18)
                _price_increment_x18_cache[(self.network, pid)] = pi
                _price_increment_cache[(self.network, pid)] = pi / 1e18
                entry["price_x18"] = pi
            except (TypeError, ValueError):
                pass
        if min_x18 is not None:
            try:
                mi = int(min_x18)
                _min_size_x18_cache[(self.network, pid)] = mi
                entry["min_x18"] = mi
            except (TypeError, ValueError):
                pass
        return entry

    def get_product_min_notional_usd(self, product_id: int) -> Optional[float]:
        """Minimum order notional (USD) for a product — spot OR perp. The venue
        bumps a smaller order up to this floor, so Delta Neutral uses it to
        reject an undersized Size instead of silently growing both legs (the
        "$50 set but $97 executed" surprise). Returns None when unresolved."""
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            return None
        try:
            payload = self._query_rest("all_products") or {}
            data = payload.get("data") or payload
            for key in ("perp_products", "spot_products"):
                for p in data.get(key) or []:
                    if not isinstance(p, dict) or int(p.get("product_id") or -1) != pid:
                        continue
                    book = p.get("book_info") or {}
                    raw = (p.get("min_size_x18") or book.get("min_size_x18")
                           or p.get("min_size") or book.get("min_size"))
                    val = self._x18_to_float(raw)
                    return val if (val and val > 0) else None
        except Exception as e:  # noqa: BLE001
            logger.debug("get_product_min_notional_usd failed pid=%s err=%s", product_id, e)
        return None

    def _warm_product_increment_cache(self, product_id: int) -> None:
        key = (self.network, product_id)
        if key in _size_increment_x18_cache and key in _price_increment_x18_cache:
            return
        # Shared Redis layer: increments are static venue metadata, so hydrate
        # the whole network's map from one Upstash entry before falling back to
        # an all_products REST query (a per-IP gateway hit on every worker boot).
        redis_key = f"nado:increments:{self.network}"
        blob = self._read_shared_cache(redis_key)
        if isinstance(blob, dict) and blob:
            for pid_str, vals in blob.items():
                try:
                    pid = int(pid_str)
                except (TypeError, ValueError):
                    continue
                if isinstance(vals, dict):
                    self._store_increment(
                        pid, vals.get("size_x18"), vals.get("price_x18"), vals.get("min_x18")
                    )
            if key in _size_increment_x18_cache and key in _price_increment_x18_cache:
                return
        try:
            data = self._query_rest("all_products") or {}
            if data.get("status") != "success":
                return
            payload = data.get("data", {}) or {}
            # BOTH markets: spot orders (Volume bot) previously had NO cached
            # increments because only perp_products was parsed — their price/size
            # went to the venue unaligned and every spot order was rejected
            # ("volume bot places no orders"), while perp strategies worked.
            all_rows = list(payload.get("perp_products", []) or []) + list(
                payload.get("spot_products", []) or []
            )
            fresh_blob: dict = {}
            for p in all_rows:
                pid = p.get("product_id")
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    continue
                book_info = p.get("book_info", {}) or {}
                size_inc_x18 = p.get("size_increment_x18") or book_info.get("size_increment")
                price_inc_x18 = p.get("price_increment_x18") or book_info.get("price_increment_x18")
                min_size_x18 = p.get("min_size_x18") or book_info.get("min_size_x18") or p.get("min_size")
                entry = self._store_increment(pid, size_inc_x18, price_inc_x18, min_size_x18)
                if entry:
                    fresh_blob[str(pid)] = entry
            if fresh_blob:
                self._write_shared_cache(redis_key, fresh_blob, _INCREMENTS_CACHE_TTL)
        except Exception as e:
            logger.debug("Could not warm product increment cache for %s: %s", product_id, e)

    def _build_signed_request(self, params, op, *, product_id=None):
        """Run the SDK prepare->sign pipeline WITHOUT sending.

        ``op`` is "place_order" or "cancel_orders". Returns (signed_params,
        body_dict) where body_dict is the exact JSON the gateway accepts on BOTH
        /execute (REST) and /ws/v2. We reuse the SDK's own prepare_execute_params
        / _sign / to_execute_request so the signature, nonce and digest are
        byte-identical to the REST path — no re-derivation.
        """
        from nado_protocol.engine_client.types.execute import to_execute_request
        from nado_protocol.contracts.types import NadoExecuteType

        exec_type = (
            NadoExecuteType.PLACE_ORDER if op == "place_order"
            else NadoExecuteType.CANCEL_ORDERS
        )
        eng = self.client.context.engine_client
        if hasattr(params, "order"):
            params.order = eng.prepare_execute_params(params.order, True)
            if not getattr(params, "signature", None):
                params.signature = eng._sign(
                    exec_type, params.order.dict(), product_id
                )
        else:
            params = eng.prepare_execute_params(params, True)
            if not getattr(params, "signature", None):
                params.signature = eng._sign(exec_type, params.dict())
        body = to_execute_request(params).dict()
        return params, body

    def _dispatch_execute(self, params, op, *, product_id=None):
        """Send a signed execute over /ws/v2 when enabled+healthy, else REST.

        ``op`` is "place_order" or "cancel_orders". Always returns an SDK
        ``ExecuteResponse`` object so every existing caller
        (``hasattr(result, "data")`` etc.) works unchanged: the v2 socket
        returns a raw dict, which we parse back into ``ExecuteResponse``. On ANY
        v2 transport fault we fall back to the SDK REST send using the SAME
        signed params, so a socket hiccup never drops an order.
        """
        from nado_protocol.engine_client.types.execute import ExecuteResponse
        from src.nadobro.venue import nado_ws_actions

        is_place = op == "place_order"
        send_rest = (
            self.client.market.place_order if is_place
            else self.client.market.cancel_orders
        )
        if nado_ws_actions.v2_enabled():
            try:
                params, body = self._build_signed_request(
                    params, op, product_id=product_id
                )
                inner = next(iter(body.values()))
                resp = nado_ws_actions.send_execute_sync(self.network, op, inner)
                if isinstance(resp, dict):
                    return ExecuteResponse.parse_obj(resp)
                return resp
            except Exception as exc:  # noqa: BLE001 - degrade to REST on v2 fault
                logger.warning(
                    "ws v2 send failed (%s); falling back to REST: %s",
                    op, _format_sdk_error(exc),
                )
                return send_rest(params)
        return send_rest(params)

    def place_order(
        self,
        product_id: int,
        size: float,
        price: float,
        order_type: str = "default",
        is_buy: bool = True,
        isolated_only: bool = False,
        isolated_margin: Optional[float] = None,
        reduce_only: bool = False,
        sender: Optional[str] = None,
        client_id: Optional[int] = None,
        never_grow: bool = False,
        _retry_count: int = 0,
    ) -> dict:
        """``never_grow``: this order REDUCES exposure (a close / flatten / sweep),
        so its size must never be increased by a retry.

        SPOT-CLOSE-BUMP (2026-07-28): the min-notional retry below did
        ``retry_size = max(size, target)`` for EVERY order — side-blind,
        reduce_only-blind, balance-blind. A spot close sized to exactly the held
        base (KBTC min notional is $100; a $99 DN/vol leg lands just under it) was
        grown ABOVE the balance, the retry could not fill, and the spot leg was
        left naked. On a perp, growing a close past the position size flips the
        side outright. A close must shrink or fail, never grow.
        Note reduce_only cannot carry this: the engine adapter STRIPS it for spot
        because the venue rejects reduce-only spot orders (error_code 5000)."""
        if not self._initialized or not self.client:
            return {"success": False, "error": "Client not initialized. Please try /start again."}

        # Per-wallet execute budget (600 weight/min). Place w/ spot leverage = 1.
        place_sender = (sender or "").strip() or self.subaccount_hex
        if not self._gateway_allowed(weight=1, kind="execute", wallet=place_sender, user_scoped=False):
            logger.warning("place_order throttled by wallet execute budget sender=%s", _mask_address(place_sender))
            return {"success": False, "error": "Rate limited — please retry in a moment.", "rate_limited": True}

        try:
            sender_hex = (sender or "").strip() or self.subaccount_hex
            try:
                builder_id, builder_fee_rate = get_nado_builder_routing_config(self.network)
            except ValueError as cfg_err:
                logger.error("Builder routing misconfiguration; rejecting order: %s", cfg_err)
                return {"success": False, "error": f"Builder routing misconfigured: {cfg_err}"}

            self._warm_product_increment_cache(product_id)
            size_increment = _size_increment_cache.get((self.network, product_id))
            size_increment_x18 = _size_increment_x18_cache.get((self.network, product_id))
            min_size_x18 = _min_size_x18_cache.get((self.network, product_id))
            if size_increment and size_increment > 0:
                aligned_size = self._align_size_to_increment(size, size_increment)
                if aligned_size <= 0:
                    return {
                        "success": False,
                        "error": (
                            f"Order size {size} is below minimum increment for product {product_id} "
                            f"(size increment {size_increment})."
                        ),
                    }
                size = aligned_size

            price_increment = _price_increment_cache.get((self.network, product_id))
            price_increment_x18 = _price_increment_x18_cache.get((self.network, product_id))
            if price_increment and price_increment > 0:
                price = self._align_price_to_increment(price, price_increment, is_buy, order_type)

            from nado_protocol.engine_client.types.execute import PlaceOrderParams, OrderParams
            from nado_protocol.utils.expiration import get_expiration_timestamp
            from nado_protocol.utils.nonce import gen_order_nonce

            appendix_order_type_int = {
                "default": 0,
                "ioc": 1,
                "fok": 2,
                "post_only": 3,
            }.get(order_type, 0)

            amount = size if is_buy else -size
            expiration_secs = 10 if order_type == "ioc" else _limit_order_expiration_seconds()

            amount_x18 = self._to_x18_int(amount)
            if size_increment_x18 and size_increment_x18 > 0:
                amount_x18 = self._align_x18_to_increment(amount_x18, int(size_increment_x18))
                if amount_x18 == 0:
                    return {
                        "success": False,
                        "error": (
                            f"Order size {size} is below minimum increment for product {product_id} "
                            f"(size increment {size_increment})."
                        ),
                    }

            price_x18 = self._to_x18_int(price)
            if price_increment_x18 and price_increment_x18 > 0:
                price_x18 = self._align_x18_to_increment(price_x18, int(price_increment_x18))

            # Proactively enforce exchange minimum notional before submission.
            # AUDIT 2026-07-29: this block must honour never_grow too. Gating only
            # the POST-rejection retry (below) left THIS one growing a reducing
            # order above the held balance BEFORE the order was ever signed — so
            # the adapter's balance clamp was discarded one call later and the
            # stranded-spot-leg incident was fully reproducible. A close must
            # shrink or fail, never grow, on EVERY path.
            pre_bump_size = float(size)
            size_bumped = False
            # Only a RESTING order is subject to the venue's minimum. place_market_order
            # calls in with order_type="ioc" AND a real (slippage-bounded) price, so
            # gating on never_grow alone refused the marketable exit that the adapter
            # converts to precisely because market orders are exempt — the spot leg
            # could then never be sold and DN parked in CLOSING forever. Audit round 3.
            _marketable = str(order_type or "").lower() in ("ioc", "fok", "market")
            if (never_grow and not _marketable
                    and min_size_x18 and min_size_x18 > 0 and price_x18 > 0
                    and amount_x18 != 0):
                _notional_x36 = abs(int(amount_x18)) * int(price_x18)
                if _notional_x36 < int(min_size_x18) * (10 ** 18):
                    logger.warning(
                        "place_order: reducing order on product_id=%s is below the "
                        "venue min notional and must NOT be grown (size=%s) — "
                        "caller needs a marketable/aggregated exit",
                        product_id, size,
                    )
                    return {"success": False,
                            "error": "Order is below the venue minimum notional and "
                                     "cannot be increased because it reduces exposure.",
                            "min_notional_block": True}
            if (not never_grow) and min_size_x18 and min_size_x18 > 0 and price_x18 > 0 and amount_x18 != 0:
                min_notional_x36 = int(min_size_x18) * (10 ** 18)
                current_notional_x36 = abs(int(amount_x18)) * int(price_x18)
                if current_notional_x36 < min_notional_x36:
                    required_abs_amount_x18 = (min_notional_x36 + int(price_x18) - 1) // int(price_x18)
                    bumped_amount_x18 = required_abs_amount_x18 if amount_x18 > 0 else -required_abs_amount_x18
                    if size_increment_x18 and size_increment_x18 > 0:
                        bumped_amount_x18 = self._align_x18_up_to_increment(
                            bumped_amount_x18,
                            int(size_increment_x18),
                        )
                    if abs(int(bumped_amount_x18)) > abs(int(amount_x18)):
                        logger.info(
                            "Bumping order amount for min notional product_id=%s amount_x18=%s->%s min_size_x18=%s",
                            product_id,
                            amount_x18,
                            bumped_amount_x18,
                            min_size_x18,
                        )
                        amount_x18 = int(bumped_amount_x18)
                        size = abs(float(amount_x18) / 1e18)
                        size_bumped = True

            isolated_margin_x6 = 0
            if isolated_only:
                if isolated_margin is None:
                    isolated_margin = abs(float(size) * float(price))
                elif size_bumped and pre_bump_size > 0:
                    # Caller computed isolated_margin against the pre-bump size.
                    # Scale it up by the same ratio so margin_x6 still covers the
                    # bumped notional — otherwise the venue rejects with
                    # error_code 2006 ("account health below threshold").
                    isolated_margin = float(isolated_margin) * (float(size) / pre_bump_size)
                isolated_margin_x6 = max(0, self._to_x6_int(float(isolated_margin)))

            # Unique-ID tagging (WS v2 / MM correlation): when a ``client_id`` is
            # supplied, embed its low 20 bits in the order nonce — the docs are
            # explicit that ``client_id`` is NOT part of the order digest, so the
            # authoritative way to distinguish otherwise-identical orders (same
            # grid level placed repeatedly) is the last 20 bits of the nonce.
            # We ALSO pass it as ``PlaceOrderParams.id`` so it echoes back in the
            # ``order_update`` / ``fill`` subscription events for fast lookup.
            #   docs: .../api/gateway/executes/place-order (client id)
            tag: Optional[int] = None
            if client_id is not None:
                tag = int(client_id) & 0xFFFFF  # 20-bit space
                order_nonce = gen_order_nonce(random_int=tag)
            else:
                order_nonce = gen_order_nonce()

            order = OrderParams(
                sender=sender_hex,
                priceX18=price_x18,
                amount=amount_x18,
                expiration=get_expiration_timestamp(expiration_secs),
                nonce=order_nonce,
                appendix=self._build_order_appendix(
                    appendix_order_type_int,
                    isolated=bool(isolated_only),
                    reduce_only=bool(reduce_only),
                    margin_x6=isolated_margin_x6,
                    builder_id=builder_id,
                    builder_fee_rate=builder_fee_rate,
                ),
            )

            params = PlaceOrderParams(product_id=product_id, order=order, id=tag)
            result = self._dispatch_execute(params, "place_order", product_id=product_id)

            if hasattr(result, 'data') and result.data:
                if hasattr(result.data, 'digest') and result.data.digest:
                    # A confirmed execute proves the IP can write again — close
                    # any open ip_query_only write circuit immediately.
                    try:
                        from src.nadobro.venue.gateway_budget import clear_write_ban
                        clear_write_ban(self._rest_url())
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "clear_write_ban failed after confirmed execute — "
                            "write circuit may stay armed and short-circuit healthy writes: %s",
                            e,
                        )
                    return {
                        "success": True,
                        "digest": result.data.digest,
                        "product_id": product_id,
                        "size": size,
                        "price": price,
                        "side": "LONG" if is_buy else "SHORT",
                        "client_id": tag,
                    }

            result_str = str(result)
            lowered_result = result_str.lower()
            is_failure_result = (
                (hasattr(result, "status") and str(getattr(result, "status")).lower() == "failure")
                or ('"status":"failure"' in lowered_result)
                or ("'status': 'failure'" in lowered_result)
            )
            is_blocked_result = "blocked" in lowered_result or "reason" in lowered_result

            # ip_query_only: Nado downgrades a saturated IP to query-only — every
            # execute is rejected while the ban holds (we have seen it persist
            # for many minutes, e.g. 2026-05-29 00:29 → 00:43). A sub-second
            # inline retry cannot beat a multi-minute ban and only adds load, so
            # instead we OPEN the per-host write circuit (see gateway_budget) and
            # return a clear transient error. Subsequent executes short-circuit
            # cheaply until the cooldown lapses, letting the IP recover and the
            # controller retry on a later tick. NOTE: the SDK usually *raises*
            # this rather than returning it — the exception handler below records
            # it too; this branch covers the returned-result shape.
            compact_result = lowered_result.replace("_", "").replace("-", "")
            if (is_failure_result or is_blocked_result) and "ipqueryonly" in compact_result:
                try:
                    from src.nadobro.venue.gateway_budget import record_ip_query_only
                    record_ip_query_only(self._rest_url())
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "record_ip_query_only failed — write circuit not armed; "
                        "executes may keep failing with no backoff: %s",
                        e,
                    )
                logger.warning(
                    "place_order rejected ip_query_only (write circuit armed) "
                    "product_id=%s sender=%s host=%s raw=%s",
                    product_id, _mask_address(sender_hex), self._rest_url(),
                    _mask_payload(result_str),
                )
                return {
                    "success": False,
                    "error": "Venue temporarily blocked order placement (ip_query_only). Retrying shortly.",
                    "rate_limited": True,
                    "ip_query_only": True,
                }

            if is_failure_result:
                return {"success": False, "error": self._friendly_error(result_str)}
            if is_blocked_result:
                return {"success": False, "error": self._friendly_error(result_str)}

            return {
                "success": False,
                "error": self._friendly_error(
                    "Exchange did not confirm order acceptance (missing digest/status)."
                ),
            }
        except Exception as e:
            err_str = str(e)

            # ip_query_only is RAISED by the SDK (requests/engine_client throws
            # on the rejection body), so it never reaches the returned-result
            # branch above — this is the real-world path. Arm the write circuit
            # and return a transient error instead of falling through to the
            # increment/min-notional retries (which don't apply) and a misleading
            # hard failure. This is the fix for the 2026-05-29 incident where
            # three executes failed 24ms apart with no backoff and no retry.
            compact_err = err_str.lower().replace("_", "").replace("-", "")
            if "ipqueryonly" in compact_err:
                try:
                    from src.nadobro.venue.gateway_budget import record_ip_query_only
                    record_ip_query_only(self._rest_url())
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "record_ip_query_only failed — write circuit not armed; "
                        "executes may keep failing with no backoff: %s",
                        e,
                    )
                logger.warning(
                    "place_order raised ip_query_only (write circuit armed) "
                    "product_id=%s sender=%s host=%s raw=%s",
                    product_id, _mask_address(sender_hex), self._rest_url(),
                    _mask_payload(err_str),
                )
                return {
                    "success": False,
                    "error": "Venue temporarily blocked order placement (ip_query_only). Retrying shortly.",
                    "rate_limited": True,
                    "ip_query_only": True,
                }

            increment = self._extract_price_increment_from_error(err_str, product_id)
            size_increment = self._extract_size_increment_from_error(err_str, product_id)
            min_size_x18 = self._extract_min_size_x18_from_error(err_str, product_id)

            if increment and increment > 0:
                _price_increment_cache[(self.network, product_id)] = increment
            if size_increment and size_increment > 0:
                _size_increment_cache[(self.network, product_id)] = size_increment
            if min_size_x18 and min_size_x18 > 0:
                _min_size_x18_cache[(self.network, product_id)] = int(min_size_x18)

            # Retry with normalized size/price when exchange returns increment errors.
            if _retry_count < 3 and ((increment and increment > 0) or (size_increment and size_increment > 0)):
                retry_size = size
                retry_price = price
                if size_increment and size_increment > 0:
                    retry_size = self._align_size_to_increment(size, size_increment)
                if increment and increment > 0:
                    retry_price = self._align_price_to_increment(price, increment, is_buy, order_type)

                if retry_size > 0 and (abs(retry_size - size) > 1e-12 or abs(retry_price - price) > 1e-12):
                    logger.info(
                        "Retrying place_order with aligned increments product_id=%s size=%s->%s price=%s->%s",
                        product_id,
                        size,
                        retry_size,
                        price,
                        retry_price,
                    )
                    try:
                        return self.place_order(
                            product_id=product_id,
                            size=retry_size,
                            price=retry_price,
                            order_type=order_type,
                            is_buy=is_buy,
                            isolated_only=isolated_only,
                            isolated_margin=isolated_margin,
                            reduce_only=reduce_only,
                            sender=sender,
                            client_id=client_id,
                            never_grow=never_grow,
                            _retry_count=_retry_count + 1,
                        )
                    except Exception as retry_e:
                        logger.error("place_order retry failed: %s", retry_e)
                        return {"success": False, "error": self._friendly_error(str(retry_e))}

            # Retry with bumped size when exchange enforces min notional.
            # SPOT-CLOSE-BUMP: never for a reducing order — see never_grow.
            if never_grow and self._is_min_notional_error(err_str):
                logger.warning(
                    "place_order min-notional REJECT not bumped (reducing order) "
                    "product_id=%s size=%s is_buy=%s — a close must never grow; "
                    "the remainder is below the venue minimum and needs a "
                    "marketable/aggregated exit instead",
                    product_id, size, is_buy,
                )
                return {"success": False, "error": self._friendly_error(err_str),
                        "min_notional_block": True}
            if _retry_count < 3 and not never_grow and self._is_min_notional_error(err_str):
                required_min_x18 = _min_size_x18_cache.get((self.network, product_id)) or min_size_x18
                if required_min_x18 and required_min_x18 > 0 and price > 0:
                    required_notional = float(required_min_x18) / 1e18
                    # Add tiny safety buffer so downstream tick/rounding does not dip below threshold.
                    target_size = (required_notional / float(price)) * 1.01
                    if size_increment and size_increment > 0:
                        target_size = self._align_size_up_to_increment(target_size, size_increment)
                    retry_size = max(float(size), float(target_size))
                    if retry_size > size:
                        logger.info(
                            "Retrying place_order with min-notional bump product_id=%s size=%s->%s min_notional=%s",
                            product_id,
                            size,
                            retry_size,
                            required_notional,
                        )
                        try:
                            return self.place_order(
                                product_id=product_id,
                                size=retry_size,
                                price=price,
                                order_type=order_type,
                                is_buy=is_buy,
                                isolated_only=isolated_only,
                                isolated_margin=isolated_margin,
                                reduce_only=reduce_only,
                                sender=sender,
                                client_id=client_id,
                                never_grow=never_grow,
                                _retry_count=_retry_count + 1,
                            )
                        except Exception as retry_e:
                            logger.error("place_order min-notional retry failed: %s", retry_e)
                            return {"success": False, "error": self._friendly_error(str(retry_e))}

            # Some exchange error payloads truncate size_increment value. In that case
            # probe a small ladder of common increments to find a valid divisible amount.
            if _retry_count < 6 and self._is_size_increment_error(err_str):
                for candidate_size in self._fallback_size_candidates(size):
                    if abs(candidate_size - size) <= 1e-12:
                        continue
                    logger.info(
                        "Retrying place_order with fallback size alignment product_id=%s size=%s->%s",
                        product_id,
                        size,
                        candidate_size,
                    )
                    try:
                        retry_result = self.place_order(
                            product_id=product_id,
                            size=candidate_size,
                            price=price,
                            order_type=order_type,
                            is_buy=is_buy,
                            isolated_only=isolated_only,
                            isolated_margin=isolated_margin,
                            reduce_only=reduce_only,
                            sender=sender,
                            client_id=client_id,
                            never_grow=never_grow,
                            _retry_count=_retry_count + 1,
                        )
                        if retry_result.get("success"):
                            return retry_result
                    except Exception as retry_e:
                        logger.error("place_order fallback retry failed: %s", retry_e)
                    # Keep trying broader candidate increments until one succeeds.
                    continue

            if increment and increment > 0:
                aligned_price = self._align_price_to_increment(price, increment, is_buy, order_type)
                if aligned_price > 0 and abs(aligned_price - price) > 1e-12:
                    logger.info(
                        "Retrying place_order with aligned tick price product_id=%s original=%s aligned=%s increment=%s",
                        product_id,
                        price,
                        aligned_price,
                        increment,
                    )
                    try:
                        return self.place_order(
                            product_id=product_id,
                            size=size,
                            price=aligned_price,
                            order_type=order_type,
                            is_buy=is_buy,
                            isolated_only=isolated_only,
                            isolated_margin=isolated_margin,
                            reduce_only=reduce_only,
                            sender=sender,
                            client_id=client_id,
                            never_grow=never_grow,
                            _retry_count=_retry_count + 1,
                        )
                    except Exception as retry_e:
                        logger.error("place_order aligned-price retry failed: %s", retry_e)
                        return {"success": False, "error": self._friendly_error(str(retry_e))}

            logger.error("place_order failed: %s", err_str)
            return {"success": False, "error": self._friendly_error(err_str)}

    def place_market_order(
        self,
        product_id: int,
        size: float,
        is_buy: bool = True,
        slippage_pct: float = 1.0,
        isolated_only: bool = False,
        isolated_margin: Optional[float] = None,
        reduce_only: bool = False,
        never_grow: bool = False,
        sender: Optional[str] = None,
        client_id: Optional[int] = None,
    ) -> dict:
        # AUDIT-FIX-NC-1: defensive dict access. get_market_price can return
        # an unexpected shape (None on certain SDK error paths); the previous
        # mp["mid"] would KeyError/TypeError and crash the placement path.
        mp = self.get_market_price(product_id) or {}
        try:
            mid_value = float(mp.get("mid") or 0)
            ask_value = float(mp.get("ask") or 0)
            bid_value = float(mp.get("bid") or 0)
        except (TypeError, ValueError):
            return {"success": False, "error": "Could not fetch market price"}
        if mid_value <= 0 or (ask_value <= 0 and bid_value <= 0):
            return {"success": False, "error": "Could not fetch market price"}
        try:
            slippage_pct = float(slippage_pct)
        except (TypeError, ValueError):
            slippage_pct = 1.0
        slippage_pct = max(0.1, min(slippage_pct, 10.0))
        multiplier = 1.0 + (slippage_pct / 100.0)
        price = ask_value * multiplier if is_buy else bid_value / multiplier
        return self.place_order(
            product_id,
            size,
            price,
            order_type="ioc",
            is_buy=is_buy,
            isolated_only=isolated_only,
            isolated_margin=isolated_margin,
            reduce_only=reduce_only,
            sender=sender,
            client_id=client_id,
            never_grow=never_grow,
        )

    def place_limit_order(
        self,
        product_id: int,
        size: float,
        price: float,
        is_buy: bool = True,
        isolated_only: bool = False,
        isolated_margin: Optional[float] = None,
        reduce_only: bool = False,
        never_grow: bool = False,
        post_only: bool = False,
        sender: Optional[str] = None,
        client_id: Optional[int] = None,
    ) -> dict:
        return self.place_order(
            product_id,
            size,
            price,
            order_type="post_only" if post_only else "default",
            is_buy=is_buy,
            isolated_only=isolated_only,
            isolated_margin=isolated_margin,
            reduce_only=reduce_only,
            sender=sender,
            client_id=client_id,
            never_grow=never_grow,
        )

    @staticmethod
    def _result_is_ip_query_only(result) -> bool:
        """BUG-CANCEL-1: detect the same transient ip_query_only downgrade that
        place_order handles, but on a cancel response. Nado returns this as a
        failure/blocked *status in the response body* (not an exception), so
        the cancel paths used to silently report success while the venue had
        actually refused the cancel."""
        try:
            result_str = str(result)
        except Exception:  # pragma: no cover
            return False
        lowered = result_str.lower()
        is_failure = (
            (hasattr(result, "status") and str(getattr(result, "status")).lower() == "failure")
            or ('"status":"failure"' in lowered)
            or ("'status': 'failure'" in lowered)
        )
        is_blocked = "blocked" in lowered or "reason" in lowered
        compact = lowered.replace("_", "").replace("-", "")
        return (is_failure or is_blocked) and "ipqueryonly" in compact

    def cancel_order(
        self, product_id: int, digest: str, sender: Optional[str] = None, _retry_count: int = 0
    ) -> dict:
        if not self._initialized or not self.client:
            return {"success": False, "error": "Client not initialized"}

        eff_sender = (sender or "").strip() or self.subaccount_hex
        # Cancel with 1 digest = wallet weight 1.
        if not self._gateway_allowed(weight=1, kind="execute", wallet=eff_sender, user_scoped=False):
            return {"success": False, "error": "Rate limited — please retry in a moment.", "rate_limited": True}

        try:
            from nado_protocol.engine_client.types.execute import CancelOrdersParams

            cancel_params = CancelOrdersParams(
                sender=eff_sender,
                productIds=[product_id],
                digests=[digest],
            )
            result = self.client.market.cancel_orders(cancel_params)
            # BUG-CANCEL-1: never falsely report a query-only-blocked cancel as
            # success (that would leave a stale order live). Arm the write
            # circuit so subsequent executes short-circuit until the ban lifts,
            # and return a transient failure the caller can retry.
            if self._result_is_ip_query_only(result):
                try:
                    from src.nadobro.venue.gateway_budget import record_ip_query_only
                    record_ip_query_only(self._rest_url())
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "record_ip_query_only failed — write circuit not armed; "
                        "executes may keep failing with no backoff: %s",
                        e,
                    )
                logger.warning(
                    "cancel_order rejected ip_query_only (write circuit armed) "
                    "product_id=%s host=%s raw=%s",
                    product_id, self._rest_url(), _mask_payload(result),
                )
                return {
                    "success": False,
                    "error": self._friendly_error(str(result)),
                    "digest": digest,
                    "rate_limited": True,
                    "ip_query_only": True,
                }
            try:
                from src.nadobro.venue.gateway_budget import clear_write_ban
                clear_write_ban(self._rest_url())
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "clear_write_ban failed after confirmed cancel — "
                    "write circuit may stay armed and short-circuit healthy writes: %s",
                    e,
                )
            return {"success": True, "digest": digest}
        except Exception as e:
            err_str = str(e)
            compact_err = err_str.lower().replace("_", "").replace("-", "")
            if "ipqueryonly" in compact_err:
                try:
                    from src.nadobro.venue.gateway_budget import record_ip_query_only
                    record_ip_query_only(self._rest_url())
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "record_ip_query_only failed — write circuit not armed; "
                        "executes may keep failing with no backoff: %s",
                        e,
                    )
                logger.warning(
                    "cancel_order raised ip_query_only (write circuit armed) "
                    "product_id=%s host=%s raw=%s",
                    product_id, self._rest_url(), _mask_payload(err_str),
                )
                return {"success": False, "error": err_str, "digest": digest, "rate_limited": True, "ip_query_only": True}
            logger.error(f"cancel_order failed: {e}")
            return {"success": False, "error": err_str}

    def cancel_all_orders(self, product_id: int) -> dict:
        orders = self.get_open_orders(product_id)
        results = []
        for o in orders:
            r = self.cancel_order(product_id, o["digest"])
            results.append(r)
        return {"success": True, "cancelled": len([r for r in results if r["success"]])}

    def get_perp_funding_rates(self, product_ids: list[int]) -> dict:
        """Latest funding rates for the given perp products, read from the
        indexer funding endpoint (SDK ``get_perp_funding_rates``).

        Returns ``{product_id(int): {"product_id", "funding_rate", "update_time"}}``
        where ``funding_rate`` is the **signed 24h (daily) rate**: positive means
        longs pay shorts (favorable for a Delta-Neutral short leg), negative means
        shorts pay longs. The indexer reports ``funding_rate_x18`` (the daily rate
        × 1e18), so we divide by 1e18.

        This is the *funding rate* — NOT the cumulative funding accumulator
        (``cum_funding_x18``) the old ``all_products`` read mistook for a rate
        (that field is a monotonic settlement index, not a rate). Sign is
        preserved so callers can tell favorable from unfavorable funding.
        """
        from src.nadobro.venue.nado_weights import query_weight

        ids = [int(p) for p in (product_ids or []) if p is not None]
        if not ids:
            return {}
        if not self._ensure_sdk_client():
            return {}
        # Indexer reads charge the archive host's per-IP token bucket
        # (user_scoped=False ⇒ no in-flight slot to release). One batched request
        # covers every perp regardless of list length.
        if not self._gateway_allowed(
            weight=query_weight("funding_rate"), url=self._archive_url(), user_scoped=False
        ):
            return {}
        try:
            raw = self.client.context.indexer_client.get_perp_funding_rates(ids)
        except Exception as e:  # noqa: BLE001 - normalize venue errors
            logger.error("SDK get_perp_funding_rates failed: %s", _format_sdk_error(e))
            return {}
        plain = self._to_plain(raw)
        rows = plain.values() if isinstance(plain, dict) else (plain or [])
        rates: dict = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = row.get("product_id")
            raw_rate = row.get("funding_rate_x18")
            if pid is None or raw_rate is None:
                continue
            try:
                pid_int = int(pid)
                rate = int(str(raw_rate)) / 1e18
            except (TypeError, ValueError):
                continue
            rates[pid_int] = {
                "product_id": pid_int,
                "funding_rate": rate,
                "update_time": row.get("update_time"),
            }
        return rates

    def get_all_funding_rates(self) -> dict:
        cache_key = f"{self.network}:funding"
        with _caches_lock:
            cached = _FUNDING_CACHE.get(cache_key)
        if cached and (time.time() - cached["ts"] < _FUNDING_TTL):
            return cached["data"]
        product_ids = [
            int(pid)
            for name in get_perp_products(network=self.network, client=self)
            if (pid := get_product_id(name, network=self.network, client=self)) is not None
        ]
        rates = self.get_perp_funding_rates(product_ids)
        if rates:
            with _caches_lock:
                _FUNDING_CACHE[cache_key] = {"data": rates, "ts": time.time()}
        return rates

    def get_funding_rate(self, product_id: int) -> Optional[dict]:
        rates = self.get_all_funding_rates()
        try:
            return rates.get(int(product_id))
        except (TypeError, ValueError):
            return rates.get(product_id)

    def get_product_market_stats(self, product_id: int) -> dict:
        """
        Best-effort market stats for a product from Nado gateway payloads.

        Fields are optional because API payload keys can differ by environment.
        """
        stats = {
            "product_id": int(product_id),
            "product_name": get_product_name(int(product_id), network=self.network, client=self),
            "bid": 0.0,
            "ask": 0.0,
            "mid": 0.0,
            "spread": 0.0,
            "spread_bps": 0.0,
            "funding_rate": None,
            "volume_24h_usd": None,
            "open_interest": None,
            "change_24h_pct": None,
            "high_24h": None,
            "low_24h": None,
            "mark_price": None,
            "index_price": None,
        }

        try:
            px = self.get_market_price(int(product_id)) or {}
            bid = float(px.get("bid") or 0)
            ask = float(px.get("ask") or 0)
            mid = float(px.get("mid") or 0)
            spread = (ask - bid) if (ask > 0 and bid > 0) else 0.0
            spread_bps = (spread / mid * 10000.0) if mid > 0 else 0.0
            stats.update(
                {
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "spread": spread,
                    "spread_bps": spread_bps,
                }
            )
        except Exception:  # policy: degrade-ok(stats enrichment is display-only)
            pass

        try:
            fr = self.get_funding_rate(int(product_id)) or {}
            if isinstance(fr, dict) and fr.get("funding_rate") is not None:
                stats["funding_rate"] = float(fr.get("funding_rate") or 0)
        except Exception:  # policy: degrade-ok(stats enrichment is display-only)
            pass

        try:
            data = self._query_rest("all_products") or {}
            if data.get("status") != "success":
                return stats
            rows = ((data.get("data") or {}).get("perp_products") or [])
            if not isinstance(rows, list):
                return stats
            row = None
            for candidate in rows:
                try:
                    if int(candidate.get("product_id")) == int(product_id):
                        row = candidate
                        break
                except Exception:  # policy: degrade-ok(malformed catalog row; skipped)
                    continue
            if not isinstance(row, dict):
                return stats

            book = row.get("book_info") or {}

            def _pick(*keys):
                for key in keys:
                    if key in row and row.get(key) is not None:
                        return row.get(key)
                    if key in book and book.get(key) is not None:
                        return book.get(key)
                return None

            volume_raw = _pick(
                "volume_24h",
                "quote_volume_24h",
                "notional_volume_24h",
                "turnover_24h",
                "total_volume_24h",
                "volume24h",
            )
            oi_raw = _pick("open_interest", "open_interest_x18", "oi", "oi_x18")
            change_raw = _pick(
                "change_24h_pct",
                "price_change_24h_pct",
                "percent_change_24h",
                "change24h",
            )
            high_raw = _pick("high_24h", "price_high_24h")
            low_raw = _pick("low_24h", "price_low_24h")
            mark_raw = _pick("mark_price", "mark_price_x18")
            index_raw = _pick("index_price", "index_price_x18", "oracle_price", "oracle_price_x18")

            if volume_raw is not None:
                stats["volume_24h_usd"] = float(self._from_x18_dynamic(volume_raw))
            if oi_raw is not None:
                stats["open_interest"] = float(self._from_x18_dynamic(oi_raw))
            if change_raw is not None:
                stats["change_24h_pct"] = float(self._from_x18_dynamic(change_raw))
            if high_raw is not None:
                stats["high_24h"] = float(self._from_x18_dynamic(high_raw))
            if low_raw is not None:
                stats["low_24h"] = float(self._from_x18_dynamic(low_raw))
            if mark_raw is not None:
                stats["mark_price"] = float(self._from_x18_dynamic(mark_raw))
            if index_raw is not None:
                stats["index_price"] = float(self._from_x18_dynamic(index_raw))
        except Exception as e:
            logger.debug("get_product_market_stats all_products lookup failed for %s: %s", product_id, e)

        return stats

    def get_all_products_info(self) -> dict:
        cache_key = f"{self.network}:products"
        cached = _ALL_PRODUCTS_CACHE.get(cache_key)
        if cached and (time.time() - cached["ts"] < _ALL_PRODUCTS_TTL):
            return cached["data"]
        # Redis layer: the catalog is identical for every user on a network and
        # changes rarely, so absorb the per-cycle build_product_meta_from_catalog
        # hits across all workers via a shared Upstash entry before paying for an
        # SDK get_all_products call (a per-IP gateway query that helped saturate
        # the gateway into ip_query_only). On a hit, also warm the in-process
        # cache so subsequent same-process calls skip Redis entirely.
        redis_key = f"nado:catalog:{self.network}:products"
        redis_cached = self._read_shared_cache(redis_key)
        if isinstance(redis_cached, dict) and "perp" in redis_cached and "spot" in redis_cached:
            _ALL_PRODUCTS_CACHE[cache_key] = {"data": redis_cached, "ts": time.time()}
            return redis_cached
        try:
            if self._initialized and self.client:
                products = self.client.context.engine_client.get_all_products()
                data = {
                    "perp": [{"id": p.product_id} for p in products.perp_products],
                    "spot": [{"id": p.product_id} for p in products.spot_products],
                }
                _ALL_PRODUCTS_CACHE[cache_key] = {"data": data, "ts": time.time()}
                self._write_shared_cache(redis_key, data, _ALL_PRODUCTS_TTL)
                return data
        except Exception as e:
            logger.error(f"get_all_products_info failed: {e}")
        return {"perp": [], "spot": []}

    # ------------------------------------------------------------------
    # NLP Vault (Nado Liquidity Provider) — deposit / withdraw / pool stats
    # ------------------------------------------------------------------

    def mint_nlp(self, usdt0_amount: float, *, spot_leverage: bool = False) -> dict:
        """Deposit USDT0 into the NLP vault.

        Args:
            usdt0_amount: Amount of USDT0 to lock in the vault (UI-friendly float).
                Internally converted to integer x10^18 per Nado gateway contract.
            spot_leverage: When False (default) the gateway will reject the mint if
                fulfilling it requires borrowing against existing positions. Forced
                False so a vault deposit can never silently lever up the user's
                trading account.
        """
        if not self._initialized or not self.client:
            return {"success": False, "error": "Client not initialized. Please try /start again."}
        if usdt0_amount is None or float(usdt0_amount) <= 0:
            return {"success": False, "error": "Deposit amount must be positive."}
        sender_hex = self.subaccount_hex or ""
        if not sender_hex:
            return {"success": False, "error": "Subaccount unavailable. Re-link your wallet via /start."}
        try:
            from nado_protocol.engine_client.types.execute import MintNlpParams
            quote_amount_x18 = int(round(float(usdt0_amount) * 1e18))
            if quote_amount_x18 <= 0:
                return {"success": False, "error": "Deposit amount rounds to zero. Try a larger amount."}
            params = MintNlpParams(
                sender=sender_hex,
                quoteAmount=quote_amount_x18,
                spot_leverage=bool(spot_leverage),
            )
            resp = self.client.market.mint_nlp(params)
            digest = getattr(resp, "digest", None) or getattr(resp, "tx_hash", None)
            return {
                "success": True,
                "quote_amount_usdt0": float(usdt0_amount),
                "quote_amount_x18": quote_amount_x18,
                "digest": digest,
            }
        except Exception as e:
            logger.error("mint_nlp failed user=%s err=%s", _mask_address(self.address or ""), e)
            return {"success": False, "error": self._friendly_error(str(e))}

    def burn_nlp(self, nlp_amount: float, *, amount_x18: int | None = None) -> dict:
        """Burn NLP tokens to withdraw USDT0 from the vault.

        Args:
            nlp_amount: Amount of NLP tokens to redeem (UI-friendly float).
                Converted to integer x10^18 per gateway contract.
            amount_x18: EXACT on-chain amount in wei. When supplied it wins over
                ``nlp_amount``: a "burn everything" must send the venue's own
                integer, because ``int(round(float(U)/1e18 * 1e18))`` can round
                a few wei ABOVE the real balance and the venue rejects that with
                error 2096 ("Do not have enough unlocked NLP").
        """
        if not self._initialized or not self.client:
            return {"success": False, "error": "Client not initialized. Please try /start again."}
        if amount_x18 is None and (nlp_amount is None or float(nlp_amount) <= 0):
            return {"success": False, "error": "Withdraw amount must be positive."}
        sender_hex = self.subaccount_hex or ""
        if not sender_hex:
            return {"success": False, "error": "Subaccount unavailable. Re-link your wallet via /start."}
        try:
            from nado_protocol.engine_client.types.execute import BurnNlpParams
            if amount_x18 is not None:
                nlp_amount_x18 = int(amount_x18)
                nlp_amount = nlp_amount_x18 / 1e18
            else:
                nlp_amount_x18 = int(round(float(nlp_amount) * 1e18))
            if nlp_amount_x18 <= 0:
                return {"success": False, "error": "Withdraw amount rounds to zero. Try a larger amount."}
            params = BurnNlpParams(sender=sender_hex, nlpAmount=nlp_amount_x18)
            resp = self.client.market.burn_nlp(params)
            digest = getattr(resp, "digest", None) or getattr(resp, "tx_hash", None)
            return {
                "success": True,
                "nlp_amount": float(nlp_amount),
                "nlp_amount_x18": nlp_amount_x18,
                "digest": digest,
            }
        except Exception as e:
            logger.error("burn_nlp failed user=%s err=%s", _mask_address(self.address or ""), e)
            return {"success": False, "error": self._friendly_error(str(e))}

    @staticmethod
    def _x18_to_float(value) -> float:
        if value is None or value == "":
            return 0.0
        try:
            v = float(int(value)) / 1e18
            return v
        except (TypeError, ValueError):
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

    def resolve_nlp_product_id(self) -> int | None:
        """Discover the NLP spot product id from all_products (cached per client).

        NLP is the only spot product configured as a zero-interest, zero-spot-
        withdraw-fee vault token: its 4-day lock and bps fee live in the vault,
        not the spot interest model, so ``interest_*_x18`` and ``withdraw_fee_x18``
        are all 0 (every other spot — kBTC, wETH, RWAs — carries non-zero borrow
        interest). We anchor to the known per-network id and validate it against
        that config.

        Bug history: the previous heuristic matched ``book_info.min_size`` starting
        with ``100000000000000000``, but EVERY Nado spot shares
        ``min_size=100000000000000000000`` — so the scan returned the FIRST match
        (kBTC, pid 1, ~$64k) and the vault reported the user's kBTC dust as a
        phantom NLP position (~$3.20 for 0.00005 kBTC) instead of $0.
        """
        cached = getattr(self, "_nlp_product_id", None)
        if cached is not None:
            return cached
        override = os.environ.get("NADO_NLP_PRODUCT_ID")
        if override:
            try:
                self._nlp_product_id = int(override)
                return self._nlp_product_id
            except ValueError:
                pass
        default = 11 if self.network == "mainnet" else 1

        def _is_zero(cfg: dict, key: str) -> bool:
            try:
                return int(cfg.get(key) or 0) == 0
            except (TypeError, ValueError):
                return False

        def _is_nlp_like(sp: dict) -> bool:
            cfg = sp.get("config") or {}
            # The vault token has no spot borrow interest and no spot withdraw fee.
            return (
                _is_zero(cfg, "interest_floor_x18")
                and _is_zero(cfg, "interest_small_cap_x18")
                and _is_zero(cfg, "interest_large_cap_x18")
                and _is_zero(cfg, "withdraw_fee_x18")
            )

        try:
            payload = self._query_rest("all_products") or {}
            data = payload.get("data") or payload
            candidates: list[int] = []
            for sp in data.get("spot_products") or []:
                if not isinstance(sp, dict):
                    continue
                pid = sp.get("product_id")
                if pid is None or int(pid) == 0:  # 0 = USDT0 quote, never NLP
                    continue
                if _is_nlp_like(sp):
                    candidates.append(int(pid))
            # Prefer the known per-network id when the venue confirms it; else, if
            # the config uniquely identifies one product, trust that; otherwise
            # fall back to the per-network default.
            if default in candidates:
                self._nlp_product_id = default
                return default
            if len(candidates) == 1:
                self._nlp_product_id = candidates[0]
                return candidates[0]
        except Exception as e:
            logger.debug("resolve_nlp_product_id failed network=%s err=%s", self.network, e)
        self._nlp_product_id = default
        return self._nlp_product_id

    def _spot_balance_amount(self, product_id: int) -> float:
        try:
            payload = self._query_rest("subaccount_info", {"subaccount": self.subaccount_hex}) or {}
            data = payload.get("data") or payload
            for row in data.get("spot_balances") or data.get("spotBalances") or []:
                if not isinstance(row, dict):
                    continue
                pid = row.get("product_id")
                if int(pid) != int(product_id):
                    continue
                bal = row.get("balance") or {}
                return self._x18_to_float(bal.get("amount"))
        except Exception as e:
            logger.debug("spot_balance_amount failed pid=%s err=%s", product_id, e)
        return 0.0

    def _nlp_oracle_price(self, nlp_product_id: int) -> float:
        try:
            payload = self._query_rest("all_products") or {}
            data = payload.get("data") or payload
            for sp in data.get("spot_products") or []:
                if not isinstance(sp, dict):
                    continue
                if int(sp.get("product_id") or -1) != int(nlp_product_id):
                    continue
                price_x18 = (
                    sp.get("oracle_price_x18")
                    or (sp.get("risk") or {}).get("price_x18")
                )
                px = self._x18_to_float(price_x18)
                return px if px > 0 else 1.0
        except Exception as e:
            logger.debug("nlp_oracle_price failed err=%s", e)
        return 1.0

    def get_nlp_locked_balances(self) -> dict:
        """Return locked/unlocked NLP balances and per-lock unlock timestamps.

        ``ok`` distinguishes "the venue says you have nothing locked" from "we
        could not find out". This query costs gateway weight 20 against an
        8 rps / 24 burst per-user budget, so a throttled call returns None from
        ``_query_rest`` — booking that as ``unlocked = 0`` (the old behaviour)
        made the vault card fall back to the raw spot balance and label the
        WHOLE position "Unlocked", so a 100% withdraw tried to burn locked
        tokens and the venue rejected it with error 2096.

        ``balance_unlocked_x18`` is the EXACT on-chain integer (wei). Burning
        the max must use it directly: the float round-trip
        (``int(round(float(U)/1e18 * 1e18))``) can land a few wei ABOVE ``U``,
        which the venue also rejects with 2096.
        """
        empty = {
            "ok": False,
            "balance_locked": 0.0,
            "balance_unlocked": 0.0,
            "balance_unlocked_x18": 0,
            "locked_entries": [],
        }
        try:
            payload = self._query_rest(
                "nlp_locked_balances", {"subaccount": self.subaccount_hex},
            )
            if not payload:
                # Throttled / transport failure — UNKNOWN, never "zero".
                return empty
            data = payload.get("data") or payload
            if not isinstance(data, dict):
                return empty
            locked = data.get("balance_locked") or {}
            unlocked = data.get("balance_unlocked") or {}
            entries = []
            for row in data.get("locked_balances") or []:
                if not isinstance(row, dict):
                    continue
                bal = (row.get("balance") or {}).get("balance") or {}
                entries.append({
                    "amount": self._x18_to_float(bal.get("amount")),
                    "unlocked_at": int(row.get("unlocked_at") or 0),
                })
            unlocked_raw = (unlocked.get("balance") or {}).get("amount")
            try:
                unlocked_x18 = int(unlocked_raw) if unlocked_raw is not None else 0
            except (TypeError, ValueError):
                unlocked_x18 = 0
            return {
                "ok": True,
                "balance_locked": self._x18_to_float((locked.get("balance") or {}).get("amount")),
                "balance_unlocked": self._x18_to_float(unlocked_raw),
                "balance_unlocked_x18": unlocked_x18,
                "locked_entries": entries,
            }
        except Exception as e:
            logger.debug("nlp_locked_balances failed user=%s err=%s", _mask_address(self.address or ""), e)
            return empty

    def get_max_nlp_mintable(self, *, spot_leverage: bool = False, product_id: int | None = None) -> dict:
        """Maximum USDT0 the user can mint into NLP right now.

        ``product_id`` is REQUIRED by the gateway (QueryMaxLpMintableParams =
        sender + product_id). Omitting it returned 0 for everyone, which the UI
        surfaced as "deposits closed" even while the vault was open. Resolve the
        NLP product id when the caller doesn't supply it.
        """
        try:
            pid = int(product_id) if product_id is not None else self.resolve_nlp_product_id()
            if pid is None:
                return {"ok": False, "max_mintable_usdt0": 0.0, "raw": {}}
            payload = self._query_rest(
                "max_nlp_mintable",
                {
                    "sender": self.subaccount_hex,
                    "product_id": int(pid),
                    "spot_leverage": "true" if spot_leverage else "false",
                },
            )
            if payload is None:
                # Gateway-budget throttle or transport failure. This MUST NOT
                # read as "mintable = 0": the vault card once conflated a
                # throttled first query with $0 and told a depositable user
                # their margin was in use (2026-07-18 incident — the query
                # costs weight 20 against an 8 rps / 24 burst user budget, so
                # the snapshot's own call burst throttled it).
                return {"ok": False, "max_mintable_usdt0": 0.0, "raw": {}}
            data = payload.get("data") or payload
            amount_x18 = data.get("max_quote_amount")
            if amount_x18 is None:
                return {"ok": False, "max_mintable_usdt0": 0.0, "raw": data}
            return {
                "ok": True,
                "max_mintable_usdt0": self._x18_to_float(amount_x18),
                "raw": data,
            }
        except Exception as e:
            logger.debug("max_nlp_mintable failed user=%s err=%s", _mask_address(self.address or ""), e)
            return {"ok": False, "max_mintable_usdt0": 0.0, "raw": {}}

    def get_nlp_position(self) -> dict:
        """Return the user's NLP position (LP balance + USDT0 NAV).

        Gateway no longer exposes ``nlp_position``; derive from spot balance,
        oracle price, and locked-balance metadata.
        """
        result: dict = {
            "exists": False,
            "lp_balance": 0.0,
            "lp_value_usdt0": 0.0,
            "last_mint_ts_ms": None,
            "nlp_product_id": None,
            # Burnable truth. ``unlocked_known`` False means the locked-balance
            # query did not answer — callers must NOT treat lp_balance as
            # burnable (that is what produced venue error 2096).
            "unlocked_known": False,
            "lp_unlocked": 0.0,
            "lp_unlocked_x18": 0,
            "lp_locked": 0.0,
        }
        try:
            nlp_pid = self.resolve_nlp_product_id()
            if nlp_pid is None:
                return result
            locked_info = self.get_nlp_locked_balances()
            unlocked_known = bool(locked_info.get("ok"))
            lp_balance = locked_info["balance_locked"] + locked_info["balance_unlocked"]
            if lp_balance <= 0:
                # Either the query failed (unknown) or the venue reports no
                # lock rows for a balance the spot ledger still shows. Display
                # the spot balance, but do NOT claim any of it is burnable.
                spot_balance = self._spot_balance_amount(nlp_pid)
                if spot_balance > 0:
                    unlocked_known = False
                lp_balance = spot_balance
            result.update({
                "unlocked_known": unlocked_known,
                "lp_unlocked": float(locked_info["balance_unlocked"]) if unlocked_known else 0.0,
                "lp_unlocked_x18": int(locked_info.get("balance_unlocked_x18") or 0) if unlocked_known else 0,
                "lp_locked": float(locked_info["balance_locked"]) if unlocked_known else 0.0,
            })
            oracle = self._nlp_oracle_price(nlp_pid)
            lp_value = max(0.0, lp_balance * oracle)
            last_mint_ts_ms = None
            if locked_info["locked_entries"]:
                # Most recent lock → infer mint time (4-day lockup before burn).
                latest_unlock = max(e["unlocked_at"] for e in locked_info["locked_entries"])
                if latest_unlock > 0:
                    last_mint_ts_ms = int((latest_unlock - (4 * 24 * 60 * 60)) * 1000)
            result.update({
                "exists": lp_balance > 0 or lp_value > 0,
                "lp_balance": lp_balance,
                "lp_value_usdt0": lp_value,
                "last_mint_ts_ms": last_mint_ts_ms,
                "nlp_product_id": nlp_pid,
                # Oracle NAV per NLP token — lets the UI estimate mint/burn
                # proceeds even when the user holds zero NLP.
                "nav_usdt0": float(oracle or 0.0),
            })
        except Exception as e:
            logger.debug("get_nlp_position failed user=%s err=%s", _mask_address(self.address or ""), e)
        return result

    def get_nlp_pool_info(self) -> dict:
        """Return aggregate NLP pool stats (raw gateway payload)."""
        try:
            payload = self._query_rest("nlp_pool_info") or {}
            data = payload.get("data") or payload
            return {"raw": data}
        except Exception as e:
            logger.debug("nlp_pool_info REST failed err=%s", e)
            return {"raw": {}}

    def get_nlp_pool_stats(self) -> dict:
        """Parse pool-level TVL hint from nlp_pool_info (APR filled by metrics service)."""
        raw = self.get_nlp_pool_info().get("raw") or {}
        tvl_usdt0 = 0.0
        try:
            pools = raw.get("nlp_pools") or []
            for pool in pools:
                if not isinstance(pool, dict):
                    continue
                info = pool.get("subaccount_info") or {}
                healths = info.get("healths") or []
                if healths and isinstance(healths[0], dict):
                    assets = self._x18_to_float(healths[0].get("assets"))
                    if assets > tvl_usdt0:
                        tvl_usdt0 = assets
        except Exception as e:
            logger.debug("get_nlp_pool_stats parse failed err=%s", e)
        return {
            "tvl_usdt0": tvl_usdt0,
            "apr_pct": None,
            "raw": raw,
        }


# Legacy address-keyed cache. Kept for backward compatibility with
# ``clear_client_cache(address=..., network=...)`` callers in user_service.
# NO_ORDERS_AUDIT-FIX-R6b: ``get_nado_client`` now delegates to the
# digest-keyed cache above, which is rotation-safe (a new private key for the
# same address evicts the stale entry instead of returning it).
_client_cache: dict[str, NadoClient] = {}


def _cache_key_for(address: str, network: str) -> str:
    return f"{address.lower()}_{network}"


def get_nado_client(private_key: str, network: str = "testnet", main_address: str = None) -> NadoClient:
    """Public factory for signing clients. Delegates to the digest-keyed
    cache so every construction path in the codebase shares the same pool."""
    client = get_or_create_signing_client(private_key, network, main_address=main_address)
    # Keep the legacy address-keyed dict populated so existing
    # ``clear_client_cache(address=...)`` consumers still flush correctly.
    signer_address = client.address
    if signer_address:
        _client_cache[_cache_key_for(signer_address, network)] = client
    if main_address and client.main_address != main_address:
        # Subaccount can drift if the caller re-uses the same signer with a
        # different main wallet (sub-account on a different account).
        client.main_address = main_address
        client.subaccount_hex = client._compute_subaccount_hex(main_address)
    return client


def clear_client_cache(address: str = None, network: str = None):
    """Legacy clear by address/network. Also reaches into the digest cache
    by walking the user index — but since we don't have the user_id here,
    we do a best-effort scan of the digest cache for matching addresses."""
    if address and network:
        cache_key = _cache_key_for(address, network)
        cached = _client_cache.pop(cache_key, None)
        if cached is not None:
            # Best-effort: remove from the digest cache too. The digest key
            # is keyed by sha256(private_key); we don't have the pk here,
            # so we scan the small cache for instances pointing at this
            # address.
            with _NADO_CLIENT_CACHE_LOCK:
                stale = [
                    k for k, v in _NADO_CLIENT_CACHE.items()
                    if k[0] == "signer"
                    and k[2] == network
                    and getattr(v, "address", "").lower() == str(address).lower()
                ]
                for k in stale:
                    _NADO_CLIENT_CACHE.pop(k, None)
    else:
        _client_cache.clear()
        with _NADO_CLIENT_CACHE_LOCK:
            _NADO_CLIENT_CACHE.clear()
            _NADO_CLIENT_CACHE_USER_INDEX.clear()


def clear_linked_signer_cache(
    address: Optional[str] = None,
    network: Optional[str] = None,
) -> None:
    """Drop cached linked-signer state for a wallet (both layers).

    Called from user_service when a user unlinks / re-links a 1CT key, so a
    stale ``verified=True`` cannot mask the change. Pass ``address=None`` to
    drop everything (e.g. test setup).
    """
    target_addr = (address or "").strip().lower() or None
    target_net = (network or "").strip().lower() or None

    if target_addr is None:
        _linked_signer_cache.clear()
    else:
        # The cache key is (network, subaccount_hex, expected_signer). The
        # expected_signer is the lower-cased address we want to drop; we also
        # accept subaccount_hex starting with that address. Walk the keys
        # rather than reconstructing the subaccount_hex (which depends on
        # subaccount name and we can't always assume "default" here).
        addr_no0x = target_addr[2:] if target_addr.startswith("0x") else target_addr
        for key in list(_linked_signer_cache.keys()):
            net, subaccount_hex, expected = key
            if target_net and net != target_net:
                continue
            if expected == target_addr or (subaccount_hex or "").lower().startswith(addr_no0x):
                _linked_signer_cache.pop(key, None)
