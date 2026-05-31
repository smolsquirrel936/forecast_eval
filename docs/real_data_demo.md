# [real_data_demo.py](../real_data_demo.py)

**Role:** The standard real-data backtest — Toto2 on real TXF 1-min bars.
**Entry point:** `python -m forecast_eval.real_data_demo`   ·   needs Toto2
**Depends on:** [forecaster/toto2.py](forecaster/toto2.md), [run.py](run.md), [logging_io.py](logging_io.md), [metrics.py](metrics.md), [strategy/threshold.py](strategy/threshold.md)

## Responsibility
Wires Toto2 + ThresholdEmitter into [run_backtest](run.md) over a trailing window
of the 1-min file, writes the artifact bundle, and prints the metric pack plus a
buy-and-hold floor. The default config matches the toto2 notebooks (context 3008,
horizon 30).

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `DEFAULT_DATA` | const | path to `dataset/tick/TXF_OHLC_1min.csv` |
| `load_minute_bars(path, n_bars)` | function | last `n_bars` non-NaN rows → tick schema |
| `main(argv)` | function | parse CLI, run, log, print metrics |

## Key CLI flags
`--n-bars` (6000) · `--checkpoint` (313m) · `--context-length` (3008) ·
`--horizon-bars` (30) · `--stride-bars` (30) · `--buy-threshold` / `--sell-threshold`
· `--fee-rate` · `--forced-close` · `--output-dir`. Full table in
[usage.md §5](../usage.md).

## Key points / gotchas
- **NaN drop is essential** — the raw 1-min file is ~17% NaN closes (session
  gaps/halts) and Toto2 propagates NaN; `load_minute_bars` drops them, so a
  "30-bar horizon" means 30 *traded* minutes.
- `volume` is a constant 1 (the 1-min OHLC file has no usable per-bar volume here).
- Output goes to `outputs/real_data_demo_<ts>/` with a price-chart background from
  `--data`.
- Prints strategy net vs. buy-and-hold over the **same** window/fee — the floor the
  model must beat.

## Related
[real_data.py](real_data.md) · [compare_models.py](compare_models.md) · [forecaster/toto2.py](forecaster/toto2.md) · [usage.md §5](../usage.md)
