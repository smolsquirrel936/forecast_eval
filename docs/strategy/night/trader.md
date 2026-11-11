# [strategy/night/trader.py](../../../strategy/night/trader.py)

**Role:** NIGHT's Trader — a per-session copy of the signal-to-order state machine.
**Pipeline:** the last box of the NIGHT pipeline; built as a factory over `Execution`.
**Depends on:** [events.py](../../events.md), [execution.py](../../execution.md), [exits/base.py](../../exits/base.md)   **Used by:** [strategy/night/wiring.py](wiring.md), [run.py](../../run.md)

## Responsibility
NIGHT's own copy of the Trader state machine — the signal → order path, the
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
- **Two-sided, no divergence from the shared copy** (`night_iter30`). The
  `night_iter10` package this replaced carried a `long_only` entry veto;
  `autoresearch_night_1004` tried the same veto (iter26) and discarded it —
  dropping shorts raised the drawdown, because two-sided shorts were acting as
  fast exits from longs.
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
- **Roll carry (2026-11-11).** `rolling()` is the optional hook `live/runtime.py`
  enters around a roll's calls. The re-open it submits is marked, and when that
  OPEN fills on the closed position's side, `on_fill` restores the closed
  position's entry (price, bar, time) instead of stamping a fresh one — so the
  percent stop keeps its width and a take-profit trail keeps its best price
  across the roll, as on the backtest's spliced tape. A later signal or exit
  cancels the mark; the carry is used once. Same mechanism as day's and
  fullday's Traders.

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
