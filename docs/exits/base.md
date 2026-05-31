# [exits/base.py](../../exits/base.py)

**Role:** The `ExitRule` abstract base + the `PositionState` snapshot (SPEC §3, §4.1 step 2).
**Pipeline:** queried by the Trader each tick a position is open.
**Depends on:** [events.py](../events.md)   **Used by:** [stop_loss.py](stop_loss.md), [time_stop.py](time_stop.md), [trader.py](../trader.md), [run.py](../run.md)

## Responsibility
Defines the tiny interface every risk exit implements, plus the immutable
`PositionState` view handed to it. One interface covers stop-loss, time-stop,
take-profit, etc.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `PositionState` | dataclass | read-only open-position view: side, size, entry price/bar/timestamp |
| `ExitRule` | ABC | `should_exit(position, market, bar_idx) -> bool` |

## Key points
- An ExitRule expresses only the **decision** (close now or not); the Trader owns
  the actual order submission (`submit_exit`).
- `PositionState` is a *snapshot*, not the live Trader — a rule can read entry
  context but cannot mutate trading state.
- Checked **every tick** (not just on forecast boundaries), so a stop can fire
  intra-stride.

## Related
[stop_loss.py](stop_loss.md) · [time_stop.py](time_stop.md) · [trader.py](../trader.md) · [SPEC §4.1](../../SPEC.md)
