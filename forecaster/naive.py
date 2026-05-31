"""Naive baseline: predict next price = last price (zero predicted return).

Per SPEC §6 Phase 2 + §9: with a threshold emitter this trades nothing and
sits at break-even; with the dummy emitter it exercises the full plumbing.
"""
import pandas as pd

from ..events import Forecast
from .base import Forecaster


class NaiveLastPrice(Forecaster):
    def __init__(
        self,
        *,
        warmup_bars: int = 0,
        forecast_stride_bars: int = 12,
        forecast_horizon_bars: int = 12,
    ):
        # Tech: store the three scheduling attributes the run loop reads.
        # Why:  even a no-op model must honor the Forecaster contract (SPEC §3) so
        #       the harness can schedule it identically to a real model.
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = forecast_horizon_bars

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: read the last observed row and predict that same price (return 0).
        # Why:  "tomorrow looks like today" is the canonical zero-skill baseline; a
        #       threshold emitter sees 0 return and holds, so the strategy sits at
        #       break-even — the floor every real model must beat (SPEC §9).
        last_row = history_df.iloc[-1]
        last_price = float(last_row["price"])
        # Tech: stamp the forecast at the last history timestamp and pack the
        #       predicted price/return/last price into the payload.
        # Why:  timestamp == last row keeps it look-ahead-clean by construction;
        #       the payload keys match what ThresholdEmitter and metrics expect.
        return Forecast(
            timestamp=last_row["timestamp"],
            horizon_bars=self.forecast_horizon_bars,
            payload={
                "predicted_price": last_price,
                "predicted_return": 0.0,
                "last_price": last_price,
            },
        )
