"""EntryPercentStop: exit when price moves a fixed fraction of the ENTRY price
against the position.

Entry-anchored, percent-width: the stop sits ``entry_price * stop_pct`` on the
adverse side of the entry fill and never moves, so the point gap is set once,
by the price level at entry. This is the live loop's requested "float" —
``max_daily_loss_points`` is a fixed point budget that means different
fractions of the contract at different price levels (the day tape spans
6 952 to 26 000); a 3 % stop is ~209 points at a 2011 entry and ~780 at a
2026 one. Unlike ``TrailingStop`` (day_trail post-mortem: fires at V-bottoms,
every width discarded), the reference is the entry, not the best print — a
winner's open profit is never given back by the rule itself.

The rule is stateless: ``should_exit`` reads only the ``PositionState``
snapshot the run loop rebuilds every tick, so a re-entry minutes after a
stop fires is handled with no arming machinery at all.
"""
from ...events import MarketEvent
from ...exits.base import ExitRule, PositionState


class EntryPercentStop(ExitRule):
    def __init__(self, stop_pct: float):
        # Tech: require a fractional width, then store it.
        # Why:  a non-positive stop fires immediately (or never) and a width
        #       >= 1 puts a long's stop at a non-positive price — both are
        #       meaningless; the width is a fraction of the entry price, not
        #       points, which is the whole point of the rule.
        if not 0.0 < stop_pct < 1.0:
            raise ValueError(f"stop_pct must be in (0, 1), got {stop_pct}")
        self.stop_pct = stop_pct

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: offset = this position's entry price * stop_pct; a long exits
        #       when price falls to/below entry − offset, a short when it
        #       rises to/above entry + offset.
        # Why:  the width scales with the entry's price level (the era-
        #       dependence the trail sweep measured: fixed points meant
        #       19–54 % of price in the train era and ~8 % in 2026), the
        #       comparison flips with side, and <=/>= make the stop fire
        #       exactly at the threshold, not only past it — SPEC §4.5's
        #       placement-time classification then handles the fill.
        offset = position.entry_price * self.stop_pct
        if position.side == "LONG":
            return market.price <= position.entry_price - offset
        return market.price >= position.entry_price + offset
