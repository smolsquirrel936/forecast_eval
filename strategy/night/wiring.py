"""NIGHT's strategy wiring — the ``PINNED`` configuration.

Provenance: ``results.tsv`` commit ``4282d33``, re-measured on the rebuilt night
tape (TAPE_REBUILD T5, 2026-09-01) — Calmar **3.6006**, 235 trades, net
+24 135.60 points, max drawdown -6 703.24, on ``TXF_RPT_minute_night.csv``.
These values must stay in step with ``live/config.py:PINNED``, which
``tests/test_live_config.py`` asserts against the same row.

NOT a claim this beats the market: over the evaluated window it now trails
buy-and-hold by 9 205 points. The Calmar is earned by a small drawdown.
"""
from typing import Any, Dict

from ...exits.time_stop import TimeStop
from .emitter import ThresholdEmitter      # noqa: F401 -- see RSI note below
from .forecaster import make_forecaster   # noqa: F401 -- see RSI note below
from .rsi import RsiEmitter, make_rsi_forecaster
from .trader import Trader

# Tech: the holding cap, in bars (~840 per night session, so 4000 is ~4.8
#       sessions ~ one calendar week).
# Why:  night-loop iter05. iter03's drawdown anatomy: the only exits are RSI>=80
#       or a later opposing signal, so in a decline the losers ride with no exit
#       at all -- the four disaster longs held 8 to 35 days and carried the max
#       drawdown (-4 074 / -3 178 / -1 495 / -1 367 points). A one-week cap
#       bounds that tail; measured on a scratch replay of iter03's own
#       invocation (validated bit-identical against the harness numbers first),
#       tstop 4000 took net +12 833 -> +15 623 and dd -4 799 -> -2 791, with
#       2000/4000/6000/8000 reading 5.57/5.60/5.03/3.50 -- 2000-4000 is a
#       plateau, 4000 the mid-band cell, not the peak of a spike.
TIME_STOP_BARS = 4000

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
    "buy_threshold": 0.00045,
    "sell_threshold": -0.00045,
    "min_prob": 0.5,
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


# Tech: this package now trades PLAIN RSI -- RSI(14) over 5-minute closes, BUY at
#       20, SELL at 80 -- built in `rsi.py`. `ThresholdEmitter` and
#       `make_forecaster` are still imported but no longer used by
#       `make_strategy`.
# Why:  they are kept reachable for two reasons that are not style. First,
#       `tests/test_strategy_isolation.py` constructs `wiring.ThresholdEmitter`
#       by name to prove the three packages hold distinct classes (G3), so
#       dropping the import would fail the isolation gate for a reason unrelated
#       to isolation. Second, `live/config.py` imports `.emitter` and
#       `.forecaster` as MODULES, not through this file -- so live still trades
#       the pinned Toto2 threshold strategy, and the pin stays recoverable by
#       pointing `make_strategy` back at it. See the divergence warning below.
#
# WARNING -- backtest and live now disagree for this session.
#       `--strategy <session>` (backtest) runs RSI. `--config <session>` (live)
#       runs the pinned Toto2 threshold strategy, because `live/config.py`
#       reaches `.emitter`/`.forecaster` directly and never calls `make_strategy`.
#       That is deliberate: a strategy change must not reach a live deployment as
#       a side effect of a backtest edit. To make live match, `live/config.py`
#       must be changed too -- and it is FROZEN by the freeze line.
#
# The Toto2 knobs in PARAMS above (`checkpoint`, `context_length`, and the
# `buy_threshold`/`sell_threshold`/`min_prob`/`full_confidence_prob` band) are
# now INERT on the RSI path. They cannot be removed: `real_data.py`
# `_resolve_strategy_knobs` raises unless a package's PARAMS keys are exactly the
# CLI's knob set, and `real_data.py` is frozen. `stride_bars` and the trader
# knobs are still live.


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
    # Tech: the RSI pair. The forecaster reports the indicator; the emitter reads
    #       it. RSI's own settings live in `rsi.py`, not in PARAMS -- see the note
    #       at the top of this file for why they cannot go in PARAMS.
    # Why:  the indicator needs price HISTORY and the emitter is only ever handed
    #       a `Forecast`, at stride boundaries. The Forecaster is the one seam
    #       that receives the history-through-t frame, so a 5-minute RSI has to be
    #       computed there and travel in the payload.
    forecaster = make_rsi_forecaster(
        warmup_bars=warmup_bars,
        stride_bars=p["stride_bars"],
        horizon_bars=p["horizon_bars"],
    )
    emitter = RsiEmitter()

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
        # Tech: the one-week holding cap (see TIME_STOP_BARS above), as a fresh
        #       TimeStop instance per make_strategy call.
        # Why:  ExitRule and emitter SELL race per tick — whichever fires first
        #       closes — so the cap only ever bounds the RSI>=80 exit's blind
        #       spot (declines where 80 never comes), it does not replace it. A
        #       fresh instance per call keeps a stateful rule from leaking across
        #       runs even though TimeStop happens to be stateless.
        "exit_rule": TimeStop(TIME_STOP_BARS),
        "forced_close": p["forced_close"],
        "params": p,
    }
