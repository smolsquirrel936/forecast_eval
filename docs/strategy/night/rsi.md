# [strategy/night/rsi.py](../../../strategy/night/rsi.py)

**Role:** NIGHT's plain RSI strategy — the Forecaster that reports the indicator and the SignalEmitter that trades it.
**Pipeline:** a concrete [Forecaster](../../forecaster/base.md) + [SignalEmitter](../base.md), owned by the NIGHT package.
**Depends on:** [events.py](../../events.md), [forecaster/base.py](../../forecaster/base.md), [strategy/base.py](../base.md)   **Used by:** [strategy/night/wiring.py](wiring.md)

## Responsibility
RSI(14) over **5-minute closes**; **BUY at 30, SELL at 84**. No model, no GPU, no
probability gate, no trend or path confirmation — this replaces the Toto2 banded
threshold strategy on the backtest path.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `rsi(closes, period=14)` | function | Wilder's RSI, or `None` when there are fewer than `period+1` closes |
| `RsiForecaster(...)` | class | resamples `price` to 5-minute closes and reports `payload["rsi"]` |
| `RsiEmitter(buy_below=30, sell_above=84)` | class | BUY at or below 30 **inside the entry window**, SELL at or above 84 at any hour, else HOLD |
| `make_rsi_forecaster(...)` | function | construction seam; swallows the Toto2-shaped keywords |
| `RSI_PERIOD` / `RSI_BUY_BELOW` / `RSI_SELL_ABOVE` / `BAR_MINUTES` | constants | 14 / 30.0 (iter06) / 84.0 (iter07) / 5 |
| `ENTRY_FIRST_HOUR` / `ENTRY_LAST_HOUR` | constants | 15 / 20 (iter08) — BUY candidates outside this clock window HOLD |

## Key points / gotchas
- **The indicator lives in a Forecaster, and that is not a stylistic choice.** The
  run loop hands the emitter a `Forecast`, never price history, and only at stride
  boundaries — one price every 30 minutes here, which cannot produce a 5-minute
  RSI. The Forecaster is the one seam that receives the history-through-*t* frame.
  `RsiForecaster` forecasts nothing; it reports a state of the tape at *t*.
- **RSI's knobs are module constants, not `PARAMS`.** `real_data.py`'s
  `_resolve_strategy_knobs` raises unless a package's `PARAMS` keys are *exactly*
  the CLI's knob set, and `real_data.py` is frozen. Same convention
  `short_window` / `min_trend_ticks` already follow: edit this file.
- **`RSI_BUY_BELOW` is 30, not 20, since iter06 (2026-09-08).** With the hour
  window and the one-week cap in place, a scratch sweep read
  26/28/30/32/34 → 5.90/7.03/7.28/5.74/5.91 Calmar — a hump whose whole band
  beats the incumbent, centred on the textbook oversold line. Night-session mean
  reversion pays at the standard dip; waiting for RSI 20 in a thin tape skips
  the V-recoveries entirely. The shared level test in
  [tests/test_rsi_strategy.py](../../../tests/test_rsi_strategy.py) now reads
  each package's own constants.
- **`RSI_SELL_ABOVE` is 84, not 80, since iter07 (2026-09-08).** A scratch
  sell-level sweep from the iter06 state read 78/80/82/84/85/86/88 →
  5.63/7.28/9.00/8.74/8.65/6.06/4.42 — an 82–85 plateau with the fall-off at
  86. 84 is the measured interior cell: the thin tape's overbought readings
  rarely clear the low 80s, so waiting past 84 would rely on prints that stop
  arriving, and the exit stays ungated by the hour window and only bounded by
  the one-week cap.
- **Wilder smoothing, not a rolling mean** — `ewm(alpha=1/period, adjust=False)`.
  The simple moving average is a different indicator sharing the name and would
  not match any chart this is compared against.
- **`None` is not 50.** Too little history returns `None` and the emitter HOLDs;
  returning a neutral 50 would have the first strides after warm-up trading a
  fabricated midpoint.
- **The payload carries no `predicted_return`**, so forecast-quality metrics (hit
  rate, Spearman IC) read NaN — an indicator predicts nothing, and inventing a
  number would put a fabricated forecast into `forecasts.csv`. Trading metrics and
  attribution are unaffected. `logging_io` and `metrics` both use `.get`.
- **Empty 5-minute bars are dropped**, mirroring
  `Toto2Forecaster._prepare_context`. A NaN close would propagate through `diff()`
  and void the average gain/loss for `period` bars after a thin stretch.
- **Entries are windowed to 15:00–20:59 (night-loop iter03, narrowed iter08,
  2026-09-08).** A BUY candidate outside the clock window becomes HOLD; SELL is
  never windowed, so an exit can always fire overnight. Grounded in iter02's
  entry-hour split: +20,733 pts for the 15:00–21:59 stretch vs -9,891 pts for
  22:00–04:59 — the thin globex stretch makes 5-minute RSI dips that keep
  falling; iter08 then cut the 21:00 hour, which read net-negative in two
  consecutive states (US equities open at 21:30–22:00 local is a momentum
  window where the evening's mean-reversion edge has decayed). The gate reads
  the forecast's own timestamp, so it applies identically on the per-call and
  batch paths; pinned by
  [tests/test_night_entry_window.py](../../../tests/test_night_entry_window.py).
- **Backtest and live diverge for this session.** `live/config.py` imports
  `.emitter` and `.forecaster` as *modules* and never calls `make_strategy`, so
  `--config night` still trades the pinned Toto2 threshold strategy. See
  [wiring.py](wiring.md).

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/night/emitter.md](emitter.md) · [strategy/night/forecaster.md](forecaster.md) · [tests/test_rsi_strategy.py](../../tests.md)
