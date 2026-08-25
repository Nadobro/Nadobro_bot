"""P2 fill-nudge: WS fill events trigger immediate strategy cycles.

The nudge deliberately bypasses the interval gate (a fill means the book
changed), relying on the per-user job lock + coalescing for overlap safety
and a 1s in-memory debounce for burst absorption. It must be a no-op for
stopped sessions and for strategies outside the engine-mapped set.
"""
import asyncio

from _stubs import install_test_stubs

install_test_stubs()

import src.nadobro.strategy.bot_runtime as bot_runtime  # noqa: E402
import src.nadobro.venue.nado_ws as nado_ws  # noqa: E402


def _reset_debounce():
    bot_runtime._last_nudge_ts.clear()
    bot_runtime._nudge_skip_until.clear()


def _run_nudge(monkeypatch, state, *, user=42, network="mainnet"):
    """Drive nudge_strategy_cycle inside a running loop; return captured enqueues."""
    captured = []

    async def _fake_enqueue(payload, dedupe_key=None):
        captured.append((payload, dedupe_key))
        return True

    monkeypatch.setattr(bot_runtime, "enqueue_strategy", _fake_enqueue)
    monkeypatch.setattr(bot_runtime, "_load_state", lambda uid, net: dict(state))

    async def body():
        assert bot_runtime.nudge_strategy_cycle(user, network) is True
        await asyncio.sleep(0.05)  # let the scheduled task run

    asyncio.run(body())
    return captured


def test_fill_nudge_enqueues_running_engine_strategy(monkeypatch):
    _reset_debounce()
    captured = _run_nudge(monkeypatch, {"running": True, "strategy": "mid"})
    assert len(captured) == 1
    payload, dedupe = captured[0]
    assert payload["telegram_id"] == 42 and payload["strategy"] == "mid"
    assert "fillnudge" in dedupe
    # AUDIT-MM-2026-07-14 #4: the payload must carry nudge=True — _run_cycle's
    # interval gate uses a 1s min-gap for nudged cycles instead of the full
    # interval (without the flag every nudge died as "skipped_interval").
    assert payload["nudge"] is True


def test_fill_nudge_is_a_noop_when_not_running(monkeypatch):
    _reset_debounce()
    captured = _run_nudge(monkeypatch, {"running": False, "strategy": "mid"})
    assert captured == []


def test_fill_nudge_ignores_non_engine_strategies(monkeypatch):
    _reset_debounce()
    captured = _run_nudge(monkeypatch, {"running": True, "strategy": "bro"})
    assert captured == []


def test_fill_nudge_debounces_bursts(monkeypatch):
    _reset_debounce()
    captured = []

    async def _fake_enqueue(payload, dedupe_key=None):
        captured.append(payload)
        return True

    monkeypatch.setattr(bot_runtime, "enqueue_strategy", _fake_enqueue)
    monkeypatch.setattr(
        bot_runtime, "_load_state", lambda uid, net: {"running": True, "strategy": "mid"}
    )

    async def body():
        first = bot_runtime.nudge_strategy_cycle(42, "mainnet")
        second = bot_runtime.nudge_strategy_cycle(42, "mainnet")  # inside 1s window
        await asyncio.sleep(0.05)
        assert first is True and second is False

    asyncio.run(body())
    assert len(captured) == 1


def test_ws_fill_listener_registry_swallows_listener_errors():
    seen = []

    def _bad(uid, net):
        raise RuntimeError("boom")

    def _good(uid, net):
        seen.append((uid, net))

    nado_ws._fill_listeners.clear()
    nado_ws.register_fill_listener(_bad)
    nado_ws.register_fill_listener(_good)
    nado_ws._notify_fill_listeners(7, "mainnet")  # must not raise
    assert seen == [(7, "mainnet")]
    nado_ws._fill_listeners.clear()


def test_ws_register_is_idempotent():
    nado_ws._fill_listeners.clear()

    def _cb(uid, net):
        return None

    nado_ws.register_fill_listener(_cb)
    nado_ws.register_fill_listener(_cb)
    assert nado_ws._fill_listeners.count(_cb) == 1
    nado_ws._fill_listeners.clear()


def test_not_running_negative_cache_skips_state_reads(monkeypatch):
    """A fill from a user with no running engine strategy must not cost a DB
    state read per fill-burst: the first read caches the negative and later
    nudges inside the window are rejected on the WS loop without IO."""
    _reset_debounce()
    reads = []

    def _counting_load(uid, net):
        reads.append(uid)
        return {"running": False, "strategy": "mid"}

    async def _fake_enqueue(payload, dedupe_key=None):
        return True

    monkeypatch.setattr(bot_runtime, "enqueue_strategy", _fake_enqueue)
    monkeypatch.setattr(bot_runtime, "_load_state", _counting_load)

    async def body():
        assert bot_runtime.nudge_strategy_cycle(42, "mainnet") is True
        await asyncio.sleep(0.05)          # first nudge: reads state, caches negative
        bot_runtime._last_nudge_ts.clear()  # get past the debounce, keep the neg-cache
        assert bot_runtime.nudge_strategy_cycle(42, "mainnet") is False

    asyncio.run(body())
    assert len(reads) == 1


def test_nudge_dicts_are_size_bounded():
    _reset_debounce()
    for i in range(bot_runtime._NUDGE_DICT_MAX + 10):
        bot_runtime._last_nudge_ts[f"k{i}"] = float(i)
    bot_runtime._nudge_prune(bot_runtime._last_nudge_ts)
    assert len(bot_runtime._last_nudge_ts) <= bot_runtime._NUDGE_DICT_MAX
    # Oldest entries were evicted, newest survive.
    assert f"k{bot_runtime._NUDGE_DICT_MAX + 9}" in bot_runtime._last_nudge_ts
    assert "k0" not in bot_runtime._last_nudge_ts
    _reset_debounce()


def test_legacy_dedupe_bucket_honors_supported_turbo_cadence():
    """With the central scheduler disabled, 5s Turbo ticks need distinct queue
    keys; the old fixed 20s bucket deduped all four attempts into one."""
    at_100 = bot_runtime._legacy_strategy_dedupe_bucket(100.0, 5.0)
    at_105 = bot_runtime._legacy_strategy_dedupe_bucket(105.0, 5.0)
    assert at_100 != at_105
    assert int(100.0 / bot_runtime.RUNTIME_TICK_SECONDS) == int(
        105.0 / bot_runtime.RUNTIME_TICK_SECONDS
    )


def _install_coalescing_fakes(monkeypatch, strategy):
    """Return (calls, started, release) for handle_strategy_job overlap tests."""
    import src.nadobro.core.user_circuit as user_circuit
    import src.nadobro.runtime.runtime_supervisor as runtime_supervisor

    calls = []
    started = asyncio.Event()
    release = asyncio.Event()
    state = {"running": True, "strategy": strategy}

    monkeypatch.setattr(bot_runtime, "_load_state", lambda _uid, _net: dict(state))
    monkeypatch.setattr(bot_runtime, "_save_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_supervisor, "is_multiprocess_enabled", lambda: False)
    monkeypatch.setattr(user_circuit, "is_open", lambda *_args: False)
    monkeypatch.setattr(user_circuit, "record_success", lambda *_args: None)

    async def _fake_run(_uid, _net, _state, *, nudge=False, safety_only=False):
        calls.append(bool(nudge))
        if len(calls) == 1:
            started.set()
            await release.wait()
        return True, None

    monkeypatch.setattr(bot_runtime, "_run_cycle", _fake_run)
    bot_runtime._job_locks.clear()
    bot_runtime._job_pending_payloads.clear()
    bot_runtime._job_coalesce_counts.clear()
    return calls, started, release


def test_coalesced_fill_keeps_nudge_flag_for_deferred_cycle(monkeypatch):
    """A fill arriving during a normal tick must bypass the interval gate on
    the deferred tick; the original implementation re-used the first payload."""

    async def body():
        calls, started, release = _install_coalescing_fakes(monkeypatch, "mid")
        first = asyncio.create_task(
            bot_runtime.handle_strategy_job(
                {"telegram_id": 42, "network": "mainnet", "strategy": "mid"}
            )
        )
        await started.wait()
        await bot_runtime.handle_strategy_job(
            {"telegram_id": 42, "network": "mainnet", "strategy": "mid", "nudge": True}
        )
        release.set()
        await first
        assert calls == [False, True]

    try:
        asyncio.run(body())
    finally:
        bot_runtime._job_locks.clear()
        bot_runtime._job_pending_payloads.clear()
        bot_runtime._job_coalesce_counts.clear()


def test_volume_fill_upgrades_an_already_pending_normal_tick(monkeypatch):
    """Volume drops extra overlaps by design, but a fill must still upgrade
    the one pending payload to nudge=True instead of being discarded."""

    async def body():
        calls, started, release = _install_coalescing_fakes(monkeypatch, "vol")
        first = asyncio.create_task(
            bot_runtime.handle_strategy_job(
                {"telegram_id": 42, "network": "mainnet", "strategy": "vol"}
            )
        )
        await started.wait()
        await bot_runtime.handle_strategy_job(
            {"telegram_id": 42, "network": "mainnet", "strategy": "vol"}
        )
        await bot_runtime.handle_strategy_job(
            {"telegram_id": 42, "network": "mainnet", "strategy": "vol", "nudge": True}
        )
        release.set()
        await first
        assert calls == [False, True]

    try:
        asyncio.run(body())
    finally:
        bot_runtime._job_locks.clear()
        bot_runtime._job_pending_payloads.clear()
        bot_runtime._job_coalesce_counts.clear()
