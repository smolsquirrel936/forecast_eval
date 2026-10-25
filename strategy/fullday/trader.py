"""Trader: signal -> order state machine (SPEC §4.2, §4.3, §4.4).

Tracks entry price + bar/time of the currently open position so that
ExitRules (§4.1 step 2) can decide on stop-loss / time-stop / etc.


PER-SESSION COPY (STRATEGY_SPLIT_PLAN S1). Verbatim copy of
``trader.py`` at sha256:fcb896217167 (newline-normalised, as
``tests/test_strategy_isolation.py`` pins it), with only the relative-import depth
changed. It is this package's to edit; the shared original is frozen and
hash-guarded by ``tests/test_strategy_isolation.py``. Diverge freely here.

DIVERGED (fullday_rev_iter02, autoresearch_trading_plan_fullday_reversal.md
decision 2): ``on_signal`` gained an entry-only probability gate — from flat,
a signal whose ``strength`` falls below the floor implied by ``entry_min_prob``
submits nothing, while closes are never gated. At the default
``entry_min_prob`` 0.50 the floor is 0.0 and the gate is inert, so runs are
byte-identical to the pre-gate copy.
"""
from datetime import datetime
from typing import Literal, Optional

from ...events import MarketEvent, OrderEvent
from ...execution import Execution
from ...exits.base import PositionState

Direction = Literal["BUY", "SELL", "HOLD"]


class Trader:
    def __init__(
        self,
        execution: Execution,
        *,
        tick_size: float = 1.0,
        aggression_ticks: int = 3,
        max_position: int = 1,
        max_aggression_ticks: Optional[int] = None,
        entry_min_prob: float = 0.5,
        full_confidence_prob: float = 0.9,
    ):
        # Tech: hold the execution handle and order-placement parameters, and
        #       initialize position/entry tracking to flat.
        # Why:  the Trader owns *intent* (what order to send) while Execution owns
        #       *fills*; aggression_ticks×tick_size is the price offset applied to
        #       every limit (§4.3). entry_* are mirrored here (separate from the
        #       Portfolio's) because ExitRules query the live position via this object.
        # Tech: max_aggression_ticks (optional) is the aggressive end of a
        #       confidence-scaled offset ramp whose base is aggression_ticks; None
        #       disables scaling. max_position caps confidence-scaled entry size.
        # Why:  with max_aggression_ticks=None and max_position=1 (the defaults) the
        #       offset is always aggression_ticks and size is always 1 — every existing
        #       run/test is byte-identical. Set them to opt into #1 (aggression) and
        #       #7 (sizing) where a high-conviction forecast crosses more ticks and
        #       trades more contracts.
        self.execution = execution
        self.tick_size = tick_size
        self.aggression_ticks = aggression_ticks
        self.max_position = max_position
        self.max_aggression_ticks = max_aggression_ticks
        # Tech: entry-only probability gate — convert entry_min_prob into the
        #       strength floor it implies on the emitter's conviction ramp,
        #       strength = (dir_prob - 0.5) / (full_confidence_prob - 0.5)
        #       clamped to [0, 1] (emitter.py §5).
        # Why:  autoresearch_trading_plan_fullday_reversal.md decision 2 — the
        #       gate lives here, after the emitter, because the emitter's trend
        #       filter stores a price only when a signal gets past its earlier
        #       gates; gating entries here leaves that stored state exactly what
        #       it is today. entry_min_prob=0.50 (floor 0.0, strength >= 0
        #       always) is the inert control. The ramp saturates at 1.0, so the
        #       gate cannot tell probabilities at or above full_confidence_prob
        #       apart; wiring validates min_prob <= entry_min_prob <
        #       full_confidence_prob so the gate is never vacuous or total.
        self.entry_min_prob = entry_min_prob
        self.entry_floor = (entry_min_prob - 0.5) / (full_confidence_prob - 0.5)
        self.position = 0  # signed contracts; +long / -short
        self.entry_price: float = 0.0
        self.entry_bar_idx: int = -1
        self.entry_timestamp: Optional[datetime] = None

    # ---- confidence-scaled placement helpers -----------------------------

    def _entry_offset(self, strength: float) -> float:
        # Tech: linearly interpolate the limit offset between aggression_ticks (base)
        #       and max_aggression_ticks by strength∈[0,1], rounded to whole ticks;
        #       when max_aggression_ticks is None, return the base offset unchanged.
        # Why:  #1 — a strong signal crosses more ticks to all but guarantee a fill,
        #       a weak one rests closer to the touch to save cost. Rounding keeps
        #       limits on the tick grid. The None short-circuit preserves v1 behavior.
        if self.max_aggression_ticks is None:
            ticks = self.aggression_ticks
        else:
            span = self.max_aggression_ticks - self.aggression_ticks
            ticks = self.aggression_ticks + int(round(strength * span))
        return ticks * self.tick_size

    def _entry_qty(self, strength: float) -> int:
        # Tech: scale entry size by strength up to max_position, with a floor of 1
        #       contract for any acted-on signal.
        # Why:  #7 — size with conviction. The floor means a threshold-passing but
        #       low-conviction signal still trades the minimum lot rather than 0;
        #       max_position=1 collapses this to a constant 1 (v1 behavior).
        if self.max_position <= 1:
            return 1
        return max(1, min(self.max_position, int(round(strength * self.max_position))))

    # ---- signal-driven entries / closes ----------------------------------

    def on_signal(
        self, direction: Direction, market: MarketEvent, *, strength: float = 1.0
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

        # Tech: capture the reference price; entries use a confidence-scaled offset
        #       and size, closes use the base offset and flatten the whole position.
        # Why:  #1/#7 — conviction (strength) only shapes *entries*; a close is risk-
        #       reducing and should fill reliably at base aggression, and must clear
        #       the full position (abs(position)) so the no-flip machine returns to
        #       flat. base_offset preserves v1 close behavior exactly.
        cur = market.price
        base_offset = self.aggression_ticks * self.tick_size

        # Tech: the §4.2 state machine — from flat, open in the signal's direction;
        #       while long, only a SELL acts (closes, never flips); while short,
        #       only a BUY acts (closes, never flips).
        # Why:  no pyramiding / no flipping in v1 — a flip would be two economic
        #       actions (close + open) collapsed into one tick, which muddies PnL
        #       attribution, so same-direction-while-in-position is deliberately a
        #       no-op and an opposing signal closes only.
        order: Optional[OrderEvent] = None
        if self.position == 0:
            # Tech: entry-only conviction gate — a below-floor signal from flat
            #       submits nothing (the unconditional cancel above still ran, so
            #       any resting order is gone per §4.4).
            # Why:  weak-conviction opens are the entries this loop wants to skip
            #       (reversal plan decision 2); a gated signal is still a signal,
            #       so a stale forecast's order must not outlive it. Closes are
            #       never gated — see __init__.
            if strength < self.entry_floor:
                return None
            offset = self._entry_offset(strength)
            qty = self._entry_qty(strength)
            if direction == "BUY":
                order = OrderEvent(
                    market.timestamp, "BUY", cur + offset, "OPEN", quantity=qty
                )
            else:
                order = OrderEvent(
                    market.timestamp, "SELL", cur - offset, "OPEN", quantity=qty
                )
        elif self.position > 0:
            if direction == "SELL":
                order = OrderEvent(
                    market.timestamp, "SELL", cur - base_offset, "CLOSE",
                    quantity=abs(self.position),
                )
        else:
            if direction == "BUY":
                order = OrderEvent(
                    market.timestamp, "BUY", cur + base_offset, "CLOSE",
                    quantity=abs(self.position),
                )

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
        qty = abs(self.position)
        if self.position > 0:
            order = OrderEvent(
                market.timestamp, "SELL", cur - offset, "CLOSE", quantity=qty
            )
        else:
            order = OrderEvent(
                market.timestamp, "BUY", cur + offset, "CLOSE", quantity=qty
            )
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
