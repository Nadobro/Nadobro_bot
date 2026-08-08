"""Short-ladder controller — the SELL-side counterpart of GridController.

This is D-Grid's downtrend phase: the same static multi-level ladder as
:class:`GridController`, resting SELLs stepping UP from mid and taking profit one
step lower, driven by :class:`ReverseGridExecutor`.

It is NOT the Reverse Grid strategy. R-Grid is
:class:`~src.nadobro.engine.controllers.rgrid.RGridController` — a different thing
entirely (exposure-average anchor, taker breaks, trailing soft reset, no phases).
This class used to be called ``ReverseGridController``, and that name is a large
part of how the two got conflated: R-Grid was wired to the D-Grid phase switcher
and users were told their R-Grid session had "switched RGRID → GRID".
"""
from __future__ import annotations

from src.nadobro.engine.controllers.grid_trading import GridController
from src.nadobro.engine.executors.reverse_grid_executor import ReverseGridExecutor
from src.nadobro.engine.types import TradeType


class ShortLadderController(GridController):
    SIDE = TradeType.SELL
    EXECUTOR_CLS = ReverseGridExecutor
    # D-Grid turns the regime gate off for this phase in production: the phase is
    # only armed BECAUSE a downtrend was detected, so a gate that pauses on trends
    # would immediately stand the ladder down (see engine_runtime).

    def __init__(self, **kwargs: object) -> None:
        kwargs.setdefault("name", "short_ladder")
        super().__init__(**kwargs)
