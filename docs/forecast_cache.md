# [forecast_cache.py](../forecast_cache.py)

**Role:** an on-disk store of forecasts, **one per bar**, shared by every backtest run with the same forecaster, tape and runtime.
**Pipeline:** `real_data --forecast-cache <dir>` → `load_or_compute` → cached bars + one GPU pass for the missing ones → `run_backtest(forecasts_cache=…)`.
**Depends on:** [environment.py](environment.md), [events.py](events.md)   **Used by:** [real_data.py](real_data.md)

```powershell
uv run --no-sync python -m forecast_eval.real_data --strategy fullday `
  --data \\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_fullday.csv `
  --warmup-bars 5000 --train-bars 1651591 --precompute `
  --forecast-cache \\DESKTOP-SQUIRRE\forecast_eval_folder\night\forecast_cache\<loop> `
  --output-dir outputs\<run>
```

## Responsibility

A forecast at bar `b` depends on the history through `b` and on the forecaster's
configuration, never on stride, warmup or any emitter/trader/exit knob. A search
that only moves those knobs asks the GPU the same questions every run. The cache
answers each question once:

- **An emitter-, trader- or stop-only change computes nothing.**
- **A coarser stride is a subset of a finer one** at the same warmup (stride 15 ⊂ 5),
  so it computes nothing either.
- **A finer stride computes only the bars no earlier run asked for.**

Measured 2026-10-26 on a 30 000-bar fullday slice (1 667 forecasts, RTX 5090):
39.0 s fresh, 20.6 s cold-cache, **1.3 s warm**. The fresh, cold and warm runs
wrote byte-identical forecasts, signals, orders, fills and trades. On disk it
takes ~0.5 kB per forecast, so a stride-5 fullday stream is ~240 MB.

## The key

`meta.json` in each `<dir>/<key[:16]>/` folder holds every field the key hashes:

| field | why |
|---|---|
| forecaster class, `checkpoint`, `context_length`, `horizon_bars`, `bar_freq`, `signal_step` | what a forecast is |
| `forecaster_sha` | a session package's `forecaster.py` (STRATEGY_SPLIT_PLAN §2.4) |
| `tape` | sha256 of the in-session timestamps and prices the loop sees. Content, not path: the host reads `D:\…`, the VM a UNC path |
| `runtime`: GPU name, torch, CUDA, toto-2 versions | **two venvs on the same card can disagree in the last bits.** A host run and a VM run share entries only when all four match |

Stride and warmup are **not** in the key. They choose which bars are forecast,
not what a bar's forecast is.

## The rule that matters for a search

**A forecast's last bits depend on the GPU batch it was computed in.** Serving
stride-15 bars from a cache that stride 5 filled gave the same trades on the
test slice, but 8 of 1 667 forecasts differed from a fresh stride-15 run, by at
most 9e-8 in `predicted_return`. So:

- **Entries are never overwritten,** and when two writers race the first copy of
  a bar wins. A run scored on the cache stays reproducible on that cache.
- **Score every run in a loop on the same cache folder.** A cached run is
  comparable with other runs on that cache. It is only *approximately* comparable
  with a fresh run, or with a run on another cache that was filled in a
  different order.
- **The cache folder is loop state.** Do not delete or prune it mid-loop. Give each
  loop its own folder.

## Public surface

| Symbol | Kind | One-liner |
|---|---|---|
| `load_or_compute(forecaster, ticks, cache_dir, *, forecaster_sha, batch_size, progress, runtime)` | function | `({bar_idx: Forecast}, stats)` — what `precompute_forecasts` returns, from the cache plus the GPU |
| `cache_key(forecaster, *, forecaster_sha, tape, runtime)` | function | `(hex, fields)` |
| `tape_digest(timestamps, prices)` | function | sha256 of the in-session stream |
| `runtime_identity()` | function | GPU + torch/CUDA/toto-2 versions (imports torch) |
| `boundaries_for(n, warmup, stride)` | function | the run loop's forecast bars |

## Storage

Shards are `part-<ns>-<pid>.npz`: `bar_idx` plus one float64 column per scalar
payload field, and one float64 block per list field. They are written to a temp
name and then renamed, so a reader never sees half a shard, and the host and
the VM can append to the same folder at once. Arrays, not pickle: loading a
shared file must not run code. A payload field that is not a number or a list
of numbers raises `TypeError`. Decoding returns Python floats in the original
field order, so a decoded payload compares equal to the computed one.

## Key gotchas

- A `meta.json` whose full key does not match its folder raises `RuntimeError`.
- Changing the tape, `forecaster.py`, the checkpoint, the horizon or the venv
  starts a new folder and computes from scratch. That is intended.
- The cache does not know about the held-out split. It stores the whole tape's
  forecasts, as a fresh run computes them.
