# [reports.py](../reports.py)

**Role:** Performance-report charts — PNG + interactive HTML (SPEC §4.9).
**Pipeline:** artifact bundle on disk → **reports** → `report/*.png` + `report.html`.
**Depends on:** [metrics.py](metrics.md), `matplotlib` (required), `plotly` (optional)
**Used by:** [logging_io.write_all](logging_io.md) (auto), and as a standalone CLI

## Responsibility
Reads the parquet/CSV bundle from a run directory and renders three chart groups.
Runs automatically after every backtest, and is also a standalone CLI for
re-rendering from existing logs.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `generate_report(run_dir, data_path=None, …)` | function | render PNGs + HTML; returns paths written |
| `plot_equity_drawdown_*` | functions | equity curve + underwater drawdown (mpl + plotly) |
| `plot_price_fills_*` | functions | price series with BUY/SELL markers |
| `plot_signal_vs_realized_*` | functions | frictionless signal PnL vs realized (the drag) |
| `main(argv)` | function | CLI entry: `python -m forecast_eval.reports <run_dir>` |

## The three charts
- **equity_drawdown** — cumulative net PnL with an underwater drawdown panel.
- **price_fills** — price (real ticks if `--data` given, else reconstructed from
  fills) with green ▲ BUY / red ▼ SELL markers.
- **signal_vs_realized** — the frictionless signal curve overlaid on realized PnL;
  the shaded gap is the execution + cost drag.

## Key points / gotchas
- **PNGs always work** (matplotlib is a hard dep); the combined `report.html` is
  produced only when `plotly` is installed (`_HAS_PLOTLY`).
- An optional `--data` ticks CSV provides the price-chart background; large files
  are clipped to the fills' time window so the chart stays light.
- `generate_report` raises a precise `FileNotFoundError` listing any missing
  artifact — it expects a bundle written by [logging_io.write_all](logging_io.md).

## Related
[logging_io.py](logging_io.md) · [metrics.py](metrics.md) · [usage.md §7](../usage.md) · [SPEC §4.9](../SPEC.md)
