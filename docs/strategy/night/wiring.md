# [strategy/night/wiring.py](../../../strategy/night/wiring.py)

**Role:** NIGHT's strategy wiring — `PARAMS` plus `make_strategy()`.
**Pipeline:** the NIGHT entry in the [strategy seam](../wiring.md).
**Depends on:** [emitter.py](emitter.md), [forecaster.py](forecaster.md), [trader.py](trader.md)   **Used by:** [strategy/wiring.py](../wiring.md), [real_data.py](../../real_data.md)

## Responsibility
The entry point for NIGHT's strategy set: `PARAMS` declares the session's default
knobs, and `make_strategy()` builds its Forecaster, SignalEmitter and Trader
factory. This is the file a search for this session edits first.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `PARAMS` | dict | the twelve default knobs `--strategy night` resolves against |
| `make_strategy(*, warmup_bars, tick_size=1.0, **overrides)` | function | the session's strategy stack |

## Return contract
| key | type | meaning |
|---|---|---|
| `forecaster` | `Forecaster` | built by [forecaster.py](forecaster.md) |
| `emitter` | `SignalEmitter` | a fresh [emitter.py](emitter.md) instance |
| `make_trader` | `callable(Execution) -> Trader` | factory, not an instance — see [trader.py](trader.md) |
| `exit_rule` | `Optional[ExitRule]` | `None` — this session declares no risk exit |
| `forced_close` | `bool` | force close at session boundaries |
| `params` | dict | the resolved knobs, for the run banner and `params.json` |

## Provenance
`PINNED` (commit `4282d33`) — Calmar 3.6006, 235 trades, band ±0.00045 / `min_prob` 0.5.

## Key points / gotchas
- **Starting at the pinned config is what makes the split checkable.**
  `--strategy night` must reproduce the recorded run exactly (gate G1), so
  any deviation is a bug in the split rather than a strategy change.
- An unknown `**overrides` key **raises** rather than being ignored. A silently
  dropped override would run the pinned value while the command line said
  otherwise — the class of mismatch `live/config.py` was written to end.
- `warmup_bars` and `tick_size` come from the run, not the strategy; everything
  else defaults from `PARAMS` and is beaten by an explicit CLI flag.
- Four emitter knobs are deliberately **not** in `PARAMS` — `short_window`,
  `min_trend_ticks`, `path_momentum`, `trend_filter`. `real_data.py` never
  exposed them, so every recorded run used the emitter's class defaults; leaving
  them there is what reproduces those runs.

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
