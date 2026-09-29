"""Nado NLP Vault — deposit (mint) and withdraw (burn) orchestration."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from src.nadobro.utils.env import env_float, env_int
from src.nadobro.utils.venue_scope import EXACT_ELSE_TESTNET, coerce_nado_network
from src.nadobro.models.database import (
    get_bot_state,
    get_vault_deposit_watch,
    get_vault_lp_events,
    insert_vault_lp_event,
    set_bot_state,
)
from src.nadobro.venue.nado_archive import query_nlp_lp_events
from src.nadobro.users.user_service import get_user, get_user_nado_client
from src.nadobro.vault.vault_metrics_service import (
    build_lp_ledger_from_archive,
    compute_pnl_from_ledger,
    deposit_room_usdt0,
    get_pool_metrics,
)

logger = logging.getLogger(__name__)

PRIVATE_ALPHA_CAP_USDT0 = 20_000.0
# Ceiling on how much of the user's USDT0 balance a vault deposit may consume
# (margin buffer for their trading account). Product default: 70%.
VAULT_DEPOSIT_MAX_PCT = min(100.0, max(1.0, env_float("NADO_VAULT_DEPOSIT_MAX_PCT", 70.0)))
LOCKUP_SECONDS = 4 * 24 * 60 * 60
SEQUENCER_FEE_USDT0 = 1.0
WITHDRAW_FEE_BPS = 10
WITHDRAW_FEE_FLOOR_USDT0 = 1.0
USDT0_PRODUCT_ID = 0

# Re-sync archive mint/burn events at most every N seconds per user.
_LP_EVENTS_SYNC_TTL_SECONDS = max(60, env_int("NADO_VAULT_LP_EVENTS_SYNC_TTL", 300))


def lockup_remaining_seconds(last_mint_ts_ms: Optional[int]) -> int:
    if not last_mint_ts_ms:
        return 0
    try:
        elapsed = time.time() - (int(last_mint_ts_ms) / 1000.0)
    except (TypeError, ValueError):
        return 0
    remaining = LOCKUP_SECONDS - int(elapsed)
    return max(0, remaining)


def _next_unlock_hint(locked_info: dict) -> str:
    """" — unlocks in ~Xh" for the soonest still-locked tranche, else ""."""
    now = time.time()
    future = [
        int(e.get("unlocked_at") or 0)
        for e in (locked_info.get("locked_entries") or [])
        if int(e.get("unlocked_at") or 0) > now
    ]
    if not future:
        return ""
    hours = (min(future) - now) / 3600.0
    if hours < 1:
        return f" — next tranche unlocks in ~{max(1, int(hours * 60))}m"
    return f" — next tranche unlocks in ~{hours:.1f}h"


def _is_insufficient_unlocked_error(error: object) -> bool:
    """True for the venue's 'not enough unlocked NLP' rejection (code 2096)."""
    text = str(error or "").lower()
    return "2096" in text or "unlocked nlp" in text


def estimate_withdraw_fee_usdt0(usdt0_out: float) -> float:
    if usdt0_out <= 0:
        return SEQUENCER_FEE_USDT0 + WITHDRAW_FEE_FLOOR_USDT0
    bps_fee = max(WITHDRAW_FEE_FLOOR_USDT0, usdt0_out * (WITHDRAW_FEE_BPS / 10_000.0))
    return SEQUENCER_FEE_USDT0 + bps_fee


def _user_network(user) -> str:
    return str(getattr(getattr(user, "network_mode", None), "value", None) or "mainnet")


def _ledger_rows_for_pnl(telegram_id: int, network: str) -> list[dict]:
    """Archive-sourced rows only (rows with submission_idx) — bot-side rows
    are stored for audit but excluded so PnL never double-counts after the
    archive backfill catches the same tx."""
    rows = get_vault_lp_events(telegram_id, network=network)
    ledger = []
    for row in rows:
        if not row.get("submission_idx"):
            continue
        ledger.append({
            "event_type": row.get("event_type"),
            "quote_usdt0": float(row.get("quote_usdt0") or 0.0),
            "nlp_amount": float(row.get("nlp_amount") or 0.0),
            "timestamp": int(row["event_ts"].timestamp()) if row.get("event_ts") else 0,
            "submission_idx": row.get("submission_idx"),
        })
    return ledger


def _lp_events_sync_marker_key(telegram_id: int, network: str) -> str:
    return f"vault_lp_events_sync:{network}:{telegram_id}"


def _sync_lp_events_with_archive(
    telegram_id: int,
    network: str,
    subaccount_hex: str,
    nlp_product_id: int,
    *,
    force: bool = False,
) -> None:
    """Pull archive mint/burn events into vault_lp_events_{network}, deduped
    via the unique constraint. TTL-gated to avoid hammering the archive."""
    marker_key = _lp_events_sync_marker_key(telegram_id, network)
    if not force:
        marker = get_bot_state(marker_key) or {}
        last_ts = float(marker.get("ts") or 0)
        if (time.time() - last_ts) < _LP_EVENTS_SYNC_TTL_SECONDS:
            return

    try:
        payload = query_nlp_lp_events(network, subaccount_hex, limit=500)
    except Exception as e:
        logger.debug("vault lp events archive query failed user=%s err=%s", telegram_id, e)
        return

    ledger = build_lp_ledger_from_archive(payload, nlp_product_id)
    for row in ledger:
        ts = row.get("timestamp")
        event_ts = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
        insert_vault_lp_event(
            telegram_id,
            event_type=str(row.get("event_type") or "mint"),
            quote_usdt0=float(row.get("quote_usdt0") or 0.0),
            nlp_amount=float(row.get("nlp_amount") or 0.0),
            submission_idx=row.get("submission_idx"),
            event_ts=event_ts,
            network=network,
        )
    try:
        set_bot_state(marker_key, {"ts": time.time(), "count": len(ledger)})
    except Exception as e:
        logger.debug("vault lp events sync marker write failed user=%s err=%s", telegram_id, e)


def get_user_vault_snapshot(telegram_id: int) -> dict:
    """Return a UI-ready snapshot with Nado-parity vault metrics."""
    snapshot: dict = {
        "ready": False,
        "usdt0_balance": 0.0,
        "lp_balance": 0.0,
        "lp_value_usdt0": 0.0,
        "position_usdt0": 0.0,
        "all_time_earned_usdt0": 0.0,
        "unrealized_pnl_usdt0": 0.0,
        "max_mintable_usdt0": 0.0,
        "deposit_room_usdt0": 0.0,
        "deposit_max_usdt0": 0.0,
        "mintable_known": False,
        "nlp_nav_usdt0": 0.0,
        "lockup_seconds_remaining": 0,
        "last_mint_ts_ms": None,
        # Burnable truth (see nado_client.get_nlp_locked_balances). When
        # ``unlocked_known`` is False the locked-balance query did not answer,
        # so lp_balance must NOT be presented as withdrawable.
        "unlocked_known": False,
        "lp_unlocked": 0.0,
        "lp_locked": 0.0,
        "pool": {"tvl_usdt0": 0.0, "apr_pct": None, "apr_source": "unavailable"},
        "private_alpha_cap_usdt0": PRIVATE_ALPHA_CAP_USDT0,
        "deposit_watch_enabled": False,
        "deposit_capacity_open": False,
        "error": None,
    }
    user = get_user(telegram_id)
    if not user:
        snapshot["error"] = "Wallet not initialized. Use /start first."
        return snapshot
    network = _user_network(user)
    client = get_user_nado_client(telegram_id)
    if not client:
        snapshot["error"] = "Wallet not linked. Open 💼 Wallet Vault from the home menu to link a signer."
        return snapshot
    if not client._initialized:
        client.initialize()
    if not client._initialized:
        snapshot["error"] = "Nado SDK unavailable. Try again shortly."
        return snapshot

    try:
        balance = client.get_balance() or {}
        balances = balance.get("balances") or {}
        raw_usdt0 = balances.get(USDT0_PRODUCT_ID, balances.get(str(USDT0_PRODUCT_ID), 0)) or 0
        snapshot["usdt0_balance"] = float(raw_usdt0)
    except Exception as e:
        logger.debug("vault snapshot: usdt0 balance failed user=%s err=%s", telegram_id, e)

    # Query mintable BEFORE the display-only NLP reads. Every NLP query costs
    # gateway weight 20 against the per-user budget (8 rps, burst 24): when
    # this load-bearing query ran after nlp_locked_balances it landed on a
    # drained bucket, throttled, and the old code booked the failure as
    # "mintable = $0" → a depositable user saw 🔒 Margin in use (2026-07-18).
    # Legacy ``network == "mainnet"`` → pid 11, else 1 (kept exactly); an
    # Arcus scope raises VenueScopeError.
    nlp_default_pid = {"testnet": 1, "mainnet": 11}[
        coerce_nado_network(network, EXACT_ELSE_TESTNET, site="nlp_vault_service.get_user_vault_snapshot")
    ]
    nlp_product_id = int(client.resolve_nlp_product_id() or nlp_default_pid)
    mintable = client.get_max_nlp_mintable(spot_leverage=False, product_id=nlp_product_id) or {}
    mintable_ok = bool(mintable.get("ok"))
    max_mintable = float(mintable.get("max_mintable_usdt0") or 0.0) if mintable_ok else 0.0
    snapshot["mintable_known"] = mintable_ok

    pos = client.get_nlp_position() or {}
    snapshot["lp_balance"] = float(pos.get("lp_balance") or 0.0)
    snapshot["nlp_nav_usdt0"] = float(pos.get("nav_usdt0") or 0.0)
    snapshot["lp_value_usdt0"] = float(pos.get("lp_value_usdt0") or 0.0)
    snapshot["position_usdt0"] = snapshot["lp_value_usdt0"]
    snapshot["last_mint_ts_ms"] = pos.get("last_mint_ts_ms")
    snapshot["lockup_seconds_remaining"] = lockup_remaining_seconds(snapshot["last_mint_ts_ms"])
    snapshot["unlocked_known"] = bool(pos.get("unlocked_known"))
    snapshot["lp_unlocked"] = float(pos.get("lp_unlocked") or 0.0)
    snapshot["lp_locked"] = float(pos.get("lp_locked") or 0.0)

    snapshot["max_mintable_usdt0"] = max_mintable
    snapshot["deposit_room_usdt0"] = deposit_room_usdt0(
        snapshot["lp_value_usdt0"], max_mintable, PRIVATE_ALPHA_CAP_USDT0,
    )
    snapshot["deposit_capacity_open"] = mintable_ok and max_mintable >= 100.0
    # Deposit ceiling: never commit more than NADO_VAULT_DEPOSIT_MAX_PCT
    # (default 70%) of the user's USDT0 into the vault, so trading margin
    # keeps a buffer. The venue's own no-borrow check remains the final
    # arbiter at mint time.
    balance_ceiling = snapshot["usdt0_balance"] * (VAULT_DEPOSIT_MAX_PCT / 100.0)
    if mintable_ok:
        snapshot["deposit_max_usdt0"] = max(
            0.0, min(max_mintable, balance_ceiling, snapshot["deposit_room_usdt0"])
        )
    else:
        # Capacity unknown (throttle/transport): offer the balance-derived
        # ceiling and let the venue's spot_leverage=false mint self-guard —
        # per the docs it "fails if the transaction causes a borrow".
        snapshot["deposit_max_usdt0"] = max(0.0, balance_ceiling)

    # Classification. Semantics verified against the live gateway
    # (2026-07-18): max_nlp_mintable ≈ min(per-wallet vault cap remainder,
    # spendable USDT0), where spot_leverage only affects the spendable term.
    # A SUCCESSFUL 0 therefore means cap-consumed, vault closed, or margin —
    # and a FAILED query means nothing at all.
    snapshot["deposit_blocked_reason"] = None
    snapshot["mintable_with_borrow_usdt0"] = 0.0
    if mintable_ok and max_mintable <= 1.0:
        if snapshot["lp_value_usdt0"] >= PRIVATE_ALPHA_CAP_USDT0 - 1.0:
            snapshot["deposit_blocked_reason"] = "wallet_cap_reached"
        else:
            try:
                lev = client.get_max_nlp_mintable(
                    spot_leverage=True, product_id=nlp_product_id
                ) or {}
                lev_ok = bool(lev.get("ok"))
                lev_mintable = float(lev.get("max_mintable_usdt0") or 0.0) if lev_ok else 0.0
            except Exception:  # policy: degrade-ok(diagnostic only; gate stays strict)
                lev_ok, lev_mintable = False, 0.0
            snapshot["mintable_with_borrow_usdt0"] = lev_mintable
            if lev_ok and lev_mintable > 1.0:
                snapshot["deposit_blocked_reason"] = "margin_locked"
            elif lev_ok:
                snapshot["deposit_blocked_reason"] = "vault_full"
            # lev not ok → leave None: we know nothing, so we blame nothing.

    pool = get_pool_metrics(network, client=client)
    snapshot["pool"] = pool

    try:
        _sync_lp_events_with_archive(
            telegram_id, network, client.subaccount_hex, nlp_product_id,
        )
        ledger = _ledger_rows_for_pnl(telegram_id, network)
        pnl = compute_pnl_from_ledger(ledger, snapshot["lp_value_usdt0"])
        snapshot["all_time_earned_usdt0"] = float(pnl.get("all_time_earned_usdt0") or 0.0)
        snapshot["unrealized_pnl_usdt0"] = float(pnl.get("unrealized_pnl_usdt0") or 0.0)
    except Exception as e:
        logger.debug("vault pnl metrics failed user=%s err=%s", telegram_id, e)

    watch = get_vault_deposit_watch(telegram_id, network=network)
    snapshot["deposit_watch_enabled"] = bool(watch and watch.get("enabled"))
    snapshot["ready"] = True
    return snapshot


def _log_vault_event(
    telegram_id: int,
    network: str,
    *,
    event_type: str,
    quote_usdt0: float | None = None,
    nlp_amount: float | None = None,
    digest: str | None = None,
) -> None:
    """Audit log for bot-initiated mint/burns. Stored with NULL submission_idx
    so they are excluded from PnL accounting; archive sync supplies the
    canonical PnL rows once the indexer catches up."""
    insert_vault_lp_event(
        telegram_id,
        event_type=event_type,
        quote_usdt0=quote_usdt0,
        nlp_amount=nlp_amount,
        tx_digest=str(digest) if digest else None,
        event_ts=datetime.now(timezone.utc),
        network=network,
    )
    # Invalidate the archive sync marker so the next vault refresh refetches
    # quickly and (eventually) replaces this row with the indexed event.
    try:
        set_bot_state(_lp_events_sync_marker_key(telegram_id, network), {"ts": 0})
    except Exception:
        pass


def deposit_to_vault(telegram_id: int, usdt0_amount: float) -> dict:
    usdt0_amount = float(usdt0_amount or 0)
    if usdt0_amount <= 0:
        return {"success": False, "error": "Enter a positive USDT0 amount."}
    user = get_user(telegram_id)
    network = _user_network(user) if user else "mainnet"
    client = get_user_nado_client(telegram_id)
    if not client:
        return {"success": False, "error": "Wallet not linked. Open 💼 Wallet Vault from the home menu first."}
    if not client._initialized:
        client.initialize()
    if not client._initialized:
        return {"success": False, "error": "Nado SDK unavailable. Try again shortly."}

    snap = get_user_vault_snapshot(telegram_id)
    balance = float(snap.get("usdt0_balance", 0.0) or 0.0)
    if balance + 1e-9 < usdt0_amount:
        return {
            "success": False,
            "error": (
                f"Insufficient USDT0 balance "
                f"(have ${balance:,.2f}). "
                "Deposit USDT0 into your Nado account first."
            ),
        }
    balance_ceiling = balance * (VAULT_DEPOSIT_MAX_PCT / 100.0)
    if usdt0_amount > balance_ceiling + 1e-9:
        return {
            "success": False,
            "error": (
                f"Vault deposits are capped at {VAULT_DEPOSIT_MAX_PCT:.0f}% of your "
                f"USDT0 balance (${balance_ceiling:,.2f} right now) so your trading "
                "account keeps a margin buffer."
            ),
        }
    # Only enforce the venue-derived room when the capacity query actually
    # succeeded — a throttled query must not block a legitimate deposit. The
    # mint itself runs with spot_leverage=false, which the gateway rejects if
    # it would cause a borrow, so the venue stays the final arbiter either way.
    if snap.get("mintable_known") and usdt0_amount > float(snap.get("deposit_room_usdt0") or 0.0) + 1e-9:
        room = float(snap.get("deposit_room_usdt0") or 0.0)
        return {
            "success": False,
            "error": (
                f"Deposit room is ${room:,.2f} USDT0 "
                f"(Private Alpha cap ${PRIVATE_ALPHA_CAP_USDT0:,.0f} and/or vault capacity)."
            ),
        }
    projected = float(snap.get("lp_value_usdt0", 0.0)) + usdt0_amount
    if projected > PRIVATE_ALPHA_CAP_USDT0 + 1e-9:
        room = max(0.0, PRIVATE_ALPHA_CAP_USDT0 - float(snap.get("lp_value_usdt0", 0.0)))
        return {
            "success": False,
            "error": (
                f"Private Alpha cap is ${PRIVATE_ALPHA_CAP_USDT0:,.0f} USDT0 per account. "
                f"You can add at most ${room:,.2f} more."
            ),
        }
    result = client.mint_nlp(usdt0_amount, spot_leverage=False)
    if result.get("success"):
        _log_vault_event(
            telegram_id, network,
            event_type="mint",
            quote_usdt0=usdt0_amount,
            digest=result.get("digest"),
        )
        logger.info(
            "NLP deposit user=%s amount=%.2f USDT0 digest=%s",
            telegram_id, usdt0_amount, result.get("digest"),
        )
    return result


def withdraw_from_vault(telegram_id: int, nlp_amount: float) -> dict:
    nlp_amount = float(nlp_amount or 0)
    if nlp_amount <= 0:
        return {"success": False, "error": "Enter a positive NLP amount."}
    user = get_user(telegram_id)
    network = _user_network(user) if user else "mainnet"
    client = get_user_nado_client(telegram_id)
    if not client:
        return {"success": False, "error": "Wallet not linked. Open 💼 Wallet Vault from the home menu first."}
    if not client._initialized:
        client.initialize()
    if not client._initialized:
        return {"success": False, "error": "Nado SDK unavailable. Try again shortly."}
    snap = get_user_vault_snapshot(telegram_id)
    if nlp_amount > float(snap.get("lp_balance", 0.0)) + 1e-9:
        return {
            "success": False,
            "error": (
                f"You only have {snap.get('lp_balance', 0.0):.6f} NLP. "
                "Reduce the amount or pick a percentage preset."
            ),
        }
    lockup = int(snap.get("lockup_seconds_remaining") or 0)
    if lockup > 0:
        hours = lockup / 3600.0
        return {
            "success": False,
            "error": (
                f"Lockup active — burns are blocked for ~{hours:.1f}h more "
                "(4-day post-mint lock from docs.nado.xyz/nlp)."
            ),
        }
    # Burnable amount = the venue's UNLOCKED balance, never the total. Re-read it
    # here (fresh, not from the snapshot) because this weight-20 query is the one
    # that loses the gateway-budget race during a snapshot burst — a throttled
    # read previously degraded to "0 locked / 0 unlocked", the card showed the
    # whole spot balance as "Unlocked", and the burn came back 2096.
    locked_info = client.get_nlp_locked_balances() or {}
    unlocked_known = bool(locked_info.get("ok"))
    unlocked = float(locked_info.get("balance_unlocked") or 0.0)
    unlocked_x18 = int(locked_info.get("balance_unlocked_x18") or 0)
    amount_x18: Optional[int] = None
    if unlocked_known:
        if unlocked_x18 <= 0:
            still_locked = float(locked_info.get("balance_locked") or 0.0)
            when = _next_unlock_hint(locked_info)
            return {
                "success": False,
                "error": (
                    f"None of your {still_locked:.6f} NLP is unlocked yet{when}. "
                    "NLP unlocks 4 days after each mint (docs.nado.xyz/nlp)."
                ),
            }
        if nlp_amount > unlocked + 1e-12:
            when = _next_unlock_hint(locked_info)
            return {
                "success": False,
                "error": (
                    f"Only {unlocked:.6f} of your {float(snap.get('lp_balance') or 0.0):.6f} NLP "
                    f"is unlocked right now{when}. Withdraw up to {unlocked:.6f} NLP, "
                    "or wait for the rest to unlock."
                ),
            }
        # Burning (effectively) everything unlocked: send the venue's own exact
        # integer. The float round-trip can land a few wei high -> 2096.
        if nlp_amount >= unlocked - 1e-12:
            amount_x18 = unlocked_x18
    lp_value = float(snap.get("lp_value_usdt0") or 0.0)
    lp_balance = float(snap.get("lp_balance") or 0.0)
    est_usdt0 = lp_value * (nlp_amount / lp_balance) if lp_balance > 0 else 0.0
    result = client.burn_nlp(nlp_amount, amount_x18=amount_x18)
    if not result.get("success") and _is_insufficient_unlocked_error(result.get("error")):
        result = {
            "success": False,
            "error": (
                "Nado says that amount is not unlocked yet. NLP unlocks 4 days "
                "after each mint — tap Refresh and try a smaller amount."
            ),
        }
    if result.get("success"):
        _log_vault_event(
            telegram_id, network,
            event_type="burn",
            nlp_amount=nlp_amount,
            quote_usdt0=est_usdt0,
            digest=result.get("digest"),
        )
        logger.info(
            "NLP withdraw user=%s amount=%.6f NLP digest=%s",
            telegram_id, nlp_amount, result.get("digest"),
        )
    return result
