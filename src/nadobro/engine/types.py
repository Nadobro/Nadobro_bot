"""Engine types — enums and dataclass models shared across the engine.

NOTE (deviation from brief): the brief specifies *Pydantic v2* models. This
repository pins Pydantic **v1.10** (a transitive dependency) and upgrading is
out of scope for Phase 1, so the engine uses stdlib ``dataclasses`` +
``Decimal`` + ``Enum`` with explicit ``__post_init__`` validation. This keeps
the new engine fully typed and decoupled from the legacy Pydantic v1 surface.

Implemented in Phase 1.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional


def _dec(value: object) -> Decimal:
    """Coerce a numeric/str value to Decimal without float artefacts."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_bool(value: object, default: bool = False) -> bool:
    """Coerce a UI/config flag to bool. The UI stores flags as "1"/"0" STRINGS, so a
    bare ``bool("0")`` (a non-empty string, hence True) is wrong — parse the value.
    Shared by the engine controllers and the strategy mapper so the three call sites
    cannot drift.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class TradeType(Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> "TradeType":
        return TradeType.SELL if self is TradeType.BUY else TradeType.BUY


class OrderType(Enum):
    LIMIT = "LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"
    MARKET = "MARKET"


class ExecutionStrategy(Enum):
    LIMIT = "LIMIT"
    LIMIT_MAKER = "LIMIT_MAKER"
    MARKET = "MARKET"
    LIMIT_CHASER = "LIMIT_CHASER"


class ExecutorState(Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    TERMINATED = "TERMINATED"


class CloseType(Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TIME_LIMIT = "TIME_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"
    EARLY_STOP = "EARLY_STOP"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PositionAction(Enum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True)
class ExchangePair:
    """A venue trading pair, e.g. ``ExchangePair('SOL', 'USDC').name == 'SOL-USDC'``."""

    base: str
    quote: str

    @property
    def name(self) -> str:
        return f"{self.base}-{self.quote}"

    @classmethod
    def parse(cls, pair: str) -> "ExchangePair":
        sep = "-" if "-" in pair else "/"
        base, _, quote = pair.partition(sep)
        if not base or not quote:
            raise ValueError(f"Invalid trading pair: {pair!r}")
        return cls(base=base.upper(), quote=quote.upper())


@dataclass
class TrailingStop:
    """Arms when the position's unrealized return reaches ``activation_price``
    (a return fraction, e.g. ``0.02`` == +2%); thereafter the stop trails the
    best-seen price by ``trailing_delta`` (a fraction)."""

    activation_price: Decimal
    trailing_delta: Decimal

    def __post_init__(self) -> None:
        self.activation_price = _dec(self.activation_price)
        self.trailing_delta = _dec(self.trailing_delta)
        if self.trailing_delta <= 0:
            raise ValueError("trailing_delta must be > 0")
        if self.activation_price < 0:
            raise ValueError("activation_price must be >= 0")


@dataclass
class TripleBarrierConfig:
    """Take-profit / stop-loss / time-limit / trailing-stop barriers, each with
    its own closing order type. ``take_profit`` and ``stop_loss`` are return
    fractions relative to entry; ``time_limit`` is seconds."""

    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    time_limit: Optional[int] = None
    trailing_stop: Optional[TrailingStop] = None
    open_order_type: OrderType = OrderType.LIMIT
    take_profit_order_type: OrderType = OrderType.MARKET
    stop_loss_order_type: OrderType = OrderType.MARKET
    time_limit_order_type: OrderType = OrderType.MARKET
    trailing_stop_order_type: OrderType = OrderType.MARKET

    def __post_init__(self) -> None:
        if self.take_profit is not None:
            self.take_profit = _dec(self.take_profit)
            if self.take_profit <= 0:
                raise ValueError("take_profit must be > 0")
        if self.stop_loss is not None:
            self.stop_loss = _dec(self.stop_loss)
            if self.stop_loss <= 0:
                raise ValueError("stop_loss must be > 0")
        if self.time_limit is not None and self.time_limit <= 0:
            raise ValueError("time_limit must be > 0 seconds")

    @property
    def has_any_barrier(self) -> bool:
        return any(
            b is not None
            for b in (self.take_profit, self.stop_loss, self.time_limit, self.trailing_stop)
        )


@dataclass
class LimitChaserConfig:
    """LIMIT_CHASER parameters: post ``distance`` off mid; re-post once mid
    has moved more than ``refresh_threshold`` (both fractions of mid)."""

    distance: Decimal
    refresh_threshold: Decimal
    max_refreshes: int = 50

    def __post_init__(self) -> None:
        self.distance = _dec(self.distance)
        self.refresh_threshold = _dec(self.refresh_threshold)
        if self.distance < 0:
            raise ValueError("distance must be >= 0")
        if self.refresh_threshold <= 0:
            raise ValueError("refresh_threshold must be > 0")


@dataclass
class RiskLimits:
    """Guardrails consumed by the Risk Engine. ``None`` disables a gate.

    Pre-tick gates: ``daily_pnl_floor_quote`` (block when daily PnL drops to/below),
    ``max_drawdown_pct`` (fraction), ``daily_cost_cap_usd``.
    Per-executor gates: ``max_open_executors``, ``max_single_order_quote``,
    ``max_position_size_quote``.
    """

    daily_pnl_floor_quote: Optional[Decimal] = None
    max_drawdown_pct: Optional[Decimal] = None
    daily_cost_cap_usd: Optional[Decimal] = None
    max_open_executors: Optional[int] = None
    max_single_order_quote: Optional[Decimal] = None
    max_position_size_quote: Optional[Decimal] = None


@dataclass
class RiskState:
    daily_pnl_quote: Decimal = Decimal(0)
    total_exposure_quote: Decimal = Decimal(0)
    executor_count: int = 0
    drawdown_pct: Decimal = Decimal(0)
    daily_cost_usd: Decimal = Decimal(0)
    is_blocked: bool = False
    block_reason: Optional[str] = None
    # BUG-RISK-1 fix: snapshot the UTC date the daily aggregates were
    # computed for. The provider checks this against today's date and
    # zero-resets daily_pnl_quote / daily_cost_usd on a new day so a stale
    # state object can't keep the daily-loss kill switch armed indefinitely.
    daily_anchor_utc: Optional[str] = None

    def rolled_over(self, today_utc: str) -> "RiskState":
        """Return a copy with daily counters reset if the day rolled over."""
        if self.daily_anchor_utc == today_utc:
            return self
        return RiskState(
            daily_pnl_quote=Decimal(0),
            total_exposure_quote=self.total_exposure_quote,
            executor_count=self.executor_count,
            drawdown_pct=self.drawdown_pct,
            daily_cost_usd=Decimal(0),
            is_blocked=False,
            block_reason=None,
            daily_anchor_utc=today_utc,
        )
