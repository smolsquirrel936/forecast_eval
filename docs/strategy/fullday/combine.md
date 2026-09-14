# [strategy/fullday/combine.py](../../../strategy/fullday/combine.py)

**Role:** FULL-DAY's forecast-figure combination — the stride consensus (iter03, deepened iter05).
**Pipeline:** a subclass of the package's [emitter.py](emitter.md), owned by the FULL-DAY package.
**Depends on:** [emitter.py](emitter.md), [events.py](../../events.md)   **Used by:** [strategy/fullday/wiring.py](wiring.md)

## Responsibility
Toto2 fires one forecast per stride boundary and stride == horizon (30), so
consecutive forecasts are successive vantage points on overlapping windows. This
module combines the two headline figures — `predicted_return` and `prob_up` —
across the last few boundaries before the banded emitter's gates see them: each
figure is averaged over its window, and the *averaged* figures must clear the
band and the probability gate.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `RET_K` / `PROB_K` | constant | 3 / 3 (iter05) — how many boundaries each figure averages |
| `ConsensusThresholdEmitter(*, ret_k=RET_K, prob_k=PROB_K, **kw)` | class | the banded emitter over combined figures |
| `_decide(forecast)` | method | rewrite the two figures to their means, delegate to the inherited gates |

## Provenance
**iter03** (user-directed research: "combine the forecast figure from Toto2"),
two-sided trader, fee 0.0002, one precompute per probe (2026-09-09):

| cells (ret_K/prob_K) | n | net | dd | full Calmar |
|---|---|---|---|---|
| 1/1 (iter02 baseline) | 206 | +31931.5 | -5201.8 | 6.139 |
| **2/2 (iter03, shipped)** | **154** | **+34422.6** | **-3621.3** | **9.505** |
| 2/1 (return only) | 145 | +33625.9 | -5369.0 | 6.263 |
| 1/2 (prob only) | 193 | +29633.2 | -6792.5 | 4.363 |
| 3/3 | 119 | +32021.3 | -4474.5 | 7.156 |
| 4/4 | 106 | +29179.0 | -7590.7 | 3.844 |

The lift needs **both** figures averaged — return-only is flat, prob-only is
*worse* than the baseline (a mean prob pulled toward 0.5 weakens the gate while
the return stays noisy). Fine band grid at 2/2
(0.00065/0.0007/0.00075/0.0008/0.00085 → 8.87/9.14/9.505/8.75/6.50) keeps the
swept 0.00075; 0.00075 also wins the worse-half rule. Fan-derived prob (prob
from the averaged 9-decile fan) probed 6.031 and path-consensus 5.736 — both
rejected; the mean-of-probs consensus stands.

**iter05** — after iter04 made the trader long-only, the optimum deepened:

| cells (long-only trader) | n | net | dd | worse half | full Calmar |
|---|---|---|---|---|---|
| 2/2 (iter04, shipped) | 100 | +37066.1 | -3378.6 | 2.319 | 10.971 |
| **3/3 (iter05, shipped)** | **77** | **+35050.0** | **-2708.9** | **3.616** | **12.939** |
| 4/4 | 63 | +33303.6 | -4698.0 | 1.890 | 7.089 |
| 5/5 | 61 | +33924.9 | -4398.0 | 3.807 | 7.714 |

K=3/3 improves both halves (h1 3.616 / h2 9.573); the band grid at K=3 keeps
0.00075 (0.0006/0.0007/0.0008 → 7.05/8.54/9.99); time-stops do not compose with
K=3 (8000/16000/24000 → 8.85/8.63/9.56). K=3 is a hump peak (10.971 / 12.939 /
7.089) — flagged for manual-stop inspection.

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
  exactly, so extra knobs must live in the module (the convention night's and
  day's since-deleted `rsi.py` constants followed too). That resolver rule was
  relaxed to CLI ⊆ PARAMS on 2026-09-09 (LIVE_STRATEGY_ROUTING_PLAN D6), so these
  could now move into PARAMS.
- Stateful: `_hist` is a rolling deque, so a fresh instance per run is required.
  `wiring.make_strategy()` builds one each call for that reason.

## Related
[strategy/fullday/emitter.md](emitter.md) · [strategy/fullday/wiring.md](wiring.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)