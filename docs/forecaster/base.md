# [forecaster/base.py](../../forecaster/base.py)

**Role:** The `Forecaster` abstract base + a look-ahead protection helper (SPEC §3, §6).
**Pipeline:** defines the model contract consumed by [run.py](../run.md).
**Depends on:** [events.py](../events.md)   **Used by:** [naive.py](naive.md), [toto2.py](toto2.md), [tests/test_lookahead.py](../tests.md)

## Responsibility
Declares the entire contract a forecasting model must satisfy to plug into the
harness, and ships a test helper that catches the most common look-ahead bug.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `Forecaster` | ABC | three bar-unit attributes + `forecast(history_df) -> Forecast` |
| `assert_no_lookahead(forecaster, full_history, k=None)` | function | proves output at t depends only on rows ≤ t |

## The contract
- Class attributes the run loop reads: `warmup_bars`, `forecast_stride_bars`,
  `forecast_horizon_bars`.
- `forecast(history_df)` is given history **up to and including** t; it MUST NOT
  consult any source outside that frame, and the returned `Forecast.timestamp`
  must be ≤ t.

## How the leak guard works
`assert_no_lookahead` forecasts a length-k prefix (baseline), then shows the
forecaster a *larger* frame, then re-forecasts the same prefix. A forecaster that
caches the largest frame it has ever seen will now answer differently — that
divergence is the leak it catches. The real structural defense, though, is the run
loop passing only rows ≤ t.

## Related
[naive.py](naive.md) · [toto2.py](toto2.md) · [run.py](../run.md) · [SPEC §3](../../SPEC.md)
