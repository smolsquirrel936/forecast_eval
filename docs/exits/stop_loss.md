# [exits/stop_loss.py](../../exits/stop_loss.py)

**Role:** Fixed stop-loss — exit when adverse excursion exceeds N ticks from entry.
**Pipeline:** a concrete [ExitRule](base.md).
**Depends on:** [events.py](../events.md), [exits/base.py](base.md)   **Used by:** [run.py](../run.md) (`demo_stop_loss`, Phase-4 demo)

## Responsibility
Closes the position once price moves a fixed number of ticks against the entry.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `FixedStopLoss(stop_loss_ticks, tick_size=1.0)` | class | the N-tick stop |
| `should_exit(position, market, bar_idx)` | method | True past the stop level |

## Key points
- The stop sits on the **adverse** side of entry, so the comparison flips with
  position side: long exits at `entry − offset`, short at `entry + offset`.
- Uses `<=` / `>=` so the stop fires exactly *at* the threshold.
- Construction rejects `stop_loss_ticks <= 0` (would trigger immediately or never).

## Related
[exits/base.py](base.md) · [time_stop.py](time_stop.md) · [trader.py](../trader.md) · [SPEC §6 Phase 3](../../SPEC.md)
