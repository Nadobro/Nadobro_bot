import json
import logging
import time
import uuid

from src.nadobro.db import query_all
from src.nadobro.models.database import set_bot_state

logger = logging.getLogger(__name__)


def _loc(text: str, lang: str = "en") -> str:
    from src.nadobro.i18n import localize_text
    return localize_text(text, lang)

_SL_KEY_PREFIX = "stop_loss:"
_ERROR_NOTIFY_COOLDOWN_SECONDS = 300


def _now_iso() -> str:
    # AUDIT-FIX-DT-1: utcnow() is deprecated in Py 3.12+ and returns a naive
    # datetime that compares poorly with aware ones. Use aware UTC.
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _rule_key(telegram_id: int, network: str, product: str, rule_id: str) -> str:
    return f"{_SL_KEY_PREFIX}{telegram_id}:{network}:{product}:{rule_id}"


def _should_trigger_stop_loss(side: str, mark_price: float, target_price: float) -> bool:
    side_upper = (side or "").upper()
    if side_upper == "LONG":
        return mark_price <= target_price
    if side_upper == "SHORT":
        return mark_price >= target_price
    return False


def register_stop_loss_rule(
    telegram_id: int,
    network: str,
    product: str,
    side: str,
    stop_price: float,
    size: float,
) -> dict:
    try:
        stop_price_f = float(stop_price)
    except (TypeError, ValueError):
        return {"success": False, "error": "Invalid SL price."}
    if stop_price_f <= 0:
        return {"success": False, "error": "SL price must be greater than 0."}

    rule_id = uuid.uuid4().hex[:10]
    payload = {
        "active": True,
        "rule_id": rule_id,
        "user_id": int(telegram_id),
        "network": str(network or "mainnet"),
        "product": str(product).upper(),
        "side": str(side).upper(),
        "size": float(size or 0),
        "stop_price": stop_price_f,
        "created_at": _now_iso(),
        "triggered_at": None,
        "last_error": None,
        "last_error_notified_at": 0.0,
    }
    key = _rule_key(payload["user_id"], payload["network"], payload["product"], rule_id)
    set_bot_state(key, payload)
    return {"success": True, "rule_id": rule_id, "stop_price": stop_price_f}


def process_stop_losses(prices: dict) -> list[dict]:
    rows = query_all("SELECT key, value FROM bot_state WHERE key LIKE %s", (f"{_SL_KEY_PREFIX}%",))
    notifications = []
    now_ts = time.time()

    for row in rows:
        key = row.get("key")
        raw = row.get("value")
        if not key or not raw:
            continue
        try:
            rule = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            logger.warning("Invalid stop-loss rule payload for key=%s", key)
            continue
        if not isinstance(rule, dict) or not rule.get("active"):
            continue

        product = str(rule.get("product") or "").upper().replace("-PERP", "")
        side = str(rule.get("side") or "").upper()
        user_id = int(rule.get("user_id") or 0)
        network = str(rule.get("network") or "mainnet")
        target = float(rule.get("stop_price") or 0)
        if not product or not side or not user_id or target <= 0:
            continue

        mark = float(((prices.get(product) or {}).get("mid", 0)) or 0)
        if mark <= 0:
            continue
        if not _should_trigger_stop_loss(side, mark, target):
            continue

        from src.nadobro.i18n import get_user_language
        user_lang = get_user_language(user_id)

        from src.nadobro.trading.trade_service import close_position

        try:
            close_result = close_position(user_id, product, network=network)
        except Exception as exc:  # an unreadable position book (DENIED-vs-EMPTY) is a failed close, retried next scan
            close_result = {"success": False, "error": str(exc)}
        if close_result.get("success"):
            rule["active"] = False
            rule["triggered_at"] = _now_iso()
            rule["last_error"] = None
            set_bot_state(key, rule)
            notifications.append(
                {
                    "user_id": user_id,
                    "text": (
                        f"{_loc('🛑 Stop-loss executed for', user_lang)} {product}-PERP.\n"
                        f"{_loc('Trigger', user_lang)}: ${target:,.2f} | {_loc('Mark', user_lang)}: ${mark:,.2f}"
                    ),
                }
            )
        else:
            err = str(close_result.get("error", "unknown error"))
            if "No open positions" in err:
                rule["active"] = False
                rule["triggered_at"] = _now_iso()
                rule["last_error"] = None
                set_bot_state(key, rule)
                continue
            last_notified = float(rule.get("last_error_notified_at") or 0)
            rule["last_error"] = err[:250]
            if now_ts - last_notified >= _ERROR_NOTIFY_COOLDOWN_SECONDS:
                rule["last_error_notified_at"] = now_ts
                set_bot_state(key, rule)
                notifications.append(
                    {
                        "user_id": user_id,
                        "text": (
                            f"{_loc('⚠️ Stop-loss trigger failed for', user_lang)} {product}-PERP "
                            f"${mark:,.2f}.\n"
                            f"{_loc('Reason', user_lang)}: {err}"
                        ),
                    }
                )
            else:
                set_bot_state(key, rule)

    return notifications
