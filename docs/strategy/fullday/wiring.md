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
Band ±0.00075 / `min_prob` 0.5 — the **worse-half winner** of the Step-0 band sweep on
`TXF_RPT_minute_fullday.csv` (`outputs/fullday_band_sweep.csv`: worst half 1.7818,
full-window 6.2573, 206 trades). Committed as `iter00: seed band from sweep`; replaces
DAY's inherited ±0.001 / 0.65, which were never measured on this tape.

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

> **The band is seeded, not tuned.** The values in `PARAMS` come from the Step-0
> sweep alone; the loop's first `results_fullday.tsv` row is still to come, and
> until a tuned row exists `live/cli.py` refuses `--config fullday`
> (STRATEGY_SPLIT_PLAN §5).

## Related
[strategy/fullday/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
