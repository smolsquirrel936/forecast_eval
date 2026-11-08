"""fullday max_hold_bars (strategy_test_1108 step 4): a TimeStop composed into the exit stack."""
import pytest

from forecast_eval.exits.time_stop import TimeStop
from forecast_eval.strategy.fullday import wiring
from forecast_eval.strategy.fullday.exits import AnyOf


def _exit(**kw):
    return wiring.make_strategy(warmup_bars=100, **kw)["exit_rule"]


def test_control_composes_no_time_stop():
    assert wiring.PARAMS["max_hold_bars"] is None
    rule = _exit()
    rules = rule.rules if isinstance(rule, AnyOf) else [rule]
    assert not any(isinstance(r, TimeStop) for r in rules)


def test_knob_adds_a_time_stop_with_the_cap():
    rule = _exit(max_hold_bars=1140)
    assert isinstance(rule, AnyOf)
    ts = [r for r in rule.rules if isinstance(r, TimeStop)]
    assert len(ts) == 1 and ts[0].max_bars == 1140


def test_time_stop_alone_when_no_other_exit():
    rule = _exit(max_hold_bars=50, stop_pct=None, lock_after_pct=None)
    assert isinstance(rule, TimeStop) and rule.max_bars == 50


@pytest.mark.parametrize("bad", [0, -5, 1140.0, True, "1140"])
def test_bad_values_rejected(bad):
    with pytest.raises(ValueError, match="max_hold_bars"):
        _exit(max_hold_bars=bad)
