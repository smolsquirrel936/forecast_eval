# [strategy/hybrid/router.py](../../../strategy/hybrid/router.py)

**Role:** HYBRID's router — one engine-facing emitter, trader and exit rule, each with two halves behind it.
**Pipeline:** between `run_backtest` and the two halves; routes by session.
**Depends on:** [environment.py](../../environment.md) (`classify_session`), [events.py](../../events.md), [exits/base.py](../../exits/base.md), [strategy/base.py](../base.md)   **Used by:** [strategy/hybrid/wiring.py](wiring.md), [strategy/hybrid/forecaster.py](forecaster.md)

## Responsibility
`run_backtest` takes one emitter, one trader factory and one exit rule; HYBRID
has two of each. These classes stand in for them and send every call to the
half whose session it is.

## Public surface
| Symbol | Kind | Routes |
|---|---|---|
| `session_of(ts)` | function | `'DAY'` / `'NIGHT'`; raises for an off-hours timestamp |
| `RoutedEmitter(day, night)` | class | `emit` / `emit_signal`: **both** halves see every forecast; the result is the half's of `forecast.timestamp`'s session |
| `RoutedTrader(day, night)` | class | `on_signal` / `submit_exit` by `market.session`; `on_fill` to **both**; `position` / `position_state()` checked to agree; assigning `position` or `entry_price` / `entry_bar_idx` / `entry_timestamp` writes both halves (a live `--resume`); `rolling()` enters each half's own `rolling()` hook during a live contract roll, and the router carries the original entry across it |
| `RoutedExitRule(day, night)` | class | `should_exit` by `market.session`; a half with no rule never fires; `adoptable` only if every present rule is |
| `SessionTrail(day, night)` | class | per-session take-profit trails over **one** best price: every print feeds every present rule, the verdict is `market.session`'s; a session with no rule never fires; restorable from `best_price` (both rules seeded) |

## Key points / gotchas
- **One position, two mirrors.** Both traders fold every fill, so a lot one half
  opened is managed — and can be closed — by the other after the session
  changes. `position` raises if the mirrors ever disagree.
- **Roll carry lives in the router (2026-11-11).** A roll's re-open is submitted
  inside `rolling()` to one half, but its fill reaches both. The router marks
  the re-open, remembers the entry the roll's close flattened, and when the
  re-open fills on that side writes the original entry (price, bar, time) to
  **both** halves — so the stops and the position-level take-profit trail keep
  measuring from it, as on the backtest's spliced tape, and the mirrors never
  split. A later signal, or an exit on a held position, cancels the mark; the
  carry is used once.
- **`SessionTrail` feeds the off-session rule too.** Unlike `RoutedExitRule`,
  each print goes to both sessions' trails, so both ratchet the same best price
  since entry and only the arm / give-back differ. A day-only trail therefore
  measures its give-back from a night high. Built by `wiring.py` from the
  `day_tp_*` / `night_tp_*` knobs.
- **The incoming half owns an inherited lot from its session's first tick**: its
  exit rule checks it on every tick, its emitter decides at its first boundary.
- **One `Execution`.** Both traders share it, so either half's cancel-first
  `on_signal` cancels the one resting order (SPEC §4.4), as a single Trader would.
- **Both emitters see every forecast; only the session's half decides.** A
  half's trend-filter memory therefore follows the continuous DAY+NIGHT stream, as
  its forecaster's context does. The first build fed a half only its own
  session's forecasts. That split the memory at every session change, and Step 0
  parity caught it: 251 of 78 648 signals differed from one emitter with the same
  settings.
- **Frozen during the loop** (`autoresearch_trading_plan_hybrid_split07.md`, "The freeze
  line"): an edit here moves both halves at once.

## Related
[wiring.py](wiring.md) · [forecaster.py](forecaster.md) · [day_half/trader.py](day_half/trader.md) · [night_half/trader.py](night_half/trader.md) · [run.py](../../run.md)
