"""NanoGPT OpenAI-compatible chat API (https://nano-gpt.com/api)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from src.nadobro.connectors.provider_config import nanogpt_api_key, nanogpt_base_url as _configured_base_url
from src.nadobro.llm.provider_runtime import post_json_with_retries, provider_timeout_seconds, record_provider_degraded
from src.nadobro.utils.env import env_bool

logger = logging.getLogger(__name__)


def nanogpt_is_configured() -> bool:
    return bool(nanogpt_api_key())


def nanogpt_base_url() -> str:
    raw = _configured_base_url()
    if env_bool("NANOGPT_USE_LEGACY_ENDPOINT", False):
        if raw.endswith("/v1"):
            return raw[: -len("/v1")] + "/v1legacy"
    return raw


def nanogpt_default_model() -> str:
    from src.nadobro.connectors.provider_config import clean_env_value

    return clean_env_value(os.environ.get("NANOGPT_MODEL")) or "chatgpt-4o-latest"


def _nano_error_text(payload: dict[str, Any], body_text: str, status_code: int) -> str:
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or err)[:300]
    if err:
        return str(err)[:300]
    msg = payload.get("message") if isinstance(payload, dict) else None
    if msg:
        return str(msg)[:300]
    return (body_text or f"HTTP {status_code}")[:300]


def openai_compatible_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> tuple[bool, str, dict[str, Any]]:
    """POST /chat/completions. Returns (ok, assistant_text, raw_json)."""
    base = base_url.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    try:
        effective_timeout = provider_timeout_seconds("nanogpt", timeout)
        resp, _latency_ms = post_json_with_retries(
            "nanogpt",
            url,
            headers=headers,
            json_body=body,
            timeout=effective_timeout,
        )
    except Exception as exc:
        logger.warning("openai_compatible_chat failed model=%s: %s", model, exc)
        record_provider_degraded(
            "nanogpt",
            f"OpenAI-compatible chat failed model={model}: {exc}",
            allowed_use="llm",
            source_url=base_url,
        )
        return False, "", {"error": str(exc)[:300], "model": model}

    status_code = int(getattr(resp, "status_code", 0) or 0)
    body_text = ""
    try:
        body_text = str(getattr(resp, "text", "") or "")[:500]
    except Exception:
        body_text = ""
    payload: dict[str, Any] = {}
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            payload = parsed
    except Exception:
        payload = {}

    if status_code >= 400:
        err = _nano_error_text(payload, body_text, status_code)
        logger.warning(
            "openai_compatible_chat failed model=%s status=%s err=%s",
            model,
            status_code,
            err[:240],
        )
        record_provider_degraded(
            "nanogpt",
            f"OpenAI-compatible chat failed model={model} status={status_code}: {err[:180]}",
            allowed_use="llm",
            source_url=base_url,
        )
        return False, "", {
            "error": err,
            "status": status_code,
            "model": model,
        }

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, "", payload or {"error": "empty", "model": model, "status": status_code}
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return True, msg["content"], payload
    if isinstance(choices[0], dict) and isinstance(choices[0].get("text"), str):
        return True, choices[0]["text"], payload
    return False, "", payload or {"error": "empty", "model": model, "status": status_code}


def nanogpt_chat_completion(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> tuple[bool, str, dict[str, Any]]:
    key = nanogpt_api_key()
    if not key:
        return False, "", {}
    m = (model or nanogpt_default_model()).strip()
    return openai_compatible_chat(
        base_url=nanogpt_base_url(),
        api_key=key,
        model=m,
        messages=messages,
        temperature=temperature,
        timeout=timeout,
    )


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from LLM output; strips ```json fences if present."""
    raw = (text or "").strip()
    if not raw:
        return None
    if "```" in raw:
        for part in raw.split("```"):
            chunk = part.strip()
            if chunk.lower().startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                raw = chunk
                break
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None
