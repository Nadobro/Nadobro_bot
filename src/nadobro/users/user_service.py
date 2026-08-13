import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from src.nadobro.models.database import UserRow, NetworkMode
from src.nadobro.db import query_one, query_all, execute, query_count
from src.nadobro.venue.nado_client import (
    get_nado_client,
    NadoClient,
    clear_client_cache,
    clear_linked_signer_cache,
)
from src.nadobro.i18n import get_active_language, localize_text
from src.nadobro.config import get_nado_builder_routing_config, get_product_id
from src.nadobro.utils.env import env_bool

logger = logging.getLogger(__name__)


def _loc(text):
    return localize_text(text, get_active_language())


_user_cache = {}
_USER_CACHE_TTL = 10
_USER_CACHE_MAX_ENTRIES = 512
# `get_user` is called via `run_blocking` from many handlers (16-worker
# thread pool). The eviction loop below iterates and pops while another
# worker may be reading or writing — without a lock this races. (Audit
# 2026-05.)
_user_cache_lock = threading.RLock()


def _cache_user(user: UserRow):
    with _user_cache_lock:
        if len(_user_cache) >= _USER_CACHE_MAX_ENTRIES:
            now = time.time()
            stale = [k for k, v in _user_cache.items() if now - float(v.get("ts") or 0) > _USER_CACHE_TTL]
            for k in stale:
                _user_cache.pop(k, None)
            while len(_user_cache) >= _USER_CACHE_MAX_ENTRIES:
                oldest = min(_user_cache, key=lambda k: float(_user_cache[k].get("ts") or 0))
                _user_cache.pop(oldest, None)
        _user_cache[user.telegram_id] = {"user": user, "ts": time.time()}


def _get_cached_user(telegram_id: int) -> Optional[UserRow]:
    with _user_cache_lock:
        entry = _user_cache.get(telegram_id)
        if entry and (time.time() - entry["ts"] < _USER_CACHE_TTL):
            return entry["user"]
        if entry:
            _user_cache.pop(telegram_id, None)
    return None


def invalidate_user_cache(telegram_id: Optional[int] = None):
    with _user_cache_lock:
        if telegram_id:
            _user_cache.pop(telegram_id, None)
        else:
            _user_cache.clear()


def get_or_create_user(
    telegram_id: int, username: str = None, language_code: str | None = None
) -> tuple[UserRow, bool, Optional[str]]:
    row = query_one("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    if row:
        if username:
            execute(
                "UPDATE users SET last_active = %s, telegram_username = %s WHERE telegram_id = %s",
                (datetime.now(timezone.utc).isoformat(), username, telegram_id),
            )
        else:
            execute(
                "UPDATE users SET last_active = %s WHERE telegram_id = %s",
                (datetime.now(timezone.utc).isoformat(), telegram_id),
            )
        user = UserRow(row)
        _cache_user(user)
        return user, False, None

    # Seed language from the Telegram client locale so a Korean device starts
    # in Korean. normalize_lang() falls back to "en" for unknown/unsupported
    # codes, preserving the previous default behaviour when no code is given.
    from src.nadobro.i18n import normalize_lang
    seed_lang = normalize_lang(language_code)
    execute(
        "INSERT INTO users (telegram_id, telegram_username, language, network_mode) VALUES (%s, %s, %s, %s)",
        (telegram_id, username, seed_lang, "mainnet"),
    )
    row = query_one("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    user = UserRow(row) if row else UserRow({"telegram_id": telegram_id, "network_mode": "mainnet"})
    _cache_user(user)
    return user, True, None


def get_user(telegram_id: int) -> Optional[UserRow]:
    cached = _get_cached_user(telegram_id)
    if cached:
        return cached
    row = query_one("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    if not row:
        return None
    user = UserRow(row)
    _cache_user(user)
    return user


def switch_network(telegram_id: int, network: str) -> tuple[bool, str]:
    user = get_user(telegram_id)
    if not user:
        return False, _loc("User not found. Use /start first.")

    # Stop all active strategies before changing the active mode so teardown
    # runs against the old network state instead of racing the new one.
    try:
        from src.nadobro.strategy.bot_runtime import stop_all_strategies_for_user
        stop_all_strategies_for_user(telegram_id)
        logger.info("Stopped all strategies for user %s due to network switch", telegram_id)
    except Exception as e:
        logger.warning("Failed to stop strategies on network switch for %s: %s", telegram_id, e)

    execute("UPDATE users SET network_mode = %s WHERE telegram_id = %s", (network, telegram_id))
    # SCALE FIX: invalidate only THIS user's client caches across both networks
    # so a single user switching mode never disrupts the other 999 users' cached
    # SDK sessions. Previously this cleared every user's NadoClient + readonly
    # cache, causing a thundering-herd against the venue.
    _invalidate_user_caches(user.main_address, telegram_id)
    invalidate_user_cache(telegram_id)

    addr = user.main_address
    if addr:
        msg = f"{_loc('Switched to')} {network} {_loc('mode.')}\n{_loc('Active wallet:')} `{addr}`"
    else:
        msg = (
            f"{_loc('Switched to')} {network} {_loc('mode.')}\n"
            f"{_loc('Link your wallet via the Wallet button to trade.')}"
        )
    return True, msg


def get_user_nado_client(telegram_id: int, network: str | None = None, **kwargs) -> Optional[NadoClient]:
    user = get_user(telegram_id)
    if not user or not user.linked_signer_address or not user.main_address:
        return None
    enc_pk = user.encrypted_linked_signer_pk
    if not enc_pk:
        return None
    if user.salt:
        logger.error("User %s has a legacy passphrase-encrypted key. They must unlink and re-link their wallet.", telegram_id)
        return None
    try:
        import base64
        from src.nadobro.core.crypto import decrypt_with_server_key
        ciphertext = base64.b64decode(enc_pk) if isinstance(enc_pk, str) else enc_pk
        pk_bytes = decrypt_with_server_key(ciphertext)
        pk = pk_bytes.decode("utf-8") if isinstance(pk_bytes, bytes) else pk_bytes
        selected_network = str(network or user.network_mode.value)
        client = get_nado_client(pk, selected_network, main_address=user.main_address)
        client.acting_user_id = int(telegram_id)
        return client
    except Exception as e:
        logger.warning("Failed to get Nado client for user %s: %s", telegram_id, e)
        return None


_readonly_cache: dict[str, dict] = {}
_READONLY_CACHE_TTL = 60
_READONLY_CACHE_MAX_ENTRIES = 256


def _prune_readonly_cache(now: float | None = None) -> None:
    ts = now or time.time()
    stale = [k for k, v in _readonly_cache.items() if ts - float(v.get("ts") or 0) > _READONLY_CACHE_TTL]
    for k in stale:
        _readonly_cache.pop(k, None)
    while len(_readonly_cache) > _READONLY_CACHE_MAX_ENTRIES:
        oldest = min(_readonly_cache, key=lambda k: float(_readonly_cache[k].get("ts") or 0))
        _readonly_cache.pop(oldest, None)


def _invalidate_user_caches(address: Optional[str], telegram_id: int) -> None:
    """SCALE: per-user cache invalidation.

    Wipes only the cached NadoClient + readonly-client entries belonging to a
    single wallet address across both networks, plus the user's portfolio
    snapshot. Used on network switch / unlink so a single user's mutation
    never invalidates every other user's cached SDK sessions.
    """
    addr = (str(address or "").strip().lower()) or None
    if addr:
        for net in ("testnet", "mainnet"):
            try:
                clear_client_cache(address=addr, network=net)
            except Exception:
                logger.debug("clear_client_cache(addr=%s, net=%s) failed", addr, net, exc_info=True)
            try:
                # Also drop cached "verified" linkage state — otherwise a
                # user who re-links to a new signer would still hit the old
                # verified=True for up to 60s + Redis TTL.
                clear_linked_signer_cache(address=addr, network=net)
            except Exception:
                logger.debug(
                    "clear_linked_signer_cache(addr=%s, net=%s) failed",
                    addr, net, exc_info=True,
                )
        for key in list(_readonly_cache.keys()):
            # Readonly cache key is "ro:<addr>:<network>".
            if key.startswith(f"ro:{addr}:"):
                _readonly_cache.pop(key, None)
    try:
        from src.nadobro.venue.nado_sync import clear_cache as _clear_portfolio_snapshot_cache

        _clear_portfolio_snapshot_cache(int(telegram_id))
    except Exception:
        logger.debug("nado_sync cache clear failed user=%s", telegram_id, exc_info=True)


def _is_wallet_fully_linked(user: Optional[UserRow]) -> bool:
    return bool(user and user.linked_signer_address and user.encrypted_linked_signer_pk)


def get_user_readonly_client(telegram_id: int, network: str | None = None) -> Optional[NadoClient]:
    user = get_user(telegram_id)
    if not user or not user.main_address:
        return None
    selected_network = str(network or user.network_mode.value)
    cache_key = f"ro:{user.main_address}:{selected_network}"
    _prune_readonly_cache()
    cached = _readonly_cache.get(cache_key)
    if cached and time.time() - float(cached.get("ts") or 0) < _READONLY_CACHE_TTL:
        return cached.get("client")
    if cached:
        _readonly_cache.pop(cache_key, None)
    # NO_ORDERS_AUDIT-FIX-R6b: go through the digest-keyed cache so this
    # client is shared with every other consumer (knowledge_service, copy
    # service, market_snapshot). The 60s ``_readonly_cache`` TTL stays in
    # place as a per-user fast path that avoids the lock on every call.
    from src.nadobro.venue.nado_client import get_or_create_readonly_client
    client = get_or_create_readonly_client(
        user.main_address, selected_network, user_id=int(telegram_id),
    )
    client.acting_user_id = int(telegram_id)
    _readonly_cache[cache_key] = {"client": client, "ts": time.time()}
    return client


def get_user_wallet_info(telegram_id: int, verify_signer: bool = False) -> Optional[dict]:
    user = get_user(telegram_id)
    if not user:
        return None
    network = user.network_mode.value
    linked = _is_wallet_fully_linked(user)
    info = {
        "testnet_address": user.main_address,
        "mainnet_address": user.main_address,
        "network": network,
        "active_address": user.main_address,
        "linked_signer_address": user.linked_signer_address,
        "is_linked": linked,
        "testnet_ready": linked,
        "mainnet_ready": linked,
        "signer_verification": None,
    }

    if verify_signer and linked and user.main_address:
        try:
            readonly = get_user_readonly_client(telegram_id)
            if readonly:
                check = readonly.verify_linked_signer(user.linked_signer_address)
                info["signer_verification"] = check
        except Exception as e:
            logger.warning("Signer verification failed for user %s: %s", telegram_id, e)
            info["signer_verification"] = {"verified": False, "error": str(e), "current_signer": None, "expected_signer": user.linked_signer_address}

    return info


def save_linked_signer(
    telegram_id: int,
    main_address: str,
    linked_signer_address: str,
    encrypted_pk: bytes,
    salt: bytes = None,
) -> None:
    import base64
    execute(
        "UPDATE users SET main_address = %s, linked_signer_address = %s, encrypted_linked_signer_pk = %s, salt = NULL WHERE telegram_id = %s",
        (
            main_address,
            linked_signer_address,
            base64.b64encode(encrypted_pk).decode("ascii"),
            telegram_id,
        ),
    )
    invalidate_user_cache(telegram_id)
    _invalidate_user_caches(main_address, telegram_id)


def has_mode_private_key(telegram_id: int, network: str) -> bool:
    user = get_user(telegram_id)
    if not user:
        return False
    return _is_wallet_fully_linked(user)


def ensure_active_wallet_ready(telegram_id: int, *, verify_on_chain: bool = True) -> tuple[bool, str]:
    user = get_user(telegram_id)
    if not user:
        return False, _loc("User not found. Use /start first.")
    if not has_mode_private_key(telegram_id, user.network_mode.value):
        return (
            False,
            _loc("Wallet not linked. Use the Wallet button to connect your wallet (Linked Signer)."),
        )
    if user.network_mode.value == "mainnet":
        require_linked = env_bool("NADO_REQUIRE_MAINNET_LINKED_SIGNER", True)
        if require_linked and user.main_address and user.linked_signer_address:
            if str(user.main_address).lower() == str(user.linked_signer_address).lower():
                return (
                    False,
                    _loc("Mainnet requires a linked signer hot key. Re-link wallet with a separate signer key."),
                )
    if user.salt:
        return False, "Your wallet key uses an old format. Please unlink and re-link your wallet."
    # Tap/preview cards must not wait on get_linked_signer. Start/trade paths
    # keep verify_on_chain=True so a mismatched 1CT key still blocks orders.
    if verify_on_chain and user.linked_signer_address and user.main_address:
        try:
            readonly = get_user_readonly_client(telegram_id, network=user.network_mode.value)
            if readonly:
                check = readonly.verify_linked_signer(user.linked_signer_address)
                if not check.get("error") and not check.get("verified"):
                    return (
                        False,
                        _loc("Linked signer on Nado does not match the bot key. Open Wallet and re-link your 1CT signer."),
                    )
        except Exception:
            pass
    return True, ""


def get_runtime_wallet_readiness(telegram_id: int, verify_signer: bool = True) -> dict:
    user = get_user(telegram_id)
    if not user:
        return {"ready": False, "error": "user_not_found"}
    ready, message = ensure_active_wallet_ready(telegram_id)
    selected_network = str(user.network_mode.value)
    has_signing_client = bool(get_user_nado_client(telegram_id, network=selected_network))
    has_readonly_client = bool(get_user_readonly_client(telegram_id, network=selected_network))
    payload = {
        "ready": bool(ready and has_signing_client),
        "network": selected_network,
        "message": message,
        "has_signing_client": has_signing_client,
        "has_readonly_client": has_readonly_client,
        "linked_signer_address": user.linked_signer_address,
        "main_address": user.main_address,
    }
    if verify_signer and user.linked_signer_address and user.main_address:
        try:
            ro = get_user_readonly_client(telegram_id, network=selected_network)
            if ro:
                payload["signer_verification"] = ro.verify_linked_signer(user.linked_signer_address)
        except Exception as e:
            payload["signer_verification"] = {"verified": False, "error": str(e)}
    signer_check = payload.get("signer_verification") or {}
    if verify_signer and signer_check and not signer_check.get("error") and not signer_check.get("verified"):
        payload["ready"] = False
        payload["message"] = _loc("Linked signer on Nado does not match the bot key. Open Wallet and re-link your 1CT signer.")
    return payload


def run_strategy_start_preflight(
    telegram_id: int,
    product: str,
    network: str,
    vol_market: str = "perp",
) -> tuple[bool, str]:
    ready, message = ensure_active_wallet_ready(telegram_id)
    if not ready:
        return False, message

    signing_client = get_user_nado_client(telegram_id, network=network)
    if not signing_client:
        return False, "Wallet signing client unavailable for active mode. Re-link signer in Wallet settings."

    try:
        get_nado_builder_routing_config(network)
    except ValueError as e:
        # Keep infrastructure/config detail in (redacted) logs, not in the
        # user-facing string.
        logger.warning("Builder routing misconfigured for user %s on %s: %s", telegram_id, network, e)
        return False, "Order routing is temporarily misconfigured. Please try again shortly."

    vm = str(vol_market or "perp").strip().lower()
    if vm == "spot":
        from src.nadobro.config import get_spot_product_id, normalize_volume_spot_symbol

        sym = normalize_volume_spot_symbol(str(product or "").strip())
        product_id = get_spot_product_id(sym, network=network)
        if product_id is None:
            return False, f"Unknown spot product '{sym}' on {network}."
    else:
        product_id = get_product_id(str(product).upper(), network=network)
        if product_id is None:
            return False, f"Unknown product '{product}'."
    try:
        mp = signing_client.get_market_price(int(product_id))
        mid = float((mp or {}).get("mid") or 0.0)
    except Exception as e:
        # Don't echo the raw exception (can carry host/URL/internal detail) to
        # the user — log it and show a generic message.
        logger.warning("get_market_price failed in preflight for %s (%s): %s", product, network, e)
        return False, f"Could not fetch market price for {str(product).upper()} ({network}). Market may be unavailable."
    if mid <= 0:
        return False, f"Could not fetch market price for {str(product).upper()} ({network})."

    return True, ""


def update_trade_stats(
    telegram_id: int,
    volume_usd: float,
    increment_trade_count: bool = True,
    *,
    network: str = "mainnet",
):
    network = str(network or "mainnet").strip().lower()
    volume_column = "testnet_volume_usd" if network == "testnet" else "mainnet_volume_usd"
    trade_delta = 1 if increment_trade_count else 0
    volume_delta = float(volume_usd or 0.0)
    execute(
        f"""
        UPDATE users
        SET total_trades = COALESCE(total_trades, 0) + %s,
            total_volume_usd = COALESCE(total_volume_usd, 0) + %s,
            {volume_column} = COALESCE({volume_column}, 0) + %s,
            last_trade_at = %s
        WHERE telegram_id = %s
        """,
        (
            trade_delta,
            volume_delta,
            volume_delta,
            datetime.now(timezone.utc).isoformat(),
            telegram_id,
        ),
    )
    invalidate_user_cache(telegram_id)
    try:
        from src.nadobro.users.referral_service import record_referred_volume

        record_referred_volume(
            int(telegram_id),
            float(volume_usd or 0.0),
            network=network,
            increment_trade_count=bool(increment_trade_count),
        )
    except Exception as e:
        logger.warning("Failed to record referral volume for user %s: %s", telegram_id, e)


def get_all_users_count() -> int:
    return query_count("SELECT COUNT(*) FROM users")


def get_active_users_count() -> int:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    return query_count("SELECT COUNT(*) FROM users WHERE last_active >= %s", (cutoff,))


def remove_user_private_key(telegram_id: int, network: str = "testnet") -> tuple[bool, str]:
    # Resolve the address BEFORE wiping the row so we can target this user's
    # entries in the global caches instead of nuking every other user's.
    prior_user = get_user(telegram_id)
    prior_address = prior_user.main_address if prior_user else None
    execute(
        "UPDATE users SET main_address = NULL, linked_signer_address = NULL, encrypted_linked_signer_pk = NULL, salt = NULL WHERE telegram_id = %s",
        (telegram_id,),
    )
    invalidate_user_cache(telegram_id)
    _invalidate_user_caches(prior_address, telegram_id)
    try:
        from src.nadobro.users.audit_log import record_audit_event

        record_audit_event(telegram_id, "wallet_unlinked", f"network={network}")
    except Exception:
        pass
    return True, _loc("{network} wallet unlinked. You can link again via Wallet button.").format(network=network)


def update_user_language(telegram_id: int, lang: str, *, source: str = "settings"):
    """Persist a user's language and emit an audit trail.

    Previously this was a silent UPDATE, so a language switch left no trace in
    the logs. We now normalize the value, log the change, and write an
    append-only audit row (fail-soft) so the switch is observable.
    """
    from src.nadobro.i18n import normalize_lang

    new_lang = normalize_lang(lang)
    previous = None
    try:
        existing = query_one("SELECT language FROM users WHERE telegram_id = %s", (telegram_id,))
        if existing:
            previous = existing.get("language")
    except Exception:
        pass

    execute("UPDATE users SET language = %s WHERE telegram_id = %s", (new_lang, telegram_id))
    invalidate_user_cache(telegram_id)

    if previous != new_lang:
        logger.info(
            "language_change telegram_id=%s old=%s new=%s source=%s",
            telegram_id, previous or "en", new_lang, source,
        )
        try:
            from src.nadobro.users.audit_log import record_audit_event
            record_audit_event(
                telegram_id, "language_changed", f"{previous or 'en'}->{new_lang} via {source}"
            )
        except Exception:
            pass
