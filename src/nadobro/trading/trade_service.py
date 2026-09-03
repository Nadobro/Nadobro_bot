import logging
import os
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from src.nadobro.models.database import (
    TradeStatus, OrderSide, OrderTypeEnum, NetworkMode,
    get_last_trade_for_rate_limit, insert_trade, update_trade, get_trades_by_user,
    find_open_trade, insert_fill_sync, get_trade_by_id,
)
from src.nadobro.config import (
    get_dn_pair,
    get_product_id,
    get_spot_product_id,
    get_spot_metadata,
    get_product_name,
    get_product_max_leverage,
    get_perp_products,
    is_product_isolated_only,
    RATE_LIMIT_SECONDS,
    MIN_TRADE_SIZE_USD,
    get_nado_builder_routing_config,
)
from src.nadobro.users.user_service import get_user, get_user_nado_client, get_user_readonly_client, update_trade_stats, ensure_active_wallet_ready
from src.nadobro.venue.nado_archive import query_order_by_digest
from src.nadobro.venue.product_catalog import is_product_id_isolated_only
from src.nadobro.venue.nado_tooling_service import get_account_snapshot

logger = logging.getLogger(__name__)
_submit_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nadobro-submit")


# Isolated-margin sizing now lives in services/margin.py so the engine adapter
# and this manual path compute it identically. These thin wrappers preserve the
# existing private call sites in this module.
def _isolated_margin_safety_multiplier() -> float:
    from src.nadobro.quant.margin import isolated_margin_safety_multiplier

    return isolated_margin_safety_multiplier()


def _compute_isolated_margin(size: float, price: float, leverage: float) -> float | None:
    from src.nadobro.quant.margin import compute_isolated_margin

    return compute_isolated_margin(size, price, leverage)


def _order_submit_timeout_seconds() -> float:
    raw = (os.environ.get("NADO_ORDER_SUBMIT_TIMEOUT_SECONDS") or "25").strip()
    try:
        val = float(raw)
    except Exception:
        return 25.0
    return max(5.0, val)


_SUBMIT_UNKNOWN_PREFIX = "Order submit status unknown"


def _unknown_submit_retry_window_seconds() -> float:
    raw = (os.environ.get("NADO_UNKNOWN_SUBMIT_RETRY_WINDOW_SECONDS") or "120").strip()
    try:
        val = float(raw)
    except Exception:
        return 120.0
    return max(30.0, val)


def _submit_unknown_message(detail: object) -> str:
    return (
        f"{_SUBMIT_UNKNOWN_PREFIX}: {detail}. "
        "The venue may still accept this order; check Portfolio before retrying."
    )


def _parse_trade_timestamp(value) -> float | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _recent_unknown_market_submit(
    telegram_id: int,
    *,
    network: str,
    product_id: int,
    side: str,
    size: float,
) -> dict | None:
    """Find a recent market submit whose venue result is still ambiguous."""
    try:
        trades = get_trades_by_user(telegram_id, limit=25, network=network) or []
    except Exception:
        return None
    now_ts = time.time()
    window_s = _unknown_submit_retry_window_seconds()
    for trade in trades:
        try:
            if int(trade.get("product_id") or 0) != int(product_id):
                continue
            if str(trade.get("status") or "").lower() != TradeStatus.PENDING.value:
                continue
            if str(trade.get("order_type") or "").lower() != OrderTypeEnum.MARKET.value:
                continue
            if str(trade.get("side") or "").lower() != str(side).lower():
                continue
            if abs(float(trade.get("size") or 0) - float(size)) > 1e-10:
                continue
            if not str(trade.get("error_message") or "").startswith(_SUBMIT_UNKNOWN_PREFIX):
                continue
            created_ts = _parse_trade_timestamp(trade.get("created_at"))
            if created_ts is None or now_ts - created_ts > window_s:
                continue
            return trade
        except Exception:  # policy: degrade-ok(dup-scan row malformed; skipped)
            continue
    return None


def _submit_with_timeout(func, *args, timeout_s: float, **kwargs):
    fut = _submit_pool.submit(func, *args, **kwargs)
    try:
        return True, fut.result(timeout=timeout_s)
    except FutureTimeoutError:
        fut.cancel()
        return False, f"Order submit timed out after {timeout_s:.0f}s"


def _trade_ts_display(val) -> str:
    if not val:
        return ""
    if isinstance(val, datetime):
        return val.isoformat()[:19]
    s = str(val)
    return s[:19] if len(s) >= 19 else s


def _builder_route_payload(network: str | None = None) -> dict:
    """Builder routing payload for trade metadata.

    Pass ``network`` so testnet trades bypass the mainnet-only builder
    registry and never fail with ``error_code=2118 "Invalid builder"``.
    """
    try:
        builder_id, builder_fee_rate = get_nado_builder_routing_config(network)
        return {
            "builder_id": int(builder_id),
            "builder_fee_rate": int(builder_fee_rate),
        }
    except Exception as e:
        logger.warning("Builder routing misconfigured on %s: %s", network, e)
        return {"error": "Order routing is temporarily misconfigured. Please try again shortly."}


def _resolve_user_network(telegram_id: int, explicit: str | None = None) -> str:
    if explicit:
        return str(explicit)
    try:
        user = get_user(telegram_id)
    except Exception:
        user = None
    return str(user.network_mode.value if user else "mainnet")


def _resolve_fill_data(client, digest: str, network: str) -> dict | None:
    """Query Nado archive for actual fill data after order placement."""
    try:
        # IOC fills can take a moment to appear in the indexer; wait long enough to match Nado.
        return query_order_by_digest(network, digest, max_wait_seconds=5.0, poll_interval=0.35)
    except Exception as e:
        logger.warning("Archive fill resolution failed for %s: %s", digest[:16] if digest else "?", e)
        return None


def _resolve_fill_data_retry(network: str, digest: str) -> dict | None:
    """Second-chance archive poll (e.g. after first path returned partial)."""
    if not digest:
        return None
    try:
        return query_order_by_digest(network, digest, max_wait_seconds=3.0, poll_interval=0.4)
    except Exception:
        return None


def _enqueue_fill_sync(trade_id: int, network: str, user_id: int, client, digest: str, product_id: int):
    """Enqueue a trade for background fill sync when inline resolution fails."""
    try:
        subaccount_hex = getattr(client, "subaccount_hex", None) or ""
        insert_fill_sync({
            "trade_id": trade_id,
            "network": network,
            "user_id": user_id,
            "subaccount_hex": subaccount_hex,
            "order_digest": digest,
            "product_id": product_id,
            "placed_at_ts": time.time(),
        })
    except Exception as e:
        logger.warning("Failed to enqueue fill sync for trade %s: %s", trade_id, e)


def _clear_portfolio_caches(user_id: int, network: str) -> None:
    try:
        from src.nadobro.venue.nado_sync import clear_cache as clear_nado_sync_cache
        from src.nadobro.portfolio.portfolio_service import clear_portfolio_snapshot_cache

        clear_nado_sync_cache(int(user_id), network)
        clear_portfolio_snapshot_cache(int(user_id), network)
    except Exception:
        logger.debug("Failed to clear portfolio caches user=%s network=%s", user_id, network, exc_info=True)


def _build_fill_update(fill_data: dict | None, mid_price: float = 0.0) -> dict:
    """Build trade update dict from fill data, with market-mid fallback."""
    if fill_data and fill_data.get("fill_price", 0) > 0:
        total_fee = float(fill_data.get("fee", 0) or 0.0) + float(fill_data.get("builder_fee", 0) or 0.0)
        update = {
            "fill_price": fill_data["fill_price"],
            "price": fill_data["fill_price"],
            "fill_size": fill_data.get("fill_size", 0),
            "fill_fee": total_fee,
            "builder_fee": float(fill_data.get("builder_fee", 0) or 0.0),
            "fees": total_fee,
            "realized_pnl": fill_data.get("realized_pnl", 0),
            "is_taker": fill_data.get("is_taker", False),
        }
        if mid_price > 0 and fill_data["fill_price"] > 0:
            update["slippage_bps"] = abs(fill_data["fill_price"] - mid_price) / mid_price * 10000
        return update
    return {"price": mid_price} if mid_price > 0 else {}


def _is_close_order_type(order_type: str | None) -> bool:
    return "CLOSE" in str(order_type or "").upper()


def _parse_trade_event_ts(raw) -> datetime | None:
    if not raw:
        return None
    dt: datetime | None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        # Postgres rows often come back tz-naive; treat as UTC so we can subtract
        # from tz-aware ``datetime.now(timezone.utc)`` without TypeError.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _normalize_trade_side(raw: str | None) -> str:
    side = str(raw or "").strip().upper()
    if side in ("BUY", "LONG"):
        return "LONG"
    if side in ("SELL", "SHORT"):
        return "SHORT"
    return side or "UNKNOWN"


def _trade_total_fee_value(trade: dict) -> float:
    if not isinstance(trade, dict):
        return 0.0
    explicit = trade.get("fees")
    if explicit is not None:
        try:
            return float(explicit or 0.0)
        except Exception:  # policy: degrade-ok(malformed fee field; falls back to computed total)
            pass
    return float(trade.get("fill_fee") or 0.0) + float(trade.get("builder_fee") or 0.0)


def _trade_notional_usd(trade: dict) -> float:
    size = float(trade.get("fill_size") or trade.get("size") or 0.0)
    price = float(trade.get("fill_price") or trade.get("price") or 0.0)
    return max(0.0, size * price)


def _is_close_trade_row(trade: dict) -> bool:
    if not isinstance(trade, dict):
        return False
    if trade.get("open_trade_id"):
        return True
    if _is_close_order_type(trade.get("order_type")):
        return True
    if str(trade.get("source") or "").strip().lower() == "vol":
        realized = float(trade.get("realized_pnl") or trade.get("pnl") or 0.0)
        if abs(realized) > 1e-12:
            return True
    return False


def _trade_event_time_for_windows(trade: dict) -> datetime | None:
    return (
        _parse_trade_event_ts(trade.get("filled_at"))
        or _parse_trade_event_ts(trade.get("closed_at"))
        or _parse_trade_event_ts(trade.get("created_at"))
    )


def _build_logical_trade_rows(trades: list[dict]) -> list[dict]:
    logical_rows: list[dict] = []
    logical_index_by_open_trade_id: dict[int, int] = {}
    unmatched_by_product: dict[str, list[int]] = {}

    def _product_key(row: dict) -> str:
        return str(row.get("product_name") or row.get("product") or "").upper()

    def _row_ts(row: dict) -> tuple[int, str]:
        dt = _trade_event_time_for_windows(row)
        return (int(dt.timestamp()) if dt else 0, str(row.get("created_at") or ""))

    for row in sorted(trades or [], key=_row_ts):
        product_key = _product_key(row)
        normalized_side = _normalize_trade_side(row.get("side"))
        unmatched_candidates = unmatched_by_product.get(product_key, [])
        vol_pair_candidate = (
            str(row.get("source") or "").strip().lower() == "vol"
            and any(_normalize_trade_side(logical_rows[idx].get("side")) != normalized_side for idx in unmatched_candidates)
        )
        if _is_close_trade_row(row) or vol_pair_candidate:
            close_price = row.get("close_price") or row.get("fill_price") or row.get("price")
            close_dt = row.get("closed_at") or row.get("filled_at") or row.get("created_at")
            linked_index = None
            linked_open_trade_id = row.get("open_trade_id")
            if linked_open_trade_id:
                linked_index = logical_index_by_open_trade_id.get(int(linked_open_trade_id))
            if linked_index is None:
                for idx in reversed(unmatched_candidates):
                    base = logical_rows[idx]
                    if _normalize_trade_side(base.get("side")) != normalized_side:
                        linked_index = idx
                        break
            if linked_index is not None:
                base = logical_rows[linked_index]
                base["close_price"] = close_price or base.get("close_price")
                base["closed_at"] = close_dt or base.get("closed_at")
                base["status"] = TradeStatus.CLOSED.value
                if (base.get("pnl") is None or abs(float(base.get("pnl") or 0.0)) <= 1e-12):
                    close_pnl = row.get("pnl")
                    if close_pnl is None:
                        close_pnl = row.get("realized_pnl")
                    if close_pnl is not None:
                        base["pnl"] = close_pnl
                base["fees"] = float(base.get("fees") or 0.0) + _trade_total_fee_value(row)
                base["funding_paid"] = float(base.get("funding_paid") or 0.0) + float(row.get("funding_paid") or 0.0)
                unmatched = unmatched_by_product.get(product_key, [])
                if linked_index in unmatched:
                    unmatched.remove(linked_index)
                continue

        logical_row = {
            "id": row.get("id"),
            "product": row.get("product_name"),
            "type": row.get("order_type"),
            "side": normalized_side,
            "size": float(row.get("fill_size") or row.get("size") or 0.0),
            "price": row.get("fill_price") or row.get("price"),
            "leverage": float(row.get("leverage") or 1),
            "status": row.get("status"),
            "pnl": row.get("pnl") if row.get("pnl") is not None else row.get("realized_pnl"),
            "close_price": row.get("close_price"),
            "network": row.get("network"),
            "created_at": _trade_ts_display(row.get("created_at")),
            "closed_at": _trade_ts_display(row.get("closed_at")),
            "filled_at": _trade_ts_display(row.get("filled_at")),
            "digest": row.get("order_digest"),
            "fees": _trade_total_fee_value(row),
            "funding_paid": float(row.get("funding_paid") or 0.0),
            "is_taker": row.get("is_taker"),
            "open_trade_id": row.get("open_trade_id"),
        }
        logical_rows.append(logical_row)
        new_idx = len(logical_rows) - 1
        if not _is_close_trade_row(row) and str(row.get("status") or "").lower() != TradeStatus.CLOSED.value:
            try:
                logical_index_by_open_trade_id[int(row.get("id"))] = new_idx
            except Exception:  # policy: degrade-ok(history pairing index is display-only)
                pass
            unmatched_by_product.setdefault(product_key, []).append(new_idx)

    logical_rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return logical_rows


def _order_sender_params(client, network: str) -> list[str | None]:
    """
    Subaccounts that may hold perp orders: None = default subaccount, else isolated child hex.
    """
    from src.nadobro.venue.nado_archive import isolated_subaccount_from_row, query_isolated_subaccounts_for_parent

    out: list[str | None] = [None]
    try:
        for row in query_isolated_subaccounts_for_parent(network, client.subaccount_hex or "") or []:
            iso = isolated_subaccount_from_row(row, client.subaccount_hex or "")
            if iso:
                out.append(iso)
    except Exception as e:
        logger.debug("order_sender_params isolated list failed: %s", e)
    return out


def _net_abs_for_subaccount(positions: list, product_id: int, subaccount: str | None) -> tuple[float, int]:
    """Residual size and direction (+1 long / -1 short) for product_id on a margin subaccount."""
    hint = (subaccount or "").strip().lower()
    for p in positions or []:
        try:
            if int(p.get("product_id", -1)) != int(product_id):
                continue
        except Exception:  # policy: degrade-ok(malformed position row; skipped)
            continue
        sk = _position_subaccount_key(p)
        if hint:
            if sk != hint:
                continue
        else:
            if sk:
                continue
        sa = float(p.get("signed_amount") or 0)
        if sa == 0.0:
            amt = float(p.get("amount", 0) or 0)
            side = str(p.get("side", "") or "").upper()
            sa = amt if side == "LONG" else (-amt if side == "SHORT" else 0.0)
        if abs(sa) <= 0:
            continue
        return abs(float(sa)), (1 if sa > 0 else -1)
    return 0.0, 0


_UNKNOWN_BOOK = "open-orders read unavailable"


def _book_unknown(errors: list[str]) -> bool:
    """True when the resting-order sweep could not READ the book (as opposed to
    read it and fail to cancel something). DENIED-vs-EMPTY: an unreadable book
    must never be treated as an empty one."""
    return any(str(e).startswith(_UNKNOWN_BOOK) for e in errors)


def _resting_orders_fallback(
    client, network: str, only_pid: int | None, known_pids, errors: list[str]
) -> tuple[dict[tuple[int, str | None], list[str]], list[str]]:
    """Per-product, per-sender single reads (weight 2 each) for the products
    that can hold an order, used when the whole-account batched read is
    UNKNOWN. ``refresh=True`` so a denied single read is ``None`` (unknown)
    rather than a stale cache or ``[]``."""
    grouped: dict[tuple[int, str | None], list[str]] = {}
    pids = [int(only_pid)] if only_pid is not None else sorted({int(p) for p in (known_pids or [])})
    if not pids:
        errors.append(f"{_UNKNOWN_BOOK} and no known products to fall back on")
        return grouped, errors
    try:
        senders = _order_sender_params(client, network)
    except Exception as exc:  # policy: degrade-ok(sender enumeration failed; a parent-only fallback still reads the default subaccount and any unreadable sender is reported below)
        logger.debug("sender enumeration failed during cancel fallback: %s", exc)
        senders = [None]
    for pid in pids:
        for sender in senders:
            try:
                rows = client.get_open_orders(pid, refresh=True, sender=sender)
            except Exception as exc:  # policy: degrade-ok(a raised single read is recorded as an UNKNOWN book for that product/sender, which fails the close loud below)
                rows = None
                logger.debug("fallback open-orders read raised pid=%s sender=%s: %s", pid, sender, exc)
            if rows is None:
                errors.append(f"{_UNKNOWN_BOOK} for {get_product_name(pid)} (sender={str(sender or 'default')[:10]})")
                continue
            for row in rows:
                digest = str(row.get("digest") or "").strip()
                if digest:
                    grouped.setdefault((pid, sender), []).append(digest)
    return grouped, errors


def _resting_orders_by_sender(
    client, only_pid: int | None = None, *, known_pids=None, network: str = "mainnet"
) -> tuple[dict[tuple[int, str | None], list[str]], list[str]]:
    """Every resting order as ``{(product_id, sender): [digests]}`` in ONE
    gateway call per sender.

    CLOSE-ALL-SWEEP (prod 2026-07-31): the close paths probed
    ``products x senders`` with a single-product ``get_open_orders`` each — for
    one open BTC position that is 90 serial round-trips BEFORE the close order
    is even sent, and 90 more to verify it, so a tap took minutes while holding
    a worker (measured; see tests/services/test_close_all_sweep.py). The venue
    exposes a batched multi-product read, which ``get_all_open_orders`` already
    uses for the portfolio poller — same senders (both derive from
    ``query_isolated_subaccounts_for_parent``), same coverage, 1 call each.
    ``sender`` is ``None`` for the parent subaccount, matching
    ``_order_sender_params`` and ``cancel_order(sender=...)``.

    DENIED-vs-EMPTY (prod 2026-09-03, session 312): ``get_all_open_orders``
    returns ``None`` when the book could not be read — and for any user with an
    isolated child subaccount that was EVERY call (the 168-weight parent read
    drained the per-user bucket, the child read timed out). ``None or []`` here
    turned that into "empty book": cancelled nothing, reported success, and the
    session rail sent "TP hit" while the asks kept filling. An unknown book now
    falls back to per-product single reads on every sender, and whatever stays
    unreadable is reported as an error the caller must fail loud on.
    """
    grouped: dict[tuple[int, str | None], list[str]] = {}
    errors: list[str] = []
    try:
        rows = client.get_all_open_orders()
    except Exception as e:
        rows = None
        errors.append(f"{_UNKNOWN_BOOK} (batched read raised: {e})")
    if rows is None:
        return _resting_orders_fallback(client, network, only_pid, known_pids, errors)
    for row in rows:
        try:
            pid = int(row.get("product_id"))
        except (TypeError, ValueError):
            continue
        if only_pid is not None and pid != int(only_pid):
            continue
        digest = str(row.get("digest") or "").strip()
        if not digest:
            continue
        sender = row.get("subaccount") or None
        grouped.setdefault((pid, sender), []).append(digest)
    return grouped, errors


def _cancel_resting_orders(
    client, grouped: dict[tuple[int, str | None], list[str]]
) -> tuple[int, list[str]]:
    """Cancel every digest in ``grouped``. Only touches products that actually
    hold an order, so a flat book costs zero execute round-trips."""
    cancelled = 0
    errors: list[str] = []
    for (pid, sender), digests in grouped.items():
        for digest in digests:
            try:
                r = client.cancel_order(pid, digest, sender=sender)
                if r.get("success"):
                    cancelled += 1
                else:
                    errors.append(f"{get_product_name(pid)}: cancel failed ({r.get('error', 'unknown')})")
            except Exception as e:
                errors.append(f"{get_product_name(pid)}: cancel exception ({e})")
    return cancelled, errors


def _cancel_open_orders_for_product(
    client, product_id: int, sender: str | None = None
) -> tuple[int, list[str]]:
    cancelled = 0
    errors: list[str] = []
    try:
        open_orders = client.get_open_orders(product_id, refresh=True, sender=sender)
    except Exception as e:
        return 0, [f"{get_product_name(product_id)}: open-orders lookup failed ({e})"]
    if open_orders is None:                      # DENIED-vs-EMPTY: unknown, not empty
        return 0, [f"{_UNKNOWN_BOOK} for {get_product_name(product_id)}"]
    for order in open_orders:
        digest = order.get("digest")
        if not digest:
            continue
        try:
            r = client.cancel_order(product_id, digest, sender=sender)
            if r.get("success"):
                cancelled += 1
            else:
                errors.append(f"{get_product_name(product_id)}: cancel failed ({r.get('error', 'unknown')})")
        except Exception as e:
            errors.append(f"{get_product_name(product_id)}: cancel exception ({e})")
    return cancelled, errors


def check_rate_limit(telegram_id: int, network: str = "mainnet") -> tuple[bool, str]:
    last_trade = get_last_trade_for_rate_limit(telegram_id, network=network)
    if last_trade and last_trade.get("created_at"):
        elapsed = None
        try:
            raw = last_trade["created_at"]
            if isinstance(raw, str):
                created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                created = raw
            if created.tzinfo:
                now = datetime.now(created.tzinfo)
            else:
                now = datetime.now(timezone.utc)
            elapsed = (now - created).total_seconds()
        except Exception as e:
            logger.warning("Rate-limit timestamp parse failed for user %s: %s", telegram_id, e)
            return True, ""
        if elapsed < 0:
            elapsed = 0
        if elapsed < RATE_LIMIT_SECONDS:
            remaining = int(RATE_LIMIT_SECONDS - elapsed)
            return False, f"Rate limit: wait {remaining}s before next trade."
    return True, ""


def validate_trade(
    telegram_id: int,
    product: str,
    size: float,
    leverage: float = 1.0,
    enforce_rate_limit: bool = True,
    reduce_only: bool = False,
    network: str | None = None,
) -> tuple[bool, str]:
    user_obj = get_user(telegram_id)
    selected_network = str(network or (user_obj.network_mode.value if user_obj else "mainnet"))
    client = get_user_readonly_client(telegram_id, network=selected_network)
    product_id = get_product_id(product, network=selected_network, client=client)
    if product_id is None:
        available = ", ".join(get_perp_products(network=selected_network, client=client))
        return False, f"Unknown product '{product}'. Available: {available}"

    if size <= 0:
        return False, "Trade size must be positive."

    max_leverage = get_product_max_leverage(product, network=selected_network, client=client)
    # Tolerance for float jitter when strategies coerce to per-asset MAX (e.g. 50.0
    # vs. catalog returning 49.999999 from a derived weight).
    if leverage > max_leverage + 1e-9:
        return False, f"Max leverage for {product.upper()} is {max_leverage}x."

    if leverage < 1:
        return False, "Leverage must be at least 1x."

    wallet_ok, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ok:
        return False, wallet_msg

    if not client:
        return False, "Could not initialize client. Please try again."

    try:
        mp = client.get_market_price(product_id)
    except Exception as e:
        logger.warning("get_market_price failed in validate_trade for %s: %s", product, e)
        return False, f"Could not fetch {product} price. Market may be unavailable."
    if not mp or mp.get("mid", 0) == 0:
        return False, f"Could not fetch {product} price. Market may be unavailable."

    notional = size * mp["mid"]
    if notional < MIN_TRADE_SIZE_USD:
        return False, f"Minimum trade size is ${MIN_TRADE_SIZE_USD}."

    if not reduce_only:
        balance = client.get_balance()
        if not balance.get("exists"):
            return False, "Subaccount not found. Please deposit funds first on Nado."

        balances = balance.get("balances", {}) or {}
        usdt_balance = balances.get(0, balances.get("0", 0))
        required_margin = notional / leverage if leverage > 1 else notional

        if required_margin > usdt_balance * 0.95:
            return False, (
                f"Insufficient margin.\n"
                f"Required: ~${required_margin:,.2f}\n"
                f"Available: ${usdt_balance:,.2f}\n"
                f"(Using 95% safety buffer)"
            )

    if enforce_rate_limit:
        allowed, msg = check_rate_limit(telegram_id, network=selected_network)
        if not allowed:
            return False, msg

    return True, ""


def execute_market_order(
    telegram_id: int,
    product: str,
    size: float,
    is_long: bool,
    leverage: float = 1.0,
    slippage_pct: float = 1.0,
    enforce_rate_limit: bool = True,
    tp_price: float = None,
    sl_price: float = None,
    source: str = "manual",
    strategy_session_id: int = None,
    reduce_only: bool = False,
    network: str | None = None,
    **kwargs,
) -> dict:
    selected_network = _resolve_user_network(telegram_id, network)
    builder_route = _builder_route_payload(selected_network)
    if builder_route.get("error"):
        return {"success": False, "error": builder_route["error"]}

    valid, msg = validate_trade(
        telegram_id,
        product,
        size,
        leverage,
        enforce_rate_limit=enforce_rate_limit,
        reduce_only=bool(reduce_only),
        network=selected_network,
    )
    if not valid:
        return {"success": False, "error": msg}

    user = get_user(telegram_id)
    selected_network = str(network or (user.network_mode.value if user else "mainnet"))
    product_id = get_product_id(product, network=selected_network)
    client = get_user_nado_client(telegram_id, network=selected_network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required. Check Wallet settings."}
    network = selected_network
    side_value = OrderSide.LONG.value if is_long else OrderSide.SHORT.value
    unknown_submit = _recent_unknown_market_submit(
        telegram_id,
        network=network,
        product_id=product_id,
        side=side_value,
        size=size,
    )
    if unknown_submit:
        return {
            "success": False,
            "pending": True,
            "trade_id": unknown_submit.get("id"),
            "error": "A matching order submit is still status unknown. Check Portfolio before retrying.",
        }

    intent_id = kwargs.get("intent_id")
    order_nonce = kwargs.get("order_nonce") or kwargs.get("client_order_id") or kwargs.get("idempotency_key")
    if source == "manual" and not order_nonce:
        order_nonce = f"manual:{uuid.uuid4().hex}"
    if not intent_id:
        try:
            from src.nadobro.trading.order_intents import build_intent_id

            intent_id = build_intent_id(
                user_id=telegram_id,
                network=selected_network,
                strategy_session_id=strategy_session_id,
                source=source,
                product=product,
                side=OrderSide.LONG.value if is_long else OrderSide.SHORT.value,
                size=size,
                reduce_only=bool(reduce_only),
                order_nonce=order_nonce,
            )
        except Exception:
            intent_id = None
    if intent_id:
        try:
            from src.nadobro.trading.order_intents import (
                OrderIntentReservationError,
                reserve_order_intent,
            )

            reserved, existing_intent = reserve_order_intent(
                intent_id,
                {
                    "user_id": telegram_id,
                    "network": selected_network,
                    "strategy_session_id": strategy_session_id,
                    "source": source,
                    "product": product,
                    "side": OrderSide.LONG.value if is_long else OrderSide.SHORT.value,
                    "size": size,
                    "reduce_only": bool(reduce_only),
                    "order_nonce": order_nonce,
                },
            )
            if not reserved and existing_intent and existing_intent.get("trade_id"):
                return {
                    "success": True,
                    "duplicate": True,
                    "intent_id": intent_id,
                    "trade_id": existing_intent.get("trade_id"),
                    "digest": existing_intent.get("order_digest"),
                    "message": "Duplicate order intent suppressed.",
                }
            if not reserved:
                return {
                    "success": False,
                    "duplicate": True,
                    "intent_id": intent_id,
                    "error": "Order is already being submitted. Please wait.",
                }
        except OrderIntentReservationError as exc:
            # AUDIT-FIX-OI-1 call site: the DB-backed reservation failed.
            # Refuse the order rather than risk a double-submit via the
            # previous non-atomic fallback. User-visible retry message.
            logger.warning(
                "execute_market_order: order intent reservation unavailable (%s); "
                "rejecting submission to avoid double-trade risk",
                exc,
            )
            return {
                "success": False,
                "intent_id": intent_id,
                "error": (
                    "Order tracking is temporarily unavailable. Please try "
                    "again in a few seconds."
                ),
            }
        except Exception as e:
            logger.warning(
                "order-intent reservation errored — proceeding WITHOUT "
                "duplicate-order protection for this submit: %s",
                e,
            )

    trade_data = {
        "user_id": telegram_id,
        "product_id": product_id,
        "product_name": get_product_name(product_id, network=selected_network),
        "order_type": OrderTypeEnum.MARKET.value,
        "side": side_value,
        "size": size,
        "leverage": leverage,
        "status": TradeStatus.PENDING.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if strategy_session_id:
        trade_data["strategy_session_id"] = strategy_session_id
    trade_id = insert_trade(trade_data, network=selected_network)
    if not trade_id:
        return {"success": False, "error": "Failed to record trade."}
    if intent_id:
        try:
            from src.nadobro.trading.order_intents import update_order_intent

            update_order_intent(intent_id, status="recorded", trade_id=trade_id)
        except Exception as e:
            logger.debug("order-intent %s 'recorded' marker not written: %s", intent_id, e)

    isolated_only = is_product_isolated_only(product, network=network, client=client)
    isolated_margin = None
    if isolated_only:
        try:
            mp = client.get_market_price(product_id)
            mid = float(mp.get("mid", 0) or 0)
            if mid > 0 and float(leverage or 0) > 0:
                isolated_margin = _compute_isolated_margin(size, mid, leverage)
        except Exception:
            isolated_margin = None

    # Capture mid price before order for slippage calculation
    pre_order_mid = 0.0
    try:
        pre_mp = client.get_market_price(product_id)
        pre_order_mid = float(pre_mp.get("mid", 0) or 0)
    except Exception:  # policy: degrade-ok(slippage display only)
        pass

    submit_timeout = _order_submit_timeout_seconds()
    submit_ok, submit_result = _submit_with_timeout(
        client.place_market_order,
        product_id,
        size,
        is_buy=is_long,
        slippage_pct=slippage_pct,
        isolated_only=isolated_only,
        isolated_margin=isolated_margin,
        reduce_only=bool(reduce_only),
        timeout_s=submit_timeout,
    )
    if not submit_ok:
        unknown_msg = _submit_unknown_message(submit_result)
        try:
            update_trade(
                trade_id,
                {
                    "status": TradeStatus.PENDING.value,
                    "error_message": unknown_msg,
                },
                network=network,
            )
        except Exception as e:
            logger.warning(
                "trade %s status not updated to PENDING after unknown submit — "
                "reconcile may misread its state: %s",
                trade_id, e,
            )
        if intent_id:
            try:
                from src.nadobro.trading.order_intents import update_order_intent

                update_order_intent(intent_id, status="recorded", error=unknown_msg, trade_id=trade_id)
            except Exception as e:
                logger.debug("order-intent %s unknown-submit marker not written: %s", intent_id, e)
        return {"success": False, "pending": True, "trade_id": trade_id, "error": unknown_msg}
    result = submit_result

    if result["success"]:
        digest = result.get("digest", "")
        fill_data = None
        update_data: dict = {}
        if digest:
            try:
                update_trade(
                    trade_id,
                    {
                        "order_digest": digest,
                    },
                    network=network,
                )
                if intent_id:
                    from src.nadobro.trading.order_intents import update_order_intent

                    update_order_intent(intent_id, status="submitted", trade_id=trade_id, order_digest=digest)
            except Exception as e:
                logger.warning(
                    "order-intent %s not marked submitted (order IS on venue) — "
                    "duplicate-protection state stale: %s",
                    intent_id, e,
                )
        try:
            fill_data = _resolve_fill_data(client, digest, network)
            if (not fill_data or not float(fill_data.get("fill_price") or 0)) and digest:
                fill_data = _resolve_fill_data_retry(network, digest) or fill_data
            fill_update = _build_fill_update(fill_data, mid_price=pre_order_mid)
            update_data = {
                "status": TradeStatus.FILLED.value,
                "order_digest": digest,
                "filled_at": datetime.now(timezone.utc).isoformat(),
            }
            update_data.update(fill_update)
            if "price" not in update_data or not update_data.get("price"):
                update_data["price"] = _get_post_fill_price(client, product_id) or result.get("price", 0)
            update_trade(trade_id, update_data, network=network)
            if intent_id:
                try:
                    from src.nadobro.trading.order_intents import update_order_intent

                    update_order_intent(
                        intent_id,
                        status="filled" if fill_data else "submitted",
                        trade_id=trade_id,
                        order_digest=digest,
                        fill_price=update_data.get("fill_price"),
                        filled_at=update_data.get("filled_at"),
                    )
                except Exception as e:
                    logger.warning(
                        "order-intent %s not marked filled/submitted (order IS on venue) — "
                        "duplicate-protection state stale: %s",
                        intent_id, e,
                    )

            # Enqueue for background sync if archive didn't resolve
            if not fill_data:
                _enqueue_fill_sync(trade_id, network, telegram_id, client, digest, product_id)
        except Exception as e:
            logger.error("Post-fill processing failed for trade %s: %s", trade_id, e)
            if digest:
                try:
                    _enqueue_fill_sync(trade_id, network, telegram_id, client, digest, product_id)
                except Exception as e2:
                    logger.warning(
                        "fill-sync enqueue failed for trade %s — fill may never be booked: %s",
                        trade_id, e2,
                    )
    else:
        try:
            update_trade(trade_id, {
                "status": TradeStatus.FAILED.value,
                "error_message": result.get("error", "Unknown error"),
            }, network=network)
        except Exception as e:
            logger.error("Failed to update trade %s status to FAILED: %s", trade_id, e)
        # AUDIT COPY-DUP-SETTLE: release the intent on a DEFINITE venue reject
        # (this branch is only reached when place_market_order returned
        # success=False — unknown outcomes were handled and returned earlier).
        # The limit path already does this; without it the intent stays
        # 'recorded' for 120s and a legitimate retry gets a duplicate-success
        # carrying no digest, which callers could mistake for a real fill.
        if intent_id:
            try:
                from src.nadobro.trading.order_intents import update_order_intent

                update_order_intent(
                    intent_id,
                    status="failed",
                    error=result.get("error", "Unknown error"),
                    trade_id=trade_id,
                )
            except Exception as e:
                logger.warning(
                    "order-intent %s not released after definite reject — retries "
                    "will be duplicate-blocked for up to 120s: %s",
                    intent_id, e,
                )

    if result["success"]:
        try:
            tracked_volume = 0.0
            fill_size = float(update_data.get("fill_size") or 0.0)
            fill_price = float(update_data.get("fill_price") or update_data.get("price") or 0.0)
            if fill_size > 0 and fill_price > 0:
                tracked_volume = fill_size * fill_price
                update_trade_stats(telegram_id, tracked_volume, increment_trade_count=True, network=network)
            else:
                update_trade_stats(telegram_id, 0.0, increment_trade_count=True, network=network)
        except Exception as e:
            logger.warning("Post-order stats update failed: %s", e)
        exec_px = float(
            (update_data.get("fill_price") if update_data.get("fill_price") else None)
            or update_data.get("price")
            or 0
        )
        if exec_px <= 0:
            exec_px = float(pre_order_mid or result.get("price", 0) or 0)
        exec_fee = update_data.get("fees")
        if exec_fee is None:
            exec_fee = update_data.get("fill_fee")
        payload = {
            "success": True,
            "side": "LONG" if is_long else "SHORT",
            "size": size,
            "product": get_product_name(product_id, network=network),
            "price": exec_px,
            "digest": result.get("digest"),
            "network": network,
            "trade_id": trade_id,
        }
        if exec_fee is not None and float(exec_fee or 0) >= 0:
            payload["fee"] = float(exec_fee or 0)
        tp_result = _place_take_profit_order(
            client=client,
            product_id=product_id,
            size=size,
            is_long=is_long,
            tp_price=tp_price,
        )
        if tp_result:
            payload.update(tp_result)
        sl_result = _arm_stop_loss_rule(
            telegram_id=telegram_id,
            network=network,
            product=get_product_name(product_id, network=network),
            is_long=is_long,
            stop_price=sl_price,
            size=size,
        )
        if sl_result:
            payload.update(sl_result)
        # SECURITY: append-only audit of every signed order (manual + agent).
        try:
            from src.nadobro.users.audit_log import record_audit_event

            record_audit_event(
                telegram_id,
                "order_placed",
                f"source={source} net={network} product={payload.get('product')} "
                f"side={payload['side']} size={size} lev={leverage}",
            )
        except Exception as e:
            logger.warning(
                "audit-log entry missing for signed order (user %s): %s",
                telegram_id, e,
            )
        return payload

    return result


def execute_spot_market_order(
    telegram_id: int,
    asset: str,
    size: float,
    is_buy: bool,
    enforce_rate_limit: bool = False,
    slippage_pct: float = 1.0,
    source: str = "manual",
    strategy_session_id: int = None,
    network: str | None = None,
    spot_product_id: int | None = None,
    spot_symbol: str | None = None,
    asset_label: str | None = None,
) -> dict:
    asset = (asset or "").upper().strip()
    user = get_user(telegram_id)
    selected_network = str(network or (user.network_mode.value if user else "mainnet"))
    builder_route = _builder_route_payload(selected_network)
    if builder_route.get("error"):
        return {"success": False, "error": builder_route["error"]}
    spot_meta = get_spot_metadata(asset, network=selected_network)
    spot_product_id = int(spot_product_id) if spot_product_id is not None else get_spot_product_id(asset, network=selected_network)
    if spot_product_id is None:
        return {"success": False, "error": f"{asset} spot is not supported for this strategy."}
    display_asset = str(asset_label or spot_symbol or spot_meta.get("symbol") or asset).upper()
    spot_symbol = str(spot_symbol or spot_meta.get("symbol") or display_asset).upper()
    if size <= 0:
        return {"success": False, "error": "Spot size must be positive."}

    wallet_ok, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ok:
        return {"success": False, "error": wallet_msg}

    readonly = get_user_readonly_client(telegram_id, network=selected_network)
    if not readonly:
        return {"success": False, "error": "Could not initialize client. Please try again."}

    spot_price = readonly.get_market_price(spot_product_id)
    mid = float(spot_price.get("mid") or 0.0)
    if mid <= 0:
        return {"success": False, "error": f"Could not fetch {display_asset} spot price."}
    notional = size * mid
    if notional < MIN_TRADE_SIZE_USD:
        return {"success": False, "error": f"Minimum trade size is ${MIN_TRADE_SIZE_USD}."}

    balance = readonly.get_balance() or {}
    balances = balance.get("balances", {}) or {}
    usdt_balance = float(balances.get(0, balances.get("0", 0)) or 0.0)
    spot_balance = float(balances.get(spot_product_id, balances.get(str(spot_product_id), 0)) or 0.0)
    if is_buy and notional > usdt_balance * 0.98:
        return {
            "success": False,
            "error": (
                f"Insufficient USDT0 for {display_asset} spot buy.\n"
                f"Required: ~${notional:,.2f}\n"
                f"Available: ${usdt_balance:,.2f}"
            ),
        }
    if not is_buy:
        if spot_balance <= 0:
            return {
                "success": False,
                "error": f"No {display_asset} spot balance to sell.",
            }
        # SELL-FULL-BALANCE fix (DN exposure bug): closing a position sells the
        # FULL held balance, so size == spot_balance. The old guard
        # `size > spot_balance * 0.999` then rejected the order (balance is
        # always > 0.999×balance), stranding the DN spot leg on close and
        # leaving the user directional. Clamp the sell to what's actually held
        # (a hair of headroom absorbs venue rounding) instead of rejecting a
        # legitimate full-balance close.
        if size > spot_balance:
            size = spot_balance
            notional = size * mid
        if notional < MIN_TRADE_SIZE_USD:
            # The remaining balance is below the venue minimum — it cannot be
            # sold as its own order. Surface it instead of silently failing.
            return {
                "success": False,
                "error": (
                    f"{display_asset} spot remainder ({size:,.6f} ≈ ${notional:,.2f}) "
                    f"is below the ${MIN_TRADE_SIZE_USD} minimum trade size."
                ),
            }

    if enforce_rate_limit:
        allowed, msg = check_rate_limit(telegram_id, network=selected_network)
        if not allowed:
            return {"success": False, "error": msg}

    client = get_user_nado_client(telegram_id, network=selected_network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required. Check Wallet settings."}
    network = selected_network
    side_value = OrderSide.LONG.value if is_buy else OrderSide.SHORT.value
    unknown_submit = _recent_unknown_market_submit(
        telegram_id,
        network=network,
        product_id=spot_product_id,
        side=side_value,
        size=size,
    )
    if unknown_submit:
        return {
            "success": False,
            "pending": True,
            "trade_id": unknown_submit.get("id"),
            "error": "A matching order submit is still status unknown. Check Portfolio before retrying.",
        }

    spot_trade_data = {
        "user_id": telegram_id,
        "product_id": spot_product_id,
        "product_name": spot_symbol,
        "order_type": OrderTypeEnum.MARKET.value,
        "side": side_value,
        "size": size,
        "leverage": 1.0,
        "status": TradeStatus.PENDING.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if strategy_session_id:
        spot_trade_data["strategy_session_id"] = strategy_session_id
    trade_id = insert_trade(spot_trade_data, network=network)
    if not trade_id:
        return {"success": False, "error": "Failed to record spot trade."}

    submit_timeout = _order_submit_timeout_seconds()
    submit_ok, submit_result = _submit_with_timeout(
        client.place_market_order,
        spot_product_id,
        size,
        is_buy=is_buy,
        slippage_pct=slippage_pct,
        timeout_s=submit_timeout,
    )
    if not submit_ok:
        unknown_msg = _submit_unknown_message(submit_result)
        try:
            update_trade(
                trade_id,
                {
                    "status": TradeStatus.PENDING.value,
                    "error_message": unknown_msg,
                },
                network=network,
            )
        except Exception as e:
            logger.warning(
                "trade %s status not updated to PENDING after unknown submit — "
                "reconcile may misread its state: %s",
                trade_id, e,
            )
        return {"success": False, "pending": True, "trade_id": trade_id, "error": unknown_msg}
    result = submit_result
    if result.get("success"):
        digest = result.get("digest", "")
        fill_data = _resolve_fill_data(client, digest, network)
        fill_update = _build_fill_update(fill_data, mid_price=mid)
        post_px = fill_update.get("price") or _get_post_fill_price(client, spot_product_id) or mid
        update_data = {
            "status": TradeStatus.FILLED.value,
            "order_digest": digest,
            "filled_at": datetime.now(timezone.utc).isoformat(),
        }
        update_data.update(fill_update)
        if "price" not in update_data or not update_data.get("price"):
            update_data["price"] = post_px
        update_trade(trade_id, update_data, network=network)
        if not fill_data:
            _enqueue_fill_sync(trade_id, network, telegram_id, client, digest, spot_product_id)
        tracked_volume = 0.0
        fill_size = float(update_data.get("fill_size") or 0.0)
        fill_price = float(update_data.get("fill_price") or post_px or 0.0)
        if fill_size > 0 and fill_price > 0:
            tracked_volume = abs(fill_size * fill_price)
            update_trade_stats(telegram_id, tracked_volume, increment_trade_count=True, network=network)
        else:
            update_trade_stats(telegram_id, 0.0, increment_trade_count=True, network=network)
        return {
            "success": True,
            "side": "BUY" if is_buy else "SELL",
            "asset": display_asset,
            "spot_symbol": spot_symbol,
            "size": size,
            "price": post_px,
            "product_id": spot_product_id,
            "digest": digest,
            "network": network,
        }

    update_trade(trade_id, {
        "status": TradeStatus.FAILED.value,
        "error_message": result.get("error", "Unknown error"),
    }, network=network)
    return result


def validate_spot_limit_order(
    telegram_id: int,
    spot_product_id: int,
    size: float,
    price: float,
    is_buy: bool,
    network: str,
    enforce_rate_limit: bool = True,
) -> tuple[bool, str]:
    if size <= 0 or price <= 0:
        return False, "Spot limit size and price must be positive."
    wallet_ok, wallet_msg = ensure_active_wallet_ready(telegram_id)
    if not wallet_ok:
        return False, wallet_msg
    client = get_user_readonly_client(telegram_id, network=network)
    if not client:
        return False, "Could not initialize client. Please try again."
    try:
        mp = client.get_market_price(int(spot_product_id))
    except Exception as e:
        logger.warning("validate_spot_limit_order: get_market_price failed: %s", e)
        return False, "Could not fetch spot market price."
    mid = float((mp or {}).get("mid") or 0.0)
    if mid <= 0:
        return False, "Could not fetch spot market price."
    notional = float(size) * float(price)
    if notional < MIN_TRADE_SIZE_USD:
        return False, f"Minimum trade size is ${MIN_TRADE_SIZE_USD}."
    balance = client.get_balance() or {}
    balances = balance.get("balances", {}) or {}
    usdt_balance = float(balances.get(0, balances.get("0", 0)) or 0.0)
    spot_balance = float(balances.get(spot_product_id, balances.get(str(spot_product_id), 0)) or 0.0)
    if is_buy and notional > usdt_balance * 0.98:
        return (
            False,
            f"Insufficient USDT0 for spot limit buy.\nRequired: ~${notional:,.2f}\nAvailable: ${usdt_balance:,.2f}",
        )
    if (not is_buy) and size > spot_balance * 0.999:
        return (
            False,
            f"Insufficient spot base balance to sell.\nRequired: {size:,.8f}\nAvailable: {spot_balance:,.8f}",
        )
    if enforce_rate_limit:
        allowed, msg = check_rate_limit(telegram_id, network=network)
        if not allowed:
            return False, msg
    return True, ""


def execute_spot_limit_order(
    telegram_id: int,
    asset: str,
    size: float,
    price: float,
    is_buy: bool,
    enforce_rate_limit: bool = True,
    post_only: bool = False,
    reduce_only: bool = False,
    source: str = "manual",
    strategy_session_id: int = None,
    network: str | None = None,
    spot_product_id: int | None = None,
    spot_symbol: str | None = None,
    asset_label: str | None = None,
    order_type_override: str | None = None,
    open_trade_id: int | None = None,
) -> dict:
    """Place a limit order on a Nado spot book (same engine path as perp limits)."""
    asset = (asset or "").upper().strip()
    user = get_user(telegram_id)
    selected_network = str(network or (user.network_mode.value if user else "mainnet"))
    builder_route = _builder_route_payload(selected_network)
    if builder_route.get("error"):
        return {"success": False, "error": builder_route["error"]}
    spot_meta = get_spot_metadata(asset, network=selected_network)
    spot_product_id = int(spot_product_id) if spot_product_id is not None else get_spot_product_id(asset, network=selected_network)
    if spot_product_id is None:
        return {"success": False, "error": f"{asset} spot is not supported."}
    display_asset = str(asset_label or spot_symbol or spot_meta.get("symbol") or asset).upper()
    spot_symbol = str(spot_symbol or spot_meta.get("symbol") or display_asset).upper()

    valid, msg = validate_spot_limit_order(
        telegram_id,
        spot_product_id,
        size,
        price,
        is_buy,
        selected_network,
        enforce_rate_limit=enforce_rate_limit,
    )
    if not valid:
        return {"success": False, "error": msg}

    client = get_user_nado_client(telegram_id, network=selected_network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required. Check Wallet settings."}
    network = selected_network

    resolved_order_type = str(order_type_override or OrderTypeEnum.LIMIT.value)
    linked_open_trade_id = int(open_trade_id) if open_trade_id else None
    if linked_open_trade_id is None and bool(reduce_only):
        linked_open = find_open_trade(telegram_id, spot_product_id, network=network)
        if linked_open:
            linked_open_trade_id = int(linked_open.get("id"))
        if resolved_order_type == OrderTypeEnum.LIMIT.value:
            resolved_order_type = "LIMIT_CLOSE"

    limit_trade_data = {
        "user_id": telegram_id,
        "product_id": spot_product_id,
        "product_name": spot_symbol,
        "order_type": resolved_order_type,
        "side": OrderSide.LONG.value if is_buy else OrderSide.SHORT.value,
        "size": size,
        "price": price,
        "leverage": 1.0,
        "status": TradeStatus.PENDING.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if strategy_session_id:
        limit_trade_data["strategy_session_id"] = strategy_session_id
    if linked_open_trade_id:
        limit_trade_data["open_trade_id"] = linked_open_trade_id
    trade_id = insert_trade(limit_trade_data, network=network)
    if not trade_id:
        return {"success": False, "error": "Failed to record trade."}

    result = client.place_limit_order(
        spot_product_id,
        float(size),
        float(price),
        is_buy=is_buy,
        reduce_only=bool(reduce_only),
        post_only=bool(post_only),
        isolated_only=False,
        isolated_margin=None,
    )

    if result.get("success"):
        digest = result.get("digest", "")
        try:
            update_trade(
                trade_id,
                {
                    "status": TradeStatus.PENDING.value,
                    "order_digest": digest,
                },
                network=network,
            )
            _enqueue_fill_sync(trade_id, network, telegram_id, client, digest, spot_product_id)
        except Exception as e:
            logger.error("Post-spot-limit-order processing failed for trade %s: %s", trade_id, e)
    else:
        try:
            update_trade(
                trade_id,
                {"status": TradeStatus.FAILED.value, "error_message": result.get("error", "Unknown error")},
                network=network,
            )
        except Exception as e:
            logger.error("Failed to update spot limit trade %s: %s", trade_id, e)

    if result.get("success"):
        return {
            "success": True,
            "side": "BUY" if is_buy else "SELL",
            "size": size,
            "product": spot_symbol,
            "spot_symbol": spot_symbol,
            "price": price,
            "digest": result.get("digest"),
            "network": network,
            "type": "LIMIT",
            "status": TradeStatus.PENDING.value,
            "trade_id": trade_id,
            "message": "Spot limit order accepted and recorded as pending until execution.",
        }
    return result


def stop_volume_spot_cleanup(
    telegram_id: int,
    spot_product_id: int,
    spot_symbol: str,
    network: str | None = None,
    slippage_pct: float = 1.0,
    strategy_session_id: int | None = None,
    max_base_size: float | None = None,
) -> dict:
    """Cancel resting spot orders for one product and market-sell remaining base (Volume spot stop)."""
    user = get_user(telegram_id)
    selected_network = str(network or (user.network_mode.value if user else "mainnet"))
    client = get_user_nado_client(telegram_id, network=selected_network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required."}
    cancelled, order_errors = _cancel_open_orders_for_product(client, int(spot_product_id))
    readonly = get_user_readonly_client(telegram_id, network=selected_network)
    base = 0.0
    if readonly:
        bal = readonly.get_balance() or {}
        balances = bal.get("balances", {}) or {}
        base = float(balances.get(int(spot_product_id), balances.get(str(spot_product_id), 0)) or 0.0)
    base_to_sell = base
    if max_base_size is not None:
        try:
            cap = max(0.0, float(max_base_size or 0.0))
        except (TypeError, ValueError):
            cap = 0.0
        base_to_sell = min(base, cap)
    spot_res: dict = {"success": True, "skipped": True}
    if base_to_sell > 1e-12:
        spot_res = execute_spot_market_order(
            telegram_id,
            spot_symbol,
            base_to_sell,
            is_buy=False,
            enforce_rate_limit=False,
            slippage_pct=float(slippage_pct or 1.0),
            source="vol_stop",
            strategy_session_id=strategy_session_id,
            network=selected_network,
            spot_product_id=int(spot_product_id),
            spot_symbol=str(spot_symbol).upper(),
            asset_label=str(spot_symbol).upper(),
        )
    hard_errors: list[str] = []
    if order_errors:
        hard_errors.extend(order_errors)
    if base_to_sell > 1e-12 and not spot_res.get("success"):
        hard_errors.append(str(spot_res.get("error") or "Spot flatten failed"))
    return {
        "success": len(hard_errors) == 0,
        "cancelled_orders": cancelled,
        "order_errors": order_errors or None,
        "spot_flatten": spot_res,
        "spot_base_detected": base,
        "spot_base_sold": base_to_sell,
        "errors": hard_errors or None,
    }


def execute_limit_order(
    telegram_id: int,
    product: str,
    size: float,
    price: float,
    is_long: bool,
    leverage: float = 1.0,
    enforce_rate_limit: bool = True,
    tp_price: float = None,
    sl_price: float = None,
    source: str = "manual",
    strategy_session_id: int = None,
    reduce_only: bool = False,
    post_only: bool = False,
    order_type_override: str | None = None,
    open_trade_id: int | None = None,
    **kwargs,
) -> dict:
    selected_network = _resolve_user_network(telegram_id, kwargs.get("network"))
    builder_route = _builder_route_payload(selected_network)
    if builder_route.get("error"):
        return {"success": False, "error": builder_route["error"]}

    valid, msg = validate_trade(
        telegram_id,
        product,
        size,
        leverage,
        enforce_rate_limit=enforce_rate_limit,
        reduce_only=bool(reduce_only),
        network=selected_network,
    )
    if not valid:
        return {"success": False, "error": msg}

    network = selected_network
    product_id = get_product_id(product, network=network)
    client = get_user_nado_client(telegram_id, network=network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required. Check Wallet settings."}

    resolved_order_type = str(order_type_override or OrderTypeEnum.LIMIT.value)
    linked_open_trade_id = int(open_trade_id) if open_trade_id else None
    if linked_open_trade_id is None and bool(reduce_only):
        linked_open = find_open_trade(telegram_id, product_id, network=network)
        if linked_open:
            linked_open_trade_id = int(linked_open.get("id"))
        if resolved_order_type == OrderTypeEnum.LIMIT.value:
            resolved_order_type = "LIMIT_CLOSE"

    limit_trade_data = {
        "user_id": telegram_id,
        "product_id": product_id,
        "product_name": get_product_name(product_id, network=network),
        "order_type": resolved_order_type,
        "side": OrderSide.LONG.value if is_long else OrderSide.SHORT.value,
        "size": size,
        "price": price,
        "leverage": leverage,
        "status": TradeStatus.PENDING.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    if strategy_session_id:
        limit_trade_data["strategy_session_id"] = strategy_session_id
    if linked_open_trade_id:
        limit_trade_data["open_trade_id"] = linked_open_trade_id

    intent_id = kwargs.get("intent_id")
    order_nonce = kwargs.get("order_nonce") or kwargs.get("client_order_id") or kwargs.get("idempotency_key")
    if source == "manual" and not order_nonce:
        order_nonce = f"manual:{uuid.uuid4().hex}"
    if not intent_id:
        try:
            from src.nadobro.trading.order_intents import build_intent_id

            intent_id = build_intent_id(
                user_id=telegram_id,
                network=network,
                strategy_session_id=strategy_session_id,
                source=source,
                product=product,
                side=OrderSide.LONG.value if is_long else OrderSide.SHORT.value,
                size=size,
                reduce_only=bool(reduce_only),
                price=price,
                order_nonce=order_nonce,
            )
        except Exception:
            intent_id = None
    if intent_id:
        try:
            from src.nadobro.trading.order_intents import (
                OrderIntentReservationError,
                reserve_order_intent,
            )

            reserved, existing_intent = reserve_order_intent(
                intent_id,
                {
                    "user_id": telegram_id,
                    "network": network,
                    "strategy_session_id": strategy_session_id,
                    "source": source,
                    "product": product,
                    "side": OrderSide.LONG.value if is_long else OrderSide.SHORT.value,
                    "size": size,
                    "price": price,
                    "reduce_only": bool(reduce_only),
                    "order_nonce": order_nonce,
                },
            )
            if not reserved and existing_intent and existing_intent.get("trade_id"):
                return {
                    "success": True,
                    "duplicate": True,
                    "intent_id": intent_id,
                    "trade_id": existing_intent.get("trade_id"),
                    "digest": existing_intent.get("order_digest"),
                    "message": "Duplicate order intent suppressed.",
                }
            if not reserved:
                return {
                    "success": False,
                    "duplicate": True,
                    "intent_id": intent_id,
                    "error": "Order is already being submitted. Please wait.",
                }
        except OrderIntentReservationError as exc:
            # AUDIT-FIX-OI-1 call site: refuse rather than risk double-submit.
            logger.warning(
                "execute_limit_order: order intent reservation unavailable (%s); "
                "rejecting submission to avoid double-trade risk",
                exc,
            )
            return {
                "success": False,
                "intent_id": intent_id,
                "error": (
                    "Order tracking is temporarily unavailable. Please try "
                    "again in a few seconds."
                ),
            }
        except Exception as e:
            logger.warning(
                "order-intent reservation errored — proceeding WITHOUT "
                "duplicate-order protection for this submit: %s",
                e,
            )

    trade_id = insert_trade(limit_trade_data, network=network)
    if not trade_id:
        return {"success": False, "error": "Failed to record trade."}
    if intent_id:
        try:
            from src.nadobro.trading.order_intents import update_order_intent

            update_order_intent(intent_id, status="recorded", trade_id=trade_id)
        except Exception as e:
            logger.debug("order-intent %s 'recorded' marker not written: %s", intent_id, e)

    isolated_only = is_product_isolated_only(product, network=network, client=client)
    isolated_margin = None
    if isolated_only and float(leverage or 0) > 0:
        isolated_margin = _compute_isolated_margin(size, price, leverage)
    logger.debug(
        "execute_limit_order place_limit_order isolated_margin=%s size=%.8f price=%.6f leverage=%.2f post_only=%s reduce_only=%s",
        isolated_margin,
        float(size),
        float(price),
        float(leverage),
        bool(post_only),
        bool(reduce_only),
    )
    result = client.place_limit_order(
        product_id,
        size,
        price,
        is_buy=is_long,
        reduce_only=bool(reduce_only),
        post_only=bool(post_only),
        isolated_only=isolated_only,
        isolated_margin=isolated_margin,
    )

    if result["success"]:
        digest = result.get("digest", "")
        try:
            update_trade(
                trade_id,
                {
                    "status": TradeStatus.PENDING.value,
                    "order_digest": digest,
                },
                network=network,
            )
            if intent_id:
                try:
                    from src.nadobro.trading.order_intents import update_order_intent

                    update_order_intent(intent_id, status="submitted", trade_id=trade_id, order_digest=digest)
                except Exception as e:
                    logger.warning(
                        "order-intent %s not marked submitted (limit order IS on venue) — "
                        "duplicate-protection state stale: %s",
                        intent_id, e,
                    )
            # Enqueue for background fill sync (limit orders fill asynchronously)
            _enqueue_fill_sync(trade_id, network, telegram_id, client, digest, product_id)
        except Exception as e:
            logger.error("Post-limit-order processing failed for trade %s: %s", trade_id, e)
    else:
        try:
            update_trade(trade_id, {"status": TradeStatus.FAILED.value, "error_message": result.get("error", "Unknown error")}, network=network)
        except Exception as e:
            logger.error("Failed to update limit trade %s status to FAILED: %s", trade_id, e)
        if intent_id:
            try:
                from src.nadobro.trading.order_intents import update_order_intent

                update_order_intent(intent_id, status="failed", error=result.get("error", "Unknown error"), trade_id=trade_id)
            except Exception as e:
                logger.debug("order-intent %s failed-marker not written: %s", intent_id, e)

    if result["success"]:
        _clear_portfolio_caches(telegram_id, network)
        return {
            "success": True,
            "side": "LONG" if is_long else "SHORT",
            "size": size,
            "product": get_product_name(product_id, network=network),
            "price": price,
            "digest": result.get("digest"),
            "network": network,
            "type": "LIMIT",
            "status": TradeStatus.PENDING.value,
            "trade_id": trade_id,
            "message": "Limit order accepted and recorded as pending until execution.",
        }

    return result


def _place_take_profit_order(client, product_id: int, size: float, is_long: bool, tp_price: float | None) -> dict:
    if tp_price is None:
        return {}
    try:
        tp = float(tp_price)
    except (TypeError, ValueError):
        return {"tp_requested": True, "tp_set": False, "tp_error": "Invalid TP price."}
    if tp <= 0:
        return {"tp_requested": True, "tp_set": False, "tp_error": "TP price must be greater than 0."}

    # TP for a long is a sell limit, for a short is a buy limit.
    try:
        tp_result = client.place_limit_order(
            product_id,
            float(size),
            tp,
            is_buy=(not is_long),
            reduce_only=True,
        )
    except TypeError:
        # Backward compatibility for mocked/legacy clients without reduce_only.
        tp_result = client.place_limit_order(product_id, float(size), tp, is_buy=(not is_long))
    if tp_result.get("success"):
        return {
            "tp_requested": True,
            "tp_set": True,
            "tp_price": tp,
            "tp_digest": tp_result.get("digest"),
        }
    return {
        "tp_requested": True,
        "tp_set": False,
        "tp_price": tp,
        "tp_error": tp_result.get("error", "Failed to place TP order."),
    }


def _arm_stop_loss_rule(
    telegram_id: int,
    network: str,
    product: str,
    is_long: bool,
    stop_price: float | None,
    size: float,
) -> dict:
    if stop_price is None:
        return {}
    from src.nadobro.trading.stop_loss_service import register_stop_loss_rule

    side = "LONG" if is_long else "SHORT"
    result = register_stop_loss_rule(
        telegram_id=telegram_id,
        network=network,
        product=product,
        side=side,
        stop_price=stop_price,
        size=size,
    )
    if result.get("success"):
        return {
            "sl_requested": True,
            "sl_armed": True,
            "sl_price": float(result.get("stop_price") or 0),
            "sl_rule_id": result.get("rule_id"),
        }
    return {
        "sl_requested": True,
        "sl_armed": False,
        "sl_price": float(stop_price or 0),
        "sl_error": result.get("error", "Failed to arm stop-loss."),
    }


def apply_tp_sl_to_open_position(
    telegram_id: int,
    product: str,
    tp_price: float | None = None,
    sl_price: float | None = None,
) -> dict:
    """
    Place a reduce-only take-profit limit and/or arm the bot stop-loss rule for an existing PERP position.
    Used by natural-language commands so replies reflect real execution, not LLM text.
    """
    tp_price = None if tp_price is None else float(tp_price)
    sl_price = None if sl_price is None else float(sl_price)
    if (tp_price is None or tp_price <= 0) and (sl_price is None or sl_price <= 0):
        return {"success": False, "error": "Provide a valid TP and/or SL price greater than zero."}

    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    product_id = get_product_id(product, network=network)
    if product_id is None:
        return {"success": False, "error": f"Unknown product '{product}'."}

    client = get_user_nado_client(telegram_id)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required."}

    from src.nadobro.users.settings_service import get_user_settings

    _, settings = get_user_settings(telegram_id)
    leverage = float(settings.get("default_leverage", 3) or 3)
    leverage = max(1.0, leverage)

    net_positions = _normalize_net_positions(client.get_all_positions() or [])
    product_pos = net_positions.get(product_id)
    if not product_pos:
        return {"success": False, "error": f"No open position on {get_product_name(product_id, network=network)}."}

    legs = _iter_position_legs(product_pos)
    if not legs:
        return {"success": False, "error": f"No open position on {get_product_name(product_id, network=network)}."}
    leg0 = legs[0]
    signed_amount = float(leg0.get("signed_amount", 0) or 0)
    pos_size = abs(signed_amount)
    if pos_size <= 0:
        return {"success": False, "error": f"No open position on {get_product_name(product_id, network=network)}."}

    is_long = signed_amount > 0
    product_name = get_product_name(product_id, network=network)
    sender_leg = leg0.get("subaccount")

    isolated_only = is_product_isolated_only(product, network=network, client=client)
    isolated_margin = None
    if isolated_only:
        mp = client.get_market_price(product_id)
        mid = float(mp.get("mid", 0) or 0)
        if mid > 0:
            isolated_margin = _compute_isolated_margin(pos_size, mid, leverage)

    out: dict = {
        "product": product_name,
        "network": network,
        "position_size": pos_size,
        "side": "LONG" if is_long else "SHORT",
    }
    any_ok = False

    if tp_price is not None and tp_price > 0:
        out["tp_requested"] = True
        tp_res = client.place_limit_order(
            product_id,
            float(pos_size),
            float(tp_price),
            is_buy=(not is_long),
            isolated_only=isolated_only,
            isolated_margin=isolated_margin,
            reduce_only=True,
            sender=sender_leg,
        )
        if tp_res.get("success"):
            any_ok = True
            out["tp_set"] = True
            out["tp_price"] = float(tp_price)
            out["tp_digest"] = tp_res.get("digest")
        else:
            out["tp_set"] = False
            out["tp_price"] = float(tp_price)
            out["tp_error"] = tp_res.get("error", "Failed to place TP order.")

    if sl_price is not None and sl_price > 0:
        out["sl_requested"] = True
        sl_part = _arm_stop_loss_rule(
            telegram_id=telegram_id,
            network=network,
            product=product_name,
            is_long=is_long,
            stop_price=float(sl_price),
            size=float(pos_size),
        )
        out.update(sl_part)
        if sl_part.get("sl_armed"):
            any_ok = True

    if out.get("tp_requested") and out.get("sl_requested"):
        out["success"] = bool(any_ok)
    elif out.get("tp_requested"):
        out["success"] = bool(out.get("tp_set"))
    elif out.get("sl_requested"):
        out["success"] = bool(out.get("sl_armed"))
    else:
        out["success"] = False

    if not out.get("success"):
        errs = []
        if out.get("tp_error"):
            errs.append(str(out["tp_error"]))
        if out.get("sl_error"):
            errs.append(str(out["sl_error"]))
        out["error"] = errs[0] if errs else "TP/SL placement failed."

    return out


def limit_close_position(
    telegram_id: int,
    product: str,
    limit_price: float,
    size: float | None = None,
) -> dict:
    """
    Place a reduce-only limit order to close (fully or partially) an existing PERP position.
    """
    try:
        lp = float(limit_price)
    except (TypeError, ValueError):
        return {"success": False, "error": "Invalid limit price."}
    if lp <= 0:
        return {"success": False, "error": "Limit price must be greater than zero."}

    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    product_id = get_product_id(product, network=network)
    if product_id is None:
        return {"success": False, "error": f"Unknown product '{product}'."}

    client = get_user_nado_client(telegram_id)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required."}

    from src.nadobro.users.settings_service import get_user_settings

    _, settings = get_user_settings(telegram_id)
    leverage = float(settings.get("default_leverage", 3) or 3)
    leverage = max(1.0, leverage)

    net_positions = _normalize_net_positions(client.get_all_positions() or [])
    product_pos = net_positions.get(product_id)
    if not product_pos:
        return {"success": False, "error": f"No open position on {get_product_name(product_id, network=network)}."}

    legs = _iter_position_legs(product_pos)
    if not legs:
        return {"success": False, "error": f"No open position on {get_product_name(product_id, network=network)}."}
    leg0 = legs[0]
    signed_amount = float(leg0.get("signed_amount", 0) or 0)
    pos_size = abs(signed_amount)
    if pos_size <= 0:
        return {"success": False, "error": f"No open position on {get_product_name(product_id, network=network)}."}

    is_long = signed_amount > 0
    close_sz = float(size) if size is not None else pos_size
    close_sz = min(close_sz, pos_size)
    if close_sz <= 0:
        return {"success": False, "error": "Invalid close size."}

    product_name = get_product_name(product_id, network=network)
    sender_leg = leg0.get("subaccount")
    isolated_only = is_product_isolated_only(product, network=network, client=client)
    isolated_margin = None
    if isolated_only:
        mp = client.get_market_price(product_id)
        mid = float(mp.get("mid", 0) or 0)
        if mid > 0:
            isolated_margin = _compute_isolated_margin(close_sz, mid, leverage)

    r = client.place_limit_order(
        product_id,
        float(close_sz),
        float(lp),
        is_buy=(not is_long),
        isolated_only=isolated_only,
        isolated_margin=isolated_margin,
        reduce_only=True,
        sender=sender_leg,
    )
    if not r.get("success"):
        return {"success": False, "error": r.get("error", "Limit close failed.")}

    digest = r.get("digest")

    # Record close trade in DB and enqueue for fill sync
    try:
        linked_open_trade = find_open_trade(telegram_id, product_id, network=network)
        trade_id = insert_trade({
            "user_id": telegram_id,
            "product_id": product_id,
            "product_name": product_name,
            "order_type": "LIMIT_CLOSE",
            "side": "BUY" if not is_long else "SELL",
            "size": close_sz,
            "price": float(lp),
            "leverage": leverage,
            "status": TradeStatus.PENDING.value,
            "order_digest": digest,
            "source": "manual",
            "open_trade_id": int(linked_open_trade.get("id")) if linked_open_trade else None,
        }, network=network)
        if trade_id and digest:
            _enqueue_fill_sync(trade_id, network, telegram_id, client, digest, product_id)
    except Exception as exc:
        logger.warning("limit_close_position: DB recording failed: %s", exc)

    return {
        "success": True,
        "kind": "LIMIT_CLOSE",
        "product": product_name,
        "network": network,
        "size": close_sz,
        "limit_price": float(lp),
        "digest": digest,
        "side": "LONG" if is_long else "SHORT",
    }


def _position_subaccount_key(p: dict) -> str:
    s = (p.get("subaccount") or p.get("sender_subaccount") or "").strip()
    return s.lower() if s else ""


def _iter_position_legs(entry: dict | None) -> list[dict]:
    """Expand a normalized position entry into one or more close legs (per margin subaccount)."""
    if not entry:
        return []
    legs = entry.get("legs")
    if isinstance(legs, list) and legs:
        return legs
    return [entry]


def _normalize_net_positions(positions: list) -> dict[int, dict]:
    """
    Build net position by product (and margin subaccount) with de-dup protection.

    Isolated margin uses child subaccounts; rows include ``subaccount`` so we do not
    merge distinct subaccounts into one net. Multiple subaccounts for the same
    product_id produce a ``legs`` list for sequential closes.
    """
    seen_rows: dict[tuple[int, str], set] = defaultdict(set)
    bucket_net: dict[tuple[int, str], dict] = {}

    for p in positions or []:
        try:
            pid = int(p.get("product_id", -1))
            if pid < 0:
                continue
            sub_key = _position_subaccount_key(p)
            bkey = (pid, sub_key)
            signed_amount_raw = p.get("signed_amount", None)
            signed_amount = float(signed_amount_raw) if signed_amount_raw is not None else 0.0
            side = str(p.get("side", "") or "").upper()
            amount = float(p.get("amount", 0) or 0)
            if amount <= 0 and signed_amount != 0:
                amount = abs(signed_amount)
            if amount <= 0:
                continue
            if side not in ("LONG", "SHORT"):
                side = "LONG" if (signed_amount if signed_amount != 0 else amount) > 0 else "SHORT"
            fp = (pid, sub_key, side, round(amount, 12), round(float(p.get("price", 0) or 0), 8))
            if fp in seen_rows[bkey]:
                continue
            seen_rows[bkey].add(fp)

            signed = signed_amount if signed_amount != 0 else (amount if side == "LONG" else -amount)
            current = bucket_net.get(
                bkey,
                {"product_name": p.get("product_name", get_product_name(pid)), "signed_amount": 0.0},
            )
            current["signed_amount"] = float(current.get("signed_amount", 0.0)) + signed
            bucket_net[bkey] = current
        except Exception as e:
            logger.warning(
                "position row skipped during flatten netting — residual exposure "
                "may be undercounted: %s",
                e,
            )
            continue

    by_pid: dict[int, list[dict]] = defaultdict(list)
    for (pid, sub_key), row in bucket_net.items():
        sa = float(row.get("signed_amount", 0.0) or 0.0)
        if abs(sa) <= 0:
            continue
        by_pid[pid].append(
            {
                "signed_amount": sa,
                "product_name": row.get("product_name", get_product_name(pid)),
                "subaccount": sub_key if sub_key else None,
            }
        )

    net_by_product: dict[int, dict] = {}
    for pid, legs in by_pid.items():
        if not legs:
            continue
        if len(legs) == 1:
            leg = legs[0]
            net_by_product[pid] = {
                "signed_amount": float(leg["signed_amount"]),
                "product_name": leg.get("product_name", get_product_name(pid)),
                "subaccount": leg.get("subaccount"),
            }
        else:
            total = sum(float(x["signed_amount"]) for x in legs)
            net_by_product[pid] = {
                "signed_amount": total,
                "product_name": legs[0].get("product_name", get_product_name(pid)),
                "legs": legs,
            }
    return net_by_product


def _get_post_fill_price(client, product_id: int) -> float | None:
    try:
        price_data = client.get_market_price(product_id)
        mid = float(price_data.get("mid", 0) or 0)
        return mid if mid > 0 else None
    except Exception:
        return None

# NOTE: The Nado SDK place_order response only returns a digest (order hash),
# not the exchange-confirmed fill price. There is no fills/trade-history endpoint
# in the current SDK to retrieve actual execution prices post-trade. Using the
# real-time market mid immediately after a successful IOC order is the best
# available approximation — IOC orders fill instantly at market, so mid at that
# moment closely tracks the actual fill level. This is strictly better than the
# old approach of using the slippage-inflated submission price.
# If the SDK adds a fills endpoint in the future, _get_post_fill_price should
# be updated to query actual execution data by digest.


def _resolve_close_session_id(
    strategy_session_id: int | None,
    open_trade: dict | None,
    close_size: float,
) -> int | None:
    """Session id to tag onto a recorded close row.

    An EXPLICIT caller-provided id always wins (the caller knows which run it is
    closing and sized the close accordingly). Otherwise we inherit the matched
    open trade's session, but ONLY when this close isn't an account-wide flatten
    larger than that open's own size: ``close_all_positions`` flattens the WHOLE
    venue position and matches an arbitrary open trade via ``find_open_trade``, so
    inheriting unconditionally let a 0.0032-BTC run book a 0.02785-BTC "close" and
    corrupted its volume/PnL. The venue's own per-session close match (digest
    back-linked in ``nado_sync._write_matches``) carries the correctly-sized close
    for session attribution, so dropping the tag here loses nothing real. When the
    open's size is unknown we keep the legacy inherit (least surprise)."""
    if strategy_session_id is not None:
        return int(strategy_session_id)
    if not open_trade:
        return None
    inherited = open_trade.get("strategy_session_id")
    if inherited is None:
        return None
    try:
        open_size = abs(float(open_trade.get("fill_size") or open_trade.get("size") or 0) or 0.0)
    except (TypeError, ValueError):
        open_size = 0.0
    try:
        csize = abs(float(close_size or 0) or 0.0)
    except (TypeError, ValueError):
        csize = 0.0
    # 1% tolerance absorbs fee/rounding drift between the open and its close.
    if open_size <= 0 or csize <= open_size * 1.01 + 1e-9:
        return int(inherited)
    return None


def _record_close_in_db(
    telegram_id: int,
    product_id: int,
    close_size: float,
    pos_size: float,
    side: str,
    client,
    fill_price: float = None,
    network: str | None = None,
    fill_data: dict | None = None,
    close_trade_id: int | None = None,
    open_trade_id: int | None = None,
    source: str = "manual",
    order_type: str = "MARKET_CLOSE",
    order_digest: str | None = None,
    strategy_session_id: int | None = None,
):
    try:
        user = get_user(telegram_id)
        selected_network = str(network or (user.network_mode.value if user else "mainnet"))

        # Determine close price: prefer archive fill_data > fill_price param > market mid
        close_price = 0.0
        close_fee = 0.0
        close_builder_fee = 0.0
        archive_pnl = None  # Authoritative PnL from exchange

        if fill_data and fill_data.get("fill_price", 0) > 0:
            close_price = fill_data["fill_price"]
            close_fee = float(fill_data.get("fee", 0) or 0.0)
            close_builder_fee = float(fill_data.get("builder_fee", 0) or 0.0)
            if fill_data.get("realized_pnl", 0) != 0:
                archive_pnl = fill_data["realized_pnl"]
        elif fill_price and fill_price > 0:
            close_price = fill_price
        else:
            try:
                price_data = client.get_market_price(product_id)
                close_price = float(price_data.get("mid", 0) or 0)
            except Exception as e:
                logger.warning(
                    "close price unresolved for product %s — close PnL may be "
                    "recorded against a stale/zero price: %s",
                    product_id, e,
                )

        open_trade = None
        if open_trade_id:
            open_trade = get_trade_by_id(int(open_trade_id), network=selected_network)
        if not open_trade:
            # ``side`` on this close row is the ORDER side; the open being
            # closed sits on the opposite (position) side.
            pos_side = "long" if str(side).lower() in ("short", "sell") else "short"
            open_trade = find_open_trade(
                telegram_id, product_id, network=selected_network, side=pos_side
            )
        is_full_close = close_size >= pos_size

        if open_trade:
            open_price = float(open_trade.get("fill_price") or open_trade.get("price") or 0)
            open_side = open_trade.get("side", "")

            # Use exchange-reported realized PnL if available (authoritative)
            if archive_pnl is not None:
                pnl = archive_pnl
            elif open_price > 0 and close_price > 0:
                if open_side == "long":
                    pnl = (close_price - open_price) * close_size
                elif open_side == "short":
                    pnl = (open_price - close_price) * close_size
                else:
                    pnl = 0.0
                # Subtract fees from PnL for net profit
                open_fee = float(open_trade.get("fill_fee") or open_trade.get("fees") or 0)
                pnl = pnl - open_fee - close_fee - close_builder_fee
            else:
                pnl = 0.0

            update_data = {
                "close_price": close_price,
                "pnl": round(pnl, 4),
                "realized_pnl": fill_data.get("realized_pnl", pnl) if fill_data else round(pnl, 4),
            }
            if fill_data:
                update_data["is_taker"] = fill_data.get("is_taker", False)

            if is_full_close:
                update_data["status"] = TradeStatus.CLOSED.value
                update_data["closed_at"] = datetime.now(timezone.utc).isoformat()
                update_trade(open_trade["id"], update_data, network=selected_network)
                logger.info(
                    "Trade #%d fully closed: %s %s size=%.4f open=%.2f close=%.2f pnl=%.4f fee=%.4f%s",
                    open_trade["id"], open_side, get_product_name(product_id),
                    close_size, open_price, close_price, pnl, close_fee,
                    " (archive)" if archive_pnl is not None else "",
                )
            else:
                update_trade(open_trade["id"], update_data, network=selected_network)
                logger.info(
                    "Trade #%d partially closed: %s %s closed=%.4f/%.4f open=%.2f close=%.2f pnl=%.4f",
                    open_trade["id"], open_side, get_product_name(product_id),
                    close_size, pos_size, open_price, close_price, pnl,
                )

            close_trade_data = {
                "user_id": telegram_id,
                "product_id": product_id,
                "product_name": get_product_name(product_id, network=selected_network),
                "order_type": str(order_type or "MARKET_CLOSE"),
                "side": side,
                "size": close_size,
                "price": close_price,
                "leverage": float(open_trade.get("leverage") or 1.0),
                "status": TradeStatus.CLOSED.value,
                "order_digest": order_digest,
                "close_price": close_price,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "filled_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "open_trade_id": int(open_trade.get("id")),
            }
            session_for_close = _resolve_close_session_id(strategy_session_id, open_trade, close_size)
            if session_for_close is not None:
                close_trade_data["strategy_session_id"] = int(session_for_close)
            if fill_data:
                close_trade_data["fill_price"] = close_price
                close_trade_data["fill_fee"] = close_fee + close_builder_fee
                close_trade_data["builder_fee"] = close_builder_fee
                close_trade_data["fees"] = close_fee + close_builder_fee
                close_trade_data["fill_size"] = fill_data.get("fill_size", 0)
                close_trade_data["realized_pnl"] = fill_data.get("realized_pnl", 0)
                close_trade_data["is_taker"] = fill_data.get("is_taker", False)
            if close_trade_id:
                close_trade_update = {
                    "status": TradeStatus.CLOSED.value,
                    "order_digest": order_digest,
                    "price": close_price,
                    "filled_at": close_trade_data.get("filled_at"),
                    "close_price": close_price,
                    "closed_at": close_trade_data.get("closed_at"),
                    "fill_price": close_trade_data.get("fill_price"),
                    "fill_size": close_trade_data.get("fill_size"),
                    "fill_fee": close_trade_data.get("fill_fee"),
                    "builder_fee": close_trade_data.get("builder_fee"),
                    "fees": close_trade_data.get("fees"),
                    "source": source,
                    "strategy_session_id": close_trade_data.get("strategy_session_id"),
                    "open_trade_id": int(open_trade.get("id")),
                    "realized_pnl": close_trade_data.get("realized_pnl"),
                    "is_taker": close_trade_data.get("is_taker"),
                }
                update_trade(int(close_trade_id), close_trade_update, network=selected_network)
            else:
                insert_trade(close_trade_data, network=selected_network)
            try:
                if fill_data:
                    update_trade_stats(telegram_id, close_size * close_price, increment_trade_count=True, network=selected_network)
                else:
                    update_trade_stats(telegram_id, 0.0, increment_trade_count=True, network=selected_network)
            except Exception as e:
                logger.warning(
                    "trade stats not updated for user %s — volume counters "
                    "(points/referrals) will undercount: %s",
                    telegram_id, e,
                )
        else:
            close_trade_data = {
                "user_id": telegram_id,
                "product_id": product_id,
                "product_name": get_product_name(product_id),
                "order_type": str(order_type or "MARKET_CLOSE"),
                "side": side,
                "size": close_size,
                "price": close_price,
                "leverage": 1.0,
                "status": TradeStatus.CLOSED.value,
                "order_digest": order_digest,
                "close_price": close_price,
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "filled_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
            }
            if fill_data:
                close_trade_data["fill_price"] = close_price
                close_trade_data["fill_fee"] = close_fee + close_builder_fee
                close_trade_data["builder_fee"] = close_builder_fee
                close_trade_data["fees"] = close_fee + close_builder_fee
                close_trade_data["fill_size"] = fill_data.get("fill_size", 0)
                close_trade_data["realized_pnl"] = fill_data.get("realized_pnl", 0)
                close_trade_data["pnl"] = round(archive_pnl, 4) if archive_pnl is not None else 0.0
                close_trade_data["is_taker"] = fill_data.get("is_taker", False)
            if strategy_session_id:
                close_trade_data["strategy_session_id"] = int(strategy_session_id)
            if close_trade_id:
                close_trade_update = {
                    "status": TradeStatus.CLOSED.value,
                    "order_digest": order_digest,
                    "price": close_price,
                    "filled_at": close_trade_data.get("filled_at"),
                    "close_price": close_price,
                    "closed_at": close_trade_data.get("closed_at"),
                    "fill_price": close_trade_data.get("fill_price"),
                    "fill_size": close_trade_data.get("fill_size"),
                    "fill_fee": close_trade_data.get("fill_fee"),
                    "builder_fee": close_trade_data.get("builder_fee"),
                    "fees": close_trade_data.get("fees"),
                    "source": source,
                    "strategy_session_id": close_trade_data.get("strategy_session_id"),
                    "realized_pnl": close_trade_data.get("realized_pnl"),
                    "pnl": close_trade_data.get("pnl"),
                    "is_taker": close_trade_data.get("is_taker"),
                }
                update_trade(int(close_trade_id), close_trade_update, network=selected_network)
            else:
                insert_trade(close_trade_data, network=selected_network)
            try:
                if fill_data:
                    update_trade_stats(telegram_id, close_size * close_price, increment_trade_count=True, network=selected_network)
                else:
                    update_trade_stats(telegram_id, 0.0, increment_trade_count=True, network=selected_network)
            except Exception as e:
                logger.warning(
                    "trade stats not updated for user %s — volume counters "
                    "(points/referrals) will undercount: %s",
                    telegram_id, e,
                )
            logger.info(
                "Close trade recorded (no matching open): %s %s size=%.4f price=%.2f fee=%.4f",
                side, get_product_name(product_id, network=selected_network), close_size, close_price, close_fee + close_builder_fee,
            )
    except Exception as e:
        logger.warning("Failed to record close in DB: %s", e)


def reconcile_close_trade_fill(trade_id: int, network: str, fill_data: dict) -> None:
    trade_row = get_trade_by_id(int(trade_id), network=network)
    if not trade_row:
        return
    if not (_is_close_order_type(trade_row.get("order_type")) or trade_row.get("open_trade_id")):
        return
    telegram_id = int(trade_row.get("user_id"))
    product_id = int(trade_row.get("product_id"))
    close_size = abs(float(trade_row.get("size") or fill_data.get("fill_size") or 0.0))
    linked_open_trade = get_trade_by_id(int(trade_row.get("open_trade_id")), network=network) if trade_row.get("open_trade_id") else None
    linked_open_size = abs(float((linked_open_trade or {}).get("size") or close_size or 0.0))
    client = get_user_nado_client(telegram_id, network=network) or get_user_readonly_client(telegram_id, network=network)
    if not client or close_size <= 0:
        return
    _record_close_in_db(
        telegram_id=telegram_id,
        product_id=product_id,
        close_size=close_size,
        pos_size=linked_open_size,
        side=str(trade_row.get("side") or ""),
        client=client,
        fill_price=float(fill_data.get("fill_price") or 0.0),
        network=network,
        fill_data=fill_data,
        close_trade_id=int(trade_id),
        open_trade_id=trade_row.get("open_trade_id"),
        source=str(trade_row.get("source") or "manual"),
        order_type=str(trade_row.get("order_type") or "MARKET_CLOSE"),
        order_digest=str(trade_row.get("order_digest") or ""),
        strategy_session_id=trade_row.get("strategy_session_id"),
    )


def close_position(
    telegram_id: int,
    product: str,
    size: float = None,
    network: str | None = None,
    source: str = "manual",
    strategy_session_id: int | None = None,
    **kwargs,
) -> dict:
    user = get_user(telegram_id)
    active_network = user.network_mode.value if user else "mainnet"
    selected_network = str(network or active_network)
    product_id = get_product_id(product, network=selected_network)
    if product_id is None:
        return {"success": False, "error": f"Unknown product '{product}'."}

    client = get_user_nado_client(telegram_id, network=selected_network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required."}

    cancelled_orders = 0
    order_errors: list[str] = []
    for snd in _order_sender_params(client, selected_network):
        c_count, c_errs = _cancel_open_orders_for_product(client, product_id, sender=snd)
        cancelled_orders += c_count
        order_errors.extend(c_errs)

    net_positions = _normalize_net_positions(client.get_all_positions() or [])
    product_pos = net_positions.get(product_id)
    if not product_pos:
        if cancelled_orders > 0:
            return {
                "success": True,
                "cancelled": 0.0,
                "product": get_product_name(product_id, network=selected_network),
                "cancelled_orders": cancelled_orders,
                "order_errors": order_errors if order_errors else None,
            }
        return {"success": False, "error": f"No open positions on {product}."}

    signed_amount = float(product_pos.get("signed_amount", 0) or 0)
    pos_size = abs(signed_amount)
    if pos_size <= 0:
        return {"success": False, "error": f"No open positions on {product}."}

    close_size = min(pos_size, float(size)) if size else pos_size
    full_close_requested = size is None or close_size >= pos_size
    close_slippage_pct = max(0.1, float(kwargs.get("slippage_pct") or 1.0))

    from src.nadobro.users.settings_service import get_user_settings

    _, settings = get_user_settings(telegram_id)
    leverage = max(1.0, float(settings.get("default_leverage", 3) or 3))

    legs = _iter_position_legs(product_pos)
    budget = float(close_size)
    total_filled = 0.0

    for leg in legs:
        if budget <= 0:
            break
        sub_hint = leg.get("subaccount")
        leg_cap = abs(float(leg.get("signed_amount", 0) or 0))
        if leg_cap <= 0:
            continue
        leg_budget = min(budget, leg_cap)
        leg_remaining = leg_budget
        close_side = "short" if float(leg.get("signed_amount", 0) or 0) > 0 else "long"

        attempts = 0
        while leg_remaining > 0 and attempts < 3:
            attempts += 1
            raw_positions = client.get_all_positions() or []
            latest_abs, dir_sign = _net_abs_for_subaccount(raw_positions, product_id, sub_hint)
            if latest_abs <= 0:
                leg_remaining = 0.0
                break
            is_buy = dir_sign < 0
            this_close_size = min(leg_remaining, latest_abs)

            iso_only = is_product_id_isolated_only(product_id, network=selected_network, client=client)
            iso_margin = None
            if iso_only:
                mp = client.get_market_price(product_id)
                mid = float(mp.get("mid", 0) or 0)
                if mid > 0:
                    iso_margin = _compute_isolated_margin(this_close_size, mid, leverage)

            r = client.place_market_order(
                product_id,
                this_close_size,
                is_buy=is_buy,
                slippage_pct=close_slippage_pct,
                reduce_only=True,
                isolated_only=iso_only,
                isolated_margin=iso_margin,
                sender=sub_hint,
            )
            if not r.get("success"):
                return {"success": False, "error": f"Failed to close position: {r.get('error', 'unknown')}"}
            close_digest = r.get("digest", "")
            # Tag the close digest with its true origin BEFORE the venue match
            # syncs. Close orders never went through the open-order intent
            # reservation, so their fills arrived untagged and the window
            # fallback could swallow a manual close into a concurrent session
            # on the same product (hiding it from History), or orphan a
            # session close as manual (losing the session's close volume).
            if close_digest:
                try:
                    from src.nadobro.trading.order_intents import link_digest_intent

                    link_digest_intent(
                        str(close_digest),
                        selected_network,
                        source=source,
                        strategy_session_id=strategy_session_id,
                        product_id=product_id,
                        product_name=get_product_name(product_id, network=selected_network),
                    )
                # policy: degrade-ok(close link best-effort; close fill still records, attribution falls back)
                except Exception:  # noqa: BLE001 - link is best-effort
                    pass
            close_fill_data = _resolve_fill_data(client, close_digest, selected_network) if close_digest else None
            fill_price = (close_fill_data or {}).get("fill_price") or _get_post_fill_price(client, product_id)
            # NOTE: source deliberately NOT forwarded to the synthetic close row —
            # it stays 'manual' (rollup-excluded) so the intent-tagged venue fill
            # is the SINGLE counted truth for session close volume; forwarding a
            # non-manual source here would double-count the close in the rollup.
            _record_close_in_db(
                telegram_id,
                product_id,
                this_close_size,
                pos_size,
                close_side,
                client,
                fill_price=fill_price,
                network=selected_network,
                fill_data=close_fill_data,
                order_digest=(str(close_digest) or None),
                strategy_session_id=strategy_session_id,
            )
            leg_remaining -= this_close_size
            budget -= this_close_size
            total_filled += this_close_size

    post_positions = _normalize_net_positions(client.get_all_positions() or [])
    post_pos = post_positions.get(product_id)
    post_open_orders = 0
    for snd in _order_sender_params(client, selected_network):
        post_open_orders += len(client.get_open_orders(product_id, sender=snd) or [])
    if full_close_requested:
        still_open = bool(post_pos and abs(float(post_pos.get("signed_amount", 0) or 0)) > 0)
        if still_open or post_open_orders:
            detail = []
            if still_open:
                detail.append("position still open")
            if post_open_orders:
                detail.append(f"{post_open_orders} open orders remain")
            return {
                "success": False,
                "error": f"Close verification failed for {product}: {', '.join(detail)}.",
            }

    payload = {
        "success": True,
        "cancelled": total_filled if total_filled > 0 else close_size,
        "product": get_product_name(product_id, network=selected_network),
    }
    if cancelled_orders:
        payload["cancelled_orders"] = cancelled_orders
    if order_errors:
        payload["order_errors"] = order_errors
    return payload


def _read_spot_balance(readonly, spot_product_id: int) -> float:
    balances = (readonly.get_balance() or {}).get("balances", {}) or {}
    return float(balances.get(spot_product_id, balances.get(str(spot_product_id), 0)) or 0.0)


def _spot_mid(readonly, spot_product_id: int) -> float:
    try:
        return float((readonly.get_market_price(spot_product_id) or {}).get("mid") or 0.0)
    except Exception:  # noqa: BLE001 - valuation is best-effort
        return 0.0


def _close_spot_leg_with_sweep(
    telegram_id: int,
    asset: str,
    spot_product_id: int,
    spot_symbol: str,
    *,
    network: str,
    slippage_pct: float,
    source: str,
    strategy_session_id: int | None,
    max_rounds: int = 3,
) -> dict:
    """Flatten the spot leg and VERIFY it, retrying up to ``max_rounds``.

    A single market sell can partial-fill on a thin book or be rejected, which
    is how the DN spot leg got stranded after a stop. Each round re-reads the
    balance and sells what remains; a remainder below the venue minimum can't be
    sold as its own order, so it's treated as flat (dust). If a real position is
    still held after all rounds, returns success=False with the exact residual
    so the stop path surfaces the exposure instead of hiding it."""
    readonly = get_user_readonly_client(telegram_id, network=network)
    if not readonly:
        return {"success": False, "error": "Readonly client unavailable for spot close."}

    last_sell: dict = {"success": True, "skipped": True, "spot_size": 0.0}
    rounds = 0
    for _ in range(max_rounds):
        size = _read_spot_balance(readonly, spot_product_id)
        if size <= 0:
            return {"success": True, "spot_size": 0.0, "rounds": rounds, "skipped": rounds == 0}
        mid = _spot_mid(readonly, spot_product_id)
        if mid > 0 and size * mid < MIN_TRADE_SIZE_USD:
            # Sub-minimum remainder — un-sellable as its own order; treat as flat.
            return {"success": True, "spot_size": size, "residual_value_usd": size * mid,
                    "rounds": rounds, "dust": True}
        rounds += 1
        last_sell = execute_spot_market_order(
            telegram_id, asset, size, is_buy=False, enforce_rate_limit=False,
            slippage_pct=slippage_pct, source=source, strategy_session_id=strategy_session_id,
            network=network, spot_product_id=int(spot_product_id), spot_symbol=spot_symbol,
            asset_label=spot_symbol,
        )
        if not last_sell.get("success"):
            break  # rejection (e.g. sub-min) — verify below, don't spin

    # Final verification — flat, dust-only, or genuinely still exposed.
    final_size = _read_spot_balance(readonly, spot_product_id)
    mid = _spot_mid(readonly, spot_product_id)
    final_value = final_size * mid if mid > 0 else 0.0
    if final_size <= 0 or (mid > 0 and final_value < MIN_TRADE_SIZE_USD):
        return {"success": True, "spot_size": final_size, "residual_value_usd": final_value,
                "rounds": rounds}
    return {
        "success": False,
        "error": (
            f"{spot_symbol} spot leg NOT fully closed after {rounds} attempt(s): "
            f"{final_size:,.6f} (~${final_value:,.2f}) still held — "
            f"{last_sell.get('error') or 'close it manually from Portfolio.'}"
        ),
        "spot_size": final_size,
        "residual_value_usd": final_value,
        "rounds": rounds,
    }


def close_delta_neutral_legs(
    telegram_id: int,
    asset: str,
    network: str | None = None,
    slippage_pct: float = 1.0,
    source: str = "dn",
    strategy_session_id: int | None = None,
    pair: dict | None = None,
) -> dict:
    asset = str(asset or "").upper().replace("-PERP", "").strip()
    if not asset:
        return {"success": False, "error": "Asset is required to close delta-neutral legs."}

    user = get_user(telegram_id)
    active_network = user.network_mode.value if user else "mainnet"
    selected_network = str(network or active_network)

    readonly = get_user_readonly_client(telegram_id, network=selected_network)
    if not readonly:
        return {"success": False, "error": "Readonly client unavailable for DN close."}
    dn_pair = dict(pair or get_dn_pair(asset, network=selected_network) or {})
    perp_product = str(dn_pair.get("product") or asset).upper()
    spot_product_id = dn_pair.get("spot_product_id")
    spot_symbol = str(dn_pair.get("spot_symbol") or asset).upper()

    perp_result: dict = {"success": True, "skipped": True}
    try:
        close_result = close_position(
            telegram_id,
            perp_product,
            network=selected_network,
            source=source,
            strategy_session_id=strategy_session_id,
        )
        if close_result.get("success") or "No open positions" in str(close_result.get("error") or ""):
            perp_result = close_result if close_result.get("success") else {"success": True, "skipped": True}
        else:
            perp_result = close_result
    except Exception as e:
        perp_result = {"success": False, "error": str(e)}

    spot_result: dict = {"success": True, "skipped": True, "spot_size": 0.0}
    try:
        if spot_product_id is None:
            spot_product_id = get_spot_product_id(asset, network=selected_network)
        if spot_product_id is not None:
            # Verify-and-sweep: retry until the spot leg is provably flat (or
            # only dust remains). A single sell could partial-fill or be rejected
            # and silently leave the user exposed after a stop.
            spot_result = _close_spot_leg_with_sweep(
                telegram_id,
                asset,
                int(spot_product_id),
                spot_symbol,
                network=selected_network,
                slippage_pct=slippage_pct,
                source=source,
                strategy_session_id=strategy_session_id,
            )
    except Exception as e:
        spot_result = {"success": False, "error": str(e)}

    success = bool(perp_result.get("success")) and bool(spot_result.get("success"))
    if success:
        return {
            "success": True,
            "asset": asset,
            "perp": perp_result,
            "spot": spot_result,
        }

    return {
        "success": False,
        "asset": asset,
        "error": perp_result.get("error") or spot_result.get("error") or "Failed to close delta-neutral legs.",
        "perp": perp_result,
        "spot": spot_result,
    }


def cancel_resting_orders_for_user(
    telegram_id: int, network: str | None = None, *, only_pid: int | None = None
) -> dict:
    """Cancel every resting order for the user (optionally one product) WITHOUT
    flattening positions — the venue half of an engine stop that has no local
    controller to do it (a redeploy stand-down, a cross-process stop). Same
    DENIED-vs-EMPTY discipline as ``close_all_positions``: an unreadable book is
    reported, never treated as clear."""
    user = get_user(telegram_id)
    active_network = user.network_mode.value if user else "mainnet"
    selected_network = str(network or active_network)
    client = get_user_nado_client(telegram_id, network=selected_network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required."}
    known_pids: list[int] = []
    try:
        from src.nadobro.models.database import get_open_order_product_ids, get_open_position_product_ids
        known_pids = list(get_open_order_product_ids(telegram_id, selected_network))
        known_pids += list(get_open_position_product_ids(telegram_id, selected_network))
    except Exception:  # policy: degrade-ok(scope hint only; an unknown book still fails loud below)
        pass
    if only_pid is not None:
        known_pids.append(int(only_pid))
    resting, errors = _resting_orders_by_sender(
        client, only_pid=only_pid, known_pids=known_pids, network=selected_network,
    )
    cancelled, cancel_errors = _cancel_resting_orders(client, resting)
    errors.extend(cancel_errors)
    ok = not _book_unknown(errors) and not (cancelled == 0 and errors)
    out = {"success": ok, "cancelled_orders": cancelled, "order_errors": errors}
    if not ok:
        out["error"] = "Could not confirm the order book is clear: " + "; ".join(errors[:4])
    return out


def close_all_positions(
    telegram_id: int, network: str | None = None, only_product: str | None = None,
    strategy_session_id: int | None = None, **kwargs
) -> dict:
    user = get_user(telegram_id)
    active_network = user.network_mode.value if user else "mainnet"
    selected_network = str(network or active_network)
    client = get_user_nado_client(telegram_id, network=selected_network)
    if not client:
        return {"success": False, "error": "Wallet not initialized or key migration required."}

    # Scope: a strategy stop only needs to flatten the one product it traded.
    # When ``only_product`` resolves to a pid, cancel/close JUST that market.
    # Unresolvable/None means every market — which the batched order read below
    # covers at the same cost, so an unscoped manual "close all" is no longer
    # the slow path it used to be.
    only_pid = (
        get_product_id(only_product, network=selected_network, client=client)
        if only_product else None
    )

    # Cancel on default + isolated margin subaccounts (orders rest on the same
    # sender as quotes). ONE batched read per sender — see
    # ``_resting_orders_by_sender`` for why this must not fan out per product.
    # Products that can hold an order (for the fallback when the batched read is
    # unknown): the DB's open rows + open positions + the scoped product.
    known_pids: list[int] = []
    try:
        from src.nadobro.models.database import get_open_order_product_ids, get_open_position_product_ids
        known_pids = list(get_open_order_product_ids(telegram_id, selected_network))
        known_pids += list(get_open_position_product_ids(telegram_id, selected_network))
    except Exception:  # policy: degrade-ok(scope hint only; an unknown book still fails loud below)
        pass
    if only_pid is not None:
        known_pids.append(int(only_pid))
    resting, order_errors = _resting_orders_by_sender(
        client, only_pid=only_pid, known_pids=known_pids, network=selected_network,
    )
    cancelled_orders, cancel_errors = _cancel_resting_orders(client, resting)
    order_errors.extend(cancel_errors)

    net_positions = _normalize_net_positions(client.get_all_positions() or [])
    if not net_positions:
        # FAIL LOUD (2026-09-03): "no positions" is only a clean result when the
        # book was actually READ. An unreadable book, or errors with nothing
        # cancelled, must not report success — the session rail turns success
        # into a "TP hit / cleanup done" message over orders that still rest.
        if _book_unknown(order_errors) or (cancelled_orders == 0 and order_errors):
            return {
                "success": False,
                "cancelled": 0.0,
                "products": [],
                "cancelled_orders": cancelled_orders,
                "order_errors": order_errors,
                "error": "Could not confirm the order book is clear: " + "; ".join(order_errors[:4]),
            }
        if cancelled_orders > 0 and not order_errors:
            return {
                "success": True,
                "cancelled": 0.0,
                "products": [],
                "cancelled_orders": cancelled_orders,
            }
        if cancelled_orders > 0:
            return {
                "success": True,
                "cancelled": 0.0,
                "products": [],
                "cancelled_orders": cancelled_orders,
                "order_errors": order_errors,
            }
        return {
            "success": True,
            "cancelled": 0.0,
            "products": [],
        }

    from src.nadobro.users.settings_service import get_user_settings

    _, settings = get_user_settings(telegram_id)
    leverage = max(1.0, float(settings.get("default_leverage", 3) or 3))

    cancelled = 0.0
    errors = []
    products_closed = set()
    for pid, p in net_positions.items():
        if only_pid is not None and int(pid) != int(only_pid):
            continue
        for leg in _iter_position_legs(p):
            try:
                signed_amount = float(leg.get("signed_amount", 0) or 0)
                pos_size = abs(signed_amount)
                if pos_size <= 0:
                    continue
                is_buy = signed_amount < 0
                sub_hint = leg.get("subaccount")
                iso_only = is_product_id_isolated_only(pid, network=selected_network, client=client)
                iso_margin = None
                if iso_only:
                    mp = client.get_market_price(pid)
                    mid = float(mp.get("mid", 0) or 0)
                    if mid > 0:
                        iso_margin = (float(pos_size) * mid) / leverage
                r = client.place_market_order(
                    pid,
                    pos_size,
                    is_buy=is_buy,
                    slippage_pct=1.0,
                    reduce_only=True,
                    isolated_only=iso_only,
                    isolated_margin=iso_margin,
                    sender=sub_hint,
                )
                if r["success"]:
                    cancelled += pos_size
                    product_name = leg.get("product_name", p.get("product_name", get_product_name(pid, network=selected_network)))
                    products_closed.add(product_name)
                    close_side = "short" if signed_amount > 0 else "long"
                    close_digest = r.get("digest", "")
                    # Link the close digest → session (source='strategy') at
                    # placement so the venue-synced close fill is attributed to the
                    # session and COUNTS toward its volume. A session's volume is
                    # turnover (opens + closes); without this the flatten's ~equal
                    # close volume orphaned as source='manual', session=null (it
                    # fills after stopped_at, so the window fallback can't reach it).
                    # Session-less flattens (/stop_all) are tagged manual so the
                    # window fallback can't swallow them into an unrelated live
                    # session on the same product.
                    if close_digest:
                        try:
                            from src.nadobro.trading.order_intents import link_digest_intent

                            if strategy_session_id:
                                link_digest_intent(
                                    str(close_digest), selected_network,
                                    source="strategy",
                                    strategy_session_id=int(strategy_session_id),
                                    product_id=pid,
                                    product_name=product_name,
                                )
                            else:
                                link_digest_intent(
                                    str(close_digest), selected_network, source="manual",
                                    product_id=pid,
                                    product_name=product_name,
                                )
                        # policy: degrade-ok(close link best-effort; close fill still records, attribution falls back to window)
                        except Exception:  # noqa: BLE001 - link is best-effort
                            pass
                    close_fill_data = _resolve_fill_data(client, close_digest, selected_network) if close_digest else None
                    fill_price = (close_fill_data or {}).get("fill_price") or _get_post_fill_price(client, pid)
                    # Pass the digest through: the recorder row is the venue
                    # sync's fallback source for product AND session when the
                    # order_intents write above was lost — without the digest
                    # the synced close fill lands product_id=0/unattributed
                    # (invisible to History, missing from the session rollup).
                    _record_close_in_db(
                        telegram_id,
                        pid,
                        pos_size,
                        pos_size,
                        close_side,
                        client,
                        fill_price=fill_price,
                        network=selected_network,
                        fill_data=close_fill_data,
                        order_digest=(str(close_digest) or None),
                        strategy_session_id=(int(strategy_session_id) if strategy_session_id else None),
                    )
                else:
                    errors.append(
                        f"{leg.get('product_name', get_product_name(pid, network=selected_network))}: "
                        f"{r.get('error', 'unknown')}"
                    )
            except Exception as e:
                errors.append(f"{leg.get('product_name', 'unknown')}: {str(e)}")

    if cancelled == 0 and errors:
        all_errors = list(errors)
        if order_errors:
            all_errors.extend(order_errors)
        return {"success": False, "error": f"Failed to close positions: {'; '.join(all_errors)}"}

    result = {
        "success": True,
        "cancelled": cancelled,
        "products": list(products_closed),
    }
    if cancelled_orders:
        result["cancelled_orders"] = cancelled_orders
    if order_errors:
        result["order_errors"] = order_errors
    # Verify flatten succeeded before reporting success to callers that notify users.
    post_positions = _normalize_net_positions(client.get_all_positions() or [])
    if only_pid is not None:
        # Scoped close: only verify the product we actually flattened, so an
        # unrelated position on another market can't false-fail a strategy stop.
        post_positions = {pid: p for pid, p in post_positions.items() if int(pid) == int(only_pid)}
    if post_positions:
        result["success"] = False
        result["error"] = (
            "Close-all verification failed: open positions remain on "
            + ", ".join(get_product_name(pid, network=selected_network) for pid in post_positions.keys())
        )
    still_resting, verify_errors = _resting_orders_by_sender(client, only_pid=only_pid)
    remaining_orders = sum(len(d) for d in still_resting.values())
    for err in verify_errors:
        logger.warning(
            "open-orders read failed during stop-all report — remaining-orders "
            "count may understate: %s", err,
        )
    if remaining_orders > 0:
        result["success"] = False
        existing_error = result.get("error", "")
        suffix = f"{remaining_orders} open orders remain."
        result["error"] = f"{existing_error} {suffix}".strip()
    return result


def get_trade_history(telegram_id: int, limit: int = 20) -> list:
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    requested = max(1, int(limit or 20))
    # Over-fetch modestly because logical rows can merge related open/close records.
    fetch_limit = min(max(requested * 3, requested), 500)
    trades = get_trades_by_user(telegram_id, limit=fetch_limit, network=network)
    logical_rows = _build_logical_trade_rows(trades)
    for row in logical_rows:
        row["network"] = network
    return logical_rows[:requested]


def get_open_limit_orders(telegram_id: int, refresh: bool = False) -> list[dict]:
    """
    Return currently open exchange orders for the active user/network, enriched
    with local trade metadata so the UI can display pending/partial states.
    """
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    client = get_user_readonly_client(telegram_id, network=network)
    if not client:
        return []

    try:
        trades = get_trades_by_user(telegram_id, limit=500, network=network) or []
    except Exception:
        trades = []

    pending_like = {
        TradeStatus.PENDING.value,
        TradeStatus.PARTIALLY_FILLED.value,
    }
    by_digest: dict[str, dict] = {}
    for t in trades:
        digest = str(t.get("order_digest") or "").strip()
        status = str(t.get("status") or "").lower()
        if status in pending_like and digest:
            by_digest[digest] = t

    rows: list[dict] = []
    try:
        all_open_orders = client.get_all_open_orders(refresh=refresh) or []
    except Exception:
        all_open_orders = []

    for order in all_open_orders:
        digest = str(order.get("digest") or "").strip()
        trade = by_digest.get(digest)
        db_status = str((trade or {}).get("status") or "").lower()
        status_label = "pending"
        if db_status == TradeStatus.PARTIALLY_FILLED.value:
            status_label = "partially filled"
        order_type = str((trade or {}).get("order_type") or "LIMIT").upper()
        created_at = (trade or {}).get("created_at") or ""
        requested_size = float((trade or {}).get("size") or order.get("amount") or 0)
        filled_size = float((trade or {}).get("fill_size") or 0)
        rows.append(
            {
                "digest": digest,
                "type": order_type,
                "side": str(order.get("side") or (trade or {}).get("side") or "").upper(),
                "product": str(order.get("product_name") or (trade or {}).get("product_name") or ""),
                "size": float(order.get("amount") or 0),
                "limit_price": float(order.get("price") or 0),
                "created_at": _trade_ts_display(created_at),
                "status": status_label,
                "requested_size": requested_size,
                "filled_size": max(0.0, filled_size),
            }
        )

    rows.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return rows


def get_trade_analytics(telegram_id: int, strategy_session_id: int | None = None) -> dict:
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    target_session_id = None
    if strategy_session_id is not None:
        try:
            target_session_id = int(strategy_session_id)
        except (TypeError, ValueError):
            target_session_id = None
    analytics_limit = 5000 if target_session_id is None else 2000
    trades = get_trades_by_user(
        telegram_id,
        limit=analytics_limit,
        network=network,
        strategy_session_id=target_session_id,
    )
    if not trades:
        return {"total_trades": 0}
    logical_trades = _build_logical_trade_rows(trades)
    total = len(logical_trades)
    filled = [t for t in logical_trades if str(t.get("status") or "").lower() == TradeStatus.FILLED.value]
    closed = [t for t in logical_trades if str(t.get("status") or "").lower() == TradeStatus.CLOSED.value]
    failed = [t for t in trades if t.get("status") == TradeStatus.FAILED.value]
    completed = closed
    def _realized_pnl(trade: dict):
        value = trade.get("pnl")
        if value is None:
            value = trade.get("realized_pnl")
        return value

    pnl_trades = [t for t in completed if _realized_pnl(t) is not None]
    total_pnl = sum(float(_realized_pnl(t)) for t in pnl_trades) if pnl_trades else 0
    decisive_pnl_trades = [t for t in pnl_trades if abs(float(_realized_pnl(t))) > 1e-12]
    wins = len([t for t in decisive_pnl_trades if float(_realized_pnl(t)) > 0])
    losses = len([t for t in decisive_pnl_trades if float(_realized_pnl(t)) < 0])
    win_rate = (wins / len(decisive_pnl_trades) * 100) if decisive_pnl_trades else 0
    now = datetime.now(timezone.utc)
    volume_windows = {"24h": 0.0, "7d": 0.0, "30d": 0.0, "all": 0.0}
    total_fees = 0.0
    total_funding = 0.0
    for trade in trades:
        status = str(trade.get("status") or "").lower()
        if status not in (TradeStatus.FILLED.value, TradeStatus.CLOSED.value, TradeStatus.PARTIALLY_FILLED.value):
            continue
        notional = _trade_notional_usd(trade)
        fee_value = _trade_total_fee_value(trade)
        funding_value = float(trade.get("funding_paid") or 0.0)
        event_ts = _trade_event_time_for_windows(trade)
        volume_windows["all"] += notional
        total_fees += fee_value
        total_funding += funding_value
        if event_ts:
            age = now - event_ts
            if age <= timedelta(days=1):
                volume_windows["24h"] += notional
            if age <= timedelta(days=7):
                volume_windows["7d"] += notional
            if age <= timedelta(days=30):
                volume_windows["30d"] += notional
    total_volume = volume_windows["all"]

    by_product = {}
    for t in pnl_trades:
        product = (t.get("product") or t.get("product_name") or "Unknown").replace("-PERP", "")
        if product not in by_product:
            by_product[product] = {"pnl": 0.0, "count": 0}
        by_product[product]["pnl"] += float(_realized_pnl(t))
        by_product[product]["count"] += 1
    by_product = dict(sorted(by_product.items(), key=lambda x: abs(x[1]["pnl"]), reverse=True))

    return {
        "total_trades": total,
        "filled": len(filled),
        "closed": len(closed),
        "failed": len(failed),
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "total_volume": total_volume,
        "volume_windows": {k: round(v, 4) for k, v in volume_windows.items()},
        "total_fees": round(total_fees, 6),
        "total_funding": round(total_funding, 6),
        "by_product": by_product,
    }


def get_account_and_performance_snapshot(telegram_id: int, prefer_cli: bool = False) -> dict:
    account = get_account_snapshot(telegram_id, prefer_cli=prefer_cli)
    performance = get_trade_analytics(telegram_id)
    return {
        "success": bool(account.get("success")),
        "account_source": account.get("source"),
        "account": account.get("data"),
        "account_error": account.get("error"),
        "performance": performance,
    }


# ---------------------------------------------------------------------------
# Round-trip aggregator for the History tab
# ---------------------------------------------------------------------------
def compute_round_trips(
    telegram_id: int,
    network: str,
    *,
    limit: int = 100,
) -> list[dict]:
    """FIFO-pair manual fills into round-trips.

    Walks every ``trades_<network>`` row owned by ``telegram_id`` that is
    NOT tagged with a ``strategy_session_id`` (i.e. text-to-trade,
    manual buttons, or venue-sync rows that couldn't be back-linked to a
    strategy intent). Lots are matched per
    ``(product_id, isolated)`` with FIFO; each close emits a round-trip
    record.

    The result is sorted newest-first by ``close_ts`` and capped at
    ``limit``. The ``trip_key`` is the close trade id (stable for the
    Share PnL callback ``portfolio:share_pnl:rt:{key}``).
    """
    from src.nadobro.db import query_all as _query_all

    def _history_epoch() -> str:
        # Override with NADO_HISTORY_EPOCH (ISO timestamp) if pre-fix rows are
        # ever repaired enough to extend the window backwards.
        import os

        from src.nadobro.connectors.provider_config import clean_env_value

        return clean_env_value(os.environ.get("NADO_HISTORY_EPOCH")) or "2026-07-09T00:00:00+00:00"

    table = "trades_testnet" if str(network).lower() == "testnet" else "trades_mainnet"
    try:
        rows = _query_all(
            f"""
            SELECT id, product_id, product_name, side, status,
                   COALESCE(fill_size, size, 0) AS sz,
                   COALESCE(NULLIF(fill_price, 0), price, 0) AS px,
                   COALESCE(fill_fee, fees, 0) + COALESCE(builder_fee, 0) AS fee,
                   COALESCE(realized_pnl, pnl, 0) AS realized_pnl,
                   COALESCE(funding_paid, 0) AS funding_paid,
                   COALESCE(isolated, false) AS isolated,
                   COALESCE(leverage, 0) AS leverage,
                   order_type,
                   COALESCE(filled_at, created_at) AS ts
            FROM {table}
            WHERE user_id = %s
              AND strategy_session_id IS NULL
              AND COALESCE(source, 'manual') = 'manual'
              AND status IN ('filled', 'closed', 'partially_filled')
              -- Authoritative venue fills only. submission_idx is UNIQUE (dedup
              -- index) so each real fill appears once, and the bot's synthetic
              -- ``MARKET_CLOSE`` rows (account-wide, no submission_idx) are excluded
              -- so they can't double-count or over-close the FIFO inventory.
              AND submission_idx IS NOT NULL
              -- Product-less venue fills (product_id 0 — this indexer's match feed
              -- carries no product_id) can't be paired per product without mixing
              -- BTC with ETH, so they're excluded rather than shown WRONG. The
              -- recording fix (resolve product_id at insert) lands separately.
              AND COALESCE(product_id, 0) <> 0
              -- History epoch: before 2026-07-09 the manual stream has PROVABLE
              -- holes (desk fills unrecorded, ~500 unattributable product_id=0
              -- legs), so FIFO pairings crossing that era fabricate multi-day
              -- holds (an old lot whose real close is invisible gets "closed"
              -- by a much later fill). Pair only the complete-recording era.
              AND COALESCE(filled_at, created_at) >= %s::timestamptz
            ORDER BY COALESCE(filled_at, created_at) ASC, id ASC
            """,
            (int(telegram_id), _history_epoch()),
        )
    except Exception:
        logger.warning("compute_round_trips failed user=%s", telegram_id, exc_info=True)
        return []

    inventory: dict[tuple[int, bool], list[dict]] = {}
    round_trips: list[dict] = []
    for row in rows or []:
        side = str(row.get("side") or "").lower()
        order_type = str(row.get("order_type") or "").upper()
        product_id = int(row.get("product_id") or 0)
        isolated = bool(row.get("isolated"))
        key = (product_id, isolated)
        size = float(row.get("sz") or 0)
        if size <= 0:
            continue
        price = float(row.get("px") or 0)
        # A fill without a resolvable price carries NO price information: queued
        # as an open lot at $0 it turns every later close into "entry @ $0.00,
        # PnL = full exit notional"; as a close it emits an all-zero trip. Same
        # philosophy as the product_id=0 exclusion above — skip rather than show
        # WRONG. (Migration 0015 + the match-sync human-column backfill repair
        # the rows that have the venue x18 quote; the rest have no price data.)
        if price <= 0:
            continue
        fee = float(row.get("fee") or 0)
        funding = float(row.get("funding_paid") or 0)
        leverage = float(row.get("leverage") or 0)
        ts = row.get("ts")
        product_name = str(row.get("product_name") or "")
        # Reduce-only closes (close orders) always pop the FIFO regardless
        # of side. Otherwise we pop opposite-side lots and queue same-side.
        is_close = "CLOSE" in order_type
        queued = inventory.setdefault(key, [])
        existing_side = queued[0]["side"] if queued else None
        opposite = existing_side and existing_side != side
        if is_close or opposite:
            remaining = size
            close_fees = fee
            close_funding = funding
            open_lots: list[dict] = []
            while remaining > 1e-12 and queued:
                lot = queued[0]
                take = min(lot["size"], remaining)
                lot_share = take / lot["size"] if lot["size"] > 0 else 1.0
                open_lots.append({
                    "trade_id": lot["trade_id"],
                    "size": take,
                    "price": lot["price"],
                    "ts": lot["ts"],
                    "fees": lot["fees"] * lot_share,
                    "funding": lot["funding"] * lot_share,
                    "leverage": lot.get("leverage") or 0.0,
                })
                lot["size"] -= take
                lot["fees"] -= lot["fees"] * lot_share
                lot["funding"] -= lot["funding"] * lot_share
                remaining -= take
                if lot["size"] <= 1e-12:
                    queued.pop(0)
            if open_lots:
                total_open_qty = sum(l["size"] for l in open_lots) or 1.0
                avg_open = sum(l["price"] * l["size"] for l in open_lots) / total_open_qty
                open_fees = sum(l["fees"] for l in open_lots)
                open_funding = sum(l["funding"] for l in open_lots)
                # Both legs use the MATCHED quantity. Using the close row's full
                # size overstated a partially-matched close (its leftover opens a
                # new lot whose notional is counted again when THAT trip closes).
                volume_usd = total_open_qty * (avg_open + price)
                # Realized PnL is DERIVED from open/close prices, NOT read from the
                # per-fill ``realized_pnl`` column — this venue reports none, so that
                # column is always 0 and every venue-closed manual trade showed PnL 0.
                # Gross of fees (fees are a separate field), consistent with the
                # session/portfolio surfaces. FIFO-derived total over a full
                # round-trip equals the avg-cost engine's (pair_fills_into_trades).
                opened_long = existing_side in ("long", "buy")
                derived_realized = sum(
                    ((price - l["price"]) if opened_long else (l["price"] - price)) * l["size"]
                    for l in open_lots
                )
                round_trips.append({
                    "trip_key": str(row.get("id")),
                    "product_id": product_id,
                    "product_name": product_name,
                    "pair": product_name,
                    "isolated": isolated,
                    "side": "long" if existing_side in ("long", "buy") else "short",
                    "size": min(total_open_qty, size),
                    "leverage": open_lots[0].get("leverage") or 0.0,
                    "avg_open_price": avg_open,
                    "avg_close_price": price,
                    "open_ts": open_lots[0]["ts"],
                    "close_ts": ts,
                    "fees": open_fees + close_fees,
                    "funding_paid": open_funding + close_funding,
                    "realized_pnl": derived_realized,
                    "volume_usd": volume_usd,
                })
            if remaining > 1e-12:
                # Close was bigger than inventory — record the leftover as
                # an opening lot for the opposite side.
                queued.append({
                    "trade_id": int(row.get("id") or 0),
                    "side": "long" if side in ("long", "buy") else "short",
                    "size": remaining,
                    "price": price,
                    "fees": fee * (remaining / size),
                    "funding": funding * (remaining / size),
                    "leverage": leverage,
                    "ts": ts,
                })
        else:
            queued.append({
                "trade_id": int(row.get("id") or 0),
                "side": side if side in ("long", "short", "buy", "sell") else "long",
                "size": size,
                "price": price,
                "fees": fee,
                "funding": funding,
                "leverage": leverage,
                "ts": ts,
            })

    round_trips.sort(key=lambda r: (r.get("close_ts") or ""), reverse=True)
    return round_trips[: max(1, int(limit))]


def find_round_trip(telegram_id: int, network: str, trip_key: str) -> dict | None:
    """Return the single round-trip whose ``trip_key`` matches.

    Uses :func:`compute_round_trips` so the result is consistent with the
    History UI. Returns ``None`` when no match is found.
    """
    target = str(trip_key)
    for trip in compute_round_trips(telegram_id, network, limit=500):
        if str(trip.get("trip_key")) == target:
            return trip
    return None
