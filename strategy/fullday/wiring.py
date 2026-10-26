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

``fullday_rev_iter02`` (autoresearch_trading_plan_fullday_reversal.md, decision 2)
added the package-declared ``entry_min_prob`` knob: the Trader skips an OPEN from
flat whose signal strength falls below the floor it implies, while closes stay
ungated. Its value here is 0.50 — the inert control, floor 0.0, byte-identical
to the pre-knob package. The loop's search moves it through
``0.55 / 0.60 / 0.65 / 0.70`` as edits to ``PARAMS``.

``fullday_rev_iter16`` (same plan, decision 8 / phase E) added the
package-declared ``exit_threshold`` knob: while positioned, an opposite signal
clears at ``|predicted_return| >= exit_threshold`` while entries keep the full
band. Its value here is 0.001 — equal to the band, the inert control, so runs
are byte-identical to the pre-knob package. The loop's search moves it through
``0.0005 / 0.00025 / 0.0`` as edits to ``PARAMS``.

``fullday_rev_iter21`` (same plan, phase F) kept the declared pair
``stride_bars`` 10 + ``entry_min_prob`` 0.60 (Calmar 3.9681, ride 0.3800) —
the current incumbent of this loop.

``fullday_rev_iter23`` (same plan, decisions 9/10, phase G control) added the
package-declared ``lock_after_pct`` and ``giveback_frac`` knobs, composed with
``stop_pct`` through the any-of seam in this package's ``exits.py``. Both None
here — the inert control: wiring returns the bare ``EntryPercentStop``, so runs
are byte-identical to the pre-knob package. The loop's search moves them
through ``0.02 / 0.01 / 0.03`` and ``0.5 / 0.35`` as edits to ``PARAMS``.

``fullday_rev_iter24`` / ``fullday_rev_iter26`` (phase G1) kept the break-even
lock at 0.02, then 0.03 — the current incumbent of this loop (Calmar 4.4402,
ride 0.3704).

``fullday_rev_iter29`` (same plan, decision 11, phase H control) added the
package-declared ``channel_exit_bars`` knob: once the position is at least N
bars old, a long exits when price prints below the lowest of its last N
in-position bars, a short above the highest. None here — the inert control.
"""
from typing import Any, Dict

from .emitter import ThresholdEmitter
from .exits import (
    AnyOf,
    BreakEvenLock,
    ChannelExit,
    EntryPercentStop,
    GivebackTrail,
)
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
    "stride_bars": 10,
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
    # profit-protection exits — package-declared, beyond the twelve CLI knobs
    # (D6/D7), reachable only through ``--param lock_after_pct=...`` /
    # ``--param giveback_frac=...``.
    # Why:  reversal loop decisions 9/10 (phase G). lock_after_pct (G1): once
    # the best price since entry is this fraction of the entry in the
    # position's favour, exit on a return to the entry. giveback_frac (G2):
    # once the best price is 2% in favour (GIVEBACK_ARM_PCT), exit when the
    # open gain falls to (1-giveback_frac) of its maximum. Both None is the
    # CONTROL: wiring returns the bare EntryPercentStop, byte-identical to the
    # pre-knob package. The rules are stateful -> adoptable=False, so a live
    # --resume into a held lot is refused for them.
    "lock_after_pct": 0.03,
    "giveback_frac": None,
    # price-channel exit — package-declared, beyond the twelve CLI knobs
    # (D6/D7), reachable only through ``--param channel_exit_bars=...``.
    # Why:  reversal loop decision 11 (phase H). Once the position is at least
    # N bars old, a long exits when price prints below the lowest of its last
    # N in-position bars, a short above the highest — model-free, no forecast
    # lag. None is the CONTROL: the rule is not composed in, byte-identical
    # to the pre-knob package. Stateful -> adoptable=False.
    "channel_exit_bars": None,
    # entry-probability gate — package-declared, beyond the twelve CLI knobs
    # (D6/D7), reachable only through ``--param entry_min_prob=...``.
    # Why:  reversal loop decision 2. 0.50 is the CONTROL value: the floor it
    # implies is 0.0 and no emitted signal can fall below it, so every entry
    # still opens and a run at 0.50 must be byte-identical to one without the
    # knob. The gate applies to opens only; min_prob (above) stays the exit
    # gate inside the emitter.
    "entry_min_prob": 0.60,
    # exit-side band — package-declared, beyond the twelve CLI knobs
    # (D6/D7), reachable only through ``--param exit_threshold=...``.
    # Why:  reversal loop decision 8 (phase E). While positioned, an opposite
    # signal needs only |predicted_return| >= exit_threshold; from flat, and
    # on the same side, the full band above still applies. 0.001 equals the
    # band — the CONTROL value, at which the exit cut *is* the sell band, so
    # a run at 0.001 must be byte-identical to one without the knob. min_prob,
    # the trend filter and the path check still gate the exit signal.
    "exit_threshold": 0.001,
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

    # Tech: validate the entry gate against the emitter's two probability knobs,
    #       before anything is built.
    # Why:  reversal plan decision 2 — an entry floor below min_prob gates
    #       nothing the emitter has not already blocked (vacuous), while one at
    #       or above full_confidence_prob is unreachable because the strength
    #       ramp saturates at 1.0 (it would silently block every entry). Failing
    #       loudly at construction beats trading the wrong shape.
    if not p["min_prob"] <= p["entry_min_prob"] < p["full_confidence_prob"]:
        raise ValueError(
            f"entry_min_prob ({p['entry_min_prob']}) must satisfy "
            f"min_prob ({p['min_prob']}) <= entry_min_prob < "
            f"full_confidence_prob ({p['full_confidence_prob']})"
        )

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
        exit_threshold=p["exit_threshold"],
    )

    def make_trader(execution: Any) -> Trader:
        trader = Trader(
            execution=execution,
            tick_size=tick_size,
            aggression_ticks=p["aggression_ticks"],
            max_position=p["max_position"],
            max_aggression_ticks=p["max_aggression_ticks"],
            entry_min_prob=p["entry_min_prob"],
            full_confidence_prob=p["full_confidence_prob"],
        )
        # Tech: bind the emitter to the trader this factory just built, so the
        #       exit band knows which side is the exit side.
        # Why:  reversal decision 8's mechanism — the emitter reads the live
        #       signed position through the getter at each forecast (fills land
        #       before emit in run.py's tick order, so no look-ahead, and the
        #       frozen engine needs no new seam). Re-binding on every call
        #       keeps a re-made trader (fee sweeps re-run make_trader) the
        #       emitter's sole counterpart.
        emitter.bind_position(lambda: trader.position)
        return trader

    # Tech: build the exit stack from the knobs — the shared percent stop plus
    #       this package's protection rules (decisions 9/10), composed through
    #       the any-of seam. With stop_pct the only exit set, return the bare
    #       EntryPercentStop, exactly what every recorded run used.
    # Why:  the control (lock/giveback None) must be byte-identical to the
    #       pre-knob package, so the composition wrapper appears only when a
    #       protection rule is actually set. run_backtest queries the rule
    #       every tick a position is open (run.py Step 2) and submits a closing
    #       limit on fire — the flatten-and-continue semantics the live
    #       deployment wants, so the backtest measures precisely what live
    #       would do.
    exit_rules = []
    if p["stop_pct"] is not None:
        exit_rules.append(EntryPercentStop(stop_pct=p["stop_pct"]))
    if p["lock_after_pct"] is not None:
        exit_rules.append(BreakEvenLock(lock_after_pct=p["lock_after_pct"]))
    if p["giveback_frac"] is not None:
        exit_rules.append(GivebackTrail(giveback_frac=p["giveback_frac"]))
    if p["channel_exit_bars"] is not None:
        exit_rules.append(ChannelExit(channel_exit_bars=p["channel_exit_bars"]))
    if not exit_rules:
        exit_rule = None
    elif len(exit_rules) == 1:
        exit_rule = exit_rules[0]
    else:
        exit_rule = AnyOf(exit_rules)

    return {
        "forecaster": forecaster,
        "emitter": emitter,
        "make_trader": make_trader,
        "exit_rule": exit_rule,
        "forced_close": p["forced_close"],
        "params": p,
    }
