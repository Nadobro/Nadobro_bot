"""
Nado Archive Indexer client.

Queries the archive API for actual fill data (prices, fees, realized PnL)
after trades are placed via the Nado SDK (which only returns a digest).

Archive docs: https://docs.nado.xyz/developer-resources/api/archive-indexer
"""
import logging
import re
import threading
import time
import requests
from decimal import Decimal, InvalidOperation
from typing import Optional

from src.nadobro.utils.env import env_float, env_int
from src.nadobro.config import NADO_TESTNET_ARCHIVE, NADO_MAINNET_ARCHIVE
from src.nadobro.core.log_redaction import redact_sensitive_text

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 8.0
_MAX_RETRIES = max(0, env_int("NADO_ARCHIVE_MAX_RETRIES", 1))
_RETRY_BASE_SECONDS = env_float("NADO_ARCHIVE_RETRY_BASE_SECONDS", 0.5)
_MAX_CONCURRENT = max(1, env_int("NADO_ARCHIVE_MAX_CONCURRENT", 3))
_MIN_INTERVAL_SECONDS = max(0.0, env_float("NADO_ARCHIVE_MIN_INTERVAL_SECONDS", 0.35))
# BUG-NAR-1 fix: 429 cooldown is now exponential, anchored on Retry-After
# when provided. _429_COOLDOWN_SECONDS is the *base*; consecutive 429s within
# a cooldown window double the wait (capped at _429_COOLDOWN_MAX_SECONDS).
_429_COOLDOWN_SECONDS = max(1.0, env_float("NADO_ARCHIVE_429_COOLDOWN_SECONDS", 12.0))
_429_COOLDOWN_MAX_SECONDS = max(
    _429_COOLDOWN_SECONDS,
    env_float("NADO_ARCHIVE_429_COOLDOWN_MAX_SECONDS", 300.0),
)
_POOL_SIZE = max(_MAX_CONCURRENT, env_int("NADO_ARCHIVE_POOL_MAXSIZE", _MAX_CONCURRENT + 2))

_rate_lock = threading.RLock()
_request_semaphore = threading.Semaphore(_MAX_CONCURRENT)
_last_request_at = 0.0
_rate_limited_until = 0.0
_last_429_log_at = 0.0
# Track consecutive 429 occurrences within a cooldown window so the next
# limit doubles the backoff (exponential decay back to base on first
# successful request).
_consecutive_429s = 0

# Reuse the shared session from nado_client for connection pooling.
_session: requests.Session | None = None


def is_archive_rate_limited() -> bool:
    with _rate_lock:
        return time.time() < _rate_limited_until


def archive_rate_limit_remaining() -> float:
    with _rate_lock:
        return max(0.0, _rate_limited_until - time.time())


def _parse_retry_after(headers: object) -> float:
    """Pull a Retry-After hint (seconds) from the venue response. Honors
    both numeric and HTTP-date forms; returns 0.0 if absent or unparseable.
    BUG-NAR-1 partial: lets the venue tell us exactly how long to wait
    instead of guessing.
    """
    if not headers:
        return 0.0
    try:
        raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    except Exception:  # noqa: BLE001
        return 0.0
    if raw is None:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    # HTTP-date form
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(str(raw))
        if dt is not None:
            return max(0.0, dt.timestamp() - time.time())
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0


def _mark_rate_limited(
    status: int, body: str, attempt: int, retry_after: float = 0.0,
) -> None:
    """BUG-NAR-1 fix: exponential backoff (12s, 24s, 48s, …) capped at
    _429_COOLDOWN_MAX_SECONDS. If the venue sent Retry-After we honor it
    when it exceeds our computed wait.
    """
    global _rate_limited_until, _last_429_log_at, _consecutive_429s
    now = time.time()
    with _rate_lock:
        # If we're still inside a cooldown when this 429 fires, it's a
        # *consecutive* limit — escalate. If we're past the prior cooldown,
        # reset to the base.
        if now < _rate_limited_until:
            _consecutive_429s += 1
        else:
            _consecutive_429s = 1
        backoff = min(
            _429_COOLDOWN_MAX_SECONDS,
            _429_COOLDOWN_SECONDS * (2 ** max(0, _consecutive_429s - 1)),
        )
        wait = max(backoff, retry_after)
        _rate_limited_until = max(_rate_limited_until, now + wait)
        should_log = (now - _last_429_log_at) >= 5.0
        if should_log:
            _last_429_log_at = now
    if should_log:
        logger.warning(
            "Archive API HTTP %s (attempt %d, consecutive %d, retry_after=%.1fs): %s "
            "— backing off %.1fs",
            status,
            attempt,
            _consecutive_429s,
            retry_after,
            redact_sensitive_text(body),
            wait,
        )


def _reset_rate_limit_streak() -> None:
    """Called after a successful response so the next 429 starts fresh
    (not at an escalated backoff)."""
    global _consecutive_429s
    if _consecutive_429s != 0:
        with _rate_lock:
            _consecutive_429s = 0


def _wait_for_request_slot() -> None:
    while True:
        with _rate_lock:
            now = time.time()
            cooldown = max(0.0, _rate_limited_until - now)
            spacing = max(0.0, _MIN_INTERVAL_SECONDS - (now - _last_request_at))
            wait = max(cooldown, spacing)
        if wait <= 0:
            return
        time.sleep(min(wait, 1.0))


def _note_request_sent() -> None:
    global _last_request_at
    with _rate_lock:
        _last_request_at = time.time()


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        # Share the hardened session (browser-like UA + Accept headers + pool
        # adapters) so the archive endpoint stops getting Cloudflare-challenged
        # on every poll. Pool sizes already chosen for high concurrency.
        from src.nadobro.core.http_session import SESSION as _shared

        _session = _shared
    return _session


def archive_url_for_network(network: str) -> str:
    return NADO_MAINNET_ARCHIVE if network == "mainnet" else NADO_TESTNET_ARCHIVE


def _from_x18(value) -> float:
    if value is None:
        return 0.0
    try:
        v = float(value) if not isinstance(value, (int, float)) else float(value)
        if abs(v) >= 1e9:
            return v / 1e18
        return v
    except (ValueError, TypeError):
        return 0.0


def _derive_archive_weight(payload: dict) -> float:
    """Documented archive IP weight for a ``_post`` payload (``{type: params}``)."""
    try:
        from src.nadobro.venue.nado_weights import query_weight
    except Exception:
        return 5.0
    if not isinstance(payload, dict) or not payload:
        return 5.0
    key = next(iter(payload))
    inner = payload.get(key) if isinstance(payload.get(key), dict) else {}
    limit = inner.get("limit")
    if isinstance(limit, dict):
        limit = limit.get("raw")
    # The archive "Orders" weight uses a /20 divisor; map to that formula.
    qt = "archive_orders" if key == "orders" else key
    return float(
        query_weight(
            qt,
            {
                "limit": limit,
                "subaccounts": inner.get("subaccounts"),
                "digests": inner.get("digests"),
            },
        )
    )


def _post(url: str, payload: dict, *, weight: float | None = None) -> dict | list | None:
    # BUG-NAR-3 partial: short-circuit *before* acquiring the semaphore so we
    # don't hold a slot during cooldown.
    if is_archive_rate_limited():
        return None
    # SCALE: also short-circuit when our Cloudflare circuit breaker is open
    # for this host. Avoids stacking up archive calls into a queue that just
    # gets 403'd back to back.
    try:
        from src.nadobro.core.http_session import is_circuit_open

        if is_circuit_open(url):
            return None
    except Exception:
        pass

    # Charge the documented archive IP weight against the shared per-host
    # weight bucket so this path and the SDK indexer paths in nado_client share
    # one 2400/min archive budget. Skip (serve None) when starved.
    try:
        from src.nadobro.core.http_session import throttle_host

        cost = _derive_archive_weight(payload) if weight is None else float(weight)
        if not throttle_host(url, cost=cost):
            return None
    except Exception:
        pass

    _request_semaphore.acquire()
    try:
        # Re-check after acquiring the slot — another thread may have
        # marked us rate-limited while we were waiting.
        if is_archive_rate_limited():
            return None
        _wait_for_request_slot()
        session = _get_session()
        for attempt in range(_MAX_RETRIES + 1):
            try:
                _note_request_sent()
                resp = session.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
                resp.raise_for_status()
                _reset_rate_limit_streak()
                return resp.json()
            except requests.HTTPError as e:
                status = getattr(e.response, "status_code", "?")
                body = ""
                retry_after = 0.0
                content_type = ""
                try:
                    body = (e.response.text or "")[:200]
                    retry_after = _parse_retry_after(getattr(e.response, "headers", None))
                    content_type = (getattr(e.response, "headers", {}) or {}).get("content-type") or ""
                except Exception:  # noqa: BLE001
                    pass
                if status == 429:
                    _mark_rate_limited(status, body, attempt + 1, retry_after=retry_after)
                    return None
                # Also honor Retry-After for 5xx; pause without escalating
                # the 429 counter.
                if isinstance(status, int) and 500 <= status < 600 and retry_after > 0:
                    with _rate_lock:
                        global _rate_limited_until
                        _rate_limited_until = max(
                            _rate_limited_until, time.time() + retry_after,
                        )
                # Cloudflare-challenge: route through the throttled logger and
                # let the breaker capture the failure so callers stop hammering.
                if status == 403 and "text/html" in content_type.lower():
                    try:
                        from src.nadobro.core.http_session import _log_cf_warning, _record_challenge, _host  # noqa: WPS437

                        _log_cf_warning(url, status, body)
                        _record_challenge(_host(url))
                    except Exception:
                        pass
                else:
                    logger.warning(
                        "Archive API HTTP %s (attempt %d): %s",
                        status,
                        attempt + 1,
                        redact_sensitive_text(body),
                    )
                if attempt >= _MAX_RETRIES:
                    return None
            except requests.RequestException as e:
                logger.warning("Archive API request failed (attempt %d): %s", attempt + 1, e)
                if attempt >= _MAX_RETRIES:
                    return None
            time.sleep(_RETRY_BASE_SECONDS * (2 ** attempt))
        return None
    finally:
        _request_semaphore.release()


def _orders_list_from_archive_response(result) -> list:
    """Normalize archive POST bodies that may return orders at top level or under data."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    o = result.get("orders")
    if isinstance(o, list):
        return o
    data = result.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("orders")
        if isinstance(inner, list):
            return inner
    return []


def _matches_list_from_archive_response(result) -> list:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    m = result.get("matches")
    if isinstance(m, list):
        return m
    data = result.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("matches")
        if isinstance(inner, list):
            return inner
    return []


def _isolated_subaccounts_list_from_response(result) -> list:
    """Normalize archive POST bodies for isolated_subaccounts queries."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    rows = result.get("isolated_subaccounts") or result.get("isolatedSubaccounts")
    if isinstance(rows, list):
        return rows
    data = result.get("data")
    if isinstance(data, dict):
        inner = data.get("isolated_subaccounts") or data.get("isolatedSubaccounts")
        if isinstance(inner, list):
            return inner
    if isinstance(data, list):
        return data
    return []


def isolated_subaccount_from_row(row: dict, parent_subaccount_hex: str = "") -> str:
    """Extract an isolated child subaccount from known archive response shapes."""
    if not isinstance(row, dict):
        return ""
    parent = (parent_subaccount_hex or "").strip().lower()
    for key in (
        "isolated_subaccount",
        "isolatedSubaccount",
        "isolated_subaccount_hex",
        "isolatedSubaccountHex",
        "child_subaccount",
        "childSubaccount",
        "subaccount_hex",
        "subaccountHex",
        "subaccount",
    ):
        value = (row.get(key) or "").strip()
        if not value:
            continue
        if parent and value.lower() == parent:
            continue
        return value
    return ""


def _bytes32_hex_to_u8_array(hex_value: str) -> list[int] | None:
    """Decode a bytes32 hex string into a 32-element ``u8`` array.

    The archive ``isolated_subaccounts`` endpoint deserializes ``subaccount``
    as ``[u8; 32]`` directly (not as a hex string and not as a list of hex
    strings). Two previous attempts confirmed this empirically via the 422
    payload:
      * ``[parent_hex]`` -> ``subaccount[0]: invalid type: string, expected u8``
      * ``parent_hex``   -> ``subaccount: invalid type: string, expected an array``
    Returns ``None`` on malformed input so the caller can skip the request.
    """
    if not hex_value:
        return None
    h = hex_value.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) > 64 or any(c not in "0123456789abcdef" for c in h):
        return None
    h = h.rjust(64, "0")
    try:
        return list(bytes.fromhex(h))
    except ValueError:
        return None


def query_isolated_subaccounts_for_parent(
    network: str,
    parent_subaccount_hex: str,
    limit: int = 100,
) -> list[dict]:
    """
    Isolated margin positions live on child subaccounts. List those linked to the
    default (parent) subaccount.

    https://docs.nado.xyz/developer-resources/api/archive-indexer/isolated-subaccounts
    """
    parent = (parent_subaccount_hex or "").strip()
    if not parent:
        return []
    subaccount_bytes = _bytes32_hex_to_u8_array(parent)
    if subaccount_bytes is None:
        logger.warning("isolated_subaccounts: invalid parent subaccount hex %s", parent[:12])
        return []
    url = archive_url_for_network(network)
    payload = {
        "isolated_subaccounts": {
            "subaccount": subaccount_bytes,
            "limit": min(max(1, int(limit)), 500),
        }
    }
    result = _post(url, payload)
    return _isolated_subaccounts_list_from_response(result) or []


def _pick(d: dict, *keys, default=0):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _parse_order(order: dict) -> dict:
    """Parse a single order from the archive orders response."""
    base_filled = _from_x18(_pick(order, "base_filled", "baseFilled", default=0))
    quote_filled = _from_x18(_pick(order, "quote_filled", "quoteFilled", default=0))
    fee = _from_x18(_pick(order, "fee", default=0))
    realized_pnl = _from_x18(_pick(order, "realized_pnl", "realizedPnl", default=0))
    closed_amount = _from_x18(_pick(order, "closed_amount", "closedAmount", default=0))
    closed_net_entry = _from_x18(_pick(order, "closed_net_entry", "closedNetEntry", default=0))

    fill_price = 0.0
    if abs(base_filled) > 0 and abs(quote_filled) > 0:
        fill_price = abs(quote_filled / base_filled)

    is_filled = abs(base_filled) > 0

    return {
        "digest": order.get("digest", ""),
        "product_id": order.get("product_id"),
        "fill_price": fill_price,
        "fill_size": abs(base_filled),
        "quote_filled": abs(quote_filled),
        "fee": abs(fee),
        "builder_fee": _from_x18(order.get("builder_fee", 0)),
        "realized_pnl": realized_pnl,
        "closed_amount": abs(closed_amount),
        "closed_net_entry": closed_net_entry,
        "first_fill_ts": order.get("first_fill_timestamp"),
        "last_fill_ts": order.get("last_fill_timestamp"),
        "is_filled": is_filled,
        "isolated": order.get("isolated", False),
        "prev_position": _from_x18(order.get("prev_position", 0)),
        "original_amount": _from_x18(order.get("amount", 0)),
        "original_price": _from_x18(order.get("price_x18", 0)),
    }


def _parse_match(match: dict) -> dict:
    """Parse a single match from the archive matches response."""
    base_filled = _from_x18(match.get("base_filled", 0))
    quote_filled = _from_x18(match.get("quote_filled", 0))
    fee = _from_x18(match.get("fee", 0))
    realized_pnl = _from_x18(match.get("realized_pnl", 0))

    fill_price = 0.0
    if abs(base_filled) > 0 and abs(quote_filled) > 0:
        fill_price = abs(quote_filled / base_filled)

    return {
        "submission_idx": match.get("submission_idx"),
        "digest": match.get("digest", ""),
        "is_taker": match.get("is_taker", False),
        "fill_price": fill_price,
        "fill_size": abs(base_filled),
        "quote_filled": abs(quote_filled),
        "fee": abs(fee),
        "sequencer_fee": _from_x18(match.get("sequencer_fee", 0)),
        "builder_fee": _from_x18(match.get("builder_fee", 0)),
        "realized_pnl": realized_pnl,
        "closed_amount": _from_x18(match.get("closed_amount", 0)),
        "closed_net_entry": _from_x18(match.get("closed_net_entry", 0)),
        "isolated": match.get("isolated", False),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def query_orders_by_digests(network: str, digests: list[str]) -> dict[str, dict]:
    """Batch lookup of order fill data keyed by digest."""
    unique: list[str] = []
    seen: set[str] = set()
    for digest in digests or []:
        value = str(digest or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    if not unique:
        return {}

    url = archive_url_for_network(network)
    payload = {"orders": {"digests": unique}}
    result = _post(url, payload)
    if not result:
        return {}

    orders = _orders_list_from_archive_response(result)
    out: dict[str, dict] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        parsed = _parse_order(order)
        digest = str(parsed.get("digest") or "").strip()
        if digest:
            out[digest] = parsed
    return out


def query_order_by_digest(
    network: str,
    digest: str,
    max_wait_seconds: float = 2.0,
    poll_interval: float = 0.5,
) -> Optional[dict]:
    """
    Query the Nado archive for fill data of a specific order by its digest.

    For IOC (market) orders that fill instantly, the archive may take a short
    time to index the fill.  This function polls up to *max_wait_seconds*.

    Returns parsed fill data dict or None if not found / not filled.
    """
    url = archive_url_for_network(network)
    payload = {"orders": {"digests": [digest]}}
    deadline = time.time() + max_wait_seconds
    attempt = 0

    while True:
        if is_archive_rate_limited():
            logger.debug(
                "Archive rate limited; stopping digest poll for %s (%.1fs remaining)",
                digest[:16],
                archive_rate_limit_remaining(),
            )
            return None

        attempt += 1
        result = _post(url, payload)
        if result:
            orders = _orders_list_from_archive_response(result)
            if isinstance(orders, list):
                for o in orders:
                    if not isinstance(o, dict):
                        continue
                    parsed = _parse_order(o)
                    if parsed["is_filled"]:
                        logger.info(
                            "Archive fill resolved for digest %s: price=%.6f size=%.6f fee=%.6f pnl=%.6f (attempt %d)",
                            digest[:16], parsed["fill_price"], parsed["fill_size"],
                            parsed["fee"], parsed["realized_pnl"], attempt,
                        )
                        return parsed

        if time.time() >= deadline:
            logger.debug("Archive fill not resolved for digest %s after %.1fs", digest[:16], max_wait_seconds)
            return None

        sleep_for = max(float(poll_interval), archive_rate_limit_remaining())
        time.sleep(sleep_for)


def query_orders_by_subaccount(
    network: str,
    subaccount_hex: str,
    product_ids: list[int] | None = None,
    limit: int = 100,
    max_time: int | None = None,
) -> list[dict]:
    """Query recent orders for a subaccount from the archive."""
    url = archive_url_for_network(network)
    params: dict = {
        "subaccounts": [subaccount_hex],
        "limit": min(limit, 500),
    }
    if product_ids:
        params["product_ids"] = product_ids
    if max_time:
        params["max_time"] = max_time

    result = _post(url, {"orders": params})
    if not result:
        return []

    orders_raw = _orders_list_from_archive_response(result)
    if not isinstance(orders_raw, list):
        return []
    return [_parse_order(o) for o in orders_raw if isinstance(o, dict)]


def query_matches_by_subaccount(
    network: str,
    subaccount_hex: str,
    product_ids: list[int] | None = None,
    limit: int = 50,
    max_time: int | None = None,
) -> list[dict]:
    """Query recent matches/fills for a subaccount from the archive."""
    url = archive_url_for_network(network)
    params: dict = {
        "subaccounts": [subaccount_hex],
        "limit": min(limit, 500),
    }
    if product_ids:
        params["product_ids"] = product_ids
    if max_time:
        params["max_time"] = max_time

    result = _post(url, {"matches": params})
    if not result:
        return []

    matches_raw = _matches_list_from_archive_response(result)
    if not isinstance(matches_raw, list):
        return []
    return [_parse_match(m) for m in matches_raw if isinstance(m, dict)]


# ---------------------------------------------------------------------------
# Market snapshots: rolling 24h volume per pair (Phase 2 / POV engine).
# ---------------------------------------------------------------------------

# (network, product_id) -> (timestamp, value)
_VOLUME_CACHE: dict[tuple[str, int], tuple[float, float]] = {}
_VOLUME_CACHE_TTL_SECONDS = 60.0


def _cumulative_volumes_from_snapshot(snapshot, product_id: int) -> Optional[float]:
    """Pull the x18 cumulative-volume entry for ``product_id`` from a snapshot.

    The archive accepts a few response shapes; ``cumulative_volumes`` may be
    keyed by the int product_id, the string form, or wrapped in a list of
    ``{"product_id": ..., "cumulative_volume_x18": ...}`` rows.
    """
    if not isinstance(snapshot, dict):
        return None
    cv = snapshot.get("cumulative_volumes") or snapshot.get("cumulativeVolumes")
    if isinstance(cv, dict):
        candidate = cv.get(product_id)
        if candidate is None:
            candidate = cv.get(str(product_id))
        if candidate is None:
            return None
        try:
            return float(candidate)
        except (TypeError, ValueError):
            return None
    if isinstance(cv, list):
        for row in cv:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("product_id") or row.get("productId") or -1)
            except (TypeError, ValueError):
                continue
            if pid != int(product_id):
                continue
            value = (
                row.get("cumulative_volume_x18")
                or row.get("cumulativeVolumeX18")
                or row.get("cumulative_volume")
                or row.get("volume_x18")
                or row.get("value")
            )
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None
    return None


def _snapshots_list_from_response(result) -> list:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    snaps = result.get("snapshots") or result.get("market_snapshots") or result.get("marketSnapshots")
    if isinstance(snaps, list):
        return snaps
    data = result.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("snapshots") or data.get("market_snapshots") or data.get("marketSnapshots")
        if isinstance(inner, list):
            return inner
    return []


def get_pair_24h_volume_usd(
    network: str,
    product_id: int,
    *,
    refresh: bool = False,
    cache_only: bool = False,
) -> Optional[float]:
    """Return the rolling 24h USD volume for ``product_id`` from Nado archive.

    Calls ``POST [ARCHIVE]/market_snapshots`` with ``interval.granularity=3600``
    and ``interval.count=24`` and returns
    ``(latest.cumulative_volumes - oldest.cumulative_volumes) / 1e18``.

    Cached per (network, product_id) for 60 seconds. Returns ``None`` when the
    archive does not respond with a parseable snapshot pair (caller should fall
    back to a sensible default rather than crash the cycle).
    """
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return None
    if pid < 0:
        return None
    network = network or "mainnet"
    key = (network, pid)
    now = time.time()
    if not refresh:
        cached = _VOLUME_CACHE.get(key)
        if cached and (now - cached[0] < _VOLUME_CACHE_TTL_SECONDS):
            return cached[1]
        if cache_only:
            return cached[1] if cached else None

    url = archive_url_for_network(network)
    payload = {
        "market_snapshots": {
            "interval": {"granularity": 3600, "count": 24},
            "product_ids": [pid],
        }
    }
    result = _post(url, payload)
    snapshots = _snapshots_list_from_response(result)
    if len(snapshots) < 2:
        return None

    # Snapshots may be returned newest-first or oldest-first; normalize by the
    # archive's ``timestamp`` field when present, otherwise trust the order.
    def _ts(s):
        try:
            return float(s.get("timestamp") or s.get("time") or 0)
        except (TypeError, ValueError):
            return 0.0

    ordered = sorted(snapshots, key=_ts)
    oldest_v = _cumulative_volumes_from_snapshot(ordered[0], pid)
    latest_v = _cumulative_volumes_from_snapshot(ordered[-1], pid)
    if oldest_v is None or latest_v is None:
        return None

    delta_x18 = max(0.0, float(latest_v) - float(oldest_v))
    volume_usd = delta_x18 / 1e18
    _VOLUME_CACHE[key] = (now, volume_usd)
    return volume_usd


def query_funding_payments(
    network: str,
    subaccount_hex: str,
    product_ids: list[int],
    limit: int = 100,
) -> list[dict]:
    """Query interest and funding payments for a subaccount."""
    url = archive_url_for_network(network)
    payload = {
        "interest_and_funding_payments": {
            "subaccount": subaccount_hex,
            "product_ids": product_ids,
            "limit": min(limit, 100),
        }
    }
    result = _post(url, payload)
    if not result or not isinstance(result, dict):
        return []

    payments = []
    for key in ("funding_payments", "interest_payments"):
        for p in result.get(key, []):
            if not isinstance(p, dict):
                continue
            payments.append({
                "type": "funding" if key == "funding_payments" else "interest",
                "product_id": p.get("product_id"),
                "timestamp": p.get("timestamp"),
                "amount": _from_x18(p.get("amount", 0)),
                "balance_amount": _from_x18(p.get("balance_amount", 0)),
                "rate": _from_x18(p.get("rate_x18", 0)),
                "oracle_price": _from_x18(p.get("oracle_price_x18", 0)),
            })
    return payments


def query_nlp_snapshots(
    network: str,
    *,
    count: int = 30,
    granularity: int = 86400,
) -> list[dict]:
    """Return daily NLP pool snapshots (TVL, cumulative PnL, etc.)."""
    url = archive_url_for_network(network)
    payload = {
        "nlp_snapshots": {
            "interval": {
                "count": max(1, min(int(count), 500)),
                "granularity": max(60, int(granularity)),
            },
        },
    }
    result = _post(url, payload)
    if not result or not isinstance(result, dict):
        return []
    snapshots = result.get("snapshots") or []
    return [s for s in snapshots if isinstance(s, dict)]


# ---------------------------------------------------------------------------
# Ink airdrop allocation.
# ---------------------------------------------------------------------------

_EVM_ADDRESS_RE = re.compile(r"(0[xX])?[0-9a-fA-F]{40}")

# (network, address) -> (timestamp, amount in whole INK). Allocations are
# snapshot-static, so a long TTL keeps repeat checks free against the shared
# archive weight budget.
_AIRDROP_CACHE: dict[tuple[str, str], tuple[float, Decimal]] = {}
_AIRDROP_CACHE_TTL_SECONDS = 3600.0

_INK_X18 = Decimal(10) ** 18


def normalize_evm_address(value) -> str:
    """Return the 0x-prefixed lowercase 20-byte address, or "" if invalid."""
    text = str(value or "").strip()
    if not _EVM_ADDRESS_RE.fullmatch(text):
        return ""
    if not text.lower().startswith("0x"):
        text = "0x" + text
    return text.lower()


def query_ink_airdrop(
    network: str,
    address: str,
    *,
    refresh: bool = False,
) -> Optional[Decimal]:
    """Ink token airdrop allocation for a wallet address, in whole INK.

    ``POST [ARCHIVE] {"ink_airdrop": {"address": ...}}`` returns
    ``{"amount": "<x18 string>"}`` (documented IP weight = 2). The venue keys
    the airdrop off the plain 20-byte wallet address, not a subaccount.

    Returns ``None`` when the archive is unreachable / rate-limited or the
    response is malformed — callers must not render that as "0 INK"; only
    ``Decimal("0")`` means the address has no allocation *recorded so far*.
    The table reflects distributions Nado has loaded to date; pending
    points-to-INK conversions (TGE) do not appear until the venue loads them,
    so an active trader can legitimately read 0 here (verified live
    2026-07-18: nonzero allocations exist only for a sparse set of wallets,
    none registered after early Feb 2026).

    https://docs.nado.xyz/developer-resources/api/archive-indexer/ink-airdrop
    """
    addr = normalize_evm_address(address)
    if not addr:
        return None
    network = network or "mainnet"
    key = (network, addr)
    now = time.time()
    if not refresh:
        cached = _AIRDROP_CACHE.get(key)
        if cached and (now - cached[0] < _AIRDROP_CACHE_TTL_SECONDS):
            return cached[1]

    url = archive_url_for_network(network)
    result = _post(url, {"ink_airdrop": {"address": addr}})
    if not isinstance(result, dict):
        return None
    raw = result.get("amount")
    if not isinstance(raw, (str, int)):
        return None
    try:
        amount = Decimal(str(raw).strip()) / _INK_X18
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    _AIRDROP_CACHE[key] = (now, amount)
    return amount


def query_nlp_lp_events(
    network: str,
    subaccount_hex: str,
    *,
    limit: int = 500,
) -> dict:
    """Return mint_nlp / burn_nlp archive events (+ txs) for a subaccount."""
    if not subaccount_hex:
        return {"events": [], "txs": []}
    url = archive_url_for_network(network)
    payload = {
        "events": {
            "subaccounts": [subaccount_hex],
            "event_types": ["mint_nlp", "burn_nlp"],
            "limit": {"raw": max(1, min(int(limit), 500))},
        },
    }
    result = _post(url, payload)
    if not result or not isinstance(result, dict):
        return {"events": [], "txs": []}
    return {
        "events": [e for e in (result.get("events") or []) if isinstance(e, dict)],
        "txs": [t for t in (result.get("txs") or []) if isinstance(t, dict)],
    }
