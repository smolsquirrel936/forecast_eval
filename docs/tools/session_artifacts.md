# [tools/session_artifacts.py](../../tools/session_artifacts.py)

**Role:** rebuilds a finished live run's artifacts — and its report — from the `session.jsonl` it already wrote.
**Pipeline:** `session.jsonl` → the event lists a `LiveLoop` held → `logging_io.write_all` → `reports.generate_report`.
**Depends on:** [events.py](../events.md), [logging_io.py](../logging_io.md), [reports.py](../reports.md)   **Used by:** run by hand, on historical run directories

```powershell
uv run --no-sync python -m forecast_eval.tools.session_artifacts outputs\live_20260824_150112
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

**The price series comes from [live/capture.py](../live/capture.md).** This
module briefly carried its own reader and rediscovered, as a fresh finding, that
`ticks.csv` mixes both ISO-8601 shapes — a print landing exactly on a second has
no fractional part. `capture.read_ticks` had already solved and documented that
(§2.17). It now delegates to `capture.price_series`: one reader for one format,
because a second copy is how the two drift. Simtrade prints are dropped there
(§2.5), so the price line is the series the run actually saw.

**`bars_held` is 0 on every rebuilt trade.** `build_trades` computes it from
`fill_bar_idx`, which `write_all` does not pass — so this is true of a *live*
run's own `trades.csv` too, not an artifact of the reconstruction. The `fill`
records do carry `bar_idx`, so it is recoverable if it is ever wanted.

## `params.json`, which is the Settings block on the report

A live run writes `params.json` at start-up, so `generate_report` opens its
report with a **Settings** table. A historical directory has none, and its
rebuilt report opened straight into the charts — the one structural difference
between a rebuilt report and a live one (measured 2026-09-02, comparing
`live_20260824_150112` against `live_night_20260902_024124`).

`params_from_meta` closes it by parsing the recorder's `meta` header, whose
`config` field is `LiveConfig.describe()`. Looking the summary up in `CONFIGS`
instead would be exact and useless: the runs that need rebuilding are precisely
the ones the registry has moved past — `live_20260824_150112` ran
`e44a047 calmar=13.6742` against today's `4282d33 calmar=3.6006` — so the lookup
would miss exactly when it is needed.

The keys and their order are `live/artifacts.live_params`', so the two files
diff. Three of them are **not in the summary line** and are written `null`
rather than guessed:

| key | why it is missing |
|---|---|
| `source_dataset` | never printed; today's path would be a plausible-looking lie |
| `max_aggression_ticks` | never printed |
| `warmup_days` | the `warm_up` record logs bars and a date range, not the requested window |

`sell_threshold` is the negation of `band=±x`; an asymmetric band is lost at
`describe()`, not here. `roll_at` is recovered exactly, because
`SessionPolicy.roll_at_time()` is a pure function of the session set.

An unparseable header is not an error — the rebuild proceeds and the report opens
without Settings, which is where it was before. And an existing `params.json` is
**never** overwritten, `--force` included: that one came from the run's own
`LiveConfig` and is complete, where this is a reconstruction from one line.

## Seeding the ledger — a migration, not the ordinary path

`--seed-ledger` appends a finished run's fills to the deployment's
[fill ledger](../live/ledger.md), so a later `--resume` replays them:

```powershell
uv run --no-sync python -m forecast_eval.tools.session_artifacts `
  outputs\live_20260824_150112 --seed-ledger --config night
```

A live run keeps its own ledger current as it goes, so this is needed only once:
the position a soak is holding **today** can predate the ledger entirely, and
then `--resume` has nothing to replay, falls back to the synthetic single fill,
and loses a history that is sitting in a log on disk.

Rows are tagged with the soak on disk (`soak_epoch.json`) unless `--soak-id` says
otherwise; without a soak they would be replayed by every future soak, including
one that `--fresh-start` deliberately started clean.

**Seeding twice is refused.** Appending again doubles every fill, and the
reconciliation in `cli.py` — which compares the replay's net against what the
venue holds — is the only thing standing between a wrong ledger and a
confidently wrong `trades.csv`.

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
| `params_from_meta(records, run_dir)` | function | `params.json` from the `meta` header, or None |
| `seed_ledger(result, run_dir, *, config, soak_id)` | function | append the fills to a deployment's ledger, once |
| `main(argv)` | function | CLI: rebuild, verify, write, render |
| `VENUE_UTC_OFFSET` | constant | `timedelta(hours=8)` — Asia/Taipei, no DST |

## Verified on

`outputs/live_20260824_150112` — 5 026 records → 4 455 bars, 149 forecasts, 149
signals, 3 orders, 3 fills, and one closed round trip: SHORT 44893 → 44520,
**+373.0 gross, +359.59 net of 13.41 in fees**. The third fill is the BUY at
44555 that was still open when the run ended — the same lot the current soak
adopts on `--resume`, which is what ties the reconstruction to the live account.
