"""NIGHT's Trader is long-only (night autoresearch iter02): a SELL never OPENS.

The iter01 baseline on the night tape split its round trips by side — 236 longs
at +6,000 pts net, 273 shorts at -17,692 pts — across a window in which the
tape 4.5x'd. Selling overbought into that rise carried the entire loss, so the
NIGHT package's Trader drops short entries. The shared Trader still opens them;
that divergence is what the per-session split exists to allow, so it is pinned
here.
"""
import pandas as pd

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.night.trader import Trader
from forecast_eval.trader import Trader as SharedTrader


def _market(price: float = 17500.0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2026-09-08 15:00:00"),
        price=price,
        volume=1,
        session="NIGHT",
    )


def _trader() -> Trader:
    # aggression 0 matches the package's PARAMS: limits rest at the touch.
    return Trader(Execution(fee_rate=0.0), tick_size=1.0,
                  aggression_ticks=0, max_position=1)


def _shared() -> SharedTrader:
    return SharedTrader(Execution(fee_rate=0.0), tick_size=1.0,
                        aggression_ticks=0, max_position=1)


def test_flat_sell_opens_no_short_in_the_night_package():
    tr = _trader()
    assert tr.on_signal("SELL", _market()) is None
    assert tr.position == 0
    assert not tr.execution.has_pending()


def test_flat_buy_still_opens_a_long():
    tr = _trader()
    order = tr.on_signal("BUY", _market(17500))
    assert order is not None
    assert order.side == "BUY" and order.intent == "OPEN"


def test_sell_while_long_still_closes():
    """The RSI>=80 exit path is untouched: only the short ENTRY is gone."""
    tr = _trader()
    tr.position = 1
    tr.entry_price = 17480
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2026-09-08 15:00:00")
    order = tr.on_signal("SELL", _market(17500))
    assert order is not None
    assert order.side == "SELL" and order.intent == "CLOSE"


def test_a_dropped_sell_still_cancels_a_resting_order():
    """§4.4 holds for the dropped signal exactly as it does for HOLD."""
    tr = _trader()
    tr.on_signal("BUY", _market(17500))
    assert tr.execution.has_pending()
    tr.on_signal("SELL", _market(17500))
    assert not tr.execution.has_pending()
    assert tr.position == 0


def test_the_shared_trader_still_opens_shorts():
    """Pins the divergence: night drops the short entry, the shared machine does
    not, so this file cannot silently become a shared-code edit."""
    order = _shared().on_signal("SELL", _market(17500))
    assert order is not None
    assert order.side == "SELL" and order.intent == "OPEN"