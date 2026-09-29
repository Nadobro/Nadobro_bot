"""Nado WebSocket v2 ACTION socket (`/ws/v2`).

This is the concurrent-dispatch executes/queries socket described at
https://docs.nado.xyz/developer-resources/api/gateway/websocket-v2 — it is NOT
the subscriptions socket (that is `nado_ws.py` / `/v1/subscribe`).

Why v2 for market making
------------------------
On a v1 connection the gateway processes requests strictly serially, so a taker
order sitting in its per-request send-rate guard blocks every post-only order
queued behind it on the same connection. v2 dispatches each request to the
engine in the background, so a post-only order sent right after a taker order
reaches the engine first. This lets a single connection carry mixed post-only +
taker flow without the post-only stream being penalised — removing the need to
segregate post-only and taker onto separate connections.

The catch: v2 responses arrive OUT OF ORDER. We therefore attach a unique
`id` to every supported execute and resolve the matching in-flight future by
that id. Per the docs, only `place_order`, `cancel_orders` and
`cancel_product_orders` echo `id` back; anything else (queries,
`cancel_and_place`, low-frequency executes) must be correlated by response
data or kept single-flight on a dedicated connection — so we deliberately only
expose the three id-correlatable hot-path executes here.

Payloads, EIP-712 signing and response shapes are IDENTICAL to v1 — only the URL
and the out-of-order correlation differ. The signed order body (sender,
priceX18, amount, expiration, nonce — including the low-20-bit client tag —,
appendix, signature) is still built by `nado_client`; this transport only
frames it, attaches the correlation `id`, sends it, and resolves by `id`.

Gated behind `NADO_WS_V2_ENABLED` (default off). REST / v1 remain the fallback
until this is validated on testnet.

Rate limits, authentication, subscriptions and the indexer are unchanged on v2.
A wallet may keep at most 5 authenticated websocket connections open, so size
any connection pool built on top of this with that ceiling in mind.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging

from src.nadobro.utils.env import env_bool, env_float
from src.nadobro.utils.venue_scope import STR_EXACT_ELSE_TESTNET, coerce_nado_network
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# v2 requires a websocket ping every 30 seconds to keep the connection alive.
_PING_INTERVAL_SECONDS = 30
_PING_TIMEOUT_SECONDS = 30
_DEFAULT_REQUEST_TIMEOUT_SECONDS = env_float("NADO_WS_V2_REQUEST_TIMEOUT_SECONDS", 5.0)

# Executes that echo `id` back in ExecuteResponse.id (per the WebSocket v2 docs).
# These are the only requests we route over this hot-path socket; everything
# else stays on REST / v1 or a dedicated single-flight connection.
_ID_CORRELATABLE = ("place_order", "cancel_orders", "cancel_product_orders")


def actions_url_for_network(network: str) -> str:
    """Concurrent-dispatch ACTION websocket endpoint (`/ws/v2`).

    IMPORTANT: this is the executes/queries socket, NOT `/v1/subscribe` (live
    data) and NOT `/v1/ws` (the serial v1 action socket).
    """
    # Legacy ``str(network) == "mainnet"`` → prod, else test (kept exactly);
    # an Arcus scope raises VenueScopeError.
    net = coerce_nado_network(network, STR_EXACT_ELSE_TESTNET, site="nado_ws_actions.actions_url_for_network")
    env = {"testnet": "test", "mainnet": "prod"}[net]
    return f"wss://gateway.{env}.nado.xyz/ws/v2"


def v2_enabled() -> bool:
    """Feature flag. v2 stays off until validated on testnet alongside v1."""
    return env_bool("NADO_WS_V2_ENABLED")


# ---------------------------------------------------------------------------
# Background event loop + sync bridge
#
# place_order runs SYNC inside a worker thread (run_blocking_sdk); cancel_orders
# runs on the main loop. Rather than special-case the caller context, every v2
# send is submitted to one dedicated daemon loop via run_coroutine_threadsafe.
# All sockets live on (and are only touched from) that loop, so their state is
# single-threaded and asyncio-safe regardless of which thread called in.
# ---------------------------------------------------------------------------
_bg_loop: "Optional[asyncio.AbstractEventLoop]" = None
_bg_lock = threading.Lock()
_sockets: "dict[str, NadoActionWsV2]" = {}


def _ensure_bg_loop() -> "asyncio.AbstractEventLoop":
    """Start (once) a daemon thread running a private event loop for v2 IO."""
    global _bg_loop
    with _bg_lock:
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, name="nado-ws-v2-loop", daemon=True)
        t.start()
        _bg_loop = loop
        return loop


async def _get_socket(network: str) -> "NadoActionWsV2":
    """Create/reuse one socket per network. Runs ON the bg loop."""
    sock = _sockets.get(network)
    if sock is None:
        sock = NadoActionWsV2(network)
        _sockets[network] = sock
    return sock


def send_execute_sync(
    network: str,
    execute_name: str,
    inner_body: "dict[str, Any]",
    *,
    timeout: "Optional[float]" = None,
) -> "dict[str, Any]":
    """Synchronous entry point usable from ANY thread.

    Submits the (already-signed) inner execute body to the per-network v2 socket
    on the background loop and blocks for the correlated response. Raises on
    transport failure so the caller can fall back to REST. ``inner_body`` is the
    unwrapped payload, e.g. the value of ``{"place_order": {...}}``.
    """
    loop = _ensure_bg_loop()

    async def _go() -> "dict[str, Any]":
        sock = await _get_socket(network)
        if execute_name == "place_order":
            return await sock.place_order(inner_body, timeout=timeout)
        if execute_name == "cancel_orders":
            return await sock.cancel_orders(inner_body, timeout=timeout)
        if execute_name == "cancel_product_orders":
            return await sock.cancel_product_orders(inner_body, timeout=timeout)
        raise ValueError(f"{execute_name} is not routable over the v2 hot-path")

    fut = asyncio.run_coroutine_threadsafe(_go(), loop)
    wait_s = (timeout if timeout is not None else _DEFAULT_REQUEST_TIMEOUT_SECONDS) + 2.0
    return fut.result(timeout=wait_s)


class NadoActionWsV2:
    """A single persistent `/ws/v2` connection with id-correlated requests.

    Correlation id layout: high 16 bits = connection index (so ids never
    collide across the up-to-5 connections a wallet may open), low 32 bits = a
    per-connection monotonic counter. The same value is also what we set as the
    order's request `id`; the durable cross-event identity remains the low
    20 bits of the order nonce (see `nado_client.place_order` / `order_tags`).
    """

    def __init__(self, network: str, conn_index: int = 0) -> None:
        self._network = network
        self._conn_index = conn_index & 0xFFFF
        self._seq = itertools.count(1)
        self._inflight: dict[int, asyncio.Future] = {}
        self._ws: Any = None
        self._reader: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        return (self._conn_index << 32) | (next(self._seq) & 0xFFFFFFFF)

    async def connect(self) -> None:
        import websockets

        async with self._lock:
            if self._ws is not None:
                return
            # `compression="deflate"` advertises permessage-deflate; the gateway
            # rejects subscriptions without it and it is harmless on the action
            # socket.
            self._ws = await websockets.connect(
                actions_url_for_network(self._network),
                ping_interval=_PING_INTERVAL_SECONDS,
                ping_timeout=_PING_TIMEOUT_SECONDS,
                compression="deflate",
            )
            self._reader = asyncio.create_task(
                self._read_loop(), name=f"nado-ws-v2-{self._network}-{self._conn_index}"
            )

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                if rid is None:
                    # Responses without id (queries, cancel_and_place, etc.) are
                    # not routed here — they belong on a dedicated single-flight
                    # connection. Drop with a debug log rather than guess.
                    logger.debug("ws v2 response without id dropped: %s", msg.get("request_type"))
                    continue
                fut = self._inflight.pop(rid, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reader must not crash silently
            logger.warning("ws v2 read loop ended for %s: %s", self._network, exc)
            self._fail_all_inflight(exc)

    def _fail_all_inflight(self, exc: BaseException) -> None:
        for fut in list(self._inflight.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._inflight.clear()

    async def _request(
        self, execute_name: str, inner_payload: dict[str, Any], *, timeout: Optional[float] = None
    ) -> dict[str, Any]:
        if execute_name not in _ID_CORRELATABLE:
            raise ValueError(
                f"{execute_name} is not id-correlatable; do not route it over the v2 hot-path socket"
            )
        if self._ws is None:
            await self.connect()

        rid = self._next_id()
        inner = dict(inner_payload)
        inner["id"] = rid
        payload = {execute_name: inner}

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight[rid] = fut
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(
                fut, timeout if timeout is not None else _DEFAULT_REQUEST_TIMEOUT_SECONDS
            )
        finally:
            self._inflight.pop(rid, None)

    async def place_order(self, order_payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        """`order_payload` is the fully-signed place_order body from nado_client."""
        return await self._request("place_order", order_payload, **kw)

    async def cancel_orders(self, cancel_payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return await self._request("cancel_orders", cancel_payload, **kw)

    async def cancel_product_orders(self, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return await self._request("cancel_product_orders", payload, **kw)

    async def close(self) -> None:
        async with self._lock:
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._reader = None
            if self._ws is not None:
                try:
                    await self._ws.close()
                finally:
                    self._ws = None
            self._fail_all_inflight(ConnectionError("ws v2 connection closed"))
