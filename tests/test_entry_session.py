"""The fullday Trader's entry session (strategy_test_1108, step 4b).

With ``entry_session`` "DAY" or "NIGHT", an OPEN from flat is skipped outside that
session; closes and a roll's re-open are untouched. None (wiring "ANY") is the control.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.fullday import wiring
from forecast_eval.strategy.fullday.trader import Trader

T0 = pd.Timestamp("2024-01-02 09:00")


def _mkt(session, minute=0, price=17000.0):
    return MarketEvent(timestamp=T0 + pd.Timedelta(minutes=minute), price=price, volume=1, session=session)


def _trader(**kw):
    return Trader(Execution(fee_rate=0.0), aggression_ticks=0, **kw)


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
def test_night_only_skips_day_opens_and_allows_night_opens(direction):
    tr = _trader(entry_session="NIGHT")
    assert tr.on_signal(direction, _mkt("DAY")) is None
    assert tr.on_signal(direction, _mkt("NIGHT", 1)).side == direction


def test_day_only_is_the_mirror():
    tr = _trader(entry_session="DAY")
    assert tr.on_signal("BUY", _mkt("NIGHT")) is None
    assert tr.on_signal("BUY", _mkt("DAY", 1)) is not None


def test_close_is_not_gated():
    tr = _trader(entry_session="NIGHT")
    tr.on_fill("BUY", 1, fill_price=17000.0, bar_idx=0, timestamp=T0)
    order = tr.on_signal("SELL", _mkt("DAY", 5))
    assert order is not None and order.side == "SELL"


def test_roll_reopen_bypasses_it():
    tr = _trader(entry_session="NIGHT")
    with tr.rolling():
        assert tr.on_signal("BUY", _mkt("DAY")) is not None


def test_control_opens_in_both_sessions():
    tr = _trader()
    assert tr.on_signal("BUY", _mkt("DAY")) is not None


def test_bad_value_rejected():
    with pytest.raises(ValueError, match="entry_session"):
        _trader(entry_session="night")


def test_wiring_any_is_the_control_and_values_pass_through():
    assert wiring.PARAMS["entry_session"] == "ANY"
    make = lambda **kw: wiring.make_strategy(warmup_bars=100, **kw)["make_trader"](Execution(fee_rate=0.0))
    assert make().entry_session is None
    assert make(entry_session="NIGHT").entry_session == "NIGHT"
    with pytest.raises(ValueError, match="entry_session"):
        wiring.make_strategy(warmup_bars=100, entry_session="EVENING")
