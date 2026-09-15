# DAY/NIGHT reset to Toto2 — plan

**Status: COMPLETE.** Phases 0–8 were executed on 2026-09-14 for day and night (§7), and
the same reset was applied to fullday (§8). All gates passed. Both resets are merged into
`main` and were pushed on 2026-09-15 (`origin/main` at `7fb7e8f`). What actually happened,
including where it departed from the text below, is in §7 and §8. Later edits to the session
plans are noted where they apply.

**Goal.** Return `strategy/day/` and `strategy/night/` to their pre-RSI state —
the pinned Toto2 threshold strategy each package held at the split — so a fresh
autoresearch loop can start from a clean, reproducible baseline in both sessions.
Delete the RSI strategy outright rather than leaving it dormant. `fullday` was out of scope
for this plan as written; §8 extended the reset to it on 2026-09-14, at the user's request.

---

## 1. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | **Target = `20767bb`'s contents of `strategy/{day,night}/` and `docs/strategy/{day,night}/`** — the last commit before `a8ddd85` switched both packages to RSI. | That is the state STRATEGY_SPLIT_PLAN's gate G1 proved reproduces `results.tsv` exactly, so the reset has a pass/fail check (§4 G3) instead of a judgement call. `333e3dd` (plain RSI) was the other candidate; rejected because the new loops will research on Toto2 forecasts. |
| D2 | **Restore by path on a new branch. Never `git reset --hard` `main`.** | Infrastructure commits are interleaved with the loop commits since `20767bb`: `af4be0b` (report, fee default 0.00015 → 0.0002), `bfd4338` (live routes through `make_strategy`, `param_override.py`, `--param`, `real_data.py` knob relaxation D6, 1.5k lines of live tests), `1900714` (fullday iter01–05), `33496c6` (D10). A hard reset discards all of them. |
| D3 | **Delete RSI entirely:** `strategy/{day,night}/rsi.py`, `docs/strategy/{day,night}/rsi.md`, `tests/test_rsi_strategy.py`. | A dormant `rsi.py` sits inside the mutable surface and invites a re-probe of the RSI × Toto2 family that day iter08–12 already exhausted. It stays recoverable from the archive tags (D4). |
| D4 | **Archive before touching anything:** git tags, run folders, results files, plan files. | The restore is a deletion of two research runs' worth of code; the tags are what make it reversible. |
| D5 | **Old baselines are not carried.** Both loops re-establish `iter01` from the restored package. | The RSI baselines measured a strategy that no longer exists, and the pre-split Toto2 numbers were measured at fee 0.00015 — the harness now defaults to 0.0002. |
| D6 | **The live consequence is accepted, not incidental** (pending confirmation, §6 Q1). | Since `bfd4338`, `LiveRuntime` builds from `make_strategy`, so this reset moves DAY/NIGHT *live* from RSI back to Toto2 in the same edit. That is the follow-up D9 recorded as intended, and it closes D10 (night's `TimeStop` goes away). |

---

## 2. Current state (measured 2026-09-14)

**Repo** (`forecast_eval/`, branch `main` at `1c7901d`, clean tree, ahead of
`origin/main` by 5 — the day iter08–12 tsv commits). One worktree. **No stashes:**
the two from `autoresearch_result_1` (2026-07-08, 2026-08-05) were judged obsolete
and dropped by the user on 2026-09-14; nothing in this plan depended on them.

**What the loops added on `main`:**

| session | final state | code changes vs `20767bb` |
|---|---|---|
| day | iter05, Calmar 7.4448 (122 trades) | `rsi.py` (RSI + SMA-6000 regime gate), `trader.py` long-only, `wiring.py` RSI pair |
| night | iter09, Calmar 12.0594 (464 trades) | `rsi.py` (B30/S84, entry window 15–20h, depression exit), `trader.py` long-only, `wiring.py` RSI pair + `TimeStop(4000)` |

`emitter.py` and `forecaster.py` are **unchanged** in both packages since the split.
`bfd4338`'s edits to both `wiring.py` files are **comments only** (no code), so
`20767bb`'s `wiring.py` loses nothing functional.

**Loop-added tests** (all pure additions, no shared test was edited by a loop):
`test_day_regime_gate.py`, `test_day_trader.py`, `test_night_trader.py`,
`test_night_wiring.py`, `test_night_entry_window.py`, `test_night_depression.py`.

**RSI mentions outside the two packages** (need a wording update, not a code change):

| file | line(s) | what it says |
|---|---|---|
| `docs/live/config.md` | 59, 74 | "night and day deployments now trade RSI" |
| `docs/strategy/wiring.md` | 45 | "RSI, so that is what they trade live (plan D9)" |
| `docs/strategy/fullday/combine.md` | 68 | cross-reference to `rsi.py` constants |
| `tests/conftest.py` | 24 | `cheap_strategy` docstring: "night and day build an RSI strategy" |
| `tests/test_live_parity_sessions.py` | 60, 357 | `_emitter_for` docstring; historical note |
| `tests/test_live_config.py` | 221 | historical note — accurate as history, leave |
| `tests/test_live_owner_adoption.py` | 30–36 | comment: night returns a `TimeStop` |
| `LIVE_STRATEGY_ROUTING_PLAN.md` | 72–73, 131, 576–578 | D9, D10, §5 known state |

**Environment — broken, and by this session.** On 2026-09-14 18:10 a
`uv run python -m pytest` issued while drafting this plan **recreated `.venv/`**
(uv judged the existing venv incompatible and rebuilt it from `pyproject.toml`,
which declares only `shioaji`). It now holds shioaji alone: no torch, toto-2,
pandas or pytest. Nothing in §4 can run until Phase 0 is done.

---

## 3. Phases

`git` runs against `forecast_eval/`; `uv run` runs from `night/` (AGENTS.md).

### Phase 0 — rebuild the environment

1. Follow `UV_SETUP.md` Steps 2–5 in order (torch from the **cu130** index first,
   then `toto-2`, then the test/plot stack incl. `pytest`).
2. Verify: `uv run python -c "import torch; print(torch.cuda.is_available())"` → `True`;
   `uv run python -m pytest forecast_eval/tests/ -q` on **unmodified `main`** and
   record the pass count as `N_main` (bfd4338 recorded 1537 passed + 1 known
   `volmultitf` CRLF failure).
3. Environment parity is re-proven in Phase 5 G3 — do not run a separate
   `4282d33` check here.

### Phase 1 — archive (D4)

```bash
git -C forecast_eval tag archive/day-rsi-iter05   1c7901d
git -C forecast_eval tag archive/night-rsi-iter09 c70bb38
git -C forecast_eval tag archive/pre-toto2-reset  1c7901d
```

- Move `outputs/iter*_day_*` and `outputs/iter*_night_*` (folders and `.log` /
  `.console.log`) into `outputs/archive_rsi_2026-09/`. Keeps the plan rule
  "never reuse an `iterNN`" satisfiable when the new loops start at `iter01`.
  Leave `iter*_fullday_*` in place.
- Copy `autoresearch_trading_plan{,_day,_night}.md` (repo root `night/`, **not
  under git**) to `outputs/archive_rsi_2026-09/plans/`. They are about to be
  rewritten and nothing else preserves them.
- `results_day.tsv` / `results_night.tsv` need no copy — they are tracked, and
  the tags hold them.

### Phase 2 — branch

```bash
git -C forecast_eval switch -c daynight_toto2_reset main
```

### Phase 3 — restore the packages (D1)

```bash
git -C forecast_eval checkout 20767bb -- \
    strategy/day strategy/night docs/strategy/day docs/strategy/night
git -C forecast_eval rm strategy/day/rsi.py strategy/night/rsi.py \
    docs/strategy/day/rsi.md docs/strategy/night/rsi.md
```

`checkout <commit> -- <dir>` overwrites files but does not delete ones that did not
exist at that commit — hence the explicit `rm`. Check:

```bash
git -C forecast_eval diff --stat 20767bb -- strategy/day strategy/night docs/strategy/day docs/strategy/night
# expected: empty
```

### Phase 4 — remove RSI-only and loop-added tests (D3)

```bash
git -C forecast_eval rm tests/test_rsi_strategy.py \
    tests/test_day_regime_gate.py tests/test_day_trader.py \
    tests/test_night_trader.py tests/test_night_wiring.py \
    tests/test_night_entry_window.py tests/test_night_depression.py
```

Then `grep -rn "rsi\|RsiEmitter\|TimeStop\|test_night_\|test_day_" tests/` and
confirm every remaining hit is in the §2 table.

### Phase 5 — update shared references (wording only)

For each row of the §2 RSI table:

- **Docs** (`docs/live/config.md`, `docs/strategy/wiring.md`,
  `docs/strategy/fullday/combine.md`): state that day and night trade the Toto2
  threshold strategy again, as of this plan. Past-tense history may stay.
- **Test docstrings/comments** (`conftest.py`, `test_live_parity_sessions.py`,
  `test_live_owner_adoption.py`): fix the stated reason. **Keep the
  `cheap_strategy` fixture and `_emitter_for`** — they now exist to avoid loading
  a real Toto2 checkpoint in unit tests, which is still necessary. No assertion
  changes.
- **`LIVE_STRATEGY_ROUTING_PLAN.md`**: do not rewrite D9/D10; append a §5 entry
  "D9/D10 follow-up landed 2026-09-xx via DAYNIGHT_TOTO2_RESET_PLAN" and update the
  "Known state" bullets.

Frozen files (`test_strategy_isolation.py` pins) are **not** touched by any phase.

### Phase 6 — verify (gates in §4)

Run G1 → G2 → G3 in order; stop at the first failure.

### Phase 7 — reset the research records (D5)

- `results_day.tsv`, `results_night.tsv` → header line only (keep the existing
  8-column header; no BOM — the current `results_day.tsv` starts with one, drop it;
  end with a trailing newline, per the two row-repair commits).
- Rewrite `autoresearch_trading_plan_day.md` and `_night.md` (under `night/`):
  - "What the package runs today" → Toto2 + `ThresholdEmitter` + two-sided `Trader`.
  - Locked decisions 6–8 → Toto2 knobs are **live**, forecasts need the GPU and
    `--precompute`, `forecaster_sha` is meaningful again.
    *(Qualified 2026-09-15: the sha covers `forecaster.py` only, not `checkpoint`,
    `context_length`, `horizon_bars` or `stride_bars`; each plan's decision 7 says so.)*
  - Evaluation command → add `--checkpoint`; warmup chosen for Toto2's
    `context_length`, pinned as a bar count with its calendar timestamp.
    *(`--checkpoint` was not added: `--strategy` already supplies it from the package's
    `PARAMS`, so the command does not pass it. The warmup was pinned as written.)*
  - Remove the RSI open item and its "Resolved 2026-09-09" note.
  - Add a short **"Carried lessons"** section (ideas, not code): long-only entries
    were the largest single gain in both sessions and in fullday; fullday's
    stride-consensus (`combine.py`, copy — never import) reached Calmar 12.94; the
    RSI × Toto2 consensus family is exhausted.
    *(Superseded 2026-09-15: the section was removed from all three plans. It steered a
    loop that the reset meant to start clean, its evidence came from other strategies and
    fees, and two of its claims did not match the archived results. The history stays in
    the archive tags and `outputs/archive_*` folders.)*
- Index `autoresearch_trading_plan.md`: both sessions "baseline: none yet — Toto2".

### Phase 8 — land

1. Commit on `daynight_toto2_reset` (one commit for Phases 3–5, one for Phase 7's
   tsv reset), messages in the repo's declarative style.
2. Merge into `main`. Push only on explicit request (§6 Q3).
3. Each new loop then runs in its **own worktree/branch** (index plan, "one loop
   at a time").
   *(Changed 2026-09-15: no worktree. Each loop runs on its own branch
   `autoresearch_<session>_<MMDD>`, forked from the latest `main` in the normal checkout, one
   loop at a time. The session plans' "Before the loop" section has the commands.)*

---

## 4. Gates

| gate | check | pass |
|---|---|---|
| **G1 suite** | `uv run python -m pytest forecast_eval/tests/ -q` | `N_main` − (tests removed in Phase 4) passed, same pre-existing failures, no new ones. Includes `test_strategy_isolation.py` and both `test_docs_coverage.py` directions (no module without a page, no page without a module). |
| **G2 import** | `uv run python -c "from forecast_eval.real_data import main; print('ok')"` and `make_strategy("day"/"night", warmup_bars=0)` returns `ThresholdEmitter` + the Toto2 forecaster with `exit_rule is None` | `ok`, types as stated |
| **G3 parity** | Re-run STRATEGY_SPLIT_PLAN's G1 at the **old fee**: <br>`--strategy night --data …minute_night.csv --warmup-bars 538790 --precompute --fee-rate 0.00015` <br>`--strategy day --data …minute_day.csv --warmup-bars 676123 --precompute --fee-rate 0.00015` | **Exact** match to `results.tsv`: night `4282d33` — 235 trades, net +24 135.5960, dd −6 703.2441, Calmar 3.6006; day `3c3ae38` (W5b) — 67 trades, net +9 748.0117, dd −5 755.5022, Calmar 1.6937. ~260 s + ~100 s on the GPU. |

G3 is the gate that matters: it proves the packages are back to the pinned
strategies **and** that the rebuilt environment scores bit-identically, in one
run per session. Output dirs: `outputs/reset_g3_{night,day}_<ts>/` (not `iterNN`).

**If G3 mismatches:** first confirm the tape vintage is still `@2026-09-01`
(TAPE_REBUILD_PLAN rebuilds in place); then diff `params.json` against the
`results.tsv` description; only then suspect the restore.

---

## 5. Risks

- **Live moves with the backtest (D6).** Any DAY/NIGHT simtrade started from the
  reset branch trades Toto2 and needs the GPU. Do not run live from a research
  worktree mid-loop — `make_strategy` is now a live deployment's definition.
- **Tests that construct day/night strategies may now touch Toto2.** Fullday is
  already Toto2 and the routing tests parametrize over it, so construction is
  expected to be lazy — G1 confirms it. If a test tries to load the checkpoint,
  route it through `cheap_strategy`; do not weaken the assertion.
- **`results.tsv` rows `4282d33`/`3c3ae38` are tape-vintage-bound.** If the tapes
  were rebuilt after 2026-09-01, G3 cannot pass and must be re-baselined instead —
  a human decision, recorded before proceeding.
- **Untracked plan files.** The three `autoresearch_trading_plan*.md` live outside
  git; Phase 1's copy is their only backup.

---

## 6. Open questions

1. **Live:** accept that DAY/NIGHT live switch back to Toto2 when this merges (D6)?
   **Answered 2026-09-14: no live trading is running**, so the merge has no immediate
   live effect. The next DAY or NIGHT live start will trade Toto2 and needs the GPU.
2. **Archive tags:** push them to `origin`, or keep local? **Answered 2026-09-14: keep
   local for now.**
3. **Push:** push `main` after merge? **Answered:** the docs commit was pushed on 2026-09-14
   (`f2b3251`), and the user pushed both reset merges on 2026-09-15 (`origin/main` at
   `7fb7e8f`).
4. **Day's starting knobs:** restore W5b unchanged, or loosen before `iter01`?
   **Answered 2026-09-14: unchanged. The reset returns to the initial state, and the
   best Calmar is decided by the first run.** No recorded Calmar (the pinned rows, the G3
   parity runs, the archived RSI loops) carries into either loop as a bar to beat, because
   an inherited high Calmar would block the loop's progress. Both session plans and the
   index were edited to say so, and their Calmar figures were removed.

## 7. Execution record (2026-09-14)

- **Phase 0.** `.venv` rebuilt per UV_SETUP Steps 2–5: torch 2.14.0+cu130 (RTX 5090
  detected), toto-2, pandas 2.3.3, pytest 9.1.1. `N_main` on `main@f2b3251` =
  **1565 passed, 1 failed, 2 skipped** (the failure is the `volmultitf.py` CRLF pin).
  Verification used `.venv/Scripts/python.exe` directly, not `uv run`, so uv could
  not re-sync the venv.
- **Phase 1.** Tags `archive/day-rsi-iter05` → `1c7901d`, `archive/night-rsi-iter09`
  → `c70bb38`, **`archive/pre-toto2-reset` → `f2b3251`** (not `1c7901d` as written:
  `main` had moved to the docs commit that added this plan). Local only. 31 entries
  (`iter*_day_*`, `iter*_night_*` folders and logs) moved to
  `outputs/archive_rsi_2026-09/`, plus the three plan files under `plans/`.
- **Phases 2–4.** As written. Index diff against `20767bb` for the four restored paths:
  empty.
- **Phase 5.** As written, plus one item §2 missed: `strategy/night/wiring.py`'s docstring
  said the knobs "must stay in step with `live/config.py:PINNED`". That has been false since
  `bfd4338`, so the docstring was reworded. **That is the only departure from
  `20767bb` in the restored packages, and it is docstring-only.**
- **G1.** **1467 passed, 1 failed (same CRLF pin), 2 skipped.** The 98-test drop is fully
  accounted for by diffing collected node IDs against `main`: 92 tests in the seven deleted
  files, plus 6 per-module parametrizations of `rsi.py` (`test_docs_coverage` ×2,
  `test_undefined_globals` ×4). No test was added or renamed.
- **G2.** Pass. day: `strategy.day.emitter.ThresholdEmitter` + `Toto2Forecaster`,
  `exit_rule=None`, band ±0.001 / 0.65. night: same types, ±0.00045 / 0.5. fullday
  unchanged (`ConsensusThresholdEmitter`, ±0.00075 / 0.5).
- **G3.** Pass, exact. Tapes dated 2026-09-01 18:11. Output in
  `outputs/reset_g3_{night,day}_20260914_184243/`.

  | | trades | net PnL | max DD | Calmar | forecasts / time |
  |---|---|---|---|---|---|
  | night (`4282d33`) | 235 | +24 135.596000 | −6 703.244050 | 3.6006 | 43 674 / 263.7 s |
  | day (`3c3ae38`) | 67 | +9 748.011650 | −5 755.502200 | 1.6937 | 15 687 / 101.7 s |

- **Phase 7.** Both results files reset to the 8-column header, LF, trailing newline (the day
  file's BOM dropped). Plans rewritten for Toto2 with the window pinned to the G3 warmups
  (night 538790 → 2020-03-05 18:02; day 676123 → 2020-03-11 08:49) and a "Carried lessons"
  section (removed again 2026-09-15, see Phase 7 above). The index plan was updated. None of the plan files are under git; their
  pre-reset copies are in the archive folder.
- **Phase 8.** Committed on `daynight_toto2_reset` as `0297590` (Phases 3–5) and `a454173`
  (tsv reset), plus `9b56410` and `90bff4c` recording the §6 answers. Fast-forwarded into
  `main`. Pushed with §8's commits on 2026-09-15.

## 8. Extension: FULLDAY reset (requested and executed 2026-09-14)

**Request:** apply the same strategy to the fullday session and reset it too. **Decision
F-D1:** the target is `20767bb`, the same commit as day/night. At that commit
`strategy/fullday/` is day's code verbatim, with day's inherited band (±0.001 / 0.65,
UNTUNED). Restoring it gives fullday the same strategy and the same starting settings, and
drops the fullday loop's Step-0 sweep seed (±0.00075 / 0.5) along with iter03–05. That matches
Q4's principle: the first run decides; no tuned starting point or Calmar carries in.

**What the fullday loop had changed** (`4823e6a` … `30f70d7`, merged in `1900714`): `wiring.py`
(swept band, `ConsensusThresholdEmitter`), new `combine.py`, a long-only `trader.py`, docs for
all of these, two new test files, and **one shared test**: `e773c23` rewrote
`tests/test_strategy_selector.py`'s fullday band test to assert the swept band.
`tools/band_sweep.py`'s fee default change belongs to `af4be0b` (infrastructure) and stays.

**Execution record**

- **F1 archive.** Tags `archive/fullday-toto2-iter05` → `30f70d7` (the loop's tip) and
  `archive/pre-fullday-reset` → `90bff4c`, both local. `iter0{1..5}_fullday_*` (10 entries) and
  the four `fullday_band_sweep*` files moved to `outputs/archive_fullday_2026-09/`. Copies of
  `autoresearch_trading_plan_fullday.md` and the index went under `plans/`.
- **F2–F4.** Branch `fullday_toto2_reset` from `main@90bff4c`. `strategy/fullday` and
  `docs/strategy/fullday` restored to `20767bb` (index diff empty). `combine.py`, `combine.md`,
  `test_fullday_combine.py` and `test_fullday_trader.py` deleted. The fullday band test was
  put back to its `20767bb` form (`test_fullday_is_days_band_and_is_marked_untuned`,
  byte-identical); `bfd4338`'s edits elsewhere in that file were kept.
- **F5 wording.** The restored `20767bb` text claimed two things that are no longer true: that
  the configuration was "never measured" on the tape (the sweep measured it), and that
  `live/cli.py` refuses `--config fullday` (removed by routing D1 in `bfd4338`). Reworded in
  `strategy/fullday/wiring.py`'s docstring (keeping "UNTUNED", which the test asserts),
  `docs/strategy/fullday/wiring.md`, and `docs/live/config.md`'s PINNED_FULLDAY section. That
  last one had been stale since `bfd4338`. A second follow-up was appended to
  LIVE_STRATEGY_ROUTING_PLAN §5.
- **G1.** **1448 passed, 1 failed (the same `volmultitf` CRLF pin), 2 skipped**, against
  `main`'s 1467. Node-ID diff accounts for the 19: 11 tests in `test_fullday_combine.py`, 5 in
  `test_fullday_trader.py`, 3 `combine.py` module parametrizations (docs coverage + two
  undefined-globals checks), and the sweep band test replaced one-for-one by the restored
  test.
- **G2.** fullday builds `strategy.fullday.emitter.ThresholdEmitter` + `Toto2Forecaster`,
  `exit_rule=None`, ±0.001 / 0.65. `params("fullday") == params("day")`. `emitter.py`,
  `trader.py` and `forecaster.py` are identical to day's apart from the docstring's first line.
- **G3F.** There is no `results.tsv` row for fullday, so the target is the 2026-09-09 sweep's
  ±0.001 / 0.65 cell. Its trustworthiness was checked first: the sweep's ±0.00075 / 0.5 cell
  reproduces the loop's recorded iter01 row exactly (206 trades, +32 319.88295, Calmar
  6.2573).

  | | trades | net PnL | max DD | Calmar | forecasts / time |
  |---|---|---|---|---|---|
  | sweep cell (fee 0.00015, warmup 5000) | 97 | +9 835.18935 | — | 1.3975137 | — |
  | restored package, same invocation | **97** | **+9 835.189350** | −7 037.6335 | **1.3975137** | 78 648 / 470.9 s |

  Output in `outputs/reset_g3_fullday_20260914_*/`; banner `forecaster_sha b3629e2074f0`.
- **F7.** `results_fullday.tsv` reset to its header. `autoresearch_trading_plan_fullday.md`
  rewritten in the day/night Toto2 format: the Step-0 sweep is no longer a precondition,
  `--warmup-bars 5000` (eval from 2018-01-09 02:50), no inherited Calmar, and the selector test
  is flagged as the one test a band change must update. Index row updated.
- **F8.** Committed on `fullday_toto2_reset` as `34afa06` (restore) and `7fb7e8f` (tsv reset),
  fast-forwarded into `main`, and pushed by the user on 2026-09-15 (`origin/main` at
  `7fb7e8f`).

## Related

`STRATEGY_SPLIT_PLAN.md` (§1 defaults, G1) · `LIVE_STRATEGY_ROUTING_PLAN.md` (D9, D10) ·
`TAPE_REBUILD_PLAN.md` · `results.tsv` · `../UV_SETUP.md` ·
`../autoresearch_trading_plan.md`
