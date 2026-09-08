# [strategy/day/rsi.py](../../../strategy/day/rsi.py)

**Role:** DAY's plain RSI strategy — the Forecaster that reports the indicator and the SignalEmitter that trades it.
**Pipeline:** a concrete [Forecaster](../../forecaster/base.md) + [SignalEmitter](../base.md), owned by the DAY package.
**Depends on:** [events.py](../../events.md), [forecaster/base.py](../../forecaster/base.md), [strategy/base.py](../base.md)   **Used by:** [strategy/day/wiring.py](wiring.md)

## Responsibility
RSI(14) over **5-minute closes**; **BUY at 20, SELL at 80** — with **BUY entries
gated on regime** (autoresearch `iter05`): the entry only fires when the last
price sits on or above `REGIME_SMA_BARS` (6 000 one-minute rows ≈ one month of
day sessions) simple moving average, read from the same history-through-*t*
frame. No model, no GPU, no probability gate.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `rsi(closes, period=14)` | function | Wilder's RSI, or `None` when there are fewer than `period+1` closes |
| `regime_sma(prices, window=6000)` | function | trailing SMA of raw prints, or `None` when fewer than `window` exist |
| `RsiForecaster(...)` | class | resamples `price` to 5-minute closes; payload carries `rsi`, `last_price`, `regime_sma` |
| `RsiEmitter(buy_below=20, sell_above=80)` | class | BUY at or below 20 **only above the regime SMA**, SELL at or above 80, else HOLD |
| `make_rsi_forecaster(...)` | function | construction seam; swallows the Toto2-shaped keywords |
| `RSI_PERIOD` / `RSI_BUY_BELOW` / `RSI_SELL_ABOVE` / `BAR_MINUTES` / `REGIME_SMA_BARS` | constants | 14 / 20.0 / 80.0 / 5 / 6000 |

## Key points / gotchas
- **The regime gate (iter05) vets BUY entries, never the SELL exit.** An
  oversold print below the one-month SMA is a bear-phase knife-catch — iter02's
  drawdown was exactly those entries — so it HOLDs. A missing or NaN
  `regime_sma` also HOLDs: "regime unreadable" is not "regime fine".
- **The gate makes `--context-length` non-inert for the *non-precompute* mode
  only.** The batch path (what `--precompute`, the recorded-run mode, always
  uses) computes the SMA over the full series; a per-call replay hands the
  forecaster `history_window=context_length` (3008) rows, shorter than the 6
  000-bar window, so every BUY HOLDs there. Deterministic either way — the
  tsv row's invocation says which stream ran.
- **The indicator lives in a Forecaster, and that is not a stylistic choice.** The
  run loop hands the emitter a `Forecast`, never price history, and only at stride
  boundaries — one price every 30 minutes here, which cannot produce a 5-minute
  RSI. The Forecaster is the one seam that receives the history-through-*t* frame.
  `RsiForecaster` forecasts nothing; it reports a state of the tape at *t*.
- **RSI's knobs are module constants, not `PARAMS`.** `real_data.py`'s
  `_resolve_strategy_knobs` raises unless a package's `PARAMS` keys are *exactly*
  the CLI's knob set, and `real_data.py` is frozen. Same convention
  `short_window` / `min_trend_ticks` already follow: edit this file.
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
- **Backtest and live diverge for this session.** `live/config.py` imports
  `.emitter` and `.forecaster` as *modules* and never calls `make_strategy`, so
  `--config day` still trades the pinned Toto2 threshold strategy. See
  [wiring.py](wiring.md).

## Related
[strategy/day/wiring.md](wiring.md) · [strategy/day/emitter.md](emitter.md) · [strategy/day/forecaster.md](forecaster.md) · [tests/test_rsi_strategy.py](../../tests.md)
