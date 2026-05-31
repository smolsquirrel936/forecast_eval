"""Trader state machine + entry tracking (SPEC §4.2, §4.3)."""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent
from forecast_eval.execution import Execution
from forecast_eval.trader import Trader


def _market(price: float = 17500.0, offset_s: int = 0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 09:00:00") + pd.Timedelta(seconds=offset_s),
        price=price,
        volume=1,
        session="DAY",
    )


def _trader(aggression: int = 3) -> Trader:
    return Trader(Execution(fee_rate=0.0), tick_size=1.0,
                  aggression_ticks=aggression, max_position=1)


# -------- §4.2 state machine -------------------------------------------------

def test_flat_buy_submits_aggressive_limit_above_market():
    tr = _trader(aggression=3)
    order = tr.on_signal("BUY", _market(17500))
    assert order is not None
    assert order.side == "BUY" and order.intent == "OPEN"
    assert order.limit_price == 17503


def test_flat_sell_submits_aggressive_limit_below_market():
    tr = _trader(aggression=3)
    order = tr.on_signal("SELL", _market(17500))
    assert order.side == "SELL" and order.intent == "OPEN"
    assert order.limit_price == 17497


def test_flat_hold_does_nothing():
    tr = _trader()
    assert tr.on_signal("HOLD", _market(17500)) is None
    assert not tr.execution.has_pending()


def test_long_buy_no_pyramiding():
    tr = _trader()
    tr.position = 1  # simulate already long
    assert tr.on_signal("BUY", _market(17500)) is None


def test_long_sell_submits_close_not_flip():
    tr = _trader(aggression=3)
    tr.position = 1
    tr.entry_price = 17480
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 09:00:00")
    order = tr.on_signal("SELL", _market(17500))
    assert order.intent == "CLOSE"
    assert order.side == "SELL"
    assert order.limit_price == 17497


def test_short_buy_submits_close_not_flip():
    tr = _trader(aggression=3)
    tr.position = -1
    tr.entry_price = 17520
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 09:00:00")
    order = tr.on_signal("BUY", _market(17500))
    assert order.intent == "CLOSE"
    assert order.side == "BUY"
    assert order.limit_price == 17503


# -------- §4.4 pending cancelled on new signal -------------------------------

def test_new_signal_cancels_prior_pending():
    tr = _trader()
    tr.on_signal("BUY", _market(17500))
    assert tr.execution.has_pending()
    # HOLD cancels the pending order even though it submits none.
    tr.on_signal("HOLD", _market(17501, 1))
    assert not tr.execution.has_pending()


# -------- Entry tracking (Phase 3 requirement) -------------------------------

def test_on_fill_records_entry_on_open_and_clears_on_close():
    tr = _trader()
    ts0 = pd.Timestamp("2024-01-02 09:00:00")
    tr.on_fill("BUY", 1, fill_price=17500.0, bar_idx=0, timestamp=ts0)
    assert tr.position == 1
    assert tr.entry_price == 17500.0
    assert tr.entry_bar_idx == 0
    assert tr.entry_timestamp == ts0
    state = tr.position_state()
    assert state is not None and state.side == "LONG"

    ts1 = ts0 + pd.Timedelta(seconds=10)
    tr.on_fill("SELL", 1, fill_price=17510.0, bar_idx=10, timestamp=ts1)
    assert tr.position == 0
    assert tr.entry_timestamp is None
    assert tr.position_state() is None


# -------- submit_exit cancels pending and emits a close ----------------------

def test_submit_exit_cancels_pending_and_emits_close_when_long():
    tr = _trader(aggression=3)
    tr.on_fill("BUY", 1, fill_price=17500.0, bar_idx=0,
               timestamp=pd.Timestamp("2024-01-02 09:00:00"))
    # Park a stale order on the execution book — submit_exit must cancel it
    # before placing the close.
    from forecast_eval.events import OrderEvent
    stale = OrderEvent(pd.Timestamp("2024-01-02 09:00:01"), "BUY",
                       limit_price=17499, intent="OPEN")
    tr.execution.submit(stale, current_price=17500)
    assert tr.execution.has_pending()

    order = tr.submit_exit(_market(17520, 5))
    assert order is not None
    assert order.intent == "CLOSE"
    assert order.side == "SELL"
    assert order.limit_price == 17517  # 17520 - 3
    # The pending order on the book is now the close, not the stale one.
    assert tr.execution.pending_order() is order


def test_submit_exit_when_flat_returns_none():
    tr = _trader()
    assert tr.submit_exit(_market(17500)) is None
