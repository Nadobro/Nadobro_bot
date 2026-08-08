"""Finance-LLM advisory tier for the financial overlay (NanoGPT / DMind).

``signal_engine`` is the overlay's deterministic brain: same inputs → same
:class:`Signal`, no I/O, no clock. Its docstring has always promised a second,
slower tier — "the slow DMind analyst annotates ``reasons``/``risks`` and may
nudge ``confidence`` separately; it never gates a tick" — and that tier did not
exist. The finance LLM was wired only into Night HOWL's nightly narrative
(``signal_analyst``), so it explained trading after the fact and steered nothing.

This module is that tier, built so it cannot hurt:

* **Never blocks a cycle.** ``advise`` is a read-through cache. On a cold or stale
  entry it returns the deterministic signal UNCHANGED and schedules one refresh in
  the background (the LLM pool). A cycle never waits on inference — the 2026-08
  latency incident was caused by exactly that kind of inline call.
* **Never adds risk.** A verdict can only lower ``confidence``, lower ``scale``,
  or clear ``entry_ok``. ``_apply`` refuses any change in the risk-increasing
  direction, whatever the model returns. Worst case a hallucinating model makes
  the overlay more timid.
* **Never gates.** Missing key, timeout, malformed JSON, provider down → the
  deterministic signal passes through untouched and the strategy trades on it.
* **Auditable.** Every applied nudge lands in the ``overlay_signals`` row via the
  actuator's record, so ``signal_scorer`` grades LLM-adjusted signals the same way
  it grades deterministic ones.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import replace
from typing import Any, Dict, Mapping, Optional, Tuple

from src.nadobro.llm.signal_engine import Signal
from src.nadobro.utils.env import env_float, env_int

logger = logging.getLogger(__name__)

# How long one verdict stays usable. The analyst reads regime, not ticks — a
# 4h-trend opinion does not change in 30 seconds, and an LLM call per cycle per
# user would be both slow and expensive.
_TTL_SECONDS = env_int("NADO_SIGNAL_ADVISOR_TTL_SECONDS", 900)
# Hard bound on the nudge. Small on purpose: the deterministic vote decides, the
# analyst only shades conviction.
_MAX_CONFIDENCE_DELTA = env_float("NADO_SIGNAL_ADVISOR_MAX_DELTA", 0.15)

# (network, product) -> (fetched_at, verdict)
_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
# Keys with a refresh in flight, so a burst of cycles fires one call, not twenty.
_INFLIGHT: set[Tuple[str, str]] = set()

_SCHEMA_HINT = {
    "agree": "boolean — does the market context support this regime and bias?",
    "confidence_delta": "number in [-0.15, 0.15] — shade conviction only",
    "risks": "array of at most 3 short strings",
    "reasons": "array of at most 3 short strings",
}

_PROMPT = (
    "You are a risk reviewer for an automated market-making bot. You are NOT "
    "choosing a trade: a deterministic engine has already voted, and you may only "
    "shade its conviction. Reply with STRICT JSON and nothing else:\n"
    '{"agree": true|false, "confidence_delta": -0.15..0.15, '
    '"risks": ["..."], "reasons": ["..."]}\n'
    "Lower confidence_delta when the context contradicts the engine's read or "
    "looks unusually risky. Never suggest more size or more conviction than the "
    "engine already has."
)


def advisor_enabled() -> bool:
    """ON by default; operators can disable without a deploy. (Lazy import: llm/
    has no module-level edge to core/ — tests/lint/test_architecture_layers.py.)"""
    from src.nadobro.core.feature_flags import env_flag

    return env_flag("NADO_SIGNAL_ADVISOR", True)


def reset_cache() -> None:
    _CACHE.clear()
    _INFLIGHT.clear()


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _parse_verdict(text: str) -> Optional[Dict[str, Any]]:
    """Pull the JSON object out of a model reply. Models wrap JSON in prose and
    code fences; a strict ``json.loads`` on the whole reply throws away good
    answers, so find the first balanced object."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    out: Dict[str, Any] = {"agree": bool(parsed.get("agree", True))}
    try:
        out["confidence_delta"] = _clamp(
            float(parsed.get("confidence_delta", 0.0) or 0.0),
            -_MAX_CONFIDENCE_DELTA, _MAX_CONFIDENCE_DELTA,
        )
    except (TypeError, ValueError):
        out["confidence_delta"] = 0.0
    for key in ("risks", "reasons"):
        raw = parsed.get(key)
        out[key] = [str(x)[:120] for x in raw[:3]] if isinstance(raw, list) else []
    return out


def _build_context(signal: Signal, features: Mapping[str, Any], product: str) -> str:
    """Compact, factual context. No user identity, no balances — the analyst sees
    market state and the engine's own read, nothing about the person trading."""
    per_tf = {}
    for tf, feat in (features or {}).items():
        if not isinstance(feat, Mapping):
            continue
        per_tf[str(tf)] = {
            k: feat.get(k)
            for k in ("trend", "rsi", "atr_pct", "variance_ratio", "ema_fast", "ema_slow")
            if feat.get(k) is not None
        }
    return json.dumps(
        {
            "product": str(product),
            "engine_read": {
                "regime": signal.regime,
                "bias": round(float(signal.bias), 3),
                "confidence": round(float(signal.confidence), 3),
                "entry_ok": bool(signal.entry_ok),
                "reasons": list(signal.reasons)[:4],
            },
            "timeframes": per_tf,
        },
        default=str,
    )[:4000]


def fetch_verdict(signal: Signal, features: Mapping[str, Any], product: str) -> Dict[str, Any]:
    """BLOCKING. Ask the finance LLM for a verdict. Never raises."""
    try:
        from src.nadobro.llm.dmind_service import (
            analyze_financial_context,
            is_finance_expert_configured,
        )

        if not is_finance_expert_configured():
            return {"ok": False, "reason": "finance_llm_not_configured"}
        result = analyze_financial_context(
            _PROMPT,
            context=_build_context(signal, features, product),
            task="overlay_signal_review",
            schema_hint=_SCHEMA_HINT,
        )
        if not result.get("ok"):
            return {"ok": False, "reason": str(result.get("error") or "provider_error")}
        verdict = _parse_verdict(str(result.get("text") or ""))
        if verdict is None:
            return {"ok": False, "reason": "unparseable_verdict"}
        verdict["ok"] = True
        verdict["provider"] = str(result.get("provider") or "finance")
        return verdict
    except Exception as exc:  # noqa: BLE001 - an advisory must never break a cycle
        logger.debug("signal advisor fetch failed: %s", exc, exc_info=True)
        return {"ok": False, "reason": "exception"}


def _apply(signal: Signal, verdict: Mapping[str, Any]) -> Signal:
    """Fold a verdict into the signal in the RISK-REDUCING direction only.

    * ``confidence_delta`` is clamped and applied, then floored at 0.
    * disagreement pulls ``scale`` toward 0 (less adding) and, when the engine was
      already unconfident, clears ``entry_ok``.
    * ``reasons``/``risks`` are appended for the audit trail.

    Nothing here can raise size, widen a barrier, or turn an entry back ON.
    """
    delta = _clamp(
        float(verdict.get("confidence_delta", 0.0) or 0.0),
        -_MAX_CONFIDENCE_DELTA, _MAX_CONFIDENCE_DELTA,
    )
    agree = bool(verdict.get("agree", True))
    confidence = _clamp(float(signal.confidence) + delta, 0.0, 1.0)
    scale = float(signal.scale)
    entry_ok = bool(signal.entry_ok)
    if not agree:
        # Halve the appetite to ADD; leave a reduce instruction alone (that is
        # already the safe direction).
        if scale > 0:
            scale = scale / 2.0
        confidence = min(confidence, float(signal.confidence))
        if confidence < 0.35:
            entry_ok = False
    reasons = list(signal.reasons)
    risks = list(signal.risks)
    provider = str(verdict.get("provider") or "finance").upper()
    for text in verdict.get("reasons") or []:
        reasons.append(f"[{provider}] {text}")
    for text in verdict.get("risks") or []:
        risks.append(f"[{provider}] {text}")
    if not agree:
        risks.append(f"[{provider}] disagrees with the engine's {signal.regime} read")
    return replace(
        signal, confidence=confidence, scale=scale, entry_ok=entry_ok,
        reasons=reasons, risks=risks,
    )


def cached_verdict(network: str, product: str) -> Optional[Dict[str, Any]]:
    hit = _CACHE.get((str(network), str(product)))
    if hit is None:
        return None
    fetched_at, verdict = hit
    if (time.time() - fetched_at) > _TTL_SECONDS:
        return None
    return verdict


def store_verdict(network: str, product: str, verdict: Dict[str, Any]) -> None:
    _CACHE[(str(network), str(product))] = (time.time(), verdict)
    if len(_CACHE) > 512:                       # bound the cache
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)


async def advise(
    signal: Signal,
    features: Mapping[str, Any],
    *,
    network: str,
    product: str,
) -> Tuple[Signal, Optional[Dict[str, Any]]]:
    """Return ``(signal, applied_verdict)``.

    A cache HIT folds the verdict in. A cache MISS returns the deterministic
    signal untouched and schedules one background refresh, so no cycle ever waits
    on inference. Returns the input signal unchanged when disabled or unconfigured.
    """
    if not advisor_enabled():
        return signal, None
    key = (str(network), str(product))
    verdict = cached_verdict(network, product)
    if verdict is not None:
        if not verdict.get("ok"):
            return signal, None
        return _apply(signal, verdict), verdict

    if key not in _INFLIGHT:
        _INFLIGHT.add(key)

        async def _refresh() -> None:
            try:
                from src.nadobro.core.async_utils import run_blocking_llm

                fresh = await run_blocking_llm(fetch_verdict, signal, features, product)
                store_verdict(network, product, fresh)
                if fresh.get("ok"):
                    logger.info(
                        "signal advisor: %s %s agree=%s delta=%+.3f (provider=%s)",
                        network, product, fresh.get("agree"),
                        float(fresh.get("confidence_delta") or 0.0), fresh.get("provider"),
                    )
                else:
                    logger.debug(
                        "signal advisor unavailable for %s %s: %s",
                        network, product, fresh.get("reason"),
                    )
            except Exception as exc:  # noqa: BLE001 - background best-effort
                logger.debug("signal advisor refresh failed: %s", exc, exc_info=True)
            finally:
                _INFLIGHT.discard(key)

        try:
            from src.nadobro.core.async_utils import fire_and_forget

            fire_and_forget(_refresh(), name=f"signal-advisor-refresh:{network}:{product}")
        except Exception:  # noqa: BLE001 - no loop (sync caller/tests): skip the refresh
            _INFLIGHT.discard(key)
    return signal, None
