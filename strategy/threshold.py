"""Threshold signal emitter (SPEC §5 config example).

Reads payload['predicted_return'] from the Forecast and emits BUY/SELL/HOLD
based on configured thresholds.
"""
from typing import Literal, Optional

from ..events import Forecast
from .base import SignalEmitter


class ThresholdEmitter(SignalEmitter):
    def __init__(
        self,
        *,
        buy_threshold: float = 0.001,
        sell_threshold: float = -0.001,
    ):
        # Tech: reject configs where sell_threshold isn't strictly below
        #       buy_threshold, then store both.
        # Why:  overlapping/crossed thresholds would create an ambiguous band where a
        #       return could qualify as both BUY and SELL; enforcing sell < buy
        #       guarantees the three zones (sell / hold / buy) are well-ordered.
        if sell_threshold >= buy_threshold:
            raise ValueError(
                f"sell_threshold ({sell_threshold}) must be < "
                f"buy_threshold ({buy_threshold})"
            )
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold

    def emit(self, forecast: Optional[Forecast]) -> Literal["BUY", "SELL", "HOLD"]:
        # Tech: with no forecast or no usable predicted_return in the payload, hold.
        # Why:  during warm-up or for a model that didn't emit a return there is no
        #       basis to act, and standing aside is the safe default (no blind trade).
        if forecast is None:
            return "HOLD"
        r = forecast.payload.get("predicted_return") if isinstance(
            forecast.payload, dict
        ) else None
        if r is None:
            return "HOLD"
        # Tech: BUY when the predicted return clears the upper band, SELL when it
        #       breaches the lower band, otherwise HOLD.
        # Why:  the dead zone between thresholds filters weak/noisy signals so the
        #       strategy only trades on convictions large enough to (hopefully) beat
        #       execution cost — the central tunable of this strategy.
        if r >= self.buy_threshold:
            return "BUY"
        if r <= self.sell_threshold:
            return "SELL"
        return "HOLD"
