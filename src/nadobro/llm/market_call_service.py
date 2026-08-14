"""Market Call — Claude synthesizer over an evidence pack.

Grok (NanoGPT) gathers X. GPT (NanoGPT) gathers web. Claude writes the
user-facing call and does not search. Analysis never places a trade.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from src.nadobro.llm.conversation_intent import classify_conversation_intent, wants_market_call
from src.nadobro.llm.market_intel import EvidencePack, build_market_data_pack

logger = logging.getLogger(__name__)

_DISCLAIMER = "Not financial advice — probabilistic read, not a guarantee."

MARKET_CALL_SYSTEM = """You are Nadobro's Market Call desk. You write the user-facing answer. You do NOT search the web or X. You do NOT invent candles, consensus, quotes, or tweets.

Today's date: {current_date}
User: {user_name}
{language_instruction}

DONE MEANS DONE
- Answer the question that was asked. If they asked beat-or-miss AND up-or-down, give TWO independent calls. A beat can still be a down tape.
- If they asked only a chart direction, give UP / DOWN / RANGE for the asked horizon first.
- Do not dump a stats card. Do not hedge with "mixed signals, stay nimble."
- Use ONLY numbers and facts in EVIDENCE PACK. If a leg is in missing[], say so and lower confidence. Never fabricate OHLCV, EPS, or sources.

VOICE
- Direct, sharp, desk-like. Short bullets. Telegram-readable.
- No forced slang. No "next step on Nado" unless the pack says the name is tradeable_on_nado.

FORMAT (skip a section if that axis was not asked)
**Call**
- Event (if asked): BEAT / MISS / INLINE — confidence N%
- Price (if asked): UP / DOWN / RANGE for {{horizon}} — confidence N%
**Why** (3-5 bullets from the pack)
**Invalidation / levels** (from pack sr / quote)
**Venue:** which data you actually used
**Based on:** source tags from the pack only
One last line: {disclaimer}

If the pack has a deterministic signal.confidence, start price confidence from that (0-100). Do not raise it above what the timeframes support. Event confidence should reflect how complete event/web/x legs are.
"""


def _current_date() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _lang_instruction() -> str:
    try:
        from src.nadobro.i18n import LANGUAGE_LABELS, get_active_language

        lang = get_active_language()
        if lang == "en":
            return ""
        lang_name = LANGUAGE_LABELS.get(lang, "English")
        return (
            f"Respond entirely in {lang_name}. Keep tickers, numbers, and URLs unchanged."
        )
    except Exception:
        return ""


def _nanogpt_text(task: str, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str:
    from src.nadobro.llm.llm_gateway import model_for
    from src.nadobro.llm.nanogpt_client import nanogpt_chat_completion, nanogpt_is_configured

    if not nanogpt_is_configured():
        return ""
    model = model_for(task)
    ok, text, _raw = nanogpt_chat_completion(messages, model=model, temperature=temperature)
    return (text or "").strip() if ok else ""


def _research_web(pack: EvidencePack) -> dict[str, Any]:
    news_bits = []
    for item in (pack.event.get("news") or [])[:6]:
        if isinstance(item, dict):
            news_bits.append(f"- {item.get('title')} ({item.get('url')})")
    if not pack.symbol and not news_bits:
        return {}
    prompt = (
        "Extract only verifiable facts for this market question from the provided sources. "
        "If sources are thin, say so. Reply JSON: "
        '{"facts": ["..."], "consensus": "...", "prior_reaction": "...", "citations": ["..."]}. '
        "Do not invent numbers that are not in the sources.\n\n"
        f"Question: {pack.question}\nSymbol: {pack.symbol}\n"
        f"FMP event: {json.dumps(pack.event, default=str)[:4000]}\n"
        f"Quote: {json.dumps(pack.quote, default=str)[:1500]}\n"
        f"News:\n" + "\n".join(news_bits)
    )
    text = _nanogpt_text(
        "web",
        [
            {"role": "system", "content": "You are a research assistant. JSON only. No user-facing call."},
            {"role": "user", "content": prompt},
        ],
    )
    if not text:
        return {}
    from src.nadobro.llm.nanogpt_client import extract_json_object

    parsed = extract_json_object(text) or {"raw": text[:2000]}
    parsed["model_task"] = "web"
    return parsed


def _research_x(pack: EvidencePack) -> dict[str, Any]:
    tweets: list[str] = []
    try:
        from src.nadobro.market_data.x_api_client import is_available, search_topic_tweets

        if is_available() and pack.symbol:
            rows = search_topic_tweets(pack.symbol, max_results=12, hours_back=72) or []
            for row in rows[:12]:
                txt = str(row.get("text") or row.get("full_text") or "").strip()
                if txt:
                    tweets.append(txt[:280])
    except Exception as exc:
        logger.debug("X API fetch failed: %s", exc)
    if not tweets and not pack.symbol:
        return {}
    prompt = (
        "Summarize positioning / sentiment on X for this ticker from the tweets. "
        "JSON only: {\"sentiment\": \"bull|bear|mixed|unknown\", \"bullets\": [\"...\"], "
        "\"crowded\": true/false}. If tweets are missing, sentiment=unknown.\n\n"
        f"Symbol: {pack.symbol}\nQuestion: {pack.question}\nTweets:\n"
        + ("\n".join(f"- {t}" for t in tweets) if tweets else "[no tweets fetched]")
    )
    text = _nanogpt_text(
        "x",
        [
            {"role": "system", "content": "You are an X/Twitter research assistant. JSON only. No user-facing call."},
            {"role": "user", "content": prompt},
        ],
    )
    if not text:
        return {"tweets_fetched": len(tweets)} if tweets else {}
    from src.nadobro.llm.nanogpt_client import extract_json_object

    parsed = extract_json_object(text) or {"raw": text[:2000]}
    parsed["tweets_fetched"] = len(tweets)
    parsed["model_task"] = "x"
    return parsed


def enrich_research_legs(pack: EvidencePack) -> EvidencePack:
    """GPT web + Grok X via NanoGPT. Never writes the user answer."""
    try:
        web = _research_web(pack)
        if web:
            pack.web = web
            pack.sources.append("GPT web (NanoGPT)")
    except Exception as exc:
        logger.warning("web research failed: %s", exc)
        pack.missing.append("web")
    try:
        xleg = _research_x(pack)
        if xleg:
            pack.x = xleg
            pack.sources.append("Grok X (NanoGPT)")
        elif "x" not in pack.missing:
            pack.missing.append("x")
    except Exception as exc:
        logger.warning("X research failed: %s", exc)
        pack.missing.append("x")
    pack.sources = list(dict.fromkeys(pack.sources))
    pack.missing = list(dict.fromkeys(pack.missing))
    return pack


def _claude_complete(pack: EvidencePack, user_name: str) -> str:
    from src.nadobro.llm.llm_gateway import ta_model_candidates
    from src.nadobro.llm.nanogpt_client import nanogpt_chat_completion, nanogpt_is_configured

    if not nanogpt_is_configured():
        return _deterministic_fallback(pack)
    system = MARKET_CALL_SYSTEM.format(
        current_date=_current_date(),
        user_name=user_name or "trader",
        language_instruction=_lang_instruction(),
        disclaimer=_DISCLAIMER,
    )
    user = (
        f"User question:\n{pack.question}\n\n"
        f"EVIDENCE PACK (JSON):\n{json.dumps(pack.prompt_json(), default=str)[:14000]}"
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_err = ""
    for model in ta_model_candidates():
        ok, text, raw = nanogpt_chat_completion(messages, model=model, temperature=0.2)
        if ok and (text or "").strip():
            return text.strip()
        err = ""
        if isinstance(raw, dict):
            err = str(raw.get("error") or raw.get("message") or "")[:200]
        last_err = err or "empty"
        logger.warning("TA model %s failed: %s", model, last_err)
        low = last_err.lower()
        if "model_not_supported" not in low and "not found" not in low and "does not exist" not in low:
            break
    logger.warning("Claude Market Call failed, using deterministic fallback: %s", last_err)
    return _deterministic_fallback(pack)


def _deterministic_fallback(pack: EvidencePack) -> str:
    """Honest engine read when Claude is down. Still not a stats dump."""
    sig = (pack.chart or {}).get("signal") or {}
    bias = float(sig.get("bias") or 0)
    conf = float(sig.get("confidence") or 0)
    regime = str(sig.get("regime") or "unknown")
    if bias > 0.15:
        direction = "UP"
    elif bias < -0.15:
        direction = "DOWN"
    else:
        direction = "RANGE"
    conf_pct = int(round(conf * 100))
    lines = []
    if pack.asks_event:
        lines.append("**Call**")
        lines.append("- Event: unknown — earnings data incomplete, no Claude synthesizer")
        if pack.asks_direction:
            lines.append(f"- Price: **{direction}** for {pack.horizon} — confidence {conf_pct}% (engine only)")
    else:
        lines.append("**Call**")
        lines.append(f"- Price: **{direction}** for {pack.horizon} — confidence {conf_pct}%")
    reasons = list(sig.get("reasons") or [])[:3]
    risks = list(sig.get("risks") or [])[:2]
    why = reasons or [f"Regime {regime}, bias {bias:+.2f}."]
    lines.append("**Why**")
    for r in why:
        lines.append(f"- {r}")
    for r in risks:
        lines.append(f"- Risk: {r}")
    sr = (pack.chart or {}).get("sr") or {}
    if sr.get("support") or sr.get("resistance"):
        lines.append(
            f"**Invalidation / levels** support {sr.get('support')} / resistance {sr.get('resistance')}"
        )
    venue = "Nado" if pack.tradeable_on_nado else pack.asset_class
    lines.append(f"**Venue:** {venue} {pack.symbol} ({pack.horizon})")
    if pack.sources:
        lines.append("**Based on:** " + ", ".join(pack.sources[:6]))
    if pack.missing:
        lines.append("Missing: " + ", ".join(pack.missing[:6]))
    lines.append(_DISCLAIMER)
    return "\n".join(lines)


def run_market_call_sync(question: str, *, network: str = "mainnet", user_name: str | None = None) -> str:
    pack = build_market_data_pack(question, network=network)
    pack = enrich_research_legs(pack)
    return _claude_complete(pack, user_name or "trader")


async def stream_market_call(
    text: str,
    telegram_id: int | None = None,
    user_name: str | None = None,
    *,
    network: str = "mainnet",
) -> AsyncIterator[str]:
    from src.nadobro.core.async_utils import run_blocking_llm, run_blocking_sdk

    if telegram_id is not None:
        try:
            from src.nadobro.llm.knowledge_service import _add_to_chat_history, _get_user_network

            network = _get_user_network(telegram_id)
            _add_to_chat_history(telegram_id, "user", text)
        except Exception:
            pass

    pack = await run_blocking_sdk(build_market_data_pack, text, network=network)

    def _finish(p=pack):
        p = enrich_research_legs(p)
        return _claude_complete(p, user_name or "trader")

    answer = await run_blocking_llm(_finish)
    if telegram_id is not None and answer:
        try:
            from src.nadobro.llm.knowledge_service import _add_to_chat_history

            _add_to_chat_history(telegram_id, "assistant", answer)
        except Exception:
            pass
    yield answer


def should_route_market_call(text: str) -> bool:
    return wants_market_call(text) or classify_conversation_intent(text).name in {
        "chart_ta",
        "event_predict",
    }
