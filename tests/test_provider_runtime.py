from _stubs import install_test_stubs

install_test_stubs()


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"choices": [{"message": {"content": "ok"}}]}
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_post_json_with_retries_retries_transient_status(monkeypatch):
    from src.nadobro.llm import provider_runtime

    calls = []

    def _post(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response(status_code=500 if len(calls) == 1 else 200)

    monkeypatch.setattr(provider_runtime.requests, "post", _post)
    monkeypatch.setattr(provider_runtime.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(provider_runtime, "provider_retry_count", lambda _provider: 1)

    resp, latency_ms = provider_runtime.post_json_with_retries(
        "test",
        "https://example.invalid",
        headers={},
        json_body={},
        timeout=3,
    )

    assert resp.status_code == 200
    assert latency_ms >= 0
    assert len(calls) == 2


def test_nanogpt_chat_uses_provider_timeout_and_records_degraded(monkeypatch):
    from src.nadobro.llm import nanogpt_client

    degraded = {}

    def _post_json(*args, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(nanogpt_client, "post_json_with_retries", _post_json)
    monkeypatch.setattr(nanogpt_client, "provider_timeout_seconds", lambda provider, default: 7)
    monkeypatch.setattr(
        nanogpt_client,
        "record_provider_degraded",
        lambda provider, detail, **kwargs: degraded.update({"provider": provider, "detail": detail, **kwargs}),
    )

    ok, text, raw = nanogpt_client.openai_compatible_chat(
        base_url="https://nano.example",
        api_key="key",
        model="model",
        messages=[{"role": "user", "content": "hi"}],
        timeout=30,
    )

    assert ok is False
    assert text == ""
    assert raw.get("error")
    assert "timeout" in str(raw.get("error"))
    assert degraded["provider"] == "nanogpt"
    assert "timeout" in degraded["detail"]


def test_openai_compatible_chat_logs_model_and_status_on_400(monkeypatch):
    from src.nadobro.llm import nanogpt_client

    class _Bad:
        status_code = 400
        text = '{"error":{"message":"model_not_supported","code":"model_not_supported"}}'

        def json(self):
            return {"error": {"message": "model_not_supported", "code": "model_not_supported"}}

    monkeypatch.setattr(
        nanogpt_client,
        "post_json_with_retries",
        lambda *args, **kwargs: (_Bad(), 12.0),
    )
    monkeypatch.setattr(nanogpt_client, "provider_timeout_seconds", lambda provider, default: 7)
    monkeypatch.setattr(nanogpt_client, "record_provider_degraded", lambda *a, **k: None)

    ok, text, raw = nanogpt_client.openai_compatible_chat(
        base_url="https://nano.example",
        api_key="key",
        model="anthropic/claude-opus-4.8",
        messages=[{"role": "user", "content": "hi"}],
        timeout=30,
    )

    assert ok is False
    assert text == ""
    assert raw["status"] == 400
    assert raw["model"] == "anthropic/claude-opus-4.8"
    assert "model_not_supported" in str(raw["error"])
