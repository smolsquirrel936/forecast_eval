"""SimBroker conformance against the Broker interface.

SimBroker must be a transparent wrapper: every SPEC §4.5 fill rule and the
same-tick guard have to survive the indirection untouched. These mirror the
golden cases in test_execution.py, driven through the Broker API instead.
"""
from datetime import datetime, timedelta

import pytest

from forecast_eval.events import MarketEvent, OrderEvent
from forecast_eval.execution import Execution
from forecast_eval.live.broker.base import Broker
from forecast_eval.live.broker.sim import SimBroker

T0 = datetime(2024, 1, 2, 9, 0, 0)


def _market(offset_s: int, price: float) -> MarketEvent:
    return MarketEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        price=price,
        volume=1,
        session="DAY",
    )


def _broker(fee_rate=0.00015) -> SimBroker:
    return SimBroker(Execution(fee_rate=fee_rate))


def test_simbroker_is_a_broker():
    assert isinstance(_broker(), Broker)


def test_empty_book_yields_no_fills():
    b = _broker()
    assert b.has_pending() is False
    assert b.pending_order() is None
    assert b.poll_fills(_market(1, 100.0)) == []


def test_submit_then_pending_accessors():
    b = _broker()
    order = OrderEvent(T0, "BUY", 103.0, "OPEN")
    b.submit(order, current_price=100.0)
    assert b.has_pending() is True
    assert b.pending_order() is order


def test_cancel_clears_the_book():
    b = _broker()
    b.submit(OrderEvent(T0, "BUY", 103.0, "OPEN"), current_price=100.0)
    b.cancel()
    assert b.has_pending() is False
    assert b.poll_fills(_market(1, 100.0)) == []


def test_same_tick_guard_survives_the_wrapper():
    """An order placed at tick t must not fill against tick t's own print."""
    b = _broker()
    b.submit(OrderEvent(T0, "BUY", 103.0, "OPEN"), current_price=100.0)
    assert b.poll_fills(MarketEvent(T0, 100.0, 1, "DAY")) == []
    # ...but the next print does fill it.
    assert len(b.poll_fills(_market(1, 101.0))) == 1


def test_marketable_buy_fills_at_next_print_price():
    b = _broker()
    b.submit(OrderEvent(T0, "BUY", 103.0, "OPEN"), current_price=100.0)
    fills = b.poll_fills(_market(1, 101.5))
    assert len(fills) == 1
    assert fills[0].fill_price == 101.5
    assert fills[0].side == "BUY"


def test_passive_buy_needs_a_strict_cross_and_fills_at_the_limit():
    b = _broker()
    b.submit(OrderEvent(T0, "BUY", 98.0, "OPEN"), current_price=100.0)
    assert b.poll_fills(_market(1, 98.0)) == []      # touch is not a cross
    fills = b.poll_fills(_market(2, 97.5))           # strictly below
    assert len(fills) == 1
    assert fills[0].fill_price == 98.0               # no price improvement


def test_fill_is_drained_not_repeated():
    """poll_fills consumes; a second call must not re-report the same fill."""
    b = _broker()
    b.submit(OrderEvent(T0, "BUY", 103.0, "OPEN"), current_price=100.0)
    assert len(b.poll_fills(_market(1, 101.0))) == 1
    assert b.poll_fills(_market(2, 101.0)) == []
    assert b.has_pending() is False


def test_fee_scales_with_quantity():
    b = _broker(fee_rate=0.001)
    b.submit(OrderEvent(T0, "BUY", 103.0, "OPEN", quantity=3), current_price=100.0)
    fill = b.poll_fills(_market(1, 100.0))[0]
    assert fill.quantity == 3
    assert fill.fee == pytest.approx(100.0 * 0.001 * 3)


def test_broker_position_is_none_offline():
    """The sim has no venue to ask, so reconciliation must skip it."""
    assert _broker().broker_position() is None
