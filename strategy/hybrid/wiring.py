"""HYBRID's strategy wiring — hybrid_split_iter22 plus the 2026-11-01 percent stops.

**The DAY half trades the DAY session and the NIGHT half the NIGHT session**, and
a lot may carry across a session change (``forced_close`` False), as in
``fullday``. Each half's knobs are prefixed ``day_`` / ``night_``.

**The code is the reset packages'; the values are tuned.** Both halves are
copied from ``main``'s reset day and night packages (DAYNIGHT_TOTO2_RESET_PLAN,
``20767bb``), Toto2-1B at context 3008. ``hybrid_split_iter22`` (``9c0741f`` on
``autoresearch_split_0.7_hybrid``, ``results_hybrid.tsv``; adopted here as
``58eb8b5``) tuned them in one joint search
(``autoresearch_trading_plan_hybrid_split07.md``): day ±0.0005 / ``min_prob`` 0.60,
no openings 09:30–13:00; night ±0.0007 / 0.5, closing carried lots but never
opening. Train Calmar 12.8738, held-out 6.9773.

**The stops were added after the search** (2026-11-01, HYBRID_PCTSTOP_REPORT.md on
``autoresearch_hybrid_pctstop_1101``): day ``stop_pct`` 0.05, night 0.06, a user
decision against that report's None/None — see ``PARAMS``. With them the package
no longer reproduces the iter22 row: train Calmar 12.2446, held-out 6.92.

**The twelve CLI knobs.** ``real_data.py`` resolves twelve unprefixed knobs for
every package. Four mean the same here as anywhere: ``stride_bars`` and
``horizon_bars`` (shared by both halves — see ``forecaster.py``),
``max_position`` (one position, both traders) and ``forced_close``.
``context_length`` is the history window the run loop materialises, so it must
equal the larger half's context. The other seven have no single meaning in a
two-half package and must stay None; ``make_strategy`` refuses a value, so a
typed ``--buy-threshold`` fails instead of being ignored.

Registered for live as ``PINNED_HYBRID`` (``live/config.py``, 2026-10-27), with
its own file slug ``hybrid``; live builds from this same ``make_strategy``.
"""
from typing import Any, Dict, Optional

from ...exits.base import ExitRule
from ...exits.pct_stop import EntryPercentStop
from .day_half.emitter import ThresholdEmitter as DayThresholdEmitter
from .day_half.forecaster import make_forecaster as make_day_forecaster
from .day_half.trader import Trader as DayTrader
from .forecaster import make_forecaster
from .night_half.emitter import ThresholdEmitter as NightThresholdEmitter
from .night_half.forecaster import make_forecaster as make_night_forecaster
from .night_half.trader import Trader as NightTrader
from .router import RoutedEmitter, RoutedExitRule, RoutedTrader

# Tech: this package's default knobs — the twelve `real_data.py` resolves, then
#       each half's own, prefixed.
# Why:  the CLI requires the twelve (LIVE_STRATEGY_ROUTING_PLAN D6) and resolves
#       the prefixed ones from here the same way, so every knob that ran reaches
#       the banner and `params.json`. Knob changes are edits here, not `--param`,
#       so the committed package is what a results row scored.
PARAMS: Dict[str, Any] = {
    # shared
    "context_length": 3008,         # history window: max of the halves' contexts
    "horizon_bars": 30,
    "stride_bars": 30,
    "max_position": 1,
    "forced_close": False,
    # unprefixed CLI knobs with no meaning here — must stay None
    "checkpoint": None,
    "buy_threshold": None,
    "sell_threshold": None,
    "min_prob": None,
    "full_confidence_prob": None,
    "aggression_ticks": None,
    "max_aggression_ticks": None,
    # DAY half (main:strategy/day at the reset)
    "day_checkpoint": "Datadog/Toto-2.0-1B",
    "day_context_length": 3008,
    "day_buy_threshold": 0.0005,
    "day_sell_threshold": -0.0005,
    "day_min_prob": 0.6,
    "day_full_confidence_prob": 0.9,
    "day_aggression_ticks": 0,
    "day_max_aggression_ticks": None,
    # Adopted 2026-11-01 from HYBRID_PCTSTOP_REPORT.md (autoresearch_hybrid_pctstop_1101,
    # cell day 0.05 / night 0.06): the tightest widths that do not wreck the
    # held-out read — day 0.03 churns (test Calmar 1.34), night 0.05 fires a
    # second loser (test 3.73 / MDD -3288). A user decision against the report's
    # None/None: train Calmar 12.87 -> 12.24, MDD -1129 -> -1157; test 6.98 -> 6.92,
    # MDD -2342 unchanged. With day at 0.05 the night stop never fired on either
    # window (night 0.06-0.08 are byte-identical); it caps a carried lot overnight,
    # when only the night rule is checked. --param day_stop_pct=none
    # night_stop_pct=none restores the stop-less run.
    "day_stop_pct": 0.05,
    # hybrid_split_iter18: the day half stops opening positions in the midday
    # lull (start inclusive / end exclusive) — those train entries lost -1267
    # points over 30 while morning (08-10) entries made +8467. hybrid_split_iter20
    # moved the start to 10: hour-10 entries average +23.6/entry vs ~92 for the
    # remaining hours (weakest entry hour left). hybrid_split_iter22 moved the
    # start to 9.5 (half-hour resolution, float hours): 9:00-9:29 entries made
    # +5127 over 39 while 9:30-9:59 entries lost -989 over 29. Closes are never
    # blocked; --param day_open_block_start=none day_open_block_end=none
    # restores the copied behavior.
    "day_open_block_start": 9.5,
    "day_open_block_end": 13,
    # NIGHT half (main:strategy/night at the reset)
    "night_checkpoint": "Datadog/Toto-2.0-1B",
    "night_context_length": 3008,
    "night_buy_threshold": 0.0007,
    "night_sell_threshold": -0.0007,
    "night_min_prob": 0.5,
    "night_full_confidence_prob": 0.9,
    "night_aggression_ticks": 0,
    "night_max_aggression_ticks": None,
    "night_stop_pct": 0.06,         # see day_stop_pct
    # hybrid_split_iter09: the night half closes but never opens (its own
    # entries measured -997.1 train points at iter08); --param night_allow_open=true
    # restores the copied behavior
    "night_allow_open": False,
}

# Tech: the unprefixed knobs that must be None.
# Why:  see the module docstring — each half has its own, so a value here could
#       only be a mistake (a typed CLI flag, a copied PARAMS line).
INERT = ("checkpoint", "buy_threshold", "sell_threshold", "min_prob",
         "full_confidence_prob", "aggression_ticks", "max_aggression_ticks")


def _stop(stop_pct: Optional[float]) -> Optional[ExitRule]:
    return EntryPercentStop(stop_pct=stop_pct) if stop_pct is not None else None


def make_strategy(
    *,
    warmup_bars: int,
    tick_size: float = 1.0,
    **overrides: Any,
) -> Dict[str, Any]:
    """Build both halves and the routers that put them behind one strategy.

    ``warmup_bars`` and ``tick_size`` come from the *run*. Everything else
    defaults from ``PARAMS`` and may be overridden by an explicit CLI flag.

    Returns a dict with keys ``forecaster``, ``emitter``, ``make_trader``,
    ``exit_rule``, ``forced_close``, ``params``.
    """
    # Tech: resolve the knobs and refuse unknown ones, set inert ones, and a
    #       history window that does not cover both halves' contexts.
    # Why:  each would run something other than what the command line or PARAMS
    #       says. A window shorter than a half's context would silently feed that
    #       half a truncated history in the sequential path.
    unknown = set(overrides) - set(PARAMS)
    if unknown:
        raise ValueError(
            f"unknown strategy override(s) {sorted(unknown)}; "
            f"this package declares {sorted(PARAMS)}"
        )
    p = {**PARAMS, **overrides}
    set_inert = [k for k in INERT if p[k] is not None]
    if set_inert:
        raise ValueError(
            f"hybrid has no single {set_inert}; set the day_/night_ knobs instead"
        )
    widest = max(p["day_context_length"], p["night_context_length"])
    if p["context_length"] != widest:
        raise ValueError(
            f"context_length {p['context_length']} must equal the larger half's "
            f"context ({widest})"
        )

    # Tech: build each half's forecaster from its own construction site, then
    #       the router over the two.
    # Why:  the halves' files are what each iteration edits; the router decides
    #       whether the two can share one instance (forecaster.py).
    halves_fc = {
        half: build(
            warmup_bars=warmup_bars,
            stride_bars=p["stride_bars"],
            horizon_bars=p["horizon_bars"],
            context_length=p[f"{half}_context_length"],
            checkpoint=p[f"{half}_checkpoint"],
        )
        for half, build in (("day", make_day_forecaster), ("night", make_night_forecaster))
    }
    forecaster = make_forecaster(
        day=halves_fc["day"], night=halves_fc["night"], warmup_bars=warmup_bars,
        stride_bars=p["stride_bars"], horizon_bars=p["horizon_bars"],
    )
    emitter = RoutedEmitter(
        day=DayThresholdEmitter(
            buy_threshold=p["day_buy_threshold"],
            sell_threshold=p["day_sell_threshold"],
            min_prob=p["day_min_prob"],
            full_confidence_prob=p["day_full_confidence_prob"],
        ),
        night=NightThresholdEmitter(
            buy_threshold=p["night_buy_threshold"],
            sell_threshold=p["night_sell_threshold"],
            min_prob=p["night_min_prob"],
            full_confidence_prob=p["night_full_confidence_prob"],
        ),
    )

    # Tech: a factory over Execution that builds both traders on that one
    #       Execution and routes between them.
    # Why:  `run_backtest` owns Execution; one Execution means one resting order,
    #       which is what a single account has.
    def make_trader(execution: Any) -> RoutedTrader:
        return RoutedTrader(
            day=DayTrader(
                execution=execution,
                tick_size=tick_size,
                aggression_ticks=p["day_aggression_ticks"],
                max_position=p["max_position"],
                max_aggression_ticks=p["day_max_aggression_ticks"],
                open_block_start=p["day_open_block_start"],
                open_block_end=p["day_open_block_end"],
            ),
            night=NightTrader(
                execution=execution,
                tick_size=tick_size,
                aggression_ticks=p["night_aggression_ticks"],
                max_position=p["max_position"],
                max_aggression_ticks=p["night_max_aggression_ticks"],
                allow_open=p["night_allow_open"],
            ),
        )

    # Tech: route per-half stops when either half has one; None when neither.
    # Why:  None keeps `run_backtest` skipping the exit step entirely, as every
    #       reset package does, so the unedited hybrid matches them tick for tick.
    day_stop, night_stop = _stop(p["day_stop_pct"]), _stop(p["night_stop_pct"])
    exit_rule = (RoutedExitRule(day_stop, night_stop)
                 if day_stop is not None or night_stop is not None else None)

    return {
        "forecaster": forecaster,
        "emitter": emitter,
        "make_trader": make_trader,
        "exit_rule": exit_rule,
        "forced_close": p["forced_close"],
        "params": p,
    }
