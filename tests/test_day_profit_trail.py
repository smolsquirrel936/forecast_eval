"""DAY's take-profit trail (``strategy/day/profit_trail.py``) and the Trader's
take-profit re-entry cooldown (``tp_cooldown_signals``).

The trail does nothing until the best price since entry is ``arm_pct`` in the
position's favour, then exits on a give-back (fraction of max gain, or percent
off the best price). The cooldown arms only after a profitable exit-rule close.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.exits.base import PositionState
from forecast_eval.exits.pct_stop import EntryPercentStop
from forecast_eval.strategy.day.profit_trail import AnyOf, ProfitTrail
from forecast_eval.strategy.day.trader import Trader
from forecast_eval.strategy.day.wiring import make_strategy

T0 = pd.Timestamp("2024-01-02 09:00:00")


def _mkt(price, minutes=0):
    return MarketEvent(timestamp=T0 + pd.Timedelta(minutes=minutes),
                       price=price, volume=1, session="DAY")


def _pos(side="LONG", entry=100.0, bar=10):
    return PositionState(side=side, size=1, entry_price=entry,
                         entry_bar_idx=bar, entry_timestamp=T0)


def _run(rule, prices, pos=None):
    pos = pos or _pos()
    return [rule.should_exit(pos, _mkt(p), bar_idx=10) for p in prices]


# -------- ProfitTrail ----------------------------------------------------------

def test_unarmed_trail_never_fires_even_on_a_loss():
    rule = ProfitTrail(0.05, giveback_frac=0.5)
    assert _run(rule, [100, 104.9, 90, 80]) == [False] * 4


def test_giveback_long_fires_at_fraction_of_max_gain():
    rule = ProfitTrail(0.05, giveback_frac=0.5)
    # arms at 105; best 110 (gain 10) -> fires at gain <= 5, i.e. price 105
    assert _run(rule, [105, 110, 105.5, 105]) == [False, False, False, True]


def test_giveback_short_mirror():
    rule = ProfitTrail(0.05, giveback_frac=0.5)
    assert _run(rule, [95, 90, 94.5, 95], _pos("SHORT")) == [False, False, False, True]


def test_trail_pct_fires_off_the_best_price():
    rule = ProfitTrail(0.05, trail_pct=0.02)
    # best 110 -> fires at <= 107.8
    assert _run(rule, [106, 110, 108, 107.8]) == [False, False, False, True]


def test_trail_pct_short_mirror():
    rule = ProfitTrail(0.05, trail_pct=0.02)
    # best 90 -> fires at >= 91.8
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


@pytest.mark.parametrize("kw", [
    dict(arm_pct=0.0, giveback_frac=0.5),
    dict(arm_pct=0.05),
    dict(arm_pct=0.05, giveback_frac=0.5, trail_pct=0.02),
    dict(arm_pct=0.05, giveback_frac=1.0),
    dict(arm_pct=0.05, trail_pct=0.0),
])
def test_invalid_arguments_rejected(kw):
    with pytest.raises(ValueError):
        ProfitTrail(**kw)


def test_anyof_with_stop_is_restorable_on_trail_state():
    rule = AnyOf([EntryPercentStop(0.10), ProfitTrail(0.05, trail_pct=0.02)])
    assert rule.adoptable is False and rule.restorable is True
    assert rule.state_keys == ("best_price",)
    assert _run(rule, [89.9]) == [True]                # the stop still fires


# -------- wiring ---------------------------------------------------------------

def test_wiring_control_is_the_bare_percent_stop():
    s = make_strategy(warmup_bars=100, tp_arm_pct=None, tp_trail_pct=None)
    assert type(s["exit_rule"]) is EntryPercentStop


def test_wiring_default_is_the_adopted_trail():
    # adopted 2026-11-11: arm 0.15 / trail 0.02 around the 0.10 stop
    s = make_strategy(warmup_bars=100)
    assert isinstance(s["exit_rule"], AnyOf)
    stop, trail = s["exit_rule"].rules
    assert type(stop) is EntryPercentStop and stop.stop_pct == 0.10
    assert isinstance(trail, ProfitTrail)
    assert (trail.arm_pct, trail.trail_pct, trail.giveback_frac) == (0.15, 0.02, None)


def test_wiring_composes_trail_with_stop():
    s = make_strategy(warmup_bars=100, tp_arm_pct=0.05, tp_giveback_frac=0.5,
                      tp_trail_pct=None)
    assert isinstance(s["exit_rule"], AnyOf)
    s = make_strategy(warmup_bars=100, stop_pct=None, tp_arm_pct=0.05, tp_trail_pct=0.02)
    assert isinstance(s["exit_rule"], ProfitTrail)


def test_wiring_rejects_giveback_without_arm():
    with pytest.raises(ValueError):
        make_strategy(warmup_bars=100, tp_arm_pct=None, tp_trail_pct=None,
                      tp_giveback_frac=0.5)


# -------- Trader take-profit cooldown -----------------------------------------

def _trader(k):
    return Trader(execution=Execution(fee_rate=0.0), aggression_ticks=0,
                  tp_cooldown_signals=k)


def _open_long(tr, price=100.0):
    tr.on_signal("BUY", _mkt(price))
    tr.on_fill("BUY", 1, fill_price=price, bar_idx=1, timestamp=T0)


def _exit_close(tr, price):
    tr.submit_exit(_mkt(price, 5))
    tr.on_fill("SELL", 1, fill_price=price, bar_idx=5, timestamp=T0)


def test_profitable_exit_close_blocks_same_side_reentry_for_k_signals():
    tr = _trader(2)
    _open_long(tr)
    _exit_close(tr, 110.0)
    assert tr.on_signal("BUY", _mkt(110.0, 30)) is None   # 1 of 2
    assert tr.on_signal("HOLD", _mkt(110.0, 60)) is None  # 2 of 2
    assert tr.on_signal("BUY", _mkt(110.0, 90)) is not None


def test_opposite_side_opens_during_cooldown():
    tr = _trader(5)
    _open_long(tr)
    _exit_close(tr, 110.0)
    assert tr.on_signal("SELL", _mkt(110.0, 30)) is not None


def test_losing_exit_close_and_signal_close_do_not_arm():
    tr = _trader(5)
    _open_long(tr)
    _exit_close(tr, 90.0)                                  # a stop, at a loss
    assert tr.on_signal("BUY", _mkt(90.0, 30)) is not None
    tr.on_fill("BUY", 1, fill_price=90.0, bar_idx=6, timestamp=T0)
    tr.on_signal("SELL", _mkt(100.0, 60))                 # signal close, in profit
    tr.on_fill("SELL", 1, fill_price=100.0, bar_idx=7, timestamp=T0)
    assert tr.on_signal("BUY", _mkt(100.0, 90)) is not None


def test_cooldown_zero_is_inert():
    tr = _trader(0)
    _open_long(tr)
    _exit_close(tr, 110.0)
    assert tr.on_signal("BUY", _mkt(110.0, 30)) is not None


def test_negative_cooldown_rejected():
    with pytest.raises(ValueError):
        _trader(-1)
