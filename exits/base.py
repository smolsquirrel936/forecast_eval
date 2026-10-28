"""ExitRule interface + PositionState carrier (SPEC §3, §4.1 step 2).

An ExitRule is queried each tick when a position is open. Returning True
causes the Trader to submit a closing limit at `current_price ∓ aggression`.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Literal, Optional, Tuple

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
    # Tech: True when `should_exit` reads only `side`, `size` and `entry_price`
    #       from the snapshot — never `entry_bar_idx` or `entry_timestamp`.
    #       Defaults to False; a subclass opts in.
    # Why:  a live restart that adopts a held lot (`live/runtime.py`
    #       `adopt_position`) recovers the venue's side and average entry, but
    #       the entry bar and time belong to the dead process and are seeded
    #       with sentinels. Only a rule that never reads them can run against an
    #       adopted position; the default keeps any rule that has not said so
    #       — a time stop, a trail — refused rather than timed from a sentinel.
    adoptable: bool = False

    # Tech: True when a rule that is NOT adoptable can still be resumed, because
    #       its per-trade state round-trips through `export_state` /
    #       `restore_state` as a flat dict of numbers named in `state_keys`.
    #       Defaults to False with no keys; a subclass opts in.
    # Why:  a stateful rule (a break-even lock's best price since entry) cannot
    #       be rebuilt from the venue's side and entry, but it can be written to
    #       a file while live (`live/exit_state.py`) and read back, or typed in
    #       by hand, at the next `--resume`. Named keys are what make the typed
    #       form checkable: an unknown or missing key refuses the restart.
    restorable: bool = False
    state_keys: Tuple[str, ...] = ()

    def export_state(self) -> Optional[Dict[str, float]]:
        # Tech: the rule's per-trade state as `{key: number}`, or None when it
        #       holds none (no position queried yet, or a stateless rule).
        # Why:  the live loop writes this after every bar's exit check, so it
        #       must be cheap and JSON-safe; None means "nothing worth saving".
        return None

    def restore_state(self, position: PositionState,
                      state: Dict[str, float]) -> None:
        # Tech: seed the per-trade state for `position` from `state`, so the
        #       next `should_exit` continues the trade instead of restarting it.
        # Why:  called once by `adopt_position` on a restorable rule, before
        #       the first bar; a rule that has not opted in must never be
        #       handed state it would silently ignore.
        raise NotImplementedError(
            f"{type(self).__name__} has no restorable state")

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
