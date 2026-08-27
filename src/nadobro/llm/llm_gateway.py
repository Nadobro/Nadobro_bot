"""Single LLM gateway — route all reasoning through NanoGPT (one API key, many
models) with graceful fallback to native XAI/OpenAI when NanoGPT is not
configured.

The user's NanoGPT subscription exposes Claude, GPT-5.5, DMind, etc. under one
key, so every reasoning surface (chat, morning brief, desk parse, edge/market
scan, finance analyst) should point here and select a model per task via env.

NanoGPT is OpenAI-compatible, so ``chat_client()`` returns an ``OpenAI`` SDK
client that is a drop-in for every existing ``.chat.completions.create(...)``
call site — the migration only swaps which client and which model string a
factory returns, never the call itself.

Per-task model env vars (set these to the exact model ids your NanoGPT plan
lists — e.g. ``anthropic/claude-sonnet-5``, ``openai/gpt-5.5``,
``openai/gpt-5-mini``, ``anthropic/claude-opus-4.8``). Values are sanitized, so a stray
trailing ``# note`` is stripped. Leave them UNSET to take the defaults below:

    NANOGPT_MODEL_CHAT      general chat + Ask Nadobro     (anthropic/claude-sonnet-5)
    NANOGPT_MODEL_TA        Market Call synthesizer        (anthropic/claude-opus-4.8)
    NANOGPT_MODEL_WEB       open-web research pack         (openai/gpt-5-mini)
    NANOGPT_MODEL_X         X/Twitter research pack        (x-ai/grok-4-fast)
    NANOGPT_MODEL_FINANCE   finance / analyst reasoning    (anthropic/claude-opus-4.8)
    NANOGPT_MODEL_BRIEF     morning brief / news synthesis (anthropic/claude-sonnet-5)
    NANOGPT_MODEL_INTENT    cheap/fast intent classify     (openai/gpt-5-mini)
    NANOGPT_MODEL_SCAN      edge / market alpha scanning   (anthropic/claude-sonnet-5)
    NANOGPT_MODEL_JSON      cheap/fast JSON extraction     (openai/gpt-5-mini)
    NANOGPT_MODEL           global default when a task is unset
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from openai import OpenAI
except Exception:  # optional in degraded/test environments
    OpenAI = None  # type: ignore

from src.nadobro.connectors.provider_config import nanogpt_api_key
from src.nadobro.llm.nanogpt_client import nanogpt_base_url

_client: Optional["OpenAI"] = None

# task -> (env var, built-in default). Verified NanoGPT ids (2026-07): quality/
# conversational surfaces run Claude Sonnet 5; the cheap/fast structured tasks
# run GPT-5 mini (the prior gpt-4o / gpt-4o-mini defaults are retired). Finance
# stays on the Web3-native DMind. Operators override with any exact id their
# plan exposes.
_TASK_MODEL_ENV: dict[str, tuple[str, str]] = {
    "chat": ("NANOGPT_MODEL_CHAT", "anthropic/claude-sonnet-5"),
    # Market Call user-facing synthesizer. Claude only — Grok/GPT gather
    # research packs, they never write this answer. Operators override with
    # the exact Claude id their NanoGPT plan lists; the call site falls down
    # the Claude chain on model_not_supported.
    "ta": ("NANOGPT_MODEL_TA", "anthropic/claude-opus-4.8"),
    "web": ("NANOGPT_MODEL_WEB", "openai/gpt-5-mini"),
    "x": ("NANOGPT_MODEL_X", "x-ai/grok-4-fast"),
    "finance": ("NANOGPT_MODEL_FINANCE", "anthropic/claude-opus-4.8"),
    "brief": ("NANOGPT_MODEL_BRIEF", "anthropic/claude-sonnet-5"),
    "intent": ("NANOGPT_MODEL_INTENT", "openai/gpt-5-mini"),
    "scan": ("NANOGPT_MODEL_SCAN", "anthropic/claude-sonnet-5"),
    "json": ("NANOGPT_MODEL_JSON", "openai/gpt-5-mini"),
    # Second structured-output route. NanoGPT fronts xAI too, so a model-level
    # failure no longer needs a DIRECT OpenAI call to recover — the retry stays
    # inside the one gateway (one key, one bill, one rate limit). Deliberately a
    # different vendor from "json" so a vendor-side outage is actually covered.
    "json_fallback": ("NANOGPT_MODEL_JSON_FALLBACK", "x-ai/grok-4-fast"),
    # Embeddings for the knowledge/vector store. NanoGPT is OpenAI-compatible, so
    # /v1/embeddings works when the plan exposes an embedding model; vector_store
    # probes once and falls back to native OpenAI if it does not.
    "embed": ("NANOGPT_MODEL_EMBED", "openai/text-embedding-3-small"),
}


def gateway_configured() -> bool:
    """True when NanoGPT is the active reasoning provider."""
    return bool(nanogpt_api_key()) and OpenAI is not None


def _timeout() -> float:
    try:
        from src.nadobro.llm.provider_runtime import provider_timeout_seconds

        return provider_timeout_seconds("nanogpt", 90)
    except Exception:
        return 90.0


def chat_client() -> Optional["OpenAI"]:
    """OpenAI-SDK client pointed at NanoGPT. ``None`` when not configured, so
    callers fall back to their native XAI/OpenAI client."""
    global _client
    if _client is not None:
        return _client
    if not gateway_configured():
        return None
    _client = OpenAI(
        api_key=nanogpt_api_key(),
        base_url=nanogpt_base_url(),
        timeout=_timeout(),
    )
    return _client


def model_for(task: str, fallback: Optional[str] = None) -> str:
    """Resolve the NanoGPT model id for a task, honoring the per-task env var,
    then the global ``NANOGPT_MODEL`` override, then the built-in default.

    Env values are sanitized (``clean_env_value``) so a model id pasted with a
    trailing ``# note``/description — the exact failure that made the edge
    scanner send ``"...  # edge/market scan"`` and get 400 model_not_supported —
    is stripped back to the bare model id."""
    from src.nadobro.connectors.provider_config import clean_env_value

    env, default = _TASK_MODEL_ENV.get(task, ("", ""))
    if env:
        val = clean_env_value(os.environ.get(env))
        if val:
            return val
    global_default = clean_env_value(os.environ.get("NANOGPT_MODEL"))
    return default or global_default or fallback or "anthropic/claude-sonnet-5"


# Claude-only fallbacks for the Market Call synthesizer. Never GPT/Grok.
# Sonnet 5 sits second: it is the working chat default on this NanoGPT plan,
# so a 403/400 on opus-4.8 recovers instead of walking older Claude ids first.
TA_MODEL_FALLBACKS: tuple[str, ...] = (
    "anthropic/claude-opus-4.8",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4",
    "anthropic/claude-sonnet-4",
)


def ta_model_candidates() -> list[str]:
    """Primary ``ta`` model then lower Claude versions, de-duplicated.

    Non-Claude ids are dropped so a mistaken env override cannot send the
    user-facing Market Call to GPT or Grok.
    """
    primary = model_for("ta")
    out: list[str] = []
    for mid in (primary, *TA_MODEL_FALLBACKS):
        if mid and mid not in out and "claude" in mid.lower():
            out.append(mid)
    return out or list(TA_MODEL_FALLBACKS)


def reset_cache() -> None:
    """Test hook — drop the memoized client so env changes take effect."""
    global _client
    _client = None
