# [strategy/fullday/combine.py](../../../strategy/fullday/combine.py)

**Role:** FULL-DAY's forecast-figure combination — the stride consensus (iter03).
**Pipeline:** a subclass of the package's [emitter.py](emitter.md), owned by the FULL-DAY package.
**Depends on:** [emitter.py](emitter.md), [events.py](../../events.md)   **Used by:** [strategy/fullday/wiring.py](wiring.md)

## Responsibility
Toto2 fires one forecast per stride boundary and stride == horizon (30), so
consecutive forecasts are two vantage points on overlapping windows. This module
combines the two headline figures — `predicted_return` and `prob_up` — across the
last two boundaries before the banded emitter's gates see them: each figure is
averaged over its window, and the *averaged* figures must clear the band and the
probability gate.

## Public surface
| Symbol | Kind | One-liner |
|---|---|
| `RET_K` / `PROB_K` | constant | 2 / 2 — how many boundaries each figure averages |
| `ConsensusThresholdEmitter(*, ret_k=RET_K, prob_k=PROB_K, **kw)` | class | the banded emitter over combined figures |
| `_decide(forecast)` | method | rewrite the two figures to their means, delegate to the inherited gates |

## Provenance
**iter03, user-directed research ("combine the forecast figure from Toto2").**
Scratch-probed on the full-day tape at the swept band, fee 0.0002, one precompute
(2026-09-09), replayed off the cached stream:

| cells (ret_K/prob_K) | n | net | dd | full Calmar |
|---|---|---|---|---|
| 1/1 (iter02 baseline) | 206 | +31931.5 | -5201.8 | 6.139 |
| **2/2 (shipped)** | **154** | **+34422.6** | **-3621.3** | **9.505** |
| 2/1 (return only) | 145 | +33625.9 | -5369.0 | 6.263 |
| 1/2 (prob only) | 193 | +29633.2 | -6792.5 | 4.363 |
| 3/3 | 119 | +32021.3 | -4474.5 | 7.156 |
| 4/4 | 106 | +29179.0 | -7590.7 | 3.844 |

The lift needs **both** figures averaged — return-only is flat, prob-only is
*worse* than the baseline (a mean prob pulled toward 0.5 weakens the gate while
the return stays noisy). The band grid at 2/2 (0.00045/0.0006/0.00075/0.001/0.0015
→ 2.92/6.05/9.50/1.45/1.67) keeps the swept 0.00075 as a clear peak; 0.00075 also
wins on the worse-half rule (2.864 vs 2.372/0.871/-0.041/0.440).

## Key points / gotchas
- The parent's every other gate (band, `min_prob`, strength ramp, trend filter,
  path momentum) reads the **combined** figures; tape-level fields (`last_price`,
  `median_path`) stay per-boundary. That is exactly the probed configuration.
- Boundaries with a missing `predicted_return` HOLD **without entering the
  history** — an uninformative boundary never dilutes the consensus.
- During the first boundaries after warm-up the means run over what has been seen
  (fewer than K samples); this matches the probed behavior.
- `ret_k`/`prob_k` are **module constants, not PARAMS knobs** — `real_data.py`'s
  resolver requires a package's declared knob set to equal the CLI's twelve
  exactly, so extra knobs must live in the module (same convention as night's
  `rsi.py` constants).
- Stateful: `_hist` is a rolling deque, so a fresh instance per run is required.
  `wiring.make_strategy()` builds one each call for that reason.

## Related
[strategy/fullday/emitter.md](emitter.md) · [strategy/fullday/wiring.md](wiring.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)