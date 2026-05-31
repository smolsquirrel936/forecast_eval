"""ExitRule interface + PositionState carrier (SPEC §3, §4.1 step 2).

An ExitRule is queried each tick when a position is open. Returning True
causes the Trader to submit a closing limit at `current_price ∓ aggression`.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..events import MarketEvent


@dataclass
class PositionState:
    """Snapshot of an open position, supplied to ExitRule each tick."""
    # Tech: an immutable read-only view of the open position — side, size, and the
    #       entry price/bar-index/timestamp.
    # Why:  ExitRules receive this snapshot instead of the live Trader so they can
    #       read entry context (for stop distance, bars-in-trade) but cannot mutate
    #       trading state; entry_bar_idx/timestamp are what time-based stops measure.
    side: Literal["LONG", "SHORT"]
    size: int                       # absolute contract count
    entry_price: float
    entry_bar_idx: int
    entry_timestamp: datetime


class ExitRule(ABC):
    @abstractmethod
    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: subclasses decide, given the open position, the current print, and
        #       the bar index, whether to close now (True) or keep holding (False).
        # Why:  one tiny interface covers stop-loss, time-stop, take-profit, etc.;
        #       the Trader owns the actual order submission, so a rule only expresses
        #       the *decision*, keeping risk logic pluggable (SPEC §4.1 step 2).
        ...
