"""Execution: fill simulator for trade-print-only data (SPEC §4.5).

Marketable rule (BUY limit >= current price, or SELL limit <= current price):
  - Fills at the next trade print.
  - Fill price = that trade's price (crossed the spread).

Passive rule (otherwise):
  - Fills only when a subsequent print strictly crosses the limit
    (strictly below for BUY, strictly above for SELL).
  - Fill price = the limit price (no price improvement).

Fees per side: fill_price * fee_rate  (SPEC §4.6, taken literally — bundles
slippage and tax). Units are price-points; reporting may scale by the
contract multiplier.
"""
from dataclasses import dataclass
from typing import Optional

from .events import FillEvent, MarketEvent, OrderEvent


@dataclass
class _Pending:
    # Tech: bundles the live order with the market price observed at the moment
    #       it was submitted, plus the timestamp of that submission tick.
    # Why:  the marketable/passive classification (SPEC §4.5) is defined relative
    #       to the price *when the order was sent*, and the same-tick guard needs
    #       the placement timestamp — both must be frozen at submit time, not
    #       re-read later when the market has moved on.
    order: OrderEvent
    current_at_placement: float
    placed_at_timestamp: object


class Execution:
    def __init__(self, fee_rate: float = 0.00015):
        # Tech: store the per-side fee rate and start with no resting order.
        # Why:  v1 models a single working order at a time (`_pending`); the
        #       trader cancels-then-submits, so there is never a need to track a
        #       book of competing limits. fee_rate defaults to the SPEC §4.6 value.
        self.fee_rate = fee_rate
        self._pending: Optional[_Pending] = None  # v1: one pending at a time

    def submit(self, order: OrderEvent, current_price: float) -> None:
        # Tech: wrap the order in a _Pending snapshot, capturing the current price
        #       and the order's timestamp as the placement reference.
        # Why:  overwrites any existing pending order unconditionally — the Trader
        #       guarantees it has already cancelled the prior one (SPEC §4.4), so
        #       a plain assignment is the whole contract here.
        self._pending = _Pending(
            order=order,
            current_at_placement=current_price,
            placed_at_timestamp=order.timestamp,
        )

    def cancel(self) -> None:
        # Tech: drop any resting order by clearing the slot.
        # Why:  pending-order lifetime is one forecast boundary (SPEC §4.4); the
        #       Trader calls this on every new signal and before every exit/forced
        #       close so a stale limit can never survive into the next decision.
        self._pending = None

    def has_pending(self) -> bool:
        # Tech: True iff an order is currently resting.
        # Why:  lets callers (Trader, tests) branch on book state without reaching
        #       into the private `_pending` attribute.
        return self._pending is not None

    def pending_order(self) -> Optional[OrderEvent]:
        # Tech: return the resting OrderEvent, or None when the book is empty.
        # Why:  read-only accessor for inspection/assertions; unwraps the order out
        #       of its _Pending envelope so callers don't depend on that internal type.
        return self._pending.order if self._pending else None

    def check_fill(self, market: MarketEvent) -> Optional[FillEvent]:
        # Tech: short-circuit when there's nothing to fill.
        # Why:  the run loop calls check_fill on every single tick; an empty book
        #       is the common case and must cost nothing.
        p = self._pending
        if p is None:
            return None
        # Tech: refuse to fill if this tick is the same one the order was placed on.
        # Why:  Per §4.1, fills are checked at the start of a tick, before any new
        #       orders are submitted. So an order placed during tick N cannot fill
        #       against tick N's own print.
        if market.timestamp == p.placed_at_timestamp:
            return None

        # Tech: classify the order as marketable from limit-vs-placement price —
        #       BUY is marketable when its limit sits at/above the reference price,
        #       SELL when at/below it.
        # Why:  a marketable order would have crossed the spread immediately in a
        #       real book; this single boolean decides the entire fill model below.
        order = p.order
        if order.side == "BUY":
            marketable = order.limit_price >= p.current_at_placement
        else:
            marketable = order.limit_price <= p.current_at_placement

        # Tech: marketable -> take the incoming print's price as the fill; passive
        #       -> require a strict cross of the limit (below for BUY, above for
        #       SELL) and fill at the limit itself, otherwise return no fill.
        # Why:  marketable orders execute against resting liquidity, so the actual
        #       trade price is realistic. Passive orders sit *behind* the queue we
        #       can't see in trade-print data, so we demand a strict cross before
        #       assuming a fill and grant no price improvement (SPEC §4.5).
        if marketable:
            fill_price = market.price
        else:
            # Passive: requires strictly crossing the limit.
            if order.side == "BUY" and market.price < order.limit_price:
                fill_price = order.limit_price
            elif order.side == "SELL" and market.price > order.limit_price:
                fill_price = order.limit_price
            else:
                return None

        # Tech: charge the per-side fee on the full filled quantity, clear the book,
        #       and emit the FillEvent for order.quantity contracts (no partial fills
        #       in this print-only sim — the whole order fills at once).
        # Why:  the order is consumed exactly once — clearing `_pending` before
        #       returning prevents a double fill on the next tick, and fee is taken
        #       on this leg because SPEC §4.6 charges every fill, open or close. Fee
        #       scales with quantity since §4.6 is a per-contract cost; quantity=1
        #       (the v1 default) leaves both fee and size identical to before (#7).
        fee = fill_price * self.fee_rate * order.quantity
        self._pending = None
        return FillEvent(
            timestamp=market.timestamp,
            side=order.side,
            fill_price=fill_price,
            quantity=order.quantity,
            fee=fee,
        )
