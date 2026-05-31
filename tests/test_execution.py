"""Fill simulator golden cases (SPEC §4.5, §6 Phase 5)."""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent, OrderEvent
from forecast_eval.execution import Execution


def _make_market(price: float, offset_s: int = 0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 09:00:00") + pd.Timedelta(seconds=offset_s),
        price=price,
        volume=1,
        session="DAY",
    )


# -------- Same-tick guard ----------------------------------------------------

def test_no_fill_on_same_tick_as_placement():
    ex = Execution(fee_rate=0.0)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    order = OrderEvent(placed_at, "BUY", limit_price=17503, intent="OPEN")
    ex.submit(order, current_price=17500)

    # The very next call has timestamp == placed_at; must NOT fill (SPEC §4.1).
    fill = ex.check_fill(MarketEvent(placed_at, 17502, 1, "DAY"))
    assert fill is None
    assert ex.has_pending()


# -------- Marketable BUY (SPEC §4.5 worked example) --------------------------

def test_marketable_buy_fills_at_next_print_price():
    """Current=17500, aggression=3 → limit 17503; next prints 17501,17502,17499.
    Fill on the first next print at 17501."""
    ex = Execution(fee_rate=0.00015)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    ex.submit(
        OrderEvent(placed_at, "BUY", limit_price=17503, intent="OPEN"),
        current_price=17500,
    )
    f = ex.check_fill(_make_market(17501, offset_s=1))
    assert f is not None
    assert f.side == "BUY"
    assert f.fill_price == 17501
    assert f.fee == pytest.approx(17501 * 0.00015)
    assert not ex.has_pending()


def test_marketable_sell_fills_at_next_print_price():
    ex = Execution(fee_rate=0.00015)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    ex.submit(
        OrderEvent(placed_at, "SELL", limit_price=17497, intent="OPEN"),
        current_price=17500,
    )
    f = ex.check_fill(_make_market(17499, offset_s=1))
    assert f is not None
    assert f.side == "SELL"
    assert f.fill_price == 17499
    assert f.fee == pytest.approx(17499 * 0.00015)


def test_at_the_touch_buy_is_marketable():
    """limit == current → marketable (BUY limit >= current)."""
    ex = Execution(fee_rate=0.0)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    ex.submit(
        OrderEvent(placed_at, "BUY", limit_price=17500, intent="OPEN"),
        current_price=17500,
    )
    f = ex.check_fill(_make_market(17499, offset_s=1))
    assert f is not None and f.fill_price == 17499


# -------- Passive BUY (SPEC §4.5 worked example) -----------------------------

def test_passive_buy_does_not_fill_at_touch_only_strictly_below():
    """Current=17500, limit=17497 (passive). Prints 17499 (above), 17497
    (touch, no fill), 17496 (strictly below → fill at limit price)."""
    ex = Execution(fee_rate=0.0)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    ex.submit(
        OrderEvent(placed_at, "BUY", limit_price=17497, intent="OPEN"),
        current_price=17500,
    )
    assert ex.check_fill(_make_market(17499, 1)) is None
    assert ex.check_fill(_make_market(17497, 2)) is None  # touch: no fill
    f = ex.check_fill(_make_market(17496, 3))
    assert f is not None
    assert f.fill_price == 17497, "passive fill uses limit price, not improved"


def test_passive_sell_does_not_fill_at_touch_only_strictly_above():
    ex = Execution(fee_rate=0.0)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    ex.submit(
        OrderEvent(placed_at, "SELL", limit_price=17503, intent="OPEN"),
        current_price=17500,
    )
    assert ex.check_fill(_make_market(17502, 1)) is None
    assert ex.check_fill(_make_market(17503, 2)) is None
    f = ex.check_fill(_make_market(17504, 3))
    assert f is not None and f.fill_price == 17503


def test_passive_buy_never_crosses_no_fill():
    ex = Execution(fee_rate=0.0)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    ex.submit(
        OrderEvent(placed_at, "BUY", limit_price=17490, intent="OPEN"),
        current_price=17500,
    )
    for k, p in enumerate([17498, 17495, 17492, 17491], start=1):
        assert ex.check_fill(_make_market(p, k)) is None
    assert ex.has_pending()


# -------- Cancellation -------------------------------------------------------

def test_cancel_removes_pending():
    ex = Execution(fee_rate=0.0)
    placed_at = pd.Timestamp("2024-01-02 09:00:00")
    ex.submit(
        OrderEvent(placed_at, "BUY", limit_price=17503, intent="OPEN"),
        current_price=17500,
    )
    assert ex.has_pending()
    ex.cancel()
    assert not ex.has_pending()
    assert ex.check_fill(_make_market(17502, 1)) is None
