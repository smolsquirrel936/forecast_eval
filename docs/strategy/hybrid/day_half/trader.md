# [strategy/hybrid/day_half/trader.py](../../../../strategy/hybrid/day_half/trader.py)

**Role:** HYBRID's DAY-half Trader — a copy of the signal-to-order state machine.
**Pipeline:** one of the two Traders behind [RoutedTrader](../router.md), on the run's one `Execution`.
**Depends on:** [events.py](../../../events.md), [execution.py](../../../execution.md), [exits/base.py](../../../exits/base.md)   **Used by:** [strategy/hybrid/wiring.py](../wiring.md)

## Responsibility
The DAY half's own copy of the Trader — signal → order, the no-flip rule
(SPEC §4.2), pending-order lifetime (§4.4), confidence-scaled aggression and
sizing, and the position mirror `ExitRule`s read.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Trader(execution, *, tick_size, aggression_ticks, max_position, max_aggression_ticks)` | class | the state machine |
| `on_signal` / `submit_exit` / `on_fill` / `position_state` | method | as in [trader.py](../../../trader.md) |

## Key points / gotchas
- **Copied from `main:strategy/day/trader.py`** (a verbatim copy of
  [trader.py](../../../trader.md)) with only the import depth changed. The DAY
  half's to edit in its phase; the night half must never import it.
- **It decides only in the DAY session but folds every fill.** [RoutedTrader](../router.md)
  sends `on_signal` / `submit_exit` here only when `market.session` is DAY, and
  `on_fill` to both halves, so a lot the night half opened is this half's to
  manage (and close) once the session changes.
- It shares the run's one `Execution` with the night half, so its cancel-first
  `on_signal` cancels whatever order is resting, whichever half placed it.

## Related
[strategy/hybrid/wiring.py](../wiring.md) · [strategy/hybrid/router.py](../router.md) · [strategy/day/ (the session package)](../../day/wiring.md)
