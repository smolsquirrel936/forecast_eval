"""BarsInTradeExit: close after N bars in the position — the horizon exit.

FULL-DAY package's own exit rule (branch ``forecaster_eval_1116``). The
signal-level study of Timestral's gated 5-minute stream found its edge is a
first-minute mean-reversion bounce; the strategy's default shape (no exit
rule, no-flip) holds such a signal until the next *opposite* gated signal —
weeks, in practice (the first no-exit run held a trade from 2018-03-20 to
2018-04-18 and drew down 16 402 points). This rule closes the position
after ``bars`` bars, the natural "hold for the forecast's horizon" semantics
that the study measured: enter on the signal, exit at the horizon's end.

Stateless: ``should_exit`` reads only ``entry_bar_idx`` and the current
``bar_idx`` from the snapshots the run loop rebuilds every tick.

ADOPTABLE AS EXPIRED (simtrade_0929 port, 2026-11-19, user decision): a live
restart that adopts a held lot seeds ``entry_bar_idx`` with the flat sentinel
-1 (``live/runtime.py`` ``adopt_position``), because the entry bar belongs to
the dead process. This rule reads that sentinel as "the hold is already over"
and fires on the first bar, so the adopted lot is closed at once. Any restart
longer than ``bars`` minutes would have closed the lot in the backtest anyway.
"""
from ...events import MarketEvent
from ...exits.base import ExitRule, PositionState


class BarsInTradeExit(ExitRule):
    # Tech: adoptable — an adopted lot (sentinel entry_bar_idx < 0) is
    #       treated as past its hold and fires on the first bar queried.
    # Why:  ExitRule.adoptable's contract forbids timing a rule FROM the
    #       sentinel; this rule never does — it reads the sentinel as
    #       "expired" instead. A held lot from a dead process is at least as
    #       old as the restart took, which is minutes against a 5-bar hold,
    #       so closing at once is what the backtest would have done.
    adoptable = True

    def __init__(self, bars: int):
        # Tech: require a positive bar count.
        # Why:  zero would fire on the entry bar's own exit check (before
        #       the position has seen a single bar) and a negative value is
        #       nonsense; failing loudly beats a rule that never fires.
        if bars < 1:
            raise ValueError(f"bars must be >= 1, got {bars}")
        self.bars = bars

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: exit once the position has been open for `bars` bars.
        # Why:  entry_bar_idx is the bar the entry fill landed on, so
        #       bar_idx - entry_bar_idx counts the bars actually held —
        #       the same clock a horizon forecast covers. The comparison
        #       is >= so a fill on the same bar as the signal already
        #       counts its first held bar on the next tick's check.
        # Tech: a negative entry_bar_idx (adoption's sentinel) fires at once.
        # Why:  see the class comment — an adopted lot's hold is over. A
        #       backtest or live-opened position always carries the fill's
        #       real bar index (>= 0), so this branch is live-restart only.
        if position.entry_bar_idx < 0:
            return True
        return bar_idx - position.entry_bar_idx >= self.bars
