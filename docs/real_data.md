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
| `load_minute_bars(path, n_bars)` | function | drop NaN closes; `n_bars=None` → full dataset |
| `main(argv)` | function | parse CLI, split, run (with progress bar), log, print metrics |

## Key points
- **`--data` is required** — there is no default path. The old default named the
  DAY file while every tuning run used NIGHT, and a session picked by a default
  is one nothing can check (SHIOAJI_PLAN §2.12, the same reason `fetch_bars`
  takes a required policy).
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

## The contract collapse is a no-op — measured
`load_minute_bars` keeps the highest-volume row per minute when a `contract`
column is present, on the assumption that several months print the same minute.
**They do not.** All three RPT files carry exactly one contract per minute (2026-08-23):

| file | rows | minutes with >1 contract |
|---|---|---|
| `TXF_RPT_minute_night.csv` | 1 733 348 | **0** |
| `TXF_RPT_minute_day.csv` | 1 124 790 | **0** |
| `TXF_RPT_minute.csv` | 2 858 138 | **0** |

So the files arrive already front-month-continuous, spliced by **calendar** — 106
of 108 seams land exactly one day after the expiring month's last trading day.
The rule stays as a guard against a future file that does mix months; it is not
the convention the model was evaluated on, and [live/roll.py](live/roll.md)
therefore splices live bars on the calendar rather than on volume
(SHIOAJI_PLAN §2.6).

The seam is worth knowing when reading model behaviour around a roll: its median
jump is **0.70 %** (95 points) against a median ordinary minute of 0.0095 %, and
95 % of seams exceed the strategy's ±0.00045 band. That is basis *plus* a whole
day session's move, because the last trading day's 15:00–23:59 block is absent
from the file.

## Related
[run.py](run.md) · [forecaster/toto2.py](forecaster/toto2.md) · [SPEC §4.8](../SPEC.md)
