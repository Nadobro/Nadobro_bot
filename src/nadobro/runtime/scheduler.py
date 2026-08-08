import logging
import asyncio
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from src.nadobro.utils.env import env_float, env_int
from src.nadobro.notify.alert_service import get_triggered_alerts
from src.nadobro.trading.stop_loss_service import process_stop_losses
from src.nadobro.venue.nado_client import NadoClient
from src.nadobro.core.async_utils import run_blocking
from src.nadobro.core.perf import timed_metric
from src.nadobro.trading.execution_queue import enqueue_alert
from src.nadobro.users.lowiq_relay_client import relay_poll_interval_seconds

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")
_bot_app = None
_check_client = None
_ALERT_SCAN_SECONDS = env_int("ALERT_SCAN_SECONDS", 5)
# Archive indexer can lag behind fills; allow longer polling than inline trade resolution.
_FILL_SYNC_DIGEST_WAIT = env_float("NADO_FILL_SYNC_DIGEST_WAIT_SECONDS", 8.0)
_FILL_SYNC_DIGEST_POLL = env_float("NADO_FILL_SYNC_DIGEST_POLL_SECONDS", 1.0)
_MARKET_SNAPSHOT_TTL_SECONDS = env_float("NADO_MARKET_SNAPSHOT_TTL_SECONDS", 3.0)
_last_market_snapshot: dict = {"ts": 0.0, "prices": {}}
# AUDIT-FIX-SCH-2: serialize cache fetches so two coroutines don't both miss
# the cache and fire concurrent get_all_market_prices() requests. Lazy-init
# because module load happens before the asyncio loop exists.
# NOTE: this helper was lost during a main-branch merge; without it every
# alert/price-tracker tick raised ``NameError: name '_market_snapshot_lock_get'
# is not defined`` (visible in production logs every 5s).
_market_snapshot_lock: asyncio.Lock | None = None


def _market_snapshot_lock_get() -> asyncio.Lock:
    global _market_snapshot_lock
    if _market_snapshot_lock is None:
        _market_snapshot_lock = asyncio.Lock()
    return _market_snapshot_lock


def _resolve_market_snapshot_lock() -> asyncio.Lock:
    # Defensive shim: tolerates merge regressions / partial reloads that
    # remove ``_market_snapshot_lock_get`` from the module namespace.
    helper = globals().get("_market_snapshot_lock_get")
    if helper is not None:
        return helper()
    global _market_snapshot_lock
    if _market_snapshot_lock is None:
        _market_snapshot_lock = asyncio.Lock()
    return _market_snapshot_lock


_RECORDED_FILL_STATUSES = frozenset({"filled", "partially_filled", "closed"})


def _trade_has_recorded_fill(trade_row: dict | None) -> bool:
    if not trade_row:
        return False
    status = str(trade_row.get("status") or "").lower()
    if status in _RECORDED_FILL_STATUSES:
        return True
    try:
        return abs(float(trade_row.get("fill_size") or 0.0)) > 0.0
    except Exception:
        return False


def _format_alert_metric_value(condition: str, value: float) -> str:
    condition = str(condition or "")
    if condition.startswith("funding_"):
        return f"{float(value):,.4f}%"
    return f"${float(value):,.2f}"


def _alert_condition_label(condition: str) -> str:
    labels = {
        "above": "Price Above",
        "below": "Price Below",
        "funding_above": "Funding Above",
        "funding_below": "Funding Below",
        "pnl_above": "PnL Above",
        "pnl_below": "PnL Below",
    }
    return labels.get(str(condition or ""), str(condition or ""))


async def _build_alert_context(network: str | None = None) -> tuple[dict, dict]:
    """Build optional context maps used by funding/pnl alerts.

    ``network`` scopes the scan to the evaluation network (the alert worker's
    network in handle_alert_job). Without it, PnL positions were fetched from
    each user's CURRENT active network — a user browsing testnet had their
    mainnet PnL alert evaluated against testnet positions (wrong-fire or
    never-fire)."""
    from src.nadobro.models.database import get_all_active_alerts, AlertCondition
    from src.nadobro.config import get_product_id
    from src.nadobro.users.user_service import get_user_readonly_client

    funding_rates: dict = {}
    positions_by_user: dict = {}

    active_alerts = await run_blocking(get_all_active_alerts, network)
    if not active_alerts:
        return funding_rates, positions_by_user

    needs_funding = False
    needs_pnl = False
    funding_products: set[str] = set()
    pnl_user_ids: set[int] = set()

    for alert in active_alerts:
        cond = alert.get("condition")
        if cond in (AlertCondition.FUNDING_ABOVE.value, AlertCondition.FUNDING_BELOW.value):
            needs_funding = True
            product = str((alert.get("product_name") or "")).replace("-PERP", "")
            if product:
                funding_products.add(product)
        elif cond in (AlertCondition.PNL_ABOVE.value, AlertCondition.PNL_BELOW.value):
            needs_pnl = True
            uid = alert.get("user_id")
            if uid is not None:
                try:
                    pnl_user_ids.add(int(uid))
                except Exception:
                    continue

    if needs_funding and _check_client:
        for product in funding_products:
            try:
                pid = get_product_id(
                    product,
                    network=getattr(_check_client, "network", "mainnet"),
                    client=_check_client,
                )
                if pid is None:
                    continue
                fr = await run_blocking(_check_client.get_funding_rate, pid)
                if isinstance(fr, dict):
                    funding_rates[product] = float(fr.get("funding_rate", 0) or 0)
            except Exception:
                continue

    if needs_pnl:
        for user_id in pnl_user_ids:
            try:
                # Pin the client to the evaluation network — the alert lives in
                # that network's table, so its PnL must be read there regardless
                # of which mode the user is currently browsing.
                client = await run_blocking(get_user_readonly_client, user_id, network)
                if not client:
                    continue
                positions = await run_blocking(client.get_all_positions)
                positions_by_user[user_id] = positions or []
            except Exception:
                continue

    return funding_rates, positions_by_user


def set_bot_app(app):
    global _bot_app
    _bot_app = app
    try:
        from src.nadobro.strategy.time_limit_watcher import set_bot_app as set_time_limit_bot_app

        set_time_limit_bot_app(app)
    except Exception:
        pass
    try:
        from src.nadobro.trading.desk_runtime import set_bot_app as set_desk_bot_app

        set_desk_bot_app(app)
    except Exception:
        pass


def set_check_client(client: NadoClient):
    global _check_client
    _check_client = client


async def check_alerts():
    global _bot_app, _check_client
    if not _bot_app or not _check_client:
        return

    try:
        await enqueue_alert({"ts": asyncio.get_running_loop().time()}, dedupe_key=f"scan:{int(asyncio.get_running_loop().time() / 5)}")
    except Exception as e:
        logger.error(f"Alert enqueue failed: {e}")


async def handle_alert_job(payload: dict):
    global _bot_app, _check_client
    if not _bot_app or not _check_client:
        return
    try:
        with timed_metric("scheduler.check_alerts.total"):
            prices = await _get_market_snapshot()
        if not prices:
            return
        # Scope evaluation to the alert worker's configured network so we
        # don't fire mainnet alerts against testnet prices (or vice versa).
        # Alerts in the OTHER network are paused until a second worker
        # covers it. (Audit 2026-05.)
        alert_network = getattr(_check_client, "network", "mainnet")
        funding_rates, positions_by_user = await _build_alert_context(alert_network)
        triggered = await run_blocking(
            get_triggered_alerts,
            prices,
            funding_rates,
            positions_by_user,
            alert_network,
        )
        for alert in triggered:
            try:
                from src.nadobro.i18n import language_context, get_user_language, localize_text, get_active_language
                with language_context(get_user_language(alert["user_id"])):
                    lang = get_active_language()
                    condition_label = _alert_condition_label(alert.get("condition"))
                    target_fmt = _format_alert_metric_value(alert.get("condition"), alert.get("target", 0))
                    current_fmt = _format_alert_metric_value(
                        alert.get("condition"),
                        alert.get("current_value", alert.get("current_price", 0)),
                    )
                    msg = (
                        f"{localize_text('Alert Triggered!', lang)}\n"
                        f"{alert['product']} {localize_text('is', lang)} {condition_label}: {target_fmt}\n"
                        f"{localize_text('Current value:', lang)} {current_fmt}"
                    )
                await _bot_app.bot.send_message(chat_id=alert["user_id"], text=msg)
                logger.info(f"Alert sent to user {alert['user_id']}: {alert['product']} {alert['condition']}")
            except Exception as e:
                logger.error(f"Failed to send alert to {alert['user_id']}: {e}")
        sl_notifications = await run_blocking(process_stop_losses, prices)
        for note in sl_notifications:
            try:
                from src.nadobro.i18n import language_context, get_user_language, localize_text, get_active_language
                with language_context(get_user_language(note["user_id"])):
                    lang = get_active_language()
                    await _bot_app.bot.send_message(chat_id=note["user_id"], text=localize_text(note["text"], lang))
            except Exception as e:
                logger.error("Failed to send stop-loss notification to %s: %s", note.get("user_id"), e)
    except Exception as e:
        logger.error(f"Alert check failed: {e}")


async def tick_price_tracker():
    global _check_client
    if not _check_client:
        return
    try:
        from src.nadobro.market_data.price_tracker import record_prices_snapshot
        prices = await _get_market_snapshot()
        if prices:
            await run_blocking(record_prices_snapshot, prices)
    except Exception as e:
        logger.error("Price tracker tick failed: %s", e)


async def _get_market_snapshot(force_refresh: bool = False) -> dict:
    global _last_market_snapshot
    # AUDIT-FIX-SCH-2: hold the lock for the full check-then-fetch sequence
    # so two concurrent coroutines don't both miss the cache and race a
    # second venue request.
    async with _resolve_market_snapshot_lock():
        now = asyncio.get_running_loop().time()
        if (not force_refresh) and _last_market_snapshot.get("prices") and (
            now - float(_last_market_snapshot.get("ts") or 0.0) < _MARKET_SNAPSHOT_TTL_SECONDS
        ):
            return _last_market_snapshot.get("prices") or {}
        prices = await run_blocking(_check_client.get_all_market_prices)
        _last_market_snapshot = {"ts": now, "prices": prices or {}}
        return _last_market_snapshot["prices"]


async def tick_howl():
    global _bot_app
    if not _bot_app:
        return
    try:
        from src.nadobro.llm.howl_service import run_howl_analysis, format_howl_message, get_pending_howl
        from src.nadobro.strategy.bot_runtime import _load_state
        from src.nadobro.db import query_all
        import json

        rows = await run_blocking(
            query_all,
            "SELECT key, value FROM bot_state WHERE key LIKE %s",
            ("strategy_bot:%",),
        )
        for row in rows:
            try:
                key = row.get("key", "")
                state = json.loads(row.get("value") or "{}")
                if not state.get("running") or state.get("strategy") != "bro":
                    continue
                if not state.get("bro_state", {}).get("started_at"):
                    continue

                settings = state.get("bro_state", {})
                if not bool(state.get("howl_enabled", True)):
                    continue

                user_network = key.replace("strategy_bot:", "")
                user_id_str, network = user_network.split(":", 1)
                telegram_id = int(user_id_str)

                existing = await run_blocking(get_pending_howl, telegram_id, network)
                if existing:
                    continue

                howl_data = await run_blocking(run_howl_analysis, telegram_id, network, state)
                if howl_data and howl_data.get("suggestions"):
                    msg = format_howl_message(howl_data)
                    from src.nadobro.llm.howl_ui import howl_approval_kb
                    from src.nadobro.i18n import language_context, get_user_language, localize_text, localize_markup, get_active_language
                    suggestions_count = len(howl_data.get("suggestions", []))
                    try:
                        with language_context(get_user_language(telegram_id)):
                            lang = get_active_language()
                            await _bot_app.bot.send_message(
                                chat_id=telegram_id,
                                text=localize_text(msg, lang),
                                parse_mode="HTML",
                                reply_markup=localize_markup(howl_approval_kb(suggestions_count), lang),
                            )
                    except Exception as e:
                        logger.error("Failed to send HOWL to user %s: %s", telegram_id, e)
            except Exception as e:
                logger.debug("HOWL skip for row: %s", e)
    except Exception as e:
        logger.error("HOWL ticker failed: %s", e)


async def tick_night_howl():
    """Hourly sweep that delivers each active user's Night HOWL report at THEIR
    local 8am (per-user UTC offset; falls back to UTC). Builds a backtest-backed
    trade-pattern report, sends it, and saves it for later (/howl). De-duped per
    local day; users with no 24h activity are marked sent without a noisy ping."""
    global _bot_app
    if not _bot_app:
        return
    try:
        from src.nadobro.llm.night_howl_service import (
            build_report, last_sent_date, mark_sent, night_howl_due,
            night_howl_enabled, _user_tz_offset,
        )
        from src.nadobro.db import query_all
        from src.nadobro.i18n import (
            language_context, get_user_language, localize_text, get_active_language,
        )

        now_utc = datetime.now(timezone.utc)
        rows = await run_blocking(
            query_all, "SELECT key FROM bot_state WHERE key LIKE %s", ("strategy_bot:%",),
        )
        seen = set()
        for row in rows:
            try:
                user_network = str(row.get("key", "")).replace("strategy_bot:", "")
                user_id_str, network = user_network.split(":", 1)
                telegram_id = int(user_id_str)
                if (telegram_id, network) in seen:
                    continue
                seen.add((telegram_id, network))

                if not await run_blocking(night_howl_enabled, telegram_id):
                    continue
                offset = await run_blocking(_user_tz_offset, telegram_id)
                last = await run_blocking(last_sent_date, telegram_id, network)
                if not night_howl_due(now_utc, offset, last):
                    continue

                report = await run_blocking(build_report, telegram_id, network, now_utc=now_utc)
                if not report:
                    continue
                # Mark sent regardless so we don't rebuild every hour today.
                await run_blocking(mark_sent, telegram_id, network, report.get("date"))
                # Skip the ping for a no-activity day (report is still saved).
                if int((report.get("pattern") or {}).get("trades") or 0) <= 0:
                    continue
                md = report.get("markdown")
                if not md:
                    continue
                try:
                    with language_context(get_user_language(telegram_id)):
                        lang = get_active_language()
                        await _bot_app.bot.send_message(
                            chat_id=telegram_id, text=localize_text(md, lang),
                            parse_mode="Markdown",
                        )
                except Exception as e:
                    logger.error("Failed to send Night HOWL to user %s: %s", telegram_id, e)
            except Exception as e:
                logger.debug("Night HOWL skip for row: %s", e)
    except Exception as e:
        logger.error("Night HOWL ticker failed: %s", e)


async def tick_signal_scorer():
    """Grade overlay signals whose horizon has elapsed.

    Read-only against the venue and write-only against ``signal_outcomes`` — it
    places no orders and touches no strategy state, so it is safe to run on a
    short interval. Anything it cannot grade this pass stays ungraded and is
    retried next run."""
    try:
        from src.nadobro.llm.signal_scorer import grade_pending

        result = await run_blocking(grade_pending)
        if result.get("graded"):
            logger.info("signal scorer graded %s outcomes", result["graded"])
    except Exception as e:
        logger.error("Signal scorer ticker failed: %s", e)


async def poll_lowiqpts_relay():
    global _bot_app
    if not _bot_app:
        return
    try:
        from src.nadobro.users.points_service import poll_lowiqpts_relay_events
        await poll_lowiqpts_relay_events(_bot_app)
    except Exception as e:
        logger.error("LOWIQPTS relay poll failed: %s", e)


def _is_partial_fill(requested_size: float, fill_size: float, epsilon: float = 1e-12) -> bool:
    if requested_size <= 0:
        return False
    if fill_size <= 0:
        return False
    return fill_size + epsilon < requested_size


async def _notify_limit_order_filled_once(trade_row: dict, network: str):
    global _bot_app
    if not _bot_app or not trade_row:
        return
    try:
        from src.nadobro.models.database import get_bot_state, set_bot_state

        source = str((trade_row or {}).get("source") or "").strip().lower()
        strategy_session_id = trade_row.get("strategy_session_id")
        if source in {"vol", "grid", "rgrid", "dn", "bro"} or strategy_session_id:
            return

        trade_id = int(trade_row.get("id"))
        dedupe_key = f"limit_fill_notified:{network}:{trade_id}"
        if await run_blocking(get_bot_state, dedupe_key):
            return

        user_id = int(trade_row.get("user_id"))
        side = str(trade_row.get("side") or "").upper()
        product = str(trade_row.get("product_name") or "")
        size = float(trade_row.get("size") or 0)
        fill_price = float(trade_row.get("fill_price") or trade_row.get("price") or 0)

        msg = (
            "✅ Limit order filled\n\n"
            "📋 Type: LIMIT\n"
            f"📌 Side: {side or '?'}\n"
            f"🪙 Product: {product or '?'}\n"
            f"📏 Size: {size:.4f}\n"
            f"💲 Fill price: ${fill_price:,.6f}\n"
            "📶 Status: fully filled\n"
            f"🌐 Network: {network}"
        )
        await _bot_app.bot.send_message(chat_id=user_id, text=msg)
        await run_blocking(set_bot_state, dedupe_key, {"notified_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        logger.warning("Failed to send limit-fill notification: %s", e)


def _infer_cancel_source(trade_row: dict) -> str:
    source = str((trade_row or {}).get("source") or "").strip().lower()
    # Best-effort: manual/chat-driven trades are usually user-originated.
    if source in {"manual", "chat", "intent", "ui"}:
        return "user_initiated"
    return "system_detected"


async def _notify_limit_order_cancelled_once(
    trade_row: dict,
    network: str,
    cancel_source: str = "system_detected",
):
    global _bot_app
    if not _bot_app or not trade_row:
        return
    try:
        from src.nadobro.models.database import get_bot_state, set_bot_state

        source = str((trade_row or {}).get("source") or "").strip().lower()
        strategy_session_id = trade_row.get("strategy_session_id")
        if source in {"vol", "grid", "rgrid", "dn", "bro"} or strategy_session_id:
            return

        trade_id = int(trade_row.get("id"))
        dedupe_key = f"limit_cancel_notified:{network}:{trade_id}"
        if await run_blocking(get_bot_state, dedupe_key):
            return

        user_id = int(trade_row.get("user_id"))
        side = str(trade_row.get("side") or "").upper()
        product = str(trade_row.get("product_name") or "")
        size = float(trade_row.get("size") or 0)
        limit_price = float(trade_row.get("price") or 0)

        is_user = str(cancel_source or "").lower() == "user_initiated"
        headline = "🟠 Limit order cancelled by user" if is_user else "⚙️ Limit order cancelled (system-detected)"
        source_label = "user-initiated" if is_user else "system-detected"
        msg = (
            f"{headline}\n\n"
            "📋 Type: LIMIT\n"
            f"📌 Side: {side or '?'}\n"
            f"🪙 Product: {product or '?'}\n"
            f"📏 Size: {size:.4f}\n"
            f"💲 Limit price: ${limit_price:,.6f}\n"
            "📶 Status: cancelled\n"
            f"🧭 Cancel source: {source_label}\n"
            f"🌐 Network: {network}"
        )
        await _bot_app.bot.send_message(chat_id=user_id, text=msg)
        await run_blocking(set_bot_state, dedupe_key, {"notified_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        logger.warning("Failed to send limit-cancel notification: %s", e)


_FILL_SYNC_BATCH_SIZE = env_int("NADO_FILL_SYNC_BATCH_SIZE", 10)
_FILL_SYNC_CONCURRENCY = max(1, env_int("NADO_FILL_SYNC_CONCURRENCY", 2))
_FILL_SYNC_DIGEST_BATCH = max(1, env_int("NADO_FILL_SYNC_DIGEST_BATCH", 10))


def _chunked(items: list, size: int):
    for idx in range(0, len(items), size):
        yield items[idx: idx + size]


async def sync_pending_fills():
    """Background job: resolve pending fills via Nado archive API.

    Each entry can spend up to ``_FILL_SYNC_DIGEST_WAIT`` seconds polling the
    archive when a batch lookup misses. We prefetch digests in batches to keep
    archive request volume low, then only poll individually for unresolved rows.
    """
    try:
        from src.nadobro.models.database import (
            claim_pending_fill_syncs, update_trade, resolve_fill_sync,
            expire_fill_sync, release_fill_sync,
            increment_session_metrics,
            get_trade_by_id,
        )
        from src.nadobro.venue.nado_archive import (
            is_archive_rate_limited,
            query_order_by_digest,
            query_orders_by_digests,
            query_orders_by_subaccount,
        )
        from src.nadobro.trading.trade_service import reconcile_close_trade_fill
        from src.nadobro.users.user_service import update_trade_stats

        pending = await run_blocking(claim_pending_fill_syncs, _FILL_SYNC_BATCH_SIZE)
        if not pending:
            return

        prefetched: dict[tuple[str, str], dict] = {}
        if not is_archive_rate_limited():
            by_network: dict[str, list[str]] = {}
            for entry in pending:
                network = str(entry.get("network") or "mainnet")
                digest = str(entry.get("order_digest") or "").strip()
                if not digest:
                    continue
                by_network.setdefault(network, []).append(digest)

            for network, digests in by_network.items():
                unique_digests = list(dict.fromkeys(digests))
                for batch in _chunked(unique_digests, _FILL_SYNC_DIGEST_BATCH):
                    if is_archive_rate_limited():
                        break
                    batch_results = await run_blocking(query_orders_by_digests, network, batch)
                    for digest, data in (batch_results or {}).items():
                        prefetched[(network, str(digest))] = data

        semaphore = asyncio.Semaphore(_FILL_SYNC_CONCURRENCY)

        async def _process_entry(entry: dict) -> bool:
            """Return True if the entry was resolved/closed; False if released for retry."""
            async with semaphore:
                try:
                    sync_id = entry["id"]
                    trade_id = entry["trade_id"]
                    network = entry["network"]
                    digest = entry["order_digest"]
                    attempts = int(entry.get("attempts", 0))
                    trade_row = await run_blocking(get_trade_by_id, trade_id, network)

                    # Expire old entries
                    created = entry.get("created_at")
                    if created and attempts >= 10:
                        from datetime import datetime as dt

                        if isinstance(created, str):
                            # Keep explicit timezone offsets (including trailing Z -> UTC).
                            created_dt = dt.fromisoformat(created.replace("Z", "+00:00"))
                        else:
                            created_dt = created

                        # Normalize both sides to aware UTC before comparing.
                        if created_dt.tzinfo is None:
                            created_dt = created_dt.replace(tzinfo=timezone.utc)
                        age_seconds = (dt.now(timezone.utc) - created_dt.astimezone(timezone.utc)).total_seconds()
                        if age_seconds > 7200:  # 2 hours
                            await run_blocking(expire_fill_sync, sync_id)
                            return False

                    fill_data = prefetched.get((network, str(digest)))
                    if not fill_data or not fill_data.get("is_filled"):
                        if is_archive_rate_limited():
                            await run_blocking(release_fill_sync, sync_id)
                            return False
                        fill_data = await run_blocking(
                            query_order_by_digest,
                            network,
                            digest,
                            _FILL_SYNC_DIGEST_WAIT,
                            _FILL_SYNC_DIGEST_POLL,
                        )
                    if not fill_data or not fill_data.get("is_filled"):
                        if is_archive_rate_limited() or _trade_has_recorded_fill(trade_row):
                            await run_blocking(release_fill_sync, sync_id)
                            return False
                        # Check if order was cancelled only after enough retries to avoid
                        # misclassifying late archive indexing as cancellation.
                        if attempts >= 8:
                            try:
                                from src.nadobro.users.user_service import get_user_nado_client
                                user_id = entry["user_id"]
                                product_id = entry["product_id"]
                                client = await run_blocking(get_user_nado_client, int(user_id), network=network)
                                if client:
                                    open_orders = await run_blocking(client.get_open_orders, product_id, refresh=True) or []
                                    digest_still_open = any(
                                        str(o.get("digest")) == digest for o in open_orders
                                    )
                                    if not digest_still_open:
                                        recent_orders = await run_blocking(
                                            query_orders_by_subaccount,
                                            network,
                                            entry["subaccount_hex"],
                                            [product_id],
                                            100,
                                            None,
                                        )
                                        archive_hit = next(
                                            (o for o in (recent_orders or []) if str(o.get("digest") or "") == digest and o.get("is_filled")),
                                            None,
                                        )
                                        if archive_hit:
                                            fill_data = archive_hit
                                            digest_still_open = True
                                    if not digest_still_open:
                                        if attempts < 20:
                                            await run_blocking(release_fill_sync, sync_id)
                                            return False
                                        await run_blocking(
                                            update_trade, trade_id,
                                            {"status": "cancelled"}, network,
                                        )
                                        refreshed_trade = await run_blocking(get_trade_by_id, trade_id, network)
                                        trade_for_notify = refreshed_trade or trade_row
                                        await _notify_limit_order_cancelled_once(
                                            trade_for_notify,
                                            network,
                                            cancel_source=_infer_cancel_source(trade_for_notify or {}),
                                        )
                                        await run_blocking(resolve_fill_sync, sync_id)
                                        return True
                            except Exception:
                                pass
                        if not fill_data or not fill_data.get("is_filled"):
                            await run_blocking(release_fill_sync, sync_id)
                            return False

                    requested_size = abs(float((trade_row or {}).get("size") or 0))
                    filled_size = abs(float(fill_data.get("fill_size") or 0))
                    is_partial = _is_partial_fill(requested_size, filled_size)

                    # Fill resolved — update trade. Keep queue pending for partial fills.
                    total_fee = float(fill_data.get("fee", 0) or 0.0) + float(fill_data.get("builder_fee", 0) or 0.0)
                    is_close_trade = (
                        "CLOSE" in str((trade_row or {}).get("order_type") or "").upper()
                        or bool((trade_row or {}).get("open_trade_id"))
                    )
                    update_data = {
                        "status": "partially_filled" if is_partial else ("closed" if is_close_trade else "filled"),
                        "fill_price": fill_data["fill_price"],
                        "price": fill_data["fill_price"],
                        "fill_size": fill_data.get("fill_size"),
                        "fill_fee": total_fee,
                        "builder_fee": float(fill_data.get("builder_fee", 0) or 0.0),
                        "fees": total_fee,
                        "realized_pnl": fill_data.get("realized_pnl", 0),
                        "is_taker": fill_data.get("is_taker"),
                    }
                    if fill_data.get("first_fill_ts"):
                        from datetime import datetime as dt
                        try:
                            # AUDIT-FIX-SCH-1: utcfromtimestamp is deprecated
                            # (Py 3.12+) and returns a NAIVE datetime, which
                            # blows up comparisons against aware timestamps
                            # elsewhere in this file. Use an aware UTC instant.
                            update_data["filled_at"] = dt.fromtimestamp(
                                int(fill_data["first_fill_ts"]), tz=timezone.utc,
                            ).isoformat()
                        except Exception:
                            pass

                    await run_blocking(update_trade, trade_id, update_data, network)
                    previous_fill_size = abs(float((trade_row or {}).get("fill_size") or 0.0))
                    current_fill_size = abs(float(fill_data.get("fill_size") or 0.0))
                    delta_fill_size = max(0.0, current_fill_size - previous_fill_size)
                    if delta_fill_size > 0:
                        try:
                            await run_blocking(
                                update_trade_stats,
                                int(entry["user_id"]),
                                delta_fill_size * float(fill_data.get("fill_price") or 0.0),
                                False,
                            )
                        except Exception:
                            pass
                        session_id = int((trade_row or {}).get("strategy_session_id") or 0)
                        if session_id > 0:
                            try:
                                previous_fees = float((trade_row or {}).get("fees") or 0.0)
                                current_fees = float(total_fee or 0.0)
                                fee_delta = max(0.0, current_fees - previous_fees)
                                previous_pnl = float((trade_row or {}).get("realized_pnl") or 0.0)
                                current_pnl = float(fill_data.get("realized_pnl") or 0.0)
                                pnl_delta = current_pnl - previous_pnl
                                await run_blocking(
                                    increment_session_metrics,
                                    session_id,
                                    0,
                                    0,
                                    1 if previous_fill_size <= 0 else 0,
                                    0,
                                    pnl_delta,
                                    fee_delta,
                                    delta_fill_size * float(fill_data.get("fill_price") or 0.0),
                                    0.0,
                                )
                            except Exception:
                                pass
                    if not is_partial and is_close_trade:
                        try:
                            await run_blocking(reconcile_close_trade_fill, trade_id, network, fill_data)
                        except Exception:
                            logger.warning("Close-trade reconciliation failed for trade %s", trade_id)
                    if is_partial:
                        digest_still_open = True
                        try:
                            from src.nadobro.users.user_service import get_user_nado_client
                            user_id = entry["user_id"]
                            product_id = entry["product_id"]
                            client = await run_blocking(get_user_nado_client, int(user_id), network=network)
                            if client:
                                open_orders = await run_blocking(client.get_open_orders, product_id, refresh=True) or []
                                digest_still_open = any(str(o.get("digest")) == digest for o in open_orders)
                        except Exception:
                            digest_still_open = True
                        resolved_partial = False
                        if not digest_still_open:
                            await run_blocking(resolve_fill_sync, sync_id)
                            resolved_partial = True
                        else:
                            await run_blocking(release_fill_sync, sync_id)
                        logger.info(
                            "Fill sync partial trade #%s: filled=%.6f/%.6f price=%.6f",
                            trade_id,
                            filled_size,
                            requested_size,
                            fill_data["fill_price"],
                        )
                        return resolved_partial

                    await run_blocking(resolve_fill_sync, sync_id)
                    refreshed_trade = await run_blocking(get_trade_by_id, trade_id, network)
                    await _notify_limit_order_filled_once(refreshed_trade or trade_row, network)
                    logger.info(
                        "Fill sync resolved trade #%s: price=%.6f fee=%.6f pnl=%.6f",
                        trade_id, fill_data["fill_price"],
                        fill_data.get("fee", 0), fill_data.get("realized_pnl", 0),
                    )
                    return True
                except Exception as e:
                    logger.warning("Fill sync error for entry %s: %s", entry.get("id"), e)
                    try:
                        await run_blocking(release_fill_sync, entry.get("id"))
                    except Exception:
                        pass
                    return False

        results = await asyncio.gather(
            *(_process_entry(entry) for entry in pending),
            return_exceptions=True,
        )
        resolved_count = sum(1 for r in results if r is True)

        if resolved_count > 0:
            logger.info("Fill sync: resolved %d/%d pending fills", resolved_count, len(pending))
    except Exception as e:
        logger.error("Fill sync job failed: %s", e)


async def tick_edge_scanner():
    """Scan for trading edges, promotions, and multipliers on X."""
    try:
        from src.nadobro.llm.edge_scanner import async_scan_edges
        await async_scan_edges()
    except Exception as e:
        logger.error("Edge scanner tick failed: %s", e)


async def tick_perf_slo():
    """Aggregate SLO watchdog: warn when click/message p95 degrades fleet-wide."""
    try:
        from src.nadobro.core.perf import check_slo

        check_slo()
    except Exception as e:
        logger.debug("perf SLO tick failed: %s", e)


async def initial_ai_setup():
    """Run initial AI setup: KB indexing + first edge scan."""
    try:
        from src.nadobro.llm.edge_scanner import async_initial_scan
        await async_initial_scan()
    except Exception as e:
        logger.error("Initial AI setup failed: %s", e)


_EDGE_SCAN_SECONDS = env_int("EDGE_SCAN_INTERVAL_SECONDS", 1800)
_NEWS_WARMUP_MINUTES = env_int("NEWS_WARMUP_MINUTES", 12)
# Signal grading. The shortest horizon is 15m, so a 5-minute cadence keeps the
# ledger close to live without re-querying candles that haven't moved. Set to 0
# to disable grading entirely.
_SIGNAL_SCORER_SECONDS = env_int("SIGNAL_SCORER_INTERVAL_SECONDS", 300)


async def tick_news_warmup() -> None:
    """Pre-warm the news bundle cache so user-facing /brief calls hit warm data.

    Skips when no users have been active recently (no chat history entries) to
    avoid burning outbound HTTP calls on idle days.
    """
    try:
        from src.nadobro.llm.knowledge_service import _chat_history

        if not _chat_history:
            logger.debug("news warmup skipped: no recent chat activity")
            return
    except Exception:
        pass

    try:
        from src.nadobro.market_data.news_aggregator import fetch_news_bundle

        bundle = await fetch_news_bundle()
        logger.info("news warmup ok: %d items, %d sources", len(bundle.items), len(bundle.sources_used))
    except Exception as exc:
        logger.warning("news warmup failed: %s", exc)


async def tick_vault_deposit_watch_job():
    from src.nadobro.core.feature_flags import vault_deposit_watch_enabled
    if not vault_deposit_watch_enabled():
        return
    from src.nadobro.vault.vault_deposit_watch_service import tick_vault_deposit_watch
    # Watches are stored per-network; tick both networks every cycle so we
    # don't miss capacity openings depending on which network the env points
    # to. Each call is a no-op if no users are opted in for that network.
    for network in ("mainnet", "testnet"):
        try:
            await tick_vault_deposit_watch(network=network)
        except Exception as exc:
            logger.warning("vault deposit watch tick failed network=%s: %s", network, exc)


def start_scheduler():
    relay_poll_seconds = relay_poll_interval_seconds()
    from src.nadobro.core.feature_flags import (
        portfolio_sync_enabled,
        portfolio_sync_interval_seconds,
        time_limit_enabled,
        vault_deposit_watch_enabled,
        vault_deposit_watch_interval_seconds,
    )
    from src.nadobro.strategy.time_limit_watcher import time_limit_tick

    # Misfire / coalesce tuning. The event loop occasionally blocks for 5-20s
    # (slow callback, archive 422 retries, place_order chains) and short-tick
    # jobs were emitting "Run time of job ... was missed by Ns" warnings. We
    # don't care about a missed alert tick — coalesce stacked misses into one
    # and give the scheduler a grace window before logging.
    _SHORT_TICK = {"misfire_grace_time": 30, "coalesce": True, "max_instances": 1}
    _LONG_TICK = {"misfire_grace_time": 120, "coalesce": True, "max_instances": 1}

    scheduler.add_job(
        check_alerts, "interval", seconds=_ALERT_SCAN_SECONDS,
        id="check_alerts", replace_existing=True, **_SHORT_TICK,
    )
    scheduler.add_job(
        tick_price_tracker, "interval", seconds=60,
        id="price_tracker", replace_existing=True, **_SHORT_TICK,
    )
    if time_limit_enabled():
        scheduler.add_job(
            time_limit_tick, "interval", seconds=60,
            id="time_limit_watcher", replace_existing=True, **_SHORT_TICK,
        )
    if portfolio_sync_enabled():
        from src.nadobro.venue.nado_sync import sync_active_users

        scheduler.add_job(
            sync_active_users,
            "interval",
            seconds=portfolio_sync_interval_seconds(),
            id="portfolio_sync",
            replace_existing=True,
            kwargs={"reason": "poll"},
            **_LONG_TICK,
        )
    scheduler.add_job(tick_howl, "cron", hour=2, minute=0, id="howl_nightly", replace_existing=True, **_LONG_TICK)
    # Night HOWL: runs every hour on the hour and delivers to users for whom it's
    # now their local 8am (per-user UTC offset), de-duped per local day.
    scheduler.add_job(
        tick_night_howl, "cron", minute=0, id="night_howl_hourly",
        replace_existing=True, **_LONG_TICK,
    )
    # Grade elapsed overlay signals. Read-only against the venue; no trading
    # side effects, so it is safe on a short interval.
    if _SIGNAL_SCORER_SECONDS > 0:
        scheduler.add_job(
            tick_signal_scorer, "interval", seconds=_SIGNAL_SCORER_SECONDS,
            id="signal_scorer", replace_existing=True, **_LONG_TICK,
        )
    scheduler.add_job(
        poll_lowiqpts_relay, "interval", seconds=relay_poll_seconds,
        id="lowiqpts_relay_poll", replace_existing=True, **_SHORT_TICK,
    )
    # sync_pending_fills can legitimately take longer than its 30s interval
    # when the archive lags. Keep a single in-flight tick so overlapping runs
    # do not amplify archive API pressure.
    scheduler.add_job(
        sync_pending_fills, "interval", seconds=30,
        id="fill_sync", replace_existing=True,
        max_instances=1, misfire_grace_time=60, coalesce=True,
    )
    scheduler.add_job(
        tick_edge_scanner, "interval", seconds=_EDGE_SCAN_SECONDS,
        id="edge_scanner", replace_existing=True, **_LONG_TICK,
    )
    scheduler.add_job(
        tick_news_warmup, "interval", minutes=_NEWS_WARMUP_MINUTES,
        id="news_warmup", replace_existing=True, **_LONG_TICK,
    )
    if vault_deposit_watch_enabled():
        scheduler.add_job(
            tick_vault_deposit_watch_job,
            "interval",
            seconds=vault_deposit_watch_interval_seconds(),
            id="vault_deposit_watch",
            replace_existing=True,
            **_LONG_TICK,
        )
    scheduler.add_job(
        tick_perf_slo, "interval", seconds=60,
        id="perf_slo", replace_existing=True, **_SHORT_TICK,
    )
    # Desk text-to-trade plan runner: trigger watch + TWAP/exit driving.
    # 5s keeps price triggers responsive without hammering the venue (mids
    # are fetched once per product per tick inside the controller).
    from src.nadobro.trading.desk_runtime import desk_enabled, tick_desk_runner

    if desk_enabled():
        scheduler.add_job(
            tick_desk_runner, "interval", seconds=5,
            id="desk_runner", replace_existing=True, **_SHORT_TICK,
        )
    scheduler.add_job(initial_ai_setup, "date", id="initial_ai_setup", replace_existing=True)
    scheduler.start()
    logger.info(
        "Scheduler started - alerts %ss, price tracker 60s, HOWL nightly 02:00 UTC, LOWIQ relay %ss, fill sync 30s, edge scanner %ss",
        _ALERT_SCAN_SECONDS,
        relay_poll_seconds,
        _EDGE_SCAN_SECONDS,
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def get_scheduler_diagnostics() -> dict:
    try:
        jobs = scheduler.get_jobs() if scheduler.running else []
    except Exception:
        jobs = []
    return {
        "running": bool(scheduler.running),
        "job_count": len(jobs),
        "alert_scan_seconds": int(_ALERT_SCAN_SECONDS),
    }
