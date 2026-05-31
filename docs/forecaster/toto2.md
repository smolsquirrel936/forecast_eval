# [forecaster/toto2.py](../../forecaster/toto2.py)

**Role:** Adapter wrapping the Datadog Toto-2.0 time-series model as a [Forecaster](base.md).
**Pipeline:** the concrete real-world model.
**Depends on:** [events.py](../events.md), [forecaster/base.py](base.md), `torch` + `toto2` (lazy)
**Used by:** [real_data_demo.py](../real_data_demo.md), [real_data.py](../real_data.md), [compare_models.py](../compare_models.md), [test_toto2.py](../test_toto2.md)

## Responsibility
Feeds the framework's close-price history into Toto-2.0 (univariate) and reads the
median forecast back as a `predicted_return`. A thin adapter — all the trading
logic lives elsewhere.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Toto2Forecaster(warmup_bars, …, context_length, checkpoint, device, bar_freq, signal_step)` | class | the model adapter |
| `forecast(history_df)` | method | run inference; return a `Forecast` with the median path |
| `_load_model` / `_prepare_context` | private | lazy load; build the ≤context_length series |

## Key points / gotchas
- **Lazy imports:** `torch` and `toto2` import on the *first* `forecast()` call, so
  the rest of the framework runs without those deps installed. The model is also
  cached on the instance → this object is **stateful** (use factories in sweeps).
- **patch_size alignment:** context length is truncated to a multiple of
  `model.config.patch_size` (the `PatchedCausalStdScaler` requires divisibility).
  A context shorter than one patch raises.
- **Shape:** `(B=1, C=1, T)`, close-price only. Multivariate OHLC is a clean
  extension (`_prepare_context` → `(C, T)`, set `series_ids` shape).
- `bar_freq` optionally resamples ticks into bars before inference (matches the
  notebook convention of context=3008 minute-bars); `None` feeds raw history.
- `signal_step` (`last`/`first`/`mean`) chooses which horizon step becomes
  `predicted_price`.
- Checkpoints: `Datadog/Toto-2.0-{4m, 22m, 313m, 1B, 2.5B}`.

## Related
[forecaster/base.py](base.md) · [naive.py](naive.md) · [compare_models.py](../compare_models.md) · [SPEC §6 Phase 2](../../SPEC.md)
