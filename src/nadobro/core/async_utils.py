"""Purpose-split thread pools for blocking work.

Phase 2: a single 16-worker pool was shared by DB, Nado SDK, and misc
handlers — one slow strategy cycle could starve Telegram reply DB reads.

Pools (env-tunable, defaults sized for ~250 users/shard):

* ``run_blocking_db`` — Postgres via psycopg2 (default 30 workers)
* ``run_blocking_sdk`` — Nado SDK READS / signing (default 12 workers)
* ``run_blocking_exec`` — order placement & cancels ONLY (default 8 workers)
* ``run_blocking`` — everything else on a user-facing path (default 8 workers)
* ``run_blocking_bg`` — background batch fan-out (default 6 workers)
* ``run_blocking_llm`` — LLM inference + stream chunk waits (default 12 workers)

``run_blocking_exec`` is deliberately separate from ``run_blocking_sdk``: a
portfolio-poll storm must never be able to delay an order placement or a cancel.
That isolation used to come from execution living on the default executor while
polling used the SDK pool — accidental, and it stopped working when the VM was
downsized to 1 CPU (5 default workers for the whole process).

NEVER use ``asyncio.to_thread`` / ``run_in_executor(None, ...)``. Those land on
the event loop's IMPLICIT default executor, which is sized
``min(32, cpu_count + 4)`` — FIVE workers on the 1-CPU production VM, shared by
every caller in the process. In 2026-08 the engine adapter (12 gateway calls per
strategy tick), portfolio sync, and the market snapshot all shared those five
threads: strategy cycles ran 45s, APScheduler logged "maximum number of running
instances reached", and Telegram taps hit an 84s p-max.
``tests/lint/test_no_default_executor.py`` keeps that door shut.

``run_blocking_bg`` exists so a news/brief/snapshot fan-out cannot occupy the
misc pool that click-path work (desk parsing, card renders) shares.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging

from src.nadobro.utils.env import env_float, env_int
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, ParamSpec, TypeVar

_DB_WORKERS = env_int("NADO_DB_POOL_WORKERS", 30)
_SDK_WORKERS = env_int("NADO_SDK_POOL_WORKERS", 12)
_MISC_WORKERS = env_int("NADO_MISC_POOL_WORKERS", 8)
# Execution concurrency ≈ number of strategies ticking at once (a ladder places
# its legs sequentially within one cycle), plus manual trades.
_EXEC_WORKERS = env_int("NADO_EXEC_POOL_WORKERS", 8)
_BG_WORKERS = env_int("NADO_BG_POOL_WORKERS", 6)
# AI chat parks a thread per streamed chunk-queue wait plus one per generation,
# for the full duration of the answer — two per concurrent conversation.
_LLM_WORKERS = env_int("NADO_LLM_POOL_WORKERS", 12)

_db_pool = ThreadPoolExecutor(max_workers=max(1, _DB_WORKERS), thread_name_prefix="nadobro-db")
_sdk_pool = ThreadPoolExecutor(max_workers=max(1, _SDK_WORKERS), thread_name_prefix="nadobro-sdk")
_misc_pool = ThreadPoolExecutor(max_workers=max(1, _MISC_WORKERS), thread_name_prefix="nadobro-misc")
_exec_pool = ThreadPoolExecutor(max_workers=max(1, _EXEC_WORKERS), thread_name_prefix="nadobro-exec")
_bg_pool = ThreadPoolExecutor(max_workers=max(1, _BG_WORKERS), thread_name_prefix="nadobro-bg")
_llm_pool = ThreadPoolExecutor(max_workers=max(1, _LLM_WORKERS), thread_name_prefix="nadobro-llm")
# Legacy alias — misc pool.
_blocking_pool = _misc_pool

P = ParamSpec("P")
R = TypeVar("R")


async def _run_in(pool: ThreadPoolExecutor, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(pool, lambda: ctx.run(func, *args, **kwargs))


async def run_blocking(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    return await _run_in(_misc_pool, func, *args, **kwargs)


async def run_blocking_db(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    return await _run_in(_db_pool, func, *args, **kwargs)


async def run_blocking_sdk(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    return await _run_in(_sdk_pool, func, *args, **kwargs)


async def run_blocking_exec(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Order placement and cancels — money paths, isolated from everything else.

    Keep reads (positions, balance, candles, depth, open orders) on
    ``run_blocking_sdk``. Mixing them here would let a poll storm queue in front
    of a cancel, which is how open orders get left on the venue.
    """
    return await _run_in(_exec_pool, func, *args, **kwargs)


async def run_blocking_bg(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Background/batch blocking work (news fan-out, snapshots, nightly briefs).

    Isolated from ``run_blocking`` so a slow scheduled fan-out never queues ahead
    of the click path.
    """
    return await _run_in(_bg_pool, func, *args, **kwargs)


async def run_blocking_llm(func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """LLM inference + streaming chunk waits.

    These park a thread for the whole generation (tens of seconds), and the
    streaming loops park one per chunk-queue ``get(timeout=...)``. They get their
    own pool so an AI-chat conversation cannot occupy threads the venue or the
    click path need.
    """
    return await _run_in(_llm_pool, func, *args, **kwargs)


# Default wall-clock ceiling for an SDK call that sits on a user-facing render
# path. The Nado SDK session timeout is read=12s × up-to-3 sessions (~30s), which
# is far too long to make a Telegram tap wait. ``run_blocking_sdk_capped`` bounds
# the *await* so the handler returns a placeholder; the underlying thread keeps
# running (it warms the cache for the next render) — we just stop blocking on it.
_SDK_CALL_CEILING_SECONDS = env_float("NADO_SDK_CALL_CEILING_SECONDS", 1.5)


async def run_blocking_sdk_capped(
    func: Callable[P, R],
    *args: P.args,
    timeout_seconds: float | None = None,
    default: R = None,  # type: ignore[assignment]
    **kwargs: P.kwargs,
) -> R:
    """Run a blocking SDK call on the SDK pool with a hard wall-clock cap.

    On timeout, returns ``default`` immediately instead of blocking the click
    path for the full SDK timeout chain. The orphaned thread runs to completion
    (Python can't cancel it) and typically warms the relevant cache.
    """
    ceiling = _SDK_CALL_CEILING_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        return await asyncio.wait_for(run_blocking_sdk(func, *args, **kwargs), timeout=ceiling)
    except asyncio.TimeoutError:
        return default


# Strong refs for fire-and-forget tasks: the event loop only holds weak
# references, so an unreferenced task can be garbage-collected before it runs.
logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def _reap_background_task(task: asyncio.Task) -> None:
    """Drop the strong reference and RETRIEVE the exception.

    Discarding without reading ``task.exception()`` leaves a failure invisible: it
    surfaces only as asyncio's "Task exception was never retrieved" at GC time,
    with no logger, no counter, and no way to tell which task it was. Every
    current caller happens to catch its own exceptions, so this is defense in
    depth for the NEXT one — the primitive should not be a place where errors go
    to die.
    """
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("background task %s failed: %s", task.get_name(), exc,
                       exc_info=exc)


def fire_and_forget(coro, *, name: str | None = None) -> asyncio.Task:
    """Schedule ``coro`` without awaiting it, keeping a strong reference.

    For side work whose result nobody needs and whose latency nobody should pay
    for — e.g. the Telegram typing indicator or a callback-query ack, each a full
    round-trip that used to sit in front of every button tap.

    Pass ``name`` to make a failure identifiable in the log; without it asyncio's
    generic ``Task-N`` is all the warning can report.
    """
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_reap_background_task)
    return task


def pool_stats() -> dict[str, int]:
    return {
        "db_workers": _DB_WORKERS,
        "sdk_workers": _SDK_WORKERS,
        "misc_workers": _MISC_WORKERS,
        "exec_workers": _EXEC_WORKERS,
        "bg_workers": _BG_WORKERS,
        "llm_workers": _LLM_WORKERS,
    }
