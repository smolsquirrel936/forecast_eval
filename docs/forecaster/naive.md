# [forecaster/naive.py](../../forecaster/naive.py)

**Role:** Zero-skill baseline forecaster — "predict next price = last price" (SPEC §6 Phase 2, §9).
**Pipeline:** a concrete [Forecaster](base.md).
**Depends on:** [events.py](../events.md), [forecaster/base.py](base.md)   **Used by:** [run.py](../run.md) (`demo_naive`), [tests/test_lookahead.py](../tests.md)

## Responsibility
The simplest possible model: it always predicts the last observed price, i.e. a
predicted return of exactly 0. Used to validate plumbing and as the floor every
real model must beat.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `NaiveLastPrice(warmup_bars, forecast_stride_bars, forecast_horizon_bars)` | class | the baseline forecaster |
| `forecast(history_df)` | method | returns `predicted_return = 0.0`, stamped at the last row |

## Key points
- With a [ThresholdEmitter](../strategy/threshold.md) it trades **nothing** (0
  return never clears a threshold) → sits at break-even, the §9 milestone.
- With the [dummy emitter](../strategy/dummy.md) it still exercises the full
  fill/fee plumbing (the emitter ignores the forecast).
- Look-ahead-clean by construction: timestamp == last history row.

## Related
[forecaster/base.py](base.md) · [strategy/threshold.py](../strategy/threshold.md) · [toto2.py](toto2.md) · [SPEC §9](../../SPEC.md)
