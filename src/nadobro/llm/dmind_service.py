"""Finance-specialist LLM layer: NanoGPT (preferred) or DMind.

Generic LLMs may provide style and explanation elsewhere, but structured
market recommendations should pass through this service or clearly disclose
degraded mode when no finance provider is configured.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from src.nadobro.llm.nanogpt_client import nanogpt_chat_completion, nanogpt_is_configured
from src.nadobro.connectors.provider_config import dmind_configured
from src.nadobro.llm.provider_runtime import post_json_with_retries, provider_timeout_seconds
from src.nadobro.connectors.source_registry import record_source

logger = logging.getLogger(__name__)


DMIND_BASE_URL = os.environ.get("DMIND_BASE_URL", "https://api.dmind.ai").rstrip("/")
DMIND_MODEL = os.environ.get("DMIND_MODEL", "dmind-finance")
DMIND_TIMEOUT_SECONDS = provider_timeout_seconds("dmind", 20)
NANOGPT_FINANCE_TIMEOUT = provider_timeout_seconds("nanogpt_finance", 45)


class DMindUnavailable(RuntimeError):
    pass


def is_dmind_configured() -> bool:
    return dmind_configured()


def is_finance_expert_configured() -> bool:
    return nanogpt_is_configured() or is_dmind_configured()


def _dmind_headers() -> dict[str, str]:
    key = os.environ.get("DMIND_API_KEY", "")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _extract_text(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    if isinstance(payload.get("text"), str):
        return payload["text"]
    if isinstance(payload.get("answer"), str):
        return payload["answer"]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
        if isinstance(choices[0].get("text"), str):
            return choices[0]["text"]
    return json.dumps(payload)[:4000]


def _analyze_via_nanogpt(
    prompt: str,
    *,
    context: str,
    task: str,
    schema_hint: dict[str, Any] | None,
) -> dict[str, Any]:
    # Finance/analyst reasoning defaults to Claude Opus 4.8 on NanoGPT (one key).
    # DECISION (2026-08-26): NanoGPT RETIRED dmind/dmind-1 (HTTP 400 "Model is
    # disabled"), and DMind-3 has no hosted inference provider and is a ~20B+ GPU
    # model — un-runnable on our 1-CPU / 2GB / no-GPU prod box (fly.toml). Opus 4.8
    # is already the proven Market-Call model here and out-reasons a 20B model on
    # structured JSON analysis; this tier is advisory-only and fails open, so a
    # frontier hosted model is the rational choice over self-hosting. Override with
    # NANOGPT_FINANCE_MODEL (sanitized: a trailing "# note" is stripped).
    from src.nadobro.connectors.provider_config import clean_env_value

    model = clean_env_value(os.environ.get("NANOGPT_FINANCE_MODEL")) or "anthropic/claude-opus-4.8"
    user_payload = json.dumps(
        {
            "task": task,
            "prompt": prompt,
            "context": context[:20000],
            "schema_hint": schema_hint or {},
        },
        ensure_ascii=True,
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are Nadobro's finance-native expert. Structure data, summarize market context, "
                "score signals, and give cautious trading views grounded only in the provided context. "
                "Do not invent live prices or events missing from context."
            ),
        },
        {"role": "user", "content": user_payload},
    ]
    started = time.perf_counter()
    ok, text, raw = nanogpt_chat_completion(
        messages,
        model=model,
        temperature=0.1,
        timeout=NANOGPT_FINANCE_TIMEOUT,
    )
    latency_ms = (time.perf_counter() - started) * 1000
    if ok and text.strip():
        record_source(
            "nanogpt",
            ttl_seconds=300,
            confidence=0.88,
            latency_ms=latency_ms,
            source_url="https://nano-gpt.com/api",
            detail=f"NanoGPT finance {task}",
            allowed_use="finance_llm",
            metadata={"model": model},
        )
        return {
            "ok": True,
            "degraded": False,
            "provider": "nanogpt",
            "model": model,
            "text": text.strip(),
            "raw": raw,
            "confidence": 0.88,
        }
    logger.warning("NanoGPT finance request failed or empty: ok=%s", ok)
    record_source(
        "nanogpt",
        ttl_seconds=30,
        confidence=0.0,
        latency_ms=latency_ms,
        source_url="https://nano-gpt.com/api",
        detail="NanoGPT finance request failed",
        allowed_use="finance_llm",
    )
    return {
        "ok": False,
        "degraded": True,
        "provider": "nanogpt",
        "error": "NanoGPT returned no usable text",
        "text": "",
        "confidence": 0.0,
    }


def _analyze_via_dmind(
    prompt: str,
    *,
    context: str,
    task: str,
    schema_hint: dict[str, Any] | None,
) -> dict[str, Any]:
    url = f"{DMIND_BASE_URL}/v1/chat/completions"
    body = {
        "model": DMIND_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Nadobro's finance-native expert model. Structure data, "
                    "summarize market context, score signals, and produce cautious "
                    "trading recommendations grounded only in provided sources."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": task,
                        "prompt": prompt,
                        "context": context[:20000],
                        "schema_hint": schema_hint or {},
                    },
                    ensure_ascii=True,
                ),
            },
        ],
        "temperature": 0.1,
    }
    started = time.perf_counter()
    try:
        resp, latency_ms = post_json_with_retries(
            "dmind",
            url,
            headers=_dmind_headers(),
            json_body=body,
            timeout=DMIND_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        payload = resp.json()
        text = _extract_text(payload)
        record_source(
            "dmind",
            ttl_seconds=300,
            confidence=0.9,
            latency_ms=latency_ms,
            source_url="https://dmind.ai/",
            detail=f"DMind {task}",
            allowed_use="finance_llm",
            metadata={"model": DMIND_MODEL},
        )
        return {
            "ok": True,
            "degraded": False,
            "provider": "dmind",
            "model": DMIND_MODEL,
            "text": text,
            "raw": payload,
            "confidence": 0.9,
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        logger.warning("DMind request failed: %s", exc)
        record_source(
            "dmind",
            ttl_seconds=30,
            confidence=0.0,
            latency_ms=latency_ms,
            source_url="https://dmind.ai/",
            detail="DMind request failed",
            allowed_use="finance_llm",
        )
        return {
            "ok": False,
            "degraded": True,
            "provider": "dmind",
            "error": str(exc),
            "text": "",
            "confidence": 0.0,
        }


def analyze_financial_context(
    prompt: str,
    *,
    context: str = "",
    task: str = "market_analysis",
    schema_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run finance analysis via NanoGPT (if configured) else DMind.

    Returns a stable envelope when neither is configured so callers can
    disclose degraded mode rather than silently falling back.
    """
    if nanogpt_is_configured():
        return _analyze_via_nanogpt(prompt, context=context, task=task, schema_hint=schema_hint)
    if is_dmind_configured():
        return _analyze_via_dmind(prompt, context=context, task=task, schema_hint=schema_hint)

    record_source(
        "finance_llm",
        ttl_seconds=30,
        confidence=0.0,
        source_url="https://nano-gpt.com/api",
        detail="Finance LLM not configured",
        allowed_use="finance_llm",
    )
    return {
        "ok": False,
        "degraded": True,
        "provider": "none",
        "error": "NANOGPT_API_KEY or DMIND_API_KEY not configured",
        "text": "",
        "confidence": 0.0,
    }


def build_degraded_notice() -> str:
    if is_finance_expert_configured():
        return ""
    return (
        "Finance expert LLM is not configured (set NANOGPT_API_KEY for NanoGPT, "
        "or DMIND_API_KEY for DMind); responses that rely on it are in degraded mode."
    )
