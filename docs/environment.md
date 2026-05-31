# [environment.py](../environment.py)

**Role:** Replays tick data chronologically and tags each tick DAY/NIGHT (SPEC §4.7).
**Pipeline:** Tick stream → **Environment** → `MarketEvent` → Execution.
**Depends on:** [events.py](events.md)   **Used by:** [run.py](run.md)

## Responsibility
Turns a tick `DataFrame` into a lazy stream of `MarketEvent`s in time order,
classifying each by TXF trading session. It owns chronological ordering and
session membership — nothing about orders or PnL.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `classify_session(ts)` | function | returns `"DAY"`, `"NIGHT"`, or `None` (off-hours) |
| `Environment(ticks, drop_non_session=True)` | class | validates + sorts the tick frame |
| `Environment.stream()` | method | yields `MarketEvent`s one at a time |
| `Environment.__len__` | method | tick count (used to size progress bars) |

## Key points
- **Session windows:** DAY 08:45–13:45, NIGHT 15:00–05:00 (next day). The night
  window wraps past midnight, so it's "after the open OR before next-day close",
  not a single range check.
- **Chronological order is the core invariant** — `__init__` always sorts; an
  out-of-order tick would break fills and the look-ahead defense.
- `stream()` pulls columns into plain Python lists first — far faster than
  per-row pandas access over millions of ticks (the hottest path in real runs).
- Off-hours ticks are dropped by default (`drop_non_session=True`);
  `False` labels them `"DAY"` as a fallback (for synthetic/test data).

## Related
[events.py](events.md) · [run.py](run.md) · [data/loader.py](data/loader.md) · [SPEC §4.7](../SPEC.md)
