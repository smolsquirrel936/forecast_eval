"""Volatility + multi-timeframe breakout emitter (autoresearch exp227 winner).

This is the strategy the autoresearch sweep converged on — the highest-Calmar
(6.28) configuration across 227+ experiments. It only enters when **all** of the
following agree, which together neutralize the counter-trend PnL leak that a bare
return-threshold emitter suffered:

  * the point forecast clears the return threshold (BUY / SELL band);
  * the directional probability from Toto2's quantile fan clears ``min_prob``;
  * the **short-window** trend of recent prices supports the signal;
  * recent volatility (short-window price range) exceeds ``min_vol`` — skip dead,
    rangebound tape;
  * price sits at a breakout **extreme** of the short window (near the high for a
    BUY, near the low for a SELL); and
  * the **long-window** trend does not oppose the signal.

It reads the same ``payload`` fields Toto2Forecaster already surfaces
(``predicted_return``, ``last_price``, ``prob_up``), so no forecaster change is
needed. Unlike ThresholdEmitter's ``trend_filter`` (an SMA regime gate on a field
the forecaster computes), this emitter carries its own price memory in two deques
and derives every filter itself.
"""
from collections import deque
from typing import Literal, Optional, Tuple

from ..events import Forecast, Signal
from .base import SignalEmitter

Direction = Literal["BUY", "SELL", "HOLD"]


class VolMultiTfEmitter(SignalEmitter):
    def __init__(
        self,
        *,
        buy_threshold: float = 0.0010,
        sell_threshold: float = -0.0010,
        min_prob: Optional[float] = 0.614,
        short_window: int = 3,
        long_window: int = 5,
        min_trend_ticks: float = 2.5,
        breakout_buy: float = 0.65,
        breakout_sell: float = 0.35,
        min_vol: float = 13.0,
    ):
        # Tech: reject crossed thresholds, then store the return band, the optional
        #       probability gate, and the six multi-timeframe / volatility / breakout
        #       knobs. The two deques hold the most recent short/long window prices
        #       (+1 slot so a window-length trend has both an "early" and "late" half).
        # Why:  sell < buy keeps the three return zones well-ordered (same invariant as
        #       ThresholdEmitter). The exp227 defaults are baked in here so constructing
        #       the emitter with no args reproduces the winning strategy exactly.
        if sell_threshold >= buy_threshold:
            raise ValueError(
                f"sell_threshold ({sell_threshold}) must be < "
                f"buy_threshold ({buy_threshold})"
            )
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.min_prob = min_prob
        self.short_window = short_window
        self.long_window = long_window
        self.min_trend_ticks = min_trend_ticks
        self.breakout_buy = breakout_buy
        self.breakout_sell = breakout_sell
        self.min_vol = min_vol
        self._short_prices: deque = deque(maxlen=short_window + 1)
        self._long_prices: deque = deque(maxlen=long_window + 1)

    def _decide(self, forecast: Optional[Forecast]) -> Tuple[Direction, float]:
        # Tech: the shared decision core returning (direction, strength); emit() and
        #       emit_signal() both route through it so they can never disagree.
        # Why:  one place owns the full filter cascade, keeping the BUY/SELL/HOLD
        #       direction (used by attribution) consistent with the emitted Signal.
        if forecast is None:
            return "HOLD", 0.0
        payload = forecast.payload if isinstance(forecast.payload, dict) else {}
        r = payload.get("predicted_return")
        if r is None:
            return "HOLD", 0.0

        # Tech: push this bar's price into both rolling windows before any gating, so
        #       the trend/vol/breakout filters below see history through the current bar.
        # Why:  the filters are all defined on recent price action; the deques are the
        #       emitter's own memory (it is stateful — one instance per backtest run).
        last_price = payload.get("last_price")
        if last_price is not None:
            self._short_prices.append(last_price)
            self._long_prices.append(last_price)

        # Tech: base return-threshold direction — BUY above the upper band, SELL below
        #       the lower band, else HOLD.
        # Why:  the same dead-zone filter as ThresholdEmitter; every later filter can
        #       only turn a directional call into HOLD, never flip it.
        if r >= self.buy_threshold:
            signal_dir: Direction = "BUY"
        elif r <= self.sell_threshold:
            signal_dir = "SELL"
        else:
            return "HOLD", 0.0

        # Tech: directional-probability gate — prob_up is P(up); for a SELL the relevant
        #       conviction is P(down) = 1 - prob_up. Drop signals below min_prob.
        # Why:  the point forecast cleared the band but the quantile fan may be too
        #       uncertain to act; this is the #2 probability filter, inert when either
        #       prob_up is absent or min_prob is None.
        prob_up = payload.get("prob_up")
        if prob_up is not None and self.min_prob is not None:
            dir_prob = prob_up if signal_dir == "BUY" else 1.0 - prob_up
            if dir_prob < self.min_prob:
                return "HOLD", 0.0

        # Tech: short-window filters — need >=3 samples to split into early/late halves.
        #       (a) trend: late-half mean minus early-half mean must move at least
        #       min_trend_ticks in the signal's direction; (b) volatility: the window's
        #       high-low range must exceed min_vol; (c) breakout: last_price's position
        #       within [min, max] must sit past breakout_buy (BUY) / below breakout_sell
        #       (SELL).
        # Why:  this is the core of the winner — only trade a signal that recent tape
        #       already confirms (momentum), in a live-enough regime (vol), at an extreme
        #       where a breakout is underway rather than mean-reverting into the range.
        if len(self._short_prices) >= 3:
            prices = list(self._short_prices)
            early_avg = sum(prices[: len(prices) // 2]) / (len(prices) // 2)
            late_avg = sum(prices[len(prices) // 2 :]) / (
                len(prices) - len(prices) // 2
            )
            trend_change = late_avg - early_avg
            if signal_dir == "BUY" and trend_change < self.min_trend_ticks:
                return "HOLD", 0.0
            if signal_dir == "SELL" and trend_change > -self.min_trend_ticks:
                return "HOLD", 0.0

            price_range = max(prices) - min(prices)
            if price_range < self.min_vol:
                return "HOLD", 0.0

            if price_range > 0:
                normalized = (last_price - min(prices)) / price_range
                if signal_dir == "BUY" and normalized < self.breakout_buy:
                    return "HOLD", 0.0
                if signal_dir == "SELL" and normalized > self.breakout_sell:
                    return "HOLD", 0.0

        # Tech: long-window trend confirmation — the slower early/late mean difference
        #       must at least not oppose the signal (>=0 for BUY, <=0 for SELL).
        # Why:  a second, longer timeframe agreeing keeps the entry aligned with the
        #       broader regime; it only vetoes outright counter-trend entries.
        if len(self._long_prices) >= 3:
            prices = list(self._long_prices)
            early_avg = sum(prices[: len(prices) // 2]) / (len(prices) // 2)
            late_avg = sum(prices[len(prices) // 2 :]) / (
                len(prices) - len(prices) // 2
            )
            long_trend = late_avg - early_avg
            if signal_dir == "BUY" and long_trend < 0:
                return "HOLD", 0.0
            if signal_dir == "SELL" and long_trend > 0:
                return "HOLD", 0.0

        # Tech: a signal that survives every filter is emitted at full conviction.
        # Why:  the cascade is a hard gate, not a graded score — anything that passes is
        #       a full-size entry (the winner ran max_position=1, so strength is 1.0).
        return signal_dir, 1.0

    def emit(self, forecast: Optional[Forecast]) -> Direction:
        # Tech: direction-only view of the decision, for callers (attribution) that
        #       don't need conviction.
        # Why:  keeps the minimal SignalEmitter.emit contract intact.
        return self._decide(forecast)[0]

    def emit_signal(self, forecast: Optional[Forecast]) -> Signal:
        # Tech: full decision as a Signal(direction, strength).
        # Why:  this is what the run loop calls so the direction reaches the Trader.
        direction, strength = self._decide(forecast)
        return Signal(direction=direction, strength=strength)
