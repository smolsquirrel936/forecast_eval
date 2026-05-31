# [exits/time_stop.py](../../exits/time_stop.py)

**Role:** Time stop — exit after N bars in the position.
**Pipeline:** a concrete [ExitRule](base.md).
**Depends on:** [events.py](../events.md), [exits/base.py](base.md)   **Used by:** available under the `ExitRule` interface (not wired into a demo)

## Responsibility
A purely time-based exit: caps how long capital sits in any one trade, independent
of price.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `TimeStop(max_bars)` | class | the holding-period cap |
| `should_exit(position, market, bar_idx)` | method | True once `bar_idx − entry_bar_idx >= max_bars` |

## Key points
- Measures elapsed time in **bar indices**, matching the run loop's notion of time
  (no wall-clock arithmetic).
- Construction rejects `max_bars <= 0` (would close on/before the entry bar).

## Related
[exits/base.py](base.md) · [stop_loss.py](stop_loss.md) · [trader.py](../trader.md) · [SPEC §6 Phase 3](../../SPEC.md)
