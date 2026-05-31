# usage.md

Practical guide for running `forecast_eval`. For the architecture and design rationale see [SPEC.md](SPEC.md); for environment / interpreter notes see [CLAUDE.md](CLAUDE.md).

> **The harness** — used throughout this doc — refers to the backtest engine in [run.py](run.py) (`run_backtest()` and its per-tick event loop wiring `Environment → Execution → Trader → Forecaster → SignalEmitter → Portfolio`). Calling `Toto2Forecaster.forecast()` directly bypasses the harness — no orders, no fills, no PnL accounting. [compare_models.py](compare_models.py) now does **both** per checkpoint: a fast forecast-quality eval (direct `.forecast()` calls) *and* a full harness backtest (so parquet artifacts of fills / trades / signals / orders / forecasts get written). Pass `--skip-backtest` for the forecast-only mode.

All commands below assume your shell is in **`myPaper/`** (the parent of this folder). The `paper` conda env Python is at:

```
C:\Users\squirrel\.conda\envs\paper\python.exe
```

It is the only interpreter that has `toto2`, `lightning`, and `pytest` installed. Substitute `python` below with that full path whenever you touch the model or tests.

---

## 1. Quick orientation

There are **four entry points** plus a test suite:

| Entry point | What it does | Needs Toto2? |
|---|---|---|
| [run.py](run.py) — `python -m forecast_eval.run` | Runs Phases 1–4 demos back-to-back on synthetic ticks | No |
| [test_toto2.py](test_toto2.py) — `python -m forecast_eval.test_toto2` | One-shot smoke test: load checkpoint, run one forecast | Yes |
| [real_data_demo.py](real_data_demo.py) — `python -m forecast_eval.real_data_demo` | Full backtest of Toto2 on real TXF 1-min bars | Yes |
| [compare_models.py](compare_models.py) — `python -m forecast_eval.compare_models` | Sweep all 5 Toto2 sizes on the same data; per-window forecast eval **plus** a full backtest with parquet artifacts per checkpoint | Yes |
| `pytest forecast_eval/tests/` | 33 unit + integration tests (~0.3 s) | No |

Reports / charts are generated **automatically** at the end of every backtest by [reports.py](reports.py) (via the `auto_report=True` hook in `write_all`). You can also re-run [reports.py](reports.py) on any existing run directory to regenerate them — see §7.

---

## 2. Running tests

```bash
python -m pytest forecast_eval/tests/ -v
```

Run a single file or test:

```bash
python -m pytest forecast_eval/tests/test_execution.py -v
python -m pytest forecast_eval/tests/test_execution.py::test_marketable_buy_fills_at_next_print_price -v
```

The suite covers the no-flip rule, marketable / passive fill rules, look-ahead defense, round-trip pairing, metric math, and end-to-end PnL on known data. See [tests/](tests/) for what's covered.

---

## 3. Synthetic backtest demos ([run.py](run.py))

Synthetic random-walk ticks; no model, no real data, runs in seconds. Useful for sanity-checking changes to the trader / execution / metrics code.

```bash
python -m forecast_eval.run
```

Prints summaries for four demos:

1. **Phase 1** — dummy alternating signal (no forecaster)
2. **Phase 2** — `NaiveLastPrice` forecaster + `ThresholdEmitter`
3. **Phase 3** — alternating + 5-tick fixed stop-loss
4. **Phase 3** — alternating + DAY↔NIGHT forced session close

The script also calls `demo_with_logs_and_metrics()` which writes a full artifact bundle to `myPaper/outputs/phase4_demo_<ts>/` (see §6).

To call individual demos from Python:

```python
from forecast_eval.run import demo, demo_naive, demo_stop_loss, demo_forced_close
res = demo_naive()
print(res.summary())
```

---

## 4. Toto2 smoke test ([test_toto2.py](test_toto2.py))

The smallest possible end-to-end check that Toto2 is wired up correctly. Downloads `Datadog/Toto-2.0-313m` on first run (~600 MB), then runs one forecast on a 512-row synthetic random walk:

```bash
python -m forecast_eval.test_toto2
```

Expected: a printed `=== Forecast ===` block with `last_price`, `predicted_price`, `predicted_return`, and the first / last 5 values of the `median_path`. If you see `[IMPORT ERROR]`, install `torch` and the local `toto2` package (see [CLAUDE.md](CLAUDE.md)).

Use this whenever you change [forecaster/toto2.py](forecaster/toto2.py) before running the slow real-data demo.

---

## 5. Real-data backtest ([real_data_demo.py](real_data_demo.py))

End-to-end backtest of Toto2 on real TXF 1-min OHLC data. Defaults to 6000 bars of `dataset/tick/TXF_OHLC_1min.csv`, context_length=3008, horizon=30, matching the toto2 notebook configuration.

```bash
# defaults (313m checkpoint, 6000 bars)
python -m forecast_eval.real_data_demo

# tweak thresholds
python -m forecast_eval.real_data_demo \
    --buy-threshold 0.0002 --sell-threshold -0.0002

# different checkpoint + more bars
python -m forecast_eval.real_data_demo \
    --n-bars 8000 --checkpoint Datadog/Toto-2.0-313m

# enable session-boundary forced close
python -m forecast_eval.real_data_demo --forced-close
```

Key CLI flags (see `--help` for the full list):

| Flag | Default | Meaning |
|---|---|---|
| `--data` | `dataset/tick/TXF_OHLC_1min.csv` | Source 1-min CSV |
| `--n-bars` | 6000 | Trailing rows to use |
| `--checkpoint` | `Datadog/Toto-2.0-313m` | HuggingFace checkpoint id |
| `--context-length` | 3008 | Bars fed to the model per inference |
| `--horizon-bars` | 30 | Forecast horizon |
| `--stride-bars` | 30 | Re-forecast every N bars |
| `--buy-threshold` | 0.0005 | Predicted return above this → BUY |
| `--sell-threshold` | -0.0005 | Predicted return below this → SELL |
| `--fee-rate` | 0.00015 | Per-side rate |
| `--forced-close` | False | Flatten at DAY↔NIGHT boundaries |
| `--output-dir` | (auto) | Defaults to `myPaper/outputs/real_data_demo_<ts>/` |

Output: parquet artifacts + `params.json` under `myPaper/outputs/real_data_demo_<ts>/` (see §6) and three blocks of metrics printed to stdout (trading, forecast quality, attribution, plus a buy-and-hold floor).

---

## 6. Output layout

Every run that calls [logging_io.write_all](logging_io.py) writes the same five (or six) files:

```
myPaper/outputs/<run_name>_<YYYYMMDD_HHMMSS>/
    fills.parquet       # every executed FillEvent
    orders.parquet      # every OrderEvent submitted
    forecasts.parquet   # every Forecast (timestamp, horizon, predicted_return, ...)
    signals.parquet     # every SignalEmitter output + emit-time price
    trades.parquet      # round-trips (entry+exit pairs) with PnL
    params.json         # all hyperparameters that produced the run
```

Parquet falls back to CSV transparently if no parquet engine is installed ([logging_io.py:_write](logging_io.py)).

Read an artifact in pandas:

```python
import pandas as pd
trades = pd.read_parquet("outputs/real_data_demo_20260525_003535/trades.parquet")
trades.head()
```

---

## 7. Generating charts from a run ([reports.py](reports.py))

Charts are produced **automatically** as the last step of every backtest (SPEC §4.9) — every call to `logging_io.write_all(...)` runs `reports.generate_report(...)` on the just-written log bundle. You only need the CLI below to **re-render** an existing run (e.g. after editing chart code, or to add a `--data` overlay that wasn't passed originally).

Three chart groups (PNG + interactive HTML):

* **equity_drawdown** — cumulative net PnL with underwater drawdown
* **price_fills** — price series with BUY / SELL fill markers
* **signal_vs_realized** — frictionless signal PnL vs realized PnL (the gap is execution + cost drag)

```bash
# re-render from an existing run dir
python -m forecast_eval.reports outputs/real_data_demo_20260525_003535

# overlay the real price series on the price-fills chart
python -m forecast_eval.reports outputs/real_data_demo_20260525_003535 \
    --data dataset/tick/TXF_OHLC_1min.csv
```

Output:

```
<run_dir>/report/
    equity_drawdown.png
    price_fills.png
    signal_vs_realized.png
<run_dir>/report.html        # all three plotly figures in one file
```

Open `report.html` in a browser for interactive zoom / hover.

To skip auto-generation (e.g. headless sweeps where only aggregate numbers matter), pass `auto_report=False` to `write_all`.

---

## 8. Model-size sweep ([compare_models.py](compare_models.py))

Compares all five Datadog Toto-2.0 checkpoints (4m / 22m / 313m / 1B / 2.5B) on the same TXF 1-min bars. For each checkpoint it runs **two** passes on the same data:

1. **Forecast-quality eval** — N evenly-spaced windows: predicted return vs. realized return, inference latency.
2. **Full backtest** — runs the harness (`run_backtest` + `ThresholdEmitter`) and writes the standard parquet bundle (`fills`, `orders`, `forecasts`, `signals`, `trades`) to a per-checkpoint subdirectory. Trading metrics get added to `summary.csv` as `bt_*` columns.

```bash
# defaults: all 5 checkpoints, 20 windows, 6000 bars, backtest ON
python -m forecast_eval.compare_models

# quick smoke test (two smallest checkpoints, few windows)
python -m forecast_eval.compare_models \
    --n-windows 5 --checkpoints Datadog/Toto-2.0-4m Datadog/Toto-2.0-22m

# more windows on a larger window of bars
python -m forecast_eval.compare_models --n-windows 30 --n-bars 8000

# tune the backtest thresholds (mirrors real_data_demo.py)
python -m forecast_eval.compare_models \
    --buy-threshold 0.0002 --sell-threshold -0.0002 --forced-close

# forecast-quality only — skip the harness pass, no parquet artifacts
python -m forecast_eval.compare_models --skip-backtest
```

Extra CLI flags for the backtest pass (mirroring [real_data_demo.py](real_data_demo.py)): `--buy-threshold`, `--sell-threshold`, `--fee-rate`, `--stride-bars`, `--aggression-ticks`, `--contract-multiplier`, `--forced-close`, `--skip-backtest`.

Output to `myPaper/outputs/compare_models_<ts>/`:

```
compare_models_<ts>/
    per_window.csv             # one row per (checkpoint × window)
    summary.csv                # one row per checkpoint (forecast quality + bt_* metrics)
    params.json                # hyperparameters + per-checkpoint failures
    Datadog_Toto-2.0-4m/       # one subdir per checkpoint (slashes -> underscores)
        fills.parquet
        orders.parquet
        forecasts.parquet
        signals.parquet
        trades.parquet
        params.json
        report.html            # auto-generated charts (interactive)
        report/                # auto-generated PNGs
            equity_drawdown.png
            price_fills.png
            signal_vs_realized.png
    Datadog_Toto-2.0-22m/
        ...
```

`summary.csv` columns:

* Forecast quality: `n_windows`, `load_s`, `mean_latency_s`, `direction_hit_rate`, `spearman_ic`, `mean_abs_return_err`, `mean_pred_return`, `mean_real_return`
* Backtest (omitted under `--skip-backtest`): `bt_n_trades`, `bt_net_pnl_points`, `bt_sharpe_per_trade`, `bt_max_drawdown_pts`, `bt_realized_net_portfolio`, `bt_elapsed_s`, `bt_output_dir`

VRAM is released between checkpoints (`del fc + torch.cuda.empty_cache()`), so the sweep runs sequentially without OOM on a single GPU. The same `Toto2Forecaster` instance serves both the forecast-quality eval and the backtest within one checkpoint (no double model load); a fresh instance is built per checkpoint.

Charts under each checkpoint subdir are produced automatically (price-fills uses `--data` from the sweep's CLI as the background). To re-render after the fact:

```bash
python -m forecast_eval.reports outputs/compare_models_<ts>/Datadog_Toto-2.0-313m \
    --data dataset/tick/TXF_OHLC_1min.csv
```

Note: the 1B and 2.5B checkpoints together pull ~5 GB from HuggingFace on first run.

---

## 9. Programmatic use — Toto2 without the harness

If you want to drive Toto2 directly (the "way #3" pattern used by [compare_models.py](compare_models.py)):

```python
import pandas as pd
from forecast_eval.forecaster.toto2 import Toto2Forecaster

bars = pd.read_csv("dataset/tick/TXF_OHLC_1min.csv").dropna(subset=["close"])
bars = bars.tail(6000).reset_index(drop=True)
bars = pd.DataFrame({
    "timestamp": pd.to_datetime(bars["timestamp"]),
    "price": bars["close"].astype(float),
    "volume": 1,
})

fc = Toto2Forecaster(
    warmup_bars=3008,
    forecast_stride_bars=30,
    forecast_horizon_bars=30,
    context_length=3008,
    checkpoint="Datadog/Toto-2.0-313m",
    device="auto",
    bar_freq=None,        # bars already at 1-min resolution
    signal_step="last",
)

forecast = fc.forecast(bars.iloc[:3008])
p = forecast.payload
print(f"last_price       = {p['last_price']:.2f}")
print(f"predicted_price  = {p['predicted_price']:.2f}")
print(f"predicted_return = {p['predicted_return']:+.5f}")
print(f"median_path[-5:] = {p['median_path'][-5:]}")
```

`fc.forecast(history_df)` is **stateless externally** — the model cache lives on the instance, but every call takes a fresh history slice. To run a custom backtest, hand the same `Toto2Forecaster` to `run_backtest(..., forecaster=fc)` from [run.py](run.py).

---

## 10. Plugging in your own forecaster or emitter

The harness only sees abstract base classes — concrete model and strategy implementations are swappable.

**Custom forecaster.** Subclass [forecaster/base.py:Forecaster](forecaster/base.py) and implement `forecast(history_df) -> Forecast`. Look-ahead is enforced at runtime: returning a `forecast.timestamp > history_df["timestamp"].iloc[-1]` raises. See [forecaster/naive.py](forecaster/naive.py) for a minimal example and [forecaster/toto2.py](forecaster/toto2.py) for a model adapter.

**Custom emitter.** Subclass [strategy/base.py:SignalEmitter](strategy/base.py) and implement `emit(forecast) -> {"BUY", "SELL", "HOLD"}`. The trader handles the rest (no-flip rule, pending-order lifetime). See [strategy/threshold.py](strategy/threshold.py).

**Custom exit rule.** Subclass [exits/base.py:ExitRule](exits/base.py). Examples: [exits/stop_loss.py](exits/stop_loss.py), [exits/time_stop.py](exits/time_stop.py).

Pass instances into `run_backtest()`:

```python
from forecast_eval.run import run_backtest
res = run_backtest(
    ticks,
    forecaster=MyForecaster(...),
    emitter=MyEmitter(...),
    exit_rule=MyExitRule(...),
    tick_size=1.0, aggression_ticks=3, fee_rate=0.00015,
    forced_close_on_session_end=True,
)
```

For sweeps where the emitter or forecaster carries per-run state (iterator cycles, model caches), use [run.py:fee_sensitivity_sweep](run.py)'s `make_emitter=` / `make_forecaster=` factory pattern — passing instances directly will silently reuse state across iterations.

---

## 11. Computing metrics standalone

Given a `BacktestResult` from `run_backtest()`:

```python
from forecast_eval.metrics import compute_metrics, buy_and_hold_pnl

metrics = compute_metrics(res, ticks, forced_close=False)
print(metrics["trading"])      # round-trip Sharpe/Sortino, fee drag, drawdown
print(metrics["forecast"])     # hit rate, Spearman IC
print(metrics["attribution"])  # signal PnL - realized PnL = execution drag

print(buy_and_hold_pnl(ticks, fee_rate=0.00015))
```

When `forced_close=True`, `metrics["trading"]` is replaced by `metrics["trading_day"]` + `metrics["trading_night"]`. See [metrics.py](metrics.py) for what each field means.

---

## 12. Common gotchas

* **Run from `myPaper/`, not from inside `forecast_eval/`.** The `python -m forecast_eval.xxx` form requires the parent. Direct execution (`python real_data_demo.py`) only works for files with the `sys.path.insert(parent.parent)` trampoline at the top.
* **Use the `paper` env Python (full path) for anything touching `toto2`, `lightning`, or `pytest`.** There is no `conda activate` in the default shell.
* **The 1-min TXF file is ~17 % NaN rows** (session gaps / halts). [real_data_demo.py:load_minute_bars](real_data_demo.py) drops them — Toto2 propagates NaN otherwise.
* **`context_length` must divide the model's `patch_size`** ([forecaster/toto2.py](forecaster/toto2.py) truncates automatically, but if you see a shape mismatch this is why).
* **No-flip rule** ([SPEC.md §4.2](SPEC.md)): a SELL while long closes only — it does not flip to short. Same for BUY while short. Pyramiding is also disabled.
* **Reusing stateful emitters / forecasters across sweep iterations is silently wrong.** Use factories (`make_emitter=lambda: ...`) for any object with per-run state.
