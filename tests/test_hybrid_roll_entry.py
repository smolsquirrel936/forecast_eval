"""hybrid's RoutedTrader carries the original entry across a live roll.

The carry lives in the router: decisions go to one half, fills to both, so only
the router sees a roll's re-open AND its fill, and writing the carried entry to
both halves keeps their mirrors in lockstep. The roll's close and re-open are
the calls `live/runtime.py` makes inside `rolling()`; the re-open's fill
arrives later, outside it.

Most cases build the router from default half-Traders, so they test the carry
itself on any branch; the last ones use the packaged trader at the real roll
instant (12:45, DAY session), whatever its tuned entry filters are.
"""
import pandas as pd

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.hybrid import wiring
from forecast_eval.strategy.hybrid.day_half.trader import Trader as DayHalfTrader
from forecast_eval.strategy.hybrid.night_half.trader import Trader as NightHalfTrader
from forecast_eval.strategy.hybrid.router import RoutedTrader

T0 = pd.Timestamp("2026-09-10 09:00:00")
T_ROLL = pd.Timestamp("2026-09-16 12:45:00")       # a DAY-session instant
T_NIGHT = pd.Timestamp("2026-09-16 15:30:00")      # a NIGHT-session instant


def _market(price, ts=T_ROLL, session="DAY"):
    return MarketEvent(timestamp=ts, price=price, volume=1, session=session)


def _trader():
    # default halves over one Execution: no entry filters on either branch
    ex = Execution(fee_rate=0.0)
    return RoutedTrader(DayHalfTrader(ex, aggression_ticks=0),
                        NightHalfTrader(ex, aggression_ticks=0))


def _packaged_trader():
    return wiring.make_strategy(warmup_bars=0)["make_trader"](Execution(fee_rate=0.0))


def _entries(tr):
    return {k: (h.entry_price, h.entry_bar_idx, h.entry_timestamp)
            for k, h in tr.halves.items()}


def _long_then_rolled_close(tr, *, entry=17000.0, close=17600.0):
    tr.on_fill("BUY", 1, fill_price=entry, bar_idx=10, timestamp=T0)
    with tr.rolling():
        tr.on_fill("SELL", 1, fill_price=close, bar_idx=-1, timestamp=T_ROLL)


def test_a_roll_re_open_keeps_the_original_entry_in_both_halves():
    tr = _trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        assert tr.on_signal("BUY", _market(17640.0), strength=1.0) is not None
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert set(_entries(tr).values()) == {(17000.0, 10, T0)}
    ps = tr.position_state()
    assert (ps.entry_price, ps.entry_bar_idx) == (17000.0, 10)


def test_a_night_session_roll_carries_too():
    tr = _trader()
    tr.on_fill("SELL", 1, fill_price=17000.0, bar_idx=10, timestamp=T0)
    with tr.rolling():
        tr.on_fill("BUY", 1, fill_price=16500.0, bar_idx=-1, timestamp=T_NIGHT)
        assert tr.on_signal("SELL", _market(16480.0, T_NIGHT, "NIGHT"), strength=1.0) is not None
    tr.on_fill("SELL", 1, fill_price=16470.0, bar_idx=500, timestamp=T_NIGHT)
    assert set(_entries(tr).values()) == {(17000.0, 10, T0)}


def test_a_strategy_re_open_after_a_flatten_is_a_fresh_entry():
    tr = _trader()
    _long_then_rolled_close(tr)
    assert tr.on_signal("BUY", _market(17640.0), strength=1.0) is not None
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert set(_entries(tr).values()) == {(17650.0, 500, T_ROLL)}


def test_a_cancelled_roll_re_open_does_not_mark_the_next_open():
    tr = _trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        tr.on_signal("BUY", _market(17640.0), strength=1.0)
    tr.on_signal("HOLD", _market(17640.0))
    tr.on_signal("BUY", _market(17640.0), strength=1.0)
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert tr.entry_price == 17650.0


def test_an_exit_cancels_the_roll_mark_only_when_held():
    tr = _trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        tr.on_signal("BUY", _market(17640.0), strength=1.0)
    tr.submit_exit(_market(17640.0))           # flat: nothing to close, no cancel
    assert tr._roll_reopen is True
    tr.position = 1                            # pretend held
    tr.submit_exit(_market(17640.0))
    assert tr._roll_reopen is False


def test_the_other_side_is_never_carried():
    tr = _trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        tr.on_signal("SELL", _market(17640.0), strength=1.0)
    tr.on_fill("SELL", 1, fill_price=17630.0, bar_idx=500, timestamp=T_ROLL)
    assert tr.entry_price == 17630.0


def test_the_carry_is_used_once():
    tr = _trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        tr.on_signal("BUY", _market(17640.0), strength=1.0)
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    tr.on_fill("SELL", 1, fill_price=17700.0, bar_idx=600, timestamp=T_ROLL)
    tr.on_signal("BUY", _market(17700.0), strength=1.0)
    tr.on_fill("BUY", 1, fill_price=17710.0, bar_idx=700, timestamp=T_ROLL)
    assert set(_entries(tr).values()) == {(17710.0, 700, T_ROLL)}


def test_the_packaged_trader_carries_the_live_roll():
    # the deployed trader at hybrid's real roll instant (12:45, DAY session):
    # its own entry filters must let the roll's re-open through, and the
    # entry must come back in both halves
    tr = _packaged_trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        assert tr.on_signal("BUY", _market(17640.0), strength=1.0) is not None
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert set(_entries(tr).values()) == {(17000.0, 10, T0)}


def test_the_position_trail_stays_armed_across_the_roll():
    # the per-session trail (adopted on simtrade_0929) is off: the two are exclusive
    s = wiring.make_strategy(warmup_bars=0, tp_arm_pct=0.15, tp_trail_pct=0.02,
                             tp_giveback_frac=None,
                             day_tp_arm_pct=None, day_tp_trail_pct=None,
                             night_tp_arm_pct=None, night_tp_trail_pct=None)
    tr, rule = s["make_trader"](Execution(fee_rate=0.0)), s["exit_rule"]
    tr.on_fill("BUY", 1, fill_price=17000.0, bar_idx=10, timestamp=T0)
    assert not rule.should_exit(tr.position_state(), _market(19700.0), bar_idx=20)
    with tr.rolling():
        tr.on_fill("SELL", 1, fill_price=19700.0, bar_idx=-1, timestamp=T_ROLL)
        tr.on_signal("BUY", _market(19700.0), strength=1.0)
    tr.on_fill("BUY", 1, fill_price=19710.0, bar_idx=500, timestamp=T_ROLL)
    assert not rule.should_exit(tr.position_state(), _market(19500.0), bar_idx=501)
    assert rule.should_exit(tr.position_state(), _market(19300.0), bar_idx=502)
