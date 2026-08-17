"""SimBroker — the existing print-only fill simulator behind the Broker API.

This exists so the live loop can be driven over historical ticks and diffed
against `run.run_backtest` (SHIOAJI_PLAN.md phase V0). It adds no fill logic of
its own: every decision still comes from `Execution`, untouched. If a parity
test fails, the bug is in the loop restructuring — never in here.
"""
from typing import List, Optional

from ...events import FillEvent, MarketEvent, OrderEvent
from ...execution import Execution
from .base import Broker


class SimBroker(Broker):
    def __init__(self, execution: Execution):
        # Tech: hold the Execution instance this broker delegates to.
        # Why:  composition rather than subclassing, because `Execution` predates
        #       the Broker interface and must keep working standalone in
        #       `run_backtest`. Injecting it (instead of constructing one from a
        #       fee_rate) lets a parity test hand both paths an identically
        #       configured simulator.
        self._execution = execution

    def submit(self, order: OrderEvent, *, current_price: float) -> None:
        # Tech/Why: straight pass-through — see `Execution.submit`.
        self._execution.submit(order, current_price=current_price)

    def cancel(self) -> None:
        # Tech/Why: straight pass-through — see `Execution.cancel`.
        self._execution.cancel()

    def has_pending(self) -> bool:
        # Tech/Why: straight pass-through — see `Execution.has_pending`.
        return self._execution.has_pending()

    def pending_order(self) -> Optional[OrderEvent]:
        # Tech/Why: straight pass-through — see `Execution.pending_order`.
        return self._execution.pending_order()

    def poll_fills(self, market: MarketEvent) -> List[FillEvent]:
        # Tech: resolve the resting order against this print and return it as a
        #       0- or 1-element list.
        # Why:  the print-only model fills a whole order at once and holds at most
        #       one pending order, so it can never yield two fills from one tick —
        #       boxing the single Optional into a list is the entire adaptation to
        #       the multi-fill Broker signature, and keeps the loop's fill handling
        #       identical to `run_backtest`'s single-fill block.
        fill = self._execution.check_fill(market)
        return [fill] if fill is not None else []
