"""The shared take-profit trail (``exits/profit_trail.py``), the composition
builder (``exits/any_of.py``), and every package's ``tp_*`` knobs.

The trail does nothing until the best price since entry is ``arm_pct`` in the
position's favour, then exits on a give-back: percent off the best price, or a
fraction of the max gain. Each package declares the knobs, default off.
"""
import importlib

import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.exits.any_of import AnyOf, any_of
from forecast_eval.exits.base import PositionState
from forecast_eval.exits.pct_stop import EntryPercentStop
from forecast_eval.exits.profit_trail import ProfitTrail, profit_trail_from_knobs

T0 = pd.Timestamp("2024-01-02 09:00:00")
PACKAGES = ("day", "night", "fullday", "hybrid")


def _mkt(price, minutes=0, session="DAY"):
    return MarketEvent(timestamp=T0 + pd.Timedelta(minutes=minutes),
                       price=price, volume=1, session=session)


def _pos(side="LONG", entry=100.0, bar=10):
    return PositionState(side=side, size=1, entry_price=entry,
                         entry_bar_idx=bar, entry_timestamp=T0)


def _run(rule, prices, pos=None):
    pos = pos or _pos()
    return [rule.should_exit(pos, _mkt(p), bar_idx=10) for p in prices]


# -------- ProfitTrail ----------------------------------------------------------

def test_unarmed_trail_never_fires_even_on_a_loss():
    assert _run(ProfitTrail(0.05, giveback_frac=0.5), [100, 104.9, 90, 80]) == [False] * 4


def test_giveback_long_fires_at_fraction_of_max_gain():
    # arms at 105; best 110 (gain 10) -> fires at gain <= 5, i.e. price 105
    assert _run(ProfitTrail(0.05, giveback_frac=0.5), [105, 110, 105.5, 105]) == [False, False, False, True]


def test_giveback_short_mirror():
    rule = ProfitTrail(0.05, giveback_frac=0.5)
    assert _run(rule, [95, 90, 94.5, 95], _pos("SHORT")) == [False, False, False, True]


def test_trail_pct_fires_off_the_best_price():
    # best 110 -> fires at <= 107.8
    assert _run(ProfitTrail(0.05, trail_pct=0.02), [106, 110, 108, 107.8]) == [False, False, False, True]


def test_trail_pct_short_mirror():
    # best 90 -> fires at >= 91.8
    rule = ProfitTrail(0.05, trail_pct=0.02)
    assert _run(rule, [94, 90, 91.5, 91.9], _pos("SHORT")) == [False, False, False, True]


def test_never_fires_on_the_arming_print():
    assert _run(ProfitTrail(0.05, trail_pct=0.001), [105]) == [False]
    assert _run(ProfitTrail(0.05, giveback_frac=0.99), [105]) == [False]


def test_state_resets_on_a_new_entry():
    rule = ProfitTrail(0.05, giveback_frac=0.5)
    _run(rule, [110])                                  # armed on trade 1
    # a new entry at 120: 115 is a loss, not a give-back of trade 1's gain
    assert _run(rule, [118, 115], _pos(entry=120.0, bar=50)) == [False, False]


def test_export_and_restore_best_price():
    rule = ProfitTrail(0.05, giveback_frac=0.5)
    assert rule.export_state() is None
    _run(rule, [110])
    assert rule.export_state() == {"best_price": 110.0}
    fresh = ProfitTrail(0.05, giveback_frac=0.5)
    fresh.restore_state(_pos(), {"best_price": 110.0})
    assert _run(fresh, [105]) == [True]


def test_resumability_flags():
    rule = ProfitTrail(0.05, trail_pct=0.02)
    assert rule.adoptable is False and rule.restorable is True
    assert rule.state_keys == ("best_price",)


@pytest.mark.parametrize("kw", [
    dict(arm_pct=0.0, giveback_frac=0.5),
    dict(arm_pct=1.0, trail_pct=0.02),
    dict(arm_pct=0.05),
    dict(arm_pct=0.05, giveback_frac=0.5, trail_pct=0.02),
    dict(arm_pct=0.05, giveback_frac=1.0),
    dict(arm_pct=0.05, trail_pct=0.0),
])
def test_invalid_arguments_rejected(kw):
    with pytest.raises(ValueError):
        ProfitTrail(**kw)


def test_from_knobs():
    assert profit_trail_from_knobs(None) is None
    rule = profit_trail_from_knobs(0.15, 0.02, None)
    assert (rule.arm_pct, rule.trail_pct, rule.giveback_frac) == (0.15, 0.02, None)
    with pytest.raises(ValueError, match="need tp_arm_pct"):
        profit_trail_from_knobs(None, 0.02, None)
    with pytest.raises(ValueError, match="need tp_arm_pct"):
        profit_trail_from_knobs(None, None, 0.5)


# -------- any_of / AnyOf -------------------------------------------------------

def test_any_of_collapses():
    stop = EntryPercentStop(0.10)
    assert any_of([None, None]) is None
    assert any_of([stop, None]) is stop
    both = any_of([stop, ProfitTrail(0.05, trail_pct=0.02)])
    assert isinstance(both, AnyOf) and both.rules[0] is stop


def test_anyof_with_stop_is_restorable_on_trail_state():
    rule = AnyOf([EntryPercentStop(0.10), ProfitTrail(0.05, trail_pct=0.02)])
    assert rule.adoptable is False and rule.restorable is True
    assert rule.state_keys == ("best_price",)
    assert rule.export_state() is None
    assert _run(rule, [95.0]) == [False]
    assert rule.export_state() == {"best_price": 95.0}
    assert _run(rule, [89.9]) == [True]                # the stop still fires


def test_anyof_of_adoptable_rules_stays_adoptable():
    rule = AnyOf([EntryPercentStop(0.10), EntryPercentStop(0.20)])
    assert rule.adoptable is True and rule.restorable is False


def test_anyof_restore_hands_each_member_its_keys():
    trail = ProfitTrail(0.05, giveback_frac=0.5)
    rule = AnyOf([EntryPercentStop(0.10), trail])
    rule.restore_state(_pos(), {"best_price": 110.0})
    assert trail.export_state() == {"best_price": 110.0}
    assert _run(rule, [105]) == [True]


# -------- every package declares the knobs, off ------------------------------

def _wiring(pkg):
    return importlib.import_module(f"forecast_eval.strategy.{pkg}.wiring")


@pytest.mark.parametrize("pkg", PACKAGES)
def test_package_declares_the_three_knobs(pkg):
    params = _wiring(pkg).PARAMS
    for k in ("tp_arm_pct", "tp_trail_pct", "tp_giveback_frac"):
        assert k in params


@pytest.mark.parametrize("pkg", PACKAGES)
def test_knobs_off_build_no_trail(pkg):
    w = _wiring(pkg)
    off = dict(tp_arm_pct=None, tp_trail_pct=None, tp_giveback_frac=None)
    rule = w.make_strategy(warmup_bars=10, **off)["exit_rule"]
    members = getattr(rule, "rules", [rule])
    assert not any(isinstance(r, ProfitTrail) for r in members)


@pytest.mark.parametrize("pkg", PACKAGES)
def test_knobs_on_compose_a_restorable_trail(pkg):
    w = _wiring(pkg)
    rule = w.make_strategy(warmup_bars=10, tp_arm_pct=0.15, tp_trail_pct=0.02,
                           tp_giveback_frac=None)["exit_rule"]
    members = getattr(rule, "rules", [rule])
    trails = [r for r in members if isinstance(r, ProfitTrail)]
    assert len(trails) == 1
    assert (trails[0].arm_pct, trails[0].trail_pct) == (0.15, 0.02)
    assert rule.restorable is True


@pytest.mark.parametrize("pkg", PACKAGES)
def test_shape_without_arm_is_refused(pkg):
    with pytest.raises(ValueError, match="need tp_arm_pct"):
        _wiring(pkg).make_strategy(warmup_bars=10, tp_arm_pct=None,
                                   tp_trail_pct=0.02, tp_giveback_frac=None)


def test_hybrid_trail_sees_both_sessions():
    # one position-level trail beside the routed stops: a night print updates
    # the best price that a day print then gives back from
    w = _wiring("hybrid")
    rule = w.make_strategy(warmup_bars=10, tp_arm_pct=0.05, tp_trail_pct=0.02,
                           tp_giveback_frac=None, day_stop_pct=0.10)["exit_rule"]
    pos = _pos()
    assert rule.should_exit(pos, _mkt(110.0, session="NIGHT"), bar_idx=11) is False
    assert rule.should_exit(pos, _mkt(107.7, minutes=1, session="DAY"), bar_idx=12) is True
