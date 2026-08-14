"""Trading Bro answer orchestration.

This layer keeps broad educational/analysis chat separate from execution flows
while reusing the existing OpenAI/Grok knowledge pipeline underneath.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from src.nadobro.llm.conversation_intent import classify_conversation_intent, wants_market_call
from src.nadobro.llm.knowledge_service import answer_nado_question, stream_nado_answer


def answer_mode_for_text(text: str) -> str:
    intent = classify_conversation_intent(text)
    if intent.name in {"chart_ta", "event_predict"}:
        return "market_call"
    if intent.name == "learn":
        q = (text or "").lower()
        if any(term in q for term in ("build", "create", "design", "architecture", "implement", "code")):
            return "strategy_design"
        return "educational_guide"
    if intent.name == "debug":
        return "debugging"
    if intent.name in {"market", "quote"}:
        return "market_analysis"
    if intent.name == "product_support":
        return "product_support"
    if intent.name == "casual":
        return "casual"
    return "general_trading_bro"


def build_trading_bro_question(text: str, *, mode: str | None = None) -> str:
    selected_mode = mode or answer_mode_for_text(text)
    return (
        "TRADING BRO ANSWER MODE\n"
        f"Mode: {selected_mode}\n\n"
        "Answer like a cool buddy who trades with the user, not a narrow product-support bot.\n"
        "- Tone: relaxed, helpful, confident, and human. Warm bro energy, not forced degen slang.\n"
        "- If the user is asking to learn/build/debug, do not execute trades or strategies.\n"
        "- Give a clear answer first, then structured sections with practical steps.\n"
        "- For strategy/build questions, cover architecture, logic, risk controls, execution, monitoring, and tests.\n"
        "- Keep Telegram readability high: short headings, concise bullets, useful examples, no filler.\n"
        "- Include sources only when live data, X/social context, quoted docs, or external facts were actually used.\n"
        "- Whenever the question touches markets or trade ideas, close with one '🎯 Actionable Insight: …' line"
        " — one concrete next step, or 'sit out' if there is no edge.\n\n"
        f"User message:\n{text}"
    )


async def stream_trading_bro_answer(
    text: str,
    telegram_id: int | None = None,
    user_name: str | None = None,
    *,
    mode: str | None = None,
) -> AsyncIterator[str]:
    if wants_market_call(text):
        from src.nadobro.llm.market_call_service import stream_market_call

        async for chunk in stream_market_call(text, telegram_id=telegram_id, user_name=user_name):
            yield chunk
        return
    framed = build_trading_bro_question(text, mode=mode)
    async for chunk in stream_nado_answer(framed, telegram_id=telegram_id, user_name=user_name):
        yield chunk


async def answer_trading_bro_question(
    text: str,
    telegram_id: int | None = None,
    user_name: str | None = None,
    *,
    mode: str | None = None,
) -> str:
    if wants_market_call(text):
        from src.nadobro.llm.market_call_service import stream_market_call

        chunks: list[str] = []
        async for chunk in stream_market_call(text, telegram_id=telegram_id, user_name=user_name):
            chunks.append(chunk)
        return "".join(chunks)
    framed = build_trading_bro_question(text, mode=mode)
    return await answer_nado_question(framed, telegram_id=telegram_id, user_name=user_name)
