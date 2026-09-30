"""Arcus clock: ``/v1/time`` offset, strictly increasing per-key ``ct``, GTT.

Signed writes carry ``ct`` = Unix NANOseconds that "Must be within ±30,000 ms …
of server wall-clock" (place-order) and must equal ``X-Timestamp``. We measure
the server offset (``GET /v1/time``: "Returns the current server wall-clock time
as Unix nanoseconds"; changelog: "useful for aligning the nanosecond signing
timestamp with the gateway's ±30s drift window") and draw every ``ct`` as
``max(local_ns + offset, last_ct[key] + 1)`` so a key never reuses a timestamp
(testnet ``TimestampReused``).

Concurrency (why there is no explicit lock): every method except :meth:`sync`
is SYNCHRONOUS — there is no ``await`` between reading and writing
``_last_ct`` — so two coroutines on the one runtime loop can never draw the
same ``ct``; the event loop serialises them. Arcus objects are loop-bound (the
hub enforces one loop), and this package never imports ``threading``.

DENIED ≠ EMPTY: a denied ``/v1/time`` leaves the offset and the last-sync
stamp untouched (never "skew 0"). A large but MEASURED skew is corrected by
the offset and never blocks a write (02 D2); only an opening with no recent
sync is refused, by the client.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Callable, Final, Protocol

from src.nadobro.utils.venue_scope import parse_arcus_net
from src.nadobro.venue.arcus.errors import Ok, ReadResult
from src.nadobro.venue.arcus.types import INT64_MAX, Lane

logger = logging.getLogger(__name__)

MAX_SYNC_RTT_MS: Final = 5_000  # a measurement with a longer round trip is discarded
SKEW_WARN_MS: Final = 10_000  # logged + exposed; never blocks writes (02 D2)
GTT_MIN_DAYS: Final = 32
GTT_MAX_DAYS: Final = 180
# Local "at least one month ahead" floor the client checks before sending
# (place-order: "Must be at least one month ahead of the current system timestamp").
GTT_MIN_AHEAD_US: Final = 31 * 86_400 * 1_000_000
_US_PER_DAY: Final = 86_400 * 1_000_000
_MIN_SERVER_NS: Final = 10**18  # "Current server time in nanoseconds since the Unix epoch"
_CT_KEYS_MAX: Final = 10_000


class TimeSource(Protocol):
    """What :meth:`ArcusClock.sync` needs from the REST client (``get_time``)."""

    async def get_time(self, *, lane: Lane, max_wait_s: float | None = None) -> ReadResult[int]: ...


class ArcusClock:
    """Per-network server-clock offset and per-API-key ``ct`` high-water marks."""

    def __init__(
        self,
        network: str,
        *,
        time_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.network = parse_arcus_net(network)
        self._time_ns = time_ns
        self._monotonic = monotonic
        self._offset_ns = 0
        self._skew_ms: float | None = None
        self._last_sync_mono: float | None = None
        self._last_rtt_ms: float | None = None
        self._last_ct: OrderedDict[str, int] = OrderedDict()
        self._gtt_clamp_warned = False

    async def sync(
        self, client: TimeSource, *, lane: Lane = Lane.L1_ENGINE, max_wait_s: float = 1.0
    ) -> float | None:
        """Measure the offset once. Returns the skew in ms, or None when the
        read was denied / implausible (state unchanged)."""
        t0 = self._time_ns()
        result = await client.get_time(lane=lane, max_wait_s=max_wait_s)
        t1 = self._time_ns()
        if not isinstance(result, Ok):
            return None
        server_ns = result.value
        if not isinstance(server_ns, int) or isinstance(server_ns, bool) or not (
            _MIN_SERVER_NS <= server_ns <= INT64_MAX
        ):
            logger.warning("arcus %s /v1/time returned an implausible value; ignored", self.network)
            return None
        rtt_ms = (t1 - t0) / 1e6
        if rtt_ms < 0 or rtt_ms > MAX_SYNC_RTT_MS:
            logger.warning(
                "arcus %s clock sync discarded (round trip %.0f ms)", self.network, rtt_ms
            )
            return None
        offset_ns = server_ns - (t0 + t1) // 2
        self._offset_ns = offset_ns
        self._last_sync_mono = self._monotonic()
        self._last_rtt_ms = rtt_ms
        skew_ms = abs(offset_ns) / 1e6
        self._skew_ms = skew_ms
        if skew_ms > SKEW_WARN_MS:
            logger.warning("arcus %s clock skew %.0f ms (corrected)", self.network, skew_ms)
        return skew_ms

    def skew_ms(self) -> float | None:
        """|server − local| in ms from the last successful sync; None before one."""
        return self._skew_ms

    def offset_ns(self) -> int:
        return self._offset_ns

    def last_sync_age_s(self) -> float | None:
        if self._last_sync_mono is None:
            return None
        return self._monotonic() - self._last_sync_mono

    def synced_within(self, max_age_s: float) -> bool:
        age = self.last_sync_age_s()
        return age is not None and age <= max_age_s

    def invalidate(self) -> None:
        """Forget WHEN we last synced (offset kept) — e.g. after a signed-write
        401, so the next opening re-syncs first."""
        self._last_sync_mono = None

    def next_ct_ns(self, api_key_hex: str) -> int:
        """SYNC, no await: the next ``ct`` for this API key, strictly greater
        than every ``ct`` this process issued for it."""
        if not isinstance(api_key_hex, str) or not api_key_hex:
            raise ValueError("api key required")
        now = self._time_ns() + self._offset_ns
        last = self._last_ct.get(api_key_hex)
        ct = now if last is None else max(now, last + 1)
        if not 0 < ct <= INT64_MAX:
            raise ValueError("ct out of range")
        self._last_ct[api_key_hex] = ct
        self._last_ct.move_to_end(api_key_hex)
        while len(self._last_ct) > _CT_KEYS_MAX:
            self._last_ct.popitem(last=False)
        return ct

    def now_us(self) -> int:
        """Server-corrected wall clock, epoch microseconds."""
        return (self._time_ns() + self._offset_ns) // 1000

    def gtt_us(self, days: int) -> int:
        """``goodTilTime`` = now + ``days`` (clamped to [32, 180], WARNING once),
        epoch µs (guides: "+ 40 * 86_400 * 1_000_000   # epoch µs, ≥1 month ahead")."""
        if not isinstance(days, int) or isinstance(days, bool):
            raise TypeError("days must be an int")
        clamped = max(GTT_MIN_DAYS, min(GTT_MAX_DAYS, days))
        if clamped != days and not self._gtt_clamp_warned:
            self._gtt_clamp_warned = True
            logger.warning("arcus gtt days %d clamped to %d", days, clamped)
        return self.now_us() + clamped * _US_PER_DAY

    def snapshot(self) -> dict[str, object]:
        return {
            "network": self.network,
            "offset_ms": self._offset_ns / 1e6,
            "skew_ms": self._skew_ms,
            "last_sync_age_s": self.last_sync_age_s(),
            "last_rtt_ms": self._last_rtt_ms,
        }


__all__ = [
    "MAX_SYNC_RTT_MS",
    "SKEW_WARN_MS",
    "GTT_MIN_DAYS",
    "GTT_MAX_DAYS",
    "GTT_MIN_AHEAD_US",
    "TimeSource",
    "ArcusClock",
]
