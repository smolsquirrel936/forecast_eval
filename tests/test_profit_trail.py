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
from forecast_eval.strategy.hybrid.router import RoutedExitRule, SessionTrail

T0 = pd.Timestamp("2024-01-02 09:00:00")
PACKAGES = ("day", "night", "fullday", "hybrid")
# hybrid's per-session trail, adopted on simtrade_0929 (2026-11-12): off, for the
# cases that exercise the position-level tp_* trail it excludes
SESSION_OFF = dict(day_tp_arm_pct=None, day_tp_trail_pct=None,
                   night_tp_arm_pct=None, night_tp_trail_pct=None)


def _session_off(pkg):
    return SESSION_OFF if pkg == "hybrid" else {}


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
    rule = w.make_strategy(warmup_bars=10, **off, **_session_off(pkg))["exit_rule"]
    members = getattr(rule, "rules", [rule])
    assert not any(isinstance(r, ProfitTrail) for r in members)


@pytest.mark.parametrize("pkg", PACKAGES)
def test_knobs_on_compose_a_restorable_trail(pkg):
    w = _wiring(pkg)
    rule = w.make_strategy(warmup_bars=10, tp_arm_pct=0.15, tp_trail_pct=0.02,
                           tp_giveback_frac=None, **_session_off(pkg))["exit_rule"]
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
                           tp_giveback_frac=None, day_stop_pct=0.10, **SESSION_OFF)["exit_rule"]
    pos = _pos()
    assert rule.should_exit(pos, _mkt(110.0, session="NIGHT"), bar_idx=11) is False
    assert rule.should_exit(pos, _mkt(107.7, minutes=1, session="DAY"), bar_idx=12) is True


# -------- hybrid per-session trail (router.SessionTrail) -----------------------

def test_hybrid_default_is_the_adopted_session_trail():
    # adopted 2026-11-12: day arm 0.10 / trail 0.03, night arm 0.12 / trail 0.02,
    # beside the routed 0.05 / 0.06 stops; the position-level trail stays off
    w = _wiring("hybrid")
    assert (w.PARAMS["tp_arm_pct"], w.PARAMS["tp_trail_pct"], w.PARAMS["tp_giveback_frac"]) == (None,) * 3
    rule = w.make_strategy(warmup_bars=10)["exit_rule"]
    assert isinstance(rule, AnyOf)
    routed, trail = rule.rules
    assert isinstance(routed, RoutedExitRule) and isinstance(trail, SessionTrail)
    day, night = trail.halves["DAY"], trail.halves["NIGHT"]
    assert (day.arm_pct, day.trail_pct, day.giveback_frac) == (0.10, 0.03, None)
    assert (night.arm_pct, night.trail_pct, night.giveback_frac) == (0.12, 0.02, None)
    assert rule.restorable is True and rule.state_keys == ("best_price",)


def test_hybrid_session_trail_uses_the_session_width_over_one_best_price():
    # night trail 5% / day trail 2%, both armed at 5%: the best price 110 is set
    # on a night print; a day print 2% off it fires, a night print there does not
    w = _wiring("hybrid")
    rule = w.make_strategy(warmup_bars=10, day_tp_arm_pct=0.05, day_tp_trail_pct=0.02,
                           night_tp_arm_pct=0.05, night_tp_trail_pct=0.05)["exit_rule"]
    pos = _pos()
    assert rule.should_exit(pos, _mkt(110.0, session="NIGHT"), bar_idx=11) is False
    assert rule.should_exit(pos, _mkt(107.7, minutes=1, session="NIGHT"), bar_idx=12) is False
    assert rule.should_exit(pos, _mkt(107.7, minutes=2, session="DAY"), bar_idx=13) is True


def test_hybrid_session_trail_off_session_never_fires_but_tracks_best():
    # day-only trail: a night print sets the best and gives back without a
    # fire; the next day print, still 2% off that night best, fires
    w = _wiring("hybrid")
    rule = w.make_strategy(warmup_bars=10, day_tp_arm_pct=0.05,
                           day_tp_trail_pct=0.02)["exit_rule"]
    pos = _pos()
    assert rule.should_exit(pos, _mkt(110.0, session="NIGHT"), bar_idx=11) is False
    assert rule.should_exit(pos, _mkt(100.0, minutes=1, session="NIGHT"), bar_idx=12) is False
    assert rule.should_exit(pos, _mkt(107.0, minutes=2, session="DAY"), bar_idx=13) is True


def test_hybrid_session_trail_equal_widths_match_the_position_trail():
    w = _wiring("hybrid")
    prices = [(100, "DAY"), (112, "NIGHT"), (111, "DAY"), (109.9, "NIGHT"),
              (109.7, "DAY"), (115, "DAY"), (112.6, "NIGHT")]
    one = w.make_strategy(warmup_bars=10, tp_arm_pct=0.1, tp_trail_pct=0.02, **SESSION_OFF)["exit_rule"]
    two = w.make_strategy(warmup_bars=10, day_tp_arm_pct=0.1, day_tp_trail_pct=0.02,
                          night_tp_arm_pct=0.1, night_tp_trail_pct=0.02)["exit_rule"]

    def fires(rule):
        pos = _pos()
        return [rule.should_exit(pos, _mkt(p, minutes=k, session=s), bar_idx=11 + k)
                for k, (p, s) in enumerate(prices)]
    assert fires(one) == fires(two) and any(fires(one))


def test_hybrid_session_trail_restores_both_sessions_from_one_best_price():
    w = _wiring("hybrid")
    rule = w.make_strategy(warmup_bars=10, day_tp_arm_pct=0.05, day_tp_trail_pct=0.02,
                           night_tp_arm_pct=0.05, night_tp_trail_pct=0.04)["exit_rule"]
    assert rule.restorable is True and rule.state_keys == ("best_price",)
    pos = _pos()
    rule.restore_state(pos, {"best_price": 110.0})
    assert rule.export_state() == {"best_price": 110.0}
    assert rule.should_exit(pos, _mkt(107.7, session="NIGHT"), bar_idx=11) is False
    assert rule.should_exit(pos, _mkt(107.7, minutes=1, session="DAY"), bar_idx=12) is True


def test_hybrid_position_and_session_trails_are_exclusive():
    with pytest.raises(ValueError, match="not both"):
        _wiring("hybrid").make_strategy(warmup_bars=10, tp_arm_pct=0.1, tp_trail_pct=0.02,
                                        night_tp_arm_pct=0.1, night_tp_trail_pct=0.03)


def test_hybrid_session_shape_without_arm_is_refused():
    with pytest.raises(ValueError, match="need tp_arm_pct"):
        _wiring("hybrid").make_strategy(warmup_bars=10, night_tp_arm_pct=None,
                                        night_tp_trail_pct=0.03)
