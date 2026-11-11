"""DAY's take-profit trail: an exit that arms only once the trade is in profit.

Why:  the day_trail post-mortem (2026-10-11) measured pure fixed-point trails
armed from the entry fill — every width discarded, because a trail that is
live from the first print fires at V-bottoms on trades that never earned
anything. This rule cannot: it does nothing until the best price since entry
is ``arm_pct`` of the entry in the position's favour, and from then on it
protects part of the open gain. Two give-back shapes, exactly one per rule:

  * ``giveback_frac`` f: exit when the open gain falls to (1 - f) of its
    maximum — the stop widens with the gain banked (fullday's GivebackTrail
    with the arm level made a knob instead of a 2% constant).
  * ``trail_pct`` y: exit when price falls y of the best price below it (a
    long) — a classic percent trail, width fixed relative to price.

``AnyOf`` composes it with the package's ``EntryPercentStop`` (copied from
``strategy/fullday/exits.py``; packages never import a sibling).

The rule is STATEFUL (best price since entry), so ``adoptable = False``; like
fullday's rules it is ``restorable`` from that one ``best_price``, which live
writes to ``exit_state_<slug>.json`` and ``--resume`` restores. DAY's Trader
does not carry an entry across a live roll, so a roll restarts the trail.
"""
from typing import List, Optional

from ...events import MarketEvent
from ...exits.base import ExitRule, PositionState


class ProfitTrail(ExitRule):
    """Arm at ``arm_pct`` in favour, then exit on a give-back from the best price."""

    # Tech: stateful — best price since entry under the entry-identity key.
    # Why:  a live resume cannot know the best price the dead process saw;
    #       see ``ExitRule.adoptable``. ``restorable`` covers that instead.
    adoptable = False
    restorable = True
    state_keys = ("best_price",)

    def __init__(
        self,
        arm_pct: float,
        *,
        giveback_frac: Optional[float] = None,
        trail_pct: Optional[float] = None,
    ):
        # Tech: require a fractional arm level and exactly one give-back shape,
        #       itself a fraction in (0, 1).
        # Why:  an arm <= 0 is the pure trail the day_trail loop already killed;
        #       a give-back of 0 fires the tick after arming and one of 1 never
        #       fires; two shapes at once would make a cell's label ambiguous.
        if not 0.0 < arm_pct < 1.0:
            raise ValueError(f"arm_pct must be in (0, 1), got {arm_pct}")
        if (giveback_frac is None) == (trail_pct is None):
            raise ValueError("set exactly one of giveback_frac / trail_pct")
        for name, v in (("giveback_frac", giveback_frac), ("trail_pct", trail_pct)):
            if v is not None and not 0.0 < v < 1.0:
                raise ValueError(f"{name} must be in (0, 1), got {v}")
        self.arm_pct = arm_pct
        self.giveback_frac = giveback_frac
        self.trail_pct = trail_pct
        self._best: Optional[float] = None
        self._key = None

    def export_state(self):
        # Tech: the whole per-trade state is the best price; None before one.
        # Why:  live persists this to exit_state_<slug>.json for --resume.
        return None if self._best is None else {"best_price": self._best}

    def restore_state(self, position: PositionState, state) -> None:
        # Tech: seed the best price under the adopted snapshot's identity.
        # Why:  restoring under that key is what stops should_exit's
        #       new-trade check from resetting it on the first print.
        self._key = (position.entry_bar_idx, position.entry_price, position.side)
        self._best = float(state["best_price"])

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: reset the best price on a new entry identity (bar, price, side).
        # Why:  ``run.py`` rebuilds the PositionState snapshot every tick, so
        #       the identity is the only way to see a re-entry; two trades can
        #       never share a fill bar, so the key is unique.
        key = (position.entry_bar_idx, position.entry_price, position.side)
        if key != self._key:
            self._key = key
            self._best = market.price
        # Tech: express everything in the position's favour (sign +1 long, -1
        #       short): ratchet the best price, test the arm, then the give-back.
        # Why:  one code path for both sides. At the arming print price ==
        #       best, so neither shape can fire on the arm tick itself.
        entry = position.entry_price
        if position.side == "LONG":
            self._best = max(self._best, market.price)
            sign = 1.0
        else:
            self._best = min(self._best, market.price)
            sign = -1.0
        max_gain = sign * (self._best - entry)
        if max_gain < self.arm_pct * entry:
            return False
        if self.giveback_frac is not None:
            gain = sign * (market.price - entry)
            return gain <= (1.0 - self.giveback_frac) * max_gain
        return sign * (self._best - market.price) >= self.trail_pct * self._best


class AnyOf(ExitRule):
    """Compose exit rules: fire when any wrapped rule fires.

    Copied from ``strategy/fullday/exits.py`` (packages never import a sibling).
    """

    def __init__(self, rules: List[ExitRule]):
        # Tech: the composite is adoptable only if every member is, and
        #       restorable when every non-adoptable member is restorable; its
        #       state keys are the union of theirs.
        # Why:  a live resume must not run one resumable and one unresumable
        #       rule as a set — the set is as resumable as its weakest member.
        self.rules = list(rules)
        self.adoptable = all(r.adoptable for r in self.rules)
        self._stateful = [r for r in self.rules if not r.adoptable]
        self.restorable = bool(self._stateful) and all(
            r.restorable for r in self._stateful)
        self.state_keys = tuple(sorted(
            {k for r in self._stateful if r.restorable for k in r.state_keys}))

    def export_state(self):
        # Tech: merge the stateful members' states; None until each holds one.
        # Why:  shared keys name the same quantity, so the first value stands.
        merged = {}
        for rule in self._stateful:
            state = rule.export_state()
            if state is None:
                return None
            for key, value in state.items():
                merged.setdefault(key, value)
        return merged

    def restore_state(self, position: PositionState, state) -> None:
        # Tech: hand each stateful member the keys it declares.
        # Why:  adopt_position has already checked state carries state_keys.
        for rule in self._stateful:
            rule.restore_state(position, {k: state[k] for k in rule.state_keys})

    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool:
        # Tech: any() with short-circuit — the first True wins the tick.
        # Why:  a skipped later rule's state update is harmless: the close is
        #       already submitted and every stateful rule resets on the next
        #       trade's entry identity.
        return any(
            r.should_exit(position, market, bar_idx=bar_idx) for r in self.rules
        )
