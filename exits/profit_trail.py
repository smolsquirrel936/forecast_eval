"""ProfitTrail: a take-profit trail that arms only once the trade is in profit.

The rule does nothing until the best price since entry is ``arm_pct`` of the
entry in the position's favour; from then on it protects part of the open gain
with exactly one of two give-back shapes:

  * ``trail_pct`` y: exit when price falls y of the best price below it (a
    long; a short mirrors it) — a classic percent trail.
  * ``giveback_frac`` f: exit when the open gain falls to (1 - f) of its
    maximum — the stop widens with the gain banked.

Why an arm: DAY's day_trail loop (2026-10-11) killed every pure trail armed
from the entry fill — it fires at V-bottoms on trades that never earned
anything. A trade that never reaches ``arm_pct`` can never fire this rule.

Measured on DAY only (``autoresearch_day_tptrail_1111``,
``TPTRAIL_POSTMORTEM.md``); every other package declares the knobs off, and a
width tuned for one session does not transfer to another.

The rule is STATEFUL (best price since entry), so ``adoptable = False``; it is
``restorable`` from that one ``best_price``, which live writes to
``exit_state_<slug>.json`` and ``--resume`` restores (or the operator types
with ``--exit-state best_price=...``). The state is keyed on the entry identity
(bar, price, side): a Trader that carries the original entry across a live
roll keeps the trail armed through it; one that does not restarts it.

Promoted from ``strategy/day/profit_trail.py`` (2026-11-11), the way
``EntryPercentStop`` was promoted from the day package on 2026-10-12.
"""
from typing import Optional

from ..events import MarketEvent
from .base import ExitRule, PositionState


class ProfitTrail(ExitRule):
    """Arm at ``arm_pct`` in favour, then exit on a give-back from the best price."""

    # Tech: stateful — best price since entry under the entry-identity key —
    #       so not adoptable, but restorable from that one number.
    # Why:  a live resume cannot know the best price the dead process saw; see
    #       `ExitRule.adoptable` / `ExitRule.restorable`.
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
        # Why:  `run.py` rebuilds the PositionState snapshot every tick, so the
        #       identity is the only way to see a re-entry; two trades can never
        #       share a fill bar, so the key is unique.
        key = (position.entry_bar_idx, position.entry_price, position.side)
        if key != self._key:
            self._key = key
            self._best = market.price
        # Tech: express everything in the position's favour (sign +1 long, -1
        #       short): ratchet the best price, test the arm, then the give-back.
        # Why:  one code path for both sides. At the arming print price == best,
        #       so neither shape can fire on the arm tick itself.
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


def profit_trail_from_knobs(
    arm_pct: Optional[float],
    trail_pct: Optional[float] = None,
    giveback_frac: Optional[float] = None,
) -> Optional[ProfitTrail]:
    """Build the rule from a package's three ``tp_*`` knobs, or None when off."""
    # Tech: no arm -> no rule; a give-back shape without an arm is refused.
    # Why:  every package declares the same three knobs and must turn them into
    #       the same rule the same way; a shape set without an arm would be
    #       silently inert, so the typo stops the run instead.
    if arm_pct is None:
        if trail_pct is not None or giveback_frac is not None:
            raise ValueError("tp_trail_pct / tp_giveback_frac need tp_arm_pct")
        return None
    return ProfitTrail(arm_pct, trail_pct=trail_pct, giveback_frac=giveback_frac)
