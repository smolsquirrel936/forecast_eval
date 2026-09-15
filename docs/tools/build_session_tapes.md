# [tools/build_session_tapes.py](../../tools/build_session_tapes.py)

**Role:** turn T1's per-contract intermediate into the evaluated tapes — one per deployment, plus the full-day tuning window.
**Pipeline:** `TXF_RPT_minute_bycontract.csv` (4 551 359 bars) → `front_month_series(ts, policy)` → `TXF_RPT_minute_{day,night,fullday}.csv` and `TXF_RPT_minute.csv`.
**Depends on:** [live/roll.py](../live/roll.md), [live/sessions.py](../live/sessions.md), [environment.py](../environment.md)   **Used by:** run by hand, once per rebuild — `TAPE_REBUILD_PLAN.md` T2/T3

```powershell
uv run --no-sync python -m forecast_eval.tools.build_session_tapes `
  --src     \\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_bycontract.csv `
  --out-dir \\DESKTOP-SQUIRRE\dataset\RPT
```

25 seconds, no GPU. `--dry-run` builds and checks in memory without writing.
**Overwrites the three shipped tapes** (and writes a fourth) — the rollback is
`dataset/RPT/_prerebuild_backup_20260901/`, and a full rebuild cannot be undone
with `truncate`.

## Responsibility

`TAPE_REBUILD_PLAN.md` §2.3 needs **two** splice instants — day and the combined
full-day tape at 12:45, night at 15:30 — and none of the three tapes is a session
split of another. So the front-month choice cannot happen once, upstream; it
happens here, per policy, and the session split *is* that choice rather than a
separate pass over a front-filtered file (which is why plan step T3 folds in).

Each tape is `front_month_series(ts, policy)` over the intermediate, keeping one
row per minute, then filtered to the minutes that policy's sessions contain.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `session_labels(stamps)` | function | `classify_session` for a whole column, via a time-of-day lookup |
| `front_month_tape(bars, labels, policy)` | function | one deployment's tape: front-month-continuous, its sessions only |
| `main(argv)` | function | CLI: build all four, report the checks, write |
| `TAPES` | constant | the four `(filename, policy factory, start)` triples |
| `FULLDAY_START` | constant | `2018-01-01` — first minute of the full-day tuning window |
| `SETTLEMENT_CLOSE` | constant | `13:30` — when the expiring month stops trading |

## The selection rule, and the one place it is easy to get wrong

It is [`collapse_front_month`](../live/backfill.md)'s rule restated over an OHLCV
frame: **front month if it printed this minute, otherwise the most-traded other
month.** Volume is the fallback, never the rule — at a roll the expiring month
usually still has the larger volume, so a volume rule keeps feeding a contract the
deployment has already sold.

**The fallback may reach only *earlier* months.** `fetch_bars_continuous` fetches
"the front month plus whatever earlier months are still listed", plus the cache
that carries a delisted one; it can never fetch the far month. A tape that filled
from a later month would be a series the live run cannot reproduce — the exact
class of divergence the rebuild exists to close (§1, §7).

Measured on the night tape, the restriction matters:

| fallback reach | bars filled | dates | verdict |
|---|---|---|---|
| earlier months only | 10 240 | 216 (post-roll windows) | **what live does** |
| any other month | 12 288 | 752 (all times) | 2 048 bars live cannot see |
| none | 0 | — | re-opens the gap at every roll |

**What the fallback costs, kept visible on purpose.** Those bars carry the
inter-month basis (§0.4: 84–325 points), so `|Δclose|` on them is **median 20 /
p95 181** points against **median 2 / p95 12** on ordinary bars — one-bar spikes,
~47 minutes per roll window. They are kept because the live path produces them,
not because they are pretty. Note they buy nothing for §0.2's roll holes: those
close from the splice instant alone, and the last-trading-day bar counts are
identical without the fallback.

## Verification

Printed by every run, including `--dry-run`.

| tape | bars | splice | filled from another month | dead-contract bars |
|---|---|---|---|---|
| `TXF_RPT_minute_day.csv` | 1 146 712 | 12:45 | 1 | **0** |
| `TXF_RPT_minute_night.csv` | 1 849 007 | 15:30 | 10 240 | **0** |
| `TXF_RPT_minute.csv` | 2 995 727 | 12:45 | 10 319 | **0** |
| `TXF_RPT_minute_fullday.csv` | 2 364 417 | 12:45 | 10 319 | **0** |

**The roll count is unchanged: 112 splices on the night tape and 188 on the day
tape**, exactly `TAPE_REBUILD_PLAN.md` §0's figures for the pre-rebuild files.
Only the *instants* moved, which is the whole intent. Note the report counts
changes in the running-maximum month, not in the `contract` column — with the
fallback in place a raw change count reads 14 498 on the night tape, because a
filled minute borrows the outgoing month for one bar and hands it back.

**Dead contracts (T2).** Zero bars anywhere are stamped after their own
contract's last trading day 13:30. `last_trading_day` is the naive third
Wednesday with no holiday table, so on the four holiday-shifted expiries it names
an *earlier* date than the real one and the check is strict rather than loose —
the pre-rebuild day tape scores **1 141** under the same check, all of it on those
four dates (2013-08-22, 2015-02-24, 2023-01-30, 2026-02-23).

**Roll holes (T3).** Gone on the day side and nearly gone on the night side:

| | pre-rebuild | rebuilt |
|---|---|---|
| DAY bars on the 183 last-trading-day sessions | median **285**, short by 2 723 | median **300**, short by **0** |
| NIGHT bars on the 110 last-trading-day sessions | median **297** | median **834** of 840 |

The residual 1 612 night bars are minutes with no print in *any* kept contract —
genuine thin data, not a rule artifact.

**Reconciliation (T2).** `day + night` and the combined tape agree on every
timestamp but eight, and hold a different delivery month on 3 143 bars — all of
them **15:00–15:29 on a roll date**, which is exactly §2.3's prediction: a
full-day deployment rolled at 12:45 and the night one has not. The eight extra
minutes in the combined tape are 15:04–15:26 on six roll Tuesdays where only the
*incoming* month printed; the combined tape is already on it, the night tape is
still on the outgoing month that printed nothing, and the earlier-only fallback
correctly refuses to substitute a later one. Nothing ever goes the other way.

**Through the real consumer.** All four load through
[`real_data.load_minute_bars`](../real_data.md) with no row lost: one contract per
minute (so its highest-volume collapse stays the no-op it has always been), no NaN
closes, strictly monotonic timestamps, no duplicates, `float64` OHLCV, and each
tape carries only its own sessions.

## What it deliberately drops

The intermediate's **1 416 out-of-session bars** — the 13:45 closing-auction print
(1 333) and the 05:00 night boundary (83) — reach no tape. Both
`SessionPolicy.label()` and `classify_session` return `None` for them, so
`run_backtest` and the live aggregator already ignored them; their only effect was
to make raw row counts disagree with in-session bar counts, which is how
`real_data.py` came to print a train/eval split it was not using (§0.5). This is a
**drop**, never a relabel: nothing is moved to 13:44 where the session filters
would start accepting it.

## The fourth tape: `TXF_RPT_minute_fullday.csv`

Same policy, same 12:45 splice, same rows — **only the window differs**. It is
`TXF_RPT_minute.csv` from `FULLDAY_START` (2018-01-01) onward, and the builder
asserts that equality bar-for-bar on every run.

It exists because the combined tape spans 2011 and **TAIFEX had no after-hours
session until 2017-05-15**, so its first **468 524 bars (15.6 %)** are day-session
only — a 300-bar "session" where a full-day deployment's is 1 140. Night *hours*
were 15:00–04:59 from day one; what ramps is coverage of those 840 minutes:
0.84–0.90 through 2017, 0.931 in 2018-01, **0.989 in 2018-02** and ~0.99 after.

The cut keeps January 2018 despite it sitting below the plateau — one
transitional month out of 8.6 years, against an unexplainable start date. Change
`FULLDAY_START` and rebuild for 2018-02-01 (strict) or 2017-05-15 (everything).

**Truncating the combined tape instead would be wrong.** That file is the record
and the DAY deployment's history genuinely reaches to 2011; this one is the
tuning window. One file cannot be both.
