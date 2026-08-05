# [trader.py](../trader.py)

**Role:** The signal→order state machine (SPEC §4.2–4.4).
**Pipeline:** SignalEmitter → **Trader** → Execution.
**Depends on:** [events.py](events.md), [execution.py](execution.md), [exits/base.py](exits/base.md)
**Used by:** [run.py](run.md)

## Responsibility
Decides *what order to send* given a signal and the current position, enforcing
the no-flip rule. Also mirrors the open position's entry price/bar/timestamp so
`ExitRule`s can query it. It owns intent; [execution.py](execution.md) owns fills.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Trader(execution, tick_size, aggression_ticks, max_position=1, max_aggression_ticks=None)` | class | the state machine |
| `on_signal(direction, market)` | method | apply §4.2 rule; submit/cancel orders |
| `on_signal(signal, market)` | method | conviction-aware: scales entry offset & size by `signal.strength` |
| `submit_exit(market)` | method | cancel-then-submit a closing limit (exits + forced close) |
| `on_fill(side, qty, …)` | method | update position + entry tracking |
| `position_state()` | method | immutable `PositionState` snapshot, or `None` if flat |

## The §4.2 state machine
| Position | Signal | Action |
|---|---|---|
| flat | BUY / SELL | open in that direction at `current ± aggression` |
| long | SELL | **close** (never flip to short) |
| short | BUY | **close** (never flip to long) |
| any | same-dir while in position | no-op (no pyramiding in v1) |
| any | HOLD | no-op (but still cancels any pending) |

## Key invariants / gotchas
- **Every** new signal — *including HOLD* — first cancels any pending order
  (SPEC §4.4): a stale forecast's order can't outlive the forecast.
- `submit_exit` likewise cancels-then-submits, so a stale entry limit can't survive
  a forced close.
- Order price = `current ± aggression_ticks × tick_size`; positive aggression makes
  the limit marketable (the default).
- **Confidence-scaled entries** (`max_aggression_ticks`, `max_position`, from
  ORDER_PLACEMENT_IDEAS #1/#7): `_entry_offset(strength)` ramps the entry limit
  offset from `aggression_ticks` (base) to `max_aggression_ticks` by `strength`;
  `_entry_qty(strength)` scales the entry size up to `max_position` (floor 1).
  Closes always use base aggression and flatten the whole position, so the no-flip
  state machine returns cleanly to flat. With the defaults (`max_aggression_ticks
  = None`, `max_position = 1`) every order is byte-identical to v1.
- The Trader keeps its *own* position mirror, separate from [portfolio.py](portfolio.md),
  because it drives order logic and ExitRule queries.

## Related
[execution.py](execution.md) · [strategy/base.py](strategy/base.md) · [exits/base.py](exits/base.md) · [tests/test_trader.py](tests.md) · [SPEC §4.2](../SPEC.md)
