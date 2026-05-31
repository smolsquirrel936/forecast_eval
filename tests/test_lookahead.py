"""Forecaster look-ahead protection (SPEC §6 Phase 2 milestone)."""
import pandas as pd
import pytest

from forecast_eval.events import Forecast
from forecast_eval.forecaster.base import Forecaster, assert_no_lookahead
from forecast_eval.forecaster.naive import NaiveLastPrice


def _history(n: int = 30) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-02 09:00:00", periods=n, freq="1s"),
        "price": [100.0 + i for i in range(n)],
        "volume": [1] * n,
    })


def test_naive_forecaster_passes_lookahead_guard():
    fc = NaiveLastPrice(warmup_bars=0, forecast_stride_bars=1,
                        forecast_horizon_bars=5)
    assert_no_lookahead(fc, _history(30), k=10)


class _LeakyForecaster(Forecaster):
    """Caches the largest frame ever shown and predicts from it.

    A correct Forecaster would only consult the frame currently passed in;
    this one peeks at past calls' (larger) frames — the exact bug the
    look-ahead guard is meant to catch.
    """
    warmup_bars = 0
    forecast_stride_bars = 1
    forecast_horizon_bars = 1

    def __init__(self):
        self._cached = None

    def forecast(self, history_df):
        if self._cached is None or len(history_df) > len(self._cached):
            self._cached = history_df
        last_price = float(self._cached["price"].iloc[-1])
        return Forecast(
            timestamp=history_df["timestamp"].iloc[-1],
            horizon_bars=self.forecast_horizon_bars,
            payload={"predicted_price": last_price, "predicted_return": 0.0},
        )


def test_leaky_forecaster_is_caught_by_guard():
    with pytest.raises(AssertionError, match="Look-ahead leak"):
        assert_no_lookahead(_LeakyForecaster(), _history(30), k=10)
