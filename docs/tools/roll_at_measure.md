# [tools/roll_at_measure.py](../../tools/roll_at_measure.py)

**Role:** measure roll-day liquidity on **both** delivery months, to derive `ROLL_AT` from data instead of argument.
**Pipeline:** `dataset/RPT/extracted_data/Daily_*.rpt` → per-half-hour volume by month → the tables in `DAY_SESSION_PLAN.md` §4 C4.
**Depends on:** [live/roll.py](../live/roll.md) (for `last_trading_day`, `ROLL_LEAD_DAYS`)   **Used by:** run by hand, once per decision

> **Result 2026-08-25:** DAY `ROLL_AT` = **12:45**, NIGHT stays **15:30**.
> Full tables and reasoning in `DAY_SESSION_PLAN.md` §4 C4.

```powershell
uv run --no-sync python tools/roll_at_measure.py
```

Offline and read-only. Touches no venue and no live state. The module is
**import-safe**: the surveys run only under the command above (the suite's
static name check imports every module, so module-level execution would run —
on a machine without the raw files, crash — on every test run). With no
readable files under `RAW`, `measure` prints `no rows matched` and returns an
empty table instead of raising.

## Responsibility
`ROLL_AT` decides when a deployment swaps months, and the wrong value is not
loud: the clock still fires and the ladder still runs, just against a tape the
run is not following. The value therefore has to come from measurement.

The awkward part is the source. **The RPT minute and tick files cannot answer
this** — both are front-month-continuous, one contract per minute (measured
2026-08-23, recorded in `real_data.load_minute_bars`' docstring). On the roll
date 2026-08-18, `TXF_RPT_minute_day.csv` holds 300 day-session rows, all
`202608`; the incoming month is simply absent. A roll is two orders on two
months, so a one-month series can say nothing about it.

The raw TAIFEX `Daily_*.rpt` files carry every listed month and are the only
source here that can.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `read_day(d, extra_days)` | function | TX rows for trading date `d`, across the files that hold them |
| `measure(open_hm, span_min, label, extra_days)` | function | bucket a session into half hours, split by expiring/incoming |
| `EXPIRIES` | constant | the three most recent expiries measured |

## Two traps it exists to remember
- **A trading date's NIGHT session lives in the *next* calendar day's file.**
  `Daily_2026_08_18.rpt` carries trading dates `20260817` and `20260818`. Filter
  on the `trading_date` column and read both files, or the night table silently
  comes back with only its post-midnight half — which is what happened on the
  first attempt.
- **The product code is `TX`, not `TXF`.** `TXF` matches nothing and yields an
  empty table rather than an error.

## What the numbers meant
The per-bucket volume this produces is a **liquidity survey**, not the decision
input. `RollSchedule.due` gates when a roll may *start*; the fills are worked by
[`advance_roll_ladder`](../live/runtime.md), which is not bounded by
`ROLL_WINDOW`. So what decided C4 was the market in the ~2 minutes the ladder
actually runs, and the intra-minute price range there against the ladder's
25-tick cap — see §4 C4.
