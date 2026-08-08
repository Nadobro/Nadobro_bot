import asyncio
import base64
import logging
import re
import secrets
import time
from io import BytesIO
from typing import Optional

from src.nadobro.utils.env import env_int
from src.nadobro.users.points_ui import points_followup_options_kb, points_scope_kb
from src.nadobro.users.lowiq_relay_client import (
    close_session as relay_close_session,
    poll_events as relay_poll_events,
    relay_is_configured,
    send_user_reply_option as relay_send_user_reply_option,
    send_user_reply as relay_send_user_reply,
    start_session as relay_start_session,
)
from src.nadobro.models.database import get_bot_state, set_bot_state
from src.nadobro.core.async_utils import run_blocking
from src.nadobro.users.user_service import get_user

logger = logging.getLogger(__name__)


async def _relay_with_retry(func, *args, **kwargs):
    """Execute a relay function with retry logic and exponential backoff."""
    last_error = None
    for attempt in range(POINTS_MAX_RETRIES):
        try:
            return await asyncio.wait_for(func(*args, **kwargs), timeout=POINTS_RELAY_TIMEOUT)
        except asyncio.TimeoutError:
            last_error = "Relay timeout"
            logger.warning("Points relay timeout (attempt %d/%d)", attempt + 1, POINTS_MAX_RETRIES)
        except Exception as e:
            last_error = str(e)
            logger.warning("Points relay error (attempt %d/%d): %s", attempt + 1, POINTS_MAX_RETRIES, e)
        if attempt < POINTS_MAX_RETRIES - 1:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
    raise RuntimeError(f"Points relay failed after {POINTS_MAX_RETRIES} attempts: {last_error}")


_POINTS_CACHE: dict[int, dict] = {}
_POINTS_CACHE_TTL_SECONDS = 3600
_POINTS_WINDOW_LABEL = "Last 7 Days"
# LOWIQPTS can take minutes per step; short timeouts dropped pending state so plain
# replies ("0", "Yes") were handled by other bots instead of the relay.
_POINTS_REPLY_TIMEOUT_SECONDS = max(
    60,
    env_int("LOWIQPTS_REPLY_TIMEOUT_SECONDS", 900),
)
_LOWIQPTS_PENDING_TTL_SECONDS = max(
    300,
    env_int("LOWIQPTS_PENDING_TTL_SECONDS", 1800),
)

POINTS_RELAY_TIMEOUT = max(
    210,
    env_int("POINTS_RELAY_TIMEOUT", 210),
)
POINTS_MAX_RETRIES = env_int("POINTS_MAX_RETRIES", 3)


def _friendly_lowiqpts_relay_failure(code: str | None, *, for_refresh: bool) -> str | None:
    """User-visible copy for structured LOWIQPTS relay errors (HTTP 200, ok=false)."""
    err = str(code or "").strip()
    if err == "channel_busy":
        return (
            "⏳ LOWIQPTS is busy with another refresh right now. Try again in a minute."
            if for_refresh
            else "⏳ LOWIQPTS is busy right now. Try again in a moment."
        )
    if err == "session_race":
        return (
            "⚠️ Points refresh conflict. Tap Refresh again."
            if for_refresh
            else "⚠️ Points flow conflict. Tap Refresh or try again shortly."
        )
    return None


_PENDING_QUEUE_KEY = "lowiqpts_pending_queue"
_PENDING_BY_WALLET_KEY = "lowiqpts_pending_by_wallet"
_ACTIVE_BY_CHAT_KEY = "lowiqpts_active_by_chat"
_RELAY_CURSOR_KEY = "lowiqpts_relay_cursor"
# bot_data is process memory; persist the pending queue + cursors here so a bot
# restart mid-refresh can resume instead of silently dropping the user's request.
_RELAY_STATE_KEY = "lowiqpts_relay_state"

_WALLET_RE = re.compile(r"0x[a-fA-F0-9]{40}")
_NO_POINTS_HINT_RE = re.compile(
    r"(no\s+points|no\s+data|no\s+trades|nothing\s+found|not\s+found|0\s*points?)",
    re.IGNORECASE,
)
# Mid-flow LOWIQPTS prompts often embed Points:/Volume: previews and "no
# trades" phrasing while still asking for input (extra costs, "send 0", ...).
# Never treat a message that matches this as terminal: completing the pending
# request here drops relay state before the user's "0" / Yes reply arrives.
# (Restored from the batch5 hardening that commit 6305681 reverted to bisect.)
_LOWIQPTS_INTERACTIVE_PROMPT_RE = re.compile(
    r"(?i)(\bextra\s+costs?\b|\bsend\s+0\b|\balready\s+fetched\s+from\s+api\b|\benter\s+(your\s+)?extras?\b)",
)
# Bare numbers ("0", "0.5") and yes/no are the typical replies LOWIQPTS expects.
# When persistence/rehydration ever misses an edge case, these would otherwise fall
# through the message handler chain into the AI chat — surface "session expired"
# instead of dumping the user into Trading Bro.
_LOWIQPTS_ORPHAN_REPLY_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?|yes|no|y|n)\s*$",
    re.IGNORECASE,
)

# A pending points refresh lives for up to LOWIQPTS_PENDING_TTL_SECONDS (30min
# default). The relay used to claim EVERY free-text message inside that window,
# which is above the trade parsers in handlers/messages.py — so a user who typed
# "Short 0.3 ETH 49x" while a refresh was pending had their trade forwarded to
# LOWIQPTS ("Data error: Текст должен быть JSON массивом/объектом или JSONL")
# instead of opening a position. The relay now only claims text SHAPED like an
# answer to what LOWIQPTS actually asks for: a bare number (or a list of them),
# yes/no, a wallet, or one of the option labels it just offered. Everything else
# falls through to the normal handler chain.
_LOWIQPTS_ANSWER_RE = re.compile(
    r"""^\s*(?:
          \d+(?:[.,]\d+)?(?:\s*[,;/]\s*\d+(?:[.,]\d+)?)*   # 0 | 0.5 | 0,1,2
        | 0x[a-fA-F0-9]{40}                                # wallet paste
        | y|n|yes|no|ok|okay|none|skip|nope|yep|yeah        # confirmations
        | да|нет|ага                                       # LOWIQPTS is RU-facing
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
# Belt-and-braces veto: trading vocabulary is never a points answer, even if it
# somehow matched the shape above. Mirrors desk_parser.TRADE_VERBS (duplicated
# rather than imported: users/ must not depend on trading/ for a regex).
_LOWIQPTS_TRADE_VETO_RE = re.compile(
    r"\b(long|short|buy|sell|close|market|limit|leverage|twap|dca|"
    r"accumulate|acquire|purchase|grab|ape|scale|perp|flip|hedge)\b",
    re.IGNORECASE,
)


def looks_like_orphan_lowiqpts_reply(text: str) -> bool:
    return bool(_LOWIQPTS_ORPHAN_REPLY_RE.match(text or ""))


def text_is_lowiqpts_answer(text: str, req: Optional[dict] = None) -> bool:
    """Is this free text plausibly an answer to a pending LOWIQPTS prompt?

    Gate for ``relay_user_reply_to_lowiqpts``: a pending points refresh must not
    swallow trade instructions, questions, or chat. Option labels offered by the
    relay itself always pass (they can be arbitrary text); everything else has to
    look like a number / confirmation / wallet and carry no trading vocabulary.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return False

    # The relay just offered these exact labels — tapping-by-typing must work
    # regardless of what the label says.
    options = (req or {}).get("relay_options")
    if isinstance(options, list):
        normalized = cleaned.casefold()
        for option in options:
            if str(option or "").strip().casefold() == normalized:
                return True

    if _LOWIQPTS_TRADE_VETO_RE.search(cleaned):
        return False
    return bool(_LOWIQPTS_ANSWER_RE.match(cleaned))


def _pending_maps(bot_data: dict) -> tuple[list[dict], dict[str, list[dict]]]:
    queue = bot_data.setdefault(_PENDING_QUEUE_KEY, [])
    by_wallet = bot_data.setdefault(_PENDING_BY_WALLET_KEY, {})
    return queue, by_wallet


def _active_map(bot_data: dict) -> dict[int, str]:
    active = bot_data.setdefault(_ACTIVE_BY_CHAT_KEY, {})
    normalized: dict[int, str] = {}
    for chat_id, req_id in (active or {}).items():
        try:
            normalized[int(chat_id)] = str(req_id)
        except (TypeError, ValueError):
            continue
    active.clear()
    active.update(normalized)
    return active


def _extract_wallet(text: str) -> Optional[str]:
    if not text:
        return None
    match = _WALLET_RE.search(text)
    return match.group(0).lower() if match else None


def _remove_pending_req(bot_data: dict, req: dict) -> None:
    queue, by_wallet = _pending_maps(bot_data)
    try:
        queue.remove(req)
    except ValueError:
        pass
    wallet = str(req.get("wallet", "")).lower()
    items = by_wallet.get(wallet) or []
    items = [x for x in items if x is not req]
    if items:
        by_wallet[wallet] = items
    else:
        by_wallet.pop(wallet, None)
    req_id = str(req.get("req_id", ""))
    active = _active_map(bot_data)
    for chat_id, active_req_id in list(active.items()):
        if active_req_id == req_id:
            active.pop(chat_id, None)


def _drain_pending_points_for_chat(bot_data: dict, chat_id: int) -> list[str]:
    """Remove all queued LOWIQPTS pending rows for this chat; return relay session ids to close."""
    queue, _ = _pending_maps(bot_data)
    stale_sids: list[str] = []
    cid = int(chat_id)
    for r in list(queue):
        try:
            if int(r.get("chat_id", 0)) != cid:
                continue
        except (TypeError, ValueError):
            continue
        sid = str(r.get("relay_session_id", "")).strip()
        if sid:
            stale_sids.append(sid)
        _remove_pending_req(bot_data, r)
    return stale_sids


def _pending_req_by_id(bot_data: dict, req_id: str) -> Optional[dict]:
    if not req_id:
        return None
    queue, _ = _pending_maps(bot_data)
    for req in queue:
        if str(req.get("req_id", "")) == str(req_id):
            return req
    return None


def _set_active_req_for_chat(bot_data: dict, chat_id: int, req_id: str) -> None:
    _active_map(bot_data)[int(chat_id)] = str(req_id)


def _touch_pending_request(req: dict) -> None:
    req["ts"] = time.time()


def _prune_stale_pending(bot_data: dict, ttl_seconds: int | None = None) -> None:
    if ttl_seconds is None:
        ttl_seconds = _LOWIQPTS_PENDING_TTL_SECONDS
    now = time.time()
    queue, by_wallet = _pending_maps(bot_data)
    fresh = [req for req in queue if now - float(req.get("ts", 0)) <= ttl_seconds]
    queue[:] = fresh
    wallet_map: dict[str, list[dict]] = {}
    for req in fresh:
        wallet = str(req.get("wallet", "")).lower()
        if wallet:
            wallet_map.setdefault(wallet, []).append(req)
    by_wallet.clear()
    by_wallet.update(wallet_map)
    queue_req_ids = {str(req.get("req_id", "")) for req in fresh}
    active = _active_map(bot_data)
    for chat_id, req_id in list(active.items()):
        if req_id not in queue_req_ids:
            active.pop(chat_id, None)


def get_active_pending_request(bot_data: dict, chat_id: int) -> Optional[dict]:
    """Resolve pending LOWIQPTS row for this chat, repairing stale active pointers."""
    _prune_stale_pending(bot_data)
    cid = int(chat_id)
    active = _active_map(bot_data)
    req_id = str(active.get(cid, "")).strip()
    if req_id:
        req = _pending_req_by_id(bot_data, req_id)
        if req:
            return req
        active.pop(cid, None)

    queue, _ = _pending_maps(bot_data)
    candidates: list[dict] = []
    for r in queue:
        try:
            if int(r.get("chat_id", 0)) == cid:
                candidates.append(r)
        except (TypeError, ValueError):
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda r: float(r.get("ts", 0)), reverse=True)
    best = candidates[0]
    rid = str(best.get("req_id", "")).strip()
    if rid:
        _set_active_req_for_chat(bot_data, cid, rid)
    return best


def complete_pending_request(bot_data: dict, req: dict) -> None:
    _remove_pending_req(bot_data, req)


def _schedule_timeout(application, req_id: str) -> None:
    job_queue = getattr(application, "job_queue", None)
    if not job_queue:
        return
    job_name = f"lowiqpts-timeout:{req_id}"
    for job in job_queue.get_jobs_by_name(job_name):
        try:
            job.schedule_removal()
        except Exception:
            pass
    job_queue.run_once(
        _on_points_refresh_timeout,
        when=_POINTS_REPLY_TIMEOUT_SECONDS,
        data={"req_id": req_id},
        name=job_name,
    )


def _serialize_relay_state(bot_data: dict) -> dict:
    queue, _ = _pending_maps(bot_data)
    cursors: dict[str, str] = {}
    prefix = _RELAY_CURSOR_KEY + ":"
    for key, value in bot_data.items():
        if isinstance(key, str) and key.startswith(prefix) and value:
            cursors[key[len(prefix):]] = str(value)
    return {"queue": [dict(req) for req in queue], "cursors": cursors}


def _write_relay_state_sync(state: dict) -> None:
    try:
        set_bot_state(_RELAY_STATE_KEY, state)
    except Exception:
        logger.warning("Failed to persist LOWIQPTS relay state", exc_info=True)


async def _persist_relay_state(bot_data: dict) -> None:
    """Mirror the in-memory pending queue to bot_state so a restart can resume mid-flow."""
    # Serialize on the event loop (no awaits here) so we never iterate bot_data
    # while another coroutine mutates it; hand only the DB write to a worker thread.
    state = _serialize_relay_state(bot_data)
    await run_blocking(_write_relay_state_sync, state)


def _drop_relay_cursor(bot_data: dict, session_id: str) -> None:
    sid = str(session_id or "").strip()
    if sid:
        bot_data.pop(f"{_RELAY_CURSOR_KEY}:{sid}", None)


def rehydrate_lowiqpts_pending_state(application) -> None:
    """Restore the pending LOWIQPTS queue + relay cursors after a process restart.

    bot_data is process memory: without this, a deploy or crash mid-refresh silently
    drops the user's pending request, and their next "0"/"Yes" falls through to chat.
    """
    bot_data = getattr(application, "bot_data", None)
    if bot_data is None:
        return
    try:
        state = get_bot_state(_RELAY_STATE_KEY)
    except Exception:
        logger.warning("Failed to load persisted LOWIQPTS relay state", exc_info=True)
        return
    # Always log so deploy/runtime verification is unambiguous: the presence of
    # this line confirms the P0 rehydration code is actually running in this image.
    if state is None:
        logger.info("LOWIQPTS rehydration: no persisted state (bot_state empty)")
        return
    if not isinstance(state, dict):
        logger.info("LOWIQPTS rehydration: persisted state has unexpected type %s", type(state).__name__)
        return

    raw_queue = state.get("queue")
    cursors = state.get("cursors")
    raw_queue_count = len(raw_queue) if isinstance(raw_queue, list) else 0
    raw_cursor_count = len(cursors) if isinstance(cursors, dict) else 0
    logger.info(
        "LOWIQPTS rehydration: bot_state has queue=%d cursors=%d",
        raw_queue_count, raw_cursor_count,
    )
    queue, by_wallet = _pending_maps(bot_data)
    queue.clear()
    by_wallet.clear()
    active = _active_map(bot_data)
    active.clear()

    now = time.time()
    restored = 0
    if isinstance(raw_queue, list):
        for raw in raw_queue:
            if not isinstance(raw, dict):
                continue
            req_id = str(raw.get("req_id", "")).strip()
            session_id = str(raw.get("relay_session_id", "")).strip()
            chat_id = raw.get("chat_id")
            # No relay session => the req was mid-start when we crashed: unrecoverable.
            if not req_id or not session_id or chat_id is None:
                continue
            req = dict(raw)
            # Grace period: the restart was not the user's fault — do not let prune or
            # the timeout job immediately drop a flow that was healthy before the bounce.
            req["ts"] = now
            queue.append(req)
            wallet = str(req.get("wallet", "")).lower()
            if wallet:
                by_wallet.setdefault(wallet, []).append(req)
            restored += 1

    # Rebuild active-by-chat as newest req per chat; get_active_pending_request self-heals.
    newest_by_chat: dict[int, dict] = {}
    for req in queue:
        try:
            cid = int(req.get("chat_id"))
        except (TypeError, ValueError):
            continue
        current = newest_by_chat.get(cid)
        if current is None or float(req.get("ts", 0)) >= float(current.get("ts", 0)):
            newest_by_chat[cid] = req
    for cid, req in newest_by_chat.items():
        active[cid] = str(req.get("req_id", ""))

    if isinstance(cursors, dict):
        live_sessions = {str(req.get("relay_session_id", "")).strip() for req in queue}
        for sid, cursor in cursors.items():
            sid = str(sid).strip()
            # Only restore cursors for sessions we are actually resuming, so a stale
            # cursor cannot shadow a fresh session and replay old events.
            if sid and cursor and sid in live_sessions:
                bot_data[f"{_RELAY_CURSOR_KEY}:{sid}"] = str(cursor)

    # Re-arm the timeout job for each rehydrated flow so it stays bounded.
    for req in queue:
        _schedule_timeout(application, str(req.get("req_id", "")))

    logger.info(
        "LOWIQPTS rehydration: restored=%d skipped_no_session=%d",
        restored, raw_queue_count - restored,
    )


def _extract_session_id(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = [
        payload.get("session_id"),
        (payload.get("data") or {}).get("session_id") if isinstance(payload.get("data"), dict) else None,
        (payload.get("session") or {}).get("id") if isinstance(payload.get("session"), dict) else None,
    ]
    for value in candidates:
        if value:
            return str(value)
    return ""


def _extract_event_text(event: dict) -> str:
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text = (
        event.get("text")
        or event.get("message")
        or event.get("caption")
        or payload.get("text")
    )
    return str(text or "").strip()


def _extract_event_session_id(event: dict) -> str:
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    session = event.get("session") if isinstance(event.get("session"), dict) else {}
    candidates = [event.get("session_id"), payload.get("session_id"), session.get("id")]
    for value in candidates:
        if value:
            return str(value)
    return ""


def _extract_event_options(event: dict) -> list[str]:
    if not isinstance(event, dict):
        return []
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    raw = event.get("options")
    if raw is None:
        raw = payload.get("options")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        label = str(item or "").strip()
        if label:
            out.append(label)
    return out


def _extract_event_photo(event: dict) -> tuple[Optional[bytes], Optional[str]]:
    if not isinstance(event, dict):
        return None, None
    b64 = event.get("photo_base64")
    if not b64 or not isinstance(b64, str):
        return None, None
    try:
        raw = base64.b64decode(b64.encode("ascii"))
    except Exception:
        logger.warning("Invalid LOWIQPTS relay photo payload")
        return None, None
    if not raw:
        return None, None
    mime = str(event.get("photo_mime") or "image/jpeg").strip() or "image/jpeg"
    return raw, mime


def _relay_photo_filename(mime: Optional[str]) -> str:
    m = (mime or "").lower()
    if "png" in m:
        return "nado_points.png"
    if "webp" in m:
        return "nado_points.webp"
    return "nado_points.jpg"


def _looks_like_nado_report_summary_text(text: str) -> bool:
    """Block-shaped LOWIQPTS report (forwarded as text before the image card)."""
    if not text or len(text) < 100:
        return False
    upper = text.upper()
    if "NADO REPORT" in upper:
        return True
    if "📊" in text and "EPOCH" in upper and ("POINTS" in upper or "CORE" in upper):
        return True
    return False


def _extract_event_source_message_id(event: dict) -> Optional[int]:
    if not isinstance(event, dict):
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    raw = event.get("source_message_id")
    if raw is None:
        raw = payload.get("source_message_id")
    try:
        if raw is None:
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extract_events_response(payload: dict) -> tuple[list[dict], Optional[str]]:
    if not isinstance(payload, dict):
        return [], None
    events_raw = payload.get("events")
    if events_raw is None and isinstance(payload.get("data"), dict):
        events_raw = payload["data"].get("events")
    events = events_raw if isinstance(events_raw, list) else []
    next_cursor = payload.get("next_cursor")
    if not next_cursor and isinstance(payload.get("data"), dict):
        next_cursor = payload["data"].get("next_cursor")
    if not next_cursor:
        next_cursor = payload.get("cursor")
    return [e for e in events if isinstance(e, dict)], (str(next_cursor) if next_cursor else None)


def _extract_session_status(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    status = payload.get("session_status")
    if not status and isinstance(payload.get("data"), dict):
        status = payload["data"].get("session_status")
    return str(status or "").strip().lower()


def _claim_pending_for_event(bot_data: dict, session_id: str, text: str) -> Optional[dict]:
    _prune_stale_pending(bot_data)
    queue, by_wallet = _pending_maps(bot_data)
    if session_id:
        for req in queue:
            if str(req.get("relay_session_id", "")) == session_id:
                _touch_pending_request(req)
                _set_active_req_for_chat(bot_data, int(req.get("chat_id")), str(req.get("req_id", "")))
                return req
    wallet = _extract_wallet(text)
    if wallet and by_wallet.get(wallet):
        req = by_wallet[wallet][-1]
        _touch_pending_request(req)
        _set_active_req_for_chat(bot_data, int(req.get("chat_id")), str(req.get("req_id", "")))
        return req
    return None


def parse_lowiq_points_reply(text: str) -> Optional[dict]:
    if not text:
        return None
    num = r"([\-]?[\d\s,\.]+)"
    points_match = re.search(r"(?i)(?:\*{0,2}\s*)?points?\s*:\s*(?:\*{0,2}\s*)?(?:\x24)?\s*" + num, text)
    volume_match = re.search(r"(?i)(?:\*{0,2}\s*)?volume\s*:\s*(?:\*{0,2}\s*)?(?:\x24)?\s*" + num, text)
    cpp_match = re.search(
        r"(?i)(?:\*{0,2}\s*)?(?:cost\s*/\s*point|cost\s+per\s+point)\s*:\s*(?:\*{0,2}\s*)?(?:\x24)?\s*" + num,
        text,
    )
    explicit_fields = bool(points_match or volume_match or cpp_match)
    if not explicit_fields and _NO_POINTS_HINT_RE.search(text):
        # "no trades"-style copy inside an interactive prompt is NOT terminal —
        # LOWIQPTS sends it mid-flow while still waiting for the user's
        # "0"/Yes reply. Only a terminal no-activity message (no prompt
        # phrasing) completes with a zero snapshot.
        if _LOWIQPTS_INTERACTIVE_PROMPT_RE.search(text):
            return None
        return {
            "points": 0.0,
            "volume_usd": 0.0,
            "cost_per_point": 0.0,
            "no_activity": True,
        }
    if not explicit_fields:
        return None

    def _to_float(match_obj) -> float:
        if not match_obj:
            return 0.0
        raw = match_obj.group(1).replace(",", "").replace(" ", "").replace("$", "")
        try:
            return float(raw)
        except ValueError:
            return 0.0

    points = _to_float(points_match)
    volume = _to_float(volume_match)
    cost_per_point = _to_float(cpp_match)
    if points <= 0 and volume <= 0 and cost_per_point <= 0:
        return {
            "points": 0.0,
            "volume_usd": 0.0,
            "cost_per_point": 0.0,
            "no_activity": True,
        }
    return {
        "points": points,
        "volume_usd": volume,
        "cost_per_point": cost_per_point,
        "no_activity": False,
    }


def build_dashboard_payload(parsed: dict) -> dict:
    points = float(parsed.get("points") or 0.0)
    volume = float(parsed.get("volume_usd") or 0.0)
    cpp = float(parsed.get("cost_per_point") or 0.0)
    total_cost = cpp * points if points > 0 and cpp > 0 else 0.0
    ppm = ((points / volume) * 1_000_000.0) if volume > 0 else 0.0
    return {
        "ok": True,
        "scope": "week",
        "scope_mode": "lowiqpts_week",
        "window_label": _POINTS_WINDOW_LABEL,
        "points_source": "lowiqpts_relay",
        "points_estimated": False,
        "volume_usd": volume,
        "points": points,
        "cost_per_point": cpp,
        "total_costs": total_cost,
        "fees_paid": total_cost,
        "ppm": ppm,
        "maker_count": 0,
        "taker_count": 0,
        "maker_pct": 0.0,
        "taker_pct": 0.0,
        "maker_bar": "░░░░░░░░░░",
        "positions": 0,
        "avg_hold_seconds": 0,
        "missing_fields": [],
        "no_activity": bool(parsed.get("no_activity")),
    }


def save_points_snapshot(telegram_id: int, payload: dict) -> None:
    _POINTS_CACHE[int(telegram_id)] = {"ts": time.time(), "data": payload}


def get_points_dashboard(telegram_id: int, scope: str = "week") -> dict:
    scope_norm = str(scope or "week").strip().lower()
    if scope_norm not in ("week", "wk", ""):
        return {
            "ok": False,
            "error": (
                "LOWIQPTS returns a last-week snapshot only. Use the Week button; "
                "Month/All are not available through this relay yet."
            ),
        }
    cached = _POINTS_CACHE.get(int(telegram_id))
    if cached and (time.time() - float(cached.get("ts", 0)) < _POINTS_CACHE_TTL_SECONDS):
        return cached["data"]
    user = get_user(telegram_id)
    if not user or not user.main_address:
        return {"ok": False, "error": "Wallet not linked yet. Open 👛 Wallet first."}
    return {"ok": False, "error": "No points snapshot yet. Tap Refresh to fetch last-week data."}


async def request_points_refresh(context, telegram_id: int, chat_id: int) -> dict:
    user = get_user(telegram_id)
    if not user or not user.main_address:
        return {"ok": False, "error": "❌ Please connect your wallet first via the Wallet button."}
    if not relay_is_configured():
        return {"ok": False, "error": "❌ LOWIQPTS relay is not configured."}

    wallet = str(user.main_address).lower()
    bot_data = context.application.bot_data
    _prune_stale_pending(bot_data)
    stale_sessions = _drain_pending_points_for_chat(bot_data, int(chat_id))
    for sid in stale_sessions:
        try:
            await relay_close_session(session_id=sid, reason="superseded_by_refresh")
        except Exception:
            logger.debug("Could not close superseded LOWIQPTS session %s", sid, exc_info=True)
        _drop_relay_cursor(bot_data, sid)

    queue, by_wallet = _pending_maps(bot_data)
    req_id = secrets.token_urlsafe(12)
    req = {
        "req_id": req_id,
        "telegram_id": int(telegram_id),
        "chat_id": int(chat_id),
        "wallet": wallet,
        "ts": time.time(),
    }
    queue.append(req)
    by_wallet.setdefault(wallet, []).append(req)
    _set_active_req_for_chat(bot_data, chat_id, req_id)

    try:
        relay_resp = await _relay_with_retry(
            relay_start_session,
            telegram_user_id=int(telegram_id),
            chat_id=int(chat_id),
            wallet=wallet,
            request_id=req_id,
        )
    except Exception as e:
        _remove_pending_req(bot_data, req)
        await _persist_relay_state(bot_data)
        logger.warning("Failed to start lowiq relay session: %s", e)
        return {"ok": False, "error": "❌ Could not reach LOWIQPTS relay. Try again shortly."}

    if not relay_resp.get("ok"):
        _remove_pending_req(bot_data, req)
        await _persist_relay_state(bot_data)
        err = str(relay_resp.get("error") or "")
        friendly = _friendly_lowiqpts_relay_failure(err, for_refresh=True)
        if friendly:
            return {"ok": False, "error": friendly}
        logger.warning("relay_start_session logical failure: %s", relay_resp)
        return {"ok": False, "error": "❌ Could not start LOWIQPTS session. Try again shortly."}

    session_id = _extract_session_id(relay_resp)
    if not session_id:
        _remove_pending_req(bot_data, req)
        await _persist_relay_state(bot_data)
        logger.warning("relay_start_session missing session_id: %s", relay_resp)
        return {"ok": False, "error": "❌ Could not start LOWIQPTS session. Try again shortly."}

    req["relay_session_id"] = session_id
    _schedule_timeout(context.application, req_id)
    await _persist_relay_state(bot_data)
    # Deploy/runtime verification: this line + the rehydration log together prove
    # that persistence is reaching bot_state on every refresh click.
    logger.info(
        "LOWIQPTS refresh requested: req_id=%s chat_id=%d session_id=%s",
        req_id, int(chat_id), session_id,
    )
    return {
        "ok": True,
        "req_id": req_id,
        "timeout_seconds": _POINTS_REPLY_TIMEOUT_SECONDS,
    }


def set_pending_banner_message(
    bot_data: dict,
    req_id: str,
    chat_id: int | None,
    message_id: int | None,
) -> None:
    """Anchor the timeout banner to the user's original "Refresh requested" card.

    Without this, ``_on_points_refresh_timeout`` posts a fresh chat message 15
    minutes later — which surfaces inside whatever flow the user has since
    moved to (strategy work, trade flow, etc.) and looks like the bot just
    "brought up the Points buttons" out of nowhere.
    """
    if not req_id or chat_id is None or message_id is None:
        return
    req = _pending_req_by_id(bot_data, str(req_id))
    if not req:
        return
    try:
        req["banner_chat_id"] = int(chat_id)
        req["banner_message_id"] = int(message_id)
    except (TypeError, ValueError):
        return


async def relay_user_reply_to_lowiqpts(context, chat_id: int, text: str) -> dict:
    req = get_active_pending_request(context.application.bot_data, chat_id)
    if not req:
        return {"ok": False, "handled": False}
    if not relay_is_configured():
        return {"ok": False, "handled": True, "error": "❌ LOWIQPTS relay is not configured."}

    cleaned = (text or "").strip()
    if not cleaned:
        return {"ok": True, "handled": True}

    cancel_key = cleaned.lower().strip()
    if cancel_key.startswith("/"):
        cancel_key = cancel_key[1:].strip()
    if cancel_key in {"cancel", "stop", "exit", "quit"}:
        session_id = str(req.get("relay_session_id", "")).strip()
        if session_id:
            await relay_close_session(session_id=session_id, reason="cancelled_by_user")
        complete_pending_request(context.application.bot_data, req)
        _drop_relay_cursor(context.application.bot_data, session_id)
        await _persist_relay_state(context.application.bot_data)
        return {"ok": True, "handled": True, "cancelled": True}

    # The user typed something that is not an answer to LOWIQPTS (a trade, a
    # question, chat). Decline the message so the normal handler chain — trade
    # parsers, desk, AI — gets it. The pending request stays open; the relay is
    # still free to prompt again, and the user's next number/yes still lands.
    if not text_is_lowiqpts_answer(cleaned, req):
        logger.info(
            "LOWIQPTS relay declined free text (not an answer shape): req_id=%s chat=%d",
            req.get("req_id"), chat_id,
        )
        return {"ok": False, "handled": False}

    session_id = str(req.get("relay_session_id", "")).strip()
    if not session_id:
        return {"ok": False, "handled": True, "error": "❌ LOWIQPTS relay session is missing."}

    # Diagnostic: which session does the user's reply hit? Pair this with the
    # earlier "LOWIQPTS refresh requested" line; if the session_ids differ, two
    # bot processes / stale rehydration are in play. If they match, the relay
    # closed the session out from under us.
    logger.info(
        "LOWIQPTS user-reply: req_id=%s chat=%d session=%s text=%r",
        req.get("req_id"), chat_id, session_id, cleaned[:40],
    )

    try:
        relay_resp = await _relay_with_retry(
            relay_send_user_reply, session_id=session_id, text=cleaned
        )
    except Exception as e:
        logger.warning(
            "Failed to relay user reply to lowiqpts relay (session=%s): %s",
            session_id, e,
        )
        return {"ok": False, "handled": True, "error": "❌ Could not relay your reply to LOWIQPTS right now."}

    if not relay_resp.get("ok"):
        friendly = _friendly_lowiqpts_relay_failure(relay_resp.get("error"), for_refresh=False)
        if friendly:
            return {"ok": False, "handled": True, "error": friendly}
        logger.warning(
            "relay_send_user_reply logical failure (session=%s req_id=%s): %s",
            session_id, req.get("req_id"), relay_resp,
        )
        return {"ok": False, "handled": True, "error": "❌ Could not relay your reply to LOWIQPTS right now."}

    _touch_pending_request(req)
    _schedule_timeout(context.application, str(req.get("req_id", "")))
    await _persist_relay_state(context.application.bot_data)
    return {"ok": True, "handled": True}


async def _process_relay_event(bot_app, bot_data: dict, event: dict) -> None:
    text = _extract_event_text(event)
    photo_bytes, photo_mime = _extract_event_photo(event)
    if not text.strip() and not photo_bytes:
        return

    session_id = _extract_event_session_id(event)
    req = _claim_pending_for_event(bot_data, session_id, text)
    if not req:
        return

    chat_id = int(req.get("chat_id"))
    telegram_id = int(req.get("telegram_id"))
    options = _extract_event_options(event)
    source_message_id = _extract_event_source_message_id(event)
    # Diagnostic: visibility on every event the bot acts on, so when a req
    # disappears mid-flow we can see exactly which event preceded its removal.
    logger.info(
        "LOWIQPTS event: req_id=%s session=%s text_len=%d options=%d photo=%s preview=%r",
        req.get("req_id"), session_id, len(text or ""), len(options or []),
        bool(photo_bytes), (text or "")[:80],
    )

    if options:
        req["relay_options"] = [str(o).strip() for o in options if str(o).strip()]
        if source_message_id is not None:
            req["relay_options_source_message_id"] = int(source_message_id)
        else:
            req.pop("relay_options_source_message_id", None)

    parsed = parse_lowiq_points_reply(text) if text else None
    summary_block = _looks_like_nado_report_summary_text(text)

    if summary_block and parsed:
        payload = build_dashboard_payload(parsed)
        save_points_snapshot(telegram_id, payload)

    if photo_bytes:
        try:
            await bot_app.bot.send_photo(
                chat_id=chat_id,
                photo=BytesIO(photo_bytes),
                filename=_relay_photo_filename(photo_mime),
                reply_markup=points_scope_kb(),
            )
        except Exception as e:
            logger.warning("LOWIQPTS relay: send_photo failed: %s", e)
        relay_sid = str(req.get("relay_session_id", "")).strip()
        logger.info(
            "LOWIQPTS req_id=%s completed via PHOTO (session=%s)",
            req.get("req_id"), relay_sid,
        )
        complete_pending_request(bot_data, req)
        if relay_sid:
            await relay_close_session(session_id=relay_sid, reason="completed")
        _drop_relay_cursor(bot_data, relay_sid)
        return

    if summary_block:
        _touch_pending_request(req)
        _schedule_timeout(bot_app, str(req.get("req_id", "")))
        return

    if text.strip():
        await bot_app.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=points_followup_options_kb(options) if options else None,
        )

    if options:
        _touch_pending_request(req)
        _schedule_timeout(bot_app, str(req.get("req_id", "")))
        return

    if not parsed:
        _schedule_timeout(bot_app, str(req.get("req_id", "")))
        return

    if _LOWIQPTS_INTERACTIVE_PROMPT_RE.search(text):
        # Mid-flow prompt embedding Points:/Volume: previews — the relay is
        # still waiting on the user's reply, so keep the request pending.
        _touch_pending_request(req)
        _schedule_timeout(bot_app, str(req.get("req_id", "")))
        return

    payload = build_dashboard_payload(parsed)
    save_points_snapshot(telegram_id, payload)
    relay_sid = str(req.get("relay_session_id", "")).strip()
    logger.info(
        "LOWIQPTS req_id=%s completed via PARSED REPORT (session=%s, points=%s volume=%s)",
        req.get("req_id"), relay_sid, parsed.get("points"), parsed.get("volume_usd"),
    )
    complete_pending_request(bot_data, req)
    if relay_sid:
        await relay_close_session(session_id=relay_sid, reason="completed")
    _drop_relay_cursor(bot_data, relay_sid)


async def relay_option_reply_to_lowiqpts(context, chat_id: int, option_index: int | str) -> dict:
    bot_data = context.application.bot_data
    req = get_active_pending_request(bot_data, chat_id)
    if not req:
        # Diagnostic: dump every queued req so we can see WHY the active lookup
        # missed the one with relay_options set.
        queue, _ = _pending_maps(bot_data)
        active = _active_map(bot_data)
        logger.info(
            "LOWIQPTS option-click MISS (no active req): chat=%d idx=%s "
            "active_map=%r queue_size=%d queue_chat_ids=%r queue_req_ids=%r "
            "queue_with_options=%r",
            chat_id, option_index, dict(active), len(queue),
            [r.get("chat_id") for r in queue],
            [r.get("req_id") for r in queue],
            [(r.get("req_id"), r.get("relay_options")) for r in queue if r.get("relay_options")],
        )
        return {"ok": False, "error": "No active LOWIQPTS request."}
    options = req.get("relay_options")
    if not isinstance(options, list) or not options:
        # Diagnostic: req exists but has no relay_options — show its full state
        # so we can see which fields ARE set and pinpoint why options went missing.
        logger.info(
            "LOWIQPTS option-click MISS (no relay_options on req): chat=%d idx=%s "
            "req_id=%s session=%s ts_age=%.1fs req_keys=%r relay_options=%r",
            chat_id, option_index, req.get("req_id"), req.get("relay_session_id"),
            time.time() - float(req.get("ts", 0) or 0),
            sorted(req.keys()), options,
        )
        return {"ok": False, "error": "No selectable options available right now."}
    logger.info(
        "LOWIQPTS option-click HIT: chat=%d idx=%s req_id=%s options=%r",
        chat_id, option_index, req.get("req_id"), options,
    )
    try:
        idx = int(option_index)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Invalid option."}
    if idx < 0 or idx >= len(options):
        return {"ok": False, "error": "That option is no longer available."}
    choice = str(options[idx]).strip()
    if not choice:
        return {"ok": False, "error": "Invalid option."}
    source_message_id = req.get("relay_options_source_message_id")
    session_id = str(req.get("relay_session_id", "")).strip()
    if not session_id:
        return {"ok": False, "error": "LOWIQPTS relay session is missing."}
    if source_message_id is None:
        # Fallback for older events without source message metadata.
        relay_result = await relay_user_reply_to_lowiqpts(context, chat_id, choice)
        relay_result["choice"] = choice
        return relay_result
    try:
        relay_result = await _relay_with_retry(
            relay_send_user_reply_option,
            session_id=session_id,
            option_text=choice,
            source_message_id=int(source_message_id),
        )
    except RuntimeError as e:
        logger.warning("LOWIQPTS option relay failed after retries: %s", e)
        relay_result = {"ok": False, "error": str(e)}
    if not relay_result.get("ok"):
        friendly = _friendly_lowiqpts_relay_failure(relay_result.get("error"), for_refresh=False)
        if friendly:
            return {"ok": False, "error": friendly}
        logger.warning(
            "LOWIQPTS inline click failed (%s); sending choice as plain relay text",
            relay_result.get("error"),
        )
        try:
            relay_result = await _relay_with_retry(
                relay_send_user_reply,
                session_id=session_id,
                text=choice,
            )
        except RuntimeError as e:
            return {"ok": False, "error": f"Could not reach LOWIQPTS relay: {e}"}
        if not relay_result.get("ok"):
            friendly_fb = _friendly_lowiqpts_relay_failure(relay_result.get("error"), for_refresh=False)
            if friendly_fb:
                return {"ok": False, "error": friendly_fb}
            return {
                "ok": False,
                "error": "Could not relay your button choice to LOWIQPTS. Try typing it in chat.",
            }
    _touch_pending_request(req)
    _schedule_timeout(context.application, str(req.get("req_id", "")))
    await _persist_relay_state(context.application.bot_data)
    relay_result["choice"] = choice
    return relay_result


async def _finalize_dead_session(bot_app, bot_data: dict, session_id: str, status: str) -> None:
    """Relay reports the session is no longer active — stop polling it and tell the user.

    Without this the bot keeps polling an expired/closed/failed relay session forever
    (the heartbeat keeps re-arming the timeout), and the user's flow hangs silently.
    """
    queue, _ = _pending_maps(bot_data)
    affected = [
        r for r in queue
        if str(r.get("relay_session_id", "")).strip() == str(session_id)
    ]
    _drop_relay_cursor(bot_data, session_id)
    if not affected:
        return
    logger.info(
        "LOWIQPTS session %s no longer active (status=%s); finalizing %d request(s)",
        session_id, status, len(affected),
    )
    for req in affected:
        complete_pending_request(bot_data, req)
        try:
            await bot_app.bot.send_message(
                chat_id=int(req.get("chat_id")),
                text=(
                    "⚠️ Your points refresh session ended before completing.\n"
                    "Tap 🏆 Refresh points to try again."
                ),
                reply_markup=points_scope_kb(),
            )
        except Exception as e:
            logger.warning("Failed to deliver LOWIQPTS session-ended notice: %s", e)


async def poll_lowiqpts_relay_events(bot_app) -> None:
    if not relay_is_configured():
        return
    bot_data = bot_app.bot_data
    queue, _ = _pending_maps(bot_data)
    # Heartbeat: keep pending rows alive while we wait on a slow LOWIQPTS session.
    for req in queue:
        if str(req.get("relay_session_id", "")).strip():
            _touch_pending_request(req)
    seen_sessions: set[str] = set()
    session_ids: list[str] = []
    for req in queue:
        sid = str(req.get("relay_session_id", "")).strip()
        if sid and sid not in seen_sessions:
            seen_sessions.add(sid)
            session_ids.append(sid)
    if not session_ids:
        return
    for session_id in session_ids:
        cursor_key = f"{_RELAY_CURSOR_KEY}:{session_id}"
        cursor = bot_data.get(cursor_key)
        response = await relay_poll_events(session_id=session_id, cursor=str(cursor) if cursor else None)
        if not response.get("ok"):
            continue

        events, next_cursor = _extract_events_response(response)
        if next_cursor:
            bot_data[cursor_key] = next_cursor
        for raw in events:
            event = dict(raw) if isinstance(raw, dict) else {}
            if not _extract_event_session_id(event):
                event["session_id"] = session_id
            try:
                await _process_relay_event(bot_app, bot_data, event)
            except Exception:
                logger.warning("Failed to process lowiq relay event", exc_info=True)

        # Reconcile liveness: a relay session the bot is still polling can be
        # expired/closed/failed underneath it (e.g. the bot was down past the
        # relay idle window). Stop polling it and notify the user instead of
        # hanging forever on a dead session.
        status = _extract_session_status(response)
        if status and status != "active":
            try:
                await _finalize_dead_session(bot_app, bot_data, session_id, status)
            except Exception:
                logger.warning("Failed to finalize dead LOWIQPTS session", exc_info=True)

    await _persist_relay_state(bot_data)


async def _on_points_refresh_timeout(context) -> None:
    data = getattr(context.job, "data", {}) or {}
    req_id = str(data.get("req_id", "")).strip()
    if not req_id:
        return

    bot_data = context.application.bot_data
    req = _pending_req_by_id(bot_data, req_id)
    if not req:
        # Already matched by relay response.
        return

    # poll_lowiqpts_relay_events refreshes req["ts"] every cycle while the bot is
    # actively polling a live LOWIQPTS session. LOWIQPTS routinely takes 20+ minutes
    # per step, so a quiet stretch is normal mid-flow — only expire when the request
    # has gone genuinely stale (heartbeat stopped: scheduler down or session dropped).
    last_activity = float(req.get("ts", 0) or 0)
    if last_activity and (time.time() - last_activity) < _POINTS_REPLY_TIMEOUT_SECONDS:
        _schedule_timeout(context.application, req_id)
        return

    chat_id = int(req.get("chat_id"))
    session_id = str(req.get("relay_session_id", "")).strip()
    logger.info(
        "LOWIQPTS req_id=%s expired by timeout (session=%s, idle=%.1fs)",
        req_id, session_id, time.time() - last_activity,
    )
    _remove_pending_req(bot_data, req)
    if session_id:
        await relay_close_session(session_id=session_id, reason="timeout")
    _drop_relay_cursor(bot_data, session_id)
    await _persist_relay_state(bot_data)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⏱ Points refresh is taking longer than expected.\n"
                "Please tap Refresh to retry."
            ),
            reply_markup=points_scope_kb(),
        )
    except Exception as e:
        logger.warning("Failed to deliver points timeout banner: %s", e)
