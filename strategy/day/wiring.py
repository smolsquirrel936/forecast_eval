"""DAY's strategy wiring — the ``PINNED_DAY`` configuration.

Provenance of the knobs below: ``results.tsv`` commit ``3c3ae38``, re-measured
on the rebuilt day tape (TAPE_REBUILD T7, 2026-09-01) over
``TXF_RPT_minute_day.csv`` at warmup 676 123 — the loop's own window — but at
**fee 0.00015**, the harness default at the time; it is 0.0002 now.

**No Calmar carries out of that row** (DAYNIGHT_TOTO2_RESET_PLAN §6 Q4): an
inherited high one would block the loop's progress, so ``results_day.tsv``
starts empty and ``iter01`` — this package unedited, at the current fee — sets
the first best. The row's own figures stay in ``results.tsv`` and in
``live/config.py:PINNED_DAY``'s ``source_commit``/``source_calmar``.

The knobs live here and nowhere else: since LIVE_STRATEGY_ROUTING_PLAN D2 a
live run builds from ``make_strategy`` below, so an edit to this package is a
live change.

Four facts DAY_SESSION_PLAN §2.6 names as the ones most likely to be lost when
these components are rewritten. They are properties of THIS strategy, not of
the harness, so they live in the file whoever rewrites it will read:

  * The pin was selected on the **worst-half** score, not the headline Calmar,
    and was deliberately NOT moved to the better cell after the tape rebuild
    (§2.3). Re-tuning on a re-measurement is the discipline D1 rejected.
  * The roll instant is **12:45**, not one calendar day after expiry (§4 C4).
  * **89.4 % of DAY's gross accrues overnight** (C8) — the position is held
    through the sessions this deployment does not trade.
  * Median hold is **20 days** (p90 57, max 173) and 100 % of trades span more
    than one calendar day, which is why ``forced_close`` is False here.

It trails eval-window buy-and-hold by 24 419 points, and it has never traded.
"""
from typing import Any, Dict

from .emitter import ThresholdEmitter
from .forecaster import make_forecaster
from .trader import Trader

# Tech: this session's default knobs — the twelve values `real_data.py`'s
#       `--strategy` resolves against, keyed by the CLI destination name.
# Why:  STRATEGY_SPLIT_PLAN §1/D4. Starting each package at its *pinned* config
#       is what makes the split a pure refactor: `--strategy day` must
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
    "buy_threshold": 0.001,
    "sell_threshold": -0.001,
    "min_prob": 0.65,
    "full_confidence_prob": 0.9,
    # trader
    "aggression_ticks": 0,
    "max_aggression_ticks": None,
    "max_position": 1,
    "forced_close": False,
}

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

    return {
        "forecaster": forecaster,
        "emitter": emitter,
        "make_trader": make_trader,
        "exit_rule": None,
        "forced_close": p["forced_close"],
        "params": p,
    }
