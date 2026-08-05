"""VolMultiTfEmitter (autoresearch exp227 winner) filter-cascade tests.

The emitter only enters when the return band, probability gate, short-window
trend, volatility, breakout, and long-window trend all agree. Each test drives a
short price sequence into the emitter (it is stateful — it accumulates prices in
rolling deques) and asserts the decision on the final bar.
"""
from datetime import datetime, timedelta

from forecast_eval.events import Forecast
from forecast_eval.strategy.volmultitf import VolMultiTfEmitter

T0 = datetime(2026, 1, 1, 9, 0, 0)


def _feed(em, prices, *, predicted_return, prob_up):
    """Push each price as a Forecast; return the decision on the last one."""
    direction = "HOLD"
    for i, p in enumerate(prices):
        payload = {
            "predicted_return": predicted_return,
            "last_price": float(p),
            "prob_up": prob_up,
        }
        fc = Forecast(timestamp=T0 + timedelta(minutes=i),
                      horizon_bars=30, payload=payload)
        direction = em.emit(fc)
    return direction


def test_none_and_empty_payload_hold():
    em = VolMultiTfEmitter()
    assert em.emit(None) == "HOLD"
    assert em.emit(Forecast(timestamp=T0, horizon_bars=30, payload={})) == "HOLD"


def test_all_filters_agree_emits_buy():
    # Rising, volatile, price at the window high, high prob -> BUY passes every gate.
    em = VolMultiTfEmitter()
    assert _feed(em, [100, 110, 125, 140],
                 predicted_return=0.002, prob_up=0.8) == "BUY"


def test_low_probability_gated_to_hold():
    # Same supportive tape but the directional probability is below min_prob (0.614).
    em = VolMultiTfEmitter()
    assert _feed(em, [100, 110, 125, 140],
                 predicted_return=0.002, prob_up=0.55) == "HOLD"


def test_counter_trend_gated_to_hold():
    # Return says BUY but the short-window trend is falling -> HOLD.
    em = VolMultiTfEmitter()
    assert _feed(em, [140, 125, 110, 100],
                 predicted_return=0.002, prob_up=0.8) == "HOLD"


def test_low_volatility_gated_to_hold():
    # Trend supports BUY but the window range (5) is below min_vol (13) -> HOLD.
    em = VolMultiTfEmitter()
    assert _feed(em, [100, 100, 105, 105],
                 predicted_return=0.002, prob_up=0.8) == "HOLD"


def test_no_breakout_gated_to_hold():
    # Trend + vol pass, but last price sits mid-range (normalized 0.45 < 0.65) -> HOLD.
    em = VolMultiTfEmitter()
    assert _feed(em, [100, 120, 140, 118],
                 predicted_return=0.002, prob_up=0.8) == "HOLD"


def test_below_band_holds():
    # A return inside the dead zone never becomes directional regardless of tape.
    em = VolMultiTfEmitter()
    assert _feed(em, [100, 110, 125, 140],
                 predicted_return=0.0, prob_up=0.8) == "HOLD"
