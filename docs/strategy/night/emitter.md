# [strategy/night/emitter.py](../../../strategy/night/emitter.py)

**Role:** NIGHT's SignalEmitter — a per-session copy of the banded threshold emitter.
**Pipeline:** a concrete [SignalEmitter](../base.md), owned by the NIGHT package.
**Depends on:** [events.py](../../events.md), [strategy/base.py](../base.md)   **Used by:** [strategy/night/wiring.py](wiring.md)

## Responsibility
NIGHT's own copy of the banded `ThresholdEmitter`. It turns
`payload["predicted_return"]` into BUY/SELL/HOLD, gates on the directional
probability `prob_up`, and derives the conviction `strength` the Trader sizes on.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ThresholdEmitter(...)` | class | the banded emitter — same constructor as the shared original |
| `emit(forecast)` | method | BUY above the upper band, SELL below the lower, else HOLD |
| `emit_signal(forecast)` | method | `Signal(direction, strength)` |

## Key points / gotchas
- **This is a per-session copy**, created verbatim from
  [strategy/threshold.py](../threshold.md) at the split (STRATEGY_SPLIT_PLAN S1)
  with only the relative-import depth changed. It is NIGHT's to edit; changes here
  reach no other session.
- The shared original is **frozen and hash-guarded** by
  `tests/test_strategy_isolation.py`. Edit that file and all three sessions move
  at once — copy into your package instead.
- Constructed by [wiring.py](wiring.md) at NIGHT's pinned band (±0.00045 / `min_prob` 0.5). The four
  knobs `real_data.py` never exposed — `short_window`, `min_trend_ticks`,
  `path_momentum`, `trend_filter` — stay at this class's own defaults
  (3 / 3.0 / True / True), which is what every recorded run used.
- Stateful: `_short_prices` is a rolling deque, so a fresh instance per run is
  required. `wiring.make_strategy()` builds one each call for that reason.

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
