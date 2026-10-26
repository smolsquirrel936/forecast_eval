"""fullday's trader carries the original entry across a live roll.

The backtest's spliced tape never closes a position at a roll, so the percent
stop and the break-even lock keep measuring from the original entry. Live closes
the old month and re-opens on the new one; without the carry, the re-open's fill
became a fresh entry and the lock restarted. A roll's re-open is the OPEN the
trader submits inside `rolling()`; its fill arrives later, outside the hook.
"""
from datetime import datetime

import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.exits.base import PositionState
from forecast_eval.strategy.fullday.exits import BreakEvenLock
from forecast_eval.strategy.fullday.trader import Trader

T0 = pd.Timestamp("2026-09-10 09:00:00")
T_ROLL = pd.Timestamp("2026-09-16 13:30:00")


def _market(price, ts=T_ROLL):
    return MarketEvent(timestamp=ts, price=price, volume=1, session="DAY")


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
    # A strategy signal cancels the resting roll order (§4.4); the open that
    # fills afterwards is the strategy's own.
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


def test_the_break_even_lock_stays_armed_across_the_roll():
    # Armed at +3 % before the roll; after the re-open, a return to the
    # ORIGINAL entry must still fire it, as it would in the backtest.
    tr = _trader()
    lock = BreakEvenLock(lock_after_pct=0.03)
    tr.on_fill("BUY", 1, fill_price=17000.0, bar_idx=10, timestamp=T0)
    assert not lock.should_exit(tr.position_state(), _market(17600.0), bar_idx=20)
    with tr.rolling():
        tr.on_fill("SELL", 1, fill_price=17600.0, bar_idx=-1, timestamp=T_ROLL)
        tr.on_signal("BUY", _market(17640.0), strength=1.0)
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert not lock.should_exit(tr.position_state(), _market(17300.0), bar_idx=501)
    assert lock.should_exit(tr.position_state(), _market(17000.0), bar_idx=502)


def test_without_the_carry_the_lock_would_have_restarted():
    # The defect, measured through the lock: a fresh entry at the new month's
    # price re-keys it, so the same return to 17000 is a 3.7 % loss, not a lock.
    tr = _trader()
    lock = BreakEvenLock(lock_after_pct=0.03)
    tr.on_fill("BUY", 1, fill_price=17000.0, bar_idx=10, timestamp=T0)
    lock.should_exit(tr.position_state(), _market(17600.0), bar_idx=20)
    tr.on_fill("SELL", 1, fill_price=17600.0, bar_idx=-1, timestamp=T_ROLL)
    tr.on_signal("BUY", _market(17640.0), strength=1.0)        # not rolling
    tr.on_fill("BUY", 1, fill_price=17650.0, bar_idx=500, timestamp=T_ROLL)
    assert not lock.should_exit(tr.position_state(), _market(17000.0), bar_idx=502)


@pytest.mark.usefixtures("cheap_strategy")
def test_a_live_settled_leg_re_opens_on_its_original_entry():
    # Through LiveRuntime: it settles the lot and re-opens it on the new month;
    # the re-open's fill is then delivered as LiveLoop would, outside the hook,
    # and the trader's entry is still the one the lot was opened at.
    from forecast_eval.tests.test_live_roll import _Market, _rolling_runtime
    rt, rec = _rolling_runtime()
    tr = Trader(rt.broker, tick_size=1.0, aggression_ticks=0,
                reentry_cooldown_signals=16)
    tr.on_fill("BUY", 1, fill_price=17500.0, bar_idx=7,
               timestamp=datetime(2026, 8, 18, 16, 30))
    rt.trader = rt.loop.trader = tr
    rt.settle_rolled_leg(price=17600.0, timestamp=datetime(2026, 8, 19, 14, 55))
    rt._resume_rolled_exposure(_Market(datetime(2026, 8, 19, 16, 0), 17610.0))
    assert tr._roll_reopen is True
    tr.on_fill("BUY", 1, fill_price=17612.0, bar_idx=900,
               timestamp=datetime(2026, 8, 19, 16, 1))
    assert tr.position == 1
    assert (tr.entry_price, tr.entry_bar_idx) == (17500.0, 7)
