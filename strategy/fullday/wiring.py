"""FULL-DAY's strategy wiring — ``fullday_split_iter07`` (SIMTRADE_0929).

**These parameters come from the split loop, scored on its TRAIN 70 % only.**
``autoresearch_split_0.7_fullday`` @ ``394ce85`` moved ``stride_bars`` 30 -> 15
and ``min_prob`` 0.65 -> 0.50 from the DAY band and gate this package inherited
at the split (STRATEGY_SPLIT_PLAN §5 / D5). Calmar 2.8213 over 134 trades on the
train window; the held-out 30 % has never been scored. The row lives in
``results_fullday.tsv``, not ``results.tsv``, so ``PINNED_FULLDAY`` still
records ``source_commit=None`` and names this commit in its ``source_note``.
At 0.50 the probability gate is nearly idle: 0.55 -> 0.50 left the trade
count at 134.

The tape to search on is ``dataset/RPT/TXF_RPT_minute_fullday.csv`` —
2 364 417 bars, 2018-01-02 -> 2026-08-21. NOT the combined
``TXF_RPT_minute.csv``, whose first 15.6 % is day-only because TAIFEX had no
night session before 2017-05-15 (TAPE_REBUILD §2.5).

Since LIVE_STRATEGY_ROUTING_PLAN D2 a live run builds from ``make_strategy``
below, so an edit here is a live change. D1 removed ``live/cli.py``'s refusal of
untuned configs; ``PINNED_FULLDAY`` still records itself unprovenanced.
"""
from typing import Any, Dict

from ...exits.pct_stop import EntryPercentStop
from .emitter import ThresholdEmitter
from .forecaster import make_forecaster
from .trader import Trader

# Tech: this session's default knobs — the twelve values `real_data.py`'s
#       `--strategy` resolves against, keyed by the CLI destination name.
# Why:  STRATEGY_SPLIT_PLAN §1/D4. Starting each package at its *pinned* config
#       is what makes the split a pure refactor: `--strategy fullday` must
#       reproduce the recorded run exactly (gate G1), so any deviation is a bug
#       in the split rather than a strategy change. An explicit CLI flag still
#       beats these; the run banner prints which knob came from where.
PARAMS: Dict[str, Any] = {
    # forecaster
    "checkpoint": "Datadog/Toto-2.0-1B",
    "context_length": 3008,
    "horizon_bars": 30,
    "stride_bars": 15,
    # emitter
    "buy_threshold": 0.001,
    "sell_threshold": -0.001,
    "min_prob": 0.50,
    "full_confidence_prob": 0.9,
    # trader
    "aggression_ticks": 0,
    "max_aggression_ticks": None,
    "max_position": 1,
    "forced_close": False,
    # percent-stop knob — package-declared, beyond the twelve CLI knobs
    # (D6/D7), reachable only through ``--param stop_pct=...``.
    "stop_pct": 0.05,
}

# Tech: the knob's tuned value — 0.05, ADOPTED on simtrade_0929 (2026-10-18).
# Why:  the 2026-10-18 width comparison (outputs/stopcmp_20261018_145644,
#       warmup 5000, --train-bars 1651591) scored train-70% Calmar none
#       2.8213 / 0.03 2.0734 / 0.05 3.2466 / 0.07 2.6855 / 0.10 2.7895 /
#       0.20 and 0.30 = none (inert). 0.05 is the only above-incumbent
#       cell: 142 trades, net +9674 (-136), DD -3477 -> -2980, 12 train
#       firings. It is an ISOLATED peak — 0.07 falls below the incumbent on
#       both windows (whole-data 4.5121) — so the width is fragile. The
#       whole-data figure (7.6706 vs none 6.1216 and 0.10 7.3612) includes
#       the held-out 30%, which the split loop never scored. Replaces 0.10
#       (c7e2178, the catastrophe rail). User decision 2026-10-18. An
#       explicit ``--param stop_pct=...`` still overrides it.

# Tech: the emitter knobs deliberately absent from PARAMS — `short_window`,
#       `min_trend_ticks`, `path_momentum`, `trend_filter`.
# Why:  `real_data.py` has never exposed them as flags, so every recorded run
#       used `ThresholdEmitter`'s own class defaults (3 / 3.0 / True / True,
#       which is also what `LiveConfig` declares). Leaving them to the copied
#       emitter's defaults reproduces those runs exactly; a search that wants to
#       move them edits `emitter.py`, which this package owns.


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
    )

    def make_trader(execution: Any) -> Trader:
        return Trader(
            execution=execution,
            tick_size=tick_size,
            aggression_ticks=p["aggression_ticks"],
            max_position=p["max_position"],
            max_aggression_ticks=p["max_aggression_ticks"],
        )

    # Tech: build the percent stop only when its knob is set; None means no
    #       exit rule, exactly what every recorded run before this knob used.
    # Why:  run_backtest queries the rule every tick a position is open
    #       (run.py Step 2) and submits a closing limit on fire — the
    #       flatten-and-continue semantics the live deployment wants, so the
    #       backtest measures precisely what live would do.
    exit_rule = (
        EntryPercentStop(stop_pct=p["stop_pct"])
        if p["stop_pct"] is not None
        else None
    )

    return {
        "forecaster": forecaster,
        "emitter": emitter,
        "make_trader": make_trader,
        "exit_rule": exit_rule,
        "forced_close": p["forced_close"],
        "params": p,
    }
