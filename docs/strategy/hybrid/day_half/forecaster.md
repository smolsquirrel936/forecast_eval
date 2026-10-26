# [strategy/hybrid/day_half/forecaster.py](../../../../strategy/hybrid/day_half/forecaster.py)

**Role:** HYBRID's DAY-half Forecaster construction site.
**Pipeline:** builds the `Toto2Forecaster` that [RoutedForecaster](../forecaster.md) uses for DAY-session boundaries.
**Depends on:** [forecaster/toto2.py](../../../forecaster/toto2.md)   **Used by:** [strategy/hybrid/wiring.py](../wiring.md)

## Responsibility
`make_forecaster(...)` builds the DAY half's Toto2 forecaster from the
`day_checkpoint` / `day_context_length` knobs and the shared stride and horizon.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `make_forecaster(*, warmup_bars, stride_bars, horizon_bars, context_length, checkpoint, device="auto", bar_freq=None, signal_step="last")` | function | the half's `Forecaster` |

## Key points / gotchas
- **Copied from `main:strategy/day/forecaster.py`**, import depth changed. Thin by
  design: to change model internals, copy the class body from
  [forecaster/toto2.py](../../../forecaster/toto2.md) in here first.
- **One of the three files `forecaster_sha("hybrid")` hashes**
  ([strategy/wiring.py](../../wiring.md) `FORECASTER_FILES`), so an edit here is a
  forecast-stream change in `results_hybrid.tsv`.
- It reads the **continuous DAY+NIGHT history**, not DAY bars only. Filtering to
  its own session is a forecaster change made here.
- When both halves' forecasters are configured identically, the router uses the
  **day** half's instance for both sessions — an edit that changes this file's
  configuration (or class) is what ends that sharing.

## Related
[strategy/hybrid/wiring.py](../wiring.md) · [strategy/hybrid/router.py](../router.md) · [strategy/day/ (the session package)](../../day/wiring.md) · [strategy/hybrid/forecaster.py](../forecaster.md)
