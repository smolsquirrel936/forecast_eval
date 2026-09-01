# [tools/session_artifacts.py](../../tools/session_artifacts.py)

**Role:** rebuilds a finished live run's artifacts — and its report — from the `session.jsonl` it already wrote.
**Pipeline:** `session.jsonl` → the event lists a `LiveLoop` held → `logging_io.write_all` → `reports.generate_report`.
**Depends on:** [events.py](../events.md), [logging_io.py](../logging_io.md), [reports.py](../reports.md)   **Used by:** run by hand, on historical run directories

```powershell
uv run python -m forecast_eval.tools.session_artifacts outputs\live_20260824_150112
```

## Why this exists and where it must not be used

[live/artifacts.py](../live/artifacts.md) gives a **running** deployment the
backtest's artifact set by handing `LiveLoop.result()` to `write_all` — no
projection, one writer, so the schemas cannot drift. That is the right answer,
and it is not available for a run that has already ended: the loop object is
gone and the recorder's event stream is all that survives.

So this is the projection, deliberately, and **only for historical directories**.
Wiring it into the live path would reintroduce exactly the drift the one-writer
rule exists to prevent.

What keeps it honest is that the rebuilt lists go through the real `FillEvent` /
`OrderEvent` / `Forecast` types rather than straight into a frame. If the events
change, this stops working loudly instead of quietly writing the old shape.

## The clock, which is the part that can be silently wrong

**MEASURED on `live_20260824_150112`.** One file carries two timestamp shapes:

| record | timestamp | example |
|---|---|---|
| `bar`, `order`, `signal`, `forecast` | naive **local** ISO | `"2026-08-24T20:01:00"` |
| `fill` | float **epoch**, UTC | `1787572931.325908` |

The same instant, eight hours apart. Converting the epoch without the offset puts
every fill *before* the order that produced it, mislabels its session through
`classify_session`, and draws it off the left edge of the price chart — while the
artifacts still write and the report still renders. Nothing else looks wrong,
which is what makes it worth a named constant and a check.

`VENUE_UTC_OFFSET` is `timedelta(hours=8)`: TAIFEX is Asia/Taipei, UTC+8 all year
with no DST, so it is a constant rather than a lookup. `check_alignment` then
verifies it against the run's own bars and complains if any fill falls outside
them — the one symptom a wrong offset produces.

## Two more measured details

**`ticks.csv` mixes both ISO shapes.** A print landing exactly on a second has no
fractional part (`"2026-08-24T15:11:23"` at row 320, among 287 726 that do), so
`pd.to_datetime` must be given `format="ISO8601"`; inference locks onto the first
row and then raises on the first row that differs, which reads as a corrupt
capture rather than an ordinary one. Simtrade prints are dropped (§2.5) so the
price line is the series the run actually saw.

**`bars_held` is 0 on every rebuilt trade.** `build_trades` computes it from
`fill_bar_idx`, which `write_all` does not pass — so this is true of a *live*
run's own `trades.csv` too, not an artifact of the reconstruction. The `fill`
records do carry `bar_idx`, so it is recoverable if it is ever wanted.

## The overwrite guard

A run that wrote its own artifacts from the loop holds the **primary** record;
this produces a reconstruction. Replacing the first with the second is a
downgrade, so the tool refuses when `fills.csv`, `trades.csv` or `signals.csv`
already exist and asks for `--force` rather than deciding.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `read_records(path)` | function | every JSON object, skipping a half-written final line |
| `rebuild(records)` | function | the event lists a `LiveLoop` held, as a `BacktestResult` |
| `check_alignment(result, records)` | function | complains when a fill lands outside the run's bars |
| `load_price_series(run_dir)` | function | `ticks.csv` as the report's price frame |
| `main(argv)` | function | CLI: rebuild, verify, write, render |
| `VENUE_UTC_OFFSET` | constant | `timedelta(hours=8)` — Asia/Taipei, no DST |

## Verified on

`outputs/live_20260824_150112` — 5 026 records → 4 455 bars, 149 forecasts, 149
signals, 3 orders, 3 fills, and one closed round trip: SHORT 44893 → 44520,
**+373.0 gross, +359.59 net of 13.41 in fees**. The third fill is the BUY at
44555 that was still open when the run ended — the same lot the current soak
adopts on `--resume`, which is what ties the reconstruction to the live account.
