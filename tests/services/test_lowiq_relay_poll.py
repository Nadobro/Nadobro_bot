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


def test_request_failures_are_throttled_and_describe_the_exception(monkeypatch, caplog):
    """A wedged relay must not emit one WARNING per 2s poll, and the reason must
    never be blank (httpx timeout/connect errors carry empty args)."""
    import logging
    from src.nadobro.users import lowiq_relay_client as client

    client._fail_streaks.clear()
    monkeypatch.setattr(client, "_FAIL_LOG_INTERVAL_SECONDS", 10_000.0)

    with caplog.at_level(logging.WARNING, logger=client.logger.name):
        # First failure logs once with the exception class, even for empty args.
        client._log_request_failure("GET", "/events/poll", client.httpx.ConnectTimeout(""))
        for _ in range(50):  # a stream of identical failures
            client._log_request_failure("GET", "/events/poll", client.httpx.ConnectTimeout(""))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "streak of failures must collapse to a single WARNING"
    assert "ConnectTimeout" in warnings[0].getMessage()  # reason is never blank
    assert client._fail_streaks["GET /events/poll"]["count"] == 51


def test_request_failure_recovery_is_logged(monkeypatch, caplog):
    import logging
    from src.nadobro.users import lowiq_relay_client as client

    client._fail_streaks.clear()
    client._log_request_failure("GET", "/events/poll", client.httpx.ConnectError(""))
    client._log_request_failure("GET", "/events/poll", client.httpx.ConnectError(""))
    with caplog.at_level(logging.INFO, logger=client.logger.name):
        client._clear_request_failures("GET", "/events/poll")
    assert any("recovered" in r.getMessage() for r in caplog.records)
    assert "GET /events/poll" not in client._fail_streaks
