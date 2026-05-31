# [compare_models.py](../compare_models.py)

**Role:** Model-size sweep — forecast quality **and** a full backtest per checkpoint (SPEC §6 Phase 5).
**Entry point:** `python -m forecast_eval.compare_models`   ·   needs Toto2
**Depends on:** [forecaster/toto2.py](forecaster/toto2.md), [run.py](run.md), [logging_io.py](logging_io.md), [metrics.py](metrics.md), [real_data_demo.py](real_data_demo.md) (loader)

## Responsibility
Runs the same data through every Toto-2.0 checkpoint (4m → 2.5B) in **two passes**
each, so trading metrics land on the same axis as forecast quality — answering
whether bigger models convert better IC into better realized PnL.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `DEFAULT_CHECKPOINTS` | const | the 5 checkpoints, small → large |
| `evaluate_checkpoint(...)` | function | one checkpoint: load, eval windows, optional backtest, teardown |
| `_per_window_eval(...)` | function | Phase A: predicted vs realized return + latency per window |
| `_run_backtest_for_checkpoint(...)` | function | Phase B: full harness backtest + parquet bundle |
| `summarize(...)` | function | one summary row per checkpoint (quality + bt_* metrics) |
| `main(argv)` | function | CLI driver; writes `per_window.csv`, `summary.csv`, `params.json` |

## The two passes (per checkpoint)
1. **Forecast-quality eval** — N evenly-spaced windows → `per_window.csv` (hit rate,
   Spearman IC, latency). Cheap, no trading.
2. **Full backtest** — `run_backtest` + `ThresholdEmitter` → standard parquet bundle
   under `<ckpt>/`, with trading metrics added to `summary.csv` as `bt_*` columns.
   Skipped under `--skip-backtest`.

## Key points / gotchas
- **One model load per checkpoint:** the same `Toto2Forecaster` serves both passes;
  a *fresh* instance is built per checkpoint, then `del`'d + `torch.cuda.empty_cache()`
  to release VRAM before the next (so all 5 run sequentially without OOM).
- Per-checkpoint failures are **caught** (logged to `params.json`), so one bad/OOM
  checkpoint doesn't lose the others.
- Backtest knobs mirror [real_data_demo.py](real_data_demo.md); output layout is in
  [usage.md §8](../usage.md).

## Related
[real_data_demo.py](real_data_demo.md) · [forecaster/toto2.py](forecaster/toto2.md) · [metrics.py](metrics.md) · [usage.md §8](../usage.md) · [SPEC §6 Phase 5](../SPEC.md)
