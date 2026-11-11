"""night's trader carries the original entry across a live roll.

The backtest's spliced tape never closes a position at a roll, so the percent
stop and the take-profit trail keep measuring from the original entry. Live
closes the old month and re-opens on the new one; without the carry, the
re-open's fill became a fresh entry and the trail restarted. A roll's re-open
is the OPEN the trader submits inside `rolling()`; its fill arrives later,
outside the hook. Mirrors tests/test_fullday_roll_entry.py.
"""
import pandas as pd

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.exits.profit_trail import ProfitTrail
from forecast_eval.strategy.night.trader import Trader

T0 = pd.Timestamp("2026-09-10 09:00:00")
T_ROLL = pd.Timestamp("2026-09-16 15:30:00")


def _market(price, ts=T_ROLL):
    return MarketEvent(timestamp=ts, price=price, volume=1, session="NIGHT")


def _trader(**kw):
    kw.setdefault("aggression_ticks", 0)
    return Trader(Execution(fee_rate=0.0), tick_size=1.0, **kw)


def _long_then_rolled_close(tr, *, entry=17000.0, close=17600.0):
    tr.on_fill("BUY", 1, fill_price=entry, bar_idx=10, timestamp=T0)
    with tr.rolling():
        tr.on_fill("SELL", 1, fill_price=close, bar_idx=-1, timestamp=T_ROLL)


def test_a_roll_re_open_keeps_the_original_entry():
    tr = _trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        assert tr.on_signal("BUY", _market(17640.0), strength=1.0) is not None
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert (tr.entry_price, tr.entry_bar_idx, tr.entry_timestamp) == (17000.0, 10, T0)


def test_a_strategy_re_open_after_a_flatten_is_a_fresh_entry():
    tr = _trader()
    _long_then_rolled_close(tr)
    assert tr.on_signal("BUY", _market(17640.0), strength=1.0) is not None
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert (tr.entry_price, tr.entry_bar_idx) == (17650.0, 500)


def test_a_cancelled_roll_re_open_does_not_mark_the_next_open():
    tr = _trader()
    _long_then_rolled_close(tr)
    with tr.rolling():
        tr.on_signal("BUY", _market(17640.0), strength=1.0)
    tr.on_signal("HOLD", _market(17640.0))
    tr.on_signal("BUY", _market(17640.0), strength=1.0)
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert tr.entry_price == 17650.0


def test_an_exit_cancels_the_roll_mark_too():
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
    assert tr.entry_price == 17710.0


def test_the_take_profit_trail_stays_armed_across_the_roll():
    # Armed at +15 % before the roll (best 19700); after the re-open a 2 % give
    # back from that best must still fire, as it would in the backtest.
    tr = _trader()
    trail = ProfitTrail(0.15, trail_pct=0.02)
    tr.on_fill("BUY", 1, fill_price=17000.0, bar_idx=10, timestamp=T0)
    assert not trail.should_exit(tr.position_state(), _market(19700.0), bar_idx=20)
    with tr.rolling():
        tr.on_fill("SELL", 1, fill_price=19700.0, bar_idx=-1, timestamp=T_ROLL)
        tr.on_signal("BUY", _market(19700.0), strength=1.0)
    tr.on_fill("BUY", 1, fill_price=19710.0, bar_idx=500, timestamp=T_ROLL)
    assert not trail.should_exit(tr.position_state(), _market(19500.0), bar_idx=501)
    assert trail.should_exit(tr.position_state(), _market(19300.0), bar_idx=502)


def test_without_the_carry_the_trail_would_have_restarted():
    # The defect, measured through the trail: a fresh entry at the new month's
    # price re-keys it, so the same 19300 is a loss on a new trade, not a give-back.
    tr = _trader()
    trail = ProfitTrail(0.15, trail_pct=0.02)
    tr.on_fill("BUY", 1, fill_price=17000.0, bar_idx=10, timestamp=T0)
    trail.should_exit(tr.position_state(), _market(19700.0), bar_idx=20)
    tr.on_fill("SELL", 1, fill_price=19700.0, bar_idx=-1, timestamp=T_ROLL)
    tr.on_signal("BUY", _market(19700.0), strength=1.0)        # not a roll leg
    tr.on_fill("BUY", 1, fill_price=19710.0, bar_idx=500, timestamp=T_ROLL)
    assert not trail.should_exit(tr.position_state(), _market(19300.0), bar_idx=502)
