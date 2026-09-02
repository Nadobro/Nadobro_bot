"""Tap latency guards for the per-user update serialization wrapper.

Production 2026-08-06: ``SLO breach callback.total: p95=4579ms (target 1000ms)
p50=371ms max=84374ms``. Two causes lived here — every tap paid a Telegram
round-trip (``query.answer()``) before any work started, and one slow handler
froze that user's whole UI because the per-user lock had no bound.
"""
import asyncio
from types import SimpleNamespace

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import update_serialization as us


class _FakeQuery:
    def __init__(self, data="nav:main"):
        self.data = data
        self.answered = asyncio.Event()
        self.answer_text = None

    async def answer(self, *_a, text=None, **_k):
        self.answer_text = text
        self.answered.set()


class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **_k):
        self.replies.append(text)


def _update(query=None, message=None, user_id=99):
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
    )


def _reset_state():
    us._user_locks.clear()
    us._busy_notice_sent.clear()


def test_callback_is_acked_before_the_serialization_lock():
    """The spinner must stop even while the user's PREVIOUS action is running."""
    _reset_state()
    release = asyncio.Event()
    query = _FakeQuery()

    async def slow_handler(_update, _context):
        await release.wait()

    wrapped = us.with_callback_ack(us.with_user_serialized(slow_handler))

    async def scenario():
        # Occupy the user's lock with a first, still-running update.
        first = asyncio.create_task(wrapped(_update(_FakeQuery()), None))
        await asyncio.sleep(0)
        # Second tap: blocked on the lock, but must still be acknowledged.
        second = asyncio.create_task(wrapped(_update(query), None))
        await asyncio.wait_for(query.answered.wait(), timeout=1.0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert query.answered.is_set()


def test_points_cancel_is_not_pre_acked():
    """It answers itself with show_alert=True when no request is pending."""
    _reset_state()
    query = _FakeQuery("points:cancel")

    async def handler(_update, _context):
        return "done"

    wrapped = us.with_callback_ack(handler)
    assert asyncio.run(wrapped(_update(query), None)) == "done"
    assert not query.answered.is_set()


def _ack_and_capture(data):
    """Run the pre-ack for one callback and return the toast text it emitted."""
    _reset_state()
    query = _FakeQuery(data)

    async def handler(_update, _context):
        return "ok"

    wrapped = us.with_callback_ack(handler)

    async def scenario():
        await wrapped(_update(query), None)
        await asyncio.wait_for(query.answered.wait(), timeout=1.0)

    asyncio.run(scenario())
    return query.answer_text


def test_action_taps_get_a_present_tense_toast():
    """The Phase 2 win: a tap that does real work says so, instantly."""
    assert _ack_and_capture("exec_trade:abc") == "Placing your order…"
    assert _ack_and_capture("pos:close:BTC") == "Closing position…"
    assert _ack_and_capture("strategy:stop") == "Stopping strategy…"
    assert _ack_and_capture("portfolio:view") == "Loading portfolio…"


def test_unmapped_tap_keeps_the_silent_bare_ack():
    """No regression: an unmapped callback still acks, with no toast text."""
    assert _ack_and_capture("nav:main") is None
    # A sub-flow micro-step must stay silent — toasting each one would be noise.
    assert _ack_and_capture("card:trade:sess-1:size:0.05") is None


def test_confirm_screen_is_not_mislabelled_as_the_action():
    """pos:close_all only OPENS a confirm dialog; it must not say 'Closing…'."""
    assert _ack_and_capture("pos:close_all") is None
    assert _ack_and_capture("pos:confirm_close_all") == "Closing everything…"


def test_toast_resolution_does_no_io():
    """The toast must be a pure map — the pre-ack path stays free of DB/venue.

    A blocking lookup here would reintroduce exactly the round-trip the pre-ack
    was built to remove.
    """
    import src.nadobro.handlers.ui as ui_mod

    src = __import__("inspect").getsource(ui_mod.toast_for)
    for forbidden in ("get_user", "query_", "await", "requests", "get_balance"):
        assert forbidden not in src, f"toast_for must not reference {forbidden!r}"


def test_lock_wait_is_bounded_and_the_user_is_told(monkeypatch):
    """A wedged handler must not silently freeze the user's UI for 84 seconds."""
    _reset_state()
    monkeypatch.setattr(us, "_LOCK_WAIT_SECONDS", 0.05)
    release = asyncio.Event()
    calls = []
    message = _FakeMessage()

    async def slow_handler(_update, _context):
        calls.append("ran")
        await release.wait()

    wrapped = us.with_user_serialized(slow_handler)

    async def scenario():
        first = asyncio.create_task(wrapped(_update(message=_FakeMessage()), None))
        await asyncio.sleep(0)
        result = await wrapped(_update(message=message), None)
        release.set()
        await first
        return result

    result = asyncio.run(scenario())
    assert result is None, "the queued update is dropped rather than replayed late"
    assert calls == ["ran"], "the second handler never ran"
    assert len(message.replies) == 1 and "Still finishing" in message.replies[0]


def test_busy_notice_is_rate_limited(monkeypatch):
    """A tap storm behind a wedged handler gets one notice, not one per tap."""
    _reset_state()
    monkeypatch.setattr(us, "_LOCK_WAIT_SECONDS", 0.02)
    release = asyncio.Event()

    async def slow_handler(_update, _context):
        await release.wait()

    wrapped = us.with_user_serialized(slow_handler)
    message = _FakeMessage()

    async def scenario():
        first = asyncio.create_task(wrapped(_update(message=_FakeMessage()), None))
        await asyncio.sleep(0)
        for _ in range(4):
            await wrapped(_update(message=message), None)
        release.set()
        await first

    asyncio.run(scenario())
    assert len(message.replies) == 1


def test_lock_is_released_after_a_timed_out_waiter(monkeypatch):
    """A cancelled acquire() must not leave the lock held (asyncio.Lock semantics)."""
    _reset_state()
    monkeypatch.setattr(us, "_LOCK_WAIT_SECONDS", 0.02)
    release = asyncio.Event()
    ran = []

    async def slow_handler(_update, _context):
        ran.append("slow")
        await release.wait()

    async def quick_handler(_update, _context):
        ran.append("quick")
        return "ok"

    slow = us.with_user_serialized(slow_handler)
    quick = us.with_user_serialized(quick_handler)

    async def scenario():
        first = asyncio.create_task(slow(_update(message=_FakeMessage()), None))
        await asyncio.sleep(0)
        assert await quick(_update(message=_FakeMessage()), None) is None  # times out
        release.set()
        await first
        # The lock must be free again for the next update.
        return await quick(_update(message=_FakeMessage()), None)

    assert asyncio.run(scenario()) == "ok"
    assert ran == ["slow", "quick"]
