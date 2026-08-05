"""Mutable strategy wiring for the autoresearch loop.

The autoresearch agent edits THIS file to change how the frozen harness
constructs the strategy stack (which emitter, which exit rule, trader
kwargs like aggression_ticks / max_position / forced_close). The harness
(`autoresearch_eval.evaluate`) imports `make_strategy` from here, so any
edit takes effect on the next `evaluate` call.

The emitter classes themselves live in `strategy/*.py` (also mutable);
`trader.py` (also mutable) holds the Trader state machine. This file is
just the wiring seam between them and the frozen harness.
"""
from typing import Any, Dict, Optional

from .base import SignalEmitter
from ..exits.base import ExitRule


def make_strategy() -> Dict[str, Any]:
    """Return the strategy stack for the current experiment.

    Returns a dict with keys:
        emitter: SignalEmitter instance
        exit_rule: Optional[ExitRule] — None for no exit rule
        aggression_ticks: int — limit offset in ticks
        max_position: int — max contracts
        max_aggression_ticks: Optional[int] — confidence-scaled aggression ramp
        forced_close: bool — force close at session boundaries

    The autoresearch agent edits the body of this function to swap emitters,
    adjust thresholds, add exit rules, change sizing, etc.
    """
    from .threshold import ThresholdEmitter

    return {
        "emitter": ThresholdEmitter(
            buy_threshold=0.0005,
            sell_threshold=-0.0005,
        ),
        "exit_rule": None,
        "aggression_ticks": 0,
        "max_position": 1,
        "max_aggression_ticks": None,
        "forced_close": False,
    }