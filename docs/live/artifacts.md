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

## The report gets the run's own price capture

MEASURED 2026-09-02, on a real run: `write_all` was called with no
`report_ticks`, so the report's **"Price with fills"** panel drew the fills
against an empty axis. The panel still rendered, which is why it took a reader to
notice rather than a failure. The capture is sitting in the same directory, so
`price_series(out_dir / "ticks.csv")` now supplies it — and **only** when
`report=True`, keeping a capture that reaches tens of MB off the once-a-minute
path.

`python -m forecast_eval.reports <run_dir>` falls back to the same capture, so
any live directory can be re-rendered without re-running anything.

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
| `live_params(config, *, strategy_params, …)` | function | the `params.json` payload for a live run; strategy keys come from the run's **resolved** knobs, not from `LiveConfig` (D2) |
| `strategy_sha(package)` | function | 12-hex sha256 over `strategy/<package>/*.py`, **newlines normalised** |
| `git_commit()` | function | `git rev-parse --short=12 HEAD`, or `None` when unreadable |
| `git_dirty()` | function | whether the tree has uncommitted changes — without it the commit misdescribes the run |
| `MIN_INTERVAL_SECONDS`, `ARTIFACT_EXT` | constants | 60 s · `.csv` |

`write()` is duck-typed on `.loop` (and an optional `.roll_fills`) rather than
typed against `LiveRuntime`, which is what lets the tests drive it from a bare
`LiveLoop` with no model, no broker and no GPU.

## `roll_fills`

Passed through to `build_trades`, exactly as `real_data.py` passes it. Without
it a position carried across a contract roll is booked as **two** round trips
live and **one** in the backtest — a disagreement with nothing to do with
trading. A test pins it.


## Provenance is the record that replaced the gate (Phase 5)

`live/cli.py` no longer refuses an unprovenanced deployment (D1), so `params.json`
and the recorder's `meta` header carry five fields that say what actually ran:

| key | what it answers |
|---|---|
| `strategy_package` | which `strategy/<session>/` built the components |
| `strategy_sha` | **which content** — catches an uncommitted edit, which a commit hash cannot |
| `git_commit` | the repo state, `None` if there is no `.git` |
| `git_dirty` | **whether that commit can be trusted.** The first live run to carry these fields reported `af4be0b` while every live module it executed was modified and unstaged |
| `strategy_origins` | per knob: the package name, or `"param"` if typed |
| `strategy_overridden` | one boolean: **is this row comparable to a `results.tsv` row?** |

`strategy_sha` normalises newlines on purpose. `tests/test_strategy_isolation.py`
pins raw bytes and fails on a Windows checkout purely because `autocrlf=true`
rewrites line endings — a provenance hash that moves with the checkout records
the checkout, not the strategy.

These live as their own `meta` fields rather than inside `LiveConfig.describe()`.
Packing them into that line would have meant extending
`tools/session_artifacts.py:_DESCRIBE_RE`, which parses it back out of every
session file already on disk. The rebuild reads the new fields when present and
yields `None`/`{}` for sessions recorded before they existed.
