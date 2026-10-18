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

from ...exits.pct_stop import EntryPercentStop
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
    "stride_bars": 15,
    # emitter
    "buy_threshold": 0.0004,
    "sell_threshold": -0.0004,
    "min_prob": 0.6,
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
#       whole night window, warmup 6000) scored none 7.2974 / 0.03 7.0854 /
#       0.05 9.1078 / 0.07 8.8814 / 0.10 5.6650 / 0.20 and 0.30 = none
#       (inert, max MAE 16.83%). 0.05 is the best cell and 0.07 sits close
#       beside it — the one active width with a neighbour that also beats
#       the incumbent: 381 trades, net +30271 (+1164), DD -3989 -> -3324,
#       11 firings in the tape. It replaces 0.10 (260a033, the catastrophe
#       rail), whose single firing sold the 2024-08-05 gap at -15% and
#       widened DD to -5083. In-sample: night's settings were tuned on this
#       same window (no held-out tail). User decision 2026-10-18. An
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
