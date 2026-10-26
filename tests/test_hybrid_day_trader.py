"""The day half's midday open block (hybrid_split_iter18, half-hour in iter22).

The gate suppresses only the flat-position open branch inside the window:
a day signal while flat 11:00-12:59 submits nothing (after the §4.4 cancel),
opens normally outside it, and closes of a held lot are never blocked.
None/None is the copied behavior exactly. Boundaries are float hours
(hour + minute/60), so 9.5 blocks 09:30 onward but not 09:29.
"""
import pandas as pd

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.hybrid.day_half.trader import Trader


def _market(price: float = 17500.0, hour: int = 9,
            minute: int = 30) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp(f"2024-01-02 {hour:02d}:{minute:02d}:00"),
        price=price, volume=1, session="DAY",
    )


def _trader(start=None, end=None) -> Trader:
    return Trader(Execution(fee_rate=0.0), tick_size=1.0,
                  aggression_ticks=3, max_position=1,
                  open_block_start=start, open_block_end=end)


def test_open_block_suppresses_open_inside_the_window():
    tr = _trader(start=11, end=13)
    for hour in (11, 12):
        assert tr.on_signal("BUY", _market(hour=hour)) is None
        assert not tr.execution.has_pending()
    # 13:00 is outside (end exclusive), 10:59 is outside (start inclusive).
    o1 = tr.on_signal("BUY", _market(hour=13))
    assert o1 is not None and o1.intent == "OPEN"
    tr.execution.cancel()
    o2 = tr.on_signal("BUY", _market(hour=10))
    assert o2 is not None and o2.intent == "OPEN"


def test_open_block_never_blocks_closes():
    tr = _trader(start=11, end=13)
    tr.on_fill("BUY", 1, fill_price=17480.0, bar_idx=0,
               timestamp=pd.Timestamp("2024-01-02 09:30:00"))
    order = tr.on_signal("SELL", _market(hour=12))
    assert order is not None
    assert order.side == "SELL" and order.intent == "CLOSE"
    assert order.quantity == 1


def test_none_window_is_the_copied_behavior():
    tr = _trader()
    order = tr.on_signal("BUY", _market(hour=12))
    assert order is not None
    assert order.side == "BUY" and order.intent == "OPEN"
    assert order.limit_price == 17503


def test_blocked_signal_still_cancels_a_resting_order():
    tr = _trader(start=11, end=13)
    opener = _trader()
    opener.execution = tr.execution  # the halves share one Execution
    opener.on_signal("BUY", _market(hour=10))
    assert tr.execution.has_pending()
    assert tr.on_signal("SELL", _market(hour=12)) is None
    assert not tr.execution.has_pending()


def test_half_hour_start_boundary():
    tr = _trader(start=9.5, end=13)
    # 09:30 exactly is inside (start inclusive)...
    assert tr.on_signal("BUY", _market(hour=9, minute=30)) is None
    assert not tr.execution.has_pending()
    # ...09:29 is outside.
    o = tr.on_signal("BUY", _market(hour=9, minute=29))
    assert o is not None and o.intent == "OPEN"
    tr.execution.cancel()
    # integer values keep their hour-only meaning: 13:30 outside [9.5, 13).
    o2 = tr.on_signal("BUY", _market(hour=13, minute=30))
    assert o2 is not None and o2.intent == "OPEN"
