# [strategy/fullday/wiring.py](../../../strategy/fullday/wiring.py)

**Role:** FULL-DAY's strategy wiring — `PARAMS` plus `make_strategy()`.
**Pipeline:** the FULL-DAY entry in the [strategy seam](../wiring.md).
**Depends on:** [emitter.py](emitter.md), [forecaster.py](forecaster.md), [trader.py](trader.md)   **Used by:** [strategy/wiring.py](../wiring.md), [real_data.py](../../real_data.md)

## Responsibility
The entry point for FULL-DAY's strategy set: `PARAMS` declares the session's default
knobs, and `make_strategy()` builds its Forecaster, SignalEmitter and Trader
factory. This is the file a search for this session edits first.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `PARAMS` | dict | the twelve default knobs `--strategy fullday` resolves against |
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
**UNTUNED** — DAY's numbers, inherited and never measured here, band ±0.001 / `min_prob` 0.65 (inherited from DAY).

## Key points / gotchas
- **Starting at the pinned config is what makes the split checkable.**
  `--strategy fullday` must reproduce the recorded run exactly (gate G1), so
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

> **This session is UNTUNED.** The values in `PARAMS` are DAY's, inherited so the
> package is runnable and parity-checkable. They were never measured on
> `TXF_RPT_minute_fullday.csv`, there is no `results.tsv` row for full-day, and
> this configuration has never traded. `live/cli.py` refuses `--config fullday`
> until a tuned row exists (STRATEGY_SPLIT_PLAN §5).

## Related
[strategy/fullday/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
