"""Async Arcus REST client: every endpoint the Arcus phases need, typed outcomes.

Every call returns exactly one typed outcome (``errors.py``); HTTP-level
results are never raised. ``ValueError`` / ``InexactUnitError`` are raised only
for programming errors, BEFORE anything is sent. No method retries, and no
method logs or returns request bytes, headers, signatures, keys or addresses.

Reads (all tagged Public in the docs — "No authentication header is required";
``probe:probe_log_20260927.txt`` shows keyless testnet reads): every read
spends IP weight from :class:`~budget.IpBudget` BEFORE it is sent (a local
denial sends nothing), pays the documented list add-on AFTER the response
(rows actually returned), and turns every non-2xx, transport error, non-JSON
body or schema drift into a DENIED outcome — never an empty result
(DENIED ≠ EMPTY). Account-scoped reads always send ``address`` AND
``accountIndex`` ("An unrecognised parameter name is not an error — it is
ignored, and the request silently resolves to index 0").

Writes (REST only): ``placeOrder`` / ``cancelOrder`` / ``batchCancelOrders``
(Scheme 1, signed per payload) and ``setLeverage`` (Scheme 2). A write is
refused locally while a server 429 block is active (``reason: ip`` "Reduce
non-write traffic from this IP"). Only an OPENING placement is ever refused on
clock grounds (no sync within ``ARCUS_CLOCK_MAX_AGE_S``, 02 D2); cancels,
batch cancels, reduce-only placements and setLeverage never are. A write that
may have been forwarded is ``Ambiguous`` (never resend; reconcile by clientId).

Cancels: the bot cancels ONLY through :meth:`ArcusClient.batch_cancel` (a batch
of one when single) — build_decisions "cancel by id via batchCancelOrders
(≤100)". :meth:`ArcusClient.cancel_order` exists for the owner-run probe's
signature check only. There is deliberately no cancel-all, modify, batch-place,
dead-man's-switch, API-key management, withdraw or transfer method (a lint pins
the forbidden endpoint strings).

Transport: ``httpx.AsyncClient`` with ``trust_env=False`` (signed requests never
ride an env proxy / netrc), ``follow_redirects=False`` (a redirected POST is
never re-sent), ``retries=0``, an optional IPv4 source pin
(``ARCUS_FORCE_IPV4``, for a whitelisted static egress) and NO browser-style
``Origin``/``Referer`` headers (contrast Nado's ``core/http_session.py``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Callable, Final, Literal, Mapping, NoReturn, Sequence, TypeVar
from urllib.parse import quote, urlsplit

import httpx

from src.nadobro.config import arcus_rest_url
from src.nadobro.core.feature_flags import arcus_clock_max_age_s, arcus_force_ipv4
from src.nadobro.utils.venue_scope import arcus_scope_for, parse_arcus_net
from src.nadobro.venue.arcus.budget import (
    IpBudget,
    batch_addon,
    endpoint_weight,
    l2_weight,
    list_addon,
)
from src.nadobro.venue.arcus.clock import GTT_MIN_AHEAD_US, ArcusClock
from src.nadobro.venue.arcus.errors import (
    Accepted,
    Ambiguous,
    ArcusSchemaError,
    Forbidden,
    LocalDenied,
    NoActivity,
    NotFound,
    Ok,
    PoolName,
    ReadResult,
    Rejected,
    Throttled,
    Transmission,
    Unauthorized,
    Unavailable,
    WriteResult,
    classify_http,
    pool_reading_of,
    record_schema_error,
)
from src.nadobro.venue.arcus.parse import (
    ApiKeyEntry,
    BboView,
    CandleRow,
    ComplianceView,
    L2BookView,
    LeverageEntry,
    PriceView,
    parse_account,
    parse_api_keys,
    parse_bbo,
    parse_candles,
    parse_compliance,
    parse_fills_payload,
    parse_funding_payload,
    parse_l2,
    parse_leverages,
    parse_markets_payload,
    parse_mids,
    parse_open_orders_payload,
    parse_order_row,
    parse_positions_payload,
    parse_prices,
    parse_rate_limit,
    parse_time,
)
from src.nadobro.venue.arcus.signing import (
    ArcusAuth,
    canonical_json,
    cancel_payload,
    legacy_message,
    place_payload,
    to_quantums,
    to_ticks,
    wire_decimal,
)
from src.nadobro.venue.arcus.types import (
    ARCUS_MAX_BATCH,
    ARCUS_MIN_EPOCH_US,
    ARCUS_PAGE_MAX,
    INT64_MAX,
    ORDER_ID_RE,
    TICKER_RE,
    AccountRow,
    ArcusAccountRef,
    CancelSpec,
    FillRow,
    FundingRow,
    Lane,
    OrderRow,
    OrderSpec,
    PoolReading,
    PositionRow,
    normalize_address,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
# NO Origin / Referer / Sec-Fetch-* / browser User-Agent.
_BASE_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {"Accept": "application/json", "User-Agent": "nadobro-arcus/1"}
)
# Transport errors that PROVE the request never left (02 D3): certain not-sent.
_NOT_SENT_EXC: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
# get-open-orders status: "On this endpoint only two of the values can ever match".
_OPEN_ORDER_STATUSES: Final = frozenset({"OPEN", "UNTRIGGERED"})
# get-ohlcv-candles timeframe enum.
_CANDLE_TIMEFRAMES: Final = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w"}
)
_CANDLES_COUNTBACK_MAX: Final = 1500
_L2_MAX_LEVELS: Final = 100
_WRITE_KEYS: Final = frozenset({"placeOrder", "cancelOrder", "batchCancelOrders", "setLeverage"})
_RAW_PATH_RE: Final = re.compile(r"^/(?:health|v1/[A-Za-z0-9/_.-]+)?$")
# Documented list containers (02 §7.1) that carry a post-flight row add-on.
_LIST_CONTAINER: Final[Mapping[str, str]] = MappingProxyType(
    {
        "markets": "markets",
        "openOrders": "orders",
        "fills": "fills",
        "funding": "fundingPayments",
        "apiKeys": "apiKeys",
        "candles": "candles",
    }
)
_KNOWN_429_REASONS: Final = frozenset({"ip", "account_empty", "account_partial", "unknown"})
_IP_BLOCKING_LAYERS: Final = frozenset({"read_ip", "ip", "unknown"})
_LOG_429_EVERY_S: Final = 10.0
_LEVERAGE_WIRE_MAX: Final = 1000  # SetLeverageRequest.leverage "maximum: 1000"
_LOCAL_HOSTS: Final = frozenset({"127.0.0.1", "localhost"})
_LOG_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def build_transport(*, force_ipv4: bool) -> httpx.AsyncHTTPTransport:
    """The Arcus transport: IPv4 source pin when asked (``local_address="0.0.0.0"``
    binds an IPv4 source address, so connections use IPv4 only), no retries,
    HTTP/1.1, bounded pool with keep-alive reuse (one DNS lookup per connection)."""
    return httpx.AsyncHTTPTransport(
        local_address="0.0.0.0" if force_ipv4 else None,
        retries=0,
        http2=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0),
    )


def _check_base_url(url: str) -> str:
    """An explicit ``base_url`` gets the same rule as ``config.arcus_rest_url``:
    https, or http only for a loopback fake; no credentials/query/fragment."""
    if not isinstance(url, str):
        raise ValueError("ARCUS REST URL must be https")
    url = url.rstrip("/")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port
    except ValueError:
        raise ValueError("ARCUS REST URL must be https") from None
    ok = (
        bool(host)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
        and (
            parts.scheme.lower() == "https"
            or (parts.scheme.lower() == "http" and (host or "").lower() in _LOCAL_HOSTS)
        )
    )
    if not ok:
        raise ValueError("ARCUS REST URL must be https")
    return url


def _reject_json_constant(name: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


def _decode_json(content: bytes) -> object | None:
    """JSON with decimals as ``Decimal`` (never float); None when absent or not JSON."""
    if not content:
        return None
    try:
        decoded: object = json.loads(content, parse_float=Decimal, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError):
        return None
    return decoded


def _raw_items(body: object, container: str | None, count_map: bool) -> int:
    """Rows the server returned (what it charges the add-on on)."""
    if count_map:
        return len(body) if isinstance(body, Mapping) else 0
    if container is None or not isinstance(body, Mapping):
        return 0
    rows = body.get(container)
    return len(rows) if isinstance(rows, list) else 0


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _check_lane(lane: object) -> Lane:
    if not isinstance(lane, Lane):
        raise ValueError("lane must be a Lane")
    return lane


def _market_param(market: object) -> str | None:
    """Optional ``market`` filter: a ticker (``BTC-USD``) or a numeric id (``"1"``);
    both are documented ("Accepts either the display name … or the numeric market id")."""
    if market is None:
        return None
    if not isinstance(market, str) or TICKER_RE.match(market) is None:
        raise ValueError("invalid market filter")
    return market


def _ticker_path(ticker: object) -> str:
    if not isinstance(ticker, str) or TICKER_RE.match(ticker) is None:
        raise ValueError("invalid ticker")
    return quote(ticker, safe="")


def _epoch_us_param(value: object, name: str) -> str | None:
    """Optional epoch-MICROsecond bound (">= 1e14": "Second- and millisecond-scale
    values are rejected with a 400")."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not ARCUS_MIN_EPOCH_US <= value <= INT64_MAX:
        raise ValueError(f"{name} must be epoch microseconds")
    return str(value)


def _limit_param(limit: object) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ARCUS_PAGE_MAX:
        raise ValueError("limit must be an int in [1, 1000]")
    return limit


def _ref_params(ref: ArcusAccountRef) -> dict[str, str]:
    if not isinstance(ref, ArcusAccountRef):
        raise ValueError("an ArcusAccountRef is required")
    return {"address": ref.address, "accountIndex": str(ref.account_index)}


def _as_read(out: object) -> ReadResult[Any]:
    """Narrow ``classify_http`` for a read (it never yields write-only types;
    defensively DENIED if it ever did)."""
    if isinstance(
        out, (Ok, Throttled, Unauthorized, Forbidden, NoActivity, NotFound, Unavailable, LocalDenied, Ambiguous)
    ):
        return out
    return Unavailable(http_status=0, message="unexpected_outcome")


def _as_write(out: object, client_id: str | None) -> WriteResult:
    """Narrow ``classify_http`` for a write (defensively ``Ambiguous`` if it
    ever yielded a read-only type)."""
    if isinstance(
        out,
        (Accepted, Rejected, Throttled, Unauthorized, Forbidden, NotFound, Unavailable, Transmission, Ambiguous, LocalDenied),
    ):
        return out
    return Ambiguous(client_id=client_id, detail="unexpected_outcome")


def _token(value: str | None) -> str:
    """A server enum value (status / reason / errorType) for a log line; anything
    that is not a plain token is replaced (free text could carry an address)."""
    if value is None:
        return "-"
    return value if _LOG_TOKEN_RE.match(value) else "?"


def _describe(out: object) -> str:
    """Outcome summary for logs: class + OUR vocabulary / server enum tokens only
    (never server free text, which could carry an address)."""
    name = type(out).__name__
    if isinstance(out, Accepted):
        return f"{name} {_token(out.status)}" + (f" {_token(out.rejection_reason)}" if out.rejection_reason else "")
    if isinstance(out, Rejected):
        return f"{name} {_token(out.error_type)}"
    if isinstance(out, Throttled):
        return f"{name} {out.layer} {out.retry_after_ms}ms"
    if isinstance(out, Forbidden):
        return f"{name} {out.kind}"
    if isinstance(out, Unavailable):
        detail = out.message if out.message.startswith("not_sent:") else ""
        return f"{name} {out.http_status} {detail}".rstrip()
    if isinstance(out, Ambiguous):
        return f"{name} {out.detail}"
    if isinstance(out, LocalDenied):
        return f"{name} {out.reason}"
    return name


def backward_page_cursor(
    created_us_desc: Sequence[int], *, limit: int, prev_to_us: int | None
) -> int | None | Literal["stuck"]:
    """Next ``to`` for a newest-first backfill (fills / funding / openOrders).

    A page shorter than ``limit`` is the last one (None). Otherwise the next
    ``to`` is the page's OLDEST timestamp ("page backward by sending the oldest
    ``createdAt`` you received as the next request's ``to``"); the bound is
    inclusive ("page boundaries overlap by design — deduplicate by id"). A full
    page that does not move the cursor back is ``"stuck"`` (e.g. a full page
    inside one microsecond) — the caller reports DENIED, never skips rows.
    """
    if not _is_int(limit) or limit < 1:
        raise ValueError("limit must be an int >= 1")
    if len(created_us_desc) < limit:
        return None
    nxt = min(created_us_desc)
    if prev_to_us is not None and nxt >= prev_to_us:
        return "stuck"
    return nxt


@dataclass(frozen=True)
class BatchCancelResult:
    """``rows`` is aligned to the request (02 D8); ``None`` = the batch was
    accepted but no response row echoed that element (cancel sent, pending).
    ``rows == ()`` unless ``outcome`` is ``Accepted``."""

    outcome: WriteResult
    rows: tuple[Accepted | Rejected | None, ...]


@dataclass(frozen=True)
class _Response:
    status: int
    body: Mapping[str, object] | None
    headers: httpx.Headers


class _Candidate:
    __slots__ = ("order_id", "client_id", "market_id", "row", "used")

    def __init__(
        self, order_id: str | None, client_id: str | None, market_id: int | None, row: Mapping[str, object]
    ) -> None:
        self.order_id = order_id
        self.client_id = client_id
        self.market_id = market_id
        self.row = row
        self.used = False


def _echo_ok(body: Mapping[str, object], ref: ArcusAccountRef) -> bool:
    """A write/row echo of ``address``/``accountIndex`` (when present) is ours."""
    address = body.get("address")
    if address is not None:
        try:
            if normalize_address(address) != ref.address:
                return False
        except ValueError:
            return False
    index = body.get("accountIndex")
    if index is not None and (not _is_int(index) or index != ref.account_index):
        return False
    return True


class ArcusClient:
    """One per network (the hub owns them). Construct on the runtime loop."""

    def __init__(
        self,
        network: str,
        *,
        clock: ArcusClock,
        ip_budget: IpBudget,
        http: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        force_ipv4: bool | None = None,
        clock_max_age_s: Callable[[], float] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._network = parse_arcus_net(network)
        scope = arcus_scope_for(self._network)
        if not isinstance(clock, ArcusClock) or arcus_scope_for(clock.network) != scope:
            raise ValueError("clock must be an ArcusClock for the same network")
        if not isinstance(ip_budget, IpBudget) or arcus_scope_for(ip_budget.network) != scope:
            raise ValueError("ip_budget must be an IpBudget for the same network")
        self._clock = clock
        self._budget = ip_budget
        self._base = _check_base_url(base_url) if base_url is not None else arcus_rest_url(self._network)
        self._clock_max_age_s = clock_max_age_s or arcus_clock_max_age_s
        self._mono = monotonic
        self._owns_http = http is None
        if http is None:
            pin = arcus_force_ipv4() if force_ipv4 is None else bool(force_ipv4)
            http = httpx.AsyncClient(
                transport=build_transport(force_ipv4=pin),
                timeout=_TIMEOUT,
                headers=dict(_BASE_HEADERS),
                trust_env=False,
                follow_redirects=False,
            )
        self._http = http
        self._counts: Counter[str] = Counter()
        self._last_status: dict[str, int | str] = {}
        self._last_429_log: dict[tuple[str, str], float] = {}

    # --- properties / lifecycle -------------------------------------------------------
    @property
    def network(self) -> str:
        return self._network

    @property
    def clock(self) -> ArcusClock:
        return self._clock

    @property
    def ip_budget(self) -> IpBudget:
        return self._budget

    async def aclose(self) -> None:
        """Close the HTTP client only if this object created it."""
        if self._owns_http:
            await self._http.aclose()

    def stats(self) -> dict[str, object]:
        return {
            "network": self._network,
            "counts": dict(self._counts),
            "last_status": dict(self._last_status),
        }

    # --- shared plumbing ------------------------------------------------------------
    def _note(self, path_key: str, out: object, status: int | str) -> None:
        self._counts[f"{path_key}:{type(out).__name__}"] += 1
        self._last_status[path_key] = status

    def _on_throttled(self, path_key: str, out: Throttled, body: Mapping[str, object] | None) -> None:
        """Every SERVER 429: block the IP budget when the IP layer rejected us,
        and log ONE fixed greppable line (rate-limited per (path_key, layer))."""
        if out.layer in _IP_BLOCKING_LAYERS:
            self._budget.note_server_429(out.retry_after_ms)
        raw = body.get("reason") if body is not None else None
        if raw is None:
            reason = "none"
        elif isinstance(raw, str) and raw in _KNOWN_429_REASONS:
            reason = raw
        else:
            reason = "unknown"  # opaque values never reach the log verbatim
        key = (path_key, out.layer)
        now = self._mono()
        last = self._last_429_log.get(key)
        if last is not None and now - last < _LOG_429_EVERY_S:
            return
        self._last_429_log[key] = now
        logger.warning(
            "arcus_http_429 net=%s path=%s reason=%s retry_after_ms=%d",
            self._network,
            path_key,
            reason,
            out.retry_after_ms,
        )

    async def _get(
        self,
        path_key: str,
        path: str,
        params: Mapping[str, str] | None,
        *,
        lane: Lane,
        max_wait_s: float | None,
        parse: Callable[[object], T],
        weight: int | None = None,
        container: str | None = None,
        count_map: bool = False,
    ) -> ReadResult[T]:
        lane = _check_lane(lane)
        w = endpoint_weight(path_key) if weight is None else weight
        started = self._mono()
        if not await self._budget.acquire(w, lane, max_wait_s=max_wait_s):
            out: ReadResult[T] = LocalDenied(reason=f"ip_budget:{lane.name}")
            self._note(path_key, out, "local")
            logger.debug("arcus %s GET %s -> local-denied w=%d lane=%s", self._network, path_key, w, lane.name)
            return out
        try:
            resp = await self._http.get(
                self._base + path,
                params=params,
                headers=dict(_BASE_HEADERS),
                timeout=_TIMEOUT,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            name = type(exc).__name__
            out = Unavailable(http_status=0, message=name)
            self._note(path_key, out, name)
            logger.debug(
                "arcus %s GET %s -> %s w=%d lane=%s %.0fms",
                self._network, path_key, name, w, lane.name, (self._mono() - started) * 1000,
            )
            return out
        status = resp.status_code
        raw = _decode_json(resp.content)
        logger.debug(
            "arcus %s GET %s -> %s w=%d lane=%s %.0fms",
            self._network, path_key, status, w, lane.name, (self._mono() - started) * 1000,
        )
        if 200 <= status < 300:
            # The venue charges the add-on on the rows it returned, whatever we
            # make of them, so pay it before parsing.
            addon = list_addon(path_key, _raw_items(raw, container, count_map))
            self._budget.charge_after(addon)
            if raw is None:
                record_schema_error(f"{path_key}.body")
                out = Unavailable(http_status=status, message="schema")
            else:
                try:
                    out = Ok(value=parse(raw), http_status=status, weight_charged=w + addon)
                except ArcusSchemaError:
                    out = Unavailable(http_status=status, message="schema")
            self._note(path_key, out, status)
            return out
        body = raw if isinstance(raw, Mapping) else None
        out = _as_read(classify_http(status, body, resp.headers, is_write=False, client_id=None))
        if isinstance(out, Throttled):
            self._on_throttled(path_key, out, body)
        self._note(path_key, out, status)
        return out

    def _check_auth(self, auth: object) -> ArcusAuth:
        if not isinstance(auth, ArcusAuth):
            raise ValueError("an ArcusAuth is required")
        if auth.ref.scope != arcus_scope_for(self._network):
            raise ValueError("auth is for another network")
        return auth

    async def _post(
        self,
        path: str,
        *,
        auth: ArcusAuth,
        content: bytes,
        ct: int,
        signature: str,
        client_id: str | None,
    ) -> _Response | WriteResult:
        headers = {
            **_BASE_HEADERS,
            "Content-Type": "application/json",
            "X-API-Key": auth.api_key_hex,
            "X-Timestamp": str(ct),
            "X-Signature": signature,
        }
        try:
            resp = await self._http.post(
                self._base + path,
                params={"address": auth.ref.address},
                content=content,
                headers=headers,
                timeout=_TIMEOUT,
                follow_redirects=False,
            )
        except _NOT_SENT_EXC as exc:
            return Unavailable(http_status=0, message=f"not_sent:{type(exc).__name__}")
        except httpx.HTTPError as exc:
            # Sent (or maybe sent) and the fate is unknown: never resend.
            return Ambiguous(client_id=client_id, detail=type(exc).__name__)
        raw = _decode_json(resp.content)
        return _Response(
            status=resp.status_code,
            body=raw if isinstance(raw, Mapping) else None,
            headers=resp.headers,
        )

    def _after_write(
        self,
        op: str,
        out: WriteResult,
        *,
        body: Mapping[str, object] | None,
        status: int | None,
        market_id: int | str | None,
        client_id: str | None,
    ) -> None:
        if isinstance(out, Throttled):
            self._on_throttled(op, out, body)
        elif isinstance(out, Unauthorized):
            # Next opening re-syncs first (contract §4.4 "Re-sync … after any Unauthorized").
            self._clock.invalidate()
        self._note(op, out, status if status is not None else type(out).__name__)
        logger.info(
            "arcus %s %s m=%s cid=%s -> %s %s",
            self._network,
            op,
            "-" if market_id is None else market_id,
            client_id or "-",
            "-" if status is None else status,
            _describe(out),
        )

    def _local_write(self, op: str, reason: str, *, market_id: int | str | None, client_id: str | None) -> LocalDenied:
        out = LocalDenied(reason=reason)
        self._after_write(op, out, body=None, status=None, market_id=market_id, client_id=client_id)
        return out

    def _check_write_echo(
        self,
        out: WriteResult,
        body: Mapping[str, object] | None,
        ref: ArcusAccountRef,
        *,
        market_id: int,
        expect: Mapping[str, str],
        client_id: str | None,
    ) -> WriteResult:
        """A 2xx write whose echo names another account / market / order is
        never trusted as an ACK (or a rejection of OUR request)."""
        if body is None or not isinstance(out, (Accepted, Rejected)):
            return out
        if not _echo_ok(body, ref):
            record_schema_error("write.account")
            return Ambiguous(client_id=client_id, detail="echo_mismatch")
        echoed_market = body.get("marketId")
        if echoed_market is not None and (not _is_int(echoed_market) or echoed_market != market_id):
            record_schema_error("write.marketId")
            return Ambiguous(client_id=client_id, detail="echo_mismatch")
        for key, expected in expect.items():
            echoed = body.get(key)
            if echoed is None or echoed == "":
                continue
            if echoed != expected:
                record_schema_error(f"write.{key}")
                return Ambiguous(client_id=client_id, detail="echo_mismatch")
        return out

    async def _single_write(
        self,
        op: str,
        path: str,
        *,
        auth: ArcusAuth,
        body: Mapping[str, object],
        ct: int,
        signature: str,
        market_id: int,
        client_id: str | None,
        expect_pool: PoolName | None,
        expect: Mapping[str, str],
    ) -> WriteResult:
        res = await self._post(
            path, auth=auth, content=canonical_json(body), ct=ct, signature=signature, client_id=client_id
        )
        if isinstance(res, _Response):
            out = _as_write(
                classify_http(
                    res.status,
                    res.body,
                    res.headers,
                    is_write=True,
                    client_id=client_id,
                    now_mono=self._mono(),
                    expect_pool=expect_pool,
                ),
                client_id,
            )
            if 200 <= res.status < 300:
                out = self._check_write_echo(
                    out, res.body, auth.ref, market_id=market_id, expect=expect, client_id=client_id
                )
            self._after_write(op, out, body=res.body, status=res.status, market_id=market_id, client_id=client_id)
            return out
        self._after_write(op, res, body=None, status=None, market_id=market_id, client_id=client_id)
        return res

    # --- public reads -------------------------------------------------------------------
    async def get_time(self, *, lane: Lane, max_wait_s: float | None = None) -> ReadResult[int]:
        """``GET /v1/time`` -> server time in NANOseconds."""
        return await self._get("time", "/v1/time", None, lane=lane, max_wait_s=max_wait_s, parse=parse_time)

    async def get_markets(
        self, *, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[list[Mapping[str, object]]]:
        """``GET /v1/markets`` -> raw market objects (the catalog parses them)."""
        return await self._get(
            "markets", "/v1/markets", None, lane=lane, max_wait_s=max_wait_s,
            parse=parse_markets_payload, container="markets",
        )

    async def get_compliance(
        self, address: str | None, *, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[ComplianceView]:
        """``GET /v1/compliance``. ``geo`` describes the CALLER's IP (the bot's
        egress), not the user; the ``address`` section only with ``?address=``."""
        params = None if address is None else {"address": normalize_address(address)}
        return await self._get(
            "compliance", "/v1/compliance", params, lane=lane, max_wait_s=max_wait_s, parse=parse_compliance
        )

    async def get_account(
        self, ref: ArcusAccountRef, *, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[AccountRow]:
        """``GET /v1/account``; ``NoActivity`` for the exact 404 body,
        ``Forbidden("whitelist")`` for a non-whitelisted address."""
        params = _ref_params(ref)

        def parse(obj: object) -> AccountRow:
            return parse_account(obj, ref=ref, now_mono=self._mono())

        return await self._get("account", "/v1/account", params, lane=lane, max_wait_s=max_wait_s, parse=parse)

    async def get_positions(
        self, ref: ArcusAccountRef, *, market: str | None, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[dict[int, PositionRow]]:
        """``GET /v1/positions``; ``Ok({})`` ONLY for a 200 carrying ``"positions": {}``."""
        params = _ref_params(ref)
        market_param = _market_param(market)
        if market_param is not None:
            params["market"] = market_param

        def parse(obj: object) -> dict[int, PositionRow]:
            return parse_positions_payload(obj, ref=ref)

        return await self._get("positions", "/v1/positions", params, lane=lane, max_wait_s=max_wait_s, parse=parse)

    async def get_open_orders(
        self,
        ref: ArcusAccountRef,
        *,
        market: str | None,
        status: Sequence[str] = ("OPEN",),
        limit: int = 1000,
        max_pages: int = 5,
        lane: Lane,
        max_wait_s: float | None = None,
    ) -> ReadResult[list[OrderRow]]:
        """``GET /v1/openOrders``, paged backward (newest-first by ``createdAt``;
        next ``to`` = the page's oldest ``createdAt``; rows deduplicated by
        ``orderId``). Any denied page -> that denial (never a partial list);
        still full after ``max_pages`` (or a page that cannot move the cursor)
        -> ``Unavailable(200, "truncated")``."""
        params = _ref_params(ref)
        if isinstance(status, str) or not isinstance(status, Sequence) or not status:
            raise ValueError("status must be a non-empty sequence")
        statuses: list[str] = []
        for value in status:
            if not isinstance(value, str) or value not in _OPEN_ORDER_STATUSES:
                raise ValueError("open-order status must be OPEN or UNTRIGGERED")
            if value not in statuses:
                statuses.append(value)
        limit = _limit_param(limit)
        if not _is_int(max_pages) or max_pages < 1:
            raise ValueError("max_pages must be an int >= 1")
        market_param = _market_param(market)
        if market_param is not None:
            params["market"] = market_param
        params["status"] = ",".join(statuses)
        params["limit"] = str(limit)
        lane = _check_lane(lane)

        rows: dict[str, OrderRow] = {}
        weight = 0
        http_status = 200
        prev_to: int | None = None
        for _page in range(max_pages):
            page_params = dict(params)
            if prev_to is not None:
                page_params["to"] = str(prev_to)
            page = await self._get(
                "openOrders", "/v1/openOrders", page_params, lane=lane, max_wait_s=max_wait_s,
                parse=parse_open_orders_payload, container="orders",
            )
            if not isinstance(page, Ok):
                return page
            weight += page.weight_charged
            http_status = page.http_status
            for row in page.value:
                rows[row.order_id] = row  # a later page's copy is the fresher read
            if len(page.value) < limit:
                return Ok(value=list(rows.values()), http_status=http_status, weight_charged=weight)
            created = [row.created_us for row in page.value]
            if any(c is None for c in created):
                record_schema_error("openOrders.createdAt")
                return Unavailable(http_status=http_status, message="schema")
            cursor = backward_page_cursor([c for c in created if c is not None], limit=limit, prev_to_us=prev_to)
            if cursor is None:  # pragma: no cover - a full page never yields None
                return Ok(value=list(rows.values()), http_status=http_status, weight_charged=weight)
            if cursor == "stuck":
                logger.warning("arcus %s openOrders paging cannot advance (full page in one microsecond)", self._network)
                return Unavailable(http_status=http_status, message="truncated")
            prev_to = cursor
        logger.warning("arcus %s openOrders still full after %d pages", self._network, max_pages)
        return Unavailable(http_status=http_status, message="truncated")

    async def get_order(
        self, ref: ArcusAccountRef, order_id: str, *, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[OrderRow]:
        """``GET /v1/order/{orderId}``. ``NotFound`` is ABSENT, never "gone" by
        itself. The returned ``orderId`` must be the one asked for."""
        params = _ref_params(ref)
        if not isinstance(order_id, str) or ORDER_ID_RE.match(order_id) is None:
            raise ValueError("invalid order id")

        def parse(obj: object) -> OrderRow:
            row = parse_order_row(obj)
            if row.order_id != order_id:
                record_schema_error("order.orderId")
                raise ArcusSchemaError("order.orderId")
            return row

        return await self._get(
            "order", f"/v1/order/{quote(order_id, safe='')}", params, lane=lane, max_wait_s=max_wait_s, parse=parse
        )

    async def get_fills(
        self,
        ref: ArcusAccountRef,
        *,
        market: str | None,
        from_us: int | None,
        to_us: int | None,
        limit: int = 1000,
        lane: Lane,
        max_wait_s: float | None = None,
    ) -> ReadResult[list[FillRow]]:
        """``GET /v1/fills`` — ONE page, newest-first by ``(created_us, trade_id)``;
        page with :func:`backward_page_cursor` and dedupe by ``tradeId``."""
        params = self._window_params(ref, market=market, from_us=from_us, to_us=to_us, limit=limit)

        def parse(obj: object) -> list[FillRow]:
            return parse_fills_payload(obj, ref=ref)

        return await self._get(
            "fills", "/v1/fills", params, lane=lane, max_wait_s=max_wait_s, parse=parse, container="fills"
        )

    async def get_funding(
        self,
        ref: ArcusAccountRef,
        *,
        from_us: int | None,
        to_us: int | None,
        market: str | None = None,
        limit: int = 1000,
        lane: Lane,
        max_wait_s: float | None = None,
    ) -> ReadResult[list[FundingRow]]:
        """``GET /v1/funding`` (container ``fundingPayments``), newest-first. Bounds
        are epoch MICROseconds; pass ``from_us`` explicitly (the server default
        is 30 days back)."""
        params = self._window_params(ref, market=market, from_us=from_us, to_us=to_us, limit=limit)
        return await self._get(
            "funding", "/v1/funding", params, lane=lane, max_wait_s=max_wait_s,
            parse=parse_funding_payload, container="fundingPayments",
        )

    @staticmethod
    def _window_params(
        ref: ArcusAccountRef, *, market: str | None, from_us: int | None, to_us: int | None, limit: int
    ) -> dict[str, str]:
        params = _ref_params(ref)
        market_param = _market_param(market)
        if market_param is not None:
            params["market"] = market_param
        lo = _epoch_us_param(from_us, "from_us")
        hi = _epoch_us_param(to_us, "to_us")
        if lo is not None and hi is not None and int(lo) > int(hi):
            raise ValueError("from_us must be <= to_us")
        if lo is not None:
            params["from"] = lo
        if hi is not None:
            params["to"] = hi
        params["limit"] = str(_limit_param(limit))
        return params

    async def get_rate_limit(
        self, ref: ArcusAccountRef, *, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[tuple[PoolReading, PoolReading]]:
        """``GET /v1/rateLimit`` -> (order, cancel) readings; an echo of another
        address / index is DENIED."""
        params = _ref_params(ref)

        def parse(obj: object) -> tuple[PoolReading, PoolReading]:
            return parse_rate_limit(obj, ref=ref, now_mono=self._mono())

        return await self._get("rateLimit", "/v1/rateLimit", params, lane=lane, max_wait_s=max_wait_s, parse=parse)

    async def get_api_keys(
        self, address: str, *, account_index: int | None, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[list[ApiKeyEntry]]:
        """``GET /v1/apiKeys``. ``account_index=None`` omits the parameter ("Omit
        it to list the keys of every subaccount — … an omitted value is not the
        same as ``accountIndex=0``"). Every entry must belong to ``address``."""
        wanted = normalize_address(address)
        params = {"address": wanted}
        if account_index is not None:
            if not _is_int(account_index) or not 0 <= account_index <= 9:
                raise ValueError("invalid account index")
            params["accountIndex"] = str(account_index)

        def parse(obj: object) -> list[ApiKeyEntry]:
            entries = parse_api_keys(obj)
            if any(entry.address != wanted for entry in entries):
                record_schema_error("apiKeys.address")
                raise ArcusSchemaError("apiKeys.address")
            return entries

        return await self._get(
            "apiKeys", "/v1/apiKeys", params, lane=lane, max_wait_s=max_wait_s, parse=parse, container="apiKeys"
        )

    async def get_leverages(
        self, ref: ArcusAccountRef, *, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[list[LeverageEntry]]:
        """``GET /v1/leverages`` with echo checks."""
        params = _ref_params(ref)

        def parse(obj: object) -> list[LeverageEntry]:
            return parse_leverages(obj, ref=ref)

        return await self._get("leverages", "/v1/leverages", params, lane=lane, max_wait_s=max_wait_s, parse=parse)

    async def get_bbo(self, ticker: str, *, lane: Lane, max_wait_s: float | None = None) -> ReadResult[BboView]:
        """``GET /v1/bbo/{market}`` — path form only (the query form answers 405,
        ``probe:probe_log_20260927.txt``)."""
        path = f"/v1/bbo/{_ticker_path(ticker)}"
        return await self._get("bbo", path, None, lane=lane, max_wait_s=max_wait_s, parse=parse_bbo)

    async def get_mids(self, *, lane: Lane, max_wait_s: float | None = None) -> ReadResult[dict[str, Decimal]]:
        """``GET /v1/mids``; an "" mid is omitted, never 0."""
        return await self._get("mids", "/v1/mids", None, lane=lane, max_wait_s=max_wait_s, parse=parse_mids)

    async def get_prices(
        self, *, market: str | None = None, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[dict[str, PriceView]]:
        """``GET /v1/prices`` keyed by ticker; numeric-zero prices -> None."""
        market_param = _market_param(market)
        params = None if market_param is None else {"market": market_param}
        return await self._get(
            "prices", "/v1/prices", params, lane=lane, max_wait_s=max_wait_s, parse=parse_prices, count_map=True
        )

    async def get_l2_orderbook(
        self, ticker: str, *, n_levels: int = 20, lane: Lane, max_wait_s: float | None = None
    ) -> ReadResult[L2BookView]:
        """``GET /v1/l2OrderBook/{market}?nLevels=`` (ticker in the path; a numeric
        id answers 404). Weight ``2 + floor(n/20)`` is charged BEFORE the call."""
        path = f"/v1/l2OrderBook/{_ticker_path(ticker)}"
        if not _is_int(n_levels):
            raise ValueError("n_levels must be an int")
        n = max(1, min(_L2_MAX_LEVELS, n_levels))
        return await self._get(
            "l2OrderBook", path, {"nLevels": str(n)}, lane=lane, max_wait_s=max_wait_s,
            parse=parse_l2, weight=l2_weight(n),
        )

    async def get_candles(
        self,
        ticker: str,
        timeframe: str,
        *,
        to_us: int,
        from_us: int | None = None,
        countback: int | None = None,
        final_only: bool = True,
        lane: Lane,
        max_wait_s: float | None = None,
    ) -> ReadResult[list[CandleRow]]:
        """``GET /v1/candles`` -> bars ASCENDING by open time (the live endpoint is
        newest-first although the docs say oldest-first; the order is never
        trusted). ``final_only`` drops the still-forming bar."""
        if not isinstance(ticker, str) or TICKER_RE.match(ticker) is None:
            raise ValueError("invalid ticker")
        if not isinstance(timeframe, str) or timeframe not in _CANDLE_TIMEFRAMES:
            raise ValueError("invalid timeframe")
        if from_us is not None and countback is not None:
            raise ValueError("from_us and countback are mutually exclusive")
        if not isinstance(final_only, bool):
            raise ValueError("final_only must be a bool")
        hi = _epoch_us_param(to_us, "to_us")
        if hi is None:
            raise ValueError("to_us is required")
        params = {"market": ticker, "timeframe": timeframe, "to": hi}
        lo = _epoch_us_param(from_us, "from_us")
        if lo is not None:
            if int(lo) > int(hi):
                raise ValueError("from_us must be <= to_us")
            params["from"] = lo
        if countback is not None:
            if not _is_int(countback) or not 1 <= countback <= _CANDLES_COUNTBACK_MAX:
                raise ValueError("countback must be an int in [1, 1500]")
            params["countback"] = str(countback)

        def parse(obj: object) -> list[CandleRow]:
            return parse_candles(obj, final_only=final_only)

        return await self._get(
            "candles", "/v1/candles", params, lane=lane, max_wait_s=max_wait_s, parse=parse, container="candles"
        )

    async def get_raw(
        self,
        path_key: str,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        lane: Lane = Lane.L3_BACKGROUND,
        max_wait_s: float | None = None,
    ) -> ReadResult[object]:
        """SCRIPTS ONLY (capture / probe drift reports): a GET of ``path`` whose
        decoded JSON is returned as-is. ``path_key`` must be a READ key of the
        weight table; ``path`` must be ``/``, ``/health`` or ``/v1/…`` without
        ``..``. Weight as the typed method would pay (l2 depth pre-flight, list
        add-on post-flight)."""
        if not isinstance(path_key, str) or path_key in _WRITE_KEYS:
            raise ValueError("get_raw is for read endpoints only")
        endpoint_weight(path_key)
        if not isinstance(path, str) or ".." in path or _RAW_PATH_RE.match(path) is None:
            raise ValueError("invalid raw path")
        query: dict[str, str] = {}
        for key, value in (params or {}).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("raw params must be str -> str")
            query[key] = value
        if path_key == "l2OrderBook":
            weight = l2_weight(int(query.get("nLevels", "20")))
        else:
            weight = endpoint_weight(path_key)

        def parse(obj: object) -> object:
            return obj

        return await self._get(
            path_key, path, query or None, lane=lane, max_wait_s=max_wait_s, parse=parse, weight=weight,
            container=_LIST_CONTAINER.get(path_key), count_map=path_key == "prices",
        )

    # --- signed writes ---------------------------------------------------------------------
    async def place_order(self, auth: ArcusAuth, spec: OrderSpec) -> WriteResult:
        """``POST /v1/placeOrder`` (Scheme 1). The spec is signed EXACTLY as given
        (no re-quantization, no size change): an inexact price/size raises
        ``InexactUnitError`` before anything is sent. ``Accepted(status="ACK")``
        is NOT open — the definitive state arrives later."""
        auth = self._check_auth(auth)
        if not isinstance(spec, OrderSpec):
            raise ValueError("an OrderSpec is required")
        price_ticks = to_ticks(spec.price, spec.tick_size)
        qty_quantums = to_quantums(spec.quantity, spec.step_size)
        op, m, cid = "placeOrder", spec.market_id, spec.client_id
        if self._budget.write_blocked():
            return self._local_write(op, "ip_blocked", market_id=m, client_id=cid)
        if not spec.reduce_only and not self._clock.synced_within(self._clock_max_age_s()):
            # 02 D2: only an OPENING needs a recent offset; one inline sync attempt.
            await self._clock.sync(self, lane=Lane.L1_ENGINE, max_wait_s=1.0)
            if not self._clock.synced_within(self._clock_max_age_s()):
                return self._local_write(op, "clock_unsynced", market_id=m, client_id=cid)
            if self._budget.write_blocked():
                return self._local_write(op, "ip_blocked", market_id=m, client_id=cid)
        if spec.good_til_us < self._clock.now_us() + GTT_MIN_AHEAD_US:
            # "Must be at least one month ahead of the current system timestamp".
            return self._local_write(op, "gtt_too_near", market_id=m, client_id=cid)
        ref = auth.ref
        ct = self._clock.next_ct_ns(auth.api_key_hex)  # sign at send time: no await until the POST
        payload = place_payload(
            address=ref.address,
            account_index=ref.account_index,
            client_id=cid,
            ct_ns=ct,
            good_til_us=spec.good_til_us,
            market_id=m,
            price_ticks=price_ticks,
            qty_quantums=qty_quantums,
            reduce_only=spec.reduce_only,
            side=spec.side,
            tif=spec.tif,
        )
        body: dict[str, object] = {
            "address": ref.address,
            "accountIndex": ref.account_index,
            "marketId": m,
            "orderSide": spec.side.value,
            "orderType": spec.order_type.value,
            "quantity": wire_decimal(spec.quantity),
            "price": wire_decimal(spec.price),
            "timeInForce": spec.tif.name,
            "goodTilTime": str(spec.good_til_us),
            "reduceOnly": spec.reduce_only,
            "clientId": cid,
            "timestamp": ct,
        }
        return await self._single_write(
            op, "/v1/placeOrder", auth=auth, body=body, ct=ct, signature=auth.sign_hex(payload),
            market_id=m, client_id=cid, expect_pool="order", expect={"clientId": cid},
        )

    async def cancel_order(self, auth: ArcusAuth, spec: CancelSpec) -> WriteResult:
        """``POST /v1/cancelOrder`` — OWNER-PROBE ONLY (signature check). Bot code
        cancels through :meth:`batch_cancel`. Never refused on clock grounds."""
        auth = self._check_auth(auth)
        if not isinstance(spec, CancelSpec):
            raise ValueError("a CancelSpec is required")
        op, m, cid = "cancelOrder", spec.market_id, spec.client_id
        if self._budget.write_blocked():
            return self._local_write(op, "ip_blocked", market_id=m, client_id=cid)
        ref = auth.ref
        ct = self._clock.next_ct_ns(auth.api_key_hex)
        payload = cancel_payload(
            address=ref.address,
            account_index=ref.account_index,
            ct_ns=ct,
            market_id=m,
            order_id=spec.order_id,
            client_id=cid,
        )
        body = self._cancel_body(ref, spec, ct)
        expect = {"orderId": spec.order_id} if spec.order_id is not None else {"clientId": cid or ""}
        return await self._single_write(
            op, "/v1/cancelOrder", auth=auth, body=body, ct=ct, signature=auth.sign_hex(payload),
            market_id=m, client_id=cid, expect_pool="cancel", expect=expect,
        )

    @staticmethod
    def _cancel_body(ref: ArcusAccountRef, spec: CancelSpec, ct: int) -> dict[str, object]:
        body: dict[str, object] = {
            "address": ref.address,
            "accountIndex": ref.account_index,
            "marketId": spec.market_id,
            "timestamp": ct,
        }
        if spec.order_id is not None:
            body["kind"] = "orderId"
            body["orderId"] = spec.order_id
        else:
            body["kind"] = "clientId"
            body["clientId"] = spec.client_id
        return body

    async def batch_cancel(self, auth: ArcusAuth, specs: Sequence[CancelSpec]) -> BatchCancelResult:
        """``POST /v1/batchCancelOrders`` — THE bot cancel path (1..100 targets).

        One shared ``ct``; every element signed on its own; the ``X-Signature``
        header is element 0's signature (omitting it "rejects every element with
        ``invalid order signature``"). Response rows are matched to the request
        by their echoed ``orderId`` / ``clientId`` (never by position). A 429 is
        all-or-nothing ("A batch is rejected all-or-nothing")."""
        auth = self._check_auth(auth)
        if isinstance(specs, (str, bytes)) or not isinstance(specs, Sequence):
            raise ValueError("specs must be a sequence of CancelSpec")
        targets = tuple(specs)
        if not 1 <= len(targets) <= ARCUS_MAX_BATCH:
            raise ValueError("a batch cancel needs 1..100 targets")
        if not all(isinstance(s, CancelSpec) for s in targets):
            raise ValueError("specs must be CancelSpec")
        order_ids = [s.order_id for s in targets if s.order_id is not None]
        client_ids = [s.client_id for s in targets if s.client_id is not None]
        if len(set(order_ids)) != len(order_ids) or len(set(client_ids)) != len(client_ids):
            raise ValueError("duplicate cancel target in one batch")
        op = "batchCancelOrders"
        markets = ",".join(str(m) for m in sorted({s.market_id for s in targets}))
        if self._budget.write_blocked():
            out: WriteResult = self._local_write(op, "ip_blocked", market_id=markets, client_id=None)
            return BatchCancelResult(outcome=out, rows=())
        ref = auth.ref
        ct = self._clock.next_ct_ns(auth.api_key_hex)
        elements: list[object] = []
        signatures: list[str] = []
        for spec in targets:
            sig = auth.sign_hex(
                cancel_payload(
                    address=ref.address,
                    account_index=ref.account_index,
                    ct_ns=ct,
                    market_id=spec.market_id,
                    order_id=spec.order_id,
                    client_id=spec.client_id,
                )
            )
            element = self._cancel_body(ref, spec, ct)
            element["signature"] = sig
            elements.append(element)
            signatures.append(sig)
        res = await self._post(
            "/v1/batchCancelOrders",
            auth=auth,
            content=canonical_json({"cancels": elements}),
            ct=ct,
            signature=signatures[0],
            client_id=None,
        )
        rows: tuple[Accepted | Rejected | None, ...] = ()
        if not isinstance(res, _Response):
            out = res
        elif 200 <= res.status < 300:
            if res.body is None:
                record_schema_error("write.body")
            out = Accepted(
                http_status=res.status,
                order_id=None,
                client_id=None,
                status="ACK",
                rejection_reason=None,
                # "one snapshot for the request, not per row"
                pool=pool_reading_of(
                    None if res.body is None else res.body.get("rateLimit"),
                    expect_pool="cancel",
                    now_mono=self._mono(),
                ),
            )
            rows = self._match_batch_rows(targets, res.body, res.status, ref)
            self._budget.charge_after(batch_addon(len(targets)))
        else:
            out = _as_write(
                classify_http(
                    res.status,
                    res.body,
                    res.headers,
                    is_write=True,
                    client_id=None,
                    now_mono=self._mono(),
                    expect_pool="cancel",
                ),
                None,
            )
        self._after_write(
            op,
            out,
            body=res.body if isinstance(res, _Response) else None,
            status=res.status if isinstance(res, _Response) else None,
            market_id=markets,
            client_id=None,
        )
        return BatchCancelResult(outcome=out, rows=rows)

    @staticmethod
    def _match_batch_rows(
        specs: tuple[CancelSpec, ...],
        body: Mapping[str, object] | None,
        http_status: int,
        ref: ArcusAccountRef,
    ) -> tuple[Accepted | Rejected | None, ...]:
        """Rows aligned to ``specs`` by echo; a row that is not ours / not
        matched is counted, never assigned. No fabricated ACKs."""
        out: list[Accepted | Rejected | None] = [None] * len(specs)
        if body is None:
            return tuple(out)
        responses = body.get("responses")
        if not isinstance(responses, list):
            record_schema_error("batch.responses")
            return tuple(out)
        candidates: list[_Candidate] = []
        for raw in responses:
            if not isinstance(raw, Mapping):
                record_schema_error("batch.rows")
                continue
            if not _echo_ok(raw, ref):
                record_schema_error("batch.echo")
                continue
            market = raw.get("marketId")
            if market is not None and not _is_int(market):
                record_schema_error("batch.rows")
                continue
            candidates.append(
                _Candidate(
                    _nonempty_str(raw.get("orderId")),
                    _nonempty_str(raw.get("clientId")),
                    market if isinstance(market, int) else None,
                    raw,
                )
            )

        def pick(spec: CancelSpec, *, by_client: bool, allow_client_echo: bool) -> _Candidate | None:
            for cand in candidates:
                if cand.used or (cand.market_id is not None and cand.market_id != spec.market_id):
                    continue
                if by_client and cand.client_id == spec.client_id:
                    return cand
                if not by_client and cand.order_id == spec.order_id and (allow_client_echo or cand.client_id is None):
                    return cand
            return None

        def row_result(cand: _Candidate) -> Accepted | Rejected | None:
            row = cand.row
            raw_status = row.get("status")
            if not isinstance(raw_status, str) or not raw_status.strip():
                record_schema_error("batch.row.status")
                return None
            upper = raw_status.strip().upper()
            if upper == "ERROR":
                # Undocumented on CancelOrderResponse; handled defensively, never an ACK.
                return Rejected(
                    http_status=http_status,
                    error_type=None,
                    error_source="Cancel",
                    message=(_nonempty_str(row.get("error")) or "ERROR")[:200],
                    client_id=cand.client_id,
                )
            return Accepted(
                http_status=http_status,
                order_id=cand.order_id,
                client_id=cand.client_id,
                status=upper,
                rejection_reason=_nonempty_str(row.get("rejectionReason")),
                pool=None,
            )

        # clientId targets first, then orderId targets (prefer rows that echo no
        # clientId: a clientId cancel may ALSO echo its resolved orderId).
        for i, spec in enumerate(specs):
            if spec.client_id is not None:
                cand = pick(spec, by_client=True, allow_client_echo=True)
                if cand is not None:
                    cand.used = True
                    out[i] = row_result(cand)
        for i, spec in enumerate(specs):
            if spec.order_id is not None:
                cand = pick(spec, by_client=False, allow_client_echo=False) or pick(
                    spec, by_client=False, allow_client_echo=True
                )
                if cand is not None:
                    cand.used = True
                    out[i] = row_result(cand)
        for cand in candidates:
            if not cand.used:
                record_schema_error("batch.rows")
        return tuple(out)

    async def set_leverage(self, auth: ArcusAuth, market_id: int, leverage: int) -> WriteResult:
        """``POST /v1/setLeverage`` (Scheme 2, weight 125 on L2). ONLY from an
        explicit user tap — never the engine. Margin mode is never changed (no
        ``isolated``). 200 -> ``Accepted("APPLIED")``; 202 -> ``Accepted("ACK")``
        (re-read ``get_leverages``); 422 -> ``Rejected(422, rejectReason)``."""
        auth = self._check_auth(auth)
        if not _is_int(market_id) or not 0 <= market_id <= 65535:
            raise ValueError("invalid market id")
        if not _is_int(leverage) or not 1 <= leverage <= _LEVERAGE_WIRE_MAX:
            raise ValueError("leverage must be an int in [1, 1000]")
        op = "setLeverage"
        if self._budget.write_blocked():
            return self._local_write(op, "ip_blocked", market_id=market_id, client_id=None)
        lane = Lane.L2_INTERACTIVE
        if not await self._budget.acquire(endpoint_weight(op), lane, max_wait_s=3.0):
            return self._local_write(op, f"ip_budget:{lane.name}", market_id=market_id, client_id=None)
        ref = auth.ref
        ct = self._clock.next_ct_ns(auth.api_key_hex)
        body: dict[str, object] = {
            "accountIndex": ref.account_index,
            "address": ref.address,
            "leverage": leverage,
            "marketId": market_id,
        }
        signature = auth.sign_hex(legacy_message(ct, op, body))
        return await self._single_write(
            op, "/v1/setLeverage", auth=auth, body=body, ct=ct, signature=signature,
            market_id=market_id, client_id=None, expect_pool=None, expect={},
        )


__all__ = [
    "ArcusClient",
    "BatchCancelResult",
    "build_transport",
    "backward_page_cursor",
    # view types live in parse.py (02 D6), re-exported here
    "ComplianceView",
    "ApiKeyEntry",
    "LeverageEntry",
    "BboView",
    "L2BookView",
    "PriceView",
    "CandleRow",
]
