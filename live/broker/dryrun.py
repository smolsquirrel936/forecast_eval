"""DryRunBroker — decides and books exactly as live, but sends nothing.

The V3 dry run needs a session's worth of *real decisions* against a *real feed*
without an order ever reaching the venue. The naive way to do that — accept
orders and never fill them — is useless: the Trader would never open a position,
the no-flip state machine would never advance, and every signal after the first
would be evaluated against a flat book the live system would not have had.

So this broker keeps the offline fill model (the same `Execution` `SimBroker`
uses) and simply *records* what it would have sent. Positions evolve, exits fire,
the audit trail is complete — and `place_order` is never called.

That also makes shadow parity meaningful: replaying the session's bars offline
through `run_backtest` should reproduce a dry run exactly, because both sides
share one fill model. Any divergence is then attributable to the live path
(bar construction, timing, dropped ticks) rather than to execution.
"""
from typing import Any, List, Optional

from ...events import FillEvent, MarketEvent, OrderEvent
from ...execution import Execution
from .base import Broker


class DryRunBroker(Broker):
    def __init__(self, execution: Execution, *, recorder: Any = None):
        # Tech: wrap an Execution for fills and keep the log of intended orders.
        # Why:  same composition as `SimBroker` — the fill model is shared, not
        #       reimplemented — plus `intended`, which is the artifact the dry run
        #       exists to produce.
        self._execution = execution
        self.recorder = recorder
        self.intended: List[dict] = []

    def submit(self, order: OrderEvent, *, current_price: float) -> None:
        # Tech: record what would have gone to the venue, then hand the order to
        #       the offline fill model instead of the venue.
        # Why:  the recorded fields are exactly what `ShioajiBroker.submit` would
        #       have translated — side, limit, quantity, intent — so a dry-run log
        #       can be diffed against a later live log order for order.
        self.intended.append({
            "timestamp": order.timestamp,
            "side": order.side,
            "limit_price": order.limit_price,
            "quantity": order.quantity,
            "intent": order.intent,
            "current_at_placement": current_price,
        })
        if self.recorder is not None:
            self.recorder.record("dryrun_order", **self.intended[-1])
        self._execution.submit(order, current_price=current_price)

    def cancel(self) -> None:
        self._execution.cancel()

    def has_pending(self) -> bool:
        return self._execution.has_pending()

    def pending_order(self) -> Optional[OrderEvent]:
        return self._execution.pending_order()

    def poll_fills(self, market: MarketEvent) -> List[FillEvent]:
        # Tech: resolve against the print, exactly as `SimBroker` does.
        # Why:  simulated fills are what keep the position state machine honest;
        #       they are clearly labelled as simulated in the recorder, so no
        #       downstream analysis can mistake them for venue fills.
        fill = self._execution.check_fill(market)
        if fill is None:
            return []
        if self.recorder is not None:
            self.recorder.record(
                "dryrun_fill", timestamp=fill.timestamp, side=fill.side,
                fill_price=fill.fill_price, quantity=fill.quantity, fee=fill.fee,
                simulated=True,
            )
        return [fill]

    def broker_position(self) -> Optional[int]:
        # Tech: no venue to ask.
        # Why:  returning None makes the reconciliation rail skip, which is
        #       correct — there is no real position to reconcile against, and
        #       fabricating one would test nothing.
        return None
