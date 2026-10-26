# [strategy/hybrid/night_half/trader.py](../../../../strategy/hybrid/night_half/trader.py)

**Role:** HYBRID's NIGHT-half Trader — a copy of the signal-to-order state machine.
**Pipeline:** one of the two Traders behind [RoutedTrader](../router.md), on the run's one `Execution`.
**Depends on:** [events.py](../../../events.md), [execution.py](../../../execution.md), [exits/base.py](../../../exits/base.md)   **Used by:** [strategy/hybrid/wiring.py](../wiring.md)

## Responsibility
The NIGHT half's own copy of the Trader — signal → order, the no-flip rule
(SPEC §4.2), pending-order lifetime (§4.4), confidence-scaled aggression and
sizing, and the position mirror `ExitRule`s read.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Trader(execution, *, tick_size, aggression_ticks, max_position, max_aggression_ticks, allow_open)` | class | the state machine |
| `on_signal` / `submit_exit` / `on_fill` / `position_state` | method | as in [trader.py](../../../trader.md) |

## Key points / gotchas
- **Copied from `main:strategy/night/trader.py`** (a verbatim copy of
  [trader.py](../../../trader.md)) with only the import depth changed. The NIGHT
  half's to edit in its own iterations; the day half must never import it.
- **`allow_open=False` (hybrid_split_iter09, the committed default): the half
  never opens.** A signal while flat submits nothing, so the night half only
  ever closes inherited lots (`night_allow_open=true` restores the copied
  behavior). The §4.4 cancel still runs first, so a night signal still retires
  a resting order placed by either half.
- **It decides only in the NIGHT session but folds every fill.** [RoutedTrader](../router.md)
  sends `on_signal` / `submit_exit` here only when `market.session` is NIGHT, and
  `on_fill` to both halves, so a lot the day half opened is this half's to
  manage (and close) once the session changes.
- It shares the run's one `Execution` with the day half, so its cancel-first
  `on_signal` cancels whatever order is resting, whichever half placed it.

## Related
[strategy/hybrid/wiring.py](../wiring.md) · [strategy/hybrid/router.py](../router.md) · [strategy/night/ (the session package)](../../night/wiring.md)
