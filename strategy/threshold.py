"""Threshold signal emitter (SPEC §5 config example).

Reads ``payload['predicted_return']`` from the Forecast and emits BUY/SELL/HOLD
based on configured thresholds. When the forecast also carries a directional
probability (``payload['prob_up']``, e.g. from Toto2's quantile fan) the emitter
additionally:

  * optionally **gates** trades on a minimum directional probability
    (``min_prob``) — the probability filter from ORDER_PLACEMENT_IDEAS #2; and
  * derives a conviction **strength** in [0, 1] that the Trader uses to scale
    order aggression (#1) and position size (#7).

With ``min_prob=None`` and a payload that has no ``prob_up`` (any non-Toto2
forecaster), behavior is identical to the original return-threshold emitter and
strength is 1.0.
"""
from typing import Literal, Optional, Tuple

from ..events import Forecast, Signal
from .base import SignalEmitter

Direction = Literal["BUY", "SELL", "HOLD"]


class ThresholdEmitter(SignalEmitter):
    def __init__(
        self,
        *,
        buy_threshold: float = 0.001,
        sell_threshold: float = -0.001,
        min_prob: Optional[float] = None,
        full_confidence_prob: float = 0.9,
    ):
        # Tech: reject configs where sell_threshold isn't strictly below
        #       buy_threshold, then store both plus the probability knobs.
        # Why:  overlapping/crossed thresholds would create an ambiguous band where a
        #       return could qualify as both BUY and SELL; enforcing sell < buy
        #       guarantees the three zones (sell / hold / buy) are well-ordered.
        if sell_threshold >= buy_threshold:
            raise ValueError(
                f"sell_threshold ({sell_threshold}) must be < "
                f"buy_threshold ({buy_threshold})"
            )
        # Tech: validate the optional probability gate and the strength-saturation
        #       point — both are probabilities, and full_confidence_prob must sit
        #       strictly above 0.5 to define a usable [0.5, full] conviction ramp.
        # Why:  a min_prob outside (0.5, 1) would either never gate or reject every
        #       directional call; full_confidence_prob <= 0.5 would make the strength
        #       ramp divide by zero / go negative. Failing here beats silent garbage.
        if min_prob is not None and not 0.5 <= min_prob < 1.0:
            raise ValueError(
                f"min_prob ({min_prob}) must be in [0.5, 1.0) or None"
            )
        if not 0.5 < full_confidence_prob <= 1.0:
            raise ValueError(
                f"full_confidence_prob ({full_confidence_prob}) must be in (0.5, 1.0]"
            )
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.min_prob = min_prob
        self.full_confidence_prob = full_confidence_prob

    def _decide(self, forecast: Optional[Forecast]) -> Tuple[Direction, float]:
        # Tech: the shared decision core returning (direction, strength); emit() and
        #       emit_signal() both route through it so they can never disagree.
        # Why:  one place owns "what do we do and how strongly", keeping the BUY/SELL/
        #       HOLD direction (used by attribution) bit-for-bit consistent with the
        #       conviction the Trader sizes on.
        if forecast is None:
            return "HOLD", 0.0
        payload = forecast.payload if isinstance(forecast.payload, dict) else {}
        r = payload.get("predicted_return")
        if r is None:
            return "HOLD", 0.0

        # Tech: the original return-threshold direction — BUY above the upper band,
        #       SELL below the lower band, else HOLD.
        # Why:  the dead zone between thresholds filters weak/noisy point forecasts;
        #       this is unchanged so default backtests reproduce exactly.
        if r >= self.buy_threshold:
            direction: Direction = "BUY"
        elif r <= self.sell_threshold:
            direction = "SELL"
        else:
            return "HOLD", 0.0

        # Tech: if the forecast has no directional probability, trade at full strength
        #       with no probability gate (legacy / non-distributional path).
        # Why:  prob_up is a Toto2-style extra; absent it there's no basis to scale or
        #       gate, so we preserve the pre-distribution behavior exactly.
        prob_up = payload.get("prob_up")
        if prob_up is None:
            return direction, 1.0

        # Tech: directional probability = P(move in the signal's direction); optionally
        #       gate on min_prob, then map [0.5, full_confidence_prob] -> [0, 1] for the
        #       conviction strength (clamped).
        # Why:  prob_up is P(up); for a SELL the relevant conviction is P(down). A
        #       directional prob below min_prob means the point forecast cleared the
        #       threshold but the distribution is too uncertain to act (#2). The strength
        #       ramp gives weak-but-passing signals a small size/passive offset and
        #       saturates at full_confidence_prob so the model's max decile read == 1.0.
        dir_prob = prob_up if direction == "BUY" else 1.0 - prob_up
        if self.min_prob is not None and dir_prob < self.min_prob:
            return "HOLD", 0.0
        span = self.full_confidence_prob - 0.5
        strength = (dir_prob - 0.5) / span
        strength = max(0.0, min(1.0, strength))
        return direction, strength

    def emit(self, forecast: Optional[Forecast]) -> Direction:
        # Tech: direction-only view of the decision, for callers (attribution, the
        #       dummy-path default) that don't need conviction.
        # Why:  keeps the minimal SignalEmitter.emit contract intact.
        return self._decide(forecast)[0]

    def emit_signal(self, forecast: Optional[Forecast]) -> Signal:
        # Tech: full decision as a Signal(direction, strength).
        # Why:  this is what the run loop calls so conviction reaches the Trader for
        #       aggression (#1) and sizing (#7).
        direction, strength = self._decide(forecast)
        return Signal(direction=direction, strength=strength)
