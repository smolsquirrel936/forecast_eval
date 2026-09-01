# Evaluated-Tape Rebuild — Plan

**DRAFT v1, 2026-08-25.** Written after `DAY_SESSION_PLAN.md` §4 C4a found that
`front_month_at` and the evaluated tape splice delivery months at different
instants. Investigating that turned up a larger defect in the tape itself.

**Nothing has been built and no rebuild has been run.** Two decisions in §6 are
the user's and are not assumed anywhere above them.

**Updated 2026-09-01 — §6's D1 and D2 are both taken, so the rebuild is no
longer gated on a decision.** D1: NIGHT moves to the new tape, one vintage, not
two — overriding this plan's recommendation, with the live risk it accepts
recorded at §6. D2: the combined `TXF_RPT_minute.csv` **survives**, reserved for
a future third deployment trading DAY and NIGHT as one session; its splice
instant is `SessionPolicy.both().roll_at_time()` = **12:45**, the same as the
*day* tape's, so no new rule is needed — though none of the three tapes is a
session-split of another (§2.3). D1's scope sub-question is settled the same day:
**(a) re-measure, do not re-search**. `roll_at_time()` was amended so any policy
trading DAY rolls at 12:45 (`DAY_SESSION_PLAN.md` C4).

**Re-verified 2026-09-01 — still true, and both defects are still in the file.**
The day tape was re-measured end-to-end against §0; the numbers confirm it, one
new finding is added, and one cross-plan claim in §1 turns out not to hold as
written. See **§0.5**.

**Status of the proposal:** the user proposed re-making the tape on a new rule
rather than changing the code to match the old one. That direction is right —
§1 is the evidence — but the specific instant proposed needs adjusting (§2.2).

---

## 0. What the tape does today — measured, not assumed

The files are `dataset/RPT/TXF_RPT_minute_{day,night}.csv`, reached in code as
the UNC `\\DESKTOP-SQUIRRE\dataset\RPT\...` path (same bytes, verified).
`PINNED.source_dataset` names the night one.

| | night | day |
|---|---|---|
| bars | 1 787 913 | 1 145 145 |
| span | 2017-05-15 → 2026-08-21 | 2011-01-03 → 2026-08-21 |
| contract splices | **112** | **188** |

### 0.1 The rule, and where it lives

`dataset/NOTE.md`'s rebuild procedure names it outright:

> per `trading_date` keep `contract == min(contract)` (front month; **this is
> what produces the calendar splice one day after expiry**)

Implemented in `process_txf.ipynb` **cell 7**:

```python
front_map = tx_all.groupby("trading_date")["contract_int"].min()
tx_front  = tx_all[tx_all.contract_int == tx_all.front_contract]
```

One front month per **trading date**, chosen as the numerically smallest listed
month. That is the whole rule, and both defects below follow from it.

### 0.2 Defect A — the tape holds a contract that has stopped trading

TAIFEX retires the expiring month at **13:30 on its last trading day**, while
every other month runs to 13:44:59 and on into the night session. Measured on
2026-08-19: `202608` last print **13:29:58**, `202609` **13:44:59**.

`min(contract)` keeps the expiring month as "front" for that whole trading date
regardless. So the minutes after 13:30 are attributed to a contract that is not
printing, and **no bar is written at all**:

| tape | window | bars present | expected | missing per roll |
|---|---|---|---|---|
| **night** | 15:00 → 05:00 on the last trading day | ~294–300 | 840 | **~540** |
| **day** | 08:45 → 13:45 on the last trading day | **285** | 300 | **15** |

Night, per expiry — the last day-session bar is 13:29 in every case:

| expiry | last trading day | night bars | |
|---|---|---|---|
| 202601 | 2026-01-21 | 300 | hole |
| 202602 | 2026-02-18 | 0 | Lunar New Year — market closed, **not** a hole; confirm before quoting |
| 202603 | 2026-03-18 | 300 | hole |
| 202604 | 2026-04-15 | 292 | hole |
| 202605 | 2026-05-20 | 292 | hole |
| 202606 | 2026-06-17 | 300 | hole |
| 202607 | 2026-07-15 | 290 | hole |
| 202608 | 2026-08-19 | 294 | hole |

The ~300 bars that do survive are the 00:00–04:59 remnant of the *next* calendar
date, after `min(contract)` has moved on.

**This is the defect that matters.** It is not a labelling disagreement; it is
missing data — ~540 night bars and 15 day bars every month, at a completely
predictable moment.

### 0.3 Defect B — the splice is 1 d 8 h 30 m away from the live rule

`front_month_at` flips at `roll_at(month)` = last trading day − `ROLL_LEAD_DAYS`
at `ROLL_AT`. The tape flips at the first session after expiry. Measured on the
night tape, every month from 202601 to 202608:

| | tape switches | `front_month_at` flips | gap |
|---|---|---|---|
| 202606→202607 | 2026-06-18 00:00 | 2026-06-16 15:30 | **1 d 8 h 30 m** |
| 202607→202608 | 2026-07-16 00:00 | 2026-07-14 15:30 | **1 d 8 h 30 m** |
| 202608→202609 | 2026-08-20 00:00 | 2026-08-18 15:30 | **1 d 8 h 30 m** |

Identical every month. (January shows 15 d and February 6 d — New Year and Lunar
New Year; the tape's rule is "the session after expiry", not "+24 h".)

`collapse_front_month`'s comment predicted this exact number — "the warm-up
context and the tape would disagree for a day and a half every month, and
nothing would detect it" — while claiming the code implements the tape's rule.
It does not, and **it cannot**: the roll must precede expiry to be tradeable at
all, and the tape splices after it.

### 0.4 What the disagreement is worth

In the disputed window the two candidate series are two different delivery
months, and they are not close:

| roll | overlapping minutes | incoming − expiring | as fraction of price |
|---|---|---|---|
| 202606→202607 | 1 467 | +119.3 pts (25 … 367) | 0.00261 |
| 202607→202608 | 1 449 | +324.5 pts (77 … 444) | **0.00720** |
| 202608→202609 | 1 516 | +84.3 pts (−67 … 156) | 0.00187 |

The strategy's band is **0.00045**, so the two series differ by **4×–16× a band
width** across ~1 500 minutes a month.

### 0.5 Re-verification, 2026-09-01 — the defects survived the 2026-08-24 regeneration

Measured directly on
`D:\…\dataset\RPT\TXF_RPT_minute_day.csv`, asked as a plain question: does
this file still need rebuilding? **Yes.**

The file has not changed since this plan was written: mtime **2026-08-24
03:53:26**, 73 743 465 bytes. That regeneration (`DAY_SESSION_PLAN.md` §2.2)
extended coverage to 2026-08-21 but kept `min(contract)` per `trading_date`, so
it fixed neither defect. §0's own figures reproduce exactly — 1 145 145 rows,
2011-01-03 → 2026-08-21, 188 splices.

**Defect A — confirmed, and quantified across the whole tape.** §0.2 measured a
representative roll; this is all 189 of them:

| | measured 2026-09-01 |
|---|---|
| last-trading-day sessions | 189 |
| median bars on one | **285** (a full DAY session is 300) |
| ending 13:29 or 13:30 rather than 13:44 | **187 / 189** |
| total bars missing | **2 782** |
| missing inside `DAY_SESSION_PLAN` W5's eval window | **1 157**, across 79 expiries |

Every one of the last nine expiries is short by 14–15 bars, `202608` on
2026-08-19 included — the same contract §0.2 measured at tick level.

**Defect B — confirmed.** **183 of 187** splices land exactly **1 calendar day**
after the outgoing contract's last trading day, which is §0.3's "the session
after expiry" rule seen from the day side. The four exceptions are the holiday
cases §0.3 already names.

**New — 1 215 rows the tape carries and nothing consumes.** Not in §0, found on
the way. Rows stamped **13:45**, on ~32 % of dates, median volume **4** against
**2 191** on the 13:44 bar. Both `SessionPolicy.day().label()` and
`environment.classify_session()` return `None` for 13:45, so the live aggregator
and `run_backtest` **both already drop them** — this is not a correctness defect
and it is not an argument for the rebuild. It matters for one reason: it makes
raw row counts disagree with in-session bar counts (the night tape has 82 such
rows), which is how `real_data.py` came to print a train/eval split it was not
using. Fixed there; recorded here so the rebuild does not reintroduce it or
"fix" it into something the session filters would start accepting.

**Consequence the rebuild has to plan for: `warmup_bars` will not survive it.**
`DAY_SESSION_PLAN` §2.2 made warmup-in-bars the reproducibility mechanism, and
that holds for a tape growing **at the end**. A bar count is an index into the
in-session sequence, so **inserting** bars *before* the split moves what it
points at — and fixing Defect A restores ~15 day bars at **every** expiry, ~63 of
which precede 2020-03. After T0–T4, `674 498` names a different bar than it does
today. **Every `warmup_bars` in `results.tsv` must be recomputed from a target
timestamp, not carried across as a number**, or the re-measurement reproduces
§2.2's alignment trap with a new cause.

**§1's W7 claim does not hold as written.** §1 says the roll holes make this
rebuild "a prerequisite for `DAY_SESSION_PLAN.md` W7, not an unrelated cleanup",
while that plan's §5 says W7 "does not read the tape" and is unaffected. **W7 was
built 2026-09-01 the way its own plan specifies — V0-style, on synthetic ticks
with a deterministic fake forecaster — so it never spans a real roll and passed
without touching any of this.** Both statements are therefore true of different
things, and the disagreement is worth naming rather than leaving for someone to
rediscover:

- the *transcription* parity W7 delivers (live loop ≡ `run_backtest`, same bars
  in) is genuinely independent of the tape, and is done;
- the *tape-spanning* parity §1 is describing — replaying the real evaluated
  series across a roll — **has not been done, and cannot pass today**, because
  the two series structurally differ there. That is a real gap, it is not W7,
  and it only becomes checkable after this rebuild.

---

## 1. Why this is worth fixing rather than documenting

**The backtest has a hole the live run structurally cannot have.**

`collapse_front_month` — the *live* path — already handles exactly this case:

> a minute the front month did not print is better filled from the other month
> than dropped, and that is exactly what the fallback does.

The offline tape builder has no such fallback, which is why the holes in §0.2
exist. So at every roll, twelve times a year, the evaluated series and the live
series differ **structurally**, not merely in labelling. Any parity check
spanning a roll will trip over it — which makes this a prerequisite for
`DAY_SESSION_PLAN.md` W7, not an unrelated cleanup.

The pinned config was tuned *through* those holes. Whatever the tuning learned
about roll weeks, it learned from data the live deployment will never see.

---

## 2. The proposed rule

### 2.1 Splice at `roll_at(month)` — the instant the deployment actually rolls

- **Zero gap** between tape and live, by construction: the tape builder and
  `front_month_at` become the same rule.
- **Zero hole**: from `roll_at` the front month is the *incoming* one, which is
  printing continuously. Nothing is ever attributed to a dead contract.
- **C4a dissolves** rather than being fixed — two rules cannot diverge if there
  is only one.

### 2.2 Why not "15:30 on the 3rd Wednesday" (the original proposal)

Two reasons, both measured:

- **It leaves a 30-minute hole.** The expiring month dies at 13:30 on the 3rd
  Wednesday. Splicing at 15:30 that day still attributes 15:00–15:29 to it —
  30 empty bars a month instead of 540. Better, not fixed.
- **It cannot equal the live rule, which was the point.** `roll_at('202608')` is
  **2026-08-18 15:30** (`ROLL_LEAD_DAYS = 1`); 15:30 on the 3rd Wednesday is
  2026-08-19 15:30, exactly 24 h later. `ROLL_LEAD_DAYS` cannot go to 0 to close
  the difference: at 15:30 on the last trading day the expiring contract is
  already dead, so there would be no position left to sell.

### 2.3 The consequence to accept: two tapes, two splice points

`ROLL_AT` is policy-carried since 2026-08-25 (DAY 12:45, NIGHT 15:30), so the
day and night tapes would splice at different instants. That is correct — they
serve different deployments — but it means the two files stop being two views of
one series, and **`TXF_RPT_minute.csv` (combined) needs a stated choice or
should stop being used for tuning**. Decision D2.

**RESOLVED 2026-09-01 by D2 — three tapes, two splice points.** The combined
file survives as the evaluated series for a future full-day deployment, so it is
spliced on that deployment's own rule: `SessionPolicy.both().roll_at_time()`.

**AMENDED the same day — that is 12:45, not 15:30.** `roll_at_time()` tested
`sessions == {"DAY"}` and so handed a `both()` policy NIGHT's 15:30; it now
tests `"DAY" in sessions`. The reason is the clearance day and it is a hard
constraint (`DAY_SESSION_PLAN.md` C4, "EXTENDED 2026-09-01"): the expiring month
stops trading at **13:30** on its last trading day, a full-day deployment's last
waking moment before that is the clearance-day morning, and a roll scheduled past
it does not roll — **the position expires**. So:

| tape | splice | serves |
|---|---|---|
| `TXF_RPT_minute.csv` | **12:45** | full-day deployment (future) |
| `TXF_RPT_minute_day.csv` | **12:45** | DAY deployment |
| `TXF_RPT_minute_night.csv` | **15:30** | NIGHT deployment |

**Day and combined share a splice; night is the odd one out.** (An earlier draft
of this section had it the other way round, on the pre-amendment 15:30.)

> **This contradicts §3's pipeline, and the contradiction has to be resolved in
> T0/T1 rather than discovered in T3.** §3 draws one front-month rule at cell 7
> producing one tick file, one combined minute file, and then a *session split*
> into day and night. That structure can carry only **one** splice instant, and
> there are two.
>
> **No one of the three is a session-split of another**, which is the part that
> breaks the diagram:
>
> - the **day** tape is not a split of the combined one — a split takes a subset
>   of rows, and outside 12:45→15:30 these two agree, but the combined tape must
>   also carry night bars the day tape has no rows for at all;
> - the **night** tape is not a split of the combined one either, because they
>   splice at different instants. On roll Tuesday the night session opens 15:00
>   while the combined tape has already been on the incoming month since 12:45 —
>   so from **15:00 to 15:30** the two hold **different delivery months**. That
>   is 30 minutes a roll, twelve times a year, and it is correct: they serve
>   deployments that genuinely hold different contracts at that moment.
>
> So the rebuild needs the front-month selection applied **per policy**, not once
> at cell 7. Two ways, not chosen here:
>
> - re-derive from `extracted_data/` once per splice instant — simple, and pays
>   §3's "expensive path" twice (12:45 and 15:30);
> - carry `contract` through to the minute stage and apply the per-policy
>   selection at the split — one derivation, but the split step stops being a
>   filter and becomes part of the splice rule.
>
> §3's diagram and T1–T3 should be rewritten to whichever is chosen. Left as-is
> above so the choice is visible rather than silently assumed.

---

## 3. Scope — what has to be rebuilt

**The 9.2 GB tick file is downstream of the defect.** `process_txf.ipynb` applies
the front-month filter at cell 7 and writes `TXF_RPT_ticks.csv` at cell 9, so
that file already has the wrong months in it. A rebuild cannot start from it.

```
dataset/RPT/TaifexRPT/**.zip      (raw, all months)      <- the only clean source
  -> extracted_data/Daily_*.rpt   (3 832 files, all months)
  -> [cell 7: front-month rule]   <- THE CHANGE, one cell
  -> TXF_RPT_ticks.csv            (288 943 166 rows)     <- must be rebuilt
  -> TXF_RPT_minute.csv           (2 933 058 bars)       <- must be rebuilt
  -> tools/split_session_txf_ohlc.py
  -> TXF_RPT_minute_{day,night}.csv                      <- must be rebuilt
```

The code change is small — replace the per-`trading_date` `min(contract)` with
a per-*timestamp* rule keyed on `roll_at`. The cost is the full re-derivation,
which `NOTE.md` calls the expensive path (incremental append exists precisely to
avoid touching the 9.2 GB file).

### 3.1 Estimated blast radius on the night tape

| | |
|---|---|
| bars re-attributed to the other month | ~112 000 (**6.3 %**) |
| bars newly filled that are missing today | ~60 000 |

**This invalidates the pinned config.** Calmar 13.6742, and W4b's re-baselined
16.3122, were both measured on the current tape. A 6 % re-attribution plus
60 000 new bars is a different series; the config has to be re-tuned against it,
and `results.tsv` needs a new `dataset` vintage (the column already exists — W0).

---

## 4. Sequencing — the constraint that shapes everything

**This lands before `DAY_SESSION_PLAN.md` W5, or W5 is wasted.** Tuning the day
config on a tape about to be replaced produces a pinned row with no provenance.
W4b's night re-baseline needs re-running for the same reason.

So the day port's order becomes:

```
rebuild both tapes  ->  re-baseline night  ->  W5a/W5b  ->  W6 onward
```

against a first live roll on **2026-09-15**, with V5 not yet passed.

> **SUPERSEDED 2026-09-01 — the order above was not followed, deliberately.**
> `DAY_SESSION_PLAN.md` W5a, W5b, W6 and W7 were all run *before* this rebuild,
> on that plan's explicitly **provisional** branch: W5a/W5b are measured against
> Calmar 16.3122 and are re-measured after T0–T4 whatever they said. W6 and W7
> never read the tape and are unaffected. So the remaining order is not
> "rebuild → W5" but:
>
> ```
> T0-T4 rebuild  ->  T5 re-measure NIGHT  ->  T6 re-pin (D1)
>                ->  re-run W4b, W5a, W5b on the new tape  ->  W8 onward
> ```
>
> The prediction in this section held: W5's numbers do need re-running. What it
> bought by going first is that the *machinery* is built and tested, so the
> re-measurement is a re-run rather than a fresh piece of work — and W5a's
> finding that night's parameters do not transfer at all is a fact about the
> strategy, not about the tape vintage, so it will survive the rebuild whatever
> the numbers do.

---

## 5. Order of work

```
T0  pin the new rule as one function, with tests        -> verify: agrees with front_month_at on every
    (shared by the notebook and front_month_at)            splice 2017-2026; no dead-contract minutes
T1  rebuild TXF_RPT_ticks.csv from extracted_data/      -> verify: row count within the expected delta;
                                                           byte sizes recorded for rollback
T2  rebuild TXF_RPT_minute.csv                          -> verify: day + night reconcile to the total
T3  re-split day/night                                  -> verify: 0 bars on a dead contract; the
                                                           per-roll holes in 0.2 are gone
T4  diff old vs new tape                                -> verify: a measured table -- bars changed,
                                                           bars added, by roll
T5  re-run the pinned config on the NEW night tape      -> verify: the number that replaces 16.3122
    [D1 = (a): re-measure only. The edit:/keep: chain          (and W4b's, which rests on the same run)
    is NOT re-run; parameters stay, tape changes.]
T6  re-pin NIGHT to the new row (D1)                    -> verify: results.tsv row + dataset vintage;
    [decided; the live-change risk is recorded at 6]           and whether the V5 soak clock restarts
T7  re-run W4b, W5a and W5b on the new tape             -> verify: the DAY rows lose PROVISIONAL.
    [NOT "only then run W5a/W5b" -- those are already          warmup_bars must be RECOMPUTED from a
    done provisionally, see 4. This is the re-measure.]        target timestamp, never carried across
                                                              as a number (0.5)
```

T0–T4 need no GPU. T5 is ~4 minutes (§2.13's instrument).

**Rollback.** `NOTE.md`'s incremental procedure records byte sizes so
`truncate -s <size>` is an exact undo. That does **not** apply to a full rebuild:
**take a copy of all four CSVs first** (~9.5 GB). The existing `.bak` files are
the pre-2026-08-24 vintage and are not a substitute.

---

## 6. Decisions — **both taken 2026-09-01**

- **D1 — does NIGHT get re-tuned now, or run on the old pin while the new tape
  is validated in parallel?** The live deployment is mid-soak with the first
  real roll on 2026-09-15. Re-pinning is a live change; validating in parallel
  costs nothing but leaves two vintages in play for a while.
  *(Recommendation had been: validate in parallel, do not re-pin before the
  roll.)*

  **DECIDED: NIGHT moves to the new tape — one vintage, not two.** The soaking
  deployment is re-pinned to numbers measured on the rebuilt tape rather than
  kept on the old pin. Reason given: the time cost of the testing is the binding
  constraint, and validating two vintages in parallel pays it twice.

  **This overrides the recommendation above, deliberately.** Recorded with the
  risk it accepts, because §4 and `DAY_SESSION_PLAN.md` §2.2 both flagged it and
  nothing else in this file now will:

  - **Re-pinning is a live change to a deployment that is mid-soak**, with the
    first real roll on **2026-09-15**. `SHIOAJI_PLAN.md`'s V5 soak terms were
    measured against the *current* pin; changing the configuration under a
    running soak is the kind of event that ordinarily restarts its clock. Decide
    explicitly whether it does, before re-pinning — not after.
  - **The old pin does not disappear.** `results.tsv` keeps `e44a047` at Calmar
    13.6742 on `TXF_RPT_minute_night.csv@2026-05-15`; the new row is a new row on
    a new vintage. One vintage is the *forward* policy, not a rewrite of the
    ledger.

  **Scope settled 2026-09-01: (a) — re-measure, do not re-search.** "Re-tuned"
  could have meant either, and they differ by orders of magnitude:

  | | what it costs | chosen |
  |---|---|---|
  | **(a) re-measure and re-pin** — run the *existing* pinned parameters on the rebuilt tape, record the row, re-pin to it | one ~5-minute run | **yes** |
  | (b) re-tune — re-run the whole `edit:`/`keep:` search on the rebuilt tape | GPU-days | no |

  So T5 is one run of the pinned configuration, T6 re-pins to its row, and the
  `edit:`/`keep:` chain is **not** re-run. The parameters stay as they are; only
  the tape underneath them changes. Consistent with the reason D1 was decided the
  way it was — testing time is the binding constraint.

- **D2 — does `TXF_RPT_minute.csv` (combined) survive?** With policy-dependent
  splice points it has no single correct rule. Either it is retired, or it is
  rebuilt on one named policy and documented as such.
  *(Recommendation had been: retire it for tuning.)*

  **DECIDED: it survives, reserved for a future third deployment that trades
  DAY and NIGHT as one session.** The intended end state is three deployments —
  **day / night / full-day** — and the combined tape is the full-day one's
  evaluated series.

  **This resolves the "no single correct rule" objection rather than overriding
  it**, and the rule is already in the code. §2.1 splices at
  `roll_at(month)` — *the instant the deployment actually rolls* — and a
  `SessionPolicy.both()` deployment has one. Measured:

  | policy | sessions | bars/session | `roll_at_time()` | `cache_slug()` | warm-up days |
  |---|---|---|---|---|---|
  | night | NIGHT | 840 | 15:30 | `night` | 30 |
  | day | DAY | 300 | 12:45 | `day` | 84 |
  | **both** | DAY+NIGHT | **1 140** | **12:45** | `day_night` | **22** |

  So the combined tape is rebuilt at **12:45**, the **same instant as the day
  tape**. §2.3's "two tapes, two splice points" becomes *three tapes, two splice
  points*: day and combined at 12:45, night at 15:30. No new splice rule is
  needed — but see §2.3, none of the three is a session-split of another.

  **`roll_at_time()` was amended for this on 2026-09-01**: it tested
  `sessions == {"DAY"}`, which handed a `both()` policy NIGHT's 15:30, and now
  tests `"DAY" in sessions`. A full-day deployment left on 15:30 would be
  scheduled past the last moment it can still reach the expiring contract, which
  stops trading at 13:30 on the clearance day — the position would expire rather
  than be rolled. Full reasoning and the twelve-roll timetable are in
  `DAY_SESSION_PLAN.md` C4, "EXTENDED 2026-09-01"; 5 new tests.

  **What a third deployment would still need — none of it done, none of it in
  this plan's scope.** Listed so the decision's cost is visible now rather than
  discovered later:

  - `results.tsv`'s session column admits only `NIGHT` / `DAY`
    (`tests/test_live_config.py`'s `VALID_SESSIONS`). A full-day row needs a
    third value, and `_best_row`'s scoping then extends to it for free.
  - **Two guards assert a pinned config trades exactly one session** —
    `_session_of` and `test_every_registered_config_trades_exactly_one_session`.
    Both are deliberate (§C1: on Calmar the sessions are not comparable, so an
    unscoped "best" would let one deployment re-pin another) and both would need
    re-stating for a policy whose *single deployment* legitimately spans two
    session labels. That is a real design question, not a line to delete.
  - `forced_close_on_session_end` stops being moot. For day and night it is
    `False` because the session boundary is the deployment boundary; a full-day
    deployment crosses DAY↔NIGHT *inside* one position's life, which is exactly
    what C8 is about and what `run.py`'s force-close latch already implements.
  - It needs its own tuned configuration. Neither `PINNED` nor `PINNED_DAY`
    transfers — W5a measured that going one direction (Calmar 16.3122 → 0.2671),
    and a 1 140-bar session is a third regime again.

---

## 7. Not covered here

Whether the strategy's *numbers* survive the rebuild — that is T5's output and
is not predictable from the diff. Nor any change to `ROLL_LEAD_DAYS` or to
`front_month_at`'s code: both stay as they are. This plan changes the tape to
match the code, deliberately, because the code's rule is the one the live
deployment can actually execute.
