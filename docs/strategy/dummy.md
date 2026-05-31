# [strategy/dummy.py](../../strategy/dummy.py)

**Role:** Phase-1 sanity emitter — alternates BUY/SELL on every call (SPEC §6 Phase 1).
**Pipeline:** a concrete [SignalEmitter](base.md), no model required.
**Depends on:** [events.py](../events.md), [strategy/base.py](base.md)   **Used by:** [run.py](../run.md) (default emitter), [tests/](../tests.md)

## Responsibility
Emits a deterministic alternating signal, ignoring the forecast entirely. Combined
with the Trader's no-flip rule, it produces a clean open-then-close round-trip every
two forecast boundaries — ideal for hand-verifying fills and fee math.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `DummyAlternatingEmitter(start="BUY")` | class | infinite BUY/SELL cycle |
| `emit(forecast=None)` | method | next side in the cycle |

## Key points / gotchas
- Built on `itertools.cycle` → it carries **per-run iterator state**. Reusing one
  instance across sweep iterations resumes mid-cycle and silently corrupts results;
  use the `make_emitter` factory pattern in [fee_sensitivity_sweep](../run.md).
- It's the default emitter when `run_backtest` is called with no forecaster
  (the Phase-1 path).

## Related
[strategy/base.py](base.md) · [threshold.py](threshold.md) · [run.py](../run.md) · [SPEC §6](../../SPEC.md)
