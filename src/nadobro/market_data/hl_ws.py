"""Hyperliquid public market-data WebSocket — the signal feed for Mid mode.

Why this exists
---------------
Nado publishes **no public book or trade websocket**. Its four streams
(``order_update``, ``fill``, ``position_change``, ``funding_payment``) are
account-scoped, and ``/ws/v2`` is the concurrent-dispatch ACTION socket, not a
data feed. Nado's book and tape are therefore poll-only, which floors every
microstructure signal at the strategy cadence (3-8s) and spends query budget
that scales with the number of products.

Hyperliquid publishes exactly what Nado withholds, pushed and free:

    l2Book        levels [[bids],[asks]] of {px, sz, n}   (n = orders at level)
    trades        {coin, side, px, sz, hash, time}        (aggressor side!)
    activeAssetCtx  {funding, openInterest, markPx, oraclePx, midPx,
                     impactPxs, premium, dayNtlVlm}
    allMids       {COIN: price}

THE BOUNDARY (do not cross)
---------------------------
This feed supplies the **forecast** — direction, regime, volatility, flow.
It must NEVER price a Nado quote. Our orders rest in *Nado's* book, so the
quote anchor stays Nado's own depth: post-only that crosses is rejected (not
converted), and quoting HL's fair value on Nado hands the basis to cross-venue
arbitrageurs. ``snapshot()`` returns a book in the same shape as
``nado_client.get_market_liquidity`` precisely so the pure math in ``quant/``
runs on either — but the *caller* decides which is anchor and which is signal.

One connection, process-wide
----------------------------
HL market data is public and identical for every user, so a single connection
serves every Nadobro user: 7 majors x 3 subscriptions + allMids = 22, against a
per-IP cap of 1000 subscriptions / 10 connections. A per-user design would
multiply that by the user count for no benefit. This is the whole economic
argument for the feed, so the singleton is load-bearing, not an optimisation.

Staleness is a READ-side property
---------------------------------
Every datum stamps its arrival time and ``snapshot()`` returns ``None`` once it
ages past ``max_age_s``. A caller cannot forget to check freshness, and a frozen
socket degrades to "no data" rather than to confidently wrong data. Consumers
must fail open: no HL snapshot means anchor-only quoting, never a blocked tick.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from typing import Any, Callable, Deque, Iterable, Optional

from src.nadobro.core.ipv4_egress import websocket_connect_kwargs
from src.nadobro.utils.env import env_bool, env_float, env_int, env_str

logger = logging.getLogger(__name__)

HL_WS_URL = env_str("NADO_HL_WS_URL", "wss://api.hyperliquid.xyz/ws")

# Per-coin subscriptions we open. ``allMids`` is global and subscribed once.
_COIN_STREAMS = ("l2Book", "trades", "activeAssetCtx")

# HL closes idle connections; the official SDK pings on a 50s timer
# (``Event.wait(50)`` — seconds). Protocol-level pings are configured too, but
# the app-level ping is what the venue documents.
_PING_SECONDS = env_float("NADO_HL_WS_PING_SECONDS", 50.0)

# Default freshness bound for a read. Deliberately short: these feeds push on
# every book/trade event, so silence means the socket is sick, not that the
# market is quiet.
_DEFAULT_MAX_AGE_S = env_float("NADO_HL_MAX_AGE_SECONDS", 10.0)

# Bounded per-coin tape. ~2k trades is minutes of history on a major and caps
# memory on a burst.
_TRADE_RING = env_int("NADO_HL_TRADE_RING", 2000)

# Kill switch: off => the listener never starts and every read returns None,
# so Mid degrades to anchor-only quoting.
def enabled() -> bool:
    return env_bool("NADO_HL_FEED_ENABLED", True)


# --- process-wide state -----------------------------------------------------
# Keyed by HL coin symbol (e.g. "BTC"). Each entry carries its own arrival
# timestamp so one stalled stream cannot make the others look fresh.
_books: dict[str, dict] = {}
_ctxs: dict[str, dict] = {}
_mids: dict[str, dict] = {}
_trades: dict[str, Deque[dict]] = {}
_trade_keys: dict[str, Deque[tuple]] = {}
_trade_key_set: dict[str, set] = {}

_book_listeners: list = []
_trade_listeners: list = []
_mids_listeners: list = []


def register_mids_listener(callback: Callable[[str], None]) -> None:
    """Register ``callback("")`` fired on every allMids update. Used to
    reconcile per-coin subscriptions against what HL actually lists.

    Register a BOUND METHOD, never a fresh closure: dedupe is by equality, and
    two lambdas built from the same source are never equal, so a closure
    registered per instance accumulates forever and every allMids frame fans
    out to objects nobody uses any more.
    """
    if callback not in _mids_listeners:
        _mids_listeners.append(callback)


def unregister_mids_listener(callback: Callable[[str], None]) -> None:
    """Drop a previously registered allMids listener. Idempotent."""
    try:
        _mids_listeners.remove(callback)
    except ValueError:
        pass


def register_book_listener(callback: Callable[[str], None]) -> None:
    """Register ``callback(coin)`` fired on every l2Book update."""
    if callback not in _book_listeners:
        _book_listeners.append(callback)


def register_trade_listener(callback: Callable[[str], None]) -> None:
    """Register ``callback(coin)`` fired on every trades batch."""
    if callback not in _trade_listeners:
        _trade_listeners.append(callback)


def _notify(listeners: list, coin: str) -> None:
    for cb in list(listeners):
        try:
            cb(str(coin))
        except Exception:  # noqa: BLE001 - a listener bug must not kill the stream
            logger.debug("hl listener failed coin=%s", coin, exc_info=True)


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


# --- parsing ----------------------------------------------------------------

def _parse_levels(raw: Any) -> list:
    """HL level -> ``[price, size]``, matching the Nado depth shape so the pure
    microstructure math is venue-agnostic. ``n`` (orders at level) is dropped
    here; add a richer accessor if a signal ever needs it."""
    out: list = []
    for lvl in raw or []:
        if isinstance(lvl, dict):
            px, sz = _f(lvl.get("px")), _f(lvl.get("sz"))
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            px, sz = _f(lvl[0]), _f(lvl[1])
        else:
            continue
        if px is not None and sz is not None and px > 0 and sz > 0:
            out.append([px, sz])
    return out


def _on_l2book(data: dict) -> None:
    coin = str(data.get("coin") or "").upper().strip()
    if not coin:
        return
    levels = data.get("levels") or []
    bids = _parse_levels(levels[0] if len(levels) > 0 else [])
    asks = _parse_levels(levels[1] if len(levels) > 1 else [])
    # Best-first, matching nado_client.get_market_liquidity.
    bids.sort(key=lambda r: -r[0])
    asks.sort(key=lambda r: r[0])
    _books[coin] = {
        "bids": bids,
        "asks": asks,
        "timestamp": (_f(data.get("time")) or 0.0) / 1000.0,
        "received_at": time.time(),
    }
    _notify(_book_listeners, coin)


def _trade_key(t: dict) -> tuple:
    return (t.get("hash"), t.get("time"), t.get("px"), t.get("sz"), t.get("side"))


def _on_trades(rows: Any) -> None:
    if not isinstance(rows, list):
        return
    touched: set = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        coin = str(row.get("coin") or "").upper().strip()
        px, sz = _f(row.get("px")), _f(row.get("sz"))
        if not coin or px is None or sz is None or px <= 0 or sz <= 0:
            continue
        # Dedupe: a reconnect can replay recent trades, and double-counting
        # them doubles every volume and imbalance figure downstream.
        key = _trade_key(row)
        seen = _trade_key_set.setdefault(coin, set())
        if key in seen:
            continue
        keys = _trade_keys.setdefault(coin, deque(maxlen=_TRADE_RING))
        if len(keys) == keys.maxlen and keys:
            seen.discard(keys[0])
        keys.append(key)
        seen.add(key)

        ring = _trades.setdefault(coin, deque(maxlen=_TRADE_RING))
        ring.append({
            "px": px,
            "sz": sz,
            # NOTE: `side` semantics (aggressor vs resting maker) and encoding
            # are UNVERIFIED against a live socket. Stored raw; any signal that
            # derives a sign from it must self-check (see the plan's open items)
            # and disable itself rather than trade backwards.
            "side": row.get("side"),
            "ts": (_f(row.get("time")) or 0.0) / 1000.0,
            "received_at": time.time(),
        })
        touched.add(coin)
    for coin in touched:
        _notify(_trade_listeners, coin)


def _on_ctx(data: dict) -> None:
    coin = str(data.get("coin") or "").upper().strip()
    if not coin:
        return
    ctx = data.get("ctx") if isinstance(data.get("ctx"), dict) else data
    _ctxs[coin] = {
        "funding": _f(ctx.get("funding")),
        "open_interest": _f(ctx.get("openInterest")),
        "mark_px": _f(ctx.get("markPx")),
        "oracle_px": _f(ctx.get("oraclePx")),
        "mid_px": _f(ctx.get("midPx")),
        "premium": _f(ctx.get("premium")),
        "day_ntl_vlm": _f(ctx.get("dayNtlVlm")),
        "received_at": time.time(),
    }


def _on_all_mids(data: dict) -> None:
    mids = data.get("mids") if isinstance(data.get("mids"), dict) else {}
    now = time.time()
    for coin, px in mids.items():
        value = _f(px)
        if value is not None and value > 0:
            _mids[str(coin).upper().strip()] = {"mid": value, "received_at": now}
    if mids:
        _notify(_mids_listeners, "")


# One-shot per-channel shape log. The wire format here is coded from the
# official SDK's type definitions, but could not be probed against a live
# socket from the build environment (egress policy blocks the venue hosts).
# Logging the first frame of each channel at INFO makes the very first real
# deployment answer the open questions — subscribe-envelope acceptance, the
# `side` encoding, and the actual l2Book depth — instead of leaving them to
# guesswork. Cheap: at most one line per channel per process.
_shape_logged: set = set()


def _log_shape_once(channel: str, event: dict) -> None:
    if channel in _shape_logged:
        return
    _shape_logged.add(channel)
    try:
        logger.info("hl ws first frame channel=%s shape=%s", channel, json.dumps(event)[:600])
    except (TypeError, ValueError):
        logger.info("hl ws first frame channel=%s (unserialisable)", channel)


def _dispatch(event: dict) -> None:
    channel = str(event.get("channel") or "")
    data = event.get("data")
    _log_shape_once(channel, event)
    if channel == "l2Book" and isinstance(data, dict):
        _on_l2book(data)
    elif channel == "trades":
        _on_trades(data)
    elif channel == "activeAssetCtx" and isinstance(data, dict):
        _on_ctx(data)
    elif channel == "allMids" and isinstance(data, dict):
        _on_all_mids(data)
    elif channel in ("subscriptionResponse", "pong", "error"):
        if channel == "error":
            logger.warning("hl ws error frame: %s", str(data)[:300])
        else:
            logger.debug("hl ws control frame: %s", channel)
    else:
        logger.debug("hl ws unhandled channel=%r", channel)


# --- reads (staleness enforced here) ----------------------------------------

def _fresh(entry: Optional[dict], max_age_s: Optional[float]) -> Optional[dict]:
    if not entry:
        return None
    age_bound = _DEFAULT_MAX_AGE_S if max_age_s is None else max_age_s
    if age_bound > 0 and (time.time() - float(entry.get("received_at") or 0.0)) > age_bound:
        return None
    return entry


def book(coin: str, *, max_age_s: Optional[float] = None) -> Optional[dict]:
    """Sized book in the Nado depth shape, or ``None`` when stale/absent."""
    return _fresh(_books.get(str(coin).upper().strip()), max_age_s)


def ctx(coin: str, *, max_age_s: Optional[float] = None) -> Optional[dict]:
    """funding / open interest / mark / oracle / premium, or ``None``."""
    return _fresh(_ctxs.get(str(coin).upper().strip()), max_age_s)


def mid(coin: str, *, max_age_s: Optional[float] = None) -> Optional[float]:
    """Best available HL mid: the book's own mid, else ``allMids``."""
    bk = book(coin, max_age_s=max_age_s)
    if bk and bk["bids"] and bk["asks"]:
        return (bk["bids"][0][0] + bk["asks"][0][0]) / 2.0
    entry = _fresh(_mids.get(str(coin).upper().strip()), max_age_s)
    return float(entry["mid"]) if entry else None


def recent_trades(coin: str, *, window_s: float = 60.0) -> list:
    """Trades within ``window_s``, oldest-first. Empty when absent or stale."""
    ring = _trades.get(str(coin).upper().strip())
    if not ring:
        return []
    cutoff = time.time() - max(0.0, float(window_s))
    return [t for t in ring if float(t.get("received_at") or 0.0) >= cutoff]


def snapshot(coin: str, *, max_age_s: Optional[float] = None) -> Optional[dict]:
    """Everything known about ``coin`` right now, or ``None`` if the book is
    stale — the book is the load-bearing datum, so its staleness governs."""
    bk = book(coin, max_age_s=max_age_s)
    if bk is None:
        return None
    return {
        "coin": str(coin).upper().strip(),
        "book": bk,
        "ctx": ctx(coin, max_age_s=max_age_s),
        "mid": mid(coin, max_age_s=max_age_s),
    }


def health() -> dict:
    """Feed liveness for logging/telemetry — never used to gate a trade."""
    now = time.time()

    def _age(store: dict) -> dict:
        return {
            c: round(now - float(v.get("received_at") or 0.0), 2)
            for c, v in store.items()
        }

    return {
        "enabled": enabled(),
        "coins": sorted(_books),
        "book_age_s": _age(_books),
        "ctx_age_s": _age(_ctxs),
        "trade_counts": {c: len(r) for c, r in _trades.items()},
    }


def reset_state() -> None:
    """Drop all cached feed state. Tests and reconnect-hygiene only."""
    for store in (_books, _ctxs, _mids, _trades, _trade_keys, _trade_key_set):
        store.clear()


# --- the listener -----------------------------------------------------------

class HyperliquidWs:
    """One process-wide connection to HL's public market-data socket.

    Reconnect/backoff, the listener registry and task lifecycle mirror
    ``venue/nado_ws.py``; the difference is that this holds a SINGLE task for
    all coins rather than one per user, because the data is public.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        # What callers asked for (Nado product bases) vs what we actually
        # subscribed. They differ because HL does not list Nado's equity/RWA
        # markets, and subscribing to a coin HL does not have just earns error
        # frames — so we reconcile against ``allMids`` before subscribing.
        self._desired: set = set()
        self._active: set = set()
        self._ws: Any = None

    def _on_mids_update(self, _coin: str) -> None:
        """allMids arrived -> some desired coin may now be known to be listed.

        A bound method, and registered in ``start()`` rather than ``__init__``:
        registering a per-instance lambda at construction time made the dedupe
        in ``register_mids_listener`` unreachable, so every instance ever built
        stayed alive in the module-global list.
        """
        self._schedule_reconcile()

    def subscribe_coins(self, coins: Iterable[str]) -> None:
        """Register interest in ``coins`` (Nado product bases). Idempotent.

        Nothing is subscribed until ``allMids`` confirms HL lists the coin, so
        an equity/RWA that HL does not carry is silently skipped rather than
        erroring — the caller then simply never gets a snapshot for it.
        """
        self._desired |= {str(c).upper().strip() for c in coins if str(c).strip()}
        self._schedule_reconcile()

    def covered(self) -> set:
        """Desired coins HL actually lists (empty until allMids arrives)."""
        return {c for c in self._desired if c in _mids}

    def _schedule_reconcile(self) -> None:
        if self._ws is None:
            return  # the next connect resubscribes from scratch
        # Strong reference: a bare create_task can be garbage-collected
        # mid-flight. Tolerate being called off-loop at boot.
        from src.nadobro.core.async_utils import fire_and_forget

        try:
            fire_and_forget(self._reconcile_subs(), name="hl-reconcile-subs")
        except RuntimeError:
            logger.debug("hl subscribe deferred to next connect")

    async def _reconcile_subs(self) -> None:
        for coin in sorted(self.covered() - self._active):
            await self._subscribe_coin(coin)
            self._active.add(coin)

    def start(self) -> None:
        if not enabled():
            logger.info("hl feed disabled (NADO_HL_FEED_ENABLED=false)")
            return
        if self._task and not self._task.done():
            return
        register_mids_listener(self._on_mids_update)
        self._task = asyncio.create_task(self._run(), name="hl-market-data-ws")

    async def stop(self) -> None:
        unregister_mids_listener(self._on_mids_update)
        task, self._task = self._task, None
        self._ws = None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            return
        await ws.send(json.dumps(payload))

    async def _subscribe_coin(self, coin: str) -> None:
        try:
            for stream in _COIN_STREAMS:
                await self._send({
                    "method": "subscribe",
                    "subscription": {"type": stream, "coin": coin},
                })
        except Exception:  # noqa: BLE001 - the reconnect loop owns recovery
            logger.debug("hl subscribe failed coin=%s", coin, exc_info=True)

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("hl ws disconnected: %s", exc)
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.2))
                backoff = min(60.0, backoff * 2)
            finally:
                self._ws = None

    async def _connect_once(self) -> None:
        import websockets

        async with websockets.connect(
            HL_WS_URL,
            ping_interval=20,
            ping_timeout=20,
            **websocket_connect_kwargs(),
        ) as ws:
            self._ws = ws
            self._active = set()
            logger.info("hl ws connected desired=%s", sorted(self._desired))
            # allMids first: it tells us which desired coins HL actually lists,
            # and its arrival drives per-coin subscription via the reconcile
            # listener. Anything already known from a previous connect is
            # resubscribed immediately.
            await self._send({"method": "subscribe", "subscription": {"type": "allMids"}})
            await self._reconcile_subs()

            ping = asyncio.create_task(self._ping_loop(), name="hl-ws-ping")
            try:
                async for raw in ws:
                    try:
                        event = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                    except (TypeError, ValueError):
                        continue
                    if isinstance(event, dict):
                        _dispatch(event)
            finally:
                ping.cancel()
                await asyncio.gather(ping, return_exceptions=True)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(_PING_SECONDS)
            try:
                await self._send({"method": "ping"})
            except Exception:  # noqa: BLE001 - the read loop will see the drop
                return


hl_ws = HyperliquidWs()
