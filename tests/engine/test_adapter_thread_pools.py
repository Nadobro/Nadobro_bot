"""The engine adapter's blocking calls must not land on the default executor.

Production 2026-08-06: every gateway call in ``engine/adapter/nado.py`` went
through ``asyncio.to_thread``, i.e. the event loop's implicit default executor —
``min(32, cpu_count + 4)`` = 5 threads on the 1-CPU Fly VM, shared with
``venue/nado_sync`` and ``market_data``. Symptoms: ``Strategy cycle end ...
elapsed_ms=45324.7``, skipped APScheduler ticks, ``callback.total`` max=84s.

Thread-name assertions are the cheap, direct proof of which pool ran the work.
"""
import asyncio
import threading

from _stubs import install_test_stubs

install_test_stubs()


def _thread_name_via(dispatcher):
    async def scenario():
        return await dispatcher(lambda: threading.current_thread().name)

    return asyncio.run(scenario())


def test_adapter_sdk_dispatch_uses_the_sdk_pool():
    from src.nadobro.engine.adapter.nado import _sdk

    assert _thread_name_via(_sdk).startswith("nadobro-sdk")


def test_execution_has_its_own_pool_isolated_from_reads():
    """A portfolio-poll storm on the SDK pool must not delay an order or cancel.

    That isolation used to come from execution living on the default executor
    while polling used the SDK pool — accidental, and it silently degraded when
    the VM dropped to 1 CPU (5 default workers for the whole process).
    """
    from src.nadobro.engine.adapter.nado import _exec, _sdk

    assert _thread_name_via(_exec).startswith("nadobro-exec")
    assert _thread_name_via(_exec) != _thread_name_via(_sdk)


def test_adapter_db_dispatch_uses_the_db_pool():
    """The on_place hook does synchronous DB writes — psycopg2 pool, not SDK."""
    from src.nadobro.engine.adapter.nado import _db

    assert _thread_name_via(_db).startswith("nadobro-db")


def test_adapter_dispatch_never_uses_the_default_executor():
    from src.nadobro.engine.adapter.nado import _db, _exec, _sdk

    async def default_executor_name():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(  # policy: default-executor-ok(control sample)
            None, lambda: threading.current_thread().name
        )

    default_name = asyncio.run(default_executor_name())
    for dispatcher in (_sdk, _db, _exec):
        assert _thread_name_via(dispatcher) != default_name


def test_every_pool_is_reported_and_independently_sized():
    from src.nadobro.core.async_utils import pool_stats

    stats = pool_stats()
    for key in (
        "db_workers",
        "sdk_workers",
        "exec_workers",
        "misc_workers",
        "bg_workers",
        "llm_workers",
    ):
        assert stats[key] >= 1, key


def test_fire_and_forget_keeps_a_strong_reference():
    """An unreferenced task can be garbage-collected before it ever runs."""
    from src.nadobro.core.async_utils import _background_tasks, fire_and_forget

    ran = asyncio.Event()

    async def scenario():
        async def side_work():
            ran.set()

        task = fire_and_forget(side_work())
        assert task in _background_tasks
        await asyncio.wait_for(ran.wait(), timeout=1.0)
        await task
        assert task not in _background_tasks

    asyncio.run(scenario())
