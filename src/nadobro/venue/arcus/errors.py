"""Typed outcomes for every Arcus call, and the single HTTP classifier.

Every client call returns exactly ONE of these values; HTTP-level results are
never raised. Exceptions are for programming errors only (``ValueError``,
``InexactUnitError``). DENIED ≠ EMPTY: a read that is not ``Ok`` is never
"no orders" / "no position" / "$0" (:func:`is_denied`).

``classify_http`` is evaluated top to bottom, first match wins (02 §4.2):

 1. 2xx write -> ``Accepted`` (``status: "ERROR"`` -> ``Rejected``, never a false ACK)
 2. 429 -> ``Throttled`` (read without ``reason`` = ``read_ip``)
 3. ``code == "GEO_RESTRICTED"`` -> ``Forbidden("geo")``
 4. ``errorType == "Transmission"`` -> write ``Transmission`` / read ``Unavailable``
 5. 503 or ``errorType == "Unavailable"`` -> ``Unavailable``
 6. 401 or ``errorType == "Unauthorized"`` -> ``Unauthorized``
 7. 403 or ``errorType == "Forbidden"`` -> ``Forbidden(whitelist|scope|unknown)``
 8. 404 -> read ``NoActivity`` (exact body) / ``NotFound``
 9. 422 -> write ``Rejected(422, rejectReason)`` / read ``Unavailable``
10. 400 -> write ``Rejected`` / read ``Unavailable("client_error")``
11. other 5xx -> write ``Ambiguous`` (may have been forwarded) / read ``Unavailable``
12. anything else -> write ``Ambiguous`` / read ``Unavailable("unexpected_status")``

Sources (docs mirror): place-order "`202 Accepted` … `status` is `ACK`",
"`Transmission` … could not deliver it to the matching engine. Retry.",
"`Internal` │ Unexpected gateway-side failure unrelated to transmission.",
"`GEO_RESTRICTED` … only state-changing actions are blocked";
rate-limits "`{"error":"rate limited"}` — no `reason` field │ Per-IP weight,
on a read endpoint", "`retryAfterMs`, the precise millisecond wait";
get-account "stable error string "this account has no activity yet"";
set-leverage "`422` Engine rejected the change (`status: REJECTED`)".
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final, Generic, Literal, Mapping, TypeVar, Union

from src.nadobro.venue.arcus.types import PoolReading

logger = logging.getLogger(__name__)

T = TypeVar("T")

ThrottleLayer = Literal["ip", "account_empty", "account_partial", "unknown", "read_ip", "local"]
ForbiddenKind = Literal["whitelist", "geo", "scope", "unknown"]
PoolName = Literal["order", "cancel"]


# --- outcomes ----------------------------------------------------------------


@dataclass(frozen=True)
class Ok(Generic[T]):
    """Successful READ."""

    value: T
    http_status: int
    weight_charged: int


@dataclass(frozen=True)
class Accepted:
    """Successful WRITE (202 = ACK only; 200 = best-effort definitive state).

    ``status`` is the venue's upper-case status (``ACK``,
    ``CANCEL_ACKNOWLEDGED``, ``OPEN``, ``FILLED``, ``CANCELED``, ``REJECTED``,
    ``APPLIED``, …). ACK is NOT open: the definitive state arrives later.
    """

    http_status: int
    order_id: str | None
    client_id: str | None
    status: str
    rejection_reason: str | None
    pool: PoolReading | None


@dataclass(frozen=True)
class Rejected:
    """400/422 structured rejection, or a 2xx body whose status is ERROR."""

    http_status: int
    error_type: str | None
    error_source: str | None
    message: str
    client_id: str | None


@dataclass(frozen=True)
class Throttled:
    """429. ``layer == "local"`` is reserved (never produced by the classifier)."""

    layer: ThrottleLayer
    retry_after_ms: int
    client_ids: tuple[str, ...]


@dataclass(frozen=True)
class Unauthorized:
    """401 — NEVER proof that the key is dead."""

    message: str


@dataclass(frozen=True)
class Forbidden:
    kind: ForbiddenKind
    message: str


@dataclass(frozen=True)
class NoActivity:
    """404 with the exact body "this account has no activity yet"."""


@dataclass(frozen=True)
class NotFound:
    """Any other 404. Absent is never "gone" by itself."""

    message: str


@dataclass(frozen=True)
class Unavailable:
    """503 / errorType Unavailable / read 5xx / transport failure / schema drift."""

    http_status: int
    message: str


@dataclass(frozen=True)
class Transmission:
    """errorType Transmission: not delivered to the engine, charge refunded."""

    message: str


@dataclass(frozen=True)
class Ambiguous:
    """A WRITE whose fate is unknown (may have been forwarded): never resend,
    reconcile by clientId."""

    client_id: str | None
    detail: str


@dataclass(frozen=True)
class LocalDenied:
    """Refused locally before anything was sent (budget, clock, GTT, …)."""

    reason: str


ReadResult = Union[
    Ok[T],
    Throttled,
    Unauthorized,
    Forbidden,
    NoActivity,
    NotFound,
    Unavailable,
    LocalDenied,
    Ambiguous,
]
WriteResult = Union[
    Accepted,
    Rejected,
    Throttled,
    Unauthorized,
    Forbidden,
    NotFound,
    Unavailable,
    Transmission,
    Ambiguous,
    LocalDenied,
]


class InexactUnitError(ValueError):
    """A price/size is not an exact multiple of its unit (or out of range).
    The message never carries the numbers."""


class ArcusSchemaError(ValueError):
    """A required response field is missing/invalid (schema drift).

    The message is the dotted field path only (``"fills.fee"``) — never a value.
    Always counted through :func:`record_schema_error` before being raised.
    """

    def __init__(self, where: str) -> None:
        super().__init__(where)
        self.where = where


def is_denied(r: object) -> bool:
    """True for every non-``Ok`` read outcome (the DENIED ≠ EMPTY helper)."""
    return not isinstance(r, Ok)


# --- schema-drift registry ----------------------------------------------------

_SCHEMA_KEYS_MAX: Final = 512
_SCHEMA_ERRORS: Counter[str] = Counter()
_SCHEMA_WARNED: set[str] = set()


def record_schema_error(where: str) -> None:
    """Count one schema drift at ``where`` (a dotted field path, never a value);
    WARNING once per ``where`` per process. Bounded key space."""
    key = where[:120] if isinstance(where, str) and where else "unknown"
    if key not in _SCHEMA_ERRORS and len(_SCHEMA_ERRORS) >= _SCHEMA_KEYS_MAX:
        key = "other"
    _SCHEMA_ERRORS[key] += 1
    if key not in _SCHEMA_WARNED:
        _SCHEMA_WARNED.add(key)
        logger.warning("arcus schema drift at %s", key)


def schema_error_counts() -> dict[str, int]:
    return dict(_SCHEMA_ERRORS)


def _reset_schema_errors_for_tests() -> None:
    _SCHEMA_ERRORS.clear()
    _SCHEMA_WARNED.clear()


# --- small helpers --------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_or_none(body: Mapping[str, object] | None, key: str) -> str | None:
    """A non-empty string field, else None (absent, null, wrong type, "")."""
    if body is None:
        return None
    value = body.get(key)
    return value if isinstance(value, str) and value else None


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Case-insensitive header lookup that works for ``httpx.Headers`` and dicts."""
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        lname = name.lower()
        for key, candidate in headers.items():
            if isinstance(key, str) and key.lower() == lname:
                value = candidate
                break
    return value if isinstance(value, str) else None


_RETRY_AFTER_DEFAULT_MS: Final = 1000
_RETRY_AFTER_MAX_MS: Final = 120_000
_RETRY_AFTER_SECONDS_RE: Final = re.compile(r"^[0-9]{1,9}$")
_retry_clamp_warned = False


def retry_after_ms_of(body: Mapping[str, object] | None, headers: Mapping[str, str] | None) -> int:
    """Milliseconds to wait after a 429.

    ``body["retryAfterMs"]`` (an int >= 0) wins; else the ``Retry-After``
    header read as whole seconds x 1000 (an HTTP-date or garbage -> 1000); else
    1000. Clamped to [0, 120000] (a clamp logs one WARNING per process).
    """
    global _retry_clamp_warned
    ms: int | None = None
    if isinstance(body, Mapping):
        raw = body.get("retryAfterMs")
        if _is_int(raw) and isinstance(raw, int) and raw >= 0:
            ms = raw
    if ms is None:
        header = _header(headers, "Retry-After")
        if header is not None:
            text = header.strip()
            ms = int(text) * 1000 if _RETRY_AFTER_SECONDS_RE.match(text) else _RETRY_AFTER_DEFAULT_MS
    if ms is None:
        ms = _RETRY_AFTER_DEFAULT_MS
    if ms > _RETRY_AFTER_MAX_MS:
        if not _retry_clamp_warned:
            _retry_clamp_warned = True
            logger.warning("arcus retry-after %d ms clamped to %d ms", ms, _RETRY_AFTER_MAX_MS)
        ms = _RETRY_AFTER_MAX_MS
    return ms


def pool_reading_of(
    obj: object, *, expect_pool: str | None, now_mono: float
) -> PoolReading | None:
    """A write body's ``rateLimit`` -> ``PoolReading`` (source ``"write"``).

    Absent/null -> None ("Omitted when rate limiting is not configured").
    Malformed -> schema count + None. A pool other than ``expect_pool`` ->
    ``record_schema_error("rateLimit.pool")`` + None. ``remaining == -1`` ->
    ``remaining=None``: AMBIGUOUS — the documented "not enforced" sentinel, but
    also ``floor(cap - consumed)`` on the first drip action — so the pool
    governor reads it as UNKNOWN, never unlimited (R2-2). Other values
    verbatim (may be <= 0 on the drip: "Can be 0 or negative while the request
    still succeeds").
    """
    if obj is None:
        return None
    if not isinstance(obj, Mapping):
        record_schema_error("rateLimit")
        return None
    pool = obj.get("pool")
    remaining = obj.get("remaining")
    if pool not in ("order", "cancel") or not _is_int(remaining) or not isinstance(remaining, int):
        record_schema_error("rateLimit")
        return None
    if expect_pool is not None and pool != expect_pool:
        record_schema_error("rateLimit.pool")
        return None
    return PoolReading(
        remaining=None if remaining == -1 else remaining,
        cap=None,
        used=None,
        next_available_ms=None,
        source="write",
        as_of_mono=now_mono,
    )


_REASON_LAYER: Final[Mapping[str, ThrottleLayer]] = {
    "ip": "ip",
    "account_empty": "account_empty",
    "account_partial": "account_partial",
    "unknown": "unknown",
}
_NO_ACTIVITY_TEXT: Final = "this account has no activity yet"
_CLIENT_ERROR_WARN_EVERY_S: Final = 60.0
_client_error_warned_at: dict[int, float] = {}


def _warn_read_client_error(status: int, etype: str | None) -> None:
    # A read 4xx is OUR bug (bad query), never "empty". Rate-limited per status.
    now = time.monotonic()
    last = _client_error_warned_at.get(status)
    if last is not None and now - last < _CLIENT_ERROR_WARN_EVERY_S:
        return
    _client_error_warned_at[status] = now
    logger.warning(
        "arcus read got HTTP %d (errorType=%s): treated as DENIED",
        status,
        (etype or "none")[:40],
    )


def _classify_429(
    body: Mapping[str, object] | None,
    headers: Mapping[str, str] | None,
    *,
    is_write: bool,
) -> Throttled:
    reason = _str_or_none(body, "reason")
    layer: ThrottleLayer
    if reason is None:
        layer = "unknown" if is_write else "read_ip"
    else:
        # "treat unknown values as opaque": an unrecognised reason is "unknown".
        layer = _REASON_LAYER.get(reason, "unknown")
    retry_ms = retry_after_ms_of(body, headers)
    client_ids: tuple[str, ...] = ()
    if is_write and body is not None:
        ids = body.get("clientIds")
        if isinstance(ids, list) and ids:
            # Positionally aligned with the submitted batch; keep "" verbatim.
            if any(not isinstance(e, str) for e in ids):
                record_schema_error("rateLimited.clientIds")
            client_ids = tuple(e if isinstance(e, str) else "" for e in ids)
        else:
            one = _str_or_none(body, "clientId")
            if one is not None:
                client_ids = (one,)
    return Throttled(layer=layer, retry_after_ms=retry_ms, client_ids=client_ids)


def _classify_write_2xx(
    status: int,
    body: Mapping[str, object] | None,
    *,
    client_id: str | None,
    now_mono: float,
    expect_pool: str | None,
) -> Accepted | Rejected:
    if body is None:
        record_schema_error("write.body")
        return Accepted(
            http_status=status,
            order_id=None,
            client_id=client_id,
            status="ACK",
            rejection_reason=None,
            pool=None,
        )
    raw_status = body.get("status")
    if isinstance(raw_status, str) and raw_status.strip():
        upper = raw_status.strip().upper()
    else:
        record_schema_error("write.status")
        upper = "ACK"  # the conservative state: accepted, not yet confirmed
    cid = _str_or_none(body, "clientId") or client_id
    if upper == "ERROR":
        # OrderResponse.error "Present when status is ERROR" — never an ACK.
        source = "Order" if expect_pool == "order" else "Cancel" if expect_pool == "cancel" else None
        message = (_str_or_none(body, "error") or "ERROR")[:200]
        return Rejected(
            http_status=status, error_type=None, error_source=source, message=message, client_id=cid
        )
    return Accepted(
        http_status=status,
        order_id=_str_or_none(body, "orderId"),
        client_id=cid,
        status=upper,
        rejection_reason=_str_or_none(body, "rejectionReason") or _str_or_none(body, "rejectReason"),
        pool=pool_reading_of(body.get("rateLimit"), expect_pool=expect_pool, now_mono=now_mono),
    )


def _forbidden_kind(body: Mapping[str, object] | None, err: str, code: str | None) -> ForbiddenKind:
    if code == "AddressNotOnWhitelist" or "whitelist" in err.lower():
        return "whitelist"
    return "scope" if body is not None else "unknown"


def classify_http(
    status: int,
    body: Mapping[str, object] | None,
    headers: Mapping[str, str] | None,
    *,
    is_write: bool,
    client_id: str | None,
    now_mono: float | None = None,
    expect_pool: PoolName | None = None,
) -> WriteResult | ReadResult[Any]:
    """Map one HTTP response to a typed outcome (table in the module docstring).

    ``body`` is the decoded JSON (``None`` when absent / not JSON / not an
    object). ``now_mono`` stamps a write's ``PoolReading.as_of_mono``;
    ``expect_pool`` is the pool a write must have charged. A 2xx READ raises
    ``ValueError`` (reads are parsed by the client, not classified).
    """
    if not _is_int(status):
        raise ValueError("status must be an int")
    b: Mapping[str, object] | None = body if isinstance(body, Mapping) else None
    err = (_str_or_none(b, "error") or "")[:200]
    etype = _str_or_none(b, "errorType")
    code = _str_or_none(b, "code")

    if 200 <= status < 300:
        if not is_write:
            raise ValueError("2xx reads are parsed by the client, not classified")
        return _classify_write_2xx(
            status,
            b,
            client_id=client_id,
            now_mono=time.monotonic() if now_mono is None else now_mono,
            expect_pool=expect_pool,
        )
    if status == 429:
        return _classify_429(b, headers, is_write=is_write)
    if code == "GEO_RESTRICTED":
        return Forbidden(kind="geo", message=err)
    if etype == "Transmission":
        return Transmission(message=err) if is_write else Unavailable(http_status=status, message="transmission")
    if status == 503 or etype == "Unavailable":
        return Unavailable(http_status=status, message=err or "unavailable")
    if status == 401 or etype == "Unauthorized":
        return Unauthorized(message=err)
    if status == 403 or etype == "Forbidden":
        return Forbidden(kind=_forbidden_kind(b, err, code), message=err)
    if status == 404:
        if not is_write and err.strip().lower() == _NO_ACTIVITY_TEXT:
            return NoActivity()
        return NotFound(message=err)
    if status == 422:
        if is_write:
            return Rejected(
                http_status=422,
                error_type=_str_or_none(b, "rejectReason") or etype,
                error_source=None,
                message=err or "REJECTED",
                client_id=client_id,
            )
        _warn_read_client_error(422, etype)
        return Unavailable(http_status=422, message="client_error")
    if status == 400:
        if is_write:
            return Rejected(
                http_status=400,
                error_type=etype,
                error_source=_str_or_none(b, "errorSource"),
                message=err,
                client_id=client_id,
            )
        _warn_read_client_error(400, etype)
        return Unavailable(http_status=400, message="client_error")
    if 500 <= status < 600:
        if is_write:
            return Ambiguous(client_id=client_id, detail=f"http_{status}")
        return Unavailable(http_status=status, message=etype or "server_error")
    if is_write:
        return Ambiguous(client_id=client_id, detail=f"http_{status}")
    return Unavailable(http_status=status, message="unexpected_status")


__all__ = [
    "Ok",
    "Accepted",
    "Rejected",
    "Throttled",
    "Unauthorized",
    "Forbidden",
    "NoActivity",
    "NotFound",
    "Unavailable",
    "Transmission",
    "Ambiguous",
    "LocalDenied",
    "ReadResult",
    "WriteResult",
    "ThrottleLayer",
    "ForbiddenKind",
    "PoolName",
    "InexactUnitError",
    "ArcusSchemaError",
    "is_denied",
    "record_schema_error",
    "schema_error_counts",
    "retry_after_ms_of",
    "pool_reading_of",
    "classify_http",
]
