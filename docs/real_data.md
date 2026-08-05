# [real_data.py](../real_data.py)

**Role:** Full-dataset backtest variant with a time-based warmup/eval split.
**Entry point:** `python -m forecast_eval.real_data`   ·   needs Toto2
**Depends on:** [forecaster/toto2.py](forecaster/toto2.md), [run.py](run.md), [logging_io.py](logging_io.md), [metrics.py](metrics.md), [strategy/threshold.py](strategy/threshold.md)

## Responsibility
The full real-data backtest: runs Toto2 + ThresholdEmitter over the whole deduped
1-min file, splitting it so the first `lookback_frac` (30%) is silent warmup (no
trading) and the trailing 70% is the backtest window. Writes the artifact bundle
and prints the metric pack plus a buy-and-hold floor.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `DEFAULT_DATA` | const | path to the 1-min CSV |
| `load_minute_bars(path, n_bars)` | function | drop NaN closes; `n_bars=None` → full dataset |
| `main(argv)` | function | parse CLI, split, run (with progress bar), log, print metrics |

## Key points
- `--n-bars` defaults to **None** (use everything); `--lookback-frac` (0.30) sets
  the silent-warmup fraction.
- `warmup_bars` is the *data-driven split*, so trading is suppressed across the whole
  lookback region — but `context_length` (3008) still caps each forecast's window
  (Toto2 truncates anything longer, so a bigger context wouldn't help).
- Buy-and-hold is computed over the **eval window only**, so the comparison is fair
  (the strategy never traded the warmup region).
- Runs with a tqdm **progress bar** (the full file is millions of bars).
- `--precompute` batches every forecast on the GPU up front, then replays execution.

## Key gotchas
- Refuses to run if `warmup < context_length` (the model could never fire).
- `--lookback-frac` must be strictly in (0, 1).

## Related
[run.py](run.md) · [forecaster/toto2.py](forecaster/toto2.md) · [SPEC §4.8](../SPEC.md)
