# [strategy/night/ridge.py](../../../strategy/night/ridge.py)

**Role:** NIGHT's forecaster since 2026-11-09: the price ridge, which replaced Toto2.
**Pipeline:** the forecaster box of the NIGHT pipeline.
**Depends on:** [forecaster/base.py](../../forecaster/base.md), [events.py](../../events.md)   **Used by:** [strategy/night/wiring.py](wiring.md)

## Responsibility
Predicts the next `target_bars` log return as a linear function of the tape's
own past 60/300/1140/3420-bar log returns and its 1140-bar realised volatility,
the "price ridge" of the 2026-11-08 forecast screen. The live weights are
`strategy/night/ridge_h1140.json`: target 1140 bars, fitted on the night tape
over bars whose target ends by 2024-01-18 03:35.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `fit_ridge(timestamps, prices, *, fit_end, target_bars, step=10, lam=10.0)` | function | fit the weights; returns the JSON-ready coefficient dict |
| `load_coefficients(path)` | function | read that dict back |
| `features_at(logp, b)` | function | the five features at one index of a log-price array |
| `LOOKBACK_BARS` | int | 3420, the longest feature window |
| `RidgeForecaster(*, warmup_bars, forecast_stride_bars, coefficients)` | class | the forecaster |
| `forecast(history_df)` | method | one bar's forecast from the history-through-t frame |
| `forecast_series_batch(timestamps, prices, boundaries, ...)` | method | every boundary at once |

## Key points / gotchas
- **Fitted offline, frozen in the run.** `fit_ridge` samples every `step`-th
  bar whose *target* bar is stamped at or before `fit_end`, so no later price
  reaches the weights. The forecaster reads only bars <= t. Live uses the
  backtested weights unchanged (user decision 2026-11-09: no refit).
- **Needs `LOOKBACK_BARS` + 1 bars of history.** Fewer predicts 0, which holds.
  Live keeps exactly `context_length` bars, so the wiring sets it to 3421 and
  refuses less.
- `forecast_horizon_bars` is the file's `target_bars` (1140), so the report's
  direction hit is measured at the ridge's own horizon; the wiring refuses a
  `horizon_bars` that differs.
- The payload carries only `predicted_price`, `predicted_return` and
  `last_price`. With no `prob_up` the [emitter](emitter.md)'s probability gate
  is skipped and every passing signal trades at strength 1.0; with no
  `median_path` the path check is inert. The trend filter still runs.
- No model, no GPU: live's pre-warm just runs one forecast, and the banner
  shows `ckpt=None`.
- Built and measured on branch `strategy_test_1108_ridge` (`f0c74e9`); the code
  is that branch's file. [forecaster.py](forecaster.md) (Toto2) stays in the
  package unused, so `forecaster_sha` and the Toto2 forecast-cache key do not move.

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/night/forecaster.md](forecaster.md) · [forecaster/base.py](../../forecaster/base.md)
