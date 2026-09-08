# [strategy/fullday/trader.py](../../../strategy/fullday/trader.py)

**Role:** FULL-DAY's Trader — a per-session copy of the signal-to-order state machine.
**Pipeline:** the last box of the FULL-DAY pipeline; built as a factory over `Execution`.
**Depends on:** [events.py](../../events.md), [execution.py](../../execution.md), [exits/base.py](../../exits/base.md)   **Used by:** [strategy/fullday/wiring.py](wiring.md), [run.py](../../run.md)

## Responsibility
FULL-DAY's own copy of the Trader state machine — the signal → order path, the
no-flip rule (SPEC §4.2), pending-order lifetime (§4.4), confidence-scaled
aggression and sizing, and the fill bookkeeping that feeds `ExitRule` queries.

**Diverged at iter04: this Trader is long-only.** A SELL from flat opens no
short (it still cancels any resting order, §4.4); a SELL while long still
closes. Grounded in the iter03 side split — 95 longs at +30,220 pts net
(avg +318) vs 59 shorts at +4,203 (avg +71) on a tape that rose ~5x across the
eval window — plus the no-flip freeing effect: while short, a BUY only closes,
so dropping short entries lets BUY crossings open longs immediately. Probed:
net +34,423 → +37,066, dd -3,621 → -3,379, full-window Calmar 9.505 → 10.971
(n 154 → 100). The shared Trader and the day/night copies still open shorts;
the divergence is pinned by [tests/test_fullday_trader.py](../../../tests/test_fullday_trader.py).

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
[strategy/fullday/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
