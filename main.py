import os
import sys
import logging
import asyncio
import signal
import json
import time
from urllib.parse import urlparse
from dotenv import load_dotenv

from src.nadobro.core.log_redaction import RedactingFormatter, SensitiveDataRedactFilter


_redact_filter = SensitiveDataRedactFilter()
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.addFilter(_redact_filter)
_stream_handler.setFormatter(RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stream_handler],
)

logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
# NO_ORDERS_AUDIT-FIX-R6a: the Nado SDK logs "Initializing default {mode}
# context" at INFO on every default-context construction (~400 ms each). With
# repeated client construction across users, this dominates the logs and
# masks real signal. Quiet it to WARNING; real errors still surface.
logging.getLogger("nado_protocol").setLevel(logging.WARNING)
logging.getLogger("nado_protocol.client").setLevel(logging.WARNING)

logger = logging.getLogger("nadobro")

# Load local .env during development before reading config vars.
load_dotenv()

from src.nadobro.config import TELEGRAM_TOKEN, ENCRYPTION_KEY, DATABASE_URL
from src.nadobro.core.crypto import validate_encryption_key
from src.nadobro.utils.env import env_bool, env_int, env_str

if not ENCRYPTION_KEY:
    logger.error(
        "ENCRYPTION_KEY is required for wallet encryption. "
        "Please set ENCRYPTION_KEY in environment variables or a local .env file."
    )
    sys.exit(1)

try:
    validate_encryption_key()
    logger.info("Encryption key validated successfully")
except RuntimeError as e:
    logger.error(str(e))
    sys.exit(1)


def check_config():
    transport_mode, webhook_url, webhook_path = _resolve_transport_settings()
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not DATABASE_URL and not os.environ.get("SUPABASE_DATABASE_URL"):
        missing.append("DATABASE_URL or SUPABASE_DATABASE_URL")
    if transport_mode not in ("polling", "webhook"):
        missing.append("TELEGRAM_TRANSPORT must be polling or webhook")
    if transport_mode == "webhook" and not webhook_url:
        missing.append("TELEGRAM_WEBHOOK_URL (required when TELEGRAM_TRANSPORT=webhook)")
    data_env = (os.environ.get("DATA_ENV") or "").strip()
    if data_env and data_env not in ("nadoMainnet", "nadoTestnet"):
        missing.append("DATA_ENV must be nadoMainnet or nadoTestnet when set")
    xai_key = os.environ.get("XAI_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    if not xai_key and not openai_key:
        logger.warning(
            "No AI key set (XAI_API_KEY/OPENAI_API_KEY) - support AI features will be unavailable"
        )
    elif not xai_key:
        logger.info("XAI_API_KEY not set - OpenAI-only support mode enabled")
    elif not openai_key:
        logger.info("OPENAI_API_KEY not set - xAI-only support mode enabled")

    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    if data_env == "nadoMainnet":
        require_linked = env_bool("NADO_REQUIRE_MAINNET_LINKED_SIGNER", True)
        if require_linked:
            logger.info("Mainnet guardrail enabled: linked signer required for active wallet readiness checks.")
    if env_bool("NADO_TOOLING_ENABLE", True):
        logger.info("Nado tooling adapter enabled (SDK writes remain primary).")

    logger.info("Configuration check passed (transport=%s)", transport_mode)


def _resolve_transport_settings():
    transport_mode = (os.environ.get("TELEGRAM_TRANSPORT") or "").strip().lower()
    webhook_path = (os.environ.get("TELEGRAM_WEBHOOK_PATH") or "/telegram/webhook").strip()
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path
    webhook_url = (os.environ.get("TELEGRAM_WEBHOOK_URL") or "").strip()

    if not transport_mode:
        # Auto-prefer webhook on Fly deployments to reduce update polling latency.
        if os.environ.get("FLY_APP_NAME"):
            transport_mode = "webhook"
        elif webhook_url:
            transport_mode = "webhook"
        else:
            transport_mode = "polling"
    if transport_mode not in ("polling", "webhook"):
        transport_mode = "polling"

    if transport_mode == "webhook" and not webhook_url:
        fly_app = os.environ.get("FLY_APP_NAME", "").strip()
        if fly_app:
            webhook_url = f"https://{fly_app}.fly.dev{webhook_path}"

    return transport_mode, webhook_url, webhook_path


async def _start_bootstrap_health_server(port: int):
    """Serve a minimal temporary health endpoint during webhook startup."""
    async def _bootstrap_health_handler(reader, writer):
        try:
            await reader.read(4096)
            body = json.dumps({"status": "booting", "ts": time.time()}).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_server(_bootstrap_health_handler, "0.0.0.0", port)


def _runtime_health_payload() -> dict:
    payload = {"status": "ok", "ts": time.time()}
    try:
        from src.nadobro.trading.execution_queue import get_queue_diagnostics
        from src.nadobro.runtime.runtime_supervisor import get_runtime_supervisor_diagnostics
        from src.nadobro.trading.copy_service import get_copy_polling_diagnostics
        from src.nadobro.runtime.scheduler import get_scheduler_diagnostics
        from src.nadobro.core.perf import summary_lines

        queue_diag = get_queue_diagnostics()
        scheduler_diag = get_scheduler_diagnostics()
        payload.update(
            {
                "queue": queue_diag,
                "scheduler": scheduler_diag,
                "runtime_supervisor": get_runtime_supervisor_diagnostics(),
                "copy_polling": get_copy_polling_diagnostics(),
                "perf_top": summary_lines(top_n=5),
            }
        )
        if int(queue_diag.get("strategy_qsize") or 0) >= int(queue_diag.get("strategy_qmax") or 0):
            payload["status"] = "degraded"
        if not scheduler_diag.get("running"):
            payload["status"] = "degraded"
        try:
            from src.nadobro.venue.gateway_budget import snapshot as gateway_snapshot
            from src.nadobro.venue.ws_health import snapshot as ws_snapshot
            from src.nadobro.venue.market_feed import snapshot as market_snapshot
            from src.nadobro.core.async_utils import pool_stats
            from src.nadobro.core.user_circuit import snapshot as circuit_snapshot
            from src.nadobro.core.feature_flags import strategy_scheduler_enabled
            from src.nadobro.strategy.strategy_scheduler import get_scheduler

            payload["gateway"] = gateway_snapshot()
            payload["ws_health"] = ws_snapshot()
            payload["market_feed"] = market_snapshot()
            payload["thread_pools"] = pool_stats()
            payload["user_circuit"] = circuit_snapshot()
            if strategy_scheduler_enabled():
                payload["strategy_scheduler"] = get_scheduler().stats()
        except Exception:
            pass
    except Exception as e:
        payload["status"] = "degraded"
        payload["health_error"] = str(e)
    return payload


def setup_bot():
    from telegram.ext import (
        Application,
        ApplicationHandlerStop,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        TypeHandler,
        filters,
    )
    from telegram import Update
    from telegram.constants import ChatType

    from src.nadobro.config import BOT_USERNAME
    from src.nadobro.handlers.commands import (
        cmd_start, cmd_help, cmd_status, cmd_ops, cmd_stop_all, cmd_revoke,
        cmd_mm_status, cmd_mm_fills, cmd_airdrop,
    )
    from src.nadobro.handlers.managed_agent import cmd_agent_on, cmd_agent_off, cmd_agent_status
    from src.nadobro.handlers.brief_commands import cmd_market_news, cmd_morning_brief, cmd_night_howl
    from src.nadobro.handlers.messages import handle_message
    from src.nadobro.handlers.callbacks import handle_callback
    from src.nadobro.handlers.update_serialization import (
        with_callback_ack,
        with_user_serialized,
    )

    async def _private_chat_only(update: Update, context):
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            raise ApplicationHandlerStop()

    async def _language_middleware(update: Update, context):
        user = update.effective_user
        if user:
            from src.nadobro.core.async_utils import run_blocking_db
            from src.nadobro.i18n import _ACTIVE_LANG, normalize_lang
            from src.nadobro.users.user_service import get_user
            # Runs on EVERY update, ahead of every handler. ``get_user`` serves a
            # 10s in-process cache but falls through to a synchronous psycopg2
            # round-trip — on the event loop that stalls every other user's tick
            # and tap. Hand the miss to the DB pool. (The contextvar is still set
            # here, in the handler's own context, so localization is unaffected.)
            existing = await run_blocking_db(get_user, user.id)
            if existing is not None and getattr(existing, "language", None):
                # Respect the user's stored choice (single source of truth).
                lang = normalize_lang(existing.language)
            else:
                # Brand-new user whose row doesn't exist yet: localize the very
                # first screen from the Telegram client locale. Creation will
                # persist this same value (see get_or_create_user).
                lang = normalize_lang(getattr(user, "language_code", None))
            _ACTIVE_LANG.set(lang)

    async def _error_handler(update: object, context) -> None:
        logger.exception("Unhandled Telegram update error", exc_info=context.error)

    async def _post_init(application) -> None:
        try:
            me = await application.bot.get_me()
            actual = (me.username or "").lstrip("@")
            expected = (BOT_USERNAME or "").lstrip("@")
            if expected and actual and actual.lower() != expected.lower():
                logger.warning(
                    "BOT_USERNAME mismatch: configured=%s telegram_getMe=%s. Referral links may be wrong.",
                    expected,
                    actual,
                )
        except Exception as exc:
            logger.warning("Could not validate BOT_USERNAME against Telegram getMe(): %s", exc)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(TypeHandler(Update, _private_chat_only), group=-2)
    app.add_handler(TypeHandler(Update, _language_middleware), group=-1)

    app.add_handler(CommandHandler("start", with_user_serialized(cmd_start)))
    app.add_handler(CommandHandler("help", with_user_serialized(cmd_help)))
    app.add_handler(CommandHandler("status", with_user_serialized(cmd_status)))
    app.add_handler(CommandHandler("ops", with_user_serialized(cmd_ops)))
    app.add_handler(CommandHandler("stop_all", with_user_serialized(cmd_stop_all)))
    app.add_handler(CommandHandler("revoke", with_user_serialized(cmd_revoke)))
    app.add_handler(CommandHandler("agent_on", with_user_serialized(cmd_agent_on)))
    app.add_handler(CommandHandler("agent_off", with_user_serialized(cmd_agent_off)))
    app.add_handler(CommandHandler("agent_status", with_user_serialized(cmd_agent_status)))
    app.add_handler(CommandHandler("brief", with_user_serialized(cmd_morning_brief)))
    app.add_handler(CommandHandler("howl", with_user_serialized(cmd_night_howl)))
    app.add_handler(CommandHandler("news", with_user_serialized(cmd_market_news)))
    app.add_handler(CommandHandler("airdrop", with_user_serialized(cmd_airdrop)))
    # Phase 3: Tread-style live MM dashboard.
    app.add_handler(CommandHandler("mm_status", with_user_serialized(cmd_mm_status)))
    app.add_handler(CommandHandler("mm_fills", with_user_serialized(cmd_mm_fills)))
    # Desk text-to-trade plan list (TWAP / triggers / exits / spot).
    from src.nadobro.handlers.desk_handler import cmd_desk

    app.add_handler(CommandHandler("desk", with_user_serialized(cmd_desk)))

    # ``with_callback_ack`` sits OUTSIDE the serialization wrapper on purpose: the
    # button spinner is cleared before we queue behind this user's previous
    # action, so a tap feels instant even when the prior handler is still working.
    app.add_handler(CallbackQueryHandler(with_callback_ack(with_user_serialized(handle_callback))))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, with_user_serialized(handle_message)))
    app.add_error_handler(_error_handler)

    logger.info("Bot handlers registered (pure bot mode, language middleware active)")
    return app


async def run_bot():
    check_config()
    from src.nadobro.models.database import init_db

    logger.info("Initializing database...")
    init_db()
    logger.info("Database initialized")

    logger.info("Setting up Telegram bot...")
    bot_app = setup_bot()

    from src.nadobro.runtime.scheduler import set_bot_app, set_check_client, start_scheduler
    from src.nadobro.strategy.bot_runtime import (
        set_bot_app as set_runtime_app,
        restore_running_bots,
        stop_runtime,
        handle_strategy_job,
    )
    from src.nadobro.runtime.runtime_supervisor import (
        start_runtime_supervisor,
        stop_runtime_supervisor,
    )
    from src.nadobro.runtime.scheduler import handle_alert_job
    from src.nadobro.trading.execution_queue import register_handlers, start_workers, stop_workers
    from src.nadobro.trading.copy_service import set_copy_bot_app
    from src.nadobro.trading.copy_service import start_copy_polling, stop_copy_polling
    from src.nadobro.vault.vault_deposit_watch_service import set_vault_watch_bot_app
    set_bot_app(bot_app)
    set_runtime_app(bot_app)
    set_copy_bot_app(bot_app)
    set_vault_watch_bot_app(bot_app)
    # P2 fill-nudge: venue WS fill events wake the strategy runtime so MM
    # controllers re-quote within ~a second of a fill instead of next tick.
    # Kill switch: NADO_FILL_NUDGE=false reverts to pure interval ticking.
    from src.nadobro.core.feature_flags import fill_nudge_enabled

    if fill_nudge_enabled():
        try:
            from src.nadobro.venue.nado_ws import register_fill_listener
            from src.nadobro.strategy.bot_runtime import nudge_strategy_cycle

            register_fill_listener(nudge_strategy_cycle)
            logger.info("Fill-nudge listener registered (WS fills trigger immediate cycles)")
        except Exception:
            logger.warning("Fill-nudge listener registration failed", exc_info=True)
    register_handlers(handle_strategy_job, handle_alert_job)
    _sw_raw = env_str("NADO_STRATEGY_WORKERS")
    _sw = int(_sw_raw) if _sw_raw else None
    start_workers(
        strategy_workers=_sw,
        alert_workers=env_int("NADO_ALERT_WORKERS", 1),
    )
    start_runtime_supervisor()
    from src.nadobro.runtime.runtime_supervisor import runtime_mode
    from src.nadobro.trading.execution_queue import get_queue_diagnostics

    _diag = get_queue_diagnostics()
    logger.info(
        "Strategy queue: NADO_STRATEGY_WORKERS=%s workers=%s NADO_RUNTIME_MODE=%s NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS=%s",
        _sw_raw or "auto",
        _diag.get("strategy_workers_target"),
        runtime_mode(),
        (os.environ.get("NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS") or "180").strip(),
    )

    try:
        from src.nadobro.venue.nado_client import NadoClient
        alert_network = (os.environ.get("NADO_ALERT_CHECK_NETWORK") or "testnet").strip().lower()
        alert_pk = (os.environ.get("NADO_ALERT_CHECK_PRIVATE_KEY") or "").strip()
        alert_address = (
            os.environ.get("NADO_ALERT_CHECK_ADDRESS")
            or "0x0000000000000000000000000000000000000000"
        ).strip()
        if alert_pk:
            # NO_ORDERS_AUDIT-FIX-R6b: go through the digest-keyed cache so
            # we don't pay the SDK init cost on every restart-without-restart
            # (e.g. health-check restarts).
            from src.nadobro.venue.nado_client import get_or_create_signing_client
            alert_client = get_or_create_signing_client(alert_pk, alert_network)
        else:
            # Read-only client is sufficient for alert price checks.
            from src.nadobro.venue.nado_client import get_or_create_readonly_client
            alert_client = get_or_create_readonly_client(alert_address, alert_network)
        set_check_client(alert_client)
        from src.nadobro.venue.market_feed import bind_fetcher

        bind_fetcher(alert_client.get_all_market_prices)
        logger.info("Alert price-check client initialized (network=%s)", alert_network)
    except Exception as e:
        logger.warning(f"Alert price-check client failed to initialize: {e}")

    start_scheduler()
    from src.nadobro.core.feature_flags import strategy_scheduler_enabled
    from src.nadobro.strategy.strategy_scheduler import get_scheduler
    from src.nadobro.strategy.bot_runtime import _load_state

    if strategy_scheduler_enabled():
        await get_scheduler().start(_load_state)
        logger.info("Central strategy scheduler started")
    # Redeploys must never re-arm strategies on their own (operator rule:
    # resume is strictly user-initiated). Opt back in with
    # NADO_AUTO_RESTORE_STRATEGIES=1 only for supervised environments.
    auto_restore = env_bool("NADO_AUTO_RESTORE_STRATEGIES", False)
    restore_running_bots(enabled=auto_restore)
    try:
        from src.nadobro.core.feature_flags import time_limit_enabled
        from src.nadobro.strategy.time_limit_watcher import time_limit_tick

        if time_limit_enabled():
            asyncio.create_task(time_limit_tick())
    except Exception as e:
        logger.warning("Could not schedule startup time-limit catch-up: %s", e)

    portfolio_history_enabled = env_bool("NADO_PORTFOLIO_HISTORY", True)
    if portfolio_history_enabled:
        try:
            from src.nadobro.portfolio.portfolio_history_worker import (
                start_portfolio_history_worker,
            )

            start_portfolio_history_worker()
        except Exception as e:
            logger.warning("Could not start portfolio history worker: %s", e)

    transport_mode, webhook_url, webhook_path = _resolve_transport_settings()
    webhook_secret = (os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    if transport_mode == "webhook" and not webhook_secret:
        raise RuntimeError(
            "TELEGRAM_WEBHOOK_SECRET must be set when TELEGRAM_TRANSPORT=webhook. "
            "Webhook mode without a secret allows spoofed updates."
        )
    if transport_mode == "webhook" and webhook_url:
        parsed = urlparse(webhook_url)
        if parsed.path.rstrip("/") != webhook_path.rstrip("/"):
            base = f"{parsed.scheme}://{parsed.netloc}"
            webhook_url = base + webhook_path

    webhook_listen = env_str("TELEGRAM_WEBHOOK_LISTEN", "0.0.0.0")
    # Prefer TELEGRAM_WEBHOOK_PORT when set so a reverse proxy can own PORT (e.g. nginx on 8080, PTB on 8082).
    webhook_port = env_int("TELEGRAM_WEBHOOK_PORT", env_int("PORT", 8080))
    bootstrap_health_server = None
    async def _start_polling_mode():
        # Ensure Telegram stops sending updates to webhook before polling starts.
        try:
            await bot_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logger.warning("Could not delete existing webhook before polling fallback: %s", e)
        await bot_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info("Nadobro is live in polling mode.")

    if transport_mode == "webhook":
        try:
            bootstrap_health_server = await _start_bootstrap_health_server(webhook_port)
            logger.info("Bootstrap health check listening on port %s", webhook_port)
        except Exception as e:
            logger.warning("Bootstrap health server failed to start (non-fatal): %s", e)

    logger.info("Starting bot (transport=%s)...", transport_mode)
    await bot_app.initialize()
    await bot_app.start()

    # bot_data is process memory: restore any LOWIQPTS points refresh that was
    # in flight before this process (re)started so the user's flow resumes
    # instead of silently dropping their pending request.
    try:
        from src.nadobro.users.points_service import rehydrate_lowiqpts_pending_state
        rehydrate_lowiqpts_pending_state(bot_app)
    except Exception as e:
        logger.warning("LOWIQPTS pending-state rehydration failed (non-fatal): %s", e)

    # Copy trading boots AFTER bot_app.initialize()/start() — the stand-down
    # prompt and poll-loop notifications need a working bot API.
    copy_enabled = env_bool("NADO_COPY_TRADING", True)
    if copy_enabled:
        # Redeploy rule: boot = stand-down. Active mirrors are paused (owners
        # get a Resume/Stop prompt) unless auto-resume is explicitly enabled.
        if not env_bool("NADO_COPY_AUTO_RESUME", False):
            from src.nadobro.trading.copy_service import boot_stand_down_mirrors

            try:
                stood_down = await boot_stand_down_mirrors()
                if stood_down:
                    logger.info(
                        "Copy boot stand-down: paused %d mirror(s); resume is user-initiated",
                        stood_down,
                    )
            except Exception:
                logger.warning("Copy boot stand-down failed", exc_info=True)
        await start_copy_polling()
        logger.info("Copy trading polling started")

    from telegram import BotCommand
    await bot_app.bot.set_my_commands([
        BotCommand("start", "Open your home dashboard"),
        BotCommand("help", "Guide and examples"),
        BotCommand("desk", "Talk out a trade, then confirm"),
        BotCommand("status", "Bot and strategy status"),
        BotCommand("ops", "Order flow and diagnostics"),
        BotCommand("brief", "Your morning market brief"),
        BotCommand("howl", "Your Night HOWL trade reports"),
        BotCommand("news", "Latest market news"),
        BotCommand("airdrop", "Check your Ink airdrop allocation"),
        BotCommand("mm_status", "Live market-making dashboard"),
        BotCommand("mm_fills", "Recent MM fills"),
        BotCommand("stop_all", "Stop strategies and flatten bot exposure"),
        BotCommand("revoke", "Revoke your 1CT signer"),
        BotCommand("agent_on", "Turn on managed AI"),
        BotCommand("agent_off", "Turn off managed AI"),
        BotCommand("agent_status", "Check managed AI"),
    ])
    logger.info("Bot commands registered in Menu")

    try:
        from telegram import MenuButtonCommands

        await bot_app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Telegram menu button reset to commands")
    except Exception as e:
        logger.warning("Telegram menu button not updated: %s", e)

    if transport_mode == "webhook":
        if bootstrap_health_server:
            try:
                bootstrap_health_server.close()
                await bootstrap_health_server.wait_closed()
            except Exception:
                pass
        logger.info(
            "Starting webhook server listen=%s port=%s path=%s",
            webhook_listen,
            webhook_port,
            webhook_path,
        )
        try:
            await bot_app.updater.start_webhook(
                listen=webhook_listen,
                port=webhook_port,
                url_path=webhook_path.lstrip("/"),
                webhook_url=webhook_url,
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
                secret_token=webhook_secret or None,
            )
            logger.info("Nadobro is live in webhook mode.")
        except OSError as e:
            if getattr(e, "errno", None) == 98:
                logger.error(
                    "Webhook bind failed on %s:%s (address in use). Falling back to polling mode.",
                    webhook_listen,
                    webhook_port,
                )
                await _start_polling_mode()
            else:
                raise
    else:
        await _start_polling_mode()

        # Polling mode only: lightweight TCP health responder on PORT.
        port_str = os.environ.get("PORT")
        if port_str:
            try:
                port = int(port_str)

                async def _health_handler(reader, writer):
                    try:
                        await reader.read(4096)
                        payload = _runtime_health_payload()
                        body = json.dumps(payload).encode("utf-8")
                        status_line = b"HTTP/1.1 200 OK\r\n" if payload.get("status") == "ok" else b"HTTP/1.1 503 Service Unavailable\r\n"
                        writer.write(
                            status_line
                            + b"Content-Type: application/json\r\n"
                            + f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
                            + body
                        )
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                health_server = await asyncio.start_server(_health_handler, "0.0.0.0", port)

                async def _serve_health():
                    async with health_server:
                        await asyncio.Future()  # run until cancelled

                asyncio.create_task(_serve_health())
                logger.info("Health check listening on port %s", port)
            except Exception as e:
                logger.warning("Health server failed (non-fatal): %s", e)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_signal_safely(sig_name: str) -> None:
        logger.info("Received signal %s, shutting down...", sig_name)
        stop_event.set()

    # AUDIT-FIX-MAIN-1: prefer loop.add_signal_handler so the callback runs
    # in the asyncio loop's context. Falls back to signal.signal() on
    # platforms (Windows) where add_signal_handler is unsupported. Calling
    # stop_event.set() from a sync signal handler is technically safe in
    # CPython because asyncio.Event.set() is thread-safe, but the asyncio
    # path is the documented one and avoids racing other libraries' sync
    # signal handlers.
    for _sig, _name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        try:
            loop.add_signal_handler(_sig, _handle_signal_safely, _name)
        except (NotImplementedError, RuntimeError):
            signal.signal(_sig, lambda s, _f, n=_name: _handle_signal_safely(n))

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        from src.nadobro.runtime.scheduler import stop_scheduler
        from src.nadobro.core.feature_flags import strategy_scheduler_enabled
        from src.nadobro.strategy.strategy_scheduler import get_scheduler
        from src.nadobro.venue.nado_ws import portfolio_ws

        stop_scheduler()
        if strategy_scheduler_enabled():
            await get_scheduler().stop()
        await portfolio_ws.stop()
        await stop_copy_polling()
        stop_runtime()
        stop_runtime_supervisor()
        await stop_workers()
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        logger.info("Bot stopped cleanly")


if __name__ == "__main__":
    asyncio.run(run_bot())
