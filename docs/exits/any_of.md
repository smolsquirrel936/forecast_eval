# [exits/any_of.py](../../exits/any_of.py)

**Role:** AnyOf — compose exit rules into one that fires when any member fires; `any_of` is the builder the packages use.
**Pipeline:** wraps concrete [ExitRule](base.md)s so a package can hand `run.py` / `live/loop.py` its one exit rule.
**Depends on:** [events.py](../events.md), [exits/base.py](base.md)   **Used by:** [strategy/day/wiring.py](../strategy/day/wiring.md), [strategy/night/wiring.py](../strategy/night/wiring.md), [strategy/fullday/wiring.py](../strategy/fullday/wiring.md), [strategy/hybrid/wiring.py](../strategy/hybrid/wiring.md)

## Responsibility
The engines take ONE exit rule per run. A package with a percent stop and a
take-profit trail composes them here.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `AnyOf(rules)` | class | fire when any wrapped rule fires; short-circuits |
| `any_of(rules)` | function | drop the `None`s; return None, the bare rule, or an `AnyOf` |

## Key points / gotchas
- **`any_of` collapses.** With only the stop set it returns the bare
  `EntryPercentStop`; with nothing set, None (so `run_backtest` skips the exit
  step). Every run recorded before the take-profit knobs keeps its exact shape.
- **Resumability is the weakest member's.** `adoptable` is True only if every
  member is; `restorable` when every non-adoptable member is restorable, with
  `state_keys` the union of theirs. A percent stop plus a `ProfitTrail` resumes
  from one `best_price`.
- **Short-circuit is safe.** A later member skipped on a firing tick misses one
  state update, but the close is already submitted and stateful rules reset on
  the next entry identity.
- `strategy/fullday/exits.py` keeps its own `AnyOf` (its kept protection rules
  predate this module); the two behave the same.

## Related
[exits/profit_trail.md](profit_trail.md) · [exits/pct_stop.md](pct_stop.md) · [exits/base.md](base.md)
