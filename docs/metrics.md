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
| `build_trades(fills, *, roll_fills=None, …)` | function | pair opening fills with closing fills; `roll_fills` spans a contract roll |
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
- **`roll_fills` keeps a contract roll one trade.** Pass the timestamps of the
  fills that were roll legs and a closing leg listed there does not end the
  round-trip: its PnL and fees are banked and the trade continues at the
  re-entry's price, with `TradeRecord.rolled` counting the boundaries crossed.
  Without it a roll splits a median 4.5-day hold into two shorter trades and
  inflates the trade count, so per-trade metrics stop being comparable with the
  backtest, where a held position simply crosses a splice (§2.6). The parameter
  is opt-in — omit it and behaviour is unchanged. A roll whose re-entry never
  arrives is still booked, at the price the position actually left at.

## Related
[run.py](run.md) · [portfolio.py](portfolio.md) · [reports.py](reports.md) · [tests/test_metrics.py](tests.md) · [SPEC §7](../SPEC.md)
