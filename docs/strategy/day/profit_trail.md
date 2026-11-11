# [strategy/day/profit_trail.py](../../../strategy/day/profit_trail.py)

**Role:** DAY's take-profit trail — an exit that arms only once the trade is in profit.
**Pipeline:** concrete [ExitRule](../../exits/base.md) implementations, owned by the DAY package.
**Depends on:** [events.py](../../events.md), [exits/base.py](../../exits/base.md)   **Used by:** [strategy/day/wiring.py](wiring.md)

## Responsibility
Protect part of a winner's open gain without touching trades that never earned
anything. The rule is inert until the best price since entry is `arm_pct` of
the entry in the position's favour, then exits on a give-back from that best
price. `AnyOf` composes it with the shared percent stop.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ProfitTrail(arm_pct, *, giveback_frac=None, trail_pct=None)` | class | arm at `arm_pct` in favour; then exit when the open gain falls to `(1-giveback_frac)` of its max, or when price is `trail_pct` off the best price — exactly one shape |
| `AnyOf(rules)` | class | fire when any wrapped rule fires (copied from `strategy/fullday/exits.py`) |

## Key points / gotchas
- **Why an arm.** The day_trail loop (2026-10-11) killed every pure trail armed
  from the entry fill: it fires at V-bottoms. A trade that never reaches
  `arm_pct` can never fire this rule.
- **Stateful, therefore `adoptable = False` — but restorable.** The whole state
  is the best price since entry (`state_keys = ("best_price",)`), which live
  writes to `exit_state_<slug>.json` and `--resume` restores, as fullday's rules do.
- **A live roll restarts it.** DAY's Trader does not carry the entry across a
  roll (fullday's does), so the re-open is a new entry identity.
- **Knobs** (`wiring.py`): `tp_arm_pct`, `tp_giveback_frac`, `tp_trail_pct`,
  plus the Trader's `tp_cooldown_signals` (skip same-side opens for K signals
  after a profitable exit-rule close). All off by default.
