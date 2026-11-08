# [strategy/night/reversal.py](../../../strategy/night/reversal.py)

**Role:** NIGHT's model-free reversal forecaster, a stand-in for Toto2 (strategy_test_1108, plan step 2).
**Pipeline:** replaces the Toto2 box of the NIGHT pipeline when `signal_source='reversal'`.
**Depends on:** [forecaster/base.py](../../forecaster/base.md), [events.py](../../events.md)   **Used by:** [strategy/night/wiring.py](wiring.md)

## Responsibility
Predicts that the last `lookback_bars` move reverses:
`predicted_return = -(last / price_N_bars_ago - 1)`. Run under the unchanged
emitter and trader, it tests whether Toto2 adds anything over a plain reversal
rule.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ReversalForecaster(*, warmup_bars, forecast_stride_bars, forecast_horizon_bars, lookback_bars)` | class | the forecaster |
| `forecast(history_df)` | method | one bar's forecast from the history-through-t frame |
| `forecast_series_batch(timestamps, prices, boundaries, ...)` | method | every boundary at once, by direct indexing |

## Key points / gotchas
- The payload carries only `predicted_price`, `predicted_return` and
  `last_price`. With no `prob_up` the [emitter](emitter.md)'s probability gate
  is skipped and every passing signal trades at strength 1.0. With no `median_path` the path check
  is inert. The trend filter still runs, because it needs only `last_price`.
- A raw N-bar return is on a different scale from Toto2's 30-bar forecast, so
  a comparison run sets its own band (`buy_threshold`/`sell_threshold`).
- Fewer than N+1 bars of history predicts 0, which holds.
- `forecast_series_batch` overrides the base fallback, which rebuilds the whole
  history frame per boundary and would take hours on a full tape. It returns
  the same values as `forecast()` (test-covered).
- **Kept out of `forecaster.py` on purpose.** That file is what
  `forecaster_sha` hashes, and the sha is part of the Toto2 forecast-cache key,
  so editing it would orphan every cached Toto2 forecast. A reversal run is
  identified by `signal_source`/`reversal_bars` in `params.json`. Don't run it
  with `--forecast-cache`: there is nothing to save.

## Related
[strategy/night/forecaster.md](forecaster.md) · [strategy/night/wiring.md](wiring.md) · [forecaster/base.py](../../forecaster/base.md)
