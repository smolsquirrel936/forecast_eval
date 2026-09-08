# [strategy/day/trader.py](../../../strategy/day/trader.py)

**Role:** DAY's Trader — a per-session copy of the signal-to-order state machine.
**Pipeline:** the last box of the DAY pipeline; built as a factory over `Execution`.
**Depends on:** [events.py](../../events.md), [execution.py](../../execution.md), [exits/base.py](../../exits/base.md)   **Used by:** [strategy/day/wiring.py](wiring.md), [run.py](../../run.md)

## Responsibility
DAY's own copy of the Trader state machine — the signal → order path, the
no-flip rule (SPEC §4.2), pending-order lifetime (§4.4), confidence-scaled
aggression and sizing, and the fill bookkeeping that feeds `ExitRule` queries.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Trader(execution, *, tick_size, aggression_ticks, max_position, max_aggression_ticks)` | class | the state machine |
| `on_signal(direction, market, *, strength)` | method | cancel-then-act; open from flat, close on an opposing signal, never flip |
| `submit_exit(market)` | method | closing limit for an `ExitRule` firing or a session-boundary forced close |
| `on_fill(...)` / `position_state()` | method | position mirror and the immutable snapshot `ExitRule`s read |

## Key points / gotchas
- **This is a per-session copy** of [trader.py](../../trader.md), verbatim at the
  split with only the import depth changed (STRATEGY_SPLIT_PLAN S1). The shared
  original is frozen and hash-guarded.
- **Diverged at autoresearch `iter02`: long-only entries.** A SELL from flat
  submits nothing — no short is ever opened — while a SELL while long still
  closes. Grounds: the iter01 baseline on the day tape split 146 longs
  (+2,592 pts net) against 184 shorts (−19,784 pts) over 2011–2026, a window in
  which the tape 5x'd; selling overbought into that rise was the entire loss.
  The shared Trader and the night/fullday copies still open shorts — this
  package-level divergence is exactly what the split exists to allow.
- It is handed to `run_backtest` as a **factory**, not an instance —
  `make_trader(execution)` — because `Execution` is built inside the run
  (`fee_rate` is a run parameter). See [run.py](../../run.md) §2.1 of the split
  plan.
- The v1 invariants still hold here and the shared tests still cover them: no
  pyramiding, no flipping, and *every* signal (HOLD included) cancels any
  resting order first.
- `on_fill` has **no average-entry branch**: anything above `max_position=1` is
  unsound rather than merely risky, which is why the live rails refuse it at
  start-up.

## Related
[strategy/day/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
