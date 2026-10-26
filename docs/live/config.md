# [live/config.py](../../live/config.py)

**Role:** A live deployment's identity — which sessions it trades, its provenance, and the run-level values that are not strategy.
**Pipeline:** `PINNED` → [runtime.py](runtime.md) → `strategy.wiring.make_strategy` → forecaster · emitter · trader · rails.
**Depends on:** [live/sessions.py](sessions.md), [strategy/wiring.py](../strategy/wiring.md)   **Used by:** the live entry point (V3), [tests/test_live_config.py](../tests.md)

## Responsibility
Names a deployment and carries its provenance. It **no longer holds strategy
values** — LIVE_STRATEGY_ROUTING_PLAN D2 moved those to
`strategy/<session>/wiring.py:PARAMS`, which is now the single source for live
and the backtest alike.

## Why it exists
Before this module the tuned configuration existed only as a *command string* in
`results.tsv` plus a scatter of `params.json` files. Three separate defaults
contradicted it, and none of the mismatches raises:

| knob | `Toto2Forecaster` | `real_data.py` CLI | `strategy/wiring.py` | **tuned** |
|---|---|---|---|---|
| checkpoint | `2.5B` | `313m` | — | **`1B`** |
| buy / sell band | — | ±0.0005 | ±0.0005 | **±0.00045** |
| `min_prob` | — | `None` | `None` | **0.5** |

The checkpoint is the sharpest: `313m` versus `1B` is a *different model*, not a
different setting.

**That table is history — corrected 2026-09-09.** It described `wiring.py` as an
autoresearch seam whose entrypoint no longer existed, and concluded it was "not a
source of truth for live". The opposite now holds: `make_strategy` **is** the
entrypoint, and it is the *only* source of truth for live. The column can no
longer disagree with the tuned value, because there is nothing left here for it
to disagree with. The lesson the table teaches — that a second declaration of a
strategy value is a defect waiting to happen — is why the fields were deleted.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `LiveConfig(...)` | frozen dataclass | provenance, `policy`, and the run-level values that are not strategy (`tick_size`, `fee_rate`, `contract_multiplier`) |
| `PINNED` / `PINNED_DAY` / `PINNED_FULLDAY` / `PINNED_HYBRID` | constants | the four deployments |
| `PACKAGE_SESSIONS` / `HALVES` | dicts | which sessions each package trades; the halves of a routed package (`hybrid`) |
| `CONFIGS` / `config_named(name)` | dict / function | the registry `--config` selects from |
| `is_tuned` | property | False when `source_commit`/`source_calmar` are None — what `cli.py` refuses on |
| `strategy_package` | property | `"day"`/`"night"`/`"fullday"`, **derived from the policy**, or the named `package` (`"hybrid"`), checked against `PACKAGE_SESSIONS` at construction |
| `describe(params=None)` | method | one-line banner, provenance first. Pass the run's **resolved** knobs and it renders those; omit them and it describes the deployment. Format is fixed — `tools/session_artifacts.py:_DESCRIBE_RE` parses it back out of recorded `meta` headers |
| `warmup_days_default(*, emitter_prime_boundaries=500)` | method | calendar-day `--warmup-days` default, derived from `policy.bars_per_session()` — NIGHT 30, DAY 84 — DAY_SESSION_PLAN C6 |

## No factories here any more (LIVE_STRATEGY_ROUTING_PLAN D2)

`build_emitter`, `build_forecaster` and `build_trader` were **removed on
2026-09-09**, along with the ~19 strategy fields they read. [runtime.py](runtime.md)
calls `strategy.wiring.make_strategy(cfg.strategy_package, warmup_bars=0,
tick_size=cfg.tick_size, **overrides)` once in `__init__` and keeps the resolved
knobs as `strategy_params`.

**Why they had to go.** `build_emitter` imported `.emitter.ThresholdEmitter` *by
name*. A package whose `make_strategy` built something else was therefore
invisible to live — which is not hypothetical: `strategy/fullday/combine.py`'s
consensus emitter carried an entire research loop (Calmar 6.14 → 12.94) while
being unreachable from a live run, and `strategy/night` and `strategy/day` built
an RSI strategy from `a8ddd85` until DAYNIGHT_TOTO2_RESET_PLAN that live never saw. Worse,
`build_trader` *did* route through the package, so a live fullday run would have
been one package's emitter on another's trader.

`tests/test_live_parity_sessions.py` could not catch it: it built **both** sides
with `cfg.build_emitter()`, comparing the copy against itself. The replacement
assertion is
`test_live_config.py::test_live_builds_the_package_the_backtest_would_run`.

`strategy_package` survives, and is still a **property derived from
`policy.sessions`** rather than a stored field. A stored one could disagree with
the policy, and a deployment running one session's policy against another
session's components is the C2 defect `--config` exists to prevent, re-entering
one level down.

> **Consequence, live today:** night and day deployments trade their pinned Toto2
> threshold strategies again (`PINNED` / `PINNED_DAY` knobs, now declared only in
> `strategy/<session>/wiring.py`), because that is what their packages build. They
> traded RSI between the routing (D9) and DAYNIGHT_TOTO2_RESET_PLAN, which
> restored both packages to `20767bb`.

## `PINNED_FULLDAY` is registered and UNTUNED

It carries `source_commit=None` and `source_calmar=None`, which is the declared
meaning of unprovenanced, and `is_tuned` is False. Its band (±0.001, `min_prob`
0.65) is **DAY's, inherited because full-day shares DAY's 12:45 roll instant —
not chosen for the full-day tape**. There is no `results.tsv` row for it and it
has never traded. (The fullday loop ran iter00–05 on a swept band and a consensus
emitter; DAYNIGHT_TOTO2_RESET_PLAN §8 restored the package to these inherited
values on 2026-09-14.)

*Updated 2026-09-14:* [live/cli.py](cli.md) **no longer** refuses `--config
fullday`. LIVE_STRATEGY_ROUTING_PLAN D1 removed the `is_tuned` refusal because
`simulation=True` is hardcoded; `is_tuned` survives as a record. Backtesting
remains the supported path: `real_data.py --strategy fullday`.

W5a is why the inheritance is not evidence: NIGHT's band on the day tape scores
Calmar 0.4129 against 3.6006. There is no reason to expect DAY's band to transfer
to full-day any better. The work that replaces the stub is `tools/band_sweep.py`
on `TXF_RPT_minute_fullday.csv`.

## `PINNED_HYBRID` (registered 2026-10-27)

Hybrid trades DAY+NIGHT like fullday, so the sessions cannot name its package:
it sets `package="hybrid"` (refused at construction unless the policy is
DAY+NIGHT) and `SessionPolicy.both(slug="hybrid")`, so its ledger, bar cache,
wire, prices and run folders are `*_hybrid*`, never fullday's `day_night`.
`describe()` renders it half by half (`day[...] night[...]`), because its
unprefixed band and checkpoint knobs are None; that line does not match
`tools/session_artifacts.py`'s `_DESCRIBE_RE`, so a rebuild of a hybrid session
returns None and its own `params.json` is the record. On `main` it is untuned
(Step 0); the live branch restates `source_note` for the adopted result.

## What is pinned, and to what
`results.tsv` row **`4282d33`** — Calmar **3.6006**, 235 trades, net **+24 135.60**
points, max drawdown **−6 703.24**, on `TXF_RPT_minute_night.csv@2026-09-01`.

**Re-pinned 2026-09-01 (TAPE_REBUILD_PLAN T6, decision D1), and not one
executable field moved.** The evaluated tape was rebuilt to splice where the
deployment actually rolls; the same parameters score **16.3122** on the
pre-rebuild tape and **3.6006** on the corrected one. Only the four `source_*`
provenance fields changed, so a live run places identical orders before and
after — which is why this did **not** restart the V5 soak clock, decided
explicitly rather than by default.

- `e44a047` (Calmar 13.6742, `...@2026-05-15`) stays in the ledger as history.
  The drift guards scope "the best known row" by **session and tape vintage**,
  so a number no file on disk can reproduce cannot re-pin a deployment.
- **3.6006 is not a clean 4.5× decay measurement.** The rebuild inserts bars
  inside the eval window, so the forecast grid necessarily re-phased, and
  `DAY_SESSION_PLAN.md` §2.2 measured phase *alone* moving 13.6742 → 8.7651.
  `TAPE_REBUILD_PLAN.md` §5.1 has the control run and the caveat.
- **NIGHT policy**, because every tuning run used the night file (verified from
  `params.json` and from all 174 trades of the latest run being tagged NIGHT).
- **`forced_close=False` kept deliberately.** Median holding period is 4.5 days;
  closing at every session boundary would truncate ~89 % of trades — a different
  strategy, not a safety setting.

## Key points / gotchas
- **`warmup_bars` is *not* pinned**, and that is intentional. In the backtest it
  is the train/eval split — a property of that experiment. Live, warm-up comes
  from the [backfill](backfill.md), so the entry point decides. `make_strategy`
  takes it as an argument.
- **`make_strategy()` returns a fresh instance every call.** `ThresholdEmitter`
  carries rolling deques; sharing one across runs silently produces wrong numbers.
- **The drift guard is the point.** [tests/test_live_config.py](../tests.md)
  re-parses `results.tsv` and asserts the pinned thresholds, `min_prob` and Calmar
  match the row they claim to come from.
- **One test fails on purpose when research wins.**
  `test_pinned_row_is_still_the_best_known_config` goes red as soon as a higher
  Calmar appears in `results.tsv`. Promoting a new config to live should be an
  explicit act; resolve it by re-pinning `PINNED`, not by relaxing the test.

## Related
[live/sessions.py](sessions.md) · [live/backfill.py](backfill.md) · [strategy/threshold.py](../strategy/threshold.md) · [strategy/wiring.py](../strategy/wiring.md) · [SHIOAJI_PLAN.md](../../SHIOAJI_PLAN.md)
