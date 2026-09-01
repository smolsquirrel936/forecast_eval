# [live/artifacts.py](../../live/artifacts.py)

**Role:** gives a live run the same directory a backtest produces — `fills`, `orders`, `forecasts`, `signals`, `trades`, `params.json`, and a rendered report.
**Pipeline:** `LiveLoop.result()` → `logging_io.write_all` → the same CSVs `real_data.py` writes → `reports.generate_report`.
**Depends on:** [logging_io.py](../logging_io.md), [live/loop.py](loop.md), [live/config.py](config.md)   **Used by:** [live/cli.py](cli.md)

## What this is not

It is **not a projection from `session.jsonl`**. `LiveLoop.result()` already
returns a `BacktestResult` — built for this, and its own comment says so:

> every metric, report and log writer already consumes `BacktestResult`; reusing
> it means a live session is analysable with the exact same tooling

So this module hands that object to `write_all`, the function the backtest calls,
with the arguments the backtest passes. **The schemas cannot drift, because there
is only one writer.** A hand-written projection would have been a second schema to
keep in sync, and `tests/test_live_artifacts.py` pins the columns against a real
backtest run precisely so nobody introduces one later.

What the module actually adds is **when** a soak writes.

## Before and after

| | live run, before | live run, now | backtest |
|---|---|---|---|
| `session.jsonl` | ✅ | ✅ | — |
| `ticks.csv` | ✅ | ✅ | — |
| `fills` / `orders` / `forecasts` / `signals` / `trades` | — | ✅ | ✅ |
| `params.json` | — | ✅ | ✅ |
| `report.html` | — | ✅ (at shutdown) | ✅ |

`reports.generate_report` only ever wanted three CSVs in a directory — fills,
trades and signals. It never knew what produced them, and a soak can now supply
them, so the same report renders over either kind of run.

## The three gates

A soak runs for weeks and the CLI calls `write()` on its **once-a-second clock
tick**, so writing unconditionally would be the run's dominant I/O — and for the
ten of every twenty-four hours the market is shut, every write would be
byte-identical to the last.

| gate | what it does | why |
|---|---|---|
| throttle | at most one write per `MIN_INTERVAL_SECONDS` (60) | the stride is 30 bars — one forecast every 30 min — so a minute's resolution is already far finer than the data changes |
| change | skip when no list has grown | the gate is on **content**, not the clock, so the 05:00–15:00 dead zone costs nothing and a busy session is never skipped |
| `force` | ignores both | shutdown must land the last bars even if the throttle just fired |

`report=True` is passed **only** at shutdown. Rendering imports matplotlib and
redraws every chart: acceptable once on a process that is already stopping,
wasteful every minute of a three-month soak.

## Failures are counted, never raised

These files are a *record* of a live run and never an input to one, so a full
disk or a locked file must not be able to stop a process that is holding a
position. `write()` swallows, counts, and returns `None`; the count reaches the
console and a `artifact_summary` record at shutdown. It is the same reasoning
`write_all` already applies one level up, where a failing chart backend degrades
to a printed warning rather than invalidating logs that wrote successfully.

## `.csv`, not `.parquet`

`write_all` defaults to `.parquet` with a CSV fallback, so the format would
depend on whether a parquet engine happened to be installed. `real_data.py` —
the CLI every backtest in `results.tsv` came from — pins `.csv`, and a soak's
directory is meant to be diffable against those without a format conversion
first. `ARTIFACT_EXT` is the one place that is decided.

## `params.json`

Two halves. The strategy fields are written under the **same keys** a backtest
uses (`checkpoint`, `context_length`, `horizon_bars`, `stride_bars`,
`buy_threshold`, `sell_threshold`, `aggression_ticks`, `min_prob`,
`max_position`, `forced_close`), so the two files diff directly when a live run
disagrees with the backtest it came from. The rest is context a backtest has no
concept of: `broker`, `warmup_days`, `sessions`, `roll_at`, `soak_id`, and the
pin's own `source_commit` / `source_calmar` / `source_dataset`.

It is written at **start-up**, alone, so a run that dies in its first minute
still says what it was trying to be.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ArtifactWriter(out_dir, *, min_interval_seconds, ext)` | class | keeps a run directory current, in the backtest's schema |
| `ArtifactWriter.write(runtime, *, report, force)` | method | write if something changed; returns paths or `None` |
| `ArtifactWriter.write_params(params)` | method | `params.json`, at start-up |
| `ArtifactWriter.summary()` | method | `artifact_writes` / `artifact_failures` |
| `live_params(config, …)` | function | the `params.json` payload for a live run |
| `MIN_INTERVAL_SECONDS`, `ARTIFACT_EXT` | constants | 60 s · `.csv` |

`write()` is duck-typed on `.loop` (and an optional `.roll_fills`) rather than
typed against `LiveRuntime`, which is what lets the tests drive it from a bare
`LiveLoop` with no model, no broker and no GPU.

## `roll_fills`

Passed through to `build_trades`, exactly as `real_data.py` passes it. Without
it a position carried across a contract roll is booked as **two** round trips
live and **one** in the backtest — a disagreement with nothing to do with
trading. A test pins it.
