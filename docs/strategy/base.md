# [strategy/base.py](../../strategy/base.py)

**Role:** The `SignalEmitter` abstract base (SPEC §3).
**Pipeline:** Forecaster → **SignalEmitter** → Trader.
**Depends on:** [events.py](../events.md)   **Used by:** [dummy.py](dummy.md), [threshold.py](threshold.md), [run.py](../run.md)

## Responsibility
Defines the single decision point that turns a `Forecast` into `BUY` / `SELL` /
`HOLD`. This is the seam that separates *what the model predicts* from *what we
decide to do*, so the model and the strategy are independently swappable.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `SignalEmitter` | ABC | `emit(forecast) -> {"BUY","SELL","HOLD"}` |

## Key points
- `emit` takes `Optional[Forecast]` — model-less paths (the dummy emitter) pass
  `None`, and threshold-style emitters return `HOLD` on `None`.
- The Trader handles everything after the signal (no-flip rule, pending-order
  lifetime), so an emitter only expresses *direction*.

## Related
[threshold.py](threshold.md) · [dummy.py](dummy.md) · [trader.py](../trader.md) · [SPEC §3](../../SPEC.md)
