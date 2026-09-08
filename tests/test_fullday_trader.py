"""FULL-DAY's Trader is long-only (fullday autoresearch iter04): a SELL never OPENS.

The iter03 consensus run split its round trips by side — 95 longs at +30,220 pts
net (avg +318) vs 59 shorts at +4,203 (avg +71) on a tape that rose ~5x across
the eval window — and dropping the short entry also frees BUY crossings that
previously just closed shorts (the no-flip machine needs two BUYs to get long
again). The probed gain: net +34,423 -> +37,066, dd -3,621 -> -3,379,
full-window Calmar 9.505 -> 10.971 at n 154 -> 100. The shared Trader still
opens shorts; that divergence is what the per-session split exists to allow,
so it is pinned here.
"""
from datetime import datetime

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.fullday.trader import Trader
from forecast_eval.trader import Trader as SharedTrader

T0 = datetime(2026, 9, 9, 9, 0, 0)


def _market(price: float = 22000.0) -> MarketEvent:
    return MarketEvent(
        timestamp=T0,
        price=price,
        volume=1,
        session="DAY",
    )


def _trader() -> Trader:
    # aggression 0 matches the package's PARAMS: limits rest at the touch.
    return Trader(Execution(fee_rate=0.0), tick_size=1.0,
                  aggression_ticks=0, max_position=1)


def _shared() -> SharedTrader:
    return SharedTrader(Execution(fee_rate=0.0), tick_size=1.0,
                        aggression_ticks=0, max_position=1)


def test_flat_sell_opens_no_short_in_the_fullday_package():
    tr = _trader()
    assert tr.on_signal("SELL", _market()) is None
    assert tr.position == 0
    assert not tr.execution.has_pending()


def test_flat_buy_still_opens_a_long():
    tr = _trader()
    order = tr.on_signal("BUY", _market(22000))
    assert order is not None
    assert order.side == "BUY" and order.intent == "OPEN"


def test_sell_while_long_still_closes():
    """The consensus exit path is untouched: only the short ENTRY is gone."""
    tr = _trader()
    tr.position = 1
    tr.entry_price = 21980
    tr.entry_bar_idx = 0
    tr.entry_timestamp = T0
    order = tr.on_signal("SELL", _market(22000))
    assert order is not None
    assert order.side == "SELL" and order.intent == "CLOSE"


def test_a_dropped_sell_still_cancels_a_resting_order():
    """§4.4 holds for the dropped signal exactly as it does for HOLD."""
    tr = _trader()
    tr.on_signal("BUY", _market(22000))
    assert tr.execution.has_pending()
    tr.on_signal("SELL", _market(22000))
    assert not tr.execution.has_pending()


def test_the_shared_trader_still_opens_a_short():
    """The divergence is fullday's alone: the shared machine is unmoved."""
    tr = _shared()
    order = tr.on_signal("SELL", _market(22000))
    assert order is not None
    assert order.side == "SELL" and order.intent == "OPEN"
    assert order.quantity == 1