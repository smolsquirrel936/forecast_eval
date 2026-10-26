# [strategy/hybrid/day_half/emitter.py](../../../../strategy/hybrid/day_half/emitter.py)

**Role:** HYBRID's DAY-half SignalEmitter — a copy of the banded threshold emitter.
**Pipeline:** a concrete [SignalEmitter](../../base.md), reached only through [RoutedEmitter](../router.md).
**Depends on:** [events.py](../../../events.md), [strategy/base.py](../../base.md)   **Used by:** [strategy/hybrid/wiring.py](../wiring.md)

## Responsibility
The DAY half's own `ThresholdEmitter`. It turns `payload["predicted_return"]`
into BUY/SELL/HOLD, gates on `prob_up`, and derives the conviction `strength`
the Trader sizes on. Its decision is used only for DAY-session forecasts.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ThresholdEmitter(...)` | class | the banded emitter — same constructor as the shared original |
| `emit(forecast)` / `emit_signal(forecast)` | method | direction / `Signal(direction, strength)` |

## Key points / gotchas
- **Copied from `main:strategy/day/emitter.py`** at the DAYNIGHT_TOTO2_RESET_PLAN
  reset (itself a verbatim copy of [strategy/threshold.py](../../threshold.md)), with
  only the import depth changed. It is the DAY half's to edit during its phase of
  `autoresearch_trading_plan_hybrid.md`; the night half must never import it.
- Built by [wiring.py](../wiring.md) at ±0.001 / `min_prob` 0.65 (the `day_*` knobs).
- **It sees every forecast, DAY and NIGHT, but decides only for DAY.**
  [RoutedEmitter](../router.md) hands each forecast to both halves and keeps the
  decision of the forecast's session's half. So its `_short_prices` deque follows
  the same continuous stream as its forecaster, and any state added here counts
  across both sessions. Stateful: a fresh instance per run.

## Related
[strategy/hybrid/wiring.py](../wiring.md) · [strategy/hybrid/router.py](../router.md) · [strategy/day/ (the session package)](../../day/wiring.md)
