# [strategy/night/ridge.py](../../../strategy/night/ridge.py)

**Role:** NIGHT's price-ridge forecaster, a stand-in for Toto2 (strategy_test_1108_ridge).
**Pipeline:** replaces the Toto2 box of the NIGHT pipeline when `signal_source='ridge'`.
**Depends on:** [forecaster/base.py](../../forecaster/base.md), [events.py](../../events.md)   **Used by:** [strategy/night/wiring.py](wiring.md)

## Responsibility
Predicts the next `target_bars` log return as a linear function of the tape's
own past 60/300/1140/3420-bar log returns and its 1140-bar realised volatility,
the "price ridge" of the 2026-11-08 forecast screen. Run under the unchanged
emitter and trader, it tests that screen's ridge as a strategy signal.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `fit_ridge(timestamps, prices, *, fit_end, target_bars, step=10, lam=10.0)` | function | fit the weights; returns the JSON-ready coefficient dict |
| `load_coefficients(path)` | function | read that dict back |
| `features_at(logp, b)` | function | the five features at one index of a log-price array |
| `RidgeForecaster(*, warmup_bars, forecast_stride_bars, coefficients)` | class | the forecaster |
| `forecast(history_df)` | method | one bar's forecast from the history-through-t frame |
| `forecast_series_batch(timestamps, prices, boundaries, ...)` | method | every boundary at once |

## Key points / gotchas
- **Fitted offline, frozen in the run.** `fit_ridge` samples every `step`-th
  bar whose *target* bar is stamped at or before `fit_end`, so no price after
  `fit_end` reaches the weights; results after `fit_end` are out of sample and
  results before it are in sample. The forecaster itself reads only bars <= t.
- The fit standardises the features on its own sample and penalises the
  intercept too, exactly as `screen_tsfm.py`'s ridge.
- `forecast_horizon_bars` is the file's `target_bars`, so the report's direction
  hit is measured at the ridge's own horizon; the wiring refuses a
  `horizon_bars` that differs.
- The payload carries only `predicted_price`, `predicted_return` and
  `last_price`. With no `prob_up` the [emitter](emitter.md)'s probability gate
  is skipped and every passing signal trades at strength 1.0; with no
  `median_path` the path check is inert. The trend filter still runs.
- A ridge prediction is on a different scale from Toto2's 30-bar forecast, so
  a comparison run sets its own band (`buy_threshold`/`sell_threshold`).
- Fewer than 3420 bars before t predicts 0, which holds.
- Kept out of `forecaster.py` for the same reason as [reversal.py](reversal.md):
  that file is hashed into the Toto2 forecast-cache key.

## Related
[strategy/night/reversal.md](reversal.md) · [strategy/night/wiring.md](wiring.md) · [forecaster/base.py](../../forecaster/base.md)
