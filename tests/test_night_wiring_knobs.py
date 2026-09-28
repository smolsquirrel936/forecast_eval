"""night_iter08: the `min_trend_ticks` PARAMS knob reaches the emitter.

`min_trend_ticks` was the first of the emitter's formerly-absent knobs to be
declared in night's `PARAMS` (iter08, value 20.0). These cases pin the
wiring-level contract: the PARAMS default is threaded into the built emitter,
an explicit override beats it, and anything undeclared still raises.
"""
import pytest

from forecast_eval.strategy.night import wiring as night_wiring


def test_params_min_trend_ticks_reaches_the_built_emitter():
    stack = night_wiring.make_strategy(warmup_bars=0)
    # Tech: assert equality with PARAMS, never a literal value.
    # Why:  the loop retunes this knob (20 -> 15 -> 12 during iters 08-10); a
    #       literal here would fail on every retune while saying nothing about
    #       the wiring contract the test exists to pin.
    assert stack["emitter"].min_trend_ticks == night_wiring.PARAMS["min_trend_ticks"]
    assert isinstance(night_wiring.PARAMS["min_trend_ticks"], float)


def test_min_trend_ticks_override_beats_params():
    stack = night_wiring.make_strategy(warmup_bars=0, min_trend_ticks=3.0)
    assert stack["emitter"].min_trend_ticks == 3.0
    assert stack["params"]["min_trend_ticks"] == 3.0


def test_emitter_knobs_left_undeclared_still_raise():
    with pytest.raises(ValueError, match="unknown strategy override"):
        night_wiring.make_strategy(warmup_bars=0, min_trend_junk=1.0)
