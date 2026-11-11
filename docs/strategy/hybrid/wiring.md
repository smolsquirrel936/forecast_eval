# [strategy/hybrid/wiring.py](../../../strategy/hybrid/wiring.py)

**Role:** HYBRID's strategy wiring — builds both halves and the routers that put them behind one strategy.
**Pipeline:** what `--strategy hybrid` resolves through [strategy/wiring.py](../wiring.md).
**Depends on:** [day_half/](day_half/emitter.md), [night_half/](night_half/emitter.md), [router.py](router.md), [forecaster.py](forecaster.md), [exits/pct_stop.py](../../exits/pct_stop.md)   **Used by:** [strategy/wiring.py](../wiring.md)

## Responsibility
HYBRID trades **both sessions** and may carry a lot across a session change
(`forced_close` False), like `fullday` — but the **DAY half trades the DAY
session and the NIGHT half the NIGHT session**. `make_strategy` builds each
half's Forecaster, SignalEmitter, Trader and optional percent stop from its
`day_*` / `night_*` knobs, and returns the routers as the one strategy stack
`run_backtest` takes.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `PARAMS` | dict | the twelve CLI knobs + each half's prefixed knobs |
| `INERT` | tuple | the seven unprefixed CLI knobs that must stay None |
| `make_strategy(*, warmup_bars, tick_size=1.0, **overrides)` | function | `{forecaster, emitter, make_trader, exit_rule, forced_close, params}` |

## The knobs
| knob | meaning here |
|---|---|
| `stride_bars`, `horizon_bars` | **shared** by both halves (the engine has one stride; the forecast cache decodes one horizon) |
| `context_length` | the history window the run loop materialises — **must equal** `max(day_context_length, night_context_length)` |
| `max_position`, `forced_close` | one position, both traders; no close at a session change |
| `checkpoint`, `buy_threshold`, `sell_threshold`, `min_prob`, `full_confidence_prob`, `aggression_ticks`, `max_aggression_ticks` | **inert**: None, and `make_strategy` refuses a value, so a typed `--buy-threshold` fails rather than being ignored |
| `day_*` / `night_*` | `checkpoint`, `context_length`, the band, `min_prob`, `full_confidence_prob`, `aggression_ticks`, `max_aggression_ticks`, `stop_pct` for that half |
| `tp_arm_pct`, `tp_trail_pct`, `tp_giveback_frac` | one position-level take-profit trail (`exits/profit_trail.py`) beside the routed stops; all None is off |
| `day_tp_*` / `night_tp_*` | the same three knobs per session, built into a `router.SessionTrail` over the same one best price; a session left None never fires the trail. **Exclusive** with the `tp_*` trail (`make_strategy` refuses both) |

## Key points / gotchas
- **Tuned: `hybrid_split_iter22` plus percent stops.** The code of both halves
  is `main`'s reset session packages (DAYNIGHT_TOTO2_RESET_PLAN, `20767bb`),
  Toto2-1B, context 3008; the values come from the joint search
  `autoresearch_trading_plan_hybrid_split07.md` (repo root), final iter22
  (`9c0741f`, adopted as `58eb8b5`): day ±0.0005 / `min_prob` 0.60, no openings
  09:30–13:00; night ±0.0007 / 0.5, closes only. Train Calmar 12.8738,
  held-out 6.9773.
- **Percent stops since 2026-11-01:** `day_stop_pct` 0.05, `night_stop_pct` 0.06,
  from `HYBRID_PCTSTOP_REPORT.md` (`autoresearch_hybrid_pctstop_1101`) — a user
  decision against the report's None/None. Train Calmar 12.2446 (−4.9 %), MDD
  −1157 (was −1129); held-out 6.92, MDD −2342 unchanged. The night stop never
  fired with the day stop on; it caps a carried lot overnight.
  `--param day_stop_pct=none night_stop_pct=none` restores the iter22 run.
- **Registered for live** (2026-10-27) as `PINNED_HYBRID` in `live/config.py`:
  named package `hybrid`, file slug `hybrid` (never fullday's `day_night`). Live
  builds from this same `make_strategy`.
- `exit_rule` is a `RoutedExitRule` when either half sets `stop_pct`, composed
  through `any_of` with the take-profit trail (`ProfitTrail` or `SessionTrail`)
  when one is set; None when nothing is — then `run_backtest` skips the exit
  step, as the reset packages do.
- Stateful objects (emitters' deques, traders' mirrors, forecasters' model
  cache) are built fresh on every `make_strategy` call.

## Related
[router.py](router.md) · [forecaster.py](forecaster.md) · [strategy/wiring.py](../wiring.md) · [strategy/fullday/wiring.py](../fullday/wiring.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
