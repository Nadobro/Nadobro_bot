import logging
import json
import time
from datetime import datetime, timedelta, timezone

from src.nadobro.models.database import get_bot_state_raw, set_bot_state
from src.nadobro.users.settings_service import get_strategy_settings, update_user_settings
from src.nadobro.llm.bro_llm import analyze_for_howl
from src.nadobro.db import query_all
from src.nadobro.utils.venue_scope import EXACT_ELSE_MAINNET, coerce_nado_network

logger = logging.getLogger(__name__)

HOWL_STATE_PREFIX = "howl:"
HOWL_PENDING_PREFIX = "howl_pending:"

# SECURITY (audit 2026-05): HOWL parameter changes are produced by an LLM whose
# input includes attacker-influenceable context (recent trade history, market
# sentiment scraped from social feeds). The LLM's structured output therefore
# must be treated as UNTRUSTED. Before this allowlist existed, ``approve_howl_
# suggestion`` wrote ``bro[<arbitrary param>] = <arbitrary value>`` directly,
# letting a hallucinated or prompt-injected suggestion crank ``budget_usd`` to
# millions, ``leverage_cap`` past venue limits, flip ``risk_level`` to
# aggressive, or zero out ``min_confidence`` — silently dismantling the Alpha
# Agent's risk rails behind a single (persuasively worded) "approve" tap.
#
# Only the keys below are tunable via HOWL, and each value is clamped to a safe
# band. Unknown keys are rejected outright. These bounds mirror the manual
# config caps enforced in handlers/callbacks.py so the auto-tuner can never push
# settings somewhere a human couldn't.
_HOWL_RISK_LEVELS = ("conservative", "balanced", "aggressive")
# (param, kind, lo, hi) — kind is "int", "float", "enum", or "bool".
_HOWL_NUMERIC_BOUNDS: dict[str, tuple[str, float, float]] = {
    "budget_usd": ("float", 10.0, 1_000_000.0),
    "max_positions": ("int", 1, 10),
    "cycle_seconds": ("int", 60, 3600),
    "tp_pct": ("float", 0.3, 20.0),
    "sl_pct": ("float", 0.3, 20.0),
    "max_loss_pct": ("float", 1.0, 50.0),
    "max_daily_loss_usd": ("float", 0.0, 1_000_000.0),
    "leverage_cap": ("int", 1, 20),
    "min_reward_risk": ("float", 0.5, 5.0),
    "min_confidence": ("float", 0.3, 0.99),
    "howl_hour_utc": ("int", 0, 23),
}
_HOWL_BOOL_PARAMS = ("use_sentiment", "use_cmc", "howl_enabled")


def _coerce_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on", "enabled"):
            return True
        if v in ("false", "0", "no", "off", "disabled"):
            return False
    return None


def validate_howl_param(param: str, value):
    """Validate an LLM-proposed bro-setting change.

    Returns ``(ok, coerced_value, error)``. ``ok`` is False (with a human
    message) for unknown params, wrong types, or out-of-band values. This is the
    single chokepoint that keeps untrusted HOWL output from weakening risk
    limits.
    """
    param = str(param or "").strip()
    if param in _HOWL_NUMERIC_BOUNDS:
        kind, lo, hi = _HOWL_NUMERIC_BOUNDS[param]
        try:
            num = float(value)
        except (TypeError, ValueError):
            return False, None, f"{param} must be numeric"
        if num != num or num in (float("inf"), float("-inf")):  # NaN / inf guard
            return False, None, f"{param} is not a finite number"
        if num < lo or num > hi:
            return False, None, f"{param} must be between {lo} and {hi}"
        return True, (int(num) if kind == "int" else num), None
    if param == "risk_level":
        v = str(value or "").strip().lower()
        if v not in _HOWL_RISK_LEVELS:
            return False, None, f"risk_level must be one of {', '.join(_HOWL_RISK_LEVELS)}"
        return True, v, None
    if param in _HOWL_BOOL_PARAMS:
        b = _coerce_bool(value)
        if b is None:
            return False, None, f"{param} must be true/false"
        return True, b, None
    if param == "products":
        if not isinstance(value, (list, tuple)) or not value:
            return False, None, "products must be a non-empty list"
        try:
            from src.nadobro.config import get_perp_products

            known = {p.upper() for p in (get_perp_products() or [])}
        except Exception:
            known = set()
        cleaned = []
        for item in value:
            sym = str(item or "").upper().replace("-PERP", "").strip()
            if not sym:
                continue
            if known and sym not in known:
                return False, None, f"unknown product '{sym}'"
            cleaned.append(sym)
        if not cleaned:
            return False, None, "no valid products in suggestion"
        return True, cleaned[:10], None
    return False, None, f"'{param}' is not a tunable parameter"


def _howl_key(telegram_id: int, network: str) -> str:
    return f"{HOWL_STATE_PREFIX}{telegram_id}:{network}"


def _pending_key(telegram_id: int, network: str) -> str:
    return f"{HOWL_PENDING_PREFIX}{telegram_id}:{network}"


def get_recent_bro_trades(telegram_id: int, hours: int = 24, network: str = "mainnet") -> list[dict]:
    # Legacy: exact "testnet"/"mainnet" keep their table, anything else reads
    # trades_mainnet (kept exactly); an Arcus scope raises VenueScopeError.
    table = "trades_" + coerce_nado_network(
        network, EXACT_ELSE_MAINNET, site="howl_service.get_recent_bro_trades"
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = query_all(
        f"SELECT product_name, side, pnl, size, price, created_at, status "
        f"FROM {table} WHERE user_id = %s AND created_at > %s "
        f"ORDER BY created_at DESC LIMIT 50",
        (telegram_id, cutoff),
    )
    return rows or []


def compute_performance_metrics(trades: list[dict], bro_state: dict) -> dict:
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0,
            "avg_pnl": 0,
            "total_pnl": bro_state.get("total_pnl", 0),
            "max_win": 0,
            "max_loss": 0,
            "consecutive_holds": bro_state.get("consecutive_holds", 0),
        }

    pnls = [float(t.get("pnl", 0) or 0) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total = wins + losses

    return {
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total * 100) if total > 0 else 0,
        "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
        "total_pnl": sum(pnls),
        "cumulative_pnl": bro_state.get("total_pnl", 0),
        "max_win": max(pnls) if pnls else 0,
        "max_loss": min(pnls) if pnls else 0,
        "consecutive_holds": bro_state.get("consecutive_holds", 0),
        "total_decisions": len(bro_state.get("decisions_log", [])),
    }


def run_howl_analysis(telegram_id: int, network: str, bot_state: dict) -> dict | None:
    _, bro_settings = get_strategy_settings(telegram_id, "bro")
    bro_state = bot_state.get("bro_state", {})

    trades = get_recent_bro_trades(telegram_id, network=network)
    metrics = compute_performance_metrics(trades, bro_state)

    result = analyze_for_howl(
        trade_history=trades,
        current_settings=bro_settings,
        performance_metrics=metrics,
    )

    if not result:
        return None

    howl_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "suggestions": result.get("suggestions", []),
        "overall_assessment": result.get("overall_assessment", ""),
        "confidence": result.get("confidence", 0),
        "status": "pending",
    }

    set_bot_state(_pending_key(telegram_id, network), howl_record)

    last_runs = []
    raw = get_bot_state_raw(_howl_key(telegram_id, network))
    if raw:
        try:
            last_runs = json.loads(raw)
            if not isinstance(last_runs, list):
                last_runs = [last_runs]
        except Exception:
            last_runs = []
    last_runs.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "trades_analyzed": len(trades),
        "suggestions_count": len(result.get("suggestions", [])),
    })
    set_bot_state(_howl_key(telegram_id, network), last_runs[-10:])

    return howl_record


def get_pending_howl(telegram_id: int, network: str) -> dict | None:
    raw = get_bot_state_raw(_pending_key(telegram_id, network))
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if data.get("status") not in ("pending", "partially_applied"):
            return None
        ts_str = data.get("timestamp")
        if ts_str:
            created = datetime.fromisoformat(ts_str)
            age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
            if age_hours > 48:
                data["status"] = "expired"
                set_bot_state(_pending_key(telegram_id, network), data)
                return None
        return data
    except Exception:
        return None


def approve_howl_suggestion(telegram_id: int, network: str, suggestion_index: int) -> tuple[bool, str]:
    pending = get_pending_howl(telegram_id, network)
    if not pending:
        return False, "No pending HOWL suggestions"

    suggestions = pending.get("suggestions", [])
    if suggestion_index < 0 or suggestion_index >= len(suggestions):
        return False, f"Invalid suggestion index ({suggestion_index})"

    suggestion = suggestions[suggestion_index]
    param = suggestion.get("parameter", "")
    new_value = suggestion.get("suggested_value")

    if not param or new_value is None:
        return False, "Invalid suggestion data"

    # SECURITY: never trust the LLM's param/value verbatim — validate against the
    # allowlist + safe bounds so a hallucinated or prompt-injected suggestion
    # can't weaken the risk rails. Rejected suggestions are marked so the user
    # isn't told a no-op "succeeded".
    ok, coerced_value, error = validate_howl_param(param, new_value)
    if not ok:
        suggestions[suggestion_index]["status"] = "rejected"
        suggestions[suggestion_index]["reject_reason"] = error
        pending["status"] = "partially_applied" if any(
            s.get("status") not in ("approved", "rejected") for s in suggestions
        ) else "completed"
        set_bot_state(_pending_key(telegram_id, network), pending)
        logger.warning(
            "Rejected unsafe HOWL suggestion user=%s param=%s: %s",
            telegram_id, param, error,
        )
        try:
            from src.nadobro.users.audit_log import record_audit_event

            record_audit_event(
                telegram_id, "howl_suggestion_rejected", f"param={param} reason={error}"
            )
        except Exception:
            pass
        return False, f"Skipped unsafe suggestion ({param}): {error}"

    new_value = coerced_value

    try:
        def _mutate(s):
            strategies = s.setdefault("strategies", {})
            bro = strategies.setdefault("bro", {})
            bro[param] = new_value

        update_user_settings(telegram_id, _mutate)

        suggestions[suggestion_index]["status"] = "approved"
        pending["status"] = "partially_applied" if any(
            s.get("status") == "pending" for s in suggestions
        ) else "applied"
        set_bot_state(_pending_key(telegram_id, network), pending)

        return True, f"Applied: {param} = {new_value}"
    except Exception as e:
        return False, f"Failed to apply: {str(e)[:100]}"


def reject_howl_suggestion(telegram_id: int, network: str, suggestion_index: int) -> tuple[bool, str]:
    pending = get_pending_howl(telegram_id, network)
    if not pending:
        return False, "No pending HOWL suggestions"

    suggestions = pending.get("suggestions", [])
    if suggestion_index < 0 or suggestion_index >= len(suggestions):
        return False, f"Invalid suggestion index"

    suggestions[suggestion_index]["status"] = "rejected"
    pending["status"] = "partially_applied" if any(
        s.get("status") not in ("approved", "rejected") for s in suggestions
    ) else "completed"
    set_bot_state(_pending_key(telegram_id, network), pending)

    return True, "Suggestion rejected"


def dismiss_all_howl(telegram_id: int, network: str) -> bool:
    pending = get_pending_howl(telegram_id, network)
    if not pending:
        return False
    pending["status"] = "dismissed"
    set_bot_state(_pending_key(telegram_id, network), pending)
    return True


def format_howl_message(howl_data: dict) -> str:
    """Telegram HTML. This was Markdown sent with NO parse_mode — every
    recipient of the nightly report saw literal *asterisks* and _underscores_
    (the 2026-06 "*hdhd*" sighting). HTML + escaped dynamics is also robust
    against LLM-generated assessment text containing markup characters."""
    from src.nadobro.utils.visual import esc

    lines = ["🐺 <b>HOWL — Nightly Optimization Report</b>\n"]

    assessment = howl_data.get("overall_assessment", "")
    if assessment:
        lines.append(f"<i>{esc(assessment)}</i>\n")

    metrics = howl_data.get("metrics", {})
    if metrics:
        total = metrics.get("total_trades", 0)
        wr = metrics.get("win_rate", 0)
        total_pnl = metrics.get("total_pnl", 0)
        lines.append(f"📊 24h: {total} trades · Win rate {wr:.0f}% · PnL ${total_pnl:+.2f}\n")

    suggestions = howl_data.get("suggestions", [])
    if suggestions:
        lines.append("<b>Suggested Changes</b>")
        for i, s in enumerate(suggestions):
            param = esc(s.get("parameter", "?"))
            current = esc(s.get("current_value", "?"))
            new = esc(s.get("suggested_value", "?"))
            rationale = s.get("rationale", "")
            impact = s.get("expected_impact", "")
            status = s.get("status", "pending")

            if status != "pending":
                emoji = "✅" if status == "approved" else "❌"
                lines.append(f"\n{emoji} <code>{param}</code>: {current} → {new} [{esc(status)}]")
            else:
                lines.append(f"\n{i+1}. <code>{param}</code>: {current} → <b>{new}</b>")
                if rationale:
                    lines.append(f"   <i>{esc(rationale)}</i>")
                if impact:
                    lines.append(f"   Expected: {esc(impact)}")
    else:
        lines.append("No specific changes suggested — current parameters look good.")

    confidence = howl_data.get("confidence", 0)
    lines.append(f"\nConfidence: {confidence:.0%}")

    return "\n".join(lines)
