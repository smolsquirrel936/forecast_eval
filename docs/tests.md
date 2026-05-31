# [tests/](../tests/) — unit + integration suite

**Role:** Validation of every invariant the harness depends on (SPEC §6 Phase 5, §9).
**Run:** `python -m pytest forecast_eval/tests/ -v` (use the **paper** env Python — see [CLAUDE.md](../CLAUDE.md)).
**33 tests, ~1 s.**

> Per-file docs are intentionally consolidated here: each test already carries a
> docstring explaining its case, so this page is a coverage map rather than 6 thin
> pages.

## What each file covers

| File | Covers | Mirrors |
|---|---|---|
| [test_execution.py](../tests/test_execution.py) | fill simulator golden cases: same-tick guard, marketable BUY/SELL, at-the-touch, passive strict-cross, no-cross, cancel | [execution.py](execution.md) · SPEC §4.5 |
| [test_trader.py](../tests/test_trader.py) | the §4.2 state machine (every position × signal cell), no-flip, pending cancelled on new signal, entry tracking, `submit_exit` | [trader.py](trader.md) · SPEC §4.2–4.4 |
| [test_metrics.py](../tests/test_metrics.py) | `build_trades` round-trip pairing, `trading_metrics` math (hit rate, drawdown + duration), `forecast_quality` (hit rate + IC), `signal_attribution` | [metrics.py](metrics.md) · SPEC §7 |
| [test_lookahead.py](../tests/test_lookahead.py) | the naive forecaster passes the leak guard; a deliberately leaky forecaster is caught | [forecaster/base.py](forecaster/base.md) · SPEC §6 |
| [test_integration.py](../tests/test_integration.py) | exact PnL on a known stream; signal PnL == realized with no slippage/fees; buy-and-hold math; fee-sweep monotonicity (incl. the stateful-factory guard) | [run.py](run.md) · SPEC §6 Phase 5 |
| [conftest.py](../tests/conftest.py) | `sys.path` shim so absolute imports work from any CWD | — |

## How the golden cases stay honest
- The **SPEC §4.5 worked examples** (marketable BUY at 17501, passive BUY filling at
  the limit 17497 on a strict cross) are reproduced **verbatim** in
  `test_execution.py`.
- `test_integration.py` constructs tick streams where the fill outcome is fully
  predictable, then asserts the *exact* net PnL down to the fee fraction.
- The fee-sweep test uses a `make_emitter` **factory** — a regression guard against
  reusing stateful emitters across sweep iterations (see [run.py](run.md)).

## Related
[execution.py](execution.md) · [trader.py](trader.md) · [metrics.py](metrics.md) · [run.py](run.md) · [usage.md §2](../usage.md)
