"""All LLM reasoning must route through the NanoGPT gateway.

CLAUDE.md: "All LLM calls go through ``llm/llm_gateway.py`` — don't call providers
directly. Exception: Grok X-search stays on the native xAI path."

NanoGPT fronts Claude, GPT, Grok and DMind under a single key, so a direct
provider call bypasses one key, one bill and one rate limit — and silently keeps
working, which is why this needs a test rather than a code review.

Two sanctioned exceptions, both narrow and both asserted below:

1. **Grok X-search.** ``extra_body.search_parameters`` is an xAI-only extension
   with no OpenAI-compatible equivalent, so the live-X search path must hold a
   native xAI client.
2. **Embeddings.** The gateway is tried first and only falls back to native
   OpenAI when the plan exposes no embedding model of the index's width
   (``vector_store._resolve_embed_route``).
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path("src/nadobro")

# Modules allowed to construct a client aimed at a provider other than NanoGPT,
# with the reason. Anything else must go through llm_gateway.chat_client().
_NATIVE_CLIENT_ALLOWLIST = {
    # Grok X-search (xAI-only extra_body.search_parameters)
    "llm/knowledge_service.py": "native xAI client for the live-X search path",
    "llm/edge_scanner.py": "native xAI client for the X-search scan",
    "market_data/market_scanner.py": "native xAI client for X sentiment search",
    # Gateway-first with a native fallback only when NanoGPT is unconfigured
    "llm/bro_llm.py": "gateway-first accessors; native only without a NanoGPT key",
    "llm/vector_store.py": "embeddings: gateway probed first, OpenAI fallback",
    # The gateway itself
    "llm/llm_gateway.py": "this IS the gateway",
}


def _py_files():
    return sorted(p for p in SRC.rglob("*.py"))


def test_no_unsanctioned_direct_provider_client():
    """No new module may construct an OpenAI/xAI/Anthropic client directly."""
    offenders = []
    for path in _py_files():
        rel = str(path.relative_to(SRC))
        if rel in _NATIVE_CLIENT_ALLOWLIST:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in {"OpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic"}:
                offenders.append(f"{rel}:{node.lineno} builds {name}() directly")
    assert not offenders, (
        "route these through llm_gateway.chat_client(), or add an explicit "
        "exception to _NATIVE_CLIENT_ALLOWLIST with a reason:\n  "
        + "\n  ".join(offenders)
    )


def test_a_native_xai_client_is_either_x_search_or_gateway_guarded():
    """Pinning ``api.x.ai`` is only legitimate for two reasons: it serves the
    xAI-only X-search extension, or it sits BEHIND a ``chat_client()`` attempt so
    it can only be reached when NanoGPT is unconfigured. A native client that is
    neither is general reasoning routed around the gateway."""
    for path in _py_files():
        text = path.read_text()
        if "api.x.ai" not in text:
            continue
        is_x_search = "search_parameters" in text
        is_gateway_guarded = "chat_client(" in text
        assert is_x_search or is_gateway_guarded, (
            f"{path.relative_to(SRC)} pins api.x.ai with neither an X-search call "
            "nor a gateway-first accessor — general reasoning must use NanoGPT"
        )


def test_chat_json_recovers_inside_the_gateway_not_via_direct_openai():
    """With NanoGPT configured, the structured-output fallback must be a second
    NanoGPT model. The old code appended a direct-OpenAI provider unconditionally,
    so the first gateway hiccup sent live traffic to api.openai.com."""
    import inspect

    from src.nadobro.llm import bro_llm

    src = inspect.getsource(bro_llm.chat_json)
    gw_branch = src.split("else:")[0]
    assert "json_fallback" in gw_branch, "gateway branch needs a NanoGPT fallback model"
    assert "_get_openai_client" not in gw_branch, (
        "the gateway branch must not reach for a direct OpenAI client"
    )


def test_gateway_exposes_a_distinct_vendor_for_the_json_fallback():
    """A fallback on the same vendor as the primary does not survive a vendor
    outage, which is the failure the fallback exists for."""
    from src.nadobro.llm.llm_gateway import model_for

    primary = model_for("json")
    fallback = model_for("json_fallback")
    assert fallback and fallback != primary
    assert primary.split("/")[0] != fallback.split("/")[0], (
        f"json={primary} and json_fallback={fallback} share a vendor prefix"
    )


def test_embedding_route_refuses_a_width_that_would_corrupt_the_index():
    """The Pinecone index is built at EMBEDDING_DIMENSION. A gateway model of a
    different width must be rejected, not mixed in — mixed widths make every
    similarity score meaningless."""
    from unittest.mock import MagicMock, patch

    from src.nadobro.llm import vector_store as vs

    class _Resp:
        def __init__(self, width):
            self.data = [MagicMock(embedding=[0.0] * width, index=0)]

    wrong = MagicMock()
    wrong.embeddings.create.return_value = _Resp(vs.EMBEDDING_DIMENSION // 2)
    native = MagicMock()

    vs.reset_embed_route()
    with patch("src.nadobro.llm.llm_gateway.chat_client", return_value=wrong), \
         patch.object(vs, "_get_openai_client", return_value=native):
        client, model = vs._resolve_embed_route()

    assert client is native, "a wrong-width gateway model must not be adopted"
    assert model == vs.EMBEDDING_MODEL
    vs.reset_embed_route()


def test_embedding_route_uses_the_gateway_when_the_width_matches():
    from unittest.mock import MagicMock, patch

    from src.nadobro.llm import vector_store as vs

    class _Resp:
        def __init__(self, width):
            self.data = [MagicMock(embedding=[0.0] * width, index=0)]

    good = MagicMock()
    good.embeddings.create.return_value = _Resp(vs.EMBEDDING_DIMENSION)

    vs.reset_embed_route()
    with patch("src.nadobro.llm.llm_gateway.chat_client", return_value=good), \
         patch.object(vs, "_get_openai_client", return_value=MagicMock()):
        client, _ = vs._resolve_embed_route()

    assert client is good
    vs.reset_embed_route()
