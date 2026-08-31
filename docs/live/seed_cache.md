# [live/seed_cache.py](../../live/seed_cache.py)

**Role:** fill a bar cache offline from the RPT tape, so a first DAY run starts warm instead of blind.
**Pipeline:** `TXF_RPT_minute_day.csv` → `rpt_to_cache_frame` → [save_bar_cache](backfill.md) → `outputs/bars_cache_<policy>.csv` → [warm_up](runtime.md).
**Depends on:** [live/backfill.py](backfill.md), [live/sessions.py](sessions.md)   **Used by:** run by hand, once per deployment

> **DAY_SESSION_PLAN C7 / W3.** Written 2026-08-24. Seeded
> `outputs/bars_cache_day.csv`: 30 000 DAY bars, 2026-03-27 → 2026-08-21,
> months `202604`–`202609`, **0 bars outside the DAY policy**.

```powershell
uv run python -m forecast_eval.live.seed_cache `
  --data \DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_day.csv `
  --out outputs/bars_cache_day.csv --session day
```

Offline and read-only against the venue: it logs into nothing and places no
orders. Exit `0` on success, `2` when the tape holds no bar for the requested
session.

## Responsibility
A DAY deployment needs ~85 calendar days of warm-up to prime the emitter (C6:
`3008 + 500 × 30` = 18 008 bars, and a DAY session is only ~300 bars long). It
cannot fetch that. Expired months are delisted (§2.8 Finding D), so an 85-day
window spans ~3 delivery months of which the older two no longer answer `kbars`,
and [`_earlier_months`](cli.md) reaches back only one month anyway. The cache is
the only other source and **starts empty**, so a first DAY run is short ~2 months
of context and cannot recover except by waiting for it to fill from live.

This module fills it from the tape the backtest already reads.

> **It mixes two bar sources in one context window** — RPT here, `kbars` at run
> time — which [v1c_check](v1c_check.md) only ever compared on a single session.
> DAY_SESSION_PLAN **D4 accepted that trade on 2026-08-24**; the alternative was
> waiting ~2 months. What makes it defensible is that the two are the same
> observations under the same labels: [backfill](backfill.md) converts `kbars`'
> right-labelled bars to this file's left-labelled convention before anything
> else, and both drop NaN closes and carry real per-bar volume.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `rpt_to_cache_frame(path, policy, *, keep, until)` | function | RPT csv → the cache's four-column frame, policy-filtered |
| `main(argv)` | function | CLI; writes through `save_bar_cache` |

## Why it filters by policy
[`warm_up`](runtime.md) rejects a warm-up frame containing bars from any session
the policy does not trade. An unfiltered seed would therefore not merely be
untidy — it would make **every start-up raise**. Filtering here is also what
keeps the seed honest: a DAY cache holds DAY bars and nothing else, which is the
same separation [C5's policy-keyed cache path](cli.md) enforces on the filename.

## What it deliberately does not do
- **No timestamp shift.** The RPT file *is* the convention `backfill` converts
  `kbars` into; shifting here would double-apply a correction only the venue's
  right-labelled feed needs.
- **No adoption of a pre-C5 `bars_cache.csv`.** That file holds NIGHT bars;
  handing them to a DAY run is the corruption the keyed path exists to prevent.
