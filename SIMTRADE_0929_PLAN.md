# SIMTRADE_0929_PLAN.md — three-session simulation trade on `simtrade_0929`

**Status: EXECUTED 2026-09-29, steps 1–7 done; step 8 is the user's launch.**
Branch `simtrade_0929` (local only, never pushed), forked from `main` @
`f830f7d1f`. Day session opens 08:45.

What differed from the plan, all recorded in `strategy/README.md`:

- **The venv was empty** (recreated 2026-09-20 01:30, no packages at all, not
  even `shioaji`). Reinstalled by UV_SETUP.md Steps 2–5 before step 2 could run;
  torch `2.14.0+cu130` sees the RTX 5090.
- **Suite: 1482 passed, 11 skipped, 0 failed.** Skips: fullday's original 2,
  night's 8 provenance assertions (its pin is now None), and 1 report-rebuild
  round-trip for night — `describe()` cannot carry an asymmetric band.
- **Three tests changed beyond the plan:** the warm-up priming floor now counts
  boundaries at each deployment's stride (fullday's stride 15 broke the bar
  count); two session-artifact tests accept an UNTUNED night pin.
- **Night's banner** prints `trend=3.0 band=±0.00065`; the stack trades 12 and
  +0.00065/−0.00045 (verified by building it). Banner only.

## 0. Decisions (taken with the user)

| # | Item | Decision |
|---|---|---|
| 1 | Branch | the existing `simtrade_0929` at `main`'s tip; stays **local** |
| 2 | Day | `day_split_iter02` (`58e81b0`) — the split loop's baseline, code **identical to `main`**, so no change. `PINNED_DAY` (`3c3ae38`, Calmar 1.6937) is kept: it describes the same settings. README explains 1.6937 (whole data) vs 5.1186 (train 70 %) |
| 3 | Night | `night_iter10` (`6159a4c`, branch `autoresearch_night_0918`, 100 % data) + knob-test fix `4bf33063c`, squashed into one commit. Live source record → `None`, provenance in `source_note` |
| 4 | Fullday | `fullday_split_iter07` (`394ce85`, branch `autoresearch_split_0.7_fullday`), squashed into one commit. Live source record stays `None`; the "UNTUNED, DAY's band borrowed" wording is replaced with the split source and train Calmar |
| 5 | Ledger rows | `results_<session>.tsv` rows are **left out** (they are research records; nothing live reads them) |
| 6 | Held-out tail | **not scored** — the user chooses strategies |
| 7 | Tests | tests pinning the old night/fullday values are updated; full suite must end **0 failed** |
| 8 | README | `forecast_eval/strategy/README.md` — settings, rationale, cautions, launch and stop |
| 9 | Outputs | rename `night/outputs` → `night/output_backup_260929`, create a new empty `outputs/` |
| 10 | Launch | owner mode: `live/owner_cli.py` first, then 3 × `live/cli.py --broker client --fresh-start`, one shared output parent. **The user starts them**; Claude supplies the commands |
| 11 | Rails | no `--max-daily-loss-points`, no `--kill-file` |
| 12 | Untested shape | three deployments under the owner for full sessions is untested (only 2 deployments × 1 lot × 10 min, §15/§23). Accepted for a simulation account; recorded as a caution |
| 13 | Flatten | Claude runs `live/flatten.py` if the account is not flat |
| 14 | Stopping | flatten or leave open and `--resume` — the user decides at the time; README covers both |

## 1. The chosen strategies

| Session | Source | Settings (final `PARAMS`) | Calmar (window) |
|---|---|---|---|
| day | `day_split_iter02` (code identical to `main`, no change needed) | band ±0.001, `min_prob` 0.65, stride 30, two-sided, no exit rule | 5.1186, 79 trades (train 70 %) |
| night | `night_iter10` | buy +0.00065 / sell −0.00045, `min_prob` 0.55, `min_trend_ticks` 12, **`long_only=True`**, stride 30 | 6.5793, 144 trades (whole data) |
| fullday | `fullday_split_iter07` | band ±0.001, **`min_prob` 0.50**, **stride 15**, two-sided, no stop | 2.8213, 134 trades (train 70 %) |

The three Calmars were scored on different windows and are **not comparable**.

**2026-10-05: night replaced by `night_iter30`** (`4cbc1e6`, branch `autoresearch_night_1004`): band ±0.0004, `min_prob` 0.6, `min_trend_ticks` 10 (emitter default), stride 15, **two-sided**; Calmar 7.2974, 376 trades (whole data). The night row above and the night steps below describe what ran until then. Current settings: `strategy/README.md`.

## 2. Steps

### Step 1 — Back up `outputs/`
1. Confirm nothing holds a file under `outputs/` (no running `python` / `live` process). If something does, **stop and report**.
2. Rename `night/outputs` → `night/output_backup_260929`; create an empty `night/outputs/`.
3. Verify: backup is 13 GB / 124 entries, `outputs/` is empty.

### Step 2 — Is the simulation account flat? (read-only)
1. Confirm which credential the owner uses; pass the same one to `flatten.py` (its default is `night`).
2. `uv run --no-sync python -m forecast_eval.live.flatten --dry-run --credential <cred>`. Do not paste the `person_id` line anywhere.
3. Flat → done. Holds TXF → **after 08:45**, run it without `--dry-run`; it must exit 0. Non-zero → **stop and report**.

### Step 3 — Night package at `night_iter10` (one commit)
1. Apply the net diff `ff3a601 → 4bf33063c`, excluding `results_night.tsv`, via `git diff … | git apply --3way`.
2. Resolve the `strategy/night/trader.py` docstring conflict: keep `main`'s hash line, add the branch's `long_only` paragraph.
3. Commit: `simtrade: night package at night_iter10 (6159a4c) + knob-test fix (4bf33063c)`.

### Step 4 — Fullday package at `fullday_split_iter07` (one commit)
1. Apply the diff `main → 394ce85` on `strategy/fullday/wiring.py` and `tests/test_strategy_selector.py` (stride 15, `min_prob` 0.50). `620b338` (ledger row repair) is skipped.
2. Commit: `simtrade: fullday package at fullday_split_iter07 (394ce85)`.

### Step 5 — Live source records and tests (one commit)
1. `live/config.py`:
   - **Night `PINNED`**: `source_commit` / `source_calmar` → `None`; `source_note` names `autoresearch_night_0918` `6159a4c`, whole-data Calmar 6.5793 (144 trades), not in `results.tsv`.
   - **Fullday**: stays `None`; note and Tech:/Why: comment now name `autoresearch_split_0.7_fullday` `394ce85`, **train-70 %** Calmar 2.8213 (134 trades).
   - **Day `PINNED_DAY`**: unchanged.
2. Verify a config without a source record only **warns** at start-up (`cli.py:567` and its path). If it refuses, **stop and report**.
3. Verify night's `long_only` and `min_trend_ticks` reach the live build path (`make_strategy` / `params`).
4. Run the suite; fix tests pinning night's old source/values and fullday's "marked_untuned" test (and its name). Night's provenance-only tests become skips like fullday's; report the new skip count and why.
5. Commit: `simtrade: live provenance follows the chosen packages`.

### Step 6 — `forecast_eval/strategy/README.md` (one commit)
- **Per session:** settings, rationale (from the kept commits), source commit/branch, Calmar and the window it was scored on.
- **Cautions:**
  - Day: 1.6937 (whole data, `3c3ae38`) vs 5.1186 (train 70 %) — same settings, different window.
  - Night: tuned and scored on 100 % of the data (no unseen bars); `long_only` bets on the uptrend continuing.
  - Fullday: `min_prob` 0.50 makes the probability gate pass everything; no stop-loss (it arrived at iter09).
  - Day's and fullday's held-out 30 % has never been scored.
  - The account can hold up to 3 lots in one direction.
  - Three deployments under the owner is untested beyond 2 × 1 lot.
  - The 500 MiB/day data allowance under four logins is unmeasured.
  - Old `outputs/…` paths in docs now live under `output_backup_260929/`.
- **How to run:** flatten check → owner → 3 clients with `--fresh-start`, and when to start each.
- **How to stop:** flatten (next run uses `--fresh-start`), or leave positions open and `--resume` with `outputs/` intact.
- First confirm `test_docs_coverage` and `test_strategy_isolation` accept a non-module file in `strategy/`.
- Commit: `simtrade: strategy README`.

### Step 7 — Verify
1. Full suite: `uv run --no-sync python -m pytest forecast_eval/tests/ -q` → **0 failed**.
2. Import smoke test.
3. `git -C forecast_eval log --oneline main..simtrade_0929` shows the 4 commits; branch not pushed.

### Step 8 — Launch commands for the user
- Read `owner_cli.py` and `cli.py` argument parsers; fix the shared output parent (the clients' `--out` parent must be the folder the owner reads).
- Check how a deployment behaves when started outside its session, to state start times.
- Deliver four copy-paste PowerShell commands (owner, day, night, fullday) with `--broker client --fresh-start`, no loss limit, no kill file; the same go in the README.

## 3. Stop conditions — report, do not work around
- A file lock on `outputs/`.
- Flatten not ending flat.
- Live refusing configs without a source record.
- Night's knobs not reaching live.
- Any test failure not traceable to an intended value change.
