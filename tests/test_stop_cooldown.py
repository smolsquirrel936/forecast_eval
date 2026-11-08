"""The fullday Trader's stop cooldown (strategy_test_1108, step 3).

After a flatten that lost at least ``stop_cooldown_loss`` of entry, an OPEN on
the closed position's side is skipped until ``stop_cooldown_minutes`` of clock
time have passed. ``stop_cooldown_minutes=0`` is the control.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.fullday import wiring
from forecast_eval.strategy.fullday.trader import Trader

T0 = pd.Timestamp("2024-01-02 09:00")


def _mkt(minute, price=17000.0):
    return MarketEvent(timestamp=T0 + pd.Timedelta(minutes=minute), price=price, volume=1, session="DAY")


def _trader(**kw):
    return Trader(Execution(fee_rate=0.0), aggression_ticks=0, **kw)


def _round_trip(tr, side, entry, exit_, minute=0):
    opp = "SELL" if side == "BUY" else "BUY"
    tr.on_fill(side, 1, fill_price=entry, bar_idx=minute, timestamp=T0 + pd.Timedelta(minutes=minute))
    tr.on_fill(opp, 1, fill_price=exit_, bar_idx=minute + 1, timestamp=T0 + pd.Timedelta(minutes=minute + 1))


def test_stop_sized_loss_blocks_same_side_until_expiry():
    tr = _trader(stop_cooldown_minutes=60, stop_cooldown_loss=0.049)
    _round_trip(tr, "BUY", 100.0, 95.0)          # -5%: arms, block ends at minute 61
    assert tr.on_signal("BUY", _mkt(30)) is None
    assert tr.on_signal("BUY", _mkt(60)) is None
    assert tr.on_signal("BUY", _mkt(61)) is not None


def test_opposite_side_opens_freely():
    tr = _trader(stop_cooldown_minutes=60, stop_cooldown_loss=0.049)
    _round_trip(tr, "BUY", 100.0, 95.0)
    assert tr.on_signal("SELL", _mkt(5)).side == "SELL"


def test_short_stop_blocks_sells():
    tr = _trader(stop_cooldown_minutes=60, stop_cooldown_loss=0.049)
    _round_trip(tr, "SELL", 100.0, 105.0)
    assert tr.on_signal("SELL", _mkt(5)) is None
    assert tr.on_signal("BUY", _mkt(5)) is not None


@pytest.mark.parametrize("exit_", [97.0, 103.0])
def test_smaller_loss_or_a_gain_does_not_arm(exit_):
    tr = _trader(stop_cooldown_minutes=60, stop_cooldown_loss=0.049)
    _round_trip(tr, "BUY", 100.0, exit_)
    assert tr.on_signal("BUY", _mkt(5)) is not None


def test_control_never_arms():
    tr = _trader()
    _round_trip(tr, "BUY", 100.0, 80.0)
    assert tr.on_signal("BUY", _mkt(5)) is not None


def test_a_roll_reopen_bypasses_the_block():
    tr = _trader(stop_cooldown_minutes=60, stop_cooldown_loss=0.049)
    _round_trip(tr, "BUY", 100.0, 95.0)
    with tr.rolling():
        assert tr.on_signal("BUY", _mkt(5)) is not None


@pytest.mark.parametrize("kw", [{"stop_cooldown_minutes": -1, "stop_cooldown_loss": 0.05},
                                {"stop_cooldown_minutes": 1.5, "stop_cooldown_loss": 0.05},
                                {"stop_cooldown_minutes": 60},
                                {"stop_cooldown_minutes": 60, "stop_cooldown_loss": 1.2}])
def test_bad_knobs_raise(kw):
    with pytest.raises(ValueError):
        _trader(**kw)


def test_wiring_control_and_loss_from_stop_pct():
    assert wiring.PARAMS["stop_cooldown_minutes"] == 0
    tr = wiring.make_strategy(warmup_bars=100, stop_cooldown_minutes=1440)["make_trader"](Execution(fee_rate=0.0))
    assert tr.stop_cooldown_minutes == 1440
    assert tr.stop_cooldown_loss == pytest.approx(0.98 * wiring.PARAMS["stop_pct"])
    with pytest.raises(ValueError, match="needs stop_pct"):
        wiring.make_strategy(warmup_bars=100, stop_cooldown_minutes=1440, stop_pct=None)
