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
| `PARAMS` | dict | the twelve shared default knobs `--strategy night` resolves against |
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
`night_iter30` (`4cbc1e6`, branch `autoresearch_night_1004`) — band ±0.0004,
`min_prob` 0.6, `stride_bars` 15, and the emitter's `min_trend_ticks` default
moved 3.0 → 10.0. Calmar **7.2974**, 376 trades, on the **whole** night eval window
(warmup 6000, no held-out tail); the row is in that branch's `results_night.tsv`,
not `results.tsv`, so `live/config.py:PINNED` carries no `source_commit`. The
search started from `PINNED`'s old settings (`4282d33`, band ±0.00045 /
`min_prob` 0.5), which its `iter01` scored at 1.1731.

## Key points / gotchas
- An unknown `**overrides` key **raises** rather than being ignored. A silently
  dropped override would run the pinned value while the command line said
  otherwise — the class of mismatch `live/config.py` was written to end.
- `warmup_bars` and `tick_size` come from the run, not the strategy; everything
  else defaults from `PARAMS` and is beaten by an explicit CLI flag.
- Four emitter knobs are deliberately **not** in `PARAMS` — `short_window`,
  `min_trend_ticks`, `path_momentum`, `trend_filter`. They stay at
  [emitter.py](emitter.md)'s class defaults, so a search that moves one edits
  that file: `night_iter05` moved `min_trend_ticks` 3.0 → 10.0 there. Being out
  of `PARAMS`, `--param` cannot reach it and `params.json` does not record it;
  the live banner and `meta` header do (`describe()` reads the class default).

## Related
[strategy/night/wiring.md](wiring.md) · [strategy/wiring.py](../wiring.md) · [strategy/base.py](../base.md) · [STRATEGY_SPLIT_PLAN.md](../../../STRATEGY_SPLIT_PLAN.md)
