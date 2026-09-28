"""NIGHT's strategy wiring — the ``PINNED`` configuration.

Provenance of the knobs below: ``results.tsv`` commit ``4282d33``, re-measured
on the rebuilt night tape (TAPE_REBUILD T5, 2026-09-01) over
``TXF_RPT_minute_night.csv`` at warmup 538 790 — the loop's own window — but at
**fee 0.00015**, the harness default at the time; it is 0.0002 now.

**No Calmar carries out of that row** (DAYNIGHT_TOTO2_RESET_PLAN §6 Q4): an
inherited high one would block the loop's progress, so ``results_night.tsv``
starts empty and ``iter01`` — this package unedited, at the current fee — sets
the first best. The row's own figures stay in ``results.tsv`` and in
``live/config.py:PINNED``'s ``source_commit``/``source_calmar``, which
``tests/test_live_config.py`` asserts against each other.

The knobs live here and nowhere else: since LIVE_STRATEGY_ROUTING_PLAN D2 a
live run builds from ``make_strategy`` below, so an edit to this package is a
live change.

NOT a claim this beats the market: over the evaluated window it trails
buy-and-hold by 9 205 points. The Calmar is earned by a small drawdown.
"""
from typing import Any, Dict

from .emitter import ThresholdEmitter
from .forecaster import make_forecaster
from .trader import Trader

# Tech: this session's default knobs — the twelve values `real_data.py`'s
#       `--strategy` resolves against, keyed by the CLI destination name.
# Why:  STRATEGY_SPLIT_PLAN §1/D4. Starting each package at its *pinned* config
#       is what makes the split a pure refactor: `--strategy night` must
#       reproduce the recorded run exactly (gate G1), so any deviation is a bug
#       in the split rather than a strategy change. An explicit CLI flag still
#       beats these; the run banner prints which knob came from where.
PARAMS: Dict[str, Any] = {
    # forecaster
    "checkpoint": "Datadog/Toto-2.0-1B",
    "context_length": 3008,
    "horizon_bars": 30,
    "stride_bars": 30,
    # emitter
    # night_iter05: 0.00045 -> 0.0006. Offline emitter replay (bit-exact on
    # the recorded signal counts) says the wider entry band drops the marginal
    # entries whose re-entry paths, not their own PnL, were the drag.
    # night_iter07: 0.0006 -> 0.00065 (grid top around the incumbent).
    "buy_threshold": 0.00065,
    "sell_threshold": -0.00045,
    # night_iter06: 0.5 -> 0.55. Rung two of the offline-replay ladder:
    # cutting barely-convictioned entries (dir_prob in [0.5, 0.55)) traded
    # ~4 points of net for ~1000 of drawdown in the replay.
    "min_prob": 0.55,
    "full_confidence_prob": 0.9,
    # night_iter08: 3.0 -> 20.0 (declared at iter08; PARAMS entry added so the
    # knob is recorded in params.json and reachable via --param). The trend
    # filter's short-window confirmation now demands a real ~20-point early->
    # late move instead of the class-default non-opposing 3.
    # night_iter09: 20.0 -> 15.0 (grid top: the tt plateau peaks at 15).
    # night_iter10: 15.0 -> 12.0 (finer grid top on the incumbent config).
    "min_trend_ticks": 12.0,
    # trader
    "aggression_ticks": 0,
    "max_aggression_ticks": None,
    "max_position": 1,
    "forced_close": False,
    # night_iter02: session-declared knob (beyond the shared twelve) — the
    # Trader's long-only entry veto. `--param long_only=False` restores shorts.
    "long_only": True,
}

# Tech: the emitter knobs still absent from PARAMS — `short_window`,
#       `path_momentum`, `trend_filter`.
# Why:  `real_data.py` has never exposed them as flags, so every recorded run
# used `ThresholdEmitter`'s own class defaults (3 / True / True). Leaving them
# to the copied emitter's defaults reproduces those runs exactly; a search that
# wants to move them edits `emitter.py`, which this package owns.
# `min_trend_ticks` was the exception: iter08 declared it (see PARAMS) once the
# offline replay had a use for it, following the same D6 pattern as long_only.


def make_strategy(
    *,
    warmup_bars: int,
    tick_size: float = 1.0,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build this session's Forecaster / SignalEmitter / Trader.

    ``warmup_bars`` and ``tick_size`` come from the *run*, not the strategy —
    the first is the train/eval split the entry point chose, the second is the
    contract's tick grid. Everything else defaults from ``PARAMS`` and may be
    overridden by an explicit CLI flag.

    Returns a dict with keys ``forecaster``, ``emitter``, ``make_trader``,
    ``exit_rule``, ``forced_close``, ``params``.
    """
    # Tech: resolve the knobs — package defaults first, explicit overrides last —
    #       and reject any override that is not a knob this package declares.
    # Why:  a silently-ignored typo would run the *pinned* value while the command
    #       line said otherwise, which is the class of mismatch `live/config.py`
    #       was written to end. Failing loudly is the point.
    unknown = set(overrides) - set(PARAMS)
    if unknown:
        raise ValueError(
            f"unknown strategy override(s) {sorted(unknown)}; "
            f"this package declares {sorted(PARAMS)}"
        )
    p = {**PARAMS, **overrides}

    # Tech: build the forecaster and emitter now, but return the Trader as a
    #       *factory* over Execution rather than as an instance.
    # Why:  `run_backtest` owns `Execution` (fee_rate is a run parameter), so a
    #       Trader cannot be constructed before the run exists. A factory also
    #       matches `run.fee_sensitivity_sweep`'s `make_emitter`/`make_forecaster`
    #       convention, which exists because these objects are stateful and
    #       reusing one across runs silently produces wrong numbers.
    forecaster = make_forecaster(
        warmup_bars=warmup_bars,
        stride_bars=p["stride_bars"],
        horizon_bars=p["horizon_bars"],
        context_length=p["context_length"],
        checkpoint=p["checkpoint"],
    )
    emitter = ThresholdEmitter(
        buy_threshold=p["buy_threshold"],
        sell_threshold=p["sell_threshold"],
        min_prob=p["min_prob"],
        full_confidence_prob=p["full_confidence_prob"],
        min_trend_ticks=p["min_trend_ticks"],
    )

    def make_trader(execution: Any) -> Trader:
        return Trader(
            execution=execution,
            tick_size=tick_size,
            aggression_ticks=p["aggression_ticks"],
            max_position=p["max_position"],
            max_aggression_ticks=p["max_aggression_ticks"],
            long_only=p["long_only"],
        )

    return {
        "forecaster": forecaster,
        "emitter": emitter,
        "make_trader": make_trader,
        "exit_rule": None,
        "forced_close": p["forced_close"],
        "params": p,
    }
