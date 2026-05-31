"""FixedStopLoss: exit when adverse excursion exceeds N ticks from entry."""
from ..events import MarketEvent
from .base import ExitRule, PositionState


class FixedStopLoss(ExitRule):
    def __init__(self, stop_loss_ticks: int, tick_size: float = 1.0):
        # Tech: require a positive tick distance, then store it with the tick size.
        # Why:  a non-positive stop would trigger immediately (or never), which is
        #       meaningless; tick_size converts the tick count into a price offset.
        if stop_loss_ticks <= 0:
            raise ValueError("stop_loss_ticks must be > 0")
        self.stop_loss_ticks = stop_loss_ticks
        self.tick_size = tick_size

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: convert the stop to a price offset; for a long, exit when price
        #       falls to/below entry−offset; for a short, when it rises to/above
        #       entry+offset.
        # Why:  the stop sits on the *adverse* side of entry for each direction, so
        #       the comparison flips with side; <=/>= make the stop fire exactly at
        #       the threshold, not only past it.
        stop_offset = self.stop_loss_ticks * self.tick_size
        if position.side == "LONG":
            return market.price <= position.entry_price - stop_offset
        return market.price >= position.entry_price + stop_offset
