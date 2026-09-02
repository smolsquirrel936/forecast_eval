# STRATEGY_SPLIT_PLAN.md — three independent strategy pipelines (day / night / full-day)

**Status (2026-09-02, end of day):** S1–S6 **built, G1–G5 green** (§9, §10).
**S7 CLOSED** — Q0–Q5 all measured (§14b; detail in §11 as amended, §13, §14).
**S8 is re-opened:** both candidate answers were tried and both are dead as
stated — per-key separation was measured false (§14), and the original (c)
needs sub-accounts nobody has yet. The live routes are **real sub-accounts** or
**the shared position broker (b)**; §16 is the reconciliation fix either way.
**§15 is the operating rule in force meanwhile: one instance at a time.**

> **Reading order.** §§0–10 describe the split and are current. §7 and §11 are
> **historical**: they record what was predicted before measurement, and several
> predictions were wrong. Each now carries a MEASURED banner pointing at the
> section that settled it. **§14, §15 and §16 are the current state** — read
> those first if you only read three.

**Supersedes:** `autoresearch_trading_plan.md`'s freeze table (one row) and its
locked decision #1 ("Toto2 is frozen; the agent never touches the model").
**Unblocks:** `DAY_SESSION_PLAN.md` W9, deferred by the user on 2026-09-02 (§2.6)
until this split lands.

---

## 0. The decisions this plan was given

Recorded as answered on 2026-09-02, before any design was drafted.

| # | Question | Decision |
|---|---|---|
| D1 | How far does the split reach? | **Backtest + live wiring.** `real_data.py` / `run.py` / `strategy/` *and* a third `CONFIGS` entry reaching `live/runtime.py`. The three sets must also be able to **trade concurrently without interfering** — §7 is where that is blocked, and it is the hard part of this plan. |
| D2 | Where does the Trader/Forecaster seam go? | **New seam in the frozen files, then re-freeze** (§2). Three edits, all no-ops by default. |
| D3 | Is `forecaster/` mutable? | **Yes, per session.** Each package owns its forecaster. Costs are real and stated in §2.4 — this is the one decision that buys capability by giving up a guarantee. |
| D4 | What are the three defaults? | **The already-pinned configs** (§1), which keeps the split a pure refactor with an exact parity gate. |
| D5 | How is full-day pinned? | **`PINNED_FULLDAY` carries an explicit UNTUNED marker and `live/cli.py` refuses `--config fullday`** until a `results.tsv` row exists (§5). |
| D6 | Isolation shape? | **Three sibling subpackages** under `strategy/` (§3). |

---

## 1. The three defaults

Each package starts at the configuration that is **already tuned and provenanced**:

| set | source | policy | band | `min_prob` | recorded result |
|---|---|---|---|---|---|
| `night` | `PINNED` (commit `4282d33`) | `SessionPolicy.night()` | ±0.00045 | 0.5 | Calmar **3.6006**, 235 trades, net **+24 135.60**, max dd **−6 703.24** |
| `day` | `PINNED_DAY` (commit `3c3ae38`) | `SessionPolicy.day()` | ±0.001 | 0.65 | Calmar **1.6937**, 67 trades, net **+9 748.01**, max dd **−5 755.50** |
| `fullday` | **none — UNTUNED** | `SessionPolicy.both()` | ±0.001 | 0.65 | never measured, never traded |

**Why this matters to the execution of the plan, not just to the numbers:** starting
from the pinned configs makes the split a **pure refactor**. DAY and NIGHT must
reproduce their recorded results *exactly*, so any deviation is a bug in the split and
nothing else — that is gate **G1** (§4), and it is the only unambiguous pass/fail this
plan has. A new strategy shipped as the default would remove it, because a wiring error
and a strategy change would be indistinguishable in the output.

A new strategy is therefore the **first commit inside a package after S3**, scored
against that package's incumbent — which is what the three-way search is for.

---

## 2. The seam (D2) — three edits, then re-freeze

Every edit below is a **no-op on the default path**. The current behaviour is the
`None` branch in each case.

### 2.1 `run.py` — the Trader becomes injectable

`run_backtest` constructs `Trader(...)` itself at **run.py:144** from four scalar
kwargs, so the Trader box is the one component the harness will not let you swap.

**Ownership problem.** `Execution` is built inside `run_backtest` (`fee_rate` is a run
parameter, not a strategy one), so an injected Trader cannot have been handed its
execution at construction time. Two options:

- (a) inject a `Trader` instance and rebind `trader.execution` — one line, but mutates
  a caller-owned object;
- (b) inject a **factory** `make_trader: Callable[[Execution], Trader]` — no mutation,
  and it matches the convention `run.fee_sensitivity_sweep` already uses for
  `make_emitter` / `make_forecaster`, which exists for exactly this reason: these
  objects are stateful and reusing one across runs silently produces wrong numbers.

**Recommend (b).** ~8 lines. Every existing call site and every test is untouched.

### 2.2 `real_data.py` — a strategy selector

At **real_data.py:282–297** the evaluation entrypoint hardcodes `Toto2Forecaster` +
`ThresholdEmitter` built from CLI flags. Add:

```
--strategy {day,night,fullday}    default: None
```

When given, the three components come from `strategy.<name>.wiring.make_strategy()`.
When absent, the current CLI-flag path runs unchanged.

**Every CLI flag stays, and stays an override.** `results.tsv` records evaluation
commands as flag strings; the whole research log must keep reproducing byte-identically.
The rule: `--strategy` supplies defaults, an explicit flag beats it, and the run banner
prints which knob came from where.

~40 lines.

### 2.3 `live/config.py` + `live/runtime.py` — `build_trader()`

`LiveConfig` already has `build_emitter()` (config.py:75) and `build_forecaster()`
(config.py:92) — the live half is two-thirds done. Add:

- `LiveConfig.build_trader(*, execution)` — mirrors the other two, returning a fresh
  instance every call for the same stated reason (stateful object).
- `LiveConfig.strategy_package: str` — `"day"` / `"night"` / `"fullday"`; the three
  factories dispatch through it.
- `runtime.py:165` becomes `self.trader = config.build_trader(execution=self.broker)`.
- `PINNED_FULLDAY` + `CONFIGS["fullday"]` (§5).

`SessionPolicy.both().cache_slug()` returns **`day_night`** — verified 2026-09-02,
distinct from `day` and `night`. So `bars_cache_<slug>.csv`, `fills_<slug>.csv`,
`live_restart.json`, `soak_epoch.json` and `live_account.json` are **already** isolated
for a third deployment by the C5/W1 and 2026-09-02 per-slug work. Nothing new is needed
there.

### 2.4 What D3 costs — stated once, plainly

Making `forecaster/` per-session mutable gives up a guarantee the autoresearch loop was
built on:

- **`--precompute` no longer amortises across iterations.** Its premise — forecasts
  depend only on fixed price history, so compute once and replay — holds *within* a run
  but not *across* runs once the forecaster can change. Any iteration that touches the
  forecaster pays a full GPU pass.
- **Iterations stop being comparable on a fixed forecast stream.** The autoresearch
  plan's locked decision #5 rested on this. A Calmar improvement can now come from the
  forecaster rather than the strategy, and the log cannot tell which.
- **Mitigation:** `results.tsv` gains a `forecaster_sha` column (hash of the package's
  `forecaster.py`). Rows sharing a SHA are comparable on a fixed stream; a SHA change
  marks a stream change and must be called out in the row's note.

This is not an argument against D3 — it is the bookkeeping D3 requires.

---

## 3. Package layout (D6)

```
strategy/
  base.py           FROZEN   SignalEmitter ABC — unchanged
  threshold.py      SHARED   ] the default library the three packages start from;
  dummy.py          SHARED   ] frozen after the split — to change one, COPY it
  volmultitf.py     SHARED   ] into your package first (§3.1)
  wiring.py         SEAM     make_strategy(session) -> dispatches to a package
  day/
    __init__.py
    wiring.py       MUTABLE  make_strategy() — the file an agent edits first
    emitter.py      MUTABLE  starts as a copy of ThresholdEmitter
    trader.py       MUTABLE  starts as a copy of trader.Trader
    forecaster.py   MUTABLE  starts as a thin Toto2Forecaster construction (§3.2)
    exits.py        MUTABLE  starts empty (exit_rule=None)
  night/    (identical shape)
  fullday/  (identical shape)
```

`strategy/wiring.py` keeps its path — `live/config.py`'s docstring already names it as
the autoresearch seam, and it is an add/add collision point with the day tree
(DAY_SESSION_PLAN §2). Rebuilding it here is the "seam rebuilt" that §2.6 asked for.

### 3.1 Copy, don't share — and a guard that enforces it

`emitter.py` and `trader.py` are **real copies** in each package (≈10 KB and ≈11 KB;
3× is affordable, and it makes isolation structural rather than promised). The shared
files under `strategy/` and the root `trader.py` become read-only to agents.

The rule needs a mechanism, not a sentence — this repo's own `test_docs_coverage.py`
exists because "a rule that depends on someone remembering to run a checker is not a
rule":

- **`tests/test_strategy_isolation.py`** pins a SHA-256 of each shared file
  (`strategy/threshold.py`, `strategy/dummy.py`, `strategy/volmultitf.py`,
  `strategy/base.py`, `trader.py`, `forecaster/toto2.py`). An agent that edits a shared
  file instead of copying it fails the test, with a message telling it to copy the file
  into its own package.
- The same test asserts no `strategy/<a>/` module imports from `strategy/<b>/`.

### 3.2 Why `forecaster.py` starts thin rather than copied

`forecaster/toto2.py` is 23 KB; three verbatim copies on day one is a large diff for a
capability that will mostly be exercised as *parameter* changes (checkpoint, context,
stride, horizon). So each package's `forecaster.py` starts as a construction site over
the shared frozen `Toto2Forecaster`, and an agent that wants to change model internals
copies the class in at that point — the same copy-on-write rule as §3.1, enforced by
the same hash guard.

---

## 4. Verification gates

The split is a refactor, so it has real pass/fail criteria. **G1 is the gate — nothing
else lands until it is green.**

| gate | check | passes when |
|---|---|---|
| **G1 parity — night** | `real_data.py --strategy night` on `TXF_RPT_minute_night.csv` | Calmar **3.6006**, **235** trades, net **+24 135.60**, max dd **−6 703.24** — exact |
| **G1 parity — day** | `--strategy day` on `TXF_RPT_minute_day.csv` | Calmar **1.6937**, **67** trades, net **+9 748.01**, max dd **−5 755.50** — exact |
| **G2 suite** | `uv run python -m pytest forecast_eval/tests/ -v` | green, including `test_trader.py`, `test_integration.py`, `test_lookahead.py`, `test_confidence_placement.py` |
| **G3 isolation** | new `test_strategy_isolation.py` | shared-file hashes intact; no cross-package imports; a mutation injected into `day/emitter.py` leaves a `night` backtest byte-identical |
| **G4 live parity** | `test_live_parity.py`, `test_live_parity_sessions.py`, `test_live_runtime.py`, `test_live_config.py` | green under `build_trader()`; `--config fullday` refused with a message naming the missing `results.tsv` row |
| **G5 docs** | `test_docs_coverage.py` | **~15 new pages** under `docs/strategy/{day,night,fullday}/` land in the same change (the 1:1 rule) |

G5 is not a formality — it is the single largest line count in this plan and the thing
most likely to be skipped under time pressure.

---

## 5. Full-day (D5)

- Tape: **`dataset/RPT/TXF_RPT_minute_fullday.csv`** — 2 364 417 bars, 2018-01-02 →
  2026-08-21. Not the combined `TXF_RPT_minute.csv`, whose first 15.6 % is day-only
  because TAIFEX had no night session before 2017-05-15 (TAPE_REBUILD §2.5).
- Roll: 12:45, same as DAY — `roll_at_time()` was extended 2026-09-01 so any policy
  that trades DAY rolls at 12:45 (DAY_SESSION_PLAN §4 C4 has a FULL-DAY row).
- `PINNED_FULLDAY` carries no `source_commit` and no `source_calmar`, and a
  `source_note` that says in words that it is untuned and has never traded. Its
  parameters are DAY's, marked as *inherited, not measured*.
- **`live/cli.py` refuses `--config fullday`** until a tuned row exists. The refusal is
  the point: an unprovenanced config that can be dry-run and parity-checked but cannot
  be sent at a broker. This mirrors how `PINNED_DAY` was handled — wired, checkable, and
  explicitly "has never traded".
- First real work after the split: `tools/band_sweep.py` on the full-day tape, to
  produce a provenanced pin.

---

## 6. What the split must not lose (DAY_SESSION_PLAN §2.6's debt)

§2.6 says this plan is owed the DAY facts that a rewritten component set will silently
drop. Carried here so they live in the file the agents actually read:

1. `PINNED_DAY`'s provenance and the **worst-half selection rule** (§2.3) — the pin was
   chosen on the worst-half score, not the headline Calmar, and was deliberately **not**
   moved to the better cell after the tape rebuild.
2. The **priming margin** (§2.5) and the warm-up window derivation (C6/W4).
3. The roll instant is **12:45**, and why (§4 C4).
4. **89.4 % of DAY's gross accrues overnight** (C8) — a property of the *strategy*, not
   the harness, and §2.6 names it as *the single most likely thing to be lost*. A DAY
   position rides ~20 calendar days (median; p90 57, max 173) of night sessions and
   weekends unmanaged.
5. Neither pin beats buy-and-hold on the rebuilt tapes (DAY trails by 24 419 points;
   NIGHT by 9 205). The Calmar is earned by a small drawdown, not by out-earning the
   index.

---

## 7. Running the three deployments at once — what actually blocks it

**This is the parallelism the split is for: three *trading* deployments running
concurrently without interfering.** The per-slug file isolation (§2.3) is already done
and is *not* the problem. The problem is that all three point at **one account holding
one net position in one contract**, and two rails read that account as if they were its
only client. Both failures below are mechanical and certain — they are not risk
trade-offs to weigh.

### 7.1 The reconciliation rail halts every deployment on the first overlapping bar — CERTAIN

- `ShioajiBroker.broker_position()` sums the venue's positions filtered by **contract
  code only** (`shioaji.py:447`; `_codes` comes from `live.contracts.contract_codes`).
  There is no per-deployment tag, and there is nowhere to put one.
- `SafetyRails.on_reconcile` (`state.py:240`) halts with `HaltAction.STOP` on any
  `mirror != venue`, and deliberately never auto-corrects — *"'close the position' is
  undefined when the size is in dispute."*
- So the moment DAY holds +1 and NIGHT holds +1 on the same front month, the venue
  answers **+2** to both. Each mirror says +1. **Both halt**, with
  `position mirror 1 != venue 2`.
- `adopt_position` (`runtime.py:246`) has the same defect on the other side: a restarting
  deployment adopts the **combined** venue position as its own.
- Given DAY's median 20-day hold and NIGHT's 4.5-day, "the first overlapping bar" means
  effectively immediately — and full-day overlaps *both* others by construction.

**Options (this is the decision the split needs from you):**

| | approach | cost |
|---|---|---|
| (a) | tag positions per deployment at the venue | **impossible** — one account nets one contract |
| (b) | **a shared position broker**: one process owns the account, the three deployments submit intents, it nets them and reconciles the venue against the *sum* of three mirrors | correct and general; the largest single piece of new work in this whole plan, and it touches the rails |
| (c) | **separate accounts / subaccounts**, one per deployment | zero code — each rail stays exactly as it is. A broker/account question, not an engineering one |
| (d) | distinct contracts (e.g. TXF vs MXF per deployment) | changes what the strategies trade |

**Recommend (c) if your broker allows subaccounts, (b) if not.** (b) should not be
started before 7.2 and 7.3 are probed, since their answers change its shape.

### 7.2 A foreign deal halts the deployment that did not place it — **MEASURED FALSE (§14)**

> **This prediction was wrong.** Q4 measured that order events *and* deals are
> delivered **per session**: session B received nothing at all while session A
> placed, filled and cancelled. There are no foreign deals to mis-match, so
> `_anomaly` never fires for this reason and §7.2 does not occur.
>
> It is left below as written because the reasoning that produced it — including
> the introspection in §11 that concluded the opposite — is exactly what the
> probe existed to check. **The failure that does occur is §7.1**, and Q4's
> result makes it *worse*, not better: see §14.

- `_match` (`shioaji.py:622`) resolves deals by `ordno` / `order_id` against *that
  broker's own* tracked orders. An unmatched deal is not ignored — it calls `_anomaly`
  (`shioaji.py:580`, *"deal for an unknown order"*), and `_anomaly` halts the rails.
- If Shioaji's account callback is delivered per-login rather than per-order, deployment
  A receives B's deal, fails to match it, and halts.
- `_learn_code` (`shioaji.py:641`) sharpens the point. Its comment reads: *"we only ever
  trade the one contract this broker was constructed for, so anything the venue
  attributes to our own orders is by definition ours."* That premise is precisely what
  three concurrent deployments break — and it silently *widens* the position filter, so a
  wrong answer here feeds straight into 7.1.
- **Verifiable only against a live session**, which needs your credentials. Probe it
  before choosing between (b) and (c).

### 7.3 Can one Shioaji credential hold three simultaneous logins? — **MEASURED YES (§11 Q2)**

> **Answered:** a second login succeeds, `usage().connections` goes 1 → 2, and
> the first session stays usable. Sessions coexist; nothing is evicted. This
> removed the "eviction forces (b)" branch — but it turned out not to matter,
> because §14 showed the constraint is the shared position book, not the session
> count.

Unverified, and it decides whether (c) is even reachable. Per this repo's own record the
docs have been wrong four times, so **introspect the installed package and probe a live
session** rather than reading. If a second login evicts the first, (b) is forced.

### 7.4 The exposure question — a risk decision, not an engineering one

DAY_SESSION_PLAN C8 already flags that **two** deployments on one account is *"two
contracts of same-direction exposure 66 % of DAY's holding time"*. Full-day makes it
three, and full-day is not a third independent strategy — it is a **superset session**
that overlaps both others by construction. Whether that is one netted account, three
accounts, or a hedged book gates any parallel live run, and it is yours to decide. The
account currently still holds LONG 1 @ 44555 (DAY_SESSION_PLAN, 2026-09-02).

### 7.5 Three concurrent *backtests* are already fine

Separate processes, separate `--output-dir`, distinct tapes, and `cache_slug()` gives
`day` / `night` / `day_night`. Nothing is shared. The only contention is the GPU: three
`--batch-size auto` probes each read the others' VRAM as free and over-commit. A file
lock around precompute, staggered starts, or a fixed per-loop `--batch-size` — minor,
and not on the critical path.

> **Note, not a blocker.** If three *autoresearch loops* are ever run at once (not what
> was asked for here), they cannot share one working tree: the loop ends each iteration
> in `git reset --hard`, and `results.tsv` is a single lock-free append. That would want
> three `git worktree`s on their own branches with a `results_<session>.tsv` each — and
> the repo root is a UNC share where `git init` fails, so the worktree paths need
> checking first. Running the loops **sequentially**, one session at a time, avoids all
> of it.

---

## 8. Sequence

```
S1  strategy/{day,night,fullday}/ packages + wiring dispatcher   -> verify: imports clean, G3
S2  run.py trader injection (§2.1)                               -> verify: G2
S3  real_data.py --strategy selector (§2.2)                      -> verify: G1  <- THE GATE
S4  live build_trader + PINNED_FULLDAY + cli refusal (§2.3, §5)  -> verify: G4
S5  docs pages for every new module (G5)                         -> verify: G5
S6  autoresearch_trading_plan.md freeze-table row + forecaster_sha column (§2.4)
--- everything above is single-deployment work and is fully specified -------------
S7  PROBE: simultaneous logins (§7.3) + is the deal callback per-login (§7.2)
S8  DECIDE: subaccounts (c) or a shared position broker (b) — §7.1
S9  build whichever S8 chose                                     -> verify: G6 (new, see below)
```

**As executed (2026-09-02) — S1–S6 done, S7 done, S8 re-opened:**

```
S1-S6  DONE, G1-G5 green                                        §9, §10
S7     CLOSED — Q0..Q5 all measured                             §14b
S8     RE-OPENED. Per-key separation measured FALSE (§14).
       Live routes: real sub-accounts, or the shared position broker (b).
       Neither is buildable until the broker answers on sub-accounts.
S9a    §16 — reconcile against the SUM of our own fill ledgers   DONE 2026-09-02
S9b    whichever of sub-accounts / (b) the broker makes possible
S10    G6
--- in force until S9b lands: §15, one instance at a time -------------------
```

**G6 (concurrent trading), restated after §14.** The original wording — *"the
venue's net matches the sum of the mirrors"* — assumed each deployment could be
compared against its own account. It cannot, on one shared book. G6 is now:

> two deployments hold non-flat positions across a full session; neither rail
> halts; the venue's reported position equals the **sum of the deployments' own
> fill ledgers**; and each deployment's PnL is still attributed to it alone.

That is testable offline against `FillLedger` as soon as §16 lands, and only the
*non-halting* half needs the venue.

**W9 unblocks at S4**, well before any of that — the DAY soak is a *single* deployment,
and it needs the split to exist, not the three to run at once.

---

## 9. Build log — S1–S3, 2026-09-02

### What landed

| file | what |
|---|---|
| `strategy/{day,night,fullday}/__init__.py` | package markers (docstring-only, so exempt from the docs rule) |
| `strategy/{day,night,fullday}/emitter.py` | verbatim copy of `strategy/threshold.py` @ `sha256:b7721ff7…`, import depth only |
| `strategy/{day,night,fullday}/trader.py` | verbatim copy of `trader.py` @ `sha256:3bea278e…`, import depth only |
| `strategy/{day,night,fullday}/forecaster.py` | thin `make_forecaster()` over the shared `Toto2Forecaster` (§3.2) |
| `strategy/{day,night,fullday}/wiring.py` | `PARAMS` + `make_strategy()`; the Trader comes back as a factory |
| `strategy/wiring.py` | **rewritten** from the dead seam into a dispatcher (`SESSIONS`, `params`, `make_strategy`) |
| `run.py` | `make_trader` parameter; `None` keeps the previous construction byte-for-byte |
| `real_data.py` | `--strategy`, `CLI_STRATEGY_DEFAULTS`, `_resolve_strategy_knobs`, one branch at the construction site |
| `tests/test_strategy_isolation.py` | G3 — hash pins, cross-import ban, behavioural non-interference |
| `tests/test_strategy_selector.py` | knob resolution, incl. the packages-vs-`live/config.py` cross-check |
| `docs/strategy/{day,night,fullday}/*.md` | 12 pages; `docs/strategy/wiring.md` rewritten |

### Four deviations from the plan as written

1. **No `exits.py` per package.** §3 listed one; it would have been an empty file
   returning `None`, plus three more doc pages. `wiring.py` returns
   `exit_rule: None` instead, and `real_data.py` *does* forward it, so a package
   that adds an exit rule is honoured the moment it declares one.
2. **S5 (docs) landed inside S1, not later.** `test_docs_coverage.py` fails the
   moment a module has no page — the repo's 1:1 rule is "same change as the
   module", so deferring it would have meant a red suite. The 12 pages are done.
3. **`tests/test_strategy_selector.py` is new**, not in the plan. It carries the
   part of G1 that does not need a GPU: each package's twelve knobs are asserted
   against the `live/config.py` pin they claim provenance from, so a band that
   drifts fails in a second rather than after an hour-long parity run.
4. **Seven files are hash-pinned, not six** — `forecaster/base.py` was added.
   It defines the `Forecaster` interface and the `assert_no_lookahead` helper, so
   editing it moves all three sessions exactly as editing `strategy/base.py`
   would.

### The one design call worth flagging

The twelve strategy-owned flags in `real_data.py` moved to `argparse.SUPPRESS`,
with their old literals in `CLI_STRATEGY_DEFAULTS`. This is not cosmetic: with a
literal default, a flag passed *at* its default value is indistinguishable from
one never passed, so `--strategy night --buy-threshold 0.0005` would silently
have run NIGHT's 0.00045. SUPPRESS makes "the user typed this" observable.
A bare run (no `--strategy`) resolves to exactly the old defaults — asserted by
`test_bare_run_reproduces_the_old_cli_defaults` — so every command string in
`results.tsv` still reproduces.

### Verification

- **G2** — 998 passed, 0 failed.
- **G3** — green, including the behavioural check: sabotaging DAY's emitter
  leaves NIGHT's output unmoved.
- **Copies confirmed verbatim** — `diff` against the shared originals shows only
  the import lines and the provenance docstring.
- **End-to-end smoke** — `--strategy night` on 20 000 bars with a 22m checkpoint
  ran the full path (forecasts → signals → fills → metrics → report) and recorded
  `strategy: night` plus the resolved knobs into `params.json`.
- **G1 night — PASSED, exactly.** `--strategy night --warmup-bars 538790
  --precompute` over the full night tape (1 849 007 bars, 43 674 forecasts,
  259 s):

  | | expected (`results.tsv` `4282d33`) | measured |
  |---|---|---|
  | trades | 235 | **235** |
  | net PnL (pts) | +24 135.5960 | **+24 135.5960** |
  | max drawdown (pts) | -6 703.2441 | **-6 703.2441** |
  | Calmar | 3.6006 | **3.6006** |

- **G1 day — PASSED, exactly.** `--strategy day --warmup-bars 676123
  --precompute` over the full day tape (15 687 forecasts, 101 s):

  | | expected (`results.tsv` `3c3ae38`, W5b) | measured |
  |---|---|---|
  | trades | 67 | **67** |
  | net PnL (pts) | +9 748.0117 | **+9 748.0117** |
  | max drawdown (pts) | -5 755.5022 | **-5 755.5022** |
  | Calmar | 1.6937 | **1.6937** |

  **Both halves of G1 match to the recorded decimal — the gate is passed.** That
  is the whole claim S1-S3 were built to support: two tuned deployments now run
  through three swapped components each (own emitter copy, own Trader copy via
  the new `make_trader` factory, own forecaster construction site) and produce
  bit-identical output to the monolithic path. The refactor moved no number.

  Run one at a time, deliberately: two `--batch-size auto` probes would each read
  the other's VRAM as free (§7.5).

---

## 10. Build log — S4–S6, 2026-09-02

### S4 — live wiring

| file | what |
|---|---|
| `live/config.py` | `is_tuned` + `strategy_package` properties; `build_trader`; `build_emitter`/`build_forecaster` now route through the deployment's package; `source_commit`/`source_calmar` became `Optional`; `describe()` renders an untuned config; **`PINNED_FULLDAY`** + `CONFIGS["fullday"]` |
| `live/runtime.py` | `self.trader = config.build_trader(execution=self.broker)` replaces the inline construction |
| `live/cli.py` | refuses an untuned `--config` before the run directory or the alerter exists |

**`strategy_package` is derived, not stored.** A stored field could disagree with
the policy, and a deployment running one session's policy against another
session's tuned components is the C2 defect `--config` exists to prevent,
re-entering one level down. Deriving it from `policy.sessions` makes that
impossible rather than merely tested.

**The refusal keys on `is_tuned`, not on the name**, so a fourth untuned
deployment is covered without an edit. It returns **exit 5** ("refused before the
first bar", the code `_preflight_orders` uses) rather than 2, which means
credentials are missing — and it runs before `out.mkdir` and before the alerter,
so a refused config leaves no run directory and no fatal-journal entry. A
mistyped `--config` is an operator slip, not a soak incident.

### Four tests that encoded "every deployment is single-session"

Registering a `both()` policy made four existing assertions false. None was
deleted; each was **re-scoped to the property it was actually about**, which in
two cases made it stricter:

- `test_every_registered_config_trades_exactly_one_session` →
  `..._every_TUNED_config_...`. Its reason (a `both()` policy makes "the best
  `results.tsv` row for this deployment" undefined) still holds, and now bites at
  the right moment: the day full-day acquires a `source_commit`, this fails and
  forces whoever tuned it to say which session name the pin is scoped by.
- The two provenance guards in `test_live_config.py` **skip** an untuned config
  with a reason, rather than failing on a missing row.
- The two `test_live_parity_sessions.py` parity tests parametrize over
  `SINGLE_SESSION`; their premise ("the other session was filtered out") is not
  merely false for full-day, it is meaningless.
- `test_build_emitter_reproduces_every_declared_field` now asserts the emitter is
  **NIGHT's own class and not the shared one** — a stronger claim than the
  `isinstance` it replaced, and the one the split is selling.

**One assertion I got wrong and corrected:** the new full-day parity test first
asserted `n_bars_dropped_off_session == 0`, on the assumption that a `both()`
policy filters nothing. It drops 10 275 — genuinely off-hours bars, which no
policy keeps. The test now asserts full-day drops *strictly fewer* than either
single-session deployment and sees exactly their union.

### S5 — docs

No new modules, so the 1:1 rule was already satisfied; the three changed pages
were rewritten to match: `docs/live/config.md` (the split, `build_trader`, and a
section on why `PINNED_FULLDAY` is registered and refused), `docs/live/runtime.md`,
`docs/live/cli.md` (the `--config` row it never had, and exit 5).

### S6 — `forecaster_sha`, and the half that needs permission

**Done:** `strategy/wiring.py:forecaster_sha(session)` (12-hex sha256 of the
package's `forecaster.py`, hashed from the file so it needs no torch and an edit
counts even when the old bytes are imported); `real_data.py` prints it in the
`--strategy` banner and records it in `params.json`; `results.tsv` gained a
`forecaster_sha` column with all 20 existing rows marked **`pre-split`** — the
honest value, since those runs predate a mutable forecaster and are comparable
with each other but not with what follows.

**Done differently, by the user's decision (2026-09-02): refer to that file, but
write the correction here.** `autoresearch_trading_plan.md` lives at the repo
root (`night/`), outside `forecast_eval/`, and is not editable from here. Rather
than leave it silently stale, the current freeze line now lives in
**[AUTORESEARCH_FREEZE.md](AUTORESEARCH_FREEZE.md)**, which states that it
supersedes that file's freeze table and names exactly what no longer holds. The
rest of `autoresearch_trading_plan.md` — loop mechanics, the Calmar objective,
the `n_trades >= 60` keep-gate, the environment notes — is unchanged and still
governs.

The three things it gets wrong, now corrected in `AUTORESEARCH_FREEZE.md` §2-§3:

1. **The freeze table's MUTABLE row** still says `strategy/*.py` + `trader.py`.
   It should be the three `strategy/{day,night,fullday}/` packages, with the
   shared files frozen and hash-guarded by `tests/test_strategy_isolation.py`.
2. **The FROZEN row** still lists `forecaster/*`. `forecaster/toto2.py` stays
   frozen, but each package's `forecaster.py` is now mutable (D3).
3. **Locked decisions #1 and #5** — "Toto2 is frozen; the agent never touches the
   model", and "forecast schedule frozen ⇒ Toto2 outputs are deterministic" —
   are both superseded by D3, which is why `forecaster_sha` exists.

`AUTORESEARCH_FREEZE.md` also carries what an agent actually needs at the start
of a run: the `--strategy` flag, which tape goes with which session, and the two
baselines to beat. **An agent should be pointed at that file, not at the repo-root
plan's freeze table.**

### Verification

- **G2** — 1003 passed, 2 skipped (the two full-day provenance guards, skipped as
  untuned). Zero failures.
- **G3** — green.
- **G4** — `test_live_parity.py`, `test_live_parity_sessions.py`,
  `test_live_runtime.py`, `test_live_config.py` all green under `build_trader`;
  `--config fullday` refused, message verified by hand and by test.
- **G5** — green.
- **G1** — unchanged from §9; S4–S6 touch no backtest path that parity covers,
  and the suite's own parity tests (live-vs-backtest) passed.

---

## 11. S7 — the probe, 2026-09-02  ·  **HISTORICAL: written before it ran**

Built: `live/multi_deploy_probe.py` (+ `docs/live/multi_deploy_probe.md`).

> **This section is the probe AS DESIGNED, and it has been overtaken.** It was
> written before any of it ran, and two of its conclusions were measured false.
> It is kept because the design reasoning is what the measurements corrected, and
> because the corrections are only legible against it. **For what is true now,
> read §13, §14 and §15.**
>
> | §11 claim | outcome |
> |---|---|
> | "the deal subscription is **per account**, so two sessions on one account both receive that account's deals" | **FALSE.** Q4: B received **nothing** while A placed, filled and cancelled. Events and deals are **per session** |
> | "§7.2 is only avoidable by giving each deployment a different account" | **moot** — §7.2 does not occur at all |
> | "S8 collapses onto one question: how many FUTOPT accounts?" | **too narrow.** The account *count* does not decide it; whether the position *book* is shared does, and that needed Q5 |
> | "two modes", "five questions" | grew to **three modes** (`accounts`, `offhours`, `session`) and **six questions** (Q0 added) |
> | "a quiet Q4 is not proof of isolation" | true only for an **unfilled** order. With `--marketable` a deal occurs, and a quiet Q4 then IS conclusive (§14) |
>
> **The one thing §11 got right and cheaply:** it is what turned "run three
> deployments and see" into six specific, individually falsifiable questions. Two
> of the six overturned reasoning that both the user and I had argued for. The
> probe earned its cost by being wrong in public.

### Introspection settled four things before the probe was written

`Shioaji` 1.7.2 is a compiled `_core.pyd` — no source, no docstrings on the calls
that matter — but the type stub `_core.pyi` and the live signatures give up more
than expected, and each of these removes a question the probe would otherwise
have had to ask:

| measured | consequence |
|---|---|
| `login(api_key, secret_key, subscribe_trade=True, receive_window, force_refresh)` | the order/deal stream is subscribed **per account**, and **every account is subscribed by default at login** |
| `subscribe_trade(account)` / `unsubscribe_trade(account)` exist | a session *can* in principle be narrowed to one account — so (c) has a mechanism, not just a hope |
| `list_positions(account=None, ...)` | position queries are **already** account-scoped, and `broker/shioaji.py` already passes an account |
| `usage() -> UsageOut.connections: int` | simultaneous connections are **directly observable**, not inferred from behaviour |

**What this changes about §7.** The plan said (7.2) that a foreign deal halting a
deployment was "LIKELY" and turned on whether callbacks are per-login or
per-order. That is now sharper: the subscription is **per account**, so two
sessions on the *same* account both receive that account's deals — B's `_match`
returns None and `_anomaly` halts it. §7.2 is only avoidable by giving each
deployment a *different* account, which is (c). It is not a separate escape
route from §7.1; the two have the same fix.

**So S8 collapses onto one question:** does this credential have more than one
FUTOPT account? That is Q1, and it needs a bare login and about thirty seconds.

### The probe

Five questions, two modes. Q1-Q3 need no market data and place no orders;
Q4-Q5 need a live session and one order.

| | Question | Mode |
|---|---|---|
| Q1 | How many FUTOPT accounts does this credential have? | offhours |
| Q2 | Does a second simultaneous login succeed — and does the first **survive** it? | offhours |
| Q3 | Does `login(subscribe_trade=False)` + `subscribe_trade(mine)` opt a session out of another account's stream? | offhours |
| Q4 | Does a deal on session A reach session B's order callback? | session |
| Q5 | Does a position opened by A appear in B's `list_positions()`? | session |

Three design points worth stating, because each is a way the probe could have
lied:

- **Q2 exercises the first session *after* the second logs in.** A second login
  that succeeds while silently evicting the first is indistinguishable from one
  that coexists — until the first session's next call fails in production.
- **Q3 proves the calls are *accepted*, not that the filtering is real.** Only a
  deal on another account shows that, and the probe says so rather than letting
  an accepted call read as a pass.
- **A quiet Q4 is not proof of isolation.** A resting order that never fills may
  produce no deal event for anyone. The probe prints that caveat itself.

Safety: `--mode session` requires `--i-understand-this-places-orders` (the same
acknowledgement `live/cli.py` demands) and exits 5 without it; the order rests
500 points from the market and is cancelled before exit. All output goes through
`ioc_probe.redact`, by value as well as by field name, because Shioaji puts a
Taiwanese national ID in account reprs.

### How to read the result — **SUPERSEDED, and wrong in its main claim**

The decision table that stood here keyed S8 off Q1 (the account count) and Q2
(session eviction), and ended *"Q4/Q5 do not change the choice."*

**Q5 changed the choice completely.** The account count was never the deciding
fact; whether the position **book** is shared is, and only Q5 could see that. The
table would have sent a reader to build the shared position broker off Q1 alone,
or — after §12 — to trust per-key separation off Q1 and Q2 alone. Both were
wrong for the same reason: they inferred position behaviour from identity and
session facts.

**The correct reading is §14's:** deals are per session, positions are shared, and
that pairing is the worst case for the rails. See §14 "What remains" for the two
live routes and §15 for the rule in force meanwhile.

### S7 RESULT — offhours mode run 2026-09-02

| | measured | reading |
|---|---|---|
| **Q1** | `login()` returned **1 account in total**; `connections` 1 | **(c) is not reachable on this credential.** One account is one netted futures position, and there is nothing to scope a deployment to |
| **Q2** | 2nd login succeeded, `connections` **1 → 2**, and the **1st session stayed usable** | concurrent sessions on one credential **do** coexist. The "a second login evicts the first" branch is dead |
| **Q3** | `login(subscribe_trade=False)` **accepted** | the selective-subscription call exists and is honoured at login. Its second half did not run — see the defect below |

**A defect in the probe, found by its own output.** Q1 printed
`FUTOPT accounts 0` for a credential that has been trading TXF nightly. The test
was `"Future" in str(account_type)`, but **`str(AccountType.Future)` is `'F'`**,
not `'Future'` — so it matched nothing and stated a confident zero. This is the
"verify by introspection; the docs have been wrong four times" rule violated by
assuming a repr instead of printing one.

Fixed: classification now compares against the `AccountType.Future` **enum
member**, the probe prints every account's raw type beside the count, and it
cross-checks `api.futopt_account` as an independent second opinion. A count with
no evidence under it is what let the wrong answer pass unnoticed.

**The conclusion survives the defect**, because it does not rest on the broken
line: `login()` returned **one** account, so there cannot be two futures
accounts. What was lost is Q3's second half — `subscribe_trade(account)` was
never called, since the probe had classified the account list as empty.

### S8 — DECIDED BY Q1: option (b), the shared position broker

Unless a **subaccount is obtained from the broker**, which is a commercial
question and not an engineering one, the three deployments cannot be separated by
account. Q2 confirms three processes *can* each hold a session, so the shape is
available — but they would all be looking at one netted position, which is
§7.1's guaranteed halt.

**What (b) has to do**, now that the mechanics are measured rather than guessed:

1. **One process owns the account.** It holds the Shioaji session that
   `subscribe_trade` covers, and it is the only thing that calls `place_order`.
2. **Three deployments submit intents, not orders.** Each keeps its own mirror
   of *its own* signed position — which is what its Trader already tracks.
3. **Reconciliation compares the venue against the SUM of the three mirrors**,
   not against any one of them. This is the single change that makes §7.1's
   `mirror != venue` halt correct instead of guaranteed, and it is a change to
   `SafetyRails.on_reconcile`, which is why S9 touches the rails.
4. **Deal attribution is the owning process's job.** `_match` already resolves a
   deal to a tracked order by `ordno`/`order_id`; with one owner there is one
   `_match` and no foreign deals, so §7.2 dissolves rather than needing a fix.
5. **Netting is a decision, not a detail.** DAY long 1 + NIGHT short 1 is either
   flat at the venue (cheaper, but neither deployment can be measured
   independently) or two legs that cannot both exist on one account. §7.4 is
   still open and now blocks S9's design, not just its deployment.

**Worth running before building it:** `--mode session` (Q4/Q5). It does not
change the decision — that is settled — but it fixes (b)'s shape by measuring
whether a second session hears the first's deals, which decides whether the three
deployments can hold *read-only* sessions of their own or must receive everything
through the owning process.

---

## 12. S8 REVISED — one credential per deployment · **SUPERSEDED by §14**

> **The premise of this section was measured false.** Separate API keys do not
> give separate position books (§14 Q5). The plumbing it describes
> (`live/credentials.py`, `config/secrets.toml`) is still correct and still
> wanted — it is what a sub-account rollout carries — but the conclusion
> *"(b) is not needed"* does not hold. Read §14.

**The user's decision, recorded as given:** obtain **one Shioaji API credential
per deployment** — day, night and full-day each get their own — because two
strategies on one account flatten each other ("strategy A says buy and strategy B
says sell simultaneously will flat the position"). Config lives in `config/`.

That is **option (c) after all**, reached by a route the code could not arrange:
S7 measured one futures account on the *existing* credential, which killed (c)
for that credential — not for the design. Three accounts make each deployment's
position its own, and **(b), the shared position broker, is not needed.** That
removes the largest single piece of work in this plan.

### Why (c) is near-zero code — measured, not hoped

Every rail already scopes by account, which S7's introspection established:

| already true | consequence |
|---|---|
| `list_positions(account=...)` takes an account, and `broker/shioaji.py` already passes one | §7.1 dissolves: each deployment's mirror is compared against **its own** account |
| `subscribe_trade(account)` subscribes one account; a login subscribes only what it owns | §7.2 dissolves: no foreign deals, so no `_anomaly` halt |
| S7 Q2: two sessions coexist, `connections` 1 → 2, first survives | three processes can each hold a session |

So the only thing missing was a way to say *which* credential a deployment uses.

### What landed

| file | what |
|---|---|
| `live/credentials.py` | `load(deployment)`; `config/secrets.toml` first, legacy env pair as fallback. (`check_account` was added here and **removed** the same day — see §13) |
| `config/secrets.example.toml` | committed template; `config/secrets.toml` is **gitignored** |
| `.gitignore` | `config/secrets.toml` ignored, `secrets.example.toml` negated — verified by exit code, not by eye |
| `live/cli.py` | credential looked up **by deployment**. (The `expected_account` login check listed here was **removed** — §13) |
| `live/multi_deploy_probe.py` | new `--mode accounts` (Q0); Q1-Q3 retired to `--mode offhours` |
| `tests/test_live_credentials.py` | 6 tests on the guards (9 before the `expected_account` removal) |
| `docs/live/credentials.md` | new page |

### Keys on disk — the choice and its guards

Storing keys in `config/secrets.toml` was chosen over naming env vars. The risk
was stated and the choice stands; these are what make it as safe as it can be:

1. **A `secrets.toml` that git is tracking is REFUSED, not read.** That is the
   one way this design leaks three live credentials at once. The refusal names
   the fix in order — `git rm --cached`, check `.gitignore`, then **rotate**,
   because a committed key is disclosed whether or not it was pushed.
2. **The template's placeholders are rejected** rather than sent to the broker,
   where they would look like a network fault.
3. **No key material in any `repr`** — that is what travels through tracebacks.
   Account ids are masked too, as `cli.py` already does.
4. **Paths resolve from the module, never the CWD.** `--out` is deliberately
   relative; credentials must not be, or a run launched from the wrong directory
   picks up a different key set and fails like a broker outage.

### `expected_account` — **REMOVED the same day (§13)**

This section argued the field was "the check the whole design rests on". It was
neither: `cli.py` already had `account_fingerprint` / `check_account_fingerprint`
doing the same job with nothing to configure, and the duplicate *refused* where
the established check deliberately *warns*. Removed — see §13.

### S7's remaining value

Q1-Q3 are answered and moot. **Q0 replaces them and is load-bearing:** do the
three credentials actually reach three distinct accounts? `--mode accounts`,
about a minute, no market; exits **1** if two deployments share an account, so a
launch script can refuse. Q4/Q5 (`--mode session`) remain worth one run to
confirm that separate accounts really do isolate deals and positions — the same
assumption, measured from the other side.

### Sequence, revised

~~S7 probe → S8 done → S9 credential wiring → S10 fill in expected_account~~

**All four steps of that sequence are void.** S10 never existed
(`expected_account` was removed); S8 was not done (§14 reopened it); and G6's
wording here — *"each venue account matches its own deployment's mirror"* —
assumed a per-deployment account, which does not exist. The current sequence and
G6 definition are in §8, "As executed".

---

## 13. Q0 RESULT — the three credentials share ONE account · MEASURED 2026-09-02

```
day:     futures account ...2125
fullday: futures account ...2125
night:   futures account ...2125
SHARED ACCOUNT  ...2125 used by ['day', 'fullday', 'night']
exit 1
```

**CORRECTED 2026-09-02, after the user pushed back.** The paragraph that stood
here said the three keys "share an account" and therefore "will flatten each
other". That conflated two different claims, and only the first was measured:

| claim | status |
|---|---|
| the three credentials report the **same `account_id`** | **MEASURED** — `...2125` for all three |
| their **positions net against each other** | **NOT MEASURED.** Inferred from the first, which does not follow |

**The user's position, and it is plausible:** Shioaji tracks the simulated book
**per API key**, so a BUY from DAY and a SELL from NIGHT do not flatten each
other even though the account id is shared. Nothing measured here contradicts
that — `list_positions` was never called on two credentials with a position
open, which is the only observation that decides it.

**So the state is UNPROVEN, not broken**, and the probe now says exactly that
(`--mode accounts` reports the identity fact, names what it does not establish,
and points at Q5).

### What decides it: Q5, in simulation

```
uv run python -m forecast_eval.live.multi_deploy_probe --mode session     --i-understand-this-places-orders
```

It opens a position on one credential and asks the other what it can see. Two
outcomes:

- **B cannot see A's position** — the per-key book is real. Concurrent
  deployments work in simulation, `on_reconcile` is correct as written, and
  nothing further is needed for a soak.
- **B sees A's position** — the books are shared, §7.1 stands, and separate
  accounts are required after all.

### The residual risk, which the user named first

*"If using real money, they will be clear on the same account."* If simulation
isolates per key but production nets per account, then **every position-
reconciliation behaviour validated during a simulated soak does not transfer to
production** — the rails would be proven in a mode that does not match the one
they must survive. This cannot be measured without real money, so it is a known
unknown to carry, not a task to close. It is the same theme `cli.py` already
records about simulation being unverifiable at runtime: `[SIMULATION]` states
intent, it does not prove anything.

Practical consequence: a simulated soak validates the *machinery* (signals,
orders, fills, restarts, rolls). It does not validate that three concurrent
deployments keep separate positions under real-money clearing. That step needs
either three genuine sub-accounts, or a first production run held to one
deployment.

### A defect in the probe, found by reading its own output

The probe promised "every line is redacted" and did not deliver it. Shioaji's
compiled `_core.pyd` prints a connection banner —
`Response Code: ... Info: #P2P/v:.../PYAPI/<id>/...` — **straight to file
descriptor 1**, where `<id>` is the user's Taiwanese national ID. Those writes
never pass through Python, so `redact()` and every print-level guard missed them
entirely; `redact`'s national-ID pattern (`[A-Z][0-9]{9}`) was always correct and
simply never saw the line.

Fixed by `redacted_stdout()`, which replaces fd 1 with a pipe and filters every
line — C-level writes included — through `redact`. Verified: the banner now
renders `PYAPI/<redacted-id>/`. It fails open with a warning if the descriptor
cannot be duplicated, because a probe that refuses to start is worse than one
that warns.

This is the second time in this session that a redaction/classification
assumption about the SDK was wrong in the same direction — see also
`str(AccountType.Future) == 'F'` in §11. Both were caught by printing the thing
rather than reasoning about it.

### `expected_account` REMOVED · 2026-09-02

It was added the same day and taken out again, because the codebase already had
it and had it better. `live/cli.py` carries `account_fingerprint` /
`check_account_fingerprint`: it digests the account behind each run, stores it
per deployment in `live_account.json`, and compares it run-to-run with **nothing
to maintain by hand**.

The duplicate was worse in kind, not just redundant. It **refused** (exit 5)
where the established check deliberately **warns** — and that check's own comment
says exactly why:

> *"This warns rather than refuses. An account change can be legitimate (a new
> sub-account), and this cannot tell simulation from production anyway — so the
> honest action is to make the change impossible to miss, not to block on it."*

Obtaining sub-accounts is the expected next step here, so a hard refusal would
have blocked the very thing the design is moving toward. The argument against the
field was written in this repo before it was added.

Removed: the `Credential.expected_account` field, `credentials.check_account`,
its `cli.py` call site, the probe's per-deployment check, the
`--print-account-ids` flag (which existed only to fill the field, and was the one
output that printed an account id unmasked), the template entries, and three
tests. `config/secrets.toml` now holds nothing but `api_key` and `secret_key` per
deployment.

**A regression this exposed:** removing `check_account` also removed `_mask`,
which sat below it and which the probe used. The full suite stayed green —
`multi_deploy_probe.py` has no test coverage, because exercising it needs live
credentials. Probe regressions are caught by running the probe, not by `pytest`;
that is a real gap and worth remembering before trusting a green suite about
anything under `live/*_probe.py`.

### The redaction fix, second attempt · 2026-09-02

The first fix for the SDK's unredacted national ID replaced **file descriptor 1**
with a pipe in-process. It worked where stdout was already a pipe and **crashed
on an interactive Windows console**:

```
File ".../multi_deploy_probe.py", line 188, in probe_accounts
    print()
OSError: [WinError 1] Incorrect function
```

Python uses `_WindowsConsoleIO` for a console fd and writes through
`WriteConsoleW`, which fails the moment fd 1 is a pipe. It passed here only
because this shell pipes stdout — the one environment where the bug is invisible,
and not the one the probe is run in.

Replaced with a **child process**: the probe re-runs itself with stderr folded
into stdout and filters that pipe through `redact`. A child's stdout is a pipe by
construction, so the SDK's C-level writes, Python prints and tracebacks all land
in one filterable stream while the parent's console handle is never touched.
Verified under both bash and PowerShell.

**General lesson for this repo:** anything that reassigns fd 1 will hit the same
wall on a Windows console. Filter a child, do not redirect yourself.

### Probe output cleaned up · 2026-09-02

Three fixes after the user ran both modes:

1. **Mid-line splicing.** Output read
   `day: source   config/secrets.tomlResponse Code: 0 | ...`. The child runs with
   `-u`, so `print` emitted the text and its newline as two separate writes and
   the SDK's C-level banner landed between them. `_say` now formats the whole
   line including the newline and emits it in **one** write. It cannot stop the
   banner appearing *between* our lines — that is another thread — but it stops
   it appearing *inside* one.
2. **`--mode offhours` carried the same over-claim** `--mode accounts` had
   already been corrected for: it read one futures account and concluded a
   "netted futures position". It now reports the identity fact and points at Q5,
   consistently with the other mode.
3. **Account ids are masked everywhere.** Q1 printed `1922125` raw in two places
   while the accounts mode masked it. The only reason to show it unmasked was
   `--print-account-ids`, which went with `expected_account`.

`offhours` and `session` also read `config/secrets.toml` now (via
`--credential`, default `night`) instead of requiring the environment pair — a
machine set up for live running could otherwise not run the probe.

### Q4/Q5 run · 2026-09-02 — one real answer, one vacuous measurement

**Q4 — evidence FOR isolation, stronger than the probe first claimed.**
Session A placed and cancelled an order; the SDK printed both order-state events,
so two events demonstrably occurred on A's session. **Session B's callback
received zero.** Order events are therefore *not* broadcast across sessions on
this account.

The probe's original verdict blamed the silence on the order never filling. That
is true of **deals** and wrong about **order events** — `set_order_callback`
delivers those too, and B missed both of A's. Calling this "not proof of
isolation" understated a real positive result. Corrected. What remains genuinely
open is whether a *deal* routes like an order event; only a filled order settles
that.

**Q5 — VACUOUS, and the probe asserted a conclusion anyway.** The order rested
500 points below the market and was cancelled unfilled, so **no position ever
existed**. "0 positions vs 0 positions" compares nothing. The probe nonetheless
printed:

> *"Both sessions query the SAME account, so any position either opens is summed
> into the other's reconciliation (§7.1)."*

as though it had been observed. It had not. That is the **third** time in this
session an unmeasured inference was printed as a finding — after the
`account_id`-implies-netting claim and the `AccountType.Future` misclassification.
The pattern is the same each time: a conclusion written before the measurement
existed, and left in place when the measurement came back empty.

Fixed: Q5 now branches on whether a position actually existed, and reports
`VACUOUS: no position existed ... Q5 is UNMEASURED` when it did not.

**To actually measure Q5:** `--marketable` places an order that fills, compares
what each session sees, then flattens it (and says so loudly if the flatten
fails). It is opt-in because it takes real simulated exposure.

```
uv run python -m forecast_eval.live.multi_deploy_probe --mode session \
    --i-understand-this-places-orders --marketable
```

**Incidental findings from the order event:** the account payload carries a
`subaccount` field, empty here — worth knowing, since sub-accounts are the
alternative to per-key isolation. `person_id` came back empty in this payload.
The order routed to `TXFI6` with `market_type: 'Day'`.

---

## 14. Q4/Q5 SETTLED · MEASURED 2026-09-02, night session

Run with `--marketable`, so the order actually filled (`FDEAL` Buy 1 TXFI6 @
46070) and was flattened immediately (`FDEAL` Sell 1 @ 46072, position after
flatten **0**).

| | measured | meaning |
|---|---|---|
| **Q4** | session B received **0** callback messages while A placed, filled and cancelled | order events **and deals** are **per-session**. Each deployment mirrors only its OWN fills |
| **Q5** | `positions seen by A = 1`, `positions seen by B = 1` — and **only A opened anything** | the **position book is SHARED** across API keys on one account |

### The premise this disproves

The working assumption since §12 was the user's: *"use multiple API keys will not
have an issue of buy and sell signal from different strategies causing a flat
position as result."* **Measured, that is false.** B sees A's position, so the
book is common — a BUY from one strategy and a SELL from another net against each
other exactly as they would on one key. Separate API keys are an authentication
boundary, not a position boundary.

This was the right thing to measure rather than argue: the earlier reasoning on
both sides (mine that shared `account_id` implies netting, the user's that
per-key books prevent it) was inference. Only Q5 settled it.

### Why this pairing is the worst case for the rails

The two results pull in opposite directions, and the combination is worse than
either alone:

* **deals are per-session** ⇒ a deployment's `Trader.position` mirror counts only
  its own fills;
* **positions are shared** ⇒ `list_positions` returns the COMBINED size to every
  deployment.

`SafetyRails.on_reconcile` compares exactly those two numbers. So the first time
two deployments hold at once, **both halt** on `mirror != venue` — §7.1, now
measured rather than predicted. Had deals also been shared, each mirror would at
least have tracked the combined size and agreed with the venue.

### What remains

**Option (c) via multiple API keys is dead.** The two live routes are:

1. **Real sub-accounts.** Every order and deal payload carries a `subaccount`
   field, empty throughout this run — that is the hook a genuine sub-account
   would populate, and `list_positions` would then scope to it. This is the
   cheap answer if the broker offers it, and unlike per-key behaviour it should
   also hold under real-money clearing.
2. **Option (b), the shared position broker** — one process owning the account,
   three deployments submitting intents, reconciliation against the SUM of the
   three mirrors.

**Until either exists, run the three deployments SEQUENTIALLY.** One at a time on
this account is safe and is what the NIGHT soak has always done. Two at once is a
measured double halt, and a measured netting of their positions.

**Unchanged by this:** everything built in S1-S6 stands. `credentials.py` is
still the right plumbing — it is what a sub-account rollout would carry — and the
backtest split is untouched by any of it.

---

## 14b. S7 CLOSED · 2026-09-02

All six questions measured; the step is done and needs nothing further.

| | question | answer |
|---|---|---|
| Q0 | do the three credentials reach distinct accounts? | **no** — same `account_id` |
| Q1 | how many futures accounts on one credential? | **1** |
| Q2 | do simultaneous logins coexist? | **yes** — `connections` 1 → 2, first survives |
| Q3 | is selective `subscribe_trade` accepted? | **yes**, returns `True` |
| Q4 | does a deal on A reach B's callback? | **no** — order events *and* deals are per-session |
| Q5 | does A's position appear in B's `list_positions`? | **yes** — the book is shared |

**What it delivered:** a decision. Per-key separation does not work, and that is
now measured rather than argued — by both sides, in both directions. It also cost
almost nothing: one afternoon, four runs, one filled contract in simulation.

**Two things that stay live after closure.**

1. **The probe is a standing gate, not a one-shot.** `--mode accounts` must be
   re-run whenever the credentials or accounts change — most obviously the day
   sub-accounts arrive. It exits **1** when two deployments share an account, so
   a launch script can refuse on it. Q3 is the only question that became moot
   (there is nothing to filter, since deals are already per-session).
2. **Everything was measured in `simulation=True`.** Q5 (shared book) is the
   blocker and would be no better with real money — the user's own point is that
   real money clears on one account. Q4 (per-session deals) is the one that could
   differ in production, and differ *favourably*: if deals were shared there,
   every mirror would track the combined size and agree with the venue. Neither
   is testable without real money, so it stays a known unknown, carried, not
   closed.

**What S7 does NOT settle**, and never could: whether the broker will issue
sub-accounts. That is a commercial question, and it is the only thing standing
between here and S9b.

---

## 15. OPERATING RULE — one instance at a time, whatever the session

> **With real money there must be exactly ONE running instance of
> `live/cli.py` at any moment — regardless of which deployment it is, and
> regardless of whether the sessions overlap on paper.**

Confirmed against the broker's official bot, 2026-09-02, after §14 measured it
here.

**Why "the sessions do not overlap" is not a safe reason to run two.** DAY's
median hold is **20 calendar days** and NIGHT's is **4.5** (§6, C8). A position
opened in one session is still open right through the other. The *sessions* are
disjoint; the *positions* are not. A DAY deployment holding long while a NIGHT
deployment opens short is the flat-position case, and both are then halted by
`mirror != venue`.

**What lifts the rule.** Only the venue giving each deployment its own book:
real sub-accounts (the `subaccount` field is empty on every order and deal
today), or the shared position broker of §7.1(b). Not more API keys — that was
measured and rejected in §14.

The rule is stated at the top of `live/cli.py`, in `docs/live/cli.md` and in
`config/secrets.example.toml`, because those are the three places someone stands
when they are about to start a second one.

---

## 16. THE FIX — record our own transactions, reconcile the SUM · **BUILT 2026-09-02**

**The user's suggestion, recorded 2026-09-02, and it is the right shape.** The
venue cannot tell us what a *deployment* holds, only what the *account* holds.
So the deployment's own position must come from **our** record of its fills, and
the venue's number must be compared against the **sum** of those records.

This is not new machinery; it is a change of operand. Everything needed exists:

| already present | what it gives |
|---|---|
| `Trader.position` | the deployment's own signed size, updated on its own fills — already correct, because Q4 measured that a session hears only its own deals |
| `live/ledger.py` — `FillLedger`, `ledger_path`, `net_position(fills)` | a durable per-deployment fill record at `fills_<slug>.csv`, and the function that turns fills into a signed lot count |
| `cache_slug()` | `day` / `night` / `day_night`, so the three ledgers are already separate files |

**The change** is at `live/loop.py:281`, which today reads:

```python
self.rails.on_reconcile(
    mirror=self.trader.position,
    venue=self.broker.broker_position(),
)
```

`mirror` must become **the sum of every deployment's ledger**, not this one's
position:

```
expected = sum(net_position(load(p)) for p in every fills_*.csv beside this run)
self.rails.on_reconcile(mirror=expected, venue=self.broker.broker_position())
```

Then the comparison is like-for-like: what *we* believe the account holds in
total, against what the venue says it holds. A disagreement is then a real
desync — a missed or double-counted fill — which is what the rail was written to
catch, instead of the routine consequence of a second deployment existing.

**Three things this design has to get right, none of them free:**

1. **Freshness.** A ledger is read from disk, so the sum is only as current as
   the other deployments' last flush. `FillLedger`'s write cadence becomes part
   of the reconciliation's correctness, not just its durability.
2. **A deployment that is not running still holds.** Its ledger must be included
   whether or not its process is alive — which is the point, since a DAY
   position persists for ~20 days across many NIGHT sessions.
3. **Per-deployment PnL stays honest.** Each deployment still books its own
   fills to its own `Portfolio`; only the *rail's* operand changes. Attribution
   must not start summing.

**This does not lift §15.** Reconciling correctly stops the spurious halt; it
does not stop the two deployments' orders from netting into one book. Only
separate books do that. §15 stands until sub-accounts or (b) exist.

### What landed

| file | what |
|---|---|
| `live/ledger.py` | `account_net_position(out_parent)` — sums every `fills_*.csv`, all deployments, all soak epochs; `None` when absent or unreadable |
| `live/loop.py` | `expected_position` callable; when it returns a value that becomes the reconcile mirror, else this deployment's own position |
| `live/runtime.py` | `ledger_dir` parameter + `_expected_account_position()` |
| `live/cli.py` | passes `ledger_dir=out.parent` — the same directory `ledger_path` writes into |
| `tests/test_live_reconcile_sum.py` | 16 tests: the arithmetic, the degradation, the end-to-end case through a real `LiveLoop`, and the three that close the assumptions (below) |
| `docs/live/{ledger,loop,state,runtime}.md` | the changed operand, on every page that documented the old one |

**Design notes, each of which is a way it could have gone wrong:**

- **Optional and inert by default.** `expected_position=None` reproduces the
  previous operand exactly, which is why the V0 parity harness and every offline
  test are untouched. It is the fourth optional passenger on `LiveLoop`, for the
  same reason the other three are optional.
- **`None` means "cannot compare", never zero.** An absent directory, no ledger
  files, or one unparseable file all return `None` and fall back. A partial sum
  would be a confident wrong number — precisely the failure this removes — so it
  refuses to produce one. Same degradation `broker_position()` already uses when
  the venue will not answer.
- **`if declared is not None`, not `if declared`.** A ledger sum of **0** is the
  common case (a flat account) and must be *used*, not discarded as falsy. A
  test pins this specifically.
- **All soak epochs count.** `FillLedger.replay()` scopes to `soak_id` because it
  rebuilds one run's *history*; this asks what the *account* holds, and a retired
  epoch's fill moved the position just as much. Retired epochs net to zero in
  practice — `--fresh-start` refuses unless the account is already flat.
- **A fill placed outside this system still halts the loop.** It appears in no
  ledger, so the sum disagrees with the venue. That is intended and is the rail's
  original purpose, now restored rather than drowned in false positives.

### Three assumptions the first tests made, now closed

The original 13 tests all built their ledgers by hand from `LEDGER_COLUMNS`.
That shares a constant with the writer but not the *writer*, and it left three
ways for the fix to be silently inert in production while the suite stayed green:

1. **Round trip.** Fills written through the real `FillLedger.sync` are now read
   back by `account_net_position`. A change to how `sync` spells `side`, or a
   column reorder, would have left every hand-built test passing while production
   summed to zero.
2. **Directory agreement.** `cli.py` writes to `ledger_path(out.parent, policy)`
   and passes `ledger_dir=out.parent`; nothing connected those two arguments. If
   they ever disagree the sum reads an empty directory, returns `None`, and falls
   back to the old behaviour — **silently**, because an empty directory is a
   legitimate state.
3. **Glob coverage.** Every deployment's real ledger filename must match
   `fills_*.csv`, and the three must be distinct files. A deployment the glob
   misses is a deployment whose position is invisible to the rail.

All three are the same failure in different clothes: the fix doing nothing, and
nothing saying so.

### The freshness caveat, unresolved by design

The sum is read from disk, so it is only as current as the *other* deployments'
last `FillLedger` flush. A deployment that fills and has not yet flushed is
invisible to its peers for that window, and the rail would see a real
disagreement and halt. This is a genuine race and it is **not** fixed here:
fixing it means either a flush-before-reconcile contract between deployments, or
accepting a tolerance window — and a tolerance on a safety rail deserves its own
decision, not a default chosen in passing. Under §15 (one instance at a time) the
race cannot occur, which is why it can wait.
