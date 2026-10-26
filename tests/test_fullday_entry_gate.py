"""fullday's entry-only probability gate (reversal loop decision 2).

The gate lives in the Trader, not the emitter, so the emitter's trend-filter
state is exactly what it is without the knob; closes are never gated; and
``entry_min_prob=0.50`` is the inert control — the implied floor is 0.0 and no
emitted signal's strength can fall below it, so every entry still opens.

The floor is the inverse of the emitter's conviction ramp
(``strength = (dir_prob - 0.5) / (full_confidence_prob - 0.5)``, clamped to
[0, 1]): at ``entry_min_prob=0.65`` / ``full_confidence_prob=0.9`` the floor is
``0.15 / 0.4 = 0.375``.
"""
import pandas as pd
import pytest

from forecast_eval.events import MarketEvent, OrderEvent
from forecast_eval.execution import Execution
from forecast_eval.strategy.fullday.trader import Trader
from forecast_eval.strategy.fullday.wiring import make_strategy


def _market(price: float = 17500.0) -> MarketEvent:
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 09:00:00"),
        price=price,
        volume=1,
        session="DAY",
    )


def _trader(entry_min_prob: float) -> Trader:
    return Trader(
        Execution(fee_rate=0.0),
        tick_size=1.0,
        aggression_ticks=0,
        max_position=1,
        entry_min_prob=entry_min_prob,
        full_confidence_prob=0.9,
    )


# -------- the gate blocks weak opens from flat -------------------------------

def test_below_floor_buy_from_flat_submits_nothing():
    tr = _trader(entry_min_prob=0.65)  # floor 0.375
    assert tr.on_signal("BUY", _market(), strength=0.30) is None
    assert not tr.execution.has_pending()


def test_below_floor_sell_from_flat_submits_nothing():
    tr = _trader(entry_min_prob=0.65)
    assert tr.on_signal("SELL", _market(), strength=0.30) is None
    assert not tr.execution.has_pending()


def test_strength_just_above_floor_opens():
    # The gate is strict, so anything at or above the floor opens. (The floor
    # (0.65-0.5)/0.4 is 0.375000...06 in floats — never test exact equality.)
    tr = _trader(entry_min_prob=0.65)
    order = tr.on_signal("BUY", _market(), strength=0.40)
    assert order is not None and order.intent == "OPEN"


def test_gated_entry_signal_still_cancels_a_resting_order():
    # §4.4 — every signal, a gated one included, cancels the resting order.
    tr = _trader(entry_min_prob=0.65)
    stale = OrderEvent(pd.Timestamp("2024-01-02 09:00:01"), "BUY",
                       limit_price=17499, intent="OPEN")
    tr.execution.submit(stale, current_price=17500)
    assert tr.execution.has_pending()
    assert tr.on_signal("BUY", _market(17501), strength=0.30) is None
    assert not tr.execution.has_pending()


# -------- the gate never blocks a close --------------------------------------

def test_below_floor_signal_while_short_still_closes():
    tr = _trader(entry_min_prob=0.65)
    tr.position = -1
    tr.entry_price = 17520
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 09:00:00")
    order = tr.on_signal("BUY", _market(), strength=0.30)
    assert order is not None
    assert order.side == "BUY" and order.intent == "CLOSE"
    assert order.quantity == 1


def test_below_floor_signal_while_long_still_closes():
    tr = _trader(entry_min_prob=0.65)
    tr.position = 1
    tr.entry_price = 17480
    tr.entry_bar_idx = 0
    tr.entry_timestamp = pd.Timestamp("2024-01-02 09:00:00")
    order = tr.on_signal("SELL", _market(), strength=0.30)
    assert order is not None
    assert order.side == "SELL" and order.intent == "CLOSE"
    assert order.quantity == 1


# -------- the control value is inert ------------------------------------------

def test_at_050_every_entry_still_opens():
    tr = _trader(entry_min_prob=0.50)
    assert tr.entry_floor == 0.0
    order = tr.on_signal("BUY", _market(), strength=0.0)
    assert order is not None and order.intent == "OPEN"
    order = tr.on_signal("SELL", _market(), strength=0.0)
    assert order is not None and order.intent == "OPEN"


# -------- wiring: pass-through + ValueError cases ------------------------------

def test_make_strategy_passes_the_gate_to_the_trader():
    strat = make_strategy(warmup_bars=100, entry_min_prob=0.65)
    tr = strat["make_trader"](Execution(fee_rate=0.0))
    assert tr.entry_min_prob == 0.65
    assert tr.entry_floor == pytest.approx((0.65 - 0.5) / (0.9 - 0.5))


def test_make_strategy_rejects_entry_min_prob_below_min_prob():
    with pytest.raises(ValueError, match="entry_min_prob"):
        make_strategy(warmup_bars=100, entry_min_prob=0.40)


def test_make_strategy_rejects_entry_min_prob_at_full_confidence():
    with pytest.raises(ValueError, match="entry_min_prob"):
        make_strategy(warmup_bars=100, entry_min_prob=0.90)


def test_make_strategy_rejects_entry_min_prob_above_full_confidence():
    with pytest.raises(ValueError, match="entry_min_prob"):
        make_strategy(warmup_bars=100, entry_min_prob=0.95)
