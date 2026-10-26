"""The night half's allow_open gate (hybrid_split_iter09).

The knob suppresses only the flat-position open branch: a night signal while
flat submits nothing (after the §4.4 cancel), while closes of an inherited lot
are untouched, and allow_open=True is the copied behavior exactly.
"""
import pandas as pd

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.hybrid.night_half.trader import Trader


def _market(price: float = 17500.0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 20:00:00"),
        price=price, volume=1, session="NIGHT",
    )


def _trader(allow_open: bool) -> Trader:
    return Trader(Execution(fee_rate=0.0), tick_size=1.0,
                  aggression_ticks=3, max_position=1, allow_open=allow_open)


def test_allow_open_false_suppresses_open_from_flat():
    tr = _trader(allow_open=False)
    assert tr.on_signal("BUY", _market()) is None
    assert not tr.execution.has_pending()
    assert tr.on_signal("SELL", _market()) is None
    assert not tr.execution.has_pending()


def test_allow_open_false_still_closes_inherited_lot():
    tr = _trader(allow_open=False)
    tr.on_fill("BUY", 1, fill_price=17480.0, bar_idx=0,
               timestamp=pd.Timestamp("2024-01-02 14:30:00"))
    order = tr.on_signal("SELL", _market())
    assert order is not None
    assert order.side == "SELL" and order.intent == "CLOSE"
    assert order.quantity == 1


def test_allow_open_true_opens_from_flat_as_copied():
    tr = _trader(allow_open=True)
    order = tr.on_signal("BUY", _market())
    assert order is not None
    assert order.side == "BUY" and order.intent == "OPEN"
    assert order.limit_price == 17503


def test_allow_open_false_signal_still_cancels_resting_order():
    tr = _trader(allow_open=False)
    opener = _trader(allow_open=True)
    opener.execution = tr.execution  # the halves share one Execution
    opener.on_signal("BUY", _market())
    assert tr.execution.has_pending()
    assert tr.on_signal("SELL", _market()) is None
    assert not tr.execution.has_pending()
