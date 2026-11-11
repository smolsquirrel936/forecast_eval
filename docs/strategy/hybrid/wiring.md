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
- **UNTUNED.** Both halves start at `main`'s reset session packages
  (DAYNIGHT_TOTO2_RESET_PLAN, `20767bb`): day ±0.001 / `min_prob` 0.65, night
  ±0.00045 / 0.5, two-sided, Toto2-1B, context 3008, no exit rule. The search is
  `autoresearch_trading_plan_hybrid_split07.md` (repo root): one joint search over
  both halves, one half per iteration.
- **Registered for live** (2026-10-27) as `PINNED_HYBRID` in `live/config.py`:
  named package `hybrid`, file slug `hybrid` (never fullday's `day_night`). Live
  builds from this same `make_strategy`.
- `exit_rule` is None unless a half sets `stop_pct` or a take-profit trail is
  set (`ProfitTrail` or `SessionTrail`, composed with the routed stops through
  `any_of`), so the unedited package runs exactly the reset packages' path (no
  exit step).
- Stateful objects (emitters' deques, traders' mirrors, forecasters' model
  cache) are built fresh on every `make_strategy` call.

## Related
[router.py](router.md) · [forecaster.py](forecaster.md) · [strategy/wiring.py](../wiring.md) · [strategy/fullday/wiring.py](../fullday/wiring.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
