# Day-Session Port — Migration Plan

**DRAFT v2, rewritten 2026-08-23 after scanning
`D:\…\forecast_eval_folder\day\forecast_eval`.** The first draft assumed the port
would start from a tree that had the live stack in it. **It does not.** Every
file reference in v1's C-items pointed at modules the day tree has never had, so
§0–§3 below are new and §4–§5 are re-anchored rather than reused.

**Updated 2026-08-24.** The day tree's July work was committed (`7fd3fa6`), a
stray `git init` at `day/` was removed, and **day's environment was rebuilt and
matched to night** — §0's facts, §2's merge shape and §5's M-steps have moved,
and §2.1 is new. M0–M2 are done; the migration itself (M3+) has not started.

**Updated 2026-08-24 (later) — all five decisions taken, and the git facts
re-measured.** The user has settled **D1 = Shape A**, **D2 = probe first**,
**D3 = hold**, **D4 = mixing RPT and `kbars` is acceptable**, and **D5 = day's
July work stays on its branch, with no cross-tree transport of any kind**.

Re-measuring both trees for that update also found that three of the git facts
this plan was built on were wrong or had gone stale, and that the RPT datasets
were rebuilt on 2026-08-24. Those corrections are in §0, §2 and the new §2.2 —
which also records the night baseline re-run on the new tape (**Calmar 16.3122**,
185 trades reproducing bit-identically) and the forecast-grid alignment trap found
on the way.

**Naming.** §5's work steps are now **W0–W9**; **D1–D5** now means only the §6
decisions. The two lists used to share letters, which made the cross-references
between them ambiguous.

**Updated 2026-08-31 — D3's hold is lifted; the migration is proceeding.**
Reasoning: progress had stalled over a week on time-cost testing concerns, and
the user decided to complete the port first and validate DAY and NIGHT in
parallel rather than wait further. §5's `HELD` note is superseded — see there.
W0–W3 and W4b were already done (2026-08-24/25, offline, no market); W4 (C6)
is done as of today. **One thing this decision does not resolve on its own:**
DAY and NIGHT will eventually both place orders through the same Shioaji
simulation account, and nothing before W9 forces that question — see the note
against W9 in §5.

**Updated 2026-09-01 — W5a's blocker written into §5, and two prerequisites
found that are not W-steps.** §5 now carries `TAPE_REBUILD_PLAN.md`'s
sequencing constraint at the point of use; until 2026-09-01 it lived only in
C4a's superseded note, where nothing reading the order of work would find it.
Two further constraints, both measured today and both recorded against §5
rather than as new steps:

- **The GPU is a shared resource with the NIGHT soak, and every live-CLI run
  takes it.** `LiveRuntime.__init__` calls `config.build_forecaster()`
  unconditionally (`live/runtime.py:174`), so a `--broker dryrun` start-up
  loads the model exactly as a soak does — W6's banner check is GPU work, not
  just W5. (**Corrected 2026-09-01:** W7 turned out *not* to be — it drives
  `LiveLoop` directly with V0's deterministic fake forecaster, as V0 does, so
  it never constructs a `LiveRuntime` and never loads the model. It runs in
  ~19 s on CPU.) The NIGHT soak restarts **2026-09-01 14:50**
  (`SHIOAJI_PLAN.md`, "SCHEDULED"), and holds the card until stopped.
- **`results.tsv` rows need a commit that exists.** The tree is 15 modified
  files and 9 untracked paths ahead of `7d1af99`, including whole modules
  (`live/soak_epoch.py`, `live/seed_cache.py`, `tools/`). W5a and W5b both end
  in a pinned row, and the `commit` column is what traces that row back to the
  code that produced it. Committing is therefore a prerequisite of W5, not
  housekeeping.

**Updated 2026-09-01 (later) — W5a and W5b are done, PROVISIONALLY.** The user
directed W5a first, then W5b. Both ran on §5's *provisional* branch — measured
against Calmar 16.3122 while the tape rebuild is still pending — and both
re-measure after `TAPE_REBUILD_PLAN.md` T0–T4 regardless of their verdict. §2.3
is the record of that choice and of the results:

- **W5a: night's configuration does not transfer** — Calmar 0.2671 vs 16.3122,
  drawdown 4× worse on a fifth the PnL, fee drag 35 % vs 2.8 %.
- **But the reason is not "no signal", and that inverts the D2 verdict.** Night's
  `information_coefficient` is −0.0119 against day's −0.0049: *neither* session's
  forecast is directionally accurate. Night's Calmar comes from payoff asymmetry
  produced by the emitter gates — which is exactly what the search tunes. So the
  probe said *search*, and W5b ran.
- **W5b: a day configuration exists** — `band 0.001 + min-prob 0.65`, Calmar
  **4.6805**, 73 trades, 17× the probe. It **does not beat buy-and-hold** (−9 273
  over the eval window, where night is +13 259), and it is **not** a live pin.
- **The selection rule had to change.** Ranking by full-window Calmar — the rule
  every NIGHT `keep:` row was chosen by — picks a 27-trade artefact scoring
  14.7697. Candidates were scored on the two halves of the eval window separately
  and selected on the worst. That rejected cell is in `results.tsv` as `reject`.
- **One follow-up, not acted on:** the night chain has never had this
  out-of-sample check applied to it. §2.3's last note explains why that is the
  user's call and not this plan's.

Also here: `real_data.py` gained **`--warmup-bars`**, without which §2.2's "pin
warmup in bars" could not be expressed; §2.2's two run directories had been
deleted, so **16.3122 was re-measured and reproduced exactly** before anything
was compared against it.

---

## 0. What `day/forecast_eval` actually is — measured, not assumed

**It is this repository's own ancestor, one whole subsystem behind** — but it did
not split where this plan first recorded. **Corrected 2026-08-24:** `2716f49` is
*not* an ancestor of night. Measured:
`git merge-base --is-ancestor 2716f49 night` → **false**. Night branched earlier,
at `aae8473`, and never took the commits that followed it on
`autoresearch_result_1`.

| | |
|---|---|
| Day HEAD | `ed0a1ba` "fix: --data is required…" — **2026-08-24** (was `7fd3fa6`, before that `2716f49`) |
| **True merge base** | **`aae8473`** "Merge branch 'Strategy_2_1_7'" — *not* `2716f49` |
| Night ahead by | **27 commits** from `aae8473` (recorded below as 24, which was measured from the wrong base) |
| Day ahead by | **7 commits** from `aae8473`: `8b9f83c`, `b5c8c5c`, `0f80b80`, `e405adf`, `2716f49`, `7fd3fa6`, `ed0a1ba` |
| Day branch | `autoresearch_result_1` (also carries `Strategy_2_1_7`, `main`) |
| Shared remote | `origin` → `smolsquirrel936/forecast_eval` — but it carries **neither** working branch, see below |
| Working trees | **both clean**; day's 114 tests pass |

**`origin` is not a transport between the trees, and will not become one.** It
holds only `main` (`196f5f0`) and `Strategy_2_1_7`, last pushed **2026-06-10**,
and the repository is **public**. Night's `night` branch has never been pushed,
and neither have day's `7fd3fa6`/`ed0a1ba`.

**DECIDED 2026-08-24 (D5): no cross-tree transport of any kind** — not a push to
GitHub, not a local path remote. The two trees stay independent. A push would
have published 106 new files to a public repo, including the whole `live/`
package, `SHIOAJI_PLAN.md`, `docs/live/` and `results.tsv`'s complete tuning
ledger. (It would not have leaked credentials: `SHIOAJI_API_KEY` /
`SHIOAJI_SECRET_KEY` come from the environment, `person_id` and `account_id` are
digested before they are written — `live/cli.py:116-142` — and `outputs/` is
gitignored at `.gitignore:219` with zero artifacts tracked.)

What night gained in those 27 commits is exactly what this port would need: the
entire `live/` package (25 modules), `results.tsv` and the whole `edit:`/`keep:`
night tuning chain, `SHIOAJI_PLAN.md`, and `docs/live/` (22 pages).

### 0.1 The day tree is not a day-session deployment

The name is doing more work than the contents. Measured:

| Component the port needs | In `day/forecast_eval`? |
|---|---|
| `live/` — broker seam, loop, aggregator, rails, backfill, roll, relogin | **absent entirely** |
| `SessionPolicy` (`live/sessions.py`) | **absent** — the parameter the whole port turns on |
| `live/config.py::PINNED` | **absent** |
| `results.tsv` | **absent** — no tuning ledger, so nothing to pin or diff |
| `SHIOAJI_PLAN.md`, `docs/live/` | **absent** |
| `environment.classify_session`, `DAY_OPEN=08:45` / `NIGHT_OPEN=15:00` | **present** (`environment.py:13-32`) — the primitive `SessionPolicy` is built on |
| Session tagging in `run.py` / `metrics.py` | **present** — the backtest core was already session-aware |

And its research configuration is the *untuned* one, not a day-tuned one:

- `DEFAULT_DATA` is `../dataset/RPT/**TXF_RPT_minute.csv**` — the combined file,
  repo-relative — not `TXF_RPT_minute_day.csv`.
- CLI defaults are checkpoint **313m**, band **±0.0005**, `min_prob=**None**` —
  precisely the trio `live/config.py`'s docstring names as the defaults that
  silently disagree with the tuned configuration.
- Its "baked winning config" (`0f80b80`) comes from the older EXP91 / EXP227
  line, not from the night search that produced the pinned Calmar 13.6742.

**So there is no day-session strategy waiting in that folder.** The gap C1
described in v1 is not smaller than stated; it is the same gap, in a tree that
additionally lacks the machinery to close it.

### 0.2 What the day tree has that exists nowhere else — now committed

**Resolved 2026-08-24.** What was 1 085 uncommitted lines is now commit
`7fd3fa6`, 18 files, **+1 577 / −205** (larger because it also swept in the 6
formerly-untracked files):

```
metrics.py +323   reports.py +283   strategy/threshold.py +122   real_data.py +118
tests/test_reports.py +409          tests/test_metrics.py +231
run.py +22        trader.py +58     strategy/wiring.py +45      txt.txt +31
logging_io.py +13 forecaster/toto2.py −(rewrite)  tests/test_confidence_placement.py −49
```

This is July research work that was never carried across, and it is the only
content in the folder not reproducible from night. **It is safe** — commits
rather than a dirty tree, so it can be merged, cherry-picked or reverted.

**Updated 2026-08-24: day has since added a second commit, and night made the
same fix independently.** Day's `ed0a1ba` — "fix: `--data` is required; drop the
`DEFAULT_DATA` session default", touching `real_data.py` and `.python-version` —
has an exact counterpart in night's `8065a35`: same message, same two files,
**no shared ancestry**. Git cannot see that they are the same change, so they
collide. Take one side wholesale rather than merging hunks. This also closes
§2.1's open item 3.

**And the day-only content is wider than `7fd3fa6`.** Because the split is at
`aae8473` and not `2716f49` (§0), `0f80b80` (the baked EXP91 / EXP227 winning
config) and `e405adf` (SMA trend-filter WIP) are absent from night too.

**D5 is settled: the work stays where it is.** Shape A removed the merge, and the
2026-08-24 decision against any cross-tree transport (§0) removes the
cherry-pick too. `7fd3fa6` and `ed0a1ba` remain on `autoresearch_result_1` in
`day/forecast_eval` as the record. If night ever wants day's `reports.py` or
`threshold.py` work, it is rewritten there, not moved.

---

## 1. The consequence — two shapes, and the migration is only one of them

> **DECIDED 2026-08-24 — D1 = Shape A.** The day deployment is hosted in
> `night/forecast_eval`; `day/forecast_eval` stays the research tree it already
> is. **There is therefore no migration.** §2's merge and §5's M-steps are not
> being done, and are kept below only as the record of what Shape B would have
> cost — corrected, because parts of that record were wrong.

**Shape A — the day deployment is hosted in `night/forecast_eval`. ✅ CHOSEN**
The live stack is already there, the session is already a parameter, and the port
is the C-item work in §5 plus `--config day`. `day/forecast_eval` stays what it
is: an older research tree. No merge, no collision, nothing to reconcile.

**Shape B — `day/forecast_eval` becomes the day deployment. ❌ not chosen**
Then the *first* task is not the port at all: it is bringing 27 commits and
25 000 lines of `live/` across, resolving the collision in §2 — and after that,
every C-item in §5 still has to be done, identically. **Shape B is Shape A plus a
merge.** The merge is a hosting decision, not migration progress.

A third possibility, named so it could not be chosen by accident: **the folder
layout implies Shape B, and picking it for that reason alone would have been
paying the merge cost to get a copy of a codebase whose design already makes the
session a parameter** (§2.12: "no module names a session"). Two trees is the fork
that `SessionPolicy` exists to make unnecessary. That is the reasoning D1
followed.

---

## 2. The merge collision — **moot under Shape A; kept as the record**

> D1 chose Shape A, so this merge is not happening. The section is corrected
> rather than deleted, because it is also the measurement of what a **cherry-pick
> for D5** runs into, file for file.

**Corrected 2026-08-24: the base is `aae8473`, and the collision is bigger than
first recorded.** Measuring `aae8473..ed0a1ba` (day) against `aae8473..night`,
**14 files are touched on both sides**, not 9 — and eight of them are conflict
kinds that stop a merge outright rather than producing mergeable hunks:

| Kind | Files |
|---|---|
| **modify/delete** — git halts and asks | `compare_models.py` — night **deleted** it, day modified it |
| **add/add** — both sides created the path independently; every one conflicts | `.python-version`, `README.md`, `strategy/volmultitf.py`, `strategy/wiring.py`, `tests/test_reports.py`, `tests/test_volmultitf.py`, `txt.txt` |
| content | `logging_io.py`, `metrics.py`, `real_data.py`, `reports.py`, `strategy/threshold.py`, `tests/test_metrics.py` |

`run.py`, `trader.py`, `execution.py` and `portfolio.py` are clear of it — and
that is not luck: SHIOAJI_PLAN's standing ground rule is that those four stay
unmodified, and 27 commits later they still are.

**Three corrections to the earlier reading of this list:**

- **`main.py` is a day-only add, not a night deletion.** Night never had it at
  `aae8473` and does not have it now, so git sees a clean add with nothing to
  conflict against — it would arrive **silently**, which is the opposite of the
  flagged collision this section first predicted.
- **`strategy/wiring.py` is an add/add**, and it is precisely the seam
  `live/config.py` names as the autoresearch entry point for C1. A careless
  resolution there breaks the tuning seam, not just a file.
- **`real_data.py` and `.python-version` carry the independent-duplicate fix**
  from §0.2 — the same change made twice with no shared ancestry.

`strategy/threshold.py` remains the collision that matters most: it is where the
night tuning chain did its work (`min_trend_ticks`, `path_momentum`, the band),
and day's +122 lines are a different edit to the same file. **Resolving that one
wrong silently changes the strategy** — and the night side of it is load-bearing
for `live/config.py`'s pinned Calmar.

Both working trees are clean.

### 2.1 Environment gaps in `day/` — found 2026-08-24, **mostly CLOSED the same day**

`day/` could not run the live stack at all when this section was written. The
environment half is now done; what remains is data and documentation.

**State after the rebuild (verified 2026-08-24):**

| | `day/` | `night/` |
|---|---|---|
| venv interpreter | **uv-managed CPython 3.12.13** ✅ | uv-managed 3.12.13 |
| `.python-version` / `requires-python` | 3.12 / `>=3.12` ✅ | 3.12 / `>=3.12` |
| `shioaji` | **1.7.2, pinned `==` in pyproject + lock** ✅ | 1.7.2, pinned `==` ✅ |
| `torch`, `toto2` | 2.13.0+cu130 (`cp312`), 2.0.0 ✅ | identical |
| site-packages | 179 packages, **11 minor transitive bumps** vs night | 179 |
| `real_data.DEFAULT_DATA` | **removed — `--data` is now required** ✅ (`ed0a1ba`) | same fix, `8065a35` |
| `UV_SETUP.md` | **present, byte-identical to night's** ✅ | present at `night/` |
| `AGENTS.md` | **still absent** | present at `night/` |
| `CLAUDE.md` | **still 2026-06-11** — verified: 0 mentions of `live/` | current (2026-08-23) |
| test suite | **114 tests, all passing** | 744 |

**What was wrong, and what it cost to find.** Three defects, none visible from
the version numbers alone:

- The venv was built from **conda base** (`home = C:\ProgramData\miniconda3`),
  which CLAUDE.md rules out explicitly. The 3.13-vs-3.12 mismatch was the
  symptom; this was the defect. Rebuilt with `uv venv --clear --python 3.12`.
- `pyproject.toml` declared `dependencies = []` and a `readme` pointing at a
  file that does not exist, so any metadata build would fail.
- **`uv lock` floated `shioaji` to 1.7.3**, and `uv pip install shioaji`
  installed 1.7.3 **while the lock said 1.7.2** — the lock is not consulted by
  `uv pip install`. Both trees are now pinned `==1.7.2` in `pyproject.toml`, and
  day's venv was downgraded in place. This matters more here than usual: the SDK
  is a compiled `_core.pyd`, and every behaviour this project knows about it —
  the `TokenError → AuthError` chain, `Snapshot.ts` as epoch-ns, `api.simulation`
  reading back `None` — was measured against **1.7.2** by probe.

> **Do not run `uv sync` in either tree.** `torch` (cu130) and `toto-2` are
> installed manually and deliberately kept out of `dependencies` — night's
> pyproject comment explains why. A sync reconciles the venv against that list
> and can strip them; on a 5090 the failure surfaces at model load, not install.
> `uv lock` is safe; `uv pip install <pkg>` is safe. `uv sync` is not.

**The 11 remaining package deltas** (filelock, fsspec, huggingface_hub, idna,
narwhals, orjson, platformdirs, pygments, scipy, typing_extensions,
typing_inspection) are transitive and minor. `huggingface_hub` 1.28.0 vs 1.27.0
is the only one with a plausible path to a trade decision, since it fetches the
Toto-2 checkpoint. **The instrument for this already exists and is better than
matching version strings: §2.13's reproduction check** — re-run the pinned config
and assert the trades and net PnL reproduce bit-identically, ~4 minutes. Under
Shape A it runs in `night/`, and it is step **W5**'s acceptance test — but see
**§2.2: its baseline no longer exists** and must be re-established first.

**Still open:**

1. ~~`shioaji` missing~~ **CLOSED 2026-08-24** — installed, pinned, imports clean.
2. ~~Python 3.13 against night's 3.12~~ **CLOSED 2026-08-24 — day rebuilt at
   uv-managed 3.12.13.** The reasoning is kept because it is the precedent for
   the next environment decision:
   Measured from the wheel tags rather than the version numbers: `shioaji` 1.7.2
   is **`cp37-abi3-win_amd64`** — the stable limited API, so it runs on any
   CPython ≥3.7 — and `torch` 2.13.0+cu130 exists for both (`cp312` in night,
   `cp313` in day, both already installed). **Neither direction is forced by a
   dependency.** So the choice is risk: upgrading *night* would mean rebuilding
   the venv in UV_SETUP.md's fixed order, re-running 744 tests and re-proving
   §2.13's bit-identical reproduction — on the tree that holds the pinned config,
   an in-flight soak and the 2026-09-15 roll — for no functional gain.

   **The version is the symptom; the real defect is the source.** Day's
   `pyvenv.cfg` reads `home = C:\ProgramData\miniconda3` — it was built from
   **conda base**, which CLAUDE.md rules out explicitly ("a local `uv` venv — not
   a conda env"). Night's reads the uv-managed
   `cpython-3.12-windows-x86_64-none`. Day's venv needs rebuilding whatever the
   version, so it may as well land on the one everything was proven on.

   If 3.13 is wanted later, it is a deliberate environment change on night gated
   by a §2.13 re-run — not a side effect of this port.
3. ~~`real_data.DEFAULT_DATA` is dangling~~ **CLOSED 2026-08-24** — day's
   `ed0a1ba` drops the default outright and makes `--data` required, so there is
   no dangling path left to reconcile. Night made the same change independently
   as `8065a35` (§0.2).
4. ~~`UV_SETUP.md` absent from `day/`~~ **CLOSED — it was never absent.** It is
   present and byte-identical to night's copy; the §2.1 table row above was
   simply wrong.
5. **`AGENTS.md` absent from `day/` — OPEN.**
6. **`day/forecast_eval/CLAUDE.md` is 2026-06-11 — OPEN.** Verified: it mentions
   `live/` **zero** times.

The two documentation items are not runtime problems but are agent problems:
day's `CLAUDE.md` documents neither `live/`, nor the uv/cu130 notes, nor the
folder-scope rule. Under Shape A they matter less — no live work happens in
`day/` — but any agent sent into that tree still reads them.

### 2.2 The RPT datasets were rebuilt on 2026-08-24 — **re-baselined on the new tape**

Found while verifying this plan on 2026-08-24, and not known to any section above
it. `dataset/RPT/` was regenerated at **03:53**, with `.bak` copies of the
previous files left beside the new ones:

| file | rows before → after | coverage ends |
|---|---|---|
| `TXF_RPT_minute_night.csv` | 1 733 348 → **1 787 913** | 2026-05-15 → **2026-08-21** |
| `TXF_RPT_minute_day.csv` | 1 124 790 → **1 145 145** | 2026-05-15 → **2026-08-21** |

`PINNED.source_dataset` is `\\DESKTOP-SQUIRRE\dataset\RPT\TXF_RPT_minute_night.csv`, which resolves to this
same, now-changed file — verified by identical size and mtime. So §2.13's
bit-identical reproduction check lost the tape its recorded numbers came from
(§2.13 states 1 733 348 bars, which is exactly the `.bak` row count).

> **DECIDED 2026-08-24 — the day port works on the new tape, and the night
> baseline was re-run on it.** Results below. **W4b is DONE.**

#### The alignment trap — the first re-run was invalid, and finding out why is the real finding

The obvious re-run — same command, new file — **does not measure what it looks
like it measures.** `--lookback-frac 0.30` sets warmup as a *fraction*, so a
longer tape moves the warmup end: 520 004 → **536 373** bars. Forecasts fire at
`warmup + k×stride` with `stride=30`, and **16 369 mod 30 = 19**, so every
forecast lands 19 bars off the original grid. Measured: **0 shared forecast
timestamps** out of 40 443 old / 41 716 new.

Over the **identical calendar window on identical data** (2020-04-06 →
2026-05-08):

| | trades | net PnL |
|---|---|---|
| original pinned run | 174 | **+40 310.44** |
| frac-0.30 run on the new tape | 187 | **+30 209.82** |
| | | **−10 100.62** |

**A 19-bar phase shift costs 10 100 points — 25 % of six years of net PnL, and
Calmar 13.6742 → 8.7651.** Nothing about the strategy changed; only *when* the
30-bar forecast clock happens to tick. Read naively, that run would have looked
like three months of decay. It is not: the added months were the best-behaved
stretch in it (+3 012.31 over 25 trades, hit rate 0.560).

**This belongs to the live deployment, not to the day port.** A live run's grid
is set by whenever it starts, so the pinned 13.6742 is one draw from a
distribution at least this wide. Recorded here because this is where it was
found; it should be carried into `SHIOAJI_PLAN.md` as a robustness item.

#### The valid re-baseline

Re-run with `--lookback-frac 0.290844409`, chosen so
`int(f × 1 787 913) = 520 004` — the original warmup exactly, so the forecast
grid matches bar-for-bar and the new tape is a **strict superset**:

| | recorded (`results.tsv`, `e44a047`) | new tape, aligned |
|---|---|---|
| trades | 185 | **208** (+23) |
| net PnL (pts) | 39 075.6312 | **+46 603.2099** |
| max drawdown (pts) | −2 856.9506 | **−2 856.9506** — unchanged |
| Calmar (net/\|dd\|) | 13.6742 | **16.3122** |

**The reproduction check passes.** The first 185 trades are identical to the
original run — entry and exit timestamps, sides, prices — with a net delta of
**0.0000000000** (max per-trade difference 1.4e−14, float repr only). That
simultaneously clears §2.1's 11 transitive package deltas: the environment drift
changes nothing that reaches a trade decision.

**The pinned config improved on the extended tape.** The 23 added trades
(2026-05-13 → 2026-08-20) contribute **+7 527.58**, the worst drawdown still sits
in the old period so `max_dd` is untouched, and the run now beats buy-and-hold by
+11 835.43 (it trailed by −1 552.35 in the mis-aligned run).

**Consequences for the plan:**

- **`results.tsv` needs two new columns, not one** — session (W0) *and* dataset
  vintage. A row's Calmar is meaningless without both.
- **Warmup must be recorded in bars, not as a fraction.** `--lookback-frac` is
  not reproducible across tapes of different length; it is the mechanism that
  invalidated the first re-run. W5a/W5b must pin warmup in bars, or the day
  search will be uncomparable to itself the next time the dataset grows.
- **W5a's transfer probe compares against 16.3122, not 13.6742**, and must use
  the day tape's own aligned warmup.
- Re-pinning `PINNED` to the new numbers is **not** done here — that is a night
  deployment change with a soak and the 2026-09-15 roll in flight, and it is the
  user's call.

Runs: `outputs/real_data_night_newtape_20260824/` (frac 0.30, invalid — kept as
the evidence for the alignment trap) and
`outputs/real_data_night_newtape_aligned_20260824/` (the valid one).

**Data for the day side is ready:** `TXF_RPT_minute_day.csv`, 73 MB,
1 145 145 rows, current to 2026-08-21.

**Superseded 2026-09-01:** both run directories above were deleted at some point
before today, so Calmar 16.3122 rested on this section alone with no run behind
it. It was **re-measured and reproduced exactly** as part of W5a — see §2.3.

---

### 2.3 W5a and W5b — the day search, run 2026-09-01 · **PROVISIONAL**

Both steps are done. **Both are provisional in the sense §5's blocking note
defines:** they were run on the branch that measures against Calmar 16.3122
while `TAPE_REBUILD_PLAN.md` is still pending, on the stated understanding that
their numbers are re-measured after T0–T4 whatever the verdict. §5 says the
indefensible thing is running W5a *without recording which branch was chosen*;
this is that record.

#### The aligned warmup, and why it is not night's fraction

§2.2 requires the probe to use "the day tape's own aligned warmup" and to compare
against 16.3122. For that comparison to mean anything the two runs must cover the
**same calendar window**, so warmup was chosen as the day-tape index of the night
baseline's first eval bar: **674 498 bars**.

> **CORRECTED 2026-09-01 (later) — the alignment is approximate, not bar-exact,
> and this section originally claimed otherwise.** `real_data.py` derived the
> split from **raw row positions** while `run_backtest` counts `bar_idx` over
> the **`Environment`-filtered** stream, and the two differ: the day tape carries
> **1 215 rows stamped 13:45** (outside the DAY session by both `policy.label`
> and `classify_session` — median volume 4 against 2 191 on the 13:44 bar), the
> night tape 82. So the windows the banner printed were not the windows the runs
> used. Measured:
>
> | | printed | actual |
> |---|---|---|
> | DAY warmup ends | 2020-03-05 13:44 | **2020-03-11 08:48** (904 rows earlier in raw terms) |
> | NIGHT warmup ends | 2020-03-05 17:44 | **2020-03-05 18:01** |
> | DAY eval bars | 470 647 | **469 432** |
> | NIGHT eval bars | 1 267 909 | **1 267 827** |
>
> **The two eval windows therefore start ~5.6 days apart, not bar-for-bar.**
> Nothing measured changes: `warmup` reached the forecaster unaltered, the night
> re-baseline still reproduces §2.2 exactly, and the day probe's first trade
> (2020-03-11 08:50) was always consistent with the corrected start. The
> misalignment is 0.24 % of a 6.5-year window, so W5a's and W5b's conclusions
> stand. What was wrong was the claim of exactness, and the banner that supported
> it — `real_data.py` now derives and reports the split on the in-session stream,
> and says so when `--lookback-frac` was taken over raw rows instead.

Reusing night's *30 % split* would have started day's evaluation in **2015-08**
and compared two different market regimes. Recorded because the 30 % figure is
the obvious thing to carry across and it is the wrong one.

`real_data.py` had no way to express this — only `--lookback-frac`.
**`--warmup-bars` was added** (§2.2's "warmup must be recorded in bars" made
operational), and validated by reproducing the night aligned run bit-for-bit from
`--warmup-bars 520004` instead of the reverse-engineered
`--lookback-frac 0.290844409`.

> **`warmup_bars` is append-stable, not rebuild-stable — and the rebuild is
> coming.** Recorded here because §2.2's consequence list says warmup in bars is
> what makes a run "comparable to itself the next time the dataset grows", and
> that is true only of growth **at the end**. A bar count is an index into the
> in-session sequence, so anything that **inserts** bars *before* the split moves
> what it points at. `TAPE_REBUILD_PLAN.md` T0–T4 does exactly that: fixing
> Defect A restores ~15 day bars at **every** expiry, and ~63 expiries precede
> 2020-03. So after the rebuild, `674 498` names a different bar than it does
> today.
>
> **Consequence for the re-measurement:** the DAY rows' `warmup_bars` must be
> **recomputed from a target timestamp**, not carried across as a number.
> Re-using it verbatim would reproduce §2.2's alignment trap in a new form —
> same failure (a moved forecast grid read as a change in the strategy), new
> cause (bars inserted mid-tape rather than a fraction over a longer one).

#### W5a — the transfer probe: night's configuration does not transfer

| | NIGHT baseline | DAY probe |
|---|---|---|
| Calmar | **16.3122** | **0.2671** |
| net PnL (pts) | +46 603.21 | +3 111.64 |
| max drawdown | −2 856.95 | **−11 649.12** |
| trades | 208 | 257 |
| **gross pts / trade** | **230.42** | **18.65** |
| fees / trade | 6.36 | 6.54 |
| **fee drag** | **2.8 %** | **35.1 %** |
| win/loss size ratio | 2.53 | 1.45 |
| median hold | 3 d 23:17 | 4 d 00:30 |

Run: `outputs/real_data_day_probe_W5a_20260901/`. The night re-measurement is
`outputs/real_data_night_aligned_W4b_repro_20260901/` and reproduces §2.2 to the
last digit (208 trades, +46 603.2099, −2 856.9506, +11 835.4348 over buy-and-hold).

#### The reason the probe's verdict is *not* "so the search is futile"

The obvious reading of a failed transfer is that the day session has no signal.
**Measured, that reading is wrong, and it is worth stating because it inverts the
conclusion.** The day forecast's `information_coefficient` is −0.0049 and its
`direction_hit_rate` 0.4987 — but **night's are −0.0119 and 0.5008**. Night's
forecast is no more directionally accurate than day's.

Night's Calmar does not come from forecast skill. It comes from **payoff
asymmetry**: it cuts losers at 329 and lets winners run to 833. Day, with the
same gates at night's settings, does the reverse — winners cut at 586, losers
held to 404. That asymmetry is produced by the trend filter, the momentum gate
and the probability gate, which are **exactly what the `edit:`/`keep:` loop
tunes**. So the probe's answer to D2 is *yes, search* — for a reason opposite to
the one its headline number suggests.

#### W5b — the search, and the selection rule it forced

The emitter never changes the forecasts, so the whole search runs off **one** GPU
precompute via `run_backtest(forecasts_cache=…)`: 110 s once, then **~2 s per
candidate** instead of ~4 min. The baseline cell reproduces the W5a CLI run
exactly (0.2671 / 3111.6436 / −11649.1183), which is what licenses the rest.

Widening the band fixes all three of day's problems at once — fewer trades, more
points each, fee drag collapsing. Night's ±0.00045 was simply far too tight for a
**300-bar** session where night has **840**.

**But ranking by full-window Calmar picks a fiction.** The top cell is
`band=0.004` at **Calmar 14.7697** — a number that reads as "day nearly matches
night". It is **27 trades, 10 of them in the out-of-sample half**, with a
first-half Calmar of 2.2781. It is the degenerate trade-almost-never corner.

So every candidate was scored on the **two halves of the eval window separately**
(split 2023-06-01) and selected on **worst half**, not full window:

| band | min_prob | n | h1 Calmar | h2 Calmar | worst | full |
|---|---|---|---|---|---|---|
| **0.001** | **0.65** | **73** | **5.6296** | **3.1557** | **3.1557** | **4.6805** |
| 0.0015 | 0.50 | 73 | 2.4754 | 2.4088 | 2.4088 | 3.4160 |
| 0.003 | 0.65 | 42 | 2.3325 | 2.4653 | 2.3325 | 3.2794 |
| 0.00125 | 0.65 | 65 | 2.8920 | 2.3173 | 2.3173 | 3.1805 |
| *0.004* | *0.50* | *27* | *2.2781* | *14.3684* | *2.2781* | *14.7697* |

Across the 33-cell grid **spearman(h1, h2) = 0.45, pearson = 0.09** — full-window
rank is weak evidence on this tape. A stable ridge does exist around
`band 0.001–0.0015 × min_prob 0.5–0.65`, holding Calmar ≈ 2.0–3.2 in both halves.

**Result — `results.tsv` gains three DAY rows** (all at commit `e073e73`, dataset
`TXF_RPT_minute_day.csv@2026-08-21`, warmup 674 498):

- the **W5a probe** (`keep`, 0.2671) — the DAY chain's baseline;
- the **rejected** `band=0.004` cell (`reject`, 14.7697) — kept as the evidence
  for the selection rule, not as a candidate;
- the **pinned DAY row** (`keep`, **4.6805**) — `band 0.001 + min-prob 0.65`,
  73 trades, reproduced through the CLI in
  `outputs/real_data_day_W5b_pinned_20260901/`; fee drag 35.1 % → 1.7 %,
  win/loss 1.45 → 2.09, hit rate 0.420 → 0.534.

`_best_row` in `tests/test_live_config.py` now filters on `status == "keep"`.
Without it the `reject` row is the highest DAY Calmar in the file and would
become "the best known DAY config" — the ledger's own counter-example promoted to
its recommendation. One new test covers this.

#### Two caveats that belong with the number

- **It does not beat buy-and-hold.** Over the eval window the pinned DAY config
  nets +24 472 against a passive +33 745 — **−9 273**. Night beats its own floor
  by **+13 259**. Day's Calmar 4.68 is earned by a *small drawdown* (−5 228), not
  by out-earning the index. (The CLI's own printed delta, −11 669, spans warmup +
  eval and is unfair to day, whose warmup is 58.9 % of the tape; −9 273 is the
  like-for-like figure.)
- **This is not a live pin.** There is no `PINNED_DAY` in `live/config.py` and W6
  has not run. C1 proposed a second pinned value; what exists now is the row it
  would be pinned *to*, which is the prerequisite, not the act.

#### A follow-up this raised about NIGHT, not acted on

The out-of-sample split that rejected day's 14.7697 has **never been applied to
the night chain**. Every `keep:` decision in those 18 commits was made by
full-window Calmar on a single window — the exact rule that, on the day tape,
selects a 27-trade artefact. This is *not* a claim that 16.3122 is overfit: night
has 208 trades, a coherent payoff story, and has not been tested. It is that the
evidence standard behind it cannot tell the two cases apart, and W5b now leaves
behind a harness that could check it in minutes. **The user's call, not assumed
here** — and it is a live-deployment question with the 2026-09-15 roll in flight,
so it belongs beside `TAPE_REBUILD_PLAN.md` §6's D1 rather than in this plan.

---

## 3. What ports free — verified by reading

These are properties of the **night** codebase, which is where `live/` exists —
and under Shape A, which D1 chose, that is where the day deployment lives too. So
they are already true today; nothing has to be ported to make them so.

| | Why it ports | Evidence |
|---|---|---|
| Bar filter | `TickAggregator` asks `policy.label(ts) in policy.sessions` | `live/bars.py:227-234` |
| Staleness rail | `same_session_instance` compares the trading day, not the label | `live/sessions.py:148` |
| FeedWatch gate | Gated on `policy.includes(now)` — a DAY policy simply falls quiet 13:45–08:45 | `live/cli.py:899` |
| Daily-loss anchor | Re-anchors on `policy.trading_day(bar.ts)` change; a DAY session sits wholly inside its own date | `live/state.py:145` |
| Weekend (§2.25) | `is_trading_day` tests the *trading* day; a DAY session never crosses midnight — the easier case | `live/sessions.py:110` |
| **Re-login anchors (§2.24)** | 08:40 and 14:55 for *every* deployment — already re-derived for a DAY policy: same two times, and the **night** pre-open then carries the 17 h 45 m interval | SHIOAJI_PLAN §2.24 |
| **`kbars` right-labelling** | The `08:46 … 13:45` → `08:45 … 13:44` correction was **derived and validated on the DAY session** — here the day path is the proven one | `live/backfill.py:138-146` |
| Roll *net* (`RollWatch`) | Watches the alias, not the clock | `live/roll.py` |
| Backtest core | `run.py` never names a session; it tags PnL from `market.session` | `run.py:238` |

---

## 4. What does not port — the substance of the port itself

**These are all night-tree file references.** In `day/forecast_eval` today, C2,
C3, C5, C6 and C7 name files that do not exist — which was §0's point, not a
separate problem. **Under Shape A it stops mattering:** all of this work happens
in `night/`, where those files do exist.

### C1 — no tuned DAY configuration exists · **BLOCKING · research**

`PINNED` carries `source_dataset=…TXF_RPT_minute_night.csv` and
`SessionPolicy.night()`; every knob is the endpoint of a night search recorded in
`results.tsv`. `tests/test_live_config.py::test_pinned_row_is_still_the_best_known_config`
asserts the pinned commit is the best row there — a DAY row cannot simply be
added, because on Calmar the two are not comparable and whichever won would
silently re-pin the *other* deployment. **`results.tsv` has no session column.**

The day tree makes this worse, not better: it has no `results.tsv` at all, and
its defaults are the untuned 313m / ±0.0005 / `min_prob=None` (§0.1).

**Proposed:** tune on `TXF_RPT_minute_day.csv`, land it as a second pinned value
`PINNED_DAY` with its own provenance and a drift test keyed to a session-tagged
row. **Mechanism:** there is no autoresearch entry point — `live/config.py`
records that `strategy/wiring.py` is that seam and *"its entrypoint no longer
exists"*, and the winning night config came from **`real_data.py` CLI flags**,
which is what the `edit:`/`keep:` commit pairs are. A DAY search is the same loop
**This work needs `results.tsv` and the tuning loop, so it happens wherever those
live — which, under Shape A, is `night/` and only `night/`.**

**Scope: settled by D2 — probe first.** A transfer baseline (night-pinned
parameters over the day file, unchanged, one ~4-minute run) comes before any
search, and its Calmar and median hold decide whether the full `edit:`/`keep:`
loop is worth launching. Sequenced as W5a → W5b.

### C2 — the entry point cannot express a DAY deployment · **DONE 2026-09-01 (W6)**

`cli.py:314` is `cfg = PINNED`; `LiveConfig` is frozen. A bare `--session` flag
would be the 2026-08-11 defect by the front door: changing the policy without
changing the *strategy* runs night-tuned parameters on a day tape, undetectably.

**Proposed:** `--config night|day` selecting the whole config, policy travelling
inside the value it belongs to.

**DONE 2026-09-01 (W6), as proposed.** `live/config.py` gained `PINNED_DAY`
(pinned to W5b's row) and a `CONFIGS` registry; `cli.py` gained
`--config night|day`, defaulting to **night** — the deployment that has actually
soaked. There is no `--session` flag and a test asserts there never is one.

**One trap this step nearly re-opened, recorded because it is invisible in a
diff.** `--warmup-days` took its default from `PINNED.warmup_days_default()`
*in the argument declaration*. Argparse evaluates that when the parser is
**built**, before `--config` has been read — so the default is always NIGHT's
30, and `--config day` would have silently inherited it. That is exactly the C6
defect W4 removed, re-entering through the entry point W6 added: at DAY's 300
bars/session, 30 days reaches ~109 emitter boundaries and about 4 non-HOLD
samples against the floor of 3 that `_check_primed` refuses below. The flag now
defaults to `None` and `main` resolves it from the **selected** config.

**Verified — the banner under each flag** (real `LiveRuntime`, real forecaster;
everything after the Shioaji login, which was not performed):

```
$ ... --config night --broker dryrun
LiveConfig[e44a047 calmar=13.6742 ['NIGHT']] ... band=±0.00045 min_prob=0.5 ...
[warm-up] 30 days (840 bars/session)   [policy] roll_at=15:30:00 cache=night

$ ... --config day --broker dryrun
LiveConfig[fdd1348 calmar=4.6805 ['DAY']] ... band=±0.001 min_prob=0.65 ...
[warm-up] 84 days (300 bars/session)   [policy] roll_at=12:45:00 cache=day
```

One flag moves the policy, the band, the probability gate, the warm-up window,
the roll time and the cache key together — W1's C5, W2's C4 and W4's C6 all
light up from the single selection, and NIGHT is unchanged. **15 new tests.**

**Not changed:** `v1_check.py` and `v4_probe.py` still import `PINNED`
directly. Both are NIGHT-specific probes, so that is correct today; if either is
ever wanted for DAY it needs the same treatment, and neither is on W6's path.

### C3 — `settle_rolled_leg(session="NIGHT")` · build, one line · **silent**

`live/runtime.py:285` books a roll-settled leg to NIGHT by literal default. On a
DAY deployment every rolled round-trip is attributed to a session the run never
traded, corrupting `compute_metrics`' day/night split. **The only genuinely
silent defect on this list, and a defect in the night tree today regardless of
whether this port happens.**

**Proposed:** derive from the fill's timestamp (`classify_session`) or the policy,
plus the regression test that fails on the literal.

### C4 — `ROLL_AT = 15:30` was measured for a night strategy · **MEASURED 2026-08-24 (W2) — decision pending**

`live/roll.py:183-190` derives it from the expiring month's final *night* session
opening at 15:00 the day before its last trading day, whose first half hour the
comment calls "the most liquid stretch either month sees". For a DAY deployment
15:30 is **outside the traded session** — the clock still fires, so the ladder
runs, unwatched and priced off a tape the run is not following.

#### The source the plan named cannot answer this

W2 was specified as "re-derive from the RPT day file (per-minute volume on both
months)". **That file cannot do it.** `TXF_RPT_minute_day.csv` is
front-month-continuous — one contract per minute, measured 2026-08-23 and
recorded in `real_data.load_minute_bars`' own docstring. On the roll date
2026-08-18 it holds 300 day-session rows, **all `202608`**; the incoming month is
absent. `TXF_RPT_ticks.csv` is the same series at tick resolution and is equally
front-month-only.

The raw TAIFEX `dataset/RPT/extracted_data/Daily_*.rpt` files carry **every**
listed month and are the only source here that can. Measured from them, product
`TX`, the three most recent rolls (2026-06-16, 2026-07-14, 2026-08-18 — last
trading day minus `ROLL_LEAD_DAYS`), volume bucketed by half hour. A roll is two
orders that must both fill, so the column that decides is the **thinner leg**:

**DAY session 08:45–13:45 — the C4 question**

| clock | expiring | incoming | both | thinner leg | share |
|---|---|---|---|---|---|
| **08:45** | 67 392 | 17 554 | 84 946 | **17 554** | 15.8 % |
| 09:15 | 54 888 | 17 530 | 72 418 | 17 530 | 13.5 % |
| 09:45 | 47 384 | 16 784 | 64 168 | 16 784 | 12.0 % |
| 10:15 | 42 312 | 15 992 | 58 304 | 15 992 | 10.9 % |
| 10:45 | 24 746 | 10 350 | 35 096 | 10 350 | 6.5 % |
| 11:15 | 29 608 | 11 118 | 40 726 | 11 118 | 7.6 % |
| 11:45 | 23 682 | 8 714 | 32 396 | 8 714 | 6.0 % |
| 12:15 | 23 924 | 10 650 | 34 574 | 10 650 | 6.4 % |
| 12:45 | 26 698 | 11 282 | 37 980 | 11 282 | 7.1 % |
| **13:15** | 49 256 | 26 734 | 75 990 | **26 734** | 14.2 % |

The peak is **13:15–13:45**, the closing stretch — but `ROLL_WINDOW` is an hour,
and an hour from 13:15 runs 30 minutes past a 13:45 close. **The most liquid
moment is the one a DAY deployment cannot use.** The best window that fits
wholly inside the session is the open: 08:45–09:45 carries 35 084 on the thinner
leg and leaves ~4 h of session behind a failed roll.

#### The validation did not validate — night's own justification does not hold

Running the same method over the night roll day, which should have reproduced
15:00–15:30 as "the most liquid stretch either month sees":

| clock | expiring | incoming | thinner leg | share |
|---|---|---|---|---|
| 15:00 | 9 476 | 2 484 | 2 484 | 4.5 % |
| **15:30** ← `ROLL_AT` | 6 336 | 1 422 | 1 422 | 2.9 % |
| 16:00 | 9 404 | 1 970 | 1 970 | 4.3 % |
| 21:30 | 20 584 | 3 556 | 3 556 | 9.1 % |
| **22:00** | 25 596 | 5 108 | **5 108** | 11.6 % |
| 22:30 | 21 226 | 3 190 | 3 190 | 9.2 % |
| 23:00 | 15 894 | 3 964 | 3 964 | 7.5 % |

*(28 buckets measured; the seven that matter are shown.)*

**15:00–15:30 is the 6th busiest bucket, not the busiest.** 22:00 is 2.1× more
liquid on the thinner leg and 2.7× on the expiring month, and the actual roll
window 15:30–16:30 carries 3 392 against 22:00–23:00's 8 298 — **2.4× worse**.
No metric in the table picks 15:30: not the thinner leg, not the expiring month,
not the combined.

What *does* justify 15:30 is the other half of the existing comment, on
`ROLL_WINDOW`: an hour from 15:30 leaves "~13 h clear of the moment the contract
stops trading". That is a **runway** argument, and it stands. The liquidity
claim beside it does not.

**Proposed, and the decision this needs:** carry `ROLL_AT` on the policy or
config so two deployments cannot share the wrong one — that part is settled and
unaffected.

#### DECIDED 2026-08-25 — **DAY `ROLL_AT` = 12:45; NIGHT stays at 15:30**

**NIGHT: unchanged.** The liquidity claim in `roll.py`'s comment does not survive
the measurement above, but the runway argument does, and the first live roll is
2026-09-15. Left as-is deliberately, not by omission.

**DAY: 12:45 — one hour before the 13:45 close**, so `ROLL_WINDOW` fills exactly
the remainder of the session and cannot overrun it.

**The window-total table above is the wrong metric, and is kept only as the
liquidity survey it is.** `RollSchedule.due` gates when a roll may *start*;
`cli.py:827` blocks re-entry the moment `roll_state` is set, and the fills are
worked by `advance_roll_ladder`, which is **not** bounded by `ROLL_WINDOW`. So
the rest of the hour is a retry budget for `_roll_evidence`, not for fills. What
decides is (a) the market at the execution moment, and (b) what is left of the
session behind it.

**Measured at the ladder's actual execution span** (first 2 min: 50 s per leg,
two legs), per roll day:

| `ROLL_AT` | incoming lots/min | intra-minute range | session left | retry tape |
|---|---|---|---|---|
| 08:45 | 276 | **132.2 pts** | 300 min | decaying |
| 09:00 | 404 | 86.3 pts | 285 min | decaying |
| 09:15 | 231 | 87.2 pts | 270 min | decaying |
| 11:00 | 102 | 52.8 pts | 165 min | flat |
| **12:45** | **145** | **26.8 pts** | **60 min** | **rising** |
| 13:15 | 130 | 29.2 pts | 30 min | rising, window overruns |

**The volatility column is what settles it.** `ROLL_LADDER_MAX_TICKS` is 25 and
`tick_size` is 1.0, so the ladder's entire escalation budget is **25 points**. At
08:45 the front month's mean intra-minute range is 132 points — the price moves
five times the ladder's whole budget inside one minute, and a 25-tick limit is
noise against it. At 12:45 the range is 26.8 points, so the cap is ~93 % of a
minute's range and each rung buys real certainty. 12:45 is the **calmest** moment
of the session, 4.9× calmer than the open.

**Depth is not a constraint anywhere.** `PINNED.max_position` is 1, so the roll
is one lot per leg against 145 lots/min — every candidate is orders of magnitude
deeper than needed. Choosing on volume was choosing on the wrong axis.

**12:45's retry path improves rather than decays.** If the first ladder pass does
not fill, the tape ahead of it ramps steadily into the close — 458 → 650 → 1 276
→ 2 194 → 2 904 → 5 084 lots/min over 13:37–13:44 — and that ramp is continuous
trade, not a single closing auction (measured minute by minute). From 08:45 the
tape fades the other way: 1 741 → 966 → 665 → 325 on the expiring month.

**The cost, stated plainly.** 60 minutes of session behind the roll instead of
300, and `roll_window_lapsed` fires at 13:45 — exactly as the market shuts, with
no same-day intervention possible. **The backstop is the next day**, not the
remainder of this one: `ROLL_LEAD_DAYS` is 1, and the expiring month still trades
its last trading day. **Measured: 08:45 → 13:29:58**, ~4 h 45 m and ~50 000 lots
across the three most recent expiries.

> **Correction to SHIOAJI_PLAN §2.6**, which says the expiring contract "traded a
> normal day session to 13:44:59" on its last trading day. It does not: the
> **expiring** month stops at **13:30**, while the incoming month runs to
> 13:44:59 (2026-08-19: `202608` last print 13:29:58, `202609` 13:44:59). The
> 13:44:59 figure in §2.6 belongs to the month the alias had already rolled to.
> Nothing in the code depends on the wrong number — `RollWatch` watches the
> alias, not the clock — but the lapse backstop is 15 minutes shorter than that
> sentence implies.

**Also noticed, not acted on:** TAIFEX lists a calendar-spread instrument
(`202608/202609`, 2 174 prints on 2026-08-19) — the purpose-built way to roll
both legs as one trade, with no leg risk. This system does not use it and W2 does
not propose starting; recorded so it is not re-discovered from scratch.

Measurement scripts: `tools/roll_at_measure.py` (per-bucket, two-month) and the
execution-moment/volatility figures in this section. Offline.

### C4a — `front_month_at` splices 1 d 8.5 h before the tape does · **FOUND 2026-08-25 · silent · NIGHT**

Found while wiring C4's per-policy `ROLL_AT`, because the two share `roll_at`.

`collapse_front_month` states the requirement exactly: the cache splice "has to
be the same rule the live stream switches on — otherwise the warm-up context and
the tape would disagree for **a day and a half every month**, and nothing would
detect it", and names the evaluated convention as "spliced by *calendar*,
switching **one day after each expiry**".

`front_month_at` does not implement that. It flips at
`roll_at(month)` = last trading day − `ROLL_LEAD_DAYS` at `ROLL_AT`, which is one
day *before* expiry. Measured against `TXF_RPT_minute_night.csv`, every month
from 202601 to 202608:

| | tape switches | `front_month_at` flips | gap |
|---|---|---|---|
| 202606→202607 | 2026-06-18 00:00 | 2026-06-16 15:30 | **1 d 8 h 30 m** |
| 202607→202608 | 2026-07-16 00:00 | 2026-07-14 15:30 | **1 d 8 h 30 m** |
| 202608→202609 | 2026-08-20 00:00 | 2026-08-18 15:30 | **1 d 8 h 30 m** |

Identical every month — 1 d 8 h 30 m, which is the gap the comment predicted, to
the hour. (January and February show 15 d and 6 d because of New Year and Lunar
New Year; the tape's rule is "the session after the expiry", not "+24 h".)

**What it costs:** for ~1.5 days each month the backfilled warm-up context is
taken from a different delivery month than the evaluated series used at the same
timestamps. It is not an error anywhere — both months are real, both print, and
the frame is well-formed. It is exactly the "nothing would detect it" case.

**Not fixed here, and C4's change was kept clear of it.** `front_month_at` is
pinned to the module default and is **not** policy-keyed, so giving DAY a 12:45
`ROLL_AT` did not move any deployment's splice. Fixing it means changing the
warm-up context of the pinned NIGHT deployment, three weeks before the first
live roll — the user's call, not a side effect. A test asserts the splice did
not move (`test_the_cache_splice_rule_was_not_moved_by_this_change`).

**Superseded 2026-08-25 by `TAPE_REBUILD_PLAN.md`.** Investigating this found a
larger defect underneath it: the tape holds the expiring contract past its
13:30 death on the last trading day, so **~540 night bars and 15 day bars are
missing every month** — data the live path's own fallback would have filled.
That reverses the fix. The code's rule is the one a live deployment can execute
(a roll must precede expiry); the tape's cannot be reproduced live. So the
**tape** is rebuilt to splice at `roll_at`, not the code changed to match the
tape. C4a then dissolves rather than being fixed. Two decisions and the
sequencing constraint — it must land before W5 — are in that plan.

### C5 — `outputs/bars_cache.csv` is shared across deployments · build, small

`cli.py:502` hardcodes one cache path. A DAY run writes DAY bars into the file a
NIGHT run reads. Not silent — `warm_up` raises at `runtime.py:562` — but it
blocks whichever soak starts second, and the night soak is the one accruing V4's
fill-gap number. **Proposed:** key the cache by policy.

### C6 — emitter priming: the 30-day default is a night-sized number · **quantified**

`_prime_emitter` replays up to `emitter_prime_boundaries = 500` boundaries; below
**3** samples `_check_primed` refuses to start, because `_trend_confirms` fails
**open**. K=500 was measured: only **3.74 %** of the pinned run's 40 443 signals
are non-HOLD, longest HOLD run **385 boundaries**. A full prime needs
`3008 + 500 × 30` = **18 008 bars**.

| | bars/session | trading days needed | `--warmup-days` needed | boundaries at the default 30 |
|---|---|---|---|---|
| NIGHT | 840 | 21.4 | ~30 — the current default **is** the design point | **487** |
| DAY | 300 | 60.0 | **~85 calendar days** | **109** — 22 % of the design |

At 109 boundaries the expected sample count is ~4 against a floor of 3. **A DAY
run on today's defaults sits one quiet fortnight from refusing to start.**

**Proposed:** derive the warm-up window from the policy's bars-per-session.

### C7 — an 85-day warm-up cannot be fetched · **the hard one**

Expired months are delisted (§2.8 Finding D), so an 85-day window spans ~3
delivery months of which the older two no longer answer `kbars`; and
`cli.py::_earlier_months` fetches `earlier[-2:]` — front plus one, another
night-sized number. The cache is the only other source and **starts empty**, so a
first DAY run is short ~2 months of context and cannot recover except by waiting.

**Proposed:** seed the day cache offline from `TXF_RPT_minute_day.csv` in the
cache's four-column schema (~40 lines, testable). It mixes two bar sources in one
context window, which V1c only ever compared on a single session.

> **DECIDED 2026-08-24 — D4: acceptable.** The seeder is in scope (step W3); the
> port does not wait ~2 months for a cache to fill from live.

### C8 — `forced_close=False` inherits a night-measured risk profile · **research**

§2.12 settled it on measured facts: median hold **4 d 11 h**, 89 % of trades
spanning more than one calendar day. Those are properties of the *night* config;
the DAY equivalent is unknown until C1 produces one. A DAY position held with
`forced_close=False` rides the night session and the weekend unmanaged.

The `FLATTEN` weakness is worse here: a flatten is `LMT`+`ROD`, ROD is cancelled
at session end, and a DAY session hands that failure a **13:45 close every day**
instead of 05:00. V4's aggressive order type is a prerequisite, not a nice-to-have.

---

## 5. Order of work

**D1 chose Shape A, so nothing precedes W0.** The M-steps are **not being done**
and are kept only as the record of what Shape B would have cost — corrected,
because M3–M5 as written would not have run:

```
M0  [DONE 2026-08-24] day's July work committed as 7fd3fa6 -> verified: both trees clean
M1  [DONE 2026-08-24] shioaji installed + pinned ==1.7.2   -> verified: imports clean, both trees pinned
M2  [DONE 2026-08-24] day venv rebuilt, uv-managed 3.12.13 -> verified: pyvenv.cfg home is uv, not conda
M3  [NOT DOING - Shape A] fetch night's branch into day/   -- as written it could not run: origin carries
                                                              NEITHER branch (0). And the merge base is
                                                              aae8473, not 2716f49 -- the stated
                                                              verification was a false fact.
M4  [NOT DOING - Shape A] resolve the collision            -- 14 files, not 9: 1 modify/delete,
                                                              7 add/add, 6 content (2)
M5  [NOT DOING - Shape A] reconcile DEFAULT_DATA + CLAUDE.md -- the DEFAULT_DATA half is already closed
                                                              by ed0a1ba; the CLAUDE.md half is open (2.1)
```

M0–M2 were worth doing regardless of D1: the conda-sourced venv and the floating
`shioaji` spec were defects in their own right, and the pin now protects the
night tree too.

**The work itself — all of it in `night/forecast_eval`:**

> **CURRENT STAGE, 2026-09-01 (end of day): W0–W7 are all done. The entire
> offline half of the port is complete; W8 is the first step that needs a
> market, and the first that needs Shioaji credentials.**
>
> | | |
> |---|---|
> | Done | W0, W1, W2, W3, **W4**, W4b, **W5a**, **W5b**, **W6**, **W7** — all offline, no market |
> | Done but PROVISIONAL | **W5a**, **W5b** — measured against Calmar 16.3122, which the tape rebuild invalidates; both re-measure after T0–T4 (§2.3) |
> | Available now | *(nothing offline remains)* — every step that does not need a market is done |
> | Not started | W8, W9 — both need a market, and W8 needs Shioaji credentials the W6 check did not have |
> | Prerequisite for any pinned row | ~~commit the tree~~ — **done**, tree is clean at `e073e73` |
>
> Nothing here is waiting on a decision except W5a. The port has not begun to
> touch the entry point yet — **no day deployment exists**; what is built so far
> is the per-policy machinery (`SessionPolicy`, cache keying, `ROLL_AT`,
> warm-up derivation, the RPT seeder) that a DAY deployment would stand on.

```
W0  [DONE 2026-08-24] session-tag results.tsv + schema     -> verified: a Calmar-99 DAY row leaves the
    + dataset vintage + warmup_bars columns (2.2)             NIGHT pin untouched; 5 new tests
W1  [DONE 2026-08-24] C3 rolled-leg + C5 cache path        -> verified: 3 + 4 tests fail against the old
                                                              literals, pass with the fix
W2  [DONE 2026-08-25] C4 ROLL_AT on the policy           -> verified: DAY 12:45 (window ends exactly at
                                                              the 13:45 close), NIGHT 15:30 unchanged;
                                                              9 new tests. C4a found, NOT fixed.
W3  [DONE 2026-08-24] C7 cache seeder             [D4 ok] -> verified: 30 000 DAY bars seeded, 0 outside
                                                              the DAY policy; 10 new tests
W4  [DONE 2026-08-31] C6 - policy-derived warm-up window    -> verified: SessionPolicy.bars_per_session()
    (NIGHT=840, DAY=300 min/session) + LiveConfig.warmup_       (round(target_bars / bars_per_session * 7/5),
    days_default() replace the flat 30-day constant             target_bars = context + stride*500 = 18008)
                                                              NIGHT reproduces 30 exactly (unchanged CLI
                                                              default), DAY computes 84 (~85 quoted); wired
                                                              into cli.py's --warmup-days default; 2 new tests
W4b [DONE 2026-08-24] re-baseline on the NEW tape (2.2)   -> verified: 185 trades reproduce bit-identically;
                                                              208 trades, Calmar 16.3122 on the new tape
W5a [DONE 2026-09-01, PROVISIONAL] [D2] TRANSFER PROBE     -> verified: Calmar 0.2671 vs NIGHT 16.3122.
    - night-pinned params over the day file, warmup 674498      Does NOT transfer: dd -2857 -> -11649 on a
    bars (aligned to the NIGHT eval start so the calendar       fifth the PnL, fee_drag 0.351 vs 0.028,
    windows match). Ran on the PROVISIONAL branch - see the     18.65 gross pts/trade vs 230.42. Verdict:
    blocking note below; its number is re-measured after        the search IS warranted, for a reason the
    the tape rebuild regardless of the verdict.                 probe alone would have got backwards - see
                                                               2.3. 257 trades, all tagged DAY.
W5b [DONE 2026-09-01, PROVISIONAL] C1 - day search         -> verified: band 0.001 + min-prob 0.65 ->
    -> PINNED DAY row, band 0.001 / min-prob 0.65,              Calmar 4.6805 (73 trades), 17x the probe.
    Calmar 4.6805. Selected on WORST-HALF Calmar, not           Reproduced through the CLI; the swept and
    full-window - the full-window max is a 27-trade             CLI numbers agree exactly. Rejected cell
    artefact and is in results.tsv as a `reject` row.           (band 0.004, Calmar 14.7697) recorded as
    See 2.3 for the method and the caveat.                      evidence. NOT a live pin - see 2.3.
W6  [DONE 2026-09-01] C2 - `--config night|day` at the      -> verified: banner reads
    entry point; PINNED_DAY + a CONFIGS registry; no             LiveConfig[fdd1348 calmar=4.6805 [DAY]]
    `--session` flag, and a test that there never is one.        band=+-0.001 min_prob=0.65, warm-up 84d,
    --warmup-days now resolves from the SELECTED config          roll_at=12:45, cache=day - and NIGHT is
    (argparse bakes parser-build-time defaults: it was           unchanged. One flag moves policy + band +
    always NIGHT's 30, which would have re-opened C6).           gate + warm-up + roll + cache together.
                                                               15 new tests.
W7  [DONE 2026-09-01] offline parity for a single-session   -> verified: bit-identical for BOTH deployments
    deployment, V0-style. V0 is session-blind (it drives         over a mixed tape - DAY 10500 bars / 250
    LiveLoop directly); W7 filters through the REAL              forecasts / 33 fills, NIGHT 29225 / 874 /
    TickAggregator on each policy, then compares against         169. Non-vacuous: 2 mutation checks, both
    run_backtest over that session's bars alone - which          caught. DAY+NIGHT = 39725 in-session bars,
    is what results.tsv's DAY rows actually assume.              matching V0's own count exactly. 9 tests,
    Uses each deployment's OWN pinned emitter.                   no GPU needed.
W8  5-minute dryrun inside a real DAY session   [MARKET]   -> verify: start-up, warm-up, banner, first bar
                                                              [DOES NOT touch the shared Shioaji account —
                                                              dryrun never calls a broker order API]
W9  soak                                        [MARKET]   -> verify: V5's terms, applied to DAY
                                                              [DOES touch the shared account — same
                                                              contract/account as the NIGHT soak. Unresolved:
                                                              separate simulation credentials for DAY, or
                                                              sequence the two soaks instead of running both
                                                              --broker shioaji at once. Decide before W9, not
                                                              during it — see SHIOAJI_PLAN.md's own account-
                                                              conflation history (the 6-lot startup_refused).]
```

W0–W5 need no market. **W5 is the long pole**; everything downstream waits on it.
C3 (inside W1) is worth doing on its own merits whatever is decided here, and
**W4b is a prerequisite for trusting W5b's acceptance test at all** (§2.2).

> **W5 is blocked on `TAPE_REBUILD_PLAN.md`, and that plan's constraint is not
> resolved by lifting D3's hold.** Recorded here because §5 is the sequencing
> document and, until 2026-09-01, the constraint lived only in C4a's superseded
> note where nothing reading this list would find it.
>
> That plan states it as §4: *"This lands before `DAY_SESSION_PLAN.md` W5, or
> W5 is wasted."* The reason is measured, not stylistic — the evaluated tape
> keeps the expiring contract past its 13:30 death on the last trading day, so
> **~540 night bars and 15 day bars are missing at every roll**, and the
> rebuild re-attributes **~6.3 %** of night bars and adds ~60 000 more. **That
> invalidates W4b's Calmar 16.3122 — which is precisely the number W5a's
> acceptance test compares against.** Run W5a first and its verdict is measured
> against a baseline scheduled for replacement, so a "no" cannot be
> distinguished from a tape artefact and a "yes" has to be re-run anyway.
>
> **Two things gate the rebuild itself and are the user's, not assumed here** —
> `TAPE_REBUILD_PLAN.md` §6's **D1** (re-tune NIGHT now, or validate the new
> tape in parallel and leave the pin alone through the 2026-09-15 roll — that
> plan recommends the latter) and **D2** (whether the combined
> `TXF_RPT_minute.csv` survives). Neither has been taken.
>
> **The order that follows from this:** rebuild T0–T4 → re-run W4b on the new
> tape → W5a → W5b. Alternatively W5a runs now as an explicitly *provisional*
> probe, on the stated understanding that its number is re-measured after the
> rebuild regardless of the answer. Either is defensible; what is not is
> running W5a without recording which of the two was chosen. **W4, W6 and W7
> are unaffected** — none of them reads the tape — so they can proceed in
> parallel with the rebuild if progress is wanted meanwhile.

> **SUPERSEDED 2026-08-31 — D3's hold is lifted.** *(Original, kept for the
> record: "HELD 2026-08-24 — D3. None of W0–W9 starts until the user says so.
> This is the plan of record, not a queue in progress.")* The user directed
> the migration to proceed after progress stalled over a week on time-cost
> testing concerns. W4 above is the first step done under this direction.
> **W5a is next in the list but is not unblocked by this** — lifting D3
> removed the *hold*, not the tape-rebuild *prerequisite*; see the blocking
> note above. W6 and W7 are the steps that can proceed with nothing pending.

---

## 6. Decisions — **all five taken 2026-08-24**

- **D1 — which tree hosts the day deployment?** **DECIDED: Shape A.**
  `night/forecast_eval` hosts it, the session stays a parameter, and
  `day/forecast_eval` stays the research tree it already is. §2's merge is not
  happening; §5's M3–M5 are cancelled.
- **D2 — how much re-tuning?** **DECIDED: probe first.** Run the cheap transfer
  baseline — night-pinned parameters over `TXF_RPT_minute_day.csv` unchanged, one
  ~4-minute run — and read the Calmar and the median hold before deciding whether
  to launch a full `edit:`/`keep:` search. Sequenced as W5a → W5b.
- **D3 — sequencing.** **DECIDED: hold.** The offline half does not start now;
  work begins when the user says so. (Context unchanged: V5 is not passed and the
  first live roll is 2026-09-15.)
- **D4 — RPT-seeded cache.** **DECIDED: acceptable.** Mixing RPT and `kbars` in
  one context window is fine. W3's seeder is in scope, and the port does not wait
  ~2 months for a cache to fill from live.
- **D5 — where day's July research work ends up. DECIDED: it stays where it is.**
  Shape A already removed the merge. The user has now also ruled out **any**
  cross-tree transport — no push to GitHub, and no local remote between the two
  folders — which closes the cherry-pick option too. Day's July work therefore
  remains commits `7fd3fa6` and `ed0a1ba` on `autoresearch_result_1` in
  `day/forecast_eval`, as the record.

  **Consequences, so they are not rediscovered later:**

  - §2's 14-file collision list is now purely historical. Nothing will ever be
    resolved against it.
  - Night does **not** get day's `reports.py`, `metrics.py` or `threshold.py`
    work. If any of it is wanted in night later, it is rewritten there, not moved.
  - The work exists in exactly one place, on one disk, with no second copy —
    `origin` last saw this repository on 2026-06-10. That is a durability fact,
    not an argument to publish; a backup that is not a git remote satisfies it.
  - `origin` (public, `main` at `196f5f0`) stays as it is. Neither working branch
    is on it and neither is going on it.

---

## 7. Not covered here

The day-session **backtest result** itself (that is W5's output); anything about
real money (V4's fill gap is a hard gate for either session); and the porting
*guide* that `docs/DOCS_PLAN.md` records as debt — this plan's eventual
deliverable, which cannot be written as a procedure until W8 has been run once.
