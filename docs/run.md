# [run.py](../run.py)

**Role:** The end-to-end backtest driver + synthetic demos (SPEC §4.1).
**Pipeline:** wires Environment → Execution → Trader → Forecaster → SignalEmitter → Portfolio.
**Depends on:** nearly every core module   **Used by:** every entry point, [tests/](tests.md)

## Responsibility
Owns the **per-tick event loop** — the heart of the harness — and packages the
outcome into a `BacktestResult`. Also provides synthetic-data demos for Phases 1–4
and a fee-sensitivity sweep helper.

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `BacktestResult` | dataclass | final portfolio + event logs + counters |
| `run_backtest(ticks, …)` | function | the per-tick loop; returns a `BacktestResult` |
| `fee_sensitivity_sweep(...)` | function | re-run at several fee rates (uses factories) |
| `demo` / `demo_naive` / `demo_stop_loss` / `demo_forced_close` | functions | Phase 1–3 synthetic demos |
| `demo_with_logs_and_metrics(...)` | function | Phase 4: run + write logs + print metrics |

## The per-tick loop (5 steps)
1. **Check fills** — resolve the pending order (can't fill on its own placement tick).
2. **Check ExitRule** — if a position is open and the rule fires, submit a close.
3. **Forecast boundary** — past warm-up and on a stride boundary: build a fresh
   history-through-*t* frame → `Forecaster.forecast` → `SignalEmitter.emit` →
   `Trader.on_signal`.
4. **Session-boundary forced close** — if enabled and the session flipped while
   holding, submit a close and **latch the prior session** for PnL booking.
5. **Record** fills / signals / forecasts.

## Key invariants / gotchas
- **Look-ahead defense:** the forecaster receives a freshly-built frame of only
  rows ≤ t; a runtime check raises if `forecast.timestamp > market.timestamp`.
- **Forced-close session tagging:** the closing fill prints on the *new* session's
  first tick, but its PnL is booked to the *prior* session via the
  `force_close_pending_session` latch.
- **Stateful factories:** `fee_sensitivity_sweep` takes `make_emitter` /
  `make_forecaster` callables, not instances — reusing a stateful emitter/forecaster
  across runs would silently corrupt results.
- With no forecaster supplied, the loop uses the dummy alternating emitter and the
  explicit `forecast_stride_bars` / `warmup_bars` kwargs (Phase-1 path).

## Related
[execution.py](execution.md) · [trader.py](trader.md) · [portfolio.py](portfolio.md) · [metrics.py](metrics.md) · [SPEC §4.1](../SPEC.md)
