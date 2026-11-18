# timestral.py — Timestral-209M forecaster (fullday's own copy)

`strategy/fullday/timestral.py` adapts Mistral's **Timestral-209M** as this
package's alternative `Forecaster`, built on branch `forecaster_eval_1116`
to evaluate replacing the incumbent Toto-2.0-1B. The `model_eval` bake-off
picked it as the strongest candidate (best pinball in every grid cell, the
only majority-month MASE<1 family, ~2.4× faster than toto2-1B).

## Contract

Reproduces the incumbent's interface exactly so the copied emitter/trader
are untouched:

- **`forecast(history_df) -> Forecast`** — the per-tick path; trailing
  `context_length` close prices, one model call, payload built from the
  9-decile fan.
- **`forecast_series_batch(...)`** — the GPU-batched path `--precompute`
  and `--forecast-cache` use; fixed chunks of 512 windows (no VRAM probe
  needed: 209M at context 2048 measured ~30 s/100k windows on the 5090).
- **Payload keys identical to Toto2's** (`predicted_return`, `prob_up`,
  `median_path`, `last_price`, the quantile fan) — the emitter's band,
  conviction gate and path-momentum veto are forecaster-agnostic.

## Quantile ladder

Timestral emits 21 levels `[0.01, 0.05, 0.1, …, 0.99]`; the nine deciles
sit at indices `[2, 4, …, 18]` (verified against the package's live
`forecast.quantile_levels`, 2026-11-16). `prob_up` uses the same
decile-CDF interpolation as `forecaster/toto2.py`, so conviction means the
same thing under both forecasters.

## Selection

Declared as the package knob `forecaster`. On simtrade_0929 `wiring.py`
PARAMS set it to `timestral` since 2026-11-19 (the iter14 port), so a bare
`--strategy fullday` already runs it; on the loop branch the default was
`toto2` and the run below named it:

```powershell
uv run --no-sync python -m forecast_eval.real_data --strategy fullday `
  --data \\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_fullday.csv `
  --param forecaster=timestral `
  --param checkpoint=mistralai/Timestral-209M `
  --context-length 2048 --horizon-bars 5 --stride-bars 5 `
  --warmup-bars 3008 --precompute --output-dir outputs\<run>
```

## Install

`timestral` is not on PyPI. Install it from GitHub at the pinned commit, after
torch (`UV_SETUP.md` Step 3b, repo root):

```powershell
uv pip install "timestral @ git+https://github.com/mistralai/timestral.git@a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff"
```

The forecast cache's runtime key (`forecast_cache.py`) does not record
`timestral`'s version, so a cache is only safe to share between machines on
the same commit.

TF32 is disabled at model load so the batched precompute stays
bit-identical to sequential forecasts (same rationale as the incumbent's
wrapper). Lazy torch/timestral imports keep the package importable
without a GPU.

See `wiring.md` for the `trend_filter` knob — timestral's gated 5-minute
stream is a mean-reversion fade, which the default momentum-confirmation
trend filter vetoes; `--param trend_filter=false` disables the veto.
