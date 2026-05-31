# [metrics.py](../metrics.py)

**Role:** The full metric pack computed from a finished run (SPEC §7).
**Pipeline:** `BacktestResult` → **metrics** → printed/logged numbers.
**Depends on:** [events.py](events.md), [environment.py](environment.md)
**Used by:** [run.py](run.md), [logging_io.py](logging_io.md), the CLI entry points

## Responsibility
Scores a backtest in three independent blocks. Metrics are computed *after* the
run from the recorded event lists — the loop stays pure. **Sharpe/Sortino are
per-trade and unannualized.**

## Public surface
| Symbol | Kind | One-liner |
|---|---|---|
| `TradeRecord` | dataclass | one closed round-trip (entry+exit, gross/fees/net) |
| `build_trades(fills, …)` | function | pair opening fills with closing fills |
| `trading_metrics(trades)` | function | PnL, hit rate, drawdown, Sharpe/Sortino, fee drag |
| `forecast_quality(forecasts, tick_df)` | function | direction hit rate + Spearman IC |
| `signal_attribution(signals, realized)` | function | frictionless signal PnL vs realized → drag |
| `signal_attribution_curve(signals)` | function | per-round-trip cumulative signal PnL (for charts) |
| `buy_and_hold_pnl(tick_df, fee_rate)` | function | the "floor" baseline |
| `compute_metrics(result, tick_df, forced_close)` | function | the top-level pack |

## The three blocks
- **Trading metrics** — from round-trips: total/net PnL, hit rate, avg win/loss,
  drawdown (+ duration in trades), turnover, fee drag, per-trade Sharpe/Sortino.
- **Forecast quality** — direction hit rate and the **Spearman IC** (own
  implementation, no SciPy). Realized return is read at `i + horizon_bars` *ticks*.
- **Attribution** — replays signals through the same no-flip state machine at
  emit-time price with zero fees/slippage. The gap `signal_pnl − realized_pnl` is
  exactly the execution + cost drag.

## Key points / gotchas
- **Session bucketing:** when `forced_close=True`, `compute_metrics` returns
  separate `trading_day` / `trading_night` blocks; forecast & attribution stay
  global (defined at the forecast/signal level).
- The forecast-quality realized window is in **ticks**, not the model's bar unit —
  approximate if the horizon is expressed in aggregated bars (e.g. Toto2 `1min`).
- `build_trades` assumes `max_position == 1` (the v1 invariant).

## Related
[run.py](run.md) · [portfolio.py](portfolio.md) · [reports.py](reports.md) · [tests/test_metrics.py](tests.md) · [SPEC §7](../SPEC.md)
