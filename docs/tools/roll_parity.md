# [tools/roll_parity.py](../../tools/roll_parity.py)

**Role:** the tape-spanning parity check — at every roll, is the shipped tape the series the live warm-up path would have built?
**Pipeline:** `TXF_RPT_minute_bycontract.csv` → the live reach + `collapse_front_month` → compared, minute for minute, against the shipped tape.
**Depends on:** [live/backfill.py](../live/backfill.md), [live/roll.py](../live/roll.md), [live/sessions.py](../live/sessions.md), [tools/build_session_tapes.py](build_session_tapes.md)   **Used by:** run by hand — `TAPE_REBUILD_PLAN.md` §0.5 (promised) / §5.3 (results)

```powershell
uv run python -m forecast_eval.tools.roll_parity --deployment night
uv run python -m forecast_eval.tools.roll_parity --deployment day `
  --out outputs\roll_parity_day.csv
```

~8 s a deployment. Exit status is 1 when any roll differs, so it can be used as a
check rather than only read.

## The two parities, and why this is the second one

`DAY_SESSION_PLAN.md` W7 proves **transcription** parity: `LiveLoop` decides what
`run_backtest` decides *given the same bars in*. It is deliberately synthetic —
both sides are handed one stream — so nothing in it can notice that the two sides
would not have been handed the same stream to begin with.

`TAPE_REBUILD_PLAN.md` §1 is about the other half: at a roll, the evaluated series
and the series a deployment would really hold **differ structurally**, because the
tape spliced 1 d 8 h 30 m after the live rule and had holes the live path cannot
have. §0.5 records that check as *"has not been done, and cannot pass today"*.
The rebuild removed the reason it could not pass. This module is the check.

## What is under test, and what is merely read

Both sides come from the per-contract intermediate, but only one of them is
rebuilt here:

| side | where it comes from |
|---|---|
| tape | the shipped CSV, read as-is — it is what `results.tsv`'s numbers were measured on |
| live | `live/backfill.collapse_front_month`, **called**, with the `front_month_at` `live/cli.py:645` really passes, over the months `_warmup_contracts` can really reach |

Neither rule is restated. A check written by copying the rule it checks proves
only that the copy was faithful — which is the failure `tests/test_tape_build.py`
already guards against at the unit level, and is not what this is for.

Three fidelity claims hold the live side up, each of them a way this could have
been written to pass by accident:

- **Reach.** `_warmup_contracts` returns "the listed months up to and including
  the front one", so a minute the front month missed can be filled only from an
  *earlier* month. A live side without that restriction would be handed prints
  the deployment cannot fetch.
- **The splice instant.** `front_month_at` is passed **unbound** — its default is
  `ROLL_AT`, NIGHT's 15:30, for every deployment. Binding the policy here would
  quietly repair the divergence the check exists to find.
- **The session filter.** `TickAggregator` drops out-of-session minutes live; the
  tape has them dropped at build time.

## What it cannot see

- The venue's `TXFR1` alias rolls at **settlement**, not at `roll_at`, so which
  months are *listed* can lag this model on the roll day. That is `RollWatch`'s
  question (`SHIOAJI_PLAN.md` §2.6), not this one.
- The comparison is **windowed** around each roll (`--window-hours`, default 24).
  Away from a roll every candidate month maps to the same answer under either
  rule, so there is nothing there to differ. NIGHT was re-run at ±96 h with an
  identical verdict.
- It compares **series**, not decisions. If the series match, W7 already carries
  the rest; where they do not, what the difference costs a backtest is a separate
  measurement.

## Results

**2026-09-02, after the fix — every tape is clean.**

| deployment | splice | rolls | identical |
|---|---|---|---|
| night | 15:30 | 112 | **112** |
| day | 12:45 | 188 | **188** |
| combined | 12:45 | 188 | **188** |
| fullday | 12:45 | 104 | **104** |

592 rolls, zero differing minutes. The one-line repair is in `live/cli.py`, which
now binds `cfg.policy` into the `front_month_at` it hands the backfill, and in
`live/roll.py`, where that argument is no longer optional. Both findings below are
closed by it — including the tape-only holes, because from 12:45 the incoming
month is *reachable* rather than "later".

**2026-09-01, the run that found the defect.** Kept because it is what the check
is for, and because pointing `live_series`' splice back at the bare function
reproduces it exactly:

| deployment | splice | rolls | identical | minutes on the wrong month | tape-only minutes |
|---|---|---|---|---|---|
| **night** | 15:30 | 112 | **112** | 0 | 0 |
| day | 12:45 | 188 | 5 | **10 979** | 0 |
| combined | 12:45 | 188 | 5 | 14 122 | 8 |
| fullday | 12:45 | 104 | 4 | 8 949 | 2 |

**NIGHT — the deployment that is actually soaking — is exact.** 112 of 112 rolls,
bar for bar, contract for contract, at ±24 h and again at ±96 h. This is the first
direct evidence that the rebuild did what it was for, and it is the claim
`SHIOAJI_PLAN.md`'s V-series has been resting on.

**Every DAY roll that traded diverged — 183 of 183.** The five "identical" DAY
rolls are days the market was shut (four Lunar New Year dates and 2024-09-17); on
each of them both series are empty. The divergence is exactly the 60 minutes
**12:45–13:44**, the span between the DAY tape's splice and the session close, and
it points the same way every time: the tape holds the **incoming** month, the live
warm-up holds the **outgoing** one, in 183 rolls out of 183. Over those 10 979
minutes `|Δclose|` is **median 27 points, p95 183, max 463** — the inter-month
basis (§0.4), not noise. `TAPE_REBUILD_PLAN.md` §5.3 has the cause and the fix.

**The combined and full-day tapes additionally carried a few minutes the live path
would not have had at all** — 8 and 2, across six 2017–2019 rolls. Between 12:45 and
15:30 the tape's front month is the incoming one, so a minute only the incoming
month printed is a normal tape bar; to the live rule that same month is still
*later* than its own front, so it is out of reach and the minute is dropped.

Checked minute by minute: all eight fall between **15:04 and 15:26** and in every
one of them the *only* contract that printed was the incoming month. That is why
they cluster where they do — the day-session half of the disagreement window is
liquid enough that the outgoing month always prints, while the 2017–2019 night
session is the thin one `FULLDAY_START` is about. A hole of the §0.2 kind arriving
by a different door: same symptom, different cause (a reach limit, not a dead
contract), and **8 bars against §0.2's 2 782**.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `roll_windows(first, last, policy, hours)` | function | every roll instant in a span, with a window around it |
| `read_windows(path, windows)` | function | the rows of a minute CSV inside those windows, chunked |
| `live_series(bars, policy)` | function | the series the live warm-up path would have built |
| `compare(tape, live)` | function | one window's verdict, counted by kind of difference |
| `main(argv)` | function | CLI: window, rebuild, compare, print the dirty rolls |
| `TAPES`, `BYCONTRACT` | constants | the four shipped tapes and their splice policies |

## Why the differences are counted by kind

A single "n mismatches" would not separate the failure the rebuild was for — a
**hole**, a minute the tape lacks entirely (§0.2) — from the one it could still
have: the same minute sourced from a **different month**, which is a splice-instant
disagreement and moves the price by the inter-month basis (§0.4). Different
causes, different consequences, so `only_tape`, `only_live`, `diff_contract` and
`diff_close` are counted apart and the summary reports rolls, not a percentage of
minutes. One dirty roll in 112 is not "99 % parity" — it is a deployment that
disagrees with its own backtest twelve times a year.
