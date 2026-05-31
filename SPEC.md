# Framework for Evaluating Futures Forecasting

**Target instrument:** Taiwan Stock Exchange Futures (TXF)
**Forecasting model:** toto2 (and pluggable alternatives)
**Version:** 0.1 spec

---

## How to use this document

This is the design spec for the project. Drop it in the repo root. When starting work in Claude Code (or any other coding agent), prompt with something like:

> Read `SPEC.md` and implement Phase 1 (see §6). Set up the project structure described in §8 and stop after the core loop runs end-to-end with the dummy signal.

The spec is the single source of truth for architecture and behavior. Update it as decisions evolve.

---

## 1. Purpose

Evaluate the trading performance of a futures forecasting model by simulating trading against historical tick data. The framework decouples the forecasting model from the trading strategy and from execution, so each layer can be swapped or evaluated independently.

The core question it answers: **given a forecast model, how much PnL is realizable after realistic execution costs, and how much of the model's predictive edge is preserved through the trader to the final result?**

## 2. Architecture

Event-driven backtest with explicit separation of concerns:

```
[Tick stream]
     ↓
Environment ─emits─→ MarketEvent
                          ↓
                    Execution ──fills──→ Portfolio
                          ↓                 ↓
                    (pending orders)     (PnL, positions)
                          ↑
                    Trader ←─signal── SignalEmitter ←─forecast── Forecaster
                       ↑                                              ↑
                  ExitRule (opt.)                              [Model: toto2 etc.]
```

### Components

| Component | Responsibility |
|---|---|
| **Environment** | Replays tick data in chronological order; emits `MarketEvent` per print; tracks session (DAY / NIGHT). |
| **Forecaster** | Wraps the forecasting model. Reads history up to time *t*; returns a `Forecast`. Look-ahead-safe by contract. |
| **SignalEmitter** | Pure function from `Forecast` → `{BUY, SELL, HOLD}`. Model-agnostic. |
| **Trader** | Position state machine `{flat, long, short}`. Owns pending limit orders and optional risk exits. Translates signals to orders per §4. |
| **ExitRule** | Optional plug-in for stop-loss / time-stop / take-profit. Default `None`. |
| **Execution** | Fill simulator for trade-print-only data. Applies marketable vs. passive logic. Charges fees. |
| **Portfolio** | Position, cash, realized and unrealized PnL; tags PnL by session when forced-close mode is on. |
| **Logger** | Persists trade, forecast, and fill events to disk. |
| **MetricsReport** | Computes the metric pack (§7) from a `BacktestResult` and the input tick frame. |
| **Report** | Renders charts (equity / drawdown, price + fills, signal-vs-realized) as PNG + interactive HTML from the log bundle. Auto-invoked after each backtest (§4.9); also available as a standalone CLI. |

## 3. Data Contracts

### Events

```python
@dataclass
class MarketEvent:
    timestamp: datetime
    price: float
    volume: int
    session: Literal["DAY", "NIGHT"]

@dataclass
class Forecast:
    timestamp: datetime
    horizon_bars: int
    payload: Any                # model-defined; opaque to SignalEmitter API

@dataclass
class SignalEvent:
    timestamp: datetime
    direction: Literal["BUY", "SELL", "HOLD"]

@dataclass
class OrderEvent:
    timestamp: datetime
    side: Literal["BUY", "SELL"]
    limit_price: float
    intent: Literal["OPEN", "CLOSE"]

@dataclass
class FillEvent:
    timestamp: datetime
    side: Literal["BUY", "SELL"]
    fill_price: float
    quantity: int
    fee: float
```

### Abstract interfaces

```python
class Forecaster(ABC):
    warmup_bars: int
    forecast_stride_bars: int
    forecast_horizon_bars: int

    @abstractmethod
    def forecast(self, history_df: pd.DataFrame) -> Forecast: ...

class SignalEmitter(ABC):
    @abstractmethod
    def emit(self, forecast: Forecast) -> Literal["BUY", "SELL", "HOLD"]: ...

class ExitRule(ABC):
    @abstractmethod
    def should_exit(
        self,
        position: PositionState,
        market: MarketEvent,
        *,
        bar_idx: int,
    ) -> bool: ...
```

## 4. Behavior Specification

### 4.1 Per-tick event flow

For each `MarketEvent` emitted by the Environment:

1. **Check fills.** Execution checks all pending limit orders against the new print; matched orders emit `FillEvent` → Portfolio updates.
2. **Check exits.** If a position is open AND an `ExitRule` is configured, query it; if it returns `True`, Trader submits a closing limit at `current_price ∓ aggression_ticks`.
3. **Check forecast boundary.** If `t` is on a forecast stride boundary AND warm-up is complete:
   - Forecaster reads history up to `t` → `Forecast`
   - SignalEmitter converts → `{BUY, SELL, HOLD}`
   - Trader **cancels any unfilled pending order**
   - Trader applies the state machine (§4.2)
4. **Check session boundary.** If `t` crosses a session boundary AND `forced_close_on_session_end = True`: submit closing limit; tag the realized PnL to the closing session.
5. **Log** the events.

### 4.2 Signal → action state machine

| Current position | Signal | Action |
|---|---|---|
| flat | BUY | Submit BUY limit at `current + aggression_ticks` |
| flat | SELL | Submit SELL limit at `current − aggression_ticks` |
| flat | HOLD | No action |
| long | BUY | No action (no pyramiding in v1) |
| long | SELL | Submit closing SELL limit; **no flip to short** |
| long | HOLD | No action |
| short | BUY | Submit closing BUY limit; **no flip to long** |
| short | SELL | No action |
| short | HOLD | No action |

`max_position` defaults to 1. When `> 1`, the rules generalize to allow incremental adds up to the cap.

### 4.3 Limit order placement

- BUY limit price = `current_trade_price + aggression_ticks × tick_size`
- SELL limit price = `current_trade_price − aggression_ticks × tick_size`
- `aggression_ticks` defaults to 3; configurable per run.
- Positive values are aggressive (marketable); zero is at-the-touch; negative values are passive.

### 4.4 Pending order lifetime

A pending limit order is cancelled when a new signal arrives at the next forecast boundary, regardless of the new signal's value. Persistence across forecasts is a future extension.

### 4.5 Fill simulation rules

Trade-print-only data means fills are inferred from subsequent prints. Two cases:

**Marketable order** (BUY limit ≥ current price; SELL limit ≤ current price):
- Fills at the **next trade print** after order placement.
- Fill price = the trade's price (assumes the order crossed the spread and executed at the best available level).
- Rationale: in a real book, such an order would execute immediately against resting liquidity.

**Passive order** (BUY limit < current price; SELL limit > current price):
- Fills when a subsequent trade prints **strictly past** the limit (strictly below for BUY, strictly above for SELL).
- Fill price = the limit price (no price improvement assumed).
- Rationale: requiring strict crossing accounts for queue position; the order is assumed to be behind the resting liquidity at that price.

**Worked example — marketable BUY:**
- Current = 17500, aggression = 3 → BUY limit at 17503
- Next prints: 17501, 17502, 17499
- Fill: at the first next print (17501), fill price = 17501

**Worked example — passive BUY (aggression = −3):**
- Current = 17500 → BUY limit at 17497
- Next prints: 17499, 17497 (touches), 17496 (strictly below)
- Fill: at the third print, fill price = 17497 (the limit, not 17496 — no price improvement)

### 4.6 Fees

Per-side fee = `fill_price × fee_rate`, applied at every fill (both opening and closing). Default `fee_rate = 0.00015`; configurable. The default bundles slippage and tax into a single number; the fill simulator does **not** apply additional slippage on top.

### 4.7 Session handling

TXF sessions:
- **DAY**: 08:45 – 13:45 (Taiwan time)
- **NIGHT**: 15:00 – 05:00 next day

Configuration option `forced_close_on_session_end`:
- **True** → open position is closed at the end of each session; PnL is reported separately for DAY and NIGHT.
- **False** → positions carry across sessions; PnL is reported as a single unified series.

### 4.8 Warm-up

Forecaster declares `warmup_bars`. Environment replays ticks during this period but the Strategy is disabled (no forecasts, no orders). Trading enables once `warmup_bars` of history has accumulated.

**Why:** most time-series models (Toto2 included) need a context window of past bars to condition on; predicting from too little history yields noise. Letting the strategy trade during that period would take real positions and pay real fees on those noisy forecasts, contaminating the PnL evaluation. Replaying ticks during warm-up (rather than skipping them) keeps the history buffer building up in chronological order, so the moment `warmup_bars` is reached the forecaster sees a realistic context exactly as it would in live trading — no artificial jump-start.

### 4.9 Report generation

After every backtest completes, the log writer automatically invokes the chart generator on its own log bundle. The same generator is also exposed as a standalone CLI so reports can be regenerated from existing logs without re-running the backtest:

```bash
python -m forecast_eval.reports <backtest_output_dir>
```

`<backtest_output_dir>` is the directory containing the parquet log bundle (`fills`, `orders`, `forecasts`, `signals`, `trades`). The generator reads the bundle and writes three PNGs (`equity_drawdown.png`, `price_fills.png`, `signal_vs_realized.png`) under `<dir>/report/`, plus a combined interactive `report.html` at the bundle root (when `plotly` is installed).

For `compare_models.py`, one report is produced per checkpoint subdirectory automatically via the same hook.

**Why automatic:** the report is the actual product of a run — leaving it as a manual follow-up step means stale or missing charts whenever someone forgets. Coupling generation to backtest completion guarantees every log bundle has a matching report. Keeping the CLI form separate means the expensive backtest does not need to be re-run when the report format changes.

Configuration knob: `reporting.auto = true` (default) runs the report after each backtest. Set to `false` to skip — useful when sweeping configs where only aggregate numbers (e.g. `summary.csv`) matter.

## 5. Configuration

A run is fully specified by a config object. Example YAML:

```yaml
data:
  path: data/txf_2024.parquet
  tick_size: 1.0
  contract_multiplier: 200          # NT$ per index point

forecaster:
  type: toto2
  warmup_bars: 200
  forecast_stride_bars: 12          # e.g., 1 forecast per 60 min on 5-min bars
  forecast_horizon_bars: 12
  model_config: { ... }

signal_emitter:
  type: threshold
  buy_threshold: 0.001
  sell_threshold: -0.001

trader:
  aggression_ticks: 3
  max_position: 1
  fee_rate: 0.00015
  forced_close_on_session_end: false

exit_rule:
  type: null                        # or "stop_loss" / "time_stop"
  # stop_loss_ticks: 20
  # time_stop_bars: 24

execution:
  fill_mode: default                # default = marketable+passive rules from §4.5

logging:
  trades_path: outputs/trades.parquet
  forecasts_path: outputs/forecasts.parquet

reporting:
  auto: true                        # auto-generate PNG charts + report.html after each backtest
```

## 6. Implementation Phases

### Phase 1 — Core loop with dummy signal
- Project skeleton, event types, Environment, Execution (both fill rules), Portfolio with session tagging.
- Dummy SignalEmitter (alternates BUY/SELL every N bars) to validate execution without a model.
- Hand-verify fee math on a small set of trades.

### Phase 2 — Forecaster integration
- Abstract Forecaster + warm-up gate.
- Naive baseline forecaster ("predict last price") for end-to-end testing.
- Wire in toto2 as a concrete Forecaster.
- **Look-ahead protection check**: assert Forecaster cannot read past its query timestamp.

### Phase 3 — Risk exits & session polish
- `FixedStopLoss(ticks)` and `TimeStop(bars)` implementations under the `ExitRule` interface.
- `forced_close_on_session_end` handling.
- Edge cases: limit placed at session boundary; warm-up incomplete when first forecast would fire.

### Phase 4 — Metrics & reporting
- Trade / forecast / fill log writers.
- Metrics module computes the full pack (§7).
- Session-bucketed report when `forced_close = True`, unified otherwise.

### Phase 5 — Validation
- Unit tests for fill simulator (golden cases at marketable / passive boundary).
- Integration test with trivial model on known data → expected PnL.
- Buy-and-hold baseline as a floor.
- Fee sensitivity sweep: `fee_rate ∈ {0, 0.00015, 0.0003}`.
- **Model-size sweep** (`compare_models.py`): same data, same emitter, same fee schedule, vary the forecaster checkpoint (Toto-2.0 in 4m / 22m / 313m / 1B / 2.5B). Each checkpoint contributes (a) per-window forecast-quality numbers — direction hit rate, Spearman IC, latency — and (b) a full harness backtest with the standard log bundle (§7 / §8) under a per-checkpoint subdirectory, so trading metrics are produced on the same axis as forecast quality. Lets us see whether bigger checkpoints actually convert better IC into better realized PnL after execution drag.

## 7. Metrics

Reported in every run.

**Trading metrics**
- Total PnL (gross, net)
- Sharpe ratio
- Sortino ratio
- Max drawdown, max drawdown duration
- Hit rate (fraction of trades with positive PnL)
- Average win / average loss
- Number of trades, turnover
- Fee drag (total fees / gross PnL)

**Forecast-quality metrics**
- Per-forecast: predicted direction, realized direction over horizon, predicted vs. realized return
- Direction hit rate (forecast level, not trade level)
- Information coefficient (Spearman correlation of predicted vs. realized return)

**Attribution**
- Signal PnL (frictionless: assume every signal executes at the model-timestamp price with zero fees) vs. realized PnL.
- The gap is execution + cost drag — how much trader plumbing eats model edge.

When `forced_close_on_session_end = True`, all of the above are reported separately for DAY and NIGHT.

## 8. Project Structure

```
forecast_eval/
  events.py
  data/
    loader.py
  environment.py
  forecaster/
    base.py
    naive.py
    toto2.py
  strategy/
    base.py
    dummy.py                 # Phase 1 alternating emitter
    threshold.py
  trader.py
  execution.py
  portfolio.py
  exits/
    base.py
    stop_loss.py
    time_stop.py
  logging_io.py
  metrics.py
  reports.py
  run.py                     # demos + run_backtest()
  real_data_demo.py          # entry point — Toto2 on real TXF 1-min bars
  compare_models.py          # entry point — model-size sweep (Phase 5)
  test_toto2.py              # smoke test — load checkpoint + one forecast
tests/
  conftest.py                # sys.path shim so absolute imports work from any CWD
  test_execution.py
  test_trader.py
  test_metrics.py
  test_integration.py
  test_lookahead.py
```

## 9. Validation Milestones

- **After Phase 1**: replay one TXF session with the dummy signal; eyeball each trade and verify fees against a hand calc.
- **After Phase 2**: assert no look-ahead leak in Forecaster; naive baseline produces approximately break-even minus fees.
- **After Phase 4**: produce a full report on a non-trivial config; verify `signal_pnl > realized_pnl` by at least the cumulative fee amount, with the gap intuitively traceable to turnover.
- **After Phase 5**: model-size sweep across all five Toto-2.0 checkpoints produces a `summary.csv` with both forecast quality (hit rate, Spearman IC) and realized backtest metrics (`bt_net_pnl_points`, `bt_n_trades`, `bt_sharpe_per_trade`, `bt_max_drawdown_pts`) on the same row, plus a parquet log bundle per checkpoint. Confirms the harness is reusable across model variants without per-checkpoint changes.

## 10. Open Questions / Future Work

- **Roll-over / continuous contracts.** v1 assumes a single near-month contract per run. Continuous-contract stitching is deferred.
- **Order book data.** If quote data becomes available, the passive fill rule can be replaced with proper queue-position modeling.
- **Pyramiding.** `max_position > 1` is supported in the state machine but not exercised in v1.
- **Additional exit rules.** Trailing stop, take-profit, volatility-scaled stops are easy additions under the existing `ExitRule` interface.
- **Live shadow mode.** Same components driving paper trading against a live tick feed.
