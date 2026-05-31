"""Trade construction + metric pack (SPEC §7)."""
import math

import numpy as np
import pandas as pd
import pytest

from forecast_eval.events import FillEvent, Forecast
from forecast_eval.metrics import (
    build_trades,
    forecast_quality,
    signal_attribution,
    trading_metrics,
)


def _fill(side, price, ts, fee=0.0) -> FillEvent:
    return FillEvent(timestamp=ts, side=side, fill_price=price,
                     quantity=1, fee=fee)


def _ts(s: int) -> pd.Timestamp:
    return pd.Timestamp("2024-01-02 09:00:00") + pd.Timedelta(seconds=s)


# -------- build_trades -------------------------------------------------------

def test_build_trades_pairs_long_round_trip():
    fills = [_fill("BUY", 100, _ts(0), fee=0.5),
             _fill("SELL", 105, _ts(10), fee=0.5)]
    trades = build_trades(fills)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "LONG"
    assert t.entry_price == 100 and t.exit_price == 105
    assert t.pnl_points == 5
    assert t.fees_points == 1.0
    assert t.net_pnl_points == 4.0


def test_build_trades_pairs_short_round_trip():
    fills = [_fill("SELL", 100, _ts(0), fee=0.5),
             _fill("BUY", 98, _ts(10), fee=0.5)]
    trades = build_trades(fills)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == "SHORT"
    assert t.pnl_points == 2  # (98 - 100) * (-1) = 2
    assert t.net_pnl_points == 1.0


def test_build_trades_handles_open_position_at_end():
    """Opening fill with no matching close — not a complete trade."""
    fills = [_fill("BUY", 100, _ts(0)),
             _fill("SELL", 105, _ts(10)),
             _fill("BUY", 110, _ts(20))]  # opens a new long; no close yet
    trades = build_trades(fills)
    assert len(trades) == 1  # only the first round-trip


# -------- trading_metrics ----------------------------------------------------

def test_trading_metrics_empty():
    m = trading_metrics([])
    assert m["n_trades"] == 0
    assert m["net_pnl_points"] == 0.0
    assert math.isnan(m["hit_rate"])


def test_trading_metrics_basic():
    # Three trades: +10, -5, +3 net.
    fills = [
        _fill("BUY", 100, _ts(0)),  _fill("SELL", 110, _ts(1)),   # +10
        _fill("BUY", 100, _ts(2)),  _fill("SELL", 95,  _ts(3)),   # -5
        _fill("BUY", 100, _ts(4)),  _fill("SELL", 103, _ts(5)),   # +3
    ]
    trades = build_trades(fills)
    m = trading_metrics(trades)
    assert m["n_trades"] == 3
    assert m["gross_pnl_points"] == pytest.approx(8.0)
    assert m["hit_rate"] == pytest.approx(2 / 3)
    assert m["avg_win_points"] == pytest.approx(6.5)   # (10+3)/2
    assert m["avg_loss_points"] == pytest.approx(-5.0)
    # equity = [10, 5, 8]; peak = [10, 10, 10]; drawdown = [0, -5, -2]
    assert m["max_drawdown_points"] == pytest.approx(-5.0)
    assert m["max_drawdown_duration_trades"] == 2


# -------- forecast_quality ---------------------------------------------------

def test_forecast_quality_direction_hit_rate_and_ic():
    ts = pd.date_range("2024-01-02 09:00:00", periods=20, freq="1s")
    # Construct a price path where realized returns over horizon=5 are
    # +0.01, +0.01, -0.005, +0.005, -0.01 (5 forecasts evaluated).
    prices = np.array([100.0] * 20)
    prices[5] = 101.0   # i=0 -> j=5: (101-100)/100 = +0.01
    prices[6] = 101.0   # i=1
    prices[7] = 99.5    # i=2 -> realized -0.005
    prices[8] = 100.5   # i=3 -> +0.005
    prices[9] = 99.0    # i=4 -> -0.01
    df = pd.DataFrame({"timestamp": ts, "price": prices, "volume": 1})

    forecasts = [
        Forecast(ts[0], horizon_bars=5, payload={"predicted_return": +0.02}),
        Forecast(ts[1], horizon_bars=5, payload={"predicted_return": +0.01}),
        Forecast(ts[2], horizon_bars=5, payload={"predicted_return": -0.01}),
        Forecast(ts[3], horizon_bars=5, payload={"predicted_return": +0.005}),
        Forecast(ts[4], horizon_bars=5, payload={"predicted_return": -0.02}),
    ]
    fq = forecast_quality(forecasts, df)
    assert fq["n_forecasts_evaluated"] == 5
    # All 5 directional predictions are correct (signs match realized).
    assert fq["direction_hit_rate"] == pytest.approx(1.0)
    # Predicted magnitudes correlate with realized magnitudes monotonically.
    assert fq["information_coefficient"] > 0.9


def test_forecast_quality_with_no_forecasts_returns_nan():
    df = pd.DataFrame({"timestamp": [_ts(0)], "price": [100.0], "volume": [1]})
    fq = forecast_quality([], df)
    assert fq["n_forecasts"] == 0
    assert math.isnan(fq["direction_hit_rate"])


# -------- signal_attribution -------------------------------------------------

def test_signal_attribution_frictionless_long_round_trip():
    signals = [
        {"timestamp": _ts(0), "direction": "BUY",  "price": 100, "session": "DAY"},
        {"timestamp": _ts(5), "direction": "SELL", "price": 105, "session": "DAY"},
    ]
    a = signal_attribution(signals, realized_net_pnl_points=2.0)
    assert a["signal_pnl_points"] == 5.0
    assert a["signal_round_trips"] == 1
    assert a["unrealized_signal_position"] == 0
    assert a["execution_and_cost_drag_points"] == pytest.approx(3.0)


def test_signal_attribution_no_flip_rule():
    """A SELL while flat opens short; a subsequent SELL is a no-op."""
    signals = [
        {"timestamp": _ts(0), "direction": "SELL", "price": 100, "session": "DAY"},
        {"timestamp": _ts(1), "direction": "SELL", "price": 99,  "session": "DAY"},   # ignored
        {"timestamp": _ts(2), "direction": "BUY",  "price": 98,  "session": "DAY"},   # closes short
    ]
    a = signal_attribution(signals, realized_net_pnl_points=0.0)
    assert a["signal_pnl_points"] == 2.0  # (100 - 98)
    assert a["signal_round_trips"] == 1
