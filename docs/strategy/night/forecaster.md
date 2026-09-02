# [strategy/night/forecaster.py](../../../strategy/night/forecaster.py)

**Role:** NIGHT's Forecaster construction site.
**Pipeline:** builds the `Toto2Forecaster` the NIGHT pipeline runs.
**Depends on:** [forecaster/toto2.py](../../forecaster/toto2.md)   **Used by:** [strategy/night/wiring.py](wiring.md)

## Responsibility
NIGHT's forecaster construction site. `make_forecaster(...)` builds the
`Toto2Forecaster` this session runs, from the knobs `wiring.PARAMS` declares.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `make_forecaster(*, warmup_bars, stride_bars, horizon_bars, context_length, checkpoint, device="auto", bar_freq=None, signal_step="last")` | function | the session's `Forecaster` |

## Key points / gotchas
- **Thin by design, unlike `emitter.py` and `trader.py`.**
  [forecaster/toto2.py](../../forecaster/toto2.md) is 23 KB and most searches
  move *parameters*, not model internals, so this starts as a construction over
  the shared frozen class. To change internals, copy the class body in here
  first — same copy-on-write rule, same hash guard
  (STRATEGY_SPLIT_PLAN §3.2).
- `torch` and `toto2` are imported **inside the function**, so this package
  imports cleanly on a machine with neither — which is what lets the isolation
  test and `--help` work without a GPU.
- This file is what `results.tsv`'s `forecaster_sha` column hashes. A change here
  is a **forecast-stream** change: runs either side of it are no longer
  comparable on a fixed stream, and `--precompute` no longer amortises across
  iterations (STRATEGY_SPLIT_PLAN §2.4).
- `warmup_bars` is a property of the *run* (the train/eval split), not of the
  strategy, which is why it is a parameter rather than a `PARAMS` entry.

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
