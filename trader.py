"""Trader: signal -> order state machine (SPEC §4.2, §4.3, §4.4).

Tracks entry price + bar/time of the currently open position so that
ExitRules (§4.1 step 2) can decide on stop-loss / time-stop / etc.
"""
from datetime import datetime
from typing import Literal, Optional

from .events import MarketEvent, OrderEvent
from .execution import Execution
from .exits.base import PositionState

Direction = Literal["BUY", "SELL", "HOLD"]


class Trader:
    def __init__(
        self,
        execution: Execution,
        *,
        tick_size: float = 1.0,
        aggression_ticks: int = 3,
        max_position: int = 1,
    ):
        # Tech: hold the execution handle and order-placement parameters, and
        #       initialize position/entry tracking to flat.
        # Why:  the Trader owns *intent* (what order to send) while Execution owns
        #       *fills*; aggression_ticks×tick_size is the price offset applied to
        #       every limit (§4.3). entry_* are mirrored here (separate from the
        #       Portfolio's) because ExitRules query the live position via this object.
        self.execution = execution
        self.tick_size = tick_size
        self.aggression_ticks = aggression_ticks
        self.max_position = max_position
        self.position = 0  # signed contracts; +long / -short
        self.entry_price: float = 0.0
        self.entry_bar_idx: int = -1
        self.entry_timestamp: Optional[datetime] = None

    # ---- signal-driven entries / closes ----------------------------------

    def on_signal(
        self, direction: Direction, market: MarketEvent
    ) -> Optional[OrderEvent]:
        # Tech: unconditionally cancel any resting order before acting.
        # Why:  §4.4 — a pending limit lives only until the next signal; cancelling
        #       first (even for HOLD) guarantees a stale forecast's order can never
        #       outlive the forecast that produced it.
        self.execution.cancel()

        # Tech: HOLD means "do nothing further" after the cancel above.
        # Why:  HOLD is a real decision (stand aside), not a no-op — its cancel is
        #       the whole point, so we return without submitting anything.
        if direction == "HOLD":
            return None

        # Tech: compute the limit-price offset from current price.
        # Why:  positive aggression makes the limit marketable (crosses the spread),
        #       which is the configured default; capturing `cur` once keeps the
        #       branch logic below symmetric.
        cur = market.price
        offset = self.aggression_ticks * self.tick_size

        # Tech: the §4.2 state machine — from flat, open in the signal's direction;
        #       while long, only a SELL acts (closes, never flips); while short,
        #       only a BUY acts (closes, never flips).
        # Why:  no pyramiding / no flipping in v1 — a flip would be two economic
        #       actions (close + open) collapsed into one tick, which muddies PnL
        #       attribution, so same-direction-while-in-position is deliberately a
        #       no-op and an opposing signal closes only.
        order: Optional[OrderEvent] = None
        if self.position == 0:
            if direction == "BUY":
                order = OrderEvent(market.timestamp, "BUY", cur + offset, "OPEN")
            else:
                order = OrderEvent(market.timestamp, "SELL", cur - offset, "OPEN")
        elif self.position > 0:
            if direction == "SELL":
                order = OrderEvent(market.timestamp, "SELL", cur - offset, "CLOSE")
        else:
            if direction == "BUY":
                order = OrderEvent(market.timestamp, "BUY", cur + offset, "CLOSE")

        # Tech: hand any order we built to Execution, passing the price at placement.
        # Why:  Execution needs current_price to classify marketable vs passive
        #       (§4.5); returning the order lets the run loop log it.
        if order is not None:
            self.execution.submit(order, current_price=cur)
        return order

    # ---- exit-driven / forced closes -------------------------------------

    def submit_exit(self, market: MarketEvent) -> Optional[OrderEvent]:
        """Submit a closing limit at current ± aggression_ticks.

        Used by both ExitRule firing (§4.1 step 2) and session-boundary
        forced close (§4.1 step 4). Cancels any pending order first.
        """
        # Tech: nothing to close when flat.
        # Why:  exits and forced closes can fire on any tick; guarding here means
        #       callers don't have to check position state themselves.
        if self.position == 0:
            return None
        # Tech: cancel any working order, then place a closing limit on the side
        #       opposite the position (SELL to close long, BUY to close short).
        # Why:  cancel-then-submit mirrors §4.4 so a stale entry limit can't survive
        #       a forced close; the close is offset by aggression so it's marketable
        #       and actually gets us flat by the boundary.
        self.execution.cancel()
        cur = market.price
        offset = self.aggression_ticks * self.tick_size
        if self.position > 0:
            order = OrderEvent(market.timestamp, "SELL", cur - offset, "CLOSE")
        else:
            order = OrderEvent(market.timestamp, "BUY", cur + offset, "CLOSE")
        self.execution.submit(order, current_price=cur)
        return order

    # ---- fill bookkeeping ------------------------------------------------

    def on_fill(
        self,
        side: str,
        quantity: int,
        *,
        fill_price: float,
        bar_idx: int,
        timestamp: datetime,
    ) -> None:
        # Tech: fold the fill into the signed position.
        # Why:  the Trader keeps its own position mirror (distinct from Portfolio)
        #       because it drives order logic and ExitRule queries, which must stay
        #       in lockstep with executions.
        signed = 1 if side == "BUY" else -1
        prev = self.position
        new = prev + signed * quantity
        self.position = new

        if prev == 0 and new != 0:
            # Tech: opening from flat — stamp entry price, bar index, and timestamp.
            # Why:  ExitRules need the entry reference (stop distance from entry,
            #       bars-in-trade); bar_idx/timestamp are recorded so time-stops can
            #       measure holding period.
            self.entry_price = fill_price
            self.entry_bar_idx = bar_idx
            self.entry_timestamp = timestamp
        elif new == 0:
            # Tech: returned to flat — clear all entry tracking to sentinels.
            # Why:  leaving stale entry data would make position_state() lie on the
            #       next trade and could trip an ExitRule against a closed position.
            self.entry_price = 0.0
            self.entry_bar_idx = -1
            self.entry_timestamp = None
        # (Partial fills / pyramiding would update average entry here; v1
        # uses max_position=1 so this branch is unreachable.)

    def position_state(self) -> Optional[PositionState]:
        # Tech: snapshot the open position as a PositionState, or None when flat.
        # Why:  ExitRules take an immutable snapshot rather than the live Trader, so
        #       they can't accidentally mutate trading state; the assert documents
        #       the invariant that a non-zero position always has an entry timestamp.
        if self.position == 0:
            return None
        assert self.entry_timestamp is not None
        return PositionState(
            side="LONG" if self.position > 0 else "SHORT",
            size=abs(self.position),
            entry_price=self.entry_price,
            entry_bar_idx=self.entry_bar_idx,
            entry_timestamp=self.entry_timestamp,
        )
