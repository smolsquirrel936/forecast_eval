# [strategy/hybrid/forecaster.py](../../../strategy/hybrid/forecaster.py)

**Role:** HYBRID's forecaster router — the one Forecaster the engine and the forecast cache see.
**Pipeline:** wraps the two halves' forecasters; routes each boundary to the half of its session.
**Depends on:** [forecaster/base.py](../../forecaster/base.md), [router.py](router.md) (`session_of`)   **Used by:** [strategy/hybrid/wiring.py](wiring.md)

## Responsibility
Each half builds its own forecaster ([day_half](day_half/forecaster.md),
[night_half](night_half/forecaster.md)). `RoutedForecaster` sends `forecast()` by
the session of the last history row, and splits `forecast_series_batch`'s
boundaries by session, batching each half once and returning the forecasts in
the original order.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `RoutedForecaster(day, night, *, warmup_bars, stride_bars, horizon_bars)` | class | the router; `shared` is True when the halves are configured identically |
| `make_forecaster(*, day, night, warmup_bars, stride_bars, horizon_bars)` | function | this file's construction site |

## Key points / gotchas
- **Identical halves share one instance**: both sessions go to the day half's
  forecaster, which is one model in VRAM and **one** batched call over every
  boundary — the same call a plain Toto2 run makes, so the batches (and last
  bits) match a single-strategy run. The comparison covers the class, `checkpoint`,
  `context_length`, `bar_freq`, `signal_step` and `device`.
- **Halves on one checkpoint share the loaded model** even when they differ
  otherwise (e.g. context length): the second half borrows Toto2's lazily loaded
  `_model` / `_torch` / `_patch_size`. Safe because `forecaster/toto2.py` is
  hash-pinned; a copied class without those attributes is simply not shared.
- **Stride and horizon are shared**: one engine stride, and `forecast_cache.py`
  decodes every cached forecast with one `forecast_horizon_bars`. A half with
  its own horizon is refused at construction.
- **Cache key.** `checkpoint`, `context_length`, `bar_freq` and `signal_step` are
  exposed as `"day=…;night=…"` strings — the attributes `forecast_cache.cache_key`
  reads — so a change to **either** half moves the key.
- One of the three files `forecaster_sha("hybrid")` hashes
  ([strategy/wiring.py](../wiring.md) `FORECASTER_FILES`).

## Related
[wiring.py](wiring.md) · [router.py](router.md) · [forecaster/toto2.py](../../forecaster/toto2.md) · [run.py](../../run.md) (`precompute_forecasts`)
