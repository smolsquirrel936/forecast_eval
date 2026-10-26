# [strategy/fullday/trader.py](../../../strategy/fullday/trader.py)

**Role:** FULL-DAY's Trader — a per-session copy of the signal-to-order state machine.
**Pipeline:** the last box of the FULL-DAY pipeline; built as a factory over `Execution`.
**Depends on:** [events.py](../../events.md), [execution.py](../../execution.md), [exits/base.py](../../exits/base.md)   **Used by:** [strategy/fullday/wiring.py](wiring.md), [run.py](../../run.md)

## Responsibility
FULL-DAY's own copy of the Trader state machine — the signal → order path, the
no-flip rule (SPEC §4.2), pending-order lifetime (§4.4), confidence-scaled
aggression and sizing, and the fill bookkeeping that feeds `ExitRule` queries.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Trader(execution, *, tick_size, aggression_ticks, max_position, max_aggression_ticks, entry_min_prob, full_confidence_prob, reentry_cooldown_signals)` | class | the state machine |
| `on_signal(direction, market, *, strength)` | method | cancel-then-act; open from flat (entry-gated), close on an opposing signal, never flip |
| `submit_exit(market)` | method | closing limit for an `ExitRule` firing or a session-boundary forced close |
| `on_fill(...)` / `position_state()` | method | position mirror and the immutable snapshot `ExitRule`s read |

## Key points / gotchas
- **This is a per-session copy** of [trader.py](../../trader.md), verbatim at the
  split with only the import depth changed (STRATEGY_SPLIT_PLAN S1). The shared
  original is frozen and hash-guarded. **Diverged in `fullday_rev_iter02`:**
  `on_signal` carries an **entry-only probability gate** — from flat, a signal
  whose `strength` is below the floor implied by `entry_min_prob`
  (`(entry_min_prob - 0.5) / (full_confidence_prob - 0.5)`, the inverse of the
  emitter's conviction ramp) submits nothing, while the §4.4 resting-order
  cancel still runs. Closes are never gated: an opposite signal is the only way
  out short of the stop, and a third of historical exits carried a directional
  probability below 0.65. At `entry_min_prob` 0.50 the floor is 0.0 and the
  gate is inert — byte-identical to the pre-gate copy.
- **Diverged in `fullday_rev_iter33` (reversal decision 12):** the Trader
  carries a **re-entry cooldown** — after a fill that flattens a position, an
  OPEN on the closed position's side is skipped for
  `reentry_cooldown_signals` signals (HOLDs count toward the budget; the
  opposite side opens freely). The cooldown is armed in `on_fill` at the
  flattening fill — the moment the re-entry risk begins — and every signal
  seen while flat and cooling consumes one of the K. At the default 0 the
  cooldown never arms, so runs are byte-identical to the pre-knob copy. The
  constructor rejects a negative or non-int count.
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
