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
| `SignalEmitter` | ABC | `emit(forecast) -> {"BUY","SELL","HOLD"}` (direction only) |
| `emit_signal(forecast)` | method | default wraps `emit` at full strength → `Signal(direction, strength=1.0)` |

## Key points
- `emit` takes `Optional[Forecast]` — model-less paths (the dummy emitter) pass
  `None`, and threshold-style emitters return `HOLD` on `None`.
- `emit_signal` is the conviction-aware entry point: distributional emitters
  (`ThresholdEmitter` with a `prob_up`-carrying forecast) override it to return a
  `Signal` whose `strength ∈ [0,1]` carries the forecast's confidence past the
  direction bottleneck; non-distributional emitters inherit the full-strength
  default and behave exactly as before. See [events.py](../events.md) for `Signal`.
- The Trader handles everything after the signal (no-flip rule, pending-order
  lifetime), so an emitter only expresses *direction* (and, via `strength`, how
  much to commit to it).

## Related
[threshold.py](threshold.md) · [dummy.py](dummy.md) · [trader.py](../trader.md) · [SPEC §3](../../SPEC.md)
