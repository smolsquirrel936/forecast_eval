"""Plain RSI mean-reversion for the NIGHT package — no model, no GPU.

RSI(14) over **5-minute closes**; **BUY at 30, SELL at 80**, with **BUY
entries clock-windowed** (iter03): the entry only fires inside the
15:00-21:59 stretch of the night session. No Toto2, no probability gate, no
trend or path confirmation.

**Why the indicator lives in a Forecaster.** The run loop hands the emitter a
`Forecast`, never price history, and it only calls it at stride boundaries. An
emitter can therefore see one price every `stride_bars` minutes — 30 here — which
cannot produce a 5-minute RSI. The `Forecaster` is the one seam that receives the
history-through-*t* frame, so the indicator is computed there and travels to the
emitter in the payload. `RsiForecaster` forecasts nothing; it reports a state of
the tape at time *t*.

**Parameters are module-level, not in `PARAMS`.** `real_data.py` requires a
package's `PARAMS` keys to be *exactly* the CLI's knob set and raises otherwise,
and `real_data.py` is frozen. So RSI's knobs follow the convention `wiring.py`
already documents for ``short_window`` / ``min_trend_ticks`` — they live in the
file the package owns, and a search that wants to move them edits this file.

**Forecast-quality metrics are vacuous here** and will read NaN: hit rate and
Spearman IC score a predicted return against a realized one, and an indicator
predicts nothing. The payload carries no ``predicted_return``; `logging_io` and
`metrics` both tolerate its absence by design. Trading metrics and attribution
are unaffected.
"""
from typing import Any, List, Literal, Optional, Sequence

import numpy as np
import pandas as pd

from ...events import Forecast, Signal
from ...forecaster.base import Forecaster
from ..base import SignalEmitter

Direction = Literal["BUY", "SELL", "HOLD"]

# Tech: the strategy's three numbers.
# Why:  named constants rather than literals buried in the classes, so the search
#       surface is one obvious block at the top of the file this package owns.
#       RSI_BUY_BELOW moved 20 -> 30 in night-loop iter06: with the hour window
#       and the one-week cap in place, a scratch sweep of the buy level read
#       26/28/30/32/34 -> 5.90/7.03/7.28/5.74/5.91 Calmar -- a hump whose whole
#       26-34 band beats the incumbent, centred on the textbook oversold line.
#       Night-session mean reversion pays at the STANDARD dip, not the deep
#       one: waiting for RSI 20 in a thin tape skips the V-recoveries entirely.
RSI_PERIOD = 14
RSI_BUY_BELOW = 30.0            # oversold -> BUY (iter06: 20 -> 30)
RSI_SELL_ABOVE = 80.0           # overbought -> SELL
BAR_MINUTES = 5

# Tech: the entry window, as local clock hours of the forecast timestamp.
# Why:  night-loop iter03. iter02's round trips split by entry hour into
#       +20,733 pts for 15:00-21:59 and -9,891 pts for 22:00-04:59 (36 winners
#       vs 32 losers spanning 2020-2026, not one disaster trade). The late
#       stretch is the thin globex tape, where a 5-minute RSI dip is noise
#       that keeps falling. Entries only -- SELL exits are never windowed, so
#       a position can always be closed overnight.
ENTRY_FIRST_HOUR = 15
ENTRY_LAST_HOUR = 21

# Tech: how many raw rows `forecast()` keeps before resampling.
# Why:  MEASURED — resampling the whole history-through-t frame made one call
#       cost 16 ms at 500 k rows and grew linearly, which over the night tape's
#       61 633 stride boundaries projected to ~31 minutes against Toto2's 259 s.
#       Wilder's average is an EWM with alpha=1/period, so the weight of a bar k
#       back is (1-1/14)^k: at 250 bars that is ~1e-8, far below float noise.
#       2 500 rows is 500 five-minute bars even with no gaps at all, i.e. double
#       that margin, and it makes the call O(1) in the length of the run.
_TAIL_BARS = 500
_TAIL_ROWS = _TAIL_BARS * BAR_MINUTES


def rsi(closes: pd.Series, period: int = RSI_PERIOD) -> Optional[float]:
    """Wilder's RSI of `closes`, or None when there is not enough history."""
    # Tech: Wilder smoothing of average gain/loss — an EWM with alpha=1/period and
    #       adjust=False, which is the recursive form Wilder defined — then
    #       100 - 100/(1+rs). Needs period+1 closes for the first diff.
    # Why:  `ewm(alpha=1/period, adjust=False)` and not `rolling(period).mean()`:
    #       the simple moving average is a different indicator that happens to
    #       share the name, and it would not match any chart the user compares
    #       against. Returning None rather than 50.0 on short history keeps
    #       "unknown" distinct from "neutral" — the emitter holds on None instead
    #       of trading a fabricated midpoint.
    if len(closes) < period + 1:
        return None
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    # Tech: the two saturated ends, handled before the division.
    # Why:  an unbroken run of up bars gives avg_loss == 0 and rs == inf; the
    #       limit of the formula there is exactly 100, and of the mirror case 0.
    #       Computing it instead would be a ZeroDivisionError on the one tape
    #       shape most likely to matter — a hard trend.
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def completed_closes(timestamps: Any, prices: Any,
                     bar_minutes: int = BAR_MINUTES) -> pd.Series:
    """5-minute closes, excluding the bar still in progress at the last tick.

    Only bars whose whole window has elapsed are returned.
    """
    # Tech: resample last-price per bar, drop empty bars, then drop any bar whose
    #       start is not strictly before the last tick floored to the grid.
    # Why:  a partial bar's "close" is not a close — it moves as more ticks
    #       arrive. That is not merely untidy: it is what would make the
    #       whole-series computation below disagree with the per-call one. With
    #       completed bars, the closes for history-through-t are exactly a PREFIX
    #       of the closes for the full tape, so a causal recursion over the whole
    #       series gives the same number at t as recomputing from scratch at t.
    #       Excluding it also removes a look-ahead question rather than answering
    #       one.
    ts = pd.to_datetime(pd.Series(list(timestamps)))
    series = pd.Series(np.asarray(prices, dtype=float), index=pd.DatetimeIndex(ts))
    closes = series.resample(f"{bar_minutes}min").last().dropna()
    if not len(closes):
        return closes
    cutoff = ts.iloc[-1].floor(f"{bar_minutes}min")
    return closes[closes.index < cutoff]


def rsi_series(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder's RSI at every close, computed in one causal pass."""
    # Tech: the same recursion as `rsi()`, vectorised with `ewm(adjust=False)`,
    #       returning a value per close (NaN until there are period+1 closes).
    # Why:  `ewm(adjust=False)` is causal — the value at i depends only on
    #       positions <= i — so one pass over the full series yields exactly what
    #       recomputing on each prefix would, and `forecast_series_batch` can be
    #       O(n) in total instead of O(n) per boundary. The saturated ends are
    #       filled after the division rather than guarded before it, because here
    #       they are whole columns rather than one scalar.
    if len(closes) < period + 1:
        return pd.Series(dtype=float, index=closes.index)
    delta = closes.diff().iloc[1:]
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    out = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    flat = (avg_loss == 0.0) & (avg_gain == 0.0)
    risen = (avg_loss == 0.0) & (avg_gain > 0.0)
    out[risen] = 100.0
    out[flat] = 50.0
    # Tech: blank the first `period-1` readings.
    # Why:  `rsi()` returns None until period+1 closes exist; the series must
    #       agree, or batch and per-call would disagree exactly at the start of a
    #       run, which is where a warm-up bug hides.
    out.iloc[: period - 1] = np.nan
    return out.reindex(closes.index)


class RsiForecaster(Forecaster):
    """Reports RSI over 5-minute closes. Predicts nothing."""

    def __init__(
        self,
        *,
        warmup_bars: int,
        forecast_stride_bars: int,
        forecast_horizon_bars: int,
        period: int = RSI_PERIOD,
        bar_minutes: int = BAR_MINUTES,
    ) -> None:
        # Tech: the three scheduling attributes the run loop reads, plus the two
        #       indicator settings.
        # Why:  `warmup_bars` and `forecast_stride_bars` are contract, not choice —
        #       `run_backtest` reads them to decide when to ask for a forecast.
        #       `forecast_horizon_bars` is carried so the Forecast is well-formed
        #       and the artifact schema is unchanged, though nothing here predicts
        #       that far: an indicator has no horizon.
        self.warmup_bars = warmup_bars
        self.forecast_stride_bars = forecast_stride_bars
        self.forecast_horizon_bars = forecast_horizon_bars
        self.period = period
        self.bar_minutes = bar_minutes

    def forecast(self, history_df: pd.DataFrame) -> Forecast:
        # Tech: resample the history-through-t frame's `price` onto `bar_minutes`
        #       bars taking the last price in each, drop empty bars, and compute
        #       RSI over the result.
        # Why:  "last price in the bar" is the close, which is what the spec asks
        #       RSI to read. Dropping empty bars matters more here than for the
        #       model: TXF's night session has thin stretches, and a NaN close
        #       would propagate through `diff()` and silently void the average
        #       gain/loss for `period` bars afterwards. This mirrors
        #       `Toto2Forecaster._prepare_context`'s resample rule exactly, so the
        #       two forecasters see the same bars.
        tail = history_df.iloc[-_TAIL_ROWS:]
        closes = completed_closes(tail["timestamp"], tail["price"],
                                  self.bar_minutes)
        value = rsi(closes, self.period)
        prices = history_df["price"]
        ts = history_df["timestamp"]
        last_price = float(prices.iloc[-1]) if len(prices) else None

        # Tech: the payload carries the indicator and the price it was read at,
        #       and deliberately no `predicted_return`.
        # Why:  `metrics.forecast_quality` scores predicted against realized
        #       return; inventing a number to fill that column would put a
        #       fabricated forecast into `forecasts.csv` and into any comparison
        #       made from it. Absent is honest, and both readers use `.get`.
        return Forecast(
            timestamp=pd.to_datetime(ts.iloc[-1]) if len(ts) else None,
            horizon_bars=self.forecast_horizon_bars,
            payload={"rsi": value, "last_price": last_price,
                     "bars": int(len(closes))},
        )

    def forecast_series_batch(
        self,
        timestamps: Sequence[Any],
        prices: Sequence[float],
        boundaries: Sequence[int],
        *,
        batch_size: Any = "auto",
        progress: bool = False,
    ) -> List[Forecast]:
        # Tech: build the 5-minute closes and the whole RSI series ONCE, then read
        #       off each boundary's value by timestamp.
        # Why:  MEASURED — the base fallback rebuilds a full Python-list DataFrame
        #       per boundary and calls `forecast()` on it, which its own docstring
        #       records as O(n) per boundary with no speedup. Over the night tape
        #       that is 61 633 rebuilds of a frame averaging ~900 k rows, and it
        #       is why the model-free strategy ran SLOWER than the GPU model it
        #       replaced. Toto2 overrides this seam for the same reason; this is
        #       the arithmetic version of the same fix.
        closes = completed_closes(timestamps, prices, self.bar_minutes)
        values = rsi_series(closes, self.period)
        ts = pd.DatetimeIndex(pd.to_datetime(pd.Series(list(timestamps))))
        prices_arr = np.asarray(prices, dtype=float)

        # Tech: for each boundary, the last completed bar strictly before the
        #       boundary's own floored minute.
        # Why:  `searchsorted` on a sorted DatetimeIndex makes the whole lookup
        #       one vectorised pass rather than a scan per boundary. "Strictly
        #       before the floored minute" is `completed_closes`' rule restated
        #       for a prefix, which is what keeps this identical to `forecast()`
        #       and free of look-ahead: every bar it can see ended at or before
        #       the boundary's own timestamp.
        cutoffs = ts[list(boundaries)].floor(f"{self.bar_minutes}min")
        idx = closes.index.searchsorted(cutoffs, side="left") - 1

        out: List[Forecast] = []
        for b, i in zip(boundaries, idx):
            value = None
            if i >= 0:
                v = values.iloc[i]
                value = None if pd.isna(v) else float(v)
            out.append(Forecast(
                timestamp=ts[b],
                horizon_bars=self.forecast_horizon_bars,
                payload={"rsi": value, "last_price": float(prices_arr[b]),
                         "bars": int(i + 1)},
            ))
        return out


class RsiEmitter(SignalEmitter):
    """BUY at or below `buy_below` inside the entry window, SELL at or above
    `sell_above` at any hour, else HOLD."""

    def __init__(
        self,
        *,
        buy_below: float = RSI_BUY_BELOW,
        sell_above: float = RSI_SELL_ABOVE,
    ) -> None:
        # Tech: reject a configuration where the two levels cross or touch.
        # Why:  `buy_below >= sell_above` makes some RSI value both oversold and
        #       overbought, and the first branch would silently win. The
        #       ThresholdEmitter this replaces rejects the same shape for the same
        #       reason; failing at construction beats a run that trades a
        #       contradiction.
        if buy_below >= sell_above:
            raise ValueError(
                f"buy_below ({buy_below}) must be strictly below "
                f"sell_above ({sell_above})"
            )
        self.buy_below = buy_below
        self.sell_above = sell_above

    def _decide(self, forecast: Optional[Forecast]) -> Direction:
        # Tech: HOLD on a missing forecast, a non-dict payload, or an RSI of None.
        # Why:  None means "not enough history yet", which is not the same as a
        #       neutral 50 — the first few strides after warm-up have no RSI at
        #       all, and trading them would be trading the absence of a signal.
        if forecast is None:
            return "HOLD"
        payload = forecast.payload if isinstance(forecast.payload, dict) else {}
        value = payload.get("rsi")
        if value is None:
            return "HOLD"
        # Tech: oversold buys, overbought sells — mean reversion — with the BUY
        #       candidate windowed to ENTRY_FIRST_HOUR..ENTRY_LAST_HOUR.
        # Why:  the direction is the whole content of the strategy and it is the
        #       opposite of a momentum reading of the same number, so it is worth
        #       being explicit: RSI 20 is a market that has fallen hard, and this
        #       strategy buys it. The window keeps that dip-buying to the liquid
        #       15:00-21:59 stretch; a missing timestamp is unreadable, and
        #       trading the absence of a clock is the same bug as trading the
        #       absence of a signal. SELL below is NOT windowed: an exit that
        #       could not fire overnight could strand a position to the next
        #       entry window, which converts a gate into a new risk.
        if value <= self.buy_below:
            ts = forecast.timestamp
            if ts is None:
                return "HOLD"
            hour = pd.Timestamp(ts).hour
            if not (ENTRY_FIRST_HOUR <= hour <= ENTRY_LAST_HOUR):
                return "HOLD"
            return "BUY"
        if value >= self.sell_above:
            return "SELL"
        return "HOLD"

    def emit(self, forecast: Optional[Forecast]) -> Direction:
        return self._decide(forecast)

    def emit_signal(self, forecast: Optional[Forecast]) -> Signal:
        # Tech: full conviction on every signal that passes.
        # Why:  strength scales the Trader's aggression and sizing, and RSI
        #       carries no probability to scale it with. A crossing is a crossing;
        #       inventing a ramp from the distance past the level would be a
        #       sizing rule nobody asked for (`max_position` is 1 regardless).
        direction = self._decide(forecast)
        return Signal(direction=direction,
                      strength=1.0 if direction != "HOLD" else 0.0)


def make_rsi_forecaster(*, warmup_bars: int, stride_bars: int,
                        horizon_bars: int, **_ignored: Any) -> RsiForecaster:
    # Tech: the construction seam, accepting and discarding the Toto2-shaped
    #       keywords (`context_length`, `checkpoint`, `device`, ...).
    # Why:  `wiring.make_strategy` and `live/config.build_forecaster` both pass
    #       the model knobs positionally by name; swallowing them here means the
    #       RSI path can be dropped in without editing either caller, and an
    #       unused `--checkpoint` on the command line is inert rather than fatal.
    return RsiForecaster(
        warmup_bars=warmup_bars,
        forecast_stride_bars=stride_bars,
        forecast_horizon_bars=horizon_bars,
    )
