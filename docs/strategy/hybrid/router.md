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
| `RoutedTrader(day, night)` | class | `on_signal` / `submit_exit` by `market.session`; `on_fill` to **both**; `position` / `position_state()` checked to agree |
| `RoutedExitRule(day, night)` | class | `should_exit` by `market.session`; a half with no rule never fires; `adoptable` only if every present rule is |

## Key points / gotchas
- **One position, two mirrors.** Both traders fold every fill, so a lot one half
  opened is managed — and can be closed — by the other after the session
  changes. `position` raises if the mirrors ever disagree.
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
