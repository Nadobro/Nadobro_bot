"""Vanity-code referral system for Nadobro.

Each user can claim exactly one globally-unique referral code (3-20 chars,
A-Z/0-9). The code is immutable once set and may be redeemed by an unlimited
number of new users. No volume threshold gates referral creation.
"""
from __future__ import annotations

import logging
import string
from typing import Optional

import psycopg2.extras

from src.nadobro.config import BOT_USERNAME
from src.nadobro.db import execute, get_db, put_db, query_all, query_one
from src.nadobro.users.invite_service import (
    INVITE_CODE_LENGTH,
    _generate_plain_code,
    _hash_code,
    normalize_code,
)
from src.nadobro.utils.venue_scope import STRIP_LOWER_ELSE_MAINNET, coerce_nado_network

logger = logging.getLogger(__name__)

REFERRAL_LINK_PREFIX = "ref_"
MIN_CODE_LEN = 3
MAX_CODE_LEN = 20
_ALLOWED_CHARS = frozenset(string.ascii_uppercase + string.digits)
# Sentinel max_redemptions for vanity codes - the column has NOT NULL +
# CHECK (max_redemptions > 0); use the 32-bit INT max so the existing check
# still passes while the code is effectively reusable forever.
_UNLIMITED_REDEMPTIONS = 2_147_483_647
RESERVED_CODES = frozenset(
    {
        "ADMIN",
        "NADOBRO",
        "NADO",
        "SUPPORT",
        "STAFF",
        "TEAM",
        "MOD",
        "OFFICIAL",
        "HELP",
        "ROOT",
        "OWNER",
        "BOT",
        "SYSTEM",
        "NULL",
        "NONE",
        "TEST",
    }
)
VALID_REFERRAL_NETWORKS = frozenset({"mainnet", "testnet"})


def normalize_network(network: str | None) -> str:
    # Legacy ``str(network or "mainnet").strip().lower()`` kept if it is a valid
    # referral network, else mainnet; an Arcus scope token raises VenueScopeError.
    return coerce_nado_network(
        network, STRIP_LOWER_ELSE_MAINNET, site="referral_service.normalize_network"
    )


def normalize_referral_payload(payload: str) -> str:
    raw = str(payload or "").strip()
    if raw.lower().startswith(REFERRAL_LINK_PREFIX):
        raw = raw[len(REFERRAL_LINK_PREFIX):]
    return normalize_code(raw)


def referral_start_payload(code: str) -> str:
    return f"{REFERRAL_LINK_PREFIX}{normalize_referral_payload(code)}"


def bot_deep_link(code: str) -> str:
    bot_username = (BOT_USERNAME or "Nadbro_bot").lstrip("@")
    return f"https://t.me/{bot_username}?start={referral_start_payload(code)}"


def validate_custom_code(raw: str) -> tuple[str, Optional[str]]:
    """Return ``(normalized, error)``. ``error`` is ``None`` on success."""
    normalized = normalize_code(raw)
    if not normalized:
        return "", "Please enter a code with letters or numbers."
    if len(normalized) < MIN_CODE_LEN:
        return normalized, f"Code must be at least {MIN_CODE_LEN} characters."
    if len(normalized) > MAX_CODE_LEN:
        return normalized, f"Code must be at most {MAX_CODE_LEN} characters."
    if not set(normalized).issubset(_ALLOWED_CHARS):
        return normalized, "Code can only contain letters (A-Z) and numbers (0-9)."
    if normalized in RESERVED_CODES:
        return normalized, "That code is reserved. Please pick a different one."
    return normalized, None


def get_user_referral_code(telegram_id: int) -> Optional[dict]:
    """Return the user's single active referral code, network-agnostic."""
    row = query_one(
        """
        SELECT id, public_code, code_prefix, network, redemption_count,
               max_redemptions, created_at
        FROM invite_codes
        WHERE code_type = 'referral'
          AND referrer_user_id = %s
          AND active = true
          AND revoked_at IS NULL
        ORDER BY created_at ASC
        LIMIT 1
        """,
        (int(telegram_id),),
    )
    if not row:
        return None
    row["link"] = bot_deep_link(row["public_code"])
    return row


def claim_referral_code(
    telegram_id: int,
    requested_code: str,
    *,
    network: str = "mainnet",
) -> tuple[bool, str, Optional[dict]]:
    """Claim a custom referral code for ``telegram_id``.

    Idempotent: if the user already has a code, returns it unchanged
    (codes are immutable once claimed). Returns a clear conflict message
    when another user owns the requested string.
    """
    user_id = int(telegram_id)
    network = normalize_network(network)

    normalized, error = validate_custom_code(requested_code)
    if error:
        return False, error, None

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT telegram_id FROM users WHERE telegram_id = %s FOR UPDATE",
                (user_id,),
            )
            user_row = cur.fetchone()
            if not user_row:
                conn.rollback()
                return False, "User not found. Use /start first.", None

            cur.execute(
                """
                SELECT id, public_code, code_prefix, network, redemption_count,
                       max_redemptions, created_at
                FROM invite_codes
                WHERE code_type = 'referral'
                  AND referrer_user_id = %s
                  AND active = true
                  AND revoked_at IS NULL
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (user_id,),
            )
            existing = cur.fetchone()
            if existing:
                conn.commit()
                existing = dict(existing)
                existing["link"] = bot_deep_link(existing["public_code"])
                if existing["public_code"] == normalized:
                    return True, "You already own this code.", existing
                return (
                    False,
                    f"You already have a referral code: {existing['public_code']}. "
                    "Codes are permanent once claimed.",
                    existing,
                )

            cur.execute(
                """
                INSERT INTO invite_codes
                    (code_hash, public_code, code_type, code_prefix, created_by,
                     referrer_user_id, network, note, max_redemptions, sequence_number)
                VALUES (%s, %s, 'referral', %s, %s, %s, %s, %s, %s, 1)
                ON CONFLICT (code_hash) DO NOTHING
                RETURNING id, public_code, code_prefix, network, redemption_count,
                          max_redemptions, created_at
                """,
                (
                    _hash_code(normalized),
                    normalized,
                    normalized[:3],
                    user_id,
                    user_id,
                    network,
                    "custom_vanity_code",
                    _UNLIMITED_REDEMPTIONS,
                ),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return (
                    False,
                    "This code is already taken. Please pick a different one.",
                    None,
                )
        conn.commit()
        row = dict(row)
        row["link"] = bot_deep_link(row["public_code"])
        return True, "Referral code claimed.", row
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db(conn)


def auto_generate_referral_code(
    telegram_id: int,
    *,
    network: str = "mainnet",
    max_attempts: int = 8,
) -> tuple[bool, str, Optional[dict]]:
    """Mint a random 8-char referral code; retries on collision."""
    existing = get_user_referral_code(int(telegram_id))
    if existing:
        return (
            False,
            f"You already have a referral code: {existing['public_code']}. "
            "Codes are permanent once claimed.",
            existing,
        )

    last_msg = "Could not generate a unique code. Try again."
    for _ in range(max(1, int(max_attempts))):
        candidate = _generate_plain_code()
        ok, msg, row = claim_referral_code(
            int(telegram_id), candidate, network=network
        )
        if ok:
            return True, "Referral code generated.", row
        last_msg = msg
        # If the user already owns one (race), surface that.
        if row is not None and not ok:
            return False, msg, row
    return False, last_msg, None


def redeem_referral_code(
    telegram_id: int,
    username: Optional[str],
    code: str,
) -> tuple[bool, str]:
    """Link ``telegram_id`` as a referred user of the owner of ``code``.

    No-op for self-referral or when the referred account is already linked
    to a different referrer. Increments the code's ``redemption_count``.
    Does NOT touch ``private_access_granted`` (the bot is open).
    """
    referred = int(telegram_id)
    normalized = normalize_referral_payload(code)
    if not normalized or len(normalized) < MIN_CODE_LEN:
        return False, "Invalid referral code."

    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, referrer_user_id, code_type, active, revoked_at,
                       public_code
                FROM invite_codes
                WHERE code_hash = %s
                FOR UPDATE
                """,
                (_hash_code(normalized),),
            )
            invite = cur.fetchone()
            if not invite:
                conn.rollback()
                return False, "Referral code not found."
            invite = dict(invite)
            if invite.get("active") is False or invite.get("revoked_at"):
                conn.rollback()
                return False, "This referral code is no longer active."
            if str(invite.get("code_type") or "") != "referral":
                conn.rollback()
                return False, "This code is not a referral code."

            referrer = int(invite.get("referrer_user_id") or 0)
            if referrer <= 0:
                conn.rollback()
                return False, "This referral code is not linked to a user."
            if referrer == referred:
                conn.rollback()
                return False, "You cannot use your own referral code."

            cur.execute(
                "SELECT referrer_user_id FROM referrals WHERE referred_user_id = %s",
                (referred,),
            )
            existing_ref = cur.fetchone()
            if existing_ref:
                existing_ref = dict(existing_ref)
                if int(existing_ref.get("referrer_user_id") or 0) == referrer:
                    conn.commit()
                    return True, "Referral already linked."
                conn.rollback()
                return False, "This account is already linked to another referrer."

            cur.execute(
                """
                INSERT INTO referrals
                    (referrer_user_id, referred_user_id, invite_code_id, referred_username)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (referred_user_id) DO NOTHING
                RETURNING id
                """,
                (referrer, referred, invite["id"], username),
            )
            inserted = cur.fetchone()
            if not inserted:
                conn.rollback()
                return False, "This account is already linked to another referrer."

            cur.execute(
                """
                UPDATE invite_codes
                SET redemption_count = redemption_count + 1,
                    redeemed_by = COALESCE(redeemed_by, %s),
                    redeemed_username = COALESCE(redeemed_username, %s),
                    redeemed_at = COALESCE(redeemed_at, now()),
                    updated_at = now()
                WHERE id = %s
                """,
                (referred, username, invite["id"]),
            )
        conn.commit()
        return True, "Referral linked."
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db(conn)


def create_referral_from_invite(
    referrer_user_id: int,
    referred_user_id: int,
    invite_code_id: int,
    username: Optional[str],
) -> tuple[bool, str]:
    """Legacy helper kept for backward compatibility with older call sites."""
    referrer = int(referrer_user_id)
    referred = int(referred_user_id)
    if referrer == referred:
        return False, "You cannot use your own referral invite code."

    try:
        execute(
            """
            INSERT INTO referrals (referrer_user_id, referred_user_id, invite_code_id, referred_username)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (referred_user_id) DO NOTHING
            """,
            (referrer, referred, int(invite_code_id), username),
        )
    except Exception as exc:
        logger.warning("create_referral_from_invite failed: %s", exc)
        return False, "Could not link referral."

    existing = query_one(
        "SELECT referrer_user_id FROM referrals WHERE referred_user_id = %s",
        (referred,),
    )
    if existing and int(existing.get("referrer_user_id") or 0) == referrer:
        return True, "Referral linked."
    return False, "This account is already linked to a referrer."


def record_referred_volume(
    referred_user_id: int,
    volume_usd: float,
    *,
    network: str = "mainnet",
    source: str = "trade_stats",
    increment_trade_count: bool = True,
) -> None:
    network = normalize_network(network)
    volume = max(0.0, float(volume_usd or 0.0))
    if volume <= 0:
        return
    conn = get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM referrals WHERE referred_user_id = %s FOR UPDATE",
                (int(referred_user_id),),
            )
            referral = dict(cur.fetchone() or {})
            if not referral:
                conn.rollback()
                return

            cur.execute(
                """
                UPDATE referrals
                SET referred_volume_usd = referred_volume_usd + %s,
                    referred_trade_count = referred_trade_count + %s,
                    first_trade_at = COALESCE(first_trade_at, now()),
                    last_trade_at = now(),
                    updated_at = now()
                WHERE id = %s
                """,
                (volume, 1 if increment_trade_count else 0, referral["id"]),
            )
            cur.execute(
                """
                INSERT INTO referral_volume_events
                    (referral_id, referrer_user_id, referred_user_id, network, volume_usd, trade_count_delta, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    referral["id"],
                    referral["referrer_user_id"],
                    int(referred_user_id),
                    network,
                    volume,
                    1 if increment_trade_count else 0,
                    source,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db(conn)


def get_referral_dashboard(telegram_id: int, network: str = "mainnet") -> dict:
    network = normalize_network(network)
    user_id = int(telegram_id)

    code = get_user_referral_code(user_id)

    stats = query_one(
        """
        SELECT COUNT(DISTINCT referrals.id) AS total_referrals,
               COALESCE(SUM(e.volume_usd), 0) AS total_referred_volume,
               COALESCE(SUM(e.trade_count_delta), 0) AS total_referred_trades
        FROM referrals
        LEFT JOIN referral_volume_events e
          ON e.referral_id = referrals.id
         AND e.network = %s
        WHERE referrals.referrer_user_id = %s
        """,
        (network, user_id),
    ) or {}
    referred_users = query_all(
        """
        SELECT r.referred_user_id,
               COALESCE(u.telegram_username, r.referred_username, '') AS username,
               COALESCE(SUM(e.volume_usd), 0) AS referred_volume_usd,
               COALESCE(SUM(e.trade_count_delta), 0) AS referred_trade_count,
               MAX(e.created_at) AS last_trade_at,
               r.created_at
        FROM referrals r
        LEFT JOIN users u ON u.telegram_id = r.referred_user_id
        LEFT JOIN referral_volume_events e
          ON e.referral_id = r.id
         AND e.network = %s
        WHERE r.referrer_user_id = %s
        GROUP BY r.id, r.referred_user_id, u.telegram_username, r.referred_username, r.created_at
        ORDER BY COALESCE(SUM(e.volume_usd), 0) DESC, r.created_at DESC
        LIMIT 10
        """,
        (network, user_id),
    )
    return {
        "telegram_id": user_id,
        "network": network,
        "share_code": code,
        "has_code": bool(code),
        "total_referrals": int(stats.get("total_referrals") or 0),
        "total_referred_volume": float(stats.get("total_referred_volume") or 0.0),
        "total_referred_trades": int(stats.get("total_referred_trades") or 0),
        "referred_users": referred_users or [],
        "min_code_len": MIN_CODE_LEN,
        "max_code_len": MAX_CODE_LEN,
    }


__all__ = [
    "REFERRAL_LINK_PREFIX",
    "MIN_CODE_LEN",
    "MAX_CODE_LEN",
    "RESERVED_CODES",
    "VALID_REFERRAL_NETWORKS",
    "normalize_network",
    "normalize_referral_payload",
    "referral_start_payload",
    "bot_deep_link",
    "validate_custom_code",
    "get_user_referral_code",
    "claim_referral_code",
    "auto_generate_referral_code",
    "redeem_referral_code",
    "create_referral_from_invite",
    "record_referred_volume",
    "get_referral_dashboard",
    "INVITE_CODE_LENGTH",
]
