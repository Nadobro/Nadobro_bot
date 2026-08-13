"""Portfolio callback handlers (portfolio:* callback data).

Extracted from callbacks.py (decomposition slice, 2026-06). May import
shared utils from callbacks at module level — callbacks only imports this
module lazily inside its _handle_portfolio shim, so there is no cycle.

Responsiveness contract (2026-06 perf fix): the read-only views (overview,
positions, history) must answer a tap from the in-process snapshot cache
immediately and let a background task refresh + edit the message in place.
A tap must NEVER wait on the venue read storm inline — that was producing
24-55s callbacks whenever the gateway was throttled or the per-user sync
lock was held by the background poller. Destructive actions (cancel/close)
still run the venue mutation inline (correctness of the flatten), but the
post-close deck refresh is backgrounded — awaiting a forced sync after a
successful close left the UI stuck on "Closing all positions…" for minutes
even after History already showed the trade closed (CLOSE-ALL-UI 2026-07-31).
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.nadobro.handlers.keyboards import portfolio_analytics_kb
from src.nadobro.core.async_utils import run_blocking, run_blocking_sdk
from src.nadobro.core.perf import timed_metric
from src.nadobro.trading.trade_service import close_all_positions
from src.nadobro.users.user_service import get_user_nado_client, get_user
from src.nadobro.utils.env import env_float
from telegram.constants import ParseMode
from telegram.error import BadRequest

from src.nadobro.handlers.callbacks import _edit_loc  # noqa: E402

logger = logging.getLogger(__name__)

# Serve cached renders instantly; kick a background refresh when the cache
# is older than this. The portfolio poller + WS invalidation keep actively
# trading users well under it, so the late edit is the exception, not the rule.
_CACHE_FRESH_SECONDS = 3.0

# Background venue sync ceiling. The tap already painted a real deck; this
# only bounds how long "Refreshing…" can linger if the gateway wedges.
_BG_SYNC_TIMEOUT_SECONDS = env_float("NADO_PORTFOLIO_BG_SYNC_TIMEOUT_SECONDS", 25.0)

# One in-flight background refresh per (chat, view); a second tap while one
# is running just re-renders the cache and rides the existing refresh.
_BG_REFRESH: dict[tuple[int, str], asyncio.Task] = {}


def _cached_snapshot(telegram_id: int, network: str | None):
    from src.nadobro.venue.nado_sync import get_cached_snapshot, mark_user_active

    mark_user_active(int(telegram_id))
    return get_cached_snapshot(int(telegram_id), network)


def _snapshot_age_s(snapshot: dict) -> float:
    try:
        return max(0.0, time.time() - float(snapshot.get("monotonic_ts") or 0))
    except (TypeError, ValueError):
        return float("inf")


# The interaction sequence each in-flight refresh expects at edit time.
# Re-tapping the SAME view while a refresh runs updates the expectation
# (the user still wants this screen); any OTHER tap moves the chat's
# sequence past it, so the late edit is dropped instead of clobbering
# whatever screen the user navigated to.
_BG_EXPECT_SEQ: dict[tuple[int, str], int] = {}


def _spawn_background_refresh(query, telegram_id: int, view_key: str, render_fresh, *, force: bool = False) -> None:
    """Refresh the snapshot off the tap path, then edit the message in place.

    ``render_fresh(snapshot) -> (text, kb)`` runs after the sync completes.
    Errors are non-fatal: the user already has the cached render on screen.
    """
    from src.nadobro.handlers.callbacks import interaction_seq

    chat = getattr(getattr(query, "message", None), "chat_id", None) or int(telegram_id)
    key = (int(chat), view_key)
    # Always (re)arm the expectation for this view — a second tap on the
    # same view while a refresh is in flight must keep that refresh's edit
    # deliverable (the tap bumped the chat sequence).
    _BG_EXPECT_SEQ[key] = interaction_seq(int(chat))
    existing = _BG_REFRESH.get(key)
    if existing and not existing.done():
        return

    async def _paint(snapshot) -> None:
        if interaction_seq(int(chat)) != _BG_EXPECT_SEQ.get(key):
            logger.debug(
                "portfolio bg refresh dropped (user navigated) user=%s view=%s",
                telegram_id, view_key,
            )
            return
        text, kb = render_fresh(snapshot)
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)

    async def _fallback_snapshot():
        from src.nadobro.handlers.portfolio_deck import empty_portfolio_snapshot
        from src.nadobro.venue.nado_sync import get_cached_snapshot

        return get_cached_snapshot(int(telegram_id)) or empty_portfolio_snapshot(telegram_id)

    async def _job():
        try:
            from src.nadobro.handlers.portfolio_deck import snapshot_for_user

            snapshot = await asyncio.wait_for(
                snapshot_for_user(telegram_id, force=force, reason="ui"),
                timeout=_BG_SYNC_TIMEOUT_SECONDS,
            )
            await _paint(snapshot)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.warning("portfolio bg refresh edit failed user=%s: %s", telegram_id, e)
        except asyncio.TimeoutError:
            logger.warning(
                "portfolio bg refresh timed out user=%s view=%s after %.0fs",
                telegram_id, view_key, _BG_SYNC_TIMEOUT_SECONDS,
            )
            try:
                await _paint(await _fallback_snapshot())
            except Exception:
                logger.warning(
                    "portfolio bg refresh fallback failed user=%s view=%s",
                    telegram_id, view_key, exc_info=True,
                )
        except Exception as e:
            logger.warning("portfolio bg refresh failed user=%s view=%s: %s", telegram_id, view_key, e)
            try:
                await _paint(await _fallback_snapshot())
            except Exception:
                logger.warning(
                    "portfolio bg refresh fallback failed user=%s view=%s",
                    telegram_id, view_key, exc_info=True,
                )
        finally:
            _BG_REFRESH.pop(key, None)
            _BG_EXPECT_SEQ.pop(key, None)

    _BG_REFRESH[key] = asyncio.create_task(_job())


async def _handle_portfolio(query, data, telegram_id):
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "view"
    try:
        user = get_user(telegram_id)
    except Exception:
        user = None
    mode_label = None
    if user:
        mode_label = user.network_mode.value.upper()

    if action == "close_all_confirm":
        from src.nadobro.handlers.portfolio_deck import render_close_all_confirm

        # Portfolio is the HTML domain: declare it even for plain confirm
        # text so the whole domain is uniform (and statically checkable).
        text, kb = render_close_all_confirm()
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action == "close_all_yes":
        # Keep a way out on the progress card. An edit without reply_markup
        # STRIPS the keyboard, so if the close chain dies mid-flight (deploy,
        # venue stall) the user is left staring at "Closing all positions…"
        # with no button to leave — exactly how a slow close looked stuck.
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        await _edit_loc(
            query, "⏳ Closing all positions…",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Portfolio", callback_data="portfolio:view")],
            ]),
        )
        network = user.network_mode.value if user else "mainnet"
        # SDK pool, NOT the 8-worker misc pool that renders every card: a venue
        # close holds its worker for the whole cancel/flatten/verify chain, so
        # running it here queued unrelated taps behind it (the callback.total
        # SLO breach). Matches the /status stop and text close-all paths.
        result = await run_blocking_sdk(close_all_positions, telegram_id, network=network)
        if isinstance(result, dict) and not result.get("success", False):
            await _edit_loc(
                query,
                f"⚠ Close all failed: {str(result.get('error') or result.get('message') or 'unknown error')[:240]}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Portfolio", callback_data="portfolio:view")],
                ]),
            )
            return
        # CLOSE-ALL-UI (2026-07-31): leave the progress text the moment the
        # venue close returns. A forced snapshot_for_user can still hang for
        # minutes under gateway throttling / the per-user sync lock — History
        # already showed Trade #2220 closed while this card stayed on
        # "Closing all positions…". Show the outcome now; refresh the deck
        # in the background (same pattern as the read-only portfolio views).
        products = [str(p) for p in (result.get("products") or []) if p]
        product_bits = ", ".join(products[:6]) if products else "all markets"
        if len(products) > 6:
            product_bits += f" (+{len(products) - 6} more)"
        cancelled = float(result.get("cancelled") or 0)
        size_bits = f" ({cancelled:g} base)" if cancelled else ""
        done_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh Portfolio", callback_data="portfolio:view")],
            [InlineKeyboardButton("🏠 Home", callback_data="nav:main")],
        ])
        await _edit_loc(
            query,
            f"✅ Closed positions on {product_bits}{size_bits}.",
            reply_markup=done_kb,
        )
        from src.nadobro.handlers.portfolio_deck import render_portfolio_deck

        _spawn_background_refresh(
            query,
            telegram_id,
            "overview",
            lambda snap: render_portfolio_deck(snap),
            force=True,
        )
        return

    if action == "cancel_all_confirm":
        from src.nadobro.handlers.orders_view import render_cancel_all_confirm

        text, kb = render_cancel_all_confirm()
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action == "cancel_all_yes":
        await _edit_loc(query, "⏳ Cancelling open orders…")
        from src.nadobro.handlers.portfolio_deck import snapshot_for_user
        from src.nadobro.handlers.positions_view import render_positions_view

        snapshot = await snapshot_for_user(telegram_id, force=True)
        client = await run_blocking(get_user_nado_client, telegram_id, snapshot.get("network"))
        if not client:
            await _edit_loc(query, "⚠ Cannot cancel orders: Nado client unavailable.")
            return
        plain_by_product: dict[int, list[str]] = {}
        trigger_by_product: dict[int, list[str]] = {}
        skipped = 0
        failures: list[str] = []
        for order in snapshot.get("open_orders") or []:
            try:
                pid = int(order.get("product_id"))
            except Exception:
                skipped += 1
                continue
            digest = str(order.get("digest") or order.get("order_digest") or "")
            if not digest:
                skipped += 1
                continue
            target = trigger_by_product if bool(order.get("is_trigger")) else plain_by_product
            target.setdefault(pid, []).append(digest)
        for pid, digests in plain_by_product.items():
            result = await client.cancel_orders(product_id=pid, digests=digests)
            if not result.get("success"):
                failures.append(str(result.get("error") or f"product {pid}"))
        for pid, digests in trigger_by_product.items():
            result = await client.cancel_trigger_orders(product_id=pid, digests=digests)
            if not result.get("success"):
                failures.append(str(result.get("error") or f"trigger product {pid}"))
        if failures:
            await _edit_loc(query, f"⚠ Some orders were not cancelled: {'; '.join(failures)[:240]}")
            return
        if skipped:
            logger.warning("portfolio_cancel_all_skipped_orders user=%s skipped=%s", telegram_id, skipped)
        snapshot = await snapshot_for_user(telegram_id, force=True)
        from src.nadobro.handlers.positions_view import render_positions_view

        text, kb = render_positions_view(snapshot)
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action == "cancel_order":
        await _edit_loc(query, "⏳ Cancelling order…")
        from src.nadobro.handlers.orders_view import sorted_orders
        from src.nadobro.handlers.portfolio_deck import snapshot_for_user
        from src.nadobro.handlers.positions_view import render_positions_view

        snapshot = await snapshot_for_user(telegram_id, force=True)
        orders = sorted_orders(snapshot)
        order = None
        if len(parts) > 3 and parts[2] == "d":
            # Digest-addressed cancel: immune to list reordering between
            # render and tap (a positional index re-resolved against a fresh
            # snapshot could cancel the WRONG order).
            want = parts[3].strip().lower()
            for candidate in orders:
                dg = str(candidate.get("digest") or candidate.get("order_digest") or "")
                if want and dg.lower().removeprefix("0x").startswith(want):
                    order = candidate
                    break
            if order is None:
                # Not an error: the order filled or was cancelled elsewhere.
                text, kb = render_positions_view(snapshot)
                await _edit_loc(
                    query,
                    "✓ That order is no longer open (filled or already cancelled).\n\n" + text,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
                return
        else:
            # Legacy positional index — only from buttons rendered before the
            # digest upgrade, or for venue rows that carry no digest.
            try:
                order_index = int(parts[2]) if len(parts) > 2 else -1
            except (TypeError, ValueError):
                order_index = -1
            if order_index < 0 or order_index >= len(orders):
                text, kb = render_positions_view(snapshot)
                await _edit_loc(query, "⚠ Order list changed. Please try again.\n\n" + text, reply_markup=kb, parse_mode=ParseMode.HTML)
                return
            order = orders[order_index]
        try:
            product_id = int(order.get("product_id"))
        except Exception:
            await _edit_loc(query, "⚠ Cannot cancel this order: missing product id.")
            return
        digest = str(order.get("digest") or order.get("order_digest") or "")
        if not digest:
            await _edit_loc(query, "⚠ Cannot cancel this order: missing order digest.")
            return
        client = await run_blocking(get_user_nado_client, telegram_id, snapshot.get("network"))
        if not client:
            await _edit_loc(query, "⚠ Cannot cancel order: Nado client unavailable.")
            return
        if bool(order.get("is_trigger")):
            result = await client.cancel_trigger_orders(product_id=product_id, digests=[digest])
        else:
            result = await client.cancel_orders(product_id=product_id, digests=[digest])
        if not result.get("success"):
            await _edit_loc(query, f"⚠ Cancel failed: {str(result.get('error') or 'unknown error')[:240]}")
            return
        snapshot = await snapshot_for_user(telegram_id, force=True)
        text, kb = render_positions_view(snapshot)
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action == "positions":
        # Combined Positions + Orders screen. Callbacks:
        #   portfolio:positions           -> both at page 0
        #   portfolio:positions:{n}       -> back-compat: both at page n
        #   portfolio:positions:pos:{n}   -> position section page n
        #   portfolio:positions:ord:{n}   -> order section page n
        from src.nadobro.handlers.positions_view import render_positions_view

        pos_page = None
        ord_page = None
        page = 0
        if len(parts) > 2:
            tail = parts[2:]
            if tail and tail[0].isdigit():
                page = int(tail[0])
            elif tail[0] == "pos" and len(tail) > 1 and tail[1].isdigit():
                pos_page = int(tail[1])
            elif tail[0] == "ord" and len(tail) > 1 and tail[1].isdigit():
                ord_page = int(tail[1])
        network = user.network_mode.value if user else None
        cached = _cached_snapshot(telegram_id, network)
        if cached:
            text, kb = render_positions_view(
                cached, page=page, pos_page=pos_page, ord_page=ord_page
            )
            try:
                await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            if _snapshot_age_s(cached) > _CACHE_FRESH_SECONDS:
                _spawn_background_refresh(
                    query, telegram_id, f"positions:{pos_page}:{ord_page}:{page}",
                    lambda s: render_positions_view(s, page=page, pos_page=pos_page, ord_page=ord_page),
                )
            return
        from src.nadobro.handlers.portfolio_deck import empty_portfolio_snapshot

        text, kb = render_positions_view(
            empty_portfolio_snapshot(telegram_id, network),
            page=page, pos_page=pos_page, ord_page=ord_page,
        )
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        _spawn_background_refresh(
            query, telegram_id, f"positions:{pos_page}:{ord_page}:{page}",
            lambda s: render_positions_view(s, page=page, pos_page=pos_page, ord_page=ord_page),
        )
        return

    if action == "orders":
        # Legacy alias: forward to the combined Positions screen so callers
        # following older callback_data still land somewhere sensible.
        from src.nadobro.handlers.positions_view import render_positions_view

        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        network = user.network_mode.value if user else None
        cached = _cached_snapshot(telegram_id, network)
        if cached:
            text, kb = render_positions_view(cached, ord_page=page)
            try:
                await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            if _snapshot_age_s(cached) > _CACHE_FRESH_SECONDS:
                _spawn_background_refresh(
                    query, telegram_id, f"orders:{page}",
                    lambda s: render_positions_view(s, ord_page=page),
                )
            return
        from src.nadobro.handlers.portfolio_deck import empty_portfolio_snapshot

        text, kb = render_positions_view(
            empty_portfolio_snapshot(telegram_id, network), ord_page=page
        )
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        _spawn_background_refresh(
            query, telegram_id, f"orders:{page}",
            lambda s: render_positions_view(s, ord_page=page),
        )
        return

    if action == "history":
        from src.nadobro.handlers.history_view import render_history_view

        try:
            page = max(0, int(parts[2])) if len(parts) > 2 else 0
        except (TypeError, ValueError):
            page = 0
        if not user:
            await _edit_loc(
                query,
                "⚠ History unavailable. Execution mode not set. Use /start to choose Testnet or Mainnet.",
            )
            return
        # History is pure DB (round-trips from recorded fills) — it never
        # needed the live venue snapshot it used to wait on. The render runs
        # in the worker pool because compute_round_trips does sync psycopg2
        # reads, which would otherwise stall the event loop for everyone.
        snapshot = {"user_id": int(telegram_id), "network": user.network_mode.value}
        text, kb = await run_blocking(render_history_view, snapshot, page)
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action == "hours":
        from src.nadobro.handlers.performance_view import render_hours_view

        if not user:
            await _edit_loc(query, "⚠ Analytics unavailable. Execution mode not set.")
            return
        # Pure DB — run off the event loop like the other performance reads.
        text, kb = await run_blocking(render_hours_view, telegram_id, user.network_mode.value)
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action in ("analytics", "performance"):
        from src.nadobro.handlers.performance_view import render_performance_view

        if not user:
            await _edit_loc(
                query,
                "⚠ Performance unavailable. Execution mode not set. Use /start to choose Testnet or Mainnet.",
            )
            return
        network = user.network_mode.value
        page = 0
        if len(parts) > 2 and parts[2].isdigit():
            page = int(parts[2])
        try:
            text, kb = await run_blocking(render_performance_view, telegram_id, network, page)
        except Exception as e:
            logger.warning("portfolio_performance_failed user=%s err=%s", telegram_id, e)
            text, kb = "📊 Performance\n\nNo performance data available.", portfolio_analytics_kb()
        await _edit_loc(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if action == "share_pnl" and not user:
        await query.answer("Execution mode not set.", show_alert=True)
        return

    if action == "share_pnl":
        # Per-trade cards (Type A, miner/trophy design):
        #   ``portfolio:share_pnl:rt:{trip_key}``   — desk/agent/manual round-trip
        #   ``portfolio:share_pnl:copy:{position_id}`` — a closed copy position
        # Strategy-session cards (Type B):
        #   ``portfolio:share_pnl:{sid}``           — a strategy session
        import io as _io

        from src.nadobro.portfolio.pnl_card_type_a import generate_type_a_card
        from src.nadobro.portfolio.pnl_card_type_b import generate_type_b_card
        from src.nadobro.portfolio.pnl_card_builder import (
            build_type_b_card_data,
            build_round_trip_card_data,
            build_copy_trade_card_data,
        )

        network = user.network_mode.value if user else "mainnet"
        session_id: int | None = None
        round_trip_key: str | None = None
        copy_position_id: int | None = None
        if len(parts) > 2:
            if parts[2] == "rt" and len(parts) > 3:
                round_trip_key = parts[3]
            elif parts[2] == "copy" and len(parts) > 3:
                try:
                    copy_position_id = int(parts[3])
                except (TypeError, ValueError):
                    copy_position_id = None
            else:
                try:
                    session_id = int(parts[2])
                except (TypeError, ValueError):
                    session_id = None
        try:
            if round_trip_key is not None:
                data = await run_blocking(
                    build_round_trip_card_data, telegram_id, network, round_trip_key
                )
            elif copy_position_id is not None:
                data = await run_blocking(
                    build_copy_trade_card_data, telegram_id, network, copy_position_id
                )
            else:
                data = None
            if data is not None and data.get("unsupported"):
                reason = data.get("unsupported")
                msg = (
                    "Shareable cards are available for perp trades for now."
                    if reason == "spot"
                    else "That trade is no longer available."
                )
                await query.answer(msg, show_alert=True)
                return
            if data is not None:  # Type A per-trade card
                png_bytes = await run_blocking(generate_type_a_card, data)
            else:  # Type B strategy-session card
                data = await run_blocking(build_type_b_card_data, telegram_id, network, session_id)
                png_bytes = await run_blocking(generate_type_b_card, data)
        except Exception as e:
            logger.warning("portfolio_share_pnl_failed user=%s err=%s", telegram_id, e)
            await query.answer("Could not generate PnL card.", show_alert=True)
            return
        # Acknowledge the tap immediately so it never looks like nothing happened,
        # then upload the full-quality PNG. The ~1.5MB card upload was tripping
        # the default 5s write timeout (err=Timed out) — give it media-sized
        # timeouts instead of downscaling the image.
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 - the send below is what matters
            pass
        try:
            await query.message.reply_photo(
                photo=_io.BytesIO(png_bytes),
                caption=(
                    "📊 *Your PnL Card*\n"
                    "Share your performance on Nado."
                ),
                parse_mode="Markdown",
                read_timeout=60,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=30,
            )
        except Exception as e:
            # No text fallback: the user is already looking at their stats and
            # just wanted the card — a text message would be noise. Log only.
            logger.warning("portfolio_share_pnl_send_failed user=%s err=%s", telegram_id, e)
        return

    # Default: portfolio overview (single shared 24h / 7d / 30d / All toggle).
    from src.nadobro.handlers.portfolio_deck import (
        empty_portfolio_snapshot,
        render_portfolio_deck,
    )

    force_refresh = action == "refresh"
    # Both ``portfolio:view:{win}`` and ``portfolio:refresh:{win}`` encode
    # the active stats window in callback_data. The default ``view`` /
    # ``refresh`` actions keep ``24h``.
    window = parts[2] if action in ("view", "refresh") and len(parts) > 2 else "24h"
    network = user.network_mode.value if user else None

    with timed_metric("cb.portfolio.view"):
        cached = _cached_snapshot(telegram_id, network)
        if cached:
            # Instant render from cache; freshen in the background when due.
            msg, reply_markup = render_portfolio_deck(
                cached,
                window=window,
                refreshing=force_refresh or _snapshot_age_s(cached) > _CACHE_FRESH_SECONDS,
            )
            try:
                await _edit_loc(query, msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
            if force_refresh or _snapshot_age_s(cached) > _CACHE_FRESH_SECONDS:
                _spawn_background_refresh(
                    query, telegram_id, f"deck:{window}",
                    lambda s: render_portfolio_deck(s, window=window),
                    force=force_refresh,
                )
            return
        # Cold start: show a real (empty) deck immediately. "Loading…" was a
        # dead end — the background venue+indexer sync could run for minutes
        # and leave the user staring at two buttons.
        msg, reply_markup = render_portfolio_deck(
            empty_portfolio_snapshot(telegram_id, network),
            window=window,
            refreshing=True,
        )
        await _edit_loc(query, msg, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        _spawn_background_refresh(
            query, telegram_id, f"deck:{window}",
            lambda s: render_portfolio_deck(s, window=window),
        )
        return
