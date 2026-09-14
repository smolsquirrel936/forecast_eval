# LIVE_STRATEGY_ROUTING_PLAN.md

Make **`strategy/<session>/` the single source of truth for what trades**, live and
in the backtest, so a strategy the autoresearch loop produces reaches live simtrade
by existing — not by being re-typed into `live/config.py`.

Drafted 2026-09-09. Design settled with the user in conversation; the decisions are
recorded in §1 so they are not re-litigated.

## ▶ RULE — all work lands on `main`

`main` is `af4be0b` and is a **strict ancestor** of `fullday_0909` (`30f70d7`), so
this is plumbing on the trunk, not a strategy branch. Work happens on `main`;
`fullday_0909` is untouched by this plan and merges later, separately.

**This plan does not put iter05 live.** `main` has no `strategy/fullday/combine.py`
and its fullday `PARAMS` is still DAY's inherited `0.001 / 0.65`. What this plan
delivers is the *route*. Merging `fullday_0909` → `main` afterwards is what puts
the stride consensus and the long-only trader into simtrade — and by then it
arrives with no live-side edit at all, which is the whole point.

### Why `main` is also the strongest place to verify it

On `main`, **all three packages' `PARAMS` already equal their live pins**:

| session | package `PARAMS` | live pin | |
|---|---|---|---|
| night | 0.00045 / 0.5 | 0.00045 / 0.5 (dataclass defaults) | match |
| day | 0.001 / 0.65 | 0.001 / 0.65 | match |
| fullday | 0.001 / 0.65 | 0.001 / 0.65 | match |

and no pin overrides `device`, `bar_freq`, `signal_step`, `tick_size`, `fee_rate`,
`contract_multiplier` or `forced_close_on_session_end` — every one sits at a
dataclass default identical to the package factory's own default.

> ### ✗ CORRECTED 2026-09-09 — the "provable no-op" claim above was WRONG
>
> It compared `PARAMS` **values** (band, `min_prob`) and never checked which
> **components** each package's `make_strategy` builds. Since commit `a8ddd85`
> — *"Day and night trade a plain RSI instead of the banded Toto2 forecast"* —
> `strategy/night/wiring.py` and `strategy/day/wiring.py` build
> `make_rsi_forecaster` + `RsiEmitter`. `ThresholdEmitter` and `make_forecaster`
> are still imported there but **unused**, kept only to satisfy the isolation
> gate. Night's `PARAMS` still declares `checkpoint: Toto-2.0-1B` and
> `context_length: 3008`; its `make_strategy` does not use them. The values
> matched; the strategies never did.
>
> `strategy/night/wiring.py:66` states the divergence outright and says routing
> live through `make_strategy` is what would end it: *"a strategy change must not
> reach a live deployment as a side effect of a backtest edit."*
>
> **Resolved by D9 in §1 — route all three anyway.** Night and day live switch
> from the pinned Toto2 threshold strategy to RSI(14), knowingly and temporarily.
> Only `fullday` is unchanged by the routing.

So the acceptance test is **not** "identical everywhere". It is: `fullday`
byte-identical, and night/day differing by **exactly** the RSI switch and nothing
else. That is what was measured (§3 Phase 3).

## 1. Decisions (settled, do not re-open)

| # | decision |
|---|---|
| D1 | **No provenance gate for simulation.** `live/cli.py`'s `is_tuned` refusal is removed. `live/cli.py:679` hardcodes `simulation=True` and is not a flag, so the broker being gated cannot lose money. The gate's home is the eventual sim→production boundary, and it does not exist yet. |
| D2 | **The package owns the strategy; live owns the risk.** Live builds every strategy component through `strategy/wiring.py::make_strategy`. `LiveConfig` keeps only what is not strategy. |
| D3 | **`max_position` is a live-owned cap, not an override.** *Shipped as a refusal, not a clamp — see the Phase 3 result.* `check_startup` already **refuses** a package asking for more than the rail allows, which is stronger than the `min(package, rails)` written here; the rails simply stopped reading `cfg.max_position`. `RailsConfig`'s comment is explicit that `1` is a *soundness* bound — `Trader.on_fill` has no average-entry branch for a second partial fill (SHIOAJI_PLAN §2.10) — so a search must be able to lower it and never to raise it. |
| D4 | **`forced_close` and `aggression_ticks` are plain overrides**, defaulting to the package value. Neither is a rails concept; neither has a soundness bound. |
| D5 | **One flag name.** `--strategy` everywhere; `--config` kept as an argparse alias so recorded soak commands and tests keep working. They have always meant the same thing — `LiveConfig.strategy_package` (`live/config.py:104-108`) already derives the package name from the policy, 1:1. |
| D6 | **Package knob sets may grow.** `real_data.py:_resolve_strategy_knobs` relaxes from `set(base) == set(CLI)` to `set(CLI) <= set(base)`. |
| D7 | **`--param KEY=VALUE`, repeatable, on both CLIs**, passed straight into `make_strategy(**overrides)`. Not generated argparse flags: the parser would have to import the package before parsing, and the package is chosen by a flag. |
| D8 | **`strategy/*` is not edited by this plan.** Verified: `make_strategy` already returns `forecaster`, `emitter`, `make_trader`, `exit_rule`, `forced_close`, and already validates overrides against `PARAMS`. The seam exists; only live fails to use it. |
| D9 | **Route all three packages, accepting that night and day live switch to RSI.** Added mid-execution, after §0's no-op premise was found false — `strategy/night/` and `strategy/day/` have built an RSI strategy since `a8ddd85`, so routing live through their packages changes what those two deployments trade. The user's words: *"Route all three, and I'll make strategy of day and night use Toto2 later."* So the switch is accepted as a known, temporary state, and putting day and night back on Toto2 is **the user's follow-up in those packages**, not this plan's work. Only `fullday` is unaffected by the routing. |
| D10 | **NIGHT cannot restart into an open position, and that is accepted as temporary.** Surfaced by merging `night` into `main` (2026-09-09): night's package returns a `TimeStop`, and since D2 live *honours* the package's `exit_rule` instead of the hardcoded `None` it used to pass — so `LiveRuntime.adopt_position` refuses, because the previous process's entry bar index is not recoverable and a bars-held rule would measure from a sentinel. Three fixes were offered (persist the entry index; flatten before every restart; drop the rule live). **The user chose none of them:** day and night are being re-developed on a Toto2 signal emitter, which declares no exit rule, so `exit_rule` returns to `None` and adoption works again on its own. DAY and FULLDAY are unaffected today. |

## 2. The defect this closes

`LiveConfig` re-declares ~15 values `strategy/<session>/wiring.py` already owns.
That duplication has produced the same failure twice:

- the 2026-08-11 policy/band defect `live/config.py:4` describes; and
- **`live/config.py:120` hardcodes `.emitter.ThresholdEmitter`**, so iter03/iter05's
  `combine.ConsensusThresholdEmitter` — the entire 6.14 → 12.94 climb — is
  unreachable from live, while `build_trader` (`:177`) *does* pick up the package
  trader. A fullday live run would therefore be iter02's emitter on iter04's
  trader at DAY's band: a combination never backtested.

`tests/test_live_parity_sessions.py:145-146` cannot catch it — it builds **both**
sides with `cfg.build_emitter()`, so it compares the copy against itself.

After this plan the copy does not exist, so neither failure is expressible.

## 3. Phases

Each phase states its verification. Do not start the next until the current one
verifies.

### Phase 0 — capture the baseline (the no-op proof)

1. `git checkout main` (tree is clean; nothing to stash).
2. Scratch script → `<scratchpad>/baseline_live_components.json`: for each of
   `night`, `day`, `fullday`, construct today's live components via
   `cfg.build_emitter()` and `cfg.build_trader(execution=<stub>)`, and record
   `cfg.build_forecaster`'s resolved kwargs without importing torch. Dump every
   public attribute.
3. Full suite: `uv run python -m pytest forecast_eval/tests/ -v`. Record the green
   count and any pre-existing failure.

> **Known pre-existing failure.** `results_fullday.tsv`'s iter01 row records that
> `test_strategy_isolation`'s `volmultitf.py` pin fails on this Windows checkout as
> a CRLF artifact (`autocrlf=true`, no `.gitattributes`; on-disk bytes match the
> pin after newline normalisation). Confirm it is present at the standing start on
> `main` and do **not** fix it here — it is out of scope and its own decision.

**Verify:** baseline JSON has three entries; suite result recorded.

#### RESULT — Phase 0 done, 2026-09-09

- On `main` at `af4be0b`; baseline at
  `<scratchpad>/baseline_live_components.json`, three entries, captured by
  `<scratchpad>/capture_live_components.py`. **Re-run that same script in Phase 3
  and diff.** One adaptation is expected there and is not a failure: the
  `deployment.forced_close_on_session_end` key reads a `LiveConfig` field that
  Phase 3 deletes, so the after-script must read `forced_close` from the returned
  stack. The *value* must still be `false`.
- Suite: **1309 passed, 1 failed, 2 skipped** in ~63 s.
- The one failure is the CRLF artifact, now confirmed exactly rather than
  assumed: on-disk sha256 `e004eeab…`, LF-normalised sha256
  `9fcac965…` — **identical to the test's pin** — and `git diff` on the file is
  empty. It is a newline artifact, not a modification.
- **A second failure appeared on the first run only and is flaky, not
  pre-existing:** `test_rsi_strategy.py::test_one_call_does_not_scan_the_whole_history[night]`.
  It is a wall-clock ratio assertion (`tests/test_rsi_strategy.py:329`,
  `large < small * 5`); despite the docstring's intent a ratio of two timings
  still moves under load. It passed in isolation and on a clean re-run. Expect it
  to reappear intermittently in later phases; it is unrelated to this plan.

> **Environment note.** The project venv was destroyed and rebuilt on 2026-09-09
> (`uv run` found `night/.venv` pointing at a non-existent interpreter and
> replaced it). It was restored via UV_SETUP.md's "Reproducing from scratch"
> recipe; all four of that document's verification commands pass, GPU included
> (`2.14.0+cu130 True NVIDIA GeForce RTX 5090`). Library versions drifted upward
> — see §4. **No number in this plan's verification is comparable to one measured
> before that date** unless the inertness check in §4 has been run.

### Phase 1 — knob sets may grow (`real_data.py`)

`_resolve_strategy_knobs`: replace the equality guard with a subset guard in one
direction — every CLI knob must exist in `PARAMS`; extra package knobs are
resolved from `PARAMS`, get an `origins` entry, print in the banner and land in
`params.json`.

This preserves the guard's stated purpose. Its comment's worry is a knob *"tuned
in research and silently dropped at evaluation"* — a subset in this direction makes
dropping impossible — and the other half, a package that *drops* a CLI knob and
falls back to a stale literal, is still caught.

**Verify:** existing tests green; new tests that (a) a package declaring an extra
knob resolves, records and banners it, and (b) a package missing a CLI knob still
raises.

#### RESULT — Phase 1 done, 2026-09-09

Three edits, all on `main`:

- `real_data.py:_resolve_strategy_knobs` — `set(base) != set(CLI)` became
  `missing = set(CLI) - set(base)`. The resolve loop already iterated `base`, so
  an extra package knob is attributed, `setattr` onto `args`, printed by the
  banner (`real_data.py`'s `for knob in sorted(origins)`) and carried into
  `params.json` via `params=vars(args)` **with no further change**.
- `real_data.py` `make_strategy` call site — `**{... for knob in
  CLI_STRATEGY_DEFAULTS}` became `... for knob in origins`. Without this an extra
  knob would be resolved, banner-printed and recorded, then **not passed** — the
  recorded set and the passed set could differ. Keying both off `origins` makes
  them one set by construction.
- `tests/test_strategy_selector.py` — the `set(declared) == set(CLI)` pin at :41
  became `set(CLI) <= set(declared)`, plus two new tests pinning each guard
  direction.

Suite: **1311 passed, 1 failed, 2 skipped** — the failure is the CRLF artifact
only. The RSI timing flake did not recur.

**Both new tests were confirmed to fail against unmodified `real_data.py`** (via
`git stash push -- real_data.py`), so neither passes vacuously. Note the
difference between them: the extra-knob test is the real behaviour change — the
old guard *raised* where the new one resolves. The dropped-knob test raised under
both; what changed is that the error now names the missing knob instead of
printing two sorted lists for the reader to diff by eye.

### Phase 2 — `--param KEY=VALUE`

New module `forecast_eval/param_override.py`, one function
`parse_overrides(pairs, params) -> dict`. Not in `strategy/` — that is the
playground, and CLI parsing is not its layer.

Coercion, explicit and loud rather than clever:

- `bool` defaults accept `true/false/1/0/yes/no`, case-insensitive. **`bool("False")`
  is `True`** — this is the trap and it gets a test.
- literal `none` → `None`.
- a default of `None` (`max_aggression_ticks`, `min_prob`) has no type to read: try
  `int` → `float` → reject, and say so.
- everything else casts to `type(default)`, raising with the knob name on failure.

Unknown-knob validation is **delegated**, not duplicated: `make_strategy` already
raises naming the package's actual knob set, which is what argparse `choices=`
would have bought.

**Verify:** unit tests for every coercion rule above, plus a typo test asserting the
error names the package's knob set.

### Phase 3 — live routes through the package (the core change)

- `live/runtime.py:175, 207, 211` — three `config.build_*` calls become one
  `make_strategy(cfg.strategy_package, warmup_bars=0, tick_size=cfg.tick_size,
  **overrides)`, unpacked into `forecaster` / `emitter` / `trader`. `warmup_bars=0`
  is already what live passes (`runtime.py:211`) and already what the comment there
  justifies; `make_strategy` builds the forecaster eagerly, which is fine because
  runtime builds all three at once anyway.
- `live/config.py` — delete the 15 strategy fields, the three `build_*` methods and
  `is_tuned`. `LiveConfig` keeps `policy`, `tick_size`, `fee_rate`,
  `contract_multiplier`, `source_dataset`, `source_note`.
- `exit_rule` and `forced_close` from the returned stack replace
  `forced_close_on_session_end`.

**Verify:** re-run Phase 0's script. Output must be **byte-identical** to the
baseline for all three sessions. This is the acceptance test for the whole plan.

#### RESULT — Phases 2 and 3 done, 2026-09-09

**Phase 2.** New `param_override.py` + `docs/param_override.md`, `--param` on
`real_data.py`, 13 coercion tests and 2 resolver tests. Verified end to end: a
run with `--param min_prob=0.7 --param aggression_ticks=2` banners them as
`[param]` while every untouched knob still reads `[night]`.

**Phase 3.** The acceptance test, restated per D9 above and measured with the
Phase 0 script:

| session | before → after |
|---|---|
| **fullday** | **IDENTICAL** — emitter, trader, forecaster, deployment, all fields |
| night | emitter `ThresholdEmitter → RsiEmitter`, forecaster `Toto2Forecaster → RsiForecaster`; **nothing else changed** |
| day | the same two class changes; **nothing else changed** |

Trader and deployment blocks are untouched for all three, so the routing itself
introduced no drift — the only difference is the strategy the packages already
declared.

Wider than the plan predicted. `LiveConfig`'s fields had six consumers beyond the
three `build_*` methods (`artifacts.live_params`, `cli._rails_config`,
`runtime.check_startup`, the `LiveLoop` construction, the position guard, and the
two seeding paths), so the change also covers:

- `LiveRuntime` builds the stack once in `__init__` and keeps `strategy_params`;
  everything that read a strategy field off the config reads that instead.
- `live_params(config, *, strategy_params, ...)` — `params.json` now records the
  resolution the components were built from, `--param` included.
- `_rails_config` stops reading `cfg.max_position` (**D3**). The rails keep their
  own limit, and `check_startup` still **refuses** a package asking for more —
  which is stronger than the `min()` D3 first proposed, and was already the
  behaviour. D3's intent is met; its arithmetic is not what shipped.
- `describe()` reads the package. Format is unchanged because
  `tools/session_artifacts.py:_DESCRIBE_RE` parses it back out of recorded `meta`
  headers; `min_trend_ticks`/`path_momentum` come from the emitter class
  signature, since `PARAMS` deliberately does not declare them.

Tests: **1340 passed, 1 failed, 2 skipped** — the failure is the CRLF artifact.
98 failures and 14 errors were worked through, in three groups:

- **assembly tests** (`test_live_runtime`, `test_live_roll`, `test_live_resume`,
  `test_live_cli`) injected a cheap forecaster through `cfg.build_forecaster`.
  They now opt into a shared `cheap_strategy` fixture in `tests/conftest.py` that
  patches the factory. Its band is fixed at `LiveConfig`'s old defaults rather
  than read from the package — day's 0.001 is wider than night's 0.00045, and
  reading the package would make one generated frame emit BUY under one policy
  and HOLD under the other, which is a strategy property, not an assembly one.
- **declaration tests** (`test_live_config`) asserted the copy. They assert the
  package now, and the three `build_*` tests were replaced by
  `test_live_builds_the_package_the_backtest_would_run` — the assertion the old
  suite structurally could not make, and the reason `combine.py` was unreachable
  from live for a whole research loop while everything passed.
- **parity tests** feed a synthetic Toto2-shaped forecast, so they build the
  package's *banded* emitter directly rather than through `make_strategy`; RSI
  reads different payload keys and produced no fills at all. Noted in the file.

Two consequences worth knowing:

- **Only knobs in `PARAMS` are overridable.** `trend_filter` is not one, so
  `--param trend_filter=false` is refused by `make_strategy`. That is D6/D7
  working as designed, but it is the answer to "which parameters can I pass".
- **`describe()` reads the package, not the run's resolved params**, so a
  `--param` run's banner does not show the override. Phase 5 should fix that when
  it moves provenance into the record.

### Phase 4 — the gate goes, the risk stays

- `live/cli.py:503-525` — remove the `is_tuned` refusal (D1).
- `live/cli.py:352` — `ap.add_argument("--strategy", "--config", dest="strategy", ...)`
  (D5), plus `--param` (D7).
- `_rails_config` — rails keep their **own** `max_position`; the Trader receives
  `min(package_value, rails_value)` (D3). `RailsConfig.max_position` stops reading
  `cfg.max_position`.

**Verify:** `--strategy fullday` reaches startup against the dry-run broker instead
of exiting 5; a test that a package declaring `max_position=2` does **not** raise
the rail, and that the Trader gets 1.

#### RESULT — Phase 4 done, 2026-09-09

- **Gate removed** (`live/cli.py`). The `is_tuned` **property** was kept, against
  the plan's Phase 3 text: `describe()` needs it to render `UNTUNED`/`calmar=nan`,
  and `tools/session_artifacts.py:_DESCRIBE_RE` parses that back out of recorded
  `meta` headers and calls `float()` on the group. Enforcement became a record.
- **`--strategy`/`--config` are one argument** with `dest="config"`, so recorded
  soak commands and the suite keep working.
- **`--param` wired**, reaching both the `params.json` mirror and
  `LiveRuntime(strategy_overrides=...)`.
- `_rails_config` had already stopped reading `cfg.max_position` in Phase 3, and
  `check_startup` still refuses a package asking for more (D3 as shipped).

**A NameError shipped in this phase, and the suite did not catch it.**
`--param` was wired into `main` while **both** its imports silently failed to
land: the patch anchored on relative imports (`from .config import ...`) and
`live/cli.py` uses absolute ones. `main` referenced `parse_overrides` and
`strategy_wiring`, neither of which existed. **1342 tests stayed green**, because
`main` logs in, makes directories and talks to a venue, so nothing calls it far
enough to reach the new code. The first thing to execute it was the user's live
invocation.

**A second, worse instance of the same class was already in the tree from
Phase 3**, undetected: the edit that repositioned `strategy_wiring`'s import in
`live/runtime.py` replaced a three-line block with one line and **deleted
`from .broker.sim import SimBroker`**, breaking `make_sim_runtime`.

`tests/test_undefined_globals.py` is the fix — an AST pass over every
module-level function asserting each global it loads resolves in the module or in
builtins. It found the `SimBroker` deletion on its first run. 87 parametrised
cases; the suite is now **1429 passed, 1 failed** (CRLF artifact).

> **Diagnosis error worth recording.** This result block previously claimed the
> run "died at the Shioaji login (no credentials), which is environmental and
> expected." That was wrong and was inferred, not observed: `head -8` had cut the
> traceback above the actual error, and the `meta` header that seemed to prove
> deep progress is written by the `Recorder` *before* the failing line. The run
> died at the NameError, immediately after that header.
>
> What the header **does** still prove is real and unchanged: the `is_tuned`
> refusal is gone (an UNTUNED config got past it) and `describe()` renders
> provenance into the artifact. What it does not prove is that the phase worked
> end to end — that is still unverified live.

> **⚠ Operational residue.** Attempts left
> `outputs/live_restart_day_night.json` at `run_in_flight: true` twice; reset both
> times to what `guard.end()` leaves. It is fullday's own guard (`day_night`
> slug) — night's and day's are untouched.

Tests: **1342 passed, 1 failed, 2 skipped** (CRLF artifact). The old
`test_an_untuned_config_is_refused_before_anything_else_runs` was replaced by
three: the untuned config is no longer refused but still self-identifies,
`--strategy`/`--config` are one flag, and `--param` is repeatable and coerces.

### Phase 5 — provenance becomes a record, not a precondition

Removing the gate does not weaken the ledger guards — it makes them unnecessary.
They existed because live held a copy that could go stale; a live path that always
builds from the package **cannot** be stale. What must survive is the record.

Stamp into the startup banner and the recorder's `meta` header: `git rev-parse
HEAD`, the package name, the resolved `PARAMS` **with `origins`** (package vs
typed flag), and a sha over the package directory.

**A run with any `--param` set is not comparable to any backtest row**, and the
artifact must say so on its face. `real_data.py` already computes `{knob: origin}`
for its banner — reuse it rather than writing a second one.

**Verify:** a dry-run's `session.jsonl` meta carries all four; a `--param`-overridden
run is visibly marked.

#### RESULT — Phase 5 done, 2026-09-09

Five fields, not four, in both `params.json` and the recorder's `meta` header:
`strategy_package`, `strategy_sha`, `git_commit`, `strategy_origins`, and
`strategy_overridden` — the last a plain boolean because the one question asked
of these files later is *"is this row comparable to a `results.tsv` row?"*, and a
reader should not have to derive it from the origins map.

- **`strategy_sha`** (`live/artifacts.py`) hashes `strategy/<package>/*.py` with
  **newlines normalised**. Deliberately unlike `test_strategy_isolation.py`,
  which pins raw bytes and fails on this checkout purely because `autocrlf=true`
  rewrites line endings — a provenance hash that moves with the checkout records
  the checkout, not the strategy. Measured distinct per package: night
  `b72ca5c45bba`, day `fda2ab7503a7`, fullday `c3216b7794b0`.
  It catches an **uncommitted** edit, which `git_commit` alone cannot — and a
  research loop spends most of its time in exactly that state.
- **`describe(params=None)`** now renders the run's resolved knobs when given
  them. It read the package unconditionally before, so an overridden run printed
  the *declared* band while trading the typed one — on the banner **and** in the
  `meta` header. Format is unchanged and pinned by a test, because
  `tools/session_artifacts.py:_DESCRIBE_RE` parses that line out of every session
  file already on disk.
- **A `--param` run prints a second banner line** naming the typed knobs and
  saying the run is not comparable to any `results.tsv` row. Needed precisely
  *because* `describe()` now renders resolved values: the line looks exactly like
  a tuned run's, which is correct and would otherwise hide that it was typed.
- The provenance fields sit **beside** `describe()`'s line rather than inside it.
  Packing them in would have meant extending `_DESCRIBE_RE` and breaking the
  rebuild for existing sessions; `tools/session_artifacts.py` reads the new
  fields when present and yields `None`/`{}` when absent.

`test_the_rebuilt_params_are_the_live_ones_key_for_key` caught the incompleteness
immediately — it holds `live_params`' key set identical to the rebuild's, and
failed until the rebuild carried all five. That guard did its job unprompted.

**A second `main()` bug shipped in this phase and the user hit it.** The
`--param` resolution was placed AFTER the `Recorder` that stamps it into the
`meta` header, so `main` loaded `strategy_params` before binding it. The same
edit wrote `artifacts.strategy_sha(...)`, where `artifacts` is a module name
`main` rebinds to an `ArtifactWriter` further down — which makes it a **local for
the whole function**, so the module was unreachable through it. Two
UnboundLocalErrors, neither visible to the suite.

That is **three failures in `main()` across three phases**, all in code added
here, all invisible to a green suite, all found by the user running the CLI. The
common cause is structural: `main` logs in, makes directories and talks to a
venue, so no unit test executes it.

`tests/test_undefined_globals.py` now carries two static checks over every
module — undefined globals, and locals loaded before they are bound — plus two
self-tests, because a guard nobody has seen fail is not evidence. The
use-before-assign check needed `_own_nodes` to stop at nested scopes: its first
draft flagged `live/cli.py:main`'s `lapsed_reported`, a closure defined early and
called after the binding, which is correct code. 176 cases.

Static checks alone are mitigation: they cannot catch a wrong *value* passed
through correctly-bound names.

**`tests/test_live_cli_main.py` closes the gap — `main()` is now executed.** The
venue, credentials and runtime are stubbed; the run is cut at
`rt.adopt_position`, the first thing needing a venue. By then argument
resolution, the restart guard, strategy resolution, `--param` coercion, the
recorder's `meta` header, `params.json` and every console banner have run. All
three shipped bugs would have failed it. 7 cases.

Two things the writing of it forced, both worth more than the tests:

- **The cut had to move.** Stopping at `LiveRuntime(...)` left the banners
  untested — `describe()` and the `--param` warning print from the runtime's own
  attributes, *after* construction. And `main`'s `finally` runs on the sentinel,
  so an `AttributeError` there masked it entirely; the stub carries `shutdown`,
  `summary`, `roll_fills` and `loop.fills`, and `ArtifactWriter.write` is
  neutralised because it renders a report off the runtime.
- **A real defect, found by the test rather than by a user for once.** A
  `--param` typo survived until `LiveRuntime` constructed it — *after*
  `guard.start()` had marked a run in flight and after a login had been spent,
  so a slip cost a phantom restart failure and a venue round trip against a
  gate whose own message warns that repeated logins extend a suspension. The
  whole strategy resolution now happens **before the guard and before
  credentials**, and an unknown or uncoercible knob is exit 5 with the package's
  real knob set named. This duplicates a check `make_strategy` also makes, which
  is deliberate here and only here: it is the CLI's argument validation, and it
  matches the file's existing rule that pure argument checks precede the guard.

**Verified live on the third attempt** (`--strategy fullday --broker dryrun
--resume --duration-minutes 10`): clean 10-minute run, `guard.end()` reached, and
`params.json` + the `meta` header both carry the provenance —
`strategy_package: fullday`, `strategy_sha: c3216b7794b0`,
`git_commit: af4be0b45e55`, all 12 origins reading `fullday`,
`strategy_overridden: false`.

**That run also exposed a gap in the record I had just called complete**, and it
is now closed: `git_commit` read `af4be0b` — `main`'s HEAD — while every live
module the run executed was modified and unstaged. `strategy_sha` covers
`strategy/`; nothing covered the harness. **`git_dirty`** is the sixth field, a
boolean rather than a diff, because the question a reader asks later is "can I
trust the commit?" and one bit answers it. Threaded through the same four points
the others were (`live_params`, the `meta` header, the session rebuild, and the
key-set drift test).

Tests: **1531 passed, 1 failed, 2 skipped** (CRLF artifact).

### Phase 6 — tests and docs the change forces

- `tests/test_live_config.py` — `build_*` tests (`:187-223`) become "live and
  backtest build the same objects"; the drift guards (`:392`, `:420`) and the
  `results.tsv` schema block lose their subject and go, with §5's reasoning
  recorded in the file so the deletion is not mistaken for a loosening.
- `tests/test_live_cli.py:496, 513` — the two untuned tests; `:513` keeps testing
  the *property* against a synthetic config or goes with the gate.
- `tests/test_live_parity_sessions.py` — one side from `wiring.make_strategy`, the
  other from live's builder. Near-tautological after Phase 3, which is the point:
  it is the assertion that was missing.
- Docs: `docs/strategy/wiring.md`, `live/config.py`'s module docstring,
  `STRATEGY_SPLIT_PLAN.md` §5/D5 (asserts the gate), `SHIOAJI_PLAN.md`. A new module
  in Phase 2 needs its page — `tests/test_docs_coverage.py` asserts it.

**Verify:** full suite green except the known CRLF artifact.

#### RESULT — Phase 6 done, 2026-09-09

Two of this phase's three instructions were written before Phases 3-5 changed
their premises, and both were **not** carried out as written:

- **The drift guards were kept, not retired.** The plan assumed provenance would
  follow the strategy values out of `LiveConfig`. It did not — D1 removed the
  *gate*, not the *record* — so `test_every_deployment_is_pinned_to_the_best_keep_row`
  and `..._row_agrees_with_its_policy_and_dataset` still guard something real:
  that a pin's claimed research row is the best `keep` row for its session and
  tape vintage. A pin can still drift from its provenance; it can no longer drift
  from the strategy, because it no longer states one. Retiring them would have
  deleted the only thing that fails when research finds something better and
  nobody re-pins. The reasoning is recorded in the test file, not only here.
- **The `results.tsv` schema block stayed** for the same reason.

The parity work, done properly:

- `test_the_backtest_cli_and_live_resolve_the_same_strategy` — `real_data.py`'s
  CLI resolution against live's, per session. Near-tautological now, which is the
  point: it fails if a second construction path is ever reintroduced.
- `test_live_takes_its_components_from_the_seam_and_nowhere_else` — the one that
  is **not** a tautology. Comparing `make_strategy` against `make_strategy` could
  never catch a live path that bypasses the seam, which is exactly what
  `build_emitter` did. So it replaces `make_strategy` with a sentinel stack and
  asserts that is what a `LiveRuntime` ends up holding. Against the pre-plan code
  it fails twice over: `build_emitter` would return a real emitter rather than
  the sentinel, and `strategy_params` would not exist at all.

Docs: `docs/tests.md` gained rows for the three new test modules, and its
`test_live_config.py` row no longer describes the deleted `build_*` factories.
`SHIOAJI_PLAN.md` needed nothing — it never described the gate.

Tests: **1537 passed, 1 failed, 2 skipped** (CRLF artifact).

### Phase 7 — first simtrade

- The sim account is **LONG 1 @ 44555**, open since 2026-08-25. `--fresh-start` is
  refused while a position is held: flatten first, or `--resume`.
- Dry-run broker before Shioaji sim.
- Size the run from what it proves (SHIOAJI_PLAN's second ground rule): everything
  this exercises — package resolution, banner, `--param` origins, warm-up — finishes
  before the first bar. Minutes, not a session.
- **Do not run it beside day or night.** STRATEGY_SPLIT_PLAN §7.1's reconciliation
  rail halts every deployment the moment two hold at once.
- Check `warmup_days_default` for a `both()` policy — it is bars-per-session derived
  and has never been exercised for fullday.

## 4. Risks

- **D1 removes the only thing standing between a strategy and a broker.** Today that
  broker is hardcoded to simulation, so the exposure is zero — but whoever builds a
  production path must build the gate at the same time. Recorded here because the
  reason it was safe to delete is a property of `cli.py:679`, not a property of the
  design.
- **`--param` lets an unmeasured configuration trade in simulation.** Deliberate
  (D7). Phase 5's origins record is the mitigation.
- **After D6, the three packages stop having identical knob sets.** Correct — they
  are diverging strategies — but anything that compares them must stop assuming a
  fixed twelve.
- **`tests/test_strategy_selector.py` is modified on `fullday_0909`** and may be
  touched here too; expect a small conflict at the later merge.
- **The venv rebuild of 2026-09-09 drifted the library stack** and no inertness
  check has been run against it. Observed vs UV_SETUP.md's table: `torch`
  2.12.1 → **2.14.0+cu130**, `plotly` 6.8.0 → **7.0.0**, `pyarrow` 24.0.0 →
  **25.0.1**, `pytest` 9.1.1 unchanged, plus newer `numpy`/`scipy`/`gluonts`.
  UV_SETUP.md's own rule is *"Run it after any environment change"* — re-run a
  pinned backtest and require a bit-identical reproduction.
  Its stated target *was* stale — the recipe named `results.tsv` row `e44a047`,
  measured on `TXF_RPT_minute_night.csv@2026-05-15`, a vintage the 2026-09-01
  rebuild replaced **in place**, so no one can reproduce it. **Corrected in
  UV_SETUP.md on 2026-09-09** to `4282d33` (Calmar 3.6006, 235 trades, warmup
  538790) on `@2026-09-01`, with the full command. The check itself is still
  **unrun**. It affects the backtest ledger, not this plan's phases — Phase 3's
  acceptance test is a before/after diff taken inside one environment.

## 5. Known state after the merges

- **NIGHT live cannot adopt a position on restart** until day/night move to a
  Toto2 emitter (D10). A night soak restarted while holding must be flattened
  first. DAY and FULLDAY are unaffected.
- **NIGHT and DAY trade RSI live** (D9), by way of their packages.
- **FULLDAY trades iter05** — `combine.ConsensusThresholdEmitter` at
  RET_K=PROB_K=3, long-only trader, band ±0.00075 / min_prob 0.5 — reached live
  by merging `fullday_0909` with **no live-side edit**, which was the goal.
- `PINNED_FULLDAY` still carries no `source_commit`, so every fullday run records
  itself `UNTUNED`. Correct: `results.tsv` has no FULLDAY row, and iter05 lives in
  `results_fullday.tsv`, a different schema. Re-pinning it is a deliberate,
  separate decision.

> **Follow-up, 2026-09-14 — D9 and D10 superseded by DAYNIGHT_TOTO2_RESET_PLAN.**
> The two bullets above about NIGHT and DAY describe the state this plan left, and
> stay as that record. The reset restored `strategy/day/` and `strategy/night/` to
> `20767bb` (the pinned Toto2 threshold strategies) and deleted RSI. Consequences:
> NIGHT and DAY trade Toto2 live again, by the same routing and with no live-side
> edit; night's package returns `exit_rule=None`, so **D10's restart-adoption block
> is gone**. FULLDAY is untouched.
>
> **Second follow-up, same day: FULLDAY reset too** (DAYNIGHT_TOTO2_RESET_PLAN §8).
> The "FULLDAY trades iter05" bullet above is now history. `strategy/fullday/` is back
> at `20767bb`, the same `ThresholdEmitter` + two-sided `Trader` + Toto2 code as day,
> with day's inherited band (±0.001 / `min_prob` 0.65). `combine.py` and the long-only
> trader are deleted; the loop is archived at `archive/fullday-toto2-iter05`. All three
> deployments now trade the same strategy code, differing only in band, gate and
> session policy.

## 6. After this lands

Merge `fullday_0909` → `main` (fast-forward) to put iter05's stride consensus and
long-only trader into simtrade. No live-side change will be required — that is the
deliverable.
