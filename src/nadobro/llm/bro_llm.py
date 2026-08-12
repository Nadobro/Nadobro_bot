import os
import json
import logging
from typing import Optional

try:
    from openai import OpenAI
except Exception:  # optional in degraded/fallback environments
    OpenAI = None  # type: ignore

logger = logging.getLogger(__name__)

_xai_client: Optional[OpenAI] = None
_openai_client: Optional[OpenAI] = None

BRO_DECISION_MODEL = os.environ.get("BRO_DECISION_MODEL", "grok-3")
BRO_SCAN_MODEL = os.environ.get("BRO_SCAN_MODEL", "grok-3-mini-fast")


def _llm_timeout_seconds() -> float:
    try:
        from src.nadobro.llm.provider_runtime import provider_timeout_seconds

        return provider_timeout_seconds("bro_llm", 45)
    except Exception:
        return 45.0


def _get_client() -> Optional[OpenAI]:
    # Prefer the NanoGPT gateway (one key: Claude / GPT / DMind) when configured;
    # fall back to native Grok only when NanoGPT is absent.
    try:
        from src.nadobro.llm.llm_gateway import chat_client

        gw = chat_client()
        if gw is not None:
            return gw
    except Exception:  # policy: degrade-ok(fall back to native xai)
        pass
    global _xai_client
    if _xai_client:
        return _xai_client
    if OpenAI is None:
        return None
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        return None
    _xai_client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1", timeout=_llm_timeout_seconds())
    return _xai_client


def _get_openai_client() -> Optional[OpenAI]:
    """Gateway-first, exactly like ``_get_client``. A DIRECT OpenAI client is only
    built when NanoGPT is unconfigured, so no caller can accidentally route real
    traffic around the gateway (matches knowledge_service's accessor)."""
    try:
        from src.nadobro.llm.llm_gateway import chat_client

        gw = chat_client()
        if gw is not None:
            return gw
    except Exception:  # policy: degrade-ok(fall back to native openai)
        pass
    global _openai_client
    if _openai_client:
        return _openai_client
    if OpenAI is None:
        return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    _openai_client = OpenAI(api_key=api_key, timeout=_llm_timeout_seconds())
    return _openai_client


def chat_json(messages: list[dict], schema: dict | None = None, model: str | None = None) -> tuple[dict, str]:
    """Provider-selected JSON chat used by features that need structured LLM output.

    NanoGPT gateway is primary (one key, per-task model); native Grok / OpenAI
    remain fallbacks for when NanoGPT is not configured or is down.
    """
    from src.nadobro.llm.llm_gateway import gateway_configured, model_for

    providers: list[tuple[str, Optional[OpenAI], str]] = []
    if gateway_configured():
        primary = model or model_for("json")
        providers.append(("nanogpt", _get_client(), primary))
        # Recover INSIDE the gateway: a second NanoGPT model (different vendor)
        # rather than a direct OpenAI call. Previously the ("openai", ...) entry
        # below was appended unconditionally, so the first gateway hiccup sent
        # real traffic straight to api.openai.com — around the one key, bill and
        # rate limit the gateway exists to centralise.
        secondary = model_for("json_fallback")
        if secondary and secondary != primary:
            providers.append(("nanogpt-fallback", _get_client(), secondary))
    else:
        # No NanoGPT key: keep the native providers so a gateway-less deployment
        # still works rather than losing structured output entirely.
        providers.append(("grok", _get_client(), model or os.environ.get("NADO_LLM_XAI_MODEL", "grok-3-mini-fast")))
        providers.append(("openai", _get_openai_client(), os.environ.get("NADO_LLM_OPENAI_MODEL", "gpt-4o")))
    last_error: Exception | None = None
    for provider, client, selected_model in providers:
        if client is None:
            continue
        for attempt in range(2 if provider.startswith(("grok", "nanogpt")) else 1):
            try:
                kwargs = {
                    "model": selected_model,
                    "messages": messages,
                    "temperature": 0,
                }
                if provider == "openai":
                    kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or "{}"
                try:
                    parsed = json.loads(content)
                except (ValueError, TypeError):
                    # Only the direct-OpenAI entry can ask for response_format,
                    # so a gateway model may fence or preface its JSON. Salvage
                    # the first balanced object instead of discarding a good
                    # answer and burning the fallback route.
                    from src.nadobro.llm.nanogpt_client import extract_json_object

                    salvaged = extract_json_object(content)
                    if salvaged is None:
                        raise
                    parsed = salvaged
                logger.info("chat_json provider=%s", provider)
                return parsed, provider
            except Exception as e:
                last_error = e
                logger.warning(
                    "chat_json failed provider=%s attempt=%s: %s",
                    provider,
                    attempt + 1,
                    e,
                )
                continue
    raise RuntimeError(f"No LLM provider returned valid JSON: {last_error}")



def _format_positions(positions: list[dict]) -> str:
    if not positions:
        return "None"
    parts = []
    for p in positions:
        product = p.get("product", "?")
        side = p.get("side", "?")
        notional = p.get("notional_usd", 0)
        pnl = p.get("unrealized_pnl", 0)
        entry = p.get("entry_price", 0)
        parts.append(f"{product} {side.upper()} ${notional:.0f} entry=${entry:,.2f} PnL=${pnl:+.2f}")
    return " | ".join(parts)


def explain_position(
    product: str,
    side: str,
    entry_price: float,
    current_price: float,
    pnl: float,
    entry_reasoning: str,
    entry_signals: list[str],
) -> Optional[str]:
    client = _get_client()
    if not client:
        return None

    prompt = (
        f"Explain why the Alpha Agent is holding this position in plain language (2-3 sentences):\n\n"
        f"Position: {product} {side.upper()} from ${entry_price:,.2f} (now ${current_price:,.2f}, PnL=${pnl:+.2f})\n"
        f"Entry reasoning: {entry_reasoning}\n"
        f"Entry signals: {', '.join(entry_signals)}\n\n"
        f"Explain the thesis, current status, and what would trigger an exit."
    )

    try:
        response = client.chat.completions.create(
            model=BRO_SCAN_MODEL,
            messages=[
                {"role": "system", "content": "You are Bro, an autonomous trading agent. Explain positions clearly and concisely."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
            temperature=0.2,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error("Position explanation failed: %s", e)
        return None


def generate_game_plan(
    products: list[str],
    budget: float,
    remaining: float,
    positions: list[dict],
    bro_profile: str,
    recent_decisions: list[dict],
) -> Optional[str]:
    client = _get_client()
    if not client:
        return None

    from src.nadobro.trading.budget_guard import get_bro_profile
    profile_data = get_bro_profile(bro_profile)

    positions_text = _format_positions(positions)
    recent_text = ""
    for d in recent_decisions[-10:]:
        recent_text += f"  {d.get('action','?')} {d.get('product','?')} conf={d.get('confidence',0):.0%} — {d.get('reasoning','')[:80]}\n"

    prompt = (
        f"Generate Bro's 24-hour game plan (3-5 bullet points):\n\n"
        f"Profile: {bro_profile.upper()} — {profile_data.get('description', '')}\n"
        f"Assets: {', '.join(products)}\n"
        f"Budget: ${budget:.0f} | Remaining: ${remaining:.0f}\n"
        f"Open positions: {positions_text}\n"
        f"Recent decisions:\n{recent_text or '  None yet'}\n\n"
        f"Include: target risk range ($), key assets to watch, conditions for emergency flatten, "
        f"and what would trigger new entries. Be concise and actionable."
    )

    try:
        response = client.chat.completions.create(
            model=BRO_SCAN_MODEL,
            messages=[
                {"role": "system", "content": "You are Bro, an autonomous trading agent. Provide a clear, concise trading plan."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        logger.error("Game plan generation failed: %s", e)
        return None


def analyze_for_howl(
    trade_history: list[dict],
    current_settings: dict,
    performance_metrics: dict,
) -> Optional[dict]:
    client = _get_client()
    if not client:
        return None

    system = """You are HOWL, the nightly optimization engine for Alpha Agent autonomous trading.

Analyze the past 24 hours of trading performance and suggest parameter adjustments.

Current settings:
{settings}

Performance metrics:
{metrics}

Recent trades:
{trades}

Suggest specific parameter changes with clear rationale. Focus on:
1. Risk level adjustment (conservative/balanced/aggressive)
2. Confidence threshold tuning
3. TP/SL optimization based on actual win rate and avg P&L
4. Product selection (which assets to focus on)
5. Leverage adjustments
6. Cycle timing
7. Bro profile adjustment (chill/normal/degen)

RESPOND WITH VALID JSON:
{{
  "suggestions": [
    {{
      "parameter": "parameter_name",
      "current_value": "current",
      "suggested_value": "new",
      "rationale": "why this change will improve performance",
      "expected_impact": "what improvement to expect"
    }}
  ],
  "overall_assessment": "1-2 sentence summary of performance",
  "confidence": 0.0 to 1.0
}}"""

    trades_text = ""
    for t in trade_history[-20:]:
        product = t.get("product_name", "?")
        side = t.get("side", "?")
        pnl = t.get("pnl", 0)
        trades_text += f"  {product} {side} PnL={pnl:+.2f}\n"

    settings_text = json.dumps(current_settings, indent=2)
    metrics_text = json.dumps(performance_metrics, indent=2)

    prompt = system.format(
        settings=settings_text,
        metrics=metrics_text,
        trades=trades_text or "  No trades in period",
    )

    try:
        response = client.chat.completions.create(
            model=BRO_DECISION_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "Run nightly HOWL analysis and suggest optimizations."},
            ],
            max_tokens=800,
            temperature=0.3,
        )

        raw = response.choices[0].message.content or ""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()

        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        return None
    except Exception as e:
        logger.error("HOWL analysis failed: %s", e)
        return None
