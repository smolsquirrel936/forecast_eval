# [strategy/wiring.py](../../strategy/wiring.py)

**Role:** The strategy seam — dispatches to one of the three per-session packages.
**Pipeline:** the single import [real_data.py](../real_data.md) uses to build a strategy stack.
**Depends on:** [strategy/day](day/wiring.md), [strategy/night](night/wiring.md), [strategy/fullday](fullday/wiring.md)   **Used by:** [real_data.py](../real_data.md) (`--strategy`)

## Responsibility
Picks a session's strategy package and forwards to it. It holds **no strategy
values of its own** — every knob lives in the package it belongs to — so this
file cannot drift from the tuned configuration the way its predecessor did.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `SESSIONS` | tuple | `("day", "night", "fullday")` — what `--strategy` renders its `choices=` from |
| `params(session)` | function | that session's declared default knobs, as a fresh copy |
| `make_strategy(session, **kwargs)` | function | that session's `{forecaster, emitter, make_trader, exit_rule, forced_close, params}` |
| `forecaster_sha(session)` | function | 12-hex sha256 of that package's `forecaster.py` — the `results.tsv` column |

## What changed, and why
Before STRATEGY_SPLIT_PLAN this module held a single `make_strategy()` that
**nothing called**. [live/config.py](../live/config.md) records that its
entrypoint no longer existed and that the winning research runs were produced
through `real_data.py` CLI flags instead — so the seam disagreed with the tuned
configuration on three knobs (checkpoint, band, `min_prob`) and nothing raised.

Rebuilding it as a dispatcher is the "seam rebuilt" that DAY_SESSION_PLAN §2.6
asked for. The old return contract (a flat dict of emitter + trader kwargs) is
gone; the per-session `make_strategy()` returns objects, including the Trader as
a **factory** over `Execution`.

## `forecaster_sha` — the bookkeeping the split owes
Making the forecaster per-session mutable (STRATEGY_SPLIT_PLAN D3) gives up the
guarantee the autoresearch loop rested on: that forecasts are a fixed stream, so
every iteration is comparable and `--precompute` amortises across them. Once the
forecaster can move, a Calmar improvement may come from the forecaster rather
than the strategy and the log cannot tell which.

`results.tsv` now carries a `forecaster_sha` column to restore the distinction —
rows sharing a sha were scored on the same forecast stream; a change marks a
stream change and has to be argued for. The 20 rows that predate the split read
`pre-split`. `real_data.py --strategy <s>` prints the sha in its banner and
records it in `params.json`, so a row can be filled in from the run that produced
it.

It hashes the **file**, not the imported module, so it needs no torch and an edit
counts even when the old bytes are already imported.

## Key points / gotchas
- `SESSIONS` is a **declared tuple, not a directory scan**. A stray sibling
  directory (a half-finished experiment, a `__pycache__`) must not become a
  selectable deployment — the same reason `live/config.py:CONFIGS` names its
  deployments explicitly.
- An unknown session name **raises**. The caller is a CLI flag with `choices=`,
  so it should be unreachable from the command line; it exists so programmatic
  callers fail loudly instead of silently falling back to a default session.
- `params()` returns a **copy**. The caller merges CLI overrides into it, and a
  mutated `PARAMS` would silently re-tune every later run in the same process.
- Importing this module does **not** import `torch` — each package defers that
  into `make_forecaster`, which is what lets `--help` and
  `tests/test_strategy_isolation.py` run on a machine without a GPU.

## Related
[strategy/base.py](base.md) · [strategy/threshold.py](threshold.md) · [real_data.py](../real_data.md) · [run.py](../run.md) · [STRATEGY_SPLIT_PLAN.md](../../STRATEGY_SPLIT_PLAN.md) · [autoresearch_trading_plan.md](../../autoresearch_trading_plan.md)
