"""Arcus value types, constants and clientId helpers — pure (no I/O, no logging).

Units follow the Arcus docs and are carried in field-name suffixes (``_us`` =
epoch microseconds, ``_ms``, ``_ns``, ``_s``). Prices and sizes are ``Decimal``
(never float). Every constructor validates and raises ``ValueError`` without
echoing the offending value.

Sources (docs mirror ``arcus_docs_mirror_20260927``):
- batch-cancel-orders: "Cancel up to 100 orders in a single request."
- place-order: clientId "maxLength: 36", pattern ``^[A-Za-z0-9_-]+$``;
  timestamp "Must be within ±30,000 ms"; "Market orders must use
  timeInForce: IOC" (guides__rest-trading); MARKET price "within 10% of the
  current mark price".
- rate-limits: pools "Order | 20,000", "Cancel | 40,000"; "1 action per 10
  seconds per pool".
- authentication: "`s` — side: `0` buy, `1` sell"; "`t` — time-in-force: `0`
  GTT, `1` FOK, `2` IOC, `3` ALO".
- get-fills: time bounds — "the server requires at least `1e14`" (µs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Final, Literal, Mapping

from src.nadobro.utils.venue_scope import arcus_scope_for, parse_arcus_net

ArcusNet = Literal["testnet", "mainnet"]

# --- constants ---------------------------------------------------------------
ARCUS_MAX_BATCH: Final = 100
ARCUS_CLIENT_ID_MAX: Final = 36
ARCUS_MARKET_PRICE_BAND: Final = Decimal("0.10")
ARCUS_TS_DRIFT_MS: Final = 30_000
ARCUS_ORDER_POOL_BASE: Final = 20_000
ARCUS_CANCEL_POOL_BASE: Final = 40_000
ARCUS_DRIP_S: Final = 10
ARCUS_PAGE_MAX: Final = 1000
ARCUS_MIN_EPOCH_US: Final = 10**14
INT64_MAX: Final = 2**63 - 1
# probe:markets_api.testnet.json — "marketId":1 BTC-USD, 2 ETH-USD, 3 SOL-USD
# (identical on mainnet, probe:markets_api.json). A renumbering is a code change.
ARCUS_V1_MARKET_IDS: Final[Mapping[str, int]] = MappingProxyType(
    {"BTC-USD": 1, "ETH-USD": 2, "SOL-USD": 3}
)

CLIENT_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
ADDRESS_RE: Final = re.compile(r"^0x[0-9a-f]{40}$")  # stored form (lowercase)
TICKER_RE: Final = re.compile(r"^[A-Za-z0-9._-]{1,32}$")  # checked before a ticker goes in a URL path
ORDER_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")  # "Server-generated order ID (hex string)"
# EthereumAddressHex pattern "^(0x|0X)?[0-9a-fA-F]{40}$" (place-order).
_ADDRESS_IN_RE: Final = re.compile(r"^(0x|0X)?([0-9a-fA-F]{40})$")
# Every order this library places is bot-owned (build_decisions: "Bot orders
# carry clientId prefix "nb…"").
BOT_CLIENT_ID_PREFIX: Final = "nb"

_B36_DIGITS: Final = "0123456789abcdefghijklmnopqrstuvwxyz"


def _is_int(value: object) -> bool:
    """A real ``int`` (``bool`` is rejected: ``True`` must never become ``1``)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def normalize_address(value: object) -> str:
    """``0x`` + 40 lowercase hex. Accepts an optional ``0x``/``0X`` prefix and
    surrounding whitespace; anything else -> ``ValueError("invalid address")``
    (the message never echoes the input)."""
    if not isinstance(value, str):
        raise ValueError("invalid address")
    m = _ADDRESS_IN_RE.match(value.strip())
    if m is None:
        raise ValueError("invalid address")
    return "0x" + m.group(2).lower()


@dataclass(frozen=True)
class ArcusAccountRef:
    """One Arcus (sub)account: network token, lowercase address, index 0..9."""

    network: ArcusNet
    address: str
    account_index: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "network", parse_arcus_net(self.network))
        object.__setattr__(self, "address", normalize_address(self.address))
        # doc AccountIndex "minimum: 0 maximum: 9"
        if not _is_int(self.account_index) or not 0 <= self.account_index <= 9:
            raise ValueError("invalid account index")

    @property
    def scope(self) -> str:
        """``'arcus_testnet'`` | ``'arcus_mainnet'``."""
        return arcus_scope_for(self.network)


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def signed(self) -> int:
        """Signed-payload side code: ``0`` buy, ``1`` sell."""
        return 0 if self is Side.BUY else 1


class Tif(Enum):
    """Values are the signed ``t`` codes; the wire string is ``.name``."""

    GTT = 0
    FOK = 1
    IOC = 2
    ALO = 3


class WireOrderType(Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Lane(IntEnum):
    L0_BRAKE = 0
    L1_ENGINE = 1
    L2_INTERACTIVE = 2
    L3_BACKGROUND = 3


@dataclass(frozen=True)
class OrderSpec:
    """One placement, already quantized by the caller.

    ``tick_size``/``step_size`` are the market's TOP-LEVEL units from the same
    catalog snapshot used to quantize (02 D1): the signed ``p``/``q`` always
    divide by the top-level ``tickSize`` ("The divisor is always the market's
    top-level tickSize, at every price"). Exactness against those units is
    checked by ``signing.to_ticks``/``to_quantums`` (``InexactUnitError``).
    """

    market_id: int
    side: Side
    order_type: WireOrderType
    tif: Tif
    quantity: Decimal
    price: Decimal  # limit price; MARKET: the protective bound
    reduce_only: bool
    client_id: str
    good_til_us: int
    tick_size: Decimal
    step_size: Decimal

    def __post_init__(self) -> None:
        if not _is_int(self.market_id) or not 0 <= self.market_id <= 65535:
            raise ValueError("invalid market id")
        if not isinstance(self.side, Side):
            raise ValueError("invalid side")
        if not isinstance(self.order_type, WireOrderType):
            raise ValueError("invalid order type")
        if not isinstance(self.tif, Tif):
            raise ValueError("invalid time in force")
        for name in ("quantity", "price", "tick_size", "step_size"):
            if not _is_positive_decimal(getattr(self, name)):
                raise ValueError(f"{name} must be a finite Decimal > 0")
        if self.order_type is WireOrderType.MARKET and self.tif is not Tif.IOC:
            raise ValueError("MARKET orders must use IOC")
        if not isinstance(self.client_id, str) or CLIENT_ID_RE.match(self.client_id) is None:
            raise ValueError("invalid client id")
        if not self.client_id.startswith(BOT_CLIENT_ID_PREFIX):
            raise ValueError("client id must carry the bot prefix")
        # goodTilTime is epoch µs ("Millisecond or second epochs are rejected");
        # the signed g = µs × 1000 must fit int64.
        if (
            not _is_int(self.good_til_us)
            or self.good_til_us < ARCUS_MIN_EPOCH_US
            or self.good_til_us * 1000 > INT64_MAX
        ):
            raise ValueError("good_til_us must be epoch microseconds")
        if not isinstance(self.reduce_only, bool):
            raise ValueError("reduce_only must be a bool")


@dataclass(frozen=True)
class CancelSpec:
    """Cancel one order by server id OR by clientId (exactly one).

    A clientId target must carry the bot prefix: the library only ever cancels
    by clientId an order it placed itself (sweeps touch only bot-owned orders).
    """

    market_id: int
    order_id: str | None = None
    client_id: str | None = None

    def __post_init__(self) -> None:
        if not _is_int(self.market_id) or not 0 <= self.market_id <= 65535:
            raise ValueError("invalid market id")
        if (self.order_id is None) == (self.client_id is None):
            raise ValueError("exactly one of order_id / client_id")
        if self.order_id is not None:
            if not isinstance(self.order_id, str) or ORDER_ID_RE.match(self.order_id) is None:
                raise ValueError("invalid order id")
        if self.client_id is not None:
            if not isinstance(self.client_id, str) or CLIENT_ID_RE.match(self.client_id) is None:
                raise ValueError("invalid client id")
            if not self.client_id.startswith(BOT_CLIENT_ID_PREFIX):
                raise ValueError("client id must carry the bot prefix")


@dataclass(frozen=True)
class PoolReading:
    """One per-subaccount pool reading. ``remaining`` None = not enforced
    (write-response sentinel ``-1``)."""

    remaining: int | None
    cap: int | None
    used: int | None
    next_available_ms: int | None
    source: Literal["write", "rest"]
    as_of_mono: float


@dataclass(frozen=True)
class OrderRow:
    """Unified REST order shape (``openOrders`` rows, ``GET /v1/order``)."""

    order_id: str
    client_id: str | None
    market_id: int
    ticker: str
    side: Side
    status: str  # raw upper-case order-status, verbatim (unknown values kept)
    state: str | None  # OPEN|PARTIALLY_FILLED|FILLED|CANCELED|REJECTED, None when absent
    price: Decimal
    original_size: Decimal
    remaining_size: Decimal
    filled_size: Decimal | None
    avg_fill_price: Decimal | None
    tif: Tif | None  # "GTC" read as Tif.GTT; unknown -> None
    reduce_only: bool  # absent -> False ("Present only when true")
    rejection_reason: str | None
    cancel_reason: str | None
    created_us: int | None
    updated_us: int
    sequence_number: int | None


@dataclass(frozen=True)
class FillRow:
    """Canonical fill (WS or REST); quantities Decimal, fee signed as reported."""

    trade_id: str
    order_id: str
    client_id: str | None
    market_id: int
    ticker: str
    side: Side
    size: Decimal
    price: Decimal
    fee: Decimal
    closed_pnl: Decimal | None
    role: str  # MAKER|TAKER|ALP
    position_effect: str | None
    liquidation_method: str | None
    created_us: int
    sequence_number: int | None
    source: Literal["ws", "rest"]


@dataclass(frozen=True)
class FundingRow:
    market_id: int
    ticker: str
    funding_rate: Decimal  # hourly rate
    size: Decimal  # signed position size at payment time
    payment: Decimal  # + received (venue convention; stored natively)
    time_us: int


@dataclass(frozen=True)
class PositionRow:
    market_id: int
    ticker: str
    size: Decimal  # signed: + long, - short
    average_entry_price: Decimal  # kept verbatim (units [U A8])
    leverage: Decimal | None
    margin_mode: str
    margin_used: Decimal | None
    position_value_notional: Decimal | None
    venue_margin_delta: Decimal | None  # = venue `unrealizedPnl` (a margin delta, never uPnL)
    mark_px: Decimal | None  # None when the venue sent a numeric zero
    sequence_number: int | None


@dataclass(frozen=True)
class AccountRow:
    equity: Decimal
    free_collateral: Decimal
    net_quote_balance: Decimal
    net_deposits: Decimal
    positions: Mapping[int, PositionRow]
    sequence_number: int
    as_of_mono: float


class StreamHealth(Enum):
    LIVE = "live"
    DEGRADED = "degraded"
    STALE = "stale"
    DOWN = "down"
    UNKNOWN = "unknown"


# --- clientId format (contract §4.9; 02 D17) ---------------------------------
# f"nb{base36(user_tag)}_{base36(session_id)}-{base36(seq)}". user_tag 0 is
# RESERVED for scripts/arcus_testnet_probe.py (Telegram ids are >= 1). Worst
# case (uid < 36^8, sid < 36^7, seq < 36^9) is 2+8+1+7+1+9 = 28 <= 36 chars.


def base36(n: int) -> str:
    """Lowercase base-36 of an int ``n >= 0`` (``base36(0) == "0"``)."""
    if not _is_int(n) or n < 0:
        raise ValueError("base36 needs an int >= 0")
    if n == 0:
        return "0"
    out: list[str] = []
    while n:
        n, r = divmod(n, 36)
        out.append(_B36_DIGITS[r])
    return "".join(reversed(out))


def user_client_prefix(user_tag: int) -> str:
    """``nb<uid36>_`` — every bot order of one user starts with this."""
    return f"{BOT_CLIENT_ID_PREFIX}{base36(user_tag)}_"


def session_client_prefix(user_tag: int, session_id: int) -> str:
    """``nb<uid36>_<sid36>-`` — every bot order of one session starts with this."""
    return f"{user_client_prefix(user_tag)}{base36(session_id)}-"


def client_id_for(user_tag: int, session_id: int, seq: int) -> str:
    """The clientId of order ``seq`` in a session; ``ValueError`` if it would
    not be a valid Arcus clientId (pattern / 36-char limit)."""
    cid = session_client_prefix(user_tag, session_id) + base36(seq)
    if len(cid) > ARCUS_CLIENT_ID_MAX or CLIENT_ID_RE.match(cid) is None:
        raise ValueError("client id out of range")
    return cid


__all__ = [
    "ArcusNet",
    "ARCUS_MAX_BATCH",
    "ARCUS_CLIENT_ID_MAX",
    "ARCUS_MARKET_PRICE_BAND",
    "ARCUS_TS_DRIFT_MS",
    "ARCUS_ORDER_POOL_BASE",
    "ARCUS_CANCEL_POOL_BASE",
    "ARCUS_DRIP_S",
    "ARCUS_PAGE_MAX",
    "ARCUS_MIN_EPOCH_US",
    "INT64_MAX",
    "ARCUS_V1_MARKET_IDS",
    "CLIENT_ID_RE",
    "ADDRESS_RE",
    "TICKER_RE",
    "ORDER_ID_RE",
    "BOT_CLIENT_ID_PREFIX",
    "normalize_address",
    "ArcusAccountRef",
    "Side",
    "Tif",
    "WireOrderType",
    "Lane",
    "OrderSpec",
    "CancelSpec",
    "PoolReading",
    "OrderRow",
    "FillRow",
    "FundingRow",
    "PositionRow",
    "AccountRow",
    "StreamHealth",
    "base36",
    "user_client_prefix",
    "session_client_prefix",
    "client_id_for",
]
