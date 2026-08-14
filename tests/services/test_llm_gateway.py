"""LLM gateway: NanoGPT-primary routing with per-task model selection and
graceful fallback. Verifies the consolidation contract without any network."""
from __future__ import annotations

import pytest

from src.nadobro.llm import llm_gateway


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Start from a known-empty provider env each test.
    for var in (
        "NANOGPT_API_KEY", "NANO_GPT_API_KEY", "NANOGPT_MODEL",
        "NANOGPT_MODEL_CHAT", "NANOGPT_MODEL_FINANCE", "NANOGPT_MODEL_JSON",
        "NANOGPT_MODEL_SCAN", "XAI_API_KEY", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    llm_gateway.reset_cache()
    yield
    llm_gateway.reset_cache()


def test_gateway_off_without_key():
    assert llm_gateway.gateway_configured() is False
    assert llm_gateway.chat_client() is None


def test_gateway_on_with_key(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-test")
    assert llm_gateway.gateway_configured() is True
    client = llm_gateway.chat_client()
    assert client is not None
    # Points at NanoGPT, not x.ai / openai.
    assert "nano-gpt.com" in str(client.base_url)


def test_model_for_precedence(monkeypatch):
    # built-in defaults (verified NanoGPT ids)
    assert llm_gateway.model_for("finance") == "dmind/dmind-1"
    assert llm_gateway.model_for("chat") == "anthropic/claude-sonnet-5"
    assert llm_gateway.model_for("ta") == "anthropic/claude-opus-4.8"
    assert llm_gateway.model_for("web") == "openai/gpt-5-mini"
    assert llm_gateway.model_for("x") == "x-ai/grok-4-fast"
    assert llm_gateway.model_for("intent") == "openai/gpt-5-mini"
    # a per-task env var beats the built-in default.
    monkeypatch.setenv("NANOGPT_MODEL_FINANCE", "dmind/dmind-3")
    monkeypatch.setenv("NANOGPT_MODEL_CHAT", "openai/gpt-5.5")
    monkeypatch.setenv("NANOGPT_MODEL_TA", "anthropic/claude-sonnet-5")
    assert llm_gateway.model_for("finance") == "dmind/dmind-3"
    assert llm_gateway.model_for("chat") == "openai/gpt-5.5"
    assert llm_gateway.model_for("ta") == "anthropic/claude-sonnet-5"


def test_unknown_task_falls_back(monkeypatch):
    monkeypatch.setenv("NANOGPT_MODEL", "gpt-5.5")
    assert llm_gateway.model_for("does-not-exist") == "gpt-5.5"


def test_model_env_inline_comment_is_stripped(monkeypatch):
    # Regression: an operator pasted the model with the docs description attached
    # ("NANOGPT_MODEL_SCAN=chatgpt-4o-latest         # edge/market scan"), which
    # reached NanoGPT verbatim -> 400 model_not_supported. The gateway must
    # sanitize the value back to the bare model id.
    monkeypatch.setenv("NANOGPT_MODEL_SCAN", "chatgpt-4o-latest         # edge/market scan")
    assert llm_gateway.model_for("scan") == "chatgpt-4o-latest"
    monkeypatch.setenv("NANOGPT_MODEL", "gpt-5.5   # global default")
    assert llm_gateway.model_for("does-not-exist") == "gpt-5.5"


def test_base_url_and_key_inline_comment_is_stripped(monkeypatch):
    from src.nadobro.connectors import provider_config as pc
    # A base URL pasted with a trailing "# note" was the "no host specified"
    # failure — the OpenAI client got an unparseable host.
    monkeypatch.setenv("NANOGPT_BASE_URL", "https://nano-gpt.com/api/v1   # base url")
    assert pc.nanogpt_base_url() == "https://nano-gpt.com/api/v1"
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-live-abc   # my key")
    assert pc.nanogpt_api_key() == "sk-live-abc"
    # A bare '#' with no leading space (e.g. inside a token) is preserved.
    assert pc.clean_env_value("model#weird") == "model#weird"
    assert pc.clean_env_value(None) == ""


def test_bro_llm_prefers_gateway(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-test")
    monkeypatch.setenv("XAI_API_KEY", "xai-should-not-win")
    llm_gateway.reset_cache()
    import src.nadobro.llm.bro_llm as bro_llm
    client = bro_llm._get_client()
    assert client is not None and "nano-gpt.com" in str(client.base_url)


def test_knowledge_service_general_gateway_but_xsearch_native(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-test")
    monkeypatch.setenv("XAI_API_KEY", "xai-native")
    llm_gateway.reset_cache()
    import src.nadobro.llm.knowledge_service as ks
    ks._xai_client = None
    ks._openai_client = None
    # General reasoning -> gateway; model resolves to the gateway chat model.
    assert "nano-gpt.com" in str(ks._get_xai_client().base_url)
    assert ks._model_for("xai") == llm_gateway.model_for("chat")
    # X-search stays on native Grok (its extra_body is Grok-only).
    assert "x.ai" in str(ks._get_native_xai_client().base_url)


def test_knowledge_service_no_native_xai_without_key(monkeypatch):
    monkeypatch.setenv("NANOGPT_API_KEY", "sk-test")
    llm_gateway.reset_cache()
    import src.nadobro.llm.knowledge_service as ks
    ks._xai_client = None
    # No XAI key -> no native client -> X-search path disabled, general still works.
    assert ks._get_native_xai_client() is None
    assert ks._get_xai_client() is not None


def test_ta_candidates_are_claude_only(monkeypatch):
    monkeypatch.setenv("NANOGPT_MODEL_TA", "anthropic/claude-opus-4.8")
    cands = llm_gateway.ta_model_candidates()
    assert cands[0] == "anthropic/claude-opus-4.8"
    assert all("claude" in m for m in cands)
    assert all("gpt" not in m and "grok" not in m and "x-ai" not in m for m in cands)


def test_ta_candidates_drop_non_claude_override(monkeypatch):
    monkeypatch.setenv("NANOGPT_MODEL_TA", "openai/gpt-5.5")
    cands = llm_gateway.ta_model_candidates()
    assert cands[0] == "anthropic/claude-opus-4.8"
    assert all("claude" in m for m in cands)
