"""HYBRID's strategy wiring — UNTUNED; both halves at the reset defaults.

**The DAY half trades the DAY session and the NIGHT half the NIGHT session**, and
a lot may carry across a session change (``forced_close`` False), as in
``fullday``. Each half's knobs are prefixed ``day_`` / ``night_``.

**These values were not chosen for the hybrid.** They are ``main``'s reset day
and night packages (DAYNIGHT_TOTO2_RESET_PLAN, ``20767bb``): day ±0.001 /
``min_prob`` 0.65, night ±0.00045 / 0.5, both two-sided Toto2-1B at context 3008,
no exit rule. There is no ``results_hybrid.tsv`` row yet; ``iter01`` of
``autoresearch_trading_plan_hybrid_split07.md`` — this package unedited — sets the first
best.

**The twelve CLI knobs.** ``real_data.py`` resolves twelve unprefixed knobs for
every package. Four mean the same here as anywhere: ``stride_bars`` and
``horizon_bars`` (shared by both halves — see ``forecaster.py``),
``max_position`` (one position, both traders) and ``forced_close``.
``context_length`` is the history window the run loop materialises, so it must
equal the larger half's context. The other seven have no single meaning in a
two-half package and must stay None; ``make_strategy`` refuses a value, so a
typed ``--buy-threshold`` fails instead of being ignored.

Not registered for live: ``live/config.py`` has no ``hybrid`` deployment, so this
package is backtest-only until the plan's "Going live" steps are done.
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
    "day_buy_threshold": 0.001,
    "day_sell_threshold": -0.001,
    "day_min_prob": 0.65,
    "day_full_confidence_prob": 0.9,
    "day_aggression_ticks": 0,
    "day_max_aggression_ticks": None,
    "day_stop_pct": None,
    # NIGHT half (main:strategy/night at the reset)
    "night_checkpoint": "Datadog/Toto-2.0-1B",
    "night_context_length": 3008,
    "night_buy_threshold": 0.00045,
    "night_sell_threshold": -0.00045,
    "night_min_prob": 0.5,
    "night_full_confidence_prob": 0.9,
    "night_aggression_ticks": 0,
    "night_max_aggression_ticks": None,
    "night_stop_pct": None,
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
    # Why:  the halves' files are what each phase edits; the router decides
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
            ),
            night=NightTrader(
                execution=execution,
                tick_size=tick_size,
                aggression_ticks=p["night_aggression_ticks"],
                max_position=p["max_position"],
                max_aggression_ticks=p["night_max_aggression_ticks"],
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
