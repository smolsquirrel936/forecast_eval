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

### Every cumulative curve opens at zero

MEASURED 2026-09-02. All three cumulative series — equity, drawdown, realized PnL
and the frictionless signal curve — emitted **one row per close**, so a run with a
single closed round trip produced a one-point line, and a `mode="lines"` trace
through one point draws *nothing*. The Cumulative PnL panel rendered empty on a
run that had made +359.59 points.

`_with_origin` prepends a zero row at the moment the curve was last flat: the
first trade's `entry_ts` for the equity and realized curves, the first non-HOLD
signal for the frictionless one. This is the missing datum rather than a display
trick — a curve that opens at +359.59 hides the step it exists to show, and the
two lines in the attribution panel have to start at the same height or the gap
between them, which *is* the panel, sits at the wrong height for the first trade.
It also gives `_buy_and_hold_curve` a window with width; a single-trade run
previously collapsed it to one print, so the reference line vanished.

A bundle whose `trades` frame predates `entry_ts` simply gets no origin.

## Key points / gotchas
- **PNGs always work** (matplotlib is a hard dep); the combined `report.html` is
  produced only when `plotly` is installed (`_HAS_PLOTLY`).
- An optional `--data` ticks CSV provides the price-chart background; large files
  are clipped to the fills' time window so the chart stays light.
- `generate_report` raises a precise `FileNotFoundError` listing any missing
  artifact — it expects a bundle written by [logging_io.write_all](logging_io.md).

## The train/test boundary
When the run held a tail back, all three figures — both panels of
equity/drawdown, price-with-fills, and signal-vs-realized — carry a dashed
orange rule at `train_split_ts` captioned `train ends`. All three share a
timestamp x-axis, so the boundary is the same instant on each.

- It is read from the run's own `params.json` (`_active_split_ts`), so a
  standalone `python -m forecast_eval.reports <dir>` re-render marks it too, and
  a bundle written before the feature simply has no key and renders as it always
  did.
- **It is drawn only when a tail was actually held back.** A default run records
  a `train_split_ts` as well — the boundary is then just the last bar — and
  drawing that would pin a meaningless rule to the right edge of every chart.
  `train_split_bars >= eval_bars` suppresses it.
- It never joins a legend: the equity panel already carries strategy plus
  buy-and-hold, and the price panel price plus BUY plus SELL. The caption says
  what it is without competing for that space.

`train_frac` renders in Settings as an **unsigned** percentage with trailing
zeros trimmed (`80%`, `75.5%`) — it is a share of a window, so the leading `+`
every other float carries reads as noise. On a `--train-bars` run the stored
value is `None` (the file records which flag was used, and that one was not), and
the table derives the share from `train_split_bars / eval_bars` instead.

## Related
[logging_io.py](logging_io.md) · [metrics.py](metrics.md) · [usage.md §7](../usage.md) · [SPEC §4.9](../SPEC.md)
