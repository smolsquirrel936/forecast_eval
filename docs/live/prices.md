# [live/prices.py](../../live/prices.py)

**Role:** the price line a soak's report is drawn against — every segment of the soak, not just the run being rendered.
**Pipeline:** sibling run dirs → [capture.price_series](capture.md) → one row a minute → `prices_<slug>.csv` → `reports.generate_report`.
**Depends on:** [live/capture.py](capture.md), [live/sessions.py](sessions.md)   **Used by:** [live/artifacts.py](artifacts.md)

## The problem

A report is rendered per run directory, and its price background was that run's
own `ticks.csv`. But a soak is *many* runs — a restart opens a new directory — so
the chart restarted with it and showed a few minutes of prices beneath fills that
span weeks.

`--resume` makes it worse than cosmetic. The [fill ledger](ledger.md) replays
fills executed in **earlier** segments, so a report could plot a fill from a day
whose prices live in a different directory entirely. That is the same "fills
drawn nowhere near the price line" failure
[tools/session_artifacts.check_alignment](../tools/session_artifacts.md) exists to
catch, arriving from the other direction — and this time nothing was checking.

## Two rules decide which captures are in

| rule | what it catches |
|---|---|
| the run's `session.jsonl` names this `soak_id` | every segment of the current soak |
| the capture's span **covers a fill being charted** | the run that opened a position this soak inherited |

The second is not a special case for one directory. Any resumed history has that
shape: the fills come from a run whose prices live somewhere else. Measured on
`soak_20260901_133944_774636` — nine directories match by soak id, and the
`cover` rule pulls in `live_20260824_150112`, whose three fills the ledger
replays. All three then fall inside the assembled line; before this they fell
outside it.

## Minute resolution, and a cache

A night is ~25 000 prints and a soak runs for months, so each capture is
downsampled to **one row a minute** before it is kept. The report draws at most
`n_ticks_for_chart` points anyway, and the last print of a minute is the same
convention the evaluated tape uses for `close` — so the line is the series the
strategy was measured on rather than a different summary of the same ticks.

The result is cached in `prices_<slug>.csv`, beside the bar cache and the fill
ledger and keyed the same way, so DAY and NIGHT cannot mix. It is keyed by run
directory **name**, so a finished segment is parsed exactly once ever.

Measured on the soak above: **6 572 minute rows spanning 2026-08-24 15:01 →
2026-09-02 02:17**, assembled in 0.35 s cold and 0.02 s from cache.

**The live segment is deliberately not cached.** It is still growing, so its rows
would be stale the moment they were written; it is re-read on every render and
unioned on top, with `drop_duplicates(keep="last")` letting the fresher copy win.

## Everything here degrades rather than fails

A chart background is the least important thing in a run directory, so every path
is best-effort: an unreadable capture costs that directory's inclusion, a corrupt
cache costs a re-parse, and any failure inside `ArtifactWriter._price_line` falls
back to this run's own capture and then to no line at all. None of it can cost
the artifacts, and none of it can touch a process holding a position.

`capture_span` reads only the **first and last** rows of a capture — a bounded
8 KB tail read — because inclusion is decided for every sibling directory and one
of them holds 287 726 prints. Parsing them all to find out which are relevant
would cost more than the report does.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `soak_price_series(out_parent, *, policy, soak_id, cover, run_dir)` | function | the soak's price line, cached |
| `minute_series(path)` | function | one capture as one row a minute |
| `capture_span(path)` | function | a capture's first and last timestamp, cheaply |
| `cache_path(out_parent, policy)` | function | `prices_<slug>.csv` |
| `CACHE_COLUMNS` | constant | `timestamp, price, run` |
