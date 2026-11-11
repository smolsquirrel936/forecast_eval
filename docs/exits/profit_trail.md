# [exits/profit_trail.py](../../exits/profit_trail.py)

**Role:** ProfitTrail — a take-profit trail that does nothing until the trade is *in profit by the arm level*, then exits on a give-back from the best price since entry.
**Pipeline:** a concrete [ExitRule](base.md); each package's `make_strategy` builds it (through `profit_trail_from_knobs`) when that package's `tp_arm_pct` knob is set, and composes it with the percent stop through [any_of](any_of.md).
**Depends on:** [events.py](../events.md), [exits/base.py](base.md)   **Used by:** [strategy/day/wiring.py](../strategy/day/wiring.md), [strategy/night/wiring.py](../strategy/night/wiring.md), [strategy/fullday/wiring.py](../strategy/fullday/wiring.md), [strategy/hybrid/wiring.py](../strategy/hybrid/wiring.md), [run.py](../run.md) Step 2

## Responsibility
Protect part of a winner's open gain without touching trades that never earned
anything. Inert until the best price since entry is `arm_pct` of the entry in
the position's favour; then exactly one give-back shape decides the exit.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `ProfitTrail(arm_pct, *, trail_pct=None, giveback_frac=None)` | class | arm at `arm_pct` in favour; then exit `trail_pct` off the best price, or when the open gain falls to `(1-giveback_frac)` of its max — exactly one shape |
| `profit_trail_from_knobs(arm_pct, trail_pct, giveback_frac)` | function | the rule from a package's three `tp_*` knobs; None when `arm_pct` is None; refuses a shape without an arm |
| `export_state()` / `restore_state(position, state)` | method | the one restorable number, `best_price` |

## Key points / gotchas
- **Why an arm.** DAY's day_trail loop (2026-10-11) killed every pure trail
  armed from the entry fill: it fires at V-bottoms. A trade that never reaches
  `arm_pct` can never fire this rule.
- **Knobs, in every package:** `tp_arm_pct`, `tp_trail_pct`, `tp_giveback_frac`,
  all `None` (OFF) by default, so no recorded baseline moves. Hybrid declares
  one set for the **position**, not one per half, because its lot carries across
  sessions and the trail must see every print of it.
- **Evidence is DAY's only** (branch `autoresearch_day_tptrail_1111`,
  `TPTRAIL_POSTMORTEM.md` there): arms of 8 % or less churn and double the
  drawdown; a re-entry cooldown after the take-profit hurt every cell. A width
  tuned for one session needs its own search before another uses it.
- **Stateful: `adoptable = False`, `restorable = True`.** The state is the best
  price since entry, which live writes to `exit_state_<slug>.json`
  ([live/exit_state.py](../live/exit_state.md)); `--resume` restores it, or the
  operator types `--exit-state best_price=...`. With neither, the resume is refused.
- **Keyed on the entry identity** (bar, price, side). A Trader that carries the
  original entry across a live roll (fullday's, day's) keeps the trail armed
  through it; one that does not restarts it at the new month.
- **Never fires on the arming print** (price == best there), and the percent
  shape compares against `trail_pct * best`, a fraction of price, not points.

## Related
[exits/any_of.md](any_of.md) · [exits/pct_stop.md](pct_stop.md) · [exits/base.md](base.md) · [run.md](../run.md) Step 2
