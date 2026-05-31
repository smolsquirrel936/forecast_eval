"""TimeStop: exit after N bars in position."""
from ..events import MarketEvent
from .base import ExitRule, PositionState


class TimeStop(ExitRule):
    def __init__(self, max_bars: int):
        # Tech: require a positive holding cap and store it.
        # Why:  max_bars <= 0 would close on (or before) the entry bar, never letting
        #       a trade develop; rejecting it surfaces the misconfiguration early.
        if max_bars <= 0:
            raise ValueError("max_bars must be > 0")
        self.max_bars = max_bars

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: exit once bars elapsed since entry (current index − entry index)
        #       reaches the cap.
        # Why:  a pure time-based exit caps how long capital sits in any one trade,
        #       independent of price; measuring in bar indices matches the run loop's
        #       notion of elapsed time without needing wall-clock arithmetic.
        return (bar_idx - position.entry_bar_idx) >= self.max_bars
