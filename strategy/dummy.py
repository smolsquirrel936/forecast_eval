"""Phase 1 sanity emitter: alternates BUY/SELL on every call.

Combined with Trader's no-flip state machine, this produces a clean
open-then-close round-trip every two forecast boundaries — ideal for
hand-verifying fills and fee math.
"""
from itertools import cycle
from typing import Literal, Optional

from ..events import Forecast
from .base import SignalEmitter


class DummyAlternatingEmitter(SignalEmitter):
    def __init__(self, start: str = "BUY"):
        # Tech: validate the start side, then build an infinite BUY/SELL (or
        #       SELL/BUY) cycle.
        # Why:  itertools.cycle gives an endless alternating stream with no counter
        #       to manage; rejecting bad `start` early prevents a silently wrong
        #       trade direction. NOTE: this iterator is per-run state — reusing one
        #       instance across sweep iterations would resume mid-cycle (see SPEC).
        if start not in ("BUY", "SELL"):
            raise ValueError("start must be 'BUY' or 'SELL'")
        order = ("BUY", "SELL") if start == "BUY" else ("SELL", "BUY")
        self._cycle = cycle(order)

    def emit(self, forecast: Optional[Forecast] = None) -> Literal["BUY", "SELL", "HOLD"]:
        # Tech: return the next side in the cycle, ignoring the forecast entirely.
        # Why:  the dummy has no model — it exists to exercise execution/fee plumbing
        #       deterministically; with the no-flip Trader, alternating BUY/SELL is a
        #       clean open-then-close round trip every two calls.
        return next(self._cycle)  # type: ignore[return-value]
