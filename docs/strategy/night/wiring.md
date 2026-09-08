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
| `forecaster` | `Forecaster` | built by [rsi.py](rsi.md) via `make_rsi_forecaster` |
| `emitter` | `SignalEmitter` | a fresh [rsi.py](rsi.md) `RsiEmitter` |
| `make_trader` | `callable(Execution) -> Trader` | factory, not an instance — see [trader.py](trader.md) |
| `exit_rule` | `Optional[ExitRule]` | `TimeStop(TIME_STOP_BARS)` — the one-week holding cap (iter05) |
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
- **The exit is a one-week `TimeStop` since iter05 (2026-09-08).** The RSI
  strategy's only organic exit is SELL at RSI>=80, which never arrives in a
  decline; iter03's four disaster longs (8–35 days, -4 074/-3 178/-1 495/-1 367
  pts) carried the max drawdown precisely because nothing could close them.
  `TIME_STOP_BARS = 4000` (~4.8 sessions) bounds that tail — measured on a
  scratch replay validated bit-identical against iter03's harness numbers:
  net +12 833 → +15 623, dd -4 799 → -2 791, with 2000–4000 a plateau (5.57 /
  5.60) and 4000 the mid-band pick. The exit races the emitter per tick
  (whichever fires first closes), so RSI>=80 winners are untouched; pinned by
  [tests/test_night_wiring.py](../../../tests/test_night_wiring.py).

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
