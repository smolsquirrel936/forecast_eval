"""Confidence-aware order placement (ORDER_PLACEMENT_IDEAS #1/#2/#7).

Covers the path that carries Toto2's quantile distribution through to order
aggression and size:

  * Toto2Forecaster._make_forecast / _prob_up — distributional payload (no GPU;
    these methods never touch the model).
  * ThresholdEmitter — strength, the optional min_prob gate, and backward compat
    when the forecast has no prob_up.
  * Trader — confidence-scaled entry offset (#1), confidence-scaled size (#7),
    and full-position closes.
  * Execution — fills order.quantity and scales fee by quantity (#7).
"""
import numpy as np
import pandas as pd
import pytest

from forecast_eval.events import Forecast, MarketEvent, OrderEvent, Signal
from forecast_eval.execution import Execution
from forecast_eval.forecaster.toto2 import (
    MEDIAN_IDX,
    QUANTILE_LEVELS,
    Toto2Forecaster,
)
from forecast_eval.strategy.threshold import ThresholdEmitter
from forecast_eval.trader import Trader


def _market(price=17500.0, offset_s=0):
    return MarketEvent(
        timestamp=pd.Timestamp("2024-01-02 09:00:00") + pd.Timedelta(seconds=offset_s),
        price=price,
        volume=1,
        session="DAY",
    )


def _fc(predicted_return, prob_up=None, **extra):
    """A Forecast whose payload mimics Toto2's, with controllable fields."""
    payload = {"predicted_return": predicted_return, "last_price": 17500.0}
    if prob_up is not None:
        payload["prob_up"] = prob_up
    payload.update(extra)
    return Forecast(timestamp=pd.Timestamp("2024-01-02 09:00:00"),
                    horizon_bars=30, payload=payload)


def _toto():
    # Constructing a Toto2Forecaster is cheap and loads no weights (lazy).
    return Toto2Forecaster(warmup_bars=10, forecast_stride_bars=5,
                           forecast_horizon_bars=3, signal_step="last")


# -------- Toto2 distributional payload ---------------------------------------

def test_make_forecast_surfaces_quantile_distribution():
    fc = _toto()
    # A rising, symmetric fan around last_price=100: median (idx4) ends at 102.
    # Shape (Q=9, H=3); each column sorted ascending across quantiles.
    base = np.linspace(-4, 4, 9)  # offsets per quantile at the final step
    fan = np.stack([np.array([100.0, 101.0, 102.0 + off]) for off in base])
    out = fc._make_forecast(fan, last_price=100.0,
                            timestamp=pd.Timestamp("2024-01-02 09:00:00"))
    p = out.payload
    assert p["quantile_levels"] == list(QUANTILE_LEVELS)
    assert len(p["quantile_prices"]) == 9
    # predicted_price is the median row at the last step (signal_step="last").
    assert p["predicted_price"] == pytest.approx(102.0)
    # Median above last_price => prob_up > 0.5; 80% band is positive width.
    assert p["prob_up"] > 0.5
    assert p["band_return_80"] > 0
    # quantile prices are sorted (model guarantees monotone quantiles).
    assert p["quantile_prices"] == sorted(p["quantile_prices"])


def test_prob_up_monotone_and_clamped():
    fc = _toto()
    q = np.array([90.0, 92.0, 94.0, 96.0, 98.0, 100.0, 102.0, 104.0, 106.0])
    # last_price below the whole fan -> almost certainly up (clamped to 1-0.1=0.9).
    assert fc._prob_up(q, 80.0) == pytest.approx(0.9)
    # last_price above the whole fan -> almost certainly down (clamped to 1-0.9=0.1).
    assert fc._prob_up(q, 120.0) == pytest.approx(0.1)
    # At the median price (98), F=0.5 so prob_up=0.5.
    assert fc._prob_up(q, 98.0) == pytest.approx(0.5)
    # Monotonic: a lower current price never lowers prob_up.
    assert fc._prob_up(q, 95.0) >= fc._prob_up(q, 99.0)


# -------- ThresholdEmitter: gate + strength ----------------------------------

def test_emitter_backward_compat_without_prob():
    em = ThresholdEmitter(buy_threshold=0.001, sell_threshold=-0.001)
    # No prob_up in payload -> direction from thresholds, full strength.
    sig = em.emit_signal(_fc(0.002))
    assert sig.direction == "BUY" and sig.strength == 1.0
    assert em.emit(_fc(-0.002)) == "SELL"
    assert em.emit(_fc(0.0)) == "HOLD"


def test_emitter_strength_from_prob_up():
    em = ThresholdEmitter(buy_threshold=0.001, sell_threshold=-0.001,
                          full_confidence_prob=0.9)
    # BUY clears the return threshold; prob_up=0.7 -> strength=(0.7-0.5)/0.4=0.5.
    sig = em.emit_signal(_fc(0.002, prob_up=0.7))
    assert sig.direction == "BUY"
    assert sig.strength == pytest.approx(0.5)
    # SELL conviction uses P(down)=1-prob_up; prob_up=0.1 -> dir_prob 0.9 -> 1.0.
    sig = em.emit_signal(_fc(-0.002, prob_up=0.1))
    assert sig.direction == "SELL"
    assert sig.strength == pytest.approx(1.0)
    # Weak distribution (prob_up at 0.5) -> strength floors at 0.
    assert em.emit_signal(_fc(0.002, prob_up=0.5)).strength == pytest.approx(0.0)


def test_emitter_min_prob_gate():
    em = ThresholdEmitter(buy_threshold=0.001, sell_threshold=-0.001,
                          min_prob=0.65)
    # Return passes, but directional prob 0.6 < 0.65 -> gated to HOLD.
    sig = em.emit_signal(_fc(0.002, prob_up=0.6))
    assert sig.direction == "HOLD"
    # 0.7 >= 0.65 -> trades.
    assert em.emit_signal(_fc(0.002, prob_up=0.7)).direction == "BUY"


def test_emitter_rejects_bad_config():
    with pytest.raises(ValueError):
        ThresholdEmitter(buy_threshold=0.0, sell_threshold=0.0)
    with pytest.raises(ValueError):
        ThresholdEmitter(min_prob=0.3)  # outside [0.5, 1)
    with pytest.raises(ValueError):
        ThresholdEmitter(full_confidence_prob=0.5)  # must be > 0.5


# -------- Trader: aggression scaling (#1) + sizing (#7) -----------------------

def _trader(**kw):
    return Trader(Execution(fee_rate=0.0), tick_size=1.0, **kw)


def test_default_trader_unchanged_by_strength():
    # max_aggression_ticks=None, max_position=1 -> strength is inert.
    tr = _trader(aggression_ticks=3)
    o_lo = tr.on_signal("BUY", _market(17500), strength=0.0)
    assert o_lo.limit_price == 17503 and o_lo.quantity == 1
    tr2 = _trader(aggression_ticks=3)
    o_hi = tr2.on_signal("BUY", _market(17500), strength=1.0)
    assert o_hi.limit_price == 17503 and o_hi.quantity == 1


def test_aggression_scales_with_strength():
    tr = _trader(aggression_ticks=1, max_aggression_ticks=5)
    # strength 0 -> base offset 1; strength 1 -> max offset 5; 0.5 -> 3.
    assert tr.on_signal("BUY", _market(17500), strength=0.0).limit_price == 17501
    tr = _trader(aggression_ticks=1, max_aggression_ticks=5)
    assert tr.on_signal("BUY", _market(17500), strength=1.0).limit_price == 17505
    tr = _trader(aggression_ticks=1, max_aggression_ticks=5)
    assert tr.on_signal("BUY", _market(17500), strength=0.5).limit_price == 17503


def test_size_scales_with_strength_and_floors_at_one():
    tr = _trader(aggression_ticks=0, max_position=4)
    assert tr.on_signal("BUY", _market(17500), strength=1.0).quantity == 4
    tr = _trader(aggression_ticks=0, max_position=4)
    assert tr.on_signal("BUY", _market(17500), strength=0.5).quantity == 2
    tr = _trader(aggression_ticks=0, max_position=4)
    # Floor: a traded signal is at least 1 contract even at zero strength.
    assert tr.on_signal("BUY", _market(17500), strength=0.0).quantity == 1


def test_close_flattens_full_sized_position():
    tr = _trader(aggression_ticks=0, max_position=3)
    open_order = tr.on_signal("BUY", _market(17500), strength=1.0)
    assert open_order.quantity == 3
    # Simulate the fill so the Trader's position becomes +3.
    tr.on_fill("BUY", 3, fill_price=17500.0, bar_idx=0,
               timestamp=pd.Timestamp("2024-01-02 09:00:00"))
    assert tr.position == 3
    # Opposing signal closes the whole position (no-flip), strength ignored for size.
    close = tr.on_signal("SELL", _market(17510, 1), strength=0.2)
    assert close.intent == "CLOSE" and close.quantity == 3


# -------- Execution: quantity fill + fee scaling (#7) ------------------------

def test_execution_fills_order_quantity_and_scales_fee():
    ex = Execution(fee_rate=0.001)
    placed = pd.Timestamp("2024-01-02 09:00:00")
    order = OrderEvent(placed, "BUY", limit_price=17503, intent="OPEN", quantity=3)
    ex.submit(order, current_price=17500)  # marketable
    fill = ex.check_fill(_market(17502, offset_s=1))
    assert fill is not None
    assert fill.quantity == 3
    # Fee is per-contract: price * rate * quantity.
    assert fill.fee == pytest.approx(17502 * 0.001 * 3)
