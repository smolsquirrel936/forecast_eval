# STRATEGY_SPLIT_PLAN.md — three independent strategy pipelines (day / night / full-day)

> ### ⚠ PARTLY SUPERSEDED 2026-09-09 — `LIVE_STRATEGY_ROUTING_PLAN.md`
>
> **§2.3's three `LiveConfig` factories no longer exist.** `build_emitter`,
> `build_forecaster` and `build_trader` were removed, together with the ~19
> strategy fields they read: `live/runtime.py` now builds the whole stack through
> `strategy.wiring.make_strategy`, the same function `real_data.py --strategy`
> uses. The reason is in that plan's §2 — `build_emitter` imported
> `.emitter.ThresholdEmitter` *by name*, so a package whose `make_strategy` built
> anything else was invisible to live, while `build_trader` *did* route through
> the package. §2.3 achieved what it set out to do; it is kept as the record of
> how the seam got here.
>
> **Still true:** `strategy_package` as a derived property, per-session component
> ownership, and `PINNED_FULLDAY` registered-and-untuned.
>
> **No longer true:** §5/D5's `is_tuned` **refusal**. Removed 2026-09-09 (routing
> plan D1) because the broker it guarded is hardcoded to simulation. The property
> survives and still marks the run in the banner and the recorder's `meta` header;
> only the refusal is gone.
>
> **Read §2.3, §11's file table and §5 with this note in hand.**

**G6 LIVE PASSED — §23 (2026-09-07, run 4).** §15 lifts for the shape it tested:
two opposed one-lot positions, ten minutes, no halt, ledgers matching the book.
**NEXT: §23.7** — the teardown is INTERMITTENT, not fixed. Booking latency was
12 s (run 4) and 42 s (run 5) against a 40 s window, now 90 s. Every run now
prints latency and callback counts. Account never held (§23.5); venue-vs-sum
was `0 == 0` (§23.1).

**Status (2026-09-07, end of day):** S9b code-complete. G6's offline half
passes; the live half ran four times — §21 (silent), §22 (every fill
dropped), §22.7 (false pass), **§23 PASSED**. **1209 tests, nothing
committed.**

**Status (2026-09-02, end of day):** S1–S6 **built, G1–G5 green** (§9, §10).
**S7 CLOSED** — Q0–Q5 all measured (§14b; detail in §11 as amended, §13, §14).
**S8 DECIDED — option (b), the owning process (§18).** Per-key separation was
measured false (§14), and `shioaji` 1.7.2 **cannot address a sub-account at
all** — it is a read-only label on inbound events, with no handle on
`FuturesOrder`, `Account`, `list_positions` or `FuturePosition` (§17.1), so no
broker cooperation reaches route (c) from here. (b) is the only route that waits
on nobody. **§18 is its spec**; three design calls in §18.4 are open before it
is written. **§15 is the operating rule in force meanwhile: one instance at a
time.**

> **Reading order.** §§0–10 describe the split and are current. §7 and §11 are
> **historical**: they record what was predicted before measurement, and several
> predictions were wrong. Each now carries a MEASURED banner pointing at the
> section that settled it. **§20 is the handoff — start there**: it carries the
> next action, the command, how to read its result, and where everything stands.
> Then §18 (the design and its measurements) and §19 (G6).

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

> **Superseded 2026-09-09** — all three factories described below were removed;
> `runtime.py` calls `strategy.wiring.make_strategy` once instead. See the note
> at the top of this file. What follows is the record of the step, not of the
> current code.

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
       BRIEF: §17. Sub-accounts are unaddressable in shioaji 1.7.2 (§17.1),
       and §16 already built (b)'s core, so the routes are now:
         (c)      a sub-account with its OWN account_id  -> zero code, needs broker
         (b-lite) shared netted book + ledger truth      -> 4 local fixes (§17.2)
         (b)      owning process + intents               -> correct without broker
       DECIDED 2026-09-02: (b), the owning process. Spec in §18.
S9a    §16 — reconcile against the SUM of our own fill ledgers   DONE 2026-09-02
S9b    build (b): owner process + ClientBroker + N-order book        §18.3
       DONE 2026-09-07 — code-complete, 1200 tests
S10    G6 — offline half PASSES (§19.1); live half built, waiting (§19.2)
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
| `live/config.py` | *(the three `build_*` factories here were removed 2026-09-09 — see the top-of-file note)* `is_tuned` + `strategy_package` properties; `source_commit`/`source_calmar` became `Optional`; `describe()` renders an untuned config; **`PINNED_FULLDAY`** + `CONFIGS["fullday"]` |
| `live/runtime.py` | `self.trader = config.build_trader(execution=self.broker)` replaces the inline construction *(since 2026-09-09: `self.strategy["make_trader"](execution=self.broker)`, from the one `make_strategy` call)* |
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

> **PARTLY LIFTED 2026-09-07 by §23.** G6's live half passed: two deployments
> held **opposed one-lot** positions for ten minutes under the owning process
> (§18), neither rail halted, and each deployment's ledger matched its own book.
> The rule below is retired **for that shape and no other** — two deployments,
> one lot each, under `owner_cli.py`. Three deployments, sizes above one lot, and
> a full session are each a bigger claim than that run makes, and the traffic
> question (§18.13) is untouched. It stands unchanged for anything wider, and for
> any deployment run **without** the owner.

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

---

## 17. S8 BRIEF — what the SDK can address, and what §16 already removed · 2026-09-02

S8 is a decision, and it was left resting on a question only the broker can
answer. Before asking it again, two things were checked here that change its
shape: **what `shioaji` 1.7.2 can actually address**, and **how much of option
(b) §16 already built**.

### 17.1 Sub-accounts: MEASURED, the SDK can read one but cannot address one

Introspected against the installed package (`shioaji 1.7.2`, `_core.pyi` +
live signatures), because per this repo's record the docs have been wrong
repeatedly:

| surface | subaccount? | consequence |
|---|---|---|
| `sj.FuturesOrder(action, price, quantity, price_type, order_type, octype, custom_field, account)` | **no** | an order **cannot be placed into** a sub-account |
| `sj.Account` — `account_id, account_type, broker_id, person_id, signed, username` | **no** | a credential cannot name one |
| `api.list_positions(account=None, unit=None, ...)` | **no** | positions **cannot be scoped** to one |
| `FuturePosition` — `id, code, direction, quantity, price, last_price, pnl` | **no** | the reconciliation rail could not read one even if it were there |
| `FuturesDeal` / `FuturesOrder` **event payloads** | **yes**, `subaccount: str` | read-only, inbound only — the empty field §14 saw |

**So `subaccount` is a label on what comes back, not a handle on what goes
out.** §14 called it "the hook a genuine sub-account would populate"; measured,
it is a hook with nothing on the other end of it.

**This sharpens the broker question from commercial to mechanical.** It is no
longer "will you issue sub-accounts" but:

> **Does a sub-account appear in `api.list_accounts()` as its own `Account` with
> its own `account_id`?**

- **Yes** → it is not a sub-account as far as this code is concerned, it is a
  second account, and **everything already built works unchanged**: §12's
  `credentials.py`, `list_positions(account=...)`, `order.account=...`, one rail
  per deployment. Route (c) is live, at near-zero code, exactly as §12 argued.
- **No — it is a tag inside one `account_id`** → **route (c) is dead in SDK
  1.7.2**, not for want of a sub-account but for want of any way to place into
  or read out of one. No amount of broker cooperation fixes that from here.

`--mode accounts` is already the gate that answers it (§14b), and it exits 1 on
a shared account.

### 17.2 §16 removed most of option (b)

§7.1 called (b) *"the largest single piece of new work in this whole plan"*.
That was true when (b)'s core was *"reconciliation compares the venue against
the SUM of the mirrors"* (§11, point 3). **§16 built exactly that**, offline,
against fill ledgers, with 16 tests. What (b) still buys over what exists is
much smaller than the §7.1 table implies, and is worth restating as a third
route.

**Route (b-lite) — the shared netted book, kept honest by the ledgers.** No
owning process, no intent protocol, no IPC. Three deployments keep placing their
own orders on the one account; §16's sum is what the rail compares. Four known
defects remain, all local:

| # | defect | where | shape of fix |
|---|---|---|---|
| 1 | `adopt_position` seeds this deployment's mirror from the **combined** venue book | `live/runtime.py:246` (`broker_position_detail()`) | adopt from **its own** ledger; the venue number becomes a check on the sum, not the source |
| 2 | `--fresh-start` **refuses a non-flat account** | `live/cli.py:771` | compare against this deployment's own ledger; the account being non-flat is normal when a peer holds |
| 3 | `flatten.py` closes the account's whole net (`abs(held)`) | `live/flatten.py:169` | close only this deployment's lots, or refuse while a peer ledger is non-flat |
| 4 | the **freshness race** — the sum is only as current as peers' last flush | §16's own open caveat | flush-before-reconcile contract, or a tolerance window (a tolerance on a safety rail is its own decision) |

**Full (b)** — one process owning the session and `place_order`, three
deployments submitting intents — additionally buys: no freshness race by
construction (one process, one in-memory book), and one `_match` owner. It costs
a new process, an intent protocol between four processes, and a restart story
for the owner. It is the only route that is correct without the broker.

### 17.3 The hazard that gates (b-lite), and is unmeasured

`ShioajiBroker.submit` sets `octype` **explicitly** — `New` for OPEN, `Cover`
for CLOSE (`shioaji.py:264`) — deliberately, so a mirror desync surfaces instead
of being masked by `Auto` (§2.3). On a **shared netted book** that premise
breaks in both directions:

- NIGHT sends `New` Sell 1 while the account is long 1 (DAY's). Is it accepted
  as an opening short that nets, or rejected?
- DAY later sends `Cover` Sell 1 to close its long — but NIGHT's short already
  netted the account flat. **Cover with nothing to cover.** Rejected, or
  silently reinterpreted?

Neither is measured, and the second is the ordinary case, not a corner. **A
rejection here is not a halt the rails were designed for** — the order simply
does not exist, and the deployment's ledger and the venue diverge for real.

This is measurable in `simulation=True` with the harness that already exists
(`multi_deploy_probe.py --mode session` opened two sessions and filled a
contract for §14). One run: A opens `New` long 1, B sends `New` short 1, then A
sends `Cover` sell 1 — record what the venue does at each step. **It gates
(b-lite) and full (b) equally**, since both place both octypes onto one book;
it does not gate (c).

### 17.4 What S8 now needs

1. **The broker question, in its sharpened form** (17.1) — does a sub-account
   carry its own `account_id`? That is a yes/no that decides (c) outright.
2. **The octype probe** (17.3) — one simulation run, no market needed, and it
   decides whether the shared-book routes are reachable at all.
3. **The exposure decision** (§7.4), still the user's and still open: DAY long +
   NIGHT short nets to a flat venue account. Aggregate PnL stays correct and
   each deployment still books its own fills at its own prices — but margin,
   exposure and the broker's own realized-PnL statement are all account-level,
   and will not match per-deployment attribution.

**§15 stands throughout: one instance at a time.** Nothing in this section
changes that, and none of the three routes lifts it until it is built.

---

## 18. S8 DECIDED — option (b), the owning process · 2026-09-02

**The user's decision, recorded as given: build full (b), not (b-lite).** It is
the only route that does not wait on the broker and the only one without §16's
freshness race, and §17.1 measured that the sub-account route cannot be reached
from this SDK even with the broker's cooperation.

### 18.1 Does the octype probe (§17.3) still matter under (b)? — the user asked

**Yes, but it is a different question now, and it is no longer a gate.**

Under (b-lite) the hazard was unavoidable: three processes each set `octype`
from *their own* intent, so DAY's `Cover` could arrive at an account NIGHT had
already flattened. **Under (b) the owner knows the account's true net**, so it
maps `octype` from the **account** net rather than from the deployment's intent,
and Cover-with-nothing-to-cover cannot be constructed. Example: DAY +1, NIGHT
−1, account 0. DAY closes — a `CLOSE` intent — but the account goes 0 → −1, so
the owner must send **`New`**, not `Cover`. The mapping is *account*-level and
it is the owner's job.

**What still needs measuring is the case that mapping cannot express: an order
that crosses through flat.** Account +1; the owner nets the pending intents to
−1; that is one Sell 2 that is `Cover` for its first lot and `New` for its
second. Does the venue accept a single quantity-2 order there, and under which
`octype` — or must the owner **split it into two orders**? That is a real design
input (it decides whether `_place` is one call or a pair), not a blocker, and it
is the same one-run simulation probe with a rewritten script.

### 18.2 The shape

Four processes. Three of them barely change.

```
day/ night/ fullday/  ──intent──►  OWNER  ──place_order──►  Shioaji
   (LiveLoop each)    ◄──fill───   (one session, one account book)
```

- **Each deployment keeps everything it has** — Forecaster, Trader, Portfolio,
  its own rails, its own ledger, its own PnL. Only its `Broker` changes.
- **`ClientBroker`** implements the same six-method `broker/base.py` interface
  and forwards `submit` / `cancel` over the transport; `poll_fills` drains what
  the owner sent back. It slots in at **`live/cli.py:729`**, the single
  construction site, as another `--broker` value.
- **The owner** holds the one Shioaji session, the one `subscribe_trade`, the
  one `_match`, and is the only caller of `place_order`. §7.2's foreign-deal
  problem dissolves — with one owner there are no foreign deals — which is what
  §11 point 4 predicted and Q4 then made moot from the other side anyway.

### 18.3 What actually has to be built — with the evidence for each

| # | piece | why, measured |
|---|---|---|
| 1 | **An N-order book in the owner** | `ShioajiBroker` tracks exactly **one** live order — `self._live` (`shioaji.py:159`), single-valued at 276/363/402/416/629. Three deployments can each have a working order. **This is the largest single piece**, and it is in the file the rails read. |
| 2 | **Per-order deployment tagging** | `_match` (`shioaji.py:622`) already resolves a deal to a tracked order by `ordno`/`order_id`. Tag each tracked order with its slug and every fill routes back to exactly one deployment — attribution is then exact, not apportioned. |
| 3 | **Account-level `octype` mapping** | §18.1. Today `submit` sets it from `order.intent` (`shioaji.py:264`). Under one owner it comes from the account net. |
| 4 | **The transport** | see 18.4 — the open call. |
| 5 | **Owner restart** | the owner must rebuild the account book from the three ledgers. §16's `account_net_position` is already exactly that function. |
| 6 | **An owner-down rule for deployments** | a `ClientBroker` whose owner is not answering must **halt**, not trade blind. `has_venue` / `broker_position() → None` already means "refuse, do not guess" (`broker/base.py`), so the existing degradation rule covers it. |
| 7 | **`adopt_position` and `--fresh-start`** | `runtime.py:246` adopts the **combined** venue book, and `cli.py:771` refuses a non-flat account. Both are correct for one deployment and wrong for three; under (b) both read the owner's per-slug answer. Same two defects (b-lite) had (§17.2), unchanged by the route. |
| 8 | **`flatten.py`** | closes the account's whole net (`flatten.py:169`). It is the one tool that *should* stay account-wide — but it must refuse, or say plainly what it is doing, while the owner is running. |

**What does not change:** the backtest split (S1–S6), `credentials.py`, §16's
ledger sum (it becomes the owner's restart seed and the deployments' own record
rather than the rail's operand), and every offline test.

### 18.4 The design calls — ANSWERED 2026-09-02

1. **Transport: filesystem line-delimited JSON.** `intents_<slug>.jsonl` /
   `events_<slug>.jsonl` beside the ledgers. Matches every existing idiom here,
   adds no dependency, and leaves a durable audit trail readable after a crash;
   the loop is per-minute, so polling latency is irrelevant. The cost it accepts
   is **stale-file semantics** — a dead deployment's file looks exactly like a
   quiet one — so intents carry a sequence number and a heartbeat, and §18.3
   item 6's owner-down rule is the same mechanism read from the other side.

2. **The owner is a pipe. No account-level cap.** *The user's question — "why
   don't let each process maintain its own ledger?" — and it is the right
   objection.* Each process already does (§16), and **all three pinned configs
   run `max_position=1`** (`config.py:76`; none of `PINNED`, `PINNED_DAY`,
   `PINNED_FULLDAY` override it). So the account is already bounded at **±3
   net** by the deployments' own rails, and an owner cap would be a second guard
   on a number that is already guarded. It would only earn its place as a
   preference for the account to carry *less* than the sum of what the three
   strategies independently want — a risk choice, not a correctness need, and
   not one being made.

   **What the owner does still need is a rail, which is not a cap:** it holds
   the account's truth, so it reconciles the venue against the sum of its own
   per-slug books. That is §16's comparison moved into the one process that can
   make it without reading anyone's disk — which also retires §16's freshness
   race, since the sum is now in memory rather than assembled from three flushes.

3. **§7.4, now answered by the above.** DAY long + NIGHT short nets flat at the
   venue. Aggregate PnL stays correct and each deployment books its own fills at
   its own prices, but margin, exposure and the broker's own realized-PnL
   statement are account-level and will not match per-deployment attribution.
   (b) does not change that — it makes it *correct*, not *separate*. Only a
   second `account_id` separates it, and §17.1 says that is the broker's to
   give. The exposure that netting produces is **bounded at ±3 and accepted**.

4. **The crossing probe (§18.1): build it now**, before the N-order book, since
   whether a crossing order is one call or a pair is an input to how the owner's
   place path is written.

### 18.5 The crossing probe — BUILT 2026-09-02, waiting to be run

`live/multi_deploy_probe.py --mode crossing` (X1). Three steps on one session:
`New` Buy 1 must leave **+1**, then `Cover` Sell 2 and, if that does not reach
**−1**, `New` Sell sized from whatever step 2 left. Verdict is one of *ONE
CALL, octype=Cover* / *ONE CALL, octype=New* / *MUST SPLIT*.

* **Refuses a non-flat account (exit 3).** Every step is read as a delta from a
  known position; starting on someone else's lot makes step 2 unattributable and
  would have the closing flatten close a position the probe did not open.
* **Every order is marketable by construction**, unlike `--mode session`'s
  resting order. §14 records what a resting order measures: "0 positions vs 0",
  and a conclusion drawn from it. Only a fill says what the venue did with an
  `octype`.
* **Flattens with `octype=Auto`** — deliberately not the explicit octype under
  test, since the flatten is not part of the measurement and must work from
  whatever state the steps left, including one where `Cover` was just refused.
* Gated on `--i-understand-this-places-orders` (exit 5), same as `--mode
  session`. Docs: [docs/live/multi_deploy_probe.md](docs/live/multi_deploy_probe.md).

**It needs the user to run it** — credentials are not reachable from the
assistant's shell. Simulation, no market required, about a minute:

```bash
uv run python -m forecast_eval.live.multi_deploy_probe --mode crossing --i-understand-this-places-orders
```

Its output is redacted, but READ IT BEFORE PASTING. **S9b's place path is
written against whatever it says.**

### 18.6 FIRST RUN — DEFECTIVE. X1 IS STILL UNMEASURED · 2026-09-02

The run printed **MUST SPLIT**. **That verdict is not a measurement, and it is
contradicted by the deals in the probe's own output.** The probe was wrong, not
the venue.

**What happened, from the transcript:**

| | |
|---|---|
| STEP 1 | `New` Buy 1 → `FDEAL` Buy 1 @ 45797. Position **+1**. Correct. |
| STEP 2 | `Cover` Sell 2 placed. After the fixed 5-second sleep its status was **`Submitted`** — *working, not finished*. The probe read the position, saw **+1**, and recorded "Cover did not cross". |
| STEP 3 | placed `New` Sell 2 **on top of the still-working Cover**. |
| then | `FDEAL` **trade_id 101ED7** — STEP 2's Cover — `Sell` **quantity 2** @ 45797. **The Cover had filled all along, in full.** Then STEP 3's two deals, 1 @ 45797 and 1 @ 45800. |
| end | 4 lots sold against a 1-lot long. Position read **−2**; the single-shot flatten's Buy 2 filled only 1 lot; the probe exited with the account **short 1**. |

**The defect: a fixed `sleep` is not a measurement of whether an order
finished.** The probe asked "where is the position" while the order that was
supposed to move it had not filled yet, then attributed the unchanged position
to the venue refusing the octype. This is the same class of error §13 and §14
each record once already — a conclusion drawn from a measurement that did not
measure — and it is now the third.

**What the run does establish**, and it points the other way:

* **The venue ACCEPTED a quantity-2 `Cover` against a 1-lot long and filled it
  in full**, at one price, with `oc_type` echoed as `Cover` on the order. So
  `Cover` is not validated against the size actually held — at least in
  simulation. That is evidence *for* **ONE CALL, octype=Cover**, but it is not
  the answer: nothing read the position cleanly after that fill and before the
  next order, which is the whole question.
* **A one-lot gap between the deals and the position.** Four lots sold from +1
  should leave −3; the read returned −2 with every deal already printed. I
  wrote here that `list_positions` was lagging. §18.8 overturned that for a
  clamp; §18.9 overturned it back to lag. **All three were wrong — §18.10
  measured the lag at 0.1 s, far too small to explain anything here.** The venue
  was inconsistent between runs. Read §18.12; ignore the mechanism claims in
  §§18.6, 18.8 and 18.9.
* `subaccount` was empty on every order and deal again, consistent with §14 and
  §17.1.

**The account was left holding.** It was flattened by hand afterwards.

### 18.7 The probe, corrected · 2026-09-02

Four changes, each one a way the first run went wrong:

1. **`_settle()` replaces the sleep.** It polls `update_status` until the order
   reaches a **terminal** status — `Filled` / `Cancelled` / `Failed` — and the
   position is never read against an order still working. `_status_name` reads
   the enum's `.value` rather than `str()`-ing it, because `str()` on a Shioaji
   enum is not its name (§13's `AccountType.Future` → `'F'`).
2. **An order that will not settle is CANCELLED, not abandoned.** On
   `--fill-timeout` (default 30 s) the probe cancels and keeps polling. A
   working order left behind is what filled underneath STEP 3.
3. **The flatten retries, up to `FLATTEN_ATTEMPTS` (3),** re-reading the
   position and re-snapshotting the price each time — the rule `flatten.py`
   already carries (§2.12: "submitted a close" is not "flat"). It now returns
   the lots still held, and the probe **exits 6** if it could not get flat, so
   the failure is visible to a script and not only to a reader.
4. **A third verdict, `SINGLE CALL LANDS WRONG`,** for a Cover that moves the
   account but not to −1. The first version folded that case into MUST SPLIT,
   which would have hidden a partial-cover result inside a "the venue refused"
   answer.

Every verdict now quotes the terminal status of each order, so a future reader
can see what the orders actually did rather than only where the probe thought
the position was.

**Re-run it.** The command is unchanged.

---

## 18.8 X1 ANSWERED — `Cover` CLAMPS AT FLAT · MEASURED 2026-09-02

Second run, clean this time — every order settled to `Filled` before the
position was read.

```
position at start   +0
A1  New   Buy  1  -> Filled, FDEAL Buy 1 @ 45772   position +1
A2  Cover Sell 2  -> Filled, FDEAL Sell 2 @ 45767  position  +0     <-- not -1
```

**The venue neither refused the order nor filled it into a short.** It accepted a
`Cover` for two lots against a one-lot long, reported a **single deal of quantity
2**, and left the account **flat**. The excess lot closed nothing and opened
nothing.

**This also explains the first run**, which is what makes it a finding rather
than an oddity. Run 1: +1, then `Cover` 2 (one lot effective → 0), then `New` 2
(both effective → −2). It read −2. Run 2: +1, then `Cover` 2 (one lot effective
→ 0). It read 0. **Both runs are off the naive arithmetic by exactly one lot,
in the same direction, at the same order.** The `list_positions`-lag reading in
§18.6 fitted one run; this fits both, and `New` is unaffected — run 1's `New`
Sell 2 opened a full two lots from flat.

### Why this is worse than a rejection, and what it costs

A refusal would be safe: the order fails, the rails see it, nothing moves. What
happens instead is that **the venue reports more than it does**.
`ShioajiBroker._match` books a fill from the *deal's* quantity into
`Trader.position` (`shioaji.py:622`). Given this deal, the mirror moves 2 while
the book moves 1 — **a silent one-lot desync on the very first crossing order**,
which the reconciliation rail would then correctly halt on, having no way to say
why.

**It is not a defect in the live path today.** `Trader.submit_exit` closes
exactly what the deployment holds, so a `Cover` larger than the position cannot
currently be constructed — it takes the shared book of §18 to build one. That is
precisely why the probe ran before the place path was written.

### What it settles for §18.3 item 1

**The owner's `submit` must never send a `Cover` for more lots than the account
holds.** A crossing intent becomes a sized **pair**:

```
cover_qty = min(intent_qty, abs(account_net))      # the lots that close
new_qty   = intent_qty - cover_qty                 # the lots that open
```

...issued Cover-first. That is the *MUST SPLIT* shape, reached for a sharper
reason than "the venue refused": it does not refuse, so nothing except this
measurement would have told us.

### One thing still unresolved, and it is the reason for Phase B

Whether the excess lot is genuinely dropped at the venue, or whether the deal
quantity is right and `list_positions` under-reports, is **not** settled — the
two differ in *which side is lying*, and both are alarming. Two things reduce it:
the sized pair never creates an excess lot in the first place, and this was
`simulation=True`, where clearing is not real. It is **carried**, and it is why
`_stable_net` now samples the position repeatedly rather than once: a lag
settles, a clamp does not.

### The probe, extended · 2026-09-02

`--mode crossing` now runs **two phases**, because knowing the single call fails
is only half an answer:

* **Phase A** — the single call, as before, plus repeated position sampling and
  a fourth verdict, **`COVER CLAMPS AT FLAT`**, for exactly the case measured
  above. The old wording folded it into "lands wrong", which said nothing about
  the mechanism.
* **Phase B** — **the remedy**: flatten, re-open +1, then cross it with the
  sized pair (`Cover` 1, then `New` 1) and check the account lands on −1. It
  also answers what Phase A cannot: whether the `New` leg opens correctly
  immediately after a `Cover` leg with no settling gap between them.

Phase B is the one that matters. The owner's place path is going to issue that
pair; measuring it costs four orders in simulation, and *not* measuring it costs
a live desync. Phase B is skipped rather than guessed if the account cannot be
returned to flat first.

**Re-run for Phase B.** Same command; it now places up to six orders and
flattens between the phases.

---

## 18.9 X1 ANSWERED FOR REAL — §18.8 WAS WRONG · MEASURED 2026-09-07, DAY session

Third run, both phases, every order settled and every position sampled three
times over six seconds.

```
PHASE A
  A1  New   Buy  1  -> Filled, FDEAL Buy 1 @ 47408      position +1
  A2  Cover Sell 2  -> Filled, TWO deals: 1 @ 47403, 1 @ 47402 (9s apart)
                       position -1   (reads over 6s: -1, -1, -1)
  => ONE CALL, octype=Cover

PHASE B
  B1  New   Buy  1  -> Filled                            position +1
  B2  Cover Sell 1  -> Filled                            position +0  (+0, +0, +0)
  B3  New   Sell 1  -> Filled                            position -1  (-1, -1, -1)
  => SPLIT PAIR CONFIRMED
```

**§18.8's headline conclusion — "`Cover` clamps at flat" — is false.** A
quantity-2 `Cover` against a one-lot long crossed the account to −1, and the
reading held steady across six seconds of sampling. Both paths work.

### The correction, and it is mine twice over

§18.6 first read the one-lot gap as **`list_positions` lagging the deal stream**.
§18.8 then overturned that in favour of a **clamp**, because the clamp fitted two
runs. **The lag reading was right, and I should not have overturned it on a
hypothesis that no run had tested directly.** What run 3 adds is not a new
mechanism but the missing method: it is the first run that sampled the position
*repeatedly*, and the ambiguity §18.8 named as unresolved — *"whether the excess
lot is dropped at the venue, or the deal quantity is right and `list_positions`
under-reports"* — resolves in favour of the second. `_stable_net` was added to
tell a lag from a clamp; it did.

Runs 1 and 2 each read the position **once**, within a second or two of the
final deal, and each came back exactly one lot short of the arithmetic. Run 3
read three times over six seconds and agreed with the arithmetic every time.

**Still not measured: how long the lag actually is.** Three runs is a small
sample, runs 1–2 were NIGHT and run 3 was DAY, and nothing here timed the
interval between a deal and the position that reflects it. The lag is the best
explanation of all three runs; it is not a measured quantity. **Carried.**

### The finding that outranks X1

**`SafetyRails.on_reconcile` halts on ANY mismatch, immediately, with no
tolerance and no debounce** (`state.py:240`), and `broker_position()` polls
`list_positions` live on every bar (`shioaji.py:455`). If the venue's position
read trails its own deal stream, then a fill landing shortly before a bar
boundary produces a mirror that is correct, a venue read that is stale, and a
**spurious `STOP`** — on the bar right after an entry or exit, which is the worst
possible moment.

**Why this has most likely not bitten the NIGHT soak:** the loop reconciles once
per minute, so a lag of a few seconds is absorbed unless a fill lands in the last
moments before a bar. With a median 4.5-day hold there are very few fills for the
race to catch. That is luck of cadence, not a guard.

**This is a defect in the live path TODAY**, not something (b) introduces —
unlike §18.8's supposed clamp, which was never reachable. It is recorded here and
**not fixed**: a tolerance on a safety rail deserves its own decision (§16 says
the same thing about the ledger freshness race), and the honest first step is to
*measure* the lag rather than pick a window for it.

### What it settles for §18.3 item 1

Both forms work, so the choice is no longer forced. **Recommend the split pair
anyway:**

```
cover_qty = min(intent_qty, abs(account_net))   # the lots that close
new_qty   = intent_qty - cover_qty              # the lots that open
```

...Cover-first, which Phase B measured end to end with no settling gap between
the legs. Not because the single call fails — it did not — but because the split
never depends on the venue tolerating a `Cover` larger than the position held,
each leg's `octype` states what that leg actually does, and the whole question of
what an over-sized `Cover` means for margin and day-trade accounting never has to
be answered. The cost is one extra order in the rare crossing case. It is a
one-line change to reverse if the single call is ever preferred.

### One more thing run 3 shows

**A2's `Cover` Sell 2 filled as TWO deals of one lot each, nine seconds apart**
(47403, then 47402). Partial fills are real on this venue, `_settle` waited
correctly for both, and `poll_fills` returning a *list* rather than an
`Optional[FillEvent]` (`broker/base.py`) is doing exactly the job its docstring
claims. It also means the owner's N-order book must accumulate partials per
order, not treat the first deal as the fill.

### X1 CLOSED

The probe has answered what it was built for. `--mode crossing` stays as a
standing check for a new credential or a venue change, like `--mode accounts`.

---

## 18.10 X2 — the lag probe · BUILT 2026-09-07, waiting to be run

§18.9 named the lag as the best explanation of three runs and refused to pick a
rail tolerance without a number. `--mode lag` gets the number.

Per sample: place a marketable Buy 1 (or Sell 1), timestamp the **deal
callback** — the earliest instant a process can know a fill happened, and exactly
where `ShioajiBroker._match` moves the mirror — then poll `list_positions` every
0.25 s until it reports the new net. The gap between those two is the lag.

* **Alternates Buy 1 / Sell 1** (`--samples`, default 4), so the account returns
  to flat after every pair and the run ends flat. Quantity 1 throughout, so a
  sample is one deal rather than a partial-fill sequence whose "the position now
  reflects it" moment is ambiguous — §18.9 measured that partials are real here.
* **Reported values are CEILINGS at 0.25 s granularity.** The position may have
  settled any time since the previous poll. That is the right direction to err
  for a safety-rail tolerance. 0.25 s is 4 reads/s, under the 25-per-5-s
  accounting limit `broker/shioaji.py` documents.
* **It measures and stops.** It prints the exposed window and says outright that
  whether that becomes a tolerance, a re-read, or nothing is this plan's call.

```bash
uv run python -m forecast_eval.live.multi_deploy_probe --mode lag --i-understand-this-places-orders
```

**Run it inside a session** (DAY 08:45-13:45 / NIGHT 15:00-05:00, Taipei) — it
measures the gap between a fill and the position that reflects it, so with
nothing filling it measures nothing and exits 4. The probe classifies the wall
clock at start-up and warns.

### RESULT · MEASURED 2026-09-07, DAY session — the lag is ~0.1 s

```
sample 1  Buy  -> +1    place->deal  12.84s    deal->position  0.09s
sample 2  Sell -> +0    place->deal   0.14s    deal->position  0.12s
sample 3  Buy  -> +1    place->deal   6.48s    deal->position  0.09s
sample 4  Sell -> +0    place->deal   1.48s    deal->position  0.11s

samples 4   min 0.09s   median 0.10s   max 0.12s     (ceilings, polled at 0.25s)
```

**Decision: the reconciliation rail needs no tolerance, and gets none.** The loop
reconciles once per bar — 60 s — against a lag of 0.1 s. That is a margin of
~600×, and even an order of magnitude worse would be absorbed. The concern
§18.9 raised against `state.py:240` **does not arise**. Nothing is changed, and
that is now a measured decision rather than an assumption. It is scoped to what
was measured: quantity-1 fills, DAY session, four samples.

**Incidental, and worth knowing for other reasons:** `place -> deal` ranged
**0.14 s to 12.84 s** for identical marketable orders. Fill latency is wildly
variable where the position read is not. Nothing here depends on it — orders are
LMT+ROD and rest — but any future timeout sized from "a marketable order fills
promptly" would be wrong.

---

## 18.12 THE THIRD CORRECTION — the lag does NOT explain runs 1 and 2

**§18.9's central claim is now falsified by §18.10's measurement, and I made it.**
§18.9 said the one-lot gap in runs 1–2 was `list_positions` trailing the deal
stream. At 0.1 s it cannot be: both of those runs read the position **at least
half a second** after the deal (`_settle` polls at 0.5 s, then reads), and run 2
read it a second time, seconds later, during the flatten. Both reads said 0.

So the two Cover-2 orders genuinely differed:

| | run 2 · 2026-09-02 · NIGHT | run 3 · 2026-09-07 · DAY |
|---|---|---|
| order | `Cover` Sell 2 against **+1** | `Cover` Sell 2 against **+1** |
| deals reported | **one**, quantity **2** @ 45767 | **two**, quantity 1 each, 9 s apart |
| position after | **0** | **−1** |

**Same operation, different outcome.** Run 1 fits run 2's column exactly (its
Cover-2 reported one 2-lot deal and moved one lot; its New-2 reported two 1-lot
deals and moved two). Whether the difference is the session, the date, the
simulation venue's mood, or something about how the fill printed — **unknown, and
I am not going to guess it a fourth time.**

**What IS established, and it is enough:**

1. An over-sized `Cover` is **not reliable**. It crossed to −1 once and clamped
   at flat twice, with no measured difference in how it was issued.
2. When it clamped, **the deal still reported the full order quantity**. The
   account moved one lot; the message said two. `_match`'s existing overfill
   guard does not catch this — the order *was* for 2, so a 2-lot deal is in
   bounds (`shioaji.py:587`) — so `Trader.position` would move 2 against a book
   that moved 1. **A silent one-lot desync, from a message the venue sent.**

**The design does not change, and is now better motivated.** §18.9 already chose
the sized split pair. Under it an over-sized `Cover` is never constructed:
`cover_qty = min(intent, |held|)` can always fully execute, and the `New` leg
opens from a known base. Neither leg can over-report because neither leg can
partially fail. **The ambiguity above is unreachable rather than resolved** —
which is the right kind of answer for a venue behaviour nobody controls.

### The lesson, recorded because it happened three times

§18.6 said lag. §18.8 said clamp. §18.9 said lag again. §18.10 measured the lag
and it was 0.1 s, so the answer is neither: **the venue was inconsistent, and
every one of those sections was a mechanism invented to make one small sample
self-consistent.** Each was stated with more confidence than a 2-3 run sample
can carry. The measurement that settled it (§18.10) is the only one that timed
the thing it was arguing about.

**The rule this earns:** when two runs of the same operation disagree, the first
move is to measure the quantity in dispute — not to find a mechanism that fits
both. And when the venue is simply inconsistent, prefer the design that never
depends on which way it goes.

---

## 18.11 S9b BUILD — the transport · 2026-09-07

First piece of §18.3. Chosen first because it defines the protocol every other
piece serves, and because it is fully testable offline.

### What landed

| file | what |
|---|---|
| `live/intents.py` | `JsonlStream` — `append(kind, **fields) -> seq`, `read_new()`, `resume_seq()`, `age_seconds()`; `intent_path` / `event_path` keyed by `cache_slug()` |
| `tests/test_live_intents.py` | 20 tests, mostly about partial writes, corruption and restart |
| `docs/live/intents.md` | new page (G5) |

**One file per direction per deployment**, so each file has exactly one writer —
which is what makes an append safe to rely on and `seq` meaningful. Paths derive
from the slug rather than being passed in, the same rule `ledger_path` follows
and for the same reason (C5).

### Three rules, each of which is a way an order could go missing

1. **Only complete lines are records.** `read_new` advances its byte offset by
   the bytes consumed, stopping at the last newline; a trailing partial write
   stays unconsumed and is returned whole later. A reader can never see half an
   order. The offset is a byte position rather than a line count precisely so
   the partial can be re-read.
2. **A complete line that will not parse RAISES.** That is damage, not a partial
   write — the newline rule already covers partials. Skipping it would drop an
   order silently, the one failure this wire cannot have.
3. **`append` flushes.** The reader is another process; an intent in this
   process's buffer is one the owner cannot act on. §16's freshness caveat is the
   same problem one file over.

`age_seconds()` reads the record's own `ts`, not the file's mtime — mtime moves
for a touch or a copy, and the question is when the far end last *said* something.
`None` (never spoken) is deliberately distinct from a small age, and a
clock-skewed future timestamp floors to `0.0`.

### `live/broker/client.py` — the deployment's end · 2026-09-07

| file | what |
|---|---|
| `live/broker/client.py` | `ClientBroker(Broker)` — the six methods over the wire, plus `heartbeat()` |
| `tests/test_live_client_broker.py` | 25 tests |
| `docs/live/broker/client.md` | new page (G5) |

**It carries `intent`, never `octype`.** OPEN/CLOSE is what the deployment
*means*; what that becomes at the venue depends on an account net this end cannot
see (§18.9's `min(intent, |held|)` split). A test pins the absence of `octype`
so the responsibility cannot drift back down here.

**Order lifetime belongs to the owner.** `cancel()` writes the intent and leaves
`_pending` set — `base.Broker` is explicit that a live cancel is asynchronous, and
clearing it early would let §4.4's cancel-on-every-signal issue a second order
while the first is still working. **A fill does not end an order either**: §18.9
measured a 2-lot order filling as two 1-lot deals nine seconds apart, so fills
accumulate and only `done` clears the pending state.

**Silence is not consent**, in three parts:

* never having spoken is **not** flat — `age_seconds()` returns None for that
  case precisely so it cannot be read as fresh, and the rails halt;
* past `owner_timeout` (90 s) `broker_position()` returns None rather than the
  last value seen — comparing a stale venue read against a fresh mirror would
  halt on *our* staleness and call it a desync;
* `has_venue = True`, so that None means **refuse**, not skip (`broker/base.py`).

90 s is generous deliberately: a false "owner is gone" halts a healthy
deployment, and §18.10 measured a fill taking up to **12.8 s** to arrive.

**Corruption is translated once, at the boundary.** `JsonlStream.read_new` raises
on a complete-but-unparseable line so nothing is dropped; the bar loop needs a
halt rather than a traceback, so `_drain` catches it and routes it through
`_anomaly` — the same mechanism `ShioajiBroker` uses.

### `live/owner.py` — the N-order book · 2026-09-07 · **§18.3 items 1 and 2**

| file | what |
|---|---|
| `live/owner.py` | `plan_legs()` — the §18.9 sizing rule; `OrderBook` — N working legs, slug-tagged, with dedup, partial accounting and the running account net |
| `tests/test_live_owner_book.py` | 29 tests |
| `docs/live/owner.md` | new page (G5) |

**`ShioajiBroker` was NOT modified.** Two reasons, and the first is the one that
decided it: it is what the live soak runs on tonight. The second is that its
place path sets `octype` from the deployment's own OPEN/CLOSE intent, which
§18.9 measured is the owner's call — so the change would not have been a
generalisation, it would have been a different behaviour in the same file.

Worth recording, because §18.3 overstated the work: **`ShioajiBroker` was already
half N-order-ready.** `_retiring` is a list, `_match` already searches it, and
dedup is already on `(ordno, exchange_seq)`. What is genuinely single-order is
the `_live` slot and the sole-order fallback — and that fallback is exactly what
must NOT be carried over.

**The one rule that could not be inherited.** `ShioajiBroker._match` ends with
*"fall back to the sole live order when neither key is known"*, which is safe
with one order and would **attribute one deployment's fill to another** here. The
owner's `_match` has no such fallback: an unattributable deal is an anomaly. A
test pins that a lone working order is still not assumed.

**`account_net` is maintained from booked fills, not read from the venue.** It is
what `plan_legs` sizes the next Cover against, so it must be right the instant a
fill books — §18.10 measured `list_positions` catching up in ~0.1 s, fast but
still after a decision the owner may already be making. `seed_net()` sets it
without booking a fill, because a restart adopts from §16's ledger sum and
adoption is not a trade.

**A property test sweeps every net from −4 to +4** and asserts no `Cover` leg
ever exceeds what is held, which is §18.12's finding as an invariant rather than
a comment.

### `live/owner_process.py` — the venue-facing half · 2026-09-07

| file | what |
|---|---|
| `live/owner_process.py` | `OwnerProcess` — one callback, the place path, the intent loop, both reconciliations, deployment liveness |
| `live/owner.py` | `+ net_for(slug)`, `seed_slug_net()`, public `match()` |
| `tests/test_live_owner_process.py` | 27 tests, driven end to end through a real `ClientBroker` |
| `docs/live/owner_process.md` | new page (G5) |

**Driven through the real `ClientBroker`, not a fake on both sides.** The wire is
the part most likely to be subtly wrong, and two fakes agreeing with each other
would prove nothing.

**Two reconciliations, two operands — and they are different checks.** The owner
compares `account_net` against `list_positions` (a real venue desync); each
deployment compares its `Trader.position` against `net_for(slug)` (an event lost
on the wire). §16 had to approximate the first from three files on disk because
no deployment could make it; made here it is exact and in memory, **which retires
§16's freshness race**. On a mismatch the anomaly names the per-deployment split —
"mirror 2 != venue 3" without knowing who holds what is not actionable.

**`done` is once per intent, not once per leg.** A deployment submits one order
and must see one `done` even when §18.9's split made two legs, because
`ClientBroker` clears `_pending` on `done` and an early one would let §4.4 place a
replacement while the second leg is still working.

**Isolation is tested three ways**, since one deployment's problem must not
become another's: a corrupt intent stream is an anomaly for that slug while the
others are still drained in the same `poll()`; a place failure emits `rejected`
to the deployment that asked; a venue rejection routes to the owning leg's slug.

**Two ordering bugs, found while writing it and worth recording:**

1. The default `OrderBook` is handed `self._anomaly`, which reads
   `self.anomalies` and `self.recorder` — so the observers must be assigned
   **before** the book is constructed. Getting it wrong fails only on the first
   anomaly, which is exactly when it is least welcome.
2. `OrderBook._match` documents "caller holds the lock" because `on_deal` does.
   The order-event handler has no lock of its own, so a public lock-taking
   `match()` was added rather than reaching for the private form across modules.

**One test was rewritten because it did not test its own name.**
`test_deal_is_dispatched_before_order` originally passed a state that matched
neither branch, so it proved nothing about the ordering. It now uses a state with
no `name`, so dispatch falls back to `str(state)` — `"OrderState.FuturesDeal"`,
which contains ORDER too — and the ambiguity the ordering exists for is real.

### §18.3 items 7 and 8 · 2026-09-07

**Item 7 needed no code change, and that is a claim worth testing rather than
asserting.** §7.1 said `adopt_position` *"adopts the COMBINED venue position as
its own"* — true of `ShioajiBroker`, whose `broker_position_detail()` sums the
account. Through a `ClientBroker` it is already correct, because the owner sends
each deployment its **own** signed size. `cli.py:771`'s `--fresh-start` gate calls
the same method, so it inherits the same fix. `runtime.py` and `cli.py` are
untouched.

`tests/test_live_owner_adoption.py` (7 tests) drives the real `OwnerProcess` and
the real `ClientBroker` through the cases §7.1 named: a restart adopting +1 while
the account holds +2; a flat deployment adopting nothing while a peer holds 2;
day +1 and night −1 both adopting correctly while the **account reads flat**; and
a silent owner refusing rather than reading as flat.

**Writing those tests found a real gap.** `broker_position_detail()` returns None
without a price, so `adopt_position` would have refused every restart: **the owner
was never sending an entry price.** The venue reports an average for the ACCOUNT,
which is the wrong number on a shared book, so the owner now keeps its own per
deployment — `OrderBook.entry_for(slug)`, published with every `position` event.

The entry arithmetic has four cases and only one is an average (8 tests):

| case | entry becomes | why |
|---|---|---|
| opened from flat | this fill's price | there is nothing to average with |
| **crossed through flat** | this fill's price | carrying a long's average onto a short prices it at what the long cost |
| added to the same side | size-weighted | the ordinary case |
| reduced | **unchanged** | the remaining lots cost what they cost; re-averaging on a partial close silently rewrites the PnL of lots it did not touch |

**Item 8: `flatten.py` stays account-wide, and now says so.** It reads
`list_positions` — the sum of every deployment's position on a shared book — so
closing it closes other people's lots and their mirrors will not know. That is
still right: it exists for when nothing else can be trusted to close a position,
and a flatten that could only close a third of the account would be useless in
the one situation it is for. What it must not do is stay quiet, so with
`--out-parent` it names whose lots are about to close and says to restart those
deployments with `--fresh-start`. **Silence when only one deployment holds is
deliberate** — a warning with nothing behind it trains an operator to ignore
warnings.

`ledger.per_deployment_nets()` is the reader behind it. It and
`account_net_position` degrade in **opposite** directions on purpose: the sum
returns `None` for the whole set on one bad file, because it feeds a safety rail
where a partial sum is a confident wrong number; the split skips that file and
keeps the rest, because it feeds a warning where naming two of three beats naming
none.

### The entry point and the CLI wiring · 2026-09-07 — **S9b IS CODE-COMPLETE**

| file | what |
|---|---|
| `live/owner_cli.py` | the owner entry point — login, ledger seed, start-up reconcile, the `poll`/`reconcile` serve loop |
| `live/cli.py` | `--broker client`; `ORDER_PLACING_BROKERS` replaces four `== "shioaji"` gates; a banner that names the owner |
| `tests/test_live_owner_cli.py` | 11 tests |
| `docs/live/owner_cli.md`, `docs/live/cli.md` | new page and the updated flag table |

```bash
uv run python -m forecast_eval.live.owner_cli --out outputs/live_soak     --i-understand-this-places-orders          # FIRST
uv run python -m forecast_eval.live.cli --config day --broker client     --out outputs/live_soak/day --resume --i-understand-this-places-orders
```

**It refuses to start when the ledgers and the venue disagree.** The owner's
whole authority rests on `account_net`, because that is what `plan_legs` sizes
every `Cover` against (§18.9). Serving on a seed the venue disputes turns one
stale ledger into a stream of wrongly-sized legs. A venue that will not *answer*
is a warning, not a refusal — "cannot compare" is not "disagrees".

**A slug with no ledger seeds flat**, not unknown: a deployment that has never
traded is the normal first-run case, and refusing it would block a start-up that
is completely correct.

**Four `== "shioaji"` gates were wrong the moment `client` existed** — the
preflight, the soak-epoch resolution, the `--fresh-start` flatness check and the
banner. A mode that sends orders through one more hop is still a mode that sends
orders. They now read `ORDER_PLACING_BROKERS`, which is the kind of thing this
plan has already been caught by once (§`_resolves_soak_epoch`'s own comment
records two conditions drifting apart).

**The `--fresh-start` gate means something better under a client.** It calls
`broker_position_detail()`, which is now *this deployment's* size — so a flat
deployment can start fresh while a peer holds, which under `shioaji` would have
been refused.

### One residual risk — the others were measured or closed (§§18.13, 18.14)

**The owner does not redact.** It prints the account's `person_id` — a Taiwanese
national ID — from Shioaji's compiled layer, exactly as `cli.py` does.
`multi_deploy_probe.py` solves this by re-running itself as a filtered child;
neither entry point does. Read before pasting.

---

## 18.13 X3 — FOUR SESSIONS COEXIST · MEASURED 2026-09-07

```
session 1 (day)      connections=1
session 2 (fullday)  connections=2
session 3 (night)    connections=3
session 4 (day)      connections=4
first session still usable   True
```

**Option (b)'s shape fits.** The owner plus one session per deployment is four
logins on one credential, §14b Q2 had measured two, and this settles it —
including the half that matters most: the **first** session still answers with
all four open. Q2's real finding was never that a second login succeeds but that
the first survives it, and a login that quietly evicts an earlier session looks
identical to one that coexists right up to the moment the owner stops hearing
deals.

**Three were already proven, accidentally.** `--mode accounts` holds every
session until its `finally`, so the 2026-09-02 Q0 run had three open at once. The
evidence was real and never reported — it printed no `connections`. G6's three
sessions were therefore never at risk; only the fourth was new ground.

### The quota, and a correction

```
bytes used today   833,006        (four logins, no market)
limit_bytes        524,288,000    = exactly 500 MiB/day
remaining_bytes    523,454,994
```

**I claimed nothing in this repo read `remaining_bytes`. That was wrong**, and it
was a truncated grep rather than a missing feature. `RestartGuard.check_usage`
(`startup.py:233`) reads it and **refuses to start** below a 50 MB floor, with a
sharper reason than I had: over quota Shioaji returns **EMPTY** history rather
than erroring, so a backfill would silently produce an empty model context.
`cli.py:711` calls it on every launch.

**What is genuinely open is narrower:** that guard is a *start-up* check and
cannot see a quota consumed *during* a run — which is exactly where four
subscriptions to one tick stream would bite.

**Sized from the tapes** (2026 night sessions, `TXF_RPT_minute_night.csv`):
median **109 326** contracts traded per session, p90 183 k, max 298 k. Prints are
fewer than contracts and bytes-per-tick is unmeasured, so the honest band is
~30–60 MB per session per subscription — **120–240 MB/day for four** against 500
MiB. It fits, but the 50 MB floor sits inside that uncertainty on a max-volume
day, and a run refused at the *next* start-up because the previous four-session
day ate the allowance is a plausible failure.

### What landed instead of a guess

| file | what |
|---|---|
| `live/startup.py` | `usage_bytes(usage) -> (used, limit, remaining)` — the duck-typed read, in one place |
| `live/cli.py` | reads the meter at start-up and teardown, prints what the run cost and what four sessions would cost, records both |
| `live/owner_cli.py` | samples the headroom on the reconcile cadence and warns below `--min-remaining-mb` |
| tests | 8 new — the reader, the warning, and that a failed `usage()` never takes the owner down |

**The floor is configurable but its default is unchanged.** Raising a safety
threshold on an estimate is the thing this plan has now been wrong about three
times; `--min-remaining-mb` makes it reachable without a code edit, and the real
number arrives after one night's soak.

**The owner warns rather than stops.** The owner stopping halts every deployment
at once, and a thin data allowance is not a reason to abandon open positions. The
refusal stays where it already is — at the next start-up, where `RestartGuard`
has always had it.

### §15 STILL STANDS

**One instance at a time, until G6 passes.** (b) now exists in code and every
piece of it is tested offline, but *offline* is the operative word: no two
deployments have yet held a position simultaneously against a real session. The
rule lifts on evidence, not on a build.

### Next — G6, and it is the only thing left

> two deployments hold non-flat positions across a full session; neither rail
> halts; the venue's reported position equals the **sum of the deployments' own
> fill ledgers**; and each deployment's PnL is still attributed to it alone.

Three of those four are already covered offline by
`tests/test_live_owner_process.py` and `tests/test_live_owner_adoption.py`. The
**non-halting half needs the venue** — an owner and two deployments, one
session, simulation, watching for a halt that should not come.

---

## 18.14 The owner's two start-up asymmetries, closed · 2026-09-07

Both were noticed while writing §18.13's summary and neither was a probe — the
first is already proven by the soak, and the second was a missing check rather
than an unknown fact.

**1. `subscribe_trade` is now explicit and checked.** The owner relied on
`login()`'s `subscribe_trade=True` default. That works — it is what `cli.py` does
live — but this is the **one** process that must hear deals: under (b) it is the
only `_match` there is, so a silent unsubscribe means every deployment's fills
stop arriving while their orders keep filling. §14b Q3 measured the call returns
`True`, so a `False` is a real answer and the owner now **refuses to start** on
it (exit 7). Called after login rather than instead of it, because re-subscribing
is harmless and a changed default in a future SDK release would otherwise fail
silently.

**2. The account fingerprint reaches the owner.** `cli.py` has had it since V4;
the owner did not, while being the process whose account **is** the account all
three deployments trade. Warns rather than refuses, exactly as `cli.py` does.

`account_fingerprint` / `check_account_fingerprint` moved out of `cli.py` into
**`live/fingerprint.py`** to make that possible — importing `cli.py` would have
dragged the forecaster, torch and the loop into a process that needs none of
them. The definitions moved unchanged and `cli.py` imports them, so its call
sites and its 11 tests are untouched.

### The bug this turned up

`_futures_account` reached the enum at `sj.constant.AccountType`, **which does
not exist in 1.7.2** and raises on attribute access. Its own test caught it
immediately. That is the same class of mistake §13 records — the first probe
reporting "0 futures accounts" for a credential trading TXF nightly, because
`str(AccountType.Future)` is `'F'` — one level up: not the wrong comparison, the
wrong path to the thing being compared. It now resolves `futopt_account` first,
falls back to the `login()` list matched on the enum member, and keeps a
`.name == "Future"` spelling as a third route, so the enum moving again does not
silently return None.

**Tally: 1191 tests. S9b remains code-complete; only G6 is left.**

---

## 19. G6 — BUILT · 2026-09-07. The offline half PASSES

> two deployments hold non-flat positions across a full session; neither rail
> halts; the venue's reported position equals the **sum of the deployments' own
> fill ledgers**; and each deployment's PnL is still attributed to it alone.

### 19.1 The offline half — `tests/test_g6_two_deployments.py` · **9 tests, green**

Everything real except the venue: two `LiveLoop`s, two `ClientBroker`s, one
`OwnerProcess`, one `OrderBook`, and the real `FillLedger` writer read back by
the real `account_net_position`. A reproduction of the wiring would prove
nothing — the wiring is what is on trial.

The venue fake is new and deliberately not the one the other suites use: those
inject deals by hand, which is right for testing attribution and wrong here. G6
is about the loop **closing** — intent → order → fill → event → mirror → ledger
→ venue — so this venue moves its own book and answers `list_positions` from it.

| clause | how it is shown |
|---|---|
| both hold, neither halts | §14's exact pairing (BUY from one, SELL from the other) run over 120 bars; both rails clean |
| venue == sum of the ledgers | same-direction case, so the sum is **2** and the check is not `0 == 0` |
| PnL attributed alone | the account is FLAT while day marks **+30** and night **−30** |
| a restart adopts its own lot | a fresh `ClientBroker` on an account holding 2 reads **1** |

Two more that are not clauses but are where this would break in practice: a
crossing intent while a peer holds (no `Cover` leg ever exceeds what the account
held), and no owner anomalies across the long run.

**The PnL test is the one worth reading.** The account is flat, so any
account-level PnL is zero — and the two deployments' PnL is ±30. That is the
whole argument for attributing per deployment rather than reading the venue,
stated as an assertion.

**One test was wrong and the code was right.** The first crossing test asserted
a deployment's mirror one bar too early: a `LiveLoop` drains fills at the START
of a bar, so a fill placed during bar 0 reaches the mirror on bar 1. Asserting
after a single bar tested the venue, not the deployment.

### 19.2 The live half — `tools/g6_live.py`, waiting to be run

What the offline half cannot show is that nothing halts against **real latency,
real partial fills and a real `list_positions`**. So this does not re-check the
arithmetic. It opens the §14 scenario for real — two deployments, one lot each,
opposite directions, so the account nets flat and only the per-deployment books
know anything is open — and watches, reporting every 30 s and failing fast on a
halt or a reconcile disagreement.

```bash
uv run python -m forecast_eval.tools.g6_live --out outputs/g6     --i-understand-this-places-orders --minutes 10
```

**Three refusals before it opens anything:** no acknowledgement (5); an `--out`
that already holds `fills_*.csv` (2) — a stale ledger would be counted into the
sum while matching no position the venue still holds, failing G6 for a reason
unrelated to G6; and an account that is not flat (3).

**Inconclusive is its own exit code (4), not a pass.** §14 records what a run
that measured nothing and reported a conclusion costs, and a G6 where neither
deployment filled has observed exactly nothing about concurrent holding.

**It flattens through the owner, not `flatten.py`** — which closes the account's
whole net and cannot update any deployment's ledger (§18.3 item 8). Those ledgers
are the thing G6 just finished checking.

**Run it inside a session** (DAY 08:45-13:45 / NIGHT 15:00-05:00, Taipei). It
needs fills.

### 19.3 What a pass lifts, and what it does not

**§15 is suspended for the duration of that run and by that run only.** A pass
lifts §15 for **the shape it tested**: two opposed one-lot positions. Three
deployments, or larger sizes, are a bigger claim than one run makes — and
§18.13 measured that four sessions coexist but never measured what four
subscriptions cost against the 500 MiB allowance (§18.13, still open).

**Everything before G6 is done.** S1-S6 built and green; S7 closed; S8 decided;
S9a (§16) and S9b (§18) code-complete; **1200 tests**.

---

# 20. NEXT ACTION — run G6 live. **START HERE.**

*Written 2026-09-07 as a handoff. Everything below is what a fresh session needs
to pick this up without re-deriving it.*

## 20.1 The one command

```bash
uv run python -m forecast_eval.tools.g6_live --out outputs/g6     --i-understand-this-places-orders --minutes 10
```

**The user runs it, not the assistant** — Shioaji credentials are not reachable
from the assistant's shell. Hand over the command and read the output.

**Three preconditions**, each of which the script checks and refuses on:

| | why |
|---|---|
| a **session must be open** — DAY 08:45–13:45 or NIGHT 15:00–05:00, Taipei | it needs FILLS; off-hours it measures nothing and exits 4 |
| the run must **fit before the session close**, with margin | it FLATTENS at the end, and a close needs a market too. Reaching the boundary while holding leaves two opposed lots overnight |
| `--out` must be **fresh** (no `fills_*.csv`) and the account **flat** | a stale ledger is counted into the sum while matching no position the venue holds, failing G6 for a reason unrelated to G6 |

NIGHT is the better window — 14 hours against DAY's five. The slugs `day` and
`night` are deployment **identities**, not schedules; running both during the
night session is correct.

## 20.2 What it is testing, and what it is not

G6, from §8:

> two deployments hold non-flat positions across a full session; neither rail
> halts; the venue's reported position equals the **sum of the deployments' own
> fill ledgers**; and each deployment's PnL is still attributed to it alone.

**Three of those four clauses are already PROVEN offline** —
`tests/test_g6_two_deployments.py`, 9 tests, green (§19.1). Do not re-argue the
arithmetic; it is settled deterministically against two real `LiveLoop`s.

**This run tests exactly one thing: that nothing halts against real latency,
real partial fills and a real `list_positions`.** It opens the §14 scenario —
two deployments, one lot each, **opposite** directions, so the account nets flat
and only the per-deployment books know anything is open — and watches.

## 20.3 How to read the result

| exit | meaning | what to do |
|---|---|---|
| **0** | PASSED | record it in a new §21; §15 lifts for this shape (see 20.5) |
| **1** | FAILED — a rail halted, or reconcile disagreed | the output names which. This is a real finding: record it before changing anything |
| **2** | `--out` was not fresh | use a new directory |
| **3** | the account was not flat at start | `python -m forecast_eval.live.flatten --out-parent <dir>` |
| **4** | INCONCLUSIVE — nothing filled within 90 s | **not a pass.** Was a session open? Is `--offset-points` enough? |
| **5** | no acknowledgement flag | pass it |

**Exit 4 is the one to be careful about.** §14 records what a run that measured
nothing and reported a conclusion costs; this plan has made that mistake, and
§§18.6/18.8/18.9 record it happening three more times with invented mechanisms.
A G6 where neither deployment filled has observed **nothing** about concurrent
holding.

**If it leaves the account holding**, it says so loudly. Flatten by hand:
`python -m forecast_eval.live.flatten --out-parent outputs/g6`.

## 20.4 Traps a fresh session would otherwise hit

1. **Do not re-derive the mechanism from a small sample.** §18.6 said lag,
   §18.8 said clamp, §18.9 said lag again — all three were wrong, and §18.10
   settled it by *measuring the quantity in dispute* (0.1 s). When two runs of
   one operation disagree, measure; do not find a story that fits both.
2. **Read the deals, not the verdict line.** §18.6's `MUST SPLIT` was produced by
   the probe's own defect and is contradicted by the deals in its own output.
3. **`--broker client` deployments still log in** — for the market feed. The
   owner plus three deployments is four sessions; MEASURED (§18.13) to coexist.
4. **The traffic figure is still unknown.** `cli.py` now prints `traffic this
   run` at teardown; the allowance is 500 MiB/day and four subscriptions
   multiply one session's cost. That number arrives from the next ordinary soak,
   not from G6.
5. **The owner does not redact** the account's `person_id`. Warn before pasting
   its console output (same as `cli.py`; only `multi_deploy_probe.py` filters).

## 20.5 What a pass lifts — and what it does not

**§15 (one instance at a time) lifts only for the shape G6 tested:** two opposed
one-lot positions, for `--minutes`. Three deployments, larger sizes, or a full
session are each a bigger claim than one run makes. Say so explicitly when
recording the result.

It does **not** close: the traffic question (§18.13), whether four subscriptions
fit the allowance, or §7.4's exposure decision — which was answered as "the
sum, bounded at ±3, and accepted" (§18.4 item 3) rather than measured.

## 20.6 Where things stand

**S1–S6** built, G1–G5 green. **S7** closed. **S8** decided — option (b).
**S9a** (§16) and **S9b** (§18) code-complete. **G6 offline PASSES** (§19.1);
**G6 live is the only thing left** (§19.2). **1200 tests, 2 skipped.**

**Nothing is committed.** The working tree holds ~20 modified and ~18 new files
from S9b — `live/{intents,owner,owner_process,owner_cli}.py`,
`live/broker/client.py`, `tools/g6_live.py`, six test files and six docs pages,
plus edits to `live/{cli,ledger,flatten,startup,multi_deploy_probe}.py`.

**If you read only three sections:** this one, §18 (the design and every
measurement behind it), and §19 (G6). §§7 and 11 are historical and several of
their predictions were measured false.
---

# 21. G6 LIVE — FIRST RUN, INCONCLUSIVE · 2026-09-07, NIGHT session

**Exit 4. Not a pass, not a failure — nothing filled, so nothing was observed
about concurrent holding.** §15 stands unchanged.

```
local time 2026-09-07 15:03:33  session=NIGHT
contract TXFR1 -> code=TXFR1 delivery_month=202609
last 47496
  BUY  1 @ 47996 submitted as an intent
  SELL 1 @ 46996 submitted as an intent
  => INCONCLUSIVE: both deployments must hold. day=+0 night=+0
--- flattening ---   flat.
```

## 21.1 What the run's own files rule out

`outputs/g6/` holds four files and no `fills_*.csv`. Both intents were written
correctly and both are marketable by 500 points **in the filling direction** —
BUY 500 above a 47496 market, SELL 500 below it:

```
intents_day.jsonl    {"kind":"submit","side":"BUY", "limit_price":47996.0,...}
intents_night.jsonl  {"kind":"submit","side":"SELL","limit_price":46996.0,...}
events_{day,night}   92 records each, ALL kind="position", signed_qty 0
```

**§20.3's two suggested causes are both eliminated.** A session *was* open — the
script classified NIGHT and `snapshots()` returned a live `last`, so ticks were
flowing. And `--offset-points 500` is not too small; it is ~1.05 % through the
market on both sides.

**The wire is not the problem either — reproduced offline.** Driving the exact
`ClientBroker.submit` → `OwnerProcess.poll()` sequence against a recording fake
produces two `place_order` calls, `octype=New` both, correct sides and prices,
zero anomalies. The intents do become venue orders.

## 21.2 Why the run could not say more — a defect in the harness, not the design

**Three different failures were indistinguishable at exit 4**, and the run
recorded which one it hit for none of them:

| silent path | why it left no trace |
|---|---|
| the intent never became an order | `_submit` returns early via `_anomaly` on a malformed intent or an empty plan |
| `place_order` raised | `_place` records an anomaly and emits `rejected` |
| the order rests unfilled | `_place` emits **nothing at all** on success — a placed order and a never-placed one look identical |

The first two land in `owner.anomalies`, and `g6_live` built its `OwnerProcess`
with **no `on_anomaly` and no `recorder`** — so those messages went into a list
the exit-4 branch then returned without printing. The third needs the venue's
own view, which the run never asked for.

*(The `rejected` case is in fact excluded after the fact: no `rejected` record
appears in either events file. That leaves paths 1 and 3, and the offline
reproduction makes 1 unlikely — but "unlikely" is not "measured".)*

## 21.3 What landed — instrumentation only, no behaviour change

`tools/g6_live.py`: anomalies now print as the owner records them, and a new
`_diagnose()` runs on the exit-4 path, printing the anomalies, the legs the
owner believes are working (with `order_id`/`ordno`/`filled`), and the venue's
answer to `update_status` + `list_trades` — status and `deal_quantity` per
order. It degrades to a warning when the venue will not answer; both branches
are smoke-tested offline. **1200 tests still pass.** Nothing in `live/` changed.

## 21.4 The leading hypothesis — NAMED, NOT CONCLUDED

**`OwnerProcess` learns fills only from `set_order_callback`.** Every probe that
has actually observed a fill — §18.9 run 3, §18.10's 0.1 s lag measurement —
also called `api.update_status()`, and both were run in the **DAY** session;
this was the first order the owner has placed in a NIGHT session. Whether the
push callback alone is sufficient has therefore never been tested.

**This is a hypothesis with a cheap decisive test, not a finding.** §20.4 trap 1
exists because this plan has three times reasoned out a mechanism that fit the
evidence and was wrong (§§18.6, 18.8, 18.9). `_diagnose` settles it in one
line: an order the venue reports **Filled** while the owner shows `filled=0`
means the callback is the gap; an order the venue reports **PendingSubmit**
means it never matched and the callback is fine.

**Do not add `update_status` polling to `OwnerProcess` before that run.** It is
a change to the live trading path made on a guess, and the measurement costs
one 10-minute window.

## 21.5 Next

Re-run the same command in a session, read the `--- why nothing filled ---`
block, and record what the venue said. **Use a fresh `--out`** — `outputs/g6`
now holds intent and event files from this run.

```bash
uv run python -m forecast_eval.tools.g6_live --out outputs/g6_2 \
    --i-understand-this-places-orders --minutes 10
```

---

# 22. THE OWNER DROPPED EVERY FILL · MEASURED 2026-09-07, NIGHT session

**Run 2 was INCONCLUSIVE again — and this time it said why.** The instrumented
exit-4 path (§21.3) answered in one block, and the answer was neither of the
two things §21 expected.

**Both orders FILLED at the venue.** `update_status` + `list_trades`:

```
ordno='046179' Buy  47960 status=Filled deals=1     <- the day deployment
ordno='04617A' Sell 46960 status=Filled deals=1     <- the night deployment
  legs the owner thinks work 2
     day   BUY  New qty=1 filled=0 order_id='101B05' ordno='046179'
     night SELL New qty=1 filled=0 order_id='101B06' ordno='04617A'
```

**The callback fired too** — all four events arrived (two `FORDER`, then two
`FDEAL` at +15.6 s, prices 47464 and 47462). The owner rejected every one:

```
ANOMALY: order callback payload was not a dict: (<OrderState.FuturesDeal: 'FDEAL'>, {...})
```

## 22.1 §21.4 WAS WRONG — the fourth wrong mechanism

§21.4 named the push callback as the leading hypothesis: that `update_status`
was needed because every probe that had seen a fill called it. **Measured false.**
The push callback delivers `FORDER` and `FDEAL` promptly in a NIGHT session; the
DAY/NIGHT difference was a coincidence of which probes had been run, and the
`update_status` correlation was a property of the probes, not of the venue.

It was recorded as a hypothesis with a named decisive test rather than acted on,
and the test cost one window and settled it — which is the only reason this is a
correction to a plan section and not a change to the live trading path. **The
rule in §20.4 trap 1 has now paid for itself four times.**

## 22.2 The defect — one line, and it was never in the proven path

`OwnerProcess._on_event` guarded with `isinstance(payload, dict)`. The compiled
layer hands out a mapping that **renders as a dict but is not a `dict`
instance**, so the guard rejected every event and the owner booked no fill for
two orders the venue had filled.

**`ShioajiBroker._on_order_state` has never had that guard.** It passes the
payload straight to handlers that only call `.get()` — which is exactly why the
V4 soak saw its fills and the owner did not. The guard was added at §18's build,
to a shape nobody had yet received.

Measured, not inferred: `_on_event(state, payload)` with a real `dict` raises no
anomaly; `_on_event((state, payload))` produces a **double**-paren repr, and the
live output has a single one. Only "2-tuple whose payload is not a `dict`"
reproduces what the venue actually sent.

**The fix is parity, not invention:** duck-type on `.get`, and name
`type(payload).__name__` in the anomaly so the next surprise from the compiled
layer is diagnosable without another live run.

## 22.3 The finding that outranks the bug

**Both of the owner's rails were blind to a total attribution failure, and the
opposed pair G6 opens is precisely the shape that blinds them.**

* `reconcile()` compares the account **sum** against the venue. Both legs
  filled: venue net `+1 −1 = 0`. Book net: `0`, because nothing was booked.
  **It agrees.**
* `_flatten` read `owner.book.net_for(slug)`, found 0, and printed `flat.`
  **without ever asking the venue.**

The account genuinely was flat — but only because the legs were opposed. **The
same code on a same-direction pair would have printed `flat.` over a real
overnight position.** Run 1 (§21) did the same thing: its orders `0460A5`/
`0460A6` also filled, also went unbooked, and it also reported flat.

`_flatten` now asks `list_positions` before it is allowed to say `flat.`, via a
`_venue_net()` that deliberately does **not** go through `reconcile` — the book
is the thing under test and cannot also be the witness. A venue that will not
answer degrades to "verify by hand", never to a confident flat.

## 22.4 What landed

| file | change |
|---|---|
| `live/owner_process.py` | `_on_event` duck-types on `.get`; the anomaly names the payload type |
| `tools/g6_live.py` | `_venue_net()`; `_flatten` confirms against the venue before saying flat |
| `tests/test_live_owner_process.py` | a dict-like non-dict now books the deal end to end; the unreadable-payload test asserts the new wording and the named type |

**1201 tests, 2 skipped.** The old test passed on the new message by accident —
it matched `"not a dict"` inside the payload's own repr — and now asserts the
wording deliberately.

## 22.5 Next — G6 has still never been observed

Two runs, four fills, and **zero** measurements of two deployments holding at
once. Re-run on a fresh `--out`:

```bash
uv run python -m forecast_eval.tools.g6_live --out outputs/g6_3 \
    --i-understand-this-places-orders --minutes 10
```

**Check the account is actually flat first** — runs 1 and 2 each left it flat
only by the arithmetic of opposed legs, and neither wrote a `fills_*.csv`, so no
ledger records the four fills that happened:

```bash
uv run python -m forecast_eval.live.flatten --out-parent outputs/g6_3
```

## 22.6 The flatten could not log in · FOUND 2026-09-07

`§22.5`'s first command failed immediately:

```
ERROR: set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY.
```

**`live/flatten.py` read `os.environ` directly** — the only live entry point
that never moved to `live/credentials.py` and `config/secrets.toml`. Every other
one (`cli.py`, `g6_live.py`, the owner) resolves through `credentials.load`,
which reads the secrets file **and** falls back to the same env pair.

**The tool this bit is the emergency one.** It exists for the moment nothing
else can be trusted to close a position, and it was the single tool that could
not start on a machine configured the way the rest of the repo expects. Worse,
it fails at exactly the moment it is reached — after something has already gone
wrong.

`--credential` now picks the section (default `night`; all three name the same
account per §13), the env pair still works, and the orphaned `os` import is
gone. Three tests: the flag selects the section, a missing credential is a clean
exit 2 rather than a crash, and the default is `night`. **1204 tests.**

**Seven other files still read the environment directly** — `ioc_probe.py`,
`prod_audit.py`, `relogin_probe.py`, `v1_check.py`, `v1c_check.py`,
`v4_probe.py`, `v72_probe.py`. All but `prod_audit.py` are historical one-off
probes from earlier phases; they are **left alone deliberately** rather than
swept up with this fix. `prod_audit.py` is the one worth converting next.

---

# 22.7 THE FALSE PASS · 2026-09-07, third run

**The callback fix worked.** Both deployments filled, both held opposed lots for
ten minutes, `reconcile=True` on all twenty samples, zero anomalies:

```
  both holding: {'day': 1, 'night': -1}  account +0
  [16:10:11] ... reconcile=True     (x20, through 16:19:44)
  owner anomalies            0
```

**And the verdict was wrong.** It printed `=> G6 PASSED`, claiming *"every fill
reached exactly one deployment"* — three lines under its own output saying:

```
  ledgers on disk            {}
  ledger sum                 None
```

## 22.7.1 G6 has still not passed

G6's central clause is *"the venue's reported position equals the **sum of the
deployments' own fill ledgers**"*. **No ledger was written, so that clause was
never checked**, and `_verdict` never looked at the two numbers it had just
printed — it tested `failures` and the owner's book, then asserted the rest.

**§15 does NOT lift.** What the run *does* establish is real and worth keeping:
the owner books fills correctly (§22's fix), two deployments held opposed
positions for ten minutes, and **neither rail halted** — which is the §14
scenario that halted both deployments before option (b). That was the hard part.
The clause that failed to be checked is the bookkeeping one.

## 22.7.2 Why the ledgers were empty — structural, not a slip

`ClientBroker` does not touch a ledger. In a real deployment `cli.py` builds the
`FillLedger` and `LiveLoop` feeds it — and `g6_live` runs **neither**. It is the
only writer of `fills_<slug>.csv` in the whole live path, and G6's live half had
no access to one. So the live half could never have checked its own central
clause, in any run, however well everything else went.

The offline half (§19.1) does not share the defect: it drives two real
`LiveLoop`s, so the real `FillLedger` writes and `account_net_position` reads it
back. That is why the clause looked covered.

## 22.7.3 What landed

| file | change |
|---|---|
| `tools/g6_live.py` | one `FillLedger` per deployment, fed by a single `_drain()` used by both the wait and the watch |
| `tools/g6_live.py` | `_verdict` refuses to pass unless the ledgers exist, match the owner's book per deployment, **and** sum to what the venue reports; "cannot ask the venue" is inconclusive, never agreement |
| `tools/g6_live.py` | the closing wait was **10 s** — shorter than the **12.8 s** §18.10 measured for one fill, which is why it announced `STILL HOLDING` over closes still in flight. Now 40 s |
| `tests/test_g6_two_deployments.py` | 5 tests on the verdict: no ledgers → 4 (the exact 2026-09-07 case), ledgers vs book mismatch → 1, venue vs sum mismatch → 1, venue silent → 4, all agreeing → 0 |

**1209 tests, 2 skipped.**

## 22.7.4 The pattern, for the fourth time

§20.4 trap 2 says *read the deals, not the verdict line* — written after §18.6's
`MUST SPLIT` was contradicted by the deals in its own output. This is the same
failure in the same file: **a verdict that asserts more than it measured, with
the contradicting evidence printed directly above it.** The guard added here is
narrow (G6's own clauses); the general lesson is that every `PASSED` in this repo
should name the numbers it checked and refuse when it has none.

## 22.7.5 Next

Re-run on a fresh `--out`. This is now the first run that can actually pass or
fail on the ledger clause rather than skip it.

```bash
uv run python -m forecast_eval.live.flatten --out-parent outputs/g6_3
uv run python -m forecast_eval.tools.g6_live --out outputs/g6_4 \
    --i-understand-this-places-orders --minutes 10
```

---

# 23. G6 LIVE — PASSED · MEASURED 2026-09-07, NIGHT session, run 4

**The first G6 verdict in which every clause was actually checked.**

```
  both holding: {'day': 1, 'night': -1}  account +0
  [16:28:50 .. 16:38:23]  reconcile=True  x20
  per-deployment positions   {'day': 1, 'night': -1}
  ledgers on disk            {'day': 1, 'night': -1}
  ledger sum                 0
  owner anomalies            0
  => G6 PASSED (live half)
```

Two deployments held opposed one-lot positions for ten minutes against a real
session. **Neither rail halted** — this is the §14 scenario that was measured as
a guaranteed double halt before option (b). The ledgers on disk matched the
owner's book per deployment, and the venue agreed with their sum.

## 23.1 What this lifts — and the part that is weaker than it looks

**§15 lifts for the shape G6 tested: two opposed one-lot positions, for ten
minutes.** Three deployments, larger sizes, or a full session are each a bigger
claim than this run makes. The traffic question (§18.13) is untouched, and
§7.4's exposure decision remains a decision rather than a measurement.

**Stated plainly, because this plan's habit is to overclaim: the venue-vs-sum
comparison was `0 == 0`.** The two deployments held opposed lots, so the account
nets flat and the ledgers sum to zero — the check passed, but it could not have
distinguished a correct sum from two errors that cancel. §19.1 chose a
**same-direction** pair offline for exactly this reason, and the live half did
not. What *was* checked non-trivially is the per-deployment half: `{'day': 1,
'night': -1}` on disk against the same in the owner's book, which no cancelling
pair of errors produces.

**So the strongest remaining G6 evidence is still offline.** A same-direction
live run (both deployments long, account `+2`) would close that gap, and is the
natural follow-up — with the caveat that it leaves a real net position, so it
must be flattened by hand and cannot be run near a session close.

## 23.2 The flatten still does not close — UNDIAGNOSED

Both run 3 and run 4 ended `*** STILL HOLDING ***`. Measured from run 4's own
files:

* the closes went through `owner._submit` **directly**, so they never appear in
  `intents_*.jsonl` — that is by design, not a fault;
* `plan_legs("SELL", 1, account_net=0)` yields `opposing = 0`, so both closes
  were placed as **`New`**, structurally identical to the opens that filled in
  8 s;
* `urgent_price` made them *more* aggressive (46945 and 47955) — both marketable;
* no anomaly fired, so `place_order` returned normally for both;
* and the position never moved: the last `events_*.jsonl` record, ~69 s after the
  watch ended, still reads `+1` / `-1`.

**No mechanism is proposed here.** §§18.6, 18.8, 18.9 and §22 record four
occasions where a story that fit the evidence was wrong, and the two candidates
that remain — a close that never matched, versus one that filled and was not
attributed — need opposite fixes.

**What landed instead is the measurement.** The `STILL HOLDING` branch printed
only that; the success branch had been taught to ask the venue (§22) and this one
had not, which is backwards — this is the branch that fires when something is
actually wrong. It now prints the venue's net and runs the same `_diagnose` the
INCONCLUSIVE path gets: anomalies, every working leg with its `filled` count and
`ordno`, and `list_trades` with each order's status and `deal_quantity`. A close
the venue reports **Filled** while the leg shows `filled=0` is an attribution
gap; **PendingSubmit** means it never matched. One run separates them.

## 23.3 Operational note

**G6's teardown is not currently reliable, so run it only when a hand flatten is
possible** — never unattended, and never close to a session boundary. The
account was left flat by the arithmetic of opposed legs both times, not by the
flatten working.

## 23.4 Next

1. `uv run python -m forecast_eval.live.flatten --out-parent outputs/g6_4`
2. Re-run G6 on a fresh `--out` to read the new `STILL HOLDING` diagnostic and
   settle §23.2.
3. Then, optionally, a **same-direction** run to close §23.1's `0 == 0` gap.

**1209 tests, 2 skipped. Nothing committed.**

## 23.5 CORRECTION — the account was never holding · 2026-09-07

`flatten.py` was run twice after run 4 and answered, correctly, both times:

```
position: +0 lot(s)
already flat — nothing to do.
```

**That is the right answer, and §23.2 was framed wrongly.** Two opposed one-lot
positions on **one netted futures account** are zero lots at the venue. There
was never a position for `flatten.py` to close — not after run 3, not after
run 4. The `*** STILL HOLDING ***` banner came from
`owner.book.net_for(slug)`, the owner's per-deployment **attribution**, which is
a bookkeeping layer over a flat account and carries no market risk.

**Nothing was ever left exposed overnight.** §23.3's operational warning stands
for a *same-direction* run, where the account genuinely nets non-zero, and is
not needed for the opposed shape.

### What the two flatten runs newly establish

**Neither printed `cancelled N working order(s) first`.** `cancel_working` runs
before anything else and reports what it withdrew; it withdrew nothing, so
**the two closing orders were not resting at the venue**. Combined with a flat
account that narrows §23.2 to:

| | still possible? |
|---|---|
| the closes are resting unfilled | **NO** — `cancel_working` found none |
| the closes filled, and the deals were not attributed to their legs | yes |
| the closes never became live orders (rejected, or never sent) | yes — though no anomaly fired, and §22's fix means a rejection would now be heard |

`list_trades` separates the two remaining cases in one line, and neither the
probe nor the flatten was printing it.

### What landed

* **`live/flatten.py` — `--show-orders`**, read-only: every order on the
  contract this session with its status and **`deal_quantity`**. The tool
  already fetched exactly this inside `cancel_working` and discarded it, so an
  operator asking "did my closes fill?" was told "already flat" — true, and not
  an answer. Two tests.
* **`tools/g6_live.py` — the banner no longer cries wolf.** It now names *which*
  book is non-flat, and sends the operator to `flatten.py` **only when the venue
  actually holds**. A message that routes an operator to an emergency close
  against a flat account teaches them to ignore the message.

**1211 tests, 2 skipped.**

### Next — one read-only command answers it

```bash
uv run python -m forecast_eval.live.flatten --out-parent outputs/g6_4 --show-orders
```

Four orders should appear for run 4. **The two opens will read `Filled dealt=1`.
What the two closes read is the whole question** — `Filled dealt=1` means the
deals were not attributed (an owner bug, in `OrderBook.match`); anything else
means they never became live orders. Run it in the same session; TAIFEX order
history does not survive the day.

## 23.6 THE CLOSES FILL AND ARE NOT BOOKED · MEASURED 2026-09-07

`flatten.py --show-orders` listed 23 orders on TXF this session. Run 4's four
are all there, and **all four filled**:

| ordno | what | status |
|---|---|---|
| `046233` Buy 1 @47950 | day OPEN | Filled dealt=1 |
| `046234` Sell 1 @46950 | night OPEN | Filled dealt=1 |
| `046250` Sell 1 @**46945** | day CLOSE | **Filled dealt=1** |
| `046251` Buy 1 @**47955** | night CLOSE | **Filled dealt=1** |

46945 and 47955 are exactly `urgent_price(±500)`, which identifies them beyond
doubt. Run 3's closes (`04620E` @46933, `04620F` @47943) filled too.

**So §23.2's remaining ambiguity is settled: the closes FILL, and the owner does
not book them.** The opens from the same run, on the same account, through the
same `_place`, were booked correctly minutes earlier.

### And it is not an unmatched deal

`OrderBook.on_deal` raises `deal for an unknown order` when `_match` fails, and
`g6_live` has printed anomalies as they happen since §21.3. **No anomaly fired
during either flatten.** Dedup cannot explain it either — the key is
`(ordno, exchange_seq)` and the four ordnos are distinct.

That leaves: **`on_deal` was never reached for the closing deals.** Either
`_on_event` did not receive them, or it received them and did not dispatch.

### What landed — the counter that separates those two

`OwnerProcess.events_seen` counts every callback by kind, incremented **before**
any parsing, so a payload the owner cannot read is still counted — otherwise a
parsing bug hides itself in the diagnostic written to find it. `_diagnose`
prints it.

**Observation only: no behaviour changes.** This plan has four times acted on a
mechanism that fit the evidence and was wrong (§§18.6, 18.8, 18.9, §22), and the
counter costs one line to read instead.

**How to read the next run:** four orders fill; `FUTURESDEAL` should reach **4**.

* `FUTURESDEAL: 4` — the deals arrived and were dropped on our side, between
  `_on_event` and `on_deal`. Look at `_handle_deal`.
* `FUTURESDEAL: 2` — the venue stopped delivering after the opens. The callback
  itself is the problem, and nothing in `OrderBook` can fix it.

**1213 tests, 2 skipped.**

### Note — this is teardown, not G6

§23's pass is unaffected. The defect is in the probe's closing sequence, after
every G6 clause has been evaluated, and it moves no position the venue reports:
both runs ended genuinely flat. What it does put in doubt is the **ledger's
accuracy after a close** — `fills_*.csv` recorded the opens and not the closes,
so a restart adopting from those ledgers would claim lots the account does not
hold. That is §16's exact failure mode and it deserves its own check once the
counter says where the deals went.

## 23.7 RUN 5 — the teardown worked, and NOTHING WAS FIXED · 2026-09-07

```
  both holding: {'day': 1, 'night': -1}  account +0
  [17:12:09] reconcile=True   [17:12:39] reconcile=True
  => G6 PASSED (live half)
--- flattening ---
  day: closing +1   night: closing -1
  flat.
```

**Read this as an intermittent failure, not a repair.** The flatten's closing
loop is byte-identical to run 4's: the 40 s wait landed in the §22.7 batch,
*before* run 4, and every change since is observational — the `STILL HOLDING`
message rewrite (failure branch only), `events_seen`, and `report_orders` in a
different file. The success branch has not been touched since §22.

**And the run did not produce the measurement it was launched for.** `_diagnose`
runs only on the failure branch, so `callbacks received` never printed. Two runs
were spent on a question that is still open.

### The number that did come out of it

| run | opens submitted | both BOOKED | elapsed |
|---|---|---|---|
| run 4 | 16:28:38 | 16:28:50 | **12 s** |
| run 5 | 17:11:27 | 17:12:09 | **42 s** |

§18.10 measured this as ~0.1 s lag with a fill at up to 12.8 s. **42 s is more
than three times that ceiling**, and the close window was 40 s — so the spread
straddles it exactly.

**That is a candidate and it is not confirmed.** It fits both runs, and "fits
both runs" is the reasoning §§18.6, 18.8 and 18.9 each used before being wrong.
What makes it worth acting on is not the story but the bound: a 40 s window
inside a distribution with an observed 42 s sample is indefensible whatever the
mechanism turns out to be.

### What landed

* **The close window is now 90 s**, matching what `_await_both` already allows
  for the same event in the opposite direction. There was never a reason for a
  close to be given less patience than an open.
* **Every run now reports the latency and the callback counts, on success as
  well as failure** — `booked in Ns callbacks {...}` when both opens land, and
  `flat. (closes booked in Ns, callbacks {...})` when the teardown completes. A
  silent success was hiding the exact series needed to size the window.

**1213 tests, 2 skipped.** No behaviour changed except the window.

### What is still open

Whether the closing deals ever arrive when the teardown fails. `FUTURESDEAL: 4`
would mean they arrive and are dropped on our side; `2` would mean the venue
stops delivering. The counter now prints on both paths, so the next failure —
or the next handful of successes with their timings — answers it either way.
