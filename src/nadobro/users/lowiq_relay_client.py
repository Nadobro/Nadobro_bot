import logging
import os

from src.nadobro.utils.env import env_float, env_int
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Session start/reply waits on @lowiqpts (can take minutes). The 2s scheduler
# poll must NOT inherit this — a hung AMS call from nrt pinned the job for
# 210s and APScheduler skip-warned every interval (2026-08-13).
_DEFAULT_TIMEOUT_SECONDS = env_float("LOWIQPTS_RELAY_TIMEOUT_SECONDS", 210.0)
_DEFAULT_POLL_LIMIT = env_int("LOWIQPTS_RELAY_POLL_LIMIT", 25)

_shared_client: Optional[httpx.AsyncClient] = None


def relay_base_url() -> str:
    return (os.environ.get("LOWIQPTS_RELAY_BASE_URL") or "").strip().rstrip("/")


def relay_poll_interval_seconds() -> int:
    raw = os.environ.get("LOWIQPTS_RELAY_POLL_SECONDS", "2")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 2


def relay_poll_timeout_seconds() -> float:
    """HTTP timeout for GET /events/poll — always shorter than the tick interval."""
    interval = float(relay_poll_interval_seconds())
    configured = env_float("LOWIQPTS_RELAY_POLL_TIMEOUT_SECONDS", 0.0)
    if configured > 0:
        return configured
    return max(0.5, min(1.5, interval * 0.75))


def relay_is_configured() -> bool:
    return bool(relay_base_url())


def _auth_header() -> dict[str, str]:
    token = (os.environ.get("LOWIQPTS_RELAY_AUTH_TOKEN") or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


async def _get_client() -> Optional[httpx.AsyncClient]:
    global _shared_client
    base = relay_base_url()
    if not base:
        return None
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(base_url=base, timeout=_DEFAULT_TIMEOUT_SECONDS, headers=_auth_header())
    return _shared_client


async def _request(
    method: str,
    path: str,
    *,
    json: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> dict:
    client = await _get_client()
    if client is None:
        return {"ok": False, "error": "relay_not_configured"}
    try:
        req_kwargs: dict[str, Any] = {}
        if timeout is not None:
            req_kwargs["timeout"] = timeout
        response = await client.request(method, path, json=json, params=params, **req_kwargs)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return data
        return {"ok": True, "data": data}
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:400]
        except Exception:
            body = ""
        logger.warning("LOWIQ relay HTTP error %s %s: %s", method, path, body)
        return {"ok": False, "error": "relay_http_error", "status_code": e.response.status_code, "body": body}
    except Exception as e:
        logger.warning("LOWIQ relay request failed %s %s: %s", method, path, e)
        return {"ok": False, "error": "relay_request_failed"}


async def start_session(*, telegram_user_id: int, chat_id: int, wallet: str, request_id: str) -> dict:
    return await _request(
        "POST",
        "/sessions/start",
        json={
            "telegram_user_id": int(telegram_user_id),
            "chat_id": int(chat_id),
            "wallet": str(wallet),
            "request_id": str(request_id),
        },
    )


async def send_user_reply(*, session_id: str, text: str) -> dict:
    return await _request(
        "POST",
        "/sessions/reply",
        json={
            "session_id": str(session_id),
            "text": str(text),
        },
    )


async def send_user_reply_option(*, session_id: str, option_text: str, source_message_id: int) -> dict:
    return await _request(
        "POST",
        "/sessions/reply_option",
        json={
            "session_id": str(session_id),
            "option_text": str(option_text),
            "source_message_id": int(source_message_id),
        },
    )


async def poll_events(*, session_id: str, cursor: Optional[str]) -> dict:
    params: dict[str, Any] = {"session_id": str(session_id), "limit": _DEFAULT_POLL_LIMIT}
    if cursor:
        params["cursor"] = str(cursor)
    return await _request(
        "GET",
        "/events/poll",
        params=params,
        timeout=relay_poll_timeout_seconds(),
    )


async def close_session(*, session_id: str, reason: Optional[str] = None) -> dict:
    payload: dict[str, Any] = {"session_id": str(session_id)}
    if reason:
        payload["reason"] = str(reason)
    return await _request("POST", "/sessions/close", json=payload)

