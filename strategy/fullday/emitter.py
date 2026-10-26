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


PER-SESSION COPY (STRATEGY_SPLIT_PLAN S1). Verbatim copy of
``strategy/threshold.py`` at sha256:2b91f9839a57 (newline-normalised, as
``tests/test_strategy_isolation.py`` pins it), with only the relative-import depth
changed. It is this package's to edit; the shared original is frozen and
hash-guarded by ``tests/test_strategy_isolation.py``. Diverge freely here.

DIVERGED (fullday_rev_iter16, autoresearch_trading_plan_fullday_reversal.md
decision 8): the emitter gained a position-aware **exit band** —
``bind_position`` lets wiring's ``make_trader`` hand it a live-position getter,
and while positioned, an *opposite*-side signal clears at the narrower
``|predicted_return| >= exit_threshold`` instead of the full band. From flat,
and for same-direction signals, the full band applies unchanged. At the
control ``exit_threshold == buy_threshold`` the cuts are exactly the
pre-knob ones, so a run at the control is byte-identical to one without the
knob; an emitter with no binding behaves as today regardless of the knob.
"""
from collections import deque
from typing import Callable, Literal, Optional, Tuple

from ...events import Forecast, Signal
from ..base import SignalEmitter

Direction = Literal["BUY", "SELL", "HOLD"]


class ThresholdEmitter(SignalEmitter):
    def __init__(
        self,
        *,
        buy_threshold: float = 0.001,
        sell_threshold: float = -0.001,
        min_prob: Optional[float] = None,
        full_confidence_prob: float = 0.9,
        exit_threshold: Optional[float] = None,
        short_window: int = 3,
        min_trend_ticks: float = 3.0,
        path_momentum: bool = True,
        trend_filter: bool = True,
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
        # Tech: exit-band validation — the narrowed exit band is defined as the
        #       mirror of the entry band on the opposite side, so it needs a
        #       symmetric band (buy == -sell), and it must sit in
        #       [0, buy_threshold] so the exit cut is never wider than the band
        #       itself and never fires on same-sign forecasts.
        # Why:  reversal plan decision 8 — a crossed or negative exit cut would
        #       exit on forecasts the entry band calls same-direction, which is
        #       not a slower-or-faster question but a different strategy. Failing
        #       at construction beats trading that silently. None disables the
        #       narrowing entirely (the emitter stays position-blind).
        if exit_threshold is not None:
            if buy_threshold != -sell_threshold:
                raise ValueError(
                    f"exit_threshold needs a symmetric band: buy_threshold "
                    f"({buy_threshold}) must equal -sell_threshold "
                    f"({-sell_threshold})"
                )
            if not 0 <= exit_threshold <= buy_threshold:
                raise ValueError(
                    f"exit_threshold ({exit_threshold}) must be in "
                    f"[0, buy_threshold ({buy_threshold})] or None"
                )
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.min_prob = min_prob
        self.full_confidence_prob = full_confidence_prob
        self.exit_threshold = exit_threshold
        # Tech: short-window trend-filter state — a rolling deque of recent last_price
        #       samples (+1 slot so an early/late half split is defined at full window).
        # Why:  the trend filter (autoresearch exp227-style) only enters when recent tape
        #       does not oppose the signal direction, neutralizing counter-trend entries
        #       that bleed drawdown. On by default (trend_filter=True) with
        #       min_trend_ticks=0.0 — a "non-opposing" veto that still confirms a flat
        #       tape (trend_change==0), so unit tests with a constant last_price are
        #       unaffected; tightening min_trend_ticks makes it demand real momentum.
        self.short_window = short_window
        self.min_trend_ticks = min_trend_ticks
        self.trend_filter = trend_filter
        self.path_momentum = path_momentum
        self._short_prices: deque = deque(maxlen=short_window + 1)
        # Tech: the live-position binding wiring.make_trader installs via
        #       bind_position(); None leaves the emitter position-blind.
        # Why:  decision 8's mechanism — the exit band needs to know which side
        #       is the exit side, but run.py (frozen) must not be touched, so
        #       the emitter pulls the position through a getter the trader's
        #       own factory provides. A getter rather than a snapshot keeps the
        #       read current at every forecast; fills land before emit in the
        #       tick order (run.py steps 1 -> 3), so there is no look-ahead.
        self._position_getter: Optional[Callable[[], int]] = None

    def bind_position(self, getter: Callable[[], int]) -> None:
        """Bind a live-position getter (wiring's make_trader wires it to the Trader)."""
        # Tech: install the getter this emitter will call at each decision.
        # Why:  the emitter must see closes/flips the moment they fill, so it
        #       reads the trader's *current* signed position per forecast —
        #       the exit band is only active while genuinely positioned.
        self._position_getter = getter

    def _trend_confirms(self, direction: Direction, payload: dict) -> bool:
        # Tech: append this bar's last_price to the rolling window, then require the
        #       late-half mean minus early-half mean to move >= min_trend_ticks in the
        #       signal's direction (>= +min_trend_ticks for BUY, <= -min_trend_ticks for
        #       SELL). Need >=3 samples for the early/late split.
        # Why:  this is the short-window momentum confirmation from the exp227 winner —
        #       only act when the very recent tape agrees with the forecast direction.
        #       Returns True (no veto) when there isn't enough history yet, so the first
        #       few forecasts after warmup still trade rather than starving the book.
        last_price = payload.get("last_price")
        if last_price is None:
            return True
        self._short_prices.append(last_price)
        if len(self._short_prices) < 3:
            return True
        prices = list(self._short_prices)
        half = len(prices) // 2
        early_avg = sum(prices[:half]) / half
        late_avg = sum(prices[half:]) / (len(prices) - half)
        trend_change = late_avg - early_avg
        if direction == "BUY":
            return trend_change >= self.min_trend_ticks
        return trend_change <= -self.min_trend_ticks

    def _path_confirms(self, direction: Direction, payload: dict) -> bool:
        # Tech: read the median forecast path (a list of horizon-step prices Toto2
        #       surfaces) and require its midpoint to sit on the same side of last_price
        #       as the endpoint — i.e. the move is sustained through mid-horizon, not a
        #       reverse-then-recover or spike-then-revert shape.
        # Why:  predicted_return is computed from the endpoint alone, so a forecast that
        #       dips hard then recovers clears the threshold yet is a poor entry; this
        #       gate uses the path shape (a field the forecaster already provides) to
        #       veto those. Returns True when median_path/last_price are absent so it is
        #       inert on non-Toto2 forecasters and during the first forecast.
        path = payload.get("median_path")
        last_price = payload.get("last_price")
        if not path or last_price is None or len(path) < 2:
            return True
        mid = path[len(path) // 2]
        if direction == "BUY":
            return mid >= last_price
        return mid <= last_price

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

        # Tech: position-aware band cuts — the full entry band by default; when a
        #       live position is bound and non-zero, the *opposite* side (the
        #       exit) clears at the narrower |r| >= exit_threshold instead.
        # Why:  reversal plan decision 8 — the ~20 h model lag is mostly the wait
        #       for the first forecast that clears the full band the other way,
        #       so an exit that needs only a weak opposite view leaves the wrong
        #       side of a reversal sooner. Same-direction signals and the flat
        #       case keep the full band, so a re-entry still demands a strong
        #       forecast; at the control (exit_threshold == buy_threshold) the
        #       cuts are today's and the run is byte-identical. The gates below
        #       (min_prob, trend filter, path check) apply to the exit signal
        #       exactly as to an entry one.
        buy_cut = self.buy_threshold
        sell_cut = self.sell_threshold
        if self.exit_threshold is not None and self._position_getter is not None:
            position = self._position_getter()
            if position > 0:
                sell_cut = -self.exit_threshold
            elif position < 0:
                buy_cut = self.exit_threshold

        if r >= buy_cut:
            direction: Direction = "BUY"
        elif r <= sell_cut:
            direction = "SELL"
        else:
            return "HOLD", 0.0

        # Tech: if the forecast has no directional probability, trade at full strength
        #       with no probability gate (legacy / non-distributional path).
        # Why:  prob_up is a Toto2-style extra; absent it there's no basis to scale or
        #       gate, so we preserve the pre-distribution behavior exactly.
        prob_up = payload.get("prob_up")
        if prob_up is None:
            # Tech: no distributional field — but still run the trend filter if enabled,
            #       because it only needs last_price (a non-distributional payload field).
            # Why:  the trend confirmation is independent of prob_up; gating it behind
            #       prob_up would silently disable a useful risk filter on non-Toto2
            #       forecasters, and the baseline (trend_filter=False) is untouched.
            if self.trend_filter and not self._trend_confirms(direction, payload):
                return "HOLD", 0.0
            if self.path_momentum and not self._path_confirms(direction, payload):
                return "HOLD", 0.0
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

        # Tech: short-window trend confirmation — require the recent early->late mean
        #       move to support the signal by at least min_trend_ticks (autoresearch
        #       exp227-style); only active when trend_filter=True.
        # Why:  a threshold+prob-confirmed signal that recent tape already contradicts is
        #       the dominant drawdown source (the model is confident but the move has
        #       reversed); this veto cuts those counter-trend entries without touching
        #       the threshold/prob logic, so it composes cleanly on top of --min-prob.
        if self.trend_filter and not self._trend_confirms(direction, payload):
            return "HOLD", 0.0
        if self.path_momentum and not self._path_confirms(direction, payload):
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
