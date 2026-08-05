# [strategy/wiring.py](../../strategy/wiring.py)

**Role:** Mutable strategy wiring seam for the autoresearch loop.
**Pipeline:** the single import the autoresearch harness uses to build the strategy stack.
**Depends on:** [strategy/base.py](base.md), [exits/base.py](../exits/base.md)   **Used by:** the autoresearch loop (the agent edits this file each iteration)

## Responsibility
This file is the **one wiring seam** between the frozen harness and the mutable
strategy. The autoresearch agent edits `make_strategy()` here to change which
emitter, which exit rule, and which trader kwargs (aggression_ticks /
max_position / max_aggression_ticks / forced_close) the run uses — without
touching the frozen evaluation entrypoint (`real_data.py`). The emitter classes
themselves live in `strategy/*.py` (also mutable); `trader.py` (also mutable)
holds the Trader state machine. This file just picks and configures them.

> **Note:** the docstring still references `autoresearch_eval.evaluate`, the
> loop runner from the `Z:\test-autoresearch` checkout. That module is not
> present in this tree — the loop here is driven by the agent reading
> `autoresearch_trading_plan.md`, not by `autoresearch_eval`. The `make_strategy`
> contract below is what matters; the import path is the only stale line.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `make_strategy() -> dict` | function | returns the strategy stack `{emitter, exit_rule, aggression_ticks, max_position, max_aggression_ticks, forced_close}` for the current experiment |

## Return contract
`make_strategy()` returns a dict with keys:

| key | type | meaning |
|---|---|---|
| `emitter` | `SignalEmitter` | a concrete emitter instance (default `ThresholdEmitter`) |
| `exit_rule` | `Optional[ExitRule]` | `None` for no risk-exit, else an `ExitRule` instance |
| `aggression_ticks` | `int` | limit offset in ticks (0 = at-the-touch) |
| `max_position` | `int` | max contracts |
| `max_aggression_ticks` | `Optional[int]` | confidence-scaled aggression ramp (`None` = off) |
| `forced_close` | `bool` | force close at session boundaries |

## Key points / gotchas
- **This is the mutable file the autoresearch loop edits** — the agent swaps
  emitters, adjusts thresholds, adds exit rules, and changes sizing by editing
  the body of `make_strategy()`. The frozen harness re-imports it each run.
- The default body imports `ThresholdEmitter` lazily inside the function so the
  module imports cleanly even before any emitter module exists.
- `make_strategy` returns a **fresh** stack each call (no shared state), which
  matters for sweeps — pair it with a factory, not a cached instance, if the
  chosen emitter is stateful (see [threshold.md](threshold.md) / [volmultitf.md](volmultitf.md)
  on statefulness).
- It is **not** wired into `real_data.py` (which still constructs
  `ThresholdEmitter` directly); it is the seam for the autoresearch loop's own
  evaluation path.

## Related
[strategy/base.py](base.md) · [strategy/threshold.py](threshold.md) · [strategy/volmultitf.py](volmultitf.md) · [exits/base.py](../exits/base.md) · [trader.py](../trader.md) · [autoresearch_trading_plan.md](../../autoresearch_trading_plan.md)