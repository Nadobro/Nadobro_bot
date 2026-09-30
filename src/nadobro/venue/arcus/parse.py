"""Tolerant, fail-closed parsers for Arcus REST (and WS-shared) payloads.

Rules for every parser (02 §7.1):

- Unknown fields are IGNORED (tolerant). A missing/invalid REQUIRED field
  raises :class:`ArcusSchemaError` (after :func:`record_schema_error`); the
  client turns that into ``Unavailable(status, "schema")`` — DENIED, never an
  empty result. An OPTIONAL field that is present (non-null) with the wrong
  type is also an error: type drift is never silently dropped.
- Numbers arrive as decimal STRINGS (the client decodes JSON with
  ``parse_float=Decimal``, so a stray JSON number is accepted too). ``float``
  never appears; NaN/Infinity are rejected.
- Echo checks: wherever the venue echoes ``address``/``accountIndex``, a
  mismatch with the requested :class:`ArcusAccountRef` is an error ("An
  unrecognised parameter name is not an error — it is ignored, and the request
  silently resolves to index 0", rate-limits). Addresses compare after
  :func:`normalize_address` (a checksum-case echo is fine).
- Zero prices are a NUMERIC test (``Decimal(v) == 0`` -> None): the live
  OFFLINE F-USD row carries ``"oraclePrice": "0.0000"``.
- List containers are named (``orders``, ``fills``, ``fundingPayments`` — not
  ``funding`` —, ``apiKeys``, ``leverages``, ``candles``, ``markets``;
  ``positions``/``mids`` are maps). A missing container or a wrong container
  type is an error; an empty container is a legitimate empty result. ``total``
  is never required and never used ("Counts the returned page").

Units: every ``*_us`` timestamp is epoch MICROseconds and must be >= 1e14
("the server requires at least `1e14`"); ``/v1/time`` is nanoseconds; API-key
``validUntil`` is epoch MILLIseconds (0 = no expiry).

View types live here (not in ``client.py``) so the WS order store can share
them without importing the HTTP client (02 D6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final, Literal, Mapping, NoReturn

from src.nadobro.venue.arcus.errors import ArcusSchemaError, record_schema_error
from src.nadobro.venue.arcus.types import (
    ARCUS_MIN_EPOCH_US,
    CLIENT_ID_RE,
    INT64_MAX,
    ORDER_ID_RE,
    AccountRow,
    ArcusAccountRef,
    FillRow,
    FundingRow,
    OrderRow,
    PoolReading,
    PositionRow,
    Side,
    Tif,
    normalize_address,
)

# --- drift tables (scripts/capture_arcus_shapes.py; pure data) ---------------------
# Documented property names, copied from the doc schemas (docs mirror 2026-09-27).
FIELD_SETS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "market": frozenset(  # get-markets MarketInfo
            {
                "addedTimestamp", "assetResolution", "baseAsset", "category",
                "currentSettlementPrice", "fullAssetName", "fundingRate", "high24h",
                "initialMarginFraction", "isLowerInExpansionZone", "isOutsideRth",
                "isUpperInExpansionZone", "low24h", "lowerExpectedExpansionAt",
                "lowerTradingBound", "lowerZoneEnteredAt", "maintenanceMarginFraction",
                "markPrice", "marketDisplayName", "marketId", "maxOrderSize",
                "minOrderNotional", "minOrderSize", "nextFundingAt", "nextFundingRate",
                "nextLowerTradingBound", "nextUpperTradingBound",
                "offHoursInitialMarginFraction", "openInterest", "openInterestCap",
                "oraclePrice", "priceChange24h", "pythId", "quoteAsset",
                "regularTradingHours", "status", "stepSize", "tickSize", "tickTiers",
                "trades24h", "type", "upperExpectedExpansionAt", "upperTradingBound",
                "upperZoneEnteredAt", "volume24h", "volume24hNotional",
            }
        ),
        "order": frozenset(  # get-open-orders / get-order-status `order`
            {
                "avgFillPrice", "cancelReason", "clientId", "createdAt", "filledSize",
                "goodTilTime", "isPositionTPSL", "marketDisplayName", "marketId", "orderId",
                "originalSize", "parentOrderId", "positionEffect", "price", "reduceOnly",
                "rejectionReason", "remainingSize", "sequenceNumber", "side", "state",
                "status", "timeInForce", "tpslType", "triggerPrice", "type", "updatedAt",
            }
        ),
        "fill": frozenset(  # get-fills `fill`
            {
                "accountIndex", "address", "clientId", "closedPnl", "createdAt", "fee",
                "liquidation", "marketDisplayName", "marketId", "orderId", "originalSize",
                "positionEffect", "price", "remainingSize", "role", "sequenceNumber", "side",
                "size", "tradeId",
            }
        ),
        "position": frozenset(  # get-positions `position`
            {
                "accountIndex", "address", "averageEntryPrice", "borrowedCapital",
                "cumulativeFunding", "leverage", "marginMode", "marginUsed", "markPx",
                "marketDisplayName", "marketId", "positionValueNotional", "sequenceNumber",
                "side", "size", "unrealizedPnl",
            }
        ),
        "account": frozenset(  # get-account `account`
            {
                "accountIndex", "address", "borrowHeadroom", "equity", "freeCollateral",
                "loanBalance", "marginState", "netDeposits", "netDepositsByAsset",
                "netQuoteBalance", "pendingDeposits", "pendingWithdrawals", "positions",
                "sequenceNumber", "spotPositions", "unsettledInterest",
            }
        ),
        "fundingPayment": frozenset(  # get-funding-payments `FundingPayment`
            {"fundingRate", "marketDisplayName", "marketId", "payment", "size", "time"}
        ),
        "apiKey": frozenset(  # get-api-keys `api-key-entry`
            {
                "accountIndex", "address", "allSubaccounts", "apiKey", "apiWalletName",
                "createdAt", "permissions", "status", "validUntil",
            }
        ),
        "rateLimit": frozenset({"accountIndex", "address", "cancel", "order"}),
        "bbo": frozenset({"bestAsk", "bestBid", "globalSequenceId", "lastSequenceId", "timestamp"}),
        "candle": frozenset(  # get-ohlcv-candles `Candle`
            {
                "close", "high", "isFinal", "low", "marketDisplayName", "marketId",
                "notionalVolume", "open", "openTime", "takerBuyNotionalVolume",
                "takerBuyVolume", "timeframe", "tradeCount", "volume",
            }
        ),
    }
)
# The fields the parsers below REQUIRE ("market" is catalog.parse_market's set).
REQUIRED_FIELDS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "market": frozenset(
            {
                "marketId", "marketDisplayName", "status", "type", "category", "baseAsset",
                "quoteAsset", "tickSize", "stepSize", "tickTiers", "minOrderNotional",
                "minOrderSize", "maxOrderSize", "initialMarginFraction",
                "maintenanceMarginFraction", "offHoursInitialMarginFraction",
            }
        ),
        "order": frozenset(
            {
                "orderId", "marketId", "marketDisplayName", "side", "status", "price",
                "originalSize", "remainingSize", "updatedAt",
            }
        ),
        "fill": frozenset(
            {
                "tradeId", "orderId", "marketId", "marketDisplayName", "side", "size",
                "price", "fee", "role", "createdAt",
            }
        ),
        "position": frozenset(
            {
                "address", "accountIndex", "marketId", "marketDisplayName", "side", "size",
                "averageEntryPrice", "marginMode",
            }
        ),
        "account": frozenset(
            {
                "accountIndex", "address", "netQuoteBalance", "equity", "freeCollateral",
                "netDeposits", "positions", "sequenceNumber",
            }
        ),
        "fundingPayment": frozenset(
            {"marketId", "marketDisplayName", "fundingRate", "size", "payment", "time"}
        ),
        "apiKey": frozenset({"apiKey", "address", "status", "validUntil", "createdAt"}),
        "rateLimit": frozenset({"address", "accountIndex", "order", "cancel"}),
        "bbo": frozenset(),
        "candle": frozenset(
            {
                "marketId", "marketDisplayName", "timeframe", "openTime", "open", "high",
                "low", "close", "volume", "notionalVolume", "tradeCount", "isFinal",
            }
        ),
    }
)

_DEC_STR_RE: Final = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_DIGITS_RE: Final = re.compile(r"^[0-9]{1,20}$")
_API_KEY_RE: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_DEC_EXP_LIMIT: Final = 100
_FILL_ROLES: Final = frozenset({"MAKER", "TAKER", "ALP"})
_POSITION_SIDES: Final = frozenset({"LONG", "SHORT"})
_MARGIN_MODES: Final = frozenset({"CROSS", "ISOLATED"})
_API_KEY_STATUSES: Final = frozenset({"ACTIVE", "PENDING_DELETE", "DELETED"})
_COMPLIANCE_STATUSES: Final = frozenset({"COMPLIANT", "BLOCKED"})
# TimeInForceResponse: "Resting orders (submitted as GTT) are currently reported
# as `GTC` for backward compatibility".
_TIF_BY_WIRE: Final[Mapping[str, Tif]] = MappingProxyType(
    {"GTC": Tif.GTT, "GTT": Tif.GTT, "IOC": Tif.IOC, "FOK": Tif.FOK, "ALO": Tif.ALO}
)
_SIDE_BY_WIRE: Final[Mapping[str, Side]] = MappingProxyType({"BUY": Side.BUY, "SELL": Side.SELL})


# --- primitive decoders ------------------------------------------------------------


def _fail(where: str) -> NoReturn:
    record_schema_error(where)
    raise ArcusSchemaError(where)


def _obj(v: object, where: str) -> Mapping[str, object]:
    if not isinstance(v, Mapping):
        _fail(where)
    return v


def _dec(
    v: object, where: str, *, gt: Decimal | int | None = None, ge: Decimal | int | None = None
) -> Decimal:
    """A finite Decimal from a non-empty decimal string, an int (not bool) or a
    Decimal. NaN/Infinity/sNaN, floats, bools and absurd exponents fail."""
    if isinstance(v, bool):
        _fail(where)
    if isinstance(v, str):
        if not _DEC_STR_RE.match(v):
            _fail(where)
        try:
            out = Decimal(v)
        except InvalidOperation:
            _fail(where)
    elif isinstance(v, int):
        out = Decimal(v)
    elif isinstance(v, Decimal):
        out = v
    else:
        _fail(where)
    if not out.is_finite() or (out != 0 and abs(out.adjusted()) > _DEC_EXP_LIMIT):
        _fail(where)
    if gt is not None and not out > gt:
        _fail(where)
    if ge is not None and not out >= ge:
        _fail(where)
    return out


def _dec_opt(
    v: object, where: str, *, zero_is_none: bool = False, ge: Decimal | int | None = None
) -> Decimal | None:
    """None / absent / "" -> None; a numeric zero -> None only if ``zero_is_none``."""
    if v is None or (isinstance(v, str) and v == ""):
        return None
    out = _dec(v, where, ge=ge)
    if zero_is_none and out == 0:
        return None
    return out


def _int(v: object, where: str, *, ge: int | None = None, le: int | None = None) -> int:
    """An int (not bool), an integral Decimal, or an all-digit string
    (``goodTilTime`` is a string)."""
    if isinstance(v, bool):
        _fail(where)
    if isinstance(v, int):
        out = v
    elif isinstance(v, Decimal):
        if not v.is_finite() or v != v.to_integral_value() or abs(v.adjusted()) > 30:
            _fail(where)
        out = int(v)
    elif isinstance(v, str) and _DIGITS_RE.match(v):
        out = int(v)
    else:
        _fail(where)
    if ge is not None and out < ge:
        _fail(where)
    if le is not None and out > le:
        _fail(where)
    return out


def _int_opt(v: object, where: str, *, ge: int | None = None, le: int | None = None) -> int | None:
    if v is None:
        return None
    return _int(v, where, ge=ge, le=le)


def _str(v: object, where: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(v, str) or not v:
        _fail(where)
    if pattern is not None and pattern.match(v) is None:
        _fail(where)
    return v


def _str_opt(v: object, where: str, *, pattern: re.Pattern[str] | None = None) -> str | None:
    """None / absent / "" -> None; otherwise a str (pattern-checked if given)."""
    if v is None or (isinstance(v, str) and v == ""):
        return None
    return _str(v, where, pattern=pattern)


def _bool(v: object, where: str) -> bool:
    if v is True or v is False:
        return bool(v)
    _fail(where)


def _enum(v: object, where: str, allowed: frozenset[str]) -> str:
    if not isinstance(v, str) or v not in allowed:
        _fail(where)
    return v


def _market_id(v: object, where: str) -> int:
    return _int(v, where, ge=0, le=65535)


def _epoch_us(v: object, where: str) -> int:
    return _int(v, where, ge=ARCUS_MIN_EPOCH_US, le=INT64_MAX)


def _side(v: object, where: str) -> Side:
    if not isinstance(v, str) or v not in _SIDE_BY_WIRE:
        _fail(where)
    return _SIDE_BY_WIRE[v]


def _check_address_echo(v: object, ref: ArcusAccountRef, where: str) -> None:
    try:
        echoed = normalize_address(v)
    except ValueError:
        _fail(where)
    if echoed != ref.address:
        _fail(where)


def _check_index_echo(v: object, ref: ArcusAccountRef, where: str) -> None:
    if _int(v, where, ge=0, le=9) != ref.account_index:
        _fail(where)


def _list_container(obj: object, key: str, where: str) -> list[object]:
    body = _obj(obj, where)
    rows = body.get(key)
    if not isinstance(rows, list):
        _fail(f"{where}.{key}")
    return rows


def tif_from_wire(v: object, where: str) -> Tif | None:
    """``timeInForce`` -> :class:`Tif`: ``GTC``/``GTT`` -> ``GTT``; ``IOC``/``FOK``/
    ``ALO`` -> enum; absent -> None; anything else -> None + a (non-fatal)
    schema count."""
    if v is None:
        return None
    if isinstance(v, str) and v in _TIF_BY_WIRE:
        return _TIF_BY_WIRE[v]
    record_schema_error(where)
    return None


# --- view types (re-exported by client.py) ------------------------------------------


@dataclass(frozen=True)
class ComplianceView:
    """``GET /v1/compliance``. ``geo`` describes the CALLER's IP (the bot's
    egress), not the user; per-user screening is ``address_status`` only."""

    country: str
    restrictions_perps: bool
    bypassed: bool
    address_status: Literal["COMPLIANT", "BLOCKED"] | None
    reason: str | None


@dataclass(frozen=True)
class ApiKeyEntry:
    api_key: str  # 64 lowercase hex (Ed25519 public key)
    address: str  # lowercase
    all_subaccounts: bool
    account_index: int | None  # None iff all_subaccounts
    api_wallet_name: str | None
    status: str  # ACTIVE | PENDING_DELETE | DELETED
    permissions: tuple[str, ...]
    valid_until_ms: int  # epoch ms; 0 = no expiry
    created_us: int

    def covers_account(self, account_index: int) -> bool:
        return self.all_subaccounts or self.account_index == account_index


@dataclass(frozen=True)
class LeverageEntry:
    market_id: int
    leverage: int
    isolated: bool
    margin_mode: str


@dataclass(frozen=True)
class BboView:
    bid: Decimal | None
    ask: Decimal | None
    bid_size: Decimal | None
    ask_size: Decimal | None
    timestamp_us: int | None


@dataclass(frozen=True)
class L2BookView:
    bids: tuple[tuple[Decimal, Decimal], ...]  # (price, size), price descending
    asks: tuple[tuple[Decimal, Decimal], ...]  # (price, size), price ascending
    last_sequence_id: int | None
    timestamp_us: int | None


@dataclass(frozen=True)
class PriceView:
    ticker: str
    market_key: int
    oracle: Decimal | None  # None when the venue sent a numeric zero
    mark: Decimal | None  # None when the venue sent a numeric zero (never fall back to oracle)
    sequencer: int


@dataclass(frozen=True)
class CandleRow:
    market_id: int
    ticker: str
    timeframe: str
    open_time_us: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    notional_volume: Decimal
    trade_count: int
    is_final: bool


# --- parsers ----------------------------------------------------------------------------


def parse_time(obj: object) -> int:
    """``TimeResponse`` -> server time in NANOseconds."""
    body = _obj(obj, "time")
    return _int(body.get("timeNs"), "time.timeNs", ge=10**18, le=INT64_MAX)


def parse_markets_payload(obj: object) -> list[Mapping[str, object]]:
    """``MarketsResponse`` -> the raw market objects (per-market parsing is the
    catalog's ``parse_market``)."""
    rows = _list_container(obj, "markets", "markets")
    out: list[Mapping[str, object]] = []
    for row in rows:
        out.append(_obj(row, "markets.row"))
    return out


def parse_order_row(obj: object, *, where: str = "order") -> OrderRow:
    """One ``order`` (openOrders row / ``GET /v1/order``)."""
    row = _obj(obj, where)
    original = _dec(row.get("originalSize"), f"{where}.originalSize", gt=0)
    remaining = _dec(row.get("remainingSize"), f"{where}.remainingSize", ge=0)
    filled = _dec_opt(row.get("filledSize"), f"{where}.filledSize", ge=0)
    if filled is not None and filled != original - remaining:
        # Derived field ("Computed as originalSize − remainingSize"): count, not fatal.
        record_schema_error(f"{where}.filledSize")
    reduce_raw = row.get("reduceOnly")
    return OrderRow(
        order_id=_str(row.get("orderId"), f"{where}.orderId", pattern=ORDER_ID_RE),
        client_id=_str_opt(row.get("clientId"), f"{where}.clientId", pattern=CLIENT_ID_RE),
        market_id=_market_id(row.get("marketId"), f"{where}.marketId"),
        ticker=_str(row.get("marketDisplayName"), f"{where}.marketDisplayName"),
        side=_side(row.get("side"), f"{where}.side"),
        status=_str(row.get("status"), f"{where}.status").upper(),
        state=_str_opt(row.get("state"), f"{where}.state"),
        price=_dec(row.get("price"), f"{where}.price", ge=0),
        original_size=original,
        remaining_size=remaining,
        filled_size=filled,
        avg_fill_price=_dec_opt(row.get("avgFillPrice"), f"{where}.avgFillPrice", zero_is_none=True, ge=0),
        tif=tif_from_wire(row.get("timeInForce"), f"{where}.timeInForce"),
        reduce_only=False if reduce_raw is None else _bool(reduce_raw, f"{where}.reduceOnly"),
        rejection_reason=_str_opt(row.get("rejectionReason"), f"{where}.rejectionReason"),
        cancel_reason=_str_opt(row.get("cancelReason"), f"{where}.cancelReason"),
        created_us=None if row.get("createdAt") is None else _epoch_us(row.get("createdAt"), f"{where}.createdAt"),
        updated_us=_epoch_us(row.get("updatedAt"), f"{where}.updatedAt"),
        sequence_number=_int_opt(row.get("sequenceNumber"), f"{where}.sequenceNumber", ge=0),
    )


def parse_open_orders_payload(obj: object) -> list[OrderRow]:
    """``GetOpenOrdersResponse`` -> rows (container ``orders``; order kept)."""
    return [parse_order_row(r, where="openOrders.order") for r in _list_container(obj, "orders", "openOrders")]


def parse_fill_row(
    obj: object, *, source: Literal["ws", "rest"], ref: ArcusAccountRef | None = None
) -> FillRow:
    """One ``fill``. ``tradeId``/``orderId`` are non-empty strings only (a
    liquidated leg's synthetic ``liq:`` order id must parse). ``clientId`` is
    "Only present on streaming updates": None on REST."""
    if source not in ("ws", "rest"):
        raise ValueError("source must be 'ws' or 'rest'")
    row = _obj(obj, "fill")
    if ref is not None:
        if row.get("address") is not None:
            _check_address_echo(row.get("address"), ref, "fill.address")
        if row.get("accountIndex") is not None:
            _check_index_echo(row.get("accountIndex"), ref, "fill.accountIndex")
    liquidation_method: str | None = None
    liquidation = row.get("liquidation")
    if liquidation is not None:
        liquidation_method = _str(_obj(liquidation, "fill.liquidation").get("method"), "fill.liquidation.method")
    client_id: str | None = None
    if source == "ws":
        client_id = _str_opt(row.get("clientId"), "fill.clientId", pattern=CLIENT_ID_RE)
    return FillRow(
        trade_id=_str(row.get("tradeId"), "fill.tradeId"),
        order_id=_str(row.get("orderId"), "fill.orderId"),
        client_id=client_id,
        market_id=_market_id(row.get("marketId"), "fill.marketId"),
        ticker=_str(row.get("marketDisplayName"), "fill.marketDisplayName"),
        side=_side(row.get("side"), "fill.side"),
        size=_dec(row.get("size"), "fill.size", gt=0),
        price=_dec(row.get("price"), "fill.price", gt=0),
        fee=_dec(row.get("fee"), "fill.fee"),
        closed_pnl=_dec_opt(row.get("closedPnl"), "fill.closedPnl"),
        role=_enum(row.get("role"), "fill.role", _FILL_ROLES),
        position_effect=_str_opt(row.get("positionEffect"), "fill.positionEffect"),
        liquidation_method=liquidation_method,
        created_us=_epoch_us(row.get("createdAt"), "fill.createdAt"),
        sequence_number=_int_opt(row.get("sequenceNumber"), "fill.sequenceNumber", ge=0),
        source=source,
    )


def parse_fills_payload(obj: object, *, ref: ArcusAccountRef) -> list[FillRow]:
    """``GetFillsResponse`` (container ``fills``) -> REST rows sorted
    newest-first by ``(created_us, trade_id)``."""
    rows = [parse_fill_row(r, source="rest", ref=ref) for r in _list_container(obj, "fills", "fills")]
    rows.sort(key=lambda f: (f.created_us, f.trade_id), reverse=True)
    return rows


def parse_funding_row(obj: object) -> FundingRow:
    """One ``FundingPayment`` ("Positive = trader received"); natural key
    ``(market_id, time_us)``."""
    row = _obj(obj, "funding")
    return FundingRow(
        market_id=_market_id(row.get("marketId"), "funding.marketId"),
        ticker=_str(row.get("marketDisplayName"), "funding.marketDisplayName"),
        funding_rate=_dec(row.get("fundingRate"), "funding.fundingRate"),
        size=_dec(row.get("size"), "funding.size"),
        payment=_dec(row.get("payment"), "funding.payment"),
        time_us=_epoch_us(row.get("time"), "funding.time"),
    )


def parse_funding_payload(obj: object) -> list[FundingRow]:
    """``GetFundingPaymentsResponse`` — the container is ``fundingPayments``
    (NOT ``funding``) -> rows newest-first by ``time_us``."""
    rows = [parse_funding_row(r) for r in _list_container(obj, "fundingPayments", "funding")]
    rows.sort(key=lambda f: (f.time_us, f.market_id), reverse=True)
    return rows


def parse_position_row(obj: object, *, ref: ArcusAccountRef) -> PositionRow | None:
    """One ``position``; None when flat (``size`` numerically 0).

    ``address``/``accountIndex`` are required echoes (the doc's ``position``
    marks both required). Sign check: LONG => size > 0, SHORT => size < 0 — a
    flatten direction must never be guessed. ``unrealizedPnl`` is a MARGIN
    delta and is kept only as ``venue_margin_delta``; ``averageEntryPrice`` is
    kept verbatim (units [U A8]).
    """
    row = _obj(obj, "position")
    _check_address_echo(row.get("address"), ref, "position.address")
    _check_index_echo(row.get("accountIndex"), ref, "position.accountIndex")
    market_id = _market_id(row.get("marketId"), "position.marketId")
    ticker = _str(row.get("marketDisplayName"), "position.marketDisplayName")
    side = _enum(row.get("side"), "position.side", _POSITION_SIDES)
    size = _dec(row.get("size"), "position.size")
    entry = _dec(row.get("averageEntryPrice"), "position.averageEntryPrice", ge=0)
    margin_mode = _enum(row.get("marginMode"), "position.marginMode", _MARGIN_MODES)
    if size == 0:
        return None
    if (side == "LONG") != (size > 0):
        _fail("position.sign")
    return PositionRow(
        market_id=market_id,
        ticker=ticker,
        size=size,
        average_entry_price=entry,
        leverage=_dec_opt(row.get("leverage"), "position.leverage"),
        margin_mode=margin_mode,
        margin_used=_dec_opt(row.get("marginUsed"), "position.marginUsed"),
        position_value_notional=_dec_opt(row.get("positionValueNotional"), "position.positionValueNotional"),
        venue_margin_delta=_dec_opt(row.get("unrealizedPnl"), "position.unrealizedPnl"),
        mark_px=_dec_opt(row.get("markPx"), "position.markPx", zero_is_none=True, ge=0),
        sequence_number=_int_opt(row.get("sequenceNumber"), "position.sequenceNumber", ge=0),
    )


def parse_positions_map(obj: object, *, ref: ArcusAccountRef) -> dict[int, PositionRow]:
    """``positions`` map ("keyed by marketId … JSON keys are stringified
    integers"): every key must equal its row's ``marketId``; flat rows omitted."""
    body = _obj(obj, "positions")
    out: dict[int, PositionRow] = {}
    for key, row in body.items():
        if not isinstance(key, str) or not _DIGITS_RE.match(key):
            _fail("positions.key")
        parsed_id = _market_id(_obj(row, "position").get("marketId"), "position.marketId")
        if int(key) != parsed_id:
            _fail("positions.key")
        position = parse_position_row(row, ref=ref)
        if position is not None:
            out[position.market_id] = position
    return out


def parse_positions_payload(obj: object, *, ref: ArcusAccountRef) -> dict[int, PositionRow]:
    """``GetPositionsResponse``: the ``positions`` map must be present ("Always
    present — an account with no open positions returns an empty object")."""
    body = _obj(obj, "positions")
    if "positions" not in body:
        _fail("positions.positions")
    return parse_positions_map(body.get("positions"), ref=ref)


def parse_account(obj: object, *, ref: ArcusAccountRef, now_mono: float) -> AccountRow:
    """``account`` (``GET /v1/account``) with echo checks."""
    body = _obj(obj, "account")
    _check_index_echo(body.get("accountIndex"), ref, "account.accountIndex")
    _check_address_echo(body.get("address"), ref, "account.address")
    if "positions" not in body:
        _fail("account.positions")
    positions = parse_positions_map(body.get("positions"), ref=ref)
    return AccountRow(
        equity=_dec(body.get("equity"), "account.equity"),
        free_collateral=_dec(body.get("freeCollateral"), "account.freeCollateral"),
        net_quote_balance=_dec(body.get("netQuoteBalance"), "account.netQuoteBalance"),
        net_deposits=_dec(body.get("netDeposits"), "account.netDeposits"),
        positions=MappingProxyType(positions),
        sequence_number=_int(body.get("sequenceNumber"), "account.sequenceNumber", ge=0),
        as_of_mono=now_mono,
    )


def _rest_pool(obj: object, where: str, now_mono: float) -> PoolReading:
    pool = _obj(obj, where)
    used = _int(pool.get("used"), f"{where}.used", ge=0)
    cap = _int(pool.get("cap"), f"{where}.cap", ge=0)
    next_ms = _int(pool.get("nextAvailableMs"), f"{where}.nextAvailableMs", ge=0)
    return PoolReading(
        remaining=cap - used,
        cap=cap,
        used=used,
        next_available_ms=next_ms,
        source="rest",
        as_of_mono=now_mono,
    )


def parse_rate_limit(
    obj: object, *, ref: ArcusAccountRef, now_mono: float
) -> tuple[PoolReading, PoolReading]:
    """``GetRateLimitResponse`` -> (order, cancel) readings; the echoed
    address/index must match ("compare that field against the index you asked
    for to catch a misspelling")."""
    body = _obj(obj, "rateLimit")
    _check_address_echo(body.get("address"), ref, "rateLimit.address")
    _check_index_echo(body.get("accountIndex"), ref, "rateLimit.accountIndex")
    return (
        _rest_pool(body.get("order"), "rateLimit.order", now_mono),
        _rest_pool(body.get("cancel"), "rateLimit.cancel", now_mono),
    )


def _api_key_entry(obj: object) -> ApiKeyEntry:
    row = _obj(obj, "apiKeys.entry")
    api_key = _str(row.get("apiKey"), "apiKeys.apiKey", pattern=_API_KEY_RE).lower()
    try:
        address = normalize_address(row.get("address"))
    except ValueError:
        _fail("apiKeys.address")
    status = _enum(row.get("status"), "apiKeys.status", _API_KEY_STATUSES)
    # Scope, fail-closed: `allSubaccounts` is "Populated on every response; use it
    # as the scope discriminator"; `accountIndex` is "Present only when
    # `allSubaccounts` is false". Every other combination is drift.
    all_raw = row.get("allSubaccounts")
    index_raw = row.get("accountIndex")
    all_subaccounts = None if all_raw is None else _bool(all_raw, "apiKeys.scope")
    account_index = None if index_raw is None else _int(index_raw, "apiKeys.scope", ge=0, le=9)
    if all_subaccounts is True and account_index is None:
        scope_all = True
    elif all_subaccounts is not True and account_index is not None:
        scope_all = False
    else:
        _fail("apiKeys.scope")
    permissions_raw = row.get("permissions")
    if permissions_raw is None:
        permissions: tuple[str, ...] = ()
    elif isinstance(permissions_raw, list) and all(isinstance(p, str) for p in permissions_raw):
        permissions = tuple(str(p) for p in permissions_raw)
    else:
        _fail("apiKeys.permissions")
    return ApiKeyEntry(
        api_key=api_key,
        address=address,
        all_subaccounts=scope_all,
        account_index=None if scope_all else account_index,
        api_wallet_name=_str_opt(row.get("apiWalletName"), "apiKeys.apiWalletName"),
        status=status,
        permissions=permissions,
        valid_until_ms=_int(row.get("validUntil"), "apiKeys.validUntil", ge=0, le=INT64_MAX),
        created_us=_int(row.get("createdAt"), "apiKeys.createdAt", ge=0, le=INT64_MAX),
    )


def parse_api_keys(obj: object) -> list[ApiKeyEntry]:
    """``GetApiKeysResponse``. Any bad entry fails the WHOLE response (the link
    flow then says "busy", never "key not found")."""
    return [_api_key_entry(r) for r in _list_container(obj, "apiKeys", "apiKeys")]


def parse_compliance(obj: object) -> ComplianceView:
    """``ComplianceResponse``; the ``address`` section is present only when the
    request carried ``?address=``."""
    body = _obj(obj, "compliance")
    geo = _obj(body.get("geo"), "compliance.geo")
    country = geo.get("country")
    if not isinstance(country, str):  # may be "" ("Empty if unknown")
        _fail("compliance.geo.country")
    restrictions = _obj(geo.get("restrictions"), "compliance.geo.restrictions")
    perps = _bool(restrictions.get("perpetuals"), "compliance.geo.restrictions.perpetuals")
    bypassed = _bool(geo.get("bypassed"), "compliance.geo.bypassed")
    address_status: Literal["COMPLIANT", "BLOCKED"] | None = None
    reason: str | None = None
    section = body.get("address")
    if section is not None:
        addr = _obj(section, "compliance.address")
        status = _enum(addr.get("status"), "compliance.address.status", _COMPLIANCE_STATUSES)
        address_status = "BLOCKED" if status == "BLOCKED" else "COMPLIANT"
        reason = _str_opt(addr.get("reason"), "compliance.address.reason")
    return ComplianceView(
        country=country,
        restrictions_perps=perps,
        bypassed=bypassed,
        address_status=address_status,
        reason=reason,
    )


def parse_leverages(obj: object, *, ref: ArcusAccountRef) -> list[LeverageEntry]:
    """``GetLeveragesResponse`` with echo checks; ``marginMode`` must agree with
    ``isolated`` ("`ISOLATED` when `isolated` is true, otherwise `CROSS`")."""
    body = _obj(obj, "leverages")
    _check_address_echo(body.get("address"), ref, "leverages.address")
    _check_index_echo(body.get("accountIndex"), ref, "leverages.accountIndex")
    out: list[LeverageEntry] = []
    for raw in _list_container(body, "leverages", "leverages"):
        row = _obj(raw, "leverages.entry")
        isolated = _bool(row.get("isolated"), "leverages.isolated")
        mode = _enum(row.get("marginMode"), "leverages.marginMode", _MARGIN_MODES)
        if isolated != (mode == "ISOLATED"):
            _fail("leverages.marginMode")
        out.append(
            LeverageEntry(
                market_id=_market_id(row.get("marketId"), "leverages.marketId"),
                leverage=_int(row.get("leverage"), "leverages.leverage", ge=1),
                isolated=isolated,
                margin_mode=mode,
            )
        )
    return out


def _bbo_level(v: object, where: str) -> tuple[Decimal, Decimal] | None:
    if v is None:
        return None
    level = _obj(v, where)
    return (
        _dec(level.get("price"), f"{where}.price", gt=0),
        _dec(level.get("size"), f"{where}.size", gt=0),
    )


def parse_bbo(obj: object) -> BboView:
    """``BBO``: each side null ("null if no bids") or {price > 0, size > 0}. A
    crossed snapshot (bid >= ask) is an error — never used for mids/ALO."""
    body = _obj(obj, "bbo")
    bid = _bbo_level(body.get("bestBid"), "bbo.bestBid")
    ask = _bbo_level(body.get("bestAsk"), "bbo.bestAsk")
    if bid is not None and ask is not None and bid[0] >= ask[0]:
        _fail("bbo.crossed")
    return BboView(
        bid=bid[0] if bid else None,
        ask=ask[0] if ask else None,
        bid_size=bid[1] if bid else None,
        ask_size=ask[1] if ask else None,
        timestamp_us=_int_opt(body.get("timestamp"), "bbo.timestamp", ge=0, le=INT64_MAX),
    )


def parse_mids(obj: object) -> dict[str, Decimal]:
    """``AllMidsResponse``: ticker -> mid; an "" value ("empty string if
    unavailable") is OMITTED, never 0."""
    body = _obj(obj, "mids")
    mids = _obj(body.get("mids"), "mids.mids")
    out: dict[str, Decimal] = {}
    for ticker, value in mids.items():
        if not isinstance(ticker, str) or not ticker:
            _fail("mids.key")
        if not isinstance(value, str):
            _fail("mids.value")
        if value == "":
            continue
        out[ticker] = _dec(value, "mids.value", gt=0)
    return out


def parse_prices(obj: object) -> dict[str, PriceView]:
    """``PricesResponse`` keyed by TICKER (the map key is not trusted as a
    market id: the doc example keys ``"0"`` to BTC-USD). Numeric-zero prices ->
    None ("markets that have not yet received a price carry "0" values")."""
    body = _obj(obj, "prices")
    out: dict[str, PriceView] = {}
    for key, raw in body.items():
        if not isinstance(key, str) or not _DIGITS_RE.match(key):
            _fail("prices.key")
        entry = _obj(raw, "prices.entry")
        ticker = _str(entry.get("marketDisplayName"), "prices.marketDisplayName")
        if ticker in out:
            _fail("prices.duplicate")
        out[ticker] = PriceView(
            ticker=ticker,
            market_key=int(key),
            oracle=_dec_opt(entry.get("oraclePrice"), "prices.oraclePrice", zero_is_none=True, ge=0),
            mark=_dec_opt(entry.get("markPrice"), "prices.markPrice", zero_is_none=True, ge=0),
            sequencer=_int(entry.get("sequencer"), "prices.sequencer", ge=0, le=INT64_MAX),
        )
    return out


def _l2_side(v: object, where: str) -> list[tuple[Decimal, Decimal]]:
    if not isinstance(v, list):
        _fail(where)
    out: list[tuple[Decimal, Decimal]] = []
    for level in v:
        if not isinstance(level, list) or len(level) != 2:
            _fail(where)
        out.append((_dec(level[0], f"{where}.price", gt=0), _dec(level[1], f"{where}.size", gt=0)))
    return out


def parse_l2(obj: object) -> L2BookView:
    """``OrderbookSnapshot`` (``[price, size]`` string pairs); sorted bids
    descending / asks ascending (order is not documented)."""
    body = _obj(obj, "l2")
    bids = _l2_side(body.get("bids"), "l2.bids")
    asks = _l2_side(body.get("asks"), "l2.asks")
    bids.sort(key=lambda lv: lv[0], reverse=True)
    asks.sort(key=lambda lv: lv[0])
    return L2BookView(
        bids=tuple(bids),
        asks=tuple(asks),
        last_sequence_id=_int_opt(body.get("lastSequenceId"), "l2.lastSequenceId", ge=0, le=INT64_MAX),
        timestamp_us=_int_opt(body.get("timestamp"), "l2.timestamp", ge=0, le=INT64_MAX),
    )


def _candle(obj: object) -> CandleRow:
    row = _obj(obj, "candles.row")
    return CandleRow(
        market_id=_market_id(row.get("marketId"), "candles.marketId"),
        ticker=_str(row.get("marketDisplayName"), "candles.marketDisplayName"),
        timeframe=_str(row.get("timeframe"), "candles.timeframe"),
        open_time_us=_epoch_us(row.get("openTime"), "candles.openTime"),
        open=_dec(row.get("open"), "candles.open", gt=0),
        high=_dec(row.get("high"), "candles.high", gt=0),
        low=_dec(row.get("low"), "candles.low", gt=0),
        close=_dec(row.get("close"), "candles.close", gt=0),
        volume=_dec(row.get("volume"), "candles.volume", ge=0),
        notional_volume=_dec(row.get("notionalVolume"), "candles.notionalVolume", ge=0),
        trade_count=_int(row.get("tradeCount"), "candles.tradeCount", ge=0),
        is_final=_bool(row.get("isFinal"), "candles.isFinal"),
    )


def parse_candles(obj: object, *, final_only: bool) -> list[CandleRow]:
    """``GetCandlesResponse`` -> bars sorted ASCENDING by open time.

    The docs say "Bars are returned oldest-first" but the live endpoint is
    newest-first (captured 2026-09-30), so the order is never trusted. A
    duplicate ``openTime`` keeps the ``isFinal`` row (else the later one) and
    counts ``candles.dup``; ``final_only`` drops still-forming bars.
    """
    by_open: dict[int, CandleRow] = {}
    for raw in _list_container(obj, "candles", "candles"):
        bar = _candle(raw)
        prev = by_open.get(bar.open_time_us)
        if prev is not None:
            record_schema_error("candles.dup")
            if prev.is_final and not bar.is_final:
                continue
        by_open[bar.open_time_us] = bar
    bars = sorted(by_open.values(), key=lambda c: c.open_time_us)
    if final_only:
        bars = [c for c in bars if c.is_final]
    return bars


__all__ = [
    "FIELD_SETS",
    "REQUIRED_FIELDS",
    "ComplianceView",
    "ApiKeyEntry",
    "LeverageEntry",
    "BboView",
    "L2BookView",
    "PriceView",
    "CandleRow",
    "tif_from_wire",
    "parse_time",
    "parse_markets_payload",
    "parse_order_row",
    "parse_open_orders_payload",
    "parse_fill_row",
    "parse_fills_payload",
    "parse_funding_row",
    "parse_funding_payload",
    "parse_position_row",
    "parse_positions_map",
    "parse_positions_payload",
    "parse_account",
    "parse_rate_limit",
    "parse_api_keys",
    "parse_compliance",
    "parse_leverages",
    "parse_bbo",
    "parse_mids",
    "parse_prices",
    "parse_l2",
    "parse_candles",
]
