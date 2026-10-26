"""FULL-DAY's own exit rules: profit protection (reversal loop decisions 9-11).

Why:  the reversal post-mortem measured that the median trade gives back 47% of
its best open gain (median 1.3% below the in-trade best, 32.5 h from best price
to exit), and 46 of 101 losers had been +1% or better — the band-only exit waits
for the model to change its mind, which phase E showed costs either hours (full
band) or the Calmar (narrow band). These rules read the tape, not the model:

  * ``BreakEvenLock`` (G1, decision 9): once the best price since entry is
    ``lock_after_pct`` in the position's favour, exit on a return to the entry.
  * ``GivebackTrail`` (G2, decision 10): arms only once the best price is
    ``GIVEBACK_ARM_PCT`` (2%) in favour, then exits when the open gain falls to
    ``(1 - giveback_frac)`` of its maximum. The arm is the day_trail lesson:
    a fixed-width trail fires at V-bottoms, and this one cannot fire on a trade
    that never earned 2%.
  * ``ChannelExit`` (H, decision 11): once the position is at least N bars
    old, a long exits when price prints below the lowest of its last N
    in-position bars, a short above the highest — model-free, the tape itself
    says the trend broke, with no forecast lag.
  * ``AnyOf``: the composition seam — fire when any wrapped rule fires, so the
    percent stop composes with the protection rules (decision 9).

The rules are STATEFUL (best price since entry, arming latch) and deliberately
``adoptable = False``: a live ``--resume`` into a held lot cannot recover the
best price the dead process saw, so ``live/runtime.py`` refuses the adoption
rather than run a lock armed on invented state. A roll closes and reopens the
position, and the rules detect the new entry (entry_bar_idx/price/side key) and
restart their state at each roll.

``run.py`` queries the exit rule only while a position is open (Step 2), so
these rules never see flat bars and their per-tick state updates cost nothing
while flat.
"""
from typing import List

from ...events import MarketEvent
from ...exits.base import ExitRule, PositionState
from ...exits.pct_stop import EntryPercentStop

# Tech: the give-back trail's arm level — a fraction of the entry price in the
#       position's favour, fixed for the module.
# Why:  decision 10 pins the arm at 2% so the search moves only the give-back
#       fraction; a trade that never earns 2% can never fire the trail, which is
#       the anti-V-bottom property the day_trail post-mortem asked for.
GIVEBACK_ARM_PCT = 0.02


class BreakEvenLock(ExitRule):
    """G1: lock a break-even exit once the trade has been well in profit."""

    # Tech: stateful — tracks the best price since entry — so the rule cannot
    #       run against a live-adopted position whose history died with the
    #       process that earned it.
    # Why:  see ``ExitRule.adoptable``; refusing the adoption beats arming the
    #       lock on an invented best price.
    adoptable = False

    def __init__(self, lock_after_pct: float):
        # Tech: require a fractional arm level.
        # Why:  a non-positive arm locks immediately (a degenerate stop at the
        #       entry) and >= 1 can never arm; both are meaningless widths.
        if not 0.0 < lock_after_pct < 1.0:
            raise ValueError(
                f"lock_after_pct must be in (0, 1), got {lock_after_pct}"
            )
        self.lock_after_pct = lock_after_pct
        self._best: float | None = None
        self._key = None

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: detect a new position by its entry identity (bar, price, side)
        #       and reset the tracked best price when it changes.
        # Why:  the rule holds per-trade state (the best price since entry);
        #       ``run.py`` rebuilds the PositionState snapshot every tick, so
        #       the entry identity is the only way to see a roll or a re-entry.
        #       Two consecutive trades cannot share a fill bar (a close fills
        #       before the next open is even placed), so the key is unique.
        key = (position.entry_bar_idx, position.entry_price, position.side)
        if key != self._key:
            self._key = key
            self._best = market.price
        if position.side == "LONG":
            # Tech: long — best is the highest print; armed once it reaches
            #       entry*(1+lock_after_pct); exit when price returns to entry.
            # Why:  decision 9 — the winner has banked enough open gain that a
            #       full round trip back to the entry is no longer acceptable;
            #       <= (not <) fires exactly at the entry, and a print below it.
            self._best = max(self._best, market.price)
            armed = self._best >= position.entry_price * (1.0 + self.lock_after_pct)
            return armed and market.price <= position.entry_price
        # Tech: short — mirror image: lowest print, armed at entry*(1-arm),
        #       exit when price returns up to the entry.
        self._best = min(self._best, market.price)
        armed = self._best <= position.entry_price * (1.0 - self.lock_after_pct)
        return armed and market.price >= position.entry_price


class GivebackTrail(ExitRule):
    """G2: once armed (2% in favour), exit on giving back a fraction of the max gain."""

    # Tech: stateful — best price since entry plus the arm latch.
    # Why:  same adoption argument as BreakEvenLock.
    adoptable = False

    def __init__(self, giveback_frac: float):
        # Tech: require a fractional give-back.
        # Why:  0 fires the tick after arming (a stop at the arm level), and
        #       >= 1 never fires; neither is a width worth scoring.
        if not 0.0 < giveback_frac < 1.0:
            raise ValueError(
                f"giveback_frac must be in (0, 1), got {giveback_frac}"
            )
        self.giveback_frac = giveback_frac
        self._best: float | None = None
        self._key = None

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: same new-position reset as BreakEvenLock — entry identity key,
        #       best price re-seeded at the first in-position print.
        # Why:  the trail's width is a fraction of THIS trade's maximum open
        #       gain, so it must not inherit the previous trade's extreme.
        key = (position.entry_bar_idx, position.entry_price, position.side)
        if key != self._key:
            self._key = key
            self._best = market.price
        if position.side == "LONG":
            # Tech: long — track the highest print; arm once the best price is
            #       GIVEBACK_ARM_PCT above the entry; once armed, exit when the
            #       open gain falls to (1-giveback_frac) of the maximum gain.
            # Why:  decision 10 — the arm gate is the day_trail lesson (a fixed
            #       trail fires at V-bottoms): a trade that never earned 2%
            #       cannot fire, and the width grows with the gain banked. At
            #       the arming print gain == max_gain, so the rule never fires
            #       on the arm tick itself.
            self._best = max(self._best, market.price)
            if self._best < position.entry_price * (1.0 + GIVEBACK_ARM_PCT):
                return False
            max_gain = self._best - position.entry_price
            return (market.price - position.entry_price) <= \
                (1.0 - self.giveback_frac) * max_gain
        # Tech: short — mirror image with the lowest print.
        self._best = min(self._best, market.price)
        if self._best > position.entry_price * (1.0 - GIVEBACK_ARM_PCT):
            return False
        max_gain = position.entry_price - self._best
        return (position.entry_price - market.price) <= \
            (1.0 - self.giveback_frac) * max_gain


class ChannelExit(ExitRule):
    """H: exit when price breaks the extreme of the last N in-position bars.

    Model-free: the tape itself says the trend broke, with no forecast lag.
    """

    # Tech: stateful — collects the in-position prints.
    # Why:  same adoption argument as the protection rules; a live resume
    #       cannot recover the dead process's channel.
    adoptable = False

    def __init__(self, channel_exit_bars: int):
        # Tech: require a whole, positive bar count.
        # Why:  a channel of 0 bars has no window to break; a non-integer has
        #       no meaning on a bar grid.
        if not isinstance(channel_exit_bars, int) or channel_exit_bars < 1:
            raise ValueError(
                f"channel_exit_bars must be an int >= 1, got {channel_exit_bars}"
            )
        self.channel_exit_bars = channel_exit_bars
        self._prices: List[float] = []
        self._key = None

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: same new-position reset — entry identity key, prints re-seeded.
        # Why:  the channel is built from THIS position's bars only, so a roll
        #       or re-entry starts an empty channel.
        key = (position.entry_bar_idx, position.entry_price, position.side)
        if key != self._key:
            self._key = key
            self._prices = []
        # Tech: collect this print, then require at least N prior in-position
        #       prints before the rule is active ("at least N bars old": the
        #       entry bar itself is print 1, so age N <-> N+1 prints seen).
        # Why:  a channel younger than N bars is a noise window; the age gate
        #       is what makes 1140 bars mean "one fullday trading day".
        self._prices.append(market.price)
        if len(self._prices) <= self.channel_exit_bars:
            return False
        # Tech: the window is the N prints BEFORE the current one — the current
        #       print must sit outside it, so it can never be its own extreme.
        # Why:  decision 11 — a long exits when price prints below the lowest
        #       of its last N in-position bars, a short above the highest;
        #       including the current print in the window would make both
        #       conditions unsatisfiable.
        window = self._prices[-(self.channel_exit_bars + 1):-1]
        if position.side == "LONG":
            return market.price < min(window)
        return market.price > max(window)


class AnyOf(ExitRule):
    """Compose exit rules: fire when any wrapped rule fires (decision 9's seam)."""

    def __init__(self, rules: List[ExitRule]):
        # Tech: store the wrapped rules and compute the composite's adoptable
        #       flag as the AND of theirs, as an instance attribute.
        # Why:  ``adoptable`` is a class attribute on the base (default False);
        #       an instance attribute shadows it cleanly, and the composite is
        #       only as adoptable as its most stateful member — a live resume
        #       must not run one adoptable and one unadoptable rule as a set.
        self.rules = list(rules)
        self.adoptable = all(r.adoptable for r in self.rules)

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: any() with short-circuit — the first True wins the tick.
        # Why:  an any-of is the composition decision 9 asks for (stop_pct
        #       composes with the protection rules); short-circuiting skips
        #       later rules' state updates on a firing tick, which is safe:
        #       the close is already submitted, and every stateful rule resets
        #       on the next trade's new entry identity.
        return any(
            r.should_exit(position, market, bar_idx=bar_idx) for r in self.rules
        )


# Tech: re-export the shared percent stop so wiring composes without a second
#       import site.
# Why:  decision 9 — composition happens here, against the one frozen
#       implementation all three sessions share.
__all__ = [
    "BreakEvenLock",
    "ChannelExit",
    "GivebackTrail",
    "AnyOf",
    "EntryPercentStop",
    "GIVEBACK_ARM_PCT",
]
