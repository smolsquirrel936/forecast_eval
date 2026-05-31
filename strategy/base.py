"""Abstract SignalEmitter (SPEC §3)."""
from abc import ABC, abstractmethod
from typing import Literal, Optional

from ..events import Forecast


class SignalEmitter(ABC):
    @abstractmethod
    def emit(self, forecast: Optional[Forecast]) -> Literal["BUY", "SELL", "HOLD"]:
        # Tech: subclasses map a Forecast (or None) to BUY/SELL/HOLD.
        # Why:  this is the single, model-agnostic decision point — it separates
        #       "what the model predicts" from "what we decide to do", so the Trader
        #       and Forecaster can each be swapped without touching the other.
        #       Optional[Forecast] allows model-less paths (dummy emitter) to pass None.
        ...
