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

Nothing has been built, no decisions taken, no run made. Five decisions in §6 are
the user's and are not assumed anywhere above them.

---

## 0. What `day/forecast_eval` actually is — measured, not assumed

**It is this repository's own ancestor, one whole subsystem behind.** Both folders
are clones of the same GitHub remote (`smolsquirrel936/forecast_eval`), and the
day tree's HEAD is a commit that `night/` already contains.

| | |
|---|---|
| Day HEAD | `7fd3fa6` "Saved" — **2026-08-24** (was `2716f49`, 2026-07-23) |
| Its relation to night | **diverged.** `2716f49` is an ancestor of night's `bb692d6`; `7fd3fa6` is **not in night** |
| Night ahead by | **24 commits**, **+25 028 / −1 792 lines across 138 files** (measured from `2716f49`) |
| Day ahead by | **1 commit**, +1 577 / −205 across 18 files — the July research work, committed 2026-08-24 |
| Day branch | `autoresearch_result_1` (also carries `Strategy_2_1_7`, `main`) |
| Shared remote | `origin` → `smolsquirrel936/forecast_eval`, both trees |
| Working trees | **both clean** |

What night gained in those 24 commits is exactly what this port would need: the
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
content in the folder not reproducible from night. **It is now safe** — a commit
rather than a dirty tree, so it can be merged, cherry-picked or reverted.

**The cost is that the branches have diverged.** `7fd3fa6` is not in night, so the
fast-forward that was available on 2026-08-23 is gone; Shape B is now a real
merge. That is the better shape of the two — collisions resolve at commit level
instead of being clobbered in a working tree — but it is no longer free.
Decision **D5** is now about *where these 18 files should end up*, not about
rescuing them.

---

## 1. The consequence — two shapes, and the migration is only one of them

**Shape A — the day deployment is hosted in `night/forecast_eval`.**
The live stack is already there, the session is already a parameter, and the port
is the C-item work in §5 plus `--config day`. `day/forecast_eval` stays what it
is: an older research tree. No merge, no collision, nothing to reconcile.

**Shape B — `day/forecast_eval` becomes the day deployment.**
Then the *first* task is not the port at all: it is bringing 24 commits and
25 000 lines of `live/` across, resolving the collision in §2 — and after that,
every C-item in §5 still has to be done, identically. **Shape B is Shape A plus a
merge.** The merge is a hosting decision, not migration progress.

A third possibility worth naming so it is not chosen by accident: **the folder
layout implies Shape B, and picking it for that reason alone would be paying the
merge cost to get a copy of a codebase whose design already makes the session a
parameter** (§2.12: "no module names a session"). Two trees is the fork that
`SessionPolicy` exists to make unnecessary.

---

## 2. The merge collision, if Shape B

**Updated 2026-08-24: the fast-forward is gone.** Day's `7fd3fa6` is not in
night, so the branches have diverged and this is a three-way merge against the
common ancestor `2716f49`. Of the 12 tracked files that commit touched, **9 are
also changed by night since the split**:

```
compare_models.py   forecaster/toto2.py   logging_io.py   metrics.py
real_data.py        reports.py            squirrel_note.md
strategy/threshold.py                     tests/test_metrics.py
```

Only **`run.py`, `trader.py`, `tests/test_confidence_placement.py`** are clear of
it — and that is not luck: SHIOAJI_PLAN's standing ground rule is that
`run.py`, `execution.py`, `trader.py` and `portfolio.py` stay unmodified, and 24
commits later they still are.

The formerly-untracked files are a second, quieter problem: night tracks all of
them, and one — `main.py` — night **deleted on purpose** (`docs/DOCS_PLAN.md`
records it as a 91-byte print stub removed as a coverage gap). Now that
`7fd3fa6` commits it, a merge re-adds it as a genuine change rather than an
untracked-file collision, which is easier to miss.

`strategy/threshold.py` is the collision that matters most: it is where the night
tuning chain did its work (`min_trend_ticks`, `path_momentum`, the band), and
day's +122 lines are a different edit to the same file. **Resolving that one
wrong silently changes the strategy** — and the night side of it is load-bearing
for `live/config.py`'s pinned Calmar.

Both working trees are clean, so the merge is now a normal one with no dirty
state on either side.

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
| `real_data.DEFAULT_DATA` | **still dangling** — see below | UNC path, valid |
| `UV_SETUP.md`, `AGENTS.md` | **still absent** | present at `night/` |
| `CLAUDE.md` | **still 2026-06-11**, predates all live work | current |

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
and assert the trades and net PnL reproduce bit-identically, ~4 minutes. It
cannot run in `day/` until there is a pinned day config and a valid data path,
so it becomes the acceptance test for D5's output.

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
3. **`real_data.DEFAULT_DATA` is dangling — STILL OPEN.** It resolves to
   `day/dataset/RPT/TXF_RPT_minute.csv` and **there is no `dataset/` under
   `day/`** — night's code uses the UNC `\\DESKTOP-SQUIRRE\dataset\RPT\…` path
   instead. Every run in `day/` must pass `--data` explicitly until this is
   reconciled, and C1's tuning run must name the day CSV anyway.

`CLAUDE.md` being three months stale is not a runtime problem but is an agent
problem: it documents neither `live/`, nor the uv/cu130 notes, nor the
folder-scope rule.

---

## 3. What ports free — verified by reading

These are properties of the **night** codebase, which is where `live/` exists.
Under Shape A they are already true; under Shape B they become true on the day
the merge lands.

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
C3, C5, C6 and C7 name files that do not exist — which is §0's point, not a
separate problem.

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
against `--data <day file>`. **This work needs `results.tsv` and the tuning loop,
so it happens wherever those live — today, only `night/`.** Scope: decision D2.

### C2 — the entry point cannot express a DAY deployment · build, small

`cli.py:314` is `cfg = PINNED`; `LiveConfig` is frozen. A bare `--session` flag
would be the 2026-08-11 defect by the front door: changing the policy without
changing the *strategy* runs night-tuned parameters on a day tape, undetectably.

**Proposed:** `--config night|day` selecting the whole config, policy travelling
inside the value it belongs to.

### C3 — `settle_rolled_leg(session="NIGHT")` · build, one line · **silent**

`live/runtime.py:285` books a roll-settled leg to NIGHT by literal default. On a
DAY deployment every rolled round-trip is attributed to a session the run never
traded, corrupting `compute_metrics`' day/night split. **The only genuinely
silent defect on this list, and a defect in the night tree today regardless of
whether this port happens.**

**Proposed:** derive from the fill's timestamp (`classify_session`) or the policy,
plus the regression test that fails on the literal.

### C4 — `ROLL_AT = 15:30` was measured for a night strategy · **measurement**

`live/roll.py:183-190` derives it from the expiring month's final *night* session
opening at 15:00 the day before its last trading day, whose first half hour is the
most liquid stretch either month sees. For a DAY deployment 15:30 is **outside
the traded session** — the clock still fires, so the ladder runs, unwatched and
priced off a tape the run is not following. The DAY analogue is presumably the
first half hour after 08:45; **presumably is not measured.**

**Proposed:** re-derive from the RPT day file (per-minute volume on both months
across the last three rolls) and carry `ROLL_AT` on the policy or config so two
deployments cannot share the wrong one. Offline.

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
cache's four-column schema (~40 lines, testable). Mixes two bar sources in one
context window, which V1c only ever compared on a single session — decision D4.

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

**Shape A** — nothing precedes D0.
**Shape B** — everything below is preceded by:

```
M0  [DONE 2026-08-24] day's July work committed as 7fd3fa6 -> verified: both trees clean
M1  [DONE 2026-08-24] shioaji installed + pinned ==1.7.2   -> verified: imports clean, both trees pinned
M2  [DONE 2026-08-24] day venv rebuilt, uv-managed 3.12.13 -> verified: pyvenv.cfg home is uv, not conda
M3  fetch night's branch into day/ (shared origin)         -> verify: merge base is 2716f49
M4  resolve the 9-file collision (2)                       -> verify: 744 tests green in day/
M5  reconcile real_data.DEFAULT_DATA + CLAUDE.md (2.1)     -> verify: a bare run finds its data
```

M0–M2 are done; **M3 onward is the actual migration and none of it has started.**
Note M1/M2 were worth doing regardless of D1: the conda-sourced venv and the
floating `shioaji` spec were defects in their own right, and the pin now protects
the night tree too.

Then, in either shape:

```
D0  Session-tag results.tsv + a DAY row schema             -> verify: test_live_config green with two pins
D1  C3 (rolled-leg session) + C5 (cache path)              -> verify: new tests fail against the old literals
D2  C4 - re-derive ROLL_AT from the RPT day file           -> verify: a measured table here, like 2.6's
D3  C7 - cache seeder from TXF_RPT_minute_day.csv          -> verify: seeded cache passes warm_up's policy check
D4  C6 - policy-derived warm-up window                     -> verify: DAY asks ~85d, NIGHT still 30d
D5  C1 - tune on the day file, real_data.py (scope: D2)    -> verify: a pinned row + reproduction, 2.13-style
D6  C2 - `--config day` at the entry point                 -> verify: dryrun banner shows DAY provenance
D7  offline parity for DAY, V0-style                       -> verify: live loop = run_backtest, bit-identical
D8  5-minute dryrun inside a real DAY session   [MARKET]   -> verify: start-up, warm-up, banner, first bar
D9  soak                                        [MARKET]   -> verify: V5's terms, applied to DAY
```

D0–D4 need no market. **D5 is the long pole**; everything downstream waits on it.
C3 is worth doing on its own merits whatever is decided here.

---

## 6. Decisions needed — the user's, not assumed

- **D1 — which tree hosts the day deployment?** Shape A (`night/`, one codebase,
  the session stays a parameter) or Shape B (`day/`, paying §2's merge first). The
  folder layout suggests B; the design argues A. **Recommendation: A**, with
  `day/` kept as the research tree it already is.
- **D2 — how much re-tuning?** A full `edit:`/`keep:` search on the day file, or
  first a cheap **transfer baseline**: night-pinned parameters over
  `TXF_RPT_minute_day.csv` unchanged, read the Calmar and the median hold. One
  ~4-minute run, and it answers whether the search is worth launching.
- **D3 — sequencing.** DOCS_PLAN's 2026-08-11 decision was "after V5". V5 is not
  passed and the first live roll is 2026-09-15. Does the offline half (D0–D4)
  start now in parallel, or wait?
- **D4 — RPT-seeded cache.** Acceptable to mix RPT and `kbars` in one context
  window, or does the port wait ~2 months for a cache to fill from live?
- **D5 — where day's July research work ends up.** ~~Deadline~~ **safe since
  2026-08-24**: it is commit `7fd3fa6` on `autoresearch_result_1`. The open
  question is now direction — merge night into day (Shape B), cherry-pick the
  parts still wanted into night (Shape A), or leave it on its branch as a record.
  Note it collides with 9 files night also changed, `strategy/threshold.py` worst
  of all.

---

## 7. Not covered here

The day-session **backtest result** itself (that is D5's output); anything about
real money (V4's fill gap is a hard gate for either session); and the porting
*guide* that `docs/DOCS_PLAN.md` records as debt — this plan's eventual
deliverable, which cannot be written as a procedure until D8 has been run once.
