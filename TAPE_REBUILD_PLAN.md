# Evaluated-Tape Rebuild — Plan

**DRAFT v1, 2026-08-25.** Written after `DAY_SESSION_PLAN.md` §4 C4a found that
`front_month_at` and the evaluated tape splice delivery months at different
instants. Investigating that turned up a larger defect in the tape itself.

~~**Nothing has been built and no rebuild has been run.**~~ **Superseded — the
rebuild ran on 2026-09-01 and T0–T7 are all complete.** Two decisions in §6 were
the user's; both were taken. **See "Status" immediately below** — the numbered
updates that follow it are a chronological log, and several of them state things
("T6 is not started") that were true when written and are not true now.

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

**Updated 2026-09-01 (T1) — the rebuild changes THREE things, not one.** §3's
"the code change is small" holds for the front-month rule and for nothing else.
Building the intermediate surfaced two further defects in the shipped tape, both
of them silent, both corrected by the rebuild rather than caused by it: the
minute **open/close depend on an unstable sort**, and **volume is doubled**
wherever a raw file carries a stale copy of an earlier session. See **§0.6** —
and note that §3.1's blast-radius estimate covers only the first cause.

**Re-verified 2026-09-01 — still true, and both defects are still in the file.**
The day tape was re-measured end-to-end against §0; the numbers confirm it, one
new finding is added, and one cross-plan claim in §1 turns out not to hold as
written. See **§0.5**.

**Updated 2026-09-01 (T2/T3) — the three tapes are rebuilt and in place.**
`dataset/RPT/TXF_RPT_minute_{day,night}.csv` and `TXF_RPT_minute.csv` are now the
new vintage; the pre-rebuild bytes are in `_prerebuild_backup_20260901/`,
verified identical before the overwrite. **Everything measured on the old tape is
now measured on a file that no longer exists at that path** — `results.tsv`'s
Calmar 13.6742 and W4b's 16.3122 included. T4 (the attributed diff) and T5 (the
re-measure) are what make the new numbers readable; until then the plan's own
figures above describe the *backup*, not `dataset/RPT/`. One choice §2 had left
open had to be made to build at all — see **§2.4**.

**Updated 2026-09-01 (T4) — the diff is measured and attributed; T0–T4 are
done.** Four causes, not three: the rebuild also drops 1 297 out-of-session rows.
The attribution is exact rather than estimated, and the headline number is not
the one to quote — on the night tape `close` moves on 8.00 % of shared minutes
and **about half of that is Defect C, not the splice**; on the day tape 69 % of
the 18.17 % is. §3.1's estimate is corrected in place, and
[docs/tools/diff_tapes.md](docs/tools/diff_tapes.md) holds the full table.
**T5 next — one ~5-minute run of the pinned config on the new night tape.**

**Updated 2026-09-01 (T5) — the number that replaces 16.3122 is 3.6006, and it
comes with a confound T5 cannot resolve.** Same tree, same eval window, warmup
recomputed from the target timestamp (520 004 → 538 790); a control run on the
pre-rebuild tape reproduced 16.3122 exactly, so the change is the tape and not
tree drift. But the forecast grid necessarily re-phased, and W4b measured phase
*alone* moving Calmar 13.6742 → 8.7651. **§5.1** has the full table and says what
that does and does not license. **T6 was not started at the time of writing** —
re-pinning a mid-soak deployment to 3.6006 is a decision that run did not make on
its own. (It was taken later the same day; see T6 and the Status block.)

**Status of the proposal:** the user proposed re-making the tape on a new rule
rather than changing the code to match the old one. That direction is right —
§1 is the evidence — but the specific instant proposed needs adjusting (§2.2).

---

## Status — 2026-09-01, end of the rebuild

**Every step T0–T7 is done.** The tape is rebuilt, the diff is attributed, NIGHT
is re-pinned, and the DAY numbers are re-measured. What is left is **not process
in this plan** — it is four hand-offs, listed here so they are not mistaken for
unfinished rebuild work.

| | what | where it is written up |
|---|---|---|
| **1** | **The T7 ledger edits, deliberately not applied.** Three DAY rows on `...@2026-09-01`, the two `PROVISIONAL` markers resolved, `DAY_SESSION_PLAN.md` §2.3's numbers, and `PINNED_DAY` re-pointed to Calmar **1.6937** on the new vintage (same cell — re-measure, not re-select). Left to the user at their instruction. | §5.2, "Decided … deliberately NOT applied here" |
| **2** | **The phase confound is unresolved.** 16.3122 → 3.6006 is the tape correction *and* a fresh forecast-grid phase draw, in unknown proportion. A sweep at `538 790 + k` would separate them (~4.5 min a run). Offered and declined; nothing depends on it, but no one should quote 3.6006 as "the tape cost 4.5×" until it is done. | §5.1 |
| **3** | **Two findings this rebuild produced that belong to other plans.** (a) The live path fills ~47 minutes a roll from the *outgoing* contract at a basis-sized offset, silently, into the warm-up context — `SHIOAJI_PLAN.md` records nothing about it. (b) Neither deployment beats buy-and-hold on the corrected tape; NIGHT went **+13 258.65 → −9 204.96**. | §2.4 and §5.2 |
| **4** | **A full-day deployment still needs everything in D2's list.** The rebuild closed exactly one item on it — the tape, now `TXF_RPT_minute_fullday.csv` (§2.5). The `results.tsv` session column, the two one-session guards, `forced_close_on_session_end`, and a tuned configuration are all untouched. | §6 D2 |

**One thing became checkable that was not before.** §0.5 records a *tape-spanning*
parity check — replaying the real evaluated series across a roll against the live
loop — as impossible while the two series structurally differed at every roll.
They no longer do. That check is now possible and has still not been run; it is
the most direct remaining test of what this whole plan was for.

**Not committed as of this writing:** the T7 tooling (`tools/band_sweep.py` and
its docs page), the full-day tape work, and the documentation edits above.

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

### 0.6 Two more defects, found while building T1's intermediate

Neither is the front-month rule, neither was known when this plan was written,
and both are **corrected** by the rebuild. They are recorded because they change
what T4's diff means: a bar that moves between the old tape and the new one now
has three possible causes, not one.

#### Defect C — the minute open/close rest on an unstable sort

`close` is the only OHLC column the backtest reads (`real_data.load_minute_bars`
projects `price = df["close"]`), and **~14 % of closes change** on a faithful
rebuild — independently of any front-month decision.

Measured: the raw carries **no sub-second precision at all**. Files before ~2015
are `HHMMSSCC` with `CC` always `00`; later files are plain `HHMMSS`. So several
prints share a timestamp key, and which of them is a minute's first or last is
decided purely by row order. Notebook cell 11 sorts with `sort_index()`, whose
default `quicksort` is **not stable**, over rows whose order had already been
disturbed by cell 7's `merge`. The answer is therefore arbitrary and not
reproducible — there is no way to rebuild the shipped tape's open/close, and no
way to isolate the front-month change by holding them fixed.

The rebuild stable-sorts and preserves raw file order, which is the exchange's
dissemination sequence and the only ordering information that exists. Verified
the raw is already time-ordered: 4 out-of-order transitions per file, all of them
the boundaries between its three trading-date blocks.

`high` and `low` are **identical** either way (0 % differ, both tapes) — they are
order-independent, which is exactly why this hid.

| | full span, 2 933 058 bars (T1) | recent window only |
|---|---|---|
| `close` differs | **7.59 %** | 14.19 % |
| `open` differs | 16.42 % | 25.56 % |
| `high` / `low` differ | **0 %** | 0 % |
| delta median / p95 / max | — | 1 pt / 9 pts / 54 pts |

**Confirmed as ordering, not data:** on all 222 655 bars where `close` differs,
**100.0000 %** of both the shipped and the rebuilt value lie inside that bar'''s own
high/low. The print set is identical; only which print is last moved. (The rate
is roughly twice as high in recent years as across the whole span — the tape
gets denser, so more prints share a second.)

#### Defect D — duplicated source files, doubled volume, and a lost session

A few raw files carry a **stale copy of an earlier session instead of their
own**. `Daily_2026_07_13.rpt` repeats 2026-07-09's night block — 29 899 TX
prints, byte-identical to the copy in `Daily_2026_07_10.rpt`. The notebook
concatenated both and summed `quantity`, so the shipped tape reads **1 596 lots
at 2026-07-09 15:00 where the source has 798**.

It hid for the same reason Defect C did: duplicating identical prints cannot move
a min or a max, so `high` and `low` are untouched and only `volume` betrays it.

**And the session the stale copy displaced is simply gone.** 2026-07-10's night
session appears in no file at all. `dataset/NOTE.md` records "~108 evening blocks
(15:00–23:00) are absent" and attributes them to the front-month roll; this is a
**second cause** for that symptom, and it is not fixable from the data on hand —
those prints are not in `extracted_data/`.

**Frequency, measured across the full span in T1** (the 2026-only scan that found
this understated it): **12 dates** affected, the earliest **2019-09-27**, and
**8 853 duplicate bars** dropped from 4 560 212. Some sessions appear in *three*
files, not two — the shipped/rebuilt volume ratio runs **exactly 2.000 (median)
to 3.000**, with 66.7 % of affected bars doubled and the rest tripled. Integer
multiples are the proof it is whole-file duplication: a partial overlap would
give fractional ratios.

Dropped by the T1 builder, which asserts the duplicates are byte-identical first
so a genuine disagreement between two files would surface rather than be resolved
by `keep="first"`. That assertion held over the whole rebuild: 15 505 rows in
duplicate groups, no field disagreeing.

> **`dataset/NOTE.md` should gain both of these**, and its "Known gaps" section
> in particular now has an incomplete root cause. Not edited from here — it sits
> outside `forecast_eval/`.

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

**AMENDED 2026-09-01 — there are four files, because the combined tape cannot be
both the record and the tuning window.** See **§2.5**: `TXF_RPT_minute.csv` keeps
every in-session minute back to 2011, and `TXF_RPT_minute_fullday.csv` is the
slice from 2018-01-01 that a full-day deployment can actually be tuned on. Same
policy, same 12:45 splice, same rows on every shared minute — only the window
differs.

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

### 2.4 The fallback — decided 2026-09-01, in T2, because it was not decided here

§2.1 and §2.2 fix *when* the tape splices. They say nothing about a minute in
which the front month **did not print at all**, and that turned out to be a
separate choice with its own blast radius. §1 quotes `collapse_front_month`'s
answer — "a minute the front month did not print is better filled from the other
month than dropped" — as though adopting the live rule settled it. It does not,
for two reasons found by measuring.

**First, "the other month" is not symmetric.** `fetch_bars_continuous` fetches
"the front month plus whatever earlier months are still listed", plus the cache
that carries a delisted one. It **cannot reach the far month**. So a tape that
filled from a later month would hold prices the live run has no way to observe —
the same class of divergence §1 exists to close, introduced from the other side.
Measured on the night tape:

| fallback reach | bars filled | dates | |
|---|---|---|---|
| **earlier months only** | **10 240** | 216 — the post-roll windows | **chosen** |
| any other month | 12 288 | 752, all times | 2 048 bars live cannot see |
| none | 0 | — | re-opens the gap at every roll |

**Second, the fallback is not free, and it is not what closes §0.2's holes.**
Those bars carry the inter-month basis (§0.4: 84–325 points), so `|Δclose|` on
them is **median 20 / p95 181** points against **median 2 / p95 12** on ordinary
bars — one-bar spikes, ~47 minutes per roll window, ~9 000 of them across the
tape. Against a band of 0.00045 that is a signal-sized artifact. It is kept
anyway, because the live path produces it and §7's principle is that the tape
adopts the code's rule.

**What it does *not* buy is the holes.** Those close from the splice instant
alone, exactly as §2.1 claimed — the last-trading-day bar counts are **identical**
with and without the fallback (NIGHT median 834, short by 1 612 either way). §1's
sentence implies otherwise and should be read as the parity argument it is, not
as a data-completeness one.

> **This is a live-path finding as much as a tape one.** The ~47 minutes a roll
> where the incoming month is thin are filled, live, from the outgoing contract at
> a basis-sized offset — into the warm-up context, silently. Nothing in
> `SHIOAJI_PLAN.md` records it. Out of this plan's scope; worth its own look.

### 2.5 The full-day tape — a fourth file, added 2026-09-01

D2 reserved `TXF_RPT_minute.csv` as the full-day deployment's evaluated series
and nobody checked what was in it. **A fifth of it is not full-day data.**

**Measured.** TAIFEX had no after-hours session until **2017-05-15**, so the
combined tape's first **468 524 bars (15.6 %)** — all of 2011–2016 — are DAY-only.
There, a "session" is 300 bars where a full-day deployment's is 1 140.

**The night hours did not change; the liquidity did.** The post-midnight tail
ends at **04:59 in every month from 2017-05 onward**, so the session was always
15:00–05:00. What ramps is how many of those 840 minutes actually print:

| | median night coverage (bars / 840) |
|---|---|
| 2017-05 (launch month) | 0.479 |
| 2017-06 … 2017-12 | **0.84 – 0.90** |
| 2018-01 | 0.931 |
| **2018-02** | **0.989** — first month at the plateau |
| 2018-03 onward | 0.95 – 1.00 |

So "full-day data" has two honest answers: it **exists** from 2017-05-15, and it
becomes **representative** in 2018-02.

**The cut is 2018-01-01**, a round boundary that excludes the whole 2017 ramp.
January 2018 (0.931) is still short of the plateau and is kept anyway — one
transitional month out of 8.6 years is a smaller distortion than an
unexplainable start date. `FULLDAY_START` in
[tools/build_session_tapes.py](tools/build_session_tapes.py) is the one place to
change it: 2018-02-01 for the strict reading, 2017-05-15 to take every full-day
minute that exists.

| | |
|---|---|
| file | `dataset/RPT/TXF_RPT_minute_fullday.csv` |
| bars | **2 364 417** (78.9 % of the combined tape) |
| span | 2018-01-02 08:45 → 2026-08-21 13:44 |
| splice | **12:45**, `SessionPolicy.both()` — identical to `TXF_RPT_minute.csv` |
| bytes | 154 418 391 |

**Why not just truncate the combined tape.** The two files answer different
questions and one file cannot answer both. `TXF_RPT_minute.csv` is the *record* —
every in-session minute the source has, and the DAY deployment's history
genuinely reaches back to 2011, so truncating would discard six years of real
data. `TXF_RPT_minute_fullday.csv` is the *tuning window*.

**It is a window, never a second splice rule.** The builder truncates *after* the
front-month selection and asserts on every run that the result equals the
combined tape's suffix bar-for-bar — verified `True`. A start date that quietly
changed which contract a minute carries would be exactly the class of hidden
second rule this whole plan exists to remove.

> **This does not make a full-day deployment ready.** §6 D2's "what a third
> deployment would still need" list is unchanged and none of it is done — the
> `results.tsv` session column, the two one-session guards, `forced_close`, and a
> tuned configuration of its own. What changes is only that the *data* it would
> be tuned on now exists as a named file instead of as a trap inside another one.

---

## 3. Scope — what has to be rebuilt

**The 9.2 GB tick file is downstream of the defect.** `process_txf.ipynb` applies
the front-month filter at cell 7 and writes `TXF_RPT_ticks.csv` at cell 9, so
that file already has the wrong months in it. A rebuild cannot start from it.

**REVISED 2026-09-01 — the front-month selection moves *after* the minute
aggregation, and the tick file is not rebuilt at all.** The pipeline below front-
filters at cell 7, which fixes **one** splice instant into everything downstream.
§2.3 needs **two** (day and combined at 12:45, night at 15:30) and none of the
three tapes is a session-split of another, so a single front-filtered series
cannot produce them. The expensive pass therefore stops *before* choosing a front
month:

```
dataset/RPT/TaifexRPT/**.zip        (raw, all months)     <- the only clean source
  -> extracted_data/Daily_*.rpt     (3 832 files, ~55 GB)
  -> tools/build_minute_bycontract.py        <- ONE pass, no front-month choice
       TX only, spreads dropped, two nearest months kept,
       minute OHLCV per (contract, minute), deduped
  -> TXF_RPT_minute_bycontract.csv  (~9 M bars)           <- NEW intermediate
       |
       +- front_month_series(ts, day())   -> TXF_RPT_minute_day.csv     12:45
       +- front_month_series(ts, both())  -> TXF_RPT_minute.csv         12:45
       +- front_month_series(ts, night()) -> TXF_RPT_minute_night.csv   15:30
```

**`TXF_RPT_ticks.csv` is not rebuilt.** `NOTE.md` calls it an intermediate — "the
minute files are what analysis code reads" — and nothing in the repo reads it.
Rebuilding it would cost 8.6 GB and could still only carry one splice instant.
The pre-rebuild copy is kept in `_prerebuild_backup_20260901/`.

> **And it left `RPT/` entirely on 2026-09-01.** Once the session tapes no longer
> derive from it, a 9.19 GB file that nothing opens and that still carries the
> old front-month rule is a liability rather than an asset — the next person to
> find it would have no way to tell from the filename that it is the *un*fixed
> series. It is byte-identical inside `_prerebuild_backup_20260901/`
> (`sha256 9b8ec441…`), which is the right place for it: an archive, labelled as
> one. `dataset/NOTE.md` keeps its documented quirks, which are the evidence
> behind §0.6's Defect C.

**Why two nearest months rather than all of them:** the front-month choice belongs
to the splice rule, and any rule between two adjacent months needs both present.
Measured on a roll date, the two nearest are **86 903 of 87 007** TX prints
(99.88 %); the rest are far months nothing trades.

The gain is that a **fourth deployment later is a filter, not a 55 GB rebuild** —
which matters now that D2 anticipates one.

### 3.1 Estimated blast radius on the night tape

| | |
|---|---|
| bars re-attributed to the other month | ~112 000 (**6.3 %**) |
| bars newly filled that are missing today | ~60 000 |

**This invalidates the pinned config.** Calmar 13.6742, and W4b's re-baselined
16.3122, were both measured on the current tape. A 6 % re-attribution plus
60 000 new bars is a different series; the config has to be re-tuned against it,
and `results.tsv` needs a new `dataset` vintage (the column already exists — W0).

> **MEASURED 2026-09-01 (T4) — the estimate was half right, and the table above
> should now be read as superseded.** `tools/diff_tapes.py` measured the night
> tape directly:
>
> | | estimated | **measured** |
> |---|---|---|
> | bars newly filled | ~60 000 | **61 176** — right |
> | bars re-attributed | ~112 000 (6.3 %) | **73 895 (4.00 %)** — overstated by ~½ |
> | bars removed | not predicted | **82**, all out-of-session |
>
> And the thing the estimate could not have said, because C and D were not yet
> known: on the night tape `close` moves on **8.00 %** of the minutes both tapes
> carry, and **only about half of that is the splice** — 70 636 bars are the
> front-month rule, 69 519 are Defect C alone, 2 784 are both. On the **day** tape
> the split is worse: 18.17 % of closes move and **69 % of them are Defect C**,
> which would have moved on any faithful rebuild whatever the splice rule was.
>
> The magnitudes separate the two cleanly and are worth carrying into T5: Defect C
> moves a close by a median of **1 point**, the front-month rule by **31–43**.
> `dclose` distributions and the per-roll table are in
> [docs/tools/diff_tapes.md](docs/tools/diff_tapes.md).

> **Understated, 2026-09-01.** These figures are the front-month rule alone. §0.6
> adds two more causes measured since: **~14 % of `close` values** move on a
> faithful rebuild (Defect C, the unstable sort), and volume is corrected
> wherever Defect D doubled it. `close` is the only OHLC column the backtest
> reads, so the series changes by more than this table implies — and the pinned
> config is invalidated more thoroughly, not less. D1 = (a) still holds: T5
> re-measures the existing parameters rather than re-searching, and the number it
> produces reflects all three causes together, which is why **T4 must attribute
> them separately** or T5's delta cannot be read.

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
T0  [DONE 2026-09-01, commit 8ed6add] pin the new rule  -> VERIFIED: roll_calendar / front_month_series
    as one function, with tests (shared by the tape         in live/roll.py, asserted against the scalar
    builder and front_month_at)                             front_month_at over the tape's whole span;
    [marker added retrospectively -- the step was            covered in tests/test_live_roll.py. The
    committed but never ticked here]                         dead-contract half was verified in T2 (0 in
                                                            all four tapes).
T1  [DONE 2026-09-01] build                             -> VERIFIED: applying the OLD rule to the
    TXF_RPT_minute_bycontract.csv from extracted_data/      intermediate reproduces the shipped tape's
    -- one pass, NO front-month choice.                     2 933 058 bars EXACTLY: 0 missing, 0 extra.
    [REVISED: not the tick file, see 3]                     high/low differ 0%; close 7.59% and open
    4 551 359 bars, 190 contracts, 0.27 GB, 28 min.         16.42% (Defect C -- 100% of both values sit
    8 853 duplicates dropped, byte-identical asserted.      inside the bar's own high/low, so ordering
    Backup at _prerebuild_backup_20260901/.                 only); volume 0.172% (Defect D -- shipped
                                                           /rebuilt ratio exactly 2.000-3.000).
T2  [DONE 2026-09-01] apply front_month_series per      -> VERIFIED: 0 bars on a dead contract in all
    policy -> the three tapes [12:45, 12:45, 15:30].        three (the OLD day tape scores 1 141 under
    day 1 146 712, night 1 849 007, combined 2 995 727.     the same check). Roll COUNT unchanged -- 112
    Built by tools/build_session_tapes.py, 25 s.            night, 188 day, exactly 0's figures; only the
    The fallback reaches earlier months only (2.4).         instants moved. day+night and combined agree
                                                           on every timestamp but 8 and differ in
                                                           delivery month on 3 143 bars, ALL of them
                                                           15:00-15:29 on a roll date -- 2.3's
                                                           prediction, measured. All three load through
                                                           real_data.load_minute_bars, 0 rows lost.
T3  [DONE 2026-09-01 -- folded into T2; the split IS    -> VERIFIED: the 0.2 holes are gone. DAY on the
    the front-month selection now, not a separate          183 last-trading-day sessions: median 285 ->
    pass]                                                  300, short by 2 723 bars -> 0. NIGHT on the
                                                           110: median 297 -> 834 of 840; the residual
                                                           1 612 are minutes with no print in ANY kept
                                                           contract, not a rule artifact.
T4  [DONE 2026-09-01] diff old vs new tape, attributed. -> VERIFIED, and the attribution is EXACT, not
    tools/diff_tapes.py. FOUR causes, not three -- the       estimated: replaying the OLD rule on the
    rebuild also drops 1 297 out-of-session rows (B).        intermediate reproduces OLD's index AND
    NIGHT: +61 094 bars; 61 176 added, 73 895                contract column exactly (T1's finding, the
    re-attributed (4.00%), 82 dropped; close moved on        load-bearing one), and 0 fields differ on
    8.00% of shared minutes -- HALF of that is Defect C.     any minute whose contract did not change.
    DAY: close moved on 18.17%, but 69% of it is C           high/low differ 0% on all three. Full table
    alone. See 3.1's correction and docs/tools/.             in docs/tools/diff_tapes.md.
T5  [DONE 2026-09-01] re-run the pinned config on the   -> MEASURED: Calmar 16.3122 -> 3.6006. Same tree,
    NEW night tape. warmup RECOMPUTED from the target      same commit, same eval window; net +46 603 ->
    timestamp: 520 004 -> 538 790 (0.5 -- carrying the     +24 136, max dd -2 857 -> -6 703, 208 -> 235
    old number would have started the eval window          trades. A CONTROL run on the pre-rebuild tape
    2020-01-31, 34 days early).                            at warmup 520 004 reproduced 16.3122 EXACTLY
    [D1 = (a): re-measure only. The edit:/keep: chain       (+46 603.2099 / -2 856.9506), so the drop is
    is NOT re-run; parameters stay, tape changes.]          the tape, not tree drift. BUT SEE 5.1 -- the
                                                           forecast grid necessarily re-phased, and W4b
                                                           measured phase ALONE moving 13.6742 -> 8.7651.
T6  [DONE 2026-09-01] re-pin NIGHT to the new row (D1)  -> VERIFIED: results.tsv row 4282d33 on
    [decided; the live-change risk is recorded at 6]       TXF_RPT_minute_night.csv@2026-09-01,
                                                           warmup 538790, Calmar 3.6006, keep.
                                                           PINNED re-pointed; e44a047 stays as history.
                                                           The drift guards now scope "best known row"
                                                           by SESSION AND TAPE VINTAGE -- without that
                                                           a Calmar no file on disk can reproduce
                                                           outranks the tape actually shipped.
                                                        -> V5 SOAK CLOCK: does NOT restart, decided
                                                           explicitly (6 requires it). Re-pinning
                                                           ordinarily would, but only because it
                                                           ordinarily changes what the loop executes.
                                                           Here NO executable field moved -- the T5
                                                           run's params.json matches PINNED field for
                                                           field, and source_* is read by no module
                                                           outside config.py. Identical orders before
                                                           and after, so there is no behaviour to
                                                           re-soak; V4's 3 fill-gap samples stay valid.
T7  [MEASURED 2026-09-01] re-run W4b, W5a and W5b on    -> W4b: satisfied by T5 -- the night baseline IS
    the new tape.                                          that run (16.3122 -> 3.6006). Not re-run.
    [NOT "only then run W5a/W5b" -- those are already    -> W5a: 0.2671 -> 0.4129 (230 trades). Night's
    done provisionally, see 4. This is the re-measure.]     config still does NOT transfer, but the gap
    DAY warmup RECOMPUTED 674 498 -> 676 123, holding      narrowed from 61x to 8.7x -- because NIGHT
    the eval window at 2020-03-11 08:49 (carrying the      fell, not because DAY rose.
    number would have started 2020-03-03).              -> W5b: the RIDGE survives, the LEVEL does not.
    W5b re-run by tools/band_sweep.py -- W5b's own          band 0.001-0.0015 x min_prob 0.5-0.65 is
    harness was ad hoc and did not survive as a file.       still where the non-degenerate cells sit;
    Three cells CLI-confirmed against the sweep.           worst-half 3.1557 -> 0.9967 for the pinned
                                                           cell. spearman(h1,h2) 0.45 -> 0.39.
                                                        -> NEW: the worst-half rule is INSUFFICIENT as
                                                           written -- see 5.2. And neither deployment
                                                           now beats buy-and-hold (5.2).
```

T0–T4 need no GPU. T5 is ~4 minutes (§2.13's instrument).

### 5.1 T5's result, and the confound it cannot resolve by itself

Both runs are on one tree, one commit, one environment, the same calendar eval
window (**2020-03-05 18:02 → 2026-08-21 04:59**), and the same parameters —
`--buy-threshold 0.00045 --sell-threshold -0.00045 --min-prob 0.5`, everything
else at the `PINNED` defaults.

| | OLD tape, warmup **520 004** | NEW tape, warmup **538 790** |
|---|---|---|
| in-session bars | 1 787 831 | 1 849 007 |
| forecasts | 42 261 | 43 674 |
| trades | 208 | **235** |
| net PnL (pts) | +46 603.2099 | **+24 135.5960** |
| max drawdown (pts) | −2 856.9506 | **−6 703.2441** |
| fee drag | 0.016 | 0.057 |
| **Calmar** | **16.3122** | **3.6006** |
| vs buy-and-hold | +11 835.43 | **−10 638.87** |

**The control is exact.** Re-running the pre-rebuild tape at its own warmup
reproduces W4b's 16.3122 to the digit (+46 603.2099 / −2 856.950550, 208 trades),
so nothing between 2026-08-24 and now moved the number. The change is the tape.

**The warmup had to be recomputed, and §0.5 was right about why.** 520 004 names
the in-session bar the eval window starts on. On the rebuilt tape that index is
**2020-01-31 23:19** — 34 days early — because ~18 786 bars were inserted *before*
the split. The target timestamp `2020-03-05 18:02` sits at **538 790**, and that
is what was used. Carrying the number across would have compared two different
calendar windows and called the difference the tape.

> **What T5 CANNOT tell apart, and T6 must not ignore.** Forecasts fire at
> `warmup + k·stride` in **bar index**, and the rebuild inserted bars *inside* the
> eval window as well as before it. So the forecast grid is necessarily re-phased:
> the two runs sample different minutes, and a bar-for-bar comparison is not
> available at any warmup. `DAY_SESSION_PLAN.md` §2.2 measured what that alone is
> worth — **Calmar 13.6742 → 8.7651 with nothing changed but the grid phase**, and
> concluded "the pinned 13.6742 is one draw from a distribution at least this
> wide."
>
> So **16.3122 → 3.6006 is the tape correction *and* a fresh phase draw, and this
> run cannot say in what proportion.** It is not evidence that the corrected tape
> costs 4.5× — nor evidence that it does not. Quantifying it is a phase sweep on
> the new tape (a handful of runs at `538 790 + k`, ~4.5 min each), which is a
> *robustness* measurement of fixed parameters, not the `edit:`/`keep:` search D1
> ruled out.

**This bears on D1.** D1 chose (a) re-measure over (b) re-tune because testing
time was the binding constraint, on the implicit expectation that the pinned
parameters would still be worth pinning. A Calmar of 3.6006 that also trails
buy-and-hold by 10 638 points is the case that expectation did not cover. Whether
that reopens D1 is the user's call and is **not assumed here**. (Written before
T6; the user chose to re-pin as measured rather than reopen D1.)

### 5.2 T7's results, and two things the rebuild exposed

DAY warmup recomputed from its target timestamp, **674 498 → 676 123**, holding
the eval window at 2020-03-11 08:49 → 2026-08-21 13:44. Carrying the number
across would have started it 2020-03-03, eight days early.

| | provisional (old tape) | **re-measured (new tape)** |
|---|---|---|
| NIGHT baseline (W4b) | 16.3122 | **3.6006** — this is T5, not re-run |
| W5a transfer probe | 0.2671 | **0.4129** (230 trades, net +5 973.62, dd −14 467.83) |
| W5b pinned cell `0.001/0.65` | 4.6805 | **1.6937** (67 trades, net +9 748.01, dd −5 755.50) |
| W5b best non-degenerate | `0.001/0.65`, worst 3.1557 | **`0.00125/0.65`, worst 1.1664**, full 1.8859 |
| spearman(h1, h2) | 0.45 | 0.39 |

**W5a's verdict stands, but its arithmetic inverted.** Night's configuration
still does not transfer to day — 0.4129 against 3.6006 — but the gap fell from
**61× to 8.7×**, and it fell because **NIGHT dropped**, not because DAY improved.
W5a's own reasoning is untouched: it argued the probe's verdict is "search, not
futile" from *forecast quality being equally poor on both sides*, and that
remains true (day IC −0.0040 / hit rate 0.4967).

**W5b's ridge survives, its level does not.** The stable region is still
`band 0.001–0.0015 × min_prob 0.5–0.65`, and the best cell is adjacent to the old
pick. Every number in it is about a third of what it was.

> **The worst-half selection rule is insufficient as written, and only the
> rebuilt tape showed it.** On the old tape the degenerate corner won the
> *full-window* ranking, so ranking on the worse half was enough to exclude it.
> On the rebuilt tape the top cell **by worst half** is `band=0.005, min_prob=0.8`
> — **9 trades**, 5 and 4 across the halves, with an **infinite** h2 Calmar
> because a 4-trade half never drew down. W5b supplied a minimum-trades floor by
> judgment and never wrote it down; `tools/band_sweep.py` now states its `n >= 40`
> filter instead of implying it. This is the same failure W5b diagnosed, one
> level up: a selection rule that works because of where the artefacts happened
> to land is not a rule.

> **Neither deployment beats buy-and-hold on the rebuilt tape.** Like-for-like
> over each eval window, which is the comparison W5b insisted on:
>
> | | strategy | buy-and-hold | delta |
> |---|---|---|---|
> | NIGHT (T5) | +24 135.60 | +33 340.56 | **−9 204.96** (was **+13 258.65**) |
> | DAY `0.001/0.65` | +9 748.01 | +34 166.58 | **−24 418.57** (was −9 272.64) |
>
> Night's "+13 259 over the floor" was one of the strongest claims in the
> ledger, and on the corrected tape it is negative. Read it with §5.1's confound
> — part of that swing is the forecast-grid re-phase, not the tape — but the
> direction is not something a phase draw was expected to do.

#### Decided 2026-09-01, deliberately NOT applied here

**`PINNED_DAY` keeps `band 0.001 / min_prob 0.65` and moves to the new vintage** —
re-measure, not re-select, the same discipline D1 chose for NIGHT. Its
`source_calmar` becomes **1.6937** on `TXF_RPT_minute_day.csv@2026-09-01`. The
`0.00125/0.65` cell is the better one on this tape and is recorded as evidence,
but pinning it would be a parameter change dressed as a re-measurement.

**The ledger edits and the `live/config.py` change are left to the user**, at
their instruction — this section is the record, not the act. What is still owed:

- three DAY rows on `...@2026-09-01`, warmup **676 123**: the W5a probe
  (`keep`, 0.4129), the re-measured pin (`keep`, 1.6937), and the new degenerate
  counter-example `0.005/0.8` (`reject`, full 37.6428 on **9 trades**) — which
  earns its place because it now tops the *worst-half* ranking, not just the
  full-window one;
- the `PROVISIONAL` markers on the two existing DAY rows resolved to point at
  their re-measurements;
- `DAY_SESSION_PLAN.md` §2.3's provisional numbers updated;
- `PINNED_DAY` re-pointed as above.

Every number needed is in the table above, in
[docs/tools/band_sweep.md](docs/tools/band_sweep.md), and in
`outputs/day_band_sweep_T7_20260901.csv`. Runs:
`outputs/real_data_day_probe_W5a_T7_20260901/`,
`real_data_day_W5b_pinned_T7_20260901/`,
`real_data_day_W5b_candidate_T7_20260901/`.

---

**Rollback.** `NOTE.md`'s incremental procedure records byte sizes so
`truncate -s <size>` is an exact undo. That does **not** apply to a full rebuild:
**take a copy of all four CSVs first** (~9.5 GB). Done — the pre-rebuild bytes
are in `dataset/RPT/_prerebuild_backup_20260901/`, hash-verified identical before
the overwrite.

> **The `.bak` files this paragraph used to point at were deleted 2026-09-01.**
> They were the pre-2026-08-24 vintage and were never a substitute for the
> backup; they were also byte-identical *prefixes* of it, so nothing unique was
> lost. They were, however, the `@2026-05-15` vintage that **13 `results.tsv`
> rows** are measured on, so the truncation points that reconstruct it now live
> in `dataset/NOTE.md` ("The `@2026-05-15` vintage") rather than in the
> filesystem.

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
  - ~~It needs a tape it can actually be tuned on.~~ **Done 2026-09-01 —
    `TXF_RPT_minute_fullday.csv`, §2.5.** This one was not on the list and should
    have been: the combined tape D2 reserved for this deployment spans 2011, and
    its first 15.6 % has no night session at all. That is the only item here the
    rebuild has closed.

---

## 7. Not covered here

Whether the strategy's *numbers* survive the rebuild — that is T5's output and
is not predictable from the diff. Nor any change to `ROLL_LEAD_DAYS` or to
`front_month_at`'s code: both stay as they are. This plan changes the tape to
match the code, deliberately, because the code's rule is the one the live
deployment can actually execute.
