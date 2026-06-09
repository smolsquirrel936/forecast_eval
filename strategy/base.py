"""Abstract SignalEmitter (SPEC §3)."""
from abc import ABC, abstractmethod
from typing import Literal, Optional

from ..events import Forecast, Signal


class SignalEmitter(ABC):
    @abstractmethod
    def emit(self, forecast: Optional[Forecast]) -> Literal["BUY", "SELL", "HOLD"]:
        # Tech: subclasses map a Forecast (or None) to BUY/SELL/HOLD.
        # Why:  this is the single, model-agnostic decision point — it separates
        #       "what the model predicts" from "what we decide to do", so the Trader
        #       and Forecaster can each be swapped without touching the other.
        #       Optional[Forecast] allows model-less paths (dummy emitter) to pass None.
        ...

    def emit_signal(self, forecast: Optional[Forecast]) -> Signal:
        # Tech: the richer decision API — returns a Signal (direction + conviction
        #       strength). Default wraps emit() at full strength (1.0).
        # Why:  the run loop calls this so confidence can reach the Trader (#1/#7),
        #       but emitters that have no notion of conviction (dummy/naive) get the
        #       correct behavior for free, and emit() stays the minimal contract.
        #       Emitters with a distribution (ThresholdEmitter) override this.
        return Signal(direction=self.emit(forecast), strength=1.0)
