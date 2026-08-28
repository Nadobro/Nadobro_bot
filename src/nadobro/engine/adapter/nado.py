"""Patched: engine/adapter/nado.py

Fixes applied (search for AUDIT-FIX in this file):
  AUDIT-FIX-1: cancel_order() now inspects the dict returned by
               NadoClient.cancel_orders. The client swallows internal errors
               and returns {"success": False, ...} instead of raising, so the
               original code treated silent failures as successful cancels —
               which could LEAK OPEN ORDERS on the venue (fund-safety risk).
  AUDIT-FIX-2: order_status() now uses the real fills aggregate
               (filled_quote from _fills_for) for partially-filled resting
               orders. The original code did `filled_base * ref.price` which
               is wrong when fills happen at a different price than the
               original limit (e.g. better fills for makers, or fills across
               multiple price ticks).
  AUDIT-FIX-3: place_order() no longer silently ignores the `leverage`
               parameter. Nado sets leverage at account level, so a per-order
               leverage hint cannot actually change leverage on this venue.
               To avoid misleading callers, we now log a one-time warning if
               a caller passes leverage != 1 without configuring it through
               the proper account/isolated-margin path.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Optional, Sequence

from src.nadobro.utils.env import env_float
from src.nadobro.engine.adapter.base import (
    AdapterError,
    Fill,
    NadoAdapterBase,
    NadoOrder,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderState,
)
from src.nadobro.engine import order_lifecycle, order_tags
from src.nadobro.engine.types import OrderType, TradeType, _dec
from src.nadobro.utils.x18 import from_x18

# The sole permitted venue import inside the engine.
from src.nadobro.venue.nado_client import NadoClient
# Pure-math isolated-margin sizing shared with the manual trade path so both
# size an isolated-only leg identically.
from src.nadobro.quant.margin import compute_isolated_margin

logger = logging.getLogger(__name__)


# --- thread-pool routing ----------------------------------------------------
# Every blocking call below used to go through ``asyncio.to_thread``, i.e. the
# event loop's IMPLICIT default executor — ``min(32, cpu_count + 4)`` workers,
# which is FIVE on the production 1-CPU Fly VM. The engine is the heaviest
# blocking-IO consumer in the process (a grid tick issues open-orders, positions,
# balance, candles, depth and N placements, each a gateway call with a ~12s read
# timeout), and it shared those five threads with venue/nado_sync's per-user
# snapshot writes and market_data's snapshot gather. Saturation showed up in
# production as 45s strategy cycles, APScheduler "maximum number of running
# instances reached", and an 84s p-max on Telegram taps.
#
# Route the work to the purpose-built pools instead (see core/async_utils):
# ``_exec`` for placements/cancels, ``_sdk`` for gateway READS, ``_db`` for
# psycopg2. Execution keeps its own pool so a portfolio-poll storm can never
# queue in front of an order or a cancel — that isolation previously came from
# execution sitting on the default executor while polling used the SDK pool,
# which quietly stopped working when the VM went to 1 CPU. Imports are
# function-local on purpose: engine/ has no module-level edge to core/
# (tests/lint/test_architecture_layers.py) and lazy imports are exempt there.
def _exec(func, *args, **kwargs):
    """Await an order placement / cancel on the dedicated execution pool."""
    from src.nadobro.core.async_utils import run_blocking_exec

    return run_blocking_exec(func, *args, **kwargs)


def _sdk(func, *args, **kwargs):
    """Await a blocking SDK/gateway READ on the dedicated SDK thread pool."""
    from src.nadobro.core.async_utils import run_blocking_sdk

    return run_blocking_sdk(func, *args, **kwargs)


def _db(func, *args, **kwargs):
    """Await a blocking psycopg2 call on the dedicated DB thread pool."""
    from src.nadobro.core.async_utils import run_blocking_db

    return run_blocking_db(func, *args, **kwargs)


# --- venue response field maps (confirm via scripts/capture_nado_shapes.py) --
_DIGEST_KEYS = ("digest", "order_digest", "order_id", "id")
_OPEN_FILLED_KEYS = ("filled", "filled_size", "cum_filled_size", "executed_size", "filled_base")
_PRICE_KEYS = ("price", "limit_price", "fill_price", "exec_price")
_MATCH_AMOUNT_KEYS = ("amount", "size", "base_filled", "filled_size", "filled_base")
_MATCH_FEE_KEYS = ("fee", "fee_quote", "fee_usd", "fee_amount")
_BID_KEYS = ("bid", "best_bid", "bid_price")
_ASK_KEYS = ("ask", "best_ask", "ask_price")
_MID_KEYS = ("mid", "mid_price", "mark", "mark_price", "price")
_OPEN_LIST_KEYS = ("orders", "open_orders", "data", "result")
_REJECTED_STATES = ("rejected", "expired", "failed", "error")
_CANCELLED_STATES = ("cancelled", "canceled", "voided")
_FILLED_STATES = ("filled", "matched", "complete", "completed")

# order_status polls fetch the WHOLE product open-orders list, and a grid ticks
# order_status once PER LEVEL — so an N-level ladder made N identical
# get_open_orders (query_orders) gateway calls every tick, a top cause of the
# venue 429 storms (and worse now that grid/rgrid/dgrid are multi-level). Coalesce
# them: the first status poll for a product fetches the snapshot, the rest of that
# tick's polls reuse it. TTL is far below the strategy tick interval (30-60s) so a
# fill is at most this stale before the next tick's fresh fetch. Poll-only path
# (verify-after-cancel / reconcile stay uncached — they need post-mutation truth).
_OPEN_ORDERS_SNAP_TTL_S = env_float("NADO_OPEN_ORDERS_SNAP_TTL_SECONDS", 2.0)

# AUDIT-FIX-3: warn once per process per non-unit leverage so we don't spam logs.
_warned_leverage_set: set[int] = set()


@dataclass
class ProductMeta:
    product_id: int
    tick_size: Decimal
    lot_size: Decimal
    min_notional: Decimal
    # ``is_perp`` / ``isolated_only`` drive margin routing in place_order. Nado
    # RWA perps on testnet are isolated-margin only: an order on such a product
    # MUST carry isolated_only + an isolated_margin amount or the venue rejects
    # it (error_code 2006). Defaults keep every existing 4-arg construction
    # (spot / cross perps) behaving as before.
    is_perp: bool = False
    isolated_only: bool = False


@dataclass
class _OrderRef:
    trading_pair: str
    product_id: int
    side: TradeType
    order_type: OrderType
    amount_base: Decimal
    price: Optional[Decimal]

    def to_record(self) -> Dict[str, Any]:
        return {
            "trading_pair": self.trading_pair,
            "product_id": int(self.product_id),
            "side": self.side.value,
            "order_type": self.order_type.value,
            "amount_base": str(self.amount_base),
            "price": str(self.price) if self.price is not None else None,
        }

    @classmethod
    def from_record(cls, rec: Dict[str, Any]) -> "_OrderRef":
        return cls(
            trading_pair=str(rec["trading_pair"]),
            product_id=int(rec["product_id"]),
            side=TradeType(rec["side"]),
            order_type=OrderType(rec["order_type"]),
            amount_base=_dec(rec["amount_base"]),
            price=_dec(rec["price"]) if rec.get("price") is not None else None,
        )


class OrderRegistry:
    """Persistence hook for the adapter's digest->ref registry."""

    def record(self, order_id: str, ref: _OrderRef) -> None:  # noqa: ARG002
        return None

    def forget(self, order_id: str) -> None:  # noqa: ARG002
        return None

    def lookup(self, order_id: str) -> Optional[_OrderRef]:  # noqa: ARG002
        return None

    def all_ids(self) -> Iterable[str]:
        return ()


def _match_dec(value: object) -> Decimal:
    """Convert an indexer match/fill amount to a HUMAN Decimal.

    The Nado indexer returns fill fields (base_filled / quote_filled / fee /
    priceX18) x18-scaled (value × 1e18). Reading them raw recorded fills 1e18×
    too large — the bug that made the DN short un-placeable (base-matched off an
    x18 fill → astronomical notional) and the long un-closeable (selling 1e18×
    the held size → venue error_code 5000 "Invalid value"). Auto-detect so an
    already-human value is left untouched: a big integer (≥ 1e9, no decimal
    point) is treated as x18; anything else is taken as-is. Mirrors
    portfolio_calculator._decimal_from_possible_x18.
    """
    if value is None:
        return Decimal(0)
    text = str(value)
    if any(c in text for c in ".eE"):
        return _to_dec(value)
    try:
        integer = int(text)
    except (TypeError, ValueError):
        return _to_dec(value)
    if abs(integer) >= 1_000_000_000:
        return from_x18(integer)
    return Decimal(integer)


def _to_dec(value: object, default: Decimal = Decimal(0)) -> Decimal:
    try:
        return _dec(value)
    except Exception:
        return default


def _funding_row_epoch(row: Dict[str, Any]) -> Optional[float]:
    """Best-effort epoch-seconds for a funding payment row (the indexer feed
    keys it ``timestamp``; the synced DB row uses ``paid_at``). Tolerates
    millisecond timestamps."""
    raw = row.get("timestamp")
    if raw is None:
        raw = row.get("paid_at")
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v / 1000.0 if v > 1e11 else v


def _first(d: Dict[str, Any], keys: Sequence[str], default: object = None) -> object:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _as_list(resp: object) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in _OPEN_LIST_KEYS:
            v = resp.get(k)
            if isinstance(v, list):
                return v
    return []


def _client_call_succeeded(resp: Any) -> tuple[bool, str]:
    """AUDIT-FIX-1 helper.

    NadoClient methods catch internal exceptions and return a dict shaped like
    ``{"success": bool, "error": str, ...}``. Treat ``success != True`` as a
    real failure even though no exception was raised. Returns (ok, error_msg).
    """
    if isinstance(resp, dict):
        # Some upstream calls use the venue's raw response shape (no "success"
        # key). We only flag the call as failed when "success" is explicitly
        # falsy — silence means "treat as OK", which preserves backward
        # compatibility with venue endpoints that don't return a success flag.
        if "success" in resp and not resp.get("success"):
            return False, str(resp.get("error") or "venue returned success=False")
    return True, ""


def _trigger_digest(resp: object) -> str:
    """Pull the trigger order's digest from a ``place_entry_trigger_order`` /
    ``place_reduce_only_stop`` response so a later cancel can target exactly this
    trigger. The trigger-service payload nests the digest under ``response`` (and
    sometimes ``response.data``) rather than at the top level, so probe the same
    shapes ``strategy/venue_stop.py::_extract_digest`` handles. Returns ``''``
    when no digest is present (the caller then fails the placement loudly — a
    trigger we can't address is a trigger we can't cancel)."""
    if not isinstance(resp, dict):
        return ""
    inner = resp.get("response")
    for container in (resp, inner if isinstance(inner, dict) else {}):
        if not isinstance(container, dict):
            continue
        for key in _DIGEST_KEYS:
            v = container.get(key)
            if v:
                return str(v)
        data = container.get("data")
        if isinstance(data, dict):
            for key in _DIGEST_KEYS:
                v = data.get(key)
                if v:
                    return str(v)
    return ""


class NadoAdapter(NadoAdapterBase):
    connector_name = "nado"

    def __init__(
        self,
        client: NadoClient,
        products: Dict[str, ProductMeta],
        registry: Optional[OrderRegistry] = None,
        on_place: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._client = client
        self._products = products
        # Optional placement hook: called with the venue digest right after a
        # successful placement (single choke point for all engine orders). The
        # runtime wires this to link digest→session at placement so fill volume
        # is attributed from the venue sync regardless of executor fill detection.
        self._on_place = on_place
        self._orders: Dict[str, _OrderRef] = {}
        self._registry: OrderRegistry = registry or OrderRegistry()
        # Phase C: last authoritative status snapshot per digest +
        # (lifecycle change-seq seen when it was taken). Lets order_status skip
        # a gateway poll while the WS lifecycle says nothing changed.
        self._status_cache: Dict[str, tuple[NadoOrder, int]] = {}
        # Per-product open-orders snapshot for intra-tick coalescing (see
        # _OPEN_ORDERS_SNAP_TTL_S): product_id -> (monotonic_ts, orders).
        self._open_orders_snap: Dict[int, tuple[float, list]] = {}
        # SEPARATE registry for venue PRICE-TRIGGER orders (Reverse Grid rungs).
        # Kept apart from ``_orders`` on purpose: a trigger digest is cancelled
        # through the venue's trigger service (``cancel_trigger_order``), NOT the
        # resting-order path — mixing them would route a trigger cancel to the
        # wrong endpoint and leak the trigger. digest -> _OrderRef.
        self._trigger_orders: Dict[str, _OrderRef] = {}

    # -- product metadata -------------------------------------------------
    def _meta(self, trading_pair: str) -> ProductMeta:
        meta = self._products.get(trading_pair)
        if meta is None:
            raise AdapterError(f"Unknown trading pair: {trading_pair}")
        return meta

    def tick_size(self, trading_pair: str) -> Decimal:
        return self._meta(trading_pair).tick_size

    def lot_size(self, trading_pair: str) -> Decimal:
        return self._meta(trading_pair).lot_size

    def min_notional(self, trading_pair: str) -> Decimal:
        return self._meta(trading_pair).min_notional

    # -- orders -----------------------------------------------------------
    async def place_order(
        self,
        trading_pair: str,
        side: TradeType,
        order_type: OrderType,
        amount_base: Decimal,
        price: Optional[Decimal] = None,
        leverage: int = 1,
        reduce_only: bool = False,
    ) -> NadoOrder:
        meta = self._meta(trading_pair)
        is_buy = side is TradeType.BUY
        amount = float(amount_base)

        # reduce_only is a PERP concept (shrink an open position). On a SPOT
        # product there is no position to reduce — the venue rejects a
        # reduce-only spot order with error_code 5000 "Invalid value", which is
        # what broke the Delta Neutral spot leg's close/rollback. Strip it for
        # spot; the DN close sells exactly the held base, so it flattens cleanly
        # without the flag.
        # SPOT-CLOSE-BUMP: capture the REDUCING intent before the flag is
        # stripped. The venue's min-notional retry in NadoClient.place_order used
        # to grow ANY rejected order (`max(size, target)`), so a spot close sized
        # to exactly the held base — a $99 leg against KBTC's $100 minimum — was
        # bumped ABOVE the balance and could never fill, stranding the leg naked.
        # ``never_grow`` is the flag that survives the strip; reduce_only cannot,
        # and for spot there is nothing else that says "this is an exit".
        never_grow = bool(reduce_only)
        if reduce_only and not bool(meta.is_perp):
            reduce_only = False

        # SPOT-EXIT-GUARANTEE. A reducing SPOT sell is clamped to the balance the
        # VENUE reports, floored to the lot size, and finished as MARKET when the
        # remainder cannot clear the venue's min notional.
        #
        # Both halves are needed because engine inventory is not venue truth: in
        # the 2026-07-28 DN incident the controller's hold said 0.00155 while the
        # venue had filled 0.0031, so a size taken from inventory can be too HIGH
        # (rejected: insufficient balance) as easily as too low. And KBTC's $100
        # min notional sits ABOVE a $99 leg, so the exit is otherwise unfillable
        # as a resting limit no matter what size we ask for — market orders are
        # not subject to the resting minimum (the DN $98.88 market sell did fill).
        if never_grow and not bool(meta.is_perp) and side is TradeType.SELL:
            # (balance clamp is spot-only: a perp has no balance to clamp, and
            # reduce_only already prevents a perp close from over-closing.)
            held = await self.held_base(trading_pair)
            if held is not None:
                avail = abs(float(held))
                if avail <= 0:
                    raise AdapterError(
                        f"spot close on {trading_pair}: venue balance is 0 — "
                        f"nothing to sell (asked {amount})"
                    )
                if amount > avail:
                    # DECIMAL, not float: float(0.00155) / float(0.00005) is
                    # 30.999999... so int() floored a whole lot away and left
                    # ~$3 of dust stranded on every exit — the very failure this
                    # clamp exists to prevent.
                    _avail_d = abs(_dec(str(held)))
                    _lot_d = _dec(meta.lot_size or 0)
                    if _lot_d > 0:
                        _lots = (_avail_d / _lot_d).to_integral_value(rounding="ROUND_FLOOR")
                        clamped_d = _lots * _lot_d
                    else:
                        clamped_d = _avail_d
                    clamped = float(clamped_d)
                    lot = float(_lot_d)
                    if clamped <= 0:
                        raise AdapterError(
                            f"spot close on {trading_pair}: balance {avail} is "
                            f"below one lot ({lot}) — cannot be sold"
                        )
                    logger.warning(
                        "SPOT-EXIT clamp %s: asked %.10f > venue balance %.10f "
                        "-> selling %.10f (lot %s). Engine inventory disagreed "
                        "with the venue; the clamp keeps the exit fillable.",
                        trading_pair, amount, avail, clamped, lot,
                    )
                    amount = clamped
                    amount_base = clamped_d
        # EXIT-MIN-NOTIONAL ESCAPE — applies to SPOT *and* PERP.
        # A RESTING order below the venue minimum can never fill, and never_grow
        # (correctly) forbids growing a close to reach the floor. Without an escape
        # the exit is simply refused: audit round 4 found the perp path had none,
        # so a sub-minimum reduce-only perp close retried 3x and terminated the
        # executor FAILED with the position still open. Market orders are not
        # subject to the resting minimum, and reduce_only means a market close
        # cannot over-close, so crossing is the safe way out for both.
        if never_grow and order_type is not OrderType.MARKET:
            _min_notional = float(meta.min_notional or 0)
            if _min_notional > 0:
                _ref = float(price) if price else float(await self.mid_price(trading_pair))
                if _ref > 0 and amount * _ref < _min_notional:
                    logger.warning(
                        "EXIT-MIN-NOTIONAL %s (%s): exit notional %.2f is under the "
                        "venue minimum %.2f, so a resting limit can never fill — "
                        "crossing instead of stranding the position.",
                        trading_pair, "perp" if meta.is_perp else "spot",
                        amount * _ref, _min_notional,
                    )
                    order_type = OrderType.MARKET

        # Isolated-margin routing. Nado RWA perps are isolated-only: the order
        # must carry isolated_only=True and an isolated_margin amount or the
        # venue rejects it (error_code 2006). We mirror the manual trade path —
        # post the computed margin on BOTH opens and reduce-only closes; the
        # reduce_only appendix bit prevents a close from growing the position.
        # The shared helper applies the safety buffer (notional * 1.20 at 1x),
        # so signing exactly the bare initial margin can't trip account health.
        isolated_only = bool(meta.isolated_only)
        isolated_margin: Optional[float] = None
        if isolated_only:
            ref_price = float(price) if price is not None else float(await self.mid_price(trading_pair))
            isolated_margin = compute_isolated_margin(amount, ref_price, int(leverage) or 1)
            if isolated_margin is None:
                raise AdapterError(
                    f"could not size isolated margin for {trading_pair} "
                    f"(amount={amount}, price={ref_price}, leverage={leverage})"
                )
        elif leverage and int(leverage) != 1 and int(leverage) not in _warned_leverage_set:
            # Cross-margin perps: leverage is account-level on Nado, so a
            # per-order hint can't change it. Warn once (AUDIT-FIX-3). Isolated
            # products are handled above and DO consume leverage, so they no
            # longer hit this misleading warning.
            _warned_leverage_set.add(int(leverage))
            logger.warning(
                "place_order received leverage=%s on a cross-margin product but "
                "Nado sets cross leverage at the account level; this hint is "
                "ignored. Use an isolated-only product to size margin per order.",
                leverage,
            )

        # Phase B: tag every engine order with a unique 20-bit client_id so the
        # WS v2 order_update / fill streams (which echo it back as ``id``) can be
        # correlated to this controller / executor / grid level. The adapter is
        # the single choke point for engine orders, so auto-tagging here covers
        # all strategies without touching each executor.
        tag = order_tags.allocate_tag()
        order_tags.register(
            tag,
            trading_pair=trading_pair,
            product_id=meta.product_id,
            side=side.name,
            order_type=order_type.name,
            amount_base=str(amount_base),
            price=(str(price) if price is not None else None),
        )

        # Diagnostic: log the exact params we send so a venue rejection (e.g.
        # error_code 5000 "Invalid value") can be matched to the order shape and
        # compared against the working manual path.
        logger.info(
            "engine place_order pair=%s pid=%s side=%s type=%s amount_base=%s "
            "is_perp=%s isolated_only=%s isolated_margin=%s reduce_only=%s leverage=%s",
            trading_pair, meta.product_id, side.name, order_type.name, amount_base,
            meta.is_perp, isolated_only, isolated_margin, reduce_only, leverage,
        )
        try:
            if order_type is OrderType.MARKET:
                resp = await _exec(
                    self._client.place_market_order, meta.product_id, amount, is_buy,
                    isolated_only=isolated_only, isolated_margin=isolated_margin,
                    reduce_only=reduce_only, never_grow=never_grow, client_id=tag,
                )
            else:
                if price is None:
                    raise AdapterError("limit order requires a price")
                resp = await _exec(
                    self._client.place_limit_order, meta.product_id, amount, float(price), is_buy,
                    isolated_only=isolated_only, isolated_margin=isolated_margin,
                    post_only=order_type is OrderType.LIMIT_MAKER, reduce_only=reduce_only,
                    never_grow=never_grow, client_id=tag,
                )
        except AdapterError:
            order_tags.forget(tag=tag)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize venue errors
            order_tags.forget(tag=tag)
            raise AdapterError(f"place_order failed: {exc}") from exc

        # AUDIT-FIX-1: also fail loudly when the client returned a non-raising
        # error dict. Placing an order and silently getting a no-op back is a
        # fund-safety risk because the caller assumes the order is live.
        ok, err = _client_call_succeeded(resp)
        if not ok:
            order_tags.forget(tag=tag)
            raise AdapterError(f"place_order rejected by venue: {err}")

        order = self._order_from_response(resp, trading_pair, side, order_type, amount_base, price)
        # Link the venue digest to the tag so stream events keyed by EITHER the
        # client id (tag) OR the digest resolve back to this order's metadata.
        order_tags.bind_digest(tag, order.id)
        order_lifecycle.seed(order.id, state=order.state, tag=tag)
        # Placement hook: link this digest to the live session NOW (before any
        # fill/venue-sync) so volume attribution doesn't depend on the executor
        # detecting the fill. The hook does synchronous DB writes, so run it OFF
        # the event loop (a grid places many orders per tick — never block the
        # loop). Best-effort: a link failure must never fail a placed order.
        if self._on_place is not None:
            try:
                await _db(self._on_place, order.id)
            except Exception:  # noqa: BLE001 - placement link is best-effort
                logger.debug("on_place link failed for %s", order.id, exc_info=True)
        ref = _OrderRef(
            trading_pair, meta.product_id, side, order_type, amount_base, price
        )
        self._orders[order.id] = ref
        # Mutation: a new resting order changes the product's open-orders list,
        # so drop the coalesced snapshot.
        self._open_orders_snap.pop(int(meta.product_id), None)
        try:
            self._registry.record(order.id, ref)
        except Exception:  # noqa: BLE001 - persistence must not break placement
            logger.warning("order registry record failed for %s", order.id, exc_info=True)

        # Reconcile fills if the venue claims FILLED but didn't include sizes.
        if order.state is OrderState.FILLED and order.filled_base <= 0:
            try:
                fb, fq, fee = await self._fills_for(meta.product_id, order.id)
                if fb > 0:
                    order = NadoOrder(
                        id=order.id, trading_pair=trading_pair, side=side,
                        order_type=order_type, amount_base=amount_base, price=price,
                        state=order.state, filled_base=fb, filled_quote=fq, fee_quote=fee,
                    )
                else:
                    order = NadoOrder(
                        id=order.id, trading_pair=trading_pair, side=side,
                        order_type=order_type, amount_base=amount_base, price=price,
                        state=OrderState.PARTIALLY_FILLED,
                        filled_base=Decimal(0), filled_quote=Decimal(0), fee_quote=Decimal(0),
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "place_order: fills follow-up failed for %s; leaving state=PARTIAL",
                    order.id, exc_info=True,
                )
                order = NadoOrder(
                    id=order.id, trading_pair=trading_pair, side=side,
                    order_type=order_type, amount_base=amount_base, price=price,
                    state=OrderState.PARTIALLY_FILLED,
                    filled_base=Decimal(0), filled_quote=Decimal(0), fee_quote=Decimal(0),
                )
        return order

    async def cancel_and_place(
        self,
        cancel_order_id: str,
        trading_pair: str,
        side: TradeType,
        order_type: OrderType,
        amount_base: Decimal,
        price: Decimal,
        leverage: int = 1,
        reduce_only: bool = False,
    ) -> NadoOrder:
        """Atomically cancel ``cancel_order_id`` and place a fresh resting order.

        Mirrors :meth:`place_order`'s tagging, isolated-margin and registry
        bookkeeping for the NEW order, but leaves the OLD order's local
        bookkeeping ALONE: the caller settles the replaced executor first (to
        capture any fill that raced the cancel while its ref still resolves),
        then calls :meth:`forget_cancelled`. On ANY failure this raises
        ``AdapterError`` with the OLD order untouched on the venue, so the
        caller can fall back to cancel-then-place with no double-order risk.
        """
        if not cancel_order_id:
            raise AdapterError("cancel_and_place requires the resting order id")
        if price is None:
            raise AdapterError("cancel_and_place requires a price (resting order)")
        meta = self._meta(trading_pair)
        is_buy = side is TradeType.BUY
        amount = float(amount_base)

        # A replace is never a reducing close in the MM path, but keep the same
        # perp/spot reduce_only handling as place_order for correctness.
        never_grow = bool(reduce_only)
        if reduce_only and not bool(meta.is_perp):
            reduce_only = False

        isolated_only = bool(meta.isolated_only)
        isolated_margin: Optional[float] = None
        if isolated_only:
            isolated_margin = compute_isolated_margin(amount, float(price), int(leverage) or 1)
            if isolated_margin is None:
                raise AdapterError(
                    f"could not size isolated margin for {trading_pair} "
                    f"(amount={amount}, price={price}, leverage={leverage})"
                )

        tag = order_tags.allocate_tag()
        order_tags.register(
            tag,
            trading_pair=trading_pair,
            product_id=meta.product_id,
            side=side.name,
            order_type=order_type.name,
            amount_base=str(amount_base),
            price=str(price),
            replaces=str(cancel_order_id),
        )
        logger.info(
            "engine cancel_and_place pair=%s pid=%s cancel=%s side=%s type=%s "
            "amount_base=%s price=%s isolated_only=%s",
            trading_pair, meta.product_id, cancel_order_id, side.name,
            order_type.name, amount_base, price, isolated_only,
        )
        try:
            resp = await _exec(
                self._client.cancel_and_place,
                product_id=meta.product_id,
                cancel_digests=[cancel_order_id],
                size=amount,
                price=float(price),
                is_buy=is_buy,
                post_only=order_type is OrderType.LIMIT_MAKER,
                isolated_only=isolated_only,
                isolated_margin=isolated_margin,
                reduce_only=reduce_only,
                never_grow=never_grow,
                client_id=tag,
            )
        except Exception as exc:  # noqa: BLE001 - normalize; OLD order untouched
            order_tags.forget(tag=tag)
            raise AdapterError(f"cancel_and_place failed: {exc}") from exc

        ok, err = _client_call_succeeded(resp)
        if not ok:
            # Atomic failure: nothing placed, OLD order still resting. Forget the
            # NEW tag; the caller falls back to cancel-then-place.
            order_tags.forget(tag=tag)
            raise AdapterError(f"cancel_and_place rejected by venue: {err}")

        try:
            order = self._order_from_response(
                resp, trading_pair, side, order_type, amount_base, price
            )
        except Exception as exc:  # noqa: BLE001 - a malformed OK response must not leak the tag
            order_tags.forget(tag=tag)
            raise AdapterError(f"cancel_and_place response parse failed: {exc}") from exc
        order_tags.bind_digest(tag, order.id)
        order_lifecycle.seed(order.id, state=order.state, tag=tag)
        if self._on_place is not None:
            try:
                await _db(self._on_place, order.id)
            except Exception:  # noqa: BLE001 - placement link is best-effort
                logger.debug("on_place link failed for %s", order.id, exc_info=True)
        ref = _OrderRef(trading_pair, meta.product_id, side, order_type, amount_base, price)
        self._orders[order.id] = ref
        # The old resting order is gone and a new one arrived: the product's
        # open-orders list changed, so drop the coalesced snapshot.
        self._open_orders_snap.pop(int(meta.product_id), None)
        try:
            self._registry.record(order.id, ref)
        except Exception:  # noqa: BLE001 - persistence must not break placement
            logger.warning("order registry record failed for %s", order.id, exc_info=True)
        return order

    def forget_cancelled(self, order_id: str) -> None:
        """Local cleanup for an order the venue cancelled inside a
        cancel_and_place. No venue call — the cancel already happened."""
        if not order_id:
            return
        ref = self._orders.pop(order_id, None) or self._registry.lookup(order_id)
        order_tags.forget(digest=order_id)
        try:
            self._registry.forget(order_id)
        except Exception:  # noqa: BLE001 - best-effort
            logger.debug("registry forget failed for %s", order_id, exc_info=True)
        if ref is not None:
            self._open_orders_snap.pop(int(ref.product_id), None)

    # -- price-trigger orders (Reverse Grid rungs) ------------------------
    async def place_trigger_order(
        self,
        trading_pair: str,
        side: TradeType,
        amount_base: Decimal,
        trigger_price: Decimal,
        *,
        slippage_pct: float = 0.5,
        dependency: Optional[str] = None,
    ) -> NadoOrder:
        """Place a venue PRICE-TRIGGER entry rung (see the base-class contract).

        Thin wrapper over :meth:`NadoClient.place_entry_trigger_order`: the client
        owns the per-side trigger encoding (BUY→``mid_price_above`` + a +amount,
        SELL→``mid_price_below`` + a −amount), the through-the-level pricing, and
        the increment / min-notional alignment. The adapter maps the engine's
        ``TradeType`` to the client's ``direction_is_buy`` flag, records the
        trigger digest in the SEPARATE ``_trigger_orders`` registry, and returns a
        :class:`NadoOrder` whose ``id`` is that digest.

        Attribution is DUAL-PATH, so the run's turnover / realized PnL / History are
        captured whether or not the venue echoes the trigger digest on the fill:
          1. PRIMARY — the placement session-link hook (``_on_place``) is called with
             the trigger digest. A Nado price trigger IS the order (conditionally
             activated), so if its fill carries this digest ``venue/nado_sync`` links
             it to the run via ``_back_link_intent``, exactly as for a resting order.
          2. BACKSTOP — if the fill carries a DIFFERENT digest (the trigger spawns a
             fresh order id), ``nado_sync``'s product+session-window fallback
             (``_resolve_session_by_window``) still attributes the fill to the session
             that owns the product during the run, labelling it ``source='strategy'``;
             it is gated on ``not intent_found`` so a manual-tagged fill is never
             swallowed. So no fill is lost regardless of the venue's fire semantics.
        It is NOT tagged with an order_tags client id (that path is for the WS
        executor fill stream, which a trigger controller does not use).
        """
        meta = self._meta(trading_pair)
        is_buy = side is TradeType.BUY
        amount = abs(float(amount_base))
        trig = float(trigger_price)
        if amount <= 0:
            raise AdapterError("place_trigger_order requires a positive amount")
        if trig <= 0:
            raise AdapterError("place_trigger_order requires a positive trigger price")

        logger.info(
            "engine place_trigger_order pair=%s pid=%s side=%s amount_base=%s "
            "trigger_price=%s isolated_only=%s dependency=%s",
            trading_pair, meta.product_id, side.name, amount_base, trig,
            meta.isolated_only, dependency,
        )
        # The client method is ``async`` and offloads the blocking SDK call to the
        # execution pool itself (like place_reduce_only_stop / cancel_trigger_orders),
        # so AWAIT it directly — do NOT wrap it in _exec (which expects a sync fn).
        try:
            resp = await self._client.place_entry_trigger_order(
                product_id=meta.product_id,
                size=amount,
                trigger_price=trig,
                direction_is_buy=is_buy,
                slippage_pct=float(slippage_pct),
                isolated=bool(meta.isolated_only),
                dependency=dependency,
            )
        except Exception as exc:  # noqa: BLE001 - normalize venue errors
            raise AdapterError(f"place_trigger_order failed: {exc}") from exc

        ok, err = _client_call_succeeded(resp)
        if not ok:
            raise AdapterError(f"place_trigger_order rejected by venue: {err}")

        digest = _trigger_digest(resp)
        if not digest:
            # A trigger we cannot address is a trigger we cannot cancel — fail
            # loudly rather than leak an untracked rung onto the venue.
            raise AdapterError("venue did not return a trigger order digest")

        ref = _OrderRef(
            trading_pair, meta.product_id, side, OrderType.LIMIT,
            abs(_dec(amount_base)), _dec(trigger_price),
        )
        self._trigger_orders[digest] = ref
        try:
            self._registry.record(digest, ref)
        except Exception:  # noqa: BLE001 - persistence must not break placement
            logger.warning("trigger registry record failed for %s", digest, exc_info=True)
        # Link the trigger digest to the live session so its fill is attributed to
        # this run (turnover / realized PnL). Best-effort, off the event loop — a
        # link failure must never fail a placed trigger.
        await self._link_placement(digest)
        return NadoOrder(
            id=digest, trading_pair=trading_pair, side=side,
            order_type=OrderType.LIMIT, amount_base=abs(_dec(amount_base)),
            price=_dec(trigger_price), state=OrderState.OPEN,
        )

    async def cancel_trigger_order(self, order_id: str) -> bool:
        """Cancel a resting price-trigger by digest via the venue TRIGGER service
        (see the base-class contract). Idempotent: an unknown trigger returns
        ``False`` without a venue call."""
        if not order_id:
            return False
        ref = self._trigger_orders.get(order_id) or self._registry.lookup(order_id)
        if ref is None:
            # Not one of our tracked triggers — nothing to cancel, and we have no
            # product_id to target the trigger service with anyway.
            return False
        # ``cancel_trigger_orders`` is async and offloads to the execution pool
        # itself — await it directly (NOT via _exec).
        try:
            resp = await self._client.cancel_trigger_orders(
                product_id=ref.product_id, digests=[order_id],
            )
        except Exception as exc:  # noqa: BLE001 - venue raised
            raise AdapterError(
                f"cancel_trigger_order failed for {order_id}: {exc}"
            ) from exc
        ok, err = _client_call_succeeded(resp)
        if not ok:
            raise AdapterError(
                f"cancel_trigger_order rejected by venue for {order_id}: {err}"
            )
        self._trigger_orders.pop(order_id, None)
        try:
            self._registry.forget(order_id)
        except Exception:  # noqa: BLE001 - best-effort
            logger.debug("trigger registry forget failed for %s", order_id, exc_info=True)
        return True

    async def place_stop_order(
        self,
        trading_pair: str,
        close_size: Decimal,
        stop_price: Decimal,
        position_is_long: bool,
        *,
        slippage_pct: float = 0.5,
    ) -> NadoOrder:
        """Place a reduce-only protective/trailing stop (see the base-class
        contract). Thin wrapper over :meth:`NadoClient.place_reduce_only_stop`,
        which owns the per-side stop encoding and the through-the-stop pricing; the
        adapter records the stop digest in the SAME ``_trigger_orders`` registry as
        the entry rungs so :meth:`cancel_trigger_order` addresses it, and returns a
        :class:`NadoOrder` whose ``id`` is the digest and whose ``side`` is the
        CLOSE side (a long is closed by a SELL)."""
        meta = self._meta(trading_pair)
        size = abs(float(close_size))
        stop = float(stop_price)
        if size <= 0:
            raise AdapterError("place_stop_order requires a positive close size")
        if stop <= 0:
            raise AdapterError("place_stop_order requires a positive stop price")
        close_side = TradeType.SELL if position_is_long else TradeType.BUY
        logger.info(
            "engine place_stop_order pair=%s pid=%s close_side=%s close_size=%s "
            "stop_price=%s position_is_long=%s isolated_only=%s",
            trading_pair, meta.product_id, close_side.name, close_size, stop,
            position_is_long, meta.isolated_only,
        )
        # Async client method (offloads to the exec pool itself) — await directly.
        try:
            resp = await self._client.place_reduce_only_stop(
                product_id=meta.product_id,
                close_size=size,
                stop_price=stop,
                position_is_long=bool(position_is_long),
                slippage_pct=float(slippage_pct),
                isolated=bool(meta.isolated_only),
            )
        except Exception as exc:  # noqa: BLE001 - normalize venue errors
            raise AdapterError(f"place_stop_order failed: {exc}") from exc

        ok, err = _client_call_succeeded(resp)
        if not ok:
            raise AdapterError(f"place_stop_order rejected by venue: {err}")

        digest = _trigger_digest(resp)
        if not digest:
            raise AdapterError("venue did not return a stop order digest")

        ref = _OrderRef(
            trading_pair, meta.product_id, close_side, OrderType.LIMIT,
            abs(_dec(close_size)), _dec(stop_price),
        )
        self._trigger_orders[digest] = ref
        try:
            self._registry.record(digest, ref)
        except Exception:  # noqa: BLE001 - persistence must not break placement
            logger.warning("stop registry record failed for %s", digest, exc_info=True)
        # A reduce-only stop that fires books a CLOSE fill — link it so the run's
        # realized PnL and turnover include the exit, not just the entries.
        await self._link_placement(digest)
        return NadoOrder(
            id=digest, trading_pair=trading_pair, side=close_side,
            order_type=OrderType.LIMIT, amount_base=abs(_dec(close_size)),
            price=_dec(stop_price), state=OrderState.OPEN,
        )

    async def _link_placement(self, digest: str) -> None:
        """Best-effort placement→session link (off the event loop) for a trigger /
        stop digest, so a venue fill for it is attributed to the run. No-op when no
        ``_on_place`` hook is wired (e.g. a backtest / a non-session adapter)."""
        if self._on_place is None or not digest:
            return
        try:
            await _db(self._on_place, digest)
        except Exception:  # noqa: BLE001 - placement link is best-effort
            logger.debug("on_place link failed for %s", digest, exc_info=True)

    def _order_from_response(
        self, resp: object, trading_pair: str, side: TradeType, order_type: OrderType,
        amount_base: Decimal, price: Optional[Decimal],
    ) -> NadoOrder:
        data = resp if isinstance(resp, dict) else {}
        digest = str(_first(data, _DIGEST_KEYS, "") or "")
        if not digest:
            raise AdapterError("venue did not return an order id")
        filled_base = _to_dec(data.get("filled_base"))
        filled_quote = _to_dec(data.get("filled_quote"))
        fee_quote = _to_dec(_first(data, _MATCH_FEE_KEYS))
        raw_state = str(_first(data, ("status", "state"), "") or "").lower()

        if raw_state in _REJECTED_STATES:
            state = OrderState.REJECTED
        elif raw_state in _CANCELLED_STATES:
            state = OrderState.CANCELLED
        elif raw_state in _FILLED_STATES or order_type is OrderType.MARKET:
            state = OrderState.FILLED
        else:
            state = OrderState.OPEN

        return NadoOrder(
            id=digest, trading_pair=trading_pair, side=side, order_type=order_type,
            amount_base=amount_base, price=price, state=state,
            filled_base=filled_base, filled_quote=filled_quote, fee_quote=fee_quote,
        )

    async def cancel_order(self, order_id: str) -> bool:
        ref = self._orders.get(order_id) or self._registry.lookup(order_id)
        if ref is None:
            ref = await self._reconcile_order(order_id)
        if ref is None:
            return False
        self._orders[order_id] = ref
        # Mutation: drop the coalesced snapshot so any post-cancel re-poll (the
        # BUG-GR-1 capture-partial-fill probe) reads fresh, not a pre-cancel state.
        self._open_orders_snap.pop(int(ref.product_id), None)

        # AUDIT-FIX-1: NadoClient.cancel_orders catches internal SDK exceptions
        # and returns {"success": False, "error": "..."} instead of raising.
        # The previous version only wrapped the call in try/except, so a
        # silently-failed cancel was treated as success and the order stayed
        # open on the venue — leaking risk and producing ghost fills.
        try:
            resp = await self._client.cancel_orders(
                product_id=ref.product_id, digests=[order_id],
            )
        except Exception as exc:  # noqa: BLE001 - venue raised
            verified = await self._verify_no_longer_open(ref.product_id, order_id)
            if verified:
                self._registry.forget(order_id)
                self._orders.pop(order_id, None)
                return True
            raise AdapterError(f"cancel_order failed for {order_id}: {exc}") from exc

        ok, err = _client_call_succeeded(resp)
        if not ok:
            # Client returned success=False. Confirm with a status probe before
            # surfacing as failure: the cancel may have raced with a fill, in
            # which case the order is gone from the open book and we can treat
            # as successful.
            verified = await self._verify_no_longer_open(ref.product_id, order_id)
            if verified:
                self._registry.forget(order_id)
                self._orders.pop(order_id, None)
                return True
            raise AdapterError(
                f"cancel_order rejected by venue for {order_id}: {err}"
            )

        self._registry.forget(order_id)
        return True

    async def _verify_no_longer_open(self, product_id: int, order_id: str) -> bool:
        try:
            open_orders = await _sdk(self._client.get_open_orders, product_id, True)
        except Exception:  # noqa: BLE001
            return False
        return self._find_open(open_orders, order_id) is None

    async def order_status(self, order_id: str) -> NadoOrder:
        ref = self._orders.get(order_id) or self._registry.lookup(order_id)
        if ref is None:
            ref = await self._reconcile_order(order_id)
        if ref is None:
            raise AdapterError(f"unknown order id: {order_id}")
        self._orders[order_id] = ref

        # Phase C: WS-driven short-circuit. Return the last authoritative
        # snapshot WITHOUT a gateway poll when the lifecycle (local WS feed, or
        # the cross-process Redis mirror) proves it's still current. Amounts
        # always came from REST (below); the lifecycle only gates whether we
        # re-poll. No entry / stale ⇒ fall through to REST. One lifecycle read
        # (at most one Redis GET) per call.
        lc = order_lifecycle.get(order_id)
        cached = self._status_cache.get(order_id)
        if cached is not None:
            snap, seen_seq = cached
            # A terminal snapshot is permanent — never poll again.
            if snap.state.is_terminal:
                return snap
            if lc is not None and lc.fresh and lc.seq == seen_seq:
                return snap
            # A fresh WS event bumped the seq since our last snapshot (e.g. a
            # fill): capture the new amounts NOW — bypass the intra-tick
            # open-orders coalescing so we don't serve a pre-event snapshot shared
            # with sibling levels on this product.
            if lc is not None and lc.fresh and lc.seq != seen_seq:
                self._open_orders_snap.pop(int(ref.product_id), None)

        order = await self._order_status_rest(order_id, ref)
        self._status_cache[order_id] = (order, lc.seq if lc is not None else -1)
        return order

    async def _open_orders_coalesced(self, product_id: int) -> list:
        """get_open_orders(product_id) with a short intra-tick TTL so N per-level
        order_status polls in one tick share ONE gateway (query_orders) call.
        Only the read-only status-poll path uses this; mutation-verification
        paths call get_open_orders directly for post-cancel/place truth."""
        pid = int(product_id)
        now = time.monotonic()
        hit = self._open_orders_snap.get(pid)
        if hit is not None and (now - hit[0]) < _OPEN_ORDERS_SNAP_TTL_S:
            return hit[1]
        orders = await _sdk(self._client.get_open_orders, pid, True)
        orders = list(orders or [])
        self._open_orders_snap[pid] = (now, orders)
        return orders

    async def _order_status_rest(self, order_id: str, ref: _OrderRef) -> NadoOrder:
        try:
            open_orders = await self._open_orders_coalesced(ref.product_id)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"order_status failed: {exc}") from exc

        resting = self._find_open(open_orders, order_id)
        if resting is not None:
            # x18-scaled on the gateway open-orders feed — convert to human so a
            # resting/partial fill isn't recorded 1e18× too large.
            filled_base = abs(_match_dec(_first(resting, _OPEN_FILLED_KEYS)))
            state = OrderState.PARTIALLY_FILLED if filled_base > 0 else OrderState.OPEN
            # AUDIT-FIX-2: pull real quote and fees from the match aggregate.
            # Previously this used filled_base * ref.price, which assumes every
            # fill happened at the resting limit price — wrong for makers that
            # got a better fill or for resting orders that crossed multiple
            # ticks. With this fix the executor records the true quote / fee
            # delta into Inventory.
            if filled_base > 0:
                fb, fq, fee = await self._fills_for(ref.product_id, order_id)
                if fb > 0:
                    # The matches feed should agree with what's in the book; if
                    # there's drift, trust the matches feed (it's the source of
                    # truth for realized quote/fee).
                    filled_base = fb
                    return self._mk_order(order_id, ref, state, filled_base, fq, fee)
                # Fall back to the original (less-accurate) estimate only when
                # the matches feed has no data yet.
                px = ref.price if ref.price is not None else _match_dec(_first(resting, ("priceX18", "price_x18", *(_PRICE_KEYS))))
                return self._mk_order(order_id, ref, state, filled_base, filled_base * px, Decimal(0))
            return self._mk_order(order_id, ref, state, filled_base, Decimal(0), Decimal(0))

        # No longer resting -> aggregate fills for this digest.
        filled_base, filled_quote, fee = await self._fills_for(ref.product_id, order_id)
        lot = self._meta(ref.trading_pair).lot_size
        unfilled = ref.amount_base - filled_base
        if unfilled <= lot:
            state = OrderState.FILLED
        elif filled_base > 0:
            # The order is no longer in the open book, so any unfilled
            # remainder is terminal. Preserve the partial fill amounts while
            # reporting CANCELLED so executors can manage the inventory.
            state = OrderState.CANCELLED
        else:
            state = OrderState.CANCELLED
        return self._mk_order(order_id, ref, state, filled_base, filled_quote, fee)

    async def _reconcile_order(self, order_id: str) -> Optional[_OrderRef]:
        for pair, meta in self._products.items():
            try:
                open_orders = await _sdk(
                    self._client.get_open_orders, meta.product_id, True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "reconcile: get_open_orders failed for %s (skipping product): %s",
                    pair, exc,
                )
                continue
            resting = self._find_open(open_orders, order_id)
            if resting is None:
                continue
            try:
                is_buy = bool(resting.get("is_buy") if isinstance(resting, dict) else False)
            except Exception:  # noqa: BLE001
                is_buy = False
            side = TradeType.BUY if is_buy else TradeType.SELL
            price = _to_dec(_first(resting, _PRICE_KEYS)) if isinstance(resting, dict) else Decimal(0)
            amount = _to_dec(_first(resting, ("amount", "size", "amount_base"))) if isinstance(resting, dict) else Decimal(0)
            ref = _OrderRef(
                trading_pair=pair, product_id=meta.product_id, side=side,
                order_type=OrderType.LIMIT,
                amount_base=amount if amount > 0 else Decimal(1),
                price=price if price > 0 else None,
            )
            try:
                self._registry.record(order_id, ref)
            except Exception:  # noqa: BLE001
                logger.warning("order registry re-record failed for %s", order_id, exc_info=True)
            return ref
        return None

    def _mk_order(
        self, order_id: str, ref: _OrderRef, state: OrderState, filled_base: Decimal,
        filled_quote: Decimal, fee: Decimal,
    ) -> NadoOrder:
        return NadoOrder(
            id=order_id, trading_pair=ref.trading_pair, side=ref.side, order_type=ref.order_type,
            amount_base=ref.amount_base, price=ref.price, state=state,
            filled_base=filled_base, filled_quote=filled_quote, fee_quote=fee,
        )

    @staticmethod
    def _find_open(open_orders: object, digest: str) -> Optional[Dict[str, Any]]:
        for o in _as_list(open_orders):
            if isinstance(o, dict) and str(_first(o, _DIGEST_KEYS, "")) == digest:
                return o
        return None

    async def _fills_for(self, product_id: int, digest: str) -> tuple[Decimal, Decimal, Decimal]:
        try:
            matches = await self._client.get_matches(product_ids=[product_id])
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"order_status fills failed: {exc}") from exc
        fb = fq = fee = Decimal(0)
        for m in matches or []:
            if str(_first(m, _DIGEST_KEYS, "")) != digest:
                continue
            # Use the per-match FILL fields (base_filled / quote_filled), not
            # ``amount`` (the order's total, which over-counts on multi-match),
            # and convert from x18 to human units (_match_dec). Take abs because
            # the indexer signs them by direction.
            base = abs(_match_dec(_first(m, ("base_filled", "base_filled_x18", "amount", "size", "filled_base"))))
            quote = abs(_match_dec(_first(m, ("quote_filled", "quote_filled_x18"))))
            if quote <= 0:
                # Older shapes without quote_filled: derive from price × base.
                px = _match_dec(_first(m, ("priceX18", "price_x18", *(_PRICE_KEYS))))
                quote = base * px
            fb += base
            fq += quote
            fee += abs(_match_dec(_first(m, _MATCH_FEE_KEYS)))
        return fb, fq, fee

    async def fill_stream(self, trading_pair: str) -> AsyncIterator[Fill]:
        meta = self._meta(trading_pair)
        try:
            matches = await self._client.get_matches(product_ids=[meta.product_id])
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"fill_stream failed: {exc}") from exc
        for m in matches or []:
            # x18-scaled fill fields — convert to human (_match_dec).
            amt = abs(_match_dec(_first(m, ("base_filled", "base_filled_x18", "amount", "size", "filled_base"))))
            px = _match_dec(_first(m, ("priceX18", "price_x18", *(_PRICE_KEYS))))
            yield Fill(
                order_id=str(_first(m, _DIGEST_KEYS, "") or ""),
                trading_pair=trading_pair,
                side=TradeType.BUY if m.get("is_buy") else TradeType.SELL,
                amount_base=amt,
                price=px,
                fee_quote=abs(_match_dec(_first(m, _MATCH_FEE_KEYS))),
                timestamp=float(m.get("timestamp", time.time())),
            )

    # -- market data ------------------------------------------------------
    async def order_book(self, trading_pair: str) -> OrderBookSnapshot:
        meta = self._meta(trading_pair)
        try:
            data = await _sdk(self._client.get_market_price, meta.product_id)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"order_book failed: {exc}") from exc
        data = data or {}
        bid = _to_dec(_first(data, _BID_KEYS))
        ask = _to_dec(_first(data, _ASK_KEYS))
        if bid <= 0 and ask <= 0:
            mid = _to_dec(_first(data, _MID_KEYS))
            bid = ask = mid
        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=[OrderBookLevel(bid, Decimal(0))] if bid > 0 else [],
            asks=[OrderBookLevel(ask, Decimal(0))] if ask > 0 else [],
            timestamp=time.time(),
        )

    async def mid_price(self, trading_pair: str) -> Decimal:
        book = await self.order_book(trading_pair)
        mid = book.mid
        if mid is None:
            raise AdapterError(f"no mid price for {trading_pair}")
        return mid

    async def candles(
        self, trading_pair: str, timeframe: str = "1h", limit: int = 200
    ) -> list:
        meta = self._meta(trading_pair)
        try:
            data = await _sdk(
                self._client.get_candlesticks, meta.product_id, timeframe, limit
            )
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"candles failed: {exc}") from exc
        return list(data or [])

    async def depth_book(
        self, trading_pair: str, depth: int = 10
    ) -> OrderBookSnapshot:
        meta = self._meta(trading_pair)
        try:
            data = await _sdk(
                self._client.get_market_liquidity, meta.product_id, depth
            )
        except Exception as exc:  # noqa: BLE001  # policy: degrade-ok(depth is an enrichment; callers stay fail-open)
            logger.debug("depth_book failed %s: %s", trading_pair, exc)
            data = None
        data = data or {}

        def _levels(rows) -> list:
            out = []
            for row in rows or []:
                try:
                    price, amount = Decimal(str(row[0])), Decimal(str(row[1]))
                except (IndexError, TypeError, ValueError, InvalidOperation):
                    continue
                if price > 0 and amount > 0:
                    out.append(OrderBookLevel(price, amount))
            return out

        return OrderBookSnapshot(
            trading_pair=trading_pair,
            bids=_levels(data.get("bids")),
            asks=_levels(data.get("asks")),
            timestamp=float(data.get("timestamp") or time.time()),
        )

    async def held_base(self, trading_pair: str) -> Optional[Decimal]:
        """Venue truth for how much of ``trading_pair`` the account holds.

        SPOT -> the balance map from ``get_balance()`` keyed by product_id
        (verified live: ``{0: 164.48, 1: 0.00155…}`` where 1 is kBTC).
        PERP -> the signed size from ``get_all_positions()``.
        ``None`` on any read failure so callers fail SAFE instead of reading a
        broken call as "flat" and leaving a leg naked.
        """
        meta = self._meta(trading_pair)
        try:
            if bool(meta.is_perp):
                rows = await _sdk(self._client.get_all_positions)
                for row in (rows or []):
                    if not isinstance(row, dict):
                        continue
                    if int(row.get("product_id") or row.get("productId") or -1) != int(meta.product_id):
                        continue
                    # SIGN MATTERS. get_all_positions rows carry "amount" as the
                    # ABSOLUTE magnitude and the signed value under
                    # "signed_amount" (nado_client.py:1784-1797). Reading "amount"
                    # made a SHORT look positive, and DN derives the sweep SIDE
                    # from this sign — it would have SOLD MORE to "close" a short,
                    # doubling the position. Prefer the signed keys, and never
                    # fall back to an unsigned one.
                    raw = _first(row, ("signed_amount", "net_amount", "size"))
                    if raw is None:
                        _abs = _first(row, ("amount", "base_amount"))
                        if _abs is None:
                            return Decimal(0)
                        _side = str(row.get("side") or row.get("side_hint") or "").upper()
                        if _side not in ("LONG", "SHORT"):
                            logger.warning(
                                "held_base: perp row for %s has no signed amount and "
                                "no usable side — refusing to guess the sign", trading_pair,
                            )
                            return None
                        _mag = abs(_to_dec(_abs))
                        return _mag if _side == "LONG" else -_mag
                    return _to_dec(raw)
                return Decimal(0)
            # force=True: get_balance is read-through cached (30s, no invalidation on
            # a fill) and bot_runtime warms it on the START path — i.e. PRE-BUY. A
            # stale map lacking this product read as Decimal(0) and the clamp then
            # REFUSED a legitimate exit. Audit round 3. Cost is bounded: this runs
            # once per close, not per tick.
            data = await _sdk(self._client.get_balance, force=True)
            # get_balance does NOT raise on failure: a gateway-budget throttle or a
            # total SDK+REST failure both return {"exists": False, "balances": {}}.
            # Treating that as "flat" broke the documented None-on-failure contract
            # and made the clamp REFUSE a legitimate exit (avail <= 0 -> raise).
            if not data or data.get("exists") is False:
                logger.warning(
                    "held_base: balance read for %s returned no account data — "
                    "reporting UNKNOWN, not flat", trading_pair,
                )
                return None
            balances = data.get("balances")
            if not isinstance(balances, dict) or not balances:
                return None
            for key, amount in balances.items():
                try:
                    if int(key) == int(meta.product_id):
                        # A PRESENT key with 0 is genuinely flat.
                        return _to_dec(amount)
                except (TypeError, ValueError):
                    continue
            # ABSENT key = UNKNOWN, not flat (audit round 4). get_balance returns
            # an entry for every product it saw — including explicit 0.0 — so a
            # missing product means this snapshot cannot answer. Returning 0 here
            # made the exit clamp raise "balance is 0 — nothing to sell" and refuse
            # a legitimate close.
            logger.warning(
                "held_base: %s (product_id=%s) absent from the balance snapshot — "
                "reporting UNKNOWN, not flat", trading_pair, meta.product_id,
            )
            return None
        except Exception as exc:  # noqa: BLE001 - unknown, NOT flat
            logger.warning("held_base read failed for %s: %s", trading_pair, exc)
            return None

    async def funding_rate(self, trading_pair: str) -> Optional[Decimal]:
        meta = self._meta(trading_pair)
        try:
            data = await _sdk(self._client.get_funding_rate, meta.product_id)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"funding_rate failed: {exc}") from exc
        if data is None:
            return None
        raw = _first(data, ("rate", "funding_rate", "funding", "hourly_funding")) if isinstance(data, dict) else data
        return _to_dec(raw) if raw is not None else None

    async def funding_since(self, trading_pair: str, since_ts: float) -> Decimal:
        """Net funding RECEIVED on the perp since ``since_ts`` (positive = the
        short collected funding). Pulls the user-scoped indexer funding feed and
        sums the product's payments. The indexer's amount is signed with
        positive = funding *paid* by the user, so we negate to report
        received-positive."""
        meta = self._meta(trading_pair)
        try:
            rows = await self._client.get_interest_and_funding_payments(
                product_ids=[meta.product_id]
            )
        except Exception as exc:  # noqa: BLE001 - normalize venue errors
            raise AdapterError(f"funding_since failed: {exc}") from exc
        from src.nadobro.quant.portfolio_calculator import funding_payment_amount

        paid_total = Decimal(0)
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("type") or "funding") != "funding":
                continue
            pid = row.get("product_id")
            if pid is not None:
                try:
                    if int(pid) != int(meta.product_id):
                        continue
                except (TypeError, ValueError):
                    pass
            ts = _funding_row_epoch(row)
            # DN-FUNDING-WINDOW fix: skip rows we can't date (ts is None). They
            # were previously summed regardless of the run window, leaking
            # pre-run funding into the run total and overstating funding earned.
            if ts is None or ts < float(since_ts):
                continue
            paid_total += funding_payment_amount(row)
        return -paid_total
