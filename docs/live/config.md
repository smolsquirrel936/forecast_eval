# [live/config.py](../../live/config.py)

**Role:** The one declared, provenanced configuration a live run trades.
**Pipeline:** `PINNED` → forecaster · emitter · trader · rails (V3 entry point).
**Depends on:** [live/sessions.py](sessions.md), [strategy/threshold.py](../strategy/threshold.md)   **Used by:** the live entry point (V3), [tests/test_live_config.py](../tests.md)

## Responsibility
Holds the tuned configuration as **importable code with a source attached**, so a
live run cannot inherit a default that was never measured.

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
different setting. `wiring.py` is the **autoresearch** seam — its entrypoint no
longer exists, and the winning run came from `real_data.py` CLI flags — so it is
not a source of truth for live and is deliberately left untouched.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `LiveConfig(...)` | frozen dataclass | every knob a live run needs, plus provenance |
| `PINNED` | constant | the configuration to trade |
| `build_emitter()` | method | a **fresh** `ThresholdEmitter` per call |
| `build_forecaster(*, warmup_bars)` | method | `Toto2Forecaster`; imports torch lazily |
| `describe()` | method | one-line banner, provenance first |
| `warmup_days_default(*, emitter_prime_boundaries=500)` | method | calendar-day `--warmup-days` default, derived from `policy.bars_per_session()` — NIGHT 30, DAY 84 — DAY_SESSION_PLAN C6 |

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
  from the [backfill](backfill.md), so the entry point decides. `build_forecaster`
  takes it as an argument.
- **`build_emitter()` returns a fresh instance every call.** `ThresholdEmitter`
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
