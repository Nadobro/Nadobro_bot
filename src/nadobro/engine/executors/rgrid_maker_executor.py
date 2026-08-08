"""R-Grid maker executor — the only way Reverse Grid touches the venue.

Reverse Grid rests POST-ONLY limit orders, like every other market-making
strategy here (the standing rule: maker-first limit orders for all strategy opens
and closes). Its geometry is what makes it momentum rather than mean reversion:

    buy  leg rests at  anchor x (1 + spread)
    sell leg rests at  anchor x (1 - spread)

which is the mirror of Grid (bid below / ask above). Read against the spec —
"buy orders are placed at a price equal to or above the average ... they only
fill when the market price rises above this buy limit price" — a bid parked ABOVE
the anchor becomes fillable exactly once price has risen past it, because only
then is it a resting bid BELOW market that a seller can hit. So the order is a
maker order and the fill is a momentum fill: the book buys into strength on a
pullback and sells into weakness on a bounce.

The consequence of the geometry is that at most ONE leg is postable at a time.
``anchor x (1+spread) < mid`` and ``anchor x (1-spread) > mid`` cannot both hold.
Inside the band neither leg is postable and R-Grid waits — it never crosses to
force a fill.

This executor exists to make maker-only structural rather than conventional:
``ExecutionStrategy.LIMIT_MAKER`` is enforced in the constructor, so an edit that
hands R-Grid a MARKET config raises instead of quietly turning a maker strategy
into one that pays ~4.3bp per side.
"""
from __future__ import annotations

from typing import Optional

from src.nadobro.engine.adapter.base import NadoAdapterBase
from src.nadobro.engine.executors.order_executor import OrderExecutor, OrderExecutorConfig
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import ExecutionStrategy, PositionAction, TradeType, _dec

# Which leg an order is. Plain strings so it survives telemetry/persistence.
LEG_ENTRY = "entry"            # the side that ADDS into the move — post-only
LEG_EXIT = "exit"              # the resting reduce-only side that books it — post-only
LEG_TRAIL_STOP = "trail_stop"  # the armed trailing stop — the ONE order that crosses


class RGridMakerExecutor(OrderExecutor):
    """One resting post-only quote for Reverse Grid. Maker-only, by construction."""

    def __init__(
        self,
        config: OrderExecutorConfig,
        *,
        user_id: int,
        controller_id: str,
        adapter: NadoAdapterBase,
        inventory: Optional[InventoryRepository] = None,
        executor_id: Optional[str] = None,
        leg: str = LEG_ENTRY,
    ) -> None:
        self.leg = str(leg or LEG_ENTRY)
        # Maker-only, with exactly ONE documented exemption: the armed trailing
        # stop. A trailing stop is not expressible as a resting maker order — it
        # sells BELOW the peak while a post-only ask must sit ABOVE the market — so
        # it is allowed to cross. Everything else must be post-only, and an edit
        # that tries to sneak a MARKET entry through raises here.
        if self.leg == LEG_TRAIL_STOP:
            if config.execution_strategy is not ExecutionStrategy.LIMIT:
                raise ValueError(
                    "the trailing stop is the crossing order: execution_strategy "
                    f"must be MARKET (got {config.execution_strategy.value})"
                )
            if config.position_action is not PositionAction.CLOSE:
                raise ValueError(
                    "the trailing stop must be reduce-only so it can never open or "
                    "flip a position"
                )
        elif config.execution_strategy is not ExecutionStrategy.LIMIT_MAKER:
            raise ValueError(
                "RGridMakerExecutor is maker-only apart from the trailing stop: "
                "every market-making strategy rests post-only limit orders, so "
                f"execution_strategy must be LIMIT_MAKER (got "
                f"{config.execution_strategy.value})"
            )
        super().__init__(
            config,
            user_id=user_id,
            controller_id=controller_id,
            adapter=adapter,
            inventory=inventory,
            executor_id=executor_id,
        )

    @property
    def is_exit(self) -> bool:
        """True for the reduce-only leg.

        Unlike a market close, a maker exit fills at a price the strategy CHOSE
        (anchor x (1-spread), or the trailing price once armed). That price is
        genuine exposure information, so — exactly as in Grid, where a sell that
        closes a long is "the strategy working" and must re-anchor — an exit fill
        still feeds its leg's exposure window. This flag is for telemetry and
        reduce-only routing, not for excluding the fill from the anchor.
        """
        return (
            self.config.position_action is PositionAction.CLOSE
            or self.leg in (LEG_EXIT, LEG_TRAIL_STOP)
        )


def build_maker_quote(
    trading_pair: str,
    side: TradeType,
    amount_base: object,
    price: object,
    *,
    leverage: int = 1,
    reduce_only: bool = False,
) -> OrderExecutorConfig:
    """Config for one resting post-only R-Grid quote."""
    return OrderExecutorConfig(
        trading_pair, side, _dec(amount_base), ExecutionStrategy.LIMIT_MAKER,
        price=_dec(price),
        leverage=int(leverage or 1),
        position_action=PositionAction.CLOSE if reduce_only else PositionAction.OPEN,
    )


def build_trail_stop(
    trading_pair: str,
    side: TradeType,
    amount_base: object,
    *,
    leverage: int = 1,
    price: object = None,
) -> OrderExecutorConfig:
    """Config for a crossing exit: a MARKETABLE LIMIT, reduce-only.

    LIMIT ORDERS ONLY. An exit has to act once price has already moved against the
    position, which is precisely where a resting post-only order cannot sit — so
    this one crosses. It is still a LIMIT with a bounded price, never a naked
    MARKET: a thin or gapped book can then refuse it, but it can never fill it at
    an arbitrary price. Reduce-only, so it can only shrink the book, never open or
    flip it. ``crosses_book`` keeps the reporting flag honest, since the order TYPE
    no longer says taker.
    """
    return OrderExecutorConfig(
        trading_pair, side, _dec(amount_base), ExecutionStrategy.LIMIT,
        price=_dec(price) if price is not None else None,
        leverage=int(leverage or 1),
        position_action=PositionAction.CLOSE,
        crosses_book=True,
    )
