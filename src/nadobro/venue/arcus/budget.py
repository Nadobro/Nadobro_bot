"""Arcus request budgets: the per-IP weight bucket (with lanes) and the
per-subaccount order/cancel pool governor. Async-safe, no ``time.sleep``.

Per-IP layer (docs ``api-reference__rate-limits.md``): "Every IP gets a token
bucket holding **1,500 weight**, refilling continuously at **1,500 weight per
minute** (25 weight/second). Each request deducts its weight before the
handler runs." List endpoints add a post-flight charge — "``weight = base +
floor(items / N)``, where ``N`` is ``20`` for most lists — ``60`` for
``candles`` and ``50`` for ``openOrders``" — and "a single large page can
briefly drive your bucket negative". ``l2OrderBook`` costs "``2 +
floor(nLevels/20)``"; batch writes "incur only a post-flight per-item charge of
``floor(N/40)``". Order writes are free on the IP layer ("the write itself is
free"); a write the bucket cannot cover is rejected with ``reason: ip``
("Reduce non-write traffic from this IP"), so a WRITE 429 (``ip`` / unknown
reason) blocks every lane and every write, L0 included
(:meth:`IpBudget.note_server_429`). A READ 429 ("no ``reason`` field — Per-IP
weight, on a read endpoint": "When the bucket can't cover a request's weight")
only says the bucket cannot cover THAT read's weight, so it blocks the weighted
lanes but never a (weight-0) write: a brake cancel that the venue really
IP-rejects comes back as its own ``Throttled(ip)`` at no cost
(:meth:`IpBudget.note_read_429`, R2-3).

Lanes: L0 (brakes) has no floor, so it can always use the last units the other
lanes may not touch (plan AD-8: "a … reserve for L0 that other lanes cannot
use"). A lane may only take tokens while the level stays at or above its floor.
Because the list add-on lands only AFTER the response, an L1-L3 list read
reserves its WORST-CASE add-on at admission (:func:`list_addon_cap`) and is
refunded the unused part once the row count is known
(:meth:`IpBudget.settle_addon`); otherwise concurrent reads that each passed
the floor check would overdraw the L0 reserve together (R2-1).

Concurrency: the check-and-deduct in :meth:`IpBudget.try_take` is synchronous
(no ``await`` between the read and the write), so it is atomic on the single
runtime loop; no lock is needed and this package never imports ``threading``.

Per-subaccount layer: order pool "20,000", cancel pool "40,000", "a slow
**drip** … **1 action per 10 seconds** per pool" once empty. Write responses
carry ``rateLimit.remaining`` (``floor(cap - consumed)``: "Can be ``0`` or
negative while the request still succeeds"; "``-1`` is a sentinel meaning the
account layer was not enforced"); ``GET /v1/rateLimit`` returns
``used``/``cap``/``nextAvailableMs``. A write's ``-1`` is therefore AMBIGUOUS
(the first drip action after the pool empties reads ``-1`` too), so it is
UNKNOWN, never "unlimited" (R2-2); only ``/v1/rateLimit`` settles it.
DENIED ≠ EMPTY: no reading, or a stale one, is UNKNOWN (``None``), never "full"
and never "empty".
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from types import MappingProxyType
from typing import Awaitable, Callable, Final, Literal, Mapping

from src.nadobro.utils.venue_scope import parse_arcus_net
from src.nadobro.venue.arcus.types import ArcusAccountRef, Lane, PoolReading

logger = logging.getLogger(__name__)

PoolKind = Literal["order", "cancel"]

# --- weights (docs rate-limits "Weight tiers") ----------------------------------------
_BASE_WEIGHT: Final[Mapping[str, int]] = MappingProxyType(
    {
        "health": 0,
        "placeOrder": 0,
        "cancelOrder": 0,
        "batchCancelOrders": 0,
        "root": 1,
        "time": 1,
        "compliance": 1,
        "bbo": 2,
        "mids": 2,
        "account": 2,
        "positions": 2,
        "order": 2,
        "feeTiers": 2,
        "leverages": 2,
        "accountStats": 2,
        "rateLimit": 2,
        "l2OrderBook": 2,
        "prices": 20,
        "markets": 20,
        "trade": 20,
        "trades": 20,
        "candles": 20,
        "portfolio": 20,
        "openOrders": 20,
        "orders": 20,
        "fills": 20,
        "funding": 20,
        "fundingRates": 20,
        "apiKeys": 20,
        "setLeverage": 125,
    }
)
# Post-flight list add-on divisors. `prices` returns a MAP, not a list; charging
# an add-on for it is a deliberate local over-estimate (over-charging only makes
# us more conservative) [U].
_LIST_DIVISOR: Final[Mapping[str, int]] = MappingProxyType(
    {
        "openOrders": 50,
        "candles": 60,
        "markets": 20,
        "prices": 20,
        "trades": 20,
        "orders": 20,
        "fills": 20,
        "funding": 20,
        "fundingRates": 20,
        "apiKeys": 20,
        "portfolio": 20,
    }
)
# "Page sizes are capped at 1,000 rows for most lists — 1,500 for candles".
_LIST_PAGE_MAX_ROWS: Final = 1000
_LIST_PAGE_MAX_ROWS_BY_KEY: Final[Mapping[str, int]] = MappingProxyType({"candles": 1500})
_L2_MIN_LEVELS: Final = 1
_L2_MAX_LEVELS: Final = 100  # "default 20, maximum 100"
_BATCH_DIVISOR: Final = 40


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def endpoint_weight(path_key: str) -> int:
    """Base IP weight of an endpoint key; an unknown key is a programming error."""
    if not isinstance(path_key, str) or path_key not in _BASE_WEIGHT:
        raise ValueError("unknown Arcus endpoint key")
    return _BASE_WEIGHT[path_key]


def list_addon(path_key: str, items: int) -> int:
    """Post-flight add-on ``floor(items / N)`` for list endpoints (0 otherwise)."""
    endpoint_weight(path_key)  # validates the key
    if not _is_int(items) or items < 0:
        raise ValueError("items must be an int >= 0")
    divisor = _LIST_DIVISOR.get(path_key)
    return items // divisor if divisor else 0


def list_addon_cap(path_key: str, rows: int | None = None) -> int:
    """Worst-case post-flight add-on of ONE page of ``path_key``: ``rows`` is the
    requested ``limit`` / ``countback`` (None -> the documented page cap:
    1,000 rows, 1,500 for ``candles``; a larger value is capped too). 0 for
    endpoints without a list add-on. Reserved at admission by L1-L3 reads."""
    endpoint_weight(path_key)  # validates the key
    if rows is not None and (not _is_int(rows) or rows < 0):
        raise ValueError("rows must be an int >= 0")
    cap = _LIST_PAGE_MAX_ROWS_BY_KEY.get(path_key, _LIST_PAGE_MAX_ROWS)
    return list_addon(path_key, cap if rows is None else min(rows, cap))


def l2_weight(n_levels: int) -> int:
    """``2 + floor(nLevels / 20)``; ``nLevels`` clamped to [1, 100] like the venue."""
    if not _is_int(n_levels):
        raise ValueError("n_levels must be an int")
    n = max(_L2_MIN_LEVELS, min(_L2_MAX_LEVELS, n_levels))
    return _BASE_WEIGHT["l2OrderBook"] + n // 20


def batch_addon(n: int) -> int:
    """Post-flight charge of a batch write of ``n`` elements: ``floor(n / 40)``."""
    if not _is_int(n) or n < 0:
        raise ValueError("n must be an int >= 0")
    return n // _BATCH_DIVISOR


# --- IP bucket ------------------------------------------------------------------------

DEFAULT_MAX_WAIT_S: Final[Mapping[Lane, float]] = MappingProxyType(
    {Lane.L0_BRAKE: 2.0, Lane.L1_ENGINE: 1.0, Lane.L2_INTERACTIVE: 3.0, Lane.L3_BACKGROUND: 0.0}
)
_DEFAULT_FLOORS: Final[Mapping[Lane, int]] = MappingProxyType(
    {Lane.L0_BRAKE: 0, Lane.L1_ENGINE: 300, Lane.L2_INTERACTIVE: 400, Lane.L3_BACKGROUND: 700}
)
_MAX_SLEEP_S: Final = 0.5
_MIN_SLEEP_S: Final = 0.005  # never spin on a float-rounding shortfall
_WARN_429_EVERY_S: Final = 10.0


class IpBudget:
    """Client-side mirror of the venue's per-IP weight bucket (one per network)."""

    def __init__(
        self,
        network: str,
        *,
        capacity: int = 1500,
        refill_per_s: float = 25.0,
        lane_floors: Mapping[Lane, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.network = parse_arcus_net(network)
        if not _is_int(capacity) or capacity <= 0:
            raise ValueError("capacity must be an int > 0")
        if isinstance(refill_per_s, bool) or not isinstance(refill_per_s, (int, float)):
            raise ValueError("refill_per_s must be a number")
        if not math.isfinite(refill_per_s) or refill_per_s <= 0:
            raise ValueError("refill_per_s must be finite and > 0")
        floors: dict[Lane, int] = dict(_DEFAULT_FLOORS)
        if lane_floors is not None:
            for lane, floor in lane_floors.items():
                if not isinstance(lane, Lane):
                    raise ValueError("lane_floors keys must be Lane")
                floors[lane] = floor
        for lane in Lane:
            floor = floors[lane]
            if not _is_int(floor) or not 0 <= floor < capacity:
                raise ValueError("lane floor must be an int in [0, capacity)")
        ordered = [floors[lane] for lane in sorted(Lane)]
        if ordered != sorted(ordered):
            raise ValueError("lane floors must be monotone: L0 <= L1 <= L2 <= L3")
        self._capacity = capacity
        self._refill = float(refill_per_s)
        self._floors: Mapping[Lane, int] = MappingProxyType(floors)
        self._clock = clock
        self._sleep = sleep
        self._level = float(capacity)  # starts FULL
        self._last = clock()
        self._blocked_until = float("-inf")  # write 429: every lane AND every write
        self._reads_blocked_until = float("-inf")  # read 429: weighted takes only
        self._taken: dict[Lane, int] = {lane: 0 for lane in Lane}
        self._denied: dict[Lane, int] = {lane: 0 for lane in Lane}
        self._impossible_warned: set[tuple[Lane, int]] = set()
        self._last_429_warn: float | None = None

    # -- internals --
    def _refill_now(self) -> float:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._level = min(float(self._capacity), self._level + elapsed * self._refill)
        self._last = now
        return now

    @staticmethod
    def _check_lane(lane: object) -> Lane:
        if not isinstance(lane, Lane):
            raise ValueError("lane must be a Lane")
        return lane

    # -- public --
    def _read_block_until(self) -> float:
        return max(self._blocked_until, self._reads_blocked_until)

    def try_take(self, weight: int, lane: Lane) -> bool:
        """Sync check-and-deduct: False while a server 429 block (write or read)
        is active, else True (and the level drops) iff ``level - weight >=
        floor[lane]``."""
        lane = self._check_lane(lane)
        if not _is_int(weight) or weight < 0:
            raise ValueError("weight must be an int >= 0")
        now = self._refill_now()
        if now < self._read_block_until():
            return False
        if self._level - weight >= self._floors[lane]:
            self._level -= weight
            self._taken[lane] += 1
            return True
        return False

    async def acquire(self, weight: int, lane: Lane, *, max_wait_s: float | None = None) -> bool:
        """Take ``weight`` for ``lane``, waiting at most ``max_wait_s`` (default
        per lane: L0 2 s, L1 1 s, L2 3 s, L3 0 s). False = the caller returns
        ``LocalDenied`` and sends nothing. Never sleeps when the wait cannot
        succeed before the deadline."""
        lane = self._check_lane(lane)
        if not _is_int(weight) or weight < 0:
            raise ValueError("weight must be an int >= 0")
        wait = DEFAULT_MAX_WAIT_S[lane] if max_wait_s is None else max_wait_s
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or wait < 0:
            raise ValueError("max_wait_s must be a finite number >= 0")
        if weight == 0:
            # 0-weight calls respect a WRITE 429 block (a read 429 cannot
            # refuse a weight-0 request: "the write itself is free").
            if self.write_blocked():
                self._denied[lane] += 1
                return False
            self._taken[lane] += 1
            return True
        floor = self._floors[lane]
        if weight > self._capacity - floor:
            key = (lane, weight)
            if key not in self._impossible_warned:
                self._impossible_warned.add(key)
                logger.warning(
                    "arcus %s ip budget: weight %d can never fit lane %s (floor %d)",
                    self.network,
                    weight,
                    lane.name,
                    floor,
                )
            self._denied[lane] += 1
            return False
        deadline = self._clock() + float(wait)
        while True:
            if self.try_take(weight, lane):
                return True
            now = self._clock()
            need = max(self._read_block_until() - now, (floor + weight - self._level) / self._refill)
            if now + need > deadline:
                self._denied[lane] += 1
                return False
            await self._sleep(max(_MIN_SLEEP_S, min(need, _MAX_SLEEP_S)))

    def charge_after(self, extra_weight: int) -> None:
        """Post-flight add-on (list rows, batch items). May drive the level
        negative (floored at ``-capacity``)."""
        if not _is_int(extra_weight) or extra_weight < 0:
            raise ValueError("extra_weight must be an int >= 0")
        if extra_weight == 0:
            return
        self._refill_now()
        self._level = max(-float(self._capacity), self._level - extra_weight)

    def settle_addon(self, reserved: int, actual: int) -> None:
        """Settle a post-flight add-on of which ``reserved`` units were taken at
        admission: charge the excess (like :meth:`charge_after`, may go
        negative) or refund the unused part (never above capacity)."""
        for value in (reserved, actual):
            if not _is_int(value) or value < 0:
                raise ValueError("add-on units must be ints >= 0")
        if actual > reserved:
            self.charge_after(actual - reserved)
        elif reserved > actual:
            self._refill_now()
            self._level = min(float(self._capacity), self._level + (reserved - actual))

    def note_server_429(self, retry_after_ms: int) -> None:
        """A WRITE 429 (``reason: ip`` / unknown): the server bucket cannot cover
        even a free write. Block every lane (L0 too) and every write until
        ``now + retry_after``; the local level drops to <= 0."""
        if not _is_int(retry_after_ms) or retry_after_ms < 0:
            raise ValueError("retry_after_ms must be an int >= 0")
        now = self._refill_now()
        self._blocked_until = max(self._blocked_until, now + retry_after_ms / 1000.0)
        self._level = min(self._level, 0.0)
        if self._last_429_warn is None or now - self._last_429_warn >= _WARN_429_EVERY_S:
            self._last_429_warn = now
            logger.warning(
                "arcus %s ip budget blocked by a server 429 for %.1fs (all lanes)",
                self.network,
                self._blocked_until - now,
            )

    def note_read_429(self, retry_after_ms: int) -> None:
        """A READ 429 (no ``reason``): the server bucket cannot cover that read's
        weight. Block every weighted take (L0 reads too) until ``now +
        retry_after`` and drop the local level to <= 0, but never writes (R2-3):
        :meth:`write_blocked` stays False, so brake cancels still go out."""
        if not _is_int(retry_after_ms) or retry_after_ms < 0:
            raise ValueError("retry_after_ms must be an int >= 0")
        now = self._refill_now()
        self._reads_blocked_until = max(self._reads_blocked_until, now + retry_after_ms / 1000.0)
        self._level = min(self._level, 0.0)
        if self._last_429_warn is None or now - self._last_429_warn >= _WARN_429_EVERY_S:
            self._last_429_warn = now
            logger.warning(
                "arcus %s ip budget: reads blocked by a server read 429 for %.1fs (writes still allowed)",
                self.network,
                self._reads_blocked_until - now,
            )

    def write_blocked(self) -> bool:
        """True while a WRITE 429 block is active (writes are refused locally).
        A read 429 never sets it."""
        return self._clock() < self._blocked_until

    def level(self) -> float:
        self._refill_now()
        return self._level

    def snapshot(self) -> dict[str, object]:
        now = self._refill_now()
        return {
            "network": self.network,
            "level": round(self._level, 3),
            "capacity": self._capacity,
            "floors": {lane.name: self._floors[lane] for lane in Lane},
            "blocked_for_s": round(max(0.0, self._blocked_until - now), 3),
            "reads_blocked_for_s": round(max(0.0, self._read_block_until() - now), 3),
            "taken": {lane.name: self._taken[lane] for lane in Lane},
            "denied": {lane.name: self._denied[lane] for lane in Lane},
        }


# --- per-subaccount pools ------------------------------------------------------------

_POOLS: Final = ("order", "cancel")


@dataclass(frozen=True)
class PoolReserves:
    order: int
    cancel: int

    def __post_init__(self) -> None:
        for name in ("order", "cancel"):
            value = getattr(self, name)
            if not _is_int(value) or value < 0:
                raise ValueError("pool reserves must be ints >= 0")


def _check_pool(pool: object) -> PoolKind:
    if pool == "order":
        return "order"
    if pool == "cancel":
        return "cancel"
    raise ValueError("pool must be 'order' or 'cancel'")


class PoolGovernor:
    """Latest order/cancel pool readings of ONE subaccount (newest wins).

    Subaccount 0 is shared with the user's own manual trading (owner decision
    4), so an old reading can overstate headroom: after ``stale_after_s`` it is
    UNKNOWN (``None``). Callers treat unknown as "below reserve".
    """

    def __init__(
        self,
        ref: ArcusAccountRef,
        *,
        stale_after_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(ref, ArcusAccountRef):
            raise ValueError("PoolGovernor needs an ArcusAccountRef")
        if (
            isinstance(stale_after_s, bool)
            or not isinstance(stale_after_s, (int, float))
            or not math.isfinite(stale_after_s)
            or stale_after_s <= 0
        ):
            raise ValueError("stale_after_s must be finite and > 0")
        self.ref = ref
        self._stale_after_s = float(stale_after_s)
        self._clock = clock
        self._latest: dict[str, PoolReading] = {}
        self._cooldown_until: float | None = None
        self._cooldown_reason: str | None = None
        self._hold_since: float | None = None

    def _keep(self, pool: PoolKind, reading: PoolReading) -> None:
        current = self._latest.get(pool)
        if current is None or reading.as_of_mono >= current.as_of_mono:
            self._latest[pool] = reading

    def update_from_write(self, pool_name: PoolKind, reading: PoolReading) -> None:
        """A write response's ``rateLimit`` reading (``source == "write"``); an
        older reading than the current one is ignored. The ``-1`` sentinel
        (``remaining is None``) is kept as the newest reading and reads as
        UNKNOWN until a newer reading (e.g. ``GET /v1/rateLimit``) arrives."""
        pool = _check_pool(pool_name)
        if not isinstance(reading, PoolReading) or reading.source != "write":
            raise ValueError("update_from_write needs a write PoolReading")
        self._keep(pool, reading)

    def update_from_rest(self, order: PoolReading, cancel: PoolReading, echoed_account_index: int) -> None:
        """``GET /v1/rateLimit`` readings. The echoed index must be ours ("compare
        that field against the index you asked for to catch a misspelling")."""
        if not _is_int(echoed_account_index) or echoed_account_index != self.ref.account_index:
            raise ValueError("rateLimit echo is for another subaccount")
        for reading in (order, cancel):
            if not isinstance(reading, PoolReading) or reading.source != "rest":
                raise ValueError("update_from_rest needs rest PoolReadings")
        self._keep("order", order)
        self._keep("cancel", cancel)

    def _fresh(self, pool: PoolKind) -> PoolReading | None:
        reading = self._latest.get(pool)
        if reading is None or self._clock() - reading.as_of_mono > self._stale_after_s:
            return None
        return reading

    def headroom(self, pool: PoolKind) -> int | None:
        """Units left in ``pool``; None = UNKNOWN (no reading, stale, or a
        write's ambiguous ``-1``: "not enforced" OR the first drip action —
        never read as unlimited, R2-2); on the drip -> ``<= 0``."""
        reading = self._fresh(_check_pool(pool))
        if reading is None:
            return None
        if reading.source == "write":
            return reading.remaining
        if reading.next_available_ms is not None and reading.next_available_ms > 0:
            return 0  # "otherwise the milliseconds until the next drip token frees up"
        if reading.cap is not None and reading.used is not None:
            return reading.cap - reading.used
        return reading.remaining

    def below_reserve(self, reserves: PoolReserves) -> bool | None:
        """None when either pool is unknown (callers treat None as True)."""
        order = self.headroom("order")
        cancel = self.headroom("cancel")
        if order is None or cancel is None:
            return None
        return order < reserves.order or cancel < reserves.cancel

    def note_cap_cooldown(self, reason: str, seconds: float) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("cooldown reason required")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("cooldown seconds must be finite and >= 0")
        until = self._clock() + float(seconds)
        if self._cooldown_until is None or until >= self._cooldown_until:
            self._cooldown_until = until
            self._cooldown_reason = reason

    def cap_cooldown_active(self) -> str | None:
        if self._cooldown_until is None or self._clock() >= self._cooldown_until:
            return None
        return self._cooldown_reason

    def note_hold(self, active: bool) -> None:
        """False->True stamps the hold start once; ->False clears it."""
        if active:
            if self._hold_since is None:
                self._hold_since = self._clock()
        else:
            self._hold_since = None

    def hold_since_mono(self) -> float | None:
        return self._hold_since

    def reading_age_s(self, pool: PoolKind) -> float | None:
        """Age of the latest reading (None when there is none) — so a card never
        shows a stale reading as current (08 CD-7)."""
        reading = self._latest.get(_check_pool(pool))
        if reading is None:
            return None
        return self._clock() - reading.as_of_mono

    def runway_hours(self, pool: PoolKind, burn_per_h: float) -> float | None:
        if isinstance(burn_per_h, bool) or not isinstance(burn_per_h, (int, float)) or math.isnan(burn_per_h):
            raise ValueError("burn_per_h must be a number")
        head = self.headroom(pool)
        if head is None:
            return None
        if burn_per_h <= 0:
            return math.inf
        return max(0, head) / burn_per_h

    def snapshot(self) -> dict[str, object]:
        """For /status; carries no address."""

        def one(pool: PoolKind) -> dict[str, object]:
            reading = self._latest.get(pool)
            return {
                "headroom": self.headroom(pool),
                "age_s": self.reading_age_s(pool),
                "source": None if reading is None else reading.source,
            }

        return {
            "network": self.ref.network,
            "account_index": self.ref.account_index,
            "order": one("order"),
            "cancel": one("cancel"),
            "cooldown": self.cap_cooldown_active(),
            "hold_since_mono": self._hold_since,
        }


def _to_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} must be finite")
        out = value
    else:
        out = Decimal(value) if isinstance(value, int) else Decimal(str(value))
    if out < 0:
        raise ValueError(f"{name} must be >= 0")
    return out


def session_reserves(
    *,
    resting_orders: int,
    reducing_requotes_per_h: float,
    hold_max_s: float,
    flatten_units: int = 4,
    margin_frac: float = 0.10,
    floor_order: int,
    floor_cancel: int,
) -> PoolReserves:
    """Per-session pool reserves (Decimal math, ROUND_CEILING — float would turn
    ``60 * 1.1`` into ``66.00000000000001`` and ceil it to 67)::

        order  = max(floor_order,  ceil((requotes/h * hold_s / 3600 + flatten_units) * (1 + margin)))
        cancel = max(floor_cancel, ceil((requotes/h * hold_s / 3600 + resting_orders) * (1 + margin)))
    """
    for name, value in (
        ("resting_orders", resting_orders),
        ("flatten_units", flatten_units),
        ("floor_order", floor_order),
        ("floor_cancel", floor_cancel),
    ):
        if not _is_int(value) or value < 0:
            raise ValueError(f"{name} must be an int >= 0")
    requotes = _to_decimal(reducing_requotes_per_h, "reducing_requotes_per_h")
    hold = _to_decimal(hold_max_s, "hold_max_s")
    margin = _to_decimal(margin_frac, "margin_frac")
    during_hold = requotes * hold / Decimal(3600)
    factor = Decimal(1) + margin
    order = ((during_hold + Decimal(flatten_units)) * factor).to_integral_value(rounding=ROUND_CEILING)
    cancel = ((during_hold + Decimal(resting_orders)) * factor).to_integral_value(rounding=ROUND_CEILING)
    return PoolReserves(order=max(floor_order, int(order)), cancel=max(floor_cancel, int(cancel)))


__all__ = [
    "DEFAULT_MAX_WAIT_S",
    "IpBudget",
    "PoolGovernor",
    "PoolReserves",
    "PoolKind",
    "endpoint_weight",
    "list_addon",
    "list_addon_cap",
    "l2_weight",
    "batch_addon",
    "session_reserves",
]
