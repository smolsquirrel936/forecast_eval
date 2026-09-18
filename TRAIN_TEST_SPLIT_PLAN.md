# TRAIN_TEST_SPLIT_PLAN.md — a held-out tail the autoresearch loop never sees

**Status: BUILT 2026-09-18, §6 steps 1–6 all done.** Branch: `main`, uncommitted.
Decisions taken with the user are marked **[DECIDED]**; two calls I made myself
are marked **[MY CALL]** and are still the ones to veto first.

**Suite: 1474 passed, 2 skipped, 1 failed** — the failure is
`test_strategy_isolation`'s `strategy/volmultitf.py` hash pin, the pre-existing
CRLF artifact UV_SETUP.md documents, unchanged from the 1447-passing baseline
taken before this work. 27 of the new passes are `tests/test_train_split.py`.

**§6.6 measured**, night tape, `--n-bars 60000 --warmup-bars 20000`, 1B
checkpoint, `--train-frac 0.8` against the same run at the default:

| | n_trades | net | max dd | Calmar |
|---|---|---|---|---|
| TRAIN 80 % (the decision figure) | 17 | −5 079.3454 | −7 633.5346 | **−0.6654** |
| WHOLE (report figure) | 21 | −6 605.0398 | −7 633.5346 | −0.8653 |

`fills.csv` and `trades.csv` are **byte-identical** between the two runs, the
report's Result summary carries the whole-data numbers in both, and Settings ends
`train_frac, train_split_bars, train_split_ts, command` with no `train_bars` key.
Two implementation notes worth keeping:

- **The §4.3 trap fired during the build.** The first cut popped `train_bars` but
  not `train_frac`, so the rebind left `train_frac` where argparse had declared it
  and the group was not contiguous. G6b caught it. Both dests are popped now.
- **G5 tests one layer down from where the plan put it.** `parse_overrides`
  forwards an unknown name *uncoerced* by design (its own docstring says so, to
  keep typo-catching in one place); `make_strategy` is what refuses. The
  assertion moved there, plus a per-session check that no package's `PARAMS`
  declares either knob.

## 1. The defect

Every number the autoresearch loop selects on is measured over the **whole**
post-warmup window. `real_data.py` splits the tape once — `warmup` bars of
silent context, then everything after it is evaluated — and the Calmar that
`results.tsv` records, and that each `iterNN` commit message quotes, covers that
entire trailing region.

So the loop's 14 iterations per session each read a number computed on the same
bars they are tuning against. `min_prob 0.65 -> 0.70 -> 0.71`, `stop_loss_ticks
2000 -> 1400`, `buy_threshold 0.001 -> 0.0009` — each keep/discard decision is
scored on data every prior decision already saw. There is no region left that
can answer "does this configuration work on bars the search never touched".

Note what this is **not**: Toto2 is pretrained and never fitted here, and
`warmup` is model *context*, not model *training*. The overfitting at risk is
the search's — knobs fitted to one realization of the tape by a loop that gets
14 draws at it. "Training set" below means *the region the loop is allowed to
score on*, nothing more.

## 2. The split

Warmup is untouched. The **eval window** subdivides:

```
|<---- warmup (no trades) ---->|<---- train ---->|<---- test (held out) ---->|
0                           warmup        warmup+T                    n_session
                                          T = n_train
```

All three lengths are denominated in **in-session bars** — the sequence
`[m.timestamp for m in Environment(ticks).stream()]` that `real_data.py:388`
already builds, not raw CSV rows. That is the unit `warmup` means and the unit
`run_backtest` counts `bar_idx` in; a length quoted against raw rows names a
different bar (MEASURED 2026-09-01: 904 rows' difference on the day tape).

### 2.1 Why one backtest pass is enough

The backtest is strictly causal: nothing at bar *t* is influenced by any bar
after *t*. The run loop hands the forecaster a freshly-built frame of
history-through-*t*, and `Execution` resolves each fill against prints that have
already arrived. Therefore **the prefix of a whole-data run through `split_ts`
is bit-identical to a run that stopped at `split_ts`.**

That is what makes this cheap. We run once over everything, keep writing
whole-data artifacts and the whole-data report exactly as today, and compute a
*second* metric pack from the same `BacktestResult` filtered to
`timestamp <= split_ts`. No second GPU pass, and the train Calmar is an exact
number rather than an approximation.

`build_trades` books a round-trip only on a **closing** fill, so a position still
open at the split produces no trade — which is also what a truncated run would
do. The straddling trade is dropped by both, identically. §5's G2 pins this.

### 2.2 The one leak, stated rather than patched

`forecast_quality` looks up the realized return at `i + horizon_bars` in the
tick frame, so a forecast in the last ~`horizon_bars` before the split reads a
price from the held-out side. With `horizon_bars=30` against eval windows of
~10^5 bars that is the last 30 bars of the train region, and it touches the
**hit-rate / IC block only** — never `trading_metrics`, never Calmar, never
anything a keep/discard decision reads.

Left alone deliberately. Truncating the tick frame at the split would make the
forecast block disagree with the same block on a whole-data run for a reason
unrelated to the split, and the block is diagnostic, not selective.

## 3. Surface

### 3.1 Two flags on `real_data.py` **[DECIDED]**

| flag | type | default | meaning |
|---|---|---|---|
| `--train-frac` | float | **1.0** | fraction of the eval window scored on |
| `--train-bars` | int | `None` | train length in in-session bars; **overrides `--train-frac` entirely** |

Exactly the `--lookback-frac` / `--warmup-bars` pair, for the same reason and
with the same precedence — one line, mirroring `real_data.py:404`:

```python
eval_bars  = n_session - warmup
n_train    = (args.train_bars if args.train_bars is not None
              else int(args.train_frac * eval_bars))
split_idx  = warmup + n_train           # first held-out in-session bar
split_ts   = in_session[split_idx - 1]  # last train bar; trades with exit_ts <= it count
```

**Why both.** A fraction does not survive a tape vintage: DAY_SESSION_PLAN 2.2
records `--lookback-frac 0.30` moving the warmup split `520004 -> 536373` bars
when the tape grew, and `results.tsv` pins `warmup_bars` because of it. The same
drift applies here — but *only* to how many trades get counted, **not** to the
forecast grid. Forecasts still fire at `warmup + k*stride` whatever
`--train-frac` says, so a drifting fraction yields a number that cannot be
re-derived on a longer tape, not a silently different strategy. Less dangerous
than the warmup case, still enough to break a `results.tsv` row. Use
`--train-frac` interactively; record `--train-bars`.

Validation, both refusing with exit 2 and an actionable message:

- `0 < train_frac <= 1.0` (1.0 admitted — it is the default and means "no split")
- `0 < args.train_bars <= eval_bars`, bounded by **eval bars**, not `n_session` and
  not raw rows

### 3.2 These are not strategy knobs **[MY CALL]**

Neither flag joins `CLI_STRATEGY_DEFAULTS`, so neither is resolvable from a
`strategy/<session>/wiring.py` `PARAMS` table, and `--param train_frac=0.7` is
refused by `parse_overrides` as an unknown key.

**Why:** a strategy package declaring its own scoring window would let the thing
being measured choose its own measurement. `--param` exists so a search can
reach a knob the CLI has no flag for (LIVE_STRATEGY_ROUTING_PLAN D7); the
evaluation protocol is the one thing it must not reach. §5's G5 pins the refusal.

### 3.3 Banner

Two lines after the existing `warmup` / `eval` pair, printed **only when a split
is active** (`n_train < eval_bars`), in the same format:

```
  train  ( 70.0%) : 74812 bars  [2024-03-11 09:01:00 .. 2025-11-04 13:44:00]   [--train-frac 0.7]
  test   ( 30.0%) : 32062 bars  [2025-11-04 13:45:00 .. 2026-08-21 05:00:00]   HELD OUT — not scored
```

The trailing tag names which flag produced the split, the way the strategy
banner names each knob's origin.

## 4. What changes, file by file

### 4.1 `metrics.py` — `compute_metrics(..., through=None)`

One new keyword-only parameter. When `through` is a timestamp:

1. filter `result.fills`, `result.signals` and `result.forecasts` to
   `timestamp <= through`;
2. replay the filtered fills through a **fresh `Portfolio`** (same
   `contract_multiplier`, `classify_session` per fill) so
   `realized_net_pnl_points` for the attribution block is defined identically to
   the whole-data path — `realized_pnl - total_fees`, not a re-sum of trade
   records, which differ by the open leg's fee;
3. everything below is unchanged and operates on the filtered result.

`tick_df` is **not** truncated (§2.2). When `through is None` the function is
byte-for-byte what it is today — the report path never passes it.

No new module, so no new `docs/` page and no `test_docs_coverage` obligation.

### 4.2 `real_data.py` — two packs, one run

```python
res = run_backtest(...)                                   # unchanged, whole tape
metrics       = compute_metrics(res, ticks, forced_close=...)             # WHOLE
train_metrics = compute_metrics(res, ticks, forced_close=..., through=split_ts)
```

- `write_all(..., metrics=metrics, ...)` is **unchanged**. The artifact bundle
  (`fills`, `orders`, `forecasts`, `signals`, `trades`) and therefore every chart,
  the equity curve, the buy-and-hold overlay and the Result-summary tables stay
  whole-data. **The HTML report represents the whole tape, as required.**
- `train_metrics` is computed and printed **only** when `n_train < eval_bars`.
  At the default 1.0 nothing extra is computed and the terminal output is
  identical to today's, so every command string recorded in `results.tsv` keeps
  reproducing.
- `annual_returns` stays attached to the whole-data pack only; it is a report
  section, and a partial-year train tail would read as a bad year rather than a
  truncated one.

### 4.3 The new Settings fields **[DECIDED]**

The report's Settings table is `_kv_table_html(params)` over
`{**vars(args), "command": ...}`, so both flags land there the moment argparse
learns them. No `reports.py` change. But their *position* would be incidental —
wherever the flags happen to be declared — so `params` places the four train
fields as one contiguous, deliberately ordered group, immediately above the
trailing `command` row:

| Settings field | source | `--train-frac 0.7` | `--train-bars 74812` |
|---|---|---|---|
| **`train_frac`** | **the option; `None` when `--train-bars` overrode it** | **`0.7`** | **`None`** |
| `train_split_bars` | resolved train length, in-session bars | `74812` | `74812` |
| `train_split_ts` | last train bar's timestamp | `2025-11-04 13:44:00` | `2025-11-04 13:44:00` |

`train_frac` sits **directly above `train_split_bars`** so the option and the
boundary it resolved to read as one line of reasoning: a bare fraction does not
identify a bar, and the resolved count is meaningless without the fraction that
produced it.

**Three fields, not four.** `args.train_bars` — the flag as typed — is
deliberately *removed* from `params` rather than recorded beside them. It is
`None` on every `--train-frac` run, so as a Settings row it is dead in the common
case, and its content is already in `command` verbatim. What it was going to
disambiguate is handled at the source instead:

> **When `--train-bars` wins, `train_frac` is recorded as `None`, not `1.0`.**

`1.0` is argparse's untouched default, and writing it on a run that never
consulted `--train-frac` records a value the run did not use — worse, one that
reads as "no split" while a split is active. `None` says *this route was not
taken*, and `train_split_bars` carries the truth in both columns above.

**A re-assignment will not place these.** `train_frac` is already a key of
`vars(args)`, and rebinding an existing key leaves a dict's insertion order
untouched — `params[...] = ...` would silently put it nowhere. The group is built
by popping both dests from the `vars(args)` copy and re-inserting three fields in
order, before `command` is appended:

```python
p = vars(args).copy()
p.pop("train_bars")                     # lives in `command`; None on a frac run
train = {
    "train_frac":       None if args.train_bars is not None else args.train_frac,
    "train_split_bars": n_train,
    "train_split_ts":   split_ts,
}
params = {**p, **train, "command": _command_string(...)}
```

Built here rather than by a late `setattr` on `args`, for the reason the existing
comment on `command` gives: Settings follows dict order, and relying on where a
late attribute lands breaks the next time one is stamped.

### 4.4 Terminal layout when a split is active **[MY CALL — veto candidate]**

The train block prints **first**, under the decision heading; the whole-data
blocks follow, demoted and labelled:

```
=== Trading metrics [TRAIN 70.0% — THE DECISION NUMBERS] ===
  ...
=== Trading metrics (WHOLE data — report figure, do NOT record) ===
  ...
```

The alternative is to suppress the whole-data trading block entirely while a
split is active, on the grounds that the loop cannot misread a number it is not
shown. I kept it because the report shows it regardless and hiding it in one
place only is a half-measure — but if you would rather the terminal show the
train numbers alone, that is a two-line change and a stronger guard.

Held-out **test** metrics are computed nowhere and printed nowhere
**[DECIDED]**. Fourteen iterations that each glance at the test Calmar have
spent the holdout, which is the defect §1 is about. Scoring the tail is a
deliberate, separate act — a later `reports.py` sub-command or a one-off, not a
per-run line. Out of scope here.

### 4.5 `results.tsv` and the three per-session ledgers **[DECIDED]**

Two columns, appended after `warmup_bars` in `results.tsv` and after `commit` in
`results_{day,night,fullday}.tsv`:

| column | value |
|---|---|
| `train_frac` | the fraction; `pinned` when `--train-bars` set the split; `1.0` when there was none |
| `train_split_bars` | the **resolved** in-session bar count, or `full` |

Both, not one: a bar count alone does not say what proportion it was without the
row's `eval_bars`, and `train_frac` alone is not reproducible (§3.1).

The two columns carry the same names and the same values as the Settings fields
(§4.3), with one substitution: Settings' `None` is written `pinned` here, because
a ledger blank reads as "not recorded" where the fact is "a bar count set this
split" — the same reason `forecaster_sha` backfilled to `pre-split` rather than
empty. There is no `train_bars` column for the same reason there is no
`train_bars` Settings field: it would be empty on exactly the `--train-frac` runs
it exists to pin, and `command` already reproduces the typed flag verbatim.

The 20 existing `results.tsv` rows backfill to `1.0` / `full` — a literal, the
way `forecaster_sha` backfilled to `pre-split` rather than blank, because a
blank reads as "not recorded" where the fact is "the whole window". The three
per-session files hold a header only, so they gain the columns and no rows.

**A Calmar measured on 70 % of the eval window is not comparable with one
measured on 100 %.** These columns are what stops the next loop ranking across
two different windows without noticing.

### 4.6 Docs (all inside `forecast_eval/`)

- `docs/real_data.md` — the two flags, precedence, the banner, the two packs,
  and the explicit statement that the report is whole-data by construction
- `docs/metrics.md` — `through=`, the fresh-`Portfolio` replay, and §2.2's leak
- `SPEC.md` §4.8 — gains a train/test paragraph beside warm-up (it is the other
  half of the same window arithmetic); §7 notes that the reported pack may be
  windowed while the report is not
- `usage.md` / `CLAUDE.md` — one example invocation carrying `--train-bars`

### 4.7 Not touched

`live/`, `run.py`, `reports.py`, every `strategy/<session>/` package, and the
seven frozen shared files. The report is unchanged **because** nothing is passed
to it that it did not already receive.

## 5. Success criteria — new `tests/test_train_split.py`

Tests need no docs page. Each is a goal, not a description:

| id | assertion |
|---|---|
| **G1** | `--train-bars` overrides `--train-frac` outright; frac alone derives `int(frac * eval_bars)`; both bounds refuse with exit 2 |
| **G2** | **the causality claim.** Build a synthetic `BacktestResult`, then assert `compute_metrics(res, ticks, through=ts)` equals `compute_metrics(res_truncated, ticks)`, where `res_truncated` holds only the fills/signals/forecasts at or before `ts`. This is the property §2.1 rests on; if it fails, the design is wrong, not the test |
| **G3** | at `--train-frac 1.0` the train pack is identical to the whole pack, and no second pack is computed |
| **G4** | a round-trip whose entry precedes `split_ts` and whose exit follows it appears in neither the train trades nor the train `n_trades` |
| **G5** | `--param train_frac=0.7` is refused; neither flag appears in `CLI_STRATEGY_DEFAULTS`; a package cannot declare either |
| **G6** | with a split active, `write_all` still receives the whole-data pack — the report's Result-summary `net_pnl_points` equals the whole-run portfolio's, not the train window's |
| **G6b** | `params.json` key order ends `… train_frac, train_split_bars, train_split_ts, command` — the three **contiguous**, `train_frac` immediately before `train_split_bars`, `command` last, and **no `train_bars` key at all**. Asserted on the key list, since §4.3's pop-and-re-insert is exactly the kind of ordering a later edit reverts to a silent no-op re-assignment |
| **G6c** | a `--train-bars` run records `train_frac: None` in `params.json` and `pinned` in the ledger — never `1.0`, which would read as "no split" on a run that has one |
| **G7** | `results.tsv` and the three per-session files carry both columns and no row is blank (the shape of the existing `forecaster_sha` column test) |

Full suite green before and after:
`uv run --no-sync python -m pytest forecast_eval/tests/ -v` (1209 tests at last count).

## 6. Order of work

1. `metrics.py` `through=` + **G2, G3, G4** → the causality property is pinned
   before anything depends on it
2. `real_data.py` flags, validation, split arithmetic, banner + **G1, G5**
3. two-pack wiring, the `params` stamps, the terminal layout + **G6**
4. `results.tsv` columns and backfill + **G7**
5. docs (§4.6)
6. one real GPU run on the night tape at `--train-frac 0.8` against the same run
   at `1.0`, confirming: identical report numbers, identical whole-data terminal
   block, and a train Calmar that differs

## 7. Open — for you, outside this folder

The three `../autoresearch_trading_plan_{night,day,fullday}.md` procedures and
their index live at the repo root and are **not** editable from here (CLAUDE.md;
the reach was declined 2026-09-18). Nothing in this plan changes them, so
**until the option is added there, every future iteration runs at the default
1.0 and the held-out tail does not exist.** What those files need:

- the `--train-bars` value each session's loop runs at, pinned like `--warmup-bars`
- the instruction that the recorded Calmar is the **train** figure
- a line in the freeze section forbidding a search from moving its own split

## Related

[SPEC.md](SPEC.md) §4.8 · [DAY_SESSION_PLAN.md](DAY_SESSION_PLAN.md) 2.2 (why a
fraction is not a split) · [docs/real_data.md](docs/real_data.md) ·
[docs/metrics.md](docs/metrics.md) · [AUTORESEARCH_FREEZE.md](AUTORESEARCH_FREEZE.md)
