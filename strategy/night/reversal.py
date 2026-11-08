"""NIGHT's model-free reversal forecaster (strategy_test_1108, plan step 2).

Predicts that the last ``lookback_bars`` move reverses:
``predicted_return = -(last / price_N_bars_ago - 1)``. It stands in for Toto2
under the unchanged emitter and trader, to test whether the model adds anything
over a plain reversal rule. The payload carries no ``prob_up`` and no
``median_path``, so the emitter's probability gate and path check are inert
and every passing signal trades at full strength; the trend filter still runs.

Kept out of ``forecaster.py`` on purpose: that file is what ``forecaster_sha``
hashes, and the sha is part of the Toto2 forecast-cache key.
"""
from typing import Any, List, Sequence, Union

import numpy as np
import pandas as pd

from ...events import Forecast
from ...forecaster.base import Forecaster


class ReversalForecaster(Forecaster):
    def __init__(self, *, warmup_bars: int, forecast_stride_bars: int,
                 forecast_horizon_bars: int, lookback_bars: int):
        # Tech: store the three scheduling attributes plus the lookback; reject a
        #       lookback that is not a positive int.
        # Why:  the run loop schedules this exactly like Toto2 (SPEC §3), so the
        #       only change in a comparison run is the signal source.
        if not isinstance(lookback_bars, int) or isinstance(lookback_bars, bool) or lookback_bars < 1:
            raise ValueError(f"lookback_bars must be an int >= 1, got {lookback_bars!r}")
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = forecast_horizon_bars
        self.lookback_bars = lookback_bars

    def _make(self, timestamp: Any, last: float, past: float) -> Forecast:
        # Tech: the forecast for one bar from its price and the price N bars back;
        #       zero predicted return when there is no N-bar history yet.
        # Why:  zero is inside every band, so a too-short history holds rather
        #       than trading on a partial lookback.
        r = -(last / past - 1.0) if past is not None and past > 0 else 0.0
        return Forecast(timestamp=timestamp, horizon_bars=self.forecast_horizon_bars,
                        payload={"predicted_price": last * (1.0 + r),
                                 "predicted_return": r, "last_price": last})

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: read the last price and the one lookback_bars earlier from the
        #       history-through-t frame.
        # Why:  only rows <= t are read, the look-ahead contract (SPEC §6).
        prices = history_df["price"]
        past = float(prices.iloc[-1 - self.lookback_bars]) if len(prices) > self.lookback_bars else None
        return self._make(history_df["timestamp"].iloc[-1], float(prices.iloc[-1]), past)

    def forecast_series_batch(self, timestamps: Sequence[Any], prices: Sequence[float],
                              boundaries: Sequence[int], *, batch_size: Union[int, str] = "auto",
                              progress: bool = False) -> List[Forecast]:
        # Tech: index the price array directly at each boundary b and b - N.
        # Why:  the base fallback rebuilds the whole history frame per boundary,
        #       O(n) each -- hours on a 1.9M-bar tape. Same values as forecast().
        p = np.asarray(prices, dtype=float)
        n = self.lookback_bars
        return [self._make(timestamps[b], float(p[b]), float(p[b - n]) if b >= n else None)
                for b in boundaries]
