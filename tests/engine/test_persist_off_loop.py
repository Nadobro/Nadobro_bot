"""Executor persistence must never block the event loop (2026-09-03).

py-spy on the live process: ``EngineRuntime.tick`` -> ``_persist_executors``
-> ``DbExecutorStore.save`` -> psycopg2, on the MAIN THREAD, once per executor
the session had ever held (1,532 of them, 20 live) — ~207 s per tick, growing
every cycle. These pin the fix: writes run on a DB worker thread; only DIRTY
executors are written (a terminated one exactly once); the session id is
resolved once per controller per batch; a pass still in flight is not stacked;
and the orchestrator prunes terminated executors beyond a bounded window after
the controller has had a tick to read them, banking their order counts first.
"""
from __future__ import annotations

import asyncio
import threading
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine import orchestrator as orch_mod
from src.nadobro.engine.controllers.market_making import MarketMakingController
from src.nadobro.engine.executors.order_executor import OrderExecutor, OrderExecutorConfig
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.types import CloseType, ExecutionStrategy, OrderType, TradeType
from src.nadobro.strategy.engine_runtime import EngineRuntime

PAIR = "BTC"
MM_CFG = {
    "trading_pair": PAIR, "spread_bp": "10", "order_amount_quote": "10",
    "levels": "1", "leverage": "1", "price_distance_tolerance": "0.0001",
    "min_quote_lifetime_s": "0", "max_quote_lifetime_s": "0",
}
MAIN = threading.main_thread().name


class _SpyStore:
    """Records which thread every batch arrives on and how often each id is written."""

    def __init__(self):
        self.batches: list = []
        self.threads: list = []
        self.writes: dict = {}
        self.states: dict = {}

    def save_rows(self, rows, controller_ids):
        self.threads.append(threading.current_thread().name)
        self.batches.append(list(rows))
        for r in rows:
            self.writes[r[0]] = self.writes.get(r[0], 0) + 1
            self.states.setdefault(r[0], []).append(r[7])
        return len(rows)


class _SaveOnlyStore:
    def __init__(self):
        self.threads: list = []

    def save(self, executor):
        self.threads.append(threading.current_thread().name)


def _runtime(store, adapter):
    rt = EngineRuntime(executor_store=store)
    orch = ExecutorOrchestrator()
    c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                               inventory=InventoryRepository(), configs=dict(MM_CFG))
    key = (1, "mainnet", "mid")
    rt._controllers[key] = c
    rt._orchestrators[key] = orch
    return rt, orch, c, key


def _market_executor(adapter, controller_id):
    cfg = OrderExecutorConfig(PAIR, TradeType.BUY, Decimal(1), ExecutionStrategy.MARKET)
    return OrderExecutor(cfg, user_id=1, controller_id=controller_id, adapter=adapter)


# --- off the loop -----------------------------------------------------------

def test_executor_writes_never_run_on_the_loop_thread():
    async def body():
        store = _SpyStore()
        rt, orch, c, key = _runtime(store, MockNadoAdapter(mid=Decimal(100)))
        await orch.spawn_controller(c)
        await rt.tick(*key)
        await rt.tick(*key)
        assert store.batches, "the tick persisted nothing"
        assert store.threads and all(t != MAIN for t in store.threads), store.threads
    asyncio.run(body())


def test_a_save_only_store_still_runs_off_the_loop():
    async def body():
        store = _SaveOnlyStore()
        rt, orch, c, key = _runtime(store, MockNadoAdapter(mid=Decimal(100)))
        await orch.spawn_controller(c)
        await rt.tick(*key)
        assert store.threads and all(t != MAIN for t in store.threads)
    asyncio.run(body())


# --- dirty tracking ---------------------------------------------------------

def test_a_terminated_executor_is_written_once_then_never_again():
    async def body():
        store = _SpyStore()
        adapter = MockNadoAdapter(mid=Decimal(100))
        rt, orch, c, key = _runtime(store, adapter)
        await orch.spawn_controller(c)
        await rt.tick(*key)                                   # bid + ask rest
        bid = next(o for o in adapter.placed if o.side is TradeType.BUY)
        adapter.fill_order(bid.id)
        await rt.tick(*key)                                   # bid executor -> FILLED -> TERMINATED
        terminated = [e for e in orch.list(c.id) if e.is_terminated]
        assert terminated, "the filled quote's executor should have terminated"
        eid = terminated[0].id
        assert "TERMINATED" in store.states[eid]
        after = store.writes[eid]
        for _ in range(4):
            await rt.tick(*key)
        assert store.writes[eid] == after, "a terminated executor was re-written on a later tick"
        # An unchanged live executor is not re-written either: the batch shrinks
        # to what actually changed.
        assert all(len(b) <= 3 for b in store.batches[2:]), [len(b) for b in store.batches]
    asyncio.run(body())


def test_a_failed_write_is_retried_next_tick():
    class _FlakyStore(_SpyStore):
        fail_next = True

        def save_rows(self, rows, controller_ids):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("db down")
            return super().save_rows(rows, controller_ids)

    async def body():
        store = _FlakyStore()
        rt, orch, c, key = _runtime(store, MockNadoAdapter(mid=Decimal(100)))
        await orch.spawn_controller(c)
        await rt.tick(*key)                                   # write fails: signatures NOT recorded
        assert not store.batches
        await rt.tick(*key)                                   # retried: every executor written now
        assert store.batches and len(store.batches[0]) == len(orch.list(c.id))
    asyncio.run(body())


def test_a_pass_still_in_flight_is_not_stacked():
    async def body():
        store = _SpyStore()
        rt, orch, c, key = _runtime(store, MockNadoAdapter(mid=Decimal(100)))
        await orch.spawn_controller(c)
        pending = asyncio.get_running_loop().create_future()  # a cancelled tick's pass still running
        rt._persist_inflight[key] = pending
        await rt.tick(*key)
        assert not store.batches
        pending.set_result(None)
        await rt.tick(*key)
        assert store.batches                                  # the dirty state carried over
    asyncio.run(body())


def test_stop_writes_the_final_rows_even_while_a_pass_is_in_flight(monkeypatch):
    """Audit 2026-09-03 finding 9: a user stop landing while a tick's batch was
    still in flight skipped the final write and left the session's rows ACTIVE
    forever (is_running/_remote_active then saw a stopped session as live)."""
    from src.nadobro.trading import engine_persistence as ep
    swept: list = []
    monkeypatch.setattr(ep, "terminate_engine_executors", lambda cid: swept.append(cid) or 0)

    async def body():
        store = _SpyStore()
        rt, orch, c, key = _runtime(store, MockNadoAdapter(mid=Decimal(100)))
        await orch.spawn_controller(c)
        await rt.tick(*key)                                   # quotes rest, rows written ACTIVE
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        rt._persist_inflight[key] = pending                   # a tick's batch still landing
        loop.call_later(0.05, pending.set_result, None)
        await rt.stop(*key)                                   # waits for it, then writes
        final = {r[0]: r[7] for r in store.batches[-1]}
        assert final and all(state == "TERMINATED" for state in final.values()), final
        assert swept, "the post-stop TERMINATED sweep must run on the local branch too"
    asyncio.run(body())


def test_a_cancelled_tick_keeps_the_guard_until_the_batch_lands():
    """Audit 2026-09-03 finding 10: cancelling the tick at the await must not
    release the in-flight guard while the DB thread is still writing."""
    gate = threading.Event()

    class _BlockingStore(_SpyStore):
        def save_rows(self, rows, controller_ids):
            gate.wait(5.0)
            return super().save_rows(rows, controller_ids)

    async def body():
        store = _BlockingStore()
        rt, orch, c, key = _runtime(store, MockNadoAdapter(mid=Decimal(100)))
        await orch.spawn_controller(c)
        tick = asyncio.ensure_future(rt.tick(*key))
        for _ in range(50):                                   # until the batch is in flight
            await asyncio.sleep(0.01)
            if key in rt._persist_inflight:
                break
        assert key in rt._persist_inflight
        tick.cancel()
        try:
            await tick
        except asyncio.CancelledError:
            pass
        assert key in rt._persist_inflight, "guard released while the DB thread still runs"
        gate.set()
        await asyncio.wait_for(asyncio.shield(rt._persist_inflight[key]), 5.0)
        await asyncio.sleep(0)                                # let the done-callback run
        assert key not in rt._persist_inflight
        assert store.batches and rt._persisted_sig[key], "the landed batch must record its signatures"
    asyncio.run(body())


def test_pruning_waits_for_a_failed_write_to_be_retried(monkeypatch):
    """Audit 2026-09-03 finding 7: a failed batch must not be outrun by the
    prune — an executor whose terminal row is not yet written stays until the
    retry lands, then goes."""
    monkeypatch.setattr(orch_mod, "DEFAULT_TERMINATED_RETENTION", 5)

    class _FlakyStore(_SpyStore):
        fail_next = True

        def save_rows(self, rows, controller_ids):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("db down")
            return super().save_rows(rows, controller_ids)

    async def body():
        store = _FlakyStore()
        adapter = MockNadoAdapter(mid=Decimal(100))
        rt, orch, c, key = _runtime(store, adapter)
        await orch.spawn_controller(c)
        ids = []
        for _ in range(12):
            ex = _market_executor(adapter, c.id)
            assert await orch.spawn(ex)
            ids.append(ex.id)
        await rt.tick(*key)                                   # write FAILS; the 12 are unpersisted
        assert not store.batches
        await rt.tick(*key)                                   # prune must skip them; retry lands
        assert set(ids) <= set(store.writes), "a terminated executor was pruned before its row was written"
        await rt.tick(*key)                                   # now persisted -> pruned to the window
        assert len([e for e in orch.list(c.id) if e.is_terminated]) == 5
    asyncio.run(body())


def test_a_failed_executor_is_prunable_once_its_order_is_terminal():
    """Audit 2026-09-03 finding 3: close_type stays FAILED forever (terminate is
    a no-op once terminated), so FAILED alone must not pin an executor whose
    cancel the F2b retry has since confirmed."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(MM_CFG))
        await orch.spawn_controller(c)
        cfg = OrderExecutorConfig(PAIR, TradeType.BUY, Decimal(1), ExecutionStrategy.LIMIT_MAKER,
                                  price=Decimal(90))
        ex = OrderExecutor(cfg, user_id=1, controller_id=c.id, adapter=adapter)
        await orch.spawn(ex)
        ex._terminate(CloseType.FAILED)
        assert orch.prune_terminated(c.id, keep=0) == 0       # order still OPEN: pinned
        await adapter.cancel_order(ex.order.id)
        ex.order = await adapter.order_status(ex.order.id)    # the retried stop confirmed the cancel
        assert ex.order.state.is_terminal
        assert orch.prune_terminated(c.id, keep=0) == 1       # FAILED but nothing rests: prunable
    asyncio.run(body())


def test_session_id_is_resolved_once_per_controller_per_batch(monkeypatch):
    from src.nadobro.trading import engine_persistence as ep
    calls: list = []
    sent: list = []
    monkeypatch.setattr(ep, "resolve_session_id_for_controller", lambda cid: (calls.append(cid), 312)[1])
    monkeypatch.setattr("src.nadobro.db.execute_batch", lambda sql, rows, **kw: sent.append(list(rows)) or len(rows))
    adapter = MockNadoAdapter(mid=Decimal(100))
    rows, cids = [], []
    for _ in range(5):
        ex = _market_executor(adapter, "mid:1:mainnet")
        asyncio.run(ex.on_create())
        rows.append(ep.executor_row(ex, None))
        cids.append(ex.controller_id)
    assert ep.DbExecutorStore().save_rows(rows, cids) == 5
    assert calls == ["mid:1:mainnet"], calls                  # once, not five times
    assert len(sent) == 1 and all(r[-1] == 312 for r in sent[0])


# --- bounded retention ------------------------------------------------------

def test_pruning_keeps_the_newest_window_and_banks_counts(monkeypatch):
    monkeypatch.setattr(orch_mod, "DEFAULT_TERMINATED_RETENTION", 5)

    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(MM_CFG))
        await orch.spawn_controller(c)
        for _ in range(12):                                   # 12 executors that terminate on create
            assert await orch.spawn(_market_executor(adapter, c.id))
        assert len([e for e in orch.list(c.id) if e.is_terminated]) == 12
        placed_before = c.order_counts()["orders_placed"]
        await orch.tick_controller(c.id)                      # the controller sees them, then they are pruned
        terminated = [e for e in orch.list(c.id) if e.is_terminated]
        assert len(terminated) == 5, len(terminated)
        assert c.order_counts()["orders_placed"] == placed_before + 2, "counts lost in the prune"  # +2 fresh quotes
    asyncio.run(body())


def test_an_executor_terminated_during_the_tick_survives_it():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(MM_CFG))
        await orch.spawn_controller(c)
        for _ in range(3):
            await orch.spawn(_market_executor(adapter, c.id))
        # keep=0: everything eligible is dropped — but only what was terminated
        # BEFORE the tick (the controller must get one tick to read a fill).
        assert orch.prune_terminated(c.id, eligible=set(), keep=0) == 0
        assert orch.prune_terminated(c.id, keep=0) == 3
    asyncio.run(body())


def test_an_executor_whose_order_may_still_rest_is_never_pruned():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(MM_CFG))
        await orch.spawn_controller(c)
        cfg = OrderExecutorConfig(PAIR, TradeType.BUY, Decimal(1), ExecutionStrategy.LIMIT_MAKER,
                                  price=Decimal(90))
        ex = OrderExecutor(cfg, user_id=1, controller_id=c.id, adapter=adapter)
        await orch.spawn(ex)                                  # rests
        assert ex.order is not None and not ex.order.state.is_terminal
        ex._terminate(CloseType.FAILED)                       # a stop that could not confirm the cancel
        assert orch.prune_terminated(c.id, keep=0) == 0
        assert orch.get(ex.id) is ex                          # F2b can still retry through it
    asyncio.run(body())


def test_a_never_placed_failed_executor_is_prunable():
    """Grid-family audit of a555e0e, finding 1: an executor whose placement
    failed ends FAILED with no order at all — nothing rests, so FAILED alone
    must not hold it for the whole session (under sustained post-only
    rejection that regrows the 1,500-executor shape, minus the DB writes)."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100), fail_on=["place_order"], fail_times=999)
        orch = ExecutorOrchestrator()
        c = MarketMakingController(user_id=1, orchestrator=orch, adapter=adapter,
                                   inventory=InventoryRepository(), configs=dict(MM_CFG))
        await orch.spawn_controller(c)
        for _ in range(3):
            await orch.spawn(_market_executor(adapter, c.id))   # placement raises -> FAILED
        failed = [e for e in orch.list(c.id) if e.is_terminated and e.close_type is CloseType.FAILED]
        assert len(failed) == 3 and all(e.order is None for e in failed)
        assert orch.prune_terminated(c.id, keep=0) == 3
    asyncio.run(body())
