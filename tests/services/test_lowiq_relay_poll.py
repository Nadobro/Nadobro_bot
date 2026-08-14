import asyncio

from _stubs import install_test_stubs

install_test_stubs()


def test_default_poll_timeout_is_under_interval(monkeypatch):
    from src.nadobro.users import lowiq_relay_client as client

    monkeypatch.delenv("LOWIQPTS_RELAY_POLL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("LOWIQPTS_RELAY_POLL_SECONDS", "2")
    timeout = client.relay_poll_timeout_seconds()
    assert 0.5 <= timeout < client.relay_poll_interval_seconds()
    assert timeout <= 1.5


def test_poll_events_passes_short_timeout(monkeypatch):
    from src.nadobro.users import lowiq_relay_client as client

    captured = {}

    async def _fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["timeout"] = kwargs.get("timeout")
        captured["params"] = kwargs.get("params")
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)
    monkeypatch.setenv("LOWIQPTS_RELAY_POLL_SECONDS", "2")
    monkeypatch.delenv("LOWIQPTS_RELAY_POLL_TIMEOUT_SECONDS", raising=False)

    asyncio.run(client.poll_events(session_id="sess_a", cursor="9"))

    assert captured["method"] == "GET"
    assert captured["path"] == "/events/poll"
    assert captured["timeout"] < 2
    assert captured["params"]["session_id"] == "sess_a"
    assert captured["params"]["cursor"] == "9"


def test_start_session_does_not_use_poll_timeout(monkeypatch):
    from src.nadobro.users import lowiq_relay_client as client

    captured = {}

    async def _fake_request(method, path, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return {"ok": True}

    monkeypatch.setattr(client, "_request", _fake_request)
    asyncio.run(
        client.start_session(
            telegram_user_id=1, chat_id=1, wallet="0x" + "ab" * 20, request_id="r1",
        )
    )
    assert captured["timeout"] is None


def test_relay_poll_is_off_by_default(monkeypatch):
    monkeypatch.delenv("LOWIQPTS_RELAY_POLL_ENABLED", raising=False)
    from src.nadobro.core.feature_flags import lowiqpts_relay_poll_enabled

    assert lowiqpts_relay_poll_enabled() is False


def test_scheduler_poll_is_a_no_op_when_disabled(monkeypatch):
    from src.nadobro.runtime import scheduler as sched

    called = []
    monkeypatch.setattr(
        "src.nadobro.users.points_service.poll_lowiqpts_relay_events",
        lambda _app: called.append(True),
    )
    monkeypatch.delenv("LOWIQPTS_RELAY_POLL_ENABLED", raising=False)
    previous = sched._bot_app
    sched._bot_app = object()
    try:
        asyncio.run(sched.poll_lowiqpts_relay())
    finally:
        sched._bot_app = previous
    assert called == []
