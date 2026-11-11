"""DAY's adopted take-profit trail and the Trader's take-profit re-entry cooldown.

The trail itself is the shared ``exits/profit_trail.py`` (tests in
``tests/test_profit_trail.py``); here: day's adopted default (arm 0.15 / trail
0.02 around the 0.10 stop, 2026-11-11), its control, and day's own cooldown
(``tp_cooldown_signals``), which arms only after a profitable exit-rule close
and never on a live roll's close or re-open.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.exits.any_of import AnyOf
from forecast_eval.exits.pct_stop import EntryPercentStop
from forecast_eval.exits.profit_trail import ProfitTrail
from forecast_eval.strategy.day.trader import Trader
from forecast_eval.strategy.day.wiring import make_strategy

T0 = pd.Timestamp("2024-01-02 09:00:00")


def _mkt(price, minutes=0):
    return MarketEvent(timestamp=T0 + pd.Timedelta(minutes=minutes),
                       price=price, volume=1, session="DAY")


# -------- wiring ---------------------------------------------------------------

def test_wiring_default_is_the_adopted_trail():
    # adopted 2026-11-11: arm 0.15 / trail 0.02 around the 0.10 stop
    s = make_strategy(warmup_bars=100)
    assert isinstance(s["exit_rule"], AnyOf)
    stop, trail = s["exit_rule"].rules
    assert type(stop) is EntryPercentStop and stop.stop_pct == 0.10
    assert isinstance(trail, ProfitTrail)
    assert (trail.arm_pct, trail.trail_pct, trail.giveback_frac) == (0.15, 0.02, None)
    assert s["exit_rule"].restorable is True


def test_wiring_control_is_the_bare_percent_stop():
    s = make_strategy(warmup_bars=100, tp_arm_pct=None, tp_trail_pct=None)
    assert type(s["exit_rule"]) is EntryPercentStop


def test_wiring_trail_alone_without_the_stop():
    s = make_strategy(warmup_bars=100, stop_pct=None)
    assert isinstance(s["exit_rule"], ProfitTrail)


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


# -------- the cooldown never touches a live roll ------------------------------

def test_a_profitable_roll_close_does_not_arm_the_cooldown():
    # live submits the roll's close inside rolling(); its venue fill arrives
    # later, outside it. The re-open must still go through and carry the entry.
    tr = _trader(5)
    _open_long(tr)
    with tr.rolling():
        tr.submit_exit(_mkt(110.0, 5))
    tr.on_fill("SELL", 1, fill_price=110.0, bar_idx=5, timestamp=T0)
    with tr.rolling():
        assert tr.on_signal("BUY", _mkt(110.0, 6)) is not None
    tr.on_fill("BUY", 1, fill_price=110.5, bar_idx=6, timestamp=T0)
    assert (tr.position, tr.entry_price, tr.entry_bar_idx) == (1, 100.0, 1)


def test_a_roll_re_open_bypasses_and_clears_an_armed_cooldown():
    tr = _trader(5)
    _open_long(tr)
    _exit_close(tr, 110.0)                                 # a real take-profit
    with tr.rolling():
        assert tr.on_signal("BUY", _mkt(110.0, 30)) is not None
    assert tr._cooldown_left == 0
