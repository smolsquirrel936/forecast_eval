# [strategy/fullday/emitter.py](../../../strategy/fullday/emitter.py)

**Role:** FULL-DAY's SignalEmitter — a per-session copy of the banded threshold emitter.
**Pipeline:** a concrete [SignalEmitter](../base.md), owned by the FULL-DAY package.
**Depends on:** [events.py](../../events.md), [strategy/base.py](../base.md)   **Used by:** [strategy/fullday/wiring.py](wiring.md)

## Responsibility
FULL-DAY's own copy of the banded `ThresholdEmitter`. It turns
`payload["predicted_return"]` into BUY/SELL/HOLD, gates on the directional
probability `prob_up`, and derives the conviction `strength` the Trader sizes on.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ThresholdEmitter(...)` | class | the banded emitter — same constructor as the shared original, plus `exit_threshold` |
| `bind_position(getter)` | method | install the live-position getter the exit band reads (decision 8) |
| `emit(forecast)` | method | BUY above the upper band, SELL below the lower, else HOLD |
| `emit_signal(forecast)` | method | `Signal(direction, strength)` |

## Key points / gotchas
- **This is a per-session copy**, created verbatim from
  [strategy/threshold.py](../threshold.md) at the split (STRATEGY_SPLIT_PLAN S1)
  with only the relative-import depth changed. It is FULL-DAY's to edit; changes here
  reach no other session.
- The shared original is **frozen and hash-guarded** by
  `tests/test_strategy_isolation.py`. Edit that file and all three sessions move
  at once — copy into your package instead.
- Constructed by [wiring.py](wiring.md) at FULL-DAY's pinned band (±0.001 / `min_prob` 0.65 (inherited from DAY)). The four
  knobs `real_data.py` never exposed — `short_window`, `min_trend_ticks`,
  `path_momentum`, `trend_filter` — stay at this class's own defaults
  (3 / 3.0 / True / True), which is what every recorded run used.
- **`exit_threshold` (reversal decision 8, `fullday_rev_iter16`):** while a
  position is bound and non-zero, an *opposite*-side signal clears at
  `|predicted_return| >= exit_threshold` instead of the full band; from flat and
  on the same side the full band applies. `wiring.make_trader` installs the
  binding (`bind_position(lambda: trader.position)`), so the emitter reads the
  trader's current signed position at each forecast — fills land before emit in
  the tick order, so there is no look-ahead. `min_prob`, the trend filter and the
  path check still gate the exit signal. The control value equals the band
  (0.001), at which the cuts are the pre-knob ones and runs are byte-identical;
  an unbound emitter behaves as today. The constructor rejects an asymmetric
  band (`buy != -sell`) or an `exit_threshold` outside `[0, buy_threshold]`.
- **Side effect of a narrowed exit band:** the trend filter stores a price
  whenever a signal passes the band, so a narrower exit band changes which
  prices its rolling window holds at the next entry.
- Stateful: `_short_prices` is a rolling deque, so a fresh instance per run is
  required. `wiring.make_strategy()` builds one each call for that reason.

## Related
[strategy/fullday/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
