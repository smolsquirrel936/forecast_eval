"""AnyOf: compose exit rules — fire when any wrapped rule fires.

``run.py`` and ``live/loop.py`` take ONE exit rule per run; a package that
wants a stop and a take-profit trail hands them this composite. ``any_of``
is the builder every package uses: it drops the rules that are off and
returns None, the bare rule, or an ``AnyOf`` — so a package with only its
percent stop set still returns the bare ``EntryPercentStop``, exactly what
every recorded run used.

Adapted from ``strategy/fullday/exits.py:AnyOf`` (fullday keeps its own copy).
"""
from typing import Iterable, List, Optional

from ..events import MarketEvent
from .base import ExitRule, PositionState


class AnyOf(ExitRule):
    """Fire when any wrapped rule fires."""

    def __init__(self, rules: List[ExitRule]):
        # Tech: the composite is adoptable only if every member is, and
        #       restorable when every non-adoptable member is restorable; its
        #       state keys are the union of theirs.
        # Why:  a live resume must not run one resumable and one unresumable
        #       rule as a set — the set is as resumable as its weakest member.
        #       Instance attributes shadow the base's class defaults cleanly.
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
        # Why:  `adopt_position` has already checked state carries state_keys.
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


def any_of(rules: Iterable[Optional[ExitRule]]) -> Optional[ExitRule]:
    """None, the single rule, or an ``AnyOf`` of the rules that are set."""
    # Tech: drop the Nones, then collapse 0 -> None and 1 -> the bare rule.
    # Why:  a package with only its stop set must return the bare stop (the
    #       shape every recorded run and test pinned), and one with nothing set
    #       must return None so `run_backtest` skips the exit step entirely.
    present = [r for r in rules if r is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return AnyOf(present)
