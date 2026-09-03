"""PositionExecutor close path under the DENIED-vs-EMPTY adapter contract
(AUDIT-DENY-2026-09-02, the DN non-MARKET close edge).

A close whose status the adapter could not confirm reads back as held-OPEN,
never as CANCELLED with unknown fills (adapter contract, #274/#275). So the
executor must (a) keep polling a held close — no phantom escalation — and
(b) when a close IS cancelled with a trusted partial fill, escalate exactly the
unfilled remainder, not the whole position.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.adapter.base import OrderState
from src.nadobro.engine.executors.order_executor import OrderExecutorConfig
from src.nadobro.engine.executors.position_executor import (
    PositionExecState,
    PositionExecutor,
    PositionExecutorConfig,
)
from src.nadobro.engine.types import ExecutionStrategy, OrderType, TradeType, TripleBarrierConfig

PAIR = "SOL-USDC"


def _pos(adapter):
    oc = OrderExecutorConfig(PAIR, TradeType.BUY, Decimal(1), ExecutionStrategy.MARKET)
    barriers = TripleBarrierConfig(take_profit=Decimal("0.05"), take_profit_order_type=OrderType.LIMIT)
    return PositionExecutor(PositionExecutorConfig(order_config=oc, barriers=barriers),
                            user_id=1, controller_id="c", adapter=adapter, inventory=None)


class _CancelledPartialAdapter(MockNadoAdapter):
    """The close order reads back CANCELLED with a TRUSTED partial fill."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.cancel_target = None

    async def order_status(self, order_id):
        o = await super().order_status(order_id)
        if order_id == self.cancel_target:
            o.state = OrderState.CANCELLED
            o.filled_base = Decimal("0.4")
            o.filled_quote = Decimal("0.4") * Decimal(106)
        return o


def test_escalation_sizes_only_the_unfilled_remainder_of_a_cancelled_close():
    async def body():
        adapter = _CancelledPartialAdapter(mid=Decimal(100))
        ex = _pos(adapter)
        await ex.on_create()                                   # MARKET entry @100
        adapter.set_mid(Decimal(106))
        await ex.on_tick()                                     # TP -> LIMIT close resting
        assert ex.position_state is PositionExecState.CLOSING
        adapter.cancel_target = ex.close_order.id
        await ex.on_tick()                                     # poll: CANCELLED, 0.4 filled -> escalate
        esc = adapter.placed[-1]
        assert esc.order_type is OrderType.MARKET and esc.side is TradeType.SELL
        assert esc.amount_base == Decimal("0.6"), "escalate the remainder, never the whole position"
    asyncio.run(body())


def test_a_held_close_read_never_escalates():
    async def body():
        adapter = MockNadoAdapter(mid=Decimal(100))
        ex = _pos(adapter)
        await ex.on_create()
        adapter.set_mid(Decimal(106))
        await ex.on_tick()                                     # LIMIT close resting
        n = len(adapter.placed)
        for _ in range(3):
            await ex.on_tick()                                 # held: OPEN, 0 filled, every poll
        assert ex.position_state is PositionExecState.CLOSING
        assert len(adapter.placed) == n, "a held close must keep polling, not be promoted to MARKET"
    asyncio.run(body())
