# [test_toto2.py](../test_toto2.py)

**Role:** Smallest end-to-end smoke test that Toto2 is wired up correctly.
**Entry point:** `python -m forecast_eval.test_toto2`   ·   needs Toto2
**Depends on:** [forecaster/toto2.py](forecaster/toto2.md)

## Responsibility
Loads the 313m checkpoint, builds a 512-row synthetic random walk, runs **one**
forecast through the wrapper, and prints the result. Not part of the pytest suite —
it's a manual sanity check before the slow real-data runs.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `make_history(n=512, …)` | function | random-walk price series shaped like tick history |
| `main()` | function | construct the forecaster, run one forecast, print + assert |

## What it checks
- The model loads and runs (downloads ~600 MB on first run).
- Prints `last_price`, `predicted_price`, `predicted_return`, and the head/tail of
  the `median_path`.
- Asserts `len(median_path) == horizon` and `forecast.timestamp == last history row`
  (look-ahead-clean).
- Distinguishes `[IMPORT ERROR]` (install torch + toto2) from `[RUNTIME ERROR]`.

Use this whenever you change [forecaster/toto2.py](forecaster/toto2.md) before
running [real_data_demo.py](real_data_demo.md).

## Related
[forecaster/toto2.py](forecaster/toto2.md) · [usage.md §4](../usage.md)
