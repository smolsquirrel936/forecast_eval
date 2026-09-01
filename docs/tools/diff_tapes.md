# [tools/diff_tapes.py](../../tools/diff_tapes.py)

**Role:** the old evaluated tape against the rebuilt one, with every changed bar attributed to a named cause.
**Pipeline:** `_prerebuild_backup_20260901/` + `TXF_RPT_minute_bycontract.csv` + `dataset/RPT/` → one printed table.
**Depends on:** [environment.py](../environment.md) (pandas/numpy otherwise)   **Used by:** run by hand — `TAPE_REBUILD_PLAN.md` T4

```powershell
uv run python -m forecast_eval.tools.diff_tapes `
  --bycontract \\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_bycontract.csv `
  --old-dir    \\DESKTOP-SQUIRRE\dataset\RPT\_prerebuild_backup_20260901 `
  --new-dir    \\DESKTOP-SQUIRRE\dataset\RPT
```

Read-only, ~1 minute. Exit code 1 if either invariant below fails.

## Responsibility

`TAPE_REBUILD_PLAN.md` T4 asks for a table of what moved, **attributed** — and
insists on it, because a bare "12 % of closes changed" would be read as the
front-month rule and would be wrong. §0.6 measures ~14 % of recent closes moving
for reasons that have nothing to do with the splice.

Four causes can move a bar:

| | cause | shows up as |
|---|---|---|
| **A** | the front-month rule — the splice instant moved from one day after expiry to `roll_at` | bars added, and minutes re-attributed to the other month |
| **B** | out-of-session rows dropped by the rebuild (13:45, 05:00) | bars removed |
| **C** | Defect C — `open`/`close` rested on an unstable sort | `open`/`close` values move |
| **D** | Defect D — duplicated raw files doubled `volume` | `volume` values move |

## How the separation is exact rather than estimated

It rests on T1's finding that replaying the **old** `min(contract)` rule on the
new intermediate reproduces the shipped tape's row index exactly. That gives a
middle term:

```
OLD   the shipped tape        _prerebuild_backup_20260901/
 |      <- C and D            (row set fixed, so only values can move)
MID   old rule replayed on the intermediate      (built by this module)
 |      <- A and B            (one source, one ordering, so only the contract can move a value)
NEW   the rebuilt tape        dataset/RPT/
```

**Two invariants make it airtight, and both are asserted at runtime, not assumed:**

- `MID` reproduces `OLD`'s `datetime` index *and* `contract` column exactly. Without
  this, OLD→MID would mix row changes into a diff that is supposed to be values only.
- **0 fields differ on any minute whose contract did not change** between MID and
  NEW. Without this, C or D would have leaked across the seam and the halves
  would not be separable.

Both hold on all three tapes. `high`/`low` differing on 0 % is the third check —
they are order- and duplicate-independent, so anything there would mean the print
set itself changed.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `old_rule_tape(bars)` | function | the shipped pipeline's `min(contract)` per calendar date, replayed |
| `old_session_split(mid, name)` | function | the shipped DAY/NIGHT split (`08:45 <= t <= 13:45`) |
| `report(old, mid, new, name)` | function | one tape's attributed diff; False if an invariant broke |
| `per_roll(mid, new, tail)` | function | added / removed / re-attributed by delivery month |

## The measured table — 2026-09-01

### Bar counts

| tape | OLD | NEW | Δ |
|---|---|---|---|
| day | 1 145 145 | 1 146 712 | **+1 567** |
| night | 1 787 913 | 1 849 007 | **+61 094** |
| combined | 2 933 058 | 2 995 727 | **+62 669** |

### By cause

| | day | night | combined |
|---|---|---|---|
| **A** bars added | 2 782 | **61 176** | 63 966 |
| **A** bars removed | 0 | 0 | 0 |
| **A** minutes re-attributed | 64 297 (5.61 %) | **73 895 (4.00 %)** | 141 335 (4.72 %) |
| **A** `\|Δclose\|` on those | med 31 / p95 186 / max 537 | med 43 / p95 224 / max 469 | med 38 / p95 216 / max 537 |
| **B** out-of-session dropped | 1 215 | 82 | 1 297 |
| **C** `close` moved | 150 322 | 72 333 | 222 655 (7.59 %) |
| **C** `open` moved | 307 265 | 174 204 | 481 469 |
| **C** `\|Δclose\|` | med 1 / p95 3 / max 40 | med 1 / p95 4 / max 78 | med 1 / p95 3 / max 78 |
| **D** `volume` moved | **0** | 5 033 | 5 033 |
| **D** old/new ratio | — | 2.000–3.000 | 2.000–3.000 |

### The net, and why the attribution was the point

| minutes both tapes carry | day | night | combined |
|---|---|---|---|
| `close` moved | 207 853 (**18.17 %**) | 142 939 (**8.00 %**) | 353 732 (**12.07 %**) |
| — A only (re-attributed) | 57 679 (27.7 %) | 70 636 (49.4 %) | 131 258 (37.1 %) |
| — A and C | 6 045 (2.9 %) | 2 784 (1.9 %) | 9 014 (2.5 %) |
| — **C only (same contract)** | **144 129 (69.3 %)** | 69 519 (48.6 %) | 213 460 (60.3 %) |
| re-attributed, close unchanged | 573 | 475 | 1 063 |

**On the day tape, 69 % of the movement is Defect C** — the unstable sort, which
would have moved those closes on any faithful rebuild whatever the splice rule
was. Reading 18.17 % as "the new splice rule" would overstate it by more than
threefold. On the night tape the two causes are near enough half and half.

Note the magnitudes differ by an order: C moves a close by a median of **1
point**, A by **31–43** — A is a different delivery month, C is which print
inside one minute was last.

### By roll (night)

Uniform, which is what makes it the rule rather than an accident: **112 of 113**
delivery months are touched, ~540 bars added and ~700–800 re-attributed at every
one. The ~540 is §0.2's "~540 missing night bars per roll" arriving from the
other direction.

```
expiry  added  removed  reattributed
202508    549        0           730
202509    556        0           672
202510    545        0           748
202511    542        0           797
202512    540        0           794
202601    545        3           784
202602    541        1           797
202603    537        1             0
202604    552        0           750
202605    544        1           731
202606    539        1           769
202607    541        0           670
202608    544        1           654
202609    544        0           707
totals: added 61 176, removed 82, re-attributed 73 895
```

## What this says about §3.1's estimate

§3.1 predicted, for the night tape, ~112 000 bars re-attributed (6.3 %) and
~60 000 newly filled. **Newly filled was right: 61 176.** Re-attribution was
overstated by about half — the actual figure is **73 895 (4.00 %)**. The estimate
was made before Defects C and D were known, so it also could not have said that
half the night tape's moved closes are the unstable sort rather than the splice.
