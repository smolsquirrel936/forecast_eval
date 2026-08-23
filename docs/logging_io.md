# [logging_io.py](../logging_io.py)

**Role:** Persists the backtest artifact bundle and `params.json` (SPEC §6 Phase 4, §8).
**Pipeline:** `BacktestResult` → **logging_io** → disk (→ [reports.py](reports.md)).
**Depends on:** [events.py](events.md), [metrics.py](metrics.md), [environment.py](environment.md)
**Used by:** [run.py](run.md), all CLI entry points

## Responsibility
Writes the five-file artifact bundle (parquet, with transparent CSV fallback), the
`params.json` reproducibility record, and — by default — triggers chart generation.


## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `DEFAULT_OUTPUT_ROOT` | const | `<repo-root>/outputs/`, anchored to the source tree |
| `timestamped_run_dir(name, root=None)` | function | `<root>/<name>_<YYYYMMDD_HHMMSS>/` |
| `write_params(params, path)` | function | pretty JSON dump (str() fallback) |
| `write_fills` / `write_orders` / `write_forecasts` / `write_signals` / `write_trades` | functions | per-artifact writers |
| `write_all(result, output_dir, *, roll_fills=None, …)` | function | dump the whole bundle + optional auto-report |

## The bundle
`fills` · `orders` · `forecasts` · `signals` · `trades` (+ `params.json`). See
[usage.md §6](../usage.md) for the on-disk layout.

## Rolls
`write_all(..., roll_fills=…)` forwards the timestamps of contract-roll legs to
[build_trades](metrics.md), so a position held across a roll is written to
`trades` as **one** round-trip rather than two (§2.6). Omit it and nothing
changes — the parameter is opt-in for exactly the reason the rest of this module
is conservative: every existing caller must keep producing the same artifact.

## Key points / gotchas
- **Parquet → CSV fallback:** `_write` tries `to_parquet`; on any failure (no
  pyarrow/fastparquet) it writes a sibling `.csv` instead. The returned path tells
  you which format actually landed.
- `DEFAULT_OUTPUT_ROOT` is resolved from this file's location, **not the CWD**, so
  artifacts always go to the same place regardless of where you launch from.
- `write_all` runs [reports.generate_report](reports.md) when `auto_report=True`
  (default, SPEC §4.9); a chart failure is caught and logged so it never
  invalidates already-written data. Pass `auto_report=False` in headless sweeps.
- Dataclass artifacts are flattened with `asdict`, so the schema tracks the event
  definitions automatically.

## Related
[metrics.py](metrics.md) · [reports.py](reports.md) · [run.py](run.md) · [SPEC §8](../SPEC.md)
